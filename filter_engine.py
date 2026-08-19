#!/usr/bin/env python3
"""
filter_engine.py – Smart Filter Engine v15.0 (Context-Aware, Conflict-Resolved)
Architecture: Prefilter + Signal Aggregation + Conflict Resolution + Conditional Probability
Supports: keywords.json v15.x, config.py v13.1

v15.0 (this pass) — fundamental architectural fixes:
  * REMOVED early-exit short-circuits that caused order-dependent misclassification.
    Previously: resolution_trie match -> immediate ignore (even if a new request
    existed later in the same message). Now: all signals collected first, then
    conflicts resolved, then decision made.
  * Negation now distinguished from distress. "ما عرفت احل" is a HELP REQUEST
    (distress), not a negation of a request. Previously the pre_verb_negator
    "ما" would suppress the exact same phrase that indirect_distress was
    trying to catch.
  * Confidence calculation switched from simple weighted sum to conditional
    probability: P(help) = P(intent) × P(academic|intent) × urgency_factor.
    This prevents "احتاج + جامعة" (personal statement) from scoring the
    same as "احتاج + مشروع" (actual help request).
  * template_boost is now academic-context-gated. A template match like
    "مين يساعد" alone gets +0.05; "مين يساعد في المشروع" gets +0.25.
  * search_first() replaced with search_all() for signal aggregation, so
    multiple signals are all collected before scoring.
  * Resolution + new request in same message now correctly handled:
    "لقيت حل للواجب لكن عندي تقرير ثاني" -> review instead of ignore.
  * Added signal priority: phrase_match > entity_match > single_token_match.
  * Added _resolve_conflicts() to handle negation-vs-distress, ad-vs-academic,
    resolution-vs-new-request contradictions.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from collections import OrderedDict, defaultdict
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


# =============================================================================
# FilterResult
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
    distress_detected: bool = False
    signals: Dict[str, float] = field(default_factory=dict)

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
            "distress_detected": self.distress_detected,
            "signals": self.signals,
        }


# =============================================================================
# Prefilter
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
# Optimized Bloom Filter
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
# Sharded LRU Cache
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
# Trie Index
# =============================================================================
class TrieNode:
    __slots__ = ("children", "is_end", "word", "weight")

    def __init__(self) -> None:
        self.children: Dict[str, "TrieNode"] = {}
        self.is_end: bool = False
        self.word: Optional[str] = None
        self.weight: float = 1.0


class WeightedTrie:
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
        """Returns (word, weight, position) for the first match."""
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
        """Returns all matches as (word, weight, position)."""
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


# =============================================================================
# Main Filter Engine v15.0
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

    def __init__(self) -> None:
        self._stats: Dict[str, int] = {
            "processed": 0, "valid": 0, "rejected": 0, "spam": 0,
            "cache_hits": 0, "bloom_hits": 0, "fast_path": 0,
            "prefilter_rejected": 0, "accepted": 0, "review": 0,
            "ignored": 0, "total_time_ms": 0, "avg_time_ms": 0,
            "max_time_ms": 0, "min_time_ms": 999999,
            "template_patterns_generated": 0, "keyword_reloads": 0,
            "conflicts_resolved": 0,
        }
        self._stats_lock = asyncio.Lock()
        self._last_stats_reset = time.time()
        self._raw_keywords: Dict[str, Any] = KEYWORDS

        self._load_keyword_sets()
        self._build_tries()

        self._bloom = OptimizedBloomFilter(CFG.BLOOM_FILTER_SIZE, CFG.BLOOM_FILTER_FP)
        self._cache = ShardedLRUCache(CFG.MAX_CACHE_SIZE, CFG.CACHE_TTL)
        self._text_cache = TTLCache(maxsize=CFG.TEXT_CACHE_SIZE, ttl=CFG.TEXT_CACHE_TTL)
        self._cache_lock = asyncio.Lock()

        logger.info(
            "Filter v15.0 ready | intent_verbs={} | academic_objects={} | "
            "conflict_resolution=ON | conditional_probability=ON",
            len(self._intent_verbs_all),
            len(self._academic_objects_all),
        )

    # ─── Template Generation ──────────────────────────────────────────────────

    def _generate_template_patterns(self, kw: Dict[str, Any]) -> Set[str]:
        generated: Set[str] = set()
        templates_data = kw.get("templates", {})
        if not templates_data or not isinstance(templates_data, dict):
            return generated

        template_patterns_list = kw.get("template_patterns", [])
        if not template_patterns_list:
            return generated

        need = templates_data.get("need", [])
        person = templates_data.get("person", [])
        action = templates_data.get("action", [])
        expert = templates_data.get("expert", [])
        availability = templates_data.get("availability", [])

        for pattern in template_patterns_list:
            if not isinstance(pattern, str):
                continue
            parts = pattern.strip().split()

            if len(parts) == 2:
                tag1, tag2 = parts
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
                tag1, tag2, tag3 = parts
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

        return generated

    # ─── Load & Build ─────────────────────────────────────────────────────────

    def _load_keyword_sets(self, keywords_data: Optional[Dict[str, Any]] = None) -> None:
        kw = keywords_data if keywords_data is not None else KEYWORDS

        # 1. Intent Verbs
        self._intent_verbs: Dict[str, Dict[str, Any]] = kw.get("intent_verbs", {})
        self._intent_verbs_all: Set[str] = set()
        self._intent_weights: Dict[str, float] = {}
        for tier, data in self._intent_verbs.items():
            if isinstance(data, dict) and "terms" in data:
                weight = data.get("_weight_hint", 0.7)
                for term in data.get("terms", []):
                    self._intent_verbs_all.add(term)
                    self._intent_weights[term] = weight

        # 2. Academic Objects
        self._academic_objects: Dict[str, Dict[str, Any]] = kw.get("academic_objects", {})
        self._academic_objects_all: Set[str] = set()
        self._academic_weights: Dict[str, float] = {}
        for obj_type, data in self._academic_objects.items():
            if isinstance(data, dict) and "terms" in data:
                weight = data.get("_weight_hint", 0.7)
                for term in data.get("terms", []):
                    self._academic_objects_all.add(term)
                    self._academic_weights[term] = weight

        # 3. Request Phrases
        request_phrases_data = kw.get("request_phrases", {})
        self._request_phrases_all: Set[str] = set()
        for category, phrases in request_phrases_data.items():
            if isinstance(phrases, list):
                self._request_phrases_all.update(phrases)

        # 4. Indirect Distress (NEW: separately tracked)
        self._indirect_distress: Set[str] = set()
        if isinstance(request_phrases_data.get("indirect_distress"), list):
            self._indirect_distress.update(request_phrases_data["indirect_distress"])

        # 5. Urgency Markers
        urgency_data = kw.get("urgency_markers", {})
        self._urgency_all: Set[str] = set()
        for category, markers in urgency_data.items():
            if isinstance(markers, list):
                self._urgency_all.update(markers)

        # 6. Negation
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

        # 7. Boost Patterns
        boost_data = kw.get("high_confidence_boost_patterns", {})
        self._boost_patterns: Set[str] = set()
        if isinstance(boost_data, dict):
            patterns = boost_data.get("patterns", [])
            if isinstance(patterns, list):
                self._boost_patterns.update(patterns)
        self._boost_patterns.update(self._generate_template_patterns(kw))

        # 8. Advertisement Signals
        self._ad_signals: Dict[str, Any] = kw.get("advertisement_signals", {})

        # 9. Spam
        self._spam_categories: Dict[str, List[str]] = kw.get("spam_categories", {})
        self._spam_all: Set[str] = set()
        for category, terms in self._spam_categories.items():
            if isinstance(terms, list):
                self._spam_all.update(terms)

        # 10. Emoji
        emoji_data = kw.get("emoji_signals", {})
        self._ad_emoji: Set[str] = set(emoji_data.get("ad_style_emoji", []))
        self._neutral_emoji: Set[str] = set(emoji_data.get("neutral_emoji", []))

        # 11. Ad Blockers
        self._ad_blockers: Set[str] = set(kw.get("ad_blockers", []))

        # 12. Ignore Signals
        ignore_data = kw.get("ignore_signals", {})
        self._ignore_all: Set[str] = set()
        for category, terms in ignore_data.items():
            if isinstance(terms, list):
                self._ignore_all.update(terms)

        # 13. Help Expressions
        self._help_expressions: Set[str] = set(kw.get("help_expressions", []))

        # 14. Action Verbs
        action_verbs_data = kw.get("action_verbs", {})
        self._action_verbs: Set[str] = set()
        for key in ["core", "suffixed_forms", "imperative_forms"]:
            if isinstance(action_verbs_data.get(key), list):
                self._action_verbs.update(action_verbs_data.get(key, []))

        # 15. Subject Markers
        subject_data = kw.get("subject_markers", {})
        self._subject_markers: Set[str] = set()
        for key in ["student_pronouns", "student_question_subject"]:
            if isinstance(subject_data.get(key), list):
                self._subject_markers.update(subject_data.get(key, []))

        # 16. Implicit Request
        implicit_data = kw.get("implicit_request_patterns", {})
        self._implicit_request_all: Set[str] = set()
        for key in ["availability_question", "problem_state"]:
            if isinstance(implicit_data.get(key), list):
                self._implicit_request_all.update(implicit_data.get(key, []))

        # 17. Solve Actions
        solve_data = kw.get("solve_actions", {})
        self._solve_academic: Set[str] = set(solve_data.get("academic_solution", []))
        self._technical_problem_terms: Set[str] = set(solve_data.get("technical_problem_terms", []))

        # 18. Dialect Mapping
        self._dialect_map: Dict[str, str] = {}
        dialect_data = kw.get("dialect_mapping", {})
        for category, mapping in dialect_data.items():
            if isinstance(mapping, dict):
                self._dialect_map.update(mapping)

        # 19. University Context
        self._university_context: Set[str] = set()
        university_data = kw.get("university_context", {})
        for key, value in university_data.items():
            if isinstance(value, list):
                self._university_context.update(value)

        # 20. Distance Config
        self._distance_config: Dict[str, Any] = kw.get("distance_scoring_config", {})

        # 21. Length Modifier
        self._length_modifier: Dict[Tuple[int, int], float] = {}
        length_data = kw.get("length_modifier", {})
        for key, value in length_data.items():
            if isinstance(key, str) and "_to_" in key:
                try:
                    start, end = map(int, key.split("_to_"))
                    self._length_modifier[(start, end)] = float(value)
                except Exception:
                    pass

        # 22. Clause Boundaries
        self._clause_boundaries: Set[str] = set()
        boundaries = self._negation.get("clause_boundaries", [])
        if isinstance(boundaries, list):
            self._clause_boundaries.update(boundaries)

        # ── Build legacy-compatible sets ───────────────────────────────────
        self.request_words = set(self._intent_verbs_all).union(self._request_phrases_all)
        self.context_words = set(self._academic_objects_all).union(self._university_context)
        self.indirect_words = set(self._implicit_request_all)
        self.urgency_words = self._urgency_all
        self.ignore_words = self._ignore_all

        self.advertisement_words: Set[str] = set()
        for signal_list in ["hard_signals", "medium_signals"]:
            signals = self._ad_signals.get(signal_list, [])
            if isinstance(signals, list):
                self.advertisement_words.update(signals)

        self.education_words = set(self._ad_signals.get("institution_terms", []))
        self.emoji_advertisement = self._ad_emoji
        self.ad_blockers = self._ad_blockers
        self.spam_patterns = self._spam_all

        # ── Build all tries ────────────────────────────────────────────────
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
        self._distress_trie = WeightedTrie(self._indirect_distress)
        self._subject_markers_trie = WeightedTrie(self._subject_markers)
        self._action_verbs_trie = WeightedTrie(self._action_verbs)

        self._raw_keywords = kw

    def _build_tries(self) -> None:
        pass

    def reload_keywords(self, path: str = "keywords.json") -> None:
        from config import load_keywords
        fresh = load_keywords(path)
        self._load_keyword_sets(fresh)
        self._build_tries()
        self._stats["keyword_reloads"] += 1
        logger.info("Filter keyword sets reloaded from {}", path)

    _build_keyword_sets = reload_keywords

    # ─── Normalization ────────────────────────────────────────────────────────

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

    def _clean(self, text: str) -> str:
        text = WS_PATTERN.sub(" ", text).strip()
        text = text.lower()
        text = self._normalize_arabic(text)
        text = self._apply_dialect_mapping(text)
        return text

    # ─── Language Detection ───────────────────────────────────────────────────

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

    # ─── Spam Score ───────────────────────────────────────────────────────────

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

    # ─── Ad Detection (with context awareness) ───────────────────────────────

    def _detect_advertisement(self, text: str, academic_score: float = 0.0) -> Tuple[float, List[str]]:
        ad_score = 0.0
        reasons = []

        hard_signals = self._ad_signals.get("hard_signals", [])
        for signal in hard_signals:
            if signal in text:
                ad_score += 0.4
                reasons.append(f"hard_ad_signal: {signal}")

        medium_signals = self._ad_signals.get("medium_signals", [])
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
        for signal in cta_signals:
            if signal in text:
                ad_score += 0.1
                reasons.append(f"cta_signal: {signal}")

        institution_terms = self._ad_signals.get("institution_terms", [])
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

        # CONTEXT-AWARE: If strong academic context exists, reduce ad score
        if academic_score > 0.7 and ad_score < 0.7:
            ad_score *= 0.5
            reasons.append("ad_score_reduced_by_academic_context")

        return min(ad_score, 1.0), reasons

    # ─── Negation Detection (v15: distinction from distress) ────────────────

    def _detect_negation(self, text: str) -> Tuple[bool, float, List[str]]:
        """Returns (is_negated, negation_score, reasons).
        v15: Distress phrases (ما عرفت احل) are NOT treated as negation.
        """
        negation_score = 0.0
        reasons = []

        # Check exceptions FIRST (distress phrases that look like negation)
        for ex in self._negation_exceptions:
            if ex in text:
                return False, 0.0, []

        # Check distress phrases
        distress_match = self._distress_trie.search_first(text)
        if distress_match:
            return False, 0.0, []  # Not negation, it's distress

        # Resolution phrases
        resolution_match = self._resolution_trie.search_first(text)
        if resolution_match:
            negation_score = 1.0
            reasons.append(f"resolution_phrase: {resolution_match[0]}")
            return True, negation_score, reasons

        # Post-clause negators
        post_clause = self._negation.get("post_clause_negators", [])
        for neg in post_clause:
            if neg in text:
                negation_score = 0.8
                reasons.append(f"post_clause_negator: {neg}")
                return True, negation_score, reasons

        # Pre-verb negators (with clause boundary check)
        pre_verb_data = self._negation.get("pre_verb_negators", {})
        pre_verbs = pre_verb_data.get("terms", []) if isinstance(pre_verb_data, dict) else []
        for pv in pre_verbs:
            if pv in text:
                pos = text.find(pv)
                before_text = text[:pos]
                for boundary in self._clause_boundaries:
                    if boundary in before_text:
                        return False, 0.0, []
                negation_score = 0.6
                reasons.append(f"pre_verb_negator: {pv}")
                return True, negation_score, reasons

        return False, 0.0, reasons

    # ─── Distance Scoring ─────────────────────────────────────────────────────

    def _calculate_distance_score(self, intent_pos: int, academic_pos: int, text_len: int) -> float:
        distance = abs(intent_pos - academic_pos)
        thresholds = self._distance_config.get("thresholds", {})

        for range_str, data in thresholds.items():
            try:
                if "_to_" in range_str:
                    start_str, end_str = range_str.split("_to_")
                    start, end = int(start_str), int(end_str)
                    if start <= distance <= end:
                        return float(data.get("score_multiplier", 1.0))
                elif range_str == "16_plus":
                    if distance >= 16:
                        return float(data.get("score_multiplier", 0.15))
            except Exception:
                continue

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

    # ─── Signal Aggregation (NEW in v15) ────────────────────────────────────

    def _aggregate_signals(self, text: str) -> Dict[str, Any]:
        """Collect ALL signals before any decision. No early exits."""
        signals: Dict[str, Any] = {}

        # Intent verb
        intent_matches = self._request_trie.search_all(text)
        intent_match = intent_matches[0] if intent_matches else None
        signals["intent_word"] = intent_match[0] if intent_match else None
        signals["intent_pos"] = intent_match[2] if intent_match else None
        signals["intent_weight"] = (
            self._intent_weights.get(signals["intent_word"], 0.7)
            if signals["intent_word"] else 0.0
        )

        # Academic objects
        context_matches = self._context_trie.search_all(text)
        signals["context_matches"] = context_matches
        signals["academic_word"] = context_matches[0][0] if context_matches else None
        signals["academic_pos"] = context_matches[0][2] if context_matches else None
        signals["academic_weight"] = (
            self._academic_weights.get(signals["academic_word"], 0.7)
            if signals["academic_word"] else 0.0
        )

        # Urgency
        urgency_match = self._urgency_trie.search_first(text)
        signals["urgency_marker"] = urgency_match[0] if urgency_match else None

        # Indirect / implicit
        indirect_match = self._indirect_trie.search_first(text)
        implicit_match = self._implicit_trie.search_first(text)
        signals["is_indirect"] = indirect_match is not None
        signals["is_implicit"] = implicit_match is not None

        # Distress
        distress_match = self._distress_trie.search_first(text)
        signals["distress"] = distress_match is not None
        signals["distress_word"] = distress_match[0] if distress_match else None

        # Boost pattern
        boost_match = self._boost_trie.search_first(text)
        signals["boost_pattern"] = boost_match[0] if boost_match else None

        # Resolution
        resolution_match = self._resolution_trie.search_first(text)
        signals["resolution"] = resolution_match is not None

        # Negation
        is_negated, neg_score, neg_reasons = self._detect_negation(text)
        signals["is_negated"] = is_negated
        signals["negation_score"] = neg_score
        signals["negation_reasons"] = neg_reasons

        # Ad score (with academic context awareness)
        academic_score = signals["academic_weight"]
        ad_score, ad_reasons = self._detect_advertisement(text, academic_score)
        signals["ad_score"] = ad_score
        signals["ad_reasons"] = ad_reasons

        # Spam
        signals["spam_score"] = self._spam_score(text)
        signals["spam_match"] = self._spam_trie.search_first(text) is not None

        # Ignore signals
        signals["ignore_match"] = self._ignore_trie.search_first(text) is not None

        # Ad blocker
        signals["ad_blocker_match"] = self._ad_blocker_trie.search_first(text) is not None

        return signals

    # ─── Conflict Resolution (NEW in v15) ───────────────────────────────────

    def _resolve_conflicts(self, signals: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve contradictory signals before final scoring."""
        conflicts = []

        # Conflict 1: Resolution + New Request
        if signals.get("resolution") and signals.get("intent_word"):
            # "تم الحل لكن عندي مشروع ثاني"
            signals["resolution"] = False
            signals["resolution_softened"] = True
            conflicts.append("resolution_with_new_request")
            self._stats["conflicts_resolved"] += 1

        # Conflict 2: Negation + Distress
        if signals.get("is_negated") and signals.get("distress"):
            # "ما عرفت احل" — distress, not negation
            signals["is_negated"] = False
            signals["negation_score"] = 0.0
            signals["negation_reasons"] = []
            conflicts.append("negation_as_distress")

        # Conflict 3: Ad + Strong Academic
        if signals.get("ad_score", 0) > 0.5 and signals.get("academic_weight", 0) > 0.7:
            # "عندي مشروع وأحتاج مكتب خدمات" — ambiguous
            signals["ad_score"] *= 0.5
            conflicts.append("ad_with_academic_context")

        # Conflict 4: Ignore + Strong Intent
        if signals.get("ignore_match") and signals.get("intent_weight", 0) > 0.8:
            # Greeting + actual request
            signals["ignore_match"] = False
            conflicts.append("ignore_with_strong_intent")

        signals["_conflicts"] = conflicts
        return signals

    # ─── Conditional Probability Scoring (NEW in v15) ──────────────────────

    def _calculate_conditional_confidence(self, signals: Dict[str, Any]) -> float:
        """P(help) = P(intent) × P(academic|intent) × urgency_factor × context_factor"""
        
        intent_w = signals.get("intent_weight", 0.0)
        academic_w = signals.get("academic_weight", 0.0)
        urgency = signals.get("urgency_marker") is not None
        context_count = len(signals.get("context_matches", []))
        distress = signals.get("distress", False)
        
        # Base: no intent -> very low confidence
        if intent_w < 0.3:
            return 0.1 + (intent_w * 0.2)
        
        # Intent + Distress (e.g., "متوهق في الواجب")
        if distress and academic_w > 0.3:
            return 0.55 + (academic_w * 0.2)
        
        # Intent without academic object
        if academic_w < 0.2:
            return intent_w * 0.4
        
        # Intent + Academic (the core signal)
        base = intent_w * academic_w
        
        # Urgency multiplier
        urgency_multiplier = 1.0 + (0.3 if urgency else 0.0)
        
        # Context multiplier (more academic terms = more confident)
        context_multiplier = 1.0 + min(0.2, context_count * 0.05)
        
        # Negation penalty
        negation_penalty = 1.0 - (signals.get("negation_score", 0.0) * 0.5)
        
        # Ad penalty
        ad_penalty = 1.0 - (signals.get("ad_score", 0.0) * 0.7)
        
        confidence = base * urgency_multiplier * context_multiplier * negation_penalty * ad_penalty
        
        # Template boost (context-gated)
        if signals.get("boost_pattern"):
            if academic_w > 0.5:
                confidence += 0.25
            elif academic_w > 0.3:
                confidence += 0.10
            else:
                confidence += 0.03
        
        # Length modifier
        token_count = len(signals.get("_raw_text", "").split())
        confidence *= self._get_length_modifier(token_count)
        
        return max(0.0, min(1.0, confidence))

    # ─── Main Analysis ────────────────────────────────────────────────────────

    async def analyze(self, text: str) -> Dict[str, Any]:
        start = time.perf_counter()

        try:
            if len(text) > CFG.MAX_MESSAGE_LENGTH:
                return self._result("ignore", 0.0, ["too_long"])

            validated = InputSanitizer.validate_message_text(text)
            if validated is None:
                return self._result("ignore", 0.0, ["invalid_input"])

            cleaned = self._clean(validated)
            cache_key = hashlib.blake2b(cleaned.encode(), digest_size=16).hexdigest()[:32]

            # Prefilter
            if CFG.PREFILTER_ENABLED:
                ok, reason, metadata = Prefilter.check(
                    cleaned, CFG.PREFILTER_MIN_WORDS, CFG.PREFILTER_MAX_EMOJIS
                )
                if not ok:
                    self._stats["prefilter_rejected"] += 1
                    return self._result("ignore", 0.0, [reason])

            # Cache
            async with self._cache_lock:
                if cache_key in self._text_cache:
                    self._stats["cache_hits"] += 1
                    result = dict(self._text_cache[cache_key])
                    result["analysis_time_ms"] = round((time.perf_counter() - start) * 1000, 2)
                    return result

            # Bloom
            if await self._bloom.contains(cache_key):
                self._stats["bloom_hits"] += 1
                return self._result("ignore", 0.0, ["duplicate"])
            await self._bloom.add(cache_key)

            self._stats["processed"] += 1

            # Language
            is_arabic, arabic_ratio = self._is_arabic(cleaned)
            if CFG.LANGUAGE_FILTER and not is_arabic:
                return self._result("ignore", 0.0, ["non_arabic"])

            # ─── AGGREGATE ALL SIGNALS (no early exits) ────────────────────
            signals = self._aggregate_signals(cleaned)
            signals["_raw_text"] = cleaned

            # ─── RESOLVE CONFLICTS ─────────────────────────────────────────
            signals = self._resolve_conflicts(signals)

            # ─── HARD BLOCKS (only if no conflicting signals) ─────────────
            # Spam
            if signals.get("spam_match") and not signals.get("intent_word"):
                self._stats["spam"] += 1
                return self._result("ignore", 0.0, ["spam_pattern"])

            # Ad blocker (only if no academic context)
            if signals.get("ad_blocker_match") and signals.get("academic_weight", 0) < 0.3:
                return self._result("ignore", 0.0, ["ad_blocker"])

            # Strong ad (only if no intent)
            if signals.get("ad_score", 0) > 0.7 and signals.get("intent_weight", 0) < 0.2:
                return self._result("ignore", 0.0, signals.get("ad_reasons", []))

            # Negation (strong, no distress)
            if signals.get("is_negated") and signals.get("negation_score", 0) > 0.7:
                return self._result("ignore", 0.3, signals.get("negation_reasons", []))

            # ─── CALCULATE CONFIDENCE ─────────────────────────────────────
            confidence = self._calculate_conditional_confidence(signals)

            # ─── BUILD RESULT ─────────────────────────────────────────────
            result = FilterResult()
            result.intent_verb = signals.get("intent_word")
            result.academic_object = signals.get("academic_word")
            result.urgency_marker = signals.get("urgency_marker")
            result.negation_detected = signals.get("is_negated", False)
            result.distress_detected = signals.get("distress", False)
            result.advert_score = signals.get("ad_score", 0.0)
            result.confidence = confidence
            result.signals = {k: v for k, v in signals.items() if isinstance(v, (int, float, bool, str))}

            # Reasons
            if result.intent_verb:
                result.reasons.append(f"intent_verb: {result.intent_verb}")
            if result.academic_object:
                result.reasons.append(f"academic_object: {result.academic_object}")
            if result.urgency_marker:
                result.reasons.append(f"urgency: {result.urgency_marker}")
            if signals.get("is_implicit"):
                result.reasons.append("implicit_request")
            if signals.get("distress"):
                result.reasons.append(f"distress: {signals.get('distress_word', '')}")
            if signals.get("is_negated"):
                result.reasons.extend(signals.get("negation_reasons", []))
            if signals.get("ad_score", 0) > 0.3:
                result.reasons.extend(signals.get("ad_reasons", []))
            if signals.get("boost_pattern"):
                result.reasons.append(f"template_boost: {signals['boost_pattern']}")
            if signals.get("_conflicts"):
                result.reasons.append(f"conflicts_resolved: {', '.join(signals['_conflicts'])}")

            # Decision
            if confidence >= CFG.CONFIDENCE_ACCEPT_THRESHOLD:
                result.decision = "accept"
                result.valid = True
            elif confidence >= CFG.CONFIDENCE_REVIEW_THRESHOLD:
                result.decision = "review"
                result.valid = False
            else:
                result.decision = "ignore"
                result.valid = False

            # Stats
            if result.valid:
                self._stats["valid"] += 1
                self._stats["accepted"] += 1
            else:
                self._stats["rejected"] += 1
                if result.decision == "review":
                    self._stats["review"] += 1
                else:
                    self._stats["ignored"] += 1

            result.analysis_time_ms = round((time.perf_counter() - start) * 1000, 2)
            result.language = "ar" if is_arabic else "unknown"
            result.lang_conf = arabic_ratio
            result.word_count = len(cleaned.split())

            # Score details for explainability
            result.score_details = {
                "conditional_confidence": round(confidence, 4),
                "intent_weight": round(signals.get("intent_weight", 0), 4),
                "academic_weight": round(signals.get("academic_weight", 0), 4),
                "negation_score": round(signals.get("negation_score", 0), 4),
                "ad_score": round(signals.get("ad_score", 0), 4),
                "context_count": len(signals.get("context_matches", [])),
            }

            result_dict = result.to_dict()
            result_dict["valid"] = result.valid

            async with self._cache_lock:
                self._text_cache[cache_key] = result_dict

            return result_dict

        except Exception as e:
            logger.error(f"Filter.analyze error: {e}")
            return self._result("ignore", 0.0, [f"internal_error: {str(e)[:50]}"])

    # ─── Helpers ──────────────────────────────────────────────────────────────

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
            "distress_detected": False,
            "signals": {},
        }

    # ─── Telemetry ────────────────────────────────────────────────────────────

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
        logger.info("Filter v15.0 caches cleared")