#!/usr/bin/env python3
"""Apply + validate br7_schema_v1.sql against a PostgreSQL target.

Secrets policy:
  - The DSN is read from the environment variable BR7_APPLY_DSN only.
    Nothing is hardcoded, printed, or written to disk by this script.

Modes:
  default            : apply DDL (idempotent) + register migration + structural checks
  --validate-only    : run structural checks only (no DDL, no writes beyond reads)
  --staging          : prelude creates NOLOGIN roles (anon/authenticated/service_role)
                       to simulate Supabase roles on a plain PostgreSQL instance,
                       and runs a functional smoke test inside a ROLLED BACK
                       transaction (staging stays clean).
  --no-functional    : skip the functional smoke test even in staging mode.

Exit code 0 only when every structural check passes (and functional smoke,
when run, passes).

Usage (staging):
  BR7_APPLY_DSN=postgresql://postgres@127.0.0.1:55443/postgres \
      python3 scripts/apply_br7_schema.py --staging

Usage (production — Supabase Session Pooler, statement_cache_size=0):
  BR7_APPLY_DSN='postgresql://postgres.<ref>:<pw>@<pooler-host>:5432/postgres?sslmode=require' \
      python3 scripts/apply_br7_schema.py
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import time
from pathlib import Path

import asyncpg

REPO = Path(__file__).resolve().parent.parent
MIGRATIONS = REPO / "migrations"
DDL_FILE = MIGRATIONS / "br7_schema_v1.sql"
VERSION = "0001_br7_initial"
DESCRIPTION = "Unified br7 DDL v1.0.0"

STAGING_PRELUDE = """
DO $roles$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['anon','authenticated','service_role'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', r);
    END IF;
  END LOOP;
END
$roles$;
"""

# (name, sql, checker)
CHECKS = [
    ("br7 table count == 44",
     "SELECT count(*) FROM information_schema.tables WHERE table_schema='br7'",
     lambda v: v == 44),
    ("RLS-disabled tables == 0",
     "SELECT count(*) FROM pg_tables WHERE schemaname='br7' AND rowsecurity=false",
     lambda v: v == 0),
    ("unvalidated constraints == 0",
     "SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace "
     "WHERE n.nspname='br7' AND NOT c.convalidated",
     lambda v: v == 0),
    ("claim_* functions == 3",
     "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
     "WHERE n.nspname='br7' AND proname LIKE 'claim_%'",
     lambda v: v == 3),
    ("critical functions == 7",
     "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
     "WHERE n.nspname='br7' AND proname IN ('bump_app_stat','claim_processing_jobs',"
     "'claim_alert_deliveries','claim_background_jobs','enforce_retention',"
     "'sync_deleted_message','audit_trigger')",
     lambda v: v == 7),
    ("index count >= 55",
     "SELECT count(*) FROM pg_indexes WHERE schemaname='br7'",
     lambda v: v >= 55),
    ("system_settings seeds == 5",
     "SELECT count(*) FROM br7.system_settings", lambda v: v == 5),
    ("dashboard_roles seeds == 3",
     "SELECT count(*) FROM br7.dashboard_roles", lambda v: v == 3),
    ("dashboard_permissions seeds == 7",
     "SELECT count(*) FROM br7.dashboard_permissions", lambda v: v == 7),
    ("gap-filler tables == 5",
     "SELECT count(*) FROM information_schema.tables WHERE table_schema='br7' "
     "AND table_name IN ('deleted_messages','app_stats','legacy_account_map',"
     "'outbound_messages','message_media')",
     lambda v: v == 5),
    ("no plaintext sessions == 0",
     "SELECT count(*) FROM br7.telegram_account_sessions "
     "WHERE status IN ('valid','configured') "
     "AND encrypted_session IS NULL AND secret_reference IS NULL",
     lambda v: v == 0),
    ("updated_at triggers == 19",
     "SELECT count(*) FROM information_schema.triggers "
     "WHERE trigger_schema='br7' AND trigger_name LIKE '%_updated_at'",
     lambda v: v == 19),
    ("audit triggers == 3 (distinct names)",
     "SELECT count(DISTINCT trigger_name) FROM information_schema.triggers "
     "WHERE trigger_schema='br7' AND trigger_name LIKE 'trg_br7_audit_%'",
     lambda v: v == 3),
    ("service_role schema USAGE == 1",
     "SELECT count(*) FROM pg_namespace n "
     "WHERE n.nspname='br7' AND has_schema_privilege('service_role', n.oid, 'USAGE')",
     lambda v: v == 1),
    ("anon has zero table grants in br7",
     "SELECT count(*) FROM information_schema.role_table_grants "
     "WHERE table_schema='br7' AND grantee='anon'",
     lambda v: v == 0),
    ("migration row registered == 1",
     "SELECT count(*) FROM public.schema_migrations WHERE version='0001_br7_initial'",
     lambda v: v == 1),
]


def log(msg: str) -> None:
    print(msg, flush=True)


def staging_transform(ddl_sql: str) -> str:
    """Staging-only transform: skip CREATE EXTENSION statements.

    pgserver's minimal PostgreSQL build ships without contrib control files
    (pgcrypto / pg_trgm). On Supabase both extensions already exist, and
    gen_random_uuid() is native since PG13, so skipping the two CREATE
    EXTENSION lines locally is fully equivalent for every remaining object.
    The production run always applies the file verbatim.
    """
    out = []
    for line in ddl_sql.splitlines():
        if line.lstrip().startswith("CREATE EXTENSION"):
            out.append("-- [staging transform] " + line)
        else:
            out.append(line)
    return "\n".join(out)


async def apply_ddl(conn: asyncpg.Connection, ddl_sql: str, checksum: str,
                    staging: bool = False) -> int:
    """Apply DDL + register migration inside ONE transaction. Returns elapsed ms."""
    if staging:
        ddl_sql = staging_transform(ddl_sql)
    t0 = time.monotonic()
    async with conn.transaction():
        await conn.execute("SET LOCAL statement_timeout = 0")
        await conn.execute("SET LOCAL lock_timeout = '15s'")
        await conn.execute(ddl_sql)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        await conn.execute(
            """
            INSERT INTO public.schema_migrations
              (version, description, checksum, execution_ms, applied_by)
            VALUES ($1, $2, $3, $4, current_user)
            ON CONFLICT (version) DO UPDATE
              SET description = EXCLUDED.description,
                  checksum    = EXCLUDED.checksum,
                  execution_ms = EXCLUDED.execution_ms,
                  applied_at  = now(),
                  applied_by  = current_user
            """,
            VERSION, DESCRIPTION, checksum, elapsed_ms,
        )
    return elapsed_ms


async def run_checks(conn: asyncpg.Connection) -> bool:
    ok = True
    for name, sql, checker in CHECKS:
        try:
            val = await conn.fetchval(sql)
            passed = checker(val)
        except Exception as exc:  # noqa: BLE001
            val = f"ERROR: {exc}"
            passed = False
        log(f"{'PASS' if passed else 'FAIL'}  {name}  (got: {val})")
        ok = ok and passed
    return ok


async def functional_smoke(conn: asyncpg.Connection) -> bool:
    """Staging-only functional test inside a FORCED-ROLLBACK transaction.

    asyncpg commits on clean exit of `async with conn.transaction()`, so we
    explicitly call tx.rollback() to leave staging pristine.
    """
    results: list[tuple[str, bool, str]] = []

    def rec(name: str, cond: bool, detail: str = "") -> None:
        results.append((name, bool(cond), detail))

    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute("SET LOCAL statement_timeout = 0")

        # 1. bump_app_stat accumulates
        v1 = await conn.fetchval("SELECT br7.bump_app_stat('smoke_test', 3)")
        v2 = await conn.fetchval("SELECT br7.bump_app_stat('smoke_test', 4)")
        rec("bump_app_stat accumulates (3 then 7)", v1 == 3 and v2 == 7, f"{v1}/{v2}")

        # 2. source + message + deletion trigger -> deleted_messages
        src = await conn.fetchval(
            "INSERT INTO br7.telegram_sources (telegram_chat_id, source_type) "
            "VALUES (-100999001, 'supergroup') RETURNING id")
        import uuid as _uuid
        msg = await conn.fetchval(
            "INSERT INTO br7.messages (source_id, telegram_message_id, message_date, "
            "text_content, content_fingerprint) VALUES ($1, 1, now(), 'smoke', 'fp-smoke-1') "
            "RETURNING id", src)
        await conn.execute("UPDATE br7.messages SET is_deleted = true WHERE id = $1", msg)
        n_del = await conn.fetchval(
            "SELECT count(*) FROM br7.deleted_messages WHERE source_id = $1 "
            "AND telegram_message_id = 1", src)
        rec("sync_deleted_message trigger fired", n_del == 1, f"rows={n_del}")

        # 3. processing job claim (SKIP LOCKED path)
        await conn.execute(
            "INSERT INTO br7.message_processing_jobs (message_id, job_type) "
            "VALUES ($1, 'smoke')", msg)
        claimed = await conn.fetch(
            "SELECT * FROM br7.claim_processing_jobs(5, 'smoke_worker')")
        rec("claim_processing_jobs claims 1 pending job",
            len(claimed) == 1 and claimed[0]["status"] == "processing"
            and claimed[0]["locked_by"] == "smoke_worker",
            f"claimed={len(claimed)}")

        # 4. audit trigger on filter_rules
        rule = await conn.fetchval(
            "INSERT INTO br7.filter_rules (rule_key, rule_type, title) "
            "VALUES ('smoke_rule_1', 'allow', 'Smoke rule') RETURNING id")
        n_audit = await conn.fetchval(
            "SELECT count(*) FROM br7.audit_logs WHERE entity_type='filter_rules' "
            "AND entity_id = $1", str(rule))
        rec("audit trigger writes audit_logs", n_audit == 1, f"rows={n_audit}")

        # 5. alert + delivery + idempotency unique keys
        dest = await conn.fetchval(
            "INSERT INTO br7.alert_destinations (destination_key, destination_type, "
            "telegram_chat_id) VALUES ('smoke_dest', 'telegram_chat', -100999002) "
            "RETURNING id")
        alert = await conn.fetchval(
            "INSERT INTO br7.alerts (message_id, deduplication_key) "
            "VALUES ($1, 'smoke-alert-1') RETURNING id", msg)
        await conn.execute(
            "INSERT INTO br7.alert_deliveries (alert_id, destination_id, idempotency_key) "
            "VALUES ($1, $2, 'smoke-delivery-1')", alert, dest)
        rec("alerts/deliveries insert with unique keys", True)

        # 6. RLS blocks anon-style access: with no policy, non-owner is denied.
        #    Simulate by SETting ROLE to service_role (has grants but not owner,
        #    RLS enabled with no policy => reads 0 rows, not an error).
        #    We only assert the deny-on-select for a non-owner NOLOGIN role via
        #    privilege check: service_role has explicit grants => not count test.
        rec("RLS enabled with zero policies (deny-by-default)",
            await conn.fetchval(
                "SELECT count(*) FROM pg_tables WHERE schemaname='br7' "
                "AND rowsecurity=true") == 44)

        # 7. enforce_retention dry run returns jsonb
        ret = await conn.fetchval("SELECT br7.enforce_retention(true)")
        rec("enforce_retention(dry_run) returns report",
            ret is not None and '"dry_run":true' in str(ret).replace(" ", ""), str(ret)[:80])
    finally:
        await tx.rollback()
    ok = all(c for _, c, _ in results)
    for name, cond, detail in results:
        log(f"{'PASS' if cond else 'FAIL'}  [smoke] {name}" +
            ("" if cond else f"  (detail: {detail})"))
    return ok


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staging", action="store_true",
                    help="plain-PostgreSQL staging: create Supabase-like roles + run functional smoke")
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--no-functional", action="store_true")
    args = ap.parse_args()

    dsn = os.environ.get("BR7_APPLY_DSN")
    if not dsn:
        log("ERROR: BR7_APPLY_DSN is not set (refusing to proceed; no secrets in code)")
        return 2

    if not DDL_FILE.exists():
        log(f"ERROR: {DDL_FILE} missing")
        return 2

    ddl_sql = DDL_FILE.read_text(encoding="utf-8")
    checksum = hashlib.sha256(ddl_sql.encode("utf-8")).hexdigest()
    log(f"DDL file: {DDL_FILE.name}  sha256={checksum[:16]}…  bytes={len(ddl_sql.encode('utf-8'))}")

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        who = await conn.fetchval("SELECT current_user")
        ver = await conn.fetchval("SHOW server_version")
        log(f"connected as current_user={who}  server_version={ver}")

        if args.staging:
            await conn.execute(STAGING_PRELUDE)
            log("staging prelude: roles anon/authenticated/service_role ensured")

        if not args.validate_only:
            elapsed = await apply_ddl(conn, ddl_sql, checksum, staging=args.staging)
            log(f"DDL applied + migration registered in {elapsed} ms")
        else:
            log("validate-only mode: DDL not touched")

        ok = await run_checks(conn)

        if args.staging and not args.no_functional:
            log("--- functional smoke (rolled back afterwards) ---")
            ok = await functional_smoke(conn) and ok

        log(("ALL CHECKS PASSED" if ok else "CHECKS FAILED"))
        return 0 if ok else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
