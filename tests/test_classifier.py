"""Unit tests for classifier.py (v10.0) — دقة التصنيف: المادة + نوع المطلوب.

Covers:
* normalize_light: همزات/تشكيل/تطويل/تكرار الحروف/الإيموجي.
* classify_text: كشف المادة والنوع من نصوص واقعية بلهجتين.
* false-positive guards: نصوص عامة بلا مادة.
* classification_line: سطر العرض للتنبيه.
* fail-safety: مدخلات شاذة لا ترفع استثناءً.
"""

import pytest

from classifier import classification_line, classify_text, normalize_light


class TestNormalizeLight:
    def test_strips_tashkeel_and_tatweel(self):
        assert normalize_light("رِيَــاضِيَــات") == normalize_light("رياضيات")

    def test_unifies_alef_variants(self):
        assert normalize_light("أحياء إحصاء آمل") == normalize_light("احياء احصاء امل")

    def test_unifies_taa_marbuta_and_alef_maqsura(self):
        assert normalize_light("برمجة مساعدة") == normalize_light("برمجه مساعده")

    def test_collapses_repeated_chars(self):
        assert normalize_light("ضروررررري") == normalize_light("ضروري")

    def test_removes_emoji(self):
        out = normalize_light("ابغي واجب 🙏🙏🔥")
        assert "🙏" not in out and "🔥" not in out
        assert normalize_light("واجب") in out

    def test_non_string_safe(self):
        assert normalize_light(None) == ""
        assert normalize_light(123) == ""


class TestSubjectClassification:
    def test_math(self):
        r = classify_text("ابغي احد يحل واجب رياضيات ضروري اليوم")
        assert r["subject"] == "رياضيات"
        assert r["subject_key"] == "math"

    def test_programming_python(self):
        r = classify_text("محتاج مساعدة في كود بايثون مشروع التخرج")
        assert r["subject"] == "برمجة"

    def test_physics(self):
        r = classify_text("عندي واجب فيزياء ما اعرف احله")
        assert r["subject"] == "فيزياء"

    def test_english(self):
        r = classify_text("ابغي احد يكتب لي essay انجليزي عن المستقبل")
        assert r["subject"] == "لغة إنجليزية"

    def test_excel(self):
        r = classify_text("مطلوب احد يشتغل لي اكسل جداول ومطلوب حل سريع")
        assert r["subject"] == "إكسل وأوفيس"

    def test_stats(self):
        r = classify_text("عندي بحث احصاء لازم spss والتحليل")
        assert r["subject"] == "إحصاء ورياضيات تطبيقية"

    def test_accounting(self):
        r = classify_text("احتاج شرح قيود محاسبه وميزانيه")
        assert r["subject"] == "محاسبة ومالية"

    def test_accounting_taa_marbuta_normalized(self):
        # «محاسبة» بتاء مربوطة يجب أن تطابق النمط المطبَّع
        r = classify_text("محتاج مساعدة في محاسبة مالية")
        assert r["subject"] == "محاسبة ومالية"

    def test_no_subject_for_generic_request(self):
        r = classify_text("ابغي مساعدة ضروري")
        assert r["subject"] == ""

    def test_no_subject_for_chat_noise(self):
        r = classify_text("الله يعطيك العافية شكرا جزيلا")
        assert r["subject"] == ""


class TestTypeClassification:
    def test_assignment(self):
        r = classify_text("ابغي احد يحل واجب رياضيات")
        assert r["type"] == "واجب"

    def test_exam(self):
        r = classify_text("عندي اختبار نهائي فيزياء اليوم")
        assert r["type"] == "اختبار"

    def test_research(self):
        r = classify_text("محتاج بحث تخرج جاهز بمراجع")
        assert r["type"] == "بحث"

    def test_summary(self):
        r = classify_text("ابغي ملخص فصل كامل بكيمياء")
        assert r["type"] == "تلخيص"

    def test_presentation(self):
        r = classify_text("مطلوب برزنتيشن عن الذكاء الاصطناعي")
        assert r["type"] == "عرض تقديمي"

    def test_subject_and_type_together(self):
        r = classify_text("ابغي احد يحل واجب رياضيات ضروري اليوم")
        assert r["subject"] == "رياضيات"
        assert r["type"] in ("واجب", "حل")


class TestFailSafety:
    def test_empty_and_none(self):
        assert classify_text("")["classified"] is False
        assert classify_text(None)["classified"] is False
        assert classify_text(42)["classified"] is False
        assert classify_text("   ")["classified"] is False

    def test_emoji_only(self):
        assert classify_text("🙏🙏🙏")["classified"] is False


class TestClassificationLine:
    def test_full_line(self):
        line = classification_line({
            "subject": "رياضيات", "type_tag": "واجب",
            "urgent": True, "confidence": 0.87,
        })
        assert "#رياضيات" in line
        assert "واجب" in line
        assert "عاجل" in line
        assert "87%" in line

    def test_empty_analysis(self):
        assert classification_line({}) == ""
        assert classification_line(None) == ""

    def test_confidence_zero_omitted(self):
        line = classification_line({"subject": "فيزياء", "confidence": 0.0})
        assert "الثقة" not in line
