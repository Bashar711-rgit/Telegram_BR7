#!/usr/bin/env python3
"""
filter_engine.py — v15.7.0

Deterministic rule-based Arabic message filtering engine.
Enhanced version of v14.2 with full backward compatibility.

Key improvements over v14.2:
  - Hard Signal Combination Gate (2+ independent signals required)
  - Compound requests are rules with exact matching (no substring)
  - Negation/question/quotation detection BEFORE signal matching
  - Configuration-driven: JSON controls behavior via signal_combinations
  - Forbidden rules with unless_any conditions
  - Deterministic priority resolution (negation > quotation > forbidden > compound > allowed)
  - Expert+academic without intent/action → rejected
  - Expert+academic WITH intent/action → accepted
  - Boost patterns cannot bypass the Hard Gate
  - Full backward compatibility with existing bot APIs
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

# ============================================================
# Safe imports with comprehensive fallbacks
# ============================================================
try:
    from config import (
        CFG, KEYWORDS, InputSanitizer,
        PHONE_PATTERN, URL_PATTERN, EMAIL_PATTERN,
        EMOJI_PATTERN, WS_PATTERN,
    )
except ImportError:
    CFG = type('CFG', (), {
        'MAX_MESSAGE_LENGTH': 2000,
        'BLOOM_FILTER_SIZE': 100000,
        'BLOOM_FILTER_FP': 0.001,
        'MAX_CACHE_SIZE': 10000,
        'CACHE_TTL': 300,
        'TEXT_CACHE_SIZE': 1000,
        'TEXT_CACHE_TTL': 60,
        'PREFILTER_ENABLED': True,
        'PREFILTER_MAX_EMOJIS': 5,
        'LANGUAGE_FILTER': True,
        'SPAM_SCORE_THRESHOLD': 0.7,
        'CONFIDENCE_ACCEPT_THRESHOLD': 0.65,
        'CONFIDENCE_REVIEW_THRESHOLD': 0.40,
        'SCORE_DIRECT_MATCH': 50,
        'SCORE_CONTEXT_MAX': 15,
        'SCORE_URGENCY': 10,
        'SCORE_INDIRECT': 30,
        'SCORE_MIN_VALID': 30,
        'DISTANCE_SCORING_ENABLED': True,
        'SCORE_WEIGHT_INTENT': 0.30,
        'SCORE_WEIGHT_ACADEMIC': 0.25,
        'SCORE_WEIGHT_GRAMMAR': 0.15,
        'SCORE_WEIGHT_DISTANCE': 0.15,
        'SCORE_WEIGHT_URGENCY': 0.05,
        'SCORE_WEIGHT_CONTEXT': 0.10,
        'NEGATION_CLAUSE_BOUNDARIES_ENABLED': True,
        'AD_WEAK_PROVIDER_THRESHOLD': 3,
        'AD_EMOJI_THRESHOLD': 3,
        'FUZZY_FALLBACK_ENABLED': True,
        'FUZZY_SCORE_CUTOFF': 85.0,
        'REGEX_TIMEOUT_S': 0.25,
        'REGEX_GUARD_WORKERS': 4,
        'METRICS_WINDOW': 2000,
        'METRICS_Z_THRESHOLD': 3.5,
        'METRICS_ENABLED': True,
        'ADAPTIVE_ALPHA': 0.05,
    })()
    KEYWORDS = {}
    InputSanitizer = type('InputSanitizer', (), {
        'validate_message_text': staticmethod(lambda t: t if t else None)
    })
    PHONE_PATTERN = re.compile(r'')
    URL_PATTERN = re.compile(r'')
    EMAIL_PATTERN = re.compile(r'')
    EMOJI_PATTERN = re.compile(r'')
    WS_PATTERN = re.compile(r'\s+')

try:
    from security import safe_search, safe_findall, SafeRegexExecutor, MAX_REGEX_INPUT_LEN
except ImportError:
    def safe_search(pattern, text, *args, **kwargs):
        return re.search(pattern, text, *args, **kwargs)
    def safe_findall(pattern, text, *args, **kwargs):
        return re.findall(pattern, text, *args, **kwargs)
    class SafeRegexExecutor:
        def __init__(self, **kwargs): pass
        def shutdown(self): pass
    MAX_REGEX_INPUT_LEN = 10000

try:
    from metrics import BoundedMetrics
except ImportError:
    class BoundedMetrics:
        def __init__(self, **kwargs): pass
        async def record(self, *args, **kwargs): return None
        async def snapshot(self): return {}

try:
    from adaptive import AdaptiveWeights
except ImportError:
    class AdaptiveWeights:
        def __init__(self, weights=None, alpha=0.05):
            self._weights = dict(weights or {})
            self._alpha = alpha
            self.feedback_count = 0
        def get(self, key, default=1.0):
            return self._weights.get(key, default)
        async def record_feedback(self, term, was_correct):
            self.feedback_count += 1
            if term in self._weights:
                delta = self._alpha if was_correct else -self._alpha
                self._weights[term] = max(0.1, min(2.0, self._weights[term] + delta))
            return self._weights.get(term, 1.0)

try:
    from rapidfuzz import fuzz, process as rf_process
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False

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


def _cfg(name: str, default: Any) -> Any:
    return getattr(CFG, name, default)


# ============================================================
# Data Classes
# ============================================================

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
    key_phrases: List[str] = field(default_factory=list)
    fuzzy_matched: bool = False
    anomaly: bool = False
    signal_combination: Dict[str, Any] = field(default_factory=dict)

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
            "signal_combination": self.signal_combination,
        }


# ============================================================
# Prefilter
# ============================================================

class Prefilter:
    """Technical prefilter only. No semantic checks."""

    @staticmethod
    def check(text: str, max_emojis: int = 5) -> Tuple[bool, str, Dict[str, Any]]:
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
        metadata["word_count"] = len(words)

        emojis = safe_findall(EMOJI_PATTERN, text) if EMOJI_PATTERN.pattern else []
        metadata["emoji_count"] = len(emojis)

        if metadata["emoji_count"] > max_emojis:
            return False, "too_many_emojis", metadata

        arabic_chars = sum(1 for c in text if '\u0600' <= c <= '\u06FF')
        metadata["arabic_ratio"] = arabic_chars / max(len(text), 1)

        if metadata["arabic_ratio"] < 0.1:
            return False, "low_arabic_ratio", metadata

        metadata["has_url"] = bool(safe_search(URL_PATTERN, text)) if URL_PATTERN.pattern else False
        metadata["has_phone"] = bool(safe_search(PHONE_PATTERN, text)) if PHONE_PATTERN.pattern else False

        return True, "ok", metadata


# ============================================================
# Bloom Filter
# ============================================================

class OptimizedBloomFilter:
    __slots__ = ("_size", "_hash_count", "_bit_array", "_lock", "_hash_cache", "_added_count", "_reset_threshold")

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
            return all(self._bit_array[pos // 8] & (1 << (pos % 8)) for pos in self._hashes(item))

    async def clear(self) -> None:
        async with self._lock:
            self._bit_array = bytearray(self._size // 8 + 1)
            self._hash_cache.clear()
            self._added_count = 0


# ============================================================
# Sharded LRU Cache (kept for backward compatibility)
# ============================================================

class ShardedLRUCache:
    """Kept for backward compatibility. Not used in analyze() hot path."""

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


# ============================================================
# Trie
# ============================================================

class TrieNode:
    __slots__ = ("children", "is_end", "word", "weight")

    def __init__(self) -> None:
        self.children: Dict[str, "TrieNode"] = {}
        self.is_end: bool = False
        self.word: Optional[str] = None
        self.weight: float = 1.0


class WeightedTrie:
    """Trie with substring and exact matching support."""

    def __init__(self, words: Set[str], weights: Optional[Dict[str, float]] = None) -> None:
        self._root = TrieNode()
        self._max_word_len = 0
        self._words = set(words)
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
        """Find first match (substring)."""
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
        """Find all matches."""
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

    def search_exact(self, text: str) -> Optional[Tuple[str, float, int]]:
        """Find exact match only (word-boundary aware, handles multi-space)."""
        normalized_text = re.sub(r'\s+', ' ', text.strip())
        limit = min(len(normalized_text), 1000)
        max_depth = min(self._max_word_len + 1, 60)
        for start in range(limit):
            if start > 0 and normalized_text[start-1].isalnum():
                continue
            node = self._root
            for i in range(start, min(start + max_depth, len(normalized_text))):
                ch = normalized_text[i]
                if ch not in node.children:
                    break
                node = node.children[ch]
                if node.is_end:
                    end = i + 1
                    if end < len(normalized_text) and normalized_text[end].isalnum():
                        break
                    return (node.word, node.weight, start)
        return None


# ============================================================
# Main Filter Engine
# ============================================================

class EnhancedFilter:
    ARABIC_CHARS: Final[Set[str]] = set("ابتثجحخدذرزسشصضطظعغفقكلمنهويأإؤئآة")
    ARABIC_NORMALIZE: Final[Dict[int, int]] = str.maketrans(
        {"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي", "ئ": "ي", "ؤ": "و"}
    )
    NEGATION_SCOPE_TOKENS: Final[int] = 6

    def __init__(self) -> None:
        self._stats: Dict[str, int] = {
            "processed": 0, "valid": 0, "rejected": 0, "spam": 0,
            "cache_hits": 0, "bloom_hits": 0, "fuzzy_path": 0,
            "prefilter_rejected": 0, "accepted": 0, "review": 0, "ignored": 0,
            "total_time_ms": 0, "avg_time_ms": 0, "max_time_ms": 0, "min_time_ms": 999999,
            "insufficient_combination": 0, "negation_blocks": 0,
            "question_modifications": 0, "quotation_suppressions": 0,
            "template_patterns_generated": 0, "keyword_reloads": 0,
            "feedback_events": 0, "anomalies_detected": 0,
        }
        self._stats_lock = asyncio.Lock()
        self._last_stats_reset = time.time()
        self._raw_keywords: Dict[str, Any] = KEYWORDS

        self._load_keyword_sets()

        self._bloom = OptimizedBloomFilter(
            _cfg("BLOOM_FILTER_SIZE", 100000),
            _cfg("BLOOM_FILTER_FP", 0.001),
        )
        self._text_cache = TTLCache(
            maxsize=_cfg("TEXT_CACHE_SIZE", 1000),
            ttl=_cfg("TEXT_CACHE_TTL", 60),
        )
        self._cache_lock = asyncio.Lock()

        # Subsystems with safe fallbacks
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
        self._fuzzy_enabled = _cfg("FUZZY_FALLBACK_ENABLED", True) and RAPIDFUZZ_AVAILABLE
        self._fuzzy_score_cutoff = _cfg("FUZZY_SCORE_CUTOFF", 85.0)

        logger.info(
            "EnhancedFilter v15.7.0 ready | intent={} | academic={} | expert={} | "
            "compound={} | allowed_rules={} | forbidden_rules={}",
            len(self._intent_verbs_all),
            len(self._academic_objects_all),
            len(self._expert_terms),
            len(self._compound_requests),
            len(self._allowed_rules),
            len(self._forbidden_rules),
        )

    # ============================================================
    # Keyword Loading
    # ============================================================

    def _load_keyword_sets(self, keywords_data: Optional[Dict[str, Any]] = None) -> None:
        kw = keywords_data if keywords_data is not None else KEYWORDS

        # ============ Intent Verbs ============
        self._intent_verbs_all: Set[str] = set()
        self._intent_weights: Dict[str, float] = {}
        intent_data = kw.get("intent_verbs", {})
        for tier, data in intent_data.items():
            if isinstance(data, dict) and "terms" in data:
                weight = data.get("_weight_hint", 0.7)
                for term in data.get("terms", []):
                    self._intent_verbs_all.add(term)
                    self._intent_weights[term] = weight

        # ============ Action Verbs ============
        self._action_verbs: Set[str] = set()
        action_data = kw.get("action_verbs", {})
        for key in ["core", "suffixed_forms", "imperative_forms"]:
            if isinstance(action_data.get(key), list):
                self._action_verbs.update(action_data.get(key, []))

        # ============ Academic Objects ============
        self._academic_objects_all: Set[str] = set()
        self._academic_weights: Dict[str, float] = {}
        academic_data = kw.get("academic_objects", {})
        for obj_type, data in academic_data.items():
            if isinstance(data, dict) and "terms" in data:
                weight = data.get("_weight_hint", 0.7)
                for term in data.get("terms", []):
                    self._academic_objects_all.add(term)
                    self._academic_weights[term] = weight

        # ============ Request Phrases ============
        self._request_phrases_all: Set[str] = set()
        request_data = kw.get("request_phrases", {})
        for category, phrases in request_data.items():
            if isinstance(phrases, list):
                self._request_phrases_all.update(phrases)

        # ============ Help Expressions ============
        self._help_expressions: Set[str] = set(kw.get("help_expressions", []))

        # ============ Expert Terms ============
        expert_terms: Set[str] = set()
        templates_data = kw.get("templates", {})
        if isinstance(templates_data, dict):
            expert_list = templates_data.get("expert", [])
            if isinstance(expert_list, list):
                expert_terms.update(expert_list)
        self._expert_terms: Set[str] = expert_terms

        # ============ Negation Data ============
        negation_data = kw.get("negation", {})
        self._negation_terms: Set[str] = set()
        pre_verb = negation_data.get("pre_verb_negators", {})
        if isinstance(pre_verb, dict):
            self._negation_terms.update(pre_verb.get("terms", []))
        post_clause = negation_data.get("post_clause_negators", [])
        if isinstance(post_clause, list):
            self._negation_terms.update(post_clause)
        self._negation_exceptions: Set[str] = set(negation_data.get("negation_exceptions", []))
        self._resolution_phrases: Set[str] = set(negation_data.get("resolution_phrases", []))
        self._clause_boundaries: Set[str] = set(negation_data.get("clause_boundaries", []))

        # ============ Context Detection ============
        context_data = kw.get("context_detection", {})
        self._question_markers: Set[str] = set(context_data.get("question_markers", []))
        self._quotation_markers: Set[str] = set(context_data.get("quotation_markers", []))
        self._mention_markers: Set[str] = set(context_data.get("mention_markers", []))

        # ============ Urgency ============
        self._urgency_all: Set[str] = set()
        urgency_data = kw.get("urgency_markers", {})
        for category, markers in urgency_data.items():
            if isinstance(markers, list):
                self._urgency_all.update(markers)

        # ============ Subject Markers ============
        self._subject_markers: Set[str] = set()
        subject_data = kw.get("subject_markers", {})
        for key in ["student_pronouns", "student_question_subject"]:
            if isinstance(subject_data.get(key), list):
                self._subject_markers.update(subject_data.get(key, []))

        # ============ Implicit Patterns ============
        self._implicit_patterns: Set[str] = set()
        implicit_data = kw.get("implicit_request_patterns", {})
        for key in ["availability_question", "problem_state"]:
            if isinstance(implicit_data.get(key), list):
                self._implicit_patterns.update(implicit_data.get(key, []))

        # ============ Boost Patterns ============
        self._boost_patterns: Set[str] = set()
        boost_data = kw.get("high_confidence_boost_patterns", {})
        if isinstance(boost_data, dict):
            patterns = boost_data.get("patterns", [])
            if isinstance(patterns, list):
                self._boost_patterns.update(patterns)

        # ============ Signal Combination Rules ============
        signal_combo = kw.get("signal_combinations", {})

        # Compound requests
        self._compound_requests: Set[str] = set()
        self._compound_priority: int = 200
        compound_data = signal_combo.get("compound_requests", {})
        if isinstance(compound_data, dict):
            self._compound_priority = compound_data.get("priority", 200)
            patterns = compound_data.get("patterns", [])
            if isinstance(patterns, list):
                self._compound_requests.update(patterns)

        # Allowed rules
        self._allowed_rules: List[Dict[str, Any]] = []
        allowed_list = signal_combo.get("allowed_rules", [])
        if isinstance(allowed_list, list):
            for rule in allowed_list:
                if isinstance(rule, dict) and "signals" in rule:
                    signals = rule.get("signals", [])
                    if isinstance(signals, list) and len(signals) >= 2:
                        self._allowed_rules.append({
                            "id": rule.get("id", "unknown"),
                            "signals": frozenset(signals[:2]),
                            "decision": rule.get("decision", "review"),
                            "priority": rule.get("priority", 50),
                        })
        self._allowed_rules.sort(key=lambda x: x["priority"], reverse=True)

        # Forbidden rules
        self._forbidden_rules: List[Dict[str, Any]] = []
        forbidden_list = signal_combo.get("forbidden_rules", [])
        if isinstance(forbidden_list, list):
            for rule in forbidden_list:
                if isinstance(rule, dict) and "signals" in rule:
                    signals = rule.get("signals", [])
                    if isinstance(signals, list) and len(signals) >= 2:
                        self._forbidden_rules.append({
                            "id": rule.get("id", "unknown"),
                            "signals": frozenset(signals[:2]),
                            "unless_any": set(rule.get("unless_any", [])),
                            "reason": rule.get("reason", ""),
                            "priority": rule.get("priority", 150),
                        })
        self._forbidden_rules.sort(key=lambda x: x["priority"], reverse=True)

        # ============ Build All Tries ============
        self._intent_trie = WeightedTrie(self._intent_verbs_all, self._intent_weights)
        self._action_trie = WeightedTrie(self._action_verbs)
        self._academic_trie = WeightedTrie(self._academic_objects_all, self._academic_weights)
        self._request_trie = WeightedTrie(self._request_phrases_all)
        self._help_trie = WeightedTrie(self._help_expressions)
        self._expert_trie = WeightedTrie(self._expert_terms)
        self._negation_trie = WeightedTrie(self._negation_terms)
        self._resolution_trie = WeightedTrie(self._resolution_phrases)
        self._implicit_trie = WeightedTrie(self._implicit_patterns)
        self._urgency_trie = WeightedTrie(self._urgency_all)
        self._subject_trie = WeightedTrie(self._subject_markers)
        self._compound_trie = WeightedTrie(self._compound_requests)
        self._boost_trie = WeightedTrie(self._boost_patterns)

        # For fuzzy fallback
        self._all_intent_terms: List[str] = list(self._intent_verbs_all)

        self._raw_keywords = kw

    def reload_keywords(self, path: str = "keywords.json") -> None:
        """Reload keywords from file (backward compatibility)."""
        try:
            from config import load_keywords
            fresh = load_keywords(path)
            self._load_keyword_sets(fresh)
            self._stats["keyword_reloads"] = self._stats.get("keyword_reloads", 0) + 1
            logger.info("Keywords reloaded from {}", path)
        except Exception as e:
            logger.error("Failed to reload keywords from {}: {}", path, e)

    _build_keyword_sets = reload_keywords

    # ============================================================
    # Normalization
    # ============================================================

    def _normalize_arabic(self, text: str) -> str:
        text = text.translate(self.ARABIC_NORMALIZE)
        if PYARABIC_AVAILABLE:
            try:
                text = araby.strip_tashkeel(text)
                text = araby.strip_tatweel(text)
            except Exception:
                pass
        return text

    def _clean(self, text: str) -> str:
        cleaned = re.sub(r'\s+', ' ', text).strip()
        cleaned = cleaned.lower()
        cleaned = self._normalize_arabic(cleaned)
        return cleaned

    # ============================================================
    # Language Detection
    # ============================================================

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

    # ============================================================
    # Context Detection
    # ============================================================

    def _detect_negation(self, text: str) -> Tuple[bool, float]:
        """Detect negation with resolution and exception handling."""
        resolution_match = self._resolution_trie.search_first(text)
        if resolution_match:
            return False, 0.0

        for exception in self._negation_exceptions:
            if exception in text:
                return False, 0.0

        negation_match = self._negation_trie.search_first(text)
        if negation_match:
            return True, 0.8

        return False, 0.0

    def _detect_question(self, text: str) -> bool:
        """Detect interrogative context."""
        for marker in self._question_markers:
            if marker in text:
                return True
        return "؟" in text or "?" in text

    def _detect_quotation(self, text: str) -> bool:
        """Detect quoted or mentioned text."""
        for marker in self._quotation_markers:
            if marker in text:
                return True
        for marker in self._mention_markers:
            if marker in text:
                return True
        return False

    # ============================================================
    # Signal Detection
    # ============================================================

    def _detect_signals(self, text: str) -> Dict[str, Any]:
        """Detect all primary and secondary signals."""
        signals: Set[str] = set()
        secondary: Set[str] = set()
        matched_terms: Dict[str, List[str]] = {}

        intent_match = self._intent_trie.search_first(text)
        if intent_match:
            signals.add("intent_verb")
            matched_terms["intent_verb"] = [intent_match[0]]

        action_match = self._action_trie.search_first(text)
        if action_match:
            signals.add("action_verb")
            matched_terms["action_verb"] = [action_match[0]]

        academic_matches = self._academic_trie.search_all(text)
        if academic_matches:
            signals.add("academic_object")
            matched_terms["academic_object"] = [m[0] for m in academic_matches[:3]]

        request_match = self._request_trie.search_first(text)
        if request_match:
            signals.add("request_phrase")
            matched_terms["request_phrase"] = [request_match[0]]

        help_match = self._help_trie.search_first(text)
        if help_match:
            signals.add("help_expression")
            matched_terms["help_expression"] = [help_match[0]]

        expert_match = self._expert_trie.search_first(text)
        if expert_match:
            signals.add("expert_term")
            matched_terms["expert_term"] = [expert_match[0]]

        implicit_match = self._implicit_trie.search_first(text)
        if implicit_match:
            signals.add("problem_state")
            matched_terms["problem_state"] = [implicit_match[0]]

        urgency_match = self._urgency_trie.search_first(text)
        if urgency_match:
            secondary.add("urgency")
            matched_terms["urgency"] = [urgency_match[0]]

        subject_match = self._subject_trie.search_first(text)
        if subject_match:
            secondary.add("subject_marker")
            matched_terms["subject_marker"] = [subject_match[0]]

        return {
            "primary": signals,
            "secondary": secondary,
            "matched_terms": matched_terms,
        }

    # ============================================================
    # Rule Resolution
    # ============================================================

    def _resolve_rules(
        self,
        text: str,
        signals: Set[str],
        is_negated: bool,
        is_question: bool,
        is_quoted: bool,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Deterministic rule resolution.
        Priority: negation → quotation → forbidden → compound → allowed → insufficient
        """
        # STEP 1: Negation
        if is_negated:
            return False, "ignore", {
                "gate_reason": "negation_detected",
                "primary_signals": sorted(signals),
                "is_sufficient": False,
                "decision_hint": "ignore",
            }

        # STEP 2: Quotation
        if is_quoted:
            return False, "ignore", {
                "gate_reason": "quoted_text_suppressed",
                "primary_signals": sorted(signals),
                "is_sufficient": False,
                "decision_hint": "ignore",
            }

        # STEP 3: Forbidden rules (conditional)
        for forbidden in self._forbidden_rules:
            forbidden_signals = forbidden["signals"]
            unless_any = forbidden["unless_any"]

            if forbidden_signals.issubset(signals):
                has_exception = bool(signals.intersection(unless_any))
                if not has_exception:
                    return False, "ignore", {
                        "gate_reason": f"forbidden_rule: {forbidden['id']}",
                        "primary_signals": sorted(signals),
                        "is_sufficient": False,
                        "decision_hint": "ignore",
                    }

        # STEP 4: Compound rules (exact match)
        compound_match = self._compound_trie.search_exact(text)
        if compound_match:
            decision = "accept"
            if is_question:
                decision = "review"
            return True, decision, {
                "gate_reason": f"compound_request: {compound_match[0]}",
                "primary_signals": ["compound_request"],
                "is_sufficient": True,
                "decision_hint": decision,
            }

        # STEP 5: Allowed rules (priority-based)
        best_allowed: Optional[Dict[str, Any]] = None
        for allowed in self._allowed_rules:
            if allowed["signals"].issubset(signals):
                if best_allowed is None or allowed["priority"] > best_allowed["priority"]:
                    best_allowed = allowed

        if best_allowed is not None:
            decision = best_allowed["decision"]
            if is_question and decision == "accept":
                decision = "review"
            return True, decision, {
                "gate_reason": f"allowed_rule: {best_allowed['id']}",
                "primary_signals": sorted(signals),
                "is_sufficient": True,
                "decision_hint": decision,
            }

        # STEP 6: Insufficient
        decision = "ignore"
        if len(signals) == 1:
            decision = "review"

        return False, decision, {
            "gate_reason": "insufficient_signals",
            "primary_signals": sorted(signals),
            "is_sufficient": False,
            "decision_hint": decision,
        }

    # ============================================================
    # Main Analysis
    # ============================================================

    async def analyze(self, text: str) -> Dict[str, Any]:
        start = time.perf_counter()

        try:
            if len(text) > _cfg("MAX_MESSAGE_LENGTH", 2000):
                return self._result("ignore", 0.0, ["too_long"])

            cleaned = self._clean(text)

            if _cfg("PREFILTER_ENABLED", True):
                ok, reason, _ = Prefilter.check(cleaned, _cfg("PREFILTER_MAX_EMOJIS", 5))
                if not ok:
                    return self._result("ignore", 0.0, [reason])

            # Language check
            is_arabic, arabic_ratio = self._is_arabic(cleaned)
            if _cfg("LANGUAGE_FILTER", True) and not is_arabic:
                return self._result("ignore", 0.0, ["non_arabic"])

            # Context detection (BEFORE signal matching)
            is_negated, _ = self._detect_negation(cleaned)
            is_question = self._detect_question(cleaned)
            is_quoted = self._detect_quotation(cleaned)

            # Signal detection
            signal_data = self._detect_signals(cleaned)
            signals = signal_data["primary"]
            secondary = signal_data["secondary"]
            matched_terms = signal_data["matched_terms"]

            # Rule resolution
            is_sufficient, gate_decision, gate_details = self._resolve_rules(
                cleaned, signals, is_negated, is_question, is_quoted
            )

            # Build result
            result = FilterResult()
            result.negation_detected = is_negated
            result.signal_combination = gate_details
            result.signal_combination["secondary_signals"] = sorted(secondary)
            result.signal_combination["matched_terms"] = matched_terms

            if not is_sufficient:
                result.valid = False
                result.reason = gate_details["gate_reason"]
                result.decision = gate_decision
                result.confidence = 0.30 if gate_decision == "review" else 0.0
                result.key_phrases = gate_details.get("primary_signals", [])

                async with self._stats_lock:
                    self._stats["rejected"] += 1
                    if gate_decision == "review":
                        self._stats["review"] += 1
                    else:
                        self._stats["insufficient_combination"] += 1
                        if is_negated:
                            self._stats["negation_blocks"] += 1
                        if is_quoted:
                            self._stats["quotation_suppressions"] += 1

                result.analysis_time_ms = round((time.perf_counter() - start) * 1000, 2)
                return result.to_dict()

            # Passed gate — accept/review
            result.valid = True
            result.intent_verb = matched_terms.get("intent_verb", [None])[0]
            result.academic_object = matched_terms.get("academic_object", [None])[0]
            result.urgency_marker = matched_terms.get("urgency", [None])[0]
            result.decision = gate_decision
            result.confidence = 0.70 if gate_decision == "accept" else 0.45
            result.key_phrases = (
                matched_terms.get("intent_verb", []) +
                matched_terms.get("academic_object", [])
            )
            result.reasons = [
                f"gate_reason: {gate_details['gate_reason']}",
                f"primary_signals: {gate_details.get('primary_signals', [])}",
            ]

            async with self._stats_lock:
                self._stats["processed"] += 1
                self._stats["valid"] += 1
                if gate_decision == "accept":
                    self._stats["accepted"] += 1
                else:
                    self._stats["review"] += 1

            result.analysis_time_ms = round((time.perf_counter() - start) * 1000, 2)
            return result.to_dict()

        except Exception as e:
            logger.exception("Filter.analyze error")
            return self._result("ignore", 0.0, [f"internal_error: {str(e)[:50]}"])

    # ============================================================
    # Helper Methods
    # ============================================================

    def _result(self, decision: str, confidence: float, reasons: List[str]) -> Dict[str, Any]:
        return FilterResult(
            valid=(decision == "accept"),
            reason=reasons[0] if reasons else decision,
            decision=decision,
            confidence=confidence,
            reasons=reasons,
        ).to_dict()

    async def record_feedback(self, term: str, term_kind: str, was_correct: bool) -> float:
        """Public feedback hook for adaptive weights."""
        if term_kind == "intent":
            weight = await self._adaptive_intent.record_feedback(term, was_correct)
        elif term_kind == "academic":
            weight = await self._adaptive_academic.record_feedback(term, was_correct)
        else:
            raise ValueError(f"Unknown term_kind: {term_kind!r}")
        async with self._stats_lock:
            self._stats["feedback_events"] += 1
        return weight

    async def get_telemetry(self) -> Dict[str, Any]:
        """Get filter statistics."""
        async with self._stats_lock:
            stats = dict(self._stats)
            stats["uptime"] = int(time.time() - self._last_stats_reset)
            stats["cache_size"] = len(self._text_cache)
        try:
            stats["latency_percentiles"] = await self._metrics.snapshot()
        except Exception:
            stats["latency_percentiles"] = {}
        stats["adaptive_feedback_count"] = (
            self._adaptive_intent.feedback_count +
            self._adaptive_academic.feedback_count
        )
        return stats

    async def clear_cache(self) -> None:
        """Clear all caches."""
        await self._bloom.clear()
        async with self._cache_lock:
            self._text_cache.clear()
        logger.info("EnhancedFilter v15.7.0 caches cleared")

    def shutdown(self) -> None:
        """Release resources."""
        try:
            self._regex_guard.shutdown()
        except Exception:
            pass


# ============================================================
# Moderation Service (backward compatibility)
# ============================================================

class ModerationService:
    """Unified façade over EnhancedFilter."""

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
            return self.filter._result("ignore", 0.0, ["analysis_timeout"])
        except Exception:
            logger.exception("analyze_unexpected_error")
            return self.filter._result("ignore", 0.0, ["internal_error"])

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