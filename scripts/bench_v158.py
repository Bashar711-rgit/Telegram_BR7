# -*- coding: utf-8 -*-
"""scripts/bench_v158.py — قياسات v15.8.0 قبل/بعد (أدلة رقمية، لا ادعاءات)

القياس أ):
  مسار الـack: ack فردي (commit لكل حدث — المسار القديم) مقابل
  ack_queue_events_batch دفعي (المسار الجديد) — فوق EnhancedDatabase
  حقيقي (SQLite محلي) مع عدّ commits الفعلي لكل مسار.

القياس ب):
  FastGate: أتمتة Python الخالصة مقابل pyahocorasick (C) — نفس مجموعة
  الأنماط ونفس النصوص، قياس زمن المسح لكل 1000 نص.

ملاحظة صدق: القرص المحلي أسرع من قرص Render الحي (db_latency المقاسة
913ms على SELECT 1) — نسبة تقليل الـcommits هي المؤشر المحمول الحقيقي،
لأن زمن كل commit على Render يقاس بالمئات من المللي-ثانية.
"""

from __future__ import annotations

import asyncio
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ.setdefault("TARGET_GROUP_ID", "-1001234567890")
os.environ.setdefault("ADMIN_CHAT_ID", "654321")
os.environ.setdefault("DB_TYPE", "sqlite")
os.environ.setdefault("MESSAGE_QUEUE_SIZE", "5000")  # فوق حجم القياس — بلا إخلاء
os.environ.pop("DATABASE_URL", None)
os.environ["DB_FILE"] = "/tmp/bench_v158.db"
TEST_DB = Path("/tmp/bench_v158.db")
if TEST_DB.exists():
    TEST_DB.unlink()

N_EVENTS = 1200
POP_BATCH = 16


async def bench_ack_paths() -> None:
    from database import EnhancedDatabase

    db = EnhancedDatabase()
    db._writer_task = None
    async def _no_writer():
        return None
    db.start_writer = _no_writer
    assert await db.connect() is True

    # عدّاد commits حقيقي — على اتصال aiosqlite نفسه ليغطي كل المسارات
    # (add/pop/ack تستدعي conn.commit مباشرة وليس _commit())
    commit_counter = {"n": 0}
    _conn = db._sqlite_conn
    _orig_conn_commit = _conn.commit

    async def counting_conn_commit():
        commit_counter["n"] += 1
        await _orig_conn_commit()

    _conn.commit = counting_conn_commit  # type: ignore

    # تعبئة أحداث
    for i in range(N_EVENTS):
        await db.add_to_queue({"text": f"event number {i} نص عربي", "i": i}, priority=5)
    assert await db.queue_size() == N_EVENTS

    print(f"═ اك فردي (القديم): {N_EVENTS} حدثاً")
    commit_counter["n"] = 0
    t0 = time.perf_counter()
    claimed = 0
    while True:
        batch = await db.pop_batch_from_queue(POP_BATCH)
        if not batch:
            break
        claimed += len(batch)
        for item in batch:
            await db.ack_queue_event(item["_queue_id"])  # commit لكل حدث
    t_single = time.perf_counter() - t0
    c_single = commit_counter["n"]
    print(f"  زمن: {t_single:.3f}s | معدل: {claimed/t_single:.1f} حدث/ث | commits: {c_single}")

    # إعادة تعبئة
    for i in range(N_EVENTS):
        await db.add_to_queue({"text": f"event number {i} نص عربي", "i": i}, priority=5)

    print(f"═ اك دفعي (v15.8.0): {N_EVENTS} حدثاً")
    commit_counter["n"] = 0
    t0 = time.perf_counter()
    claimed = 0
    pending: list = []
    while True:
        batch = await db.pop_batch_from_queue(POP_BATCH)
        if not batch:
            break
        claimed += len(batch)
        for item in batch:
            pending.append(item["_queue_id"])
        if len(pending) >= 512:
            await db.ack_queue_events_batch(pending)
            pending = []
    if pending:
        await db.ack_queue_events_batch(pending)
    t_batch = time.perf_counter() - t0
    c_batch = commit_counter["n"]
    print(f"  زمن: {t_batch:.3f}s | معدل: {claimed/t_batch:.1f} حدث/ث | commits: {c_batch}")

    print(f"═ الفرق: زمن {t_single/t_batch:.1f}x أسرع | commits {c_single}→{c_batch} "
          f"({100*(1-c_batch/max(1,c_single)):.0f}% أقل)")
    await db.close()


def bench_fastgate_engines() -> None:
    from fast_gate import PyAhoCorasick, _PyacAutomaton

    rnd = random.Random(42)
    # ~8000 نمط واقعي (عربي/إنجليزي) — نفس حجم المجموعة الحية
    arabic_words = ["شراء", "بيع", "استفسار", "حل", "واجب", "بحث", "تقرير",
                    "مستوى", "اختبار", "منهج", "محاضرة", "ملخص", "مراجعة",
                    "واجبات", "أعمال", "سنتر", "مركز", "الجامعة", "المعهد"]
    patterns = set()
    for i in range(8000):
        base = rnd.choice(arabic_words) if i % 2 == 0 else (
            "".join(rnd.choices("abcdefghijklmnopqrstuvwxyz", k=rnd.randint(4, 12)))
        )
        patterns.add(f"{base}{i}" if len(patterns) >= len(arabic_words) * 400 else base)
    patterns = {p for p in patterns if p}

    # 3000 نص (30% يحتوي نمطاً)
    texts = []
    plist = sorted(patterns)
    for i in range(3000):
        words = ["سلام", "اليوم", "عندي", "سؤال", "بخصوص", "المادة", "الدكتورة",
                 "قالت", "ان", "في", "زمن", "الواجب", "القادم", "يوجد", "تفاصيل"]
        body = " ".join(rnd.choices(words, k=rnd.randint(6, 30)))
        if i % 10 < 3:
            body += " " + rnd.choice(plist)
        texts.append(body)

    print(f"═ FastGate: {len(patterns)} نمطاً × {len(texts)} نصاً")
    t0 = time.perf_counter()
    py_ac = PyAhoCorasick()
    for p in patterns:
        py_ac.add(p)
    py_ac.build()
    t_build_py = time.perf_counter() - t0

    t0 = time.perf_counter()
    c_ac = _PyacAutomaton(patterns)
    t_build_c = time.perf_counter() - t0

    t0 = time.perf_counter()
    hits_py = sum(1 for t in texts if py_ac.any_hit(t))
    t_scan_py = time.perf_counter() - t0

    t0 = time.perf_counter()
    hits_c = sum(1 for t in texts if c_ac.any_hit(t))
    t_scan_c = time.perf_counter() - t0

    assert hits_py == hits_c, f"نتيجة مختلفة! py={hits_py} c={hits_c}"
    print(f"  بناء: python {t_build_py*1000:.0f}ms | C {t_build_c*1000:.0f}ms")
    print(f"  إصابات متطابقة: {hits_py}/{len(texts)} (تكافؤ القرار ✔)")
    print(f"  مسح: python {t_scan_py*1000:.0f}ms ({1000*len(texts)/t_scan_py/1000:.0f} نص/ث) "
          f"| C {t_scan_c*1000:.0f}ms ({1000*len(texts)/t_scan_c/1000:.0f} نص/ث)")
    print(f"  التسريع: {t_scan_py/t_scan_c:.1f}x")


if __name__ == "__main__":
    print("══════════ قياس أ: مسار الـack (EnhancedDatabase حقيقي) ══════════")
    asyncio.run(bench_ack_paths())
    print()
    print("══════════ قياس ب: FastGate — python-ac مقابل C ══════════")
    bench_fastgate_engines()
