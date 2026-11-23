#!/usr/bin/env python3
"""
fast_gate.py — البوابة السريعة للفلترة (v16.0.0)

════════════════════════════════════════════════════════════════════
الهدف (طلب المالك: «السرعة في الفلترة بشكل متزامن»)
════════════════════════════════════════════════════════════════════

المشكلة المقاسة حياً (2026-11-23):
  * 8228 رسالة فُلترت → 111 مقبولة فقط (98.6% تُرفض بعد دفع ثمن
    CPU كامل: 15-20 مسح شجرة Python + fuzzy rapidfuzz + 7 passes
    regex إعلانات + rescore الجمل على الرافضات).
  * خيوط محرك الفلتر 4 فقط أمام 8 عمال معالجة (FILTER_EXECUTOR_WORKERS).

الحل — بوابة رفض فوري مكافئة رياضياً لمسار no_keyword في المحرك:

  1. أتمتة Aho-Corasick (مسح واحد من اليسار لليمين، O(len(text)))
     مبنية من «مصادر القبول فقط» في المحرك:
       - _request_trie   (نية الطلب — الكلمة المفتاحية الأساسية)
       - _indirect_trie  (الطلب غير المباشر)
       - _implicit_trie  (الطلب الضمني)
       - _help_anchor_trie (مرساة المساعدة)
       - _REQUEST_MARKERS + _HELP_VERB_TOKENS (أرضية F-04 للمراجعة)
       - متغيرات fuzzy بمسافة تحرير 1 (حذف/إدراج/تبديل/استبدال للطول ≥7
         — مطابقة تماماً لما يقبله rapidfuzz ratio ≥ 85 على توكن ≥5)

  2. ضمان الصحة (بلا خسارة تنبيه واحد):
     كل مسار قبول في _analyze_core_sync يحتاج keyword = intent ∪
     indirect ∪ implicit ∪ help_anchor (L1826-1830)، والنية تأتي من
     الشجرة أو fuzzy (L1797-1808). أي نص تجد الأتمتة فيه إصابة
     مضمون دخوله التحليل الكامل؛ وأي نص صفري الإصابات لا يمكن أن
     يقبله المحرك (فحص rescue-fuzzy احتياطي قبل أي رفض).
     مسارات التجاوز (allowlist/rule_allow) تُحسب في monitors.py بعد
     التحليل بشكل مستقل عن نتيجة الفلتر — لا تتأثر إطلاقاً.

  3. اتجاه الفشل الآمن دائماً: أي استثناء أو غياب → "analyze"
     (التحليل الكامل كما اليوم). البوابة لا ترفض إلا وهي واثقة.

  4. الذاكرة: لا نضع الـ24k نمط boost في الأتمتة (boost يهم فقط
     بعد وجود keyword — والمسار الكامل لا يُطلق بلا keyword).
     الأنماط ≈ بضع مئات مصدر + ~12k متغير fuzzy ≈ 15-25MB
     (الحد الحي 456MB وrss الحالي 265MB).

  5. التسريع الاختياري: إن وُجدت حزمة pyahocorasick (C) تُستخدم
     تلقائياً؛ وإلا تعمل الأتمتة Python الخالصة المرفقة (بلا أي
     تبعية جديدة — صفر مخاطر بناء على Render).

استخدام:
    from fast_gate import ensure_fast_gate, get_fast_gate
    ensure_fast_gate(filter_obj)          # عند الإقلاع
    verdict, meta = get_fast_gate().evaluate(text)  # "analyze"/"reject"
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Dict, Optional, Set, Tuple

from loguru import logger

try:  # تسريع اختياري — غيابه أمر طبيعي تماماً
    import ahocorasick as _pyac  # type: ignore

    PYAC_AVAILABLE = True
except Exception:  # pragma: no cover
    _pyac = None
    PYAC_AVAILABLE = False

# سقف مسح النص في البوابة (المحرك نفسه يمسح أول 1000 حرف في الشجرات؛
# 2000 هنا اتجاه أمان — إصابة بعد 1000 تعني "analyze" لا رفضاً)
_MAX_SCAN_CHARS = 2000
# سقف صارم لعدد الأنماط (فشل-آمن ضد أي انفجار مفردات مستقبلي)
_MAX_PATTERNS = 45000
# سقف صارم لعقد الأتمتة (ذاكرة ≈ 200B/عقدة → 150k ≈ ≤30MB)
_MAX_STATES = 150000

# حروف توليد متغيرات fuzzy: عربي شائع + لاتيني صغير + أرقام
_VAR_ALPHABET: Tuple[str, ...] = tuple(
    "ابتثجحخدذرزسشصضطظعغفقكلمنهويةىأإآءمنو"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
)


class PyAhoCorasick:
    """أتمتة Aho-Corasick خالصة Python — لقرار أي-إصابة (boolean).

    مبنية على قوائم متوازية (children/fail/output) بدل عقد كائنية
    لتقليل الذاكرة. output يُنفَش أثناء BFS ففحص النص O(len) صرف.
    """

    __slots__ = ("_children", "_fail", "_output", "_pattern_count", "_states")

    def __init__(self) -> None:
        self._children: list = [{}]
        self._fail: list = [0]
        self._output: list = [False]
        self._pattern_count = 0
        self._states = 1

    def add(self, word: str) -> None:
        if not word:
            return
        state = 0
        children = self._children
        for ch in word:
            nxt = children[state].get(ch)
            if nxt is None:
                children.append({})
                self._fail.append(0)
                self._output.append(False)
                nxt = self._states
                self._states += 1
                children[state][ch] = nxt
            state = nxt
        if not self._output[state]:
            self._output[state] = True
            self._pattern_count += 1

    def build(self) -> None:
        """روابط الفشل + نفش المخرجات (BFS — الأعمق يعالج بعد الأسطح)."""
        queue: deque = deque()
        for s in self._children[0].values():
            self._fail[s] = 0
            queue.append(s)
        while queue:
            s = queue.popleft()
            # نفش المخرجات: إصابة عند s تعني إصابة عند fail[s] أيضاً
            if not self._output[s]:
                self._output[s] = self._output[self._fail[s]]
            children_s = self._children[s]
            fail_s = self._fail[s]
            for ch, t in children_s.items():
                f = fail_s
                while f != 0 and ch not in self._children[f]:
                    f = self._fail[f]
                nxt = self._children[f].get(ch, 0)
                if nxt == t:
                    nxt = 0
                self._fail[t] = nxt
                queue.append(t)

    def any_hit(self, text: str) -> bool:
        """مسح واحد — أول إصابة توقف فوراً. O(len(text))."""
        children = self._children
        fail = self._fail
        output = self._output
        state = 0
        for ch in text:
            while True:
                nxt = children[state].get(ch)
                if nxt is not None:
                    state = nxt
                    break
                if state == 0:
                    state = 0
                    break
                state = fail[state]
            if output[state]:
                return True
        return False

    @property
    def stats(self) -> Tuple[int, int]:
        return self._pattern_count, self._states


class _PyacAutomaton:  # pragma: no cover — يعمل فقط مع الحزمة الاختيارية
    """غلاف pyahocorasick بنفس واجهة any_hit."""

    __slots__ = ("_ac",)

    def __init__(self, patterns: Set[str]) -> None:
        ac = _pyac.Automaton()
        for w in patterns:
            ac.add_word(w, True)
        ac.make_automaton()
        self._ac = ac

    def any_hit(self, text: str) -> bool:
        for _ in self._ac.iter(text):
            return True
        return False

    @property
    def stats(self) -> Tuple[int, int]:
        return len(self._ac), 0


class FastGate:
    """بوابة الرفض الفوري — مكافئة لمسار no_keyword في المحرك."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ac: Any = None
        self._engine = "none"
        self._enabled = False
        self._rescue_fuzzy = True
        self._filter: Any = None
        self._gen_ids: Tuple[int, int] = (0, 0)
        # ── عدادات حية (قراءة عبر snapshot) ──
        self.checked = 0
        self.fast_rejected = 0
        self.hit_passed = 0
        self.rescued_fuzzy = 0
        self.rejected_saved_ms = 0.0
        self.errors = 0
        self.build_count = 0
        self.build_ms = 0.0
        self.patterns_count = 0
        self.states_count = 0
        self.source_terms = 0

    # ───────────────────────── البناء ─────────────────────────
    def _harvest_terms(self, flt: Any) -> Set[str]:
        """حصاد مصادر القبول من كائن المحرك الحي — بلا أي نسخ يدوية."""
        terms: Set[str] = set()
        for attr in ("_request_trie", "_indirect_trie", "_implicit_trie", "_help_anchor_trie"):
            trie = getattr(flt, attr, None)
            words = getattr(trie, "_words", None)
            if words:
                terms.update(w for w in words if w)
        for attr in ("_REQUEST_MARKERS", "_HELP_VERB_TOKENS"):
            seq = getattr(flt, attr, None)
            if seq:
                terms.update(w for w in seq if w)
        return terms

    def _fuzzy_variants(self, flt: Any, budget_left: int) -> Set[str]:
        """متغيرات تحرير-1 المطابقة تماماً لعتبة rapidfuzz ratio≥85:
        حذف/إدراج دائماً، استبدال/تبديل للطول ≥7 فقط (indel-2 على
        الطول ≥7 يعطي ratio ≥ 85.7). الفائض يتكفل به rescue-fuzzy.
        """
        out: Set[str] = set()
        by_len = getattr(flt, "_fuzzy_terms_by_len", None)
        if not by_len:
            return out
        alpha = _VAR_ALPHABET
        for _length, terms in by_len.items():
            for term in terms:
                if not term or " " in term:
                    continue
                n = len(term)
                # حذف حرف واحد
                for i in range(n):
                    out.add(term[:i] + term[i + 1:])
                # إدراج حرف واحد (كل الحروف × كل المواضع)
                for i in range(n + 1):
                    prefix, suffix = term[:i], term[i:]
                    for ch in alpha:
                        out.add(prefix + ch + suffix)
                if n >= 7:
                    # استبدال حرف واحد
                    for i in range(n):
                        prefix, suffix = term[:i], term[i + 1:]
                        for ch in alpha:
                            out.add(prefix + ch + suffix)
                    # تبديل حرفين متجاورين
                    for i in range(n - 1):
                        out.add(term[:i] + term[i + 1] + term[i] + term[i + 2:])
                out.discard(term)
                if len(out) >= budget_left:
                    return out
        return out

    def _build_automaton(self, flt: Any) -> Any:
        base = self._harvest_terms(flt)
        self.source_terms = len(base)
        patterns: Set[str] = set(base)
        budget = _MAX_PATTERNS - len(patterns)
        if budget > 0:
            variants = self._fuzzy_variants(flt, budget)
            patterns.update(variants)
        patterns.discard("")
        if not patterns:
            raise ValueError("no acceptance terms harvested")
        if len(patterns) > _MAX_PATTERNS:
            raise ValueError(f"too many patterns: {len(patterns)}")
        if PYAC_AVAILABLE:
            ac = _PyacAutomaton(patterns)
            self._engine = "pyahocorasick"
            self.patterns_count = len(patterns)
            self.states_count = 0
            return ac
        ac = PyAhoCorasick()
        for w in patterns:
            ac.add(w)
            if ac._states > _MAX_STATES:
                raise ValueError(f"too many states: {ac._states}")
        ac.build()
        self._engine = "python-ac"
        self.patterns_count, self.states_count = ac.stats
        return ac

    def rebuild(self) -> bool:
        """بنية جديدة من حالة المحرك الحية ثم تبديل ذرّي. فشل → معطلة."""
        flt = self._filter
        if flt is None:
            return False
        t0 = time.perf_counter()
        try:
            with self._lock:
                ac = self._build_automaton(flt)
                self._ac = ac
                self._enabled = True
                self._gen_ids = (
                    id(flt),
                    id(getattr(flt, "_request_trie", None)),
                )
                self.build_count += 1
                self.build_ms = round((time.perf_counter() - t0) * 1000, 1)
            logger.info(
                f"⚡ FastGate built | engine={self._engine} | patterns={self.patterns_count} "
                f"| states={self.states_count} | source_terms={self.source_terms} "
                f"| build_ms={self.build_ms} | rescue_fuzzy={self._rescue_fuzzy}"
            )
            return True
        except Exception as e:
            self._enabled = False
            self._ac = None
            self.errors += 1
            logger.warning(f"FastGate build failed (disabled, fail-open): {type(e).__name__}: {e}")
            return False

    def build_from_filter(self, flt: Any) -> bool:
        """البناء الأول عند الإقلاع — يقرأ إعدادات CFG الحية."""
        self._filter = flt
        try:
            from config import CFG

            self._rescue_fuzzy = bool(getattr(CFG, "FAST_GATE_RESCUE_FUZZY", True))
            gate_on = bool(getattr(CFG, "FAST_GATE_ENABLED", True))
        except Exception:
            gate_on = True
        if not gate_on:
            self._enabled = False
            logger.info("⚡ FastGate disabled by config (FAST_GATE_ENABLED=false)")
            return False
        return self.rebuild()

    def schedule_rebuild(self) -> None:
        """بنية خلفية بلا حجب العمال (بعد reload_keywords إدارياً)."""
        t = threading.Thread(target=self.rebuild, name="fast-gate-rebuild", daemon=True)
        t.start()

    def _check_generation(self) -> bool:
        """هل قوائم المحرك أُعيد بناؤها منذ آخر بنية؟ (reload_keywords)"""
        flt = self._filter
        if flt is None:
            return True
        cur = (id(flt), id(getattr(flt, "_request_trie", None)))
        return cur == self._gen_ids

    # ───────────────────────── التقييم ─────────────────────────
    def evaluate(self, text: str) -> Tuple[str, Dict[str, Any]]:
        """القرار: "analyze" (المسار الكامل كما اليوم) أو "reject"
        (رفض فوري مكافئ لـno_keyword). الفشل-آمن دائماً نحو analyze.
        """
        meta: Dict[str, Any] = {"engine": self._engine, "scan_ms": None, "reason": None}
        try:
            if not self._enabled or self._ac is None:
                meta["reason"] = "disabled"
                return "analyze", meta
            if not self._check_generation():
                # قوائم المحرك تغيرت — بنية خلفية وهذا النص يتحلل كاملاً
                self.schedule_rebuild()
                meta["reason"] = "generation_changed"
                return "analyze", meta
            flt = self._filter
            t0 = time.perf_counter()
            # ══ عقد المحرك: _clean يعيد Tuple[str, str] = (المنظف للمطابقة،
            # الأصلي المحفوظ) — filter_engine L1239/L1649. نطاق الشجرات هو
            # العنصر [0] حصراً، فمسح أي شيء آخر يعني صفر إصابات زائفة! ══
            try:
                _pair = flt._clean(text) if flt is not None else (text, text)
                cleaned = _pair[0] if isinstance(_pair, tuple) else _pair
            except Exception:
                cleaned = text
            if not cleaned or len(cleaned) > _MAX_SCAN_CHARS:
                meta["reason"] = "empty_or_oversize"
                self.checked += 1
                return "analyze", meta
            hit = self._ac.any_hit(cleaned)
            scan_ms = round((time.perf_counter() - t0) * 1000, 3)
            meta["scan_ms"] = scan_ms
            self.checked += 1
            if hit:
                self.hit_passed += 1
                meta["reason"] = "acceptance_term_hit"
                return "analyze", meta
            # ── rescue: fallback الضبابي الحقيقي للمحرك (فشل-آمن) ──
            # يغطي أي حافة رياضية نادرة لم تغطها المتغيرات — نفس
            # التكلفة التي كان يدفعها المحرك أصلاً لهذه الرسائل.
            if self._rescue_fuzzy and flt is not None:
                fb = getattr(flt, "_fuzzy_intent_fallback", None)
                if fb is not None:
                    try:
                        if fb(cleaned) is not None:
                            self.rescued_fuzzy += 1
                            meta["reason"] = "rescue_fuzzy"
                            return "analyze", meta
                    except Exception:
                        pass
            # ══ الرفض الفوري — مكافئ لـno_keyword تماماً ══
            self.fast_rejected += 1
            self.rejected_saved_ms += scan_ms  # مسح البوابة نفسه (المقارنة عند القياس)
            meta["reason"] = "no_acceptance_term"
            return "reject", meta
        except Exception:
            self.errors += 1
            meta["reason"] = "error_fail_open"
            return "analyze", meta

    # ───────────────────────── اللقطة ─────────────────────────
    def snapshot(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self._enabled),
            "engine": self._engine,
            "pyahocorasick_available": PYAC_AVAILABLE,
            "patterns": self.patterns_count,
            "states": self.states_count,
            "source_terms": self.source_terms,
            "rescue_fuzzy": self._rescue_fuzzy,
            "checked": self.checked,
            "fast_rejected": self.fast_rejected,
            "hit_passed": self.hit_passed,
            "rescued_fuzzy": self.rescued_fuzzy,
            "rebuilds": self.build_count,
            "build_ms": self.build_ms,
            "errors": self.errors,
        }


# ── Singleton مشترك ──────────────────────────────────────────────────────────
_fast_gate: Optional[FastGate] = None
_fast_gate_lock = threading.Lock()


def get_fast_gate() -> Optional[FastGate]:
    """البوابة singleton — None قبل ensure_fast_gate (اختبارات/بلا توصيل)."""
    return _fast_gate


def ensure_fast_gate(filter_obj: Any) -> Optional[FastGate]:
    """توصيل البوابة بكائن المحرك الحي + البناء الأول."""
    global _fast_gate
    with _fast_gate_lock:
        if _fast_gate is None:
            gate = FastGate()
            gate.build_from_filter(filter_obj)
            _fast_gate = gate
        else:
            # إعادة توصيل (نادر) — بنية جديدة من الحالة الحالية
            _fast_gate._filter = filter_obj
            try:
                _fast_gate.rebuild()
            except Exception:
                pass
    return _fast_gate


def health_snapshot() -> Dict[str, Any]:
    """لقطة آمنة لـ/health/full — تعمل حتى قبل التوصيل."""
    gate = _fast_gate
    if gate is None:
        return {"enabled": False, "engine": "not_wired", "checked": 0, "fast_rejected": 0}
    try:
        return gate.snapshot()
    except Exception:
        return {"enabled": False, "engine": "snapshot_error"}
