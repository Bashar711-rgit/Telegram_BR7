# SECURITY AUDIT — جلسة v12.2.0

النطاق: حسب أولويات §11 من المواصفة مع ما يمكن التحقق منه بلا وصول إنتاجي كتابي.

## 1. المصادقة

| البند | الحالة |
|---|---|
| مقارنة بيانات الدخول | `hmac.compare_digest` في `webadmin/auth.py::verify_credentials` ✓ (زمن ثابت) |
| قفل الدخول (webadmin) | `_LoginGuard` — حد محاولات + lockout، مفتاح IP عبر المُحلّل الموحد ✓ |
| قفل توكن اللوحة (dashboard) | `_AuthGuard` — عتبة/نافذة/قفل أُسّي بسقف + مسح عند نجاح ✓ — **جذر XFF أُصلح (S-1)** |
| جلسات webadmin | كوكي موقّعة itsdangerous (`URLSafeTimedSerializer`) + `max_age` + httponly + samesite=lax + secure تلقائي خلف https ✓ |
| CSRF | `csrf_protect` على الطفرات بمقارنة `compare_digest` مع رمز جلسة ✓ |
| توكنات مستخدمي RBAC | موقعة base64 payload.signature (dashboard) + صلاحيات مركزية ✓ |

## 2. عنوان العميل (المواصفة §11 النصية)

**أُصلح هذا الجلسة (S-1)**: المصدر الوحيد `security.resolve_client_ip`:
* نظير غير موثوق → XFF يُتجاهل كلياً (لا ترويش ولا ترويش-هروب).
* نظير موثوق → أقصى يمين غير موثوق (ما يضعه البروكسي).
* `TRUSTED_PROXIES` صريح يلغي الافتراضي (loopback+10/8) — موثق في render.yaml.
* WebSocket guard يستخدم `websocket.client.host` مباشرة (لا يمكن تزويره) ✓.

## 3. التفويض

* كل مسارات الإدارة خلف `login_required`/`csrf_protect` أو حارس Bearer — مغطاة باختبارات (test_rbac/test_rbac_expansion/test_webadmin: 114 PASS).
* حجب الصلاحيات على الخادم فقط؛ الأدوار (super_admin/admin/supervisor/viewer) مفحوصة سلبياً وإيجابياً.
* نقطة WebSocket: مصادقة قبل الترقية + رسالة رفض موقعة (لوگو فقط، بلا أسرار).

## 4. الأسرار

* المستودع: فحص diff الجلسة نظيف — صفر أسرار؛ `sessions/` و`accounts.env` خارج التتبع.
* السجلات: مسارات التنبيهات تُلطّف PII (نمط مؤسس في worklog v11.5+)؛ لا توكنات في اللوج.
* **تذكير موروث للمالك**: توكنات (GitHub PAT، Render API key، جلسات تيليجرام، توكن بوت التنبيهات، DATABASE_URL) شاركت في محادثات سابقة — **التدوير على المالك** (خارج نطاق التنفيذ الآلي حسب §2).

## 5. سطح الويب

* CORS: `allow_origins=['*']` + credentials أُزيل سابقاً (تحذير v12 حي عند غياب DASHBOARD_CORS_ORIGINS) ✓.
* ReDoS: كل regex المسارات الساخنة عبر `security.safe_*_async` (thread-pool + سقف طول + مهلة) ✓.
* رفع ملفات/نسخ احتياطية: مسار backups محروس ضد path-traversal (اختبارات test_webadmin موجودة) ✓.
* ترويش HTML للتنبيهات: escape-first + عقد بطاقة `unified_alerts/card.py` (وسوم متوازنة/قص UTF-16/أزرار نظيفة) ✓.

## 6. ثغرات لم تُعالج (بسبب/توثيق)

| البند | السبب |
|---|---|
| S-4 (mount_admin صامت عند نقص تبعية) | توصية /health/full — ترشيح موثق، لم يُنفذ لتقييد نطاق الجلسة |
| تدوير الأسرار المكشوفة تاريخياً | قرار مالك حصراً (§2) |
| تشفير SESSION_STRING في حالة السكون | موجود جزئياً (cryptography/Fernet في شجرة الاعتماديات) — مراجعة شاملة لمسار التخزين تحتاج جلسة مستقلة، موثقة كترشيح |
