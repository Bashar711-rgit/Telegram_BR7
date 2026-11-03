-- ============================================================================
-- migrations/0004_remove_dashboard.sql — v14.0
-- إزالة لوحة التحكم نهائياً (طلب المالك): حذف جداولها وصفوف إعداداتها.
--
-- ما الذي يُحذف ولماذا؟
--   dashboard_users     — مستخدمو اللوحة (RBAC). بلا لوحة = بلا فائدة.
--   dashboard_accounts  — الحسابات المضافة من اللوحة. جلسات الحسابات الفعلية
--                         تبقى في متغيرات البيئة على Render (المصدر الدائم).
--   filter_decisions    — سجل «معمل الفلترة» في اللوحة؛ مستهلكه الوحيد
--                         كان لوحة التحكم.
--   app_settings (صفوف مختارة) — تجاوزات الإعدادات التي كانت تكتبها اللوحة.
--                         بعد الحذف تعود القيم إلى بيئة Render (المصدر الرسمي).
--                         لا يُحذف الجدول نفسه: نواة البوت تستخدمه
--                         (negative_memory / reply_fix).
--
-- ⚠️ آمن للتشغيل المتكرر (idempotent): DROP IF EXISTS + DELETE الشرطي.
-- ⚠️ لا يلمس أي بيانات تشغيلية: messages / alerts / sender_contacts /
--    spam_watch / sources / rules / allowed_entities / audit_logs … كلها تبقى.
--
-- طريقة التطبيق على الإنتاج (Supabase):
--   Supabase Dashboard → SQL Editor → الصق هذا الملف كاملاً → Run.
--   أو: psql "$DATABASE_URL" -f migrations/0004_remove_dashboard.sql
-- ============================================================================

-- 1) جداول اللوحة ------------------------------------------------------------
DROP TABLE IF EXISTS filter_decisions;
DROP TABLE IF EXISTS dashboard_accounts;
DROP TABLE IF EXISTS dashboard_users;

-- 2) صفوف إعدادات اللوحة داخل app_settings (يُحتفظ بالجدول) --------------------
DELETE FROM app_settings WHERE key = 'keywords_json';
DELETE FROM app_settings WHERE key IN (
    'target_group_id', 'admin_chat_id',
    'max_alerts_per_minute', 'max_alerts_per_hour', 'alert_cooldown',
    'alert_with_buttons', 'alert_with_copy_button', 'alert_max_text_len',
    'alert_show_score',
    'dedup_enabled', 'dedup_window_seconds',
    'antispam_enabled', 'antispam_watch_duration_seconds',
    'antispam_burst_messages', 'antispam_burst_window_seconds',
    'antispam_similarity_threshold', 'antispam_similarity_window_seconds',
    'antispam_direct_spam_groups',
    'antispam_confirm_messages', 'antispam_confirm_groups',
    'confidence_accept_threshold', 'confidence_review_threshold',
    'negation_enabled', 'fuzzy_matching_enabled',
    'distance_scoring_enabled', 'ad_detection_enabled',
    'language_filter', 'spam_score_threshold',
    'max_message_length', 'min_message_length',
    'prefilter_enabled', 'prefilter_max_emojis',
    'fast_capture_enabled', 'sender_intel_enabled',
    'memory_threshold_mb', 'message_queue_size', 'db_batch_interval',
    'stats_interval', 'health_check_interval', 'cleanup_interval',
    'dead_letter_max_retries', 'dead_letter_cleanup_days',
    'retention_messages_days', 'retention_audit_days',
    'retention_notifications_days',
    'settings_applied', 'settings_stored',
    'keywords_restored', 'keywords_skipped_same'
);

-- 3) تحقق (اختياري) — يجب أن يُرجع 0 لكل منها ---------------------------------
-- SELECT count(*) FROM information_schema.tables
--  WHERE table_schema = 'public'
--    AND table_name IN ('dashboard_users','dashboard_accounts','filter_decisions');
-- SELECT count(*) FROM app_settings WHERE key = 'keywords_json';
