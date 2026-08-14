#!/usr/bin/env python3
"""
main.py – Enhanced Telegram Bot v14.0 (RENDER CLOUD EDITION, HARDENED)
متوافق مع استضافة Render المجانية (Web Service) – تشغيل 24/7
Compatible with: config.py v13.1, database.py v9.0, filter_engine.py v14.1,
                  monitors.py v9.7

v14.0 (this pass) — full audit fix, main.py ONLY. Every change below maps to
an issue ID from the accompanying engineering report (M-01..M-17):

  M-01/M-02/M-03/M-13/M-14 — MemoryMonitor was running tracemalloc without
      ever using its snapshots, and "leak_detected" was a single-sample
      `current_rss > baseline * 1.5` heuristic that produces both false
      positives (caches filling up normally after boot) and false
      negatives (slow gradual growth). Replaced with a trend-based
      ResourceMonitor: it keeps a bounded history of RSS samples and only
      raises a "growth trend" signal when a sustained monotonic rise is
      observed across consecutive samples — never from one reading. When a
      trend IS confirmed, it takes a tracemalloc snapshot diff against the
      first snapshot and logs the top allocating call sites, so the
      tracemalloc overhead now actually produces diagnostic value instead
      of running for nothing. Alert wording no longer claims "memory leak"
      (a diagnosis this heuristic can never prove) — it reports "sustained
      RSS growth" instead, which is what it actually measured.

  M-15 — Memory monitoring only ever looked at RSS. ResourceMonitor now
      also tracks SQLite DB file size and persistent queue size
      (CFG.DB_FILE, db.queue_size()) as separate signals, so disk/queue
      growth is visible even when RAM looks fine.

  M-04 — MESSAGE_QUEUE_SIZE previously only bounded the in-memory
      asyncio.Queue inside _consumer_loop while the SQLite
      processing_queue could grow unbounded. database.py v9.0 now enforces
      MESSAGE_QUEUE_SIZE as a real cap on processing_queue itself (DROP_
      OLDEST policy, fix #3/#9) and exposes `queue_evictions` /
      `db_healthy`. main.py no longer implies MESSAGE_QUEUE_SIZE only
      governs the local buffer — it surfaces the authoritative persistent
      queue size and eviction count in /stats, /health and the periodic
      report, and the internal asyncio.Queue is explicitly documented as a
      local prefetch buffer, not a second source of truth for capacity.

  M-05/M-16 — Background tasks (including the signal-handler shutdown
      task) were fire-and-forget with no unified lifecycle. All tasks are
      now created through `_spawn_task(name, coro)`, tracked in
      `self._tasks`, and `stop()` cancels + awaits every tracked task by
      name in one place. The signal handler now goes through the same
      helper instead of a bare `asyncio.create_task`.

  M-06/M-12 — Duplicate cleanup ownership: database.py v9.0 explicitly
      removed its own `_cleanup_loop` (fix #5) and documents that main.py
      is the SOLE owner of `cleanup_old_data` / `cleanup_dead_letters`
      scheduling. main.py's `_cleanup_loop` is kept as that single owner
      (no change in behavior needed there — the duplication was already
      resolved on the database.py side), and this is now stated explicitly
      in code comments so the ownership boundary can't silently regress.

  M-07 — `_keep_alive_loop` used to fall back to `http://127.0.0.1:<PORT>`
      when RENDER_EXTERNAL_URL was unset, which pings the process from
      itself and can never prevent Render free-tier sleep (it isn't
      external traffic). The loop now refuses to run a fake local
      "keep-alive" — if RENDER_EXTERNAL_URL is missing it logs a clear
      warning once and does not start the self-ping loop at all. Health
      checking (the `/health` endpoint itself) is unaffected; only the
      external self-ping is gated on having a real external URL.

  M-08/M-09/M-17 — Statistics schema mismatch: monitors.py's
      `EnhancedAccountMonitor.get_stats()` returns `alerts_sent`, not
      `alerts`, and has no per-account `queued` field (the processing
      queue is global, owned by database.py). `/status` was reading
      `s.get('alerts', 0)` (always 0) and no code referenced a `queued`
      key that never existed. Fixed to read `alerts_sent`, and the queue
      is now only ever reported once, globally, via `db.queue_size()` —
      never faked as a per-account number.

  M-10/M-11 — Admin commands and alert sending depended entirely on
      `self.main_client` with no immediate failover; the only fallback
      path was `_health_check_loop`, which could leave a window of up to
      `HEALTH_CHECK_INTERVAL` seconds where admin commands and stats
      broadcasting are dead even though a healthy fallback account exists.
      Added `_ensure_main_client()`, a single failover routine that checks
      `main_client` liveness on demand and immediately promotes the first
      connected monitor's client if it is down. This is now called before
      every admin command, before the periodic stats broadcast, and is
      still also invoked from `_health_check_loop` as a safety net — so
      failover is event-driven first, polling-driven second.

All existing public method names/signatures used by monitors.py /
database.py / filter_engine.py / dashboard.py are preserved.
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
# Resource Monitor (v14.0) — replaces the old MemoryMonitor
#
# Fixes M-01 / M-02 / M-03 / M-13 / M-14 / M-15:
#   * tracemalloc is only snapshotted (and only diffed/logged) once a
#     genuine growth TREND is detected across several consecutive samples
#     — not on every check — so it now produces real diagnostic value
#     instead of running for nothing.
#   * "leak_detected" (single-sample RSS > baseline * 1.5) is replaced by
#     a trend signal that requires N consecutive rising samples with a
#     minimum cumulative growth, which is far more resistant to normal
#     post-boot cache warm-up (false positive) and slow gradual growth
#     (false negative) than a single threshold check.
#   * Disk/queue growth (SQLite DB file size, persistent queue size) is
#     tracked as its own signal, separate from RAM, since disk exhaustion
#     never shows up as RSS growth.
#   * The public `check()` dict keeps `leak_detected` for backward
#     compatibility with any external readers, but it is now driven by the
#     trend detector, and a new `growth_trend` boolean + `trend_reason`
#     string are added so callers can distinguish "RSS is high" from
#     "RSS is *trending upward*", which are not the same claim.
# =============================================================================
class ResourceMonitor:
    def __init__(
        self,
        history_size: int = 12,
        trend_min_samples: int = 6,
        trend_min_growth_ratio: float = 1.25,
        db_file: Optional[str] = None,
    ) -> None:
        self._baseline = 0
        self._peak = 0
        self._history: Deque[int] = deque(maxlen=history_size)
        self._trend_min_samples = trend_min_samples
        self._trend_min_growth_ratio = trend_min_growth_ratio
        self._db_file = db_file
        self._tracemalloc_started = False
        self._first_snapshot: Optional[tracemalloc.Snapshot] = None
        self._last_trend_report_ts: float = 0.0

    def start(self) -> None:
        try:
            tracemalloc.start()
            self._tracemalloc_started = True
        except Exception as e:
            logger.warning(f"tracemalloc.start() failed, diagnostics disabled: {e}")
            self._tracemalloc_started = False
        self._baseline = self._current_rss()
        if self._baseline:
            self._history.append(self._baseline)

    def _current_rss(self) -> int:
        if PSUTIL_AVAILABLE:
            try:
                return psutil.Process(os.getpid()).memory_info().rss
            except Exception:
                pass
        return 0

    def _detect_growth_trend(self) -> tuple[bool, str]:
        """
        A trend is only reported when there are enough samples AND the
        series is (near-)monotonically increasing AND the total growth
        across the window exceeds trend_min_growth_ratio. A single spike
        (e.g. one large batch flush) does not qualify.
        """
        if len(self._history) < self._trend_min_samples:
            return False, "insufficient_samples"

        window = list(self._history)[-self._trend_min_samples:]
        rising_steps = sum(1 for a, b in zip(window, window[1:]) if b >= a)
        total_steps = len(window) - 1
        if total_steps <= 0:
            return False, "insufficient_samples"

        monotonic_ratio = rising_steps / total_steps
        first, last = window[0], window[-1]
        growth_ratio = (last / first) if first > 0 else 1.0

        if monotonic_ratio >= 0.8 and growth_ratio >= self._trend_min_growth_ratio:
            return True, f"monotonic_ratio={monotonic_ratio:.2f} growth_ratio={growth_ratio:.2f}"
        return False, f"monotonic_ratio={monotonic_ratio:.2f} growth_ratio={growth_ratio:.2f}"

    def _log_tracemalloc_diagnostics(self) -> None:
        if not self._tracemalloc_started:
            return
        try:
            snapshot = tracemalloc.take_snapshot()
            if self._first_snapshot is None:
                self._first_snapshot = snapshot
                return
            stats = snapshot.compare_to(self._first_snapshot, "lineno")
            top = stats[:5]
            lines = ["🔎 RSS growth trend confirmed — top allocation deltas since boot:"]
            for stat in top:
                lines.append(f"   {stat}")
            logger.warning("\n".join(lines))
        except Exception as e:
            logger.debug(f"tracemalloc diagnostics failed: {e}")

    def check(self) -> Dict[str, Any]:
        current = self._current_rss()
        self._peak = max(self._peak, current)
        if current:
            self._history.append(current)

        growth_trend, trend_reason = self._detect_growth_trend()

        if growth_trend:
            # Rate-limit diagnostic snapshot logging so it can't itself
            # become a source of overhead (once per 10 min max).
            now = time.time()
            if now - self._last_trend_report_ts > 600:
                self._last_trend_report_ts = now
                self._log_tracemalloc_diagnostics()

        return {
            "current_mb": current // (1024 * 1024),
            "peak_mb": self._peak // (1024 * 1024),
            "baseline_mb": self._baseline // (1024 * 1024),
            # Kept for backward compatibility with any external readers;
            # now driven by the trend detector instead of a single ratio.
            "leak_detected": growth_trend,
            "growth_trend": growth_trend,
            "trend_reason": trend_reason,
        }

    def check_disk(self) -> Dict[str, Any]:
        """
        Separate signal for disk/queue growth (M-15) — never conflated
        with RAM. Root cause of unbounded queue growth itself lives in
        database.py (now capped there); this just makes the size visible.
        """
        out: Dict[str, Any] = {"db_file_mb": 0.0, "db_file_exists": False}
        if self._db_file:
            try:
                if os.path.exists(self._db_file):
                    out["db_file_exists"] = True
                    out["db_file_mb"] = round(os.path.getsize(self._db_file) / (1024 * 1024), 2)
            except Exception:
                pass
        return out

    def force_gc(self) -> int:
        gc.collect()
        return self._current_rss()


# =============================================================================
# Main Bot Class v14.0
# =============================================================================
class EnhancedTelegramBot:
    def __init__(self) -> None:
        self.db = EnhancedDatabase()
        self.filter = EnhancedFilter()
        self.rate_limiter = AdaptiveRateLimiter(
            CFG.MAX_ALERTS_PER_MINUTE,
            CFG.MAX_ALERTS_PER_HOUR,
        )
        self.resource_monitor = ResourceMonitor(db_file=CFG.DB_FILE)
        self.main_client: Optional[TelegramClient] = None
        self.monitors: List[EnhancedAccountMonitor] = []
        self.is_running = False
        self._start_time = time.monotonic()
        self.health = HealthMonitor(self)

        # ── Unified background-task lifecycle (M-05 / M-16) ────────────
        # Every asyncio.Task this bot creates — including the signal
        # handler's shutdown task — is created through _spawn_task() and
        # tracked here by name, so stop() can cancel+await all of them in
        # one place instead of relying on a hand-maintained list of
        # Optional[Task] attributes.
        self._tasks: Dict[str, asyncio.Task] = {}
        self._main_client_lock = asyncio.Lock()

    # ─── Background task lifecycle helper (M-05 / M-16) ────────────────────
    def _spawn_task(self, name: str, coro: Any) -> asyncio.Task:
        existing = self._tasks.get(name)
        if existing and not existing.done():
            logger.warning(f"Task '{name}' already running, not spawning a duplicate")
            return existing
        task = asyncio.create_task(coro, name=name)
        self._tasks[name] = task

        def _on_done(t: asyncio.Task, task_name: str = name) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error(f"Background task '{task_name}' crashed: {exc}")

        task.add_done_callback(_on_done)
        return task

    async def _cancel_all_tasks(self) -> None:
        tasks = [t for t in self._tasks.values() if t and not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # ─── Consumer Loop (Producer-Consumer) ────────────────────────────────────
    async def _consumer_loop(self) -> None:
        """
        NOTE (M-04): `internal_queue` below is a local prefetch buffer only.
        The authoritative capacity limit now lives in database.py's
        `processing_queue` table itself (enforced via CFG.MESSAGE_QUEUE_SIZE
        with a DROP_OLDEST eviction policy — see database.py fix #3/#9).
        Sizing this local queue the same way keeps backpressure consistent
        end-to-end, but it does NOT duplicate or replace the persistent cap.
        """
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

    # ─── Main-client failover (M-10 / M-11) ────────────────────────────────
    async def _ensure_main_client(self) -> bool:
        """
        Single, event-driven failover routine. Previously the ONLY path
        that could replace a dead main_client was `_health_check_loop`,
        which polls every CFG.HEALTH_CHECK_INTERVAL seconds — leaving a
        window where admin commands / alert sending / stats broadcasting
        are silently dead even though a healthy fallback account exists.

        This is now called:
          - before every admin command
          - before the periodic stats broadcast
          - from _health_check_loop itself (so polling is now just a
            safety net, not the only detection path)

        Returns True if self.main_client is alive after this call.
        """
        if await self._client_ok(self.main_client):
            return True

        async with self._main_client_lock:
            # Re-check under the lock in case another caller already fixed it.
            if await self._client_ok(self.main_client):
                return True
            for m in self.monitors:
                if m.is_connected and await self._client_ok(m.client):
                    self.main_client = m.client
                    for mon in self.monitors:
                        mon.main_client = self.main_client
                    logger.warning(f"⚠️ Main client failover -> {m.account['name']}")
                    return True
        return False

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
            db_stats = await self.db.get_stats()
            return web.json_response({
                "status": "ok" if health.is_healthy else "degraded",
                "checks": health.checks,
                "uptime": int(time.monotonic() - self._start_time),
                "queue_size": await self.db.queue_size(),
                "queue_evictions": db_stats.get("queue_evictions", 0),
                "db_healthy": db_stats.get("db_healthy", True),
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
        """
        FIX (M-07): previously fell back to pinging 127.0.0.1 when
        RENDER_EXTERNAL_URL was unset. A local loopback request is not
        external traffic and can never prevent Render free-tier sleep —
        it only *looked* like a working keep-alive. Now: if there is no
        real external URL, this loop does not start at all, and a single
        clear warning is logged so the gap is visible instead of silently
        papered over. The `/health` endpoint itself is unaffected — this
        only gates the *self-ping* behavior.
        """
        if not AIOHTTP_AVAILABLE:
            return
        external_url = (os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
        if not external_url:
            logger.warning(
                "⚠️ RENDER_EXTERNAL_URL is not set — external keep-alive self-ping "
                "is DISABLED (a loopback ping to 127.0.0.1 would not be real "
                "external traffic and cannot prevent Render free-tier sleep). "
                "Set RENDER_EXTERNAL_URL, or use an external uptime pinger "
                "pointed at this service's public /health URL."
            )
            return

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

                # M-10/M-11: try failover immediately rather than skipping
                # the whole report just because main_client happens to be
                # down at this exact moment.
                if not await self._ensure_main_client():
                    logger.debug("Stats reporter: no live client available, skipping this cycle")
                    continue

                stats = await self.db.get_stats()
                filter_tele = await self.filter.get_telemetry()
                uptime = time.monotonic() - self._start_time
                h, rem = divmod(int(uptime), 3600)
                m_min = rem // 60
                rl = self.rate_limiter.status()
                mem = self.resource_monitor.check()
                disk = self.resource_monitor.check_disk()
                qsize = await self.db.queue_size()
                connected = sum(1 for m in self.monitors if m.is_connected)

                hit_rate = (filter_tele.get("valid", 0) / total * 100) if (total := filter_tele.get("processed", 0)) else 0.0

                text = (
                    f"<b>📊 إحصائيات البوت v14.0 (Render)</b>\n\n"
                    f"⏱ وقت التشغيل: {h}س {m_min}د\n"
                    f"📨 الرسائل: {stats.get('total_messages', 0):,}\n"
                    f"🚨 التنبيهات: {stats.get('alerts_sent', 0):,}\n"
                    f"👥 الحسابات: {connected}/{len(self.monitors)}\n"
                    f"🎯 نسبة الاصطياد: {hit_rate:.1f}%\n"
                    f"🗂 الطابور (عام): {qsize} رسالة | إخلاء: {stats.get('queue_evictions', 0)}\n"
                    f"📉 حد التنبيهات: {rl['per_min']}/{rl['limit_min']} بالدقيقة\n"
                    f"🧠 الذاكرة: {mem.get('current_mb', 0)}MB (ذروة {mem.get('peak_mb', 0)}MB)\n"
                    f"💽 قاعدة البيانات: {disk.get('db_file_mb', 0)}MB\n"
                    f"🩺 صحة DB: {'✅' if stats.get('db_healthy', True) else '⚠️ متدهورة'}"
                )
                if mem.get("growth_trend"):
                    text += "\n⚠️ <b>اتجاه نمو مستمر في استهلاك الذاكرة (RSS) — راجع السجلات</b>"

                await self.main_client.send_message(CFG.ADMIN_CHAT_ID, text, parse_mode="html")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Stats reporter error: {e}")

    async def _cleanup_loop(self) -> None:
        """
        NOTE (M-06/M-12): main.py is the SOLE owner of database maintenance
        scheduling. database.py v9.0 explicitly removed its own internal
        cleanup loop (see database.py fix #5) specifically so this is the
        only place `cleanup_old_data` / `cleanup_dead_letters` are ever
        invoked from — no duplicate scheduling exists anymore.
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

    async def _health_check_loop(self) -> None:
        """
        Polling-based safety net. Immediate/event-driven failover now
        happens via _ensure_main_client() at each call site (M-10/M-11);
        this loop is kept as a periodic backstop for cases nothing else
        happened to trigger a check in between.
        """
        while self.is_running:
            try:
                await asyncio.sleep(CFG.HEALTH_CHECK_INTERVAL)
                health = await self.health.check()
                if not health.is_healthy:
                    logger.warning(f"Health check failed: {health.checks}")
                    if health.checks.get("main_client") == "down":
                        await self._ensure_main_client()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")

    async def _memory_monitor_loop(self) -> None:
        while self.is_running:
            try:
                await asyncio.sleep(CFG.GC_INTERVAL)
                mem = self.resource_monitor.check()
                if mem.get("growth_trend"):
                    logger.warning(f"Sustained RSS growth trend detected: {mem}")
                    self.resource_monitor.force_gc()
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
            pattern=r"^/(stats|status|help|block|unblock|purge|accounts|health|filter_stats|dashboard|reload_keywords)(.*)$",
        ))
        async def _admin_handler(event: Any) -> None:
            try:
                await self._handle_admin_command(event)
            except Exception as e:
                logger.error(f"Admin command error: {e}")

        logger.info("Admin command listener registered")

    async def _handle_admin_command(self, event: Any) -> None:
        # M-10/M-11: try to repair main_client immediately before doing
        # anything else, rather than only relying on the periodic health
        # loop to eventually notice.
        await self._ensure_main_client()

        text: str = (event.message.text or "").strip()
        cmd, _, args = text.partition(" ")
        cmd = cmd.lstrip("/").lower()

        if cmd == "help":
            await event.reply(
                "<b>أوامر البوت:</b>\n/stats – إحصائيات\n/status – حالة الحسابات\n"
                "/accounts – التفاصيل\n/health – الصحة\n/dashboard – لوحة التحكم\n"
                "/reload_keywords – إعادة تحميل الكلمات المفتاحية\n"
                "/block <id> – حظر\n/unblock <id> – رفع حظر\n/purge – تفريغ الطابور",
                parse_mode="html",
            )
        elif cmd == "stats":
            db_stats = await self.db.get_stats()
            filter_tel = await self.filter.get_telemetry()
            rl = self.rate_limiter.status()
            uptime = time.monotonic() - self._start_time
            h, rem = divmod(int(uptime), 3600)
            m_min = rem // 60
            await event.reply(
                f"<b>📊 إحصائيات فورية</b>\n⏱ {h}س {m_min}د\n"
                f"📨 رسائل: {db_stats.get('total_messages', 0):,}\n"
                f"🚨 تنبيهات: {db_stats.get('alerts_sent', 0):,}\n"
                f"🗂 الطابور (عام): {await self.db.queue_size()} | إخلاء: {db_stats.get('queue_evictions', 0)}\n"
                f"🩺 صحة DB: {'✅' if db_stats.get('db_healthy', True) else '⚠️ متدهورة'}\n"
                f"📉 {rl['per_min']}/{rl['limit_min']} في الدقيقة",
                parse_mode="html",
            )
        elif cmd == "status":
            connected = sum(1 for m in self.monitors if m.is_connected)
            lines = [f"<b>📱 حالة الحسابات ({connected}/{len(self.monitors)})</b>\n"]
            for m in self.monitors:
                icon = "✅" if m.is_connected else "❌"
                s = await m.get_stats()
                # FIX (M-08/M-17): monitors.py's get_stats() returns
                # 'alerts_sent', never 'alerts' — reading 'alerts' always
                # produced a misleading 0 here.
                lines.append(
                    f"{icon} <b>{m.account['name']}</b> | تنبيهات: {s.get('alerts_sent', 0)} | "
                    f"رسائل: {s.get('messages_processed', 0)} | أخطاء: {s.get('errors', 0)}"
                )
            # FIX (M-09/M-17): the processing queue is global (owned by
            # database.py), not a per-account figure — shown once here
            # instead of implying each account has its own queue.
            lines.append(f"\n🗂 الطابور العام: {await self.db.queue_size()} رسالة")
            await event.reply("\n".join(lines), parse_mode="html")
        elif cmd == "accounts":
            lines = ["<b>🔑 تفاصيل الحسابات</b>\n"]
            for m in self.monitors:
                s = await m.get_stats()
                icon = "🟢" if m.is_connected else "🔴"
                lines.append(
                    f"{icon} {m.account['name']}\n   📞 {m.account['phone']}\n"
                    f"   🚨 تنبيهات: {s.get('alerts_sent', 0)}\n"
                    f"   ⚡ آخر خطأ: {(s.get('last_error') or 'لا شيء')[:60]}"
                )
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
        elif cmd == "reload_keywords":
            # Wired to filter_engine.py v14.1's reload_keywords()/
            # _build_keyword_sets alias, so a keywords.json edit can take
            # effect without a redeploy.
            try:
                self.filter.reload_keywords()
                await event.reply("✅ تم إعادة تحميل الكلمات المفتاحية بنجاح")
            except Exception as e:
                await event.reply(f"❌ فشل إعادة التحميل: {e}")
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
        logger.info("Enhanced Telegram Bot v14.0 (RENDER EDITION, HARDENED) – Initializing...")
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
            self._spawn_task("dashboard", self._start_dashboard())
            logger.info("Dashboard task started (serves /health on $PORT)")
        else:
            self._spawn_task("health_server", self._health_server())
            logger.info("Dashboard disabled - standalone health server on $PORT")

        self._spawn_task("keep_alive", self._keep_alive_loop())

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
            await self._ensure_main_client()

        logger.info(f"\n📊 Connected: {connected}/{len(accounts)}")

        if connected == 0:
            logger.warning(
                "No accounts connected - running in dashboard-only mode. "
                "Fix the *_SESSION_STRING env vars (or use the /login page), then restart."
            )

        self.resource_monitor.start()

        await self._register_admin_commands()
        await self._register_copy_handler()

        # Background Tasks — all through the unified lifecycle (M-05/M-16)
        self._spawn_task("consumer", self._consumer_loop())
        self._spawn_task("stats", self._stats_reporter())
        self._spawn_task("cleanup", self._cleanup_loop())
        self._spawn_task("health", self._health_check_loop())
        self._spawn_task("memory", self._memory_monitor_loop())

        logger.info("✅ Initialization complete (Render Edition)")
        return True

    # ─── Run & Shutdown ────────────────────────────────────────────────────────
    async def run(self) -> None:
        self.is_running = True
        loop = asyncio.get_running_loop()

        # FIX (M-05/M-16): the shutdown task triggered by SIGINT/SIGTERM now
        # goes through the same tracked _spawn_task() lifecycle as every
        # other background task, instead of a bare fire-and-forget
        # asyncio.create_task() with no reference kept anywhere. This
        # guarantees Render's SIGTERM reliably drives a graceful shutdown
        # that stop()/_cancel_all_tasks() can account for.
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: self._spawn_task("shutdown", self.stop(signal=s)),
                )
            except (NotImplementedError, ValueError, RuntimeError):
                # في بعض البيئات، قد لا تكون هذه الإشارات مدعومة بالكامل
                pass

        try:
            await self._send_startup_message()
            logger.info("=" * 60)
            logger.info("🤖 Bot v14.0 running on Render Cloud - 24/7 mode")
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

        # FIX (M-05/M-16): single point of cancellation/await for every
        # tracked background task (dashboard/health_server, keep_alive,
        # consumer, stats, cleanup, health, memory, and — if this stop()
        # call itself originated from the signal handler — that task is
        # excluded from self-cancellation since it's the one currently
        # running this coroutine.
        current = asyncio.current_task()
        tasks = [t for name, t in self._tasks.items() if t and not t.done() and t is not current]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks = {k: v for k, v in self._tasks.items() if v is current}

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

            await self.main_client.send_message(
                CFG.ADMIN_CHAT_ID,
                f"<b>🚀 البوت يعمل الآن على سحابة Render (v14.0)</b>\n\n"
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
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    else:
        if UVLOOP_AVAILABLE:
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
            logger.info("Using uvloop for optimal performance")
        else:
            logger.info("uvloop not found. Using default asyncio loop")

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
