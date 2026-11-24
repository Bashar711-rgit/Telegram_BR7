#!/usr/bin/env python3
"""supabase_backup.py — v16.3.0: نسخة احتياطية منطقية كاملة من Supabase PostgreSQL.

الغرض: نسخة احتياطية إلزامية قبل أي تعديل على قاعدة البيانات (RLS/مخطط/
ترحيل) — تقرأ كل جداول public وتصدّر كل صف إلى ملفات gz مضغوطة مع
manifest يحمل الأعداد وحصائل SHA256، قراءة-فقط على قاعدة البيانات.

لماذا لا pg_dump؟ بيئة التنفيذ لا تملك أدوات psql/pg_dump، والاتصال
المتاح عبر Pooler — asyncpg COPY يقرأ البيانات كاملة عبر الجلسة دون
أي اعتماد خارجي (نفس المكتبة التي يعمل بها التطبيق).

الاستخدام (لا تلصق DATABASE_URL في أي محادثة/سجل):
    DATABASE_URL='postgresql://...' python scripts/supabase_backup.py
    DATABASE_URL='...' python scripts/supabase_backup.py --out /tmp/br7_backup

الخرج:
    <out>/backup_YYYYMMDD_HHMMSS/<table>.csv.gz   — بيانات كل جدول (COPY CSV)
    <out>/backup_YYYYMMDD_HHMMSS/manifest.json    — الأعداد + SHA256 + الطوابع

EXIT: 0 = نجاح كامل | 1 = فشل جزئي/كلي (التفاصيل في manifest وstderr).
"""

import asyncio
import gzip
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone

EXCLUDE_TABLES = set()  # لا استثناء — كل جداول public تُنسخ


async def _connect(dsn: str):
    import asyncpg
    return await asyncpg.connect(
        dsn, statement_cache_size=0, timeout=20, command_timeout=120
    )


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def main() -> int:
    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        print("FAIL: DATABASE_URL env var is empty", file=sys.stderr)
        return 1

    out_arg = "/tmp/br7_backup"
    if "--out" in sys.argv:
        out_arg = sys.argv[sys.argv.index("--out") + 1]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(out_arg, f"backup_{stamp}")
    os.makedirs(backup_dir, exist_ok=True)

    conn = None
    try:
        conn = await _connect(dsn)
    except Exception as e:
        print(f"FAIL: connect: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        return 1

    manifest: dict = {
        "tool": "supabase_backup.py",
        "version": "v16.3.0",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_copy",
        "target": "public schema (all base tables)",
        "server": None,
        "tables": {},
        "failures": [],
    }

    failed = 0
    try:
        ver = await conn.fetchval("select version()")
        manifest["server"] = str(ver)[:120]

        rows = await conn.fetch(
            "select table_name from information_schema.tables "
            "where table_schema='public' and table_type='BASE TABLE' "
            "order by table_name"
        )
        tables = [r["table_name"] for r in rows if r["table_name"] not in EXCLUDE_TABLES]
        print(f"نسخ {len(tables)} جدولاً إلى {backup_dir}")

        for t in tables:
            gz_path = os.path.join(backup_dir, f"{t}.csv.gz")
            try:
                count = await conn.fetchval(f'select count(*) from "{t}"')
                buf = io.BytesIO()
                # COPY TO STDOUT (CSV, HEADER) — قراءة خام أسرع وأدق من صف-بصف
                # (asyncpg copy_from_table يكتب في output: write-callable/ملف)
                await conn.copy_from_table(
                    t, schema_name="public", format="csv", header=True,
                    output=buf,
                )
                with gzip.open(gz_path, "wb", compresslevel=6) as f:
                    f.write(buf.getvalue())
                digest = _sha256_file(gz_path)
                manifest["tables"][t] = {
                    "rows": int(count),
                    "file": os.path.basename(gz_path),
                    "bytes_gz": os.path.getsize(gz_path),
                    "sha256_gz": digest,
                }
                print(f"  OK  {t:>30}: {int(count):>8,} صف  "
                      f"({os.path.getsize(gz_path):,}B gz)")
            except Exception as e:
                failed += 1
                manifest["failures"].append(
                    {"table": t, "error": f"{type(e).__name__}: {str(e)[:200]}"})
                print(f"  ERR {t:>30}: {type(e).__name__}: {str(e)[:120]}",
                      file=sys.stderr)

        manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        manifest["total_rows"] = sum(
            v.get("rows", 0) for v in manifest["tables"].values()
            if isinstance(v, dict))
        manifest["verdict"] = "COMPLETE" if failed == 0 else "PARTIAL"

        mp = os.path.join(backup_dir, "manifest.json")
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1, default=str)

        print("=" * 60)
        print(f"النتيجة: {manifest['verdict']} — إجمالي الصفوف: "
              f"{manifest.get('total_rows', 0):,} | فشل: {failed}")
        print(f"manifest: {mp}")
        return 0 if failed == 0 else 1
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        manifest["failures"].append({"table": "*", "error": str(e)[:300]})
        with open(os.path.join(backup_dir, "manifest.json"), "w",
                  encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1, default=str)
        return 1
    finally:
        if conn:
            await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
