"""Filter accuracy regression tests — v14.4 engine.

Three layers:
1. The FULL built-in keywords.json test_cases suite (positive/negative/
   borderline) — the same cases the engine documentation promises.
2. Targeted regressions for every v14.4 fix class (boundary matching,
   negation traps, two-tier ignore, short messages, ads, fuzzy).
3. A loose performance guard.
"""

import json
import time
from pathlib import Path

import pytest

from filter_engine import EnhancedFilter, Prefilter

KW_FILE = Path(__file__).resolve().parent.parent / "keywords.json"


@pytest.fixture(scope="module")
def flt():
    return EnhancedFilter()


def _kw_cases():
    with open(KW_FILE, encoding="utf-8") as f:
        kw = json.load(f)
    cases = []
    for group in ("positive", "negative", "borderline"):
        for i, case in enumerate(kw.get("test_cases", {}).get(group, [])):
            cases.append((f"{group}-{i}", case["text"], case["expected"]))
    return cases


class TestBuiltInSuite:
    """Every documented test case in keywords.json must pass."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("cid", "text", "expected"), _kw_cases(), ids=[c[0] for c in _kw_cases()])
    async def test_builtin_case(self, flt, cid, text, expected):
        result = await flt.analyze(text)
        assert result["decision"] == expected, (
            f"{cid}: {text[:60]!r} expected={expected} "
            f"got={result['decision']} conf={result['confidence']:.3f} "
            f"reason={result['reason']} reasons={result['reasons'][:3]}"
        )


@pytest.mark.asyncio
class TestBoundaryMatching:
    async def test_kitabi_not_aby(self, flt):
        r = await flt.analyze("كتابي ضايع من اسبوع وما لقيته")
        assert r["decision"] == "ignore", r

    async def test_mahal_not_hal(self, flt):
        r = await flt.analyze("المحل الجديد جنب الجامعة فتحوا")
        assert r["decision"] == "ignore", r

    async def test_waw_clitic_still_matches(self, flt):
        r = await flt.analyze("وابي احد يساعدني في الواجب ضروري")
        assert r["decision"] != "ignore", r

    async def test_al_prefix_still_matches(self, flt):
        r = await flt.analyze("ابغى الاستاذ يشرح لي الفصل الاخير")
        assert r["decision"] != "ignore", r

    async def test_suffix_continuation_matches(self, flt):
        r = await flt.analyze("احتاج ملخصات شاملة للمادة قبل الميد")
        assert r["decision"] != "ignore", r

    async def test_fuzzy_no_substring_false_positive(self, flt):
        r = await flt.analyze("كتابي درست منه كثير للحصص")
        assert r["decision"] == "ignore", r


@pytest.mark.asyncio
class TestNegationOverhaul:
    async def test_laa_substring_no_longer_negates(self, flt):
        # "لا" inside "للاسئلة" must not act as the negator
        r = await flt.analyze("ابي احد يحل للاسئلة الصعبة قبل بكرة")
        assert r["decision"] != "ignore", r

    async def test_mash_substring_no_longer_negates(self, flt):
        r = await flt.analyze("اريد شرح للمشكلة الحاسوبية هذي")
        assert r["decision"] != "ignore", r

    async def test_real_negation_still_suppresses(self, flt):
        r = await flt.analyze("ما ابغى احد يساعدني خلاص انتهى الموضوع")
        assert r["decision"] == "ignore", r

    async def test_past_tense_negation_far_from_intent(self, flt):
        r = await flt.analyze("النتائج ما طلعت متسقة وياليت احد يشرح لي التحليل الاحصائي")
        assert r["decision"] != "ignore", r

    async def test_resolution_plus_new_request_capped(self, flt):
        r = await flt.analyze("خلصت الواجب الاول والحين ابي احد يحل لي الواجب الثاني")
        assert r["decision"] != "accept", r


@pytest.mark.asyncio
class TestShortMessages:
    async def test_short_request_not_dropped(self, flt):
        r = await flt.analyze("ابي حل")
        assert r["decision"] != "ignore", r

    async def test_bare_intent_bottoms_at_review(self, flt):
        r = await flt.analyze("محتاج")
        assert r["decision"] == "review", r

    async def test_fazaa_request(self, flt):
        r = await flt.analyze("فزعه ابي احد يحل لي الواجب اليوم")
        assert r["decision"] != "ignore", r


@pytest.mark.asyncio
class TestTwoTierIgnore:
    async def test_greeting_with_request_not_suppressed(self, flt):
        r = await flt.analyze("السلام عليكم، عندي مشروع تخرج ومحتاج أحد يساعدني فيه، شكراً")
        assert r["decision"] != "ignore", r

    async def test_strong_social_still_suppressed(self, flt):
        r = await flt.analyze("ابي شقة قريبة من الجامعة للايجار")
        assert r["decision"] == "ignore", r

    async def test_pure_greeting_ignored(self, flt):
        r = await flt.analyze("السلام عليكم كيف الحال جميعا")
        assert r["decision"] == "ignore", r


@pytest.mark.asyncio
class TestAdvertisementPrecision:
    async def test_hard_ad_rejected(self, flt):
        r = await flt.analyze("نسوي لكم مشاريع تخرج جاهزة للتسليم باسعار ممتازة")
        assert r["decision"] == "ignore", r

    async def test_single_ad_emoji_not_blocking(self, flt):
        r = await flt.analyze("ابي احد يساعدني في الواجب ضروري 🔥")
        assert r["decision"] != "ignore", r

    async def test_ad_emoji_flood_still_rejected(self, flt):
        r = await flt.analyze("🔥✅⚡🌟🛒🆓 نسوي لكم الواجبات والبحوث")
        assert r["decision"] == "ignore", r

    async def test_academic_drive_link_ok(self, flt):
        r = await flt.analyze("هذا رابط ملف المشروع https://drive.google.com/file/xyz مين يساعدني اكمله")
        assert r["decision"] != "ignore", r


@pytest.mark.asyncio
class TestPrefilterLanguage:
    async def test_mixed_arabic_english_passes(self, flt):
        r = await flt.analyze("ابي حل assignment 2 ضروري اليوم")
        assert r["reason"] != "low_arabic_ratio", r

    async def test_neutral_emojis_exempt_from_cap(self, flt):
        text = "ابي احد يساعدني ضروري 😭🙏😅🥺😢 اسلم الواجب بكرة"
        ok, reason, _ = Prefilter.check(
            flt._clean(text)[0], 1, 5, emoji_exempt=flt._neutral_emoji
        )
        assert ok, reason

    async def test_english_only_still_ignored(self, flt):
        r = await flt.analyze("hello everyone how are you doing today folks")
        assert r["decision"] == "ignore", r


class TestPerformance:
    def test_analysis_latency_sane(self, flt):
        import asyncio

        async def run():
            texts = [
                "ابي احد يسوي مشروع تخرج هندسة",
                "فيه احد فاهم مادة الشبكات؟",
                "نسوي لكم بحوث جاهزة تواصل واتساب",
                "السلام عليكم كيف الحال",
            ]
            t0 = time.perf_counter()
            for _ in range(5):
                for t in texts:
                    await flt.analyze(t)
            return (time.perf_counter() - t0) / 20 * 1000

        avg_ms = asyncio.run(run())
        assert avg_ms < 150, f"avg analyze latency {avg_ms:.1f}ms too high"
