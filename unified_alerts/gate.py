#!/usr/bin/env python3
"""
unified_alerts/gate.py — بوابة التنبيه الموحدة (v12.0.0 → v15.4.1)

القرار الموحد قبل الإرسال: هل يستحق هذا الحدث تنبيهاً الآن؟

═══ v15.4.1 — نضارة اللحظة (Arrival-Freshness) — إصلاح حلقة الموت ═══

  المشكلة الحية (2026-11-22): مع طابور مشبع (2000 صف) كان العمر يُقاس
  من ختم تيليجرام الأصلي (msg_date_ts) عند السحب من الطابور — فالرسالة
  الطازجة التي انتظرت 400-900 ثانية في الطابور تُرمى عند البوابة رغم
  أنها وصلتنا طازجة. النتيجة: كل التنبيهات تُسقط دائماً ما دام التأخر
  فوق الحد (صفر تنبيهات دائم — حلقة موت: العمال يعالجون القديم
  المحكوم عليه، والجديد يتأخر أكثر).

  الدلالة الجديدة:
  * النضارة تُقرر **عند لحظة الوصول إلى نظامنا** (stage="capture" في
    معالج الحدث قبل أي enqueue): العمر عند الالتقاط = timestamp −
    msg_date_ts. الأقدم من MAX_EVENT_AGE_SECONDS عند الوصول يُسقط
    فوراً ولا يدخل الطابور أصلاً (لا صفوف DB ولا إخلاءات ولا هدر
    عمال — تحذف فيض catch_up بتكلفة صفر).
  * ما وصل طازجاً يُختم "_fresh_at_capture" ولا يُقاس عمره الأصلي
    مرة أخرى أبداً في pipeline/send — انتظار الطابور لا يعاقبه.
    المقيم الوحيد لاحقاً: سقف انتظار الطابور MAX_QUEUE_WAIT_SECONDS
    (افتراضي 600s — هجر الطابور المرضي فقط).
  * التوافق الخلفي: الأحداث بلا سمة (صفوف سابقة للترقية/استرداد قديم)
    تتبع السلوك القديم (عمر أصلي مقابل الحد) — فشل-آمن.

═══ الأساس التاريخي (v12.0.0) ═══
  * عند انقطاع طويل يعيد تيليجرام تسليم تحديثات قديمة لكل حساب مراقبة
    (catch_up=True في Telethon). الحل الأصلي: فحص العمر في أقرب نقطة
    (pipeline) + مرة عند الإرسال (send).
  * الحدود تُقرأ من CFG حياً عند كل استدعاء — تغيير البيئة وإعادة
    التشغيل يطبّقه (المصدر الرسمي بعد حذف اللوحة v14.0).

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

    # ── إعدادات حيّة (تُقرأ من CFG عند كل استدعاء — المصدر الرسمي بعد حذف اللوحة v14.0: env في Render) ──
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
    def _queue_wait_limit_seconds() -> int:
        """سقف انتظار الطابور للأحداث الطازجة عند الوصول (v15.4.1).

        صفر = السقف معطّل (الأحداث الطازجة لا تُهجر مهما طال انتظارها).
        يُقرأ حياً من CFG — MAX_QUEUE_WAIT_SECONDS، افتراضي 600s.
        """
        try:
            from config import CFG

            raw = getattr(CFG, "MAX_QUEUE_WAIT_SECONDS", 600)
            val = int(raw)
        except Exception:
            return 600
        if val < 0:
            return 0
        return min(val, _MAX_AGE_LIMIT)

    @staticmethod
    def _event_age_seconds(data: Dict[str, Any], *, end_ts: Optional[float] = None) -> Optional[float]:
        """عمر الحدث بالثواني من ختم الرسالة الأصلي — None إن غاب/فاسد.

        end_ts: نقطة القياس (افتراضي now). عند stage="capture" يُمرر
        ختم الالتقاط data["timestamp"] لقياس العمر لحظة الوصول بالضبط.
        """
        try:
            ts = data.get("msg_date_ts")
            if ts is None:
                return None
            ts_f = float(ts)
            if ts_f <= 0:
                return None
            now = end_ts if end_ts is not None else time.time()
            # حماية من وحدة ms بالخطأ: ختم ms يكون أكبر من الزمن الحالي s
            if ts_f > time.time() * 10:  # ختم ميلي‌ثانية (13+ خانة)
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

            # ══ v15.4.1 — المرحلة صفر: "capture" عند لحظة الوصول ══
            if stage == "capture":
                if limit <= 0:
                    return GateDecision(allowed=True, reason="age_gate_disabled")
                try:
                    cap_ts = float(data.get("timestamp") or 0)
                except Exception:
                    cap_ts = 0.0
                if cap_ts <= 0:
                    stats.inc("age_missing_ts")
                    return GateDecision(allowed=True, reason="no_timestamp")
                cap_age = self._event_age_seconds(data, end_ts=cap_ts)
                if cap_age is None:
                    stats.inc("age_missing_ts")
                    return GateDecision(allowed=True, reason="no_timestamp")
                if cap_age > limit + _BOUNDARY_GRACE_SECONDS:
                    stats.inc("stale_dropped_capture")
                    return GateDecision(
                        allowed=False,
                        reason="stale_at_capture",
                        meta={
                            "age_seconds": int(cap_age),
                            "limit_seconds": limit,
                            "stage": stage,
                            "chat_id": data.get("chat_id"),
                            "message_id": data.get("message_id"),
                        },
                    )
                # طازج عند الوصول — يُختم ولا يُقاس عمره الأصلي مجدداً
                try:
                    data["_fresh_at_capture"] = True
                except Exception:
                    pass
                return GateDecision(
                    allowed=True,
                    reason="fresh_by_arrival",
                    meta={"age_seconds": int(cap_age), "limit_seconds": limit},
                )

            # ══ v15.4.1 — pipeline/send للأحداث المختومة "طازج عند الوصول":
            # انتظار الطابور وحده هو المقيم — انتظار الرسالة في طابورنا
            # لا يجعلها قديمة (إصلاح حلقة الموت).
            if data.get("_fresh_at_capture"):
                wait_limit = self._queue_wait_limit_seconds()
                if wait_limit <= 0:
                    return GateDecision(allowed=True, reason="fresh_by_arrival")
                try:
                    cap_ts = float(data.get("timestamp") or 0)
                except Exception:
                    cap_ts = 0.0
                if cap_ts <= 0:
                    return GateDecision(allowed=True, reason="fresh_by_arrival")
                waited = time.time() - cap_ts
                if waited > wait_limit + _BOUNDARY_GRACE_SECONDS:
                    stats.inc("stale_dropped_queue_wait")
                    return GateDecision(
                        allowed=False,
                        reason="queue_wait_exceeded",
                        meta={
                            "age_seconds": int(waited),
                            "limit_seconds": wait_limit,
                            "stage": stage,
                            "chat_id": data.get("chat_id"),
                            "message_id": data.get("message_id"),
                        },
                    )
                return GateDecision(
                    allowed=True,
                    reason="fresh_by_arrival",
                    meta={"age_seconds": int(waited), "limit_seconds": wait_limit},
                )

            # ══ المسار القديم (صفوف سابقة للترقية/بلا سمة) — سلوك v12..v15.4.0 ══
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
        """لقطة إعدادات البوابة للقراءة في /health/full."""
        try:
            return {
                "max_event_age_seconds": self._age_limit_seconds(),
                "max_queue_wait_seconds": self._queue_wait_limit_seconds(),
                "freshness_semantics": "arrival_v15_4_1",
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
