"""
memory_guard.py — RSS Enforcement Layer v14.5.1 (HARDENED EDITION)

المشكلة التي يحلها هذا الملف:
  * Render Free/Starter = 512MB (لكل عملية). البوت مع 6 حسابات Telethon
    + طبقة NLP + طبقات الكاش قد يتجاوز 400-500MB، وحين يتخطى حد الحاوية
    يقتل Render العملية بعلامة OOM دون أي فرصة لتنظيف مكتوب — فيفقد
    الطابور الدائم حالة in_flight ويبدأ دورة إعادة تشغيل صاخبة.
  * MemoryMonitor في main.py طبقة تشخيص فقط (log + tracemalloc + gc) —
    لا يتخذ أي قرار. هذا الملف هو طبقة الإنفاذ: يراقب RSS كل 30 ثانية
    ويتصاعد: تحذير عند 70%، Soft Reset عند 85%، Hard Reset عند 95%.

التصميم — ثلاث عتبات فوق سقف واحد:
  * السقف: MEMORY_LIMIT_MB (افتراضي 480MB — تحت سقف الحاوية بهامش).
  * 70%: logger.warning مرة واحدة (rate-limit 5 دقائق بين التحذيرات).
  * 85% — SOFT RESET (بلا فقدان بيانات):
      إلغاء المهام العابرة (task_util.cancel_all) → تفريغ batch الكتابة
      (db._flush) → إغلاق DB (db.close) → مسح كل الكاشات في db و monitors
      → gc.collect() → إعادة اتصال (db.connect). القسم الحرج
      (إغلاق→مسح→إعادة اتصال) محمي بـ asyncio.shield + قفل asyncio.Lock
      حتى لا يُترك DB مقفلاً لو أُلغيت مهمة المراقبة في منتصف العملية.
  * 95% — HARD RESET (عملية جديدة):
      علامة في system_health (component='memory', status='hard_reset')
      → db._flush بحد 3 ثوانٍ → db.close بحد 3 ثوانٍ → os._exit(1).
      Render يعيد تشغيل العملية تلقائياً — الذاكرة تُسترد بالكامل.

فشل-آمن:
  * أي خطأ في أي خطوة يسجل ولا يرفع — مهمة المراقبة لا تموت أبداً
    بسبب فشل إعادة تعيين (الدورة التالية تعيد المحاولة).
  * psutil غير متوفر؟ RSS من resource.getrusage (Linux) — لا تعطيل.
  * ملاحظة T3.2: علم _accepting_events غير موجود في المشروع (لا يوجد
    مستقبل أحداث بمثل هذا العلم) — الخطوة المذكورة في المواصفة سُجّلت
    هنا كغير قابلة للتطبيق وأُسقطت من التنفيذ عمداً.

ملاحظة T4 (تسريبات المهام):
  * مهمة المراقبة تُنشأ بـ asyncio.create_task لكن مرجعها محفوظ في
    _guard_task (عالمي بالوحدة) وتُلغى عبر stop_memory_guard() في
    main.stop() قبل db.close() — لا تسريب ولا مهمة يتيمة.
  * عمداً ليست عبر task_util.spawn_bg: الـSoft Reset ينادي
    task_util.cancel_all() بنفسها، ولو كانت مهمة المراقبة في _tasks
    لألغت نفسها في منتصف إعادة التعيين (db مغلق بلا إعادة اتصال).

v14.5.1 — الجزء T3 من بوابة الاستقرار (memory guard + DB hygiene).
"""

from __future__ import annotations

import asyncio
import gc
import os
import time
from typing import Any, List, Optional

import psutil
from loguru import logger

__version__ = "14.5.1"

# ═══════════════════════════════════════════════════════════════════════
# العتبات (كلها كنسب من السقف)
# ═══════════════════════════════════════════════════════════════════════
MEMORY_CHECK_INTERVAL = 30          # ثانية بين كل فحص RSS
MEMORY_REPORT_EVERY = 300           # سطر MEM: الحالة كل 5 دقائق
MEMORY_WARN_RATIO = 0.70            # تحذير عند 70%
MEMORY_SOFT_RATIO = 0.85            # Soft Reset عند 85%
MEMORY_HARD_RATIO = 0.95            # Hard Reset عند 95%
MEMORY_WARN_COOLDOWN = 300          # 5 دقائق بين تحذيرات 70%
_MEMORY_LIMIT_MB_DEFAULT = 480

# قفل إعادة التعيين — يمنع تداخل Soft Reset مع دورة أخرى
_RESET_LOCK = asyncio.Lock()

# مرجع المهمة المحفوظ (منع GC + نقطة الإلغاء النظيف)
_guard_task: Optional[asyncio.Task] = None
_guard_started_at: float = 0.0


def _limit_mb() -> int:
    """السقف بالـMB — قابل للتجاوز عبر MEMORY_LIMIT_MB (افتراضي 480)."""
    try:
        return max(256, int(os.getenv("MEMORY_LIMIT_MB", "") or _MEMORY_LIMIT_MB_DEFAULT))
    except Exception:
        return _MEMORY_LIMIT_MB_DEFAULT


def _rss_mb() -> float:
    """RSS الحالي بالـMB — psutil أولاً ثم resource (Linux) ثم 0."""
    try:
        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        pass
    try:
        import resource
        # ru_maxrss: KiB على Linux — قيمة أقصى لا لحظية، تقريب مقبول في
        # بيئة بلا psutil (أفضل من صفر مطلق).
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return 0.0


class MemoryGuard:
    """حارس الذاكرة — دورة واحدة، عتبات ثلاثة، إجراءان (soft/hard)."""

    def __init__(self, db: Any, monitors_ref: Any = None) -> None:
        self._db = db
        # مرجع حي لقائمة المراقبين (تُملأ بعد start_memory_guard في
        # initialize — القائمة نفسها تُمرر بالمرجع فيتزاها المتغير لاحقاً)
        self._monitors_ref = monitors_ref if monitors_ref is not None else []
        self._last_report = 0.0
        self._last_warn = 0.0
        self._soft_resets = 0
        self._hard_resets = 0

    # ── الحلقة الرئيسية ────────────────────────────────────────────────
    async def run_loop(self) -> None:
        limit_mb = _limit_mb()
        soft_mb = limit_mb * MEMORY_SOFT_RATIO
        hard_mb = limit_mb * MEMORY_HARD_RATIO
        warn_mb = limit_mb * MEMORY_WARN_RATIO
        logger.info(
            f"memory_guard started: check every {MEMORY_CHECK_INTERVAL}s, "
            f"limit {limit_mb}MB (warn {warn_mb:.0f} / soft {soft_mb:.0f} / hard {hard_mb:.0f})"
        )
        while True:
            try:
                await asyncio.sleep(MEMORY_CHECK_INTERVAL)
            except asyncio.CancelledError:
                logger.info("memory_guard loop cancelled — stopping cleanly")
                raise

            rss = _rss_mb()
            now = time.monotonic()

            # سطر الحالة الدوري — دائماً بمستوى info كل 5 دقائق
            if now - self._last_report >= MEMORY_REPORT_EVERY:
                self._last_report = now
                logger.info(f"MEM: {rss:.1f}MB / limit {limit_mb}MB")

            try:
                if rss >= hard_mb:
                    await self._hard_reset(f"{rss:.1f}MB >= {hard_mb:.0f}MB")
                    return  # os._exit لا يعود أصلاً — احتياط إن فشل
                if rss >= soft_mb:
                    logger.warning(
                        f"MEM: {rss:.1f}MB >= soft threshold {soft_mb:.0f}MB — soft reset scheduled"
                    )
                    await self._soft_reset()
                    continue
                if rss >= warn_mb:
                    # rate-limit: تحذير واحد كل 5 دقائق كحد أقصى
                    if now - self._last_warn >= MEMORY_WARN_COOLDOWN:
                        self._last_warn = now
                        logger.warning(
                            f"MEM: {rss:.1f}MB >= 70% of {limit_mb}MB — watching trend"
                        )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # فشل-آمن: أي خطأ في الإجراء لا يموت به مهمة المراقبة
                logger.error(f"memory_guard action error: {type(e).__name__}: {e}")

    # ── Soft Reset (85%) — بلا فقدان بيانات ────────────────────────────
    async def _soft_reset(self) -> None:
        async with _RESET_LOCK:
            self._soft_resets += 1
            logger.critical(
                f"MEMORY SOFT RESET triggered (#{self._soft_resets}): flushing, "
                "closing DB, clearing caches, reconnecting"
            )
            # الخطوة 1 من المواصفة (علم _accepting_events): غير موجود في
            # المشروع — سُجّل في docstring الملف وأُسقط عمداً.

            # الخطوة 2: إلغاء المهام العابرة (كتب write-behind معلقة إلخ).
            # هذه المهمة ليست في task_util._tasks عمداً — انظر docstring T4.
            try:
                import task_util
                n = await task_util.cancel_all(timeout=5.0)
                if n:
                    logger.info(f"memory_guard soft reset: cancelled {n} transient task(s)")
            except Exception as e:
                logger.warning(f"memory_guard soft reset: cancel_all skipped: {type(e).__name__}: {e}")

            # القسم الحرج: إغلاق → مسح → إعادة اتصال. محمي بـshield حتى لا
            # يبقى DB مقفلاً لو أُلغيت هذه المهمة في منتصف الطريق.
            core = asyncio.ensure_future(self._soft_reset_core())
            try:
                await asyncio.shield(core)
            except asyncio.CancelledError:
                logger.warning(
                    "memory_guard: guard task cancelled mid-soft-reset — "
                    "waiting (bounded 10s) for reconnect to finish"
                )
                try:
                    await asyncio.wait_for(asyncio.shield(core), timeout=10.0)
                except Exception:
                    logger.error(
                        "memory_guard: reconnect completion failed after cancel — "
                        "DB layer self-heals on next write; hard reset may follow"
                    )
                raise

    async def _soft_reset_core(self) -> None:
        # الخطوة 3: تفريغ batch الكتابة المعلق إلى القرص قبل الإغلاق
        try:
            await self._db._flush()
        except Exception as e:
            logger.warning(f"memory_guard soft reset: pre-close flush skipped: {type(e).__name__}: {e}")
        # الخطوة 4: إغلاق DB (يُلغي writer/backup tasks داخلياً)
        try:
            await self._db.close()
        except Exception as e:
            logger.warning(f"memory_guard soft reset: db.close error: {type(e).__name__}: {e}")
        # الخطوة 5: مسح كاشات db + monitors (المواصفة — خطوة 6 فيها)
        self._clear_caches()
        # الخطوة 6: استرداد الذاكرة من الحلقات الميتة
        gc.collect()
        # الخطوة 7: إعادة اتصال — إن فشل نسجل ولا نرفع (الدورة التالية
        # ستعيد المحاولة؛ db.connect يبني pool/conn من جديد)
        try:
            ok = await self._db.connect()
            if ok:
                logger.info("MEMORY SOFT RESET complete — DB reconnected")
            else:
                logger.error("MEMORY SOFT RESET: db.connect returned False — retry on next cycle")
        except Exception as e:
            logger.error(f"MEMORY SOFT RESET: reconnect failed: {type(e).__name__}: {e}")

    def _clear_caches(self) -> None:
        db = self._db
        # كاشات قابلة للاستدعاء clear() (dict/deque)
        for attr in (
            "message_cache",
            "_blocked_senders_cache",
            "_blocked_chats_cache",
            "_can_send_alert_cache",
            "_features_cache",
        ):
            try:
                obj = getattr(db, attr, None)
                if obj is not None and hasattr(obj, "clear"):
                    obj.clear()
            except Exception:
                pass
        # كاشات tuple-or-None (تُبنى من جديد عند أول قراءة)
        for attr in ("_sources_cache", "_rules_cache", "_allowed_cache"):
            try:
                if hasattr(db, attr):
                    setattr(db, attr, None)
            except Exception:
                pass
        # كاش الكيانات في كل مراقب (أكبر مستهلك بعد Telethon نفسه) —
        # تحت قفل المراقب حتى لا تُقرأ أثناء المسح
        try:
            monitors: List[Any] = list(self._monitors_ref or [])
        except Exception:
            monitors = []
        for mon in monitors:
            try:
                cache = getattr(mon, "_entity_cache", None)
                if cache is None:
                    continue
                lock = getattr(mon, "_cache_lock", None)
                if lock is not None and lock.locked():
                    # أحد المهام يحمل القفل الآن — لا تنظيف هذا الدورة
                    # (clear() متزامن بلا await، ونسخة الكاش تُبنى من
                    # get_entity عند أول حاجة بعد المسح)
                    continue
                cache.clear()
            except Exception:
                pass

    # ── Hard Reset (95%) — عملية جديدة عبر إعادة تشغيل Render ──────────
    async def _hard_reset(self, reason: str) -> None:
        self._hard_resets += 1
        logger.critical("MEMORY HARD RESET — exiting for supervisor restart")
        db = self._db
        # علامة في system_health إن أمكن (component='memory', status='hard_reset')
        try:
            execute = getattr(db, "_execute", None)
            if callable(execute):
                await execute(
                    "INSERT INTO system_health (component, status, details) "
                    "VALUES ('memory', 'hard_reset', ?)",
                    (f"rss exceeded hard threshold: {reason}",),
                )
        except Exception as e:
            logger.debug(f"memory_guard: system_health marker skipped: {type(e).__name__}: {e}")
        # تفريغ بحد 3 ثوانٍ — لا أكثر
        try:
            flush = getattr(db, "_flush", None)
            if callable(flush):
                await asyncio.wait_for(flush(), timeout=3.0)
        except Exception as e:
            logger.debug(f"memory_guard: hard reset flush skipped: {type(e).__name__}: {e}")
        # إغلاق بحد 3 ثوانٍ — لا أكثر
        try:
            close = getattr(db, "close", None)
            if callable(close):
                await asyncio.wait_for(close(), timeout=3.0)
        except Exception as e:
            logger.debug(f"memory_guard: hard reset close skipped: {type(e).__name__}: {e}")
        # Render يعيد تشغيل العملية تلقائياً بعد الخروج
        os._exit(1)


def _on_guard_done(task: asyncio.Task) -> None:
    """استرجاع الاستثناء من مهمة المراقبة (لا "Task exception was never
    retrieved") + تفريغ المرجع العالمي عند الانتهاء."""
    global _guard_task
    if _guard_task is task:
        _guard_task = None
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except (asyncio.CancelledError, asyncio.InvalidStateError):
        return
    if exc is not None:
        logger.opt(exception=exc).error("memory_guard loop raised unexpectedly")


async def start_memory_guard(db: Any, monitors_ref: Any = None) -> bool:
    """تشغيل حارس الذاكرة (استدعاء واحد من main.initialize بعد db.connect).

    يعيد True إن بدأ فعلاً، False إن كان يعمل أصلاً أو psutil مفقود
    والـRSS غير قابل للقياس إطلاقاً (لا معنى لحارس أعمى).
    """
    global _guard_task, _guard_started_at
    if _guard_task is not None and not _guard_task.done():
        logger.debug("memory_guard already running — start request ignored")
        return False
    if _rss_mb() <= 0.0:
        logger.warning("memory_guard NOT started: RSS measurement unavailable (no psutil/resource)")
        return False
    _guard_started_at = time.monotonic()
    guard = MemoryGuard(db, monitors_ref)
    _guard_task = asyncio.ensure_future(guard.run_loop(), loop=asyncio.get_running_loop())
    _guard_task.set_name("memory_guard")
    _guard_task.add_done_callback(_on_guard_done)
    return True


async def stop_memory_guard() -> None:
    """إلغاء نظيف — يُستدعى من main.stop() قبل db.close()."""
    global _guard_task
    task = _guard_task
    _guard_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
    except asyncio.CancelledError:
        pass
    except asyncio.TimeoutError:
        logger.warning("memory_guard did not stop within 5s — leaving to loop cleanup")
    except Exception as e:
        logger.debug(f"memory_guard stop: {type(e).__name__}: {e}")
    logger.info("memory_guard stopped")
