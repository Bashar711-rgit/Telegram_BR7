#!/usr/bin/env python3
"""
sender_resolver.py — Sender Intelligence / Sender Contact Resolver v14.5.2
==========================================================================

طبقة استخلاص وتطبيع وحفظ وحلّ بيانات المرسل (Backend ONLY).

v14.5.2 (طلب المستخدم — توسيع وسائل الوصول للمرسل):
  * New module-level helper build_phone_url(): builds
    https://t.me/+<phone> from a raw phone value. Used by contact_target
    as a THIRD-priority fallback after username and temporary_link.
    Works only when the user allows Telegram's "find me by phone" — a
    documented privacy limit, not a bug.
  * New SenderResolver.export_temporary_link() async method: fetches a
    temporary profile link via contacts.exportContactToken. The result
    is a https://t.me/+<token> URL that opens the user's profile EVEN
    when they have no username — a Telegram-official mechanism (see
    core.telegram.org/api/links, "Temporary profile links"). The URL
    expires server-side (usually ~1 week) so callers compute it per
    alert, not cache it.
  * snapshot() now exposes temporary_link_success / _failure metrics
    for /health observability.

v14.5.1 (سابق — correction pass):
  * _USERNAME_RE aligned to {4,31} (5-32 chars).
  * _USERNAME_RE precompiled at module load.
  * New is_valid_username() helper.
  * build_message_link validates the username before building.
  * build_display_name fallback = "مرسل غير محدد".
  * resolve_message honors the FloodWait cooldown.

CORE PRINCIPLE — capture at arrival.
Scope guard: maximum reliable access within Telegram's authorized API.
"""

from __future__ import annotations

import asyncio
import random
import re
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

__version__ = "14.5.2"


class FailureReason(str, Enum):
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


_TRANSIENT_HINTS = (
    "TIMEOUT", "TIMEDOUT", "CONNECTION", "NETWORK", "SERVERERROR",
    "SERVICEUNAVAILABLE", "INTERNAL", "SEEOTHER", "CALLFAIL",
)


def classify_telethon_error(exc: Exception) -> FailureReason:
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


_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def _clean_username(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    clean = value.strip().lstrip("@")
    if not clean or len(clean) > 64:
        return None
    if clean.isdigit():
        return None
    return clean


def is_valid_username(value: Any) -> bool:
    clean = _clean_username(value)
    return bool(clean) and bool(_USERNAME_RE.match(clean))


# ═══════════════════════════════════════════════════════════════════════════
# v14.5.2: build_phone_url — https://t.me/+<phone> builder (pure, sync)
# ═══════════════════════════════════════════════════════════════════════════
def build_phone_url(phone: Any) -> Optional[str]:
    """v14.5.2: build a https://t.me/+<phone> URL for profile access via phone.

    Requires the user to allow "find me by phone number" in Telegram privacy
    settings (documented limit — this function does NOT bypass it).

    Accepts various formats:
        "+966501234567"      → https://t.me/+966501234567
        "966501234567"       → https://t.me/+966501234567
        "00966501234567"     → https://t.me/+966501234567
        "+966 50 123 4567"   → https://t.me/+966501234567
        "+966-50-123-4567"   → https://t.me/+966501234567

    Returns None on invalid/missing input. Never raises.
    """
    if not phone:
        return None
    try:
        s = str(phone).strip()
        if not s:
            return None
        # Normalize international prefix: "00..." or "+..." → strip.
        if s.startswith("00"):
            s = s[2:]
        elif s.startswith("+"):
            s = s[1:]
        # Keep digits only.
        digits = "".join(c for c in s if c.isdigit())
        # E.164 length bounds (8..15). Shorter/longer = malformed.
        if not digits or len(digits) < 8 or len(digits) > 15:
            return None
        return f"https://t.me/+{digits}"
    except Exception:
        return None


def extract_sender(entity: Any) -> Dict[str, Any]:
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
        if not isinstance(access_hash, int):
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
            "phone": getattr(entity, "phone", None),
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
    except Exception as e:
        logger.debug(f"sender_intel: extract failed: {type(e).__name__}: {e}")
        return {}


_FLAT_META_KEYS = (
    "phone", "is_bot", "is_verified", "is_premium", "is_scam", "is_fake",
    "is_restricted", "restriction_reason", "status", "lang_code",
    "is_deleted", "photo_available", "is_contact", "is_mutual_contact",
    "usernames", "is_min", "bot_info_version",
)


def extract_flat(entity: Any) -> Dict[str, Any]:
    meta = extract_sender(entity)
    if not meta:
        return {}
    flat: Dict[str, Any] = {}
    for key in _FLAT_META_KEYS:
        flat[f"sender_{key}"] = meta.get(key)
    return flat


def meta_to_contact_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in _FLAT_META_KEYS:
        out[key] = data.get(f"sender_{key}")
    return out


def build_display_name(first_name: Optional[str], last_name: Optional[str], sender_id: int = 0) -> str:
    name = f"{first_name or ''} {last_name or ''}".strip()
    return name or "مرسل غير محدد"


@dataclass(frozen=True, slots=True)
class ParsedLink:
    raw: str = ""
    kind: str = "invalid"
    username: Optional[str] = None
    message_id: Optional[int] = None
    inner_id: Optional[int] = None
    chat_id: Optional[int] = None
    invite_hash: Optional[str] = None
    reason: Optional[FailureReason] = None


def parse_telegram_link(link: str) -> ParsedLink:
    if not link or not isinstance(link, str):
        return ParsedLink(raw=str(link or ""), kind="invalid", reason=FailureReason.INVALID_LINK)
    raw = link.strip()
    text = raw
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
    text = text.split("?", 1)[0].split("#", 1)[0].strip("/")
    if not text:
        return ParsedLink(raw=raw, kind="invalid", reason=FailureReason.INVALID_LINK)

    if text.startswith("@"):
        uname = _clean_username(text)
        if uname and _USERNAME_RE.match(uname.lstrip("@")):
            return ParsedLink(raw=raw, kind="username", username=uname.lstrip("@"))
        return ParsedLink(raw=raw, kind="invalid", reason=FailureReason.INVALID_LINK)

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
        parts = parts[1:]
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


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter_frac: float = 0.25

    def delay_for(self, attempt: int) -> float:
        raw = min(self.max_delay, self.base_delay * (2 ** max(0, attempt)))
        jitter = raw * self.jitter_frac
        return max(0.0, raw + random.uniform(-jitter, jitter))


@dataclass(slots=True)
class ResolveResult:
    entity: Any = None
    message: Any = None
    method: Optional[str] = None
    reason: Optional[FailureReason] = None
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.reason is None


class SenderResolver:
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
        self._flood_wait_until: float = 0.0

    extract = staticmethod(extract_sender)
    extract_flat = staticmethod(extract_flat)
    build_display_name = staticmethod(build_display_name)
    build_phone_url = staticmethod(build_phone_url)

    @staticmethod
    def build_input_peer(sender_id: Optional[int], access_hash: Optional[int]) -> Optional[InputPeerUser]:
        try:
            if not sender_id or access_hash is None:
                return None
            return InputPeerUser(user_id=int(sender_id), access_hash=int(access_hash))
        except Exception:
            return None

    @staticmethod
    def build_profile_link(sender_id: Optional[int], username: Optional[str]) -> str:
        if username:
            return f"https://t.me/{username.lstrip('@')}"
        return f"tg://user?id={sender_id or 0}"

    @staticmethod
    def build_message_link(chat_id: Optional[int], message_id: int, username: Optional[str] = None) -> Dict[str, str]:
        links = {"group": "#", "message": "#"}
        clean = username if is_valid_username(username) else None
        if clean:
            clean = clean.lstrip("@")
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
            "has_phone": bool(build_phone_url(data.get("sender_phone"))),
        }

    @classmethod
    def get_best_access_method(cls, data: Dict[str, Any]) -> str:
        flags = cls.build_contact_methods(data)
        for name in ("has_entity", "has_input_peer", "has_username", "has_phone",
                     "has_dialog", "has_message_context", "has_access_hash"):
            if flags.get(name):
                return name
        return "none"

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
        base = {
            "sender_resolve_success": 0, "sender_resolve_failure": 0,
            "message_resolve_success": 0, "message_resolve_failure": 0,
            "entity_cache_hit": 0, "entity_cache_miss": 0,
            "telegram_retry_count": 0, "flood_wait_count": 0,
            "inflight_dedup": 0,
            "temporary_link_success": 0, "temporary_link_failure": 0,
        }
        return {
            "enabled": bool(getattr(CFG, "SENDER_INTEL_ENABLED", True)),
            "cache_size": len(self._entity_cache),
            "flood_wait_remaining": round(max(0.0, self._flood_wait_until - time.time()), 1),
            "average_resolve_latency": round(self._avg_latency(), 2),
            **{**base, **self._metrics},
        }

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
        self._record_flood(seconds, op)
        max_sleep = float(getattr(CFG, "SENDER_FLOOD_MAX_SLEEP", 5))
        if seconds <= max_sleep:
            await asyncio.sleep(seconds)

    # ═══════════════════════════════════════════════════════════════════
    # v14.5.2: export_temporary_link — https://t.me/+<token> via API
    # ═══════════════════════════════════════════════════════════════════
    async def export_temporary_link(
        self,
        client: TelegramClient,
        sender_id: Any,
        access_hash: Any,
        *,
        timeout: float = 3.0,
    ) -> Optional[str]:
        """v14.5.2: fetch a temporary profile link via contacts.exportContactToken.

        Produces https://t.me/+<token> that opens the user's profile EVEN
        when they have no username — a Telegram-official mechanism
        (core.telegram.org/api/links, "Temporary profile links"). The link
        expires server-side (usually ~1 week), so the caller computes it
        per alert (not cached forever).

        The caller MUST pass the SENDING account's client and that
        account's own access_hash for the user — same per-account hash
        rule as the mention ladder. A cross-account hash will fail with
        USER_ID_INVALID (Telegram's rule, never bypassed here).

        Never raises. Returns None on any failure (import error, missing
        input, FloodWait, timeout, invalid response).
        """
        try:
            sid = int(sender_id or 0)
            ah = int(access_hash) if access_hash is not None else 0
        except (TypeError, ValueError):
            return None
        if not sid or not ah:
            return None
        if self._in_flood_cooldown():
            self._inc("temporary_link_failure")
            return None
        try:
            from telethon.tl.functions.contacts import ExportContactTokenRequest
            from telethon.tl.types import InputUser as _InputUser
        except Exception:
            self._inc("temporary_link_failure")
            return None
        try:
            result = await asyncio.wait_for(
                client(ExportContactTokenRequest(
                    _InputUser(user_id=sid, access_hash=ah)
                )),
                timeout=float(timeout),
            )
            url = getattr(result, "url", None)
            if isinstance(url, str) and url.startswith("https://t.me/"):
                self._inc("temporary_link_success")
                return url
            self._inc("temporary_link_failure")
            return None
        except FloodWaitError as e:
            self._record_flood(getattr(e, "seconds", 0) or 0, "export_contact_token")
            self._inc("temporary_link_failure")
            return None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"export_temporary_link failed [{sid}]: {type(e).__name__}: {e}")
            self._inc("temporary_link_failure")
            return None

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
        sender_id = int(sender_id) if sender_id else 0
        if not sender_id:
            return ResolveResult(reason=FailureReason.USER_NOT_RESOLVABLE)
        if self._in_flood_cooldown():
            return ResolveResult(reason=FailureReason.FLOOD_WAIT)

        cached = self._entity_cache.get(sender_id)
        if cached is not None:
            self._inc("entity_cache_hit")
            self._inc("sender_resolve_success")
            return ResolveResult(entity=cached, method="entity_cache")

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
            except Exception as e:
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

        peer = self.build_input_peer(sender_id, access_hash)
        if peer is not None:
            entity = await attempt(lambda: client.get_entity(peer), "input_peer")
            if entity is not None:
                return ResolveResult(entity=entity, method="input_peer", attempts=attempts)

        clean_uname = _clean_username(username)
        if clean_uname:
            entity = await attempt(lambda: client.get_entity("@" + clean_uname.lstrip("@")), "username")
            if entity is not None:
                return ResolveResult(entity=entity, method="username", attempts=attempts)

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

        entity = await attempt(lambda: client.get_entity(sender_id), "sender_id")
        if entity is not None:
            return ResolveResult(entity=entity, method="sender_id", attempts=attempts)

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

    def cached_username(self, sender_id: Any) -> Optional[str]:
        try:
            if not sender_id:
                return None
            ent = self._entity_cache.get(int(sender_id))
            if ent is not None:
                return _clean_username(getattr(ent, "username", None))
        except Exception:
            pass
        return None

    async def fetch_event_sender(self, event: Any, client: TelegramClient) -> Optional[Any]:
        sender_id = getattr(event, "sender_id", None)
        if not sender_id:
            return None
        try:
            result = await self.resolve_entity(client, int(sender_id))
            return result.entity if result.ok else None
        except Exception:
            return None

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
        started = time.perf_counter()
        self._inc("message_resolve_attempts")
        if self._in_flood_cooldown():
            self._inc("message_resolve_failure")
            self._time("resolve_message", None, chat_id, message_id, 0, started, "fail", FailureReason.FLOOD_WAIT)
            return ResolveResult(reason=FailureReason.FLOOD_WAIT, attempts=0)
        try:
            chat_entity = entity
            if chat_entity is None and parsed_link is not None:
                if parsed_link.kind in ("public_chat", "public_message") and parsed_link.username:
                    chat_entity = await client.get_entity("@" + parsed_link.username)
                elif parsed_link.kind in ("private_chat", "private_message") and parsed_link.chat_id:
                    chat_entity = await client.get_entity(parsed_link.chat_id)
                elif parsed_link.kind == "invite":
                    return ResolveResult(
                        reason=FailureReason.PRIVATE_CHAT_INACCESSIBLE, attempts=0)
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


from telethon.tl.types import (  # noqa: E402
    InputMessageEntityMentionName,
    InputUser,
    InputUserFromMessage,
    MessageEntityMentionName,
    MessageEntityTextUrl,
)


def sender_url_forms(sender_id: int) -> Tuple[str, str]:
    sid = int(sender_id or 0)
    return (f"tg://user?id={sid}", f"tg://openmessage?user_id={sid}")


def input_user_from_entity(entity: Any) -> Optional[InputUser]:
    try:
        uid = getattr(entity, "id", None)
        ah = getattr(entity, "access_hash", None)
        if isinstance(uid, int) and uid and isinstance(ah, int) and ah != 0:
            return InputUser(user_id=uid, access_hash=ah)
    except Exception:
        pass
    return None


def input_user_from_input_peer(peer: Any) -> Optional[InputUser]:
    if isinstance(peer, InputPeerUser):
        try:
            uid = getattr(peer, "user_id", None)
            ah = getattr(peer, "access_hash", None)
            if isinstance(uid, int) and uid and isinstance(ah, int) and ah != 0:
                return InputUser(user_id=uid, access_hash=ah)
        except Exception:
            pass
    return None


def input_user_from_message_context(peer: Any, msg_id: Any, sender_id: int) -> Optional[InputUserFromMessage]:
    try:
        mid = int(msg_id or 0)
        sid = int(sender_id or 0)
        if peer is None or not mid or not sid:
            return None
        return InputUserFromMessage(peer=peer, msg_id=mid, user_id=sid)
    except Exception:
        return None


def build_mention_entities(alert_html: str, sender_id: int, input_user: Any) -> Optional[Tuple[str, List[Any]]]:
    if not alert_html or not sender_id or input_user is None:
        return None
    try:
        from telethon.extensions import html as _tg_html
        text, ents = _tg_html.parse(alert_html)
        urls = sender_url_forms(sender_id)
        fixed: List[Any] = []
        replaced = 0
        for e in ents:
            try:
                if not getattr(e, "length", 0):
                    continue
                hit = False
                if isinstance(e, MessageEntityMentionName) and int(getattr(e, "user_id", 0) or 0) == int(sender_id):
                    hit = True
                elif isinstance(e, MessageEntityTextUrl) and getattr(e, "url", "") in urls:
                    hit = True
                if hit:
                    fixed.append(InputMessageEntityMentionName(int(e.offset), int(e.length), input_user))
                    replaced += 1
                else:
                    fixed.append(e)
            except Exception:
                fixed.append(e)
        if not replaced:
            return None
        return text, fixed
    except Exception:
        return None


class AccountAccessHashStore:
    DEFAULT_TTL = 7 * 86400.0

    def __init__(self, maxsize: int = 20000, ttl: float = DEFAULT_TTL):
        self._data: "OrderedDict[Tuple[str, int], Tuple[int, float]]" = OrderedDict()
        self._maxsize = max(100, int(maxsize))
        self._ttl = max(60.0, float(ttl))
        self.records = 0

    def record(self, account_name: Optional[str], sender_id: Any, access_hash: Any) -> bool:
        try:
            if not account_name or not sender_id or not isinstance(access_hash, int) or access_hash == 0:
                return False
            key = (str(account_name), int(sender_id))
            old = self._data.get(key)
            if old is not None and old[0] == access_hash:
                self._data.move_to_end(key)
                return False
            self._data[key] = (access_hash, time.time())
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)
            self.records += 1
            return True
        except Exception:
            return False

    def get(self, account_name: Optional[str], sender_id: Any) -> Optional[int]:
        try:
            if not account_name or not sender_id:
                return None
            key = (str(account_name), int(sender_id))
            entry = self._data.get(key)
            if entry is None:
                return None
            val, ts = entry
            if (time.time() - ts) > self._ttl:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return val
        except Exception:
            return None

    def size(self) -> int:
        return len(self._data)


class MentionNegativeCache:
    def __init__(self, ttl_seconds: int = 300, maxsize: int = 5000):
        self._ttl = max(10, int(ttl_seconds))
        self._maxsize = max(100, int(maxsize))
        self._data: "OrderedDict[Tuple[str, int, str], float]" = OrderedDict()

    def mark(self, account_name: Optional[str], sender_id: Any, tier: str) -> None:
        try:
            if not account_name or not sender_id or not tier:
                return
            key = (str(account_name), int(sender_id), str(tier))
            self._data[key] = time.time()
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)
        except Exception:
            pass

    def blocked(self, account_name: Optional[str], sender_id: Any, tier: str) -> bool:
        try:
            if not account_name or not sender_id or not tier:
                return False
            key = (str(account_name), int(sender_id), str(tier))
            ts = self._data.get(key)
            if ts is None:
                return False
            if time.time() - ts > self._ttl:
                self._data.pop(key, None)
                return False
            return True
        except Exception:
            return False

    def size(self) -> int:
        return len(self._data)


account_hash_store = AccountAccessHashStore()
mention_negative = MentionNegativeCache(getattr(CFG, "MENTION_NEG_TTL_SECONDS", 300))


def get_mention_intel_snapshot() -> Dict[str, Any]:
    return {
        "mention_fix_enabled": bool(getattr(CFG, "SENDER_MENTION_FIX_ENABLED", True)),
        "account_hash_store_size": account_hash_store.size(),
        "negative_cache_size": mention_negative.size(),
    }


sender_intel = SenderResolver(
    entity_cache_size=getattr(CFG, "SENDER_ENTITY_CACHE_SIZE", 2000),
    entity_cache_ttl=getattr(CFG, "SENDER_ENTITY_CACHE_TTL", 900),
)
logger.info(
    f"Sender Intelligence initialized | enabled={getattr(CFG, 'SENDER_INTEL_ENABLED', True)} | "
    f"cache={getattr(CFG, 'SENDER_ENTITY_CACHE_SIZE', 2000)} | ttl={getattr(CFG, 'SENDER_ENTITY_CACHE_TTL', 900)}s"
)


def get_sender_intel_snapshot() -> Dict[str, Any]:
    try:
        return sender_intel.snapshot()
    except Exception:
        return {"enabled": False, "cache_size": 0}
