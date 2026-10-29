#!/usr/bin/env python3
"""
unified_alerts/gate.py — بوابة التنبيه الموحدة (v12.0.0)

القرار الموحد قبل الإرسال: هل يستحق هذا الحدث تنبيهاً الآن؟

بوابة العمر (الجوهر):
  * عند انقطاع طويل يعيد تيليجرام تسليم تحديثات قديمة لكل حساب مراقبة
    (catch_up=True في Telethon). كانت هذه الأحداث تعبر كامل خط الأنابيب
    (إثراء + DB + تحليل + بناء) ويُرسل عنها تنبيهات لطلبات انتهت منذ ساعات
    (قياس حي 2025: total_lag p50 ≈ 56,224,042ms ≈ 15.6 ساعة).
  * الحل: فحص العمر في أقرب نقطة (stage="pipeline" — قبل الإثراء وقبل أي
    DB write) + مرة أخيرة عند الإرسال (stage="send" — الحدث قد يعتق وهو
    في processing_queue أثناء الازدحام).
  * مصدر العمر: data["msg_date_ts"] (ختم الإقلاع الأصلي للرسالة بالثواني،
    يُلصق في _event_to_dict). غيابه = فشل-آمن (مرور) + عداد صادق.

  * الحد MAX_EVENT_AGE_SECONDS يُقرأ من CFG حياً عند كل استدعاء — تعديله
    من اللوحة/البيئة يسري فوراً بلا إعادة تشغيل. الافتراضي 1800s (30 دقيقة).

فشل-آمن:
  * أي استثناء داخل البوابة → allowed=True (المسار مفتوح) + عداد
    gate_errors — لا تُفقد تنبيهات صالحة بسبب خلل في الحزمة أبداً.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from loguru import logger

from unified_alerts.stats import get_unified_stats

# الحد الأقصى المعقول لتجنب قيم سخيفة (0 = معطّل؛ 86400 = يوم كامل كحد أعلى)
_MIN_AGE_LIMIT = 0
_MAX_AGE_LIMIT = 86400 * 7
# هامش الحد الشامل: قرارات على الحد بالضبط يجب ألا تنقلب بين استدعاءين
# يفرق بينهما ميكروثانية (القياس يحدث لحظتين مختلفتين) — ثانية واحدة
# تجعل الحد عملياً شاملاً ومستقراً (نفس روح شمولية حدود فلتر 6..65).
_BOUNDARY_GRACE_SECONDS = 1.0


@dataclass(slots=True)
class GateDecision:
    """نتيجة قرار البوابة الموحدة."""

    allowed: bool
    reason: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


class UnifiedAlertGate:
    """بوابة العمر الموحدة — تقييم متزامن خالص (بلا I/O، بلا await).

    الاستخدام:
        dec = get_alert_gate().evaluate_event(data, stage="pipeline")
        if not dec.allowed:
            return  # الحدث قديم — مُسقط بصدق ومُحصى
    """

    # ── إعدادات حيّة (تُقرأ من CFG عند كل استدعاء — اللوحة تغيّرها فوراً) ──
    @staticmethod
    def _age_limit_seconds() -> int:
        try:
            from config import CFG

            raw = getattr(CFG, "MAX_EVENT_AGE_SECONDS", 1800)
            val = int(raw)
        except Exception:
            return 1800
        if val < _MIN_AGE_LIMIT:
            return 0
        return min(val, _MAX_AGE_LIMIT)

    @staticmethod
    def _event_age_seconds(data: Dict[str, Any]) -> Optional[float]:
        """عمر الحدث بالثواني من ختم الرسالة الأصلي — None إن غاب/فاسد."""
        try:
            ts = data.get("msg_date_ts")
            if ts is None:
                return None
            ts_f = float(ts)
            if ts_f <= 0:
                return None
            # حماية من وحدة ms بالخطأ: ختم ms يكون أكبر من الزمن الحالي s
            now = time.time()
            if ts_f > now * 10:  # ختم ميلي‌ثانية (13+ خانة)
                ts_f = ts_f / 1000.0
            age = now - ts_f
            return age if age >= 0 else 0.0
        except Exception:
            return None

    # ── التقييم ────────────────────────────────────────────────────────────
    def evaluate_event(self, data: Dict[str, Any], *, stage: str = "pipeline") -> GateDecision:
        """قرار بوابة العمر — متزامن، فشل-آمن، مُحصى.

        stage: "pipeline" (بداية _process_event_pipeline) أو "send"
        (بداية _send_alert). المرحلتان تحسبان في عدادات منفصلة لصدق الرصد.
        """
        stats = get_unified_stats()
        try:
            limit = self._age_limit_seconds()
            if limit <= 0:
                # الحد صفر = البوابة معطلة بالكامل (خيار تشغيلي صريح)
                return GateDecision(allowed=True, reason="age_gate_disabled")

            age = self._event_age_seconds(data)
            if age is None:
                stats.inc("age_missing_ts")
                # فشل-آمن: بلا ختم زمني لا نُسقط تنبيهاً صالحاً محتملاً
                return GateDecision(allowed=True, reason="no_timestamp")

            if age > limit + _BOUNDARY_GRACE_SECONDS:
                key = (
                    "stale_dropped_pipeline"
                    if stage == "pipeline"
                    else "stale_dropped_send"
                )
                stats.inc(key)
                return GateDecision(
                    allowed=False,
                    reason="stale_event",
                    meta={
                        "age_seconds": int(age),
                        "limit_seconds": limit,
                        "stage": stage,
                        "chat_id": data.get("chat_id"),
                        "message_id": data.get("message_id"),
                    },
                )

            return GateDecision(
                allowed=True,
                reason="fresh",
                meta={"age_seconds": int(age), "limit_seconds": limit},
            )
        except Exception as e:  # فشل-آمن مطلق
            stats.inc("gate_errors")
            logger.debug(f"unified gate error (fail-open): {type(e).__name__}: {e}")
            return GateDecision(allowed=True, reason="gate_error")

    def snapshot(self) -> Dict[str, Any]:
        """لقطة إعدادات البوابة للقراءة في /health واللوحة."""
        try:
            return {
                "max_event_age_seconds": self._age_limit_seconds(),
                "age_missing_ts_pass": True,
            }
        except Exception:
            return {"max_event_age_seconds": None}


# ── Singleton مشترك (نمط dedup.get_deduplicator) ─────────────────────────────
_gate: Optional[UnifiedAlertGate] = None


def get_alert_gate() -> UnifiedAlertGate:
    """يعيد بوابة التنبيه الموحدة (singleton)."""
    global _gate
    if _gate is None:
        _gate = UnifiedAlertGate()
    return _gate
