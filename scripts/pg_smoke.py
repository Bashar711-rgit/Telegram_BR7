#!/usr/bin/env python3
"""pg_smoke.py — v14.8.0 (P2.3): اختبار دخان اتصال PostgreSQL/Supabase.

يُشغَّل يدوياً من جهاز المالك أو CI بمتغير DATABASE_URL قبل اعتماد أي
ترحيل/نشر — يتحقق من: الاتصال، وON CONFLICT (الصيغة التي يبني عليها
upsert_sender_contact وكل upserts التطبيق)، والتوافق مع Pooler عبر
statement_cache_size=0 (الإعداد ذاته المعتمد في database.py).

الاستخدام (لا تلصق DATABASE_URL في أي محادثة/سجل):
    DATABASE_URL='postgresql://...' python scripts/pg_smoke.py

EXIT: 0 = نجاح | 1 = فشل (السبب مطبوع على stderr).
"""

import asyncio
import os
import sys


async def main() -> int:
    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        print("FAIL: DATABASE_URL env var is empty", file=sys.stderr)
        return 1

    try:
        import asyncpg
    except ImportError:
        print("FAIL: asyncpg not installed (pip install asyncpg)", file=sys.stderr)
        return 1

    conn = None
    try:
        # statement_cache_size=0: نفس إعداد database.py — إلزامي للتوافق
        # مع Session/Transaction Pooler (pgbouncer) في Supabase.
        conn = await asyncpg.connect(
            dsn, statement_cache_size=0, timeout=15
        )
        version = await conn.fetchval("select version()")
        print(f"connect OK: {str(version)[:80]}")

        # ON CONFLICT DO NOTHING/UPDATE — العمود الفقري لكل upserts التطبيق.
        await conn.execute(
            "create temp table _smoke(k text primary key, v int)"
        )
        await conn.execute(
            "insert into _smoke values ('a',1) on conflict do nothing"
        )
        await conn.execute(
            "insert into _smoke values ('a',2) on conflict do nothing"
        )
        v = await conn.fetchval("select v from _smoke where k='a'")
        assert v == 1, f"ON CONFLICT semantics broken: expected 1, got {v}"
        print("OK: connect + ON CONFLICT + statement_cache_size=0")
        return 0
    except Exception as e:
        print(
            f"FAIL: {type(e).__name__}: {str(e)[:300]}",
            file=sys.stderr,
        )
        return 1
    finally:
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
