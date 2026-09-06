"""Unit tests for filter_engine.py — normalization, matching, decisions,
and the audit H-1 regression (repeated text must not be dropped as
"duplicate" by the bloom filter)."""

import pytest

from filter_engine import EnhancedFilter, Prefilter


@pytest.fixture(scope="module")
def flt():
    return EnhancedFilter()


ARABIC_REQUEST = "ابغى أحد يحل لي واجب الرياضيات ضروري"


class TestArabicNormalization:
    def test_alef_variants_collapse(self, flt):
        # أ / إ / آ must all normalize to bare alef
        assert flt._clean("إحتاج أقعد أحل آهلاً")[0] is not None
        a = flt._normalize_arabic("أإآ")
        assert a == "ااا"

    def test_taa_marbuta_to_ha(self, flt):
        assert flt._normalize_arabic("مدرسة") == "مدرسه"

    def test_alef_maqsura_to_ya(self, flt):
        assert flt._normalize_arabic("على") == "علي"

    def test_tashkeel_stripped(self, flt):
        with_diacritics = flt._normalize_arabic("مُحْتَاج")
        assert all(not ('\u064B' <= c <= '\u0652') for c in with_diacritics)

    def test_diacritics_do_not_break_matching(self, flt):
        plain = flt._clean("احتاج حل واجب الرياضيات")[0]
        vocalized = flt._clean("اِحْتَـاجُ حَـلّ وَاجِب الرِّيَاضِيَـات")[0]
        assert vocalized == plain


class TestPrefilter:
    def test_empty_rejected(self):
        ok, reason, _ = Prefilter.check("", 1, 5)
        assert not ok

    def test_low_arabic_ratio_rejected(self):
        ok, reason, _ = Prefilter.check("hello world this is english", 1, 5)
        assert not ok and reason == "low_arabic_ratio"

    def test_normal_arabic_passes(self):
        ok, reason, _ = Prefilter.check("ابغى حل واجب الرياضيات", 1, 5)
        assert ok and reason == "ok"


class TestAnalyzeDecisions:
    @pytest.mark.asyncio
    async def test_clear_request_is_accepted(self, flt):
        result = await flt.analyze("ابغى احد يحل لي واجب الفيزياء ضروري جدا")
        assert result["decision"] in ("accept", "review"), result
        assert result["keyword"] is not None

    @pytest.mark.asyncio
    async def test_english_only_ignored(self, flt):
        result = await flt.analyze("hello everyone how are you doing today")
        assert result["decision"] == "ignore"

    @pytest.mark.asyncio
    async def test_too_long_ignored(self, flt):
        result = await flt.analyze("ا" * 6000)
        assert result["decision"] == "ignore"

    @pytest.mark.asyncio
    async def test_empty_ignored(self, flt):
        result = await flt.analyze("")
        assert result["decision"] == "ignore"

    @pytest.mark.asyncio
    async def test_result_shape(self, flt):
        result = await flt.analyze(ARABIC_REQUEST)
        for key in ("valid", "decision", "confidence", "reasons", "keyword",
                    "intent_verb", "score_details", "original_text"):
            assert key in result

    @pytest.mark.asyncio
    async def test_confidence_bounded(self, flt):
        for text in (ARABIC_REQUEST, "حل واجب", "مساعدة ضروري جدا في الكيمياء"):
            r = await flt.analyze(text)
            assert 0.0 <= r["confidence"] <= 1.0


class TestH1BloomRegression:
    """Audit H-1: the bloom filter must NEVER turn a repeated identical text
    into `ignore / duplicate`. Repeated identical requests (common across
    different students) must return the cached analysis instead."""

    @pytest.mark.asyncio
    async def test_repeated_text_not_dropped(self, flt):
        text = "ابغى احد يحل لي واجب الاحصاء ضروري اليوم"
        first = await flt.analyze(text)
        second = await flt.analyze(text)
        third = await flt.analyze(text)
        assert first["reason"] != "duplicate"
        assert second["reason"] != "duplicate"
        assert third["reason"] != "duplicate"
        # Decisions must be consistent across repeats.
        assert second["decision"] == first["decision"]
        assert third["decision"] == first["decision"]
        # Second/third calls hit the cache: near-instant.
        assert second["analysis_time_ms"] <= first["analysis_time_ms"] + 0.5

    @pytest.mark.asyncio
    async def test_cache_hit_returns_valid_result(self, flt):
        text = "ممكن احد يساعدني في حل مسائل التفاضل عندي اختبار"
        await flt.analyze(text)
        tel = await flt.get_telemetry()
        cached_before = tel.get("cache_hits", 0)
        await flt.analyze(text)
        tel_after = await flt.get_telemetry()
        assert tel_after.get("cache_hits", 0) == cached_before + 1


class TestTelemetry:
    @pytest.mark.asyncio
    async def test_telemetry_keys(self, flt):
        tel = await flt.get_telemetry()
        assert "processed" in tel and "valid" in tel
        assert "latency_percentiles" in tel
        assert {"p50", "p95", "p99", "mean", "count"} <= set(tel["latency_percentiles"])
