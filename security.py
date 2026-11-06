#!/usr/bin/env python3
"""
security.py — v14.5.1 (HARDENED, aligned with config/filter_engine/database v14.5.1)

Public API (unchanged, 100% backward compatible with filter_engine.py):
    MAX_REGEX_INPUT_LEN
    SafeRegexExecutor(timeout_s=0.25, max_workers=4)
        .search / .findall / .search_async / .findall_async / .shutdown
    safe_search / safe_findall / safe_search_async / safe_findall_async
    parse_trusted_proxies / trusted_proxy_networks / resolve_client_ip

v14.5.1 changes:
  * resolve_client_ip: `direct` is coerced to str via `str(...)` BEFORE
    `.strip()`, so passing None / int / arbitrary objects can no longer
    raise AttributeError out of the function. Non-string non-empty peers
    become their str() form; empty/falsy peers become "unknown". This was
    the only latent bug in the previous version.
  * The shared default executor reads CFG.REGEX_TIMEOUT_S and
    CFG.REGEX_GUARD_WORKERS (env fallback, then 0.25 / 4). Previously these
    config fields (added in config.py v14.5.1) were ignored here.
  * Optional real timeouts: if the third-party `regex` module is installed,
    patterns are transparently recompiled with it and called with
    `timeout=`. CPython's `re` holds the GIL while matching, so a thread +
    Future timeout can NOT interrupt a catastrophic pattern; `regex` can.
    Without `regex`, behavior is the legacy thread-pool guard (length cap
    stays the primary defense). Disable: SAFE_REGEX_USE_REGEX_MODULE=0.
  * Pool-poisoning protection: abandoned (timed-out) worker threads used to
    permanently occupy the pool until every later call timed out silently
    (spam/URL/phone detection quietly off). After `max_workers` consecutive
    timeouts the pool is now rotated to a fresh one.
  * Compiled-pattern cache; sync/async paths share one implementation.
  * Guard statistics: get_regex_guard_stats() for telemetry.
  * shutdown_default_executor() (+ atexit) — the shared default pool was
    never released before.
  * resolve_client_ip hardening: IPv4-mapped IPv6 (::ffff:a.b.c.d),
    "ip:port" and "[v6]:port" hops, zone ids; a non-IP hop is never
    returned as the client (falls back to the direct peer); default trusted
    proxies also cover 172.16/12, 192.168/16 and fc00::/7 (Docker/private
    networks). TRUSTED_PROXIES still overrides the default entirely.
  * New optional helpers for secrets/log hygiene (no changes needed in other
    files): redact_url, redact_dsn, mask_phone, constant_time_equals,
    sanitize_for_log.
"""

from __future__ import annotations

import asyncio
import atexit
import hmac
import ipaddress as _ipaddress
import os as _os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, Callable, Dict, Final, List, Optional, Tuple, Union
from urllib.parse import parse_qsl, urlparse

from loguru import logger

try:  # optional: real, interruptible regex timeouts
    import regex as _regex  # type: ignore
except Exception:  # noqa: BLE001
    _regex = None

MAX_REGEX_INPUT_LEN: Final[int] = 2000

_PatternLike = Union[str, "re.Pattern[str]"]

# ─────────────────────────────────────────────────────────────────────────────
# Stats
# ─────────────────────────────────────────────────────────────────────────────
_stats_lock = threading.Lock()
_stats: Dict[str, int] = {
    "calls": 0,
    "timeouts": 0,
    "errors": 0,
    "compile_errors": 0,
    "pool_rotations": 0,
    "regex_module_calls": 0,
}


def _inc(key: str, n: int = 1) -> None:
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n


def get_regex_guard_stats() -> Dict[str, Any]:
    with _stats_lock:
        out: Dict[str, Any] = dict(_stats)
    out["regex_module_available"] = _regex is not None
    return out


def _cap(text: str) -> str:
    if not text:
        return ""
    return text if len(text) <= MAX_REGEX_INPUT_LEN else text[:MAX_REGEX_INPUT_LEN]


def _cfg_value(name: str, default: Any, cast: Callable[[Any], Any]) -> Any:
    """CFG.<name> → env var → default. Never raises."""
    try:
        from config import CFG  # lazy: keeps this module importable standalone

        value = getattr(CFG, name, None)
        if value is not None:
            return cast(value)
    except Exception:  # noqa: BLE001
        pass
    raw = _os.getenv(name)
    if raw:
        try:
            return cast(raw)
        except Exception:  # noqa: BLE001
            pass
    return default


def _env_flag(name: str, default: bool) -> bool:
    raw = _os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ─────────────────────────────────────────────────────────────────────────────
# SafeRegexExecutor
# ─────────────────────────────────────────────────────────────────────────────
class SafeRegexExecutor:
    """Bounded, timeout-enforced regex execution (no SIGALRM).

    Safe from any thread. On timeout/error returns None (search) or []
    (findall) — never raises into the caller's hot path.
    """

    _GRACE_S: Final[float] = 0.05          # lets regex's own timeout fire first
    _COMPILE_CACHE_MAX: Final[int] = 256

    def __init__(
        self,
        timeout_s: float = 0.25,
        max_workers: int = 4,
        use_regex_module: Optional[bool] = None,
    ) -> None:
        self._timeout_s = max(0.01, float(timeout_s))
        self._max_workers = max(1, int(max_workers))
        if use_regex_module is None:
            use_regex_module = _env_flag("SAFE_REGEX_USE_REGEX_MODULE", True)
        self._use_regex = bool(use_regex_module) and _regex is not None
        self._state_lock = threading.Lock()
        self._shutdown = False
        self._consecutive_timeouts = 0
        self._compiled: Dict[Any, Tuple[Any, bool]] = {}
        self._executor = self._new_pool()

    # ── internals ───────────────────────────────────────────────────────
    def _new_pool(self) -> ThreadPoolExecutor:
        return ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix="safe-regex"
        )

    def _compile(self, pattern: _PatternLike) -> Optional[Tuple[Any, bool]]:
        key: Any = pattern if isinstance(pattern, str) else (pattern.pattern, pattern.flags)
        hit = self._compiled.get(key)
        if hit is not None:
            return hit
        try:
            base = pattern if isinstance(pattern, re.Pattern) else re.compile(pattern)
        except re.error as exc:
            _inc("compile_errors")
            logger.warning("safe_regex_compile_error | pattern={} err={}", pattern, exc)
            return None
        entry: Tuple[Any, bool] = (base, False)
        if self._use_regex:
            try:
                entry = (_regex.compile(base.pattern, base.flags), True)
            except Exception:  # noqa: BLE001 — unsupported syntax → stay on `re`
                entry = (base, False)
        if len(self._compiled) >= self._COMPILE_CACHE_MAX:
            self._compiled.clear()
        self._compiled[key] = entry
        return entry

    def _make_call(self, compiled: Any, is_regex: bool, op: str, text: str) -> Callable[[], Any]:
        fn = getattr(compiled, op)
        if is_regex:
            timeout = self._timeout_s
            return lambda: fn(text, timeout=timeout)
        return lambda: fn(text)

    def _submit_future(self, call: Callable[[], Any]):
        with self._state_lock:
            if self._shutdown:
                logger.debug("SafeRegexExecutor used after shutdown; skipping")
                return None
            try:
                return self._executor.submit(call)
            except RuntimeError:
                return None

    def _note_ok(self) -> None:
        with self._state_lock:
            self._consecutive_timeouts = 0

    def _note_timeout(self, compiled: Any, text_len: int, is_regex: bool) -> None:
        _inc("timeouts")
        logger.warning(
            "safe_regex_timeout | pattern={} text_len={} timeout_s={}",
            getattr(compiled, "pattern", "?"), text_len, self._timeout_s,
        )
        if is_regex:
            return  # the `regex` module aborts the match itself: no thread leaks
        old = None
        with self._state_lock:
            self._consecutive_timeouts += 1
            if self._consecutive_timeouts >= self._max_workers and not self._shutdown:
                old = self._executor
                self._executor = self._new_pool()
                self._consecutive_timeouts = 0
        if old is not None:
            _inc("pool_rotations")
            logger.error(
                "safe_regex: {} consecutive timeouts — worker pool presumed "
                "poisoned by runaway patterns; rotating to a fresh pool",
                self._max_workers,
            )
            old.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _default(op: str) -> Any:
        return None if op == "search" else []

    def _run_sync(self, pattern: _PatternLike, text: str, op: str) -> Any:
        _inc("calls")
        capped = _cap(text)
        entry = self._compile(pattern)
        if entry is None:
            return self._default(op)
        compiled, is_regex = entry
        call = self._make_call(compiled, is_regex, op, capped)

        if is_regex:
            _inc("regex_module_calls")
            try:
                res = call()
                return res if res is not None or op == "search" else []
            except TimeoutError:
                self._note_timeout(compiled, len(capped), True)
            except Exception as exc:  # noqa: BLE001
                _inc("errors")
                logger.debug("safe_regex_error | pattern={} err={}", pattern, exc)
            return self._default(op)

        future = self._submit_future(call)
        if future is None:
            return self._default(op)
        try:
            res = future.result(timeout=self._timeout_s)
            self._note_ok()
            return res if res is not None or op == "search" else []
        except FutureTimeoutError:
            self._note_timeout(compiled, len(capped), False)
        except Exception as exc:  # noqa: BLE001
            _inc("errors")
            logger.debug("safe_regex_error | pattern={} err={}", pattern, exc)
        return self._default(op)

    async def _run_async(self, pattern: _PatternLike, text: str, op: str) -> Any:
        _inc("calls")
        capped = _cap(text)
        entry = self._compile(pattern)
        if entry is None:
            return self._default(op)
        compiled, is_regex = entry
        if is_regex:
            _inc("regex_module_calls")
        call = self._make_call(compiled, is_regex, op, capped)

        future = self._submit_future(call)
        if future is None:
            return self._default(op)

        loop = asyncio.get_running_loop()
        wait = self._timeout_s + (self._GRACE_S if is_regex else 0.0)
        try:
            res = await asyncio.wait_for(asyncio.wrap_future(future, loop=loop), timeout=wait)
            if not is_regex:
                self._note_ok()
            return res if res is not None or op == "search" else []
        except (asyncio.TimeoutError, TimeoutError):
            self._note_timeout(compiled, len(capped), is_regex)
        except Exception as exc:  # noqa: BLE001
            _inc("errors")
            logger.debug("safe_regex_error_async | pattern={} err={}", pattern, exc)
        return self._default(op)

    # ── public API ──────────────────────────────────────────────────────
    def search(self, pattern: _PatternLike, text: str) -> Optional["re.Match[str]"]:
        return self._run_sync(pattern, text, "search")

    def findall(self, pattern: _PatternLike, text: str) -> List[Any]:
        return self._run_sync(pattern, text, "findall")

    async def search_async(self, pattern: _PatternLike, text: str) -> Optional["re.Match[str]"]:
        return await self._run_async(pattern, text, "search")

    async def findall_async(self, pattern: _PatternLike, text: str) -> List[Any]:
        return await self._run_async(pattern, text, "findall")

    def shutdown(self) -> None:
        """Release the thread pool. Safe to call more than once."""
        with self._state_lock:
            if self._shutdown:
                return
            self._shutdown = True
            executor = self._executor
        executor.shutdown(wait=False, cancel_futures=True)


# ─────────────────────────────────────────────────────────────────────────────
# Shared default executor (lazy singleton) — tuned from CFG
# ─────────────────────────────────────────────────────────────────────────────
_default_executor: Optional[SafeRegexExecutor] = None
_default_lock = threading.Lock()


def _get_default_executor() -> SafeRegexExecutor:
    global _default_executor
    if _default_executor is None:
        with _default_lock:
            if _default_executor is None:
                _default_executor = SafeRegexExecutor(
                    timeout_s=_cfg_value("REGEX_TIMEOUT_S", 0.25, float),
                    max_workers=_cfg_value("REGEX_GUARD_WORKERS", 4, int),
                )
    return _default_executor


def shutdown_default_executor() -> None:
    global _default_executor
    with _default_lock:
        ex, _default_executor = _default_executor, None
    if ex is not None:
        ex.shutdown()


atexit.register(shutdown_default_executor)


def safe_search(pattern: _PatternLike, text: str) -> Optional["re.Match[str]"]:
    """Sync variant. From async code prefer safe_search_async."""
    return _get_default_executor().search(pattern, text)


def safe_findall(pattern: _PatternLike, text: str) -> List[Any]:
    """Sync variant. From async code prefer safe_findall_async."""
    return _get_default_executor().findall(pattern, text)


async def safe_search_async(pattern: _PatternLike, text: str) -> Optional["re.Match[str]"]:
    return await _get_default_executor().search_async(pattern, text)


async def safe_findall_async(pattern: _PatternLike, text: str) -> List[Any]:
    return await _get_default_executor().findall_async(pattern, text)


# ─────────────────────────────────────────────────────────────────────────────
# Client IP resolution behind trusted reverse proxies
#   1. Direct peer not trusted → X-Forwarded-For is client-controlled → ignored.
#   2. Trusted peer → walk XFF right-to-left, skipping trusted hops; the first
#      untrusted VALID IP is the client.
#   3. Empty / all-trusted / invalid hop → direct peer (cannot be spoofed).
# ─────────────────────────────────────────────────────────────────────────────
_PROXY_CACHE_TTL: Final[float] = 60.0
_proxy_cache_lock = threading.Lock()
_proxy_cache: tuple = (0.0, ())

_DEFAULT_TRUSTED_PROXIES: Final[str] = (
    "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,::1/128,fc00::/7"
)
_XFF_KEY_MAX_LEN: Final[int] = 64


def parse_trusted_proxies(raw: str) -> tuple:
    """Comma-separated CIDRs → networks. Invalid entries are skipped safely."""
    nets = []
    for piece in (raw or "").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            nets.append(_ipaddress.ip_network(piece, strict=False))
        except ValueError:
            logger.warning(f"trusted proxies: invalid CIDR skipped: {piece!r}")
    return tuple(nets)


def trusted_proxy_networks() -> tuple:
    """Trusted proxy networks from TRUSTED_PROXIES or the default (60 s cache)."""
    global _proxy_cache
    now = time.time()
    with _proxy_cache_lock:
        cached_ts, cached = _proxy_cache
        if cached_ts > 0 and (now - cached_ts) < _PROXY_CACHE_TTL:
            return cached
    raw = _os.getenv("TRUSTED_PROXIES", "").strip() or _DEFAULT_TRUSTED_PROXIES
    nets = parse_trusted_proxies(raw)
    with _proxy_cache_lock:
        _proxy_cache = (time.time(), nets)
    return nets


def _normalize_ip(raw: str) -> Optional[str]:
    """Canonical IP string, or None if `raw` is not a valid IP.

    Accepts "1.2.3.4", "1.2.3.4:5678", "[::1]:80", "fe80::1%eth0" and
    IPv4-mapped IPv6 (returned as plain IPv4).
    """
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("["):
        end = s.find("]")
        if end == -1:
            return None
        s = s[1:end]
    elif s.count(":") == 1 and "." in s:
        s = s.split(":", 1)[0]
    s = s.split("%", 1)[0]
    try:
        addr = _ipaddress.ip_address(s)
    except ValueError:
        return None
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return str(addr)


def _ip_in_trusted(ip: str, nets: tuple) -> bool:
    if not ip or not nets:
        return False
    try:
        addr = _ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in nets if net.version == addr.version)


def resolve_client_ip(direct: str, xff: str) -> str:
    """Real client IP behind trusted proxies (see block comment above).

    v14.5.1: `direct` is coerced to str BEFORE .strip() so that passing
    None / int / an arbitrary object cannot raise AttributeError out of
    the function. Falsy peers become "unknown"; non-empty non-str peers
    become their str() form (then _normalize_ip will reject them, and the
    caller receives the truncated literal as a best-effort fallback).
    """
    fallback = (str(direct) if direct else "unknown").strip() or "unknown"
    try:
        direct_ip = _normalize_ip(fallback)
        if direct_ip is None:
            return fallback[:_XFF_KEY_MAX_LEN]
        nets = trusted_proxy_networks()
        if not _ip_in_trusted(direct_ip, nets):
            return direct_ip[:_XFF_KEY_MAX_LEN]
        parts = [p.strip() for p in (xff or "").split(",") if p.strip()]
        for hop in reversed(parts):
            hop_ip = _normalize_ip(hop)
            if hop_ip is None:
                return direct_ip[:_XFF_KEY_MAX_LEN]  # junk hop: never trust it
            if not _ip_in_trusted(hop_ip, nets):
                return hop_ip[:_XFF_KEY_MAX_LEN]
        return direct_ip[:_XFF_KEY_MAX_LEN]
    except Exception:  # noqa: BLE001 — fail safe: the direct peer can't be forged
        return fallback[:_XFF_KEY_MAX_LEN]


# ─────────────────────────────────────────────────────────────────────────────
# Secret / log hygiene helpers (optional; usable from database.py/config.py)
#   e.g. database.py: logger.info(f"... {redact_url(_BACKUP_UPLOAD_URL)}")
#   instead of _BACKUP_UPLOAD_URL[:40], which can leak tokens in the URL.
# ─────────────────────────────────────────────────────────────────────────────
_CTRL_CHARS_RE: Final = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]+")


def redact_url(url: Optional[str], max_len: int = 120) -> str:
    """scheme://host[:port]/path with credentials and query values masked."""
    if not url:
        return ""
    try:
        p = urlparse(str(url).strip())
        host = p.hostname or ""
        netloc = f"{host}:{p.port}" if p.port else host
        out = f"{p.scheme}://{netloc}{p.path}" if p.scheme else (netloc + p.path)
        if p.query:
            keys = [k for k, _ in parse_qsl(p.query, keep_blank_values=True)]
            out += "?" + "&".join(f"{k}=***" for k in keys)
        return out[:max_len]
    except Exception:  # noqa: BLE001
        return "<unparseable-url>"


def redact_dsn(dsn: Optional[str], max_len: int = 120) -> str:
    """Database URL with user:password removed (alias of redact_url)."""
    return redact_url(dsn, max_len)


def mask_phone(phone: Optional[str]) -> str:
    s = str(phone or "")
    return ("*" * max(0, len(s) - 4)) + s[-4:] if s else ""


def constant_time_equals(a: Optional[str], b: Optional[str]) -> bool:
    """Timing-safe string comparison for tokens/secrets."""
    if a is None or b is None:
        return False
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


def sanitize_for_log(text: Any, max_len: int = 200) -> str:
    """Strip control chars/newlines (log-forging) and truncate untrusted text."""
    s = _CTRL_CHARS_RE.sub(" ", str(text if text is not None else ""))
    return s if len(s) <= max_len else s[: max_len - 1] + "…"
