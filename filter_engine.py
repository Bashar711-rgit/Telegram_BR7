#!/usr/bin/env python3

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
        }


class Prefilter:
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
            "Filter v14.1 ready | intent_verbs={} | academic_objects={} | negation={} | boost_patterns={} | "
            "distance_scoring={}",
            len(self._intent_verbs_all),
            len(self._academic_objects_all),
            len(self._negation_all),
            len(self._boost_patterns),
            "ON" if CFG.DISTANCE_SCORING_ENABLED else "OFF",
        )

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
        kw = keywords_data if keywords_data is not None else KEYWORDS

        self._intent_verbs: Dict[str, Dict[str, Any]] = kw.get("intent_verbs", {})
        self._intent_verbs_all: Set[str] = set()
        self._intent_weights: Dict[str, float] = {}
        for tier, data in self._intent_verbs.items():
            if isinstance(data, dict) and "terms" in data:
                weight = data.get("_weight_hint", 0.7)
                for term in data.get("terms", []):
                    self._intent_verbs_all.add(term)
                    self._intent_weights[term] = weight

        self._academic_objects: Dict[str, Dict[str, Any]] = kw.get("academic_objects", {})
        self._academic_objects_all: Set[str] = set()
        self._academic_weights: Dict[str, float] = {}
        for obj_type, data in self._academic_objects.items():
            if isinstance(data, dict) and "terms" in data:
                weight = data.get("_weight_hint", 0.7)
                for term in data.get("terms", []):
                    self._academic_objects_all.add(term)
                    self._academic_weights[term] = weight

        request_phrases_data = kw.get("request_phrases", {})
        self._request_phrases_all: Set[str] = set()
        for category, phrases in request_phrases_data.items():
            if isinstance(phrases, list):
                self._request_phrases_all.update(phrases)

        self._indirect_request: List[str] = kw.get("indirect_request", [])
        self._indirect_request_all: Set[str] = set(self._indirect_request)

        urgency_data = kw.get("urgency_markers", {})
        self._urgency_all: Set[str] = set()
        for category, markers in urgency_data.items():
            if isinstance(markers, list):
                self._urgency_all.update(markers)

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

        boost_data = kw.get("high_confidence_boost_patterns", {})
        self._boost_patterns: Set[str] = set()
        if isinstance(boost_data, dict):
            patterns = boost_data.get("patterns", [])
            if isinstance(patterns, list):
                self._boost_patterns.update(patterns)

        template_generated = self._generate_template_patterns(kw)
        self._boost_patterns.update(template_generated)
        self._stats["template_patterns_generated"] = len(template_generated)

        self._ad_signals: Dict[str, Any] = kw.get("advertisement_signals", {})

        self._spam_categories: Dict[str, List[str]] = kw.get("spam_categories", {})
        self._spam_all: Set[str] = set()
        for category, terms in self._spam_categories.items():
            if isinstance(terms, list):
                self._spam_all.update(terms)

        emoji_data = kw.get("emoji_signals", {})
        self._ad_emoji: Set[str] = set(emoji_data.get("ad_style_emoji", []))
        self._neutral_emoji: Set[str] = set(emoji_data.get("neutral_emoji", []))

        self._ad_blockers: Set[str] = set(kw.get("ad_blockers", []))

        ignore_data = kw.get("ignore_signals", {})
        self._ignore_all: Set[str] = set()
        for category, terms in ignore_data.items():
            if isinstance(terms, list):
                self._ignore_all.update(terms)

        self._help_expressions: Set[str] = set(kw.get("help_expressions", []))

        action_verbs_data = kw.get("action_verbs", {})
        self._action_verbs: Set[str] = set()
        for key in ["core", "suffixed_forms", "imperative_forms"]:
            if isinstance(action_verbs_data.get(key), list):
                self._action_verbs.update(action_verbs_data.get(key, []))

        subject_data = kw.get("subject_markers", {})
        self._subject_markers: Set[str] = set()
        for key in ["student_pronouns", "student_question_subject"]:
            if isinstance(subject_data.get(key), list):
                self._subject_markers.update(subject_data.get(key, []))

        implicit_data = kw.get("implicit_request_patterns", {})
        self._implicit_request_all: Set[str] = set()
        for key in ["availability_question", "problem_state"]:
            if isinstance(implicit_data.get(key), list):
                self._implicit_request_all.update(implicit_data.get(key, []))

        solve_data = kw.get("solve_actions", {})
        self._solve_academic: Set[str] = set()
        self._technical_problem_terms: Set[str] = set()
        if isinstance(solve_data.get("academic_solution"), list):
            self._solve_academic.update(solve_data.get("academic_solution", []))
        if isinstance(solve_data.get("technical_problem_terms"), list):
            self._technical_problem_terms.update(solve_data.get("technical_problem_terms", []))

        self._dialect_map: Dict[str, str] = {}
        dialect_data = kw.get("dialect_mapping", {})
        for category, mapping in dialect_data.items():
            if isinstance(mapping, dict):
                self._dialect_map.update(mapping)

        self._university_context: Set[str] = set()
        university_data = kw.get("university_context", {})
        for key, value in university_data.items():
            if isinstance(value, list):
                self._university_context.update(value)

        self._distance_config: Dict[str, Any] = kw.get("distance_scoring_config", {})

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

        self._scoring_weights: Dict[str, float] = {}
        weights_data = kw.get("scoring_weights", {})
        positive_modules = weights_data.get("positive_modules", {})
        if isinstance(positive_modules, dict):
            for key, value in positive_modules.items():
                self._scoring_weights[key] = float(value)

        self._clause_boundaries: Set[str] = set()
        boundaries = self._negation.get("clause_boundaries", [])
        if isinstance(boundaries, list):
            self._clause_boundaries.update(boundaries)

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

        self._raw_keywords = kw

    def _build_tries(self) -> None:
        pass

    def reload_keywords(self, path: str = "keywords.json") -> None:
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

    _build_keyword_sets = reload_keywords

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

    def _detect_negation(self, text: str) -> Tuple[bool, float, List[str]]:
        negation_score = 0.0
        reasons = []

        resolution_match = self._resolution_trie.search_first(text)
        if resolution_match:
            negation_score = 1.0
            reasons.append(f"resolution_phrase: {resolution_match[0]}")
            return True, negation_score, reasons

        post_clause = self._negation.get("post_clause_negators", [])
        if isinstance(post_clause, list):
            for neg in post_clause:
                if neg in text:
                    for ex in self._negation_exceptions:
                        if ex in text:
                            return False, 0.0, []
                    negation_score = 0.8
                    reasons.append(f"post_clause_negator: {neg}")
                    return True, negation_score, reasons

        pre_verb_data = self._negation.get("pre_verb_negators", {})
        if isinstance(pre_verb_data, dict):
            pre_verbs = pre_verb_data.get("terms", [])
            if isinstance(pre_verbs, list):
                for pv in pre_verbs:
                    if pv in text:
                        for ex in self._negation_exceptions:
                            if ex in text:
                                return False, 0.0, []
                        if CFG.NEGATION_CLAUSE_BOUNDARIES_ENABLED:
                            pos = text.find(pv)
                            before_text = text[:pos]
                            for boundary in self._clause_boundaries:
                                if boundary in before_text:
                                    return False, 0.0, []
                        negation_score = 0.6
                        reasons.append(f"pre_verb_negator: {pv}")
                        return True, negation_score, reasons

        return False, 0.0, reasons

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

    def _get_length_modifier(self, token_count: int) -> float:
        for (start, end), value in self._length_modifier.items():
            if start <= token_count <= end:
                return value
        return 0.9

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

            if CFG.PREFILTER_ENABLED:
                ok, reason, metadata = Prefilter.check(
                    cleaned, CFG.PREFILTER_MIN_WORDS, CFG.PREFILTER_MAX_EMOJIS
                )
                if not ok:
                    async with self._stats_lock:
                        self._stats["prefilter_rejected"] += 1
                    return self._result("ignore", 0.0, [reason])

            if await self._bloom.contains(cache_key):
                async with self._stats_lock:
                    self._stats["bloom_hits"] += 1
                return self._result("ignore", 0.0, ["duplicate"])

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
                return self._result("ignore", 0.0, ["non_arabic"])

            spam_score = self._spam_score(cleaned)
            if spam_score > CFG.SPAM_SCORE_THRESHOLD:
                async with self._stats_lock:
                    self._stats["spam"] += 1
                return self._result("ignore", 0.0, ["spam_detected"])

            if self._spam_trie.search_first(cleaned):
                async with self._stats_lock:
                    self._stats["spam"] += 1
                return self._result("ignore", 0.0, ["spam_pattern"])

            if self._ignore_trie.search_first(cleaned):
                return self._result("ignore", 0.0, ["ignore_pattern"])

            if self._ad_blocker_trie.search_first(cleaned):
                return self._result("ignore", 0.0, ["ad_blocker"])

            is_negated, neg_score, neg_reasons = self._detect_negation(cleaned)
            if is_negated and neg_score > 0.7:
                return self._result("ignore", 1.0 - neg_score, neg_reasons)

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

            if ad_score > 0.6:
                return self._result("ignore", 1.0 - ad_score, ad_reasons)

            result = FilterResult()

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
            if intent_word:
                result.reasons.append(f"intent_verb: {intent_word}")
            if academic_word:
                result.reasons.append(f"academic_object: {academic_word}")
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
        }

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
        logger.info("Filter v14.1 caches cleared")