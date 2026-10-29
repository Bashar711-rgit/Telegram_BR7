-- ═══════════════════════════════════════════════════════════════════
-- 0002_unified_alerts.sql — v12.0 unified_alerts (2025)
--
-- تطوير قاعدة البيانات المرافق لحزمة unified_alerts:
--   1. فهرس created_at على alerts — استعلامات /health واللوحة والإحصاء
--      الحديثة تستخدم created_at (كانت تمر بـseq scan على 311+ صف وتنمو).
--   2. فهرس مركب alert_status + created_at — استعلامات "ماذا فشل
--      متى؟" (mark_alert_dispatched status=failed/unknown تشخيصاً).
--   3. فهرس sender_id + created_at على alerts — تتبع تنبيهات مرسل
--      معيّن (لوحة المراسلين) بدون فرز كامل الجدول.
--
-- تنبيهات التشغيل:
--   * كلها CREATE INDEX IF NOT EXISTS — idempotent، آمنة لإعادة التشغيل.
--   * CONCURRENTLY غير مستخدم هنا (تنفذها database.py عند الإقلاع ضمن
--     معاملة؛ الجداول صغيرة حالياً والقفل اللحظي مقبول على Free tier).
--   * ANALYZE اختياري بعد التطبيق لتحديث إحصاء المخطط (خارج معاملة).
--
-- لا حذف بيانات — لا تغيير أعمدة — فهارس فقط.
-- ═══════════════════════════════════════════════════════════════════

-- 1) الإحصاء الحديث والصحة: ORDER BY created_at DESC
CREATE INDEX IF NOT EXISTS idx_alr_created_at
    ON public.alerts (created_at DESC);

-- 2) تشخيص حالات الإرسال (failed/unknown/delivered) زمنياً
CREATE INDEX IF NOT EXISTS idx_alr_status_created
    ON public.alerts (alert_status, created_at DESC);

-- 3) لوحة المراسلين: تنبيهات مرسل معيّن حديثة أولاً
CREATE INDEX IF NOT EXISTS idx_alr_sender_created
    ON public.alerts (sender_id, created_at DESC);

-- 4) تنظيف alert_dedup أسرع على النافذة المنزلقة (يدعم cleanup_alert_fingerprints)
CREATE INDEX IF NOT EXISTS idx_dedup_last_seen
    ON public.alert_dedup (last_seen);
