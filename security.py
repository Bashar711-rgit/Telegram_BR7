#!/usr/bin/env python3
"""
security.py — Hardened regex execution utilities.

Provides ReDoS-resistant regex search/findall helpers used across the bot's
NLP pipeline (filter_engine.py v14.2).

Historically this kind of protection was implemented with SIGALRM-based
timeouts, which only work on the main thread of a Unix process and interact
badly with anything else that wants to install signal handlers (asyncio's
own signal handling, or being embedded inside a worker thread). This module
avoids SIGALRM entirely: every regex operation runs in a small bounded
thread pool and is enforced with a wall-clock timeout via
`Future.result(timeout=...)`. If a pattern doesn't return in time we stop
waiting for it (Python cannot force-kill a thread, so the runaway thread is
simply abandoned and left to die on its own) and return a safe default
(`None` for search, `[]` for findall) instead of hanging the caller.

As defense in depth, every input is capped to `MAX_REGEX_INPUT_LEN`
characters before it reaches the regex engine — most catastrophic
backtracking blowups scale with input length, so bounding the length alone
closes off most of the attack surface even before the timeout kicks in.

Public API:
    MAX_REGEX_INPUT_LEN: Final[int]
    SafeRegexExecutor(timeout_s=0.25, max_workers=4)
        .search(pattern, text) -> Optional[re.Match]
        .findall(pattern, text) -> list
        .shutdown() -> None
    safe_search(pattern, text) -> Optional[re.Match]
    safe_findall(pattern, text) -> list
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, Final, List, Optional, Pattern, Union

from loguru import logger

# Hard cap on characters fed to any regex operation guarded by this module.
# Chosen to comfortably cover a single chat message while keeping worst-case
# backtracking cost bounded regardless of pattern shape.
MAX_REGEX_INPUT_LEN: Final[int] = 2000

_PatternLike = Union[str, "re.Pattern[str]"]


def _cap(text: str) -> str:
    if not text:
        return ""
    return text if len(text) <= MAX_REGEX_INPUT_LEN else text[:MAX_REGEX_INPUT_LEN]


class SafeRegexExecutor:
    """Runs regex operations in a bounded thread pool with a wall-clock timeout.

    No SIGALRM: safe to call from any thread, and safe to call from inside
    asyncio code (call it from sync code — it only blocks the calling
    thread, never installs process-wide signal handlers).
    """

    def __init__(self, timeout_s: float = 0.25, max_workers: int = 4) -> None:
        self._timeout_s = timeout_s
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="safe-regex",
        )
        self._state_lock = threading.Lock()
        self._shutdown = False

    def search(self, pattern: _PatternLike, text: str) -> Optional["re.Match[str]"]:
        return self._submit(pattern, text, "search")

    def findall(self, pattern: _PatternLike, text: str) -> List[Any]:
        result = self._submit(pattern, text, "findall")
        return result if result is not None else []

    def _submit(self, pattern: _PatternLike, text: str, op: str) -> Any:
        capped = _cap(text)
        try:
            compiled = pattern if isinstance(pattern, re.Pattern) else re.compile(pattern)
        except re.error as exc:
            logger.warning("safe_regex_compile_error | pattern={} err={}", pattern, exc)
            return None

        func = compiled.search if op == "search" else compiled.findall

        with self._state_lock:
            if self._shutdown:
                logger.debug("SafeRegexExecutor used after shutdown; skipping")
                return None
            try:
                future = self._executor.submit(func, capped)
            except RuntimeError:
                # Executor is mid-shutdown under a race — fail safe.
                return None

        try:
            return future.result(timeout=self._timeout_s)
        except FutureTimeoutError:
            logger.warning(
                "safe_regex_timeout | pattern={} text_len={} timeout_s={}",
                getattr(compiled, "pattern", pattern), len(capped), self._timeout_s,
            )
            return None
        except Exception as exc:  # noqa: BLE001 — must never propagate into the caller's hot path
            logger.debug("safe_regex_error | pattern={} err={}", pattern, exc)
            return None

    def shutdown(self) -> None:
        """Release the thread pool. Safe to call more than once."""
        with self._state_lock:
            if self._shutdown:
                return
            self._shutdown = True
        self._executor.shutdown(wait=False, cancel_futures=True)


# Module-level default instance backing the safe_search()/safe_findall()
# convenience functions used throughout filter_engine.py. Kept separate from
# any instance a caller constructs directly (e.g. EnhancedFilter's own
# `self._regex_guard`) so per-component tuning never affects this shared
# default, and so importing this module has no side effect until it's
# actually used (lazy singleton).
_default_executor: Optional[SafeRegexExecutor] = None
_default_lock = threading.Lock()


def _get_default_executor() -> SafeRegexExecutor:
    global _default_executor
    if _default_executor is None:
        with _default_lock:
            if _default_executor is None:
                _default_executor = SafeRegexExecutor()
    return _default_executor


def safe_search(pattern: _PatternLike, text: str) -> Optional["re.Match[str]"]:
    """Drop-in, ReDoS-guarded replacement for re.search() / pattern.search()."""
    return _get_default_executor().search(pattern, text)


def safe_findall(pattern: _PatternLike, text: str) -> List[Any]:
    """Drop-in, ReDoS-guarded replacement for re.findall() / pattern.findall()."""
    return _get_default_executor().findall(pattern, text)
