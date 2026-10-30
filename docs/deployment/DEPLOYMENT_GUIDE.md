# DEPLOYMENT GUIDE — Telegram_BR7

## المنصة الحالية

Render (Plan Free) — نشر تلقائي من `main` (autoDeploy). العملية الواحدة تخدم: بوت المراقبة + dashboard/api + webadmin على منفذ واحد.

## متغيرات البيئة الإلزامية (الإقلاع يفشل بصدق بدونها)

| المتغير | الوظيفة |
|---|---|
| `TARGET_GROUP_ID` | المجموعة المراقبة (مثال `-100…`) |
| `ADMIN_CHAT_ID` | مشرف التنبيهات |
| `ACCOUNT_1..7_*` | API_ID/API_HASH/PHONE/SESSION_NAME/SESSION_STRING لكل حساب مراقبة |
| `ALERT_BOT_TOKEN` | بوت التنبيهات (عضو admin في المجموعة) |
| `DATABASE_URL` | Supabase PG (pooler + sslmode=require) — بدونه: SQLite مؤقت + تحذير CRITICAL |

### إضافات/تغييرات v12.2.0

| المتغير | الافتراضي | ملاحظة |
|---|---|---|
| `TRUSTED_PROXIES` | `127.0.0.0/8,10.0.0.0/8,::1/128` | يُضبط تلقائياً من render.yaml عند Blueprint sync؛ لو نشرت يدوياً أضفه من لوحة Render. بروكسيات أخرى (مثال Cloudflare) تحتاج نطاقاتها الصريحة. |

## إجراء النشر القياسي

1. `git push origin main` (بعد موافقة المالك) → Render يبني وينشر تلقائياً.
2. راقب اللوج حتى اكتمال الإقلاع: اتصال الحسابات السبعة + `webadmin mounted` + صيانة الإقلاع (purge/recover).
3. تحقق حي (بصمة النجاح):
   * `GET /health` → `{"status":"ok","database":"ok","db_healthy":true}` (200 — وإلا 503 صادق).
   * شارة اللوحة = `BR7 · v12.2.0` (تُحقن من BOT_VERSION).
   * `/health/full` → مفاتيح `unified_gate`، `dedup`، `unified` (filter_ok/...، queue_recovered...).

## ما بعد النشر — فحوص التشغيل الأولى

1. لوج `♻️ recovered ... in-flight` بعد نشر قديم = استرداد سليم (متوقع لا صفوف بعد أول نشر مستقر).
2. `webadmin dashboard not loaded` = نقص تبعية/عطل تركيب — راجع S-4 (مؤقتاً: الفحص اليدوي `/admin` يعيد 200).
3. تنبيه تجريبي فقط عبر قناة اختبار معتمدة — **لا رسائل للمجموعة الإنتاجية**.

## التراجع (Rollback)

Render → Deploys → اختر آخر commit سليم → Redeploy. بلا ترحيلات مخطط هادمة في السلسلة (كل الترحيلات idempotent إضافية)، التراجع آمن بيانياً.
