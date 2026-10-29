#!/usr/bin/env python3
"""
unified_alerts — التنبيهات الموحدة v12.0.0 (v12-unified-gate)

الحزمة المركزية الوحيدة التي يمر عبرها كل تنبيه قبل الوصول للمستخدم.
تعيد بناء عقد «التنبيه الموحد» فوق أساس v11.8.2 القائم:

  المشاكل التي تعالجها (بلاغ الإنتاج الحقيقي):
    1. «سبام»: بعد انقطاع طويل (حساب Render معلّق) يُعيد كل حساب تيليجرام
       تسليم تحديثات قديمة (catch-up) — كانت تُعالج كرسائل جديدة وترسل
       تنبيهات لطلبات عمرها ساعات (قياس حي: total_lag p50 ≈ 15.6 ساعة).
       → بوابة العمر (gate.py) تُسقط أي حدث أقدم من MAX_EVENT_AGE_SECONDS
         في أقرب نقطة ممكنة (قبل الإثراء وقبل DB وقبل الإرسال).
    2. «تكرار»: طبقات dedup القائمة (early-delivery + msg_hash + fingerprint
       + similarity) تبقى كما هي — الحزمة تضيف فحص العمر في نقطة الإرسال
       نفسها (الحدث قد يعتّق وهو في الطابور) وتوحّد عدادات كل الطبقات.
    3. «تنبيه لم يُبنَ صحيحاً»: عقد البطاقة (card.py) — كل بطاقة تُفحص
       قبل الإرسال: نص غير فارغ، حد UTF-16 لتيليجرام (4096)، وسوم HTML
       متوازنة، أزرار صالحة — مع إصلاح ذاتي (قص آمن) أو إسقاط صادق.

  المبادئ (نفس عقد الكود الحالي):
    - فشل-آمن: أي خلل في الحزمة يفتح المسار ولا يُسقط تنبيهات صالحة.
    - بلا I/O في المسار الساخن: قراءة CFG حيّة فقط (تتغير من اللوحة فوراً).
    - أرقام صادقة: كل قرار يُحصى في stats.py ويظهر في /health/full.

  الوحدات:
    gate.py        — UnifiedAlertGate: بوابة العمر (نقطتا pipeline/send).
    card.py        — عقد سلامة البطاقة + الإصلاح الذاتي.
    stats.py       — عدادات موحدة (singleton) للقراءة من /health واللوحة.
    maintenance.py — تنظيف الطابور العالق عند الإقلاع ودورياً.
"""

from __future__ import annotations

from unified_alerts.gate import (
    GateDecision,
    UnifiedAlertGate,
    get_alert_gate,
)
from unified_alerts.card import (
    CardReport,
    validate_and_repair_card,
)
from unified_alerts.stats import (
    get_unified_stats,
    unified_snapshot,
)
from unified_alerts.maintenance import (
    purge_stale_queue_rows,
    run_startup_maintenance,
)

__version__ = "12.0.0"

__all__ = [
    "GateDecision",
    "UnifiedAlertGate",
    "get_alert_gate",
    "CardReport",
    "validate_and_repair_card",
    "get_unified_stats",
    "unified_snapshot",
    "purge_stale_queue_rows",
    "run_startup_maintenance",
    "__version__",
]
