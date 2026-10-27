-- ══════════════════════════════════════════════════════════════════
-- v11.8 — Supabase RLS hardening (execution phase 5)
-- يشغَّل مرة واحدة في Supabase SQL Editor (أو عبر اتصال DATABASE_URL)
-- بعد أول ترحيل ناجح وقبل فتح أي وصول خارجي.
--
-- السياق الموثق (تقرير §RLS Design):
--  * التطبيق يتصل بـ DATABASE_URL مباشرة بدور postgres (supabase_admin /
--    postgres) — الأدوار المتميزة تتجاوز RLS بتصميم Postgres، لذلك RLS
--    هنا حزام أمان إضافي يحمي من وصول Keys API (anon/service_role عبر
--    PostgREST) لا من التطبيق نفسه.
--  * لا يعتمد التطبيق على Data API إطلاقاً (صفر مراجع SUPABASE* في
--    الكود — مُثبت بـ rg)؛ فسياسات anon تُقفل كل شيء افتراضياً.
-- ══════════════════════════════════════════════════════════════════

-- 1) RLS مفعّل على كل جداول التطبيق مع "لا سياسات" = رفض شامل لأي دور
--    خاضع لـ RLS (anon/authenticated عبر PostgREST).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'messages','alerts','sender_stats','sender_contacts','system_health',
    'blocked_senders','blocked_chats','processing_queue','dead_letters',
    'alert_dedup','spam_watch','spam_ignore','sources','rules',
    'allowed_entities','features','notifications','audit_logs',
    'dashboard_users','dashboard_accounts','filter_decisions',
    'users','group_memberships','group_messages','message_analysis',
    'user_activity','sender_access','app_settings'
  ]
  LOOP
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema='public' AND table_name = t) THEN
      EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
      EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);
    END IF;
  END LOOP;
END $$;

-- 2) منع الصلاحيات العامة على الجداول الحساسة (دفاع في العمق —
--    Revoking حتى لو أُضيفت سياسات لاحقاً بالخطأ).
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM anon, authenticated;

-- 3) حماية الأعمدة الحساسة من الظهور في أي Log/خطأ عام:
--    (session_string / api_hash تعيش في dashboard_accounts — تأكيد
--    أنها لا تُقرأ إلا من التطبيق نفسه; لا EXPOSE policy تُضاف أبداً).

-- 4) توثيق: دور الاتصال المباشر (postgres) يظل بصلاحيات كاملة —
--    أقل الصلاحيات على مستوى التطبيق تُدار عبر RBAC لوحة التحكم
--    (5 أدوات دورية موثقة في dashboard.py:398) لا على مستوى DB.
