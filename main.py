#!/usr/bin/env python3
"""
main.py – Enhanced Telegram Bot v13.0 (RENDER CLOUD EDITION)
متوافق مع استضافة Render المجانية (Web Service) – تشغيل 24/7
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
from typing import Any, Deque, Dict, List, Optional

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

from config import CFG, ACCOUNTS, KEYWORDS, InputSanitizer, logger
from database import EnhancedDatabase
from filter_engine import EnhancedFilter
from monitors import EnhancedAccountMonitor, HealthMonitor

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
# Adaptive Rate Limiter (Token Bucket + Per-Account)
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
# Memory Monitor
# =============================================================================
class MemoryMonitor:
    def __init__(self, threshold_mb: int = 512) -> None:
        self._threshold = threshold_mb * 1024 * 1024
        self._baseline = 0
        self._peak = 0

    def start(self) -> None:
        tracemalloc.start()
        self._baseline = self._current_rss()

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
        leak = current > self._baseline * 1.5 if self._baseline > 0 else False
        return {
            "current_mb": current // (1024 * 1024),
            "peak_mb": self._peak // (1024 * 1024),
            "baseline_mb": self._baseline // (1024 * 1024),
            "leak_detected": leak,
        }

    def force_gc(self) -> int:
        gc.collect()
        return self._current_rss()


# =============================================================================
# Main Bot Class v8.1 (Pydroid 3 Edition)
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

        # Tasks
        self._consumer_task: Optional[asyncio.Task] = None
        self._stats_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._memory_task: Optional[asyncio.Task] = None
        self._health_server_task: Optional[asyncio.Task] = None
        self._dashboard_task: Optional[asyncio.Task] = None
        self._keep_alive_task: Optional[asyncio.Task] = None

    # ─── Consumer Loop (Producer-Consumer) ────────────────────────────────────
    async def _consumer_loop(self) -> None:
        logger.info("Consumer loop started")
        internal_queue: asyncio.Queue = asyncio.Queue(maxsize=CFG.MESSAGE_QUEUE_SIZE)

        async def producer():
            while self.is_running:
                try:
                    event_data = await self.db.pop_from_queue()
                    if event_data:
                        await internal_queue.put(event_data)
                    else:
                        await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Producer error: {e}")
                    await asyncio.sleep(1)

        async def worker(worker_id: int):
            while self.is_running:
                try:
                    event_data = await asyncio.wait_for(internal_queue.get(), timeout=1.0)
                    account_name = event_data.get("account_name", "")
                    monitor = self._get_monitor(account_name)
                    if monitor:
                        await monitor.process_event_from_queue(event_data)
                    else:
                        for m in self.monitors:
                            if m.is_connected:
                                await m.process_event_from_queue(event_data)
                                break
                    internal_queue.task_done()
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Worker {worker_id} error: {e}")
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

    # ─── Copy Handler ─────────────────────────────────────────────────────────
    async def _register_copy_handler(self) -> None:
        if not self.main_client:
            return

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

        logger.info("Copy button callback handler registered")

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
                "monitors": sum(1 for m in self.monitors if m.is_connected),
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

        await asyncio.Event().wait()

    # ─── Dashboard Server (main web face on Render: binds $PORT) ──────────────
    async def _start_dashboard(self):
        if not DASHBOARD_AVAILABLE:
            return
        set_bot_reference(self)
        # Render Web Service exposes a single port via $PORT
        port = int(os.getenv("PORT", CFG.DASHBOARD_PORT))
        logger.info(f"🚀 Starting Dashboard + Health endpoint on port {port}")
        try:
            await run_dashboard(host="0.0.0.0", port=port)
        except Exception as e:
            logger.error(f"Failed to start Dashboard on port {port}: {e}")

    # ─── Keep-Alive Self-Ping (prevents Render free-tier sleep) ───────────────
    async def _keep_alive_loop(self) -> None:
        if not AIOHTTP_AVAILABLE:
            return
        external_url = (os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
        if not external_url:
            port = int(os.getenv("PORT", 10000))
            external_url = f"http://127.0.0.1:{port}"
        url = f"{external_url}/health"
        await asyncio.sleep(60)  # let the web server come up first
        logger.info(f"Keep-alive self-ping enabled -> {url} (every 10 min)")
        while self.is_running:
            try:
                timeout = ClientTimeout(total=30)
                async with ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        logger.debug(f"Keep-alive ping {url} -> HTTP {resp.status}")
            except Exception as e:
                logger.warning(f"Keep-alive ping failed: {e}")
            # Render free tier sleeps after ~15 min of no inbound traffic
            await asyncio.sleep(600)

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
                rl = self.rate_limiter.status()
                mem = self.memory_monitor.check()
                qsize = await self.db.queue_size()
                connected = sum(1 for m in self.monitors if m.is_connected)

                hit_rate = (filter_tele.get("valid", 0) / total * 100) if (total := filter_tele.get("processed", 0)) else 0.0

                text = (
                    f"<b>📊 إحصائيات البوت v13.0 (Render)</b>\n\n"
                    f"⏱ وقت التشغيل: {h}س {m_min}د\n"
                    f"📨 الرسائل: {stats.get('total_messages', 0):,}\n"
                    f"🚨 التنبيهات: {stats.get('alerts_sent', 0):,}\n"
                    f"👥 الحسابات: {connected}/{len(self.monitors)}\n"
                    f"🎯 نسبة الاصطياد: {hit_rate:.1f}%\n"
                    f"🗂 الطابور: {qsize} رسالة\n"
                    f"🧠 الذاكرة: {mem.get('current_mb', 0)}MB"
                )
                if mem.get("leak_detected"):
                    text += "\n⚠️ <b>تسرب ذاكرة محتمل!</b>"

                await self.main_client.send_message(CFG.ADMIN_CHAT_ID, text, parse_mode="html")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Stats reporter error: {e}")

    async def _cleanup_loop(self) -> None:
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

    async def _health_check_loop(self) -> None:
        while self.is_running:
            try:
                await asyncio.sleep(CFG.HEALTH_CHECK_INTERVAL)
                health = await self.health.check()
                if not health.is_healthy:
                    logger.warning(f"Health check failed: {health.checks}")
                    if health.checks.get("main_client") == "down":
                        for m in self.monitors:
                            if m.is_connected and m.client:
                                self.main_client = m.client
                                for mon in self.monitors:
                                    mon.main_client = self.main_client
                                logger.info(f"New main client: {m.account['name']}")
                                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")

    async def _memory_monitor_loop(self) -> None:
        while self.is_running:
            try:
                await asyncio.sleep(CFG.GC_INTERVAL)
                mem = self.memory_monitor.check()
                if mem.get("leak_detected"):
                    logger.warning(f"Memory leak detected: {mem}")
                    self.memory_monitor.force_gc()
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

        @self.main_client.on(tl_events.NewMessage(
            chats=CFG.ADMIN_CHAT_ID, incoming=True,
            pattern=r"^/(stats|status|help|block|unblock|purge|accounts|health|filter_stats|dashboard)(.*)$",
        ))
        async def _admin_handler(event: Any) -> None:
            try:
                await self._handle_admin_command(event)
            except Exception as e:
                logger.error(f"Admin command error: {e}")

        logger.info("Admin command listener registered")

    async def _handle_admin_command(self, event: Any) -> None:
        text: str = (event.message.text or "").strip()
        cmd, _, args = text.partition(" ")
        cmd = cmd.lstrip("/").lower()

        if cmd == "help":
            await event.reply("<b>أوامر البوت:</b>\n/stats – إحصائيات\n/status – حالة الحسابات\n/accounts – التفاصيل\n/health – الصحة\n/dashboard – لوحة التحكم\n/block <id> – حظر\n/unblock <id> – رفع حظر\n/purge – تفريغ الطابور", parse_mode="html")
        elif cmd == "stats":
            db_stats = await self.db.get_stats()
            filter_tel = await self.filter.get_telemetry()
            rl = self.rate_limiter.status()
            uptime = time.monotonic() - self._start_time
            h, rem = divmod(int(uptime), 3600)
            m_min = rem // 60
            await event.reply(f"<b>📊 إحصائيات فورية</b>\n⏱ {h}س {m_min}د\n📨 رسائل: {db_stats.get('total_messages', 0):,}\n🚨 تنبيهات: {db_stats.get('alerts_sent', 0):,}\n🗂 الطابور: {await self.db.queue_size()}\n📉 {rl['per_min']}/{rl['limit_min']} في الدقيقة", parse_mode="html")
        elif cmd == "status":
            connected = sum(1 for m in self.monitors if m.is_connected)
            lines = [f"<b>📱 حالة الحسابات ({connected}/{len(self.monitors)})</b>\n"]
            for m in self.monitors:
                icon = "✅" if m.is_connected else "❌"
                s = await m.get_stats()
                lines.append(f"{icon} <b>{m.account['name']}</b> | تنبيهات: {s.get('alerts', 0)}")
            await event.reply("\n".join(lines), parse_mode="html")
        elif cmd == "accounts":
            lines = ["<b>🔑 تفاصيل الحسابات</b>\n"]
            for m in self.monitors:
                s = await m.get_stats()
                icon = "🟢" if m.is_connected else "🔴"
                lines.append(f"{icon} {m.account['name']}\n   📞 {m.account['phone']}\n   ⚡ آخر خطأ: {(s.get('last_error') or 'لا شيء')[:60]}")
            await event.reply("\n".join(lines), parse_mode="html")
        elif cmd == "health":
            health = await self.health.check()
            status = "✅ صحي" if health.is_healthy else "⚠️ غير صحي"
            lines = [f"<b>🏥 فحص الصحة: {status}</b>\n"]
            for check, value in health.checks.items():
                lines.append(f"• {check}: {value}")
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
                token = os.getenv("DASHBOARD_AUTH_TOKEN", "change-me")
                await event.reply(f"<b>🌐 رابط لوحة التحكم</b>\nالرابط: <code>{url}</code>\n🔑 Token: <code>{token}</code>", parse_mode="html")
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

    # ─── Initialization ────────────────────────────────────────────────────────
    async def initialize(self) -> bool:
        logger.info("=" * 60)
        logger.info("Enhanced Telegram Bot v13.0 (RENDER EDITION) – Initializing...")
        logger.info("=" * 60)

        if not await self.db.connect():
            logger.error("Database connection failed - aborting")
            return False

        # Start the web layer FIRST so Render's health check passes immediately
        # and the Dashboard stays reachable even before/without any account
        # connection (sessions can then be added via the /login page).
        # Single exposed port on Render: Dashboard (FastAPI) serves both the
        # control panel and /health. aiohttp health server is only a fallback.
        if CFG.DASHBOARD_ENABLED and DASHBOARD_AVAILABLE:
            self._dashboard_task = asyncio.create_task(self._start_dashboard(), name="dashboard")
            logger.info("Dashboard task started (serves /health on $PORT)")
        else:
            self._health_server_task = asyncio.create_task(self._health_server(), name="health_server")
            logger.info("Dashboard disabled - standalone health server on $PORT")

        self._keep_alive_task = asyncio.create_task(self._keep_alive_loop(), name="keep_alive")

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

        # Background Tasks
        self._consumer_task = asyncio.create_task(self._consumer_loop(), name="consumer")
        self._stats_task = asyncio.create_task(self._stats_reporter(), name="stats")
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="cleanup")
        self._health_task = asyncio.create_task(self._health_check_loop(), name="health")
        self._memory_task = asyncio.create_task(self._memory_monitor_loop(), name="memory")

        logger.info("✅ Initialization complete (Render Edition)")
        return True

    # ─── Run & Shutdown ────────────────────────────────────────────────────────
    async def run(self) -> None:
        self.is_running = True
        loop = asyncio.get_running_loop()

        # معالجة آمنة لإشارات الإغلاق في بيئة أندرويد
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: asyncio.create_task(self.stop(signal=s)),
                )
            except (NotImplementedError, ValueError, RuntimeError):
                # في Pydroid 3، قد لا تكون هذه الإشارات مدعومة بالكامل
                pass

        try:
            await self._send_startup_message()
            logger.info("=" * 60)
            logger.info("🤖 Bot v13.0 running on Render Cloud - 24/7 mode")
            logger.info("=" * 60)
            while self.is_running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.critical(f"Fatal error in run loop: {e}")
        finally:
            await self.stop()

    async def stop(self, signal: Any = None) -> None:
        if not self.is_running:
            return
        self.is_running = False

        logger.info("Shutting down gracefully...")

        tasks = [
            self._consumer_task, self._stats_task, self._cleanup_task,
            self._health_task, self._memory_task, self._health_server_task,
            self._dashboard_task, self._keep_alive_task
        ]
        for task in tasks:
            if task and not task.done():
                task.cancel()

        await asyncio.gather(*[t for t in tasks if t], return_exceptions=True)

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
        except Exception:
            pass

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

            # رسالة مخصصة للإصدار المحلي
            await self.main_client.send_message(
                CFG.ADMIN_CHAT_ID,
                f"<b>🚀 البوت يعمل الآن على سحابة Render</b>\n\n"
                f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
                f"👥 الحسابات: {connected_count}/{len(self.monitors)}\n"
                f"🗂 الطابور: {qsize} رسالة معلقة\n"
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
        # التحقق من uvloop بشكل آمن قبل استخدامه
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
    # تشغيل حلقة الأحداث
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
