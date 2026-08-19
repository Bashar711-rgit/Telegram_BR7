#!/usr/bin/env python3
"""
filter_engine.py – Smart Filter Engine v14.2 (Template-Driven IntentEngine-NLP, HARDENED)
Architecture: Prefilter + Fast Path + Fuzzy Path + Bloom Filter + Sharded Cache + Aho-Corasick + TTLCache
Supports: keywords.json v14.0.x, config.py v13.1 (template-boost fix), monitors.py v9.7

v14.2 (this pass) — signal-aggregation / conditional-confidence rework, filter_engine.py ONLY.
Everything from v14.1 is preserved; this pass changes *how the signals already being
collected are combined into a decision*. See the accompanying summary for the full
list of behavioral changes. Short version:

- The early-exit returns that fired on a single signal (ad_score > 0.6, negation
  match, spam-trie hit, ignore-trie hit) before every other signal had even been
  computed are gone. All signals (intent, academic object, urgency, negation/
  distress, ad_score, spam_score, message-type, duplicate-similarity) are now
  gathered first; the decision is made once, by weighted voting, at the end of
  analyze(). Prefilter / bloom-duplicate / too-long / invalid-input / language
  filter remain true early exits — they are not classification signals, they are
  "there is nothing here to classify" checks, and keeping them early is what
  makes the fast path fast.
- A lightweight context-aware `_classify_message_type()` runs first and tags the
  message as one of academic_help / advertisement / social_chat / spam / general
  BEFORE the individual signal interpreters run, so e.g. the ad-detector and the
  negation-detector both know whether they're looking at a message that already
  looks like a student request.
- `_detect_negation()` now checks negation_exceptions, then a dedicated distress
  trie ("ما عرفت", "ما فهمت", "ما قدرت" + an academic-context word), then the
  general negation rule — a distress expression is *never* treated as an
  early "user solved it, ignore" signal.
- Resolution phrases ("تم الحل", "لقيت حل") are now checked for a clause boundary
  ("لكن", "بس", "الا") followed by a *new* intent verb; if found, this is a new
  request, not a resolved one, and the resolution signal is discarded.
- Confidence is no longer `(legacy + weighted) / 2` blind averaging. It is now
  conditional: P(help) = P(intent) * P(academic|intent) * urgency_factor *
  context_factor, banded as specified (intent-only and academic-only both cap
  low; intent+academic+urgency is the only path to the top band).
- Duplicate detection now extracts "core text" (strips forwarding chrome —
  "الرسالة:", "عرض الرسالة الأصلية", "tg://...", "👤: ...", "『...』" — before
  hashing) and, in addition to the exact-hash bloom filter, keeps a bounded ring
  buffer of recent core-text shingle sets so near-duplicates (Jaccard > 0.85)
  that differ only in formatting/whitespace/emoji are also caught.
- Arabic normalization keeps *both* the original and normalized text on the
  result (`raw_text` / `normalized_text` were added to FilterResult) and a new
  `_split_glued_tokens()` step separates Arabic/Latin/digit runs that are stuck
  together ("اليوماحتاج" -> "اليوم احتاج", "assignment3" -> "assignment 3")
  before tokenization, so code-switched and glued messages tokenize correctly.
- Trie scanning is replaced with a proper Aho-Corasick automaton. `WeightedTrie`
  keeps its exact public interface (`search_first` / `search_all`, same return
  shape) so every existing call site is untouched, but internally it now builds
  failure links and does one linear pass over the text instead of re-walking
  the trie from every character offset (O(n) instead of O(n * max_word_len)).
- The specific smart rules from the spec (ad/intent tug-of-war, academic-context
  ad dampening, ambiguous "دكتور" resolution, temporal urgency factor) are
  implemented as named methods so they're independently testable.

Nothing in the public API changed: `analyze()` still takes a str and returns the
same dict shape (with additive-only new keys), `FilterResult.to_dict()` still
returns every v14.1 key, `reload_keywords()` / `_build_keyword_sets` still work,
`get_telemetry()` / `clear_cache()` are untouched.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, Final, List, Optional, Set, Tuple

from cachetools import TTLCache
from loguru import logger

from config import (
    CFG,
    KEYWORDS,
    InputSanitizer,
    PHONE_PATTERN,
    URL_PATTERN,
    EMAIL_PATTERN,
    EMOJI_PATTERN,
    WS_PATTERN,
)

# ── Optional libraries ────────────────────────────────────────────────────────

try:
    from rapidfuzz import fuzz, process as rf_process
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False
    logger.warning("rapidfuzz not installed – fuzzy matching disabled")

try:
    from langdetect import detect
    LANGDETECT_AVAILABLE = True
except ImportError:
    LANGDETECT_AVAILABLE = False

try:
    from pyarabic import araby
    PYARABIC_AVAILABLE = True
except ImportError:
    PYARABIC_AVAILABLE = False

# ── Forwarding-chrome stripping (core-text extraction) ─────────────────────────
# Compiled once at module import; used by EnhancedFilter._extract_core_text().
_FORWARD_CHROME_PATTERNS: Final[List[re.Pattern]] = [
    # Note: [ةه] / [أإآا] classes below because core-text extraction runs on
    # *already Arabic-normalized* text in _clean() (ة→ه, أ/إ/آ→ا, etc.) —
    # matching only the pre-normalization spelling would silently fail to
    # strip these on every real message, defeating the whole point of this
    # step. Keeping both spellings tolerant here costs nothing and is safe
    # even if this is ever called on non-normalized text too.
    re.compile(r"الرسال[ةه]\s*:"),
    re.compile(r"عرض\s+الرسال[ةه]\s+الأصلي[ةه]|عرض\s+الرسال[ةه]\s+الاصلي[ةه]"),
    re.compile(r"tg://\S*"),
    re.compile(r"👤\s*:\s*[^\n]*"),
    re.compile(r"『[^』]*』"),
    re.compile(r"forwarded\s+from\s*:?", re.IGNORECASE),
    re.compile(r"forwarded\s+message", re.IGNORECASE),
]

# Boundary between an Arabic run and a Latin/digit run (or vice-versa) with no
# whitespace between them — used to un-glue code-switched tokens.
_GLUE_BOUNDARY: Final[re.Pattern] = re.compile(
    r"(?<=[\u0600-\u06FF])(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])(?=[\u0600-\u06FF])"
)

# Used only inside _extract_core_text() for duplicate-detection purposes —
# general punctuation/symbol noise that should not prevent two otherwise
# identical messages from hashing/shingling the same way.
_PUNCTUATION_PATTERN: Final[re.Pattern] = re.compile(
    r"[!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~،؛؟…]+"
)


# =============================================================================
# FilterResult (lightweight slots) – v14.2
# =============================================================================
@dataclass(slots=True)
class FilterResult:
    valid: bool = False
    reason: str = ""
    keyword: Optional[str] = None
    score: int = 0
    match_score: float = 0.0
    spam_score: float = 0.0
    language: str = "unknown"
    lang_conf: float = 0.0
    word_count: int = 0
    context_boost: int = 0
    indirect: bool = False
    urgent: bool = False
    context_type: str = "general"
    context_confidence: float = 0.5
    analysis_time_ms: float = 0.0

    decision: str = "ignore"
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    score_details: Dict[str, float] = field(default_factory=dict)
    intent_verb: Optional[str] = None
    academic_object: Optional[str] = None
    urgency_marker: Optional[str] = None
    negation_detected: bool = False
    advert_score: float = 0.0

    # ── v14.2 additive fields ───────────────────────────────────────────────
    message_type: str = "general"
    message_type_confidence: float = 0.0
    distress_detected: bool = False
    new_request_after_resolution: bool = False
    duplicate_near: bool = False
    raw_text: str = ""
    normalized_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "reason": self.reason,
            "keyword": self.keyword,
            "score": self.score,
            "match_score": self.match_score,
            "spam_score": self.spam_score,
            "language": self.language,
            "lang_conf": self.lang_conf,
            "word_count": self.word_count,
            "context_boost": self.context_boost,
            "indirect": self.indirect,
            "urgent": self.urgent,
            "context_type": self.context_type,
            "context_confidence": self.context_confidence,
            "analysis_time_ms": self.analysis_time_ms,
            "decision": self.decision,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "score_details": self.score_details,
            "intent_verb": self.intent_verb,
            "academic_object": self.academic_object,
            "urgency_marker": self.urgency_marker,
            "negation_detected": self.negation_detected,
            "advert_score": self.advert_score,
            # v14.2
            "message_type": self.message_type,
            "message_type_confidence": self.message_type_confidence,
            "distress_detected": self.distress_detected,
            "new_request_after_resolution": self.new_request_after_resolution,
            "duplicate_near": self.duplicate_near,
            "raw_text": self.raw_text,
            "normalized_text": self.normalized_text,
        }


# =============================================================================
# Prefilter – فحص أولي سريع
# =============================================================================
class Prefilter:
    """Ultra-fast initial check to reject obviously invalid messages."""

    @staticmethod
    def check(text: str, min_words: int = 3, max_emojis: int = 5) -> Tuple[bool, str, Dict[str, Any]]:
        metadata = {
            "word_count": 0,
            "emoji_count": 0,
            "arabic_ratio": 0.0,
            "has_url": False,
            "has_phone": False,
        }

        if not text or len(text.strip()) < 2:
            return False, "empty_or_too_short", metadata

        words = text.split()
        word_count = len(words)
        metadata["word_count"] = word_count

        if word_count < min_words:
            return False, f"too_few_words_{word_count}", metadata

        emojis = EMOJI_PATTERN.findall(text)
        emoji_count = len(emojis)
        metadata["emoji_count"] = emoji_count

        if emoji_count > max_emojis:
            return False, f"too_many_emojis_{emoji_count}", metadata

        arabic_chars = sum(1 for c in text if '\u0600' <= c <= '\u06FF')
        arabic_ratio = arabic_chars / max(len(text), 1)
        metadata["arabic_ratio"] = arabic_ratio

        if arabic_ratio < 0.1:
            return False, "low_arabic_ratio", metadata

        metadata["has_url"] = bool(URL_PATTERN.search(text))
        metadata["has_phone"] = bool(PHONE_PATTERN.search(text))

        return True, "ok", metadata


# =============================================================================
# Optimized Bloom Filter (Thread-Safe)
# =============================================================================
class OptimizedBloomFilter:
    __slots__ = ("_size", "_hash_count", "_bit_array", "_lock", "_hash_cache", "_max_cache",
                 "_added_count", "_reset_threshold")

    def __init__(self, expected_items: int = 100_000, fp_rate: float = 0.001) -> None:
        self._size = self._optimal_size(expected_items, fp_rate)
        self._hash_count = self._optimal_hash_count(self._size, expected_items)
        self._bit_array = bytearray(self._size // 8 + 1)
        self._lock = asyncio.Lock()
        self._hash_cache: Dict[str, List[int]] = {}
        self._max_cache = 10_000
        self._added_count = 0
        self._reset_threshold = expected_items * 2

    @staticmethod
    def _optimal_size(n: int, p: float) -> int:
        return max(1024, int(-n * math.log(p) / (math.log(2) ** 2)))

    @staticmethod
    def _optimal_hash_count(m: int, n: int) -> int:
        return max(1, int(m / n * math.log(2)))

    def _hashes(self, item: str) -> List[int]:
        if item in self._hash_cache:
            return self._hash_cache[item]
        h = hashlib.sha256(item.encode()).hexdigest()
        h1, h2 = int(h[:16], 16), int(h[16:32], 16)
        hashes = [(h1 + i * h2) % self._size for i in range(self._hash_count)]
        if len(self._hash_cache) < self._max_cache:
            self._hash_cache[item] = hashes
        return hashes

    async def add(self, item: str) -> None:
        async with self._lock:
            for pos in self._hashes(item):
                self._bit_array[pos // 8] |= 1 << (pos % 8)
            self._added_count += 1
            if self._added_count > self._reset_threshold:
                self._bit_array = bytearray(self._size // 8 + 1)
                self._hash_cache.clear()
                self._added_count = 0

    async def contains(self, item: str) -> bool:
        async with self._lock:
            return all(
                self._bit_array[pos // 8] & (1 << (pos % 8))
                for pos in self._hashes(item)
            )

    async def clear(self) -> None:
        async with self._lock:
            self._bit_array = bytearray(self._size // 8 + 1)
            self._hash_cache.clear()
            self._added_count = 0


# =============================================================================
# Sharded LRU Cache (16 shards)
# =============================================================================
class ShardedLRUCache:
    def __init__(self, max_size: int = 10_000, ttl: int = 300, shards: int = 16) -> None:
        self._shards: List[OrderedDict] = [OrderedDict() for _ in range(shards)]
        self._max_per_shard = max(1, max_size // shards)
        self._ttl = ttl
        self._shard_locks = [asyncio.Lock() for _ in range(shards)]

    def _idx(self, key: str) -> int:
        return hash(key) % len(self._shards)

    async def get(self, key: str) -> Optional[Dict]:
        i = self._idx(key)
        async with self._shard_locks[i]:
            entry = self._shards[i].get(key)
            if entry:
                val, ts = entry
                if time.time() - ts < self._ttl:
                    self._shards[i].move_to_end(key)
                    return val
                del self._shards[i][key]
        return None

    async def set(self, key: str, value: Dict) -> None:
        i = self._idx(key)
        async with self._shard_locks[i]:
            cache = self._shards[i]
            if key in cache:
                cache.move_to_end(key)
            else:
                while len(cache) >= self._max_per_shard:
                    cache.popitem(last=False)
            cache[key] = (value, time.time())


# =============================================================================
# Aho-Corasick backed "WeightedTrie" – v14.2
#
# Public interface is unchanged from v14.1 (search_first / search_all, same
# (word, weight, start_position) return shape), so every existing call site
# in this file keeps working with zero changes. Internally, instead of
# re-walking a trie from every character offset in the text (O(n * L) where
# L is the longest keyword), this builds Aho-Corasick failure links once at
# construction time and does a single linear pass over the text at query
# time (O(n + matches)). For keyword sets in the hundreds-to-thousands range
# scanned on every message, this is the dominant cost in the old profile.
# =============================================================================
class _ACNode:
    __slots__ = ("children", "fail", "word", "weight", "word_len")

    def __init__(self) -> None:
        self.children: Dict[str, "_ACNode"] = {}
        self.fail: Optional["_ACNode"] = None
        self.word: Optional[str] = None
        self.weight: float = 1.0
        self.word_len: int = 0


# Kept as an alias so any external code that imports TrieNode directly does
# not break; it is otherwise unused now that WeightedTrie is Aho-Corasick.
TrieNode = _ACNode


class WeightedTrie:
    """Aho-Corasick multi-pattern matcher with the v14.1 WeightedTrie API."""

    def __init__(self, words: Set[str], weights: Optional[Dict[str, float]] = None) -> None:
        self._words = words
        self._weights = weights or {}
        self._root = _ACNode()
        self._built = False
        self._build()

    # ── construction ────────────────────────────────────────────────────
    def _build(self) -> None:
        for word in self._words:
            if not word:
                continue
            node = self._root
            for ch in word:
                node = node.children.setdefault(ch, _ACNode())
            node.word = word
            node.word_len = len(word)
            node.weight = self._weights.get(word, 1.0)

        # BFS to build fail links (standard Aho-Corasick construction).
        self._root.fail = self._root
        queue: deque = deque()
        for child in self._root.children.values():
            child.fail = self._root
            queue.append(child)

        while queue:
            current = queue.popleft()
            for ch, child in current.children.items():
                queue.append(child)
                fail_node = current.fail
                while fail_node is not self._root and ch not in fail_node.children:
                    fail_node = fail_node.fail
                child.fail = fail_node.children.get(ch, self._root) if fail_node is not child else self._root
                if child.fail is child:
                    child.fail = self._root
        self._built = True

    # ── queries ─────────────────────────────────────────────────────────
    def search_first(self, text: str) -> Optional[Tuple[str, float, int]]:
        if not self._words or not text:
            return None
        node = self._root
        limit = min(len(text), 4000)
        for i in range(limit):
            ch = text[i]
            while node is not self._root and ch not in node.children:
                node = node.fail
            node = node.children.get(ch, self._root)
            hit = node
            # A node itself, or any of its fail-chain ancestors, may terminate
            # a shorter suffix match ("dictionary suffix" links).
            while hit is not self._root:
                if hit.word is not None:
                    start = i - hit.word_len + 1
                    return (hit.word, hit.weight, start)
                hit = hit.fail
        return None

    def search_all(self, text: str) -> List[Tuple[str, float, int]]:
        results: List[Tuple[str, float, int]] = []
        if not self._words or not text:
            return results
        node = self._root
        limit = min(len(text), 4000)
        for i in range(limit):
            ch = text[i]
            while node is not self._root and ch not in node.children:
                node = node.fail
            node = node.children.get(ch, self._root)
            hit = node
            while hit is not self._root:
                if hit.word is not None:
                    start = i - hit.word_len + 1
                    results.append((hit.word, hit.weight, start))
                hit = hit.fail
        return results


# =============================================================================
# Main Filter Engine v14.2 – Template-Driven IntentEngine-NLP (hardened)
# =============================================================================
class EnhancedFilter:
    ARABIC_CHARS: Final[Set[str]] = set("ابتثجحخدذرزسشصضطظعغفقكلمنهويأإؤئآة")
    ARABIC_NORMALIZE: Final[Dict[int, int]] = str.maketrans(
        {"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي", "ئ": "ي", "ؤ": "و"}
    )

    _PATTERNS: Dict[str, re.Pattern] = {
        "phone": PHONE_PATTERN,
        "url": URL_PATTERN,
        "email": EMAIL_PATTERN,
        "emoji": EMOJI_PATTERN,
    }

    # ── ambiguous-term context resolution (spec section 17, "دكتور" rule) ──
    # Extendable without code changes if more ambiguous terms show up; keyed
    # by the ambiguous term itself.
    _AMBIGUOUS_TERM_CONTEXTS: Final[Dict[str, List[Tuple[Set[str], str, float]]]] = {
        "دكتور": [
            ({"مشروع", "محاضرة", "جامعة", "فصل", "مقرر", "تقرير"}, "professor", 0.9),
            ({"موعد", "عيادة", "مستشفى", "مريض", "دواء"}, "physician", 0.1),
            ({"مكتب", "خدمة", "تواصل", "اسعار", "خصم"}, "advertisement", -0.5),
        ],
    }

    def __init__(self) -> None:
        # تهيئة الإحصائيات مبكراً (قبل أي تحميل)
        self._stats: Dict[str, int] = {
            "processed": 0,
            "valid": 0,
            "rejected": 0,
            "spam": 0,
            "cache_hits": 0,
            "bloom_hits": 0,
            "fast_path": 0,
            "fuzzy_path": 0,
            "prefilter_rejected": 0,
            "accepted": 0,
            "review": 0,
            "ignored": 0,
            "total_time_ms": 0,
            "avg_time_ms": 0,
            "max_time_ms": 0,
            "min_time_ms": 999999,
            "template_patterns_generated": 0,
            "keyword_reloads": 0,
            # ── v14.2 counters ──────────────────────────────────────────
            "distress_overrides": 0,          # negation misfire prevented (distress instead)
            "new_request_after_resolution": 0,  # resolution overridden by a fresh request
            "near_duplicates_blocked": 0,     # Jaccard/simhash near-dup catches
            "ad_academic_dampened": 0,        # ad_score reduced due to strong academic context
            "ad_review_routed": 0,            # ad>0.6 & intent>=0.3 -> review instead of ignore
            "ambiguous_term_resolved": 0,     # e.g. "دكتور" disambiguated by context
        }
        self._stats_lock = asyncio.Lock()
        self._last_stats_reset = time.time()
        self._raw_keywords: Dict[str, Any] = KEYWORDS

        self._load_keyword_sets()
        self._build_tries()

        # Bloom Filter & Caches
        self._bloom = OptimizedBloomFilter(CFG.BLOOM_FILTER_SIZE, CFG.BLOOM_FILTER_FP)
        self._cache = ShardedLRUCache(CFG.MAX_CACHE_SIZE, CFG.CACHE_TTL)
        self._text_cache = TTLCache(maxsize=CFG.TEXT_CACHE_SIZE, ttl=CFG.TEXT_CACHE_TTL)
        self._cache_lock = asyncio.Lock()

        # v14.2: bounded ring buffer of recent (core-text) shingle sets, used
        # for near-duplicate detection that survives reformatting. Exact
        # duplicates are still caught cheaply by the bloom filter; this is
        # only consulted for messages that pass the bloom check.
        ring_size = getattr(CFG, "RECENT_MESSAGE_RING_SIZE", 500)
        self._recent_shingles: deque = deque(maxlen=ring_size)
        self._recent_lock = asyncio.Lock()

        # v14.2 smart-rule thresholds (spec section 17). Read via getattr
        # with the spec's own defaults so this NEVER raises AttributeError
        # against an existing config.py that predates these — item 16's
        # "don't break config.py compatibility" requirement. If/when
        # config.py adds these as real CFG fields, they take over
        # automatically since getattr always sees the live attribute first.
        self._ad_ignore_threshold = getattr(CFG, "AD_SCORE_IGNORE_THRESHOLD", 0.6)
        self._ad_review_intent_threshold = getattr(CFG, "AD_SCORE_REVIEW_INTENT_THRESHOLD", 0.3)
        self._ad_dampen_academic_threshold = getattr(CFG, "AD_DAMPEN_ACADEMIC_THRESHOLD", 0.7)
        self._ad_dampen_factor = getattr(CFG, "AD_DAMPEN_FACTOR", 0.5)

        logger.info(
            "Filter v14.2 ready | intent_verbs={} | academic_objects={} | negation={} | boost_patterns={} | "
            "distance_scoring={}",
            len(self._intent_verbs_all),
            len(self._academic_objects_all),
            len(self._negation_all),
            len(self._boost_patterns),
            "ON" if CFG.DISTANCE_SCORING_ENABLED else "OFF",
        )

    # ─── Load & Build ──────────────────────────────────────────────────────────

    def _generate_template_patterns(self, kw: Dict[str, Any]) -> Set[str]:
        """
        v14.0: Generate boost patterns from templates defined in keywords.json.
        This replaces the static high_confidence_boost_patterns list with
        dynamically generated combinations from word sets.

        v14.1: now takes `kw` explicitly (instead of reading the module-level
        KEYWORDS constant directly) so reload_keywords() can regenerate
        patterns from freshly-read data without restarting the process.
        """
        generated: Set[str] = set()
        templates_data = kw.get("templates", {})
        if not templates_data or not isinstance(templates_data, dict):
            return generated

        template_patterns_list = kw.get("template_patterns", [])
        if not template_patterns_list or not isinstance(template_patterns_list, list):
            return generated

        need: List[str] = templates_data.get("need", [])
        person: List[str] = templates_data.get("person", [])
        action: List[str] = templates_data.get("action", [])
        expert: List[str] = templates_data.get("expert", [])
        availability: List[str] = templates_data.get("availability", [])

        if not isinstance(need, list):
            need = []
        if not isinstance(person, list):
            person = []
        if not isinstance(action, list):
            action = []
        if not isinstance(expert, list):
            expert = []
        if not isinstance(availability, list):
            availability = []

        for pattern in template_patterns_list:
            if not isinstance(pattern, str):
                continue
            parts = pattern.strip().split()

            if len(parts) == 2:
                tag1, tag2 = parts[0], parts[1]

                if tag1 == "<need>" and tag2 == "<person>":
                    for n in need:
                        for p in person:
                            generated.add(f"{n} {p}")

                elif tag1 == "<need>" and tag2 == "<expert>":
                    for n in need:
                        for e in expert:
                            generated.add(f"{n} {e}")

                elif tag1 == "<availability>" and tag2 == "<person>":
                    for a in availability:
                        for p in person:
                            generated.add(f"{a} {p}")

                elif tag1 == "<availability>" and tag2 == "<action>":
                    for a in availability:
                        for act in action:
                            generated.add(f"{a} {act}")

                elif tag1 == "<availability>" and tag2 == "<expert>":
                    for a in availability:
                        for e in expert:
                            generated.add(f"{a} {e}")

            elif len(parts) == 3:
                tag1, tag2, tag3 = parts[0], parts[1], parts[2]

                if tag1 == "<need>" and tag2 == "<person>" and tag3 == "<action>":
                    for n in need:
                        for p in person:
                            for act in action:
                                generated.add(f"{n} {p} {act}")

                elif tag1 == "<availability>" and tag2 == "<person>" and tag3 == "<action>":
                    for a in availability:
                        for p in person:
                            for act in action:
                                generated.add(f"{a} {p} {act}")

        logger.info(
            "Template engine generated {} boost patterns from {} template(s) | "
            "need={} person={} action={} expert={} availability={}",
            len(generated),
            len(template_patterns_list),
            len(need),
            len(person),
            len(action),
            len(expert),
            len(availability),
        )

        return generated

    def _load_keyword_sets(self, keywords_data: Optional[Dict[str, Any]] = None) -> None:
        """
        تحميل جميع القوائم من مصدر بيانات الكلمات المفتاحية (متوافق مع v14.0.x).
        """
        kw = keywords_data if keywords_data is not None else KEYWORDS

        # 1. Intent Verbs (موزون)
        self._intent_verbs: Dict[str, Dict[str, Any]] = kw.get("intent_verbs", {})
        self._intent_verbs_all: Set[str] = set()
        self._intent_weights: Dict[str, float] = {}
        for tier, data in self._intent_verbs.items():
            if isinstance(data, dict) and "terms" in data:
                weight = data.get("_weight_hint", 0.7)
                for term in data.get("terms", []):
                    self._intent_verbs_all.add(term)
                    self._intent_weights[term] = weight

        # 2. Academic Objects (موزون)
        self._academic_objects: Dict[str, Dict[str, Any]] = kw.get("academic_objects", {})
        self._academic_objects_all: Set[str] = set()
        self._academic_weights: Dict[str, float] = {}
        for obj_type, data in self._academic_objects.items():
            if isinstance(data, dict) and "terms" in data:
                weight = data.get("_weight_hint", 0.7)
                for term in data.get("terms", []):
                    self._academic_objects_all.add(term)
                    self._academic_weights[term] = weight

        # 3. Request Phrases (مباشرة وغير مباشرة)
        request_phrases_data = kw.get("request_phrases", {})
        self._request_phrases_all: Set[str] = set()
        for category, phrases in request_phrases_data.items():
            if isinstance(phrases, list):
                self._request_phrases_all.update(phrases)

        self._indirect_request: List[str] = kw.get("indirect_request", [])
        self._indirect_request_all: Set[str] = set(self._indirect_request)

        # 4. Urgency Markers
        urgency_data = kw.get("urgency_markers", {})
        self._urgency_all: Set[str] = set()
        for category, markers in urgency_data.items():
            if isinstance(markers, list):
                self._urgency_all.update(markers)

        # v14.2: split urgency markers into "today-class" (factor 1.5) and
        # "week-class" (factor 1.2) per the temporal rule in the spec. Falls
        # back to matching-by-membership against the raw category name so
        # this works whether keywords.json labels them "today"/"tomorrow" or
        # something else, as long as the terms themselves are recognizable.
        self._urgency_today: Set[str] = {"اليوم", "بكره", "بكرة", "الليله", "الليلة", "حالا", "الان", "الآن"}
        self._urgency_week: Set[str] = {
            "الاسبوع الجاي", "الأسبوع الجاي", "الاسبوع القادم", "الأسبوع القادم", "بعد اسبوع", "بعد أسبوع",
        }
        for category, markers in urgency_data.items():
            if not isinstance(markers, list):
                continue
            cat_l = str(category).lower()
            if "today" in cat_l or "tomorrow" in cat_l or "now" in cat_l:
                self._urgency_today.update(markers)
            elif "week" in cat_l:
                self._urgency_week.update(markers)

        # 5. Negation
        self._negation: Dict[str, Any] = kw.get("negation", {})
        self._negation_all: Set[str] = set()
        self._negation_exceptions: Set[str] = set()
        self._resolution_phrases: Set[str] = set()

        pre_verb = self._negation.get("pre_verb_negators", {})
        if isinstance(pre_verb, dict) and "terms" in pre_verb:
            self._negation_all.update(pre_verb.get("terms", []))

        post_clause = self._negation.get("post_clause_negators", [])
        if isinstance(post_clause, list):
            self._negation_all.update(post_clause)

        exceptions = self._negation.get("negation_exceptions", [])
        if isinstance(exceptions, list):
            self._negation_exceptions.update(exceptions)

        resolution = self._negation.get("resolution_phrases", [])
        if isinstance(resolution, list):
            self._resolution_phrases.update(resolution)

        # v14.2: distress phrases — "ما عرفت / ما فهمت / ما قدرت" are NOT
        # negation when followed by academic context; they are a clear
        # expression that the person is stuck and needs help. These are
        # sourced from negation_exceptions when present (so keywords.json
        # stays the single source of truth) plus a safe built-in fallback.
        self._distress_phrases: Set[str] = {"ما عرفت", "ما فهمت", "ما قدرت"}
        self._distress_phrases.update(
            p for p in self._negation_exceptions if isinstance(p, str)
        )

        # 6. High Confidence Boost Patterns (static + template-generated)
        boost_data = kw.get("high_confidence_boost_patterns", {})
        self._boost_patterns: Set[str] = set()
        if isinstance(boost_data, dict):
            patterns = boost_data.get("patterns", [])
            if isinstance(patterns, list):
                self._boost_patterns.update(patterns)

        template_generated = self._generate_template_patterns(kw)
        self._boost_patterns.update(template_generated)
        self._stats["template_patterns_generated"] = len(template_generated)

        # 7. Advertisement Signals
        self._ad_signals: Dict[str, Any] = kw.get("advertisement_signals", {})

        # 8. Spam Categories
        self._spam_categories: Dict[str, List[str]] = kw.get("spam_categories", {})
        self._spam_all: Set[str] = set()
        for category, terms in self._spam_categories.items():
            if isinstance(terms, list):
                self._spam_all.update(terms)

        # 9. Emoji Signals
        emoji_data = kw.get("emoji_signals", {})
        self._ad_emoji: Set[str] = set(emoji_data.get("ad_style_emoji", []))
        self._neutral_emoji: Set[str] = set(emoji_data.get("neutral_emoji", []))

        # 10. Ad Blockers
        self._ad_blockers: Set[str] = set(kw.get("ad_blockers", []))

        # 11. Ignore Signals
        ignore_data = kw.get("ignore_signals", {})
        self._ignore_all: Set[str] = set()
        for category, terms in ignore_data.items():
            if isinstance(terms, list):
                self._ignore_all.update(terms)

        # 12. Help Expressions
        self._help_expressions: Set[str] = set(kw.get("help_expressions", []))

        # 13. Action Verbs
        action_verbs_data = kw.get("action_verbs", {})
        self._action_verbs: Set[str] = set()
        for key in ["core", "suffixed_forms", "imperative_forms"]:
            if isinstance(action_verbs_data.get(key), list):
                self._action_verbs.update(action_verbs_data.get(key, []))

        # 14. Subject Markers
        subject_data = kw.get("subject_markers", {})
        self._subject_markers: Set[str] = set()
        for key in ["student_pronouns", "student_question_subject"]:
            if isinstance(subject_data.get(key), list):
                self._subject_markers.update(subject_data.get(key, []))

        # 15. Implicit Request Patterns
        implicit_data = kw.get("implicit_request_patterns", {})
        self._implicit_request_all: Set[str] = set()
        for key in ["availability_question", "problem_state"]:
            if isinstance(implicit_data.get(key), list):
                self._implicit_request_all.update(implicit_data.get(key, []))

        # 16. Solve Actions
        solve_data = kw.get("solve_actions", {})
        self._solve_academic: Set[str] = set()
        self._technical_problem_terms: Set[str] = set()
        if isinstance(solve_data.get("academic_solution"), list):
            self._solve_academic.update(solve_data.get("academic_solution", []))
        if isinstance(solve_data.get("technical_problem_terms"), list):
            self._technical_problem_terms.update(solve_data.get("technical_problem_terms", []))

        # 17. Dialect Mapping
        self._dialect_map: Dict[str, str] = {}
        dialect_data = kw.get("dialect_mapping", {})
        for category, mapping in dialect_data.items():
            if isinstance(mapping, dict):
                self._dialect_map.update(mapping)

        # 18. University Context
        self._university_context: Set[str] = set()
        university_data = kw.get("university_context", {})
        for key, value in university_data.items():
            if isinstance(value, list):
                self._university_context.update(value)

        # 19. Distance Scoring Config
        self._distance_config: Dict[str, Any] = kw.get("distance_scoring_config", {})

        # 20. Length Modifier
        self._length_modifier: Dict[Tuple[int, int], float] = {}
        length_data = kw.get("length_modifier", {})
        for key, value in length_data.items():
            if isinstance(key, str) and "_to_" in key:
                parts = key.split("_to_")
                try:
                    start = int(parts[0])
                    end = int(parts[1])
                    self._length_modifier[(start, end)] = float(value)
                except Exception:
                    pass

        # 21. Scoring Weights
        self._scoring_weights: Dict[str, float] = {}
        weights_data = kw.get("scoring_weights", {})
        positive_modules = weights_data.get("positive_modules", {})
        if isinstance(positive_modules, dict):
            for key, value in positive_modules.items():
                self._scoring_weights[key] = float(value)

        # 22. Clause boundaries (for negation)
        self._clause_boundaries: Set[str] = set()
        boundaries = self._negation.get("clause_boundaries", [])
        if isinstance(boundaries, list):
            self._clause_boundaries.update(boundaries)
        # spec explicitly calls out "لكن", "بس", "الا" — make sure they're
        # present even if keywords.json's list is sparse.
        self._clause_boundaries.update({"لكن", "بس", "الا", "إلا"})

        # ── تحويل البيانات الجديدة إلى القوائم القديمة (للتوافق) ──

        self.request_words: Set[str] = set(self._intent_verbs_all).union(self._request_phrases_all)
        self.context_words: Set[str] = set(self._academic_objects_all).union(self._university_context)
        self.indirect_words: Set[str] = set(self._indirect_request_all).union(self._implicit_request_all)
        self.urgency_words: Set[str] = self._urgency_all
        self.ignore_words: Set[str] = self._ignore_all

        self.advertisement_words: Set[str] = set()
        for signal_list in ["hard_signals", "medium_signals"]:
            signals = self._ad_signals.get(signal_list, [])
            if isinstance(signals, list):
                self.advertisement_words.update(signals)

        self.education_words: Set[str] = set(self._ad_signals.get("institution_terms", []))
        self.emoji_advertisement: Set[str] = self._ad_emoji
        self.ad_blockers: Set[str] = self._ad_blockers
        self.spam_patterns: Set[str] = self._spam_all

        # ── Backward compatibility: old-format keywords ────────────────────
        old_request = set(kw.get("request", []))
        old_context = set(kw.get("request_context", []))
        old_indirect = set(kw.get("indirect_request", []))
        old_urgency = set(kw.get("urgency", []))
        old_ignore = set(kw.get("ignore", []))
        old_ad = set(kw.get("advertisement", []))
        old_edu = set(kw.get("education_providers", []))
        old_emoji = set(kw.get("emoji_advertisement", []))
        old_blockers = set(kw.get("ad_blockers", []))
        old_spam = set(kw.get("spam_patterns", []))

        self.request_words.update(old_request)
        self.context_words.update(old_context)
        self.indirect_words.update(old_indirect)
        self.urgency_words.update(old_urgency)
        self.ignore_words.update(old_ignore)
        self.advertisement_words.update(old_ad)
        self.education_words.update(old_edu)
        self.emoji_advertisement.update(old_emoji)
        self.ad_blockers.update(old_blockers)
        self.spam_patterns.update(old_spam)

        self.request_words = set(self.request_words)
        self.context_words = set(self.context_words)
        self.indirect_words = set(self.indirect_words)
        self.urgency_words = set(self.urgency_words)
        self.ignore_words = set(self.ignore_words)
        self.advertisement_words = set(self.advertisement_words)
        self.education_words = set(self.education_words)
        self.emoji_advertisement = set(self.emoji_advertisement)
        self.ad_blockers = set(self.ad_blockers)
        self.spam_patterns = set(self.spam_patterns)
        self._boost_patterns = set(self._boost_patterns)

        # Build all tries (Aho-Corasick under the hood as of v14.2 — see
        # WeightedTrie above; interface is unchanged).
        self._request_trie = WeightedTrie(self.request_words, self._intent_weights)
        self._context_trie = WeightedTrie(self.context_words, self._academic_weights)
        self._indirect_trie = WeightedTrie(self.indirect_words)
        self._urgency_trie = WeightedTrie(self.urgency_words)
        self._ignore_trie = WeightedTrie(self.ignore_words)
        self._ad_trie = WeightedTrie(self.advertisement_words)
        self._education_trie = WeightedTrie(self.education_words)

        self._negation_trie = WeightedTrie(self._negation_all)
        self._distress_trie = WeightedTrie(self._distress_phrases)
        self._resolution_trie = WeightedTrie(self._resolution_phrases)
        self._boost_trie = WeightedTrie(self._boost_patterns)
        self._implicit_trie = WeightedTrie(self._implicit_request_all)
        self._spam_trie = WeightedTrie(self._spam_all)
        self._ad_blocker_trie = WeightedTrie(self._ad_blockers)

        self._subject_markers_trie = WeightedTrie(self._subject_markers)
        self._action_verbs_trie = WeightedTrie(self._action_verbs)

        self._raw_keywords = kw

    def _build_tries(self) -> None:
        """
        Tries are built as part of _load_keyword_sets() itself. Kept as a
        separate no-op call point so any external caller expecting a
        `_build_tries()` method to exist does not hit an AttributeError.
        """
        pass

    def reload_keywords(self, path: str = "keywords.json") -> None:
        """
        Re-reads keywords.json from disk and rebuilds every keyword
        set/trie in place, including regenerating template-boost patterns.
        """
        from config import load_keywords
        fresh = load_keywords(path)
        self._load_keyword_sets(fresh)
        self._build_tries()
        self._stats["keyword_reloads"] = self._stats.get("keyword_reloads", 0) + 1
        logger.info(
            "Filter keyword sets reloaded from {} | templates={} entries | template_patterns={} entries | "
            "boost_patterns_total={}",
            path,
            len(fresh.get("templates", {})) if isinstance(fresh.get("templates"), (list, dict)) else 0,
            len(fresh.get("template_patterns", [])) if isinstance(fresh.get("template_patterns"), (list, dict)) else 0,
            len(self._boost_patterns),
        )

    # Backward/forward-compatible alias.
    _build_keyword_sets = reload_keywords

    # ─── Normalization ─────────────────────────────────────────────────────────

    def _normalize_arabic(self, text: str) -> str:
        text = text.translate(self.ARABIC_NORMALIZE)
        if PYARABIC_AVAILABLE:
            try:
                text = araby.strip_tashkeel(text)
                text = araby.strip_tatweel(text)
            except Exception:
                pass
        return text

    def _apply_dialect_mapping(self, text: str) -> str:
        for variant, canonical in self._dialect_map.items():
            if variant in text:
                text = text.replace(variant, canonical)
        return text

    def _split_glued_tokens(self, text: str) -> str:
        """
        v14.2: inserts a space at Arabic<->Latin/digit boundaries so
        code-switched or glued input ("اليوماحتاج", "assignment3",
        "الواجبassignment") tokenizes and trie-matches correctly. Pure-Arabic
        or pure-Latin text is untouched (no boundaries to split).
        """
        return _GLUE_BOUNDARY.sub(" ", text)

    def _clean(self, text: str) -> str:
        text = WS_PATTERN.sub(" ", text).strip()
        text = text.lower()
        text = self._split_glued_tokens(text)
        text = WS_PATTERN.sub(" ", text).strip()
        text = self._normalize_arabic(text)
        text = self._apply_dialect_mapping(text)
        return text

    # ─── Core Text Extraction (forwarded-message stripping) ────────────────────

    def _extract_core_text(self, text: str) -> str:
        """
        v14.2: strips forwarding chrome ("الرسالة:", "عرض الرسالة الأصلية",
        "tg://...", "👤: ...", "『...』") before duplicate/hash analysis, so
        the same underlying message forwarded through different clients (or
        with different wrapper text) hashes to the same core content. Also
        strips general punctuation here (but NOT from `cleaned`, which is
        used for keyword/trie matching where punctuation like the dot in
        "bit.ly" is meaningful) — punctuation differences are exactly the
        kind of formatting noise the duplicate/near-duplicate check needs to
        see past.
        """
        core = text
        for pattern in _FORWARD_CHROME_PATTERNS:
            core = pattern.sub(" ", core)
        core = _PUNCTUATION_PATTERN.sub(" ", core)
        core = WS_PATTERN.sub(" ", core).strip()
        return core

    @staticmethod
    def _shingles(text: str, n: int = 3) -> Set[str]:
        """Word n-gram shingles used for Jaccard near-duplicate detection."""
        tokens = text.split()
        if len(tokens) < n:
            return {" ".join(tokens)} if tokens else set()
        return {" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}

    @staticmethod
    def _contains_as_words(phrase: str, text: str) -> bool:
        """
        v14.2: word-boundary-aware substring check, used for short negation
        particles ("ما", "مو", "مب") that are otherwise prone to false
        positives as a bare `in` substring check — e.g. the negator "مو"
        is a substring of "موعد" (appointment), "موضوع" (topic/subject),
        etc. and must NOT fire negation there. Multi-word phrases are
        matched as a contiguous token subsequence.
        """
        phrase_tokens = phrase.split()
        if not phrase_tokens:
            return False
        text_tokens = text.split()
        n = len(phrase_tokens)
        for i in range(len(text_tokens) - n + 1):
            if text_tokens[i:i + n] == phrase_tokens:
                return True
        return False

    @staticmethod
    def _jaccard(a: Set[str], b: Set[str]) -> float:
        if not a or not b:
            return 0.0
        inter = len(a & b)
        union = len(a | b)
        return inter / union if union else 0.0

    async def _check_near_duplicate(self, core_text: str) -> bool:
        """
        v14.2: in addition to the exact-hash bloom filter, compares this
        message's shingle set against a bounded ring buffer of recent
        messages via Jaccard similarity, so re-formatted duplicates (extra
        whitespace, punctuation, emoji, or re-wrapped forwards) that
        wouldn't hash-match are still caught. Threshold from spec: > 0.85.
        """
        shingles = self._shingles(core_text)
        if not shingles:
            return False
        threshold = getattr(CFG, "DUPLICATE_JACCARD_THRESHOLD", 0.85)
        async with self._recent_lock:
            for prior in self._recent_shingles:
                if self._jaccard(shingles, prior) > threshold:
                    return True
            self._recent_shingles.append(shingles)
        return False

    # ─── Language Detection ────────────────────────────────────────────────────

    def _is_arabic(self, text: str) -> Tuple[bool, float]:
        if not text:
            return False, 0.0
        count = sum(1 for c in text if c in self.ARABIC_CHARS)
        ratio = count / max(len(text), 1)
        if ratio > 0.35:
            return True, ratio
        if ratio < 0.12:
            if LANGDETECT_AVAILABLE:
                try:
                    if detect(text) == "ar":
                        return True, 0.9
                except Exception:
                    pass
            return False, ratio
        if LANGDETECT_AVAILABLE:
            try:
                lang = detect(text)
                return lang == "ar", 0.85 if lang == "ar" else 0.6
            except Exception:
                pass
        return ratio > 0.25, ratio

    # ─── Spam Score ────────────────────────────────────────────────────────────

    def _spam_score(self, text: str) -> float:
        score = 0.0
        if PHONE_PATTERN.search(text):
            score += 0.3
        url_count = len(URL_PATTERN.findall(text))
        score += min(0.4, url_count * 0.2)
        emoji_count = len(EMOJI_PATTERN.findall(text))
        score += min(0.2, emoji_count * 0.04)
        if re.search(r"(.)\1{4,}", text):
            score += 0.15
        return min(score, 1.0)

    # ─── Ad Detection ──────────────────────────────────────────────────────────

    def _detect_advertisement(self, text: str) -> Tuple[float, List[str]]:
        ad_score = 0.0
        reasons = []

        hard_signals = self._ad_signals.get("hard_signals", [])
        if isinstance(hard_signals, list):
            for signal in hard_signals:
                if signal in text:
                    ad_score += 0.4
                    reasons.append(f"hard_ad_signal: {signal}")

        medium_signals = self._ad_signals.get("medium_signals", [])
        if isinstance(medium_signals, list):
            for signal in medium_signals:
                if signal in text:
                    ad_score += 0.2
                    reasons.append(f"medium_ad_signal: {signal}")

        provider_data = self._ad_signals.get("provider_profile", {})
        strong_providers = provider_data.get("strong_provider", [])
        weak_providers = provider_data.get("weak_provider", [])
        individual_providers = provider_data.get("individual_provider", [])

        strong_count = sum(1 for p in strong_providers if p in text)
        weak_count = sum(1 for p in weak_providers if p in text)
        individual_count = sum(1 for p in individual_providers if p in text)

        if strong_count > 0 or individual_count > 0:
            ad_score += 0.3
            reasons.append("provider_detected")
        elif weak_count >= CFG.AD_WEAK_PROVIDER_THRESHOLD:
            ad_score += 0.25
            reasons.append("weak_provider_multiple")

        cta_signals = self._ad_signals.get("cta_signals", [])
        if isinstance(cta_signals, list):
            for signal in cta_signals:
                if signal in text:
                    ad_score += 0.1
                    reasons.append(f"cta_signal: {signal}")

        price_signals = self._ad_signals.get("price_signals", {})
        if isinstance(price_signals, dict):
            payment_methods = price_signals.get("payment_methods", {})
            if isinstance(payment_methods, dict):
                terms = payment_methods.get("terms", [])
                if isinstance(terms, list) and (strong_count > 0 or individual_count > 0):
                    for term in terms:
                        if term in text:
                            ad_score += 0.1
                            reasons.append(f"payment_signal: {term}")

        institution_terms = self._ad_signals.get("institution_terms", [])
        if isinstance(institution_terms, list):
            for term in institution_terms:
                if term in text:
                    ad_score += 0.15
                    reasons.append(f"institution_term: {term}")

        for pattern in self._ad_blockers:
            if pattern in text:
                ad_score += 0.2
                reasons.append(f"url_signal: {pattern}")

        ad_emoji_count = sum(1 for emoji in self._ad_emoji if emoji in text)
        if ad_emoji_count >= CFG.AD_EMOJI_THRESHOLD:
            ad_score += 0.2
            reasons.append(f"ad_emoji_count: {ad_emoji_count}")

        return min(ad_score, 1.0), reasons

    # ─── Context-Aware Message-Type Classification (v14.2, new) ───────────────

    def _classify_message_type(
        self,
        text: str,
        has_intent: bool,
        has_academic: bool,
        ad_score: float,
        spam_hit: bool,
    ) -> Tuple[str, float]:
        """
        v14.2: cheap first-pass classification of what *kind* of message this
        is — academic_help / advertisement / social_chat / spam / general —
        run BEFORE the individual signal interpreters decide anything. This
        lets downstream rules (ad dampening, negation-vs-distress) condition
        on message type instead of reacting to isolated keyword hits.

        This is intentionally a simple score comparison, not a separate ML
        pass — it reuses signals already being computed in analyze() so it
        adds no extra trie scans.
        """
        scores = {
            "academic_help": (0.6 if has_intent else 0.0) + (0.4 if has_academic else 0.0),
            "advertisement": ad_score,
            "spam": 1.0 if spam_hit else 0.0,
            "social_chat": 0.3 if (not has_intent and not has_academic and ad_score < 0.2) else 0.0,
        }
        best_type = max(scores, key=scores.get)
        best_score = scores[best_type]
        if best_score <= 0.0:
            return "general", 0.0
        return best_type, min(best_score, 1.0)

    # ─── Negation / Distress Detection ─────────────────────────────────────────

    def _detect_negation(self, text: str) -> Tuple[bool, float, List[str], bool]:
        """
        Returns (is_negated, negation_score, reasons, is_distress).

        v14.2 order of checks, per spec section 17:
          1. negation_exceptions (never negation)
          2. distress_trie ("ما عرفت"/"ما فهمت"/"ما قدرت" + academic word):
             flagged as distress, NOT negation — confidence is not reduced
             for this case, it's the opposite of a "stop, they're done" signal.
          3. general negation rule (pre-verb / post-clause / resolution).
        """
        reasons: List[str] = []

        # 1) Exceptions always win first, regardless of what else matches.
        for ex in self._negation_exceptions:
            if ex in text:
                # An exception phrase alone isn't necessarily distress unless
                # it also carries academic context — checked next.
                has_academic_ctx = bool(self._context_trie.search_first(text))
                if has_academic_ctx:
                    reasons.append(f"distress_exception: {ex}")
                    return False, 0.0, reasons, True
                return False, 0.0, [], False

        # 2) Distress trie: "ما عرفت" / "ما فهمت" / "ما قدرت" + any academic word.
        distress_match = self._distress_trie.search_first(text)
        if distress_match:
            has_academic_ctx = bool(self._context_trie.search_first(text))
            if has_academic_ctx:
                reasons.append(f"distress: {distress_match[0]}")
                return False, 0.0, reasons, True
            # Distress phrase with no academic anchor at all — treat as a
            # weak, non-blocking signal rather than negation.
            return False, 0.0, [], False

        # 3) Resolution phrases ("تم الحل", "لقيت حل") — clause-level check
        #    for a *new* request happens in analyze() via
        #    _check_new_request_after_resolution(); here we only report the
        #    raw resolution signal.
        resolution_match = self._resolution_trie.search_first(text)
        if resolution_match:
            reasons.append(f"resolution_phrase: {resolution_match[0]}")
            return True, 1.0, reasons, False

        # 4) General post-clause negators.
        post_clause = self._negation.get("post_clause_negators", [])
        if isinstance(post_clause, list):
            for neg in post_clause:
                if self._contains_as_words(neg, text):
                    reasons.append(f"post_clause_negator: {neg}")
                    return True, 0.8, reasons, False

        # 5) General pre-verb negators, still honoring clause boundaries.
        # v14.2: word-boundary matched (_contains_as_words), not a plain
        # substring `in` check — short negators like "مو" are otherwise a
        # false-positive substring hit inside unrelated words such as
        # "موعد" (appointment) or "موضوع" (topic).
        pre_verb_data = self._negation.get("pre_verb_negators", {})
        if isinstance(pre_verb_data, dict):
            pre_verbs = pre_verb_data.get("terms", [])
            if isinstance(pre_verbs, list):
                for pv in pre_verbs:
                    if self._contains_as_words(pv, text):
                        if CFG.NEGATION_CLAUSE_BOUNDARIES_ENABLED:
                            tokens = text.split()
                            pv_tokens = pv.split()
                            idx = next(
                                (i for i in range(len(tokens) - len(pv_tokens) + 1)
                                 if tokens[i:i + len(pv_tokens)] == pv_tokens),
                                None,
                            )
                            before_tokens = tokens[:idx] if idx is not None else []
                            before_text = " ".join(before_tokens)
                            for boundary in self._clause_boundaries:
                                if self._contains_as_words(boundary, before_text):
                                    return False, 0.0, [], False
                        reasons.append(f"pre_verb_negator: {pv}")
                        return True, 0.6, reasons, False

        return False, 0.0, reasons, False

    def _check_new_request_after_resolution(
        self, text: str, resolution_word: Optional[str]
    ) -> Optional[Tuple[str, int]]:
        """
        v14.2, spec section 17 "قاعدة الجملة المستقلة":
        resolution_phrase + clause_boundary ("لكن"/"بس"/"الا") + intent_verb
        AFTER the boundary => this is a new, independent request. The
        resolution signal must be discarded and the new request processed
        instead. Returns (intent_word, position) of the new request if found
        in text *after* a clause boundary that itself comes after the
        resolution phrase, else None.
        """
        if not resolution_word:
            return None
        res_pos = text.find(resolution_word)
        if res_pos < 0:
            return None
        after_resolution = text[res_pos + len(resolution_word):]

        for boundary in self._clause_boundaries:
            b_pos = after_resolution.find(boundary)
            if b_pos < 0:
                continue
            after_boundary = after_resolution[b_pos + len(boundary):]
            new_intent = self._request_trie.search_first(after_boundary)
            if new_intent:
                # Recompute absolute position in the original text for
                # downstream distance scoring.
                abs_pos = res_pos + len(resolution_word) + b_pos + len(boundary) + new_intent[2]
                return (new_intent[0], abs_pos)
        return None

    # ─── Ambiguous-Term Context Resolution (v14.2, new) ────────────────────────

    def _resolve_ambiguous_terms(self, text: str) -> Dict[str, Tuple[str, float]]:
        """
        Spec section 17 "قاعدة السياق للكلمات الملتبسة": resolves ambiguous
        terms (currently: "دكتور") into a sense + academic-weight adjustment
        based on nearby context words. Returns {term: (sense, weight_delta)}.
        An unresolved ambiguous term (no context word present) maps to
        ("ambiguous", 0.0) so callers can route it to manual review.
        """
        resolved: Dict[str, Tuple[str, float]] = {}
        for term, context_rules in self._AMBIGUOUS_TERM_CONTEXTS.items():
            if term not in text:
                continue
            sense_found = False
            for context_words, sense, weight_delta in context_rules:
                if any(cw in text for cw in context_words):
                    resolved[term] = (sense, weight_delta)
                    sense_found = True
                    break
            if not sense_found:
                resolved[term] = ("ambiguous", 0.0)
        return resolved

    # ─── Temporal Urgency Factor (v14.2, new) ──────────────────────────────────

    def _urgency_factor(self, text: str) -> float:
        """Spec section 17 "قاعدة الزمن"."""
        if any(term in text for term in self._urgency_today):
            return 1.5
        if any(term in text for term in self._urgency_week):
            return 1.2
        return 1.0

    # ─── Conditional Confidence (v14.2, replaces simple averaging) ────────────

    def _conditional_confidence(
        self,
        has_intent: bool,
        has_academic: bool,
        intent_weight: float,
        academic_weight: float,
        urgency_factor: float,
        context_component: float,
    ) -> float:
        """
        Spec section 17 "قاعدة الثقة الشرطية":
            P(help) = P(intent) * P(academic|intent) * urgency_factor * context_factor
        with the specified bands:
          - intent only            -> 0.10–0.30
          - academic only          -> 0.05–0.20
          - intent + academic      -> 0.50–0.90
          - intent + academic + urgency -> 0.80–0.95
        This replaces the old `(legacy_confidence + weighted_confidence) / 2`
        blind average, which treated intent and academic-context as
        independent additive terms even though the whole point of the
        signal is that an intent verb *anchored to* an academic object is
        categorically more meaningful than either alone.
        """
        context_factor = 0.5 + 0.5 * max(0.0, min(1.0, context_component))
        urgent = urgency_factor > 1.0

        if has_intent and has_academic:
            base_low, base_high = (0.8, 0.95) if urgent else (0.5, 0.9)
            base = base_low + (base_high - base_low) * ((intent_weight + academic_weight) / 2.0)
        elif has_intent and not has_academic:
            base = 0.1 + 0.2 * intent_weight
        elif has_academic and not has_intent:
            base = 0.05 + 0.15 * academic_weight
        else:
            base = 0.0

        confidence = base * min(urgency_factor, 1.5) / 1.5 * context_factor / 0.75
        # The /1.5 and /0.75 normalize the multipliers back into the target
        # bands above rather than letting them silently overshoot 0.95.
        return max(0.0, min(0.95 if (has_intent and has_academic) else 0.3, confidence))

    # ─── Distance Scoring ─────────────────────────────────────────────────────

    def _calculate_distance_score(self, intent_pos: int, academic_pos: int, text_len: int) -> float:
        distance = abs(intent_pos - academic_pos)
        thresholds = self._distance_config.get("thresholds", {})

        for range_str, data in thresholds.items():
            if "-" in range_str or "_to_" in range_str:
                try:
                    start_str, end_str = range_str.split("_to_")
                    start, end = int(start_str), int(end_str)
                    if start <= distance <= end:
                        return float(data.get("score_multiplier", 1.0))
                except Exception:
                    continue
            elif range_str == "16_plus":
                if distance >= 16:
                    return float(data.get("score_multiplier", 0.15))

        if text_len > 100:
            if distance <= 10:
                return 0.9
            elif distance <= 20:
                return 0.7
            else:
                return 0.4
        else:
            if distance <= 5:
                return 1.0
            elif distance <= 10:
                return 0.8
            else:
                return 0.5

    # ─── Length Modifier ──────────────────────────────────────────────────────

    def _get_length_modifier(self, token_count: int) -> float:
        for (start, end), value in self._length_modifier.items():
            if start <= token_count <= end:
                return value
        return 0.9

    # ─── Main Analysis ─────────────────────────────────────────────────────────

    async def analyze(self, text: str) -> Dict[str, Any]:
        start = time.perf_counter()

        try:
            if len(text) > CFG.MAX_MESSAGE_LENGTH:
                return self._result("ignore", 0.0, ["too_long"])

            validated = InputSanitizer.validate_message_text(text)
            if validated is None:
                return self._result("ignore", 0.0, ["invalid_input"])

            raw_text = validated
            cleaned = self._clean(validated)
            core_text = self._extract_core_text(cleaned)
            cache_key = hashlib.blake2b(core_text.encode(), digest_size=16).hexdigest()[:32]

            # ── True early exits: "there is nothing to classify here" ──────
            # These are NOT classification signals (they don't compete with
            # intent/academic/ad/negation for the final decision), so they
            # stay as fast, unconditional exits.

            # Prefilter
            if CFG.PREFILTER_ENABLED:
                ok, reason, metadata = Prefilter.check(
                    cleaned, CFG.PREFILTER_MIN_WORDS, CFG.PREFILTER_MAX_EMOJIS
                )
                if not ok:
                    async with self._stats_lock:
                        self._stats["prefilter_rejected"] += 1
                    return self._result("ignore", 0.0, [reason])

            # Bloom Filter (exact duplicate, by core text)
            if await self._bloom.contains(cache_key):
                async with self._stats_lock:
                    self._stats["bloom_hits"] += 1
                return self._result("ignore", 0.0, ["duplicate"])

            # Text Cache
            async with self._cache_lock:
                if cache_key in self._text_cache:
                    async with self._stats_lock:
                        self._stats["cache_hits"] += 1
                    result = dict(self._text_cache[cache_key])
                    result["analysis_time_ms"] = round((time.perf_counter() - start) * 1000, 2)
                    return result

            await self._bloom.add(cache_key)

            # Near-duplicate check (reformatted forwards etc.) — only once
            # we know it's not an exact duplicate.
            is_near_duplicate = await self._check_near_duplicate(core_text)
            if is_near_duplicate:
                async with self._stats_lock:
                    self._stats["near_duplicates_blocked"] += 1
                return self._result("ignore", 0.0, ["near_duplicate"])

            async with self._stats_lock:
                self._stats["processed"] += 1

            # Language
            is_arabic, arabic_ratio = self._is_arabic(cleaned)
            if CFG.LANGUAGE_FILTER and not is_arabic:
                return self._result("ignore", 0.0, ["non_arabic"])

            # ── Signal gathering (no early exits from here on) ─────────────
            # v14.2: every signal below used to be able to short-circuit the
            # function before the others were even computed. They are now
            # all collected first; _decide() below performs weighted voting
            # once every signal is known, per spec section 2.

            spam_score_raw = self._spam_score(cleaned)
            spam_trie_hit = self._spam_trie.search_first(cleaned)
            is_spam_signal = spam_score_raw > CFG.SPAM_SCORE_THRESHOLD or bool(spam_trie_hit)

            ignore_hit = bool(self._ignore_trie.search_first(cleaned))
            ad_blocker_hit = bool(self._ad_blocker_trie.search_first(cleaned))

            is_negated, neg_score, neg_reasons, is_distress = self._detect_negation(cleaned)

            intent_match = self._request_trie.search_first(cleaned)
            indirect_match = self._indirect_trie.search_first(cleaned)
            urgency_match = self._urgency_trie.search_first(cleaned)
            implicit_match = self._implicit_trie.search_first(cleaned)
            boost_match = self._boost_trie.search_first(cleaned)
            context_matches = self._context_trie.search_all(cleaned)
            academic_match = context_matches[0] if context_matches else None

            intent_word = intent_match[0] if intent_match else None
            intent_pos = intent_match[2] if intent_match else None
            intent_weight = self._intent_weights.get(intent_word, 0.7) if intent_word else 0.0

            academic_word = academic_match[0] if academic_match else None
            academic_pos = academic_match[2] if academic_match else None
            academic_weight = self._academic_weights.get(academic_word, 0.7) if academic_word else 0.0

            urgency_marker = urgency_match[0] if urgency_match else None
            urgent = urgency_match is not None
            is_implicit = implicit_match is not None
            boost = 0.25 if boost_match else 0.0

            ad_score, ad_reasons = self._detect_advertisement(cleaned)

            has_intent = intent_word is not None
            has_academic = academic_word is not None

            # ── Clause-level override: resolution + new request ────────────
            new_request_info = None
            if is_negated and neg_score >= 1.0:  # a resolution_phrase fired
                resolution_word = None
                for r in neg_reasons:
                    if r.startswith("resolution_phrase: "):
                        resolution_word = r.split(": ", 1)[1]
                        break
                new_request_info = self._check_new_request_after_resolution(cleaned, resolution_word)
                if new_request_info:
                    # Discard the resolution signal; treat as a new request.
                    is_negated = False
                    neg_score = 0.0
                    intent_word, new_pos = new_request_info
                    intent_pos = new_pos
                    intent_weight = self._intent_weights.get(intent_word, 0.7)
                    has_intent = True
                    async with self._stats_lock:
                        self._stats["new_request_after_resolution"] += 1

            if is_distress:
                async with self._stats_lock:
                    self._stats["distress_overrides"] += 1

            # ── Context-aware message type (spec section 3) ─────────────────
            message_type, message_type_confidence = self._classify_message_type(
                cleaned, has_intent, has_academic, ad_score, is_spam_signal
            )

            # ── Ambiguous-term resolution (spec section 17) ─────────────────
            ambiguous_resolutions = self._resolve_ambiguous_terms(cleaned)
            if ambiguous_resolutions:
                async with self._stats_lock:
                    self._stats["ambiguous_term_resolved"] += len(ambiguous_resolutions)
                for term, (sense, weight_delta) in ambiguous_resolutions.items():
                    if sense == "advertisement":
                        ad_score = min(1.0, ad_score + abs(weight_delta) * 0.2)
                    elif sense == "professor" and academic_word is None:
                        # Ambiguous term resolves to academic context even
                        # without a separate academic_objects hit.
                        academic_word = term
                        has_academic = True
                        academic_weight = max(academic_weight, 0.9)

            # ── Smart rule: ad-context dampening (spec section 17) ──────────
            academic_context_strength = min(len(context_matches) / 3.0, 1.0)
            if ad_score > 0.3 and academic_context_strength > self._ad_dampen_academic_threshold:
                ad_score = ad_score * (1 - self._ad_dampen_factor)
                ad_reasons.append("dampened_by_academic_context")
                async with self._stats_lock:
                    self._stats["ad_academic_dampened"] += 1

            # ── Temporal urgency factor (spec section 17) ────────────────────
            urgency_factor = self._urgency_factor(cleaned)

            # ── Conditional confidence (spec section 17) ─────────────────────
            confidence = self._conditional_confidence(
                has_intent=has_intent,
                has_academic=has_academic,
                intent_weight=intent_weight,
                academic_weight=academic_weight,
                urgency_factor=urgency_factor,
                context_component=academic_context_strength,
            )

            # Distance / grammar refinement (kept from v14.1, now feeds the
            # conditional-confidence result rather than a plain average).
            distance_score = 0.0
            grammar_score = 0.0
            if CFG.DISTANCE_SCORING_ENABLED:
                grammar_match = (
                    self._subject_markers_trie.search_first(cleaned)
                    or self._action_verbs_trie.search_first(cleaned)
                )
                grammar_score = 1.0 if grammar_match else 0.0
                if intent_pos is not None and academic_pos is not None:
                    distance_score = self._calculate_distance_score(intent_pos, academic_pos, len(cleaned))
                elif has_intent:
                    distance_score = 0.5
                # Small refinement, not a full second confidence source:
                # a coherent single-clause request nudges confidence up
                # slightly; a very distant match nudges it down slightly.
                confidence = confidence * (0.85 + 0.15 * distance_score)
                confidence += 0.05 * grammar_score

            token_count = len(cleaned.split())
            length_modifier = self._get_length_modifier(token_count)
            confidence *= length_modifier

            if is_negated and not is_distress:
                confidence *= (1 - neg_score * 0.7)
            # Distress is explicitly NOT allowed to reduce confidence (spec
            # section 11 / 17) — it is dropped from the multiplier chain.

            if boost_match:
                confidence += boost

            # ── Smart rule: single-signal ad/spam cannot beat strong intent ─
            reasons: List[str] = []
            decision_override: Optional[str] = None

            if ad_score > self._ad_ignore_threshold:
                if intent_weight < self._ad_review_intent_threshold:
                    decision_override = "ignore"
                    reasons.extend(ad_reasons)
                else:
                    decision_override = "review"
                    reasons.append("ad_signal_with_academic_intent")
                    async with self._stats_lock:
                        self._stats["ad_review_routed"] += 1

            if is_spam_signal and message_type != "academic_help":
                decision_override = "ignore"
                reasons.append("spam_detected" if spam_score_raw > CFG.SPAM_SCORE_THRESHOLD else "spam_pattern")

            if ignore_hit and not has_intent:
                decision_override = decision_override or "ignore"
                reasons.append("ignore_pattern")

            if ad_blocker_hit and message_type != "academic_help":
                decision_override = "ignore"
                reasons.append("ad_blocker")

            if is_negated and not is_distress and not decision_override:
                if neg_score > 0.7:
                    decision_override = "ignore"
                reasons.extend(neg_reasons)

            confidence = max(0.0, min(1.0, confidence))

            result = FilterResult()
            result.raw_text = raw_text[:500]
            result.normalized_text = cleaned[:500]
            result.message_type = message_type
            result.message_type_confidence = round(message_type_confidence, 4)
            result.distress_detected = is_distress
            result.new_request_after_resolution = new_request_info is not None
            result.duplicate_near = is_near_duplicate

            if self._is_blocked(cleaned, result) and not decision_override:
                # Ad/education-provider/ad-emoji trie hits — still subject
                # to the academic-intent override above; only short-circuit
                # here if nothing already routed this to review/ignore/accept.
                if message_type != "academic_help" or ad_score > self._ad_ignore_threshold:
                    return self._convert_result(result, is_arabic, arabic_ratio, ad_score, start)

            keyword = intent_word or (indirect_match[0] if indirect_match else None)

            if not keyword and not decision_override:
                result.valid = False
                result.reason = "no_keyword"
                result.message_type = message_type
                return self._convert_result(result, is_arabic, arabic_ratio, ad_score, start)

            score = CFG.SCORE_DIRECT_MATCH if intent_word else 0
            context_boost = min(len(context_matches) * 5, CFG.SCORE_CONTEXT_MAX)
            score += context_boost
            if urgent:
                score += CFG.SCORE_URGENCY
            if indirect_match and not intent_word:
                score += CFG.SCORE_INDIRECT
                result.indirect = True

            result.keyword = keyword
            result.score = score
            result.context_boost = context_boost
            result.urgent = urgent
            result.reason = (
                "keyword_found" if intent_word
                else ("indirect_request" if indirect_match else "no_keyword")
            )
            result.context_type = (
                "academic_request" if context_matches
                else ("urgent_request" if urgent else "direct_request")
            )
            result.context_confidence = 0.90 if context_matches else (0.85 if urgent else 0.75)

            result.confidence = confidence
            result.intent_verb = intent_word
            result.academic_object = academic_word
            result.urgency_marker = urgency_marker
            result.negation_detected = is_negated
            result.advert_score = ad_score

            if intent_word:
                result.reasons.append(f"intent_verb: {intent_word}")
            if academic_word:
                result.reasons.append(f"academic_object: {academic_word}")
            if urgency_marker:
                result.reasons.append(f"urgency: {urgency_marker}")
            if is_implicit:
                result.reasons.append("implicit_request")
            if is_distress:
                result.reasons.append("distress_expression")
            if new_request_info:
                result.reasons.append("new_request_after_resolution")
            if ad_score > 0.3:
                result.reasons.extend(ad_reasons)
            if boost_match:
                result.reasons.append(f"template_boost: {boost_match[0]}")
            result.reasons.extend(reasons)

            result.score_details = {
                "legacy_score": round(score / 100.0, 4),
                "conditional_confidence": round(confidence, 4),
                "intent_weight": round(intent_weight, 4),
                "academic_weight": round(academic_weight, 4),
                "grammar_score": round(grammar_score, 4),
                "distance_score": round(distance_score, 4),
                "length_modifier": round(length_modifier, 4),
                "urgency_factor": round(urgency_factor, 4),
                "academic_context_strength": round(academic_context_strength, 4),
                "ad_score": round(ad_score, 4),
            }

            # ── Final decision: weighted voting across all signals ─────────
            if decision_override == "ignore":
                result.decision = "ignore"
                result.valid = False
            elif decision_override == "review":
                result.decision = "review"
                result.valid = False
            elif result.confidence >= CFG.CONFIDENCE_ACCEPT_THRESHOLD:
                result.decision = "accept"
                result.valid = True
            elif result.confidence >= CFG.CONFIDENCE_REVIEW_THRESHOLD:
                result.decision = "review"
                result.valid = False
            else:
                result.decision = "ignore"
                result.valid = False

            async with self._stats_lock:
                if result.valid:
                    self._stats["valid"] += 1
                    self._stats["accepted"] += 1
                else:
                    self._stats["rejected"] += 1
                    if result.decision == "review":
                        self._stats["review"] += 1
                    else:
                        self._stats["ignored"] += 1

                self._stats["fast_path"] += 1
                self._stats["total_time_ms"] += result.analysis_time_ms
                self._stats["avg_time_ms"] = self._stats["total_time_ms"] / max(self._stats["processed"], 1)

            result.analysis_time_ms = round((time.perf_counter() - start) * 1000, 2)

            result_dict = result.to_dict()
            result_dict["valid"] = result.valid

            async with self._cache_lock:
                self._text_cache[cache_key] = result_dict

            return result_dict

        except Exception as e:
            logger.error(f"Filter.analyze error: {e}")
            return self._result("ignore", 0.0, [f"internal_error: {str(e)[:50]}"])

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _is_blocked(self, text: str, result: FilterResult) -> bool:
        if self._ad_trie.search_first(text):
            result.reason = "advertisement"
            return True
        if self._education_trie.search_first(text):
            result.reason = "education_provider"
            return True
        if any(em in text for em in self.emoji_advertisement):
            result.reason = "advertisement_emoji"
            return True
        return False

    def _convert_result(self, result: FilterResult, is_arabic: bool, arabic_ratio: float, ad_score: float, start: float) -> Dict[str, Any]:
        result.language = "ar" if is_arabic else "unknown"
        result.lang_conf = arabic_ratio
        result.spam_score = ad_score
        result.analysis_time_ms = round((time.perf_counter() - start) * 1000, 2)
        result.decision = "ignore" if not result.valid else "accept"
        result.confidence = result.score / 100.0
        return result.to_dict()

    def _result(self, decision: str, confidence: float, reasons: List[str]) -> Dict[str, Any]:
        return {
            "valid": decision == "accept",
            "reason": reasons[0] if reasons else decision,
            "keyword": None,
            "score": int(confidence * 100),
            "match_score": confidence,
            "spam_score": 0.0,
            "language": "unknown",
            "lang_conf": 0.0,
            "word_count": 0,
            "context_boost": 0,
            "indirect": False,
            "urgent": False,
            "context_type": "general",
            "context_confidence": confidence,
            "analysis_time_ms": 0.0,
            "decision": decision,
            "confidence": confidence,
            "reasons": reasons,
            "score_details": {},
            "intent_verb": None,
            "academic_object": None,
            "urgency_marker": None,
            "negation_detected": False,
            "advert_score": 0.0,
            "message_type": "general",
            "message_type_confidence": 0.0,
            "distress_detected": False,
            "new_request_after_resolution": False,
            "duplicate_near": False,
            "raw_text": "",
            "normalized_text": "",
        }

    # ─── Telemetry ─────────────────────────────────────────────────────────────

    async def get_telemetry(self) -> Dict[str, Any]:
        async with self._stats_lock:
            stats = dict(self._stats)
            stats["uptime"] = int(time.time() - self._last_stats_reset)
            stats["cache_size"] = len(self._text_cache)
            return stats

    async def clear_cache(self) -> None:
        await self._bloom.clear()
        async with self._cache_lock:
            self._text_cache.clear()
        async with self._recent_lock:
            self._recent_shingles.clear()
        logger.info("Filter v14.2 caches cleared")