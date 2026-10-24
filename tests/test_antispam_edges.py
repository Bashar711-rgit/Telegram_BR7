"""اختبارات حدود مكافحة السبام (المهمة الهندسية الموحدة §2.3/§8.6).

النوافذ المعتمدة (config defaults):
  burst: 2 رسائل / 60 ثانية
  similarity: 0.80 / 300 ثانية (5 دقائق)
  category: 2 فئات / 300 ثانية
  direct: 4 مجموعات / 300 ثانية
  confirm: 5 رسائل / 300 ثانية + 3 مجموعات / 600 ثانية
  watch duration: 600 ثانية

الحدود المختبرة: ث 59/61، د 4:59/5:01، تشابه 0.80، تأكيد 9:59/10:01،
4 مجموعات مباشر مقابل 3، watch→confirm، بلا هوية (sender=0).
لا تغيير عتبات — اختبارات توصيف للسلوك الفعلي.
"""

import sys
import time
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from antispam import AntiSpamEngine, detect_categories, normalize_for_fingerprint
from config import CFG

SID = 555000111
TEXTS_NORMAL = [
    "عندي سؤال في الرياضيات",
    "كيف حالكم جميعا اليوم",
    "الجو حلو في الخارج",
    "هل انتهى الدرس ام لا",
    "اريد ان اشارك فكرة جميلة",
]


def _eng() -> AntiSpamEngine:
    return AntiSpamEngine()


def _ratio(a: str, b: str) -> float:
    return _eng()._ratio(normalize_for_fingerprint(a), normalize_for_fingerprint(b))


# ═══════════════════ نافذة الانفجار (60 ثانية) ═════════════════════════
class TestBurstWindowEdges:
    @pytest.mark.asyncio
    async def test_second_59_counts_second_61_expires(self):
        """الرسالة عند ث 59 ضمن النافذة (59≤60 → watch)؛ عند ث 61 الأولى
        خرجت (61>60 → بلا انفجار)."""
        eng = _eng()
        await eng.observe(SID, -1001, TEXTS_NORMAL[0], ts=1000.0)
        act, info = await eng.observe(SID, -1001, TEXTS_NORMAL[1], ts=1059.0)
        assert act == "watch", "2 رسائل خلال 59 ثانية يجب أن تراقب"
        assert any("60" in r for r in info["watch_reasons"])

        eng2 = _eng()
        await eng2.observe(SID, -1001, TEXTS_NORMAL[0], ts=1000.0)
        act2, _ = await eng2.observe(SID, -1001, TEXTS_NORMAL[1], ts=1061.0)
        assert act2 == "allow", "الأولى خرجت من نافذة 60 ثانية عند ث 61"

    @pytest.mark.asyncio
    async def test_burst_needs_two_messages(self):
        """رسالة واحدة لا تراقب أحداً — الشرط رسالتان."""
        eng = _eng()
        act, _ = await eng.observe(SID, -1001, TEXTS_NORMAL[0], ts=1000.0)
        assert act == "allow"
        assert eng.stats["watch_added"] == 0


# ═══════════════════ نافذة التشابه (300 ثانية / 0.80) ══════════════════
class TestSimilarityWindowEdges:
    @pytest.mark.asyncio
    async def test_similarity_4m59_in_5m00_out(self):
        """التشابه عند 4:59 (299 ث ≤ 300) يراقب؛ عند 5:01 (301 > 300) لا."""
        a = "ابغى بحث تخرج شاب فصل اول"
        b = "ابغى بحث تخرج شاب فصل ثاني"
        assert _ratio(a, b) >= CFG.ANTISPAM_SIMILARITY_THRESHOLD

        eng = _eng()
        await eng.observe(SID, -1001, a, ts=1000.0)
        act, _ = await eng.observe(SID, -1001, b, ts=1299.0)
        assert act == "watch", "التشابه عند 4:59 يجب أن يراقب"

        eng2 = _eng()
        await eng2.observe(SID, -1001, a, ts=1000.0)
        act2, _ = await eng2.observe(SID, -1001, b, ts=1301.0)
        assert act2 == "allow", "السابقة خرجت من نافذة 5 دقائق عند 5:01"

    @pytest.mark.asyncio
    async def test_similarity_threshold_boundary(self):
        """حد 0.80: زوج فوق العتبة يراقب، زوج تحت العتبة بوضوح لا."""
        above_a = "ابغى بحث تخرج شاب فصل اول"
        above_b = "ابغى بحث تخرج شاب فصل ثاني"
        below_a, below_b = TEXTS_NORMAL[0], TEXTS_NORMAL[1]
        assert _ratio(above_a, above_b) >= 0.80
        assert _ratio(below_a, below_b) < 0.80

        eng = _eng()
        # تباعد 100 ثانية: خارج نافذة الانفجار (60) — المراقبة من التشابه حصراً
        await eng.observe(SID, -1001, above_a, ts=1000.0)
        act, info = await eng.observe(SID, -1001, above_b, ts=1100.0)
        assert act == "watch"
        assert any("تشابه" in r or "متشابهة" in r for r in info["watch_reasons"])

        eng2 = _eng()
        await eng2.observe(SID, -1001, below_a, ts=1000.0)
        act2, _ = await eng2.observe(SID, -1001, below_b, ts=1100.0)
        assert act2 == "allow", "نصان مختلفان (<0.80) وبلا انفجار لا يراقبان"


# ═══════════════════ نافذة التأكيد (5 رسائل / 600 ثانية) ═══════════════
class TestConfirmWindowEdges:
    async def _reach_watch(self, eng: AntiSpamEngine, t0: float) -> None:
        """أدخل المستخدم المراقبة (رسالتان خلال 60 ثانية)."""
        await eng.observe(SID, -1001, TEXTS_NORMAL[0], ts=t0)
        await eng.observe(SID, -1001, TEXTS_NORMAL[1], ts=t0 + 2)

    @pytest.mark.asyncio
    async def test_5_messages_within_10m_confirm_outside_not(self):
        """شرط (هـ): 5 رسائل خلال 10 دقائق تؤكد — الرابعة الخارجة من
        النافذة لا تؤكد. رسائل عادية بلا فئات وغير متشابهة (تستبعد
        الشروط أ-د)."""
        # ضمن النافذة: الرسائل 3،4 (عادية بلا مؤشر) لا تؤكد بعد — الخامسة تؤكد
        eng = _eng()
        t0 = 1000.0
        await self._reach_watch(eng, t0)
        for i in (2, 3):
            act, _ = await eng.observe(SID, -1001, TEXTS_NORMAL[i], ts=t0 + 4 + i)
            assert act in ("allow", "watch"), f"msg{i} shouldn't confirm yet: {act}"
        act5, info5 = await eng.observe(SID, -1001, TEXTS_NORMAL[4], ts=t0 + 30)
        assert act5 == "spam", "5 رسائل خلال 10 دقائق تؤكد الحظر (شرط هـ)"
        assert any("10 دقائق" in r for r in info5["reasons"])
        assert eng.stats["spam_confirmed"] == 1
        assert SID in eng._ignored

        # خارج النافذة: الأولى عمرها 601 ثانية → 4 رسائل فقط ضمن 10 دقائق
        eng2 = _eng()
        await self._reach_watch(eng2, t0)
        await eng2.observe(SID, -1001, TEXTS_NORMAL[2], ts=t0 + 4)
        await eng2.observe(SID, -1001, TEXTS_NORMAL[3], ts=t0 + 6)
        act_last, _ = await eng2.observe(SID, -1001, TEXTS_NORMAL[4], ts=t0 + 601)
        assert act_last == "allow", "الخامسة خارج نافذة 10 دقائق لا تؤكد (4 فقط)"
        assert eng2.stats["spam_confirmed"] == 0

    @pytest.mark.asyncio
    async def test_confirm_window_9m59_vs_10m01(self):
        """الرسائل ضمن 9:59 تحسب في تأكيد (هـ)؛ عند 10:01 الأولى خرجت."""
        t0 = 2000.0
        # 9:59 = 599 ثانية — كل الخمس ضمن نافذة 10 دقائق
        eng = _eng()
        await self._reach_watch(eng, t0)
        await eng.observe(SID, -1001, TEXTS_NORMAL[2], ts=t0 + 4)
        await eng.observe(SID, -1001, TEXTS_NORMAL[3], ts=t0 + 6)
        act, _ = await eng.observe(SID, -1001, TEXTS_NORMAL[4], ts=t0 + 599)
        assert act == "spam", "الخامسة عند 9:59 ضمن النافذة تؤكد (شرط هـ)"

        # 10:01 = 601 ثانية — الأولى خرجت: 4 رسائل فقط
        eng2 = _eng()
        await self._reach_watch(eng2, t0)
        await eng2.observe(SID, -1001, TEXTS_NORMAL[2], ts=t0 + 4)
        await eng2.observe(SID, -1001, TEXTS_NORMAL[3], ts=t0 + 6)
        act2, _ = await eng2.observe(SID, -1001, TEXTS_NORMAL[4], ts=t0 + 601)
        assert act2 == "allow", "الأولى خرجت عند 10:01 — 4 رسائل فقط"
        assert eng2.stats["spam_confirmed"] == 0

    @pytest.mark.asyncio
    async def test_watch_confirmed_via_new_request_category(self):
        """watch → confirm: رسالة تحمل فئة طلب أثناء المراقبة تؤكد (شرط أ)."""
        eng = _eng()
        t0 = 3000.0
        await self._reach_watch(eng, t0)
        cat_text = "ابغى بحث تخرج شاب ضروري"
        assert detect_categories(cat_text), "يجب أن يحمل النص فئة"
        act, info = await eng.observe(SID, -1001, cat_text, ts=t0 + 5)
        assert act == "spam"
        assert eng.stats["spam_confirmed"] == 1

    @pytest.mark.asyncio
    async def test_normal_message_after_watch_does_not_confirm(self):
        """رسالة عادية (بلا فئة ولا تشابه) أثناء المراقبة لا تؤكد وحدها —
        استمرار مراقبة فقط."""
        eng = _eng()
        t0 = 4000.0
        await self._reach_watch(eng, t0)
        act, info = await eng.observe(SID, -1001, TEXTS_NORMAL[4], ts=t0 + 8)
        assert act == "allow"
        assert info.get("watched") is True


# ═══════════════════ التصنيف المباشر (4 مجموعات) ═══════════════════════
class TestDirectClassification:
    @pytest.mark.asyncio
    async def test_four_groups_direct_spam(self):
        """التصنيف المباشر الفعلي: وسائط/نص فارغ (بلا norm) في 4 مجموعات
        خلال 5 دقائق (تباعد >60 ث يفصل الانفجار) → تصنيف مباشر دون مراقبة.
        (للنصوص الفئوية يُستبق التصنيف المباشر بمسار watch→confirm —
        النتيجة النهائية نفسها: حظر دائم — انظر الاختبار الموالي.)"""
        eng = _eng()
        t0 = 5000.0
        await eng.observe(SID, -1001, "", ts=t0)          # وسائط بلا نص
        await eng.observe(SID, -1002, "", ts=t0 + 70)     # خارج نافذة الانفجار
        await eng.observe(SID, -1003, "", ts=t0 + 140)
        act, info = await eng.observe(SID, -1004, "", ts=t0 + 210)
        assert act == "spam", "4 مجموعات خلال 5 دقائق = سبام مباشر"
        assert eng.stats["direct_spam"] >= 1
        assert any("تصنيف مباشر" in r for r in (info.get("reasons") or []))
        assert SID in eng._ignored

    @pytest.mark.asyncio
    async def test_three_groups_confirms_via_watch_not_direct(self):
        """3 مجموعات بنصوص فئوية: لا تصنيف مباشر (يحتاج 4) — لكن مسار
        watch→confirm يحظر عبر شرط (أ) فئة الطلب. النتيجة النهائية نفسها
        (حظر دائم) والعدّاد المباشر يبقى صفراً — توصيف صادق للسلوك."""
        eng = _eng()
        t0 = 5100.0
        a = "ابغى بحث تخرج شاب فصل اول"
        b = "ابغى بحث تخرج شاب فصل ثاني"
        await eng.observe(SID, -1001, a, ts=t0)
        await eng.observe(SID, -1002, a, ts=t0 + 2)
        act, info = await eng.observe(SID, -1003, b, ts=t0 + 4)
        assert act == "spam", "مسار watch→confirm يتصرف قبل المباشر"
        assert eng.stats["direct_spam"] == 0, "3 مجموعات لا تُصنّف مباشرة"
        assert eng.stats["spam_confirmed"] == 1
        assert not any("تصنيف مباشر" in r for r in (info.get("reasons") or []))


# ═══════════════════ بلا هوية (sender=0) — انحدار v11.7.1 ══════════════
class TestZeroIdentityRegression:
    @pytest.mark.asyncio
    async def test_zero_sender_never_watched_nor_confirmed(self):
        """أي عدد من رسائل بلا هوية: لا مراقبة ولا تأكيد ولا صف دائم —
        allow فوري دائماً."""
        eng = _eng()
        t0 = 6000.0
        for i in range(10):
            act, _ = await eng.observe(0, -1001 + i, "ابغى بحث تخرج شاب", ts=t0 + i)
            assert act == "allow"
        assert eng.stats["watch_added"] == 0
        assert eng.stats["spam_confirmed"] == 0
        assert 0 not in eng._ignored
        assert eng._activities.get(0) is None

    @pytest.mark.asyncio
    async def test_real_user_after_zero_sender_unaffected(self):
        """رسائل بلا هوية لا تلوث التتبع — مستخدم حقيقي بعدها يُراقب طبيعياً."""
        eng = _eng()
        t0 = 6100.0
        for i in range(5):
            await eng.observe(0, -1001, "ابغى بحث تخرج شاب", ts=t0 + i)
        await eng.observe(SID, -1001, "ابغى بحث تخرج شاب فصل اول", ts=t0 + 10)
        act, _ = await eng.observe(SID, -1001, "ابغى بحث تخرج شاب فصل ثاني", ts=t0 + 12)
        assert act == "watch", "المستخدم الحقيقي يُراقب بمعزل عن بلا الهوية"
        assert eng._activities.get(SID) is not None
