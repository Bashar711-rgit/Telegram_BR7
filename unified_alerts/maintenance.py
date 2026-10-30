#!/usr/bin/env python3
"""
unified_alerts/maintenance.py — الصيانة الذاتية (v12.0.0)

  1. purge_stale_queue_rows — حذف صفوف processing_queue الأقدم من
     QUEUE_STALE_PURGE_SECONDS (افتراضي 7200s = ساعتان). الطابور مخزن
     تشغيلي عابر (ليس بيانات مستخدم) — الصفوف القديمة فيه:
       * لن تصلح تنبيهاتها أبداً (بوابة العمر ستُسقطها عند pop)
       * تشغل سعة MESSAGE_QUEUE_SIZE وتدفع evictions تضيّر أحداثاً جديدة
     → التنظيف عند الإقلاع ودورياً يعيد الطابور لحجمه الفعال.

  2. run_startup_maintenance — نقطة واحدة تُستدعى من main.py بعد تهيئة DB:
     تنظيف الطابور + تقرير صادق بعدّاد unified_stats.

  الأمان:
     * حذف بالعمر حصراً (created_at < cutoff) — لا يمس الصفوف الحية.
     * أي فشل DB → يسجّل ويعيد 0 (لا استثناءات للإقلاع).
"""

from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from unified_alerts.stats import get_unified_stats


async def purge_stale_queue_rows(db: Any, *, max_age_seconds: Optional[int] = None) -> int:
    """حذف صفوف processing_queue الأقدم من max_age_seconds — يعيد العدد."""
    if db is None:
        return 0
    stats = get_unified_stats()
    try:
        if max_age_seconds is None:
            try:
                from config import CFG

                max_age_seconds = int(
                    getattr(CFG, "QUEUE_STALE_PURGE_SECONDS", 7200) or 7200
                )
            except Exception:
                max_age_seconds = 7200
        deleted = await db.purge_stale_queue_rows(max_age_seconds=max_age_seconds)
        deleted = int(deleted or 0)
        if deleted:
            stats.inc("queue_purged_periodic", deleted)
            logger.info(
                f"unified_alerts.maintenance: purged {deleted} stale queue rows "
                f"(older than {max_age_seconds}s)"
            )
        return deleted
    except Exception as e:
        logger.debug(f"purge_stale_queue_rows failed (fail-safe): {type(e).__name__}: {e}")
        return 0


async def run_startup_maintenance(db: Any) -> Dict[str, Any]:
    """صيانة الإقلاع — تُستدعى مرة واحدة من main.py بعد تهيئة DB.

    يعيد ملخصاً صادقاً لما نُفذ (للسجل و /health).
    """
    summary: Dict[str, Any] = {"purged_queue_rows": 0, "recovered_inflight": 0}
    try:
        purged = await purge_stale_queue_rows(db)
        summary["purged_queue_rows"] = purged
        if purged:
            stats = get_unified_stats()
            # عدّاد الإقلاع يوثق العملية منفصلة عن الدوري
            stats.inc("queue_purged_startup", purged)
            stats.inc("queue_purged_periodic", -purged)  # لا ازدواج في الدوري
            logger.info(
                f"✅ unified_alerts startup maintenance: queue purged {purged} stale rows"
            )
    except Exception as e:
        logger.debug(f"startup maintenance skipped: {type(e).__name__}: {e}")
    # ══ v12.1.0: استرداد in-flight من انهيار سابق — قبل أول pop ══
    try:
        recovered = await recover_inflight_events(db)
        summary["recovered_inflight"] = recovered
    except Exception as e:
        logger.debug(f"inflight recovery (startup) skipped: {type(e).__name__}: {e}")
    return summary


async def recover_inflight_events(db: Any, *, max_age_seconds: Optional[int] = None) -> int:
    """══ v12.1.0 — قلب التزامن الصلب ═══

    استرداد صفوف processing_queue العالقة في حالة in_flight — الحدث الذي
    سُحب (claim) ثم انهارت العملية قبل ack كان يُفقد للأبد في v≤12.0.1
    (pop=DELETE). الآن: reclaim → pending مع وسم _recovered=1 في
    event_data ليعيد خط الأنابيب بناء التنبيه وإرساله (at-least-once) —
    طبقات dedup اللاحقة تمنع الازدواج المرئي للمستخدم في الحالات الطبيعية.

    * attempts >= QUEUE_INFLIGHT_MAX_ATTEMPTS → dead_letters بصدق
      (لا دورة إعادة أبدية) + عداد queue_recovered_dead.
    * تُستدعى عند الإقلاع ودورياً (كل QUEUE_RECOVER_INTERVAL_SECONDS).
    * فشل-آمن: أي خلل DB → 0 بلا استثناءات.
    """
    if db is None:
        return 0
    stats = get_unified_stats()
    try:
        if max_age_seconds is None:
            try:
                from config import CFG

                max_age_seconds = int(
                    getattr(CFG, "QUEUE_INFLIGHT_MAX_AGE_SECONDS", 600) or 600
                )
            except Exception:
                max_age_seconds = 600
        result = await db.recover_stale_inflight(max_age_seconds=max_age_seconds)
        recovered = int((result or {}).get("recovered", 0) or 0)
        dead = int((result or {}).get("dead", 0) or 0)
        if recovered:
            stats.inc("queue_recovered", recovered)
            logger.warning(
                f"♻️ unified_alerts.inflight: recovered {recovered} in-flight "
                f"event(s) (claimed > {max_age_seconds}s — previous crash/deploy window)"
            )
        if dead:
            stats.inc("queue_recovered_dead", dead)
            logger.error(
                f"☠️ unified_alerts.inflight: {dead} in-flight event(s) exceeded "
                f"max attempts → dead_letters (honest dead-end, no infinite loop)"
            )
        return recovered
    except Exception as e:
        stats.inc("inflight_errors")
        logger.debug(f"recover_inflight_events failed (fail-safe): {type(e).__name__}: {e}")
        return 0
