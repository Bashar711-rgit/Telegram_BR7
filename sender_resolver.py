#!/usr/bin/env python3
"""
sender_resolver.py — Sender Intelligence / Sender Contact Resolver v1.0.0
==========================================================================

طبقة استخلاص وتطبيع وحفظ وحلّ بيانات المرسل (Backend ONLY).

CORE PRINCIPLE — capture at arrival:
    event → event.sender → extract (SYNC, never raises) → persist →
    continue processing. The message can be deleted milliseconds later;
    whatever was extracted at arrival is safe.

Scope guard (requirement #48 of the engineering brief):
    This layer provides *maximum reliable access within the Telegram
    account's authorized access and Telethon/API capabilities*. It never
    bypasses privacy, never guesses access_hash values, and never scrapes.
    phone is stored ONLY when Telegram itself includes it on the User
    entity (legal availability) — it is never fetched or inferred.

What lives here (and what deliberately does NOT):
    * extract() / extract_flat()  — pure-sync, getattr-safe User extraction
    * parse_telegram_link()       — t.me / telegram.me / @user / invite parser
    * SenderResolver              — entity/message resolution with:
        - access-method priority (entity → InputPeerUser → username →
          dialog/entity cache → DB fallback → resolve on demand)
        - bounded entity cache (TTL)
        - in-flight deduplication (N events for one sender → 1 API call)
        - exponential backoff + jitter for TRANSIENT errors only
        - FloodWaitError honored (exact Telegram seconds; fail-fast +
          cooldown instead of blocking the pipeline)
        - failure-reason enum for logs/DB only (never surfaced in alerts)
        - bounded metrics (counters + average resolve latency)
    * get_sender_intel_snapshot() — /health diagnostics block

What does NOT live here:
    * Any alert formatting. The existing _build_alert() output is a
      frozen contract; this module only feeds it richer data.
    * Any CircuitBreaker replacement. The existing monitors.CircuitBreaker
      instances stay untouched; resolver failures are internal (caught,
      translated to reasons) so they never poison the existing breakers
      with permanent (non-transient) errors.

Failure reasons are internal diagnostics (requirement #17): they are
logged and stored, but never rendered into user-facing alerts.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from cachetools import TTLCache
from loguru import logger
from telethon import TelegramClient
from telethon.errors import (
    AuthKeyError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    RPCError,
    UserNotParticipantError,
)
from telethon.tl.types import InputPeerUser

from config import CFG

__version__ = "1.0.0"


# =============================================================================
# Failure reasons — internal diagnostics only (logs / DB / metrics)
# =============================================================================
class FailureReason(str, Enum):
    """Precise internal failure reasons. NEVER shown inside alerts."""

    PRIVATE_CHAT_INACCESSIBLE = "PRIVATE_CHAT_INACCESSIBLE"
    USER_NOT_RESOLVABLE = "USER_NOT_RESOLVABLE"
    MESSAGE_NOT_FOUND = "MESSAGE_NOT_FOUND"
    MESSAGE_DELETED = "MESSAGE_DELETED"
    CHAT_NOT_FOUND = "CHAT_NOT_FOUND"
    INVALID_LINK = "INVALID_LINK"
    ACCESS_DENIED = "ACCESS_DENIED"
    FLOOD_WAIT = "FLOOD_WAIT"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    RPC_ERROR = "RPC_ERROR"
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    SESSION_ERROR = "SESSION_ERROR"
    RESTRICTED_CONTENT = "RESTRICTED_CONTENT"


# Transient error type names (string match to avoid importing every
# Telethon exception class): retried with backoff. Everything else is
# permanent for this request and returns a precise failure reason.
_TRANSIENT_HINTS = (
    "TIMEOUT", "TIMEDOUT", "CONNECTION", "NETWORK", "SERVERERROR",
    "SERVICEUNAVAILABLE", "INTERNAL", "SEEOTHER", "CALLFAIL",
)


def classify_telethon_error(exc: Exception) -> FailureReason:
    """Map a Telethon exception to an internal FailureReason."""
    if isinstance(exc, FloodWaitError):
        return FailureReason.FLOOD_WAIT
    if isinstance(exc, (AuthKeyError, PermissionError)):
        return FailureReason.SESSION_ERROR
    if isinstance(exc, (ChannelPrivateError, ChatAdminRequiredError, UserNotParticipantError)):
        return FailureReason.PRIVATE_CHAT_INACCESSIBLE
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return FailureReason.NETWORK_TIMEOUT
    if isinstance(exc, ValueError):
        return FailureReason.USER_NOT_RESOLVABLE
    if isinstance(exc, RPCError):
        name = type(exc).__name__.upper()
        msg = str(exc).upper()
        if any(h in name or h in msg for h in _TRANSIENT_HINTS):
            return FailureReason.NETWORK_TIMEOUT
        return FailureReason.RPC_ERROR
    return FailureReason.RPC_ERROR


# =============================================================================
# Safe extraction — pure sync, never raises, additive-safe
# =============================================================================
_STATUS_LABELS = {
    "UserStatusOnline": "online",
    "UserStatusOffline": "offline",
    "UserStatusRecently": "recently",
    "UserStatusLastWeek": "within_week",
    "UserStatusLastMonth": "within_month",
    "UserStatusLongAgo": "long_time_ago",
}


def _status_label(status: Any) -> Optional[str]:
    if status is None:
        return None
    cls = type(status).__name__
    label = _STATUS_LABELS.get(cls)
    if label == "online":
        expires = getattr(status, "expires", None)
        if isinstance(expires, int) and expires > 0:
            label = f"online_until_{expires}"
    if label == "offline":
        was = getattr(status, "was_online", None)
        if isinstance(was, int) and was > 0:
            label = f"offline_since_{was}"
    return label


def _clean_username(value: Any) -> Optional[str]:
    """Normalize a Telegram username: strip @, drop empties/long-ids.

    Some entity shapes expose numeric "username" placeholders for
    inaccessible users — those are not real usernames."""
    if not isinstance(value, str):
        return None
    clean = value.strip().lstrip("@")
    if not clean or len(clean) > 64:
        return None
    if clean.isdigit():
        return None
    return clean


def extract_sender(entity: Any) -> Dict[str, Any]:
    """
    Extract every safely-available field from a Telethon User entity.

    * Pure getattr — a missing attribute NEVER fails message processing.
    * phone: only when Telegram itself put it on the entity (legal
      availability). Otherwise None. Privacy is never bypassed.
    * Returned dict is small and JSON-serializable (safe to keep inside
      the FastCaptureBuffer entry or persist as a DB row).
    """
    if entity is None:
        return {}
    try:
        username = _clean_username(getattr(entity, "username", None))
        usernames: Optional[List[str]] = None
        raw_usernames = getattr(entity, "usernames", None)
        if raw_usernames:
            try:
                usernames = [
                    u.username if hasattr(u, "username") else str(u)
                    for u in raw_usernames
                    if (u.username if hasattr(u, "username") else str(u))
                ] or None
            except Exception:
                usernames = None
        access_hash = getattr(entity, "access_hash", None)
        if isinstance(access_hash, int):
            # Keep as-is (BIGINT-safe); sanitize only non-int garbage.
            pass
        else:
            access_hash = None
        photo_available = None
        photo = getattr(entity, "photo", None)
        if photo is not None:
            photo_available = True
        elif photo is None and hasattr(entity, "photo"):
            photo_available = False
        bot_info_version = getattr(entity, "bot_info_version", None)

        return {
            "sender_id": getattr(entity, "id", None),
            "access_hash": access_hash,
            "username": username,
            "usernames": usernames,
            "first_name": getattr(entity, "first_name", None),
            "last_name": getattr(entity, "last_name", None),
            "phone": getattr(entity, "phone", None),  # only if Telegram provides it
            "is_bot": bool(getattr(entity, "bot", False) or False),
            "is_verified": bool(getattr(entity, "verified", False) or False),
            "is_premium": bool(getattr(entity, "premium", False) or False),
            "is_scam": bool(getattr(entity, "scam", False) or False),
            "is_fake": bool(getattr(entity, "fake", False) or False),
            "is_restricted": bool(getattr(entity, "restricted", False) or False),
            "restriction_reason": getattr(entity, "restriction_reason", None) or None,
            "status": _status_label(getattr(entity, "status", None)),
            "lang_code": getattr(entity, "lang_code", None) or None,
            "is_deleted": bool(getattr(entity, "deleted", False) or False),
            "photo_available": photo_available,
            "is_contact": bool(getattr(entity, "contact", False) or False),
            "is_mutual_contact": bool(getattr(entity, "mutual_contact", False) or False),
            "is_min": bool(getattr(entity, "min", False) or False),
            "bot_info_version": bot_info_version if isinstance(bot_info_version, int) else None,
        }
    except Exception as e:  # extraction must NEVER break the pipeline
        logger.debug(f"sender_intel: extract failed: {type(e).__name__}: {e}")
        return {}


# Flat keys used inside event_data dicts (prefixed sender_*).
_FLAT_META_KEYS = (
    "phone", "is_bot", "is_verified", "is_premium", "is_scam", "is_fake",
    "is_restricted", "restriction_reason", "status", "lang_code",
    "is_deleted", "photo_available", "is_contact", "is_mutual_contact",
    "usernames", "is_min", "bot_info_version",
)


def extract_flat(entity: Any) -> Dict[str, Any]:
    """extract_sender() rendered as additive sender_* keys for event_data."""
    meta = extract_sender(entity)
    if not meta:
        return {}
    flat: Dict[str, Any] = {}
    for key in _FLAT_META_KEYS:
        flat[f"sender_{key}"] = meta.get(key)
    return flat


def meta_to_contact_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """Map event_data sender_* keys → sender_contacts column names.
    Missing keys become None so the DB COALESCE policy keeps the old value."""
    out: Dict[str, Any] = {}
    for key in _FLAT_META_KEYS:
        out[key] = data.get(f"sender_{key}")
    return out


def build_display_name(first_name: Optional[str], last_name: Optional[str], sender_id: int = 0) -> str:
    """Mirror of the existing _send_alert display-name rule (SAME output)."""
    name = f"{first_name or ''} {last_name or ''}".strip()
    return name or f"مستخدم ({sender_id})"


# =============================================================================
# Telegram link parser (requirement #13–#16)
# =============================================================================
@dataclass(frozen=True, slots=True)
class ParsedLink:
    raw: str = ""
    kind: str = "invalid"  # public_chat|public_message|private_chat|private_message|invite|username|invalid
    username: Optional[str] = None
    message_id: Optional[int] = None
    inner_id: Optional[int] = None          # /c/<inner_id> WITHOUT -100 prefix
    chat_id: Optional[int] = None           # resolved -100<inner_id> form
    invite_hash: Optional[str] = None
    reason: Optional[FailureReason] = None


_USERNAME_RE = None  # compiled lazily


def parse_telegram_link(link: str) -> ParsedLink:
    """
    Parse every supported t.me form. Never raises; malformed input yields
    kind='invalid' with FailureReason.INVALID_LINK.

    Supported:
      https://t.me/username                  → public_chat
      https://t.me/username/123              → public_message
      https://t.me/c/123456789               → private_chat
      https://t.me/c/123456789/456           → private_message
      https://telegram.me/username           → public_chat (alias host)
      @username                              → username
      https://t.me/+HASH / t.me/joinchat/HASH → invite
    Tolerated: trailing slash, query string, fragment. NOTE: an invite
    link does NOT imply the session can join — access depends on account
    permissions (checked at resolve time, never bypassed).
    """
    global _USERNAME_RE
    import re
    if _USERNAME_RE is None:
        _USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,63}$")

    if not link or not isinstance(link, str):
        return ParsedLink(raw=str(link or ""), kind="invalid", reason=FailureReason.INVALID_LINK)
    raw = link.strip()
    text = raw
    # strip scheme/host variations → path form
    lowered = text.lower()
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            lowered = text.lower()
            break
    for host in ("telegram.me", "t.me", "telegram.dog"):
        if lowered.startswith(host + "/"):
            text = text[len(host) + 1:]
            break
        if lowered == host:
            text = ""
            break
    # strip query + fragment + trailing slashes
    text = text.split("?", 1)[0].split("#", 1)[0].strip("/")
    if not text:
        return ParsedLink(raw=raw, kind="invalid", reason=FailureReason.INVALID_LINK)

    # @username
    if text.startswith("@"):
        uname = _clean_username(text)
        if uname and _USERNAME_RE.match(uname.lstrip("@")):
            return ParsedLink(raw=raw, kind="username", username=uname.lstrip("@"))
        return ParsedLink(raw=raw, kind="invalid", reason=FailureReason.INVALID_LINK)

    # private /c/ links — the number is the bare inner id, NOT a chat_id;
    # Telegram's internal supergroup form is -100<inner_id>.
    if text.startswith("c/") or text.startswith("+") or text.startswith("joinchat/"):
        parts = [p for p in text.split("/") if p]
        if text.startswith("+") or parts and parts[0] == "joinchat":
            if text.startswith("+"):
                h = text[1:]
            else:
                h = parts[1] if len(parts) > 1 else ""
            if h and re.match(r"^[A-Za-z0-9_-]{10,}$", h):
                return ParsedLink(raw=raw, kind="invite", invite_hash=h)
            return ParsedLink(raw=raw, kind="invalid", reason=FailureReason.INVALID_LINK)
        # c/...
        parts = parts[1:]  # drop "c"
        if not parts or not parts[0].isdigit():
            return ParsedLink(raw=raw, kind="invalid", reason=FailureReason.INVALID_LINK)
        inner = int(parts[0])
        if inner <= 0:
            return ParsedLink(raw=raw, kind="invalid", reason=FailureReason.INVALID_LINK)
        chat_id = int(f"-100{inner}")
        if len(parts) > 1:
            if not parts[1].isdigit():
                return ParsedLink(raw=raw, kind="invalid", reason=FailureReason.INVALID_LINK)
            return ParsedLink(raw=raw, kind="private_message", inner_id=inner,
                              chat_id=chat_id, message_id=int(parts[1]))
        return ParsedLink(raw=raw, kind="private_chat", inner_id=inner, chat_id=chat_id)

    # public username[/message_id]
    parts = [p for p in text.split("/") if p]
    if len(parts) > 2:
        return ParsedLink(raw=raw, kind="invalid", reason=FailureReason.INVALID_LINK)
    uname = _clean_username(parts[0])
    if not uname or not _USERNAME_RE.match(uname):
        return ParsedLink(raw=raw, kind="invalid", reason=FailureReason.INVALID_LINK)
    if len(parts) == 2:
        if not parts[1].isdigit():
            return ParsedLink(raw=raw, kind="invalid", reason=FailureReason.INVALID_LINK)
        return ParsedLink(raw=raw, kind="public_message", username=uname, message_id=int(parts[1]))
    return ParsedLink(raw=raw, kind="public_chat", username=uname)


# =============================================================================
# Retry policy — exponential backoff + jitter (transient errors only)
# =============================================================================
@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter_frac: float = 0.25  # ±25% jitter

    def delay_for(self, attempt: int) -> float:
        """attempt is 0-based: attempt 0 → base, 1 → 2*base, ... capped."""
        raw = min(self.max_delay, self.base_delay * (2 ** max(0, attempt)))
        jitter = raw * self.jitter_frac
        return max(0.0, raw + random.uniform(-jitter, jitter))


@dataclass(slots=True)
class ResolveResult:
    """Internal result envelope — reason is None on success."""
    entity: Any = None
    message: Any = None
    method: Optional[str] = None
    reason: Optional[FailureReason] = None
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.reason is None


# =============================================================================
# SenderResolver
# =============================================================================
class SenderResolver:
    """
    Stateless-ish resolver shared by ALL account monitors (module singleton).

    Guarantees:
      * cache → InputPeerUser(access_hash) → username → DB fallback →
        get_entity(sender_id) → optional bounded dialog scan
      * in-flight dedup: N concurrent resolves for the same (client,
        sender_id) share ONE underlying attempt
      * transient errors retried with exponential backoff + jitter;
        FloodWait is honored with the EXACT Telegram seconds (fail-fast
        plus a resolver-wide cooldown so we never hammer during the window)
      * every outcome is logged with operation/sender/chat/attempt/
        duration/reason — and NEVER with any secret material
    """

    _LATENCY_WINDOW = 2000

    def __init__(
        self,
        entity_cache_size: int = 2000,
        entity_cache_ttl: int = 900,
        retry: Optional[RetryPolicy] = None,
    ):
        self._entity_cache: "TTLCache[int, Any]" = TTLCache(
            maxsize=max(1, int(entity_cache_size)), ttl=max(30, int(entity_cache_ttl))
        )
        self._retry = retry or RetryPolicy(
            max_attempts=getattr(CFG, "SENDER_RESOLVE_MAX_RETRIES", 3),
            base_delay=getattr(CFG, "SENDER_RESOLVE_BACKOFF_BASE", 0.5),
            max_delay=getattr(CFG, "SENDER_RESOLVE_BACKOFF_MAX", 8.0),
        )
        self._inflight: "OrderedDict[Tuple[int, int], asyncio.Future]" = OrderedDict()
        self._metrics: Dict[str, int] = defaultdict(int)
        self._latencies: Deque[float] = deque(maxlen=self._LATENCY_WINDOW)
        self._flood_wait_until: float = 0.0  # wall-clock

    # ── sync capture helpers ──────────────────────────────────────────────
    extract = staticmethod(extract_sender)
    extract_flat = staticmethod(extract_flat)
    build_display_name = staticmethod(build_display_name)

    # ── access-method plumbing (requirement #6 / #31) ─────────────────────
    @staticmethod
    def build_input_peer(sender_id: Optional[int], access_hash: Optional[int]) -> Optional[InputPeerUser]:
        """InputPeerUser only when BOTH id and access_hash are usable ints.
        Never guesses an access_hash (requirement #46)."""
        try:
            if not sender_id or access_hash is None:
                return None
            return InputPeerUser(user_id=int(sender_id), access_hash=int(access_hash))
        except Exception:
            return None

    @staticmethod
    def build_profile_link(sender_id: Optional[int], username: Optional[str]) -> str:
        """API identity ≠ clickable URL (requirement #30): username wins;
        tg://user?id= is the generic fallback. SAME shapes the existing
        alert builder already uses."""
        if username:
            return f"https://t.me/{username.lstrip('@')}"
        return f"tg://user?id={sender_id or 0}"

    @staticmethod
    def build_message_link(chat_id: Optional[int], message_id: int, username: Optional[str] = None) -> Dict[str, str]:
        """Mirror of monitors.build_telegram_links (identical output shapes)."""
        links = {"group": "#", "message": "#"}
        if username:
            clean = username.lstrip("@")
            links["group"] = f"https://t.me/{clean}"
            links["message"] = f"https://t.me/{clean}/{message_id}"
            return links
        if str(chat_id or "").startswith("-100"):
            inner = str(chat_id)[4:]
            links["group"] = f"https://t.me/c/{inner}"
            links["message"] = f"https://t.me/c/{inner}/{message_id}"
        return links

    @staticmethod
    def build_contact_methods(data: Dict[str, Any]) -> Dict[str, bool]:
        """Internal capability flags — the alert builder decides what to
        render; this only reports what the backend KNOWS (requirement #31)."""
        return {
            "has_username": bool(data.get("sender_username") or data.get("username")),
            "has_entity": bool(data.get("_entity")),
            "has_access_hash": data.get("sender_access_hash") is not None or data.get("access_hash") is not None,
            "has_input_peer": (
                SenderResolver.build_input_peer(
                    data.get("sender_id"), data.get("sender_access_hash", data.get("access_hash"))
                )
                is not None
            ),
            "has_dialog": bool(data.get("_in_dialogs")),
            "has_message_context": bool(data.get("chat_id") and data.get("message_id")),
        }

    @classmethod
    def get_best_access_method(cls, data: Dict[str, Any]) -> str:
        flags = cls.build_contact_methods(data)
        for name in ("has_entity", "has_input_peer", "has_username", "has_dialog", "has_message_context", "has_access_hash"):
            if flags.get(name):
                return name
        return "none"

    # ── metrics ───────────────────────────────────────────────────────────
    def _inc(self, name: str) -> None:
        self._metrics[name] += 1

    def _time(self, op: str, sender_id: Optional[int], chat_id: Optional[int],
              message_id: Optional[int], attempt: int, started: float,
              result: str, reason: Optional[FailureReason] = None) -> None:
        duration_ms = (time.perf_counter() - started) * 1000
        self._latencies.append(duration_ms)
        logger.debug(
            f"sender_intel | op={op} | sender={sender_id} | chat={chat_id} | "
            f"msg={message_id} | attempt={attempt} | duration={duration_ms:.1f}ms | "
            f"result={result} | error_type={reason.value if reason else '-'}"
        )

    def _avg_latency(self) -> float:
        if not self._latencies:
            return 0.0
        return sum(self._latencies) / len(self._latencies)

    def snapshot(self) -> Dict[str, Any]:
        """Bounded metrics snapshot for /health (additive, zero-filled)."""
        base = {
            "sender_resolve_success": 0, "sender_resolve_failure": 0,
            "message_resolve_success": 0, "message_resolve_failure": 0,
            "entity_cache_hit": 0, "entity_cache_miss": 0,
            "telegram_retry_count": 0, "flood_wait_count": 0,
            "inflight_dedup": 0,
        }
        return {
            "enabled": bool(getattr(CFG, "SENDER_INTEL_ENABLED", True)),
            "cache_size": len(self._entity_cache),
            "flood_wait_remaining": round(max(0.0, self._flood_wait_until - time.time()), 1),
            "average_resolve_latency": round(self._avg_latency(), 2),
            **{**base, **self._metrics},
        }

    # ── FloodWait handling (requirement #19) ──────────────────────────────
    def _record_flood(self, seconds: float, op: str) -> None:
        wait = max(0.0, float(seconds or 0))
        candidate = time.time() + wait
        if candidate > self._flood_wait_until:
            self._flood_wait_until = candidate
        self._inc("flood_wait_count")
        logger.warning(
            f"sender_intel | FloodWait on {op}: operation wait_seconds={wait:.0f} "
            f"timestamp={int(time.time())} — honoring Telegram duration"
        )

    def _in_flood_cooldown(self) -> bool:
        return time.time() < self._flood_wait_until

    async def _sleep_or_fail_flood(self, seconds: float, op: str) -> None:
        """Wait the exact Telegram-provided duration when it is short
        enough not to stall the pipeline; otherwise record the cooldown
        and let the caller fail fast (still never re-hammering Telegram)."""
        self._record_flood(seconds, op)
        max_sleep = float(getattr(CFG, "SENDER_FLOOD_MAX_SLEEP", 5))
        if seconds <= max_sleep:
            await asyncio.sleep(seconds)
        # long windows: cooldown alone gates further attempts

    # ── entity resolution (requirement #5–#8, #21, #25) ───────────────────
    async def resolve_entity(
        self,
        client: TelegramClient,
        sender_id: Optional[int],
        *,
        access_hash: Optional[int] = None,
        username: Optional[str] = None,
        db_lookup: Optional[Callable[[], Any]] = None,
        allow_dialog_scan: bool = False,
        dialog_limit: int = 200,
    ) -> ResolveResult:
        """
        Multi-fallback sender entity resolution. Priority:
          cache → InputPeerUser(id, access_hash) → username →
          db_lookup() (re-try peer/username with fresher values) →
          get_entity(sender_id) → optional bounded dialog scan.
        Any failure returns ResolveResult(reason=...) — never raises.
        """
        sender_id = int(sender_id) if sender_id else 0
        if not sender_id:
            return ResolveResult(reason=FailureReason.USER_NOT_RESOLVABLE)
        if self._in_flood_cooldown():
            return ResolveResult(reason=FailureReason.FLOOD_WAIT)

        # 1) entity cache
        cached = self._entity_cache.get(sender_id)
        if cached is not None:
            self._inc("entity_cache_hit")
            self._inc("sender_resolve_success")
            return ResolveResult(entity=cached, method="entity_cache")

        # 2) in-flight dedup (requirement #34)
        inflight_key = (id(client), sender_id)
        existing = self._inflight.get(inflight_key)
        if existing is not None and not existing.done():
            self._inc("inflight_dedup")
            try:
                return await existing
            except asyncio.CancelledError:
                raise
            except Exception as e:
                return ResolveResult(reason=classify_telethon_error(e))

        async def _runner() -> ResolveResult:
            started = time.perf_counter()
            try:
                result = await self._resolve_entity_inner(
                    client, sender_id,
                    access_hash=access_hash, username=username,
                    db_lookup=db_lookup, allow_dialog_scan=allow_dialog_scan,
                    dialog_limit=dialog_limit,
                )
                self._time("resolve_entity", sender_id, None, None, result.attempts, started,
                           "ok" if result.ok else "fail", None if result.ok else result.reason)
                return result
            except asyncio.CancelledError:
                raise
            except Exception as e:  # absolute safety net
                reason = classify_telethon_error(e)
                self._time("resolve_entity", sender_id, None, None, 0, started, "fail", reason)
                self._inc("sender_resolve_failure")
                return ResolveResult(reason=reason)

        task: asyncio.Task = asyncio.ensure_future(_runner())
        self._inflight[inflight_key] = task
        try:
            result = await task
        finally:
            self._inflight.pop(inflight_key, None)
        if result.ok and result.entity is not None:
            self._entity_cache[sender_id] = result.entity
        return result

    async def _resolve_entity_inner(
        self,
        client: TelegramClient,
        sender_id: int,
        *,
        access_hash: Optional[int],
        username: Optional[str],
        db_lookup: Optional[Callable[[], Any]],
        allow_dialog_scan: bool,
        dialog_limit: int,
    ) -> ResolveResult:
        attempts = 0
        last_reason: Optional[FailureReason] = None

        async def attempt(coro_factory: Callable[[], Any], method: str) -> Optional[Any]:
            nonlocal attempts, last_reason
            attempts += 1
            self._inc("telegram_retry_count") if attempts > 1 else None
            try:
                value = await coro_factory()
                if value is not None:
                    self._inc("sender_resolve_success")
                    return value
                last_reason = FailureReason.ENTITY_NOT_FOUND
                return None
            except FloodWaitError as e:
                await self._sleep_or_fail_flood(getattr(e, "seconds", 0) or 0, f"resolve_entity:{method}")
                last_reason = FailureReason.FLOOD_WAIT
                return None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                reason = classify_telethon_error(e)
                if reason == FailureReason.NETWORK_TIMEOUT and attempts < self._retry.max_attempts:
                    delay = self._retry.delay_for(attempts - 1)
                    logger.debug(
                        f"sender_intel | transient {type(e).__name__} on {method} "
                        f"(attempt {attempts}/{self._retry.max_attempts}) — retrying in {delay:.2f}s"
                    )
                    await asyncio.sleep(delay)
                    return await attempt(coro_factory, method)
                last_reason = reason
                return None

        # a) InputPeerUser from the provided access_hash
        peer = self.build_input_peer(sender_id, access_hash)
        if peer is not None:
            entity = await attempt(lambda: client.get_entity(peer), "input_peer")
            if entity is not None:
                return ResolveResult(entity=entity, method="input_peer", attempts=attempts)

        # b) username
        clean_uname = _clean_username(username)
        if clean_uname:
            entity = await attempt(lambda: client.get_entity("@" + clean_uname.lstrip("@")), "username")
            if entity is not None:
                return ResolveResult(entity=entity, method="username", attempts=attempts)

        # c) DB fallback → fresher access_hash / username → re-try a/b
        if db_lookup is not None:
            try:
                row = await db_lookup()
            except Exception:
                row = None
            if row:
                db_hash = row.get("access_hash")
                db_uname = _clean_username(row.get("username"))
                if db_hash is not None and db_hash != access_hash:
                    peer = self.build_input_peer(sender_id, db_hash)
                    if peer is not None:
                        entity = await attempt(lambda: client.get_entity(peer), "db_input_peer")
                        if entity is not None:
                            return ResolveResult(entity=entity, method="db_input_peer", attempts=attempts)
                if db_uname and db_uname != clean_uname:
                    entity = await attempt(lambda: client.get_entity("@" + db_uname.lstrip("@")), "db_username")
                    if entity is not None:
                        return ResolveResult(entity=entity, method="db_username", attempts=attempts)

        # d) bare sender_id — Telethon's session cache often resolves this
        entity = await attempt(lambda: client.get_entity(sender_id), "sender_id")
        if entity is not None:
            return ResolveResult(entity=entity, method="sender_id", attempts=attempts)

        # e) optional bounded dialog scan (requirement #25: efficient use)
        if allow_dialog_scan:
            async def _scan():
                async for dialog in client.iter_dialogs(limit=dialog_limit):
                    if getattr(dialog, "id", None) == sender_id:
                        return dialog.entity
                return None
            entity = await attempt(_scan, "dialog_scan")
            if entity is not None:
                return ResolveResult(entity=entity, method="dialog_scan", attempts=attempts)

        reason = last_reason or FailureReason.USER_NOT_RESOLVABLE
        self._inc("sender_resolve_failure")
        return ResolveResult(reason=reason, attempts=attempts)

    async def fetch_event_sender(self, event: Any, client: TelegramClient) -> Optional[Any]:
        """
        Resolve the User entity for a NewMessage whose .sender is None.
        In-flight dedup by sender_id means a burst of messages from the
        same user produces ONE GetUsers-style request, not N.
        Never raises.
        """
        sender_id = getattr(event, "sender_id", None)
        if not sender_id:
            return None
        try:
            result = await self.resolve_entity(client, int(sender_id))
            return result.entity if result.ok else None
        except Exception:
            return None

    # ── message resolution + verification (requirement #26–#28) ──────────
    async def resolve_message(
        self,
        client: TelegramClient,
        *,
        entity: Any = None,
        chat_id: Optional[int] = None,
        username: Optional[str] = None,
        parsed_link: Optional[ParsedLink] = None,
        message_id: Optional[int] = None,
        expected_chat_id: Optional[int] = None,
    ) -> ResolveResult:
        """
        Direct message retrieval: get_messages(entity, ids=message_id) —
        never a text search. VERIFY before success:
          message exists, message.id == requested id, message chat matches
        the expected chat. Returns ResolveResult(message=...) or a precise
        internal failure reason.
        """
        started = time.perf_counter()
        self._inc("message_resolve_attempts")
        try:
            chat_entity = entity
            if chat_entity is None and parsed_link is not None:
                if parsed_link.kind in ("public_chat", "public_message") and parsed_link.username:
                    chat_entity = await client.get_entity("@" + parsed_link.username)
                elif parsed_link.kind in ("private_chat", "private_message") and parsed_link.chat_id:
                    chat_entity = await client.get_entity(parsed_link.chat_id)
                elif parsed_link.kind == "invite":
                    # Invite links NEVER auto-join (requirement #16) —
                    # membership/access depends on the session's rights.
                    return ResolveResult(
                        reason=FailureReason.PRIVATE_CHAT_INACCESSIBLE,
                        attempts=0,
                    )
                elif parsed_link.kind == "invalid":
                    return ResolveResult(reason=parsed_link.reason or FailureReason.INVALID_LINK, attempts=0)
            if chat_entity is None and chat_id:
                try:
                    chat_entity = await client.get_entity(int(chat_id))
                except Exception as e:
                    reason = classify_telethon_error(e)
                    self._inc("message_resolve_failure")
                    self._time("resolve_message", None, chat_id, message_id, 1, started, "fail", reason)
                    return ResolveResult(reason=reason, attempts=1)
            if chat_entity is None:
                self._inc("message_resolve_failure")
                self._time("resolve_message", None, chat_id, message_id, 1, started, "fail", FailureReason.CHAT_NOT_FOUND)
                return ResolveResult(reason=FailureReason.CHAT_NOT_FOUND, attempts=1)
            if not message_id:
                self._inc("message_resolve_failure")
                return ResolveResult(reason=FailureReason.MESSAGE_NOT_FOUND, attempts=1, entity=chat_entity)
            try:
                msg = await client.get_messages(chat_entity, ids=int(message_id))
            except FloodWaitError as e:
                await self._sleep_or_fail_flood(getattr(e, "seconds", 0) or 0, "resolve_message")
                self._inc("message_resolve_failure")
                self._time("resolve_message", None, chat_id, message_id, 1, started, "fail", FailureReason.FLOOD_WAIT)
                return ResolveResult(reason=FailureReason.FLOOD_WAIT, attempts=1, entity=chat_entity)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                reason = classify_telethon_error(e)
                self._inc("message_resolve_failure")
                self._time("resolve_message", None, chat_id, message_id, 1, started, "fail", reason)
                return ResolveResult(reason=reason, attempts=1, entity=chat_entity)
            # ── verify before success (requirement #27) ──
            if msg is None:
                self._inc("message_resolve_failure")
                self._time("resolve_message", None, chat_id, message_id, 1, started, "fail", FailureReason.MESSAGE_DELETED)
                return ResolveResult(reason=FailureReason.MESSAGE_DELETED, attempts=1, entity=chat_entity)
            if getattr(msg, "id", None) != int(message_id):
                self._inc("message_resolve_failure")
                self._time("resolve_message", None, chat_id, message_id, 1, started, "fail", FailureReason.MESSAGE_NOT_FOUND)
                return ResolveResult(reason=FailureReason.MESSAGE_NOT_FOUND, attempts=1, entity=chat_entity)
            want_chat = expected_chat_id if expected_chat_id is not None else chat_id
            if want_chat is not None and getattr(msg, "chat_id", None) != int(want_chat):
                self._inc("message_resolve_failure")
                self._time("resolve_message", None, chat_id, message_id, 1, started, "fail", FailureReason.MESSAGE_NOT_FOUND)
                return ResolveResult(reason=FailureReason.MESSAGE_NOT_FOUND, attempts=1, entity=chat_entity)
            self._inc("message_resolve_success")
            self._time("resolve_message", getattr(msg, "sender_id", None), chat_id, message_id, 1, started, "ok")
            return ResolveResult(message=msg, entity=chat_entity, method="get_messages", attempts=1)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            reason = classify_telethon_error(e)
            self._inc("message_resolve_failure")
            self._time("resolve_message", None, chat_id, message_id, 1, started, "fail", reason)
            return ResolveResult(reason=reason, attempts=1)


# =============================================================================
# Module singleton (shared by all account monitors — same pattern as
# monitors._capture) + /health diagnostics hook
# =============================================================================
sender_intel = SenderResolver(
    entity_cache_size=getattr(CFG, "SENDER_ENTITY_CACHE_SIZE", 2000),
    entity_cache_ttl=getattr(CFG, "SENDER_ENTITY_CACHE_TTL", 900),
)
logger.info(
    f"Sender Intelligence initialized | enabled={getattr(CFG, 'SENDER_INTEL_ENABLED', True)} | "
    f"cache={getattr(CFG, 'SENDER_ENTITY_CACHE_SIZE', 2000)} | ttl={getattr(CFG, 'SENDER_ENTITY_CACHE_TTL', 900)}s"
)


def get_sender_intel_snapshot() -> Dict[str, Any]:
    """Read-only snapshot for /health and dashboard diagnostics.
    Standard keys ALWAYS present (zero-filled) — stable health contract."""
    try:
        return sender_intel.snapshot()
    except Exception:
        return {"enabled": False, "cache_size": 0}
