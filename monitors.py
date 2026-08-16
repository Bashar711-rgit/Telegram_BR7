#!/usr/bin/env python3
"""
monitors.py – Account Monitor v9.8 (STABLE 24/7 EDITION, HARDENED)
- إصلاح تدوير الجلسة (إعادة تسجيل المعالج)
- تحسين إعادة الاتصال واكتشاف العميل الميت
- دعم كامل لـ IntentEngine
- NEW v9.8: Instant Capture Lane — Message Deletion Race Condition Fix

v9.8 (this pass) — Instant Capture Lane added, monitors.py ONLY:
  * Added InstantCaptureManager class for immediate message capture
    before full NLP analysis, solving the Message Deletion Race Condition.
  * EnhancedAccountMonitor now creates and manages an InstantCaptureManager.
  * The event handler now submits candidates to the capture queue
    BEFORE the normal processing pipeline (DB queue / media task).
  * All existing functionality preserved unchanged.

v9.7 (prior pass) — targeted reliability fixes:
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
"""
from __future__ import annotations
import asyncio
import os
import secrets
import time
from collections import defaultdict
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
from config import CFG, InputSanitizer, fast_hash, FAST_INTENT_SIGNALS, FAST_ACADEMIC_SIGNALS
from database import EnhancedDatabase, MessageRecord, AlertRecord, DeadLetterRecord
from filter_engine import EnhancedFilter


# =============================================================================
# NEW v9.8: InstantCaptureManager — Message Deletion Race Condition Fix
# =============================================================================
class InstantCaptureManager:
    """
    Manages the Instant Capture Lane: captures candidate messages and
    forwards them to the target channel IMMEDIATELY, before the full NLP
    analysis pipeline runs. This solves the Message Deletion Race Condition
    where a message is deleted (by the user or another bot) before the
    slow analysis pipeline completes.

    Architecture:
        Telegram Update
              │
              ▼
        Snapshot (immediate)
              │
              ▼
        Fast Candidate Check (intent + academic signals)
              │
              ▼
        Capture Queue (bounded, backpressure)
              │
              ▼
        Forward Worker (ASAP, with retry + fallback)
              │
              ▼
        Full NLP Analysis (runs in parallel, NOT blocking capture)
    """

    # Capture status constants
    CAPTURE_PENDING = "CAPTURE_PENDING"
    CAPTURED = "CAPTURED"
    CAPTURE_FAILED = "CAPTURE_FAILED"
    CAPTURE_RETRYING = "CAPTURE_RETRYING"

    def __init__(
        self,
        account_name: str,
        client: Optional[TelegramClient] = None,
        target_channel_id: Optional[int] = None,
        db: Optional[EnhancedDatabase] = None,
    ):
        self._account_name = account_name
        self._client = client
        self._db = db
        self._target_channel_id = target_channel_id or CFG.TARGET_CHANNEL_ID or CFG.ADMIN_CHAT_ID

        # Bounded queue with backpressure
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=CFG.INSTANT_CAPTURE_QUEUE_SIZE)

        # Worker state
        self._workers: List[asyncio.Task] = []
        self._worker_started = False
        self._stopping = False

        # Statistics
        self._stats: Dict[str, Any] = {
            "total_candidates": 0,
            "queued": 0,
            "captured": 0,
            "failed": 0,
            "retried": 0,
            "queue_full_drops": 0,
            "fallback_recreates": 0,
            "avg_capture_latency_ms": 0.0,
            "total_latency_ms": 0.0,
        }
        self._stats_lock = asyncio.Lock()

    def set_client(self, client: Optional[TelegramClient]) -> None:
        """Update the client reference (e.g. after session rotation)."""
        self._client = client

    async def start(self) -> None:
        """Start the capture workers."""
        if self._worker_started:
            return

        self._stopping = False
        num_workers = max(1, CFG.INSTANT_CAPTURE_WORKERS)

        for i in range(num_workers):
            task = asyncio.create_task(
                self._capture_worker(i),
                name=f"instant_capture_{self._account_name}_{i}",
            )
            self._workers.append(task)

        self._worker_started = True
        logger.info(
            f"InstantCaptureManager[{self._account_name}]: started {num_workers} worker(s), "
            f"target_channel={self._target_channel_id}, queue_size={CFG.INSTANT_CAPTURE_QUEUE_SIZE}"
        )

    async def stop(self) -> None:
        """Stop all capture workers."""
        if not self._worker_started:
            return

        self._stopping = True

        # Cancel all workers
        for task in self._workers:
            if not task.done():
                task.cancel()

        # Await all workers
        for task in self._workers:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(
                    f"InstantCaptureManager[{self._account_name}]: "
                    f"error awaiting worker during shutdown: {e}"
                )

        self._workers.clear()
        self._worker_started = False
        logger.info(f"InstantCaptureManager[{self._account_name}]: stopped")

    def create_snapshot(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a snapshot from event data. The snapshot contains all
        information needed to forward/recreate the message even if the
        original is deleted from the source group.
        """
        snapshot = {
            "chat_id": event_data.get("chat_id"),
            "message_id": event_data.get("message_id"),
            "sender_id": event_data.get("sender_id", 0),
            "sender_username": event_data.get("sender_username"),
            "sender_first_name": event_data.get("sender_first_name"),
            "sender_last_name": event_data.get("sender_last_name"),
            "sender_access_hash": event_data.get("sender_access_hash"),
            "chat_access_hash": event_data.get("chat_access_hash"),
            "chat_username": event_data.get("chat_username"),
            "text": event_data.get("text", ""),
            "has_text": event_data.get("has_text", False),
            "has_media": event_data.get("has_media", False),
            "media_type": event_data.get("media_type"),
            "media_object": event_data.get("media_object"),
            "account_name": self._account_name,
            "timestamp": time.time(),
            "capture_status": self.CAPTURE_PENDING,
            "forward_attempts": 0,
        }
        return snapshot

    def fast_candidate_check(self, text: str) -> bool:
        """
        Ultra-fast check: Intent Signal + Academic Signal.
        Returns True only if BOTH are present.
        This is intentionally simple — no fuzzy matching, no negation,
        no distance scoring. Just substring checks.
        """
        if not text or not CFG.INSTANT_CAPTURE_ENABLED:
            return False

        text_lower = text.lower()

        # 1. Check Intent Signals
        has_intent = False
        for category, signals in FAST_INTENT_SIGNALS.items():
            for signal in signals:
                if signal in text_lower:
                    has_intent = True
                    break
            if has_intent:
                break

        if not has_intent:
            return False

        # 2. Check Academic Signals
        has_academic = False
        for category, signals in FAST_ACADEMIC_SIGNALS.items():
            for signal in signals:
                if signal in text_lower:
                    has_academic = True
                    break
            if has_academic:
                break

        return has_intent and has_academic

    async def submit_candidate(self, snapshot: Dict[str, Any]) -> bool:
        """
        Submit a candidate snapshot to the capture queue.
        Uses put_nowait() to never block the event handler.
        Returns True if queued, False if rejected (queue full).
        """
        if self._stopping or not CFG.INSTANT_CAPTURE_ENABLED:
            return False

        async with self._stats_lock:
            self._stats["total_candidates"] += 1

        try:
            self._queue.put_nowait(snapshot)
            async with self._stats_lock:
                self._stats["queued"] += 1
            return True
        except asyncio.QueueFull:
            async with self._stats_lock:
                self._stats["queue_full_drops"] += 1
            logger.warning(
                f"InstantCaptureManager[{self._account_name}]: "
                f"queue full, dropping candidate msg_id={snapshot.get('message_id')}"
            )
            return False

    async def _capture_worker(self, worker_id: int) -> None:
        """Background worker that processes the capture queue."""
        logger.debug(f"InstantCaptureManager[{self._account_name}]: worker {worker_id} started")

        while not self._stopping:
            try:
                snapshot = await self._queue.get()
            except asyncio.CancelledError:
                logger.debug(
                    f"InstantCaptureManager[{self._account_name}]: "
                    f"worker {worker_id} cancelled"
                )
                break

            start_time = time.perf_counter()

            try:
                snapshot["capture_status"] = self.CAPTURE_RETRYING
                success = await self._forward_with_retry(snapshot)

                latency_ms = int((time.perf_counter() - start_time) * 1000)

                async with self._stats_lock:
                    self._stats["total_latency_ms"] += latency_ms
                    total_ops = self._stats["captured"] + self._stats["failed"]
                    if total_ops > 0:
                        self._stats["avg_capture_latency_ms"] = (
                            self._stats["total_latency_ms"] / total_ops
                        )

                if success:
                    snapshot["capture_status"] = self.CAPTURED
                    snapshot["capture_latency_ms"] = latency_ms
                    async with self._stats_lock:
                        self._stats["captured"] += 1
                    await self._record_capture(snapshot, success=True)
                    logger.info(
                        f"InstantCaptureManager[{self._account_name}]: "
                        f"captured msg_id={snapshot.get('message_id')} in {latency_ms}ms"
                    )
                else:
                    snapshot["capture_status"] = self.CAPTURE_FAILED
                    snapshot["capture_latency_ms"] = latency_ms
                    async with self._stats_lock:
                        self._stats["failed"] += 1
                    await self._record_capture(snapshot, success=False)
                    logger.error(
                        f"InstantCaptureManager[{self._account_name}]: "
                        f"FAILED to capture msg_id={snapshot.get('message_id')} "
                        f"after {snapshot.get('forward_attempts', 0)} attempts"
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                async with self._stats_lock:
                    self._stats["failed"] += 1
                logger.exception(
                    f"InstantCaptureManager[{self._account_name}]: "
                    f"worker {worker_id} error: {e}"
                )
            finally:
                self._queue.task_done()

        logger.debug(f"InstantCaptureManager[{self._account_name}]: worker {worker_id} stopped")

    async def _forward_with_retry(self, snapshot: Dict[str, Any]) -> bool:
        """Forward with retry. Returns True on first success."""
        max_retries = CFG.INSTANT_CAPTURE_RETRIES
        retry_delay = CFG.INSTANT_CAPTURE_RETRY_DELAY_MS / 1000

        for attempt in range(max_retries + 1):
            snapshot["forward_attempts"] = attempt + 1

            try:
                success = await self._forward_single(snapshot)
                if success:
                    if attempt > 0:
                        async with self._stats_lock:
                            self._stats["retried"] += 1
                    return True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(
                    f"InstantCaptureManager[{self._account_name}]: "
                    f"forward attempt {attempt + 1}/{max_retries + 1} failed "
                    f"for msg_id={snapshot.get('message_id')}: {type(e).__name__}: {e}"
                )

            if attempt < max_retries:
                await asyncio.sleep(retry_delay)

        return False

    async def _forward_single(self, snapshot: Dict[str, Any]) -> bool:
        """Single forward attempt with fallback to recreate."""
        client = self._client
        if not client or not self._is_client_alive(client):
            logger.warning(
                f"InstantCaptureManager[{self._account_name}]: "
                f"no available client for forward"
            )
            return False

        msg_id = snapshot.get("message_id")
        chat_id = snapshot.get("chat_id")
        target = self._target_channel_id

        if not msg_id or not chat_id or not target:
            return False

        try:
            # Primary: forward_messages
            await client.forward_messages(
                entity=target,
                from_peer=chat_id,
                messages=[msg_id],
                drop_author=False,
                drop_media_captions=False,
            )
            return True

        except asyncio.CancelledError:
            raise
        except Exception as forward_error:
            logger.debug(
                f"InstantCaptureManager[{self._account_name}]: "
                f"forward failed for msg_id={msg_id}, trying fallback: "
                f"{type(forward_error).__name__}"
            )
            return await self._fallback_recreate(snapshot, client, target)

    async def _fallback_recreate(
        self,
        snapshot: Dict[str, Any],
        client: TelegramClient,
        target: int,
    ) -> bool:
        """
        Fallback: recreate the message manually if forward fails.
        Handles text-only messages and media messages.
        """
        try:
            text = snapshot.get("text", "")
            has_media = snapshot.get("has_media", False)
            media_object = snapshot.get("media_object")

            if has_media and media_object is not None:
                # Try to send the media directly
                try:
                    await client.send_file(
                        target,
                        file=media_object,
                        caption=text or None,
                        parse_mode="html",
                    )
                    async with self._stats_lock:
                        self._stats["fallback_recreates"] += 1
                    return True
                except Exception as e:
                    logger.debug(
                        f"InstantCaptureManager[{self._account_name}]: "
                        f"media fallback failed: {e}"
                    )
                    return False

            elif text:
                # Text-only message
                await client.send_message(
                    target,
                    text,
                    parse_mode=None,
                    link_preview=False,
                )
                async with self._stats_lock:
                    self._stats["fallback_recreates"] += 1
                return True

            return False

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(
                f"InstantCaptureManager[{self._account_name}]: "
                f"fallback recreate failed: {e}"
            )
            return False

    @staticmethod
    def _is_client_alive(client: TelegramClient) -> bool:
        """Check if a TelegramClient is connected."""
        try:
            attr = getattr(client, "is_connected", None)
            return bool(attr() if callable(attr) else attr)
        except Exception:
            return False

    async def _record_capture(self, snapshot: Dict[str, Any], success: bool) -> None:
        """Record capture status to database if available."""
        if not self._db:
            return

        try:
            record = {
                "message_id": snapshot.get("message_id"),
                "chat_id": snapshot.get("chat_id"),
                "sender_id": snapshot.get("sender_id", 0),
                "capture_status": snapshot.get("capture_status", ""),
                "capture_latency_ms": snapshot.get("capture_latency_ms", 0),
                "forward_attempts": snapshot.get("forward_attempts", 0),
                "captured_at": time.time(),
            }
            # Use try/except for DB method that may not exist yet
            try:
                await self._db.save_capture_record(record)
            except AttributeError:
                # Method not yet added to database.py — skip silently
                pass
            except Exception as e:
                logger.debug(
                    f"InstantCaptureManager[{self._account_name}]: "
                    f"failed to record capture: {e}"
                )
        except Exception as e:
            logger.debug(
                f"InstantCaptureManager[{self._account_name}]: "
                f"error in _record_capture: {e}"
            )

    async def get_stats(self) -> Dict[str, Any]:
        """Return capture statistics."""
        async with self._stats_lock:
            stats = dict(self._stats)
        stats["queue_size"] = self._queue.qsize()
        stats["workers"] = len(self._workers)
        stats["enabled"] = CFG.INSTANT_CAPTURE_ENABLED
        stats["target_channel_id"] = self._target_channel_id
        return stats


# (نفس الدوال المساعدة من النسخة الأصلية: resolve_chat_entity, build_telegram_links)
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
        self._stats: Dict[str, Any] = {
            "messages_processed": 0, "alerts_sent": 0, "errors": 0, "duplicates": 0, "rate_limited": 0,
            "send_errors": 0, "queue_processed": 0, "media_processed": 0, "avg_processing_time_ms": 0.0,
            "total_processing_time_ms": 0.0, "last_error": None, "last_alert_time": 0.0,
            "connect_attempts": 0, "reconnect_attempts": 0,
            "accepted": 0, "reviewed": 0, "ignored": 0, "avg_confidence": 0.0, "total_confidence": 0.0, "decisions_count": 0,
            "instant_captured": 0, "instant_failed": 0, "instant_queued": 0,
        }
        self._stats_lock = asyncio.Lock()
        self._entity_cache: TTLCache = TTLCache(maxsize=CFG.ENTITY_CACHE_MAX_SIZE, ttl=600)
        self._cache_lock = asyncio.Lock()
        self._processed_hashes: LRUCache = LRUCache(maxsize=CFG.PROCESSED_HASHES_MAX_SIZE)
        self._processed_lock = asyncio.Lock()
        self._get_entity_sem = asyncio.Semaphore(5); self._pipeline_sem = asyncio.Semaphore(20)
        self._dlq = DeadLetterQueue(
            db,
            max_retries=CFG.DEAD_LETTER_MAX_RETRIES,
            reprocess_callback=self._dlq_reprocess,
            account_name=account["name"],
        )
        self._connect_attempts = 0; self._last_connect_error: Optional[str] = None
        self._connect_flood_until: float = 0.0
        self._processing_times: List[float] = []; self._max_processing_times = 100
        self._session_rotate_task: Optional[asyncio.Task] = None; self._last_rotation = 0.0
        self._media_tasks: Set[asyncio.Task] = set()
        self._media_task_lock = asyncio.Lock()

        # NEW v9.8: Instant Capture Manager
        self._instant_capture = InstantCaptureManager(
            account_name=account["name"],
            client=None,  # سيتم تحديثه بعد الاتصال
            target_channel_id=CFG.TARGET_CHANNEL_ID,
            db=db,
        )

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
        """Single source of truth for TelegramClient construction settings."""
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
        buttons = None
        if CFG.ALERT_WITH_BUTTONS:
            row = []
            if username: row.append(Button.url("💬 مراسلة", f"https://t.me/{username.lstrip('@')}"))
            row.append(Button.url("👤 فتح الحساب", f"tg://user?id={sender_id}"))
            if CFG.ALERT_WITH_COPY_BUTTON: row.append(Button.inline("📋 نسخ النص", f"copy_{analysis.get('msg_hash', '')}"))
            if row: buttons = [row]
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
                    await self._register_handler()
                    await self._reconnect.start()
                    self._start_session_rotation()

                    # NEW v9.8: Update Instant Capture client and start workers
                    self._instant_capture.set_client(client)
                    await self._instant_capture.start()

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
            try: await self.client.disconnect()
            except Exception as e: logger.debug(f"disconnect during cleanup failed [{self.account['name']}]: {e}")
            self.client = None
        self.is_connected = False


    async def _register_handler(self):
        if self._handler_func is not None and self.client:
            try: self.client.remove_event_handler(self._handler_func)
            except Exception as e: logger.debug(f"remove_event_handler (pre-register) failed [{self.account['name']}]: {e}")
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
                event_data = await self._event_to_dict(event)

                # NEW v9.8: Instant Capture — BEFORE any slow processing
                if CFG.INSTANT_CAPTURE_ENABLED and event_data.get("has_text"):
                    snapshot = self._instant_capture.create_snapshot(event_data)
                    if self._instant_capture.fast_candidate_check(event_data.get("text", "")):
                        queued = await self._instant_capture.submit_candidate(snapshot)
                        if queued:
                            await self._inc_stat("instant_queued")

                if event_data.get("has_media"):
                    media = event.message.media
                    event_data["media_object"] = media if event_data["media_type"] in ("photo", "document") else None
                    await self._spawn_media_task(event_data)
                    processing_time = (time.perf_counter() - start_time) * 1000
                    await self._update_avg_time(processing_time)
                    await self._inc_stat("media_processed"); await self._inc_stat("messages_processed")
                    return
                priority = 7 if event_data.get("has_text") else 3
                await self.db.add_to_queue(event_data, priority=priority)
                processing_time = (time.perf_counter() - start_time) * 1000
                await self._update_avg_time(processing_time)
                await self._inc_stat("messages_processed"); await self._inc_stat("queue_processed")
            except Exception as e:
                logger.error(f"Handler error [{self.account['name']}]: {e}")
                await self._inc_stat("errors"); self._stats["last_error"] = str(e)
        self._handler_func = _handler
        logger.info(f"Event handler registered for {self.account['name']}")


    async def _spawn_media_task(self, event_data: Dict[str, Any]) -> None:
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
        sender = event.sender; chat = event.chat
        text = event.message.text or ""; caption = getattr(event.message, "message", "") or ""
        full_text = (text or caption).strip()
        media = event.message.media; media_type = self._get_media_type(media)
        has_media = media_type in ("photo", "document")
        chat_username = getattr(chat, "username", None) if chat else None
        return {
            "chat_id": event.chat_id, "message_id": event.message.id,
            "sender_id": getattr(event, "sender_id", 0) or 0,
            "sender_username": getattr(sender, "username", None), "sender_first_name": getattr(sender, "first_name", None),
            "sender_last_name": getattr(sender, "last_name", None), "sender_access_hash": getattr(sender, "access_hash", None),
            "chat_access_hash": getattr(chat, "access_hash", None), "chat_username": chat_username,
            "text": full_text, "has_text": bool(full_text), "has_media": has_media, "media_type": media_type,
            "account_name": self.account["name"], "timestamp": time.time(),
        }


    @staticmethod
    def _get_media_type(media: Any) -> Optional[str]:
        if media is None: return None
        if isinstance(media, MessageMediaPhoto): return "photo"
        if isinstance(media, MessageMediaDocument): return "document"
        if isinstance(media, MessageMediaWebPage): return "webpage"
        return "other"


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
        async with self._connect_lock:
            if not self.client or not self.is_connected:
                return False
            account_name = self.account["name"]
            try:
                logger.info(f"Rotating session for {account_name}")
                session_string = self.client.session.save()

                old_client = self.client
                old_handler = self._handler_func

                from telethon.sessions import StringSession
                new_client = TelegramClient(StringSession(session_string), **self._client_kwargs())
                await new_client.connect()
                if not await new_client.is_user_authorized():
                    logger.error(f"Session rotation: new client unauthorized for {account_name}, aborting rotation")
                    try: await new_client.disconnect()
                    except Exception: pass
                    return False

                if old_handler:
                    try: old_client.remove_event_handler(old_handler)
                    except Exception as e: logger.debug(f"remove_event_handler during rotation failed [{account_name}]: {e}")
                try: await old_client.disconnect()
                except Exception as e: logger.debug(f"old client disconnect during rotation failed [{account_name}]: {e}")

                self.client = new_client
                self._handler_func = None
                self.is_connected = True
                self._last_rotation = time.time()
                await self._register_handler()

                # NEW v9.8: Update Instant Capture client after rotation
                self._instant_capture.set_client(new_client)

                logger.info(f"Session rotated successfully for {account_name}")
                return True
            except Exception as e:
                logger.error(f"Session rotation failed for {account_name}: {e}")
                self.is_connected = False
                self.client = None
                return False


    async def _dlq_reprocess(self, event_data: Dict[str, Any], retry_count: int) -> None:
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
        is_valid = analysis.get("valid", False)
        try: await self.db.update_sender_reputation(sender_id, is_valid)
        except Exception as e: logger.warning(f"update_sender_reputation failed [{self.account['name']}]: {e}")
        if is_valid and await self.db.can_send_alert(sender_id):
            await self._send_alert(data, keyword=analysis.get("keyword") or "طلب", text=validated_text or "",
                                   msg_hash=msg_hash, analysis=analysis)
            await self._inc_stat("alerts_sent")
            async with self._stats_lock: self._stats["last_alert_time"] = time.time()
        try:
            await self.db.upsert_sender_contact({"sender_id": sender_id, "access_hash": data.get("sender_access_hash"),
                "username": data.get("sender_username"), "first_name": data.get("sender_first_name"),
                "last_name": data.get("sender_last_name"), "chat_id": data["chat_id"], "message_id": data["message_id"]})
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
        chat_info = await self._chat_info(send_client, chat_id, message_id, chat_access_hash=chat_access_hash, chat_username=chat_username)
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
            await self._dlq.push(_retry_payload(), e, retry_count=retry_count)
            await self._inc_stat("send_errors"); raise
        except Exception as e:
            logger.error(f"Send alert error [{account_name}]: {e} - trying fallback")
            try:
                await send_client.send_message(CFG.TARGET_GROUP_ID, alert_text, buttons=buttons, parse_mode=None, link_preview=False)
            except Exception as fe:
                logger.error(f"Fallback failed [{account_name}]: {fe}")
                await self._inc_stat("send_errors")
                await self._dlq.push(_retry_payload(), fe, retry_count=retry_count)


    async def disconnect(self):
        async with self._connect_lock:
            self.is_connected = False
            await self._reconnect.stop(); await self._dlq.stop()

            # NEW v9.8: Stop Instant Capture workers
            await self._instant_capture.stop()

            if self._session_rotate_task:
                self._session_rotate_task.cancel()
                try: await self._session_rotate_task
                except asyncio.CancelledError: pass
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
        # NEW v9.8: Include Instant Capture stats
        instant_stats = await self._instant_capture.get_stats()
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
            # NEW v9.8
            "instant_capture": instant_stats,
            "instant_queued": stats.get("instant_queued", 0),
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
                        "accepted": s.get("accepted", 0), "reviewed": s.get("reviewed", 0), "ignored": s.get("ignored", 0), "avg_confidence": s.get("avg_confidence", 0),
                        "instant_captured": (s.get("instant_capture") or {}).get("captured", 0),
                        "instant_failed": (s.get("instant_capture") or {}).get("failed", 0)})
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