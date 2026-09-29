#!/usr/bin/env python3
"""
nav_resolver.py — Alert Navigation Resolver v10.6.0
====================================================

Centralized multi-strategy resolution for the three clickable alert actions:

  1. اسم المرسل  → يفتح محادثة/بروفايل المرسل مباشرة
  2. اسم المجموعة → يفتح المجموعة/القناة المصدر
  3. عرض الرسالة  → يفتح الرسالة الأصلية داخل مصدرها

ARCHITECTURE (priority ladders — first wins, every failure advances):

  SENDER (username → anchor ladder; the v10.4 real-mention ladder stays the
  top mechanism at send time and CONSUMES whatever username we recover here):
    S1  event username            — captured at arrival (zero cost)
    S2  shared entity cache       — sender_intel cache hit (in-memory;
        usernames are GLOBAL truth, safe for links across accounts —
        access_hash would NOT be, and is never taken from here)
    S3  DB sender_contacts row    — a username seen on ANY earlier message
    S4  DISCOVERY via the source message — users.getUsers fed with
        InputUserFromMessage(peer, msg_id, user_id). Legitimate MTProto:
        the account is a member of the source chat and RECEIVED the
        sender's message, so Telegram resolves the user through that
        context even when the participant list hides them. A recovered
        username becomes a universally-clickable t.me link AND is
        persisted (COALESCE) so every future alert is clickable too;
        the fresh per-account access_hash feeds the v10.4 mention
        ladder tier-4. Telegram returns only what it permits — a truly
        hidden username simply comes back absent and we degrade honestly.
    S5  (still no username)       — the frozen builder keeps its current
        tg://openmessage/tg://user anchors; at SEND time the v10.4
        mention ladder upgrades them when the sending account can
        resolve the user, and harden_sender_anchor() guarantees the
        fallback payload's name click lands on the SOURCE MESSAGE
        (avatar tap → profile) instead of being silently dropped.

  CHAT (group name):
    C1  resolved entity.username  — from the SENDING account's view
        (already inside chat_info.group_link via build_telegram_links)
    C2  event chat_username       — when entity resolution failed but the
        update itself carried the username
    C3  canonical t.me/c/{inner}  — deterministic private supergroup/channel
        form from the real -100 chat_id (members-only; admins monitor the
        source groups) — validated, never built for other id shapes
    C4  exportMessageLink         — LAST resort: the Telegram API's own
        message-link exporter (channels.exportMessageLink). Runs only when
        nothing valid exists yet, only for Channel-like peers, cached per
        chat (TTL) with a negative cache and single-flight lock. It can
        DISCOVER a public username the entity view lacked — upgrading a
        members-only c/ link to a universally-openable t.me/{username} link.

  MESSAGE (عرض الرسالة):
    M1  t.me/{username}/{id}      — best for every recipient
    M2  t.me/c/{inner}/{id}       — private supergroups/channels (members)
    M3  exportMessageLink result  — when M1/M2 were impossible
    M4  none                      — frozen wording «الرابط غير متاح» and the
        button is omitted (basic groups have NO message links in Telegram —
        this is a Telegram limitation, not a bug; stated, not faked)

  VERIFY (final gate — requirement #15): every generated link is checked
  against its data BEFORE the alert is built; anything malformed degrades
  to the exact frozen fallbacks (no invented URLs ever reach the wire).

HARD CONSTRAINTS:
  * The alert FORMAT is a frozen contract (tests/test_alert_regression.py,
    scripts/golden_alert_baseline.py). This module only decides the DATA
    fed into the frozen builder — never layout, wording, or buttons text.
  * No URL is invented. Every link comes from a real username, a canonical
    id form, or the Telegram API itself.
  * No privacy bypass. Invite links are never auto-joined; access_hash is
    never guessed; cross-account hashes are never reused; nothing here
    circumvents what the authenticated account is allowed to do.
  * Prefer already-available data (event/input entities, caches) over
    network calls; the single network strategy is last, cached, and
    failure-isolated so one failed strategy never stops the fallbacks.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cachetools import TTLCache
from loguru import logger
from telethon.errors import FloodWaitError

try:  # ExportMessageLink exists on every Telethon ≥1.x used by this project
    from telethon.tl.functions.channels import ExportMessageLinkRequest
except Exception:  # pragma: no cover — extremely old Telethon fallback
    ExportMessageLinkRequest = None  # type: ignore[assignment]

try:  # v10.6 sender discovery — users.getUsers via message context
    from telethon.tl.functions.users import GetUsersRequest
except Exception:  # pragma: no cover — extremely old Telethon fallback
    GetUsersRequest = None  # type: ignore[assignment]

from config import CFG

__version__ = "1.1.0"

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,63}$")


# =============================================================================
# Small pure helpers (deterministic, unit-testable, zero I/O)
# =============================================================================
def clean_username(value: Any) -> Optional[str]:
    """Normalize a Telegram username (strip @, reject empties/numeric junk)."""
    if not isinstance(value, str):
        return None
    clean = value.strip().lstrip("@")
    if not clean:
        return None
    return clean


def is_valid_username(value: Any) -> bool:
    """True only for shapes Telegram accepts as public usernames."""
    clean = clean_username(value)
    return bool(clean) and bool(_USERNAME_RE.match(clean))


def private_supergroup_inner_id(chat_id: Any) -> Optional[str]:
    """The bare inner id of a -100<inner> supergroup/channel id — the ONLY
    id shape for which t.me/c/{inner} links exist. Basic groups (plain
    negative ids) return None: Telegram provides no message links for them
    and constructing one would be an invented, dead URL."""
    s = str(chat_id or "")
    if s.startswith("-100"):
        inner = s[4:]
        if inner.isdigit() and inner != "0":
            return inner
    return None


def is_valid_tme_url(url: Any) -> bool:
    """Shape-validation for generated t.me links (never a network check —
    a network probe would be an unauthorized automation risk and pointless:
    links point at entities the recipient resolves with their own client)."""
    if not isinstance(url, str) or not url.startswith("https://t.me/"):
        return False
    rest = url[len("https://t.me/"):].strip("/")
    if not rest:
        return False
    parts = rest.split("/")
    if parts[0] == "c":
        # t.me/c/{inner}[/{msg}] — inner must be positive digits
        if len(parts) < 2 or not parts[1].isdigit() or parts[1] == "0":
            return False
        if len(parts) > 2 and not parts[2].isdigit():
            return False
        return True
    # public form — first segment must be a valid username
    if not is_valid_username(parts[0]):
        return False
    if len(parts) > 1 and not parts[1].isdigit():
        return False
    return True


def harden_sender_anchor(alert_html: str, sender_id: Any, fallback_url: Optional[str]) -> str:
    """v10.6 — guaranteed-reachable sender name for the FALLBACK payload.

    Background: when the v10.4 mention ladder cannot produce a real
    mention entity for the SENDING account, Telethon's html send path
    silently DELETES the tg://user/tg://openmessage anchor entity
    (_replace_with_mention) — the sender name renders as plain text and
    nothing is clickable. That dead anchor is exactly the «user not
    clickable» complaint.

    This helper rewrites ONLY that anchor's href (same exact displayed
    text — the wire format stays byte-identical) to `fallback_url`, the
    alert's own source-message link: opening the message always allows
    opening the sender's profile via the avatar tap, for every sender
    who sent a message, regardless of their DM/privacy settings. That
    is the maximum Telegram permits — and it is legitimately permitted.

    Pure string surgery on the two exact anchor shapes _build_alert can
    emit (sender_resolver.sender_url_forms); a username anchor (t.me)
    is already universally clickable and is never touched. A fallback
    URL that fails t.me shape validation is ignored (never inject an
    unvalidated link)."""
    if not alert_html or not fallback_url or not is_valid_tme_url(fallback_url):
        return alert_html
    try:
        sid = int(sender_id or 0)
    except Exception:
        return alert_html
    if not sid:
        return alert_html
    try:
        from sender_resolver import sender_url_forms as _forms
        hardened = alert_html
        for href in _forms(sid):
            needle = f'href="{href}"'
            if needle in hardened:
                hardened = hardened.replace(needle, f'href="{fallback_url}"')
        return hardened
    except Exception:
        return alert_html


def build_canonical_links(chat_id: Any, message_id: Any, username: Any) -> Dict[str, str]:
    """Same shapes as monitors.build_telegram_links but with the -100 guard
    the button builder was missing: c/ form ONLY for -100 ids, username
    form ONLY for validated usernames. Returns {"group", "message"} where
    "#" means «no legitimate link exists»."""
    links = {"group": "#", "message": "#"}
    uname = username if is_valid_username(username) else None
    if uname and message_id:
        clean = str(uname).lstrip("@")
        links["group"] = f"https://t.me/{clean}"
        links["message"] = f"https://t.me/{clean}/{int(message_id)}"
        return links
    if not uname:
        inner = private_supergroup_inner_id(chat_id)
        if inner and message_id:
            links["group"] = f"https://t.me/c/{inner}"
            links["message"] = f"https://t.me/c/{inner}/{int(message_id)}"
    return links


# =============================================================================
# Navigation report — internal diagnostics (logs only, never in alerts)
# =============================================================================
@dataclass
class NavReport:
    sender_strategy: str = "none"
    chat_strategy: str = "none"
    message_strategy: str = "none"
    verified_ok: bool = True
    notes: List[str] = field(default_factory=list)

    def as_line(self) -> str:
        return (
            f"sender={self.sender_strategy} | chat={self.chat_strategy} | "
            f"message={self.message_strategy} | verify={'ok' if self.verified_ok else 'degraded'}"
            + (f" | notes={';'.join(self.notes)}" if self.notes else "")
        )


# =============================================================================
# NavigationResolver — module singleton (same pattern as sender_intel)
# =============================================================================
class NavigationResolver:
    """Centralized resolver shared by ALL account monitors.

    Guarantees:
      * never raises from the public entry point (fail-safe by design)
      * one failed strategy advances to the next — never aborts the chain
      * the only network call (exportMessageLink) is single-flight per chat,
        TTL-cached, negative-cached, and honors FloodWaitError without
        hammering Telegram
    """

    EXPORT_TTL_SECONDS = 3600      # exported links are stable — 1h cache
    EXPORT_NEG_TTL_SECONDS = 300   # failed export → 5min cool-down per chat
    DISCOVERY_NEG_TTL_SECONDS = 300  # failed/absent username → 5min per sender
    DISCOVERY_LOCK_MAX = 4096      # bounded single-flight lock map

    def __init__(self):
        self._export_cache: "TTLCache[int, Dict[str, str]]" = TTLCache(
            maxsize=2000, ttl=self.EXPORT_TTL_SECONDS
        )
        self._export_negative: "TTLCache[int, bool]" = TTLCache(
            maxsize=2000, ttl=self.EXPORT_NEG_TTL_SECONDS
        )
        self._export_lock = asyncio.Lock()
        self._discovery_negative: "TTLCache[int, bool]" = TTLCache(
            maxsize=5000, ttl=self.DISCOVERY_NEG_TTL_SECONDS
        )
        self._discovery_locks: "Dict[int, asyncio.Lock]" = {}
        self._metrics: Dict[str, int] = {
            "sender_username_event": 0, "sender_username_cache": 0,
            "sender_username_db": 0, "sender_username_miss": 0,
            "sender_discovery_skip": 0, "sender_discovery_call": 0,
            "sender_discovery_ok": 0, "sender_discovery_fail": 0,
            "chat_entity_username": 0, "chat_event_username": 0,
            "chat_c_form": 0, "chat_export_hit": 0, "chat_export_call": 0,
            "chat_export_ok": 0, "chat_export_fail": 0, "chat_unlinkable": 0,
            "verify_degraded": 0,
        }

    def _inc(self, key: str) -> None:
        try:
            self._metrics[key] = self._metrics.get(key, 0) + 1
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────
    # SENDER — S1 event → S2 shared entity cache → S3 DB row → S4 discovery
    # ─────────────────────────────────────────────────────────────────────
    async def _resolve_sender_username(
        self, monitor: Any, data: Dict[str, Any], report: NavReport,
        send_client: Any = None,
    ) -> Optional[str]:
        sender_id = data.get("sender_id") or 0

        # S1 — username captured at arrival (validated, never trusted blindly)
        event_uname = clean_username(data.get("sender_username"))
        if is_valid_username(event_uname):
            self._inc("sender_username_event")
            report.sender_strategy = "event_username"
            return event_uname

        # S2 — shared resolver entity cache (in-memory). Username is global
        # truth so any account's cached entity is valid FOR LINKS ONLY.
        # access_hash is per-account and is deliberately NOT taken here.
        try:
            cached_uname = None
            if sender_id:
                from sender_resolver import sender_intel as _si
                cached_uname = _si.cached_username(sender_id)
            if is_valid_username(cached_uname):
                self._inc("sender_username_cache")
                report.sender_strategy = "entity_cache"
                # COALESCE into event data → mention ladder tier-5 + DB
                # persist stay consistent with the recovered username.
                data["sender_username"] = cached_uname
                return cached_uname
        except Exception as e:
            report.notes.append(f"S2:{type(e).__name__}")

        # S3 — persisted contact row: the username from ANY earlier message
        # makes today's anchor a universally-clickable t.me link.
        try:
            if sender_id and getattr(monitor, "db", None) is not None:
                row = await monitor.db.get_sender_contact(int(sender_id))
                db_uname = clean_username((row or {}).get("username"))
                if is_valid_username(db_uname):
                    self._inc("sender_username_db")
                    report.sender_strategy = "db_contact"
                    data["sender_username"] = db_uname
                    return db_uname
        except Exception as e:
            report.notes.append(f"S3:{type(e).__name__}")

        # S4 — network discovery THROUGH the source message (the last
        # strategy with a real API basis; legitimacy argument inside).
        try:
            uname = await self._discover_sender_username_via_message(
                monitor, data, send_client, report
            )
            if is_valid_username(uname):
                data["sender_username"] = uname
                return uname
        except Exception as e:
            report.notes.append(f"S4:{type(e).__name__}")

        self._inc("sender_username_miss")
        report.sender_strategy = "anchor_fallback"
        return None

    # ─────────────────────────────────────────────────────────────────────
    # S4 — users.getUsers(InputUserFromMessage(...)) — sender discovery
    # through the sender's own source message
    # ─────────────────────────────────────────────────────────────────────
    async def _discover_sender_username_via_message(
        self,
        monitor: Any,
        data: Dict[str, Any],
        send_client: Any,
        report: NavReport,
    ) -> Optional[str]:
        """Recover the sender's username (and a fresh per-account
        access_hash) via InputUserFromMessage — the STRONGEST legitimate
        resolution mechanism Telegram offers for group senders.

        Legitimacy (no privacy bypass, requirement #12):
          * the account is a member of the source chat (it received the
            update that produced this alert);
          * the sender identified themselves by posting there — Telegram
            itself exposes message-context peer resolution to every
            member (this is how native clients open profiles from
            messages);
          * Telegram returns ONLY what it permits for this account. A
            user who hid their username or restricted themselves simply
            comes back without one — we record an honest miss and
            degrade; nothing is inferred, guessed or scraped.

        What this buys: a username invisible to S1–S3 becomes a
        universally-clickable t.me link, is persisted (COALESCE — never
        nulls existing fields) so all FUTURE alerts for this sender are
        clickable, and the fresh access_hash feeds the v10.4 mention
        ladder tier-4 (per-account store — never shared cross-account).

        Failure handling: per-sender negative cache (5 min) + single-
        flight lock + FloodWait honored + bounded timeouts; clients are
        tried capturing-view first (guaranteed source-chat member),
        then the alert sender. Never raises."""
        if not bool(getattr(CFG, "NAV_USER_DISCOVERY_ENABLED", True)):
            self._inc("sender_discovery_skip")
            return None
        if GetUsersRequest is None:
            return None
        sender_id = int(data.get("sender_id") or 0)
        chat_id = data.get("chat_id")
        message_id = data.get("message_id")
        if not sender_id or not chat_id or not message_id:
            return None
        if self._discovery_negative.get(sender_id):
            report.notes.append("discovery:negcached")
            return None

        # bounded single-flight per sender (concurrent alerts share one call)
        if len(self._discovery_locks) > self.DISCOVERY_LOCK_MAX:
            try:
                self._discovery_locks.pop(next(iter(self._discovery_locks)))
            except Exception:
                pass
        lock = self._discovery_locks.setdefault(int(sender_id), asyncio.Lock())
        async with lock:
            if self._discovery_negative.get(sender_id):
                report.notes.append("discovery:negcached")
                return None

            # capturing account's own view first (it provably sees the
            # source chat), then the alert-sending client
            clients: List[Any] = []
            for c in (getattr(monitor, "client", None), send_client):
                if c is not None and not any(c is x for x in clients):
                    clients.append(c)

            for client in clients:
                try:
                    peer = await asyncio.wait_for(
                        client.get_input_entity(int(chat_id)), timeout=2.0
                    )
                    from sender_resolver import (
                        input_user_from_message_context as _iu_from_msg,
                    )
                    input_user = _iu_from_msg(peer, message_id, sender_id)
                    if input_user is None:
                        continue
                    self._inc("sender_discovery_call")
                    res = await asyncio.wait_for(
                        client(GetUsersRequest(id=[input_user])), timeout=5.0
                    )
                    users = list(getattr(res, "users", []) or [])
                    user = next(
                        (
                            u for u in users
                            if int(getattr(u, "id", 0) or 0) == sender_id
                        ),
                        None,
                    )
                    if user is None:
                        # server answered without our user — never guess
                        continue
                    if bool(getattr(user, "deleted", False)):
                        # deleted/deactivated account: no links can exist
                        self._discovery_negative[sender_id] = True
                        self._inc("sender_discovery_fail")
                        report.notes.append("discovery:deleted")
                        return None
                    uname = clean_username(getattr(user, "username", None))
                    if not is_valid_username(uname):
                        # username hidden/absent by the user's own privacy
                        # settings — an honest miss, negative-cached
                        self._discovery_negative[sender_id] = True
                        report.notes.append("discovery:no_username")
                        return None
                    # ── success: persist every artifact we legitimately own ──
                    ah = getattr(user, "access_hash", None)
                    try:
                        from sender_resolver import account_hash_store as _ahs
                        acct = monitor._account_name_for_client(client)
                        if isinstance(ah, int) and ah != 0 and acct:
                            _ahs.record(acct, sender_id, ah)
                            asyncio.create_task(
                                monitor._persist_account_hash(sender_id, ah)
                            )
                    except Exception as e:
                        report.notes.append(f"discovery:hash:{type(e).__name__}")
                    try:
                        if getattr(monitor, "db", None) is not None:
                            # COALESCE upsert — username-only, never nulls
                            await monitor.db.upsert_sender_contact(
                                {"sender_id": int(sender_id), "username": uname}
                            )
                    except Exception as e:
                        report.notes.append(f"discovery:db:{type(e).__name__}")
                    self._inc("sender_discovery_ok")
                    report.sender_strategy = "discovered_via_message"
                    report.notes.append(f"discovery:ok:{uname}")
                    logger.info(
                        f"🔎 nav discovery ok | sender={sender_id} → @{uname} "
                        f"(via source message, account view persisted)"
                    )
                    return uname
                except FloodWaitError as e:
                    self._discovery_negative[sender_id] = True
                    self._inc("sender_discovery_fail")
                    report.notes.append(
                        f"discovery:flood{int(getattr(e, 'seconds', 0) or 0)}"
                    )
                    logger.warning(
                        f"nav sender discovery FloodWait sender={sender_id}: {e.seconds}s"
                    )
                    return None
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    report.notes.append(f"discovery:{type(e).__name__}")
                    logger.debug(
                        f"nav sender discovery failed [{type(e).__name__}] sender={sender_id}"
                    )
                    continue  # next client — one failure never stops the chain
            self._discovery_negative[sender_id] = True
            self._inc("sender_discovery_fail")
            return None

    # ─────────────────────────────────────────────────────────────────────
    # CHAT + MESSAGE — C1 entity → C2 event username → C3 c/ form → C4 export
    # ─────────────────────────────────────────────────────────────────────
    async def _resolve_chat_links(
        self,
        monitor: Any,
        data: Dict[str, Any],
        chat_info: Dict[str, Any],
        event_chat_username: Any,
        send_client: Any,
        report: NavReport,
    ) -> None:
        chat_id = data.get("chat_id")
        message_id = data.get("message_id")
        entity = chat_info.get("entity")

        # C1 — the entity _chat_info already resolved with the SENDING
        # account: its username is the best group link (works for everyone).
        entity_uname = clean_username(getattr(entity, "username", None))
        if is_valid_username(entity_uname):
            self._inc("chat_entity_username")
            report.chat_strategy = "entity_username"
            chat_info["username"] = entity_uname
            links = build_canonical_links(chat_id, message_id, entity_uname)
            chat_info["group_link"] = links["group"]
            chat_info["msg_link"] = links["message"]
            report.message_strategy = "public_message"
            return  # best possible outcome — nothing else may run

        # C2 — entity resolution failed but the UPDATE itself carried the
        # username (event.chat.username). build_telegram_links ignored it —
        # we use it: it is real data from Telegram, not an invented URL.
        event_uname = clean_username(event_chat_username)
        if is_valid_username(event_uname):
            self._inc("chat_event_username")
            report.chat_strategy = "event_username"
            chat_info["username"] = event_uname
            links = build_canonical_links(chat_id, message_id, event_uname)
            chat_info["group_link"] = links["group"]
            chat_info["msg_link"] = links["message"]
            report.message_strategy = "public_message"
            # title: the frozen builder only renders the group card when a
            # title exists — recover it from the event when entity failed.
            if not chat_info.get("title") and data.get("chat_title"):
                chat_info["title"] = data["chat_title"]
            return

        # C3 — private supergroup/channel: the canonical c/ form built from
        # the real -100 chat_id. build_telegram_links already produced it —
        # validate; also surface it for the button path via chat_info.
        inner = private_supergroup_inner_id(chat_id)
        if inner and message_id:
            self._inc("chat_c_form")
            report.chat_strategy = "private_c_form"
            if chat_info.get("group_link", "#") == "#":
                chat_info["group_link"] = f"https://t.me/c/{inner}"
            if chat_info.get("msg_link", "#") == "#":
                chat_info["msg_link"] = f"https://t.me/c/{inner}/{int(message_id)}"
            report.message_strategy = "private_message"
            # fall through: C4 may still DISCOVER a public username and
            # upgrade members-only links to universally-openable ones.
            upgraded = await self._try_export_upgrade(
                monitor, chat_info, entity, int(chat_id), int(message_id),
                send_client, report,
            )
            if upgraded:
                return
            return

        # C4 (standalone) — no username, no -100 id: either a basic group
        # (Telegram has NO links for those — honest «غير متاح») or a chat
        # whose id shape we cannot trust. If an entity exists anyway, give
        # exportMessageLink one chance to produce the canonical link.
        if entity is not None and message_id:
            upgraded = await self._try_export_upgrade(
                monitor, chat_info, entity, int(chat_id or 0), int(message_id),
                send_client, report,
            )
            if upgraded:
                return
        self._inc("chat_unlinkable")
        if report.chat_strategy == "none":
            report.chat_strategy = "unlinkable"
        if report.message_strategy == "none":
            report.message_strategy = "none_available"

    # ─────────────────────────────────────────────────────────────────────
    # C4/M3 — channels.exportMessageLink (last resort, cached, single-flight)
    # ─────────────────────────────────────────────────────────────────────
    async def _try_export_upgrade(
        self,
        monitor: Any,
        chat_info: Dict[str, Any],
        entity: Any,
        chat_id: int,
        message_id: int,
        send_client: Any,
        report: NavReport,
    ) -> bool:
        """Returns True when the exported link upgraded chat_info links."""
        if not bool(getattr(CFG, "NAV_EXPORT_LINK_ENABLED", True)):
            return False
        if ExportMessageLinkRequest is None:
            return False
        if message_id <= 0:
            return False
        # chat ids are NEGATIVE for groups/channels; a positive id with no
        # entity is a DM-ish shape that exportMessageLink can never serve —
        # skip it instead of burning a doomed API call.
        if entity is None and chat_id > 0:
            return False
        if not self._export_linkable(entity):
            return False

        cached = self._export_cache.get(chat_id)
        if cached is not None:
            self._inc("chat_export_hit")
            return self._apply_exported(chat_info, cached, message_id, report, source="cache")

        if self._export_negative.get(chat_id):
            report.notes.append("export:negcached")
            return False

        client = send_client or getattr(monitor, "client", None)
        if client is None:
            return False

        # Single-flight: concurrent alerts for the same chat share one call.
        async with self._export_lock:
            cached = self._export_cache.get(chat_id)
            if cached is not None:
                self._inc("chat_export_hit")
                return self._apply_exported(chat_info, cached, message_id, report, source="cache")
            self._inc("chat_export_call")
            try:
                peer = entity
                if peer is None:
                    peer = await asyncio.wait_for(
                        client.get_input_entity(chat_id), timeout=3.0
                    )
                res = await asyncio.wait_for(
                    client(ExportMessageLinkRequest(peer, message_id, grouped=False)),
                    timeout=5.0,
                )
                raw_link = getattr(res, "link", None)
                exported = self._parse_exported_link(raw_link)
                if exported is None:
                    # Never invent — an unparseable response is discarded.
                    self._export_negative[chat_id] = True
                    self._inc("chat_export_fail")
                    report.notes.append("export:unparseable")
                    return False
                self._export_cache[chat_id] = exported
                self._inc("chat_export_ok")
                logger.info(
                    f"🔗 nav exportMessageLink ok | chat={chat_id} → "
                    f"{'public' if exported.get('username') else 'private'} link"
                )
                return self._apply_exported(chat_info, exported, message_id, report, source="api")
            except FloodWaitError as e:
                # honor Telegram's window without retrying inside it
                self._export_negative[chat_id] = True
                self._inc("chat_export_fail")
                report.notes.append(f"export:flood{int(getattr(e, 'seconds', 0) or 0)}")
                logger.warning(f"nav exportMessageLink FloodWait chat={chat_id}: {e.seconds}s")
                return False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._export_negative[chat_id] = True
                self._inc("chat_export_fail")
                report.notes.append(f"export:{type(e).__name__}")
                logger.debug(f"nav exportMessageLink failed chat={chat_id}: {type(e).__name__}")
                return False

    @staticmethod
    def _export_linkable(entity: Any) -> bool:
        """Deny-list: basic groups (Chat) genuinely cannot export links —
        their False is a Telegram limitation reported honestly, not retried.
        Everything else (Channel, InputPeer variants, unknown wrappers,
        None → one attempt via the input-entity path) is allowed; a wrong
        peer type simply fails the single API call and negative-caches."""
        if entity is None:
            return True  # unknown shape — one attempt is allowed (input-entity path)
        cls = type(entity).__name__
        return cls not in ("Chat", "InputPeerChat", "PeerChat")

    @staticmethod
    def _parse_exported_link(raw_link: Any) -> Optional[Dict[str, Any]]:
        """Validate the API response into canonical link data (or None).
        The parser from sender_resolver guarantees we accept only real
        Telegram link shapes — nothing is synthesized here. The MESSAGE
        link is deliberately NOT stored: a cached URL frozen at export
        time would point at the WRONG message later — the group form is
        cached and each alert rebuilds its own message link from it."""
        if not raw_link or not isinstance(raw_link, str):
            return None
        try:
            from sender_resolver import parse_telegram_link
            parsed = parse_telegram_link(raw_link)
        except Exception:
            return None
        if parsed.kind == "public_message" and parsed.username:
            u = parsed.username
            return {"group": f"https://t.me/{u}", "username": u}
        if parsed.kind == "private_message" and parsed.inner_id:
            return {"group": f"https://t.me/c/{parsed.inner_id}", "username": None}
        return None

    @staticmethod
    def _apply_exported(
        chat_info: Dict[str, Any],
        exported: Dict[str, Any],
        message_id: int,
        report: NavReport,
        source: str,
    ) -> bool:
        """Apply an exported link ONLY when it is an UPGRADE (requirement
        #14 — the most reliable link for the audience, not the shortest):
        a public username form beats the members-only t.me/c/ form because
        it opens for every recipient of the target channel, while the c/
        form opens only for members of the source group."""
        group = exported.get("group")
        if not is_valid_tme_url(group):
            return False
        is_public = bool(exported.get("username"))
        current_group = chat_info.get("group_link", "#")
        current_msg = chat_info.get("msg_link", "#")
        better = current_group == "#" or (is_public and current_group.startswith("https://t.me/c/"))
        upgraded = False
        if better:
            chat_info["group_link"] = group
            upgraded = True
        # message link rebuilt for THIS alert's message_id from the cached
        # canonical group form — same shape the API returned for that chat
        msg = f"{group}/{int(message_id)}" if message_id else "#"
        if is_valid_tme_url(msg):
            better_msg = current_msg == "#" or (is_public and current_msg.startswith("https://t.me/c/"))
            if better_msg:
                chat_info["msg_link"] = msg
                upgraded = True
        if is_public and not chat_info.get("username"):
            chat_info["username"] = exported["username"]
            upgraded = True
        if upgraded:
            report.chat_strategy = f"export_link({source})"
            report.message_strategy = f"export_message({source})"
        return upgraded

    # ─────────────────────────────────────────────────────────────────────
    # VERIFY — final gate before the alert is built (requirement #15)
    # ─────────────────────────────────────────────────────────────────────
    def verify_navigation(
        self,
        chat_info: Dict[str, Any],
        sender_username: Optional[str],
        sender_id: Any = 0,
    ) -> NavReport:
        report = NavReport(
            sender_strategy="none", chat_strategy="none", message_strategy="none"
        )
        # sender: a username we could not validate must not poison the anchor
        if sender_username is not None and not is_valid_username(sender_username):
            report.verified_ok = False
            report.sender_strategy = "invalid_username_stripped"
            report.notes.append("verify:sender_username")
            sender_username = None
        else:
            report.sender_strategy = "username" if sender_username else (
                "id_anchor" if sender_id else "none"
            )
        # chat/message: only https://t.me shapes survive; anything malformed
        # degrades to the frozen «الرابط غير متاح» path (never fixed silently
        # with invented data)
        if not is_valid_tme_url(chat_info.get("group_link", "#")):
            if chat_info.get("group_link", "#") != "#":
                report.verified_ok = False
                report.notes.append("verify:group_link")
            chat_info["group_link"] = "#"
        else:
            report.chat_strategy = "linked"
        if not is_valid_tme_url(chat_info.get("msg_link", "#")):
            if chat_info.get("msg_link", "#") != "#":
                report.verified_ok = False
                report.notes.append("verify:msg_link")
            chat_info["msg_link"] = "#"
        else:
            report.message_strategy = "linked"
        if not report.verified_ok:
            self._inc("verify_degraded")
        return report

    # ─────────────────────────────────────────────────────────────────────
    # PUBLIC ENTRY POINT — one call from _send_alert; never raises
    # ─────────────────────────────────────────────────────────────────────
    async def enrich_alert_navigation(
        self,
        *,
        monitor: Any,
        data: Dict[str, Any],
        chat_info: Dict[str, Any],
        event_chat_username: Any,
        send_client: Any = None,
    ) -> Optional[str]:
        """Resolve + verify everything the three actions need, mutate the
        caller's chat_info in place (same keys, better values), and return
        the best sender username (or None). Every strategy failure is
        isolated; the chain always completes; the verify gate always runs."""
        report = NavReport()
        sender_username: Optional[str] = None
        try:
            sender_username = await self._resolve_sender_username(
                monitor, data, report, send_client
            )
        except Exception as e:  # one strategy failing must never stop the rest
            report.notes.append(f"sender:{type(e).__name__}")
            logger.debug(f"nav sender stage error: {type(e).__name__}: {e}")
        try:
            await self._resolve_chat_links(
                monitor, data, chat_info, event_chat_username, send_client, report
            )
        except Exception as e:
            report.notes.append(f"chat:{type(e).__name__}")
            logger.debug(f"nav chat stage error: {type(e).__name__}: {e}")
        try:
            vreport = self.verify_navigation(chat_info, sender_username, data.get("sender_id") or 0)
            report.verified_ok = vreport.verified_ok
            report.notes.extend(n for n in vreport.notes if n.startswith("verify:"))
            if vreport.verified_ok:
                report.chat_strategy = report.chat_strategy or vreport.chat_strategy
                report.message_strategy = report.message_strategy or vreport.message_strategy
        except Exception as e:
            report.verified_ok = False
            report.notes.append(f"verify:{type(e).__name__}")
        logger.info(f"🧭 nav | {report.as_line()} | chat={data.get('chat_id')} msg={data.get('message_id')}")
        return sender_username

    def snapshot(self) -> Dict[str, int]:
        """Bounded in-memory metrics (logs/diagnostics)."""
        try:
            return {
                **self._metrics,
                "export_cache_size": len(self._export_cache),
                "export_negative_size": len(self._export_negative),
                "discovery_negative_size": len(self._discovery_negative),
            }
        except Exception:
            return {}


# Module singleton — shared by all account monitors (same pattern as
# sender_intel / account_hash_store).
nav_resolver = NavigationResolver()
