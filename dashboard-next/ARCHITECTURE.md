# ARCHITECTURE — BR7 Control Plane

## الموقع

```
Telegram_BR7 (monorepo)
├── بوت Python (Render: telegram-br7-7oyu.onrender.com / srv-dbhptf0ae00c73ed9m7g)
├── dashboard/assets (الـSPA القديمة — تُخدم من Render وVercel/main)
└── dashboard-next/   ← لوحة التحكم الجديدة (Next.js 15) — rootDirectory على Vercel
```

- Vercel مشروع `telegram-br-7`: الإنتاج من `main` (الـSPA القديمة حالياً)؛ Previews من `feature/vercel-ui` تبني `dashboard-next/`.
- عند الدمج الموافق عليه، الإنتاج ينتقل للوحة الجديدة.

## الطبقات

```
Browser (RTL SPA، صفر مفاتيح)
   │  كوكي br7_session (HttpOnly/Secure/Strict)
   ▼
Next.js API Routes (Vercel Functions, node)
   │  withApi: session HMAC → RBAC حي من DB → CSRF origin → rate limit → {ok,data,error}
   ├── lib/supabase.ts  → Supabase PostgREST بخدمة service_role (منح مستهدفة فقط)
   ├── lib/render.ts    → Render API v1 (نشر/env/restart)
   ├── lib/github.ts    → Octokit (releases/tags/runs/commits/secrets-أسماء)
   ├── lib/vercel.ts    → Vercel API (نشرات/إعادة نشر preview)
   └── lib/audit.ts     → كتابة audit_logs (قبل/بعد مموّه)
   ▼
Supabase PG 17 (public schema، RLS مفروض — service_role BYPASSRLS + منح أعمدة/جداول مستهدفة)
```

## قرارات معمارية

1. **جلسة موقعة ذاتياً بدل كوكيز @supabase/ssr الافتراضية**: Supabase Auth للتحقق من
   كلمة المرور فقط؛ ثم نصدر كوكي HMAC بخصائص مضبوطة حرفياً (HttpOnly/Strict/Secure).
   السبب: ضبط SameSite=Strict وHttpOnly بدقة وبلا مفاجآت طبقة ssr.
2. **RBAC من DB في كل طلب**: الدور يُقرأ حياً من `dashboard_users` — تعطيل/تخفيض
   الدور يسري فوراً حتى لو كانت الجلسة صالحة.
3. **PostgREST + منح مستهدفة** بدل اتصال Postgres مباشر: مفاتيح لا تبدأ دور
   postgres، والمنح additive على قوائم بيضاء حصراً.
4. **الجداول الحمراء قراءة-فقط على مستوى الكود** (messages/alerts/filter_decisions) —
   الرفض 403 قبل أي لمسة للـDB.
5. **إخفاء الأعمدة الحساسة**: `panel_accounts.api_hash_encrypted/session_encrypted`
   غير قابلة للـSELECT من الـAPI أصلاً (قائمة أعمدة مسموحة لكل كيان).
6. **أسرار Render كـ"sensitive"** على Vercel، و`NEXT_PUBLIC_` للرابط والanon فقط.
7. **إعادة النشر على Vercel محصورة على previews** — الإنتاج خارج نطاق اللوحة عمداً.
8. **unstable_cache (20s, tags: overview)** لبيانات الـOverview — إعادة تعبئة تلقائية.
9. **Webhooks** على `/api/webhooks/{render,vercel}` برأس سر مشترك `x-br7-webhook-secret`
   — نقاط الاستقبال جاهزة والربط الخارجي (Render/Vercel Dashboard) مؤجل لعهدة المالك.

## تدفق /deploy

صفحة `/deploy` تنفذ تسلسلاً حياً: إنشاء tag على GitHub (رأس main) → مراقبة CI →
تشغيل نشر Render → استقصاء حتى live (10 دقائق حد) → فحص /health للبوت → تحقق
قيود التدقيق. ملاحظة صادقة: Render ينشر رأس `main` دائماً — الـtag علامة إصدار
ليس محرك نشر.

## مخطط audit_logs

| العمود | الاستخدام |
|---|---|
| actor | بريد المستخدم أو webhook:render / webhook:vercel |
| action | db_insert_rules, render_env_upsert, github_tag_created, ... |
| object_type / object_id | الكيان ومعرّفه |
| old_value / new_value | JSON مموّه (كل قيمة نصية → •••••(len:N)) |
| source | vercel-dashboard / webhook:* / botpanel (البوت) |
| ip / user_agent / request_id | سياق الطلب |
