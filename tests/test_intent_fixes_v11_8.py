"""v11.8 — اختبارات انحدار إصلاحات مرحلة التنفيذ (F-01/F-03..F-08/F-15).

كل حالة هنا مأخوذة من سجل النتائج الحية في التقرير الفني (جدول 13-1/13-2)
أو من أمر التنفيذ — النتائج قبل الإصلاح موثقة كتعليق عند كل اختبار.

يعتمد على بيئة tests/conftest.py الحتمية (لا شبكة، لا أسرار).
"""
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from filter_engine import EnhancedFilter  # noqa: E402


@pytest.fixture(scope="module")
def f():
    return EnhancedFilter()


# ═══════════════ F-01: إعلان بصيغة طلب (كان accept 0.956 حياً) ═══════════════

@pytest.mark.asyncio
async def test_f01_request_shaped_ad_ignored(f: EnhancedFilter):
    """«نقدم حل واجبات جميع المواد اسعار مميزه ابي احد يجرب» كان accept 0.956
    (P0) — بعد الإصلاح: commercial_prefix + تعطيل boost + عقوبة 1.1 → ignore."""
    r = await f.analyze("نقدم حل واجبات جميع المواد اسعار مميزه ابي احد يجرب")
    assert r["decision"] == "ignore", f"reasons={r.get('reasons')}"


@pytest.mark.asyncio
async def test_f01_commercial_prefix_reason_recorded(f: EnhancedFilter):
    r = await f.analyze("نقدم حل واجبات جميع المواد اسعار مميزه ابي احد يجرب")
    reasons = " ".join(r.get("reasons") or [])
    assert "commercial_prefix" in reasons


@pytest.mark.asyncio
async def test_f01_commercial_variants_ignored(f: EnhancedFilter):
    """صيغ تجارية (أمر التنفيذ: «نقدم حل واجبات»/«نوفر حلولًا» + سياق)."""
    for text in (
        "نقدم حل واجبات جميع المواد بأسعار مميزة تواصل الآن",
        "نوفر حلول دراسيه مضمونه اسعار خاصه",
        "نسوي مشاريع تخرج باقل الاسعار",
    ):
        r = await f.analyze(text)
        assert r["decision"] == "ignore", f"{text!r} => {r['decision']}"


@pytest.mark.asyncio
async def test_f01_genuine_collective_not_killed(f: EnhancedFilter):
    """حارس الجمعية: «نحتاج نسوي بحث تخرج» طلب مجموعة حقيقي — لا يُقتل
    بسبب فعل النقدم + سياق الخدمة وحدَه."""
    r = await f.analyze("نحتاج نسوي بحث تخرج مع بعض")
    assert r["decision"] != "ignore", f"reasons={r.get('reasons')}"


# ═══════════════ F-04: «تكفون أحد يساعدني» (كان ignore 0.383 — FN) ═══════════════

@pytest.mark.asyncio
async def test_f04_polite_help_request_reaches_review(f: EnhancedFilter):
    r = await f.analyze("تكفون أحد يساعدني")
    assert r["decision"] in ("review", "accept")


@pytest.mark.asyncio
async def test_f04_needs_help_variant(f: EnhancedFilter):
    """«محتاج أحد يساعدني» كانت review 0.546 — تبقى review+."""
    r = await f.analyze("محتاج أحد يساعدني")
    assert r["decision"] in ("review", "accept")


# ═══════════════ F-05: «من يعرف …» (كانت ignore 0.0 — no_keyword) ═══════════════

@pytest.mark.asyncio
async def test_f05_man_yaaref_family(f: EnhancedFilter):
    for text in ("من يعرف يحلها", "من يعرف شخص يساعدني"):
        r = await f.analyze(text)
        assert r["decision"] in ("review", "accept"), f"{text!r} => {r['decision']}"


@pytest.mark.asyncio
async def test_f05_meen_yaaref_expert_still_accepts(f: EnhancedFilter):
    """pos18 من عقد keywords.json — يجب أن يبقى accept بعد إضافة «مين يعرف»."""
    r = await f.analyze("مين يعرف احد متخصص يسوي بوربوينت لمادة التسويق")
    assert r["decision"] == "accept"


# ═══════════════ F-06: أفعال المساعدة مع الخروج المبكر ═══════════════

@pytest.mark.asyncio
async def test_f06_help_verb_anchors_request(f: EnhancedFilter):
    """«احد يساعدني» بلا أي نية صريحة — كانت تُهمل no_keyword."""
    r = await f.analyze("احد يساعدني")
    assert r["decision"] in ("review", "accept")


@pytest.mark.asyncio
async def test_f06_imperative_help(f: EnhancedFilter):
    r = await f.analyze("ساعدوني في الواجب ضروري")
    assert r["decision"] in ("review", "accept")


@pytest.mark.asyncio
async def test_f06_generic_help_noun_alone_not_anchored(f: EnhancedFilter):
    """كلمة عامة وحدها («مساعدة») لا ترتقي للمراجعة — قيد موثق بوعي."""
    r = await f.analyze("مساعدة")
    assert r["decision"] == "ignore"


# ═══════════════ F-15: «واتساب» بلا سياق لا تُحظر ═══════════════

@pytest.mark.asyncio
async def test_f15_whatsapp_in_real_request_not_blocked(f: EnhancedFilter):
    """كانت reason=advertisement (حظر مطلق لكلمة واتساب) — الآن مسار طلب طبيعي."""
    r = await f.analyze("ابي رقم واتساب الدكتور")
    assert r.get("reason") != "advertisement"
    assert r["decision"] in ("review", "accept")


@pytest.mark.asyncio
async def test_f15_bare_whatsapp_mention_not_hard_blocked(f: EnhancedFilter):
    r = await f.analyze("واتساب: 05xxxxxxxx")
    assert r["decision"] != "accept"  # لا مرور — لكن ليست حظر إعلان صارم
    assert r.get("reason") != "advertisement"


@pytest.mark.asyncio
async def test_f15_commercial_whatsapp_still_blocked(f: EnhancedFilter):
    """الإعلان التجاري بواتساب يبقى محجوباً (neg08 من العقد)."""
    r = await f.analyze("تواصل معنا واتساب لخدمات طلابية مضمونة النتيجة")
    assert r["decision"] == "ignore"


# ═══════════════ F-03/F-09: بوابة الطول (توحيد 6..65) ═══════════════

@pytest.mark.asyncio
async def test_f03_short_request_passes_engine_gate(f: EnhancedFilter):
    """«أبي حل» (6 أحرف) — الأرضية الافتراضية للمحرك 3 ولا تُقتل داخل المحرك."""
    r = await f.analyze("أبي حل")
    assert r.get("reason") not in ("too_short", "too_long")


@pytest.mark.asyncio
async def test_f03_over_max_is_explicitly_too_long(f: EnhancedFilter):
    r = await f.analyze("ك" * 80)
    assert r.get("reason") == "too_long"


# ═══════════════ عقد keywords.json: لا انحدار ═══════════════

@pytest.mark.asyncio
async def test_keywords_contract_no_regression(f: EnhancedFilter):
    """42 حالة العقد — المسموح الفاشل: حالتا الطول >65 فقط (F-09 قيد واعٍ)
    + borderline-5 (v11.8.1 F-14: سقف resolution+طلب جديد يُطبق حتى بعد
    clause_rescore — التوقع الأصلي كان يوثق الثغرة القديمة accept 0.891)."""
    import json
    kw = json.load(open(PROJECT_DIR / "keywords.json"))
    allowed_fail = set()
    for group in ("positive", "borderline"):
        for item in kw["test_cases"].get(group, []):
            if len(item["text"]) > 65:
                allowed_fail.add(item["text"])
    # v12.1.0 توحيد السياسة: المواصفة الصريحة 6..65 شاملاً الحدين — الحالات
    # الأقصر من 6 خارج ظرف الفلتر (خط الإنتاج الحي يرفضها في _validate_event
    # قبل وصولها للمحرك منذ v11.5.2)؛ توقعاتها القديمة (review) وثّقت أرضية
    # المحرك المباشرة القديمة (3) التي أُوحّدت الآن مع المواصفة.
    from unified_alerts.filter_policy import FilterPolicy as _FP
    _spec_min = _FP.limits()[0]
    for group in ("positive", "negative", "borderline"):
        for item in kw["test_cases"].get(group, []):
            if len(item["text"]) < _spec_min:
                allowed_fail.add(item["text"])
    f14_review_override = set()
    for item in kw["test_cases"].get("borderline", []):
        if "تقرير ثاني محتاج مساعدة" in item["text"]:
            f14_review_override.add(item["text"])
    fails = []
    for group in ("positive", "negative", "borderline"):
        for item in kw["test_cases"].get(group, []):
            r = await f.analyze(item["text"])
            dec, exp = r["decision"], item["expected"]
            if item["text"] in f14_review_override:
                exp = "review"  # v11.8.1 (F-14) — سلوك مقصود موثق
            ok = (dec == exp) if exp in ("accept", "ignore") else (dec in ("accept", "review"))
            if not ok and item["text"] not in allowed_fail:
                fails.append(f"{item['text'][:40]!r}: {dec} != {exp}")
    assert not fails, "انحدارات عقد المحرك: " + "; ".join(fails)


# ═══════════════ F-07: عزل access_hash لكل حساب ═══════════════

def _make_monitor_stub():
    from monitors import EnhancedAccountMonitor
    m = object.__new__(EnhancedAccountMonitor)
    m.account = {"name": "Account B"}
    return m


class _FakeDB:
    def __init__(self, contact_row, own_hash):
        self._contact_row = contact_row
        self._own_hash = own_hash
        self.access_calls: List[int] = []

    async def get_sender_contact(self, sender_id):
        return self._contact_row

    async def get_sender_access_hash(self, account_name, sender_id):
        self.access_calls.append(sender_id)
        assert account_name == "Account B"  # v11.8: حساب المالك فقط
        return self._own_hash


@pytest.mark.asyncio
async def test_f07_enrich_uses_own_account_hash_only():
    """الإثراء كان يملأ hash من sender_contacts العام (حساب آخر) — الآن
    من sender_access للحساب المالك حصراً؛ وإن غاب يبقى None (لا تسريب)."""
    m = _make_monitor_stub()
    db = _FakeDB(
        contact_row={"username": "u1", "access_hash": 111, "first_name": "س", "last_name": "ع"},
        own_hash=222,
    )
    m.db = db
    data: Dict[str, Any] = {"sender_id": 12345}
    await m._enrich_sender_from_db(data)
    assert data["sender_access_hash"] == 222
    assert db.access_calls == [12345]


@pytest.mark.asyncio
async def test_f07_no_foreign_hash_leak_when_own_missing():
    m = _make_monitor_stub()
    db = _FakeDB(
        contact_row={"username": "u1", "access_hash": 111, "first_name": "س", "last_name": "ع"},
        own_hash=None,
    )
    m.db = db
    data: Dict[str, Any] = {"sender_id": 12345}
    await m._enrich_sender_from_db(data)
    assert data.get("sender_access_hash") is None  # لا hash أجنبي
    assert data["sender_username"] == "u1"  # الهوية العامة (username) تبقى مشروعة


# ═══════════════ F-08: صحة ردود التنبيهات (last_alert_message_id) ═══════════════

from alert_bot import AlertBot  # noqa: E402

_TARGET = None  # from CFG via conftest


class _FakeBot(AlertBot):
    def __init__(self, responses: List[Tuple[int, Dict[str, Any]]]):
        from config import CFG
        super().__init__(token="123456:TEST-TOKEN", chat_id=int(CFG.TARGET_GROUP_ID))
        self._responses = list(responses)

    async def _post(self, method, payload):
        if self._responses:
            return self._responses.pop(0)
        return 200, {"ok": True, "result": {"message_id": 777, "entities": []}}


def _alert_data() -> Dict[str, Any]:
    return {
        "chat_id": -100111222333, "message_id": 4242, "sender_id": 6079171409,
        "sender_username": None, "sender_first_name": "هناء", "sender_last_name": "ع",
        "sender_access_hash": 7234567890123, "chat_username": None,
        "chat_title": "مجموعة", "text": "محتاج مساعدة في التقرير",
        "account_name": "A1", "sender_type": "user", "msg_date_ts": 0.0,
    }


@pytest.mark.asyncio
async def test_f08_send_stamps_per_call_message_id():
    bot = _FakeBot([(200, {"ok": True, "result": {"message_id": 555}})])
    data = _alert_data()
    ok, _m, _r = await bot.send(data, msg_link=None, group_link=None)
    assert ok is True
    assert data.get("_alert_message_id") == 555
    assert bot.last_alert_message_id == 555


@pytest.mark.asyncio
async def test_f08_no_stale_id_after_failed_or_t5_call():
    """استدعاء فاشل كلياً يجب أن يترك last_alert_message_id=None (لا قيمة
    قديمة من تنبيه سابق) — الجوهر: الرد لا يذهب لرسالة سابقة بالخطأ."""
    bot = _FakeBot([(200, {"ok": True, "result": {"message_id": 555}})])
    data1 = _alert_data()
    await bot.send(data1, msg_link=None, group_link=None)
    assert bot.last_alert_message_id == 555

    # استدعاء ثانٍ فاشل (400 مكرر) — لا T5 client — يفشل بعد المحاولات
    async def _no_sleep(_s):
        return None

    bot.max_retries = 1
    bot._responses = [(400, {"ok": False, "description": "Bad Request: chat not found"})]
    import alert_bot as ab
    _orig_sleep = ab.asyncio.sleep
    ab.asyncio.sleep = _no_sleep
    try:
        data2 = _alert_data()
        ok2, _m2, _r2 = await bot.send(data2, msg_link=None, group_link=None)
        assert ok2 is False
        assert "_alert_message_id" not in data2
        assert bot.last_alert_message_id is None  # أُبطل عند الدخول — لا قيمة قديمة
    finally:
        ab.asyncio.sleep = _orig_sleep
