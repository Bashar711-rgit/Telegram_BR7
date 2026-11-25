#!/usr/bin/env python3
"""
main.py – Enhanced Telegram Bot v14.5.1 (RENDER CLOUD EDITION, HARDENED)
متوافق مع استضافة Render المجانية (Web Service) – تشغيل 24/7
متوافق مع: monitors.py v14.5.1, config.py v14.5.1, filter_engine.py v14.5.1, database.py v14.5.1

v14.5.1 — correction pass (main.py aligned with the v14.5.1 stack):
  * ACCOUNTS is used directly from config (the module-level list). The
    previous code called getattr(CFG, "ACCOUNTS", None) — which is
    ALWAYS None, because ACCOUNTS is not a field on the frozen CFG
    dataclass. That made _pick_alertui_account() start from an empty
    candidate list and skip the is_main preference entirely, without any
    warning. The dead `next(... if a.get("is_main") ...)` line right
    above the live call is also removed (it was immediately overwritten
    and could only cause confusion).
  * stop() now awaits task_util.cancel_all() after cancelling its own
    tracked tasks. Tasks created via task_util.spawn_bg() from
    nav_resolver._discover_sender_username_via_message and from
    sender_resolver's write-behind persist path lived in
    task_util._tasks — NOT in self._background_tasks — so they were
    never awaited before db.close(). Result: an "Event loop is closed"
    RuntimeError during asyncio.run() cleanup, or a silently lost
    write-behind persist (access_hash, username) if it happened to be
    mid-flight at shutdown. cancel_all() waits up to 5s per pending
    task, then gives up cleanly.
  * All boot/stats/shutdown messages now read BOT_VERSION (imported
    from config) instead of a hard-coded "v13.1". Three contradictory
    version strings (v13.1 in three places, v14.5.1 everywhere else)
    used to appear in the operator's own logs.
  * /dashboard admin-command branch is reachable now. The handler
    existed in _handle_admin_command, but "dashboard" was missing from
    the regex alternation in the @client.on(NewMessage(...)) filter —
    so the intended "dashboard removed" reply never fired. Added
    "dashboard" to the pattern.
  * Removed getattr(CFG, "ALERT_UI_CLIENT_ENABLED", True) from the
    AlertUI boot gate. That field has never been declared in config.py,
    so the clause was always True — a phantom env switch that could
    never be set. The remaining guard (ALERT_BOT_ENABLED) is the real,
    documented kill switch for the alert subsystem.

v13.1 (historical) — targeted fixes, main.py ONLY. Maps to the audit's M-01..M-17:

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
import re
import signal
import sys
import threading
import time
import tracemalloc
from collections import deque
from datetime import datetime, timezone
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

# v15.1.0: لوحة التحكم الخفيفة — استيراد محمي (فشل-آمن: /health أبداً
# لا يتأثر بمشكلة في اللوحة)
try:
    from dashboard.server import setup_dashboard as _setup_dashboard
    from dashboard.api import hydrate_dashboard as _hydrate_dashboard
    DASHBOARD_AVAILABLE = True
except Exception as _dash_import_err:  # pragma: no cover
    DASHBOARD_AVAILABLE = False
    _setup_dashboard = None
    _hydrate_dashboard = None

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
    BOT_VERSION,      # v14.1: ثوابت وحدة المستوى (CFG frozen بلا هذه الحقول)
    BOT_CODENAME,
)
from database import EnhancedDatabase
from dedup import get_dedup_snapshot, init_deduplicator
from unified_alerts import run_startup_maintenance, unified_snapshot  # v12.0 unified_alerts
from unified_alerts.gate import get_alert_gate  # v15.6.0: السقف التكيفي لانتظار الطابور
from unified_alerts.activity import ActivityWatchdog  # v14.6.0 Activity Maintenance System
from antispam import get_antispam, setup_antispam, get_antispam_snapshot
from filter_engine import EnhancedFilter
from monitors import (
    EnhancedAccountMonitor, HealthMonitor, get_capture_snapshot, alert_latency,
    register_self_sender,  # v11.5.2 حاجز صدى التنبيهات
)
from alert_bot import AlertBot  # v10.8: إرسال التنبيهات عبر Bot API
from alert_ui import AlertBotClient  # v11.0: عميل بوت حي — Callbacks + إرسال احتياطي بالأزرار
from sender_resolver import get_sender_intel_snapshot, get_mention_intel_snapshot
from contact_target import get_contact_metrics_snapshot
from memory_guard import start_memory_guard, stop_memory_guard  # v14.5.1 (T3.4): RSS enforcement — soft 85% / hard 95%

# psutil optional
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    logger.warning("psutil not installed. Memory monitoring will be limited.")


class _HealthSnapshot:
    """v14.6.1: لقطة الصحة المشتركة بين حلقة الرصد الرئيسية وخيط خادم الصحة.

    * الكتابة: من حلقة اللقطة (كل 10s) ومن set_boot_complete عند الإقلاع.
    * القراءة: من خيط خادم الصحة لكل طلب — بلا أي await على الحلقة الرئيسية
      ولا DB في مسار الطلب (عقد liveness المعزول).
    * قفل واحد + نسخ قاموس صغيرة — تأثير صفر على المسار الساخن.
    """

    __slots__ = ("_lock", "_data")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {
            "boot_complete": False,
            "db_down": None,
            "db_healthy": None,
            "payload": None,
            "payload_ts": 0.0,
        }

    def set_boot_complete(self) -> None:
        try:
            with self._lock:
                self._data["boot_complete"] = True
        except Exception:
            pass

    def update_payload(
        self,
        *,
        payload: Dict[str, Any],
        db_down: bool,
        db_healthy: bool,
    ) -> None:
        try:
            with self._lock:
                self._data["payload"] = payload
                self._data["db_down"] = bool(db_down)
                self._data["db_healthy"] = bool(db_healthy)
                self._data["payload_ts"] = time.time()
        except Exception:
            pass

    def get(self) -> Dict[str, Any]:
        try:
            with self._lock:
                return dict(self._data)
        except Exception:
            return {"boot_complete": False, "payload": None}


# =============================================================================
# v11.5.3: اختيار حساب لاعتماديات عميل أزرار التنبيهات (AlertUI)
# الأولوية: حساب is_main، ثم أول حساب متصل فعلاً له api_id+api_hash
# (نفس اختيار «Fallback main client»). None = لا اعتماديات → العميل
# يبقى معطلاً بأمان (fallback يعمل).
# v11.8.2 (F-21): كانت معرفة ثلاث مرات متطابقة — بقي تعريف واحد
# (التعريفان السابقان كانا كوداً ميتاً تُغطّيه الثالثة).
# =============================================================================
def _pick_alertui_account(accounts: Any, monitors: Any) -> Optional[dict]:
    acc = next((a for a in (accounts or []) if (a or {}).get("is_main")), None)
    if acc is not None:
        return acc
    for m in (monitors or []):
        a = getattr(m, "account", None) or {}
        if getattr(m, "is_connected", False) and a.get("api_id") and a.get("api_hash"):
            return a
    return None


def _parse_iso_utc(ts: Optional[str]) -> float:
    """v14.6.0: تحويل طابع ISO/UTC إلى epoch — فشل-آمن (0 عند أي خلل).

    يقبل صيغ "2026-11-18T11:09:19.611232Z" و"+00:00" وبدون ثوانٍ كسرية.
    """
    if not ts:
        return 0.0
    try:
        raw = str(ts).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


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

    async def wait_for_token(
        self, account_name: str = "default", max_wait_s: float = 35.0,
    ) -> bool:
        """v14.7.0: انتظار توكن متاح بدل الرفض الفوري.

        الرفض الفوري (can_proceed=False) كان يُسقط التنبيه صامتاً في
        المسار الأعلى — الآن يستدعي مسار الإرسال هذا الانتظار المحدود
        قبل التسليم لسلّم DLQ. الاستقصاء كل 0.75s يستهلك بلا أقفال
        (can_proceed نفسه هو البوابة الذرية). يعيد True عند حصول توكن.
        """
        deadline = time.monotonic() + max(0.0, float(max_wait_s))
        while True:
            try:
                if await self.can_proceed(account_name):
                    return True
            except Exception:
                return False
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.75)


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
        # v14.7.0: tracemalloc يعمل فقط عند الطلب الصريح (MEMORY_TRACEMALLOC=1).
        # تتبع كل تخصيصة يضيف بصمة RSS ثابتة وتكلفة CPU — وكان يعمل بلا
        # شرط في الإنتاج بينما الحد 480MB يُحاصر الخدمة. التشخيص يبقى
        # متاحاً بتشغيل المتغير ثم إعادة التشغيل.
        _tm_requested = os.getenv("MEMORY_TRACEMALLOC", "").strip().lower() in (
            "1", "true", "yes", "on",
        )
        if _tm_requested:
            try:
                tracemalloc.start()
                self._tracemalloc_enabled = True
                self._last_snapshot = tracemalloc.take_snapshot()
            except Exception as e:
                logger.warning(f"tracemalloc unavailable, leak diagnostics limited: {e}")
                self._tracemalloc_enabled = False
        else:
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


# ══ v15.6.0: لقطة البوابة السرعة الآمنة لـ/health/full ══
def _fast_gate_health_snapshot() -> Dict[str, Any]:
    try:
        from fast_gate import health_snapshot
        return health_snapshot()
    except Exception:
        return {"enabled": False, "engine": "snapshot_error"}


# =============================================================================
# Main Bot Class v14.5.1 (hardened)
# =============================================================================
# v14.9.1: دورة حلقة مراقبة عضوية بوت التنبيهات — ثابت وحدة لقابلية الاختبار
_ALERTBOT_WATCHDOG_CYCLE_S: float = 60.0


class EnhancedTelegramBot:
    def __init__(self) -> None:
        self.db = EnhancedDatabase()
        self.filter = EnhancedFilter()
        # ══ v15.6.0: البوابة السرعة — رفض فوري مكافئ لـno_keyword ══
        # بناء الأتمتة من حالة المحرك الحية (مصادر القبول + متغيرات
        # fuzzy). فشل البناء = بوابة معطلة = كل شيء يتحلل كاملاً كما
        # v15.5.0 — صفر مخاطر سلوكية.
        try:
            from fast_gate import ensure_fast_gate
            ensure_fast_gate(self.filter)
        except Exception as _fg_err:
            logger.warning(f"FastGate init skipped (fail-open): {type(_fg_err).__name__}: {_fg_err}")
        # v15.4.0: دفعة ابتدائية ALERT_BURST_SIZE توكن (افتراضي 20) — انفجار
        # الرسائل المتزامن يُرسل فوراً بدل تقطير 1/ثانية من البداية.
        self.rate_limiter = AdaptiveRateLimiter(
            CFG.MAX_ALERTS_PER_MINUTE,
            CFG.MAX_ALERTS_PER_HOUR,
            burst_size=max(1, int(getattr(CFG, "ALERT_BURST_SIZE", 20))),
        )
        self.memory_monitor = MemoryMonitor()
        self.main_client: Optional[TelegramClient] = None
        # v10.2: المُرسِل اللاصق — آخر عميل نجح فعلياً في إرسال تنبيه للقناة
        # الهدف. _resolve_send_clients تفضّله أولاً فيظهر الإرسال مستقراً
        # حتى لو كان main_client حساباً بلا صلاحية نشر في القناة.
        self.alert_sender_client: Optional[TelegramClient] = None
        # ══ v10.8: بوت التنبيهات (اسم البوت: @alzariqi711r_bot) ══
        # إرسال التنبيهات إلى TARGET_GROUP_ID عبر Bot API بأزرار
        # [المرسل][جروب] في صف واحد. التوكن اختياري: بدونه (أو مع أي فشل)
        # يعمل مسار حسابات المستخدمين (fallback) بنفس نص التنبيه بدون
        # أزرار كما في v10.7 — بلا أعطال.
        self.alert_bot = AlertBot(
            # v11.0: BOT_TOKEN اسم بديل مقبول (نفس توكن @alzariqi711r_bot)
            token=(getattr(CFG, "ALERT_BOT_TOKEN", None) or os.getenv("BOT_TOKEN")),
            chat_id=getattr(CFG, "TARGET_GROUP_ID", 0),
            timeout=getattr(CFG, "ALERT_BOT_TIMEOUT", 12.0),
            max_retries=getattr(CFG, "ALERT_BOT_MAX_RETRIES", 3),
        )
        # v11.0: عميل بوت التنبيهات الحي (alert_ui.AlertBotClient) —
        # يستقبل CallbackQuery فعلياً ويعمل طبقة إرسال احتياطية بالأزرار.
        # يُشغَّل في initialize() بعد نجاح فحص AlertBot، ويُربط بـalert_bot
        # عبر set_client() ليكتمل سلّم الإرسال:
        #   Bot API ← عميل البوت الحي (أزرار) ← حسابات المستخدمين (نص).
        self.alert_ui_client: Optional[AlertBotClient] = None
        self.monitors: List[EnhancedAccountMonitor] = []
        self.is_running = False
        self._start_time = time.monotonic()
        self.health = HealthMonitor(self)
        # v15.2.0: كاش صفوف حسابات اللوحة للإقلاع (قراءة واحدة عند boot)
        self._panel_rows_cache: Optional[List[Dict[str, Any]]] = None
        # v16.2.0: حسابات env المستبعدة نهائياً من اللوحة (مفاتيح أرقام بلا +)
        # تُحمّل من app_settings عند الإقلاع — الاستبعاد يدوم عبر النشرات.
        self._excluded_env_phones: set = set()
        self._excluded_env_loaded: bool = False

        # ── Background task lifecycle (M-05 / M-16) ─────────────────────
        # Every task this module creates is tracked here so shutdown can
        # cancel+await all of them uniformly, and so a task's exception is
        # never silently lost ("Task exception was never retrieved").
        self._background_tasks: Set[asyncio.Task] = set()
        self._shutdown_event = asyncio.Event()
        # v15.1.0: مرجع الحلقة الرئيسية — الجسر الذي تمر عبره كل عمليات
        # اللوحة الثقيلة (DB/Telethon/المحرك) من خيط الصحة المعزول.
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None

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
        # v14.6.1: لقطة الصحة المشتركة + خيط الخادم المستقل
        self._health_snapshot = _HealthSnapshot()
        self._keep_alive_task: Optional[asyncio.Task] = None
        self._main_client_watchdog_task: Optional[asyncio.Task] = None
        # ═══ v14.6.0 Activity Maintenance System ═══
        self.activity: Optional[ActivityWatchdog] = None
        self._activity_task: Optional[asyncio.Task] = None
        # عدّاد إعادة التشغيل (يُثبَّت في app_settings عند الإقلاع)
        self._boot_count: int = 0
        self._prev_boot_ts: Optional[str] = None
        # v14.2.1 (wake-stability): يدل على اكتمال تهيئة البوت (DB متصل +
        # الصيانة الإقلاعية أُنجزت). قبل اكتماله يُجيب /health بـ 200
        # {"status": "starting"} بدلاً من 500/انعدام الاستجابة — فيستقبل
        # Render استيقاظ الخدمة (wake) بسرعة بدل انتهاء مهلة التوجيه
        # (hibernate-wake-error 503). بعد الاكتمال يعود العقد الأصلي
        # (503 عند سقوط DB) بلا تغيير.
        self._boot_complete: bool = False
        # ══ v14.9.1: Buttons Availability Heal (عضوية بوت التنبيهات) ══
        # زرا «مراسلة» و«عرض الرسالة» يعملان عبر Bot API حصراً — فقدان
        # عضوية البوت في قناة الهدف = تنبيهات بلا أزرار (حادثة 21-11-2026).
        self._alertbot_heal_last_ts: float = 0.0
        self._alertbot_heal_attempts: int = 0
        self._alertbot_warn_last_ts: float = 0.0
        self._alertbot_membership_task: Optional[asyncio.Task] = None

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
        # v15.7.1: مرجع على self لأجل حذف الطابور من لوحة التحكم — خط
        # واحد بلا أي تغيير سلوكي على المنتج/المستهلك.
        self._internal_queue = internal_queue

        # v15.5.0: منتج دفعي — pop_batch_from_queue يسحب حتى PRODUCER_POP_BATCH
        # صف في SELECT/UPDATE/COMMIT واحدة (كان صفًّا واحدًا + commit لكل رسالة
        # تحت القفل العام — تنافس 12 مهمة على SQLite). دلالة M-07 محفوظة:
        # أي إلغاء/خطأ بين السحبة والإدخال يعيد الأحداث غير المُدخلة إلى DB
        # بأولوية 9 (at-least-once كما سابقاً).
        async def producer():
            batch_n = max(1, int(getattr(CFG, "PRODUCER_POP_BATCH", 16) or 16))
            pending: list = []
            while self.is_running:
                try:
                    if not pending:
                        pending = await self.db.pop_batch_from_queue(batch_n)
                        if not pending:
                            # v10.4: event-driven wakeup — نفس عقد النسخة
                            # التفردية السابقة (انتظار إشارة add_to_queue مع
                            # fallback 0.1s للحفاظ على الإيقاع القديم).
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
                            continue
                    item = pending.pop(0)
                    try:
                        await internal_queue.put(item)
                    except asyncio.CancelledError:
                        pending.insert(0, item)  # يعاد عبر معالج الإلغاء أدناه
                        raise
                except asyncio.CancelledError:
                    # v9.12 (audit M-07): نافذة الإيقاف — كل حدث سُحب من DB
                    # ولم يُسلَّم لطابور العمال يُعاد إلى DB (أولوية 9).
                    if pending:
                        _re = 0
                        for _item in pending:
                            try:
                                await self.db.add_to_queue(_item, priority=9)
                                _re += 1
                            except Exception as e:
                                logger.error(f"Producer shutdown: failed to re-enqueue in-flight event: {e}")
                        if _re:
                            logger.info(f"Producer shutdown: re-enqueued {_re} in-flight events to DB (M-07)")
                    break
                except Exception as e:
                    logger.error(f"Producer error: {e}")
                    if pending:
                        for _item in pending:
                            try:
                                await self.db.add_to_queue(_item, priority=9)
                            except Exception:
                                pass
                        pending = []
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
                    # ══ v12.1.0 التزامن الصلب: الحماة (ack) ══
                    # ══ v15.8.0: ack دفعي — بلا commit لكل حدث ══
                    # المعالجة اكتملت أو حُوّلت لـDLQ (المسؤولية انتقلت):
                    # المعرف يدخل مخزن الذاكرة ومهمة acker تحذفها دفعات
                    # (executemany + commit واحد). لو تحطمت العملية قبل
                    # التصفير: الصف يُسترد عبر recover_stale_inflight
                    # (at-least-once كما سابقاً) وطبقة dedup تمنع تكرار
                    # التنبيه. فشل الدفع آمن: الصف يبقى ويُعاد فحصه دورياً.
                    try:
                        _qid = event_data.get("_queue_id")
                        if _qid is not None:
                            self._pending_acks.append(int(_qid))
                            self._ack_wake.set()
                    except Exception as _ack_err:
                        logger.debug(f"ack buffer push failed (fail-safe): {type(_ack_err).__name__}")
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

        # v15.5.0: منتج واحد دفعي يكفي — PRODUCER_WORKERS × 1-row pops كان
        # سباق قفل بلا فائدة؛ الدفعة الواحدة تُغني عن التعديد. تُترك القيمة
        # للتوافق الخلفي لكن السحبة الدفعية هي المسار الفعلي الآن.
        _producer_n = max(1, int(getattr(CFG, "PRODUCER_WORKERS", 4)))
        producers = [
            asyncio.create_task(producer(), name=f"producer_{i}")
            for i in range(1)  # v15.5.0: دفعة واحدة تسحب 16 صفًّا — تعديد المنتجين يضاعف تنافس القفل فقط
        ]
        workers = [asyncio.create_task(worker(i), name=f"worker_{i}") for i in range(CFG.PROCESSING_WORKERS)]

        # ══ v15.8.0: مخزن الاكات الدفعي + مهمة التصفير ══
        # commit واحد لكل دفعة (≤512) بدل commit لكل حدث — الاختناق P0
        # المقاس (db_latency 913ms على SELECT 1 + P50 لاق 240 ثانية).
        # كل السياستَين في _ack_flush_ids — المهام ترسل معرفات فقط.
        self._pending_acks = deque()  # type: Deque[int]
        self._ack_wake = asyncio.Event()

        async def acker():
            while self.is_running:
                try:
                    await asyncio.wait_for(self._ack_wake.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                if self._pending_acks:
                    ids = []
                    while self._pending_acks and len(ids) < 512:
                        ids.append(self._pending_acks.popleft())
                    try:
                        await self._ack_flush_ids(ids)
                    except Exception as e:
                        logger.debug(f"acker flush skipped: {type(e).__name__}: {e}")
                if not self._pending_acks:
                    self._ack_wake.clear()

        acker_task = asyncio.create_task(acker(), name="ack_flusher")

        try:
            await asyncio.gather(*producers, *workers, acker_task, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        finally:
            # تصفير أخير عند الإيقاف — الأفضل جهد؛ ما بقي يُسترد لاحقاً
            # (recover_stale_inflight) بلا تكرار تنبيهات (طبقة dedup)
            try:
                remaining = []
                while self._pending_acks and len(remaining) < 512:
                    remaining.append(self._pending_acks.popleft())
                if remaining:
                    await self._ack_flush_ids(remaining)
            except Exception:
                pass

    async def _ack_flush_ids(self, ids) -> int:
        """v15.8.0: تصفير دفعة اكات عبر ack_queue_events_batch — فشل-آمن
        (الصفوف غير المحذوفة تبقى قابلة للاسترداد). تعيد عدد المحذوف."""
        deleted = 0
        try:
            deleted = int(await self.db.ack_queue_events_batch(list(ids)))
        except Exception as e:
            logger.debug(f"ack batch flush failed (rows stay recoverable): {type(e).__name__}: {e}")
        return deleted

    # ─── v15.5.0 متحكم تخفيف الحمل (Load-Shedding / Drain Mode) ───────────────
    async def _load_shed_loop(self) -> None:
        """مراقبة عمق الطابور كل 10 ثوانٍ — عند التشبع:

        * fuzzy يُعطّل مؤقتاً (مطابقة exact/regex فقط) — أثقل بند CPU في
          فلتر الـ24k نمط على 0.1 CPU من Render المجاني.
        * drain_mode=True — خط المعالجة يتوقف عن إثراء المرسل عبر
          تيليجرام (sender_resolve_failure=27 / success=0 — خسارة صافية
          تحت الحمل).

        تحت حد التعافي (hysteresis) يعود كل شيء تلقائياً. هذا يحفظ
        المعدل: أفضل رسالة متأخرة 30 ثانية بلا fuzzy من صفر تنبيهات.
        فشل-آمن بالكامل: أي استثناء → المحاولة بعد 10 ثوانٍ.
        """
        self.drain_mode = False
        self._fuzzy_degraded = False
        while self.is_running:
            try:
                try:
                    depth = int(await self.db.queue_size())
                except Exception:
                    depth = 0
                degrade_at = int(getattr(CFG, "FUZZY_DEGRADE_QUEUE_DEPTH", 200) or 0)
                recover_at = int(getattr(CFG, "FUZZY_RECOVER_QUEUE_DEPTH", 100) or 0)
                # v15.5.0-r1: fuzzy أبعد أثراً من resolver — تعطيله يفوّت
                # مطابقات قريبة (المطابق الأساسي exact/regex يعمل بدونه لكن
                # الاحتياطي الضبابي يلتقط متغيرات الكلمات). السبب الجذري
                # أثبت أنه FIFO + قفل DB لا CPU — لذا fuzzy يُعطّل فقط عند
                # حافة الاختناق (85% من صمام القبول) حيث كل ثانية CPU حاسمة،
                # بينما resolver يُتخطى مبكراً (200) — خسارة صافية دائمة
                # تحت الحمل (27 فشلاً / صفر نجاح).
                try:
                    _adm = int(getattr(CFG, "QUEUE_ADMISSION_MAX", 400) or 0)
                except Exception:
                    _adm = 400
                fuzzy_at = int(_adm * 0.85) if _adm > 0 else degrade_at
                if fuzzy_at <= degrade_at:
                    fuzzy_at = degrade_at
                fuzzy_recover = max(recover_at, int(fuzzy_at * 0.7))

                if degrade_at > 0 and depth >= degrade_at and not self.drain_mode:
                    self.drain_mode = True
                    logger.warning(
                        f"🛡️ LOAD-SHED ON | queue={depth} >= {degrade_at} — "
                        f"sender-resolver skipped (alerts first) | fuzzy gate at {fuzzy_at}"
                    )
                elif self.drain_mode and depth <= recover_at:
                    self.drain_mode = False
                    logger.info(
                        f"🟢 LOAD-SHED OFF | queue={depth} <= {recover_at} — "
                        f"full pipeline restored"
                    )

                # بوابة fuzzy منفصلة مع hysteresis خاص بها (حافة الاختناق فقط)
                if fuzzy_at > 0 and depth >= fuzzy_at and not self._fuzzy_degraded:
                    _flt = getattr(self, "filter", None)
                    if _flt is not None and getattr(_flt, "_fuzzy_enabled", False):
                        try:
                            _flt._fuzzy_enabled = False
                            self._fuzzy_degraded = True
                            from unified_alerts.stats import get_unified_stats as _gus
                            _gus().inc("fuzzy_degrade_on")
                            logger.warning(
                                f"🛡️ FUZZY DEGRADED | queue={depth} >= {fuzzy_at} — "
                                f"exact/regex only (brink shedding)"
                            )
                        except Exception:
                            pass
                elif self._fuzzy_degraded and depth <= fuzzy_recover:
                    _flt = getattr(self, "filter", None)
                    if _flt is not None:
                        try:
                            _flt._fuzzy_enabled = bool(getattr(CFG, "FUZZY_MATCHING_ENABLED", True))
                            self._fuzzy_degraded = False
                            from unified_alerts.stats import get_unified_stats as _gus
                            _gus().inc("fuzzy_degrade_off")
                            logger.info(
                                f"🟢 FUZZY RESTORED | queue={depth} <= {fuzzy_recover}"
                            )
                        except Exception:
                            pass

                # ══ v15.6.0: السقف التكيفي لانتظار الطابور ══
                # تحت الضغط: الأحداث الطازجة التي تجاوزت MAX_QUEUE_WAIT_SECONDS
                # تُسلّم متأخرة بدل إسقاطها (متأخر دقائق أفضل من أبدًا —
                # فلسفة المالك). ثلاثة مستويات مع نطاق فتورة (hysteresis):
                #   drain_mode / ≥ fuzzy_at  → السقف × المضاعف (حتى 1800s)
                #   ≥ 50% من صمام القبول      → السقف × 2
                #   ≤ 35% من صمام القبول      → عودة للسقف القياسي (CFG)
                try:
                    if bool(getattr(CFG, "ADAPTIVE_QUEUE_WAIT_ENABLED", True)):
                        _gate_obj = get_alert_gate()
                        _base_wait = int(getattr(CFG, "MAX_QUEUE_WAIT_SECONDS", 600) or 600)
                        _w_mult = max(1, int(getattr(CFG, "ADAPTIVE_QUEUE_WAIT_MULTIPLIER", 3)))
                        _w_max = int(getattr(CFG, "ADAPTIVE_QUEUE_WAIT_MAX_SECONDS", 1800) or 1800)
                        _adm_v = _adm if _adm > 0 else 400
                        if self.drain_mode or depth >= fuzzy_at:
                            _want_ceil: Optional[int] = min(
                                _w_max, max(_base_wait, _base_wait * _w_mult)
                            )
                        elif depth >= int(_adm_v * 0.5):
                            _want_ceil = min(_w_max, max(_base_wait, _base_wait * 2))
                        elif depth <= int(_adm_v * 0.35):
                            _want_ceil = None
                        else:
                            _want_ceil = _gate_obj._adaptive_wait_ceiling  # نطاق الفتورة
                        _cur_ceil = _gate_obj._adaptive_wait_ceiling
                        if _want_ceil != _cur_ceil:
                            _gate_obj.set_adaptive_wait_ceiling(_want_ceil)
                            logger.warning(
                                f"⏱️ ADAPTIVE QUEUE-WAIT CEILING → "
                                f"{_want_ceil if _want_ceil else f'default({_base_wait}s)'}"
                                f" | queue={depth} | admission={_adm_v}"
                            )
                    else:
                        _gate_obj = get_alert_gate()
                        if _gate_obj._adaptive_wait_ceiling is not None:
                            _gate_obj.set_adaptive_wait_ceiling(None)
                except Exception as _aqw_err:
                    logger.debug(f"adaptive ceiling skipped (fail-safe): {type(_aqw_err).__name__}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"load_shed_loop error (fail-safe): {type(e).__name__}: {e}")
            await asyncio.sleep(10)

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

    # ─── Health HTTP Server (v14.6.1 — thread مستقل + لقطة مخبأة) ───
    #
    # السبب الجذري لحلقة انهيار 12:30-12:47 (boots #2-#7، SIGTERM كل 2-5 دقائق):
    #   healthCheckPath=/health مفعّل على Render + خادم الصحة يعيش على نفس
    #   event loop الرئيسي. تحت الضغط (احتياطي طابور 700+ صف + محرك فلترة
    #   بـ24225 نمط regex على CPU مشترك) تنحسر الحلقة الرئيسية → طلبات
    #   /health لا تُجاب في مهلة Render → SIGTERM → إعادة تشغيل → إقلاع
    #   أثقل (استرداد in-flight + ترطيب) → تشبع أعلى → تكرار.
    #   نفس الجواب على سؤال المواصفة §ثانياً: «هل event loop يدخل في حالة
    #   حجب؟» — نعم، وكان يقتل الخدمة عبر فشل فحص الصحة.
    #
    # الإصلاح الجذري (فصل طبقات):
    #   1. خادم الصحة يعمل في THREAD مستقل بحلقة asyncio خاصة — يجيب فوراً
    #      من لقطة ذاكرة حتى لو انحسرت الحلقة الرئيسية ثوانياً.
    #   2. اللقطة (snapshot) تُحدَّث دورياً من الحلقة الرئيسية كل 10s —
    #      طلبات الصحة لا تلمس DB إطلاقاً (ضغط DB أقل أيضاً).
    #   3. /health = liveness صرف: 200 ما دامت العملية تجيب؛ حالة DB تظهر
    #      في الجسم (degraded) بلا 503 — إعادة تشغيل Render لا تشفي DB،
    #      وdatabase.py يشفي اتصاله ذاتياً. /health/full = نفس اللقطة
    #      الكاملة (حداثة ≤10s مقبولة لتشخيصات، وفشل الحلقة لا يُسقطها).
    #

    async def _build_full_payload(self) -> Dict[str, Any]:
        """بناء حمولة /health/full كاملة — تُستدعى من حلقة اللقطة (كل 10s).

        نفس منطق المسار القديم حرفياً (v14.5.5-diag + v14.6.0) — لكنه يُنفَّذ
        دورياً في الخلفية بدل لكل طلب: ضغط DB أقل وحداثة ≤10s للعرض.
        """
        health = await self.health.check()
        db_down = not (self.db and getattr(self.db, "is_connected", False))
        payload: Dict[str, Any] = {
            "status": "ok" if health.is_healthy else "degraded",
            "checks": health.checks,
            "uptime": int(time.monotonic() - self._start_time),
            "queue_size": await self.db.queue_size(),
            "db_healthy": self.db.db_healthy,
            "monitors": sum(1 for m in self.monitors if m.is_connected),
            "fast_capture": get_capture_snapshot(),
            "sender_intel": get_sender_intel_snapshot(),
            "mention_intel": get_mention_intel_snapshot(),
            "sender_contact": get_contact_metrics_snapshot(),  # v11.2 مراسلة المرسل
            "alert_latency": alert_latency.snapshot(),
            "dedup": get_dedup_snapshot(),
            "unified_gate": unified_snapshot(),  # v12.0 unified_alerts
            # v15.5.0: حالة تخفيف الحمل الحية للقراءة في /health/full
            "load_shed": {
                "drain_mode": bool(getattr(self, "drain_mode", False)),
                "fuzzy_degraded": bool(getattr(self, "_fuzzy_degraded", False)),
                "degrade_at": int(getattr(CFG, "FUZZY_DEGRADE_QUEUE_DEPTH", 200) or 0),
                "recover_at": int(getattr(CFG, "FUZZY_RECOVER_QUEUE_DEPTH", 100) or 0),
                "admission_max": int(getattr(CFG, "QUEUE_ADMISSION_MAX", 400) or 0),
                "fresh_priority": int(getattr(CFG, "QUEUE_FRESH_PRIORITY", 8) or 5),
            },
            # ══ v15.6.0: البوابة السرعة + التقاط البوتات ══
            "fast_gate": _fast_gate_health_snapshot(),
            "capture_from_bots": bool(getattr(CFG, "CAPTURE_FROM_BOTS", True)),
            "bot_messages_captured": sum(
                int(getattr(m, "_stats", {}).get("bot_messages_captured", 0) or 0)
                for m in getattr(self, "monitors", [])
            ),
            "antispam": get_antispam_snapshot(),
            # ══ v14.9.1: حالة أزرار التنبيهات (عضوية بوت التنبيهات) ══
            "alert_bot": (
                self.alert_bot.status_snapshot()
                if getattr(self, "alert_bot", None) is not None else None
            ),
            # v14.6.1: حقل db_down صريح — المستهلكون (لوحات/سكربتات) يقرؤونه
            # من اللقطة بدل استنتاج من كود HTTP.
            "db_down": bool(db_down),
        }
        # ═══ v14.8.0 (P7): db_latency_ms — زمن SELECT 1 الفعلي ═══
        # يُقاس هنا (حلقة اللقطة كل 10s على الحلقة الرئيسية) لا في مسار الطلب —
        # عقد v14.6.1: خيط خادم الصحة لا يلمس DB إطلاقاً. None = فشل القياس
        # أو الاتصال (يُقرأ مع db_healthy/db_down للتفسير).
        try:
            _t0 = time.perf_counter()
            await self.db._ping()
            payload["db_latency_ms"] = round((time.perf_counter() - _t0) * 1000, 2)
        except Exception:
            payload["db_latency_ms"] = None
        # ═══ v14.5.5-diag: تشخيصات الاستقرار الذاتي — كل قسم فشل-آمن ═══
        try:
            import psutil as _psu
            payload["rss_mb"] = round(
                _psu.Process(os.getpid()).memory_info().rss / (1024 * 1024), 1
            )
        except Exception:
            payload["rss_mb"] = None
        try:
            from memory_guard import snapshot as _mg_snap
            payload["mem_guard"] = _mg_snap()
        except Exception:
            payload["mem_guard"] = None
        try:
            rows = await self.db._fetchall(
                "SELECT component, status, details, timestamp FROM system_health "
                "WHERE component IN ('memory', 'process') "
                "ORDER BY id DESC LIMIT 5"
            )
            payload["system_health"] = [
                {
                    "c": r.get("component"),
                    "s": r.get("status"),
                    "d": str(r.get("details") or "")[:200],
                    "t": r.get("timestamp"),
                }
                for r in (rows or [])
            ]
        except Exception:
            payload["system_health"] = None
        # ═══ v14.5.5-diag2 + v14.6.0: تفاصيل المراقبين + آخر الأحداث ═══
        try:
            detail = []
            for m in self.monitors:
                acct = getattr(m, "account", None)
                name = acct.get("name", "?") if isinstance(acct, dict) else "?"
                flood_until = float(getattr(m, "_connect_flood_until", 0) or 0)
                last_ev_ts = float(getattr(m, "last_event_ts", 0) or 0)
                rm = getattr(m, "_reconnect", None)
                rm_snap = None
                if rm is not None:
                    try:
                        rm_snap = rm.snapshot()
                    except Exception:
                        rm_snap = None
                detail.append({
                    "name": name,
                    # v17.1.0: هوية قابلة للمطابقة لتسوية DB↔المحرك (صفوف اللوحة)
                    "row_id": (int(acct.get("row_id") or 0) or None) if isinstance(acct, dict) else None,
                    "phone": (str(acct.get("phone") or "") or None) if isinstance(acct, dict) else None,
                    "source": (str(acct.get("source") or "") or None) if isinstance(acct, dict) else None,
                    "connected": bool(getattr(m, "is_connected", False)),
                    "last_error": str(getattr(m, "_last_connect_error", "") or "")[:180],
                    "flood_wait_s": round(flood_until - time.time()) if flood_until > time.time() else None,
                    # v14.6.0: آخر نشاط استقبال حقيقي (بلا محتوى الرسائل — هويات فقط)
                    "last_event_age_s": round(time.time() - last_ev_ts, 1) if last_ev_ts else None,
                    "last_event_chat_id": int(getattr(m, "last_event_chat_id", 0) or 0) or None,
                    "last_event_sender_id": int(getattr(m, "last_event_sender_id", 0) or 0) or None,
                    "events_received": int(getattr(m, "events_received", 0) or 0),
                    "last_connect_success_age_s": (
                        round(time.time() - float(getattr(m, "last_connect_success_ts", 0) or 0), 1)
                        if getattr(m, "last_connect_success_ts", 0) else None
                    ),
                    # v14.7.0: تمييز «يُعاد المحاولة» عن «حالة نهائية تتطلب
                    # إعادة دخول يدوية» — الحساب الميت بجلسة ملغاة لم يعد
                    # يبدو وكأنه في حلقة إعادة اتصال عادية.
                    "needs_relogin": bool(getattr(m, "_terminal_error", None) is not None),
                    "terminal_error": (
                        str(getattr(m, "_terminal_error", None) or "")[:120] or None
                    ),
                    "reconnect": rm_snap,
                })
            payload["monitor_detail"] = detail
        except Exception:
            payload["monitor_detail"] = None
        try:
            mc = getattr(self, "main_client", None)
            payload["main_client_connected"] = bool(mc.is_connected()) if mc is not None else None
        except Exception:
            payload["main_client_connected"] = None
        # ═══ v14.6.0 Activity Maintenance: النشاط + الحسابات + إعادة التشغيل ═══
        try:
            payload["accounts_loaded"] = len(self.monitors)
        except Exception:
            payload["accounts_loaded"] = None
        try:
            act = getattr(self, "activity", None)
            payload["activity"] = act.snapshot() if act is not None else {"enabled": False}
        except Exception:
            payload["activity"] = {"enabled": False}
        try:
            prev_ts = getattr(self, "_prev_boot_ts", None)
            payload["restarts"] = {
                "boot_count": int(getattr(self, "_boot_count", 0) or 0),
                "prev_boot_ts": prev_ts,
                "prev_boot_age_s": (
                    round(max(0.0, time.time() - _parse_iso_utc(prev_ts)), 1)
                    if prev_ts else None
                ),
                "version": BOT_VERSION,
            }
        except Exception:
            payload["restarts"] = None
        return payload

    async def _health_snapshot_loop(self) -> None:
        """v14.6.1: يبني اللقطة الكاملة كل 10s ويكتبها للخيط المستقل.

        كل القراءات من الذاكرة/العدادات الجاهزة + استعلامين DB خفيفين
        (queue_size + system_health) كل 10s — أقل من معدل استطلاع Render
        السابق على نفس الطلبات. فشل الدورة لا يوقف الحلقة (فشل-آمن).
        تعمل حتى الإلغاء (وليس على is_running — تُبدأ أثناء initialize
        قبل أن يصبح is_running=True في run()).
        """
        interval = max(5, int(os.getenv("HEALTH_SNAPSHOT_INTERVAL_SECONDS", "10")))
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            try:
                if not getattr(self, "_boot_complete", False):
                    continue
                payload = await self._build_full_payload()
                db_down = bool(payload.get("db_down"))
                db_healthy = bool(payload.get("db_healthy", True))
                self._health_snapshot.update_payload(
                    payload=payload,
                    db_down=db_down,
                    db_healthy=db_healthy,
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"health snapshot update error (fail-safe): {type(e).__name__}: {e}")

    def _start_health_server_thread(self) -> None:
        """v14.6.1: خادم الصحة في thread مستقل بحلقة خاصة — liveness معزول.

        المعالجات تقرأ اللقطة من الذاكرة حصراً: صفر awaits على الحلقة
        الرئيسية، صفر DB في مسار الطلب. حتى لو انحسرت الحلقة الرئيسية
        ثوانياً (تشبع CPU) تبقى فحوصات Render تُجاب فوراً — حلقة الانهيار
        (فحص فاشل → SIGTERM → إعادة) مكسورة من جذورها.
        """
        if not AIOHTTP_AVAILABLE:
            logger.warning("Aiohttp not installed. Health server disabled.")
            return

        snapshot = self._health_snapshot

        def _thread_main() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _run() -> None:
                app = web.Application()

                async def health_handler(request):
                    # v14.6.1 liveness: 200 ما دامت العملية تجيب — حالة DB
                    # في الجسم (degraded) بلا 503 (إعادة تشغيل Render لا تشفي
                    # DB؛ database.py يشفي اتصاله ذاتياً).
                    snap = snapshot.get()
                    if not snap.get("boot_complete"):
                        return web.json_response({
                            "status": "starting",
                            "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        }, status=200)
                    db_down = bool(snap.get("db_down"))
                    db_healthy = bool(snap.get("db_healthy", True))
                    payload = {
                        "status": "ok" if (not db_down and db_healthy) else "degraded",
                        "database": "down" if db_down else "ok",
                        "db_healthy": db_healthy,
                        "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                    return web.json_response(payload, status=200)

                async def health_full_handler(request):
                    # v14.6.1: نفس المسار — يقرأ اللقطة (حداثة ≤10s) فوراً.
                    token = os.getenv("HEALTH_FULL_TOKEN", "")
                    if token:
                        supplied = (request.headers.get("Authorization", "")
                                    .replace("Bearer ", "").strip()) or request.query.get("token", "")
                        if supplied != token:
                            return web.json_response({"error": "unauthorized"}, status=401)
                    snap = snapshot.get()
                    if not snap.get("boot_complete"):
                        return web.json_response({
                            "status": "starting",
                            "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        }, status=200)
                    payload = snap.get("payload")
                    if not isinstance(payload, dict):
                        return web.json_response({
                            "status": "starting",
                            "note": "snapshot warming",
                            "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        }, status=200)
                    return web.json_response(payload)

                app.router.add_get('/health', health_handler)
                app.router.add_get('/health/full', health_full_handler)

                # v15.1.0: لوحة التحكم الخفيفة على نفس المنفذ والنفس التطبيق
                # — فشل-آمن: أي خلل في تركيب اللوحة لا يمنع /health أبداً.
                if DASHBOARD_AVAILABLE and getattr(CFG, "DASHBOARD_ENABLED", True):
                    try:
                        _setup_dashboard(app, self)
                        logger.info("Dashboard mounted at /admin (lightweight, auth-gated, bridge-to-main-loop)")
                    except Exception as _dash_err:
                        logger.error(
                            f"Dashboard setup failed (fail-safe, /health unaffected): "
                            f"{type(_dash_err).__name__}: {_dash_err}"
                        )

                # Render Web Service: MUST bind to $PORT (default 10000)
                port = int(os.getenv("PORT", 10000))
                runner = web.AppRunner(app)
                await runner.setup()

                # v14.2.1 (wake-stability): إعادة محاولة ربط المنفذ (3 محاولات × 2s).
                for _attempt in range(1, 4):
                    try:
                        site = web.TCPSite(runner, '0.0.0.0', port)
                        await site.start()
                        logger.info(f"Health server started on port {port} (dedicated thread, snapshot-based liveness)")
                        break
                    except OSError as e:
                        if _attempt >= 3:
                            # v13.2 fix (audit M-8): فشل الربط مرئي — Render
                            # يعيد تشغيل العملية ويُعاد المحاولة من إقلاع جديد.
                            logger.error(
                                f"Could not bind health server to port {port} "
                                f"after {_attempt} attempts: {e}"
                            )
                            await runner.cleanup()
                            return
                        logger.warning(
                            f"Port {port} busy (bind attempt {_attempt}/3): {e} — retrying in 2s"
                        )
                        await asyncio.sleep(2)

                # حلقة الـthread تبقى حية لخدمة الطلبات حتى إخراج العملية.
                await asyncio.Event().wait()

            try:
                loop.run_until_complete(_run())
            except Exception as e:
                logger.error(f"Health server thread crashed: {type(e).__name__}: {e}")

        t = threading.Thread(target=_thread_main, name="health_server", daemon=True)
        t.start()
        logger.info("Health server thread started (dedicated loop, snapshot-based)")

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
                    f"<b>📊 إحصائيات البوت v{BOT_VERSION} (Render)</b>\n\n"
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
                # v12.0 unified_alerts: purge دوري لصفوف الطابور المنتهية
                # (نفس سياسة الإقلاع — يمنع تراكم backlog منعّم قبل بوابة العمر)
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

    async def _inflight_recovery_loop(self) -> None:
        """══ v12.1.0 — حلقة الاسترداد الدورية (التزامن الصلب) ═══

        كل QUEUE_RECOVER_INTERVAL_SECONDS (افتراضي 120s): فحص صفوف
        in_flight العالقة (claimed_at أقدم من QUEUE_INFLIGHT_MAX_AGE_SECONDS
        = 600s) — عاملٌ انهار بين السحب (claim) والحماة (ack) كان يُفقد
        الحدث للأبد؛ الآن يُعاد للطابور بوسم _recovered ويعاد إرسال تنبيهه
        (at-least-once) أو يُودع dead_letters بصدق بعد تجاوز المحاولات.
        الإقلاع نفسه يستدعيها مرة أولى عبر run_startup_maintenance.
        فشل-آمن: أي خلل → محاولة لاحقة (لا استثناءات خارج الحلقة).
        """
        try:
            interval = max(30, int(getattr(CFG, "QUEUE_RECOVER_INTERVAL_SECONDS", 120) or 120))
        except Exception:
            interval = 120
        # أول فحص مبكر بعد الإقلاع (يلتقط عالق التشغيل السابق فوراً)
        await asyncio.sleep(min(interval, 15))
        while self.is_running:
            try:
                from unified_alerts.maintenance import recover_inflight_events
                await recover_inflight_events(self.db)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"inflight recovery loop error (fail-safe): {type(e).__name__}: {e}")
            # ══ v15.4.1: تطهير دوري سريع للطابور (كل دورة استرداد ≈ 120s) —
            # الصفوف الأقدم من QUEUE_STALE_PURGE_SECONDS (600s) محكوم عليها
            # بالبوابة أصلاً — تطهيرها يفرغ أي تراكم خلال دقائق بدل إهدار
            # العمال عليه ساعات (كان يومياً بعمر 7200s — غير كافٍ إطلاقاً).
            try:
                from unified_alerts.maintenance import purge_stale_queue_rows as _qpurge
                _n = await _qpurge(self.db)
                if _n:
                    logger.info(f"v15.4.1 periodic queue purge: {_n} stale rows removed (≤600s cadence)")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"periodic queue purge skipped (fail-safe): {type(e).__name__}: {e}")
            await asyncio.sleep(interval)

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

    async def _digest_loop(self) -> None:
        """v10.0 Precision: التقرير اليومي للإدارة.

        يفحص كل 10 دقائق؛ عندما تكون ساعة UTC الحالية == DIGEST_HOUR_UTC
        ولم يُرسل تقرير هذا اليوم بعد، يُرسل ملخصاً يومياً إلى الإدارة:
        قرارات الفلترة 24 ساعة (قبول/مراجعة/تجاهل/ثقة/المواد/الكلمات)،
        حواجز dedup والتشابه، وحالة الحسابات. فشل-آمن بالكامل.
        """
        try:
            if not getattr(CFG, "DIGEST_ENABLED", True):
                return
            last_sent_date: Optional[str] = None
            while self.is_running:
                try:
                    await asyncio.sleep(600)  # كل 10 دقائق
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
        """يبني التقرير اليومي ويرسله للإدارة — كل قسم مستقل فشل-آمن."""
        try:
            lines: List[str] = ["📊 <b>التقرير اليومي — ملخص الأداء (24 ساعة)</b>", ""]

            # 1) الإجماليات العامة + الحواجز
            # (v14.0: قسم «قرارات الفلترة» أُزيل مع جدول filter_decisions —
            #  كان يخدم معمل الفلترة في لوحة التحكم المحذوفة)
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

            # 3) حالة الحسابات
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

    # ═══════════ v14.6.0 Activity Maintenance System ═══════════
    def note_real_activity(self, account: str, chat_id: int, sender_id: int) -> None:
        """بوابة النشاط الحقيقي — يستدعيها معالج أحداث كل مراقب فور قبول حدث.

        تعيينات ذرّية بلا I/O — آمنة تماماً من المسار الساخن (لا تحجب
        event loop ولا تلمس DB). إذا لم يبدأ ActivityWatchdog بعد
        (نافذة الإقلاع) فالاستدعاء تجاهل صامت.
        """
        act = getattr(self, "activity", None)
        if act is not None:
            try:
                act.note_real_activity(account, chat_id, sender_id)
            except Exception:
                pass

    async def _persist_boot_counter(self) -> None:
        """v14.6.0: عدّاد إعادة التشغيل الصادق — app_settings['boot:last'].

        يُزاد عند كل إقلاع فعلي (بعد اتصال DB) ويعيد العدد الحالي + طابع
        الإقلاع السابق ليظهرا في /health/full (restarts). فشل-آمن كلياً:
        أي خلل DB لا يوقف الإقلاع ولا يرمي استثناء.
        """
        db = self.db
        if db is None:
            return
        try:
            import json as _json

            row = await db._fetchone(
                "SELECT value FROM app_settings WHERE key = ?", ("boot:last",)
            )
            prev_count = 0
            prev_ts: Optional[str] = None
            if row and row.get("value"):
                try:
                    prev = _json.loads(row["value"])
                    prev_count = int(prev.get("count", 0) or 0)
                    prev_ts = prev.get("ts")
                except Exception:
                    prev_count = 0
                    prev_ts = None
            self._boot_count = prev_count + 1
            self._prev_boot_ts = prev_ts
            payload = _json.dumps({
                "count": self._boot_count,
                "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "version": BOT_VERSION,
                "pid": os.getpid(),
            })
            await db._execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("boot:last", payload),
            )
            logger.info(f"Boot counter persisted: #{self._boot_count} (version={BOT_VERSION})")
        except Exception as e:
            logger.debug(f"boot counter persist skipped: {type(e).__name__}: {e}")

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
            # v14.0: لوحة التحكم أُزيلت نهائياً من المشروع (طلب المالك) —
            # الرد يبقى لتجنّب «الأمر الصامت» مع مستخدمي الأمر القديم.
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

    # ─── v14.10.0: إدارة حسابات اللوحة (ربط/تفعيل/إيقاف/إعادة اتصال) ─────────
    # كل هذه الدوال تعمل على الحلقة الرئيسية حصراً (تُستدعى من thread اللوحة
    # عبر run_coroutine_threadsafe). فك تشفير الجلسة يحدث هنا في الذاكرة فقط.

    async def spawn_panel_account(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """
        بناء مراقب لصف panel_accounts وتوصيله — تفعيل حساب جديد دون إعادة نشر.

        يُحدّث حالة الصف في DB: active عند النجاح، error/needs_relogin عند
        الفشل. المراقب يدخل تلقائياً في نظام توزيع المجموعات (تسجيل
        events.NewMessage على كل المصادر المراقبة وفق إعدادات النظام).
        """
        from dashboard.accounts import build_panel_account_dict

        row_id = int(row.get("id") or 0)
        phone_key = re.sub(r"\D", "", str(row.get("phone") or ""))
        if not row_id or not phone_key:
            return {"ok": False, "error": "صف حساب غير صالح"}

        # منع مراقب مكرر لنفس الرقم
        for m in self.monitors:
            acc_phone = re.sub(r"\D", "", str((getattr(m, "account", {}) or {}).get("phone") or ""))
            if acc_phone == phone_key:
                if getattr(m, "is_connected", False):
                    await self.db.update_panel_account(row_id, status="error", last_error="مكرر مع حساب متصل فعلاً")
                    return {"ok": False, "error": "هذا الحساب متصل فعلاً في النظام"}
                # مراقب قائم غير متصل لنفس الرقم → أزله ليُعاد بناؤه نظيفاً
                try:
                    await m.disconnect()
                except Exception:
                    pass
                try:
                    self.monitors.remove(m)
                except ValueError:
                    pass
                break

        account = build_panel_account_dict(row)
        if account is None:
            await self.db.update_panel_account(
                row_id, status="needs_relogin",
                last_error="تعذر فك تشفير الجلسة — أعد ربط الحساب",
            )
            return {"ok": False, "error": "تعذر فك تشفير الجلسة — أعد ربط الحساب"}

        mon = EnhancedAccountMonitor(account, self.db, self.filter, self.main_client)
        mon.set_bot(self)
        self.monitors.append(mon)
        ok = False
        try:
            ok = await asyncio.wait_for(mon.connect(), timeout=120)
        except asyncio.TimeoutError:
            ok = False
        except Exception as e:
            logger.error(f"spawn_panel_account connect error: {type(e).__name__}: {e}")
            ok = False

        if ok:
            await self.db.update_panel_account(row_id, status="active", last_error=None)
            await self.db.update_panel_account(
                row_id, username=account.get("username"), display_name=account.get("name"),
                tg_user_id=account.get("tg_user_id"),
            )
            if self.main_client is None:
                self.main_client = mon.client
                logger.info(f"Main client set from panel account: {account['name']}")
            logger.info(f"Panel account activated: {account['name']} (row {row_id})")
            return {"ok": True, "connected": True, "row_id": row_id}

        terminal = getattr(mon, "_terminal_error", None)
        err_text = getattr(mon, "_last_connect_error", None) or "فشل الاتصال"
        status = "needs_relogin" if terminal else "error"
        await self.db.update_panel_account(row_id, status=status, last_error=str(err_text)[:400])
        return {"ok": False, "connected": False, "error": str(err_text)[:200], "needs_relogin": bool(terminal)}

    async def stop_panel_account(self, row_id: int) -> bool:
        """إيقاف مراقبة حساب لوحة: قطع المراقب وإزالته من القائمة (حفظ الصف)."""
        phone = None
        try:
            row = await self.db.get_panel_account(row_id)
            phone = (row or {}).get("phone")
        except Exception:
            pass
        phone_key = re.sub(r"\D", "", str(phone or ""))
        for m in list(self.monitors):
            acc_phone = re.sub(r"\D", "", str((getattr(m, "account", {}) or {}).get("phone") or ""))
            if phone_key and acc_phone == phone_key:
                try:
                    await m.disconnect()
                except Exception:
                    pass
                try:
                    self.monitors.remove(m)
                except ValueError:
                    pass
                if self.main_client is m.client:
                    self.main_client = None
                    for mm in self.monitors:
                        if getattr(mm, "is_connected", False):
                            self.main_client = mm.client
                            break
                logger.info(f"Panel account monitoring stopped (row {row_id})")
                return True
        return False

    async def delete_panel_account(self, row_id: int) -> Dict[str, Any]:
        """
        حذف حساب لوحة نهائياً (الصف + الجلسة المشفّرة) — مع حماية
        «آخر حساب نشط»: يُرفض الحذف إذا كان الهدف هو الوحيد المتصل.
        """
        row = await self.db.get_panel_account(int(row_id))
        if not row:
            return {"ok": False, "error": "الحساب غير موجود"}
        phone_key = re.sub(r"\D", "", str(row.get("phone") or ""))
        target = None
        active_count = 0
        for m in self.monitors:
            acc_phone = re.sub(r"\D", "", str((getattr(m, "account", {}) or {}).get("phone") or ""))
            if getattr(m, "is_connected", False):
                active_count += 1
            if phone_key and acc_phone == phone_key:
                target = m
        target_active = target is not None and bool(getattr(target, "is_connected", False))
        if target_active and active_count <= 1:
            return {"ok": False, "error": "لا يمكن حذف آخر حساب نشط — أضف حساباً آخر وفعّله أولاً"}
        await self.stop_panel_account(int(row_id))
        await self.db.delete_panel_account(int(row_id))
        logger.info(f"Panel account deleted (row {row_id})")
        return {"ok": True}

    # ═══ v16.2.0: حذف حساب env نهائياً (استبعاد دائم يدوم عبر النشرات) ═══
    # الحساب الميت نهائياً (AuthKeyDuplicated مثلاً) لا يُصلحه اتصال مجدد —
    # المالك يحذفه من اللوحة ثم يعيد تسجيل نفس الرقم عبر معالج الربط.
    _EXCLUDED_ENV_KEY = "dashboard:excluded_env_phones"

    async def _load_excluded_env_phones(self) -> None:
        if self._excluded_env_loaded:
            return
        self._excluded_env_loaded = True
        try:
            row = await self.db._fetchone(
                "SELECT value FROM app_settings WHERE key = ?",
                (self._EXCLUDED_ENV_KEY,))
            import json as _json_mod
            raw = (row or {}).get("value") if row else None
            items = _json_mod.loads(raw) if raw else []
            if isinstance(items, list):
                self._excluded_env_phones = {
                    re.sub(r"\D", "", str(x)) for x in items
                    if re.sub(r"\D", "", str(x))}
            if self._excluded_env_phones:
                logger.info(
                    f"Excluded env accounts loaded: {len(self._excluded_env_phones)} "
                    "phone(s) — تُتخطى عند الإقلاع")
        except Exception as e:
            logger.warning(f"excluded env phones load skipped (fail-safe): {type(e).__name__}: {e}")

    async def _persist_excluded_env_phones(self) -> None:
        try:
            import json as _json_mod
            payload = _json_mod.dumps(sorted(self._excluded_env_phones))
            await self.db._execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                "updated_at = CURRENT_TIMESTAMP",
                (self._EXCLUDED_ENV_KEY, payload))
            await self.db._commit()
        except Exception as e:
            logger.error(f"excluded env phones persist failed: {type(e).__name__}: {e}")

    async def exclude_env_account(self, prefix: str) -> Dict[str, Any]:
        """
        حذف نهائي لحساب env من منظومة المراقبة (v16.2.0).

        يقطع المراقب ويزيله من القائمة، ويحفظ رقمه في app_settings
        لكي يُتخطى عند كل إقلاع لاحق. متغيرات Render نفسها لا تُمسّ —
        استعادة الحساب تتطلب حذف مفتاح الاستبعاد أو تعديل env يدوياً.
        حماية «آخر حساب متصل»: يُرفض استبعاد الوحيد المتصل.
        """
        pref = str(prefix or "").strip().upper()
        if not pref:
            return {"ok": False, "error": "معرّف الحساب مفقود"}
        target = None
        for m in list(self.monitors):
            acc = getattr(m, "account", {}) or {}
            if str(acc.get("source") or "") == "panel":
                continue  # حساب لوحة — يُحذف عبر delete_panel_account
            if str(acc.get("prefix") or "").upper() == pref:
                target = m
                break
        if target is None:
            return {"ok": False, "error": "لم يُعثر على حساب env بهذا المعرّف"}
        acc = getattr(target, "account", {}) or {}
        phone_key = re.sub(r"\D", "", str(acc.get("phone") or ""))
        if getattr(target, "is_connected", False):
            active_count = sum(
                1 for m in self.monitors if getattr(m, "is_connected", False))
            if active_count <= 1:
                return {"ok": False,
                        "error": "لا يمكن حذف آخر حساب متصل — أضف حساباً بديلاً وفعّله أولاً"}
        try:
            await target.disconnect()
        except Exception:
            pass
        try:
            self.monitors.remove(target)
        except ValueError:
            pass
        if self.main_client is getattr(target, "client", None):
            self.main_client = None
            for mm in self.monitors:
                if getattr(mm, "is_connected", False):
                    self.main_client = mm.client
                    break
        persisted = False
        if phone_key:
            self._excluded_env_phones.add(phone_key)
            await self._persist_excluded_env_phones()
            persisted = True
        logger.info(
            f"Env account excluded permanently: {acc.get('name')} "
            f"(prefix={pref}, phone_key={phone_key[:4]}***)")
        return {"ok": True, "excluded": True, "prefix": pref,
                "persisted": persisted,
                "note": "الحساب حُذف نهائياً ويُتخطى عند كل إقلاع — يمكن الآن إعادة تسجيل رقمه من اللوحة"}

    async def reconnect_account(self, kind: str, ref: Any) -> Dict[str, Any]:
        """إعادة اتصال حساب (env أو panel) دون إعادة نشر — عبر المراقب القائم."""
        monitors = list(self.monitors)
        target = None
        if kind == "env":
            ref_str = str(ref or "")
            for m in monitors:
                if (getattr(m, "account", {}) or {}).get("prefix", "").upper() == ref_str.upper():
                    target = m
                    break
            if target is None:
                # ابحث بالرقم المقابل من config
                import config as _cfg

                acc = next((a for a in (_cfg.ACCOUNTS or []) if str(a.get("prefix", "")).upper() == ref_str.upper()), None)
                if acc is not None:
                    key = re.sub(r"\D", "", str(acc.get("phone") or ""))
                    for m in monitors:
                        if re.sub(r"\D", "", str((m.account or {}).get("phone") or "")) == key:
                            target = m
                            break
        else:
            row = await self.db.get_panel_account(int(ref)) if str(ref).isdigit() else None
            if row is None:
                return {"ok": False, "error": "الحساب غير موجود"}
            key = re.sub(r"\D", "", str(row.get("phone") or ""))
            for m in monitors:
                if re.sub(r"\D", "", str((m.account or {}).get("phone") or "")) == key:
                    target = m
                    break
            if target is None:
                # لا مراقب قائم → بناء جديد (التفعيل عبر الصف)
                return await self.spawn_panel_account(row)
        if target is None:
            return {"ok": False, "error": "لم يُوجد مراقب لهذا الحساب"}

        try:
            ok = await asyncio.wait_for(target.connect(), timeout=120)
        except asyncio.TimeoutError:
            ok = False
        except Exception as e:
            logger.error(f"reconnect_account error: {type(e).__name__}: {e}")
            ok = False
        if ok and self.main_client is None:
            self.main_client = target.client
        # مزامنة حالة صف اللوحة
        if kind == "panel":
            row_id = int(ref)
            if ok:
                await self.db.update_panel_account(row_id, status="active", last_error=None)
            else:
                terminal = getattr(target, "_terminal_error", None)
                await self.db.update_panel_account(
                    row_id,
                    status="needs_relogin" if terminal else "error",
                    last_error=str(getattr(target, "_last_connect_error", None) or "فشل الاتصال")[:400],
                )
        return {"ok": True, "connected": bool(ok)}

    async def sync_panel_boot_status(self, duplicates: List[int], undecryptable: List[int]) -> None:
        """مزامنة حالات حسابات اللوحة بعد الإقلاع (مكرر env / جلسة تالفة)."""
        for row_id in duplicates:
            try:
                await self.db.update_panel_account(
                    int(row_id), status="error",
                    last_error="مكرر مع حساب بيئة بنفس الرقم — المراقبة تعطّلة لهذا الصف",
                )
            except Exception:
                pass
        for row_id in undecryptable:
            try:
                await self.db.update_panel_account(
                    int(row_id), status="needs_relogin",
                    last_error="تعذر فك تشفير الجلسة (مفتاح التشفير تغيّر؟) — أعد ربط الحساب",
                )
            except Exception:
                pass

    # ─── Initialization ────────────────────────────────────────────────────────
    async def initialize(self) -> bool:
        logger.info("=" * 60)
        logger.info(f"Enhanced Telegram Bot v{BOT_VERSION} (RENDER EDITION, HARDENED) – Initializing...")
        logger.info("=" * 60)

        # v15.1.0: تثبيت مرجع الحلقة الرئيسية قبل بدء خيط الصحة — معالجات
        # اللوحة تجسر كل عملياتها الثقيلة إلى هذه الحلقة حصراً.
        self._main_loop = asyncio.get_running_loop()

        # v14.2.1 (wake-stability): خادم الصحة يبدأ أولاً وقبل أي شيء آخر —
        # حتى أثناء db.connect/المهاجرات/الصيانة الإقلاعية يستجيب المنفذ
        # فوراً بـ {"status": "starting"}، فينجح استيقاظ Render بدل أن
        # يفتح المنفذ متأخراً جداً فيُرجع الراوتر hibernate-wake-error.
        # v14.6.1 (liveness decoupling): خادم الصحة في thread مستقل بحلقة
        # خاصة — يجيب فوراً من لقطة الذاكرة حتى لو انحسرت الحلقة الرئيسية
        # (السبب الجذري لحلقة انهيار SIGTERM: فحص /health يعيش على نفس
        # الحلقة المزدحمة → مهلة Render → قتل → إقلاع أثقل → تكرار).
        # حلقة اللقطة تُحدّثها من الحلقة الرئيسية كل 10s.
        self._start_health_server_thread()
        self._health_server_task = self._track_task(
            self._health_snapshot_loop(), "health_snapshot"
        )
        logger.info("Health server task started EARLY (thread + snapshot; /health starting→ok on $PORT)")

        if not await self.db.connect():
            logger.error("Database connection failed - aborting")
            return False

        # v14.5.1 (T3.4): حارس الذاكرة — يبدأ فور نجاح اتصال DB وقبل أي
        # مهمة أخرى: مراقبة RSS كل 30s، تحذير 70%، Soft Reset 85% (تفريغ
        # batch + إغلاق DB + مسح الكاشات + إعادة اتصال)، Hard Reset 95%
        # (os._exit ليعيد Render تشغيل العملية). مرجع self.monitors يُمرر
        # بالمرجع فتتمّ قائمة المراقبين لاحقاً في initialize.
        try:
            await start_memory_guard(self.db, self.monitors)
        except Exception as _mg_err:
            logger.warning(f"memory_guard start skipped: {type(_mg_err).__name__}: {_mg_err}")

        # v11.8.2 (F-18): ترطيب الذاكرة السلبية (tg_user_blocked + حدود
        # reply_fix) من app_settings بعد الاتصال مباشرة — قبل أي مراقب،
        # فلا تُعاد إعادة إرسال tg://user المرفوض ولا تُستهلك حصص
        # reply_fix من جديد بعد إعادة التشغيل. فشل-آمن (تجاهل مسجّل).
        try:
            import negative_memory as _negmem
            await _negmem.hydrate_all(self.db)
        except Exception as _nm_err:
            logger.warning(f"negative_memory hydrate skipped: {type(_nm_err).__name__}")

        # v9.10: تهيئة حاجز منع تكرار التنبيهات (بعد اتصال DB مباشرة —
        # singleton مشترك بين كل المراقبين، وجدول alert_dedup يُنشأ ضمن
        # _create_tables في connect()).
        init_deduplicator(self.db)

        # v12.0 unified_alerts: صيانة الإقلاع — purge صفوف processing_queue
        # الأقدم من QUEUE_STALE_PURGE_SECONDS (مخزن تشغيلي عابر). بعد
        # انقطاع/إعادة نشر قد يظل الطابور ممتلئاً بأحداث لن تصلح تنبيهاتها
        # (بوابة العمر ستُسقطها عند pop) — التنظيف هنا يمنع evictions
        # تضيّر أحداثاً جديدة. فشل-آمن بلا استثناءات للإقلاع.
        try:
            _maint = await run_startup_maintenance(self.db)
            if _maint.get("purged_queue_rows"):
                logger.info(f"unified_alerts startup maintenance: {_maint}")
        except Exception as _maint_err:
            logger.debug(f"startup maintenance skipped: {type(_maint_err).__name__}")

        # v14.6.0: عدّاد إعادة التشغيل — يُثبَّت مبكراً بعد اتصال DB
        # (فشل-آمن: لا يوقف الإقلاع أبداً).
        await self._persist_boot_counter()

        # v9.11: تهيئة محرك مكافحة السبام وتحميل قائمة التجاهل الدائم من DB
        # (قبل تشغيل أي مراقب — حتى تُتجاهل رسائل المصنفين كمزعجين منذ اللحظة الأولى).
        await setup_antispam(self.db)

        # v15.1.0: ترطيب حالة اللوحة من app_settings (مفاتيح الإيقاف المتبقية
        # من جلسة سابقة + الكلمات المخصصة) — فشل-آمن بلا استثناءات للإقلاع.
        if DASHBOARD_AVAILABLE:
            try:
                await _hydrate_dashboard(self)
            except Exception as _dh_err:
                logger.warning(
                    f"dashboard hydrate skipped (fail-safe): {type(_dh_err).__name__}: {_dh_err}"
                )

        # ══ v15.1.0 لوحة التحكم الخفيفة ══
        # نفس خادم الصحة يخدم اللوحة على /admin (نفس المنفذ $PORT) — بلا
        # FastAPI/uvicorn (درس v14.0). التركيب داخل _start_health_server_thread
        # أعلاه، والترطيب أعلاه. فشل اللوحة لا يمس /health ولا مسار الرسائل.

        self._keep_alive_task = self._track_task(self._keep_alive_loop(), "keep_alive")

        accounts = sorted(ACCOUNTS, key=lambda a: a.get("priority", 0), reverse=True)

        # ═══ v14.10.0: دمج حسابات اللوحة (DB — جلسات مشفّرة) مع حسابات env ═══
        # env أولوية؛ صف بلا مراقبة مُفعّلة يبقى محفوظاً بلا اتصال (متوقف).
        # الفشل كله فشل-آمن: مشكلة اللوحة لا توقف الإقلاع ولا تقلّل حسابات env.
        # v16.2.0: تحميل المستبعدات قبل الدمج — حساب env محذوف نهائياً من
        # اللوحة لا يُبنى له مراقب ولا يُحسب مكرراً عند إعادة تسجيل رقمه.
        await self._load_excluded_env_phones()
        if self._excluded_env_phones:
            accounts = [
                a for a in accounts
                if str(a.get("source") or "") == "panel"
                or re.sub(r"\D", "", str(a.get("phone") or "")) not in self._excluded_env_phones
            ]

        try:
            if self._panel_rows_cache is None:
                self._panel_rows_cache = await self.db.get_panel_accounts()
            from dashboard.accounts import merge_panel_accounts

            panel_accs, dup_ids, undec_ids = merge_panel_accounts(
                self._panel_rows_cache, accounts
            )
            if panel_accs:
                accounts.extend(panel_accs)
                accounts.sort(key=lambda a: a.get("priority", 0), reverse=True)
                logger.info(
                    f"Panel accounts merged at boot: {len(panel_accs)} "
                    f"(duplicates={len(dup_ids)}, undecryptable={len(undec_ids)})"
                )
            if dup_ids or undec_ids:
                await self.sync_panel_boot_status(dup_ids, undec_ids)
        except Exception as panel_boot_err:
            logger.warning(
                f"Panel accounts boot-merge skipped (fail-safe): "
                f"{type(panel_boot_err).__name__}: {panel_boot_err}"
            )

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

        # ═══ v14.5.5-fix: تسليح حلقة إعادة الاتصال حتى عند فشل الإقلاع ═══
        # كان ReconnectionManager يبدأ فقط عند نجاح الاتصال الأول (connect
        # ناجح يستدعي _reconnect.start) — أي حساب يفشل عند الإقلاع
        # (AuthKeyDuplicated أثناء تداخل نشر بصفر-انقطاع حيث تعمل النسخة
        # القديمة والجديدة معاً ثوانٍ وتتصلان بنفس الجلسة، أو FloodWait
        # قصير، أو مهلة شبكة) كان يبقى ميتاً إلى إعادة تشغيل يدوية — ساعات
        # بلا رصد. الآن يُسلَّح مدير إعادة الاتصال لكل مراقب غير متصل:
        # يعيد المحاولة بالتراجع التدريجي (5s→300s) ويتصلح ذاتياً بمجرد
        # اختفاء التعارض أو زوال السبب العابر.
        for m in self.monitors:
            if not m.is_connected:
                # v14.7.0: المراقب بحالة نهائية (AuthKeyDuplicated/2FA/...)
                # لا يُسلّح له مشرف — إعادة المحاولة بلا جدوى حتى إعادة
                # الدخول اليدوية (needs_relogin=true على /health/full).
                if getattr(m, "_terminal_error", None) is not None:
                    logger.error(
                        f"{m.account['name']} has terminal error "
                        f"({m._terminal_error}) — NOT arming reconnect supervisor "
                        f"(manual re-login required)"
                    )
                    continue
                try:
                    rm = getattr(m, "_reconnect", None)
                    if rm is not None and not getattr(rm, "_running", False):
                        await rm.start()
                        logger.info(
                            f"Reconnect supervisor armed for {m.account['name']} "
                            "(initial connect failed — will retry with backoff)"
                        )
                except Exception as arm_err:
                    logger.debug(f"Reconnect arm failed for monitor: {type(arm_err).__name__}: {arm_err}")

        if self.main_client is None:
            for m in self.monitors:
                if m.is_connected:
                    self.main_client = m.client
                    for mon in self.monitors:
                        mon.main_client = self.main_client
                    logger.warning(f"⚠️ Fallback main client: {m.account['name']}")
                    break

        logger.info(f"\n📊 Connected: {connected}/{len(accounts)}")

        # ═══ v14.10.0: مزامنة حالة صفوف اللوحة بعد الاتصال الإقلاعي ═══
        # مراقب حساب لوحة فشل إقلاعه يُحدّث صفاً في DB (needs_relogin/error)
        # ليعكسه المدخل فوراً بدل حالة 'جاهز' كاذبة.
        for m in self.monitors:
            acc = getattr(m, "account", {}) or {}
            if acc.get("source") == "panel" and acc.get("row_id"):
                row_id = int(acc["row_id"])
                try:
                    if getattr(m, "is_connected", False):
                        await self.db.update_panel_account(row_id, status="active", last_error=None)
                    else:
                        terminal = getattr(m, "_terminal_error", None)
                        err_text = str(getattr(m, "_last_connect_error", None) or "فشل الاتصال عند الإقلاع")
                        await self.db.update_panel_account(
                            row_id,
                            status="needs_relogin" if terminal else "error",
                            last_error=err_text[:400],
                        )
                except Exception as sync_err:
                    logger.debug(f"panel boot sync row {row_id} failed: {type(sync_err).__name__}")

        if connected == 0:
            logger.warning(
                "No accounts connected - monitoring disabled. "
                "Fix the *_SESSION_STRING env vars, then restart."
            )

        self.memory_monitor.start()

        # ══ v10.8: فحص عضوية بوت التنبيهات في قناة الهدف عند الإقلاع ══
        # getMe (تحقق التوكن + هوية البوت) ثم getChatMember — تحذير واضح
        # إن لم يكن البوت عضواً (يفضل أدمن) في TARGET_GROUP_ID. لا يوقف
        # الإقلاع أبداً — غير العضو سيفشل إرساله وسيعمل الـfallback تلقائياً.
        if self.alert_bot.enabled and getattr(CFG, "ALERT_BOT_ENABLED", True):
            logger.info("🤖 AlertBot مفعّل — فحص العضوية في TARGET_GROUP_ID عند الإقلاع")
            self._track_task(self._alert_bot_boot_check(), "alert_bot_boot_check")
        else:
            logger.info(
                "🤖 AlertBot غير مفعّل (لا ALERT_BOT_TOKEN أو ALERT_BOT_ENABLED=false) "
                "— التنبيهات تُرسل من حسابات المستخدمين بدون أزرار (fallback)"
            )

        # ══ v11.0: عميل بوت التنبيهات الحي — أزرار عملية باستقبال فعلي ══
        # نفس توكن البوت عبر MTProto: يستقبل CallbackQuery (معالج «copy_»
        # يجيب بنص التنبيه من DB) ويعمل طبقة إرسال احتياطية بالأزرار إن
        # فشل Bot API. فشل التشغيل لا يوقف الإقلاع أبداً (فشل-آمن).
        # v14.5.1: حُذف فحص ALERT_UI_CLIENT_ENABLED غير المعرّف في config
        # (كان دائماً True فيبدو أنه مفتاح تشغيلي وهو ليس كذلك). البوابة
        # الوحيدة الحقيقية هي ALERT_BOT_ENABLED.
        if self.alert_bot.enabled and getattr(CFG, "ALERT_BOT_ENABLED", True):
            try:
                # v11.5.3: لا حساب is_main في بيئة الإنتاج (بلاغ لوج Render:
                # «api_id/api_hash مفقود — عميل أزرار التنبيهات معطّل») —
                # الاختيار في _pick_alertui_account: is_main أولاً ثم أول
                # حساب متصل فعلاً. تفعيل العميل يُشغّل: معالجات أزرار
                # التنبيهات، طبقة الإرسال الاحتياطي بالأزرار، وتحقق تسليم
                # timeout (v11.5.2) — وكلها لا تعمل بدونه.
                # v14.5.1: ACCOUNTS module-level في config.py (ليس حقلاً على
                # CFG) — getattr(CFG, "ACCOUNTS", None) كان None دائماً،
                # فأولوية is_main معطلة بلا تحذير. حُذف السطر الميت (next(...))
                # وACCOUNTS يُمرَّر مباشرة الآن.
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
        # v9.11: معالجات زر «تواصل مع المرسل» + مسار الرد البديل
        await self._register_contact_handlers()

        # ══ v14.6.0 Activity Maintenance System ══
        # رسالة النشاط الدورية + حارس الخدمة (event loop lag / تسليح إعادة
        # اتصال المراقبين الميتين / كشف توقف الطابور / تحذيرات إدارية).
        # يبدأ بعد تجهيز alert_bot (هوية الإرسال B_Bot). فشل البدء لا يوقف
        # الإقلاع — السلّم الحقيقي يعمل بدونه.
        try:
            self.activity = ActivityWatchdog(self)
            self._activity_task = self._track_task(
                self.activity.run(), "activity_watchdog"
            )
        except Exception as _act_err:
            logger.warning(
                f"ActivityWatchdog start skipped: {type(_act_err).__name__}: {_act_err}"
            )

        # Background Tasks (M-05/M-16: all tracked uniformly)
        self._consumer_task = self._track_task(self._consumer_loop(), "consumer")
        # v15.5.0: متحكم تخفيف الحمل — fuzzy/resolver تخفيض تلقائي عند تشبع الطابور
        self._load_shed_task = self._track_task(self._load_shed_loop(), "load_shed")
        self._stats_task = self._track_task(self._stats_reporter(), "stats")
        self._cleanup_task = self._track_task(self._cleanup_loop(), "cleanup")
        # v9.12 (audit H-08): dedicated short-cadence dedup cleanup task.
        self._dedup_cleanup_task = self._track_task(self._dedup_cleanup_loop(), "dedup_cleanup")
        # v12.1.0 التزامن الصلب: استرداد دوري لأحداث in-flight العالقة
        self._inflight_recovery_task = self._track_task(
            self._inflight_recovery_loop(), "inflight_recovery"
        )
        # v10.0 Precision: التقرير اليومي للإدارة
        if getattr(CFG, "DIGEST_ENABLED", True):
            self._digest_task = self._track_task(self._digest_loop(), "daily_digest")
        self._health_task = self._track_task(self._health_check_loop(), "health")
        self._memory_task = self._track_task(self._memory_monitor_loop(), "memory")
        self._main_client_watchdog_task = self._track_task(
            self._main_client_watchdog_loop(), "main_client_watchdog"
        )
        # v10.4: warm the group-entity caches right after boot so the first
        # alerts skip the cold get_entity round-trip (bounded, fire-safe).
        self._track_task(self._warmup_chat_caches(), "chat_cache_warmup")

        # v14.2.1 + v14.6.1: من هذه اللحظة /health يتحول من "starting" إلى
        # liveness 200 (عبر اللقطة المشتركة للخيط المستقل) — حالة DB تظهر
        # في الجسم بلا 503 (فشل فحص Render كان يعيد التشغيل قسرياً).
        self._boot_complete = True
        # v14.6.1: تحرير فحص الصحة — /health يتحول من starting إلى liveness 200
        self._health_snapshot.set_boot_complete()
        logger.info("✅ Initialization complete (Render Edition, hardened) — /health now full contract")
        return True

    async def _alert_bot_boot_check(self) -> None:
        """══ v10.8: فحص بوت التنبيهات عند الإقلاع ══

        getMe أولاً (تحقق التوكن + هوية البوت) ثم getChatMember على
        TARGET_GROUP_ID: البوت غير العضو (يفضل أن يكون أدمن) سيفشل كل
        sendMessage — تحذير واضح في اللوج مع الخطوة المطلوبة. الفحص
        لا يوقف الإقلاع أبداً (فشل-آمن — fallback يعمل تلقائياً)."""
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
            # v11.5.2: هوية البوت من هويّاتنا — رسائله في مجموعة الهدف لا تُراقب
            register_self_sender(self.alert_bot.bot_id)
            # ══ v15.0: الإصلاح الحدوي الفوري عند فقدان العضوية ══
            # بدل انتظار دورة الـwatchdog (60s) — كل اكتشاف لفقدان العضوية
            # (فحص استباقي قبل الإرسال أو 403 أثناء الإرسال) يطلق الإصلاح
            # حالاً، مع احترام cooldown نفسه (حماية من عواصف الإصلاح).
            self.alert_bot.on_membership_lost = self._on_alertbot_membership_lost
            await self.alert_bot.check_membership()
            # ══ v14.9.1: الأزرار عبر Bot API حصراً — بوت غير عضو = تنبيهات
            # بلا أزرار. محاولة إصلاح ذاتي فورية + حلقة مراقبة تحدّث الحالة
            # وتعيد المحاولة بعد cooldown، وتكشف الاستعادة اليدوية سريعاً.
            if self.alert_bot.membership_ok is False:
                self._track_task(
                    self._heal_alertbot_membership(trigger="boot"),
                    "alertbot_heal_boot",
                )
            self._alertbot_membership_task = self._track_task(
                self._alertbot_membership_watchdog(), "alertbot_membership"
            )
        except Exception as e:
            logger.debug(f"alert_bot boot check skipped: {type(e).__name__}: {e}")

    # ═══════════════ v14.9.1: Buttons Availability Heal ═══════════════
    # تيليجرام يرفض إرفاق أزرار inline من حسابات المستخدمين (قيد منصة
    # موثق — silent_drop_no_reply_markup). المسار الوحيد القادر على
    # إرفاق [مراسلة][عرض الرسالة] هو Bot API بوت عضو في قناة الهدف.
    # الإصلاح الذاتي: الحسابات المشرفة تملك صلاحية الإضافة — نحاول
    # إعادة إضافة البوت وترقيته بصلاحية «نشر الرسائل» فقط (أقل صلاحية
    # ممكنة، وهي الدور نفسه قبل الفقدان). العملية قابلة للعكس ولا
    # تلمس أي إعداد آخر، ولا تُرقّي بوتاً مشرفاً أصلاً (حماية من
    # إسقاط صلاحياته الحالية — EditAdminRequest يستبدل الصلاحيات).

    def _alertbot_heal_clients(self) -> List[TelegramClient]:
        """مرشّحو الإصلاح: المرسل اللاصق (ناشر مثبت للقناة الهدف) ثم
        main_client ثم عملاء المراقبين الأحياء — بلا تكرار."""
        cands: List[TelegramClient] = []
        sc = getattr(self, "alert_sender_client", None)
        if sc is not None and sc.is_connected():
            cands.append(sc)
        mc = getattr(self, "main_client", None)
        if mc is not None and mc.is_connected() and all(mc is not c for c in cands):
            cands.append(mc)
        for m in self.monitors:
            c = getattr(m, "client", None)
            if (
                c is not None
                and c.is_connected()
                and all(c is not x for x in cands)
            ):
                cands.append(c)
        return cands

    def _alertbot_heal_cooldown_ok(self) -> bool:
        cd = max(300, int(getattr(CFG, "ALERT_BOT_HEAL_COOLDOWN_SECONDS", 1800) or 1800))
        return (time.time() - self._alertbot_heal_last_ts) >= cd

    def _on_alertbot_membership_lost(self) -> None:
        """v15.0: الإصلاح الحدوي الفوري لفقدان عضوية بوت التنبيهات.

        يستدعيه alert_bot فور اكتشاف فقدان العضوية (فحص استباقي قبل
        الإرسال أو 403 أثناء الإرسال) — يطلق heal حالاً إذا انتهت
        cooldown، وإلا يبقى الطلب في _heal_requested ويلتقطه الـwatchdog
        فور انتهاء المهلة. بلا استثناءات — فشل-آمن."""
        try:
            if not self._alertbot_heal_cooldown_ok():
                logger.debug(
                    "[ALERTBOT-HEAL] membership-lost event suppressed "
                    "(heal cooldown) — watchdog will retry after cooldown"
                )
                return
            self._track_task(
                self._heal_alertbot_membership(trigger="membership_lost_event"),
                "alertbot_heal_event",
            )
        except Exception as e:
            logger.debug(
                f"[ALERTBOT-HEAL] event heal spawn failed: {type(e).__name__}"
            )

    async def _heal_alertbot_membership(
        self,
        trigger: str = "boot",
        clients: Optional[List[TelegramClient]] = None,
    ) -> bool:
        """إعادة إضافة بوت التنبيهات لقناة الهدف وترقيته (نشر الرسائل فقط).

        v14.9.1 — التسلسل لكل مرشّح:
          1) حلّ الكيانين (القناة الهدف + البوت باسمه العام).
          2) دعوة البوت (تُتسامح حالة الوجود المسبق).
          3) فحص العضوية: مشرف أصلاً → انتهاء ناجح بلا ترقية (حماية
             صلاحياته الحالية)؛ عضو عادي → ترقية post_messages فقط.
          4) تحقق نهائي عبر check_membership (مصدر الحقيقة).
        لا يرفع استثناءات إطلاقاً — كل فشل يُسجل ويُحوّل تحذيراً إدارياً.
        """
        bot = getattr(self, "alert_bot", None)
        if bot is None or not bot.enabled or not bot.bot_username:
            return False
        if not bool(getattr(CFG, "ALERT_BOT_AUTO_ADD", True)):
            logger.info("[ALERTBOT-HEAL] skipped (ALERT_BOT_AUTO_ADD=false)")
            return False
        cands = clients if clients is not None else self._alertbot_heal_clients()
        if not cands:
            logger.warning("[ALERTBOT-HEAL] no connected client available for heal")
            await self._alertbot_admin_warn(trigger=trigger, reason="no_connected_client")
            return False
        try:
            from telethon.tl.functions.channels import (
                EditAdminRequest,
                InviteToChannelRequest,
            )
            from telethon.tl.types import ChatAdminRights
        except Exception as e:
            logger.warning(f"[ALERTBOT-HEAL] telethon imports failed: {type(e).__name__}")
            return False
        self._alertbot_heal_last_ts = time.time()
        self._alertbot_heal_attempts += 1
        _attempt = self._alertbot_heal_attempts
        logger.warning(
            f"[ALERTBOT-HEAL] attempt=#{_attempt} trigger={trigger} "
            f"candidates={len(cands)} — restoring @{bot.bot_username} membership "
            "in target channel (post_messages only)"
        )
        for c in cands:
            try:
                target = await c.get_input_entity(int(CFG.TARGET_GROUP_ID))
            except Exception as e:
                logger.debug(f"[ALERTBOT-HEAL] candidate target resolve failed: {type(e).__name__}")
                continue
            try:
                bot_ent = await c.get_input_entity(f"@{bot.bot_username}")
            except Exception as e:
                logger.debug(f"[ALERTBOT-HEAL] candidate bot resolve failed: {type(e).__name__}")
                continue
            # ══ فحص العضوية الحالية أولاً — مشرف أصلاً لا يُلمس إطلاقاً ══
            # (EditAdminRequest يستبدل الصلاحيات — الترقية الأعمى كانت ستسقط
            # صلاحيات قائمة مثل edit/delete/pin إن وُجدت).
            try:
                await bot.check_membership()
            except Exception:
                pass
            if bot.membership_ok is True and bot.membership_status in (
                "administrator", "creator"
            ):
                logger.warning(
                    f"[ALERTBOT-HEAL] SUCCESS (already-admin, rights untouched) — "
                    f"@{bot.bot_username} عضو مشرف في قناة الهدف"
                )
                await self._alertbot_admin_notify(
                    "✅ تم استعادة أزرار التنبيهات: بوت التنبيهات "
                    f"@{bot.bot_username} عضو مشرف في قناة الهدف ولم يُعدّل "
                    "أي صلاحية. زرا «مراسلة» و«عرض الرسالة» سيعملان في "
                    "التنبيه التالي."
                )
                return True
            # ══ المسار الرسمي للقنوات: إضافة-كمشرف مباشرة ══
            # القنوات ترفض دعوة البوتات كأعضاء عاديين (InviteToChannelRequest
            # → UserBotError حادثة الإقلاع 21-11). Telegram الرسمي يضيف
            # البوت للقناة عبر EditAdminRequest مباشرة — تعمل حتى لغير
            # العضو (add-as-admin في خطوة واحدة)، وصلاحية نشر الرسائل فقط.
            rights = ChatAdminRights(
                change_info=False,
                post_messages=True,
                edit_messages=False,
                delete_messages=False,
                ban_users=False,
                invite_users=False,
                pin_messages=False,
                add_admins=False,
            )
            promoted = False
            try:
                await c(EditAdminRequest(target, bot_ent, rights, "alerts"))
                promoted = True
                logger.info(
                    "[ALERTBOT-HEAL] direct add-as-admin sent "
                    "(EditAdminRequest, post_messages only)"
                )
            except Exception as e:
                s = str(e).upper()
                if "NOT_PARTICIPANT" in s or "PARTICIPANT_MISSING" in s:
                    # بعض الخوادم تشترط العضوية أولاً → دعوة ثم إضافة-كمشرف
                    try:
                        await c(InviteToChannelRequest(target, [bot_ent]))
                        await asyncio.sleep(1.5)
                    except Exception as e2:
                        s2 = str(e2).upper()
                        if "ALREADY_PARTICIPANT" in s2 or "USER_NOT_MUTUAL" in s2:
                            logger.info(f"[ALERTBOT-HEAL] invite tolerated: {type(e2).__name__}")
                        else:
                            logger.info(
                                f"[ALERTBOT-HEAL] invite fallback failed "
                                f"[{type(e2).__name__}] — next candidate"
                            )
                            continue
                    try:
                        await c(EditAdminRequest(target, bot_ent, rights, "alerts"))
                        promoted = True
                        logger.info("[ALERTBOT-HEAL] add-as-admin sent after invite")
                    except Exception as e3:
                        logger.info(
                            f"[ALERTBOT-HEAL] promote failed after invite "
                            f"[{type(e3).__name__}] — next candidate"
                        )
                        continue
                else:
                    logger.info(
                        f"[ALERTBOT-HEAL] direct add-as-admin rejected "
                        f"[{type(e).__name__}] — next candidate"
                    )
                    continue
            if not promoted:
                continue
            # ══ التحقق النهائي (مصدر الحقيقة: getChatMember من Bot API) ══
            try:
                await asyncio.sleep(1.0)
                await bot.check_membership()
            except Exception:
                pass
            if bot.membership_ok is True:
                logger.warning(
                    "[ALERTBOT-HEAL] SUCCESS — "
                    f"@{bot.bot_username} re-added as channel admin "
                    "(post_messages only) — أزرار التنبيهات مستعادة"
                )
                await self._alertbot_admin_notify(
                    "✅ تم استعادة أزرار التنبيهات تلقائياً: أعيدت إضافة بوت "
                    f"التنبيهات @{bot.bot_username} إلى قناة الهدف بصلاحية "
                    "«نشر الرسائل» فقط. زرا «مراسلة» و«عرض الرسالة» سيعملان "
                    "في التنبيه التالي (بلا إعادة تشغيل)."
                )
                return True
            logger.info(
                "[ALERTBOT-HEAL] promote sent but membership unverified — "
                "next candidate"
            )
        logger.warning(
            "[ALERTBOT-HEAL] all candidates exhausted — لا حساب يملك صلاحية "
            "الإضافة/الترقية؛ سيُرسل تحذير إداري بخطوات الإضافة اليدوية"
        )
        await self._alertbot_admin_warn(trigger=trigger, reason="no_admin_rights")
        return False

    async def _alertbot_admin_notify(self, text: str) -> None:
        """رسالة إدارية مباشرة عبر Bot API إلى ADMIN_CHAT_ID (فشل-آمن)."""
        bot = getattr(self, "alert_bot", None)
        admin = int(getattr(CFG, "ADMIN_CHAT_ID", 0) or 0)
        if bot is None or not bot.enabled or not admin:
            return
        try:
            ok, _ = await bot.send_plain(text, chat_id=admin, silent=False)
            logger.info(f"[ALERTBOT-HEAL] admin notify sent ok={ok}")
        except Exception as e:
            logger.debug(f"[ALERTBOT-HEAL] admin notify failed: {type(e).__name__}")

    async def _alertbot_admin_warn(self, trigger: str, reason: str) -> None:
        """تحذير إداري (cooldown افتراضي 6 ساعات) بخطوات الإضافة اليدوية.

        رسالة DM عبر Bot API تعمل حتى بغير عضوية قناة الهدف (المراسلة
        الخاصة لا تتطلب عضوية). الفشل مسجل فقط — لا استثناءات."""
        cd = max(600, int(getattr(CFG, "ALERT_BOT_WARN_COOLDOWN_SECONDS", 21600) or 21600))
        now = time.time()
        if (now - self._alertbot_warn_last_ts) < cd:
            return
        self._alertbot_warn_last_ts = now
        bot = getattr(self, "alert_bot", None)
        _bot_name = f"@{bot.bot_username}" if bot and bot.bot_username else "بوت التنبيهات"
        await self._alertbot_admin_notify(
            "⚠️ أزرار التنبيهات («مراسلة» و«عرض الرسالة») معطّلة: بوت التنبيهات "
            "ليس عضواً في قناة الهدف، وتيليجرام يرفض إرفاق الأزرار من حسابات "
            f"المستخدمين.\n\nجرّبت الإضافة التلقائية ({trigger}) ولم تنجح "
            f"({reason}).\n\nالحل اليدوي (30 ثانية):\n"
            "1) افتح قناة الهدف ← إدارة القناة ← المشرفون.\n"
            f"2) إضافة مشرف ← ابحث عن: {_bot_name}\n"
            "3) فعّل صلاحية «نشر الرسائل» فقط ثم احفظ.\n\n"
            "بعد الإضافة تُستعاد الأزرار تلقائياً في التنبيه التالي — لا حاجة "
            "لإعادة تشغيل الخدمة."
        )

    async def _alertbot_membership_watchdog(self) -> None:
        """حلقة مراقبة عضوية بوت التنبيهات (دورة 60s، فحص كامل دوري).

        * طلب إصلاح فوري من send() (403 not-member) → محاولة إصلاح.
        * فحص كامل دوري (ALERT_BOT_MEMBERSHIP_RECHECK_SECONDS افتراضي 900s)
          يحدّث الحالة ويكشف الاستعادة اليدوية بسرعة.
        * الحلقة تنتظر _shutdown_event (إيقاف نظيف فوري بلا نوم متبق).
        لا ترفع استثناءات إطلاقاً (فشل-آمن)."""
        check_every = max(
            1,
            int(getattr(CFG, "ALERT_BOT_MEMBERSHIP_RECHECK_SECONDS", 900) or 900) // 60,
        )
        cycle = 0
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=_ALERTBOT_WATCHDOG_CYCLE_S,
                )
                break  # الإيقاف مفعّل → إنهاء نظيف
            except asyncio.TimeoutError:
                pass
            try:
                bot = getattr(self, "alert_bot", None)
                if bot is None or not bot.enabled:
                    continue
                if bot._heal_requested:
                    bot._heal_requested = False
                    if self._alertbot_heal_cooldown_ok():
                        await self._heal_alertbot_membership(trigger="send_403")
                cycle += 1
                if cycle % check_every == 0 or bot.membership_ok is None:
                    await bot.check_membership()
                    if bot.membership_ok is False and self._alertbot_heal_cooldown_ok():
                        await self._heal_alertbot_membership(trigger="periodic")
            except Exception as e:
                logger.debug(f"[ALERTBOT-HEAL] watchdog cycle error: {type(e).__name__}")

    async def _warmup_chat_caches(self) -> None:
        """v10.4: prefetch the entities of recently-seen source chats into
        each connected monitor's long-TTL entity cache (_chat_info reads
        the same cache, so behavior is unchanged — only warmer). Bounded:
        ≤30 chats × ≤6 accounts, semaphore 3, 10s per lookup, never raises."""
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

        # v11.8.2 (F-18): تفريغ أخير للذاكرة السلبية قبل انفصال DB
        # (يلتقط ما تبقى من وسَخة لم تُفرَّغ بعد خلال نافذة التأجيل)
        try:
            import negative_memory as _negmem
            await _negmem.flush_now(self.db)
        except Exception:
            pass

        # M-05/M-16: cancel every tracked background task uniformly,
        # rather than an explicit hand-maintained list that could miss a
        # task (e.g. the previous fire-and-forget signal-handler task).
        tasks = [t for t in list(self._background_tasks) if t is not asyncio.current_task()]
        for task in tasks:
            if not task.done():
                task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # v14.5.1: مهام task_util العابرة (spawn_bg من nav_resolver و
        # sender_resolver) كانت تُلغى عند asyncio.run() cleanup بعد إغلاق
        # DB → RuntimeError: Event loop is closed أو فقدان كتابة write-behind.
        # cancel_all ينتظرها حتى 5s ثم يعود بهدوء.
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
        # v11.0: فصل عميل بوت التنبيهات الحي بشكل نظيف
        if self.alert_ui_client is not None:
            await self.alert_ui_client.stop()
            self.alert_ui_client = None
        # v11.8.1 (F-24): إغلاق جلسة HTTP الدائمة لـAlertBot
        try:
            await self.alert_bot.aclose()
        except Exception:
            pass
        # v14.5.1 (T3.4): إيقاف حارس الذاكرة قبل إغلاق DB — حتى لا يعيد
        # اتصال DB أو ينادي os._exit أثناء إنهاء متوقع
        try:
            await stop_memory_guard()
        except Exception as _mg_err:
            logger.debug(f"memory_guard stop skipped: {type(_mg_err).__name__}: {_mg_err}")
        await self.db.close()
        logger.info("✅ Shutdown complete")

    def request_safe_restart(self, reason: str = "dashboard") -> bool:
        """══ v15.1.0: إعادة تشغيل آمنة مطلوبة من لوحة التحكم ══

        إيقاف رشيق كامل (نفس مسار stop() الرسمي: تفريغ negative_memory →
        إلغاء المهام المتعقوبة → فصل العملاء → إغلاق DB) ثم خروج نظيف
        بـ exit code 0 — يعيد Render إقلاع الخدمة تلقائياً. تُستدعى من خيط
        الصحة، لذا تُجدول على الحلقة الرئيسية عبر call_soon_threadsafe.
        """
        if not self.is_running:
            logger.warning("safe_restart requested but bot is not running — ignored")
            return False
        loop = self._main_loop
        if loop is None or loop.is_closed():
            return False

        def _schedule() -> None:
            async def _restart() -> None:
                logger.warning(f"🔄 Safe restart requested via dashboard (reason={reason})")
                try:
                    await self.stop(signal=reason)
                except Exception as e:
                    logger.error(f"safe_restart stop() error (continuing to exit): {e}")
                await asyncio.sleep(0.3)
                os._exit(0)

            self._track_task(_restart(), f"safe_restart_{reason}")

        try:
            loop.call_soon_threadsafe(_schedule)
            return True
        except Exception as e:
            logger.error(f"safe_restart scheduling failed: {type(e).__name__}: {e}")
            return False

    # ── v15.7.1: عمليات الطابور والذاكرة عند الطلب من لوحة التحكم ──────
    async def purge_all_queues(self) -> Dict[str, Any]:
        """══ حذف الطابور يدوياً (زر اللوحة) — عمليّة صريحة بإسقاط ══

        المسار:
        1) تفريغ طابور التسليم الداخلي (internal_queue) بأمان — العناصر
           المسحوبة من DB ولم تُسلَّم للعمال بعد؛ get_nowait متزامن
           على نفس حلقة البوت (الجسر ينفذ هنا) فلا سباق مع المنتج.
        2) DELETE كامل لجدول processing_queue عبر purge_processing_queue
           (تحت _queue_lock — ذرة ضد pop_batch/add_to_queue).

        العناصر القليلة العالقة بين السحبة والوضع قد تُعالج طبيعياً بعد
        الحذف إن فاتها التفريغ — تقريب مقبول لعملية تشغيلية يدوية،
        والعدّ المُعاد للوحة صادق في كل الحالات.

        ملاحظة أمان: يُنفَّذ فقط عبر dashboard API (مصادق + CSRF +
        تدقيق)، ولا يظهر في أي مسار تلقائي.
        """
        drained = 0
        q = getattr(self, "_internal_queue", None)
        if q is not None:
            try:
                while True:
                    q.get_nowait()
                    drained += 1
                    if drained >= 10000:  # حد أمان ضد سباق لا نهائي
                        break
            except asyncio.QueueEmpty:
                pass
            except Exception as e:
                logger.debug(f"purge internal_queue partial: {type(e).__name__}: {e}")
        res = await self.db.purge_processing_queue()
        total = drained + int(res.get("purged", 0))
        logger.warning(
            f"QUEUE PURGE complete: db={res.get('purged', 0)}, "
            f"in_memory={drained}, total={total}"
        )
        return {
            "db_purged": int(res.get("purged", 0)),
            "memory_drained": int(drained),
            "total": int(total),
        }

    async def manual_memory_cleanup(self) -> Dict[str, Any]:
        """══ تنظيف الذاكرة يدوياً (زر اللوحة) — المسار الخفيف الآمن ══

        يوكّل لـ memory_guard.manual_cleanup: مسح كاشات db + كاش الكيانات
        لكل مراقب + كاشات محرك الفلترة + gc.collect — بلا إغلاق DB ولا
        إلغاء مهام عابرة (هذا عمل Soft Reset الثقيل فقط). يعيد إحصاءات
        صادقة (RSS قبل/بعد، كائنات gc) تُعرض للوحة كما هي.
        """
        from memory_guard import manual_cleanup as _mc
        res = await _mc(self.db, self.monitors)
        logger.info(f"Manual memory cleanup via dashboard: {res}")
        return res

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
        # ═══ v14.5.5-diag: تثبيت بصمة الانهيار في system_health قبل الخروج —
        # تُقرأ لاحقاً من /health/full (system_health) لمعرفة سبب كل إعادة
        # تشغيل بلا حاجة لسجلات Render. فشل-آمن بالكامل.
        try:
            if bot is not None and getattr(bot, "db", None) is not None:
                await bot.db._execute(
                    "INSERT INTO system_health (component, status, details) "
                    "VALUES ('process', 'crash', ?)",
                    (f"{type(e).__name__}: {str(e)[:400]}",),
                )
        except Exception:
            pass
        sys.exit(1)
    finally:
        if bot and bot.is_running:
            await bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
