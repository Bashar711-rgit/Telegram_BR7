#!/usr/bin/env python3
"""
filter_engine.py — v14.3.1

Hardened/optimized evolution of v14.3 with critical fix:
- Fixed AttributeError: '_fuzzy_only_for' was referenced inside _load_keyword_sets
  before being assigned in __init__. Now fuzzy settings are read BEFORE calling
  _load_keyword_sets, and _load_keyword_sets uses safe getattr fallback.
- All previous fixes retained (syntax, key_phrases, original_text, normalization,
  clause-level negation, fuzzy settings from JSON, prefilter min_words=1).

Compatibility: FilterResult, TrieNode, WeightedTrie, OptimizedBloomFilter, ShardedLRUCache,
Prefilter, EnhancedFilter, ModerationService.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Final, List, Optional, Set, Tuple

from cachetools import LRUCache, TTLCache
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

from security import safe_search, safe_findall, SafeRegexExecutor, MAX_REGEX_INPUT_LEN
from metrics import BoundedMetrics
from adaptive import AdaptiveWeights

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


# --------------------------------------------------------------------------
# Config accessors with safe defaults.
# --------------------------------------------------------------------------
def _cfg(name: str, default: Any) -> Any:
    return getattr(CFG, name, default)


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

    # additive fields (v14.2+)
    key_phrases: List[str] = field(default_factory=list)
    fuzzy_matched: bool = False
    anomaly: bool = False

    # new in v14.3
    original_text: str = ""

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
            "key_phrases": self.key_phrases,
            "fuzzy_matched": self.fuzzy_matched,
            "anomaly": self.anomaly,
            "original_text": self.original_text,
        }


class Prefilter:
    """Prefilter with lowered default min_words to allow short high-signal requests."""

    @staticmethod
    def check(text: str, min_words: int = 1, max_emojis: int = 5) -> Tuple[bool, str, Dict[str, Any]]:
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

        emojis = safe_findall(EMOJI_PATTERN, text)
        emoji_count = len(emojis)
        metadata["emoji_count"] = emoji_count

        if emoji_count > max_emojis:
            return False, f"too_many_emojis_{emoji_count}", metadata

        arabic_chars = sum(1 for c in text if '\u0600' <= c <= '\u06FF')
        arabic_ratio = arabic_chars / max(len(text), 1)
        metadata["arabic_ratio"] = arabic_ratio

        if arabic_ratio < 0.1:
            return False, "low_arabic_ratio", metadata

        metadata["has_url"] = bool(safe_search(URL_PATTERN, text))
        metadata["has_phone"] = bool(safe_search(PHONE_PATTERN, text))

        return True, "ok", metadata


class OptimizedBloomFilter:
    """Same as v14.3, with corrected variable names."""

    __slots__ = ("_size", "_hash_count", "_bit_array", "_lock", "_hash_cache",
                 "_added_count", "_reset_threshold")

    def __init__(self, expected_items: int = 100_000, fp_rate: float = 0.001) -> None:
        self._size = self._optimal_size(expected_items, fp_rate)
        self._hash_count = self._optimal_hash_count(self._size, expected_items)
        self._bit_array = bytearray(self._size // 8 + 1)
        self._lock = asyncio.Lock()
        self._hash_cache: LRUCache = LRUCache(maxsize=10_000)
        self._added_count = 0
        self._reset_threshold = expected_items * 2

    @staticmethod
    def _optimal_size(n: int, p: float) -> int:
        return max(1024, int(-n * math.log(p) / (math.log(2) ** 2)))

    @staticmethod
    def _optimal_hash_count(m: int, n: int) -> int:
        return max(1, int(m / n * math.log(2)))

    def _hashes(self, item: str) -> List[int]:
        cached = self._hash_cache.get(item)
        if cached is not None:
            return cached
        h = hashlib.sha256(item.encode()).hexdigest()
        h1, h2 = int(h[:16], 16), int(h[16:32], 16)
        hashes = [(h1 + i * h2) % self._size for i in range(self._hash_count)]
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


class ShardedLRUCache:
    """Kept for backward compatibility; not used in main analysis path."""

    def __init__(self, max_size: int = 10_000, ttl: int = 300, shards: int = 16) -> None:
        self._shards: List[OrderedDict] = [OrderedDict() for _ in range(shards)]
        self._max_per_shard = max(1, max_size // shards)
        self._ttl = ttl
        self._shard_locks = [asyncio.Lock() for _ in range(shards)]

    def _idx(self, key: str) -> int:
        return hash(key) % len(self._shards)

    async def get(self, key: str) -> Optional[Dict]:
        idx = self._idx(key)
        async with self._shard_locks[idx]:
            entry = self._shards[idx].get(key)
            if entry:
                val, ts = entry
                if time.time() - ts < self._ttl:
                    self._shards[idx].move_to_end(key)
                    return val
                del self._shards[idx][key]
        return None

    async def set(self, key: str, value: Dict) -> None:
        idx = self._idx(key)
        async with self._shard_locks[idx]:
            cache = self._shards[idx]
            if key in cache:
                cache.move_to_end(key)
            else:
                while len(cache) >= self._max_per_shard:
                    cache.popitem(last=False)
            cache[key] = (value, time.time())


class TrieNode:
    __slots__ = ("children", "is_end", "word", "weight")

    def __init__(self) -> None:
        self.children: Dict[str, "TrieNode"] = {}
        self.is_end: bool = False
        self.word: Optional[str] = None
        self.weight: float = 1.0


class WeightedTrie:
    """Unchanged matching algorithm, with variable names corrected."""

    def __init__(self, words: Set[str], weights: Optional[Dict[str, float]] = None) -> None:
        self._root = TrieNode()
        self._max_word_len = 0
        self._words = words
        self._weights = weights or {}
        self._build()

    def _build(self) -> None:
        for word in self._words:
            if not word:
                continue
            node = self._root
            for ch in word:
                if ch not in node.children:
                    node.children[ch] = TrieNode()
                node = node.children[ch]
            node.is_end = True
            node.word = word
            node.weight = self._weights.get(word, 1.0)
            self._max_word_len = max(self._max_word_len, len(word))

    def search_first(self, text: str) -> Optional[Tuple[str, float, int]]:
        limit = min(len(text), 1000)
        max_depth = min(self._max_word_len + 1, 60)
        for start in range(limit):
            node = self._root
            for i in range(start, min(start + max_depth, len(text))):
                ch = text[i]
                if ch not in node.children:
                    break
                node = node.children[ch]
                if node.is_end:
                    return (node.word, node.weight, start)
        return None

    def search_all(self, text: str) -> List[Tuple[str, float, int]]:
        results: List[Tuple[str, float, int]] = []
        limit = min(len(text), 1000)
        max_depth = min(self._max_word_len + 1, 60)
        for start in range(limit):
            node = self._root
            for i in range(start, min(start + max_depth, len(text))):
                ch = text[i]
                if ch not in node.children:
                    break
                node = node.children[ch]
                if node.is_end:
                    results.append((node.word, node.weight, start))
        return results


class EnhancedFilter:
    ARABIC_CHARS: Final[Set[str]] = set("ابتثجحخدذرزسشصضطظعغفقكلمنهويأإؤئآة")
    ARABIC_NORMALIZE: Final[Dict[int, int]] = str.maketrans(
        {"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي", "ئ": "ي", "ؤ": "و"}
    )
    NEGATION_SCOPE_TOKENS: Final[int] = 6

    _PATTERNS: Dict[str, re.Pattern] = {
        "phone": PHONE_PATTERN,
        "url": URL_PATTERN,
        "email": EMAIL_PATTERN,
        "emoji": EMOJI_PATTERN,
    }

    def __init__(self) -> None:
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
            "feedback_events": 0,
            "anomalies_detected": 0,
        }
        self._stats_lock = asyncio.Lock()
        self._last_stats_reset = time.time()
        self._raw_keywords: Dict[str, Any] = KEYWORDS

        # Read fuzzy settings from keywords.json/CFG BEFORE loading keywords.
        # This prevents AttributeError in _load_keyword_sets.
        fuzzy_data = self._raw_keywords.get("fuzzy_matching", {})
        self._fuzzy_enabled = (
            _cfg("FUZZY_FALLBACK_ENABLED", fuzzy_data.get("enabled", True))
            and RAPIDFUZZ_AVAILABLE
        )
        self._fuzzy_score_cutoff = _cfg("FUZZY_SCORE_CUTOFF", 85.0)
        self._fuzzy_max_edit_distance = _cfg("FUZZY_MAX_EDIT_DISTANCE", fuzzy_data.get("max_edit_distance", 1))
        self._fuzzy_min_token_length = _cfg("FUZZY_MIN_TOKEN_LENGTH", fuzzy_data.get("minimum_token_length", 5))
        self._fuzzy_only_for = fuzzy_data.get("only_for", [])

        # Now load keywords and build tries.
        self._load_keyword_sets()
        self._build_tries()

        self._bloom = OptimizedBloomFilter(CFG.BLOOM_FILTER_SIZE, CFG.BLOOM_FILTER_FP)
        self._cache = ShardedLRUCache(CFG.MAX_CACHE_SIZE, CFG.CACHE_TTL)
        self._text_cache = TTLCache(maxsize=CFG.TEXT_CACHE_SIZE, ttl=CFG.TEXT_CACHE_TTL)
        self._cache_lock = asyncio.Lock()

        self._regex_guard = SafeRegexExecutor(
            timeout_s=_cfg("REGEX_TIMEOUT_S", 0.25),
            max_workers=_cfg("REGEX_GUARD_WORKERS", 4),
        )
        self._metrics = BoundedMetrics(
            window=_cfg("METRICS_WINDOW", 2000),
            z_threshold=_cfg("METRICS_Z_THRESHOLD", 3.5),
            enabled=_cfg("METRICS_ENABLED", True),
        )
        self._adaptive_intent = AdaptiveWeights(self._intent_weights, alpha=_cfg("ADAPTIVE_ALPHA", 0.05))
        self._adaptive_academic = AdaptiveWeights(self._academic_weights, alpha=_cfg("ADAPTIVE_ALPHA", 0.05))

        logger.info(
            "Filter v14.3.1 ready | intent_verbs={} | academic_objects={} | negation={} | boost_patterns={} | "
            "distance_scoring={} | fuzzy_fallback={}",
            len(self._intent_verbs_all),
            len(self._academic_objects_all),
            len(self._negation_all),
            len(self._boost_patterns),
            "ON" if CFG.DISTANCE_SCORING_ENABLED else "OFF",
            "ON" if self._fuzzy_enabled else "OFF",
        )

    # ------------------------------------------------------------------
    # Helper: normalize a keyword term (same as input normalization)
    # ------------------------------------------------------------------
    def _normalize_term(self, term: str) -> str:
        term = term.lower()
        term = self._normalize_arabic(term)
        return term

    # ------------------------------------------------------------------
    # Keyword loading (updated to normalize all terms)
    # ------------------------------------------------------------------
    def _generate_template_patterns(self, kw: Dict[str, Any]) -> Set[str]:
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

        # Normalize all word banks
        need = [self._normalize_term(t) for t in need]
        person = [self._normalize_term(t) for t in person]
        action = [self._normalize_term(t) for t in action]
        expert = [self._normalize_term(t) for t in expert]
        availability = [self._normalize_term(t) for t in availability]

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
            "Template engine generated {} boost patterns from {} template(s)",
            len(generated), len(template_patterns_list)
        )
        return generated

    def _load_keyword_sets(self, keywords_data: Optional[Dict[str, Any]] = None) -> None:
        kw = keywords_data if keywords_data is not None else KEYWORDS

        def _norm_set(terms: Any) -> Set[str]:
            if not isinstance(terms, list):
                return set()
            return {self._normalize_term(t) for t in terms if isinstance(t, str)}

        def _norm_list(terms: Any) -> List[str]:
            if not isinstance(terms, list):
                return []
            return [self._normalize_term(t) for t in terms if isinstance(t, str)]

        # Intent verbs
        self._intent_verbs: Dict[str, Dict[str, Any]] = kw.get("intent_verbs", {})
        self._intent_verbs_all: Set[str] = set()
        self._intent_weights: Dict[str, float] = {}
        for tier, data in self._intent_verbs.items():
            if isinstance(data, dict) and "terms" in data:
                weight = data.get("_weight_hint", 0.7)
                for term in data.get("terms", []):
                    term = self._normalize_term(term)
                    self._intent_verbs_all.add(term)
                    self._intent_weights[term] = weight

        # Academic objects
        self._academic_objects: Dict[str, Dict[str, Any]] = kw.get("academic_objects", {})
        self._academic_objects_all: Set[str] = set()
        self._academic_weights: Dict[str, float] = {}
        for obj_type, data in self._academic_objects.items():
            if isinstance(data, dict) and "terms" in data:
                weight = data.get("_weight_hint", 0.7)
                for term in data.get("terms", []):
                    term = self._normalize_term(term)
                    self._academic_objects_all.add(term)
                    self._academic_weights[term] = weight

        # Request phrases
        request_phrases_data = kw.get("request_phrases", {})
        self._request_phrases_all: Set[str] = set()
        for category, phrases in request_phrases_data.items():
            if isinstance(phrases, list):
                self._request_phrases_all.update(_norm_set(phrases))

        # Indirect request
        self._indirect_request: List[str] = _norm_list(kw.get("indirect_request", []))
        self._indirect_request_all: Set[str] = set(self._indirect_request)

        # Urgency
        urgency_data = kw.get("urgency_markers", {})
        self._urgency_all: Set[str] = set()
        for category, markers in urgency_data.items():
            if isinstance(markers, list):
                self._urgency_all.update(_norm_set(markers))

        # Negation
        self._negation: Dict[str, Any] = kw.get("negation", {})
        self._negation_all: Set[str] = set()
        self._negation_exceptions: Set[str] = set()
        self._resolution_phrases: Set[str] = set()

        pre_verb = self._negation.get("pre_verb_negators", {})
        if isinstance(pre_verb, dict) and "terms" in pre_verb:
            self._negation_all.update(_norm_set(pre_verb.get("terms", [])))

        post_clause = self._negation.get("post_clause_negators", [])
        if isinstance(post_clause, list):
            self._negation_all.update(_norm_set(post_clause))

        exceptions = self._negation.get("negation_exceptions", [])
        if isinstance(exceptions, list):
            self._negation_exceptions.update(_norm_set(exceptions))

        resolution = self._negation.get("resolution_phrases", [])
        if isinstance(resolution, list):
            self._resolution_phrases.update(_norm_set(resolution))

        # Boost patterns
        boost_data = kw.get("high_confidence_boost_patterns", {})
        self._boost_patterns: Set[str] = set()
        if isinstance(boost_data, dict):
            patterns = boost_data.get("patterns", [])
            if isinstance(patterns, list):
                self._boost_patterns.update(_norm_set(patterns))

        template_generated = self._generate_template_patterns(kw)
        self._boost_patterns.update(template_generated)
        self._stats["template_patterns_generated"] = len(template_generated)

        # Ad signals
        self._ad_signals: Dict[str, Any] = kw.get("advertisement_signals", {})
        for key in ["hard_signals", "medium_signals", "cta_signals", "institution_terms"]:
            if key in self._ad_signals and isinstance(self._ad_signals[key], list):
                self._ad_signals[key] = _norm_list(self._ad_signals[key])
        provider = self._ad_signals.get("provider_profile", {})
        for pkey in ["strong_provider", "weak_provider", "individual_provider"]:
            if pkey in provider and isinstance(provider[pkey], list):
                provider[pkey] = _norm_list(provider[pkey])
        if "price_signals" in self._ad_signals:
            ps = self._ad_signals["price_signals"]
            if "payment_methods" in ps and "terms" in ps["payment_methods"]:
                if isinstance(ps["payment_methods"]["terms"], list):
                    ps["payment_methods"]["terms"] = _norm_list(ps["payment_methods"]["terms"])

        # Spam categories
        self._spam_categories: Dict[str, List[str]] = kw.get("spam_categories", {})
        self._spam_all: Set[str] = set()
        for category, terms in self._spam_categories.items():
            if isinstance(terms, list):
                self._spam_all.update(_norm_set(terms))

        # Emoji signals
        emoji_data = kw.get("emoji_signals", {})
        self._ad_emoji: Set[str] = set(emoji_data.get("ad_style_emoji", []))
        self._neutral_emoji: Set[str] = set(emoji_data.get("neutral_emoji", []))

        # Ad blockers
        self._ad_blockers: Set[str] = set(kw.get("ad_blockers", []))
        self._ad_blockers = {b.lower() for b in self._ad_blockers if isinstance(b, str)}

        # Ignore signals
        ignore_data = kw.get("ignore_signals", {})
        self._ignore_all: Set[str] = set()
        for category, terms in ignore_data.items():
            if isinstance(terms, list):
                self._ignore_all.update(_norm_set(terms))

        # Help expressions
        self._help_expressions: Set[str] = _norm_set(kw.get("help_expressions", []))

        # Action verbs
        action_verbs_data = kw.get("action_verbs", {})
        self._action_verbs: Set[str] = set()
        for key in ["core", "suffixed_forms", "imperative_forms"]:
            if isinstance(action_verbs_data.get(key), list):
                self._action_verbs.update(_norm_set(action_verbs_data.get(key, [])))

        # Subject markers
        subject_data = kw.get("subject_markers", {})
        self._subject_markers: Set[str] = set()
        for key in ["student_pronouns", "student_question_subject"]:
            if isinstance(subject_data.get(key), list):
                self._subject_markers.update(_norm_set(subject_data.get(key, [])))

        # Implicit request patterns
        implicit_data = kw.get("implicit_request_patterns", {})
        self._implicit_request_all: Set[str] = set()
        for key in ["availability_question", "problem_state"]:
            if isinstance(implicit_data.get(key), list):
                self._implicit_request_all.update(_norm_set(implicit_data.get(key, [])))

        # Solve actions
        solve_data = kw.get("solve_actions", {})
        self._solve_academic: Set[str] = set()
        self._technical_problem_terms: Set[str] = set()
        if isinstance(solve_data.get("academic_solution"), list):
            self._solve_academic.update(_norm_set(solve_data.get("academic_solution", [])))
        if isinstance(solve_data.get("technical_problem_terms"), list):
            self._technical_problem_terms.update(_norm_set(solve_data.get("technical_problem_terms", [])))

        # Dialect mapping
        self._dialect_map: Dict[str, str] = {}
        dialect_data = kw.get("dialect_mapping", {})
        for category, mapping in dialect_data.items():
            if isinstance(mapping, dict):
                for k, v in mapping.items():
                    nk = self._normalize_term(k)
                    nv = self._normalize_term(v)
                    self._dialect_map[nk] = nv
        self._dialect_pattern: Optional[re.Pattern] = None
        self._build_dialect_pattern()

        # University context
        self._university_context: Set[str] = set()
        university_data = kw.get("university_context", {})
        for key, value in university_data.items():
            if isinstance(value, list):
                self._university_context.update(_norm_set(value))

        # Distance config
        self._distance_config: Dict[str, Any] = kw.get("distance_scoring_config", {})

        # Length modifier
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

        # Scoring weights (documentation only)
        self._scoring_weights: Dict[str, float] = {}
        weights_data = kw.get("scoring_weights", {})
        positive_modules = weights_data.get("positive_modules", {})
        if isinstance(positive_modules, dict):
            for key, value in positive_modules.items():
                self._scoring_weights[key] = float(value)

        # Clause boundaries
        self._clause_boundaries: Set[str] = set()
        boundaries = self._negation.get("clause_boundaries", [])
        if isinstance(boundaries, list):
            self._clause_boundaries.update(_norm_set(boundaries))

        # Build final sets
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

        # Legacy flat keys
        old_request = _norm_set(kw.get("request", []))
        old_context = _norm_set(kw.get("request_context", []))
        old_indirect = _norm_set(kw.get("indirect_request", []))
        old_urgency = _norm_set(kw.get("urgency", []))
        old_ignore = _norm_set(kw.get("ignore", []))
        old_ad = _norm_set(kw.get("advertisement", []))
        old_edu = _norm_set(kw.get("education_providers", []))
        old_emoji = set(kw.get("emoji_advertisement", []))
        old_blockers = set(kw.get("ad_blockers", []))
        old_spam = _norm_set(kw.get("spam_patterns", []))

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

        # Ensure all sets are clean
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

        # Build tries
        self._request_trie = WeightedTrie(self.request_words)
        self._context_trie = WeightedTrie(self.context_words)
        self._indirect_trie = WeightedTrie(self.indirect_words)
        self._urgency_trie = WeightedTrie(self.urgency_words)
        self._ignore_trie = WeightedTrie(self.ignore_words)
        self._ad_trie = WeightedTrie(self.advertisement_words)
        self._education_trie = WeightedTrie(self.education_words)

        self._negation_trie = WeightedTrie(self._negation_all)
        self._resolution_trie = WeightedTrie(self._resolution_phrases)
        self._boost_trie = WeightedTrie(self._boost_patterns)
        self._implicit_trie = WeightedTrie(self._implicit_request_all)
        self._spam_trie = WeightedTrie(self._spam_all)
        self._ad_blocker_trie = WeightedTrie(self._ad_blockers)

        self._subject_markers_trie = WeightedTrie(self._subject_markers)
        self._action_verbs_trie = WeightedTrie(self._action_verbs)

        # Fuzzy candidate terms: combine from categories specified in fuzzy_matching.only_for
        fuzzy_terms_set = set()
        # Use safe getattr to avoid AttributeError if called before __init__ sets _fuzzy_only_for
        only_for = getattr(self, "_fuzzy_only_for", []) or ["intent_verbs"]
        if "intent_verbs" in only_for:
            fuzzy_terms_set.update(self._intent_verbs_all)
        if "request_phrases" in only_for:
            fuzzy_terms_set.update(self._request_phrases_all)
        if "high_confidence_boost_patterns" in only_for:
            fuzzy_terms_set.update(self._boost_patterns)
        self._all_fuzzy_terms: List[str] = list(fuzzy_terms_set)

        self._raw_keywords = kw

    def _build_tries(self) -> None:
        # Tries are built inside _load_keyword_sets; kept for API compatibility.
        pass

    def reload_keywords(self, path: str = "keywords.json") -> None:
        from config import load_keywords
        fresh = load_keywords(path)
        # Re-read fuzzy settings if they changed in the new file
        fuzzy_data = fresh.get("fuzzy_matching", {})
        self._fuzzy_enabled = (
            _cfg("FUZZY_FALLBACK_ENABLED", fuzzy_data.get("enabled", True))
            and RAPIDFUZZ_AVAILABLE
        )
        self._fuzzy_score_cutoff = _cfg("FUZZY_SCORE_CUTOFF", 85.0)
        self._fuzzy_max_edit_distance = _cfg("FUZZY_MAX_EDIT_DISTANCE", fuzzy_data.get("max_edit_distance", 1))
        self._fuzzy_min_token_length = _cfg("FUZZY_MIN_TOKEN_LENGTH", fuzzy_data.get("minimum_token_length", 5))
        self._fuzzy_only_for = fuzzy_data.get("only_for", [])

        self._load_keyword_sets(fresh)
        # Re-seed adaptive weights on reload.
        if hasattr(self, "_adaptive_intent"):
            self._adaptive_intent = self._reseed_adaptive(self._adaptive_intent, self._intent_weights)
            self._adaptive_academic = self._reseed_adaptive(self._adaptive_academic, self._academic_weights)
        self._stats["keyword_reloads"] = self._stats.get("keyword_reloads", 0) + 1
        logger.info(
            "Filter keyword sets reloaded from {} | templates={} entries | boost_patterns_total={}",
            path,
            len(fresh.get("templates", {})),
            len(self._boost_patterns),
        )

    @staticmethod
    def _reseed_adaptive(existing: AdaptiveWeights, static_defaults: Dict[str, float]) -> AdaptiveWeights:
        merged = dict(static_defaults)
        # Preserve learned weights for terms that still exist
        if hasattr(existing, "weights"):
            for term, learned in existing.weights.items():
                if term in merged:
                    merged[term] = learned
        alpha = getattr(existing, "alpha", 0.05)
        return AdaptiveWeights(merged, alpha=alpha)

    _build_keyword_sets = reload_keywords

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------
    def _build_dialect_pattern(self) -> None:
        if not self._dialect_map:
            self._dialect_pattern = None
            return
        variants = sorted(self._dialect_map.keys(), key=len, reverse=True)
        escaped = [re.escape(v) for v in variants if v]
        if not escaped:
            self._dialect_pattern = None
            return
        boundary = r"(?:(?<=^)|(?<=[\s.,!?؟،]))(%s)(?=$|[\s.,!?؟،])"
        try:
            self._dialect_pattern = re.compile(boundary % "|".join(escaped))
        except re.error as exc:
            logger.error("Failed to compile dialect pattern: {}", exc)
            self._dialect_pattern = None

    def _normalize_arabic(self, text: str) -> str:
        text = text.translate(self.ARABIC_NORMALIZE)
        if PYARABIC_AVAILABLE:
            try:
                text = araby.strip_tashkeel(text)
                text = araby.strip_tatweel(text)
            except Exception as exc:
                logger.debug("pyarabic normalization failed: {}", exc)
        return text

    def _apply_dialect_mapping(self, text: str) -> str:
        if not self._dialect_pattern:
            return text

        def _sub(match: "re.Match") -> str:
            return self._dialect_map.get(match.group(1), match.group(1))

        capped = text if len(text) <= MAX_REGEX_INPUT_LEN else text[:MAX_REGEX_INPUT_LEN]
        try:
            return self._dialect_pattern.sub(_sub, capped)
        except re.error:
            return capped

    def _clean(self, text: str) -> Tuple[str, str]:
        """Returns (cleaned_for_matching, original_preserved)."""
        original = text
        cleaned = WS_PATTERN.sub(" ", text).strip()
        cleaned = cleaned.lower()
        cleaned = self._normalize_arabic(cleaned)
        cleaned = self._apply_dialect_mapping(cleaned)
        return cleaned, original

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
                except Exception as exc:
                    logger.debug("langdetect failed: {}", exc)
            return False, ratio
        if LANGDETECT_AVAILABLE:
            try:
                lang = detect(text)
                return lang == "ar", 0.85 if lang == "ar" else 0.6
            except Exception as exc:
                logger.debug("langdetect failed: {}", exc)
        return ratio > 0.25, ratio

    def _spam_score(self, text: str) -> float:
        score = 0.0
        if safe_search(PHONE_PATTERN, text):
            score += 0.3
        url_count = len(safe_findall(URL_PATTERN, text))
        score += min(0.4, url_count * 0.2)
        emoji_count = len(safe_findall(EMOJI_PATTERN, text))
        score += min(0.2, emoji_count * 0.04)
        repeated_pattern = re.compile(r"(.)\1{4,}")
        if safe_search(repeated_pattern, text):
            score += 0.15
        return min(score, 1.0)

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
        elif weak_count >= _cfg("AD_WEAK_PROVIDER_THRESHOLD", 3):
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
        if ad_emoji_count >= _cfg("AD_EMOJI_THRESHOLD", 3):
            ad_score += 0.2
            reasons.append(f"ad_emoji_count: {ad_emoji_count}")

        return min(ad_score, 1.0), reasons

    def _detect_negation(self, text: str, intent_pos: Optional[int]) -> Tuple[bool, float, List[str]]:
        """
        Improved negation detection with clause-awareness.
        Resolution phrases are only full negation if no new request appears after them.
        """
        resolution_match = self._resolution_trie.search_first(text)
        if resolution_match:
            res_phrase, _, res_pos = resolution_match
            if intent_pos is not None and intent_pos > res_pos:
                # New request after resolution: not a pure resolution
                logger.debug("Resolution phrase '{}' found but intent appears later; not fully negated", res_phrase)
                return True, 0.2, [f"resolution_phrase_with_new_request: {res_phrase}"]
            return True, 1.0, [f"resolution_phrase: {res_phrase}"]

        post_clause = self._negation.get("post_clause_negators", [])
        if isinstance(post_clause, list):
            for neg in post_clause:
                if neg in text:
                    if any(ex in text for ex in self._negation_exceptions):
                        return False, 0.0, []
                    return True, 0.8, [f"post_clause_negator: {neg}"]

        pre_verb_data = self._negation.get("pre_verb_negators", {})
        if isinstance(pre_verb_data, dict):
            pre_verbs = pre_verb_data.get("terms", [])
            if isinstance(pre_verbs, list):
                for pv in pre_verbs:
                    pos = text.find(pv)
                    if pos == -1:
                        continue
                    if any(ex in text for ex in self._negation_exceptions):
                        return False, 0.0, []
                    if _cfg("NEGATION_CLAUSE_BOUNDARIES_ENABLED", True):
                        before_text = text[:pos]
                        if any(boundary in before_text for boundary in self._clause_boundaries):
                            return False, 0.0, []

                    if intent_pos is not None:
                        token_distance = self._token_distance(text, pos, intent_pos)
                        if token_distance > self.NEGATION_SCOPE_TOKENS:
                            continue

                    return True, 0.6, [f"pre_verb_negator: {pv}"]

        return False, 0.0, []

    @staticmethod
    def _token_distance(text: str, pos_a: int, pos_b: int) -> int:
        lo, hi = sorted((pos_a, pos_b))
        return text.count(" ", lo, hi)

    def _calculate_distance_score(self, intent_pos: int, academic_pos: int, text_len: int) -> float:
        distance = abs(intent_pos - academic_pos)
        thresholds = self._distance_config.get("thresholds", {})

        for range_str, data in thresholds.items():
            if "_to_" in range_str:
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

    def _get_length_modifier(self, token_count: int) -> float:
        for (start, end), value in self._length_modifier.items():
            if start <= token_count <= end:
                return value
        return 0.9

    def _fuzzy_intent_fallback(self, cleaned: str) -> Optional[Tuple[str, float, int]]:
        if not self._fuzzy_enabled or not cleaned.strip():
            return None
        tokens = cleaned.split()
        if not tokens or not getattr(self, '_all_fuzzy_terms', []):
            return None

        if len(cleaned) < self._fuzzy_min_token_length:
            return None

        best = rf_process.extractOne(
            cleaned,
            self._all_fuzzy_terms,
            scorer=fuzz.partial_ratio,
            score_cutoff=self._fuzzy_score_cutoff,
        )
        if not best:
            return None
        term, score, _ = best
        weight = self._adaptive_intent.get(term, self._intent_weights.get(term, 0.7)) * 0.85
        pos = cleaned.find(tokens[0])
        return term, weight, max(pos, 0)

    async def analyze(self, text: str) -> Dict[str, Any]:
        start = time.perf_counter()
        original_text = text

        try:
            if len(text) > CFG.MAX_MESSAGE_LENGTH:
                return self._result("ignore", 0.0, ["too_long"], original_text=original_text)

            validated = InputSanitizer.validate_message_text(text)
            if validated is None:
                return self._result("ignore", 0.0, ["invalid_input"], original_text=original_text)

            cleaned, original = self._clean(validated)
            cache_key = hashlib.blake2b(cleaned.encode(), digest_size=16).hexdigest()[:32]

            if CFG.PREFILTER_ENABLED:
                ok, reason, metadata = Prefilter.check(
                    cleaned, _cfg("PREFILTER_MIN_WORDS", 1), CFG.PREFILTER_MAX_EMOJIS
                )
                if not ok:
                    async with self._stats_lock:
                        self._stats["prefilter_rejected"] += 1
                    return self._result("ignore", 0.0, [reason], original_text=original_text)

            if await self._bloom.contains(cache_key):
                async with self._stats_lock:
                    self._stats["bloom_hits"] += 1
                return self._result("ignore", 0.0, ["duplicate"], original_text=original_text)

            async with self._cache_lock:
                if cache_key in self._text_cache:
                    async with self._stats_lock:
                        self._stats["cache_hits"] += 1
                    result = dict(self._text_cache[cache_key])
                    result["analysis_time_ms"] = round((time.perf_counter() - start) * 1000, 2)
                    return result

            await self._bloom.add(cache_key)

            async with self._stats_lock:
                self._stats["processed"] += 1

            is_arabic, arabic_ratio = self._is_arabic(cleaned)
            if CFG.LANGUAGE_FILTER and not is_arabic:
                return self._result("ignore", 0.0, ["non_arabic"], original_text=original_text)

            spam_score = self._spam_score(cleaned)
            if spam_score > CFG.SPAM_SCORE_THRESHOLD:
                async with self._stats_lock:
                    self._stats["spam"] += 1
                return self._result("ignore", 0.0, ["spam_detected"], original_text=original_text)

            if self._spam_trie.search_first(cleaned):
                async with self._stats_lock:
                    self._stats["spam"] += 1
                return self._result("ignore", 0.0, ["spam_pattern"], original_text=original_text)

            if self._ignore_trie.search_first(cleaned):
                return self._result("ignore", 0.0, ["ignore_pattern"], original_text=original_text)

            if self._ad_blocker_trie.search_first(cleaned):
                return self._result("ignore", 0.0, ["ad_blocker"], original_text=original_text)

            intent_match = self._request_trie.search_first(cleaned)
            fuzzy_used = False
            if intent_match is None:
                fuzzy = self._fuzzy_intent_fallback(cleaned)
                if fuzzy is not None:
                    intent_match = fuzzy
                    fuzzy_used = True
                    async with self._stats_lock:
                        self._stats["fuzzy_path"] += 1

            indirect_match = self._indirect_trie.search_first(cleaned)
            urgency_match = self._urgency_trie.search_first(cleaned)
            implicit_match = self._implicit_trie.search_first(cleaned)
            boost_match = self._boost_trie.search_first(cleaned)
            context_matches = self._context_trie.search_all(cleaned)
            academic_match = context_matches[0] if context_matches else None

            intent_word = intent_match[0] if intent_match else None
            intent_pos = intent_match[2] if intent_match else None
            intent_weight = (
                self._adaptive_intent.get(intent_word, 0.7) if intent_word and not fuzzy_used
                else (intent_match[1] if fuzzy_used and intent_match else 0.0)
            )

            academic_word = academic_match[0] if academic_match else None
            academic_pos = academic_match[2] if academic_match else None
            academic_weight = self._adaptive_academic.get(academic_word, 0.7) if academic_word else 0.0

            urgency_marker = urgency_match[0] if urgency_match else None
            urgent = urgency_match is not None
            is_implicit = implicit_match is not None
            boost = 0.25 if boost_match else 0.0

            is_negated, neg_score, neg_reasons = self._detect_negation(cleaned, intent_pos)
            if is_negated and neg_score > 0.7:
                return self._result(
                    "ignore",
                    1.0 - neg_score,
                    neg_reasons,
                    original_text=original_text
                )

            ad_score, ad_reasons = self._detect_advertisement(cleaned)
            if ad_score > 0.6:
                return self._result("ignore", 1.0 - ad_score, ad_reasons, original_text=original_text)

            result = FilterResult()
            result.original_text = original

            if self._is_blocked(cleaned, result):
                return self._convert_result(result, is_arabic, arabic_ratio, ad_score, start)

            keyword = intent_word or (indirect_match[0] if indirect_match else None)
            if not keyword:
                result.valid = False
                result.reason = "no_keyword"
                return self._convert_result(result, is_arabic, arabic_ratio, ad_score, start)

            score = CFG.SCORE_DIRECT_MATCH if intent_word else 0
            context_boost = min(len(context_matches) * 5, CFG.SCORE_CONTEXT_MAX)
            score += context_boost

            if urgent:
                score += CFG.SCORE_URGENCY

            if indirect_match and not intent_word:
                score += CFG.SCORE_INDIRECT
                result.indirect = True

            result.valid = score >= CFG.SCORE_MIN_VALID
            result.keyword = keyword
            result.score = score
            result.context_boost = context_boost
            result.urgent = urgent
            result.fuzzy_matched = fuzzy_used
            result.reason = (
                "keyword_found" if intent_word
                else ("indirect_request" if indirect_match else "no_keyword")
            )
            result.context_type = (
                "academic_request" if context_matches
                else ("urgent_request" if urgent else "direct_request")
            )
            result.context_confidence = 0.90 if context_matches else (0.85 if urgent else 0.75)

            legacy_confidence = score / 100.0

            weighted_confidence: Optional[float] = None
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
                elif intent_word:
                    distance_score = 0.5

                context_component = min(len(context_matches) / 3.0, 1.0)
                urgency_component = 1.0 if urgent else 0.0

                weight_sum = (
                    CFG.SCORE_WEIGHT_INTENT + CFG.SCORE_WEIGHT_ACADEMIC + CFG.SCORE_WEIGHT_GRAMMAR
                    + CFG.SCORE_WEIGHT_DISTANCE + CFG.SCORE_WEIGHT_URGENCY + CFG.SCORE_WEIGHT_CONTEXT
                ) or 1.0

                weighted_confidence = (
                    intent_weight * CFG.SCORE_WEIGHT_INTENT
                    + academic_weight * CFG.SCORE_WEIGHT_ACADEMIC
                    + grammar_score * CFG.SCORE_WEIGHT_GRAMMAR
                    + distance_score * CFG.SCORE_WEIGHT_DISTANCE
                    + urgency_component * CFG.SCORE_WEIGHT_URGENCY
                    + context_component * CFG.SCORE_WEIGHT_CONTEXT
                ) / weight_sum
                weighted_confidence = max(0.0, min(1.0, weighted_confidence))

            result.confidence = (
                (legacy_confidence + weighted_confidence) / 2.0
                if weighted_confidence is not None else legacy_confidence
            )

            result.intent_verb = intent_word
            result.academic_object = academic_word
            result.urgency_marker = urgency_marker
            result.negation_detected = is_negated
            result.advert_score = ad_score
            result.reasons = []
            key_phrases: List[str] = []
            if intent_word:
                result.reasons.append(f"intent_verb: {intent_word}")
                key_phrases.append(intent_word)
            if academic_word:
                result.reasons.append(f"academic_object: {academic_word}")
                key_phrases.append(academic_word)
            if urgency_marker:
                result.reasons.append(f"urgency: {urgency_marker}")
            if is_implicit:
                result.reasons.append("implicit_request")
            if is_negated:
                result.reasons.extend(neg_reasons)
            if ad_score > 0.3:
                result.reasons.extend(ad_reasons)
            if boost_match:
                result.reasons.append(f"template_boost: {boost_match[0]}")
                key_phrases.append(boost_match[0])
            result.key_phrases = key_phrases

            token_count = len(cleaned.split())
            length_modifier = self._get_length_modifier(token_count)
            result.confidence *= length_modifier

            if is_negated:
                result.confidence *= (1 - neg_score * 0.7)
            result.confidence *= (1 - ad_score * 0.9)

            if boost_match:
                result.confidence += boost

            result.confidence = max(0.0, min(1.0, result.confidence))

            result.score_details = {
                "legacy_score": round(legacy_confidence, 4),
                "weighted_score": round(weighted_confidence, 4) if weighted_confidence is not None else 0.0,
                "intent_weight": round(intent_weight, 4),
                "academic_weight": round(academic_weight, 4),
                "grammar_score": round(grammar_score, 4),
                "distance_score": round(distance_score, 4),
                "length_modifier": round(length_modifier, 4),
            }

            if result.confidence >= CFG.CONFIDENCE_ACCEPT_THRESHOLD:
                result.decision = "accept"
            elif result.confidence >= CFG.CONFIDENCE_REVIEW_THRESHOLD:
                result.decision = "review"
            else:
                result.decision = "ignore"
                result.valid = False

            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            anomaly_report = await self._metrics.record(elapsed_ms)
            if anomaly_report and anomaly_report.is_anomaly:
                result.anomaly = True
                logger.warning(
                    "analyze_latency_anomaly | value_ms={} mean_ms={} z={} n={}",
                    anomaly_report.value, round(anomaly_report.mean, 2),
                    round(anomaly_report.z_score, 2), anomaly_report.sample_size,
                )

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
                self._stats["total_time_ms"] += elapsed_ms
                self._stats["avg_time_ms"] = self._stats["total_time_ms"] / max(self._stats["processed"], 1)
                self._stats["max_time_ms"] = max(self._stats["max_time_ms"], elapsed_ms)
                self._stats["min_time_ms"] = min(self._stats["min_time_ms"], elapsed_ms)
                if result.anomaly:
                    self._stats["anomalies_detected"] += 1

            result.analysis_time_ms = elapsed_ms

            result_dict = result.to_dict()
            result_dict["valid"] = result.valid

            async with self._cache_lock:
                self._text_cache[cache_key] = result_dict

            return result_dict

        except Exception as e:
            logger.exception("Filter.analyze error")
            return self._result("ignore", 0.0, [f"internal_error: {str(e)[:50]}"], original_text=original_text)

    async def record_feedback(self, term: str, term_kind: str, was_correct: bool) -> float:
        if term_kind == "intent":
            weight = await self._adaptive_intent.record_feedback(term, was_correct)
        elif term_kind == "academic":
            weight = await self._adaptive_academic.record_feedback(term, was_correct)
        else:
            raise ValueError(f"Unknown term_kind: {term_kind!r} (expected 'intent' or 'academic')")
        async with self._stats_lock:
            self._stats["feedback_events"] += 1
        return weight

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

    def _convert_result(self, result: FilterResult, is_arabic: bool, arabic_ratio: float,
                        ad_score: float, start: float) -> Dict[str, Any]:
        result.language = "ar" if is_arabic else "unknown"
        result.lang_conf = arabic_ratio
        result.spam_score = ad_score
        result.analysis_time_ms = round((time.perf_counter() - start) * 1000, 2)
        result.decision = "ignore" if not result.valid else "accept"
        result.confidence = result.score / 100.0
        return result.to_dict()

    def _result(self, decision: str, confidence: float, reasons: List[str],
                original_text: str = "") -> Dict[str, Any]:
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
            "key_phrases": [],
            "fuzzy_matched": False,
            "anomaly": False,
            "original_text": original_text,
        }

    async def get_telemetry(self) -> Dict[str, Any]:
        async with self._stats_lock:
            stats = dict(self._stats)
            stats["uptime"] = int(time.time() - self._last_stats_reset)
            stats["cache_size"] = len(self._text_cache)
        stats["latency_percentiles"] = await self._metrics.snapshot()
        stats["adaptive_feedback_count"] = self._adaptive_intent.feedback_count + self._adaptive_academic.feedback_count
        return stats

    async def clear_cache(self) -> None:
        await self._bloom.clear()
        async with self._cache_lock:
            self._text_cache.clear()
        logger.info("Filter v14.3.1 caches cleared")

    def shutdown(self) -> None:
        self._regex_guard.shutdown()


class ModerationService:
    """Unified façade over EnhancedFilter (same as v14.3, no changes needed)."""

    def __init__(self, filter_: Optional[EnhancedFilter] = None,
                 timeout_s: float = 2.0, max_concurrency: int = 32) -> None:
        self.filter = filter_ or EnhancedFilter()
        self._timeout_s = timeout_s
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def analyze(self, text: str, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        budget = timeout_s if timeout_s is not None else self._timeout_s
        try:
            return await asyncio.wait_for(self.filter.analyze(text), timeout=budget)
        except asyncio.TimeoutError:
            logger.warning("analyze_timeout | budget_s={} text_len={}", budget, len(text))
            return self.filter._result("ignore", 0.0, ["analysis_timeout"])  # noqa: SLF001
        except Exception:
            logger.exception("analyze_unexpected_error")
            return self.filter._result("ignore", 0.0, ["internal_error"])  # noqa: SLF001

    async def analyze_batch(self, texts: List[str], timeout_s: Optional[float] = None) -> List[Dict[str, Any]]:
        async def _guarded(t: str) -> Dict[str, Any]:
            async with self._semaphore:
                return await self.analyze(t, timeout_s=timeout_s)
        return await asyncio.gather(*(_guarded(t) for t in texts))

    async def get_telemetry(self) -> Dict[str, Any]:
        return await self.filter.get_telemetry()

    async def clear_cache(self) -> None:
        await self.filter.clear_cache()

    def reload_keywords(self, path: str = "keywords.json") -> None:
        self.filter.reload_keywords(path)

    async def record_feedback(self, term: str, term_kind: str, was_correct: bool) -> float:
        return await self.filter.record_feedback(term, term_kind, was_correct)

    def shutdown(self) -> None:
        self.filter.shutdown()