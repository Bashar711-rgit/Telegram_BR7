"""v10.0 Precision Edition tests — clauses.py / similarity.py /
classifier deadline & priority / filter_engine clause re-scoring."""

import time

import pytest

from clauses import count_clauses, split_clauses
from classifier import extract_deadline, priority_tier
from similarity import SimilarityGate, jaccard, _shingles


# ─────────────────────────────────────────────────────────────────────────────
# clauses.py — تقسيم البنود
# ─────────────────────────────────────────────────────────────────────────────
class TestSplitClauses:
    def test_punctuation_split(self):
        parts = split_clauses("عندي واجب، محتاج مساعدة؛ والوقت ضيق")
        assert len(parts) >= 3
        assert all("،" not in p and "؛" not in p for p in parts)

    def test_contrast_word_split(self):
        parts = split_clauses("لقيت حل للواجب بس عندي تقرير ثاني محتاج مساعدة فيه")
        assert len(parts) == 2
        assert "تقرير" in parts[1]

    def test_lakin_split(self):
        parts = split_clauses("خلصت المشروع لكن عندي اختبار بكرة")
        assert len(parts) == 2

    def test_no_split_on_waw(self):
        # «و» العاطفة لا تقطع — فكرة واحدة تبقى بنداً واحداً
        parts = split_clauses("محتاج حل واجب وبحث سريع")
        assert len(parts) == 1

    def test_no_split_on_la_negation(self):
        parts = split_clauses("ما اقدر احله لا الوقت ولا الطول يسمح")
        assert len(parts) == 1

    def test_keep_together_protected(self):
        parts = split_clauses("ان شاء الله اقدر اساعدك اليوم")
        assert any("ان شاء الله" in p for p in parts)

    def test_short_text_single_clause(self):
        assert split_clauses("ابي حل") == ["ابي حل"]

    def test_empty_safe(self):
        assert split_clauses("") == []
        assert split_clauses(None) == []  # type: ignore[arg-type]

    def test_max_clauses_cap(self):
        text = "، ".join(f"بند رقم {i} فيه كلام كثير للتجربة" for i in range(10))
        assert len(split_clauses(text, max_clauses=4)) == 4

    def test_count_clauses(self):
        assert count_clauses("الأول. الثاني. الثالث.") == 3


# ─────────────────────────────────────────────────────────────────────────────
# similarity.py — حاجز التشابه
# ─────────────────────────────────────────────────────────────────────────────
class TestSimilarityGate:
    def test_shingles_basic(self):
        a = _shingles("ابي احد يساعدني بالواجب")
        b = _shingles("ابي احد يساعدني بالواجب")
        assert a == b and len(a) > 0

    def test_jaccard_identical(self):
        s = _shingles("نص متطابق تماما هنا")
        assert jaccard(s, s) == 1.0

    def test_jaccard_disjoint(self):
        a = _shingles("نص اول مختلف تماما")
        b = _shingles("كلام ثاني مغاير كليا هنا")
        assert jaccard(a, b) < 0.3

    def test_rephrased_same_sender_blocked(self):
        gate = SimilarityGate()
        t1 = "ابي احد يساعدني بالواجب ضروري الليلة"
        t2 = "ابي احد يساعدني في الواجب ضروري الليلة"  # إعادة صياغة طفيفة
        assert gate.check_and_record(111, t1) is None
        hit = gate.check_and_record(111, t2)
        assert hit is not None and hit["score"] >= 0.88

    def test_distinct_request_same_sender_passes(self):
        gate = SimilarityGate()
        gate.check_and_record(222, "محتاج حل واجب رياضيات تفاضل")
        hit = gate.check_and_record(222, "عندي تقرير فيزياء لازم اسلمة بكرة")
        assert hit is None

    def test_different_senders_same_text_pass(self):
        gate = SimilarityGate()
        t = "ابي احد يشرح لي الجبر الخطي"
        assert gate.check_and_record(1, t) is None
        assert gate.check_and_record(2, t) is None

    def test_window_expiry_allows_resend(self):
        gate = SimilarityGate()
        # نافذة صفرية = تعطيل عملي
        gate._recent[333] = {}
        import similarity as sim_mod

        orig = sim_mod.CFG
        class _FakeCfg:
            SIMILARITY_ENABLED = True
            SIMILARITY_THRESHOLD = 0.88
            SIMILARITY_WINDOW_SECONDS = 0
        sim_mod.CFG = _FakeCfg()
        try:
            t = "نص تجريبي للنافذة المنتهية بشكل طويل نسبياً"
            gate.check_and_record(333, t)
            assert gate.check_and_record(333, t) is None
        finally:
            sim_mod.CFG = orig

    def test_disabled_gate_passes(self):
        gate = SimilarityGate()
        import similarity as sim_mod

        orig = sim_mod.CFG
        class _FakeCfg:
            SIMILARITY_ENABLED = False
            SIMILARITY_THRESHOLD = 0.88
            SIMILARITY_WINDOW_SECONDS = 3600
        sim_mod.CFG = _FakeCfg()
        try:
            t = "نص اختبار الحاجز المعطل مرتين متطابقتين"
            assert gate.check_and_record(5, t) is None
            assert gate.check_and_record(5, t) is None
        finally:
            sim_mod.CFG = orig

    def test_snapshot_shape(self):
        gate = SimilarityGate()
        snap = gate.snapshot()
        assert "enabled" in snap and "threshold" in snap and "blocked" in snap


# ─────────────────────────────────────────────────────────────────────────────
# classifier.py — المواعيد والأولوية
# ─────────────────────────────────────────────────────────────────────────────
class TestDeadlineExtraction:
    def test_critical_now(self):
        d = extract_deadline("محتاج الواجب الان والوقت ضيق")
        assert d["level"] == "critical"

    def test_critical_after_hour(self):
        d = extract_deadline("ابي الحل بعد ساعه لو سمحتوا")
        assert d["level"] == "critical"

    def test_today_tomorrow(self):
        d = extract_deadline("الواجب لازم يتسلم باكر الصبح")
        assert d["level"] == "today"

    def test_soon_week(self):
        d = extract_deadline("المشروع التسليم هالاسبوع")
        assert d["level"] == "soon"

    def test_none_no_marker(self):
        assert extract_deadline("عندي سؤال في المحاسبة")["level"] == "none"

    def test_greeting_not_urgency(self):
        # «اهلا وسهلا» ليست عاجلة — حماية حدود الكلمات
        d = extract_deadline("اهلا وسهلا بنات عندي طلب صغير")
        assert d["level"] == "none"

    def test_empty_safe(self):
        assert extract_deadline("")["level"] == "none"
        assert extract_deadline(None)["level"] == "none"  # type: ignore[arg-type]


class TestPriorityTier:
    def test_critical_beats_all(self):
        tier = priority_tier({"urgent": False, "deadline": {"level": "critical"}})
        assert tier == "🔥 عاجل جداً"

    def test_today_beats_engine_urgent(self):
        tier = priority_tier({"urgent": True, "deadline": {"level": "today"}})
        assert tier == "⚡ اليوم"

    def test_engine_urgent(self):
        assert priority_tier({"urgent": True}) == "⚡ عاجل"

    def test_high_confidence_badge(self):
        assert priority_tier({"confidence": 0.9}) == "⭐ طلب واضح"

    def test_normal_empty(self):
        assert priority_tier({"confidence": 0.7}) == ""

    def test_fail_safe(self):
        assert priority_tier(None) == ""


# ─────────────────────────────────────────────────────────────────────────────
# filter_engine.py — إعادة تقييم البنود
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def flt():
    from filter_engine import EnhancedFilter

    return EnhancedFilter()


class TestClauseRescore:
    @pytest.mark.asyncio
    async def test_resolution_plus_new_request_upgraded(self, flt):
        # الحالة الموثقة في keywords.json: بند استرسال + بند طلب جديد
        text = "لقيت حل للواجب ما قصرتوا، بس عندي تقرير ثاني محتاج مساعدة فيه"
        r = await flt.analyze(text)
        assert r["decision"] == "accept"
        assert any(
            str(x).startswith("clause_rescore") for x in (r.get("reasons") or [])
        )

    @pytest.mark.asyncio
    async def test_plain_request_unaffected(self, flt):
        r = await flt.analyze("ابغى أحد يحل لي واجب الرياضيات ضروري")
        assert r["decision"] == "accept"
        assert not any(
            str(x).startswith("clause_rescore") for x in (r.get("reasons") or [])
        )

    @pytest.mark.asyncio
    async def test_greeting_plus_request(self, flt):
        # تحية + طلب — الحماية: التحية لا تمنع الطلب بعد التقسيم
        r = await flt.analyze("السلام عليكم، محتاج مساعدة بحل واجب قواعد بيانات")
        assert r["decision"] in ("accept", "review")

    @pytest.mark.asyncio
    async def test_negation_clause_not_upgraded(self, flt):
        # النفي المحلي يمنع ترقية البند المنفي
        r = await flt.analyze("شكرا للجميع، ما قدرت احل الواجب")
        assert r["decision"] != "accept" or not any(
            str(x).startswith("clause_rescore") for x in (r.get("reasons") or [])
        )

    @pytest.mark.asyncio
    async def test_result_shape_unchanged(self, flt):
        r = await flt.analyze("محتاج مساعدة بالتقرير، الوقت ضيق")
        for key in ("decision", "confidence", "reasons", "keyword", "valid"):
            assert key in r
