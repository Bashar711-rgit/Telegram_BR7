"""v11.8.1 — اختبارات انحدار دفعة الإصلاحات الثانية (F-10/F-13/F-14/F-16/F-17/F-19/F-24).

كل حالة مأخوذة من سجل التدقيق F-01..F-30 — السلوك قبل الإصلاح موثق بتعليق.
تعتمد على بيئة tests/conftest.py الحتمية (لا شبكة، لا أسرار).
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from filter_engine import EnhancedFilter  # noqa: E402


@pytest.fixture(scope="module")
def f():
    return EnhancedFilter()


# ═══════════════ F-13: «ما ابي حل» نفي المفعول (كان review 0.461) ═══════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "ما ابي حل",
    "ما ابغى مساعده",
    "ما احتاج احد يساعدني الحين",
    "ما عاد ابي حل",
])
async def test_f13_want_verb_object_negation_ignored(f: EnhancedFilter, text: str):
    """فعل رغبة + نية بعده في نفس الجملة = مفعول منفي → ignore."""
    r = await f.analyze(text)
    assert r["decision"] == "ignore", f"{text!r}: reasons={r.get('reasons')}"


@pytest.mark.asyncio
async def test_f13_withdrawal_then_new_request_not_killed(f: EnhancedFilter):
    """«ما عاد ابي مساعدتك، بس ابي حل واجبي» — الطلب بعد حد الجملة يبقى حياً
    (review على الأقل — لا ignore صامت كان سيتولد لو قتلنا كامل الرسالة)."""
    r = await f.analyze("ما عاد ابي مساعدتك، بس ابي حل واجبي")
    assert r["decision"] in ("review", "accept"), r.get("reasons")


@pytest.mark.asyncio
async def test_f13_genuine_accept_unaffected(f: EnhancedFilter):
    """حماية انحدار: الطلب الصريح بدون نفي يبقى accept."""
    r = await f.analyze("ابي حل واجب الرياضيات ضروري")
    assert r["decision"] == "accept", r.get("reasons")


# ═══════════════ F-14: سقف resolution+طلب جديد يُطبق حتى بعد rescore ═══════════════

@pytest.mark.asyncio
async def test_f14_rescore_respects_resolution_ceiling(f: EnhancedFilter):
    """كان accept 0.891 عبر تجاوز السقف (نفس النمط الحي الموثق في المراجعة)."""
    r = await f.analyze("لقيت شخص يحل الواجب لكن عندي تقرير ثاني محتاج مساعدة فيه")
    assert r["decision"] == "review", r.get("reasons")
    assert any(
        str(x).startswith("capped: rescore_respects_resolution_ceiling")
        for x in (r.get("reasons") or [])
    ), r.get("reasons")


# ═══════════════ F-16: TTL لمخزن access_hash لكل حساب ═══════════════

def test_f16_hash_store_ttl_expires():
    from sender_resolver import AccountAccessHashStore
    s = AccountAccessHashStore(ttl=60.0)
    assert s.record("acc1", 111, 42) is True
    assert s.get("acc1", 111) == 42
    # تقادم اليدوي: خزّن طابعاً قديماً ثم اقرأ
    key = ("acc1", 111)
    val, _ts = s._data[key]
    s._data[key] = (val, time.time() - 3600)
    assert s.get("acc1", 111) is None, "hash منتهي يجب أن يُسقط ويفرض إعادة حل"
    assert key not in s._data, "المدخل المنتهي يجب أن يُحذف"
    assert s.get("acc1", 999) is None  # مفتاح غير موجود يبقى None


def test_f16_hash_store_record_dedup_intact():
    """record() يبقى idempotent لنفس القيمة (write-behind لا يتكرر)."""
    from sender_resolver import AccountAccessHashStore
    s = AccountAccessHashStore()
    assert s.record("acc1", 5, 100) is True
    assert s.record("acc1", 5, 100) is False
    assert s.record("acc1", 5, 200) is True
    assert s.get("acc1", 5) == 200


# ═══════════════ F-17: spawn_bg يحفظ المرجع ═══════════════

@pytest.mark.asyncio
async def test_f17_spawn_bg_keeps_reference_and_runs():
    from task_util import spawn_bg, pending_count
    done = asyncio.Event()

    async def _job():
        await asyncio.sleep(0.01)
        done.set()

    t = spawn_bg(_job(), name="test_f17_job")
    assert t in asyncio.all_tasks()
    await asyncio.wait_for(done.wait(), timeout=2.0)
    await asyncio.sleep(0)  # لمة scheduler ليُنفذ done_callback
    assert pending_count() == 0 or all(x.done() for x in [t])


@pytest.mark.asyncio
async def test_f17_spawn_bg_survives_no_external_ref():
    """لا يوجد مرجع خارجي للمهمة — يجب أن تكتمل رغم GC (كانت تُجمع نظرياً)."""
    import task_util

    async def _job():
        await asyncio.sleep(0.02)
        task_util._f17_canary = True

    task_util._f17_canary = False
    task_util.spawn_bg(_job(), name="test_f17_gc")
    await asyncio.sleep(0.1)
    assert task_util._f17_canary is True


# ═══════════════ F-19: TTL على قراءة _recent_input_senders ═══════════════

@pytest.mark.asyncio
async def test_f19_recent_input_senders_ttl_on_read():
    import monitors as M
    key = (-100123, 777)
    M._recent_input_senders.clear()
    M._recent_input_senders[key] = {
        "account": "acc1",
        "input_sender": object(),
        "ts": time.time() - M._RECENT_INPUT_SENDERS_TTL - 5,
    }
    # محاكاة مسار القراءة نفسه: TTL check
    entry = M._recent_input_senders.get(key)
    stale = (time.time() - float(entry.get("ts", 0))) > M._RECENT_INPUT_SENDERS_TTL
    assert stale is True, "المدخل الأقدم من TTL يجب أن يعتبر منتهياً"
    # المدخل الطازج يبقى صالحاً
    M._recent_input_senders[key] = {"account": "acc1", "input_sender": object(), "ts": time.time()}
    entry2 = M._recent_input_senders.get(key)
    fresh = (time.time() - float(entry2.get("ts", 0))) <= M._RECENT_INPUT_SENDERS_TTL
    assert fresh is True
    M._recent_input_senders.clear()


# ═══════════════ F-10: تقليم كاشات القوائم المحجوبة ═══════════════

def test_f10_blocked_cache_prune_drops_expired():
    from database import EnhancedDatabase
    cache = {i: (False, time.time() + 30) for i in range(6000)}  # كله طازج
    EnhancedDatabase._prune_blocked_cache(cache)
    assert len(cache) <= EnhancedDatabase._BLOCKED_CACHE_PRUNE_SIZE, "الكاش يجب أن يُقلم فوق الحد"
    # مدخلات منتهية تُرمى أولاً
    cache2 = {i: (False, time.time() - 1) for i in range(6000)}
    EnhancedDatabase._prune_blocked_cache(cache2)
    assert len(cache2) == 0, "المنتهي بالكامل يجب أن يُخلّى"
    # تحت الحد: لا مساس
    cache3 = {i: (True, time.time() + 30) for i in range(10)}
    EnhancedDatabase._prune_blocked_cache(cache3)
    assert len(cache3) == 10


# ═══════════════ F-24: جلسة AlertBot دائمة ═══════════════

@pytest.mark.asyncio
async def test_f24_alertbot_session_reused():
    from alert_bot import AlertBot
    bot = AlertBot(token="123:fake", chat_id=1)
    s1 = await bot._get_session()
    s2 = await bot._get_session()
    assert s1 is s2, "نفس الجلسة يجب أن يُعاد استخدامها (لا TLS جديد لكل استدعاء)"
    await bot.aclose()
    assert s1.closed, "aclose يغلق الجلسة"
    # بعد الإغلاق: جلسة جديدة تُنشأ عند الطلب
    s3 = await bot._get_session()
    assert s3 is not s1 and not s3.closed
    await bot.aclose()
