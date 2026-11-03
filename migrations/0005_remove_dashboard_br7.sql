-- ============================================================================
-- migrations/0005_remove_dashboard_br7.sql — v14.1
-- تكملة «No Dashboard»: حذف جداول لوحة التحكم الخمسة من مخطط br7 الثانوي.
--
-- الخلفية:
--   0004_remove_dashboard.sql حذف جداول اللوحة من المخطط public (المخطط
--   الأساسي للبوت). هذا الملف يكمل العملية على مخطط br7 التجريبي الثانوي
--   الذي كانت أنشئت فيه جداول RBAC خاصة باللوحة.
--
-- ما الذي يُحذف ولماذا؟
--   dashboard_users, dashboard_roles, dashboard_permissions,
--   dashboard_user_roles, dashboard_role_permissions
--   — بلا لوحة تحكم لا وجود لمستهلكيها. CASCADE ضروري لإسقاط قيود FK
--   التابعة على br7.system_settings.updated_by و br7.audit_logs.actor_user_id
--   (الأعمدة نفسها تبقى، القيد فقط يُسقط).
--
-- ⚠️ يبقى كما هو: br7.filter_decisions — ليست جدول لوحة هنا، بل جزء من
--    مسار الفلترة في هذا المخطط (br7.alerts.decision_id تشير إليها).
-- ⚠️ لا يلمس المخطط public إطلاقاً (الرسائل 107,405+ محمية).
-- ⚠️ آمن للتشغيل المتكرر (idempotent): DROP IF EXISTS.
-- ⚠️ بعد التشغيل: أعد تشغيل scripts/apply_br7_schema.py --validate-only
--    للتأكد أن كل الفحوصات خضراء (العُدّات محدّثة: 39 جدولاً).
-- ============================================================================

-- 0) سقوط مشغّل التدقيق المرتبط بجدول سيُحذف (قبل حذف الجدول) ---------------
DROP TRIGGER IF EXISTS trg_br7_audit_user_roles ON br7.dashboard_user_roles;

-- 1) جداول RBAC الخاصة باللوحة (ترتيب آمن للتبعيات + CASCADE لقيود FK) ------
DROP TABLE IF EXISTS br7.dashboard_role_permissions CASCADE;
DROP TABLE IF EXISTS br7.dashboard_user_roles CASCADE;
DROP TABLE IF EXISTS br7.dashboard_permissions CASCADE;
DROP TABLE IF EXISTS br7.dashboard_roles CASCADE;
DROP TABLE IF EXISTS br7.dashboard_users CASCADE;

-- 2) تحقق (اختياري) — يجب أن يُرجع 0 ------------------------------------------
-- SELECT count(*) FROM information_schema.tables
--  WHERE table_schema = 'br7'
--    AND table_name LIKE 'dashboard_%';
