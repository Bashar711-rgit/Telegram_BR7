#!/usr/bin/env python3
"""
unified_alerts/activity.py — نظام صيانة النشاط (Activity Maintenance System) — v14.6.0

ثلاث وظائف مستقلة تماماً عن معالجة الطلبات الحقيقية:

1. رسالة النشاط الدورية:
   عندما تمر ACTIVITY_MESSAGE_INTERVAL_SECONDS (افتراضي 90s) دون طلبات حقيقية،
   تُرسل رسالة نصية بهوية B_Bot (Bot API عبر ALERT_BOT_TOKEN) إلى قناة النشاط
   المخصصة (ACTIVITY_CHANNEL_ID) أو إلى قناة الطلبات نفسها عند التفعيل الصريح
   (ACTIVITY_MESSAGE_FALLBACK_MAIN). الرسالة مميزة بعلامة داخلية
   (monitors.is_activity_message) فلا تُحسب طلباً، ولا تدخل الكلمات المفتاحية،
   ولا تعيد معالجة نفسها (echo guard يحصرها قبل أي عدّ).

   ملاحظة تصميمية صريحة (طلب المالك §ثانياً — مهمة): هذه الرسالة تحافظ على
   نشاط الاتصال من داخل العملية لكنها *لا تمنع* Render Free من spin-down؛
   منع السبات مسؤولية keep-alive الخارجي (_keep_alive_loop يضرب /health عبر
   العنوان العام كل KEEP_ALIVE_INTERVAL_SECONDS) أو خطة مدفوعة. لا ندّعي
   العكس — راجع سجل /health/full: keep_alive قسم مستقل.

2. حارس الخدمة (كل ACTIVITY_WATCHDOG_INTERVAL_SECONDS — افتراضي 30s):
   - قياس event loop lag (انحراف sleep الدوري عن الفترة المخططة) — نافذة
     محدودة (60 عينة) بلا نمو.
   - فحص اتصال كل مراقب: أي مراقب ميت بلا ReconnectionManager حي يُسلَّح
     فوراً (لا يبقى disconnected إلى أجل غير محدد).
   - كشف توقف الطابور (queue_size ثابت > 0 وعدّاد المعالجة لا يتقدم 3 دورات).
   - تحذيرات إدارية عبر Bot API إلى ADMIN_CHAT_ID مع cooldown لكل نوع:
     * كل الحسابات متعطلة        * لا أحداث منذ ACTIVITY_IDLE_ALERT_MINUTES
     * الطابور متوقف              * الذاكرة فوق MEMORY_THRESHOLD_MB.
   * مراقبة اتصال تيليجرام لكل حساب (heartbeat get_me موجود في
     ReconnectionManager — هنا نضمن فقط ألا يبقى أي مراقب بلا حلقة مراقبة).

3. لقطة /health/full: snapshot() صادق وفشل-آمن (مفاتيح ثابتة دائماً).

قواعد الأمان:
* بلا I/O حاجب — كل الإرسالات عبر aiohttp غير الحاجب (AlertBot.send_plain).
* بلا استثناءات تُغلق الحلقة — كل قسم في try مستقل، وفشل DB/الذاكرة/الإرسال
  يُسجَّل فقط.
* رسالة النشاط لا تلمس قاعدة البيانات إطلاقاً (لا ضغط على Supabase).
* لا جلسات ولا توكنات ولا session strings في أي مخرجات.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

from loguru import logger

try:
    from config import CFG
except Exception:  # pragma: no cover — الاختبارات المعزولة فقط
    CFG = None  # type: ignore[assignment]


class ActivityWatchdog:
    """حارس النشاط — حلقة واحدة تحمل وظيفتين بإيقاعين مختلفين.

    * كل ACTIVITY_CHECK_INTERVAL_SECONDS (افتراضي 15s): بوابة رسالة النشاط
      + قياس event loop lag.
    * كل ACTIVITY_WATCHDOG_INTERVAL_SECONDS (افتراضي 30s، أي كل N دورة):
      دورة حارس الخدمة (المراقبون/الطابور/الذاكرة/التحذيرات).
    """

    _LAG_WINDOW_MAXLEN = 60

    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._started_ts: float = time.time()
        # ── النشاط الحقيقي (تحدَّثه ذرّياً معالجات الأحداث — بلا I/O) ──
        # الإقلاع نفسه نقطة مرجعية: أول رسالة نشاط بعد 90s خمول من الإقلاع.
        self._last_real_activity_ts: float = time.time()
        self._last_real_account: Optional[str] = None
        self._last_real_chat_id: int = 0
        self._real_events: int = 0
        # ── رسالة النشاط ──
        self._last_activity_msg_ts: float = 0.0
        self._activity_fail_backoff_until: float = 0.0
        self.activity_messages_sent: int = 0
        self.activity_send_failures: int = 0
        self.last_activity_message_error: Optional[str] = None
        # ── الحارس ──
        self._cycles: int = 0
        self._watchdog_cycles: int = 0
        self._last_watchdog_cycle_ts: float = 0.0
        self._lag_window: Deque[float] = deque(maxlen=self._LAG_WINDOW_MAXLEN)
        self._reconnect_arms: int = 0
        # ── كشف توقف الطابور ──
        self._last_queue_size: Optional[int] = None
        self._last_queue_progress: Optional[int] = None
        self._queue_stall_cycles: int = 0
        self.queue_stall_detected: bool = False
        # ── التحذيرات الإدارية ──
        self._admin_alerts_sent: int = 0
        self._admin_alerts_cooldown_skipped: int = 0
        self._admin_alerts_failed: int = 0
        self._alert_cooldowns: Dict[str, float] = {}

    # ═══════════════════ استدعاءات خارجية (ذرّية، بلا I/O) ═══════════════════

    def note_real_activity(self, account: str, chat_id: int, sender_id: int) -> None:
        """يستدعيه معالج أحداث المراقب فور قبول حدث جديد من مجموعة مراقَبة.

        أي رسالة جديدة (نص أو مرفق) تعني أن الاستقبال حي — تُسجَّل قبل أي
        فلترة دلالية لأن رسالة النشاط تعتمد على «لا طلبات حقيقية» وليس
        «لا تنبيهات».
        """
        self._last_real_activity_ts = time.time()
        self._last_real_account = str(account or "")
        self._last_real_chat_id = int(chat_id or 0)
        self._real_events += 1

    # ═══════════════════ الحلقة الرئيسية ═══════════════════

    async def run(self) -> None:
        """حلقة الحارس — تُشغَّل كمهمة متتبعة من main (M-05/M-16)."""
        bot = self._bot
        # الأرضية 1s فقط (حماية من سوء الضبط العنيف) — رسالة النشاط لها
        # سقف إلزامي مستقل (≥30s) فلا يوجد خطر ضرب Bot API بإيقاع الحلقة.
        check_interval = max(
            1, int(getattr(CFG, "ACTIVITY_CHECK_INTERVAL_SECONDS", 15) or 15)
        )
        watchdog_interval = max(
            check_interval,
            int(getattr(CFG, "ACTIVITY_WATCHDOG_INTERVAL_SECONDS", 30) or 30),
        )
        watchdog_every = max(1, round(watchdog_interval / check_interval))
        logger.info(
            f"ActivityWatchdog started: check={check_interval}s "
            f"watchdog={watchdog_interval}s (activity message after "
            f"{int(getattr(CFG, 'ACTIVITY_MESSAGE_INTERVAL_SECONDS', 90) or 90)}s idle)"
        )
        while getattr(bot, "is_running", False):
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            try:
                await asyncio.sleep(check_interval)
            except asyncio.CancelledError:
                break
            # قياس lag الحلقة: الانحراف عن الفترة المخططة = زمن الحجب/الازدحام
            try:
                self._lag_window.append(self._lag_ms(t0, check_interval))
            except Exception:
                pass
            self._cycles += 1
            # 1) بوابة رسالة النشاط — مستقلة تماماً عن الحارس (فشل-آمن)
            try:
                await self._maybe_send_activity_message()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(
                    f"activity message gate error (fail-safe): {type(e).__name__}: {e}"
                )
            # 2) دورة الحارس كل watchdog_every دورة فحص
            if self._cycles % watchdog_every == 0:
                self._watchdog_cycles += 1
                try:
                    await self._watchdog_cycle()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(
                        f"watchdog cycle error (fail-safe): {type(e).__name__}: {e}"
                    )

    @staticmethod
    def _lag_ms(t0: float, planned_s: float, now: Optional[float] = None) -> float:
        """انحراف sleep الفعلي عن المخطط بالمللي ثانية (≥0)."""
        actual = (
            asyncio.get_running_loop().time() if now is None else float(now)
        )
        return round(max(0.0, (actual - t0 - float(planned_s)) * 1000.0), 1)

    # ═══════════════════ رسالة النشاط ═══════════════════

    def _activity_target_chat(self) -> int:
        """قناة رسالة النشاط: المخصصة أولاً ثم قناة الطلبات عند تفعيل fallback."""
        dedicated = int(getattr(CFG, "ACTIVITY_CHANNEL_ID", 0) or 0)
        if dedicated:
            return dedicated
        if bool(getattr(CFG, "ACTIVITY_MESSAGE_FALLBACK_MAIN", True)):
            return int(getattr(CFG, "TARGET_GROUP_ID", 0) or 0)
        return 0

    async def _maybe_send_activity_message(self) -> None:
        """أرسل رسالة النشاط إذا مرت الفترة دون طلبات حقيقية — بلا سبام.

        الشروط (كلها يجب أن تتحقق):
        * ACTIVITY_MESSAGE_ENABLED و AlertBot مفعّل وهدف متاح.
        * خمول ≥ ACTIVITY_MESSAGE_INTERVAL_SECONDS منذ آخر نشاط حقيقي.
        * لم تُرسل رسالة نشاط خلال الفترة نفسها (سقف إلزامي).
        * لا نتجاوز backoff فشل (60s بعد أي فشل إرسال).
        """
        if not bool(getattr(CFG, "ACTIVITY_MESSAGE_ENABLED", True)):
            return
        target = self._activity_target_chat()
        if not target:
            return
        alert_bot = getattr(self._bot, "alert_bot", None)
        if alert_bot is None or not getattr(alert_bot, "enabled", False):
            return
        now = time.time()
        if now < self._activity_fail_backoff_until:
            return
        interval = max(
            30, int(getattr(CFG, "ACTIVITY_MESSAGE_INTERVAL_SECONDS", 90) or 90)
        )
        if (now - self._last_real_activity_ts) < interval:
            return
        if self._last_activity_msg_ts and (now - self._last_activity_msg_ts) < interval:
            return
        text = str(getattr(CFG, "ACTIVITY_MESSAGE_TEXT", "") or "").strip()
        if not text:
            return
        silent = bool(getattr(CFG, "ACTIVITY_MESSAGE_SILENT", True))
        ok, reason = await alert_bot.send_plain(text, chat_id=target, silent=silent)
        if ok:
            self._last_activity_msg_ts = now
            self.activity_messages_sent += 1
            self.last_activity_message_error = None
            logger.info(
                f"📢 activity message sent (idle {int(now - self._last_real_activity_ts)}s) "
                f"→ {target}"
            )
        else:
            self.activity_send_failures += 1
            self.last_activity_message_error = str(reason)[:160]
            # backoff مهذب — لا نضرب Bot API كل 15s عند فشل متكرر
            self._activity_fail_backoff_until = now + 60
            logger.warning(f"activity message send failed: {reason}")

    # ═══════════════════ دورة حارس الخدمة ═══════════════════

    async def _watchdog_cycle(self) -> None:
        bot = self._bot
        self._last_watchdog_cycle_ts = time.time()

        # 1) المراقبون: تسليح ReconnectionManager لأي مراقب ميت بلا حلقة حية —
        #    لا مراقب يبقى disconnected إلى أجل غير محدد (§ثانياً/ب).
        try:
            monitors = list(getattr(bot, "monitors", []) or [])
            for m in monitors:
                if getattr(m, "is_connected", False):
                    continue
                # v14.7.0: المراقب بحالة نهائية (AuthKeyDuplicated/2FA/...)
                # لا يُعاد تسليحه — الحلقة ستمر بالحالة النهائية وتموت
                # فوراً؛ التسليح الدوري هنا كان يعيد تشغيل حلقة ميتة كل
                # دورة (محاولات بلا جدوى + عدادات مضللة). إعادة الدخول
                # يدوية — انظر needs_relogin على /health/full.
                if getattr(m, "_terminal_error", None) is not None:
                    continue
                rm = getattr(m, "_reconnect", None)
                if rm is not None and not getattr(rm, "_running", False):
                    try:
                        await rm.start()
                        self._reconnect_arms += 1
                        acct = getattr(m, "account", None)
                        name = acct.get("name", "?") if isinstance(acct, dict) else "?"
                        logger.warning(
                            f"ActivityWatchdog: re-armed reconnect supervisor for {name} "
                            f"(was disconnected with no live supervisor)"
                        )
                    except Exception:
                        pass
        except Exception:
            pass

        # 2) الطابور: كشف التوقف — حجم ثابت > 0 وبلا تقدم في المعالجة 3 دورات.
        #    فحص خفيف: queue_size() واحد + عدّاد ذاكرة — بلا ضغط على DB.
        try:
            db = getattr(bot, "db", None)
            if db is not None:
                qsize = int(await db.queue_size() or 0)
                progress = self._pipeline_progress()
                if (
                    self._last_queue_size is not None
                    and qsize > 0
                    and qsize == self._last_queue_size
                    and progress is not None
                    and progress == self._last_queue_progress
                ):
                    self._queue_stall_cycles += 1
                else:
                    self._queue_stall_cycles = 0
                if self._queue_stall_cycles == 3 and not self.queue_stall_detected:
                    logger.warning(
                        f"ActivityWatchdog: queue stall suspected "
                        f"(size={qsize}, no pipeline progress for 3 cycles)"
                    )
                self.queue_stall_detected = self._queue_stall_cycles >= 3
                self._last_queue_size = qsize
                self._last_queue_progress = progress
        except Exception:
            pass

        # 3) الذاكرة — نفس عتبة MEMORY_THRESHOLD_MB (فحص psutil خفيف).
        try:
            import psutil as _psu

            rss_mb = round(
                _psu.Process().memory_info().rss / (1024 * 1024), 1
            )
            threshold = float(getattr(CFG, "MEMORY_THRESHOLD_MB", 400) or 400)
            if rss_mb and rss_mb > threshold:
                await self._admin_alert(
                    "memory_high",
                    f"⚠️ B_Bot: استهلاك الذاكرة {rss_mb}MB تجاوز الحد "
                    f"{int(threshold)}MB — memory_guard يعالج تلقائياً عند 85%/95%.",
                )
        except ImportError:
            pass
        except Exception:
            pass

        # 4) التحذيرات الإدارية الحالة (كل نوع له cooldown مستقل).
        try:
            monitors = list(getattr(bot, "monitors", []) or [])
            total = len(monitors)
            connected = sum(
                1 for m in monitors if getattr(m, "is_connected", False)
            )
            if total > 0 and connected == 0:
                await self._admin_alert(
                    "all_monitors_down",
                    f"🚨 B_Bot: جميع الحسابات ({total}) غير متصلة — المراقبة "
                    f"متوقفة. إعادة الاتصال التلقائي مسلَّح وسيستمر بالمحاولة.",
                )
            elif connected > 0:
                idle_min = float(
                    getattr(CFG, "ACTIVITY_IDLE_ALERT_MINUTES", 15) or 15
                )
                if idle_min > 0:
                    idle_s = time.time() - self._last_real_activity_ts
                    if idle_s > idle_min * 60:
                        await self._admin_alert(
                            "idle_no_events",
                            f"⚠️ B_Bot: لا أحداث واردة منذ "
                            f"{int(idle_s / 60)} دقيقة مع {connected} حساب متصل — "
                            f"تحقق من اتصال تيليجرام/نشاط المجموعات المراقَبة.",
                        )
            if self.queue_stall_detected:
                await self._admin_alert(
                    "queue_stalled",
                    f"⚠️ B_Bot: الطابور متوقف ({self._last_queue_size} حدثاً "
                    f"بلا معالجة 3 دورات) — افحص /health/full.",
                )
        except Exception:
            pass

    def _pipeline_progress(self) -> Optional[int]:
        """عدّاد تقدم خط الأنابيب (من الذاكرة — بلا DB)."""
        try:
            from unified_alerts.stats import get_unified_stats

            snap = get_unified_stats().snapshot()
            return int(snap.get("filter_ok", 0)) + int(snap.get("recovered_resend", 0))
        except Exception:
            return None

    async def _admin_alert(self, kind: str, text: str) -> None:
        """تحذير إداري عبر Bot API مع cooldown لكل نوع — فشل-آمن كلياً."""
        if not bool(getattr(CFG, "ACTIVITY_ADMIN_ALERTS_ENABLED", True)):
            return
        cooldown = max(
            60, int(getattr(CFG, "ACTIVITY_ADMIN_ALERT_COOLDOWN_SECONDS", 600) or 600)
        )
        now = time.time()
        last = self._alert_cooldowns.get(kind, 0.0)
        if (now - last) < cooldown:
            self._admin_alerts_cooldown_skipped += 1
            return
        alert_bot = getattr(self._bot, "alert_bot", None)
        admin = int(getattr(CFG, "ADMIN_CHAT_ID", 0) or 0)
        if alert_bot is None or not getattr(alert_bot, "enabled", False) or not admin:
            return
        ok, reason = await alert_bot.send_plain(text, chat_id=admin, silent=False)
        if ok:
            self._alert_cooldowns[kind] = now
            self._admin_alerts_sent += 1
            logger.warning(f"admin alert [{kind}] sent → ADMIN_CHAT_ID")
        else:
            self._admin_alerts_failed += 1
            logger.debug(f"admin alert [{kind}] failed: {reason}")

    # ═══════════════════ لقطة /health/full ═══════════════════

    def snapshot(self) -> Dict[str, Any]:
        """لقطة صادقة وفشل-آمن — مفاتيح ثابتة (بلا مفاتيح مشروطة)."""
        try:
            now = time.time()
            lags = list(self._lag_window)
            lag_snap: Dict[str, Any] = {
                "current_ms": lags[-1] if lags else None,
                "avg_ms": round(sum(lags) / len(lags), 1) if lags else None,
                "max_ms": max(lags) if lags else None,
                "window": len(lags),
            }
            return {
                "enabled": True,
                "uptime_s": int(now - self._started_ts),
                "last_real_activity_age_s": round(
                    now - self._last_real_activity_ts, 1
                ),
                "last_real_account": self._last_real_account,
                "last_real_chat_id": self._last_real_chat_id,
                "real_events_total": self._real_events,
                "activity_message": {
                    "interval_s": int(
                        getattr(CFG, "ACTIVITY_MESSAGE_INTERVAL_SECONDS", 90) or 90
                    ),
                    "sent": self.activity_messages_sent,
                    "failures": self.activity_send_failures,
                    "last_sent_age_s": (
                        round(now - self._last_activity_msg_ts, 1)
                        if self._last_activity_msg_ts
                        else None
                    ),
                    "last_error": self.last_activity_message_error,
                },
                "event_loop_lag": lag_snap,
                "watchdog": {
                    "cycles": self._watchdog_cycles,
                    "last_cycle_age_s": (
                        round(now - self._last_watchdog_cycle_ts, 1)
                        if self._last_watchdog_cycle_ts
                        else None
                    ),
                    "reconnect_arms": self._reconnect_arms,
                    "queue_stall_detected": self.queue_stall_detected,
                    "admin_alerts_sent": self._admin_alerts_sent,
                    "admin_alerts_cooldown_skipped": self._admin_alerts_cooldown_skipped,
                    "admin_alerts_failed": self._admin_alerts_failed,
                },
            }
        except Exception:
            return {"enabled": False}
