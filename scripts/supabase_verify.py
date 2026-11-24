#!/usr/bin/env python3
"""supabase_verify.py — v16.3.0: تدقيق حي قراءة-فقط لقاعدة Supabase PostgreSQL.

الغرض: التحقق الفعلي من أن Supabase قاعدة البيانات الدائمة للمشروع —
المخطط الموجود مقابل المتوقع من database.py (v16.3.0)، الأعمدة الحرجة،
الفهارس، حالة RLS، صحة الاتصال وزمن الاستجابة، ودور الاتصال.

الاستخدام (لا تلصق DATABASE_URL في أي محادثة/سجل):
    DATABASE_URL='postgresql://...' python scripts/supabase_verify.py
    DATABASE_URL='...' python scripts/supabase_verify.py --json   # خروج JSON

EXIT: 0 = مطابق | 1 = انحراف/فشل (التفاصيل في التقرير).
قراءة-فقط: لا يكتب أي صف ولا يعدل أي جدول — الاستعلامات SELECT/information_schema حصراً.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

# الجداول المتوقعة من database.py v16.3.0 (_create_tables + _migrate_panel_auth)
EXPECTED_TABLES = {
    "messages", "alerts", "sender_stats", "sender_contacts", "system_health",
    "blocked_senders", "blocked_chats", "processing_queue", "dead_letters",
    "alert_dedup", "spam_watch", "spam_ignore", "sources", "rules",
    "allowed_entities", "features", "notifications", "audit_logs",
    "panel_users", "panel_sessions", "panel_accounts",
    "telegram_usernames_history", "users", "group_memberships",
    "group_messages", "message_analysis", "user_activity", "sender_access",
    "sender_account_hashes", "app_settings",
}

# أسماء قديمة محتملة (v15.x وأقدم) — وجودها يُوثق، غيابها ليس فشلاً
LEGACY_TABLES = {
    "dashboard_users", "dashboard_accounts", "dashboard_sessions",
    "filter_decisions",
}

# أعمدة حرجة يجب وجودها — مطابقة حرفية لـ database.py v16.3.0
# (alerts: إيصال الاسترداد message_hash؛ alert_dedup: البصمة fingerprint)
REQUIRED_COLUMNS = {
    "processing_queue": {"id", "event_data", "priority", "created_at",
                         "status", "claimed_at", "attempts"},
    "messages": {"id", "message_hash", "chat_id", "sender_id",
                 "message_text", "timestamp"},
    "alerts": {"id", "message_hash", "chat_id", "sender_id",
               "account_name", "keyword", "alert_text", "timestamp"},
    "alert_dedup": {"fingerprint", "sender_id", "first_seen", "last_seen"},
    "panel_accounts": {"id", "phone", "session_encrypted", "status"},
    "panel_users": {"id", "username", "password_hash", "role"},
    "panel_sessions": {"token_hash", "user_id", "expires_at"},
    "audit_logs": {"id", "actor", "action", "created_at"},
    "dead_letters": {"id", "event_data", "error_text", "retry_count"},
}

# فهارس حرجة على مسارات القراءة الساخنة — الأسماء الحرفية من _create_indexes
EXPECTED_INDEXES = {
    "messages": ["idx_msg_hash", "idx_msg_time", "idx_msg_sender_time"],
    "alerts": ["idx_alr_time", "idx_alr_keyword", "idx_alr_decision"],
    "processing_queue": ["idx_queue_prio", "idx_queue_status"],
    "audit_logs": ["idx_audit_time", "idx_audit_action"],
}

CRITICAL_TABLES_FOR_COUNT = [
    "messages", "alerts", "alert_dedup", "processing_queue",
    "dead_letters", "panel_accounts", "panel_users", "audit_logs",
]


async def _connect(dsn: str):
    import asyncpg
    return await asyncpg.connect(
        dsn, statement_cache_size=0, timeout=20, command_timeout=30
    )


async def main() -> int:
    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        print("FAIL: DATABASE_URL env var is empty", file=sys.stderr)
        return 1

    report: dict = {
        "tool": "supabase_verify.py",
        "version": "v16.3.0",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "checks": {},
        "drift": [],
        "notes": [],
    }

    conn = None
    try:
        conn = await _connect(dsn)
    except Exception as e:
        report["error"] = f"connect: {type(e).__name__}: {str(e)[:300]}"
        _emit(report)
        return 1

    try:
        # 1) هوية الخادم ودور الاتصال
        ver = await conn.fetchval("select version()")
        report["checks"]["server"] = str(ver)[:120]
        me = await conn.fetchrow(
            "select current_user, session_user, "
            "(select rolsuper from pg_roles where rolname = current_user) as is_super"
        )
        report["checks"]["role"] = {
            "current_user": me["current_user"],
            "session_user": me["session_user"],
            "is_superuser": bool(me["is_super"]),
        }
        if me["current_user"] not in ("br7_app", "postgres"):
            report["notes"].append(
                f"دور الاتصال غير متوقع: {me['current_user']} (الموثق: br7_app)")

        # 2) زمن الاستجابة — 5 pings
        import time as _t
        lat = []
        for _ in range(5):
            t0 = _t.perf_counter()
            await conn.fetchval("select 1")
            lat.append(round((_t.perf_counter() - t0) * 1000, 1))
        report["checks"]["latency_ms"] = {"pings": lat, "avg": round(sum(lat) / len(lat), 1)}

        # 3) جرد الجداول في public
        rows = await conn.fetch(
            "select table_name from information_schema.tables "
            "where table_schema='public' and table_type='BASE TABLE' "
            "order by table_name"
        )
        actual = {r["table_name"] for r in rows}
        report["checks"]["tables"] = {
            "expected": len(EXPECTED_TABLES),
            "actual_total": len(actual),
            "missing": sorted(EXPECTED_TABLES - actual),
            "legacy_present": sorted(LEGACY_TABLES & actual),
            "extra_unknown": sorted(
                actual - EXPECTED_TABLES - LEGACY_TABLES),
        }
        if report["checks"]["tables"]["missing"]:
            report["drift"].append(
                f"جداول ناقصة: {report['checks']['tables']['missing']}")

        # 4) أعداد الصفوف (الحرجة + البقية المتاحة)
        counts = {}
        for t in sorted(actual):
            if t.startswith("_") or t.startswith("pg_"):
                continue
            try:
                counts[t] = await conn.fetchval(f'select count(*) from "{t}"')
            except Exception as e:
                counts[t] = f"ERR:{type(e).__name__}"
        report["checks"]["row_counts"] = counts
        missing_counts = [t for t in CRITICAL_TABLES_FOR_COUNT if t not in actual]
        if missing_counts:
            report["drift"].append(f"جداول حرجة بلا عدّ: {missing_counts}")

        # 5) الأعمدة الحرجة لكل جدول مطلوب
        cols_rows = await conn.fetch(
            "select table_name, column_name from information_schema.columns "
            "where table_schema='public'"
        )
        cols: dict = {}
        for r in cols_rows:
            cols.setdefault(r["table_name"], set()).add(r["column_name"])
        col_report = {}
        for t, need in REQUIRED_COLUMNS.items():
            if t not in actual:
                col_report[t] = "TABLE_MISSING"
                continue
            have = cols.get(t, set())
            missing_cols = sorted(need - have)
            col_report[t] = {"ok": not missing_cols,
                             "missing_columns": missing_cols}
            if missing_cols:
                report["drift"].append(
                    f"أعمدة ناقصة في {t}: {missing_cols}")
        report["checks"]["required_columns"] = col_report

        # 6) الفهارس الحرجة
        idx_rows = await conn.fetch(
            "select tablename, indexname from pg_indexes "
            "where schemaname='public'"
        )
        idx: dict = {}
        for r in idx_rows:
            idx.setdefault(r["tablename"], set()).add(r["indexname"])
        idx_report = {}
        for t, want in EXPECTED_INDEXES.items():
            if t not in actual:
                continue
            have = idx.get(t, set())
            missing_idx = sorted(set(want) - have)
            idx_report[t] = {
                "indexes_found": sorted(have),
                "missing": missing_idx,
            }
            if missing_idx:
                report["drift"].append(
                    f"فهارس ناقصة في {t}: {missing_idx}")
        report["checks"]["indexes"] = idx_report

        # 7) حالة RLS والسياسات
        rls_rows = await conn.fetch(
            "select c.relname as table_name, c.relrowsecurity as rls_enabled, "
            "c.relforcerowsecurity as rls_forced "
            "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname='public' and c.relkind='r'"
        )
        pol_rows = await conn.fetch(
            "select tablename, policyname from pg_policies "
            "where schemaname='public' order by tablename"
        )
        pol: dict = {}
        for r in pol_rows:
            pol.setdefault(r["tablename"], []).append(r["policyname"])
        rls_report = {}
        for r in rls_rows:
            t = r["table_name"]
            rls_report[t] = {
                "enabled": bool(r["rls_enabled"]),
                "forced": bool(r["rls_forced"]),
                "policies": pol.get(t, []),
            }
        report["checks"]["rls"] = rls_report
        unprotected = [t for t in sorted(EXPECTED_TABLES & actual)
                       if not rls_report.get(t, {}).get("enabled")]
        if unprotected:
            report["drift"].append(
                f"جداول بلا RLS: {unprotected} — شغّل scripts/supabase_rls_v16.sql")

        # 8) المخططات (رصد مخطط br7 القديم الموازي — D-1)
        sch = await conn.fetch(
            "select nspname from pg_namespace "
            "where nspname not in ('pg_catalog','information_schema') "
            "and nspname !~ '^pg_toast' order by nspname"
        )
        report["checks"]["schemas"] = [r["nspname"] for r in sch]

        # 9) Pooler check — التقديم عبر server_version + مستخدم
        sv = await conn.fetchval("show server_version_num")
        report["checks"]["pg_version_num"] = str(sv)

        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        report["verdict"] = "MATCH" if not report["drift"] else "DRIFT"
        _emit(report)
        return 0 if not report["drift"] else 1
    except Exception as e:
        report["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        _emit(report)
        return 1
    finally:
        if conn:
            await conn.close()


def _emit(report: dict) -> None:
    as_json = "--json" in sys.argv
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
        return
    c = report.get("checks", {})
    print("=" * 64)
    print("SUPABASE VERIFY — تقرير حي قراءة-فقط")
    print("=" * 64)
    print(f"الخادم: {report.get('checks', {}).get('server', report.get('error', '?'))}")
    role = c.get("role", {})
    print(f"الدور: {role.get('current_user')} (super={role.get('is_superuser')})")
    lat = c.get("latency_ms", {})
    print(f"زمن الاستجابة: pings={lat.get('pings')} متوسط={lat.get('avg')}ms")
    t = c.get("tables", {})
    print(f"الجداول: متوقع={t.get('expected')} موجود={t.get('actual_total')} "
          f"ناقص={t.get('missing')} قديم={t.get('legacy_present')} "
          f"غير معروف={t.get('extra_unknown')}")
    rc = c.get("row_counts", {})
    if rc:
        print("أعداد الصفوف:")
        for k in sorted(rc):
            print(f"  {k:>28}: {rc[k]}")
    colr = c.get("required_columns", {})
    bad_cols = {k: v for k, v in colr.items()
                if v == "TABLE_MISSING" or not (v or {}).get("ok", True)}
    print(f"الأعمدة الحرجة: {'سليمة' if not bad_cols else bad_cols}")
    rlsr = c.get("rls", {})
    unprotected = [k for k, v in rlsr.items() if not v.get("enabled")]
    print(f"RLS: {len(rlsr)} جدولاً — بلا حماية: {unprotected or 'لا شيء'}")
    print(f"المخططات: {c.get('schemas')}")
    print("-" * 64)
    if report.get("drift"):
        print("انحرافات:")
        for d in report["drift"]:
            print(f"  ! {d}")
    print(f"الحكم: {report.get('verdict', 'ERROR')}")
    if report.get("error"):
        print(f"خطأ: {report['error']}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
