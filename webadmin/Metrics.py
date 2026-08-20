#!/usr/bin/env python3
"""
metrics.py — Bounded latency metrics with anomaly detection.

Tracks a rolling window of latency samples (milliseconds) for
EnhancedFilter.analyze() and exposes:

  - percentile snapshots (p50/p95/p99) for telemetry/dashboards
    (`get_telemetry()` in filter_engine.py reads these).
  - lightweight z-score based anomaly detection on each new sample, so a
    single unusually slow analyze() call can be logged and surfaced without
    needing an external metrics backend (Prometheus/Datadog/etc).

The window is bounded — a `collections.deque(maxlen=window)` — so memory
use and per-sample computation cost never grow with uptime. That boundedness
is the whole point of this module existing instead of reaching for a
full time-series library for what is, in practice, a single hot counter.
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

    def __init__(self, window: int = 2000, z_threshold: float = 3.5, enabled: bool = True) -> None:
        self._window: Deque[float] = deque(maxlen=max(1, window))
        self._z_threshold = z_threshold
        self._enabled = enabled
        self._lock = asyncio.Lock()

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
                mean = sum(self._window) / n
                variance = sum((v - mean) ** 2 for v in self._window) / n
                std = math.sqrt(variance)
                if std > 0:
                    z = (value_ms - mean) / std
                    is_anomaly = abs(z) >= self._z_threshold

            self._window.append(value_ms)

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
            "mean": round(sum(samples) / n, 3),
            "count": n,
        }
