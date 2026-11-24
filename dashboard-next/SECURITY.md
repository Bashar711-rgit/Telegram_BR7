# SECURITY — BR7 Control Plane

## المصادقة

- Supabase Auth (email/password) — إنشاء المستخدمين من super_admin عبر `/settings`
- الجلسة: كوكي `br7_session` = payload JSON موقّع HMAC-SHA256 بـ`SESSION_SECRET`
  - `HttpOnly` + `Secure` + `SameSite=Strict` + `Path=/` + عمر 8 ساعات
  - الحمولة: {sub(البريد), role, exp} — الدور من التوكن للعرض فقط، القرار من DB
- لا خروج من الخادم لإبطال التوكن الخارجي: تسجيل الخروج يمحو الكوكي؛ الجلسة
  تنتهي ذاتياً بعد 8 ساعات (قرار مبسط — إبطال مركزي يتطلب جدول جلسات، مؤجل)
- فشل الدخول: 401 عام + صفر Set-Cookie + audit row (rate 5 محاولات/5 دقائق/IP)

## RBAC

| الدور | الرتبة | القدرات |
|---|---|---|
| viewer | 0 | قراءة كل الجداول المسموحة + الحالة + التدقيق |
| operator | 1 | + نشر/إعادة تشغيل Render + إعادة نشر preview |
| admin | 2 | + CRUD الجداول البيضاء + env vars + tags/rerun |
| super_admin | 3 | + إدارة المستخدمين + أسماء المفاتيح والأسرار |

- فحص الدور حي من `dashboard_users.username` عند كل استدعاء (الكتابة دائماً)
- تعطيل المستخدم (`enabled=0`) يرفض كل طلباته فوراً بـ`not_authorized`

## مفاتيح لا تلمس المتصفح

- `SUPABASE_SERVICE_ROLE_KEY` / `RENDER_API_KEY` / `VERCEL_TOKEN` / `GITHUB_TOKEN`
  / `SESSION_SECRET` / `WEBHOOK_SECRET` — كلها server-side (sensitive envs)
- فحص إلزامي بكل بناء: `grep SERVICE_ROLE|RENDER_API|GITHUB_TOKEN .next/static/`
  → يجب أن يظل صفر نتائج (مؤكد CLEAN)
- أسماء فقط: أسرار GitHub تُعرض كأسماء، env في Render تُعرض بقيم مموّهة دائماً

## CSRF

- خط 1: كوكي SameSite=Strict (المتصفح لا يرسله مع طلبات خارجية)
- خط 2: كل طلب كتابة يجب أن يرسل Origin مطابقاً للـhost وإلا `csrf_rejected`

## الترويسات (next.config)

CSP (script/style 'unsafe-inline' — إطار Next التطويري، مرشح لnonce لاحقاً)،
HSTS preload، X-Frame-Options DENY، X-Content-Type-Options، Referrer-Policy،
Permissions-Policy. صفر `dangerouslySetInnerHTML` في الكود كله.

## Rate limiting

- داخل الذاكرة (خريطة IP+مسار): login 5/5د، health 30/د، كتابة DB 30/د، نشر 5/5د…
- **حد معلوم**: على Vercel كل lambda تحفظ عدادها الخاص (per-instance) — الحد
  الفعلي أضعف من الاسمي تحت توزيع الحمل. مثبت حياً على instance واحدة
  (30×200 ثم 429). الترقية الموثقة: مخزن مشترك (Upstash Redis أو جدول DB).

## حماية Render env

- مرفوض من اللوحة نهائياً: `DATABASE_URL`, `SECRET_KEY`, `SESSION_*`
- أي env تغيير → نشر تلقائي مع تأكيد مزدوج في الواجهة

## Supabase

- RLS مفروض على 33 جدولاً (من جلسة v16.3.0) — service_role يقرأ عبر BYPASSRLS
  + منح مستهدفة additive فقط (مسجلة في audit_logs id=102)
- الجداول الحمراء: SELECT فقط. audit_logs: SELECT+INSERT (لا تعديل/حذف من اللوحة)

## نقاط ضعف معلومة (صادقة)

1. إبطال الجلسة ليس مركزياً (كوكي حتى 8 ساعات أو until logout) — يعالَج بجدول جلسات
2. Rate limit per-instance (فوق)
3. CSP يسمح 'unsafe-inline' للسكربتات بسبب Next hydration — nonce مقترح لاحقاً
4. توكن GitHub الحالي (fine-grained PAT) لا يملك صلاحية Actions rerun (403) —
   الأنواع الأخرى تعمل؛ الدوران بمفتاح بصلاحية actions:write يحل ذلك
5. لا 2FA/TOTP بعد — مرحلة مقترحة قادمة (pyotp/Supabase MFA)
