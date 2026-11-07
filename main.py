#!/usr/bin/env python3
"""
main.py – Enhanced Telegram Bot v14.5.1 (RENDER CLOUD EDITION, HARDENED)
متوافق مع استضافة Render المجانية (Web Service) – تشغيل 24/7
متوافق مع: monitors.py v14.5.1, config.py v14.5.1, filter_engine.py v14.5.1, database.py v14.5.1

v16.1.2 — Memory Hardening & Stateless Cleanup:
  * Disabled tracemalloc by default (saves ~40MB). Enable only if ENABLE_TRACEMALLOC=1.
  * Added _memory_cleanup_loop: runs every 5 minutes to clear caches, force GC, and log RSS.
  * Added strict stateless memory policy.
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
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Set

# محاولة استيراد uvloop بشكل آمن
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
    BOT_VERSION,
    BOT_CODENAME,
)
from database import EnhancedDatabase
from dedup import get_dedup_snapshot, init_deduplicator
from unified_alerts import run_startup_maintenance, unified_snapshot
from antispam import get_antispam, setup_antispam, get_antispam_snapshot
from filter_engine import EnhancedFilter
from monitors import (
    EnhancedAccountMonitor, HealthMonitor, get_capture_snapshot, alert_latency,
    register_self_sender,
)
from alert_bot import AlertBot
from alert_ui import AlertBotClient
from sender_resolver import get_sender_intel_snapshot, get_mention_intel_snapshot
from contact_target import get_contact_metrics_snapshot

# psutil optional
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    logger.warning("psutil not installed. Memory monitoring will be limited.")


def _pick_alertui_account(accounts: Any, monitors: Any) -> Optional[dict]:
    acc = next((a for a in (accounts or []) if (a or {}).get("is_main")), None)
    if acc is not None:
        return acc
    for m in (monitors or []):
        a = getattr(m, "account", None) or {}
        if getattr(m, "is_connected", False) and a.get("api_id") and a.get("api_hash"):
            return a
    return None


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


class MemoryMonitor:
    def __init__(
        self,
        threshold_mb: int = 512,
        history_size: int = 10,
        sustained_samples: int = 8,
        growth_ratio: float = 1.8,
    ) -> None:
        self._threshold = threshold_mb * 1024 * 1024
        self._baseline = 0
        self._peak = 0
        self._history: Deque[int] = deque(maxlen=history_size)
        self._sustained_samples = sustained_samples
        self._growth_ratio = growth_ratio
        self._tracemalloc_enabled = False
        self._last_snapshot = None
        self._last_diagnose_ts: float = 0.0

    def start(self) -> None:
        # v16.1.2: tracemalloc اختياري — على Render 512MB يضيف 15-40%.
        self._tracemalloc_enabled = False
        if os.getenv("ENABLE_TRACEMALLOC", "0") == "1":
            try:
                tracemalloc.start()
                self._tracemalloc_enabled = True
                self._last_snapshot = tracemalloc.take_snapshot()
                logger.warning("⚠️ tracemalloc ENABLED — adds ~20% RAM overhead")
            except Exception as e:
                logger.warning(f"tracemalloc unavailable: {e}")
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
            "leak_detected": leak_suspected,
        }

        if leak_suspected and self._tracemalloc_enabled:
            result["tracemalloc_top"] = self._diagnose()

        return result

    def _diagnose(self) -> Optional[List[str]]:
        if not self._tracemalloc_enabled:
            return None
        now = time.time()
        if now - self._last_diagnose_ts < 3600:
            return None
        self._last_diagnose_ts = now
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
        self.alert_sender_client: Optional[TelegramClient] = None
        self.alert_bot = AlertBot(
            token=(getattr(CFG, "ALERT_BOT_TOKEN", None) or os.getenv("BOT_TOKEN")),
            chat_id=getattr(CFG, "TARGET_GROUP_ID", 0),
            timeout=getattr(CFG, "ALERT_BOT_TIMEOUT", 12.0),
            max_retries=getattr(CFG, "ALERT_BOT_MAX_RETRIES", 3),
        )
        self.alert_ui_client: Optional[AlertBotClient] = None
        self.monitors: List[EnhancedAccountMonitor] = []
        self.is_running = False
        self._start_time = time.monotonic()
        self.health = HealthMonitor(self)

        self._background_tasks: Set[asyncio.Task] = set()
        self._shutdown_event = asyncio.Event()

        self._consumer_task: Optional[asyncio.Task] = None
        self._stats_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._dedup_cleanup_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._memory_task: Optional[asyncio.Task] = None
        self._health_server_task: Optional[asyncio.Task] = None
        self._keep_alive_task: Optional[asyncio.Task] = None
        self._main_client_watchdog_task: Optional[asyncio.Task] = None
        self._memory_cleanup_task: Optional[asyncio.Task] = None

        self._main_client_lock = asyncio.Lock()
        self._admin_handler_func: Any = None
        self._copy_handler_func: Any = None
        self._contact_handler_funcs: List[Any] = []
        self._target_reply_handler_func: Any = None
        self._alert_msg_map: Dict[int, str] = {}

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

    async def _consumer_loop(self) -> None:
        logger.info("Consumer loop started")
        internal_queue: asyncio.Queue = asyncio.Queue(maxsize=CFG.MESSAGE_QUEUE_SIZE)

        async def producer():
            while self.is_running:
                event_data = None
                try:
                    event_data = await self.db.pop_from_queue()
                    if event_data:
                        await internal_queue.put(event_data)
                        event_data = None
                    else:
                        if getattr(CFG, "QUEUE_EVENT_WAKE", True):
                            try:
                                await asyncio.wait_for(self.db.queue_notify.wait(), timeout=0.1)
                            except asyncio.TimeoutError:
                                pass
                            try:
                                self.db.queue_notify.clear()
                            except Exception:
                                pass
                        else:
                            await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    if event_data is not None:
                        try:
                            await self.db.add_to_queue(event_data, priority=9)
                            logger.info("Producer shutdown: re-enqueued 1 in-flight event to DB (M-07 ack-after-pop)")
                        except Exception as e:
                            logger.error(f"Producer shutdown: failed to re-enqueue in-flight event: {e}")
                    break
                except Exception as e:
                    logger.error(f"Producer error: {e}")
                    if event_data is not None:
                        try:
                            await self.db.add_to_queue(event_data, priority=9)
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
                        event_data["_attributed_to"] = account_name or "unknown"
                        for m in self.monitors:
                            if m.is_connected:
                                await m.process_event_from_queue(event_data)
                                break
                        else:
                            logger.warning(
                                f"No connected monitor to process event from {account_name}; re-enqueueing"
                            )
                            try:
                                await self.db.add_to_queue(event_data, priority=9)
                            except Exception:
                                pass
                    try:
                        _qid = event_data.get("_queue_id")
                        if _qid is not None:
                            await self.db.ack_queue_event(_qid)
                    except Exception as _ack_err:
                        logger.debug(f"ack failed (fail-safe): {type(_ack_err).__name__}")
                    internal_queue.task_done()
                    event_data = None
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    if event_data is not None:
                        try:
                            await self.db.add_to_queue(event_data, priority=9)
                            logger.info(f"Worker {worker_id} shutdown: re-enqueued 1 in-flight event to DB (M-07)")
                        except Exception as e:
                            logger.error(f"Worker {worker_id} shutdown: failed to re-enqueue in-flight event: {e}")
                    break
                except Exception as e:
                    logger.error(f"Worker {worker_id} error: {e}")
                    if event_data is not None:
                        try:
                            await self.db.add_to_queue(event_data, priority=9)
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

    def _note_alert_message(self, msg_id: Any, msg_hash: str) -> None:
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

        from telethon import Button
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

        @client.on(tl_events.CallbackQuery(pattern=r"^tpl\d+_"))
        async def _tpl_handler(event):
            try:
                raw = event.data.decode("utf-8", "ignore")
                head, _, msg_hash = raw.partition("_")
                index = int(head[3:])
                ok, msg = await self._send_contact_template(msg_hash, index)
                if ok:
                    try:
                        await event.delete()
                    except Exception:
                        pass
                await event.answer(f"✅ {msg}" if ok else f"⚠️ {msg}", alert=not ok)
            except Exception as e:
                logger.error(f"tpl handler error: {e}")
                try:
                    await event.answer("حدث خطأ أثناء الإرسال", alert=True)
                except Exception:
                    pass

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
                        return
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

    async def _health_server(self):
        if not AIOHTTP_AVAILABLE:
            logger.warning("Aiohttp not installed. Health server disabled.")
            return

        app = web.Application()

        async def health_handler(request):
            await self.health.check()
            db_down = not (self.db and getattr(self.db, "is_connected", False))
            db_healthy = bool(getattr(self.db, "db_healthy", True))
            payload = {
                "status": "ok" if (not db_down and db_healthy) else "degraded",
                "database": "down" if db_down else "ok",
                "db_healthy": db_healthy,
                "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            return web.json_response(payload, status=503 if db_down else 200)

        async def health_full_handler(request):
            token = os.getenv("HEALTH_FULL_TOKEN", "")
            if token:
                supplied = (request.headers.get("Authorization", "")
                            .replace("Bearer ", "").strip()) or request.query.get("token", "")
                if supplied != token:
                    return web.json_response({"error": "unauthorized"}, status=401)
            health = await self.health.check()
            db_down = not (self.db and getattr(self.db, "is_connected", False))
            return web.json_response({
                "status": "ok" if health.is_healthy else "degraded",
                "checks": health.checks,
                "uptime": int(time.monotonic() - self._start_time),
                "queue_size": await self.db.queue_size(),
                "db_healthy": self.db.db_healthy,
                "monitors": sum(1 for m in self.monitors if m.is_connected),
                "fast_capture": get_capture_snapshot(),
                "sender_intel": get_sender_intel_snapshot(),
                "mention_intel": get_mention_intel_snapshot(),
                "sender_contact": get_contact_metrics_snapshot(),
                "alert_latency": alert_latency.snapshot(),
                "dedup": get_dedup_snapshot(),
                "unified_gate": unified_snapshot(),
                "antispam": get_antispam_snapshot(),
            }, status=503 if db_down else 200)

        app.router.add_get('/health', health_handler)
        app.router.add_get('/health/full', health_full_handler)

        port = int(os.getenv("PORT", 10000))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)

        try:
            await site.start()
            logger.info(f"Health server started on port {port}")
        except OSError as e:
            logger.error(f"Could not bind health server to port {port}: {e}")
            await runner.cleanup()
            return

        await asyncio.Event().wait()

    async def _keep_alive_loop(self) -> None:
        if not AIOHTTP_AVAILABLE:
            return
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
        await asyncio.sleep(60)
        logger.info(f"Keep-alive self-ping enabled -> {url} (every {interval}s)")
        while self.is_running:
            try:
                timeout = ClientTimeout(total=30)
                async with ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        logger.debug(f"Keep-alive ping {url} -> HTTP {resp.status}")
            except Exception as e:
                logger.warning(f"Keep-alive ping failed: {e}")
            await asyncio.sleep(interval)

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
                    f"<b>📊 إحصائيات البوت v{BOT_VERSION} (Render)</b>\n\n"
                    f"⏱ وقت التشغيل: {h}س {m_min}د\n"
                    f"📨 الرسائل: {stats.get('total_messages', 0):,}\n"
                    f"🚨 التنبيهات: {stats.get('alerts_sent', 0):,}\n"
                    f"👥 الحسابات: {connected}/{len(self.monitors)}\n"
                    f"🎯 نسبة الاصطياد: {hit_rate:.1f}%\n"
                    f"🗂 الطابور: {qsize} رسالة"
                )
                evictions = stats.get("queue_evictions", 0)
                if evictions:
                    text += f" (تم استبعاد {evictions} عنصر قديم عند الامتلاء)"
                text += f"\n🧠 الذاكرة: {mem.get('current_mb', 0)}MB"
                if not self.db.db_healthy:
                    text += "\n🔴 <b>قاعدة البيانات في حالة غير مستقرة</b>"
                if mem.get("leak_suspected"):
                    text += "\n⚠️ <b>نمو ذاكرة مستمر عبر عدة قياسات متتالية — يستحق المراجعة</b>"

                await self.main_client.send_message(CFG.ADMIN_CHAT_ID, text, parse_mode="html")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Stats reporter error: {e}")

    async def _cleanup_loop(self) -> None:
        while self.is_running:
            try:
                await asyncio.sleep(CFG.CLEANUP_INTERVAL)
                deleted = await self.db.cleanup_old_data(
                    days=CFG.RETENTION_MESSAGES_DAYS,
                    audit_days=CFG.RETENTION_AUDIT_DAYS,
                    notifications_days=CFG.RETENTION_NOTIFICATIONS_DAYS,
                )
                if deleted:
                    logger.info(f"Cleanup: {deleted} old rows removed")
                dl_cleaned = await self.db.cleanup_dead_letters(days=CFG.DEAD_LETTER_CLEANUP_DAYS)
                if dl_cleaned:
                    logger.info(f"Dead letter cleanup: {dl_cleaned} records removed")
                try:
                    from unified_alerts.maintenance import purge_stale_queue_rows as _purge_q
                    await _purge_q(self.db)
                except Exception as _pq_err:
                    logger.debug(f"queue purge skipped: {type(_pq_err).__name__}")
                gc.collect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

    async def _memory_cleanup_loop(self) -> None:
        """حلقة تنظيف الذاكرة الشاملة كل 5 دقائق (Stateless Cleanup)"""
        while self.is_running:
            try:
                await asyncio.sleep(300)  # 5 دقائق
                
                # 1. تنظيف كاش قاعدة البيانات
                if self.db:
                    try:
                        await self.db.prune_memory_caches()
                    except Exception:
                        pass
                    
                # 2. تنظيف كاش الحسابات (Entity Cache & Hashes)
                for m in self.monitors:
                    if hasattr(m, '_entity_negative_cache'):
                        m._entity_negative_cache.clear()
                    if hasattr(m, '_processed_hashes'):
                        m._processed_hashes.clear()
                        
                # 3. تنظيف خريطة رسائل التنبيهات القديمة
                if hasattr(self, '_alert_msg_map') and len(self._alert_msg_map) > 500:
                    self._alert_msg_map.clear()
                    
                # 4. جمع القمامة الإجباري
                gc.collect()
                
                # 5. طباعة الحالة في السجلات
                rss = 0
                try:
                    import psutil
                    rss = psutil.Process(os.getpid()).memory_info().rss // (1024 * 1024)
                except Exception:
                    pass
                logger.info(f"🧠 [MEM_CLEANUP] RSS={rss}MB | Tasks={len(self._background_tasks)} | Objects={len(gc.get_objects())}")
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Memory cleanup error: {e}")

    async def _inflight_recovery_loop(self) -> None:
        try:
            interval = max(30, int(getattr(CFG, "QUEUE_RECOVER_INTERVAL_SECONDS", 120) or 120))
        except Exception:
            interval = 120
        await asyncio.sleep(min(interval, 15))
        while self.is_running:
            try:
                from unified_alerts.maintenance import recover_inflight_events
                await recover_inflight_events(self.db)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"inflight recovery loop error (fail-safe): {type(e).__name__}: {e}")
            await asyncio.sleep(interval)

    async def _dedup_cleanup_loop(self) -> None:
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
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    break

    async def _digest_loop(self) -> None:
        try:
            if not getattr(CFG, "DIGEST_ENABLED", True):
                return
            last_sent_date: Optional[str] = None
            while self.is_running:
                try:
                    await asyncio.sleep(600)
                    now_utc = datetime.now(timezone.utc)
                    target_hour = max(0, min(23, int(getattr(CFG, "DIGEST_HOUR_UTC", 20))))
                    if now_utc.hour != target_hour:
                        continue
                    today = now_utc.strftime("%Y-%m-%d")
                    if last_sent_date == today:
                        continue
                    last_sent_date = today
                    await self._send_daily_digest()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"Digest loop skipped: {e}")
                    try:
                        await asyncio.sleep(120)
                    except asyncio.CancelledError:
                        break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Digest loop crashed: {e}")

    async def _send_daily_digest(self) -> None:
        try:
            lines: List[str] = ["📊 <b>التقرير اليومي — ملخص الأداء (24 ساعة)</b>", ""]
            try:
                stats = await self.db.get_stats()
                lines += [
                    "📨 <b>الإجماليات:</b>",
                    f"• رسائل معالَجة: {stats.get('total_messages', 0):,} | تنبيهات مرسلة: {stats.get('alerts_sent', 0):,}",
                ]
            except Exception as e:
                logger.debug(f"Digest: global stats skipped: {e}")
            try:
                from dedup import get_dedup_snapshot
                ds = get_dedup_snapshot()
                if ds.get("blocked", 0):
                    lines.append(f"• ♻️ تنبيهات مكررة منعت (dedup): {ds.get('blocked', 0):,}")
            except Exception:
                pass
            try:
                from similarity import get_similarity_snapshot
                ss = get_similarity_snapshot()
                if ss.get("blocked", 0):
                    lines.append(f"• 🔁 تنبيهات متشابهة منعت: {ss.get('blocked', 0):,}")
            except Exception:
                pass
            try:
                connected = sum(1 for m in self.monitors if getattr(m, "is_connected", False))
                lines += ["", f"👥 الحسابات المتصلة: {connected}/{len(self.monitors)}"]
            except Exception:
                pass
            lines += ["", f"🤖 BR7 v{BOT_VERSION} {BOT_CODENAME} — تقرير آلي يومي"]
            text = "\n".join(lines)
            await self.main_client.send_message(CFG.ADMIN_CHAT_ID, text, parse_mode="html")
            logger.info("Daily digest sent to admin")
        except Exception as e:
            logger.error(f"Daily digest send failed: {e}")

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
        async with self._main_client_lock:
            if await self._client_ok(self.main_client):
                return
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
            pattern=r"^/(stats|status|help|block|unblock|purge|accounts|health|filter_stats|spam|unspam|contact|dashboard)(.*)$",
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
            await event.reply("<b>أوامر البوت:</b>\n/stats – إحصائيات\n/status – حالة الحسابات\n/accounts – التفاصيل\n/health – الصحة\n/block &lt;id&gt; – حظر\n/unblock &lt;id&gt; – رفع حظر\n/purge – تفريغ الطابور\n/spam – قوائم مكافحة السبام\n/unspam &lt;id&gt; – إزالة من تجاهل السبام\n/contact – الرسائل الجاهزة", parse_mode="html")
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
            await event.reply("ℹ️ لوحة التحكم أُزيلت من هذا البوت — الإدارة تتم عبر أوامر تيليجرام فقط.")
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
            templates = get_contact_templates()
            lines = ["<b>📨 الرسائل الجاهزة الحالية:</b>", ""]
            for i, t in enumerate(templates, 1):
                lines.append(f"{i}️⃣ {InputSanitizer.escape_html(t)}")
            lines.append("\nℹ️ تُعدّل عبر متغير البيئة CONTACT_TEMPLATES_JSON ثم إعادة التشغيل.")
            await event.reply("\n".join(lines), parse_mode="html")

    async def initialize(self) -> bool:
        logger.info("=" * 60)
        logger.info(f"Enhanced Telegram Bot v{BOT_VERSION} (RENDER EDITION, HARDENED) – Initializing...")
        logger.info("=" * 60)

        if not await self.db.connect():
            logger.error("Database connection failed - aborting")
            return False

        try:
            import negative_memory as _negmem
            await _negmem.hydrate_all(self.db)
        except Exception as _nm_err:
            logger.warning(f"negative_memory hydrate skipped: {type(_nm_err).__name__}")

        init_deduplicator(self.db)

        try:
            _maint = await run_startup_maintenance(self.db)
            if _maint.get("purged_queue_rows"):
                logger.info(f"unified_alerts startup maintenance: {_maint}")
        except Exception as _maint_err:
            logger.debug(f"startup maintenance skipped: {type(_maint_err).__name__}")

        await setup_antispam(self.db)

        self._health_server_task = self._track_task(self._health_server(), "health_server")
        logger.info("Health server task started (serves /health on $PORT)")

        self._keep_alive_task = self._track_task(self._keep_alive_loop(), "keep_alive")

        accounts = sorted(ACCOUNTS, key=lambda a: a.get("priority", 0), reverse=True)
        if not accounts:
            logger.warning(
                "No accounts configured - monitoring disabled. "
                "Set MAIN_API_ID / MAIN_API_HASH / MAIN_PHONE / MAIN_SESSION_STRING "
                "env vars, then restart the service."
            )

        logger.info(f"Connecting {len(accounts)} account(s) sequentially...")
        connected = 0

        for idx, acc in enumerate(accounts, 1):
            acc_prefix = (acc.get("prefix") or "").upper()
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
                "No accounts connected - monitoring disabled. "
                "Fix the *_SESSION_STRING env vars, then restart."
            )

        self.memory_monitor.start()

        if self.alert_bot.enabled and getattr(CFG, "ALERT_BOT_ENABLED", True):
            logger.info("🤖 AlertBot مفعّل — فحص العضوية في TARGET_GROUP_ID عند الإقلاع")
            self._track_task(self._alert_bot_boot_check(), "alert_bot_boot_check")
        else:
            logger.info(
                "🤖 AlertBot غير مفعّل (لا ALERT_BOT_TOKEN أو ALERT_BOT_ENABLED=false) "
                "— التنبيهات تُرسل من حسابات المستخدمين بدون أزرار (fallback)"
            )

        if self.alert_bot.enabled and getattr(CFG, "ALERT_BOT_ENABLED", True):
            try:
                _main_acc = _pick_alertui_account(ACCOUNTS, getattr(self, "monitors", None))
                if _main_acc is not None and not (_main_acc or {}).get("is_main"):
                    logger.info(
                        f"🤖 AlertUI creds from connected account: {_main_acc.get('name')}"
                    )
                _ui = AlertBotClient(
                    api_id=(_main_acc or {}).get("api_id") or 0,
                    api_hash=(_main_acc or {}).get("api_hash") or "",
                    token=getattr(CFG, "ALERT_BOT_TOKEN", None) or os.getenv("BOT_TOKEN"),
                    db=self.db,
                    target_chat_id=getattr(CFG, "TARGET_GROUP_ID", 0),
                )
                if await _ui.start():
                    self.alert_ui_client = _ui
                    self.alert_bot.set_client(_ui)
                else:
                    self.alert_ui_client = None
            except Exception as _ui_err:
                logger.warning(
                    f"⚠️ AlertBotClient boot skipped: {type(_ui_err).__name__}: "
                    f"{str(_ui_err)[:120]} — السلّم يعمل بدونه"
                )
                self.alert_ui_client = None

        await self._register_admin_commands()
        await self._register_copy_handler()
        await self._register_contact_handlers()

        self._consumer_task = self._track_task(self._consumer_loop(), "consumer")
        self._stats_task = self._track_task(self._stats_reporter(), "stats")
        self._cleanup_task = self._track_task(self._cleanup_loop(), "cleanup")
        self._dedup_cleanup_task = self._track_task(self._dedup_cleanup_loop(), "dedup_cleanup")
        self._inflight_recovery_task = self._track_task(
            self._inflight_recovery_loop(), "inflight_recovery"
        )
        if getattr(CFG, "DIGEST_ENABLED", True):
            self._digest_task = self._track_task(self._digest_loop(), "daily_digest")
        self._health_task = self._track_task(self._health_check_loop(), "health")
        self._memory_task = self._track_task(self._memory_monitor_loop(), "memory")
        
        # تشغيل حلقة التنظيف كل 5 دقائق (Stateless Cleanup)
        self._memory_cleanup_task = self._track_task(self._memory_cleanup_loop(), "memory_cleanup")
        
        self._main_client_watchdog_task = self._track_task(
            self._main_client_watchdog_loop(), "main_client_watchdog"
        )
        self._track_task(self._warmup_chat_caches(), "chat_cache_warmup")

        logger.info("✅ Initialization complete (Render Edition, hardened)")
        return True

    async def _alert_bot_boot_check(self) -> None:
        try:
            me = await self.alert_bot.get_me()
            if me is None:
                logger.warning(
                    "⚠️ فشل التحقق من ALERT_BOT_TOKEN (getMe) — بوت التنبيهات "
                    "معطّل وستُرسل التنبيهات من حسابات المستخدمين (fallback) "
                    "بدون أزرار حتى يُصحَّح التوكن"
                )
                return
            logger.info(
                f"🤖 AlertBot ready: @{self.alert_bot.bot_username} "
                f"(id={self.alert_bot.bot_id})"
            )
            register_self_sender(self.alert_bot.bot_id)
            await self.alert_bot.check_membership()
        except Exception as e:
            logger.debug(f"alert_bot boot check skipped: {type(e).__name__}: {e}")

    async def _warmup_chat_caches(self) -> None:
        if not getattr(CFG, "WARMUP_CHAT_CACHE", True):
            return
        try:
            chat_ids = await self.db.recent_chat_ids(limit=30)
        except Exception:
            return
        if not chat_ids:
            return
        sem = asyncio.Semaphore(3)

        async def _warm(monitor: EnhancedAccountMonitor, chat_id: int) -> None:
            async with sem:
                try:
                    ent = await asyncio.wait_for(monitor.client.get_entity(int(chat_id)), timeout=10)
                    if ent is not None:
                        async with monitor._cache_lock:
                            monitor._entity_cache[int(chat_id)] = ent
                except Exception as e:
                    logger.debug(f"warmup chat={chat_id} [{monitor.account['name']}]: {type(e).__name__}")

        tasks: list = []
        for m in self.monitors:
            if m.client and m.is_connected:
                for cid in chat_ids:
                    tasks.append(asyncio.create_task(_warm(m, int(cid))))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info(f"🔥 chat-entity cache warmed: {len(chat_ids)} chats × "
                        f"{sum(1 for m in self.monitors if m.is_connected)} accounts")

    async def run(self) -> None:
        self.is_running = True
        loop = asyncio.get_running_loop()

        def _handle_signal(sig: signal.Signals) -> None:
            logger.warning(f"Received signal {getattr(sig, 'name', sig)}, shutting down...")
            self._shutdown_event.set()
            self._track_task(self.stop(signal=sig), "signal_shutdown")

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: _handle_signal(s))
            except (NotImplementedError, ValueError, RuntimeError):
                pass

        try:
            await self._send_startup_message()
            logger.info("=" * 60)
            logger.info(f"🤖 Bot v{BOT_VERSION} running on Render Cloud - 24/7 mode (hardened)")
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
            remaining = [t for t in list(self._background_tasks) if not t.done()]
            if remaining:
                await asyncio.gather(*remaining, return_exceptions=True)

    async def stop(self, signal: Any = None) -> None:
        if not self.is_running:
            return
        self.is_running = False
        self._shutdown_event.set()

        logger.info("Shutting down gracefully...")

        try:
            import negative_memory as _negmem
            await _negmem.flush_now(self.db)
        except Exception:
            pass

        tasks = [t for t in list(self._background_tasks) if t is not asyncio.current_task()]
        for task in tasks:
            if not task.done():
                task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            from task_util import cancel_all as _tu_cancel_all
            _n_cancelled = await _tu_cancel_all(timeout=5.0)
            if _n_cancelled:
                logger.info(
                    f"Shutdown: cancelled {_n_cancelled} task_util background task(s)"
                )
        except Exception as _tu_err:
            logger.debug(f"task_util.cancel_all skipped: {type(_tu_err).__name__}")

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
        if self.alert_ui_client is not None:
            await self.alert_ui_client.stop()
            self.alert_ui_client = None
        try:
            await self.alert_bot.aclose()
        except Exception:
            pass
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
            db_health_status = "✅ سليمة" if self.db.db_healthy else "⚠️ غير مستقرة"

            await self.main_client.send_message(
                CFG.ADMIN_CHAT_ID,
                f"<b>🚀 البوت يعمل الآن على سحابة Render</b>\n\n"
                f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
                f"👥 الحسابات: {connected_count}/{len(self.monitors)}\n"
                f"🗂 الطابور: {qsize} رسالة معلقة\n"
                f"🗄 قاعدة البيانات: {db_health_status}\n"
                f"🔑 الكلمات المفتاحية: {kw_count:,}\n\n"
                f"<b>الحسابات:</b>\n{acct_lines}\n\n"
                f"💡 اكتب /help للأوامر المتاحة",
                parse_mode="html",
            )
        except Exception as e:
            logger.error(f"Startup message failed: {e}")


async def main() -> None:
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
