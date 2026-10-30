#!/usr/bin/env python3
"""
unified_alerts/stats.py — العدادات الموحدة (v12.0.0)

عدادات singleton لكل قرارات «التنبيه الموحد». تُقرأ من:
  - /health/full (عبر unified_snapshot)
  - لوحة التحكم (نفس اللقطة عبر dedup-style snapshot)
  - سجل الرصد الدوري

قواعد:
  - بلا I/O — زيادة العدادات عملية ذرّية في الـevent loop (نمط _inc_stat).
  - بلا استثناءات — snapshot يلتقط أي خلل ويعيد قاموساً صالحاً دائماً.
  - المفاتيح ثابتة دائماً (بلا مفاتيح مشروطة) لتوازن اللوحات.
"""

from __future__ import annotations

import threading
from typing import Any, Dict

# ═══ v12.1.0 ═══

_LOCK = threading.Lock()

# العدادات الأساسية — مفاتيح ثابتة (تظهر دائماً في snapshot حتى بصفر)
_BASE: Dict[str, int] = {
    # بوابة العمر — إسقاطات في مرحلة pipeline (أقرب نقطة، قبل الإثراء)
    "stale_dropped_pipeline": 0,
    # بوابة العمر — إسقاطات في مرحلة send (الحدث اعتق في الطابور)
    "stale_dropped_send": 0,
    # أحداث بلا ختم زمني (msg_date_ts) — مرت فشل-آمن
    "age_missing_ts": 0,
    # أخطاء بوابة العمر (فشل-آمن: المسار مفتوح)
    "gate_errors": 0,
    # عقد البطاقة
    "card_valid": 0,
    "card_repaired": 0,
    "card_dropped": 0,
    "card_check_errors": 0,
    # الصيانة
    "queue_purged_startup": 0,
    "queue_purged_periodic": 0,
    # ══ v12.1.0 التزامن الصلب (claim/ack/recover) ══
    # أحداث استُرجعت من in-flight بعد انهيار (أعيدت للطابور ثم عولجت)
    "queue_recovered": 0,
    # صفوف in-flight تجاوزت محاولاتها → dead_letters (بصدق لا دورة أبدية)
    "queue_recovered_dead": 0,
    # تنبيهات أُعيد إرسالها فعلاً عبر مسار المسترد (كانت ستُفقد)
    "recovered_resend": 0,
    # أحداث مستردة وصلها إيصال تنبيه سابق (أُرسلت قبل الانهيار) — تخطٍّ صادق
    "recovered_dupe_skip": 0,
    # أخطاء ack/recover (فشل-آمن)
    "inflight_errors": 0,
    # ══ v12.1.0 سياسة الفلترة الموحدة ══
    "filter_ok": 0,
    "filter_too_short": 0,
    "filter_too_long": 0,
    "filter_invalid": 0,
    "policy_errors": 0,
}


class UnifiedAlertStats:
    """حاوية عدادات موحدة — نفس نمط AlertDeduplicator._stats."""

    def __init__(self) -> None:
        self._counters: Dict[str, int] = dict(_BASE)

    def inc(self, key: str, amount: int = 1) -> None:
        try:
            with _LOCK:
                self._counters[key] = self._counters.get(key, 0) + int(amount)
        except Exception:
            pass

    def snapshot(self) -> Dict[str, Any]:
        try:
            with _LOCK:
                base = dict(_BASE)
                base.update(self._counters)
                return base
        except Exception:
            return dict(_BASE)


_singleton = UnifiedAlertStats()


def get_unified_stats() -> UnifiedAlertStats:
    """يعيد singleton العدادات — مشترك بين كل المراقبين والوحدات."""
    return _singleton


def unified_snapshot() -> Dict[str, Any]:
    """لقطة قراءة-فقط لـ /health/full و dashboard — لا ترمي استثناءات."""
    try:
        snap = _singleton.snapshot()
        snap["enabled"] = True
        snap["version"] = "12.1.0"
        return snap
    except Exception:
        out = dict(_BASE)
        out["enabled"] = False
        return out
