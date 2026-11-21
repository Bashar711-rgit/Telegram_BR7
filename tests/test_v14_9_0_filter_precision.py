"""v14.9.0 Filter Precision — اختبارات F-01 / F-04 / F-15.

F-01: كشف العروض التجارية المنتحلة شكل طلب (commercial_offer_score +
      القاعدة الصارمة offer>=2 → تجاهل بغض النظر عن الثقة).
F-04: أرضية مراجعة للطلبات غير المباشرة القصيرة (3-6 كلمات).
F-15: مطابقة إشارات الإعلان بحدود كلمات (لا substring).

حراسات F-01 ضد الإيجابيات الخاطئة (طلاب حقيقيون بصيغة الجماعة):
  * نافٍ قبل فعل العرض يلغيه («ما نحل واجبات هنا»)
  * عندنا/لدينا تُحتسب فقط مع أسماء الحلول («عندنا واجب» طلب حقيقي)
  * سياق الطلب الشخصي الصادق («نسوي بحث واحتاج مساعدة») يبقي الأفعال وصفاً
"""

import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from filter_engine import EnhancedFilter  # noqa: E402


@pytest.fixture(scope="module")
def flt():
    return EnhancedFilter()


# ═══════════════ F-01: commercial_offer_score ═══════════════


class TestCommercialOfferScore:
    def test_ad_we_form_plus_noun_scores_2(self, flt):
        """«نحل واجبات» — صيغة فريق + اسم أكاديمي = +2."""
        assert flt.commercial_offer_score("نحل واجبات") >= 2
        assert flt.commercial_offer_score("نقدم حلول وتقارير") >= 2

    def test_ad_we_form_noun_within_4_words(self, flt):
        """الاسم خلال نافذة 1-4 كلمات بعد الفعل."""
        # نسوي _ _ _ بحث = فعل + اسم بعد 3 كلمات
        assert flt.commercial_offer_score("نسوي لكم باحترافية عالية بحث") >= 2

    def test_cta_scores_1_each(self, flt):
        assert flt.commercial_offer_score("تواصل واتساب") == 2
        assert flt.commercial_offer_score("اسعار مناسبه جدا") == 1

    def test_full_disguised_ad_hits_hard_threshold(self, flt):
        """الإعلان المقنّع: صيغة فريق + CTA → >= 2."""
        text = "نحل واجبات وتقارير تواصل واتساب"
        assert flt.commercial_offer_score(text) >= 2

    def test_real_request_scores_zero(self, flt):
        """الطلبات الحقيقية يجب ألا تُحتسب عرضاً."""
        assert flt.commercial_offer_score("ابغى احد يحل لي الواجب") == 0
        assert flt.commercial_offer_score("عندي واجب ما اعرف حله") == 0
        assert flt.commercial_offer_score("احتاج شرح للدرس ضروري") == 0

    def test_ocollective_we_have_homework_is_not_an_ad(self, flt):
        """حارس عندنا/لدينا: «عندنا واجب ما عرفناه» طلب حقيقي (اسم واجب لا يُحتسب
        مع أفعال الجماعة المحايدة — فقط أسماء الحلول)."""
        assert flt.commercial_offer_score("عندنا واجب ما عرفناه") == 0
        assert flt.commercial_offer_score("عندنا مشروع صعب احد يساعد") == 0

    def test_ocollective_we_have_solutions_is_an_ad(self, flt):
        """«عندنا حلول جاهزة» — عرض تجاري حقيقي يُحتسب."""
        assert flt.commercial_offer_score("عندنا حلول جاهزه") >= 2
        assert flt.commercial_offer_score("لدينا بحوث وتقارير") >= 2

    def test_negator_before_offer_verb_cancels(self, flt):
        """حارس النافي: «ما نحل واجبات هنا» — نافٍ قبل الفعل يلغيه."""
        assert flt.commercial_offer_score("ما نحل واجبات هنا") == 0
        assert flt.commercial_offer_score("مو نسوي بحوث لا تفكرون") == 0

    def test_personal_request_frame_suppresses_we_verbs(self, flt):
        """حارس السياق الشخصي: «نسوي بحث واحتاج مساعدة في التنسيق» — طالب يصف
        وضعه بصيغة الجماعة؛ بلا CTA لا يُحتسب عرضاً."""
        assert flt.commercial_offer_score("نسوي بحث واحتاج مساعدة في التنسيق") == 0

    def test_personal_frame_but_with_cta_still_counts(self, flt):
        """الإعلان المقنّع بسياق شخصي يحمل CTA دائماً — CTA يظل محتسباً."""
        assert flt.commercial_offer_score("احتاج شخص ينسق لي تواصل واتس") >= 2

    def test_empty_text(self, flt):
        assert flt.commercial_offer_score("") == 0


# ═══════════════ F-01: القاعدة الصارمة في classify ═══════════════


class TestCommercialOfferHardRule:
    @pytest.mark.asyncio
    async def test_disguised_ad_is_hard_ignored(self, flt):
        """إعلان مقنّع بفعل طلب — يجب تجاهله صرماً مع سبب القاعدة."""
        result = await flt.analyze("ابغى احد يحل واجبات لكم تواصل واتساب")
        assert result["decision"] == "ignore"
        reasons = " ".join(result.get("reasons") or [])
        assert "commercial_offer" in reasons or (
            result.get("score_details", {}) or {}
        ).get("offer_score", 0) >= 2

    @pytest.mark.asyncio
    async def test_real_request_survives(self, flt):
        """طلب حقيقي مباشر لا يتأثر بـF-01 (حماية من الإفراط)."""
        result = await flt.analyze("ابغى احد يحل لي واجب الرياضيات ضروري")
        assert result["decision"] in ("accept", "review")

    @pytest.mark.asyncio
    async def test_score_details_include_offer_score(self, flt):
        result = await flt.analyze("ابغى شرح درس المشتقات ضروري")
        assert "offer_score" in (result.get("score_details") or {})


# ═══════════════ F-04: أرضية المراجعة للطلبات القصيرة ═══════════════


class TestShortRequestFloor:
    @pytest.mark.asyncio
    async def test_short_indirect_request_gets_review(self, flt):
        """«احد يقدر يساعدني بالحل» (4 كلمات) — marker + فعل مساعدة → review."""
        result = await flt.analyze("احد يقدر يساعدني بالحل")
        assert result["decision"] == "review"
        assert "f04: short_request_floor" in (result.get("reasons") or [])

    @pytest.mark.asyncio
    async def test_4_word_marker_help_request(self, flt):
        result = await flt.analyze("مين يقدر يساعدني في الواجب")
        assert result["decision"] in ("review", "accept")

    @pytest.mark.asyncio
    async def test_disguised_ad_not_rescued_by_f04(self, flt):
        """F-04 شرطه offer_score == 0 — الإعلان المقنّع لا يرفعه إلى review."""
        result = await flt.analyze("ابغى احد يحل واجبات لكم تواصل واتساب")
        assert result["decision"] == "ignore"
        assert "f04: short_request_floor" not in (result.get("reasons") or [])


# ═══════════════ F-15: حدود الكلمات في مطابقة الإعلانات ═══════════════


class TestWordBoundaryAdMatching:
    def test_weak_provider_substring_not_matched(self, flt):
        """«نسويته» تحوي «نسوي» substring — بحدود الكلمات لا تُحتسب."""
        score, reasons = flt._detect_advertisement("نسويته قبل وما نسوي احد")
        assert "weak_provider_multiple" not in reasons

    def test_weak_provider_token_matched(self, flt):
        """الـtoken المستقل يُحتسب كما يجب."""
        score, reasons = flt._detect_advertisement("نحل ونسوي ونكتب باحتراف")
        assert "weak_provider_multiple" in reasons

    def test_hard_signal_still_matches(self, flt):
        score, reasons = flt._detect_advertisement("مشاريع جاهزه وبحوث جاهزه")
        assert any(r.startswith("hard_ad_signal") for r in reasons)
        assert score >= 0.4

    def test_medium_signal_prefix_not_matched(self, flt):
        """«واتسابي» تحوي «واتس» substring — اللاحقة تكسر الحد فلا تُحتسب
        (طلب حقيقي يذكر واتسابه الشخصي)، بينما «التواصل» (ال+تواصل — نفس
        الكلمة نحوياً) تُحتسب كما في المطابقة الأصلية."""
        score, reasons = flt._detect_advertisement("واتسابي مكتوب فيه السؤال")
        assert not any(r.startswith("cta_signal") for r in reasons)
        score2, reasons2 = flt._detect_advertisement("التواصل عبر الخاص فقط")
        assert any(r.startswith("cta_signal") for r in reasons2)
