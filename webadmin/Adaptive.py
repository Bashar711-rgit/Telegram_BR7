#!/usr/bin/env python3
"""
adaptive.py — Bounded EMA feedback weighting for keyword confidence.

Each intent-verb / academic-object term starts out with a static "weight
hint" pulled from keywords.json. As real accept/review/ignore decisions get
human feedback (via `EnhancedFilter.record_feedback()`), that feedback
nudges the term's weight toward 1.0 (reinforced — keep trusting this term)
or toward a floor (discounted — this term is producing bad matches) using
an exponential moving average (EMA):

    new_weight = old_weight + alpha * (target - old_weight)

`alpha` controls how fast weights move. The default (0.05, read from
CFG.ADAPTIVE_ALPHA with a safe fallback in filter_engine.py) means a single
piece of feedback only nudges a term a little, so no single mistaken
correction can swing a term's weight wildly — that's the "bounded feedback"
in the module's purpose. Weights are also hard-clamped to
[_MIN_WEIGHT, _MAX_WEIGHT] so a term can never be fully zeroed out (and
permanently blocked) or fully saturate to certainty from feedback alone.
"""

from __future__ import annotations

import asyncio
from typing import Dict


class AdaptiveWeights:
    """EMA-smoothed, bounded per-term confidence weights.

    Behaves like a `Dict[str, float]` for reads (`get()`), plus an async
    `record_feedback()` for updates. `_weights` and `_alpha` are exposed as
    plain attributes (not name-mangled/private) deliberately: filter_engine.py's
    `_reseed_adaptive()` reads them directly when merging learned weights
    into a freshly reloaded keyword set on `reload_keywords()`.
    """

    _MIN_WEIGHT = 0.1
    _MAX_WEIGHT = 1.0
    _CORRECT_TARGET = 1.0
    _INCORRECT_TARGET = 0.1
    _DEFAULT_WEIGHT = 0.7

    def __init__(self, weights: Dict[str, float], alpha: float = 0.05) -> None:
        self._weights: Dict[str, float] = dict(weights)
        self._alpha = alpha
        self._lock = asyncio.Lock()
        self.feedback_count: int = 0

    def get(self, term: str, default: float = _DEFAULT_WEIGHT) -> float:
        """Synchronous read — matches dict.get() semantics used throughout
        filter_engine.py's scoring path (called outside any await chain)."""
        return self._weights.get(term, default)

    async def record_feedback(self, term: str, was_correct: bool) -> float:
        """Apply one EMA update for `term` and return its new weight.

        `was_correct` reflects whether the term's prior contribution to a
        decision was validated as correct (e.g. by a moderator reviewing a
        flagged/ignored message). Bounded to [_MIN_WEIGHT, _MAX_WEIGHT].
        """
        async with self._lock:
            current = self._weights.get(term, self._DEFAULT_WEIGHT)
            target = self._CORRECT_TARGET if was_correct else self._INCORRECT_TARGET
            updated = current + self._alpha * (target - current)
            updated = max(self._MIN_WEIGHT, min(self._MAX_WEIGHT, updated))
            self._weights[term] = updated
            self.feedback_count += 1
            return updated
