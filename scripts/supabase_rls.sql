-- ══════════════════════════════════════════════════════════════════
-- v11.8.1 — Supabase RLS hardening (execution phase 5, REVISED)
-- يشغَّل مرة واحدة عبر Supabase SQL endpoint / SQL Editor
-- بعد أول إقلاع ناجح للتطبيق (الجداول موجودة) وقبل فتح أي وصول خارجي.
--
-- السياق المُحدَّث (تصحيح v11.8 الأصلي):
--  * التطبيق يتصل الآن بدور مخصص أقل صلاحيات: br7_app (ليس postgres).
--    br7_app هو مالك الجداول — وFORCE RLS يجعل المالك خاضعاً لـ RLS،
--    لذلك نُنشئ سياسة صريحة لـ br7_app (كل الصفوف) وإلا قرأ 0 صفوف.
--  * FORCE RLS + سياسة br7_app = أقوى وضع: أي دور آخر (حتى لو مُنحت
--    GRANTs بالخطأ لاحقاً) يخضع لـ RLS بلا سياسات = رفض شامل.
--  * لا يعتمد التطبيق على Data API إطلاقاً (صفر مراجع SUPABASE* في
--    الكود — مُثبت بـ rg)؛ REVOKE من anon/authenticated/service_role
--    يقفل بوابة PostgREST بالكامل (service_role يمتلك BYPASSRLS لذا
--    الـ REVOKE على مستوى الجداول هو ما يوقفه فعلاً).
-- ══════════════════════════════════════════════════════════════════

-- 1) RLS مفروض على كل جداول التطبيق + سياسة كاملة لدور التطبيق
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
    'user_activity','sender_access','sender_account_hashes','app_settings'
  ]
  LOOP
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema='public' AND table_name = t) THEN
      EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
      EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);
      -- سياسة دور التطبيق: وصول كامل (التطبيق هو الطبقة التي تُطبق
      -- قواعد العمل/RBAC — لا يتوقع أي تصفية صفوف على مستوى DB).
      EXECUTE format('DROP POLICY IF EXISTS br7_app_full ON public.%I', t);
      EXECUTE format(
        'CREATE POLICY br7_app_full ON public.%I FOR ALL TO br7_app USING (true) WITH CHECK (true)', t);
    END IF;
  END LOOP;
END $$;

-- 2) منع الصلاحيات العامة على الجداول (دفاع في العمق — يشمل
--    service_role الذي يتجاوز RLS بسمة BYPASSRLS).
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated, service_role;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM anon, authenticated, service_role;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated, service_role;

-- 3) منع إنشاء جداول جديدة من الأدوار العامة (br7_app فقط له CREATE).
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- 4) توثيق: دور الاتصال (br7_app) مالك الجداول بسياسة كاملة؛
--    أقل الصلاحيات على مستوى التطبيق تُدار عبر RBAC لوحة التحكم
--    (5 أدوار دورية موثقة في dashboard.py:398) لا على مستوى DB.
