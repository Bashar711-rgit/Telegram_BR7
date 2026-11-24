-- ══════════════════════════════════════════════════════════════════
-- v16.3.0 — Supabase RLS hardening (تجديد scripts/supabase_rls.sql)
-- يشغَّل مرة واحدة عبر Supabase SQL Editor أو بأي اتصال بصلاحية
-- تعديل الجداول (br7_app مالك الجداول — يكفي) بعد إقلاع ناجح للتطبيق.
--
-- الفرق عن نسخة v11.8.1:
--  * تغطية جداول v16.x الأربعة الجديدة: panel_users, panel_sessions,
--    panel_accounts, telegram_usernames_history (كانت خارج القائمة).
--  * تغطية الأسماء القديمة إن وجدت (dashboard_users/dashboard_accounts/
--    dashboard_sessions/filter_decisions) — ترحيل v16.1.0 كان إضافياً
--    فبقيت جداول قديمة محتملة على الإنتاج.
--  * Idempotent بالكامل: آمن للتشغيل المتكرر (DROP POLICY IF EXISTS +
--    فحص وجود الجدول قبل كل ALTER).
--  * نفس العقد: FORCE RLS + سياسة كاملة لـ br7_app (المالك خاضع لـ RLS
--    بإصراره، فلا بد من سياسة صريحة وإلا قرأ 0 صفوف).
--
-- بعد التشغيل: أي دور آخر (anon/authenticated/service_role) يُرفض
-- على مستوى GRANT وRLS معاً — بوابة PostgREST مغلقة بالكامل.
-- ══════════════════════════════════════════════════════════════════

DO $$
DECLARE t text;
BEGIN
  -- جداول التطبيق الحالية (v16.3.0 — 30 جدولاً كما ينشئها database.py)
  FOREACH t IN ARRAY ARRAY[
    'messages','alerts','sender_stats','sender_contacts','system_health',
    'blocked_senders','blocked_chats','processing_queue','dead_letters',
    'alert_dedup','spam_watch','spam_ignore','sources','rules',
    'allowed_entities','features','notifications','audit_logs',
    'panel_users','panel_sessions','panel_accounts',
    'telegram_usernames_history',
    'users','group_memberships','group_messages','message_analysis',
    'user_activity','sender_access','sender_account_hashes','app_settings',
    -- أسماء قديمة إن وجدت (v15.x وأقدم) — تُغطى إن وجدت فقط
    'dashboard_users','dashboard_accounts','dashboard_sessions',
    'filter_decisions'
  ]
  LOOP
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema='public' AND table_name = t) THEN
      EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
      EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);
      EXECUTE format('DROP POLICY IF EXISTS br7_app_full ON public.%I', t);
      EXECUTE format(
        'CREATE POLICY br7_app_full ON public.%I FOR ALL TO br7_app USING (true) WITH CHECK (true)', t);
    END IF;
  END LOOP;
END $$;

-- منع الصلاحيات العامة (دفاع في العمق — يشمل service_role ذي BYPASSRLS)
-- محمول: يُنفَّذ فقط على الأدوار الموجودة فعلاً (Supabase يملكها كلها؛
-- PG خام قد لا يملكها — الفشل على دور غائب لا يلغي الباقي).
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', r);
      EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM %I', r);
      EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', r);
    END IF;
  END LOOP;
END $$;

-- منع إنشاء جداول جديدة من الأدوار العامة (br7_app فقط له CREATE)
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- توثيق: دور الاتصال (br7_app) مالك الجداول بسياسة كاملة؛ أقل الصلاحيات
-- على مستوى التطبيق تُدار عبر RBAC لوحة التحكم لا على مستوى DB.
