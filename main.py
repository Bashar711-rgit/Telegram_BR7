#!/usr/bin/env python3
"""
main.py – Enhanced Telegram Bot v13.1 (RENDER CLOUD EDITION, HARDENED)
متوافق مع استضافة Render المجانية (Web Service) – تشغيل 24/7
متوافق مع: monitors.py v9.7, config.py v13.1, filter_engine.py v14.1, database.py v9.0

v13.1 (this pass) — targeted fixes, main.py ONLY. Maps to the audit's M-01..M-17:

  M-01/M-02/M-03/M-13/M-14/M-15 — MemoryMonitor rewritten:
      * tracemalloc is now actually used: on a suspected leak it takes a
        snapshot and diffs it against the previous one, logging the top
        allocating lines instead of running with no diagnostic payoff.
      * "leak_suspected" now requires a SUSTAINED upward trend across
        several consecutive samples (not a single current>baseline*1.5
        crossing), which is far less prone to false positives from
        caches filling up naturally right after boot.
      * RSS growth ("rss_elevated") and leak suspicion ("leak_suspected")
        are now separate fields — a message is never labeled "memory
        leak" just because RSS is temporarily elevated.
      * Disk/queue pressure (M-15) is surfaced independently via
        database.py's own db_healthy / queue_evictions signals rather
        than being folded into the RAM-only heuristic.

  M-04 — processing_queue capacity: database.py v9.0 now enforces
      CFG.MESSAGE_QUEUE_SIZE as a real cap on the persistent SQLite queue
      (DROP_OLDEST eviction) and exposes queue_evictions / db_healthy.
      main.py's role here is now just to surface those signals (stats
      reporter, /health, /status) instead of pretending MESSAGE_QUEUE_SIZE
      only bounds the small in-memory internal_queue.

  M-05/M-16 — Background task lifecycle unified: every task created by
      this module (including the SIGINT/SIGTERM shutdown task, which was
      previously pure fire-and-forget) is tracked in a bounded set via
      _track_task(), with exceptions captured on completion and all tasks
      cancelled+awaited during stop().

  M-06/M-12 — Duplicate cleanup: database.py v9.0 removed its own
      _cleanup_loop entirely. main.py::_cleanup_loop is now the single,
      documented owner of database maintenance.

  M-07 — Keep-alive no longer falls back to 127.0.0.1. If
      RENDER_EXTERNAL_URL is unset, keep-alive is explicitly DISABLED
      (with a clear warning) rather than pinging localhost and giving a
      false sense of protection against Render free-tier sleep.

  M-08/M-09/M-17 — Statistics contract fixed: /status now reads
      `alerts_sent` (the field monitors.py's get_stats() actually
      returns) instead of the non-existent `alerts` key, and includes
      errors/dead-letter counts for real operational visibility. No
      per-account "queued" field is fabricated anywhere in this file —
      queue size is only ever reported as the single global metric
      (db.queue_size()), which was already correct.

  M-10/M-11 — Main-client failover: a dedicated, lightweight
      _main_client_watchdog_loop checks main_client liveness on a short
      interval (independent of the much heavier _health_check_loop),
      shortening the detection-to-failover window. Failover logic is now
      a single shared method (_failover_main_client) called from both the
      watchdog and the health-check loop, and — a real gap in the
      original code — it now RE-REGISTERS the admin-command and copy
      handlers on the newly-promoted client, since those were previously
      only ever registered once on the original main_client and would
      silently go unresponsive after a failover.
"""

from __future__ import annotations

import asyncio
import gc
import os
import signal
import sys
import time
import tracemalloc
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Set

# محاولة استيراد uvloop بشكل آمن (قد لا يكون مثبتاً في Pydroid 3)
try:
    import uvloop
    UVLOOP_AVAILABLE = True
except ImportError:
    UVLOOP_AVAILABLE = False

# محاولة استيراد aiohttp بشكل آمن
try:
    from aiohttp import web, ClientSession, ClientTimeout
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

from telethon import TelegramClient, events as tl_events
from telethon.errors import FloodWaitError
from telethon.tl.types import InputPeerUser

from config import (
    CFG,
    ACCOUNTS,
    KEYWORDS,
    logger,
    get_contact_templates,
    contact_template_labels,
    InputSanitizer,
)
from database import EnhancedDatabase
from dedup import get_dedup_snapshot, init_deduplicator
from antispam import get_antispam, setup_antispam, get_antispam_snapshot
from filter_engine import EnhancedFilter
from monitors import EnhancedAccountMonitor, HealthMonitor, get_capture_snapshot
from sender_resolver import get_sender_intel_snapshot

# Import Dashboard
try:
    from dashboard import set_bot_reference, start_dashboard as run_dashboard
    DASHBOARD_AVAILABLE = True
except ImportError:
    DASHBOARD_AVAILABLE = False
    logger.warning("dashboard.py not found – Dashboard disabled")

# psutil optional
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    logger.warning("psutil not installed. Memory monitoring will be limited.")


# =============================================================================
# Adaptive Rate Limiter (Token Bucket + Per-Account) — unchanged
# =============================================================================
class AdaptiveRateLimiter:
    def __init__(
        self,
        max_per_minute: int = 20,
        max_per_hour: int = 200,
        burst_size: int = 5,
    ) -> None:
        self._max_min = max_per_minute
        self._max_hr = max_per_hour
        self._burst = burst_size
        self._tokens = float(burst_size)
        self._last_refill = time.monotonic()
        self._token_lock = asyncio.Lock()
        self._min_win: Deque[float] = deque()
        self._hr_win: Deque[float] = deque()
        self._win_lock = asyncio.Lock()
        self._acct: Dict[str, Deque[float]] = {}
        self._acct_lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        while self._min_win and self._min_win[0] < now - 60:
            self._min_win.popleft()
        while self._hr_win and self._hr_win[0] < now - 3600:
            self._hr_win.popleft()

    def _refill(self, now: float) -> None:
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * (self._max_min / 60.0))
        self._last_refill = now

    async def can_proceed(self, account_name: str = "default") -> bool:
        async with self._token_lock:
            now = time.monotonic()
            self._refill(now)
            async with self._win_lock:
                self._prune(now)
                if len(self._min_win) >= self._max_min:
                    return False
                if len(self._hr_win) >= self._max_hr:
                    return False
                if self._tokens < 1.0:
                    return False
                async with self._acct_lock:
                    if account_name not in self._acct:
                        self._acct[account_name] = deque()
                    acct_win = self._acct[account_name]
                    while acct_win and acct_win[0] < now - 60:
                        acct_win.popleft()
                    per_acct_limit = max(1, self._max_min // max(len(self._acct), 1))
                    if len(acct_win) >= per_acct_limit:
                        return False
                    self._tokens -= 1
                    self._min_win.append(now)
                    self._hr_win.append(now)
                    acct_win.append(now)
                    return True

    def status(self) -> Dict[str, Any]:
        now = time.monotonic()
        self._prune(now)
        return {
            "per_min": len(self._min_win),
            "limit_min": self._max_min,
            "per_hr": len(self._hr_win),
            "limit_hr": self._max_hr,
            "tokens": round(self._tokens, 2),
            "burst": self._burst,
        }


# =============================================================================
# Memory Monitor — v2 (hardened, see module docstring M-01/02/03/13/14/15)
# =============================================================================
class MemoryMonitor:
    """
    Separates three previously-conflated concerns:
      1. RSS observation (always recorded).
      2. "rss_elevated" — a simple, honestly-labeled fact: current RSS is
         above baseline*growth_ratio right now. This is NOT a leak claim.
      3. "leak_suspected" — only raised after a SUSTAINED upward trend
         across `sustained_samples` consecutive check() calls, which
         rules out the common false-positive of caches filling up once
         after boot and then staying flat.
    tracemalloc is genuinely used: a snapshot is taken (and diffed against
    the previous one) only when a leak is actually suspected, so the
    resulting log line names real top-allocating source lines.
    """

    def __init__(
        self,
        threshold_mb: int = 512,
        history_size: int = 10,
        sustained_samples: int = 5,
        growth_ratio: float = 1.5,
    ) -> None:
        self._threshold = threshold_mb * 1024 * 1024
        self._baseline = 0
        self._peak = 0
        self._history: Deque[int] = deque(maxlen=history_size)
        self._sustained_samples = sustained_samples
        self._growth_ratio = growth_ratio
        self._tracemalloc_enabled = False
        self._last_snapshot = None  # type: ignore[assignment]

    def start(self) -> None:
        try:
            tracemalloc.start()
            self._tracemalloc_enabled = True
            self._last_snapshot = tracemalloc.take_snapshot()
        except Exception as e:
            logger.warning(f"tracemalloc unavailable, leak diagnostics limited: {e}")
            self._tracemalloc_enabled = False
        self._baseline = self._current_rss()
        self._peak = self._baseline
        self._history.clear()
        self._history.append(self._baseline)

    def _current_rss(self) -> int:
        if PSUTIL_AVAILABLE:
            try:
                return psutil.Process(os.getpid()).memory_info().rss
            except Exception:
                pass
        return 0

    def check(self) -> Dict[str, Any]:
        current = self._current_rss()
        self._peak = max(self._peak, current)
        self._history.append(current)

        rss_elevated = current > self._baseline * self._growth_ratio if self._baseline > 0 else False

        # Sustained-trend heuristic (M-02): require the last N samples to
        # ALL be elevated AND roughly non-decreasing, not just the latest
        # one crossing a threshold.
        leak_suspected = False
        if self._baseline > 0 and len(self._history) >= self._sustained_samples:
            recent = list(self._history)[-self._sustained_samples:]
            all_elevated = all(v > self._baseline * self._growth_ratio for v in recent)
            non_decreasing = all(b >= a * 0.98 for a, b in zip(recent, recent[1:]))
            leak_suspected = all_elevated and non_decreasing

        result: Dict[str, Any] = {
            "current_mb": current // (1024 * 1024),
            "peak_mb": self._peak // (1024 * 1024),
            "baseline_mb": self._baseline // (1024 * 1024),
            "rss_elevated": rss_elevated,
            "leak_suspected": leak_suspected,
            "tracemalloc_top": None,
            # Backward-compat field name kept for any external code that
            # still reads "leak_detected" — mapped onto the new, stricter
            # leak_suspected signal rather than the old noisy heuristic.
            "leak_detected": leak_suspected,
        }

        if leak_suspected and self._tracemalloc_enabled:
            result["tracemalloc_top"] = self._diagnose()

        return result

    def _diagnose(self) -> Optional[List[str]]:
        try:
            snapshot = tracemalloc.take_snapshot()
            if self._last_snapshot is not None:
                diffs = snapshot.compare_to(self._last_snapshot, "lineno")
                top = [str(d) for d in diffs[:5]]
            else:
                stats = snapshot.statistics("lineno")
                top = [str(s) for s in stats[:5]]
            self._last_snapshot = snapshot
            return top
        except Exception as e:
            logger.debug(f"tracemalloc diagnose failed: {e}")
            return None

    def force_gc(self) -> int:
        gc.collect()
        return self._current_rss()


# =============================================================================
# Main Bot Class v13.1 (hardened)
# =============================================================================
class EnhancedTelegramBot:
    def __init__(self) -> None:
        self.db = EnhancedDatabase()
        self.filter = EnhancedFilter()
        self.rate_limiter = AdaptiveRateLimiter(
            CFG.MAX_ALERTS_PER_MINUTE,
            CFG.MAX_ALERTS_PER_HOUR,
        )
        self.memory_monitor = MemoryMonitor()
        self.main_client: Optional[TelegramClient] = None
        self.monitors: List[EnhancedAccountMonitor] = []
        self.is_running = False
        self._start_time = time.monotonic()
        self.health = HealthMonitor(self)

        # ── Background task lifecycle (M-05 / M-16) ─────────────────────
        # Every task this module creates is tracked here so shutdown can
        # cancel+await all of them uniformly, and so a task's exception is
        # never silently lost ("Task exception was never retrieved").
        self._background_tasks: Set[asyncio.Task] = set()
        self._shutdown_event = asyncio.Event()

        # Individual references kept too, purely for readability/debugging
        # (all of these are also present in _background_tasks).
        self._consumer_task: Optional[asyncio.Task] = None
        self._stats_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        # v9.12 (audit H-08): dedicated short-cadence loop for alert_dedup.
        self._dedup_cleanup_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._memory_task: Optional[asyncio.Task] = None
        self._health_server_task: Optional[asyncio.Task] = None
        self._dashboard_task: Optional[asyncio.Task] = None
        self._keep_alive_task: Optional[asyncio.Task] = None
        self._main_client_watchdog_task: Optional[asyncio.Task] = None

        # ── Main-client failover (M-10 / M-11) ──────────────────────────
        self._main_client_lock = asyncio.Lock()
        self._admin_handler_func: Any = None
        self._copy_handler_func: Any = None
        # v9.11: معالجات زر «تواصل مع المرسل» + الردود الرقمية في مجموعة التنبيهات
        self._contact_handler_funcs: List[Any] = []
        self._target_reply_handler_func: Any = None
        # خريطة (رسالة التنبيه في المجموعة الهدف → بصمة الرسالة) لدعم مسار
        # الرد البديل: رد بـ«تواصل» أو برقم 1-5 على أي تنبيه/قائمة.
        self._alert_msg_map: Dict[int, str] = {}

    # ─── Task tracking helper (M-05 / M-16) ────────────────────────────────
    def _track_task(self, coro: Any, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)

        def _on_done(t: asyncio.Task) -> None:
            self._background_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error(f"Unhandled exception in background task '{name}': {exc}")

        task.add_done_callback(_on_done)
        return task

    # ─── Consumer Loop (Producer-Consumer) ────────────────────────────────────
    async def _consumer_loop(self) -> None:
        """
        NOTE on queue capacity (M-04): `internal_queue` below is only the
        small in-process handoff buffer between the DB-poller and the
        worker pool. The actual capacity limit that matters —
        CFG.MESSAGE_QUEUE_SIZE — is now enforced directly inside
        database.py's add_to_queue() (DROP_OLDEST eviction under the DB's
        own lock), so persistent SQLite growth is bounded regardless of
        this in-memory buffer's size. This loop no longer needs to (and
        does not) duplicate that capacity logic.
        """
        logger.info("Consumer loop started")
        internal_queue: asyncio.Queue = asyncio.Queue(maxsize=CFG.MESSAGE_QUEUE_SIZE)

        async def producer():
            while self.is_running:
                event_data = None
                try:
                    event_data = await self.db.pop_from_queue()
                    if event_data:
                        await internal_queue.put(event_data)
                        # v9.12 (audit M-07): the put succeeded — clear the
                        # local reference so a CancelledError below does
                        # NOT trigger a re-enqueue of an already-queued item.
                        event_data = None
                    else:
                        await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    # v9.12 (audit M-07): graceful shutdown window — if we
                    # were cancelled BETWEEN pop_from_queue (which deletes
                    # the row from the DB) and internal_queue.put (which
                    # hands it to a worker), the event is in our local
                    # `event_data` and would be lost. Re-enqueue it to the
                    # DB so the next process picks it up. Best-effort; if
                    # the DB is also shutting down we log and drop.
                    if event_data is not None:
                        try:
                            await self.db.add_to_queue(event_data, priority=9)  # v9.12 (audit M-14): high priority for DLQ/shutdown re-inserts
                            logger.info("Producer shutdown: re-enqueued 1 in-flight event to DB (M-07 ack-after-pop)")
                        except Exception as e:
                            logger.error(f"Producer shutdown: failed to re-enqueue in-flight event: {e}")
                    break
                except Exception as e:
                    logger.error(f"Producer error: {e}")
                    # v9.12 (audit M-07): same window for non-Cancelled
                    # exceptions — don't lose the in-flight event.
                    if event_data is not None:
                        try:
                            await self.db.add_to_queue(event_data, priority=9)  # v9.12 (audit M-14): high priority for DLQ/shutdown re-inserts
                        except Exception:
                            pass
                    await asyncio.sleep(1)

        async def worker(worker_id: int):
            while self.is_running:
                event_data = None
                try:
                    event_data = await asyncio.wait_for(internal_queue.get(), timeout=1.0)
                    account_name = event_data.get("account_name", "")
                    monitor = self._get_monitor(account_name)
                    if monitor:
                        await monitor.process_event_from_queue(event_data)
                    else:
                        # v9.12 (audit M-08): the source-account monitor is
                        # missing/disconnected. The old behaviour handed the
                        # event to the FIRST connected monitor — which then
                        # attributed the alert to that account in stats and
                        # DB, even though the event actually arrived via a
                        # different account. We now mark the event with the
                        # real source account name (preserved in
                        # event_data["account_name"]) AND set a
                        # "_attributed_to" field so downstream stats can
                        # distinguish "processed by X on behalf of
                        # disconnected Y" from "processed by X originally".
                        # The first connected monitor still does the actual
                        # processing (it has a live Telegram client); we
                        # just don't silently re-attribute it.
                        event_data["_attributed_to"] = account_name or "unknown"
                        for m in self.monitors:
                            if m.is_connected:
                                await m.process_event_from_queue(event_data)
                                break
                        else:
                            # No connected monitor at all — re-enqueue for
                            # later instead of dropping the event.
                            logger.warning(
                                f"No connected monitor to process event from {account_name}; re-enqueueing"
                            )
                            try:
                                await self.db.add_to_queue(event_data, priority=9)
                            except Exception:
                                pass
                    internal_queue.task_done()
                    # v9.12 (audit M-07 / M-05 related): clear the local
                    # reference after successful processing so the
                    # except-branch doesn't try to re-enqueue an item that
                    # was actually handled (process_event_from_queue never
                    # raises for a handled error — it routes to DLQ).
                    event_data = None
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    # v9.12 (audit M-07): on shutdown, if we pulled an
                    # item from internal_queue but didn't finish processing
                    # it, re-enqueue to DB so the next process picks it up.
                    # The item was already popped from the persistent queue
                    # by the producer, so without this re-enqueue it would
                    # be lost on shutdown.
                    if event_data is not None:
                        try:
                            await self.db.add_to_queue(event_data, priority=9)  # v9.12 (audit M-14): high priority for DLQ/shutdown re-inserts
                            logger.info(f"Worker {worker_id} shutdown: re-enqueued 1 in-flight event to DB (M-07)")
                        except Exception as e:
                            logger.error(f"Worker {worker_id} shutdown: failed to re-enqueue in-flight event: {e}")
                    break
                except Exception as e:
                    logger.error(f"Worker {worker_id} error: {e}")
                    # v9.12 (audit M-07): unhandled exception after pulling
                    # from internal_queue — the item is no longer in the
                    # queue and was not processed. The DLQ inside
                    # process_event_from_queue should have caught the
                    # error; if we're here, the error was in our own
                    # routing logic. Re-enqueue to preserve at-least-once.
                    if event_data is not None:
                        try:
                            await self.db.add_to_queue(event_data, priority=9)  # v9.12 (audit M-14): high priority for DLQ/shutdown re-inserts
                        except Exception:
                            pass
                    await asyncio.sleep(1)

        producer_task = asyncio.create_task(producer(), name="producer")
        workers = [asyncio.create_task(worker(i), name=f"worker_{i}") for i in range(CFG.PROCESSING_WORKERS)]

        try:
            await asyncio.gather(producer_task, *workers, return_exceptions=True)
        except asyncio.CancelledError:
            pass

    def _get_monitor(self, name: str) -> Optional[EnhancedAccountMonitor]:
        name_lower = name.lower()
        for m in self.monitors:
            if m.account["name"].lower() == name_lower:
                return m
        return None

    # ─── Admin / Copy handler registration (idempotent, re-runnable) ──────────
    # Both are now re-registerable so a main-client failover (M-10/M-11)
    # can re-attach them to the newly-promoted client instead of leaving
    # admin commands silently unresponsive.
    async def _register_copy_handler(self) -> None:
        if not self.main_client:
            return
        if self._copy_handler_func is not None:
            try:
                self.main_client.remove_event_handler(self._copy_handler_func)
            except Exception as e:
                logger.debug(f"remove copy handler failed: {e}")

        @self.main_client.on(tl_events.CallbackQuery(pattern=r"copy_"))
        async def copy_handler(event: tl_events.CallbackQuery.Event):
            try:
                msg_hash = event.data.decode().split("_", 1)[1]
                text = await self.db.get_alert_text_by_hash(msg_hash) or await self.db.get_message_text_by_hash(msg_hash)
                if text:
                    await event.answer(text[:200] + ("..." if len(text) > 200 else ""), alert=True)
                else:
                    await event.answer("النص غير متوفر حالياً", alert=True)
            except Exception as e:
                logger.error(f"Copy handler error: {e}")
                await event.answer("حدث خطأ أثناء محاولة نسخ النص", alert=True)

        self._copy_handler_func = copy_handler
        logger.info("Copy button callback handler registered")

    # ─── Contact-button handlers (v9.11 — زر «تواصل مع المرسل») ──────────────
    def _note_alert_message(self, msg_id: Any, msg_hash: str) -> None:
        """سجّل موضع رسالة التنبيه/القائمة في المجموعة الهدف (خريطة محدودة).

        تدعم مسار الرد البديل: رد المشرف بـ«تواصل» أو برقم على رسالة
        التنبيه نفسها. الخريطة في الذاكرة فقط (تعاد التعبئة مع كل تنبيه جديد).
        """
        try:
            mid = int(msg_id)
        except (TypeError, ValueError):
            return
        if not msg_hash:
            return
        try:
            if len(self._alert_msg_map) > 2000:
                keep = list(self._alert_msg_map.items())[-1000:]
                self._alert_msg_map = dict(keep)
            self._alert_msg_map[mid] = msg_hash
        except Exception:
            pass

    async def _send_contact_template(self, msg_hash: str, index: int) -> tuple:
        """يرسل الرسالة الجاهزة رقم index إلى المرسل الأصلي للتنبيه مباشرة.

        حل المرسل (الأفضل أولاً): username → InputPeerUser(access_hash) →
        get_entity(sender_id) — أفضل الحلول المتاحة ضمن Telethon.
        """
        templates = get_contact_templates()
        if not (1 <= index <= len(templates)):
            return False, "رقم الرسالة الجاهزة غير صالح"
        info = await self.db.get_alert_sender_by_hash(msg_hash)
        if not info:
            return False, "تعذر العثور على بيانات المرسل الأصلي (تنبيه قديم؟)"
        sender_id = info.get("sender_id")
        if not sender_id:
            return False, "معرّف المرسل غير متوفر"
        client = self.main_client
        if client is None:
            return False, "لا يوجد اتصال متاح حالياً"
        text = templates[index - 1]
        username = (info.get("username") or "").strip().lstrip("@")
        access_hash = info.get("access_hash")
        peer = None
        if username:
            peer = username
        elif access_hash:
            try:
                peer = InputPeerUser(user_id=int(sender_id), access_hash=int(access_hash))
            except (TypeError, ValueError):
                peer = None
        try:
            if peer is None:
                # آخر خيار متاح: الحل بالمعرّف الخام (ينجح إذا كان الحساب قد
                # رأى هذا المرسل في مجموعة مشتركة)
                await client.send_message(int(sender_id), text, link_preview=False)
            else:
                await client.send_message(peer, text, link_preview=False)
            logger.info(
                f"Contact template #{index} sent to sender={sender_id} "
                f"(msg_hash={str(msg_hash)[:12]}...) by supervisor flow"
            )
            return True, "تم إرسال الرسالة إلى المرسل"
        except Exception as e:
            logger.warning(f"Contact template send failed sender={sender_id}: {e}")
            hint = "المستخدم حظر الرسائل الخاصة" if "block" in str(e).lower() else str(e)[:100]
            return False, f"فشل الإرسال: {hint}"

    async def _register_contact_handlers(self) -> None:
        """معالجات زر «تواصل مع المرسل» — المسار الأساسي والاحتياطي.

        المسار الأساسي (Inline callbacks — مثل نمط زر النسخ القائم):
            cnt_{hash}    → إظهار قائمة الرسائل الجاهزة كاملة.
            tpl{i}_{hash} → إرسال الرسالة الجاهزة i إلى صاحب الطلب مباشرة.
            cnx_{hash}    → إلغاء وحذف رسالة القائمة.

        المسار الاحتياطي (رد في مجموعة التنبيهات — يعمل دائماً حتى لو لم
        تُسلَّم أحداث الأزرار التفاعلية لحساب مستخدم):
            الرد على التنبيه بـ «تواصل» → إظهار القائمة.
            الرد على التنبيه/القائمة برقم 1..N → إرسال الرسالة المختارة.
        """
        if not self.main_client:
            return
        for fn in list(self._contact_handler_funcs):
            try:
                self.main_client.remove_event_handler(fn)
            except Exception as e:
                logger.debug(f"remove contact handler failed: {e}")
        self._contact_handler_funcs = []
        if self._target_reply_handler_func is not None:
            try:
                self.main_client.remove_event_handler(self._target_reply_handler_func)
            except Exception as e:
                logger.debug(f"remove target-reply handler failed: {e}")
            self._target_reply_handler_func = None

        from telethon import Button  # local import — نفس كائن Button في monitors
        from config import InputSanitizer

        templates = get_contact_templates()
        labels = contact_template_labels(len(templates))
        client = self.main_client

        def _picker_text(msg_hash: str) -> str:
            lines = ["📨 <b>الرسائل الجاهزة — اختر واحدة لإرسالها إلى صاحب الطلب:</b>", ""]
            for i, t in enumerate(templates, 1):
                lines.append(f"{i}️⃣ {InputSanitizer.escape_html(InputSanitizer.truncate(t, 200))}")
            lines.append("")
            lines.append("اضغط الزر المناسب، أو رد على هذه الرسالة برقم الخيار.")
            return "\n".join(lines)

        def _picker_buttons(msg_hash: str) -> list:
            rows: list = []
            row: list = []
            for i in range(1, len(templates) + 1):
                row.append(Button.inline(f"{i} {labels[i - 1]}", f"tpl{i}_{msg_hash}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            rows.append([Button.inline("✖ إلغاء", f"cnx_{msg_hash}")])
            return rows

        async def _show_picker(chat_key: Any, msg_hash: str, reply_to: Any = None) -> None:
            sent = await client.send_message(
                chat_key if chat_key else CFG.TARGET_GROUP_ID,
                _picker_text(msg_hash),
                buttons=_picker_buttons(msg_hash),
                parse_mode="html",
                link_preview=False,
                reply_to=reply_to,
            )
            if sent is not None and getattr(sent, "id", None):
                self._note_alert_message(sent.id, msg_hash)

        # ── cnt_: إظهار قائمة الرسائل الجاهزة ─────────────────────────────
        @client.on(tl_events.CallbackQuery(pattern=r"^cnt_"))
        async def _cnt_handler(event):
            try:
                msg_hash = event.data.decode("utf-8", "ignore").split("_", 1)[1]
                info = await self.db.get_alert_sender_by_hash(msg_hash)
                if not info:
                    await event.answer("⚠️ تعذر العثور على بيانات المرسل الأصلي", alert=True)
                    return
                await _show_picker(
                    getattr(event, "chat_id", None) or CFG.TARGET_GROUP_ID,
                    msg_hash,
                    reply_to=getattr(event, "message_id", None),
                )
                await event.answer()
            except Exception as e:
                logger.error(f"cnt_ handler error: {e}")
                try:
                    await event.answer("حدث خطأ — أعد المحاولة", alert=True)
                except Exception:
                    pass

        # ── tpl{i}_: إرسال الرسالة الجاهزة المختارة إلى المرسل ────────────
        @client.on(tl_events.CallbackQuery(pattern=r"^tpl\d+_"))
        async def _tpl_handler(event):
            try:
                raw = event.data.decode("utf-8", "ignore")
                head, _, msg_hash = raw.partition("_")
                index = int(head[3:])
                ok, msg = await self._send_contact_template(msg_hash, index)
                if ok:
                    try:
                        await event.delete()  # تنظيف رسالة القائمة
                    except Exception:
                        pass
                await event.answer(f"✅ {msg}" if ok else f"⚠️ {msg}", alert=not ok)
            except Exception as e:
                logger.error(f"tpl handler error: {e}")
                try:
                    await event.answer("حدث خطأ أثناء الإرسال", alert=True)
                except Exception:
                    pass

        # ── cnx_: إلغاء القائمة ────────────────────────────────────────────
        @client.on(tl_events.CallbackQuery(pattern=r"^cnx_"))
        async def _cnx_handler(event):
            try:
                try:
                    await event.delete()
                except Exception:
                    pass
                await event.answer("تم الإلغاء")
            except Exception:
                pass

        self._contact_handler_funcs = [_cnt_handler, _tpl_handler, _cnx_handler]

        # ── مسار الرد البديل في مجموعة التنبيهات ───────────────────────────
        target_id = CFG.TARGET_GROUP_ID
        if target_id:
            @client.on(tl_events.NewMessage(chats=target_id, incoming=True))
            async def _target_reply_handler(event):
                try:
                    if not event.is_reply:
                        return
                    text = (event.message.text or "").strip()
                    if not text:
                        return
                    replied_id = getattr(event.message, "reply_to_msg_id", None)
                    if replied_id is None or replied_id not in self._alert_msg_map:
                        return  # ليس رداً على تنبيه/قائمة — تجاهل بصمت
                    msg_hash = self._alert_msg_map.get(replied_id)
                    low = text.lower()
                    if low in ("تواصل", "/تواصل", "contact", "/contact", "قائمة", "/قائمة", "رسائل"):
                        await _show_picker(target_id, msg_hash, reply_to=event.message.id)
                    elif text.isdigit():
                        index = int(text)
                        if 1 <= index <= len(templates):
                            ok, msg = await self._send_contact_template(msg_hash, index)
                            await event.reply(f"{'✅' if ok else '⚠️'} {msg}")
                except Exception as e:
                    logger.error(f"target reply handler error: {e}")
            self._target_reply_handler_func = _target_reply_handler

        logger.info(
            f"Contact-button handlers registered "
            f"({len(templates)} ready-made templates, reply-fallback "
            f"{'on' if target_id else 'off'})"
        )

    # ─── Health HTTP Server (fallback when Dashboard is disabled) ─────────────
    async def _health_server(self):
        if not AIOHTTP_AVAILABLE:
            logger.warning("Aiohttp not installed. Health server disabled.")
            return

        app = web.Application()

        async def health_handler(request):
            health = await self.health.check()
            return web.json_response({
                "status": "ok" if health.is_healthy else "degraded",
                "checks": health.checks,
                "uptime": int(time.monotonic() - self._start_time),
                "queue_size": await self.db.queue_size(),
                "db_healthy": self.db.db_healthy,
                "monitors": sum(1 for m in self.monitors if m.is_connected),
                "fast_capture": get_capture_snapshot(),
                "sender_intel": get_sender_intel_snapshot(),
                "dedup": get_dedup_snapshot(),
                "antispam": get_antispam_snapshot(),
            })

        app.router.add_get('/health', health_handler)

        # Render Web Service: MUST bind to $PORT (default 10000)
        port = int(os.getenv("PORT", 10000))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)

        try:
            await site.start()
            logger.info(f"Health server started on port {port}")
        except OSError as e:
            logger.error(f"Could not bind health server to port {port}: {e}")
            # v13.2 fix (audit M-8): on a bind failure the task used to hang
            # forever on a never-set Event. Exit cleanly so the failure is
            # visible and Render's supervisor can restart the process.
            await runner.cleanup()
            return

        await asyncio.Event().wait()

    # ─── Dashboard Server (main web face on Render: binds $PORT) ──────────────
    async def _start_dashboard(self):
        if not DASHBOARD_AVAILABLE:
            return
        set_bot_reference(self)
        port = int(os.getenv("PORT", CFG.DASHBOARD_PORT))
        logger.info(f"🚀 Starting Dashboard + Health endpoint on port {port}")
        try:
            await run_dashboard(host="0.0.0.0", port=port)
        except Exception as e:
            logger.error(f"Failed to start Dashboard on port {port}: {e}")

    # ─── Keep-Alive Self-Ping (M-07: no localhost fallback) ────────────────────
    async def _keep_alive_loop(self) -> None:
        """
        Pings /health from an EXTERNAL address every KEEP_ALIVE_INTERVAL_SECONDS
        (default 600 = 10 min) to prevent Render free-tier services from
        sleeping after ~15 minutes idle.

        M-07 fix: previously fell back to http://127.0.0.1:<PORT>/health
        when RENDER_EXTERNAL_URL was unset. A request to localhost never
        leaves the instance, so it can never count as external traffic to
        Render and gave a false sense of the service being protected from
        sleep. Now: no external URL → keep-alive is explicitly disabled
        with a clear one-time warning, instead of silently doing nothing
        useful.

        v9.12 (audit L-06): the interval is now configurable via
        KEEP_ALIVE_INTERVAL_SECONDS (default 600). Render's free tier
        sleeps after ~15 min of no inbound traffic; the default 10-min
        cadence keeps the service awake. For PAID Render tiers (which
        don't sleep) the keep-alive is harmless but unnecessary — set
        KEEP_ALIVE_INTERVAL_SECONDS=0 to disable it explicitly. We also
        skip the loop entirely when the service is on a paid plan
        (RENDER_PLAN env var contains "starter"/"standard"/"pro"/"paid")
        to avoid wasting outbound requests.
        """
        if not AIOHTTP_AVAILABLE:
            return
        # v9.12 (audit L-06): paid Render tiers don't sleep — skip keep-alive.
        plan = (os.getenv("RENDER_PLAN") or "").lower()
        paid_plans = ("starter", "standard", "pro", "paid", "plus")
        if any(p in plan for p in paid_plans) and os.getenv("KEEP_ALIVE_FORCE", "").lower() not in ("1", "true", "yes"):
            logger.info(
                f"Keep-alive DISABLED — Render plan '{plan or 'unknown'}' does not sleep. "
                f"Set KEEP_ALIVE_FORCE=true to enable anyway."
            )
            return
        try:
            interval = max(60, int(os.getenv("KEEP_ALIVE_INTERVAL_SECONDS", "600")))
        except Exception:
            interval = 600
        if interval <= 0:
            logger.info("Keep-alive disabled via KEEP_ALIVE_INTERVAL_SECONDS=0")
            return
        external_url = (os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
        if not external_url:
            logger.warning(
                "RENDER_EXTERNAL_URL is not set — external keep-alive is "
                "DISABLED. Pinging 127.0.0.1 would not count as external "
                "traffic and would only create a false sense of protection "
                "against Render free-tier sleep, so no local fallback is "
                "used. Set RENDER_EXTERNAL_URL (Render provides this "
                "automatically for most Web Services) to enable real "
                "keep-alive."
            )
            return
        url = f"{external_url}/health"
        await asyncio.sleep(60)  # let the web server come up first
        logger.info(f"Keep-alive self-ping enabled -> {url} (every {interval}s)")
        while self.is_running:
            try:
                timeout = ClientTimeout(total=30)
                async with ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        logger.debug(f"Keep-alive ping {url} -> HTTP {resp.status}")
            except Exception as e:
                logger.warning(f"Keep-alive ping failed: {e}")
            # Render free tier sleeps after ~15 min of no inbound traffic
            await asyncio.sleep(interval)

    # ─── Background Tasks ─────────────────────────────────────────────────────
    async def _stats_reporter(self) -> None:
        while self.is_running:
            try:
                await asyncio.sleep(CFG.STATS_INTERVAL)
                if not await self._client_ok(self.main_client):
                    continue

                stats = await self.db.get_stats()
                filter_tele = await self.filter.get_telemetry()
                uptime = time.monotonic() - self._start_time
                h, rem = divmod(int(uptime), 3600)
                m_min = rem // 60
                mem = self.memory_monitor.check()
                qsize = await self.db.queue_size()
                connected = sum(1 for m in self.monitors if m.is_connected)

                hit_rate = (filter_tele.get("valid", 0) / total * 100) if (total := filter_tele.get("processed", 0)) else 0.0

                text = (
                    f"<b>📊 إحصائيات البوت v13.1 (Render)</b>\n\n"
                    f"⏱ وقت التشغيل: {h}س {m_min}د\n"
                    f"📨 الرسائل: {stats.get('total_messages', 0):,}\n"
                    f"🚨 التنبيهات: {stats.get('alerts_sent', 0):,}\n"
                    f"👥 الحسابات: {connected}/{len(self.monitors)}\n"
                    f"🎯 نسبة الاصطياد: {hit_rate:.1f}%\n"
                    f"🗂 الطابور: {qsize} رسالة"
                )
                # M-04/M-09: surface the DB's own queue-capacity signals
                # instead of pretending the queue has no observable limit.
                evictions = stats.get("queue_evictions", 0)
                if evictions:
                    text += f" (تم استبعاد {evictions} عنصر قديم عند الامتلاء)"
                text += f"\n🧠 الذاكرة: {mem.get('current_mb', 0)}MB"
                if not self.db.db_healthy:
                    text += "\n🔴 <b>قاعدة البيانات في حالة غير مستقرة</b>"
                # M-03/M-14: only ever label this "leak_suspected" (sustained
                # trend), never a bare RSS-elevated reading, and phrase it
                # as worth reviewing rather than a confirmed leak.
                if mem.get("leak_suspected"):
                    text += "\n⚠️ <b>نمو ذاكرة مستمر عبر عدة قياسات متتالية — يستحق المراجعة</b>"

                await self.main_client.send_message(CFG.ADMIN_CHAT_ID, text, parse_mode="html")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Stats reporter error: {e}")

    async def _cleanup_loop(self) -> None:
        """
        Single, authoritative owner of database maintenance (M-06/M-12).
        database.py v9.0 no longer runs its own _cleanup_loop — this is
        now the only place cleanup_old_data()/cleanup_dead_letters() are
        called from, eliminating the double-cleanup that previously
        existed between main.py and database.py.

        v9.12 (audit H-08): the alert_dedup cleanup now runs on its own
        short cadence (DEDUP_CLEANUP_INTERVAL_SECONDS, default 3600s = 1h)
        via a separate _dedup_cleanup_loop task instead of being lumped
        into the daily CLEANUP_INTERVAL pass. The dedup window is 24h,
        so a daily cleanup allowed the table to grow to 2-3x the live
        window under load; an hourly cleanup keeps the table size close
        to the live window's steady state.
        """
        while self.is_running:
            try:
                await asyncio.sleep(CFG.CLEANUP_INTERVAL)
                deleted = await self.db.cleanup_old_data(days=7)
                if deleted:
                    logger.info(f"Cleanup: {deleted} old rows removed")
                dl_cleaned = await self.db.cleanup_dead_letters(days=CFG.DEAD_LETTER_CLEANUP_DAYS)
                if dl_cleaned:
                    logger.info(f"Dead letter cleanup: {dl_cleaned} records removed")
                gc.collect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

    async def _dedup_cleanup_loop(self) -> None:
        """v9.12 (audit H-08): dedicated short-cadence loop for the
        alert_dedup table. Runs every DEDUP_CLEANUP_INTERVAL_SECONDS
        (default 3600s = 1h) so the table stays close to the live 24h
        window's steady state instead of growing 2-3x between daily
        cleanups.
        """
        try:
            interval = max(300, int(os.getenv("DEDUP_CLEANUP_INTERVAL_SECONDS", str(3600))))
        except Exception:
            interval = 3600
        while self.is_running:
            try:
                await asyncio.sleep(interval)
                from dedup import get_deduplicator
                expired = await get_deduplicator().cleanup_expired()
                if expired:
                    logger.info(f"Dedup cleanup: {expired} expired fingerprints removed")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Dedup cleanup skipped: {e}")
                # Avoid a tight error loop on persistent failure.
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    break

    async def _health_check_loop(self) -> None:
        while self.is_running:
            try:
                await asyncio.sleep(CFG.HEALTH_CHECK_INTERVAL)
                health = await self.health.check()
                if not health.is_healthy:
                    logger.warning(f"Health check failed: {health.checks}")
                    if health.checks.get("main_client") == "down":
                        await self._failover_main_client()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")

    async def _main_client_watchdog_loop(self) -> None:
        """
        M-10/M-11: a dedicated, lightweight liveness check for main_client
        ONLY (no DB ping, no filter telemetry, no per-monitor stats
        gathering — that's what the heavier _health_check_loop already
        does every CFG.HEALTH_CHECK_INTERVAL). Running this on a shorter,
        independent interval means a dead main_client is detected and
        failed over well before a full health cycle would notice it.
        """
        interval = max(5, min(15, CFG.HEALTH_CHECK_INTERVAL))
        while self.is_running:
            try:
                await asyncio.sleep(interval)
                if not await self._client_ok(self.main_client):
                    await self._failover_main_client()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Main client watchdog error: {e}")

    async def _failover_main_client(self) -> None:
        """
        Single shared failover routine (called from both the watchdog and
        the full health-check loop, so there is exactly one code path that
        performs promotion — no duplicated/diverging failover logic).

        Real gap fixed here: the original code swapped `self.main_client`
        and every monitor's `mon.main_client` reference, but NEVER
        re-registered the admin-command / copy-button handlers on the
        newly-promoted client. Those handlers were only ever attached once
        (in initialize()) to the original main_client, so after a
        failover the bot would keep running but admin commands (/stats,
        /block, ...) would go silently unresponsive. Both handlers are now
        re-registered as part of promotion.
        """
        async with self._main_client_lock:
            if await self._client_ok(self.main_client):
                return  # already recovered via another path
            for m in self.monitors:
                if m.is_connected and m.client and await self._client_ok(m.client):
                    previous = self.main_client
                    self.main_client = m.client
                    for mon in self.monitors:
                        mon.main_client = self.main_client
                    logger.warning(
                        f"Main client failover: promoting {m.account['name']} "
                        f"(previous main client was {'set' if previous else 'unset'} and unresponsive)"
                    )
                    await self._register_admin_commands()
                    await self._register_copy_handler()
                    # v9.11: أعد ربط معالجات زر التواصل أيضاً على العميل الجديد
                    await self._register_contact_handlers()
                    return
            logger.error("Main client failover: no connected/alive monitor client available")

    async def _memory_monitor_loop(self) -> None:
        while self.is_running:
            try:
                await asyncio.sleep(CFG.GC_INTERVAL)
                mem = self.memory_monitor.check()
                if mem.get("leak_suspected"):
                    logger.warning(
                        f"Sustained memory growth detected: current={mem['current_mb']}MB "
                        f"baseline={mem['baseline_mb']}MB peak={mem['peak_mb']}MB "
                        f"(elevated across {self.memory_monitor._sustained_samples} consecutive checks)"
                    )
                    for line in (mem.get("tracemalloc_top") or []):
                        logger.warning(f"  ↳ {line}")
                    self.memory_monitor.force_gc()
                # M-15: disk/queue pressure is tracked independently of RAM.
                # database.py's own _resource_pressure_check() already acts
                # on this internally (emergency cleanup); here we just log
                # for visibility so it's never confused with a RAM leak.
                if not self.db.db_healthy:
                    logger.warning(
                        "Database reporting unhealthy (write failures) — "
                        "see database.py logs for detail; this is a DB/disk "
                        "condition, not a memory leak."
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Memory monitor error: {e}")

    @staticmethod
    async def _client_ok(client: Any) -> bool:
        if client is None:
            return False
        try:
            attr = getattr(client, "is_connected", None)
            return bool(attr() if callable(attr) else attr)
        except Exception:
            return False

    # ─── Admin Commands ────────────────────────────────────────────────────────
    async def _register_admin_commands(self) -> None:
        if not self.main_client:
            return
        if self._admin_handler_func is not None:
            try:
                self.main_client.remove_event_handler(self._admin_handler_func)
            except Exception as e:
                logger.debug(f"remove admin handler failed: {e}")

        @self.main_client.on(tl_events.NewMessage(
            chats=CFG.ADMIN_CHAT_ID, incoming=True,
            pattern=r"^/(stats|status|help|block|unblock|purge|accounts|health|filter_stats|dashboard|spam|unspam|contact)(.*)$",
        ))
        async def _admin_handler(event: Any) -> None:
            try:
                await self._handle_admin_command(event)
            except Exception as e:
                logger.error(f"Admin command error: {e}")

        self._admin_handler_func = _admin_handler
        logger.info("Admin command listener registered")

    async def _handle_admin_command(self, event: Any) -> None:
        text: str = (event.message.text or "").strip()
        cmd, _, args = text.partition(" ")
        cmd = cmd.lstrip("/").lower()

        if cmd == "help":
            await event.reply("<b>أوامر البوت:</b>\n/stats – إحصائيات\n/status – حالة الحسابات\n/accounts – التفاصيل\n/health – الصحة\n/dashboard – لوحة التحكم\n/block &lt;id&gt; – حظر\n/unblock &lt;id&gt; – رفع حظر\n/purge – تفريغ الطابور\n/spam – قوائم مكافحة السبام\n/unspam &lt;id&gt; – إزالة من تجاهل السبام\n/contact – الرسائل الجاهزة", parse_mode="html")
        elif cmd == "stats":
            db_stats = await self.db.get_stats()
            rl = self.rate_limiter.status()
            uptime = time.monotonic() - self._start_time
            h, rem = divmod(int(uptime), 3600)
            m_min = rem // 60
            evictions = db_stats.get("queue_evictions", 0)
            db_health_line = "✅ سليمة" if self.db.db_healthy else "⚠️ غير مستقرة"
            await event.reply(
                f"<b>📊 إحصائيات فورية</b>\n"
                f"⏱ {h}س {m_min}د\n"
                f"📨 رسائل: {db_stats.get('total_messages', 0):,}\n"
                f"🚨 تنبيهات: {db_stats.get('alerts_sent', 0):,}\n"
                f"🗂 الطابور: {await self.db.queue_size()} (استبعاد: {evictions})\n"
                f"🗄 قاعدة البيانات: {db_health_line}\n"
                f"📉 {rl['per_min']}/{rl['limit_min']} في الدقيقة",
                parse_mode="html",
            )
        elif cmd == "status":
            connected = sum(1 for m in self.monitors if m.is_connected)
            lines = [f"<b>📱 حالة الحسابات ({connected}/{len(self.monitors)})</b>\n"]
            for m in self.monitors:
                icon = "✅" if m.is_connected else "❌"
                s = await m.get_stats()
                # M-08/M-17 fix: monitors.py's get_stats() returns
                # "alerts_sent", never "alerts" — the old code read a key
                # that never existed and always showed 0 here regardless
                # of how many alerts had actually been sent.
                lines.append(
                    f"{icon} <b>{m.account['name']}</b> | "
                    f"تنبيهات: {s.get('alerts_sent', 0)} | "
                    f"رسائل: {s.get('messages_processed', 0)} | "
                    f"أخطاء: {s.get('errors', 0)}"
                )
            await event.reply("\n".join(lines), parse_mode="html")
        elif cmd == "accounts":
            lines = ["<b>🔑 تفاصيل الحسابات</b>\n"]
            for m in self.monitors:
                s = await m.get_stats()
                icon = "🟢" if m.is_connected else "🔴"
                dlq = s.get("dlq_stats", {}) or {}
                lines.append(
                    f"{icon} {m.account['name']}\n"
                    f"   📞 {m.account['phone']}\n"
                    f"   🚨 تنبيهات: {s.get('alerts_sent', 0)} | "
                    f"مكرر: {s.get('duplicates', 0)} | "
                    f"محظور مؤقتًا: {s.get('rate_limited', 0)}\n"
                    f"   🪦 dead-letter: {dlq.get('dead_lettered', 0)}\n"
                    f"   ⚡ آخر خطأ: {(s.get('last_error') or 'لا شيء')[:60]}"
                )
            await event.reply("\n".join(lines), parse_mode="html")
        elif cmd == "health":
            health = await self.health.check()
            status = "✅ صحي" if health.is_healthy else "⚠️ غير صحي"
            lines = [f"<b>🏥 فحص الصحة: {status}</b>\n"]
            for check, value in health.checks.items():
                lines.append(f"• {check}: {value}")
            lines.append(f"• db_healthy: {self.db.db_healthy}")
            await event.reply("\n".join(lines), parse_mode="html")
        elif cmd == "filter_stats":
            tele = await self.filter.get_telemetry()
            lines = ["<b>🔍 إحصائيات الفلتر</b>\n"]
            for k, v in tele.items():
                lines.append(f"• {k}: {v}")
            await event.reply("\n".join(lines), parse_mode="html")
        elif cmd == "dashboard":
            if CFG.DASHBOARD_ENABLED and DASHBOARD_AVAILABLE:
                url = os.getenv("RENDER_EXTERNAL_URL") or f"http://localhost:{CFG.DASHBOARD_PORT}"
                # v9.12 (audit H-09): the auth token is no longer echoed
                # into the Telegram chat. The chat history, synced devices
                # and chat backups would otherwise retain the token forever
                # — anyone with read access to the admin's Telegram account
                # would gain full dashboard control. The admin should
                # retrieve the token from Render's environment variables
                # panel (or wherever they stored it) instead.
                await event.reply(
                    f"<b>🌐 رابط لوحة التحكم</b>\n"
                    f"الرابط: <code>{url}</code>\n"
                    f"🔑 استخدم التوكن الموجود في إعدادات البيئة على Render "
                    f"(DASHBOARD_AUTH_TOKEN) — لا يُرسل هنا لحماية الأمان.",
                    parse_mode="html",
                )
            else:
                await event.reply("❌ لوحة التحكم غير مفعلة")
        elif cmd == "block":
            uid = args.strip()
            if uid.isdigit():
                await self.db.block_sender(int(uid), reason="admin block", by="admin")
                await event.reply(f"✅ تم حظر المستخدم {uid}")
            else:
                await event.reply("❌ استخدام: /block <user_id>")
        elif cmd == "unblock":
            uid = args.strip()
            if uid.isdigit():
                await self.db.unblock_sender(int(uid))
                await event.reply(f"✅ تم رفع حظر المستخدم {uid}")
            else:
                await event.reply("❌ استخدام: /unblock <user_id>")
        elif cmd == "purge":
            count = await self.db.purge_queue()
            await event.reply(f"🗑 تم مسح {count} رسالة من الطابور")
        elif cmd == "spam":
            # v9.11: تقرير مكافحة السبام — لقطة + قائمة المراقبة + التجاهل الدائم
            snap = get_antispam().snapshot()
            watched = await self.db.get_recent_watchlist(limit=8, active_only=True)
            ignored = await self.db.get_spam_ignored(limit=10)
            lines = [
                "<b>🛡 مكافحة السبام</b>",
                f"الحالة: {'✅ مفعّل' if snap.get('enabled') else '⛔ معطّل'}",
                f"مراقَبون الآن: {snap.get('active_watch', 0)} | مصنّفون كمزعجين: {snap.get('permanently_ignored', 0)}",
                f"إجمالي المراقبة: {snap.get('watch_added', 0)} | تأكيدات سبام: {snap.get('spam_confirmed', 0)} | تصنيف مباشر: {snap.get('direct_spam', 0)}",
            ]
            if watched:
                lines.append("\n<b>👀 تحت المراقبة حالياً:</b>")
                for w in watched:
                    until = w.get("watch_until") or 0
                    left = max(0, int(until - time.time()))
                    lines.append(f"• <code>{w.get('sender_id')}</code> — باقي {left // 60}د — {(w.get('reason') or '')[:80]}")
            if ignored:
                lines.append("\n<b>🚫 تجاهل دائم:</b>")
                for u in ignored:
                    lines.append(f"• <code>{u.get('sender_id')}</code> — {(u.get('reason') or '')[:80]}")
            await event.reply("\n".join(lines), parse_mode="html")
        elif cmd == "unspam":
            uid = args.strip()
            if uid.lstrip("-").isdigit():
                ok = await get_antispam().unignore(int(uid))
                if ok:
                    await event.reply(f"✅ تمت إزالة {uid} من قائمة تجاهل السبام — ستُعالج رسائله وستعود التنبيهات")
                else:
                    await event.reply(f"⚠️ تعذرت الإزالة من قاعدة البيانات — راجع السجلات")
            else:
                await event.reply("❌ استخدام: /unspam <user_id>")
        elif cmd == "contact":
            # v9.11: عرض الرسائل الجاهزة الحالية (لزر «تواصل مع المرسل»)
            templates = get_contact_templates()
            lines = ["<b>📨 الرسائل الجاهزة الحالية:</b>", ""]
            for i, t in enumerate(templates, 1):
                lines.append(f"{i}️⃣ {InputSanitizer.escape_html(t)}")
            lines.append("\nℹ️ تُعدّل عبر متغير البيئة CONTACT_TEMPLATES_JSON ثم إعادة التشغيل.")
            await event.reply("\n".join(lines), parse_mode="html")

    # ─── Initialization ────────────────────────────────────────────────────────
    async def initialize(self) -> bool:
        logger.info("=" * 60)
        logger.info("Enhanced Telegram Bot v13.1 (RENDER EDITION, HARDENED) – Initializing...")
        logger.info("=" * 60)

        if not await self.db.connect():
            logger.error("Database connection failed - aborting")
            return False

        # v9.10: تهيئة حاجز منع تكرار التنبيهات (بعد اتصال DB مباشرة —
        # singleton مشترك بين كل المراقبين، وجدول alert_dedup يُنشأ ضمن
        # _create_tables في connect()).
        init_deduplicator(self.db)

        # v9.11: تهيئة محرك مكافحة السبام وتحميل قائمة التجاهل الدائم من DB
        # (قبل تشغيل أي مراقب — حتى تُتجاهل رسائل المصنفين كمزعجين منذ اللحظة الأولى).
        await setup_antispam(self.db)

        # Start the web layer FIRST so Render's health check passes immediately
        # and the Dashboard stays reachable even before/without any account
        # connection (sessions can then be added via the /login page).
        if CFG.DASHBOARD_ENABLED and DASHBOARD_AVAILABLE:
            self._dashboard_task = self._track_task(self._start_dashboard(), "dashboard")
            logger.info("Dashboard task started (serves /health on $PORT)")
        else:
            self._health_server_task = self._track_task(self._health_server(), "health_server")
            logger.info("Dashboard disabled - standalone health server on $PORT")

        self._keep_alive_task = self._track_task(self._keep_alive_loop(), "keep_alive")

        accounts = sorted(ACCOUNTS, key=lambda a: a.get("priority", 0), reverse=True)
        if not accounts:
            logger.warning(
                "No accounts configured - running in dashboard-only mode. "
                "Set MAIN_API_ID / MAIN_API_HASH / MAIN_PHONE / MAIN_SESSION_STRING "
                "env vars (or use the /login page), then restart the service."
            )

        logger.info(f"Connecting {len(accounts)} account(s) sequentially...")
        connected = 0

        for idx, acc in enumerate(accounts, 1):
            logger.info(f"\n[{idx}/{len(accounts)}] Connecting: {acc['name']} | {acc['phone']}")
            mon = EnhancedAccountMonitor(acc, self.db, self.filter, self.main_client)
            mon.set_bot(self)
            self.monitors.append(mon)

            try:
                ok = await asyncio.wait_for(mon.connect(), timeout=120)
                if ok:
                    connected += 1
                    logger.info(f"✅ {acc['name']} connected")
                    if acc.get("is_main") and self.main_client is None:
                        self.main_client = mon.client
                        logger.info(f"👑 Main client: {acc['name']}")
                else:
                    logger.error(f"❌ {acc['name']} failed to connect")
            except asyncio.TimeoutError:
                logger.error(f"⏰ {acc['name']} timed out")
            except Exception as e:
                logger.error(f"💥 {acc['name']}: {e}")

            if idx < len(accounts):
                await asyncio.sleep(3)

        if self.main_client is None:
            for m in self.monitors:
                if m.is_connected:
                    self.main_client = m.client
                    for mon in self.monitors:
                        mon.main_client = self.main_client
                    logger.warning(f"⚠️ Fallback main client: {m.account['name']}")
                    break

        logger.info(f"\n📊 Connected: {connected}/{len(accounts)}")

        if connected == 0:
            logger.warning(
                "No accounts connected - running in dashboard-only mode. "
                "Fix the *_SESSION_STRING env vars (or use the /login page), then restart."
            )

        self.memory_monitor.start()

        await self._register_admin_commands()
        await self._register_copy_handler()
        # v9.11: معالجات زر «تواصل مع المرسل» + مسار الرد البديل
        await self._register_contact_handlers()

        # Background Tasks (M-05/M-16: all tracked uniformly)
        self._consumer_task = self._track_task(self._consumer_loop(), "consumer")
        self._stats_task = self._track_task(self._stats_reporter(), "stats")
        self._cleanup_task = self._track_task(self._cleanup_loop(), "cleanup")
        # v9.12 (audit H-08): dedicated short-cadence dedup cleanup task.
        self._dedup_cleanup_task = self._track_task(self._dedup_cleanup_loop(), "dedup_cleanup")
        self._health_task = self._track_task(self._health_check_loop(), "health")
        self._memory_task = self._track_task(self._memory_monitor_loop(), "memory")
        self._main_client_watchdog_task = self._track_task(
            self._main_client_watchdog_loop(), "main_client_watchdog"
        )

        logger.info("✅ Initialization complete (Render Edition, hardened)")
        return True

    # ─── Run & Shutdown ────────────────────────────────────────────────────────
    async def run(self) -> None:
        self.is_running = True
        loop = asyncio.get_running_loop()

        # M-05/M-16 fix: the shutdown task is now tracked (not pure
        # fire-and-forget), and the main loop waits on an explicit event
        # instead of polling `is_running` in the dark — the signal handler
        # sets that event immediately, so the loop exits promptly and the
        # tracked shutdown task is guaranteed to be awaited before this
        # coroutine returns.
        def _handle_signal(sig: signal.Signals) -> None:
            logger.warning(f"Received signal {getattr(sig, 'name', sig)}, shutting down...")
            self._shutdown_event.set()
            self._track_task(self.stop(signal=sig), "signal_shutdown")

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: _handle_signal(s))
            except (NotImplementedError, ValueError, RuntimeError):
                # في Pydroid 3، قد لا تكون هذه الإشارات مدعومة بالكامل
                pass

        try:
            await self._send_startup_message()
            logger.info("=" * 60)
            logger.info("🤖 Bot v13.1 running on Render Cloud - 24/7 mode (hardened)")
            logger.info("=" * 60)
            while self.is_running:
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=1.0)
                    break
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.critical(f"Fatal error in run loop: {e}")
        finally:
            await self.stop()
            # Ensure every remaining tracked background task (including a
            # signal-triggered shutdown task, if any) is actually awaited
            # before this coroutine returns, so nothing is left dangling.
            remaining = [t for t in list(self._background_tasks) if not t.done()]
            if remaining:
                await asyncio.gather(*remaining, return_exceptions=True)

    async def stop(self, signal: Any = None) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self._shutdown_event.set()

        logger.info("Shutting down gracefully...")

        # M-05/M-16: cancel every tracked background task uniformly,
        # rather than an explicit hand-maintained list that could miss a
        # task (e.g. the previous fire-and-forget signal-handler task).
        tasks = [t for t in list(self._background_tasks) if t is not asyncio.current_task()]
        for task in tasks:
            if not task.done():
                task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            if self.main_client and await self._client_ok(self.main_client):
                uptime = time.monotonic() - self._start_time
                h, rem = divmod(int(uptime), 3600)
                m_min = rem // 60
                db_stats = await self.db.get_stats()
                await self.main_client.send_message(
                    CFG.ADMIN_CHAT_ID,
                    f"<b>🔴 البوت توقف</b>\n⏱ وقت التشغيل: {h}س {m_min}د\n📨 رسائل: {db_stats.get('total_messages', 0):,}\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    parse_mode="html",
                )
        except Exception as e:
            logger.debug(f"Shutdown notification failed: {e}")

        await asyncio.gather(*(m.disconnect() for m in self.monitors), return_exceptions=True)
        await self.db.close()
        logger.info("✅ Shutdown complete")

    async def _send_startup_message(self) -> None:
        if not self.main_client:
            return
        try:
            qsize = await self.db.queue_size()
            connected_count = sum(1 for m in self.monitors if m.is_connected)
            acct_lines = "\n".join(
                f"  {'✅' if m.is_connected else '❌'} {m.account['name']} ({m.account['phone']})"
                for m in self.monitors
            )
            kw_count = sum(len(v) for v in KEYWORDS.values())
            dashboard_status = "🟢 مفعل" if CFG.DASHBOARD_ENABLED else "🔴 غير مفعل"
            db_health_status = "✅ سليمة" if self.db.db_healthy else "⚠️ غير مستقرة"

            await self.main_client.send_message(
                CFG.ADMIN_CHAT_ID,
                f"<b>🚀 البوت يعمل الآن على سحابة Render</b>\n\n"
                f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
                f"👥 الحسابات: {connected_count}/{len(self.monitors)}\n"
                f"🗂 الطابور: {qsize} رسالة معلقة\n"
                f"🗄 قاعدة البيانات: {db_health_status}\n"
                f"🔑 الكلمات المفتاحية: {kw_count:,}\n"
                f"🌐 Dashboard: {dashboard_status}\n\n"
                f"<b>الحسابات:</b>\n{acct_lines}\n\n"
                f"💡 اكتب /help للأوامر المتاحة",
                parse_mode="html",
            )
        except Exception as e:
            logger.error(f"Startup message failed: {e}")


# =============================================================================
# Entry Point
# =============================================================================
async def main() -> None:
    # ضبط الترميز ليتوافق مع شاشة Pydroid 3
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    else:
        if UVLOOP_AVAILABLE:
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
            logger.info("Using uvloop for optimal performance")
        else:
            logger.info("uvloop not found. Using default asyncio loop (Compatible with Pydroid 3)")

    bot: Optional[EnhancedTelegramBot] = None
    try:
        bot = EnhancedTelegramBot()
        if await bot.initialize():
            await bot.run()
        else:
            logger.critical("Initialization failed")
            sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.critical(f"Fatal: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if bot and bot.is_running:
            await bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")