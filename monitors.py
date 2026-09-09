#!/usr/bin/env python3
"""
monitors.py – Account Monitor v9.10.0 (STABLE 24/7 EDITION, HARDENED + FAST CAPTURE + SENDER INTELLIGENCE + CROSS-ACCOUNT DEDUP + DYNAMIC BUTTONS)
- إصلاح تدوير الجلسة (إعادة تسجيل المعالج)
- تحسين إعادة الاتصال واكتشاف العميل الميت
- دعم كامل لـ IntentEngine

v9.7 (this pass) — targeted reliability fixes, monitors.py ONLY:
  * DeadLetterQueue._retry now genuinely re-submits failed events through the
    existing processing pipeline instead of sleeping and discarding them.
  * FloodWaitError is now honored using the exact Telegram-provided wait
    duration, gated per-account via the existing CircuitBreaker (no global
    lock, no guessed durations, no stacking).
  * Media events no longer run their full pipeline synchronously inside the
    Telethon event handler; they are offloaded to a tracked background task
    bounded by the existing pipeline semaphore, so the handler stays light
    and subsequent events for the same account are not blocked.
  * _rotate_session now reuses the exact same TelegramClient configuration
    as connect() (timeout / retries / auto_reconnect / device metadata),
    connects+authorizes the new client before tearing down the old one, and
    is guarded by the existing connection lock to avoid overlapping clients.
  * All background tasks created by this module (DLQ retries, media
    offloading) are tracked in bounded sets, have their exceptions captured
    via done-callbacks, and are cancelled/awaited cleanly on disconnect().
  * Previously silent `except Exception: pass` blocks now log at DEBUG/
    WARNING without ever logging session strings, API hashes, or tokens.

v9.7.1 (this fix) — alert gating fix:
  * Alerts are now sent ONLY when the filter's final decision is "accept".
    Previously `_analyze_and_alert` used `analysis.get("valid")`, which could
    be True even for "review" decisions, causing false alerts for generic
    short messages like "ابي" or "بنات". Now we explicitly check
    `decision == "accept"` in addition to `valid`.

v9.8 (this fix) — Fast Capture: protection against deletion bots:
  * المبدأ: "احفظ أولاً، حلل ثانياً". Admin deletion bots can remove a group
    message within milliseconds of it being posted. If our own processing
    (queue handoff, DB writes, filter analysis, any later re-fetch) is ever
    interrupted, the text would be gone. FastCaptureBuffer now persists the
    raw text SYNCHRONOUSLY (zero awaits) as the first pipeline step after
    the cheap sync early-return checks, keyed by (chat_id, msg_id) with a
    TTL + maxsize cap.
  * A MessageDeleted handler claims each deletion notice exactly once
    (shared buffer across all account clients), logs the captured text for
    diagnostics, and — if the message was never handed to the normal
    pipeline (handler crash between capture and add_to_queue) — re-submits
    a rebuilt event through the standard pipeline, where the existing
    message_hash dedup guarantees no duplicate alerts.
  * Fallback at alert time: if the pipeline's copy of the text is empty but
    the capture holds it, the captured text is used (length-bounded).
  * Gated by CFG.FAST_CAPTURE_ENABLED (env FAST_CAPTURE_ENABLED, default
    false; production rollout sets true in Render env — documented kill
    switch). Buffer sizing via CAPTURE_BUFFER_SIZE / CAPTURE_TTL_SECONDS.
  * /health (dashboard + fallback server) exposes a fast_capture snapshot
    (enabled/size/saved/deleted_captured/deleted_recovered/...).

v9.9 (this pass) — Sender Intelligence / Sender Contact Resolver:
  * Backend-ONLY enrichment layer (sender_resolver.py). 🚨 The alert
    contract is untouched: same text, same field order, same emojis, same
    HTML, same buttons, same links. _build_alert()/send paths are unchanged;
    they simply receive richer sender data through the same keys.
  * FastCaptureBuffer entries now also carry the full sender snapshot
    (username / first_name / last_name / access_hash + metadata flags)
    captured synchronously from event.sender at arrival (requirement #9:
    if NewMessage → deleted → recovery, the sender data must survive).
  * _event_to_dict() keeps every existing key with the same values and
    ADDS sender_* metadata keys (additive, backward compatible). When
    event.sender is missing, one deduplicated resolver lookup fills it
    AFTER the fast capture has already protected the text.
  * Recovery/DLQ rebuilt events now reuse the captured sender fields and
    fall back to sender_contacts in the DB — they NEVER blank sender data
    to None when the original values exist (requirement #10).
  * DB upsert policy: new non-null value → update; new null value → keep
    the stored value (COALESCE) for identity fields (requirement #24).
  * Resolver: entity cache (TTL) + in-flight dedup + exponential backoff
    with jitter for transient errors + FloodWait honored with the exact
    Telegram seconds + precise internal failure reasons (logs/DB only).
  * Gated by CFG.SENDER_INTEL_ENABLED (env SENDER_INTEL_ENABLED, default
    true; kill switch for instant disable without code changes).

v9.10 (this pass) — Cross-Account Dedup + Dynamic Alert Buttons:
  * منع تكرار التنبيهات بالكامل (dedup.py):
      - نفس المرسل بنفس النص = تنبيه واحد فقط — حتى لو التُقطت عبر الحسابات
        الستة أو أُعيد إرسالها برسالة/مجموعة جديدة. البصمة =
        fast_hash(sender_id + normalized_text) — مرسلان مختلفان بنفس النص
        يمران (طلبان حقيقيان)، والمتشابهة جزئياً تبقى مختلفة.
      - طبقتان: ذاكرة LRU (فحص فوري O(1)، آتومي داخل الـ event loop) +
        جدول alert_dedup في DB (INSERT claim آتومي — ينجو من إعادة
        التشغيل ويمنع السباق بين الـ workers).
      - الحجز قبل الإرسال وفكّه عند كل فشل (FloodWait/استثناء/fallback
        فاشل) حتى لا تعيد محاولة DLQ الإرسال فتُمنع ظلماً.
      - المكررات تُحسب في stats["duplicates"] ولا تستهلك rate-limiter.
      - فشل-آمن: خطأ DB → الحماية بالذاكرة فقط والتنبيه الأول يمر.
      - DEDUP_ENABLED / DEDUP_WINDOW_SECONDS قابلان للتعديل حياً من اللوحة
        (تُقرأ من CFG عند كل claim — تطبيق فوري بدون إعادة تشغيل).
  * الأزرار الديناميكية أسفل كل تنبيه (كود المستخدم المدمج حرفياً):
      - build_dynamic_buttons(): [ 💬 مراسلة ] [ 📨 عرض الرسالة ] في صف
        واحد. مراسلة: t.me/{username} أو tg://openmessage?user_id=؛
        عرض الرسالة: t.me/{chat}/{msg} عامة أو t.me/c/{inner}/{msg} خاصة.
      - الزر الذي تفتقر بياناته لا يُعرض إطلاقاً (لا أزرار مكسورة)، وزر
        📋 نسخ النص يبقى مضافاً لنفس الصف عند ALERT_WITH_COPY_BUTTON.
      - زر "👤 فتح الحساب" القديم حُذف (استُوعب بالكامل في "💬 مراسلة"
        الذي يغطي نفس الحالتين بشكل أدق). نص التنبيه HTML نفسه لم يتغير.
  * المسار الكامل: Event → Extract → msg_hash dedup (DB) → فلترة →
    rate-limit → **content dedup (هنا)** → بناء التنبيه + الأزرار → إرسال.

See the accompanying engineering report for the full list of changes,
the retry_count propagation fix (process_event_from_queue / _send_alert),
and documented FOLLOW-UP items for other files.
"""
from __future__ import annotations
import asyncio
import os
import secrets
import time
from collections import defaultdict, OrderedDict
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Set
from cachetools import LRUCache, TTLCache
from loguru import logger
from telethon import TelegramClient, events, Button
from telethon.errors import (
    FloodWaitError,
    SessionPasswordNeededError,
    AuthKeyDuplicatedError,
)
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaWebPage,
    InputPeerChannel,
)
from config import CFG, InputSanitizer, fast_hash
from database import EnhancedDatabase, MessageRecord, AlertRecord, DeadLetterRecord
from filter_engine import EnhancedFilter
from dedup import content_fingerprint, get_deduplicator  # v9.10 cross-account dedup
from sender_resolver import (
    extract_flat as _sender_extract_flat,
    extract_sender as _sender_extract,
    meta_to_contact_fields as _sender_meta_to_contact,
    sender_intel,
)


# (نفس الدوال المساعدة من النسخة الأصلية: resolve_chat_entity, build_telegram_links)
# v9.10: get_dedup_snapshot مُصدَّر من هنا للوحة/health عبر dedup.get_dedup_snapshot
async def resolve_chat_entity(client: TelegramClient, data: Dict[str, Any]) -> Any:
    username = data.get("username") or data.get("sender_username") or data.get("chat_username")
    if username:
        try:
            return await client.get_entity(username)
        except Exception as e:
            logger.debug(f"resolve_chat_entity: get_entity(username) failed: {e}")
    access_hash = data.get("chat_access_hash") or data.get("access_hash")
    chat_id = data.get("chat_id")
    if access_hash and chat_id and str(chat_id).startswith("-100"):
        try:
            inner_id = int(str(chat_id)[4:])
            peer = InputPeerChannel(channel_id=inner_id, access_hash=access_hash)
            return await client.get_entity(peer)
        except Exception as e:
            logger.debug(f"resolve_chat_entity: get_entity(peer) failed: {e}")
    if chat_id:
        try:
            async for dialog in client.iter_dialogs(limit=200):
                if dialog.id == chat_id:
                    return dialog.entity
        except Exception as e:
            logger.debug(f"resolve_chat_entity: iter_dialogs failed: {e}")
        try:
            return await client.get_entity(chat_id)
        except Exception as e:
            logger.debug(f"resolve_chat_entity: get_entity(chat_id) failed: {e}")
    return None


def build_telegram_links(chat_id: int, message_id: int, username: str = None) -> Dict[str, str]:
    links = {"group": "#", "message": "#"}
    if username:
        clean = username.lstrip('@')
        links["group"] = f"https://t.me/{clean}"
        links["message"] = f"https://t.me/{clean}/{message_id}"
        return links
    if str(chat_id).startswith("-100"):
        inner = str(chat_id)[4:]
        links["group"] = f"https://t.me/c/{inner}"
        links["message"] = f"https://t.me/c/{inner}/{message_id}"
    return links


# ============================================================================
# v9.10 — Dynamic alert buttons (طلب المستخدم — كود مدمج حرفياً)
#
# زرّان ديناميكيان في صف واحد أسفل كل تنبيه:
#   [ 💬 مراسلة ]  [ 📨 عرض الرسالة ]
#
# القواعد:
#   * مرسل لديه Username      → 💬 مراسلة → https://t.me/{username}
#   * مرسل بدون Username      → 💬 مراسلة → tg://openmessage?user_id={id}
#   * مجموعة عامة             → 📨 عرض الرسالة → https://t.me/{chat}/{msg_id}
#   * مجموعة خاصة             → 📨 عرض الرسالة → https://t.me/c/{inner}/{msg_id}
#   * بيانات ناقصة            → الزر المكسور لا يُعرض إطلاقاً (لا أزرار مكسورة)
# ============================================================================
def build_dynamic_buttons(sender: dict, chat: dict) -> list | None:
    row = []

    sender_id = sender.get("id")
    username = (sender.get("username") or "").strip().lstrip("@")

    # زر مراسلة
    if username:
        contact_url = f"https://t.me/{username}"
    elif sender_id:
        contact_url = f"tg://openmessage?user_id={sender_id}"
    else:
        contact_url = None

    if contact_url:
        row.append(Button.url("💬 مراسلة", contact_url))

    # زر عرض الرسالة
    chat_id = chat.get("id")
    message_id = chat.get("message_id")
    chat_uname = (chat.get("username") or "").strip().lstrip("@")

    msg_url = None

    if chat_uname and message_id:
        msg_url = f"https://t.me/{chat_uname}/{message_id}"
    elif chat_id and message_id:
        inner = str(chat_id).replace("-100", "", 1)
        msg_url = f"https://t.me/c/{inner}/{message_id}"

    if msg_url:
        row.append(Button.url("📨 عرض الرسالة", msg_url))

    return [row] if row else None


# =============================================================================
# FastCaptureBuffer — save-first protection against deletion bots (v9.8)
# المبدأ: "احفظ أولاً، حلل ثانياً"
# =============================================================================
class FastCaptureBuffer:
    """
    In-memory save-first buffer keyed by (chat_id, msg_id).

    When a monitored group receives a message, an admin deletion bot can
    remove it within tens of milliseconds. Whatever happens afterwards in
    our pipeline (queue handoff, DB writes, filter analysis, any later
    re-fetch), the raw text must already be safe in RAM. That is exactly
    what this buffer guarantees: `save()` is called SYNCHRONOUSLY as the
    first step of the NewMessage handler — after the cheap sync
    early-return checks, before the FIRST await anywhere.

    Deliberate deviations from the reference design (and why):
      * save()/get() are synchronous. An ``async def save`` that awaits an
        asyncio.Lock would introduce a yield point between handler start
        and text capture — the exact race this class exists to close. The
        single asyncio event loop makes plain dict access atomic; no lock
        is needed.
      * TTL sweeping is lazy (amortized on save, at most one sweep per
        _GC_EVERY seconds) instead of a background cleanup task — nothing
        extra to track and cancel on disconnect (v9.7 discipline: every
        background task must be tracked).
      * The instance is a module-level singleton shared by all account
        monitors: the same group message seen by several accounts lands on
        the same key, and MessageDeleted notices arriving on multiple
        clients are deduplicated by claim_deleted().

    Lifecycle flags per entry:
      queued          — the event was handed to the DB queue / DLQ / media pipeline
      alerted         — the alert for this message was successfully sent
      deleted_claimed — a MessageDeleted notice for it was already claimed once
      recovered       — it was re-submitted through the pipeline by the
                        MessageDeleted recovery path
    """

    __slots__ = ("_buf", "_maxsize", "_ttl", "_last_gc", "_stats")

    _GC_EVERY = 5.0  # seconds between lazy TTL sweeps

    def __init__(self, maxsize: int = 1000, ttl: int = 30):
        self._buf: "OrderedDict[Tuple[int, int], Dict[str, Any]]" = OrderedDict()
        self._maxsize = max(1, int(maxsize))
        self._ttl = max(1, int(ttl))
        self._last_gc: float = 0.0
        self._stats: Dict[str, int] = defaultdict(int)

    @property
    def enabled(self) -> bool:
        return bool(getattr(CFG, "FAST_CAPTURE_ENABLED", False))

    # ── core (SYNC — never await, never raise) ────────────────────────────
    def save(
        self,
        chat_id: Optional[int],
        msg_id: int,
        text: str,
        sender_id: int = 0,
        sender_name: str = "",
        account: str = "",
        sender_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = time.time()
        self._gc(now)
        key = (int(chat_id or 0), int(msg_id or 0))
        prev = self._buf.get(key)
        meta = sender_meta or {}
        if prev is not None:
            # Same message delivered via several account clients: refresh
            # content but keep the FIRST-seen ts (TTL = time since arrival).
            prev["text"] = text
            if sender_id:
                prev["sender_id"] = sender_id
            if sender_name:
                prev["sender_name"] = sender_name
            # v9.9 sender-intel merge — destructive-NULL-free (brief #24):
            # a later duplicate delivery may only ADD or REFRESH non-null
            # sender values; it can never blank an already-captured field.
            for f in ("sender_username", "sender_first_name", "sender_last_name"):
                v = meta.get(f)
                if v:
                    prev[f] = v
            if meta.get("sender_access_hash") is not None:
                prev["sender_access_hash"] = meta["sender_access_hash"]
            if meta.get("sender_meta_full") is not None:
                prev["sender_meta_full"] = meta["sender_meta_full"]
            self._stats["saved"] += 1
            return
        self._buf[key] = {
            "text": text,
            "sender_id": int(sender_id or 0),
            "sender_name": sender_name,
            "sender_username": meta.get("sender_username"),
            "sender_first_name": meta.get("sender_first_name"),
            "sender_last_name": meta.get("sender_last_name"),
            "sender_access_hash": meta.get("sender_access_hash"),
            "sender_meta_full": meta.get("sender_meta_full"),
            "chat_id": key[0],
            "msg_id": key[1],
            "ts": now,
            "account": account,
            "queued": False,
            "alerted": False,
            "deleted_claimed": False,
            "recovered": False,
        }
        self._stats["saved"] += 1
        while len(self._buf) > self._maxsize:
            self._buf.popitem(last=False)
            self._stats["evicted"] += 1

    def get(self, chat_id: Optional[int], msg_id: int) -> Optional[Dict[str, Any]]:
        key = (int(chat_id or 0), int(msg_id or 0))
        entry = self._buf.get(key)
        if entry is None:
            return None
        if time.time() - entry["ts"] > self._ttl:
            del self._buf[key]
            self._stats["expired"] += 1
            return None
        return entry

    def find_by_msg_id(self, msg_id: int) -> Optional[Dict[str, Any]]:
        """Fallback lookup for MessageDeleted updates that carry no chat_id
        (private chats / small groups): scan by msg_id only."""
        msg_id = int(msg_id or 0)
        for key, entry in self._buf.items():
            if entry["msg_id"] == msg_id:
                if time.time() - entry["ts"] > self._ttl:
                    del self._buf[key]
                    self._stats["expired"] += 1
                    return None
                return entry
        return None

    def lookup(self, chat_id: Optional[int], msg_id: int) -> Optional[Dict[str, Any]]:
        if chat_id is not None:
            return self.get(chat_id, msg_id)
        return self.find_by_msg_id(msg_id)

    def mark_queued(self, chat_id: Optional[int], msg_id: int) -> None:
        entry = self.lookup(chat_id, msg_id)
        if entry is not None:
            entry["queued"] = True

    def mark_alerted(self, chat_id: Optional[int], msg_id: int) -> None:
        entry = self.lookup(chat_id, msg_id)
        if entry is not None:
            entry["alerted"] = True

    def claim_deleted(self, chat_id: Optional[int], msg_id: int) -> Optional[Dict[str, Any]]:
        """Exactly-once claim of a MessageDeleted notice. All account
        clients share this buffer, so the same deletion arriving on several
        connections is logged/recovered once."""
        entry = self.lookup(chat_id, msg_id)
        if entry is None or entry.get("deleted_claimed"):
            return None
        entry["deleted_claimed"] = True
        return entry

    def inc(self, name: str, delta: int = 1) -> None:
        self._stats[name] += delta

    def _gc(self, now: float) -> None:
        if now - self._last_gc < self._GC_EVERY:
            return
        self._last_gc = now
        expired = [k for k, v in self._buf.items() if now - v["ts"] > self._ttl]
        for k in expired:
            del self._buf[k]
            self._stats["expired"] += 1

    # ── introspection ─────────────────────────────────────────────────────
    def size(self) -> int:
        return len(self._buf)

    def stats(self) -> Dict[str, int]:
        return dict(self._stats)

    def clear(self) -> None:
        self._buf.clear()


_capture = FastCaptureBuffer(
    maxsize=getattr(CFG, "CAPTURE_BUFFER_SIZE", 1000),
    ttl=getattr(CFG, "CAPTURE_TTL_SECONDS", 30),
)
logger.info(
    f"Fast Capture Buffer initialized | enabled={_capture.enabled} | "
    f"maxsize={_capture._maxsize} | ttl={_capture._ttl}s"
)


def get_capture_snapshot() -> Dict[str, Any]:
    """Read-only snapshot for /health and dashboard diagnostics.

    Standard counter keys are ALWAYS present (zero-filled) so the health
    contract is stable for dashboards/alerting."""
    try:
        base = {
            "saved": 0, "expired": 0, "evicted": 0,
            "deleted_captured": 0, "deleted_recovered": 0, "fallback_used": 0,
        }
        return {
            "enabled": _capture.enabled,
            "size": _capture.size(),
            **{**base, **_capture.stats()},
        }
    except Exception:
        return {"enabled": False, "size": 0}


# =============================================================================
# CircuitBreaker — now with an account-scoped FloodWait gate
# =============================================================================
class CircuitState:
    CLOSED = "closed"; OPEN = "open"; HALF_OPEN = "half_open"


class CircuitBreakerOpen(Exception): pass


class CircuitBreaker:
    """
    One instance per account/purpose (e.g. send_cb / entity_cb per monitor),
    so FloodWait state recorded here is inherently account-scoped: Account A
    hitting FloodWait only ever affects Account A's own breaker instance.
    """

    def __init__(self, name: str, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        self.name = name; self._threshold = failure_threshold; self._recovery = recovery_timeout
        self._failures = 0; self._successes = 0; self._last_fail: Optional[float] = None
        self._state = CircuitState.CLOSED; self._half_open_reqs = 0; self._lock = asyncio.Lock()
        # FloodWait gate: only ever set from a real FloodWaitError.seconds value.
        self._flood_wait_until: Optional[float] = None
        self._flood_seconds_last: float = 0.0

    async def call(self, coro_factory):
        await self._check()
        try:
            result = await coro_factory(); await self._success(); return result
        except FloodWaitError as e:
            await self._flood_failure(getattr(e, "seconds", 0) or 0)
            raise
        except Exception:
            await self._failure()
            raise

    async def _check(self):
        async with self._lock:
            now = time.monotonic()
            if self._flood_wait_until is not None:
                if now < self._flood_wait_until:
                    remaining = self._flood_wait_until - now
                    raise CircuitBreakerOpen(
                        f"'{self.name}' FLOOD_WAIT active, {remaining:.1f}s remaining"
                    )
                logger.info(f"Circuit '{self.name}' FloodWait window expired, entering HALF_OPEN")
                self._flood_wait_until = None
                self._state = CircuitState.HALF_OPEN
                self._half_open_reqs = 0
            if self._state == CircuitState.OPEN:
                if now - (self._last_fail or 0) > self._recovery:
                    self._state = CircuitState.HALF_OPEN; self._half_open_reqs = 0
                    logger.info(f"Circuit '{self.name}' HALF_OPEN")
                else:
                    raise CircuitBreakerOpen(f"'{self.name}' OPEN")
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_reqs += 1
                if self._half_open_reqs > 2:
                    raise CircuitBreakerOpen(f"'{self.name}' HALF_OPEN limit")

    async def _success(self):
        async with self._lock:
            self._successes += 1
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED; self._failures = 0; self._half_open_reqs = 0

    async def _failure(self):
        async with self._lock:
            self._failures += 1; self._last_fail = time.monotonic()
            if self._state == CircuitState.HALF_OPEN: self._state = CircuitState.OPEN
            elif self._state == CircuitState.CLOSED and self._failures >= self._threshold:
                self._state = CircuitState.OPEN; logger.warning(f"Circuit '{self.name}' OPEN ({self._failures} failures)")

    async def _flood_failure(self, seconds: float):
        """
        Records the *exact* Telegram-provided wait duration. Never guesses,
        never reuses the generic `_recovery` backoff. If another concurrent
        caller already recorded a flood window, only extend it (never
        shorten / never stack additively) so repeated FloodWait hits for the
        same account don't compound into an ever-growing sleep.
        """
        async with self._lock:
            self._failures += 1
            self._last_fail = time.monotonic()
            wait = max(0.0, float(seconds))
            candidate_until = time.monotonic() + wait
            if self._flood_wait_until is None or candidate_until > self._flood_wait_until:
                self._flood_wait_until = candidate_until
            self._flood_seconds_last = wait
            self._state = CircuitState.OPEN
            logger.warning(
                f"FloodWait on circuit '{self.name}': must wait {wait:.0f}s before next attempt"
            )

    @property
    def state(self): return self._state
    @property
    def failures(self): return self._failures
    @property
    def flood_wait_remaining(self) -> float:
        if self._flood_wait_until is None:
            return 0.0
        return max(0.0, self._flood_wait_until - time.monotonic())


# =============================================================================
# DeadLetterQueue — genuine retry, reusing the existing processing pipeline
# =============================================================================
class DeadLetterQueue:
    """
    Persists permanently-failed events and retries recoverable ones by
    re-submitting them through the existing processing pipeline via a
    caller-supplied reprocess callback (EnhancedAccountMonitor wires this to
    its own process_event_from_queue / alert re-send logic). This
    deliberately reuses the existing pipeline instead of duplicating
    filter/database/alert logic here.
    """

    def __init__(
        self,
        db: EnhancedDatabase,
        max_retries: int = 3,
        reprocess_callback: Optional[Callable[[Dict[str, Any], int], Awaitable[None]]] = None,
        account_name: str = "unknown",
    ):
        self.db = db
        self.max_retries = max_retries
        self._reprocess = reprocess_callback
        self._account_name = account_name
        self._lock = asyncio.Lock()
        self._retry_tasks: Set[asyncio.Task] = set()
        self._task_lock = asyncio.Lock()
        self._stats: Dict[str, int] = defaultdict(int)
        self._stopping = False
        # Prevents scheduling a second concurrent retry for the same
        # chat/message while one is already pending (dup-scheduling guard).
        self._scheduled_keys: Set[str] = set()

    @staticmethod
    def _event_key(event_data: Dict[str, Any]) -> str:
        chat_id = event_data.get("chat_id")
        message_id = event_data.get("message_id")
        return f"{chat_id}:{message_id}"

    async def push(self, event_data: Dict[str, Any], error: Exception, retry_count: int = 0) -> None:
        if self._stopping:
            # Never schedule new background work during shutdown — persist
            # directly so the event is not silently lost.
            await self._persist_dead_letter(event_data, error, retry_count)
            return

        if retry_count >= self.max_retries:
            await self._persist_dead_letter(event_data, error, retry_count)
            return

        key = self._event_key(event_data)
        async with self._lock:
            if key in self._scheduled_keys:
                logger.debug(
                    f"DLQ[{self._account_name}]: retry already scheduled for {key}, skipping duplicate push"
                )
                return
            self._scheduled_keys.add(key)

        delay = min(2 ** retry_count, 60)
        logger.warning(
            f"DLQ[{self._account_name}]: scheduling retry {retry_count + 1}/{self.max_retries} "
            f"for event {key} in {delay}s (reason: {type(error).__name__}: {str(error)[:150]})"
        )
        self._stats["retry_scheduled"] += 1

        task = asyncio.create_task(
            self._retry(event_data, retry_count + 1, delay, key),
            name=f"dlq_retry_{self._account_name}_{key}_{retry_count + 1}",
        )
        async with self._task_lock:
            self._retry_tasks.add(task)
        task.add_done_callback(self._on_retry_task_done)

    def _on_retry_task_done(self, task: asyncio.Task) -> None:
        self._retry_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # Guarantees no "Task exception was never retrieved" warnings.
            logger.error(f"DLQ[{self._account_name}]: unhandled exception in retry task: {exc}")

    async def _retry(self, event_data: Dict[str, Any], retry_count: int, delay: float, key: str) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            async with self._lock:
                self._scheduled_keys.discard(key)
            raise

        async with self._lock:
            self._scheduled_keys.discard(key)

        self._stats["retry_started"] += 1
        logger.info(
            f"DLQ[{self._account_name}]: retrying event {key} (attempt {retry_count}/{self.max_retries})"
        )

        if self._reprocess is None:
            logger.error(
                f"DLQ[{self._account_name}]: no reprocess callback configured, "
                f"persisting event {key} as dead letter"
            )
            await self._persist_dead_letter(event_data, RuntimeError("no reprocess callback"), retry_count)
            return

        try:
            # The reprocess callback (process_event_from_queue / alert
            # re-send) is responsible for calling push() again on failure
            # with the *current* retry_count — we deliberately do not call
            # push() again here to avoid double-scheduling the same event.
            await self._reprocess(event_data, retry_count)
            self._stats["retry_succeeded"] += 1
            logger.info(f"DLQ[{self._account_name}]: retry succeeded for event {key}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._stats["retry_failed"] += 1
            logger.warning(
                f"DLQ[{self._account_name}]: retry {retry_count}/{self.max_retries} "
                f"failed for event {key}: {type(e).__name__}: {str(e)[:150]}"
            )
            # Safety net: if the reprocess callback raised without itself
            # calling push() (unexpected path), ensure we never lose the
            # event once max_retries is reached.
            if retry_count >= self.max_retries:
                await self._persist_dead_letter(event_data, e, retry_count)

    async def _persist_dead_letter(self, event_data: Dict[str, Any], error: Exception, retry_count: int) -> None:
        try:
            await self.db.add_dead_letter(
                DeadLetterRecord(
                    event_data=event_data,
                    error_text=f"{type(error).__name__}: {str(error)[:300]}",
                    retry_count=retry_count,
                    resolved=False,
                    timestamp=time.time(),
                )
            )
            self._stats["dead_lettered"] += 1
            logger.error(
                f"DLQ[{self._account_name}]: event {self._event_key(event_data)} "
                f"moved to dead-letter storage after {retry_count} attempt(s)"
            )
        except Exception as e:
            # Last line of defense: even if DB persistence fails, log the
            # event key at CRITICAL so it is never silently unaccounted for.
            logger.critical(
                f"DLQ[{self._account_name}]: FAILED to persist dead letter "
                f"(event may be lost): {e} | event_key={self._event_key(event_data)}"
            )

    async def get_stats(self) -> Dict[str, int]:
        return dict(self._stats)

    async def stop(self) -> None:
        self._stopping = True
        async with self._task_lock:
            tasks = list(self._retry_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"DLQ[{self._account_name}]: error awaiting cancelled retry task: {e}")
        async with self._task_lock:
            self._retry_tasks.clear()
        async with self._lock:
            self._scheduled_keys.clear()


class ReconnectionManager:
    def __init__(self, monitor: "EnhancedAccountMonitor"):
        self._mon = monitor; self._backoff = 5.0; self._task: Optional[asyncio.Task] = None
        self._running = False; self._shutdown_event = asyncio.Event()
        self._last_error: Optional[str] = None; self._consecutive_failures = 0
        self._max_consecutive_failures = 10
    async def start(self):
        if self._running: return
        self._running = True; self._shutdown_event.clear()
        self._task = asyncio.create_task(self._loop(), name=f"reconnect_{self._mon.account['name']}")
    async def stop(self):
        self._running = False; self._shutdown_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
    async def reset(self):
        self._backoff = 5.0; self._consecutive_failures = 0; self._last_error = None
    async def _loop(self):
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(CFG.RECONNECT_CHECK_INTERVAL)
                if not self._mon.is_connected:
                    # Respect an active connect-time FloodWait before doing
                    # anything else — never hammer reconnects during it.
                    now_ts = time.time()
                    if self._mon._connect_flood_until and now_ts < self._mon._connect_flood_until:
                        remaining = self._mon._connect_flood_until - now_ts
                        logger.debug(
                            f"Reconnect paused for {self._mon.account['name']} - "
                            f"FloodWait {remaining:.0f}s remaining"
                        )
                        continue
                    if self._mon._connect_flood_until and now_ts >= self._mon._connect_flood_until:
                        self._mon._connect_flood_until = 0.0
                    last_error = self._mon._last_connect_error or ""
                    if any(x in last_error for x in ["AuthKeyDuplicatedError", "SessionPasswordNeededError"]):
                        logger.error(f"Unrecoverable error for {self._mon.account['name']}, suspending retries")
                        self._mon._last_connect_error = f"Unrecoverable: {last_error[:100]}"
                        await asyncio.sleep(3600); continue
                    self._consecutive_failures += 1
                    if self._consecutive_failures > self._max_consecutive_failures:
                        logger.error(f"Too many failures for {self._mon.account['name']}, marking as dead")
                        self._mon.is_connected = False; self._mon._last_connect_error = "Too many failures"
                        await asyncio.sleep(300); self._consecutive_failures = 0; continue
                    jitter = secrets.randbelow(3000) / 1000
                    sleep = min(self._backoff + jitter, CFG.MAX_RECONNECT_BACKOFF)
                    logger.info(f"Reconnecting {self._mon.account['name']} in {sleep:.1f}s (attempt {self._consecutive_failures})")
                    await asyncio.sleep(sleep)
                    ok = await self._mon.connect()
                    if ok: await self.reset(); logger.info(f"Reconnected: {self._mon.account['name']}")
                    else: self._backoff = min(self._backoff * CFG.RETRY_BACKOFF, CFG.MAX_RECONNECT_BACKOFF)
                else:
                    if self._mon.client:
                        try:
                            await asyncio.wait_for(self._mon.client.get_me(), timeout=15)
                            self._consecutive_failures = 0
                            # التحقق من وجود المعالج (إصلاح تدوير الجلسة)
                            if self._mon._handler_func is None:
                                logger.warning(f"Handler missing for {self._mon.account['name']}, re-registering...")
                                await self._mon._register_handler()
                        except Exception as e:
                            logger.warning(f"Heartbeat failed for {self._mon.account['name']}: {e}")
                            self._mon.is_connected = False; self._last_error = str(e)
            except asyncio.CancelledError: break
            except Exception as e:
                logger.error(f"Reconnect loop error [{self._mon.account['name']}]: {e}")
                await asyncio.sleep(min(self._backoff, 30))


class EnhancedAccountMonitor:
    def __init__(self, account: Dict[str, Any], db: EnhancedDatabase, flt: EnhancedFilter, main_client: Optional[TelegramClient] = None):
        self.account = account; self.db = db; self.filter = flt; self.main_client = main_client
        self.client: Optional[TelegramClient] = None; self.is_connected = False; self.started_at: float = 0.0
        self._connect_lock = asyncio.Lock(); self._reconnect = ReconnectionManager(self)
        self._send_cb = CircuitBreaker(f"send_{account['name']}", failure_threshold=5, recovery_timeout=60.0)
        self._entity_cb = CircuitBreaker(f"entity_{account['name']}", failure_threshold=3, recovery_timeout=30.0)
        self._bot_ref: Any = None; self._handler_func = None
        self._deleted_handler_func: Optional[Callable] = None
        self._stats: Dict[str, Any] = {
            "messages_processed": 0, "alerts_sent": 0, "errors": 0, "duplicates": 0, "rate_limited": 0,
            "send_errors": 0, "queue_processed": 0, "media_processed": 0, "avg_processing_time_ms": 0.0,
            "total_processing_time_ms": 0.0, "last_error": None, "last_alert_time": 0.0,
            "connect_attempts": 0, "reconnect_attempts": 0,
            "accepted": 0, "reviewed": 0, "ignored": 0, "avg_confidence": 0.0, "total_confidence": 0.0, "decisions_count": 0,
            # NEW — track add_to_queue() failures instead of losing them silently
            "queue_insert_failed": 0, "queue_backpressure": 0,
        }
        self._stats_lock = asyncio.Lock()
        self._entity_cache: TTLCache = TTLCache(maxsize=CFG.ENTITY_CACHE_MAX_SIZE, ttl=600)
        self._cache_lock = asyncio.Lock()
        self._processed_hashes: LRUCache = LRUCache(maxsize=CFG.PROCESSED_HASHES_MAX_SIZE)
        self._processed_lock = asyncio.Lock()
        self._get_entity_sem = asyncio.Semaphore(5); self._pipeline_sem = asyncio.Semaphore(20)
        # DLQ now wired to genuinely reprocess events through this monitor's
        # own pipeline (process_event_from_queue) or alert re-send path.
        self._dlq = DeadLetterQueue(
            db,
            max_retries=CFG.DEAD_LETTER_MAX_RETRIES,
            reprocess_callback=self._dlq_reprocess,
            account_name=account["name"],
        )
        self._connect_attempts = 0; self._last_connect_error: Optional[str] = None
        # Account-scoped connect-time FloodWait gate (0.0 = not active).
        self._connect_flood_until: float = 0.0
        self._processing_times: List[float] = []; self._max_processing_times = 100
        self._session_rotate_task: Optional[asyncio.Task] = None; self._last_rotation = 0.0
        # Tracked background tasks for media events offloaded out of the
        # Telethon event handler (see _register_handler / _spawn_media_task).
        self._media_tasks: Set[asyncio.Task] = set()
        self._media_task_lock = asyncio.Lock()


    def set_bot(self, bot: Any) -> None: self._bot_ref = bot


    async def _inc_stat(self, name: str, delta: int = 1):
        async with self._stats_lock:
            if name in self._stats: self._stats[name] += delta


    async def _update_avg_time(self, time_ms: float):
        async with self._stats_lock:
            self._processing_times.append(time_ms)
            if len(self._processing_times) > self._max_processing_times: self._processing_times.pop(0)
            self._stats["avg_processing_time_ms"] = sum(self._processing_times) / len(self._processing_times) if self._processing_times else 0.0


    async def _update_decision_stats(self, decision: str, confidence: float):
        async with self._stats_lock:
            if decision == "accept": self._stats["accepted"] += 1
            elif decision == "review": self._stats["reviewed"] += 1
            else: self._stats["ignored"] += 1
            self._stats["total_confidence"] += confidence; self._stats["decisions_count"] += 1
            if self._stats["decisions_count"] > 0: self._stats["avg_confidence"] = self._stats["total_confidence"] / self._stats["decisions_count"]


    def _client_kwargs(self) -> Dict[str, Any]:
        """
        Single source of truth for TelegramClient construction settings.
        Used by BOTH connect() and _rotate_session() so a session refresh
        can never silently drop timeout/retry/auto_reconnect/device
        settings (previously _rotate_session built a bare client with only
        api_id/api_hash, diverging from connect()).
        """
        return dict(
            api_id=self.account["api_id"],
            api_hash=self.account["api_hash"],
            device_model="Render Cloud", system_version="Linux", app_version="13.0",
            timeout=CFG.CONNECTION_TIMEOUT, connection_retries=5, retry_delay=5,
            auto_reconnect=False,
        )


    async def _chat_info(self, client: TelegramClient, chat_id: int, message_id: int, chat_access_hash: Optional[int] = None, chat_username: Optional[str] = None) -> Dict[str, Any]:
        cache_key = chat_id; entity = None
        async with self._cache_lock: entity = self._entity_cache.get(cache_key)
        if entity is None:
            # Route entity resolution through the (previously unused)
            # entity circuit breaker so a FloodWait here is gated per
            # account instead of hammering Telegram repeatedly.
            try:
                entity = await self._entity_cb.call(
                    lambda: resolve_chat_entity(
                        client,
                        {"chat_id": chat_id, "chat_access_hash": chat_access_hash, "username": chat_username},
                    )
                )
            except CircuitBreakerOpen as e:
                logger.debug(f"Entity resolution circuit open [{self.account['name']}]: {e}")
                entity = None
            except FloodWaitError as e:
                logger.warning(f"FloodWait resolving entity [{self.account['name']}]: {e.seconds}s")
                entity = None
            except Exception as e:
                logger.debug(f"Entity resolution failed [{self.account['name']}]: {e}")
                entity = None
            async with self._cache_lock: self._entity_cache[cache_key] = entity
        uname = getattr(entity, "username", None) if entity else None
        links = build_telegram_links(chat_id, message_id, username=uname)
        title = None
        if entity: title = getattr(entity, "title", None) or getattr(entity, "first_name", None)
        return {"entity": entity, "title": title, "group_link": links["group"], "msg_link": links["message"]}


    def _build_alert(self, sender: Dict, chat: Dict, keyword: str, text: str, analysis: Dict = None) -> Tuple[str, Optional[List]]:
        safe_text = InputSanitizer.escape_html(InputSanitizer.truncate(text, 400))
        sender_id = sender.get("id", 0); display_name = sender.get("display", "مستخدم")
        username = sender.get("username", None); access_hash = sender.get("access_hash", None)
        message_html = safe_text; sender_link = display_name
        if username:
            clean_uname = username.lstrip('@'); sender_link = f'<a href="https://t.me/{clean_uname}">{display_name}</a>'
        elif sender_id:
            if access_hash: sender_link = f'<a href="tg://openmessage?user_id={sender_id}">{display_name}</a>'
            else: sender_link = f'<a href="tg://user?id={sender_id}">{display_name}</a>'
        group_link = chat.get("group_link", "#"); chat_title = chat.get("title"); msg_link = chat.get("msg_link", "#")
        if chat_title and chat_title != "غير معروف" and group_link != "#":
            group_html = f'<a href="{group_link}">{chat_title}</a>'
            msg_html = f'<a href="{msg_link}"><b>عرض الرسالة الأصلية</b></a>' if msg_link != "#" else "الرابط غير متاح"
            group_card = f'<blockquote dir="rtl">{group_html}\n\n{msg_html}</blockquote>'
        else:
            msg_html = f'<a href="{msg_link}"><b>عرض الرسالة الأصلية</b></a>' if msg_link != "#" else "الرابط غير متاح"
            group_card = f'<blockquote dir="rtl">{msg_html}</blockquote>'
        alert = (f"<b>الرسالة:</b>\n{message_html}\n\n👤: {sender_link}\n\n{group_card}")
        # v9.10: الأزرار الديناميكية [ 💬 مراسلة ] [ 📨 عرض الرسالة ] في صف
        # واحد — تُبنى من بيانات المرسل/المحادثة المتوفرة، والزر الذي تفتقر
        # بياناته لا يُعرض إطلاقاً (لا أزرار مكسورة). زر النسخ الاختياري يُضاف
        # لنفس الصف عند تفعيل CFG.ALERT_WITH_COPY_BUTTON (قابل للتعديل حياً
        # من لوحة التحكم).
        buttons = None
        if CFG.ALERT_WITH_BUTTONS:
            dynamic = build_dynamic_buttons(
                sender={
                    "id": sender_id,
                    "username": username,
                },
                chat={
                    "id": chat.get("id"),
                    "message_id": chat.get("message_id"),
                    "username": chat.get("username"),
                },
            )
            row = list(dynamic[0]) if dynamic else []
            if CFG.ALERT_WITH_COPY_BUTTON:
                row.append(Button.inline("📋 نسخ النص", f"copy_{analysis.get('msg_hash', '')}"))
            if row:
                buttons = [row]
        return alert, buttons


    async def connect(self) -> bool:
        async with self._connect_lock:
            if self.is_connected and self.client and self._is_client_alive(self.client): return True
            await self._cleanup_old_client()
            self._connect_attempts += 1; self._stats["connect_attempts"] += 1
            account = self.account; session_name = account["session"]
            for attempt in range(1, 4):
                client: Optional[TelegramClient] = None
                try:
                    logger.info(f"Connecting {account['name']} (attempt {attempt}/3)")
                    from telethon.sessions import StringSession
                    session_string = (account.get("session_string") or "").strip()
                    secure_path = f"{session_name}.session.enc"
                    plain_path = str(CFG.SESSIONS_DIR / f"{session_name}.session")
                    if CFG.SECURE_SESSIONS and os.path.exists(secure_path):
                        from config import SecretManager; SecretManager.decrypt_session(secure_path, plain_path)
                    client_kwargs = self._client_kwargs()
                    if session_string:
                        # Render/Cloud: non-interactive StringSession from env var
                        client = TelegramClient(StringSession(session_string), **client_kwargs)
                    elif os.path.exists(plain_path) or os.path.exists(f"{session_name}.session"):
                        session_ref = plain_path if os.path.exists(plain_path) else session_name
                        client = TelegramClient(session=session_ref, **client_kwargs)
                    else:
                        logger.error(
                            f"No session for {account['name']} - set its *_SESSION_STRING env var "
                            f"or login via the dashboard /login page"
                        )
                        self._last_connect_error = "No session - login via /login"
                        self._stats["last_error"] = "No session - login via /login"
                        return False
                    await client.connect()
                    if not await client.is_user_authorized():
                        logger.error(
                            f"Session for {account['name']} is expired/revoked - "
                            f"re-login via the dashboard /login page"
                        )
                        try: await client.disconnect()
                        except Exception: pass
                        self._last_connect_error = "Session expired - re-login via /login"
                        self._stats["last_error"] = "Session expired - re-login via /login"
                        return False
                    me = await client.get_me()
                    logger.info(f"Connected {account['name']} as @{me.username or me.id}")
                    self.client = client; self.is_connected = True; self.started_at = time.time()
                    self._last_connect_error = None; self._stats["last_error"] = None
                    await self._register_handler()  # تسجيل المعالج
                    await self._reconnect.start()
                    self._start_session_rotation()
                    return True
                except SessionPasswordNeededError:
                    logger.error(f"2FA required for {account['name']} - skipping")
                    if client: await client.disconnect()
                    self._last_connect_error = "2FA required"; self._stats["last_error"] = "2FA required"; return False
                except AuthKeyDuplicatedError:
                    logger.error(f"Session duplicated for {account['name']} - another active session is using the same key")
                    if client: await client.disconnect()
                    for p in (f"{session_name}.session", str(CFG.SESSIONS_DIR / f"{session_name}.session")):
                        try: os.remove(p)
                        except FileNotFoundError: pass
                    self._last_connect_error = "AuthKeyDuplicated"; self._stats["last_error"] = "AuthKeyDuplicated"; return False
                except FloodWaitError as e:
                    # Honor Telegram's exact wait duration; do not burn the
                    # remaining local attempts (2/3, 3/3) against a window
                    # we already know will fail. Record it account-scoped so
                    # ReconnectionManager also respects it.
                    wait = max(0, int(getattr(e, "seconds", 0) or 0))
                    self._connect_flood_until = time.time() + wait
                    self._last_connect_error = f"FloodWait {wait}s"
                    self._stats["last_error"] = f"FloodWait {wait}s"
                    logger.warning(
                        f"FloodWait while connecting {account['name']}: must wait {wait}s "
                        f"(recovery ~{time.strftime('%H:%M:%S', time.localtime(self._connect_flood_until))})"
                    )
                    if client:
                        try: await client.disconnect()
                        except Exception: pass
                    return False
                except Exception as e:
                    error_msg = str(e); self._last_connect_error = error_msg[:200]; self._stats["last_error"] = error_msg[:200]
                    logger.error(f"Connect error {account['name']} [{attempt}/3]: {type(e).__name__}: {error_msg[:150]}")
                    if client:
                        try: await client.disconnect()
                        except Exception: pass
                    if attempt < 3: await asyncio.sleep(5 * attempt)
            return False


    async def _cleanup_old_client(self):
        if self.client:
            if self._handler_func is not None:
                try: self.client.remove_event_handler(self._handler_func)
                except Exception as e: logger.debug(f"remove_event_handler failed [{self.account['name']}]: {e}")
            self._handler_func = None
            if self._deleted_handler_func is not None:
                try: self.client.remove_event_handler(self._deleted_handler_func)
                except Exception as e: logger.debug(f"remove deleted-handler failed [{self.account['name']}]: {e}")
            self._deleted_handler_func = None
            try: await self.client.disconnect()
            except Exception as e: logger.debug(f"disconnect during cleanup failed [{self.account['name']}]: {e}")
            self.client = None
        self.is_connected = False


    async def _register_handler(self):
        # إزالة أي معالج سابق لتجنب التكرار
        if self._handler_func is not None and self.client:
            try: self.client.remove_event_handler(self._handler_func)
            except Exception as e: logger.debug(f"remove_event_handler (pre-register) failed [{self.account['name']}]: {e}")
        if self._deleted_handler_func is not None and self.client:
            try: self.client.remove_event_handler(self._deleted_handler_func)
            except Exception as e: logger.debug(f"remove deleted-handler (pre-register) failed [{self.account['name']}]: {e}")
            self._deleted_handler_func = None
        @self.client.on(events.NewMessage())
        async def _handler(event: events.NewMessage.Event):
            start_time = time.perf_counter()
            try:
                if event.out: return
                if event.chat_id == CFG.ADMIN_CHAT_ID: return
                if event.sender and getattr(event.sender, "bot", False): return
                msg_date = event.message.date
                if msg_date and self.started_at > 0:
                    if msg_date.timestamp() < self.started_at - 5: return
                # ══ FAST CAPTURE (v9.8): أول شيء قبل أي await ══
                # كل الفحوص أعلاه متزامنة، والحفظ هنا متزامن أيضاً (بدون أي
                # await) — لا يقع أي yield بين وصول الحدث وتخزين النص.
                if _capture.enabled:
                    self._fast_capture(event)
                event_data = await self._event_to_dict(event)
                if event_data.get("has_media"):
                    # Media is offloaded to a tracked background task instead
                    # of being processed inline here, so this handler stays
                    # light and does not block subsequent events for this
                    # account while a (potentially slow) media pipeline run
                    # is in progress. Text events keep going through the
                    # existing persistent DB queue below, unaffected.
                    media = event.message.media
                    event_data["media_object"] = media if event_data["media_type"] in ("photo", "document") else None
                    await self._spawn_media_task(event_data)
                    if _capture.enabled:
                        # الميديا الآن مملوكة لمهمة الخلفية — لا إنعاش لاحق
                        _capture.mark_queued(event_data.get("chat_id"), event_data.get("message_id"))
                    processing_time = (time.perf_counter() - start_time) * 1000
                    await self._update_avg_time(processing_time)
                    await self._inc_stat("media_processed"); await self._inc_stat("messages_processed")
                    return
                priority = 7 if event_data.get("has_text") else 3
                queue_result = await self.db.add_to_queue(event_data, priority=priority)
                if queue_result > 0:
                    # نجح الإدراج فعلياً
                    await self._inc_stat("queue_processed")
                elif queue_result == -2:
                    # DB غير سليمة (backpressure) — لا تُفقد الرسالة؛ تُحال لـ DLQ
                    # لإعادة المحاولة بدل تجاهلها، بنفس نمط _spawn_media_task
                    logger.warning(
                        f"add_to_queue backpressure [{self.account['name']}] "
                        f"chat={event_data.get('chat_id')} msg={event_data.get('message_id')} "
                        f"— DB unhealthy, routing to DLQ"
                    )
                    await self._inc_stat("queue_backpressure")
                    await self._dlq.push(
                        event_data,
                        RuntimeError("add_to_queue: db_unhealthy (backpressure)"),
                        retry_count=0,
                    )
                else:
                    # queue_result == -1 (أو أي قيمة غير متوقعة) — خطأ إدراج فعلي
                    logger.error(
                        f"add_to_queue failed [{self.account['name']}] "
                        f"chat={event_data.get('chat_id')} msg={event_data.get('message_id')} "
                        f"result={queue_result} — routing to DLQ"
                    )
                    await self._inc_stat("queue_insert_failed")
                    await self._dlq.push(
                        event_data,
                        RuntimeError(f"add_to_queue: insert failed (result={queue_result})"),
                        retry_count=0,
                    )
                # النص الآن مملوك إما للطابور أو لـ DLQ — علّم الإدخال كـ
                # queued حتى لا يحاول مسار MessageDeleted إنعاشه لاحقاً.
                if _capture.enabled:
                    _capture.mark_queued(event_data.get("chat_id"), event_data.get("message_id"))
                processing_time = (time.perf_counter() - start_time) * 1000
                await self._update_avg_time(processing_time)
                await self._inc_stat("messages_processed")
            except Exception as e:
                logger.error(f"Handler error [{self.account['name']}]: {e}")
                await self._inc_stat("errors"); self._stats["last_error"] = str(e)
        self._handler_func = _handler
        # v9.8: MessageDeleted diagnostics + recovery (fast capture).
        @self.client.on(events.MessageDeleted())
        async def _deleted_handler(event: events.MessageDeleted.Event):
            await self._handle_deleted_event(event)
        self._deleted_handler_func = _deleted_handler
        logger.info(f"Event handler registered for {self.account['name']}")


    async def _spawn_media_task(self, event_data: Dict[str, Any]) -> None:
        """
        Runs the full pipeline for a media event in a tracked background
        task, bounded by the same _pipeline_sem used by text processing, so
        it never blocks the Telethon event handler. On failure the event is
        pushed to the (now functional) DeadLetterQueue instead of being
        silently dropped, matching the guarantee text events already had.
        """
        async def _run():
            async with self._pipeline_sem:
                try:
                    await self._process_event_pipeline(event_data)
                    await self._inc_stat("queue_processed")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(
                        f"Media pipeline error [{self.account['name']}] "
                        f"chat={event_data.get('chat_id')} msg={event_data.get('message_id')}: {e}"
                    )
                    await self._inc_stat("errors"); self._stats["last_error"] = str(e)
                    await self._dlq.push(event_data, e, retry_count=0)

        task = asyncio.create_task(
            _run(), name=f"media_{self.account['name']}_{event_data.get('message_id')}"
        )
        async with self._media_task_lock:
            self._media_tasks.add(task)
        task.add_done_callback(self._on_media_task_done)


    def _on_media_task_done(self, task: asyncio.Task) -> None:
        self._media_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"Unhandled media task exception [{self.account['name']}]: {exc}")


    async def _event_to_dict(self, event: events.NewMessage.Event) -> Dict[str, Any]:
        sender = event.sender
        # v9.9: the fast capture has ALREADY protected text+sender synchronously
        # before this point, so one deduplicated resolver lookup here is safe:
        # it only enriches AFTER the save-first guarantee. When event.sender
        # is absent (entity not embedded in the update), a single shared
        # in-flight-deduped resolution fills it (requirement #3/#8: prefer
        # event.sender; get_entity only as a bounded fallback).
        if sender is None and CFG.SENDER_INTEL_ENABLED and getattr(event, "sender_id", None):
            try:
                sender = await sender_intel.fetch_event_sender(event, self.client)
            except Exception as e:
                logger.debug(f"sender_entity_fetch [{self.account['name']}]: {type(e).__name__}")
                sender = None
        chat = event.chat
        text = event.message.text or ""; caption = getattr(event.message, "message", "") or ""
        full_text = (text or caption).strip()
        media = event.message.media; media_type = self._get_media_type(media)
        has_media = media_type in ("photo", "document")
        chat_username = getattr(chat, "username", None) if chat else None
        event_data = {
            "chat_id": event.chat_id, "message_id": event.message.id,
            "sender_id": getattr(event, "sender_id", 0) or 0,
            "sender_username": getattr(sender, "username", None), "sender_first_name": getattr(sender, "first_name", None),
            "sender_last_name": getattr(sender, "last_name", None), "sender_access_hash": getattr(sender, "access_hash", None),
            "chat_access_hash": getattr(chat, "access_hash", None), "chat_username": chat_username,
            "text": full_text, "has_text": bool(full_text), "has_media": has_media, "media_type": media_type,
            "account_name": self.account["name"], "timestamp": time.time(),
        }
        # v9.9 sender intelligence: ADDITIVE metadata keys only. Every key
        # above keeps its exact name and semantics (backward compatible);
        # downstream alert rendering reads the SAME keys as before.
        if CFG.SENDER_INTEL_ENABLED and sender is not None:
            try:
                event_data.update(_sender_extract_flat(sender))
            except Exception as e:
                logger.debug(f"sender_flat_extract [{self.account['name']}]: {type(e).__name__}")
        return event_data


    @staticmethod
    def _get_media_type(media: Any) -> Optional[str]:
        if media is None: return None
        if isinstance(media, MessageMediaPhoto): return "photo"
        if isinstance(media, MessageMediaDocument): return "document"
        if isinstance(media, MessageMediaWebPage): return "webpage"
        return "other"


    # ── Fast Capture (v9.8/v9.9): deletion-race protection ─────────────
    def _fast_capture(self, event: events.NewMessage.Event) -> None:
        """SYNC save-first capture — must never await and never raise.

        Called from the NewMessage handler AFTER the cheap synchronous
        early-return checks and BEFORE the first await (the queue
        handoff). Captures the raw text (body or media caption) AND the
        full sender snapshot (v9.9) so a deletion bot that removes the
        message milliseconds later cannot make the pipeline lose either.
        """
        if not _capture.enabled:
            return
        try:
            msg = event.message
            text = (getattr(msg, "text", None) or getattr(msg, "message", None) or "")
            if not text:
                return  # voice/photo-only content — nothing to protect
            sender = getattr(event, "sender", None)
            # v9.9: build the sender snapshot synchronously (pure getattr —
            # zero API calls, zero awaits). Failure-safe by design.
            sender_meta: Optional[Dict[str, Any]] = None
            if CFG.SENDER_INTEL_ENABLED and sender is not None:
                try:
                    meta = _sender_extract(sender)
                    if meta:
                        sender_meta = {
                            "sender_username": meta.get("username"),
                            "sender_first_name": meta.get("first_name"),
                            "sender_last_name": meta.get("last_name"),
                            "sender_access_hash": meta.get("access_hash"),
                            "sender_meta_full": meta,
                        }
                except Exception as meta_err:
                    logger.debug(f"capture_sender_meta_error [{self.account['name']}]: {meta_err}")
            _capture.save(
                chat_id=event.chat_id,
                msg_id=msg.id,
                text=text.strip(),
                sender_id=getattr(event, "sender_id", 0) or 0,
                sender_name=(getattr(sender, "first_name", None) or "") if sender else "",
                account=self.account["name"],
                sender_meta=sender_meta,
            )
        except Exception as cap_err:
            logger.debug(f"capture_save_error [{self.account['name']}]: {cap_err}")

    async def _handle_deleted_event(self, event: events.MessageDeleted.Event) -> None:
        """Diagnostics + recovery for deleted messages (v9.8 fast capture).

        For every deleted message that was previously captured:
          1. Log the captured text exactly once, even when N account
             clients receive the same deletion notice (claim_deleted).
          2. If the event never reached the normal pipeline (handler crashed
             between capture and the queue handoff), re-submit a rebuilt
             event through the standard pipeline — the message_hash dedup in
             _store_message makes this safe even if the original handoff was
             still in flight.
        """
        if not _capture.enabled:
            return
        try:
            chat_id = getattr(event, "chat_id", None)
            deleted_ids = getattr(event, "deleted_ids", None)
            if deleted_ids is None:
                single = getattr(event, "deleted_id", None)
                deleted_ids = [single] if single is not None else []
            deleted_ids = list(deleted_ids or [])
            if not deleted_ids:
                return
            for msg_id in deleted_ids:
                entry = _capture.claim_deleted(chat_id, msg_id)
                if entry is None:
                    continue
                logger.info(
                    f"🗑️ deleted_captured | chat={entry['chat_id']} | msg={entry['msg_id']} | "
                    f"sender={entry['sender_name']} | queued={entry['queued']} | "
                    f"alerted={entry['alerted']} | text={entry['text'][:80]}"
                )
                _capture.inc("deleted_captured")
                if not entry["queued"] and not entry["alerted"] and not entry["recovered"]:
                    entry["recovered"] = True
                    _capture.inc("deleted_recovered")
                    await self._spawn_recovery_task(dict(entry))
        except Exception as e:
            logger.debug(f"deleted-event handling error [{self.account['name']}]: {e}")

    async def _spawn_recovery_task(self, entry: Dict[str, Any]) -> None:
        """Re-submits a captured-but-never-queued message through the
        standard pipeline inside a tracked background task (same tracking /
        cancellation discipline as _spawn_media_task)."""
        # v9.9 (brief #10): the rebuilt event REUSES the sender data saved
        # in the fast capture (username / names / access_hash) instead of
        # blanking them to None. Missing fields fall back to the persisted
        # sender_contacts row — never the reverse (no NULL overwrites).
        entry_sender_id = entry.get("sender_id", 0) or 0
        event_data: Dict[str, Any] = {
            "chat_id": entry.get("chat_id"),
            "message_id": entry.get("msg_id"),
            "sender_id": entry_sender_id,
            "sender_username": entry.get("sender_username"),
            "sender_first_name": entry.get("sender_first_name") or entry.get("sender_name"),
            "sender_last_name": entry.get("sender_last_name"),
            "sender_access_hash": entry.get("sender_access_hash"),
            "chat_access_hash": None,
            "chat_username": None,
            "text": entry.get("text", ""),
            "has_text": bool(entry.get("text")),
            "has_media": False,
            "media_type": None,
            "account_name": entry.get("account") or self.account["name"],
            "timestamp": entry.get("ts") or time.time(),
            "_fast_capture_recovery": True,
        }
        # Restore the richer metadata snapshot (flags like is_bot/premium/…)
        meta_full = entry.get("sender_meta_full")
        if isinstance(meta_full, dict) and meta_full:
            event_data.update(_sender_meta_to_contact(meta_full))
        # DB fallback ONLY for fields the capture could not provide —
        # existing (non-None) values are never overwritten (brief #10/#24).
        await self._enrich_sender_from_db(event_data)

        async def _run():
            async with self._pipeline_sem:
                try:
                    logger.info(
                        f"♻️ capture_recovery | chat={event_data['chat_id']} | "
                        f"msg={event_data['message_id']} [{self.account['name']}] — "
                        f"re-submitting captured text through the pipeline"
                    )
                    await self.process_event_from_queue(event_data)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"capture_recovery error [{self.account['name']}]: {e}")
                    await self._dlq.push(event_data, e, retry_count=0)

        task = asyncio.create_task(
            _run(),
            name=f"capture_recovery_{self.account['name']}_{event_data.get('message_id')}",
        )
        async with self._media_task_lock:
            self._media_tasks.add(task)
        task.add_done_callback(self._on_media_task_done)

    async def _enrich_sender_from_db(self, data: Dict[str, Any]) -> None:
        """v9.9 sender-intel DB fallback: fill ONLY the sender fields the
        event/recovery data is missing from the persisted sender_contacts
        row. Never overwrites an existing non-null value with another value
        or with None (brief #10/#24) — and never raises."""
        try:
            sender_id = data.get("sender_id")
            if not sender_id or not self.db or not CFG.SENDER_INTEL_ENABLED:
                return
            missing_identity = (
                not data.get("sender_username")
                or data.get("sender_access_hash") is None
                or not data.get("sender_first_name")
            )
            if not missing_identity:
                return
            row = await self.db.get_sender_contact(int(sender_id))
            if not row:
                return
            if not data.get("sender_username"):
                data["sender_username"] = row.get("username")
            if data.get("sender_access_hash") is None:
                data["sender_access_hash"] = row.get("access_hash")
            if not data.get("sender_first_name"):
                data["sender_first_name"] = row.get("first_name")
            if not data.get("sender_last_name"):
                data["sender_last_name"] = row.get("last_name")
        except Exception as e:
            logger.debug(f"sender_db_enrich error [{self.account['name']}]: {type(e).__name__}")

    def _recover_captured_text(self, data: Dict[str, Any]) -> Optional[str]:
        """v9.8 fallback at alert time: if the pipeline's copy of the text
        is empty but the fast-capture buffer holds it, return the captured
        text (length-bounded) so the alert can still go out with the real
        content instead of being treated as a no-content event."""
        if not _capture.enabled:
            return None
        if data.get("text"):
            return None  # the pipeline already has the text
        if data.get("has_media") and not data.get("media_object"):
            return None  # pure media event without caption — nothing to recover
        entry = _capture.lookup(data.get("chat_id"), data.get("message_id"))
        if not entry:
            return None
        cap_text = (entry.get("text") or "").strip()
        if cap_text and CFG.MIN_MESSAGE_LENGTH <= len(cap_text) <= CFG.MAX_MESSAGE_LENGTH:
            logger.info(
                f"fast_capture fallback | chat={data.get('chat_id')} | "
                f"msg={data.get('message_id')} — text recovered from capture buffer"
            )
            _capture.inc("fallback_used")
            return cap_text
        return None


    def _start_session_rotation(self):
        if self._session_rotate_task and not self._session_rotate_task.done(): return
        self._session_rotate_task = asyncio.create_task(self._session_rotate_loop())


    async def _session_rotate_loop(self):
        while self.is_connected:
            await asyncio.sleep(CFG.SESSION_REFRESH_INTERVAL)
            if self.is_connected and self.client:
                try: await self._rotate_session()
                except Exception as e: logger.error(f"Session rotation error [{self.account['name']}]: {e}")


    async def _rotate_session(self) -> bool:
        """
        Controlled client/session refresh (NOT cryptographic key rotation).

        Fixed in this pass:
          * Uses the exact same client configuration as connect() via
            _client_kwargs() (timeout, retries, auto_reconnect, device
            metadata) instead of a bare TelegramClient(session, id, hash).
          * Connects + authorizes the NEW client BEFORE tearing down the
            OLD one, and only registers the event handler on the new client
            after the old handler/connection are torn down — this avoids a
            window where both clients would be actively receiving/
            processing the same events.
          * Wrapped in the existing _connect_lock so this can never overlap
            with a concurrent connect() call from ReconnectionManager.
          * On any failure, self.client is explicitly cleared (rather than
            left pointing at an already-disconnected client) so the normal
            reconnect path via connect() takes over cleanly.
        """
        async with self._connect_lock:
            if not self.client or not self.is_connected:
                return False
            account_name = self.account["name"]
            old_client = self.client
            old_handler = self._handler_func
            try:
                logger.info(f"Rotating session for {account_name}")
                session_string = self.client.session.save()

                from telethon.sessions import StringSession
                new_client = TelegramClient(StringSession(session_string), **self._client_kwargs())
                await new_client.connect()
                if not await new_client.is_user_authorized():
                    logger.error(f"Session rotation: new client unauthorized for {account_name}, aborting rotation")
                    try: await new_client.disconnect()
                    except Exception: pass
                    # Old client stays connected and owns the handler — safe abort.
                    return False

                # Only now tear down the old client/handler — the new
                # client has no handler registered yet, so there is no
                # window where both clients process events simultaneously.
                if old_handler:
                    try: old_client.remove_event_handler(old_handler)
                    except Exception as e: logger.debug(f"remove_event_handler during rotation failed [{account_name}]: {e}")
                if self._deleted_handler_func:
                    try: old_client.remove_event_handler(self._deleted_handler_func)
                    except Exception as e: logger.debug(f"remove deleted-handler during rotation failed [{account_name}]: {e}")
                    self._deleted_handler_func = None
                try: await old_client.disconnect()
                except Exception as e: logger.debug(f"old client disconnect during rotation failed [{account_name}]: {e}")

                self.client = new_client
                self._handler_func = None
                self.is_connected = True
                self._last_rotation = time.time()
                await self._register_handler()
                logger.info(f"Session rotated successfully for {account_name}")
                return True
            except Exception as e:
                # v9.8 fix (audit M-2): on a mid-rotation failure the old
                # client used to be orphaned (still connected, still holding
                # the event handler) while self.client was cleared to None —
                # a duplicate-processing + resource leak. Restore the old
                # client as the live one and let the normal reconnect path
                # take over if it is actually dead.
                logger.error(f"Session rotation failed for {account_name}: {e}")
                self.client = old_client if (old_client and self._is_client_alive(old_client)) else None
                self.is_connected = self.client is not None
                if not self.is_connected:
                    self._handler_func = None
                    try:
                        if old_client: await old_client.disconnect()
                    except Exception:
                        pass
                return False


    async def _dlq_reprocess(self, event_data: Dict[str, Any], retry_count: int) -> None:
        """
        Single entry point used by DeadLetterQueue to retry an event.
        Dispatches to the correct existing pipeline based on what kind of
        failure originally produced this event: a message-processing
        failure goes back through process_event_from_queue (which will
        naturally no-op via the existing message_hash dedup if the message
        was already stored); an alert-send failure re-invokes _send_alert
        directly, since routing it back through the full pipeline would be
        deduped away by the already-stored message and never actually
        resend the alert.
        """
        if event_data.get("_dlq_kind") == "alert_resend":
            await self._retry_send_alert(event_data, retry_count)
        else:
            await self.process_event_from_queue(event_data, retry_count=retry_count)


    async def _retry_send_alert(self, event_data: Dict[str, Any], retry_count: int) -> None:
        keyword = event_data.get("_dlq_keyword") or "طلب"
        text = event_data.get("_dlq_text") or ""
        msg_hash = event_data.get("_dlq_msg_hash") or ""
        analysis = event_data.get("_dlq_analysis") or {}
        logger.info(
            f"DLQ[{self.account['name']}]: retrying alert send for msg_hash={msg_hash} (attempt {retry_count})"
        )
        await self._send_alert(
            event_data, keyword=keyword, text=text, msg_hash=msg_hash, analysis=analysis, retry_count=retry_count
        )


    async def process_event_from_queue(self, event_data: Dict[str, Any], retry_count: int = 0) -> None:
        """
        retry_count is optional and defaults to 0 to preserve the existing
        call signature used by main.py's consumer worker
        (monitor.process_event_from_queue(event_data)). It is only ever
        passed explicitly by this module's own DLQ retry path, so that a
        failure occurring on a retry correctly increments retry_count
        instead of resetting to 0 on every subsequent push() (the original
        bug: DeadLetterQueue.push() was always called with the default
        retry_count=0 here, which meant events effectively retried forever
        and never reached max_retries / dead-letter storage).
        """
        start_time = time.perf_counter()
        async with self._pipeline_sem:
            try:
                await self._process_event_pipeline(event_data)
                processing_time = (time.perf_counter() - start_time) * 1000
                await self._update_avg_time(processing_time); await self._inc_stat("queue_processed")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"process_event_from_queue error [{self.account['name']}] "
                    f"chat={event_data.get('chat_id')} msg={event_data.get('message_id')} "
                    f"(retry_count={retry_count}): {e}"
                )
                await self._inc_stat("errors"); self._stats["last_error"] = str(e)
                await self._dlq.push(event_data, e, retry_count=retry_count)


    async def _process_event_pipeline(self, data: Dict[str, Any]):
        if not await self._validate_event(data): return
        msg_hash, validated_text, is_new = await self._store_message(data)
        if not is_new: await self._inc_stat("duplicates"); return
        # v9.8 fast-capture fallback: recover the text from the buffer if
        # the pipeline's own copy is empty (deleted-message edge cases).
        if validated_text is None:
            validated_text = self._recover_captured_text(data)
        await self._analyze_and_alert(data, msg_hash, validated_text)


    async def _validate_event(self, data: Dict[str, Any]) -> bool:
        sender_id = data.get("sender_id", 0); chat_id = data.get("chat_id", 0)
        if await self.db.is_blocked_sender(sender_id): return False
        if await self.db.is_blocked_chat(chat_id): return False
        text = data.get("text", "")
        if text and not (CFG.MIN_MESSAGE_LENGTH <= len(text) <= CFG.MAX_MESSAGE_LENGTH):
            await self._inc_stat("errors"); return False
        return True


    async def _store_message(self, data: Dict[str, Any]) -> Tuple[str, Optional[str], bool]:
        text = data.get("text", ""); chat_id = data.get("chat_id", 0); message_id = data.get("message_id", 0)
        media_type = data.get("media_type", "media"); timestamp = data.get("timestamp", time.time())
        validated_text = text if (text and CFG.MIN_MESSAGE_LENGTH <= len(text) <= CFG.MAX_MESSAGE_LENGTH) else None
        norm = (text or "").lower().strip(); msg_hash = fast_hash(f"{chat_id}_{message_id}_{norm}")
        async with self._processed_lock:
            if msg_hash in self._processed_hashes: return msg_hash, validated_text, False
            self._processed_hashes[msg_hash] = True
        rec = MessageRecord(message_hash=msg_hash, chat_id=chat_id, sender_id=data.get("sender_id", 0),
            message_text=validated_text or f"[{media_type}]", timestamp=timestamp)
        is_new = await self.db.try_insert_message(rec)
        return msg_hash, validated_text, is_new


    async def _analyze_and_alert(self, data: Dict[str, Any], msg_hash: str, validated_text: Optional[str]):
        sender_id = data.get("sender_id", 0); has_media = bool(data.get("media_object"))
        start_time = time.perf_counter()
        if validated_text:
            analysis = await self.filter.analyze(validated_text)
            decision = analysis.get("decision", "ignore"); confidence = analysis.get("confidence", 0.0)
            reasons = analysis.get("reasons", [])
            intent_verb = analysis.get("intent_verb"); academic_object = analysis.get("academic_object")
            keyword = analysis.get("keyword")
            if isinstance(keyword, (tuple, list)): keyword = keyword[0] if keyword else None
            if isinstance(intent_verb, (tuple, list)): intent_verb = intent_verb[0] if intent_verb else None
            if isinstance(academic_object, (tuple, list)): academic_object = academic_object[0] if academic_object else None
            analysis.update({"decision": decision, "confidence": confidence, "reasons": reasons,
                             "intent_verb": intent_verb, "academic_object": academic_object, "msg_hash": msg_hash})
            await self._update_decision_stats(decision, confidence)
            if analysis.get("valid") and has_media: analysis["has_media"] = True
            elif has_media: analysis = {"valid": False, "reason": "media_only_no_text", "keyword": None, "decision": "ignore"}
        else:
            analysis = {"valid": False, "reason": "no_content", "keyword": None, "decision": "ignore"}
        # ====== الإصلاح الجوهري: التأكد من أن القرار النهائي هو "accept" فقط ======
        decision = analysis.get("decision", "ignore")
        is_valid = analysis.get("valid", False) and decision == "accept"
        # =========================================================================
        try: await self.db.update_sender_reputation(sender_id, is_valid)
        except Exception as e: logger.warning(f"update_sender_reputation failed [{self.account['name']}]: {e}")
        if is_valid and await self.db.can_send_alert(sender_id):
            await self._send_alert(data, keyword=analysis.get("keyword") or "طلب", text=validated_text or "",
                                   msg_hash=msg_hash, analysis=analysis)
            await self._inc_stat("alerts_sent")
            async with self._stats_lock: self._stats["last_alert_time"] = time.time()
        try:
            # v9.9: persist the FULL sender snapshot (identity + metadata)
            # into sender_contacts. Identity fields follow the COALESCE
            # policy in database.py: new non-null → update, null → keep.
            contact = {
                "sender_id": sender_id, "access_hash": data.get("sender_access_hash"),
                "username": data.get("sender_username"), "first_name": data.get("sender_first_name"),
                "last_name": data.get("sender_last_name"), "chat_id": data["chat_id"], "message_id": data["message_id"],
            }
            if CFG.SENDER_INTEL_ENABLED:
                contact.update(_sender_meta_to_contact(data))
                # last-seen status label captured at arrival (may be stale
                # later, hence stored, never trusted for delivery decisions)
            await self.db.upsert_sender_contact(contact)
        except Exception as e: logger.warning(f"upsert_sender_contact failed [{self.account['name']}]: {e}")
        processing_time = (time.perf_counter() - start_time) * 1000
        await self._update_avg_time(processing_time)


    async def _resolve_send_client(self) -> Optional[TelegramClient]:
        candidates = []
        mc = self._bot_ref.main_client if self._bot_ref else None
        if mc and self._is_client_alive(mc): candidates.append(mc)
        if self.client and self._is_client_alive(self.client): candidates.append(self.client)
        if self._bot_ref:
            for mon in self._bot_ref.monitors:
                if mon is self: continue
                if mon.client and mon.is_connected and self._is_client_alive(mon.client): candidates.append(mon.client)
        for c in candidates:
            if self._is_client_alive(c): return c
        return None


    @staticmethod
    def _is_client_alive(client: TelegramClient) -> bool:
        try:
            attr = getattr(client, "is_connected", None)
            return bool(attr() if callable(attr) else attr)
        except Exception: return False


    async def _send_alert(
        self,
        data: Dict[str, Any],
        keyword: str,
        text: str,
        msg_hash: str,
        analysis: Dict[str, Any],
        retry_count: int = 0,
    ):
        """
        retry_count defaults to 0 to preserve the original call signature
        used from _analyze_and_alert. When this is invoked as part of a DLQ
        retry (_retry_send_alert), retry_count is forwarded so that a
        repeated send failure correctly advances toward max_retries instead
        of resetting (mirrors the same fix applied to
        process_event_from_queue).
        """
        if not self._bot_ref: return
        account_name = data.get("account_name", self.account["name"])
        if not await self._bot_ref.rate_limiter.can_proceed(account_name):
            await self._inc_stat("rate_limited"); return
        chat_id = data["chat_id"]; message_id = data["message_id"]; sender_id = data["sender_id"]
        sender_username = data.get("sender_username"); sender_first_name = data.get("sender_first_name")
        sender_last_name = data.get("sender_last_name"); sender_access_hash = data.get("sender_access_hash")
        chat_access_hash = data.get("chat_access_hash"); chat_username = data.get("chat_username")
        display_name = f"{sender_first_name or ''} {sender_last_name or ''}".strip() or f"مستخدم ({sender_id})"
        send_client = await self._resolve_send_client()
        if not send_client:
            logger.error(f"No available client to send alert [{account_name}]"); await self._inc_stat("send_errors"); return
        # ── v9.10: حاجز منع تكرار التنبيهات (بعد rate-limit وقبل أي عمل مكلف) ──
        # نفس المرسل بنفس النص عبر أي حساب من الحسابات الستة = تنبيه واحد فقط.
        # الحجز آتومي (ذاكرة + DB) — وعند فشل الإرسال يُفك لإعادة المحاولة.
        dedup_fp = content_fingerprint(sender_id, text)
        if not await get_deduplicator().check_and_claim(dedup_fp):
            await self._inc_stat("duplicates")
            logger.info(
                f"Duplicate alert blocked [{account_name}] | msg_hash={msg_hash} | "
                f"sender={sender_id} | fp={dedup_fp[:12]} (cross-account/re-send dedup)"
            )
            return
        chat_info = await self._chat_info(send_client, chat_id, message_id, chat_access_hash=chat_access_hash, chat_username=chat_username)
        # v9.10: بيانات الأزرار الديناميكية — _build_alert تبني [ مراسلة /
        # عرض الرسالة ] من المعرفات الخام نفسها المستخدمة في روابط النص.
        chat_info["id"] = chat_id
        chat_info["message_id"] = message_id
        chat_info["username"] = chat_username
        analysis["msg_hash"] = msg_hash
        sender = {"id": sender_id, "display": display_name, "username": sender_username, "access_hash": sender_access_hash}
        alert_text, buttons = self._build_alert(sender, chat_info, keyword, text, analysis)
        user_media = data.get("media_object")
        async def do_send():
            sent = False
            if user_media is not None:
                try:
                    await send_client.send_file(CFG.TARGET_GROUP_ID, file=user_media, caption=alert_text, buttons=buttons, parse_mode="html", link_preview=False)
                    sent = True
                except Exception as e: logger.debug(f"User media send failed: {e}")
            if not sent:
                chat_entity = chat_info.get("entity")
                if chat_entity and getattr(chat_entity, 'id', 0) != 0:
                    try:
                        result = await send_client.get_profile_photos(chat_entity, limit=1)
                        if result and hasattr(result, 'photos') and len(result.photos) > 0:
                            await send_client.send_file(CFG.TARGET_GROUP_ID, file=result.photos[0], caption=alert_text, parse_mode="html", link_preview=False)
                            sent = True
                    except Exception as e:
                        logger.debug(f"Chat photo fallback send failed [{account_name}]: {e}")
            if not sent:
                await send_client.send_message(CFG.TARGET_GROUP_ID, alert_text, buttons=buttons, parse_mode="html", link_preview=False)
        def _retry_payload() -> Dict[str, Any]:
            payload = dict(data)
            payload["_dlq_kind"] = "alert_resend"
            payload["_dlq_keyword"] = keyword
            payload["_dlq_text"] = text
            payload["_dlq_msg_hash"] = msg_hash
            payload["_dlq_analysis"] = analysis
            return payload
        try:
            await self._send_cb.call(do_send)
            if _capture.enabled:
                _capture.mark_alerted(data.get("chat_id"), data.get("message_id"))
            safe_keyword = keyword
            if isinstance(safe_keyword, (tuple, list)): safe_keyword = safe_keyword[0] if safe_keyword else ""
            if not isinstance(safe_keyword, str): safe_keyword = str(safe_keyword) if safe_keyword is not None else ""
            await self.db.add_alert(AlertRecord(message_hash=msg_hash, chat_id=chat_id, sender_id=sender_id,
                account_name=account_name, keyword=safe_keyword, alert_text=alert_text, timestamp=time.time()))
            logger.info(f"Alert sent by {account_name} | kw={keyword!r} | sender={display_name}")
        except (FloodWaitError, CircuitBreakerOpen) as e:
            logger.warning(
                f"Alert send throttled [{account_name}] msg_hash={msg_hash}: {type(e).__name__}: {e}"
            )
            # فشل الإرسال — فك حجز الـ dedup حتى تتمكن إعادة محاولة DLQ من الإرسال لاحقاً
            await get_deduplicator().release(dedup_fp)
            await self._dlq.push(_retry_payload(), e, retry_count=retry_count)
            await self._inc_stat("send_errors"); raise
        except Exception as e:
            logger.error(f"Send alert error [{account_name}]: {e} - trying fallback")
            try:
                await send_client.send_message(CFG.TARGET_GROUP_ID, alert_text, buttons=buttons, parse_mode=None, link_preview=False)
                if _capture.enabled:
                    _capture.mark_alerted(data.get("chat_id"), data.get("message_id"))
            except Exception as fe:
                logger.error(f"Fallback failed [{account_name}]: {fe}")
                await self._inc_stat("send_errors")
                # فشل نهائي — فك الحجز لإعادة محاولة DLQ لاحقاً
                await get_deduplicator().release(dedup_fp)
                await self._dlq.push(_retry_payload(), fe, retry_count=retry_count)


    async def disconnect(self):
        async with self._connect_lock:
            self.is_connected = False
            await self._reconnect.stop(); await self._dlq.stop()
            if self._session_rotate_task:
                self._session_rotate_task.cancel()
                try: await self._session_rotate_task
                except asyncio.CancelledError: pass
            # Cancel and await any in-flight media background tasks so
            # shutdown never leaves "Task was destroyed but it is pending"
            # noise and never silently drops a media event mid-flight.
            async with self._media_task_lock:
                media_tasks = list(self._media_tasks)
            for t in media_tasks:
                if not t.done(): t.cancel()
            for t in media_tasks:
                try: await t
                except asyncio.CancelledError: pass
                except Exception as e:
                    logger.error(f"Media task error during shutdown [{self.account['name']}]: {e}")
            async with self._media_task_lock:
                self._media_tasks.clear()
            self._entity_cache.clear(); self._processed_hashes.clear(); self._processing_times.clear()
            if self.client:
                if self._handler_func:
                    try: self.client.remove_event_handler(self._handler_func)
                    except Exception as e: logger.debug(f"remove_event_handler during disconnect failed [{self.account['name']}]: {e}")
                self._handler_func = None
                if self._deleted_handler_func:
                    try: self.client.remove_event_handler(self._deleted_handler_func)
                    except Exception as e: logger.debug(f"remove deleted-handler during disconnect failed [{self.account['name']}]: {e}")
                self._deleted_handler_func = None
                if CFG.SECURE_SESSIONS:
                    try:
                        session_path = f"{self.account['session']}.session"
                        secure_path = f"{self.account['session']}.session.enc"
                        if os.path.exists(session_path):
                            from config import SecretManager
                            SecretManager.encrypt_session(session_path, secure_path); os.remove(session_path)
                    except Exception as e: logger.error(f"Session encrypt error: {e}")
                try: await self.client.disconnect()
                except Exception as e: logger.debug(f"disconnect failed [{self.account['name']}]: {e}")
            self.client = None
            logger.info(f"Disconnected {self.account['name']}")


    async def get_stats(self) -> Dict[str, Any]:
        async with self._stats_lock:
            stats = dict(self._stats)
        return {
            "name": self.account["name"], "phone": self.account["phone"], "connected": self.is_connected,
            "priority": self.account.get("priority", 0), "send_cb_state": self._send_cb.state,
            "send_cb_failures": self._send_cb.failures, "entity_cb_state": self._entity_cb.state,
            "entity_cb_failures": self._entity_cb.failures, "connect_attempts": self._connect_attempts,
            "last_error": self._last_connect_error, "messages_processed": stats.get("messages_processed", 0),
            "alerts_sent": stats.get("alerts_sent", 0), "errors": stats.get("errors", 0),
            "duplicates": stats.get("duplicates", 0), "rate_limited": stats.get("rate_limited", 0),
            "send_errors": stats.get("send_errors", 0), "queue_processed": stats.get("queue_processed", 0),
            "media_processed": stats.get("media_processed", 0), "avg_processing_time_ms": round(stats.get("avg_processing_time_ms", 0), 2),
            "last_alert_time": stats.get("last_alert_time", 0), "dlq_stats": await self._dlq.get_stats(),
            "cache_size": len(self._entity_cache), "processed_hashes_size": len(self._processed_hashes),
            "accepted": stats.get("accepted", 0), "reviewed": stats.get("reviewed", 0), "ignored": stats.get("ignored", 0),
            "avg_confidence": round(stats.get("avg_confidence", 0.0), 2), "decisions_count": stats.get("decisions_count", 0),
        }


# (HealthMonitor كما هو في النسخة الأصلية - تم الحفاظ عليه)
class HealthStatus:
    def __init__(self, is_healthy: bool, checks: Dict[str, Any], details: Optional[Dict[str, Any]] = None):
        self.is_healthy = is_healthy; self.checks = checks; self.details = details or {}; self.timestamp = time.monotonic()


class HealthMonitor:
    def __init__(self, bot_ref: Any = None): self._bot = bot_ref; self._consecutive_fail = 0
    @staticmethod
    async def client_ok(client: Any) -> bool:
        if client is None: return False
        try:
            attr = getattr(client, "is_connected", None)
            return bool(attr() if callable(attr) else attr)
        except Exception: return False
    async def check(self) -> HealthStatus:
        checks = {}; details = {}
        if self._bot:
            try:
                if self._bot.db.is_connected:
                    await self._bot.db._fetchone("SELECT 1 AS ping"); checks["database"] = "ok"
                else: checks["database"] = "disconnected"
                up = sum(1 for m in self._bot.monitors if m.is_connected); total = len(self._bot.monitors)
                checks["monitors_up"] = up; checks["monitors_total"] = total
                monitors_stats = []
                for m in self._bot.monitors:
                    s = await m.get_stats(); monitors_stats.append({"name": s.get("name"), "connected": s.get("connected"),
                        "messages_processed": s.get("messages_processed", 0), "alerts_sent": s.get("alerts_sent", 0),
                        "errors": s.get("errors", 0), "avg_time": s.get("avg_processing_time_ms", 0), "last_error": s.get("last_error"),
                        "accepted": s.get("accepted", 0), "reviewed": s.get("reviewed", 0), "ignored": s.get("ignored", 0), "avg_confidence": s.get("avg_confidence", 0)})
                details["monitors"] = monitors_stats
                checks["monitors_ok"] = up >= max(1, total * CFG.HEALTH_MIN_MONITORS_RATIO)
                main_ok = await self.client_ok(self._bot.main_client); checks["main_client"] = "up" if main_ok else "down"
                qsize = await self._bot.db.queue_size(); checks["queue_size"] = qsize; checks["queue_ok"] = qsize < CFG.MESSAGE_QUEUE_SIZE
                filter_tele = await self._bot.filter.get_telemetry(); checks["filter_processed"] = filter_tele.get("processed", 0)
                checks["filter_valid"] = filter_tele.get("valid", 0); checks["filter_accepted"] = filter_tele.get("accepted", 0); checks["filter_review"] = filter_tele.get("review", 0)
            except Exception as e: checks["database"] = f"error: {e}"
        is_healthy = checks.get("database") == "ok" and checks.get("monitors_ok", False) and checks.get("main_client") == "up"
        self._consecutive_fail = 0 if is_healthy else self._consecutive_fail + 1
        details["consecutive_unhealthy"] = self._consecutive_fail
        return HealthStatus(is_healthy=is_healthy, checks=checks, details=details)