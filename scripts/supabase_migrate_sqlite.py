#!/usr/bin/env python3
"""supabase_migrate_sqlite.py — v16.3.0: ترحيل بيانات SQLite إلى Supabase PostgreSQL.

الغرض: نقل كل البيانات الدائمة من قاعدة SQLite محلية إلى Supabase PG
(قاعدة المشروع الدائمة) بشكل Idempotent — لا تكرار ولا فقد:
  * مطابقة الأعمدة بين SQLite والمخطط الحي في PG (تقاطع الأسماء).
  * INSERT ... ON CONFLICT DO NOTHING — إعادة التشغيل آمنة بلا ازدواج.
  * ضبط Sequences بعد الكتابة (setval إلى max(id)) لمنع تعارض PK المستقبل.
  * تحقق أعداد قبل/بعد لكل جدول.
  * الوضع الافتراضي DRY_RUN (جرد وتقرير بلا كتابة) — --apply للتنفيذ الفعلي.

أمان إلزامي: --apply يرفض العمل ما لم تُمرَّر --backup-manifest بمسار
manifest.json صالح (حصيلة نسخة احتياطية قبل التعديل — عقد المالك).

الاستخدام (لا تلصق DATABASE_URL في أي محادثة/سجل):
    DATABASE_URL='postgresql://...' python scripts/supabase_migrate_sqlite.py \
        --sqlite telegram_bot.db                        # DRY_RUN افتراضي
    DATABASE_URL='...' python scripts/supabase_migrate_sqlite.py \
        --sqlite telegram_bot.db --apply \
        --backup-manifest /tmp/br7_backup/backup_X/manifest.json

EXIT: 0 = نجاح | 1 = فشل (السبب مطبوع).
"""

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

SKIP_SQLITE_SYSTEM = {"sqlite_sequence", "sqlite_stat1", "sqlite_stat4"}

# أنواع PG التي تتطلب تحويل قيم SQLite النصية/الرقمية
_TS_TYPES = {"timestamp without time zone", "timestamp with time zone", "date"}


def _coerce(data_type: str, v):
    """تحويل قيمة SQLite إلى نوع العمود في PG (asyncpg صارم في الأنواع)."""
    try:
        if v is None:
            return None
        if data_type in _TS_TYPES:
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    return None
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                return datetime.fromisoformat(s)
            return v
        if data_type == "boolean":
            if isinstance(v, (int, float)):
                return bool(v)
            if isinstance(v, str):
                return v.lower() in ("1", "t", "true", "y", "yes")
            return v
        if data_type in ("integer", "bigint", "smallint") and isinstance(v, str):
            return int(v)
        if data_type in ("real", "double precision", "numeric") and isinstance(v, str):
            return float(v)
        return v
    except Exception:
        return v


async def _connect(dsn: str):
    import asyncpg
    return await asyncpg.connect(
        dsn, statement_cache_size=0, timeout=20, command_timeout=60
    )


def _sqlite_tables(path: str) -> list:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def _sqlite_cols(path: str, table: str) -> list:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    finally:
        con.close()


def _sqlite_fetch_batches(path: str, table: str, cols: list, batch: int):
    """قراءة دفعات من SQLite — قارئ مفتوح لكل جدول مع COMMIT التزامني."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.execute("PRAGMA read_uncommitted=ON")
    try:
        cur = con.execute(f'SELECT {", ".join(chr(34)+c+chr(34) for c in cols)} '
                          f'FROM "{table}"')
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                break
            yield rows
    finally:
        con.close()


async def main() -> int:
    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        print("FAIL: DATABASE_URL env var is empty", file=sys.stderr)
        return 1

    args = sys.argv[1:]
    apply = "--apply" in args
    sqlite_path = None
    manifest_path = None
    if "--sqlite" in args:
        sqlite_path = args[args.index("--sqlite") + 1]
    if "--backup-manifest" in args:
        manifest_path = args[args.index("--backup-manifest") + 1]

    if not sqlite_path or not os.path.exists(sqlite_path):
        print(f"FAIL: ملف SQLite غير موجود: {sqlite_path}", file=sys.stderr)
        return 1

    mode = "APPLY" if apply else "DRY_RUN"
    if apply:
        if not manifest_path or not os.path.exists(manifest_path):
            print("FAIL: --apply يتطلب --backup-manifest بمسار manifest.json "
                  "صالح (نسخة احتياطية قبل التعديل — عقد إلزامي).", file=sys.stderr)
            return 1
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                man = json.load(f)
            if not man.get("tables"):
                raise ValueError("manifest بلا جداول")
            print(f"نسخة احتياطية معتمدة: {manifest_path} "
                  f"(فحص: {man.get('verdict')}, جداول: {len(man['tables'])})")
        except Exception as e:
            print(f"FAIL: manifest غير صالح: {e}", file=sys.stderr)
            return 1

    conn = None
    try:
        conn = await _connect(dsn)
    except Exception as e:
        print(f"FAIL: connect: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        return 1

    report: dict = {
        "tool": "supabase_migrate_sqlite.py",
        "version": "v16.3.0",
        "mode": mode,
        "sqlite_source": os.path.abspath(sqlite_path),
        "backup_manifest": manifest_path,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "tables": {},
        "skipped": [],
        "failures": [],
    }

    try:
        # جرد PG الحي
        pg_tables = {r["table_name"] for r in await conn.fetch(
            "select table_name from information_schema.tables "
            "where table_schema='public' and table_type='BASE TABLE'")}
        pg_cols: dict = {}
        pg_types: dict = {}
        for r in await conn.fetch(
            "select table_name, column_name, data_type from information_schema.columns "
            "where table_schema='public'"):
            pg_cols.setdefault(r["table_name"], set()).add(r["column_name"])
            pg_types.setdefault(r["table_name"], {})[r["column_name"]] = r["data_type"]

        src_tables = [t for t in _sqlite_tables(sqlite_path)
                      if t not in SKIP_SQLITE_SYSTEM]
        print(f"المصدر SQLite: {len(src_tables)} جدولاً | الوضع: {mode}")

        total_inserted = 0
        for t in src_tables:
            if t not in pg_tables:
                report["skipped"].append(
                    {"table": t, "reason": "غير موجود في Supabase public"})
                print(f"  SKIP {t:>30}: غير موجود في PG (ينشئه إقلاع التطبيق)")
                continue
            s_cols = _sqlite_cols(sqlite_path, t)
            common = [c for c in s_cols if c in pg_cols.get(t, set())]
            dropped = [c for c in s_cols if c not in pg_cols.get(t, set())]
            if not common:
                report["skipped"].append(
                    {"table": t, "reason": "لا أعمدة مشتركة"})
                continue

            src_count = sqlite3.connect(sqlite_path).execute(
                f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            dst_before = await conn.fetchval(f'select count(*) from "{t}"')

            entry = {
                "source_rows": int(src_count),
                "pg_rows_before": int(dst_before),
                "columns_used": common,
                "columns_dropped": dropped,
            }

            if not apply or int(src_count) == 0:
                entry["pg_rows_after"] = int(dst_before)
                entry["inserted"] = 0
                if apply:
                    note = "بلا صفوف في المصدر" if src_count == 0 else ""
                    if note:
                        entry["note"] = note
                report["tables"][t] = entry
                print(f"  {'PLAN' if not apply else 'OK  '} {t:>30}: "
                      f"{src_count:,} → {dst_before:,} (موجود)")
                continue

            inserted = 0
            placeholders = ", ".join(f"${i+1}" for i in range(len(common)))
            col_list = ", ".join(f'"{c}"' for c in common)
            sql = (f'INSERT INTO "{t}" ({col_list}) VALUES ({placeholders}) '
                   f"ON CONFLICT DO NOTHING")
            t_types = pg_types.get(t, {})
            try:
                for rows in _sqlite_fetch_batches(sqlite_path, t, common, 500):
                    # تحويل الأنواع حسب أعمدة PG (asyncpg صارم — SQLite متساهل:
                    # يخزن التواريخ نصاً والأرقام قد تأتي نصاً)
                    cleaned = []
                    for r in rows:
                        vals = []
                        for c, v in zip(common, r):
                            if isinstance(v, bytes):
                                try:
                                    v = v.decode("utf-8")
                                except Exception:
                                    pass
                            v = _coerce(t_types.get(c, "text"), v)
                            vals.append(v)
                        cleaned.append(tuple(vals))
                    await conn.executemany(sql, cleaned)
                    inserted += len(cleaned)

                # ضبط الـSequence للجداول ذات PK رقمي "id" (منع تعارض مستقبلي)
                # حارس: pg_get_serial_sequence يرفع خطأ إن لم يوجد العمود
                if "id" in t_types:
                    try:
                        seq = await conn.fetchval(
                            "select pg_get_serial_sequence($1, $2)", t, "id")
                        if seq:
                            mx = await conn.fetchval(
                                f'select coalesce(max(id), 0) from "{t}"')
                            await conn.execute(
                                "select setval($1, $2, true)", seq, int(mx or 1))
                            entry["sequence_set"] = {"seq": seq, "to": int(mx or 1)}
                    except Exception as se:
                        entry["sequence_set"] = {
                            "skipped": f"{type(se).__name__}: {str(se)[:120]}"}

                dst_after = await conn.fetchval(f'select count(*) from "{t}"')
                entry["inserted"] = inserted
                entry["pg_rows_after"] = int(dst_after)
                entry["verified_delta"] = int(dst_after) - int(dst_before)
                total_inserted += inserted
                report["tables"][t] = entry
                print(f"  {'DONE' if apply else 'PLAN'} {t:>30}: "
                      f"{src_count:,} ← مصدر | +{inserted:,} "
                      f"({dst_before:,} → {dst_after:,})")
            except Exception as e:
                report["failures"].append(
                    {"table": t, "error": f"{type(e).__name__}: {str(e)[:200]}"})
                print(f"  ERR  {t:>30}: {type(e).__name__}: {str(e)[:140]}",
                      file=sys.stderr)

        report["total_inserted"] = total_inserted
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        report["verdict"] = (
            "MIGRATED" if apply and not report["failures"]
            else "PLANNED" if not apply and not report["failures"]
            else "PARTIAL")

        out_json = os.path.join(
            os.path.dirname(os.path.abspath(sqlite_path)),
            f"migrate_report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json")
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1, default=str)
        print("=" * 60)
        print(f"النتيجة: {report['verdict']} | مجموع المُدرج: "
              f"{total_inserted:,} | تقرير: {out_json}")
        return 0 if not report["failures"] else 1
    finally:
        if conn:
            await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
