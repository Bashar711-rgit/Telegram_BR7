# BR7 Control Plane — dashboard-next

لوحة تحكم موحدة على Vercel تربط **GitHub + Render + Supabase + Vercel** في مركز واحد لمشروع Telegram_BR7.

- Next.js 15 (App Router) + React 19 + TypeScript strict + Tailwind — RTL عربي
- الموقع الفعلي في المستودع: `dashboard-next/` — Vercel يبنيه بـ rootDirectory

## التشغيل المحلي

```bash
cd dashboard-next
npm install
# أنشئ .env.local (متجاهل من git) بالمتغيرات المطلوبة — انظر جدول البيئة
npm run dev   # http://localhost:3000
npm run build # تحقق البناء + التحقق النمطي
```

## النشر

- الربط: GitHub `Bashar711-rgit/Telegram_BR7` ← Vercel مشروع `telegram-br-7`
- فرع الإنتاج: `main` | الفرع العامل: `feature/vercel-ui` (Previews تلقائية عند كل push)
- **الدمج إلى main يتم فقط بموافقة صريحة من المالك** — دفع main = نشر إنتاج

## المتغيرات البيئية (كلها مضبوطة على Vercel)

| المتغير | النوع | ملاحظة |
|---|---|---|
| NEXT_PUBLIC_SUPABASE_URL | عام | رابط المشروع |
| NEXT_PUBLIC_SUPABASE_ANON_KEY | عام | للتحقق من الدخول فقط |
| SUPABASE_SERVICE_ROLE_KEY | سري | server-side حصراً — لا يصل للمتصفح أبداً |
| GITHUB_TOKEN / GITHUB_OWNER / GITHUB_REPO | سري/عام | PAT المستودع |
| RENDER_API_KEY / RENDER_SERVICE_ID / RENDER_URL | سري | srv-dbhptf0ae00c73ed9m7g |
| VERCEL_TOKEN / VERCEL_PROJECT_ID | سري | لإدارة النشرات |
| SESSION_SECRET | سري | توقيع جلسة HMAC |
| WEBHOOK_SECRET | سري | رأس x-br7-webhook-secret |

## الصفحات

| المسار | الوظيفة | أدنى صلاحية |
|---|---|---|
| `/` | Overview — 4 بطاقات حية + زر النشر | viewer |
| `/database` + `/database/[entity]` | CRUD على Supabase (بيضاء/حمراء) | viewer / admin+ للكتابة |
| `/render` | حالة الخدمة + النشرات + محرر env + Deploy/Restart | viewer / operator+ |
| `/github` | Releases + CI Runs + Commits + إنشاء Tag | viewer / admin+ للوسم |
| `/deploy` | النشر الشامل (tag → CI → Render → health → audit) | operator+ |
| `/audit` | سجل التدقيق الموحد + بحث + تصدير CSV | viewer |
| `/settings` | المستخدمون (Supabase Auth + dashboard_users) + حالة المفاتيح | super_admin |
| `/login` | دخول عبر Supabase Auth | عام |

## الـAPI

كل المسارات تحت `/api/*` وتعيد `{ok, data?, error?}` بلا استثناءات، مع:
- جلسة HMAC موقعة (كوكي `br7_session`: HttpOnly + Secure + SameSite=Strict، 8 ساعات)
- فحص دور حي من `dashboard_users` عند كل استدعاء (تعطيل المستخدم يسري فوراً)
- CSRF: SameSite=Strict + تحقق Origin على كل طلب كتابة
- Rate limiting داخل الذاكرة (per-instance — انظر SECURITY.md)
- Audit إلزامي لكل كتابة على Render/Supabase بـbefore/after مموّه
- timeout 15s + retry واحد على القراءات الخارجية

## الصيانة

- تدوير المفاتيح: Vercel Dashboard → Settings → Environment Variables ثم إعادة نشر
- تعطيل مستخدم: `/settings` → تعطيل (يسري فوراً على كل مسارات الـAPI)
- حماية env في Render: `DATABASE_URL` و`SECRET_KEY` و`SESSION_*` مرفوضة من اللوحة
- سجل التدقيق: جدول `audit_logs` (سياسة الاحتفاظ 90 يوماً كما للبوت)
