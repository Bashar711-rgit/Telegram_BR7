#!/usr/bin/env python3
"""
metrics.py — Bounded latency metrics with anomaly detection.

Tracks a rolling window of latency samples (milliseconds) for
EnhancedFilter.analyze() and exposes:

  - percentile snapshots (p50/p95/p99) for telemetry consumers
    (`get_telemetry()` in filter_engine.py reads these).
  - lightweight z-score based anomaly detection on each new sample, so a
    single unusually slow analyze() call can be logged and surfaced without
    needing an external metrics backend (Prometheus/Datadog/etc).

The window is bounded — a `collections.deque(maxlen=window)` — so memory
use and per-sample computation cost never grow with uptime. That boundedness
is the whole point of this module existing instead of reaching for a
full time-series library for what is, in practice, a single hot counter.

v14.5.1:
  * record() is now O(1) per sample (was O(window)). The previous
    two-pass `sum(...)` + `sum((v - mean) ** 2 ...)` recomputed the mean
    and variance from scratch on every call — with window=2000 and 100
    msg/s that is ~200k pure-Python iterations/s just for metrics. The
    running `_sum` / `_sum_sq` now update in O(1) with each append/evict.
  * Variance = E[x²] − E[x]² is subject to catastrophic cancellation for
    extremely tight distributions; the result is clamped to ≥0 before the
    sqrt() to absorb float noise, and every 10,000 records the running
    sums are resynced from the live window so drift over the lifetime of
    a long-lived process cannot accumulate. For realistic latency
    distributions (mean≫std over thousands of samples) the extra digits
    of precision lost are irrelevant.
  * No API change: record() / snapshot() signatures and return shapes are
    identical to the previous version.
"""

from __future__ import annotations

import asyncio
import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional


@dataclass(slots=True)
class AnomalyReport:
    """Result of recording one latency sample."""

    is_anomaly: bool
    value: float
    mean: float
    std: float
    z_score: float
    sample_size: int


class BoundedMetrics:
    """Rolling-window latency tracker with z-score anomaly detection.

    `record()` and `snapshot()` are async, guarded by an internal
    `asyncio.Lock`, so it's safe to call concurrently from multiple
    in-flight `analyze()` calls without corrupting the window.
    """

    # Minimum samples required before the mean/std are trusted enough to
    # flag anomalies — avoids false positives while the window is still
    # warming up (e.g. right after process start, or right after a
    # keyword/cache reload resets timing behavior).
    _MIN_SAMPLES_FOR_DETECTION = 30

    # v14.5.1: how often to rebuild _sum / _sum_sq from the live window to
    # keep floating-point drift bounded. Every ~few minutes at typical
    # message rates; the resync itself is O(window) and happens once.
    _RESYNC_EVERY = 10_000

    def __init__(self, window: int = 2000, z_threshold: float = 3.5, enabled: bool = True) -> None:
        _w = max(1, int(window))
        self._window: Deque[float] = deque(maxlen=_w)
        self._z_threshold = z_threshold
        self._enabled = enabled
        self._lock = asyncio.Lock()
        # v14.5.1: incremental statistics. _sum / _sum_sq track the live
        # window contents so record() is O(1) instead of O(window).
        self._sum: float = 0.0
        self._sum_sq: float = 0.0
        self._records_since_resync: int = 0

    async def record(self, value_ms: float) -> Optional[AnomalyReport]:
        """Record one latency sample and return an AnomalyReport.

        Returns None only when metrics are disabled (`enabled=False`).
        Otherwise always returns a report; `is_anomaly` stays False until
        at least `_MIN_SAMPLES_FOR_DETECTION` samples have been collected.
        """
        if not self._enabled:
            return None

        async with self._lock:
            n = len(self._window)
            is_anomaly = False
            mean = value_ms
            std = 0.0
            z = 0.0

            if n >= self._MIN_SAMPLES_FOR_DETECTION:
                mean = self._sum / n
                # Variance = E[x²] − E[x]². Numeric error can make a tiny
                # variance slightly negative; clamp at 0 before sqrt().
                var = (self._sum_sq / n) - (mean * mean)
                if var > 0.0:
                    std = math.sqrt(var)
                    z = (value_ms - mean) / std
                    is_anomaly = abs(z) >= self._z_threshold

            # Evict oldest BEFORE appending so the running sums stay in
            # sync with the window contents (deque with maxlen would
            # auto-evict on append, but we need to subtract the evicted
            # value from _sum/_sum_sq first).
            if self._window.maxlen is not None and len(self._window) == self._window.maxlen:
                oldest = self._window[0]
                self._sum -= oldest
                self._sum_sq -= oldest * oldest

            self._window.append(value_ms)
            self._sum += value_ms
            self._sum_sq += value_ms * value_ms

            # Periodic resync to absorb float drift.
            self._records_since_resync += 1
            if self._records_since_resync >= self._RESYNC_EVERY:
                self._sum = sum(self._window)
                self._sum_sq = sum(v * v for v in self._window)
                self._records_since_resync = 0

            return AnomalyReport(
                is_anomaly=is_anomaly,
                value=value_ms,
                mean=round(mean, 3),
                std=round(std, 3),
                z_score=round(z, 3),
                sample_size=n,
            )

    async def snapshot(self) -> Dict[str, float]:
        """Return current p50/p95/p99/mean/count over the rolling window."""
        async with self._lock:
            samples = sorted(self._window)
            running_mean = (self._sum / len(self._window)) if self._window else 0.0

        n = len(samples)
        if n == 0:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0, "count": 0}

        def _pct(p: float) -> float:
            idx = min(n - 1, max(0, math.ceil(p / 100 * n) - 1))
            return samples[idx]

        return {
            "p50": round(_pct(50), 3),
            "p95": round(_pct(95), 3),
            "p99": round(_pct(99), 3),
            "mean": round(running_mean, 3),
            "count": n,
        }
