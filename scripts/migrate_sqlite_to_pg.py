#!/usr/bin/env python3
"""v11.8 — SQLite→PostgreSQL migration runner (execution phase 6).

Sources accepted (any one):
  --from-sqlite PATH   a SQLite database file (e.g. an exported bot.db)
  --from-json PATH     an export_critical_data() JSON payload
  --from-dashboard URL production dashboard export endpoint (needs
                       DASHBOARD_AUTH_TOKEN in env — never logged)

Target: DATABASE_URL (Supabase PostgreSQL) in env, or --dsn-file to read
the connection string from a file (never passed on CLI/chat).

What it does:
  1. connect target, verify engine == postgresql
  2. import critical tables (app_settings, sources, rules,
     allowed_entities) via the app's own import_critical_data()
  3. verify row counts before/after and print a report

NO secrets are printed. Exit 0 only when counts reconcile.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

os.environ.setdefault("TARGET_GROUP_ID", "-1001234567890")
os.environ.setdefault("ADMIN_CHAT_ID", "654321")
os.environ.setdefault("DASHBOARD_AUTH_TOKEN", os.environ.get("DASHBOARD_AUTH_TOKEN", "x" * 32))
os.environ["LOG_FILE"] = os.environ.get("LOG_FILE", "/tmp/_migrate.log")
os.environ["DASHBOARD_ENABLED"] = "false"
os.environ["ALERT_BOT_TOKEN"] = ""


def load_payload(args) -> dict:
    if args.from_json:
        return json.loads(Path(args.from_json).read_text())
    if args.from_sqlite:
        import sqlite3
        from database import CRITICAL_EXPORT_TABLES

        payload = {"version": 1, "exported_at": 0, "tables": {}}
        conn = sqlite3.connect(f"file:{args.from_sqlite}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        for table, cols in CRITICAL_EXPORT_TABLES.items():
            if table == "keywords_json" or not cols:
                continue
            try:
                rows = [dict(r) for r in conn.execute(
                    f"SELECT {', '.join(cols)} FROM {table}")]
                payload["tables"][table] = rows
            except Exception as e:
                print(f"WARN source table {table}: {type(e).__name__}")
                payload["tables"][table] = []
        conn.close()
        return payload
    if args.from_dashboard:
        import urllib.request
        req = urllib.request.Request(
            args.from_dashboard.rstrip("/") + "/api/export/critical",
            headers={"Authorization": f"Bearer {os.environ['DASHBOARD_AUTH_TOKEN']}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    raise SystemExit("one of --from-sqlite/--from-json/--from-dashboard is required")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-sqlite")
    ap.add_argument("--from-json")
    ap.add_argument("--from-dashboard")
    ap.add_argument("--dsn-file", help="file containing the PostgreSQL URI (one line)")
    args = ap.parse_args()

    if args.dsn_file:
        os.environ["DATABASE_URL"] = Path(args.dsn_file).read_text().strip()

    if not os.getenv("DATABASE_URL"):
        print("ERROR: DATABASE_URL missing (env or --dsn-file)")
        return 2

    payload = load_payload(args)
    tables = payload.get("tables") or {}
    src_counts = {t: len(rows) for t, rows in tables.items()}
    print("source counts:", src_counts)

    from config import CFG
    from database import EnhancedDatabase

    db = EnhancedDatabase()
    ok = await db.connect()
    if not ok or db.db_type != "postgresql":
        print(f"ERROR: target engine is {db.db_type!r}, expected postgresql")
        return 2
    print("target: postgresql connected")

    counts = await db.import_critical_data(payload, replace=False)
    print("import result:", counts)

    # verify
    all_ok = True
    for table, n_src in src_counts.items():
        try:
            row = await db._fetchone(f"SELECT COUNT(*) AS n FROM {table}")
            n_dst = int(row["n"]) if row else 0
        except Exception as e:
            print(f"VERIFY {table}: ERROR {type(e).__name__}")
            all_ok = False
            continue
        status = "OK" if n_dst >= n_src else "MISMATCH"
        if n_dst < n_src:
            all_ok = False
        print(f"VERIFY {table}: src={n_src} dst={n_dst} {status}")

    await db.close()
    print("MIGRATION", "VERIFIED" if all_ok else "INCOMPLETE")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
