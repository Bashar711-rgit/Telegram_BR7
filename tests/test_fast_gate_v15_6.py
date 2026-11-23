#!/usr/bin/env python3
"""اختبارات v15.6.0 — البوابة السريعة (fast_gate.py / Aho-Corasick).

العقد الأمني المركزي (صفر خسارة تنبيهات):
    ** المحرك يقبل ⇒ البوابة تقول "analyze" **
    ** البوابة ترفض ⇒ قرار المحرك ليس "accept" **

الإثبات البنيوي (موثق في fast_gate.py):
  * كل مصادر القبول (request/indirect/implicit/help_anchor + markers +
    help-verbs) محصودة في الأتمتة، ومسح الشجرة (limit=1000 + حارس clitic)
    أضيق من مسح الأتمتة (2000 حرف، بلا حارس) — إصابة الشجرة ⇒ إصابة AC.
  * أي قبول عبر fuzzy في المحرك ⇒ rescue البوابة (نفس دالة المحرك
    _fuzzy_intent_fallback على نفس النص المنظف) يعيد إصابة ⇒ "analyze".
  * إذن رفض البوابة ⇒ لا شجرة ولا fuzzy ⇒ مسار no_keyword حصراً ⇒
    decision ∈ {ignore} — لا "accept" ولا خسارة تنبيه.

كما تغطي اختبارات رجعية الخطأ الحرج المُصلح: عقد _clean يعيد
Tuple[str, str] — مسح أي شيء عدا العنصر [0] كان سيرفض كل رسالة.
"""

import asyncio
import random
from typing import List

import pytest

import fast_gate as fg_mod
from fast_gate import (
    FastGate,
    PYAC_AVAILABLE,
    PyAhoCorasick,
)
from filter_engine import EnhancedFilter


# ═══════════════════════ الأتمتة الخالصة ═══════════════════════

class TestPyAhoCorasick:
    def test_basic_hit_and_miss(self):
        ac = PyAhoCorasick()
        for w in ("احتاج", "ابغى"):
            ac.add(w)
        ac.build()
        assert ac.any_hit("مرحبا احتاج حل") is True
        assert ac.any_hit("ابغى مساعدة") is True
        assert ac.any_hit("مرحبا بكم") is False

    def test_fail_links_overlapping_patterns(self):
        """الروابط الفاشلة: نماذج متداخلة تُكتشف من أي إزاحة."""
        ac = PyAhoCorasick()
        for w in ("ab", "bc", "she", "he", "hers"):
            ac.add(w)
        ac.build()
        assert ac.any_hit("abc") is True      # ab عند 0 / bc عند 1
        assert ac.any_hit("ushers") is True   # she+hers متداخلتان
        assert ac.any_hit("xy") is False

    def test_output_propagation_via_fail(self):
        """إصابة عند عقدة أعمق تعني إصابة لاحقة قصيرة (نفش المخرجات)."""
        ac = PyAhoCorasick()
        for w in ("ab", "b"):
            ac.add(w)
        ac.build()
        assert ac.any_hit("xab") is True
        assert ac.any_hit("xb") is True

    def test_stats(self):
        ac = PyAhoCorasick()
        ac.add("احتاج"); ac.add("احتاج")  # تكرار لا يزيد العدّ
        ac.build()
        n, _s = ac.stats
        assert n == 1

    def test_empty_and_unicode(self):
        ac = PyAhoCorasick()
        ac.add("")
        ac.add("✅ok")
        ac.build()
        assert ac.any_hit("تم ✅ok الان") is True


# ═══════════════════════ البوابة مع المحرك الحي ═══════════════════════

@pytest.fixture(scope="module")
def flt():
    return EnhancedFilter()


@pytest.fixture(scope="module")
def gate(flt):
    g = FastGate()
    built = g.build_from_filter(flt)
    assert built is True, "بناء البوابة من المحرك الحي يجب أن ينجح"
    return g


def _corpus(flt) -> List[str]:
    """نصوص مختلطة: طلبات حقيقية، قريبة من الكلمات، ضجيج، أرقام، لاتيني."""
    rng = random.Random(1560)
    kw = sorted(getattr(flt, "_request_trie")._words)[:12]
    indirect = sorted(getattr(flt, "_indirect_trie")._words)[:8]
    texts: List[str] = []
    for w in kw:
        texts += [f"ابي {w} مساعدة ضرورية", f"من يقدر {w} اليوم؟", w]
    for w in indirect:
        texts.append(f"سلام عليكم، {w} لو سمحتم")
    # متغيرات تحرير-1 (typo) لكلمات الطلب — يجب ألا تضيع (fuzzy/rescue)
    for w in kw[:6]:
        if len(w) >= 6:
            texts.append(w[:-1])   # حذف آخر حرف
            texts.append(w + "ي")  # إدراج حرف
    # ضجيج لا علاقة له بالطلب
    noise = [
        "هههههههههههههههه", "كيف الحال يا جماعة", "الجو حلو اليوم",
        "١٢٣٤٥٦٧٨٩", "1234567890", "hello world this is english",
        "😀😀😀😀😀😀", "شكرا جزيلا", "تمام الحمد لله", "وينكم مغيبين",
        "صباح الخير", "مساء الخير", "الحمد لله على كل حال",
    ]
    texts += noise
    # نصوص عشوائية مطمئنة (deterministic seed)
    alpha = "ابتثجحخدذرزسشصضطظعغفقكلمنهوي "
    for _ in range(30):
        texts.append("".join(rng.choice(alpha) for _ in range(rng.randint(6, 60))))
    return [t for t in texts if t.strip()]


class TestGateContract:
    """العقد الأمني: قبول المحرك ⇒ analyze | رفض البوابة ⇒ لا قبول."""

    @pytest.mark.asyncio
    async def test_clean_contract_regression(self, flt, gate):
        """رجعي الخطأ الحرج: _clean يعيد tuple — كان سيُرفض كل شيء."""
        before_errors = gate.errors
        verdict, meta = gate.evaluate("ابي احتاج مساعدة بحل واجب")
        assert verdict == "analyze"
        assert meta["reason"] in ("acceptance_term_hit", "rescue_fuzzy")
        assert gate.errors == before_errors  # بلا fail-open إطلاقاً

    @pytest.mark.asyncio
    async def test_engine_accept_implies_gate_analyze(self, flt, gate):
        for text in _corpus(flt):
            analysis = await flt.analyze(text)
            verdict, meta = gate.evaluate(text)
            if analysis.get("decision") == "accept":
                assert verdict == "analyze", (
                    f"خسارة تنبيه محتملة! قبول المحرك والبوابة ترفض: {text!r} "
                    f"| reason={meta['reason']}")
            # العقد المقلوب أيضاً: رفض البوابة ⇒ المحرك لا يقبل
            if verdict == "reject":
                assert analysis.get("decision") != "accept", (
                    f"رفض بوابة غير صالح: {text!r} | المحرك قبلها!")

    @pytest.mark.asyncio
    async def test_noise_is_fast_rejected(self, flt, gate):
        """الضجيج الصافي (98.6% من الحركة) يُرفض فوراً بمكافئ no_keyword."""
        verdict, meta = gate.evaluate("هههههههههههههههه")
        assert verdict == "reject"
        assert meta["reason"] == "no_acceptance_term"
        analysis = await flt.analyze("هههههههههههههههه")
        assert analysis.get("decision") != "accept"  # تكافؤ no_keyword

    @pytest.mark.asyncio
    async def test_typo_request_rescued(self, flt, gate):
        """حذف حرف من كلمة الطلب: إما متغير في الأتمتة أو rescue-fuzzy —
        في الحالتين لا رفض لنص طلب حقيقي."""
        kw = sorted(getattr(flt, "_request_trie")._words)
        tested = 0
        for w in kw:
            if len(w) < 6:
                continue
            typo = w[:-1]
            verdict, meta = gate.evaluate(f"ابي {typo} ضروري الان")
            assert verdict == "analyze", (
                f"طلب بحذف حرف رُفض! term={w!r} typo={typo!r} reason={meta['reason']}")
            tested += 1
            if tested >= 6:
                break
        assert tested >= 3, "لم تُختبر متغيرات كافية"

    @pytest.mark.asyncio
    async def test_scan_ms_recorded(self, flt, gate):
        verdict, meta = gate.evaluate("مرحبا كيف الحال")
        assert meta["scan_ms"] is not None
        assert meta["scan_ms"] < 50.0  # مسح O(len) — سقف سلامة فضفاض


class TestGateModes:
    @pytest.mark.asyncio
    async def test_disabled_gate_always_analyzes(self, flt):
        g = FastGate()  # بلا بناء — disabled
        verdict, meta = g.evaluate("اي نص")
        assert verdict == "analyze"
        assert meta["reason"] == "disabled"

    @pytest.mark.asyncio
    async def test_generation_change_fail_open(self, flt, gate, monkeypatch):
        """تغيّر جيل المحرك (reload) → analyze + إعادة بنية مجدولة."""
        called = {"n": 0}
        monkeypatch.setattr(gate, "schedule_rebuild", lambda: called.__setitem__("n", called["n"] + 1))
        old = gate._gen_ids
        gate._gen_ids = (0, 0)
        try:
            verdict, meta = gate.evaluate("ابي احتاج حل")
            assert verdict == "analyze"
            assert meta["reason"] == "generation_changed"
            assert called["n"] == 1
        finally:
            gate._gen_ids = old

    @pytest.mark.asyncio
    async def test_oversize_fail_open(self, flt, gate):
        """النص الضخم لا يُرفض أبداً: إما يتجاوز سقف المسح بعد التنظيف
        (empty_or_oversize) أو يُمسح ويجد إصابة مشروعة — كلاهما analyze."""
        verdict, meta = gate.evaluate("ا" * 3000)
        assert verdict == "analyze"
        assert meta["reason"] != "no_acceptance_term"

    def test_snapshot_shape(self, gate):
        snap = gate.snapshot()
        for key in ("enabled", "engine", "patterns", "states", "checked",
                    "fast_rejected", "hit_passed", "rescued_fuzzy", "errors"):
            assert key in snap

    def test_health_snapshot_pre_wiring(self):
        from fast_gate import health_snapshot
        snap = health_snapshot()
        assert isinstance(snap, dict)
        assert "enabled" in snap


class TestConfigDefaults:
    def test_fast_gate_enabled_default(self):
        from config import CFG
        assert bool(getattr(CFG, "FAST_GATE_ENABLED", False)) is True

    def test_rescue_fuzzy_default(self):
        from config import CFG
        assert bool(getattr(CFG, "FAST_GATE_RESCUE_FUZZY", False)) is True

    def test_pyahocorasick_optional(self):
        """الحزمة المتسارعة اختيارية — الغياب لا يعطل شيئاً."""
        assert isinstance(PYAC_AVAILABLE, bool)
