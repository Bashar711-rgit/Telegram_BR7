-- v17.1.0 — منح قراءة للوحة التحكم على 3 جداول (يعمل به مالك النظام في Supabase SQL Editor)
-- السياق: اللوحة تستخدم service_role، لكن المنح على مستوى Postgres مقصود ومقيّد
-- (فلسفة القائمة البيضاء §1.1). هذه الجداول الثلاثة للقراءة فقط من اللوحة:
--   sender_stats (إحصائيات المرسلين) · blocked_chats (المحادثات المحظورة) · notifications (الإشعارات)
-- بعد التنفيذ: الصفحات /database/sender_stats · /database/blocked_chats · /database/notifications تعمل فوراً.

GRANT SELECT ON TABLE public.sender_stats   TO service_role;
GRANT SELECT ON TABLE public.blocked_chats  TO service_role;
GRANT SELECT ON TABLE public.notifications  TO service_role;

-- تحقق سريع بعد التنفيذ (يجب أن يعيد صفاً واحداً على الأقل أو صفراً بلا خطأ):
-- SELECT count(*) FROM sender_stats;
