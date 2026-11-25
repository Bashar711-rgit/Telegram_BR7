# Bot Control Center — المعمارية وعقود التكامل الدقيقة

> وثيقة المرحلة الحالية (Vercel فقط). ممنوع: تعديل Render/Supabase/محرك Python/Telethon/قاعدة الإنتاجية.
> القاعدة الذهبية: كل وظيفة إما **LIVE** (مرتبطة بمصدر حقيقي مُتحقق منه) أو **CONTRACT** (واجهة + عقد دقيق + الاعتماد المفقود موثق) — لا يوجد أي أمر وسط ولا أي نجاح وهمي.

---

## 1) خريطة القدرات الفعلية (مُتحقق منها حياً — PHASE 0)

### 1.1 جدول Supabase — الصلاحيات الفعلية لـ service_role (فحص REST حي)

| الجدول | قراءة | كتابة | الاستخدام في الوحدة |
|---|---|---|---|
| panel_accounts | ✓ (منح قائم) | UPDATE فقط (createDisabled) | صفحة الحسابات |
| sources | ✓ | CRUD | صفحة المصادر |
| rules | ✓ | CRUD | صفحة القواعد + التوجيه |
| blocked_senders | ✓ | CRUD | صفحة الحظر |
| allowed_entities | ✓ | CRUD | صفحة الحظر |
| app_settings | ✓ (value مموّه لغير المدراء) | CRUD (KV) | الإعدادات + ops + الكلمات + الملفات |
| messages | ✓ | — (أحمر) | القراءة فقط |
| alerts | ✓ | — (أحمر) | التنبيهات + معدل نجاح الإشعارات |
| filter_decisions | ✓ | — (أحمر) | جودة التحليل |
| audit_logs | ✓ | INSERT (نظام التدقيق) | سجل العمليات |
| dashboard_users | ✓ | UPDATE (نظام الصلاحيات) | إدارة المستخدمين القائمة |
| **البقية (system_health, processing_queue, dead_letters, alert_dedup, sender_stats, blocked_chats, spam_watch, spam_ignore, message_analysis, features, notifications, users, sender_contacts, group_memberships)** | ✗ 403 (لا منح) | ✗ | CONTRACT — يتطلب منح قراءة في مرحلة لاحقة |
| **غير الموجودة أصلاً (bot_commands, bot_runtime_state, keyword_categories, keyword_synonyms, filter_profiles, routing_rules, notification_destinations, processing_jobs, bot_metrics, message_feedback, account_health)** | — | — | CONTRACT — مخطط مقترح في §4، لا يُنفذ في هذه المرحلة |

### 1.2 محرك البوت على Render — الاستهلاك الفعلي (فحص كود قراءة فقط)

- `dashboard:ops` = `{"filtering_paused": bool, "alerts_paused": bool}` في app_settings:
  - المسار الحارس يفحص العلمين **لكل رسالة** (monitors.py:2330 و2893) لكن من الذاكرة.
  - العلم يُمَدّ من app_settings **عند الإقلاع فقط** (`hydrate_dashboard` في dashboard/api.py).
  - إذن: كتابة `dashboard:ops` من اللوحة = **حقيقي ومثابر** (يُطبق فوراً عند أي إقلاع/إعادة تشغيل للخدمة؛ التطبيق الفوري على العملية الحارة يتطلب منفذ تنفيذ على Render — عقد §3.1).
- `dashboard:keywords_json`: سجل الكلمات الكامل (20 مجموعة: request, indirect_request, urgency, negation, spam_patterns, intent_verbs, academic_objects, dialect_mapping...). المحرك يقرؤه عند الإقلاع ويكتب ملف المخصصة ويعيد بناء المحرك + FastGate. الكتابة من اللوحة = حقيقية ومثابرة، التطبيق الحي يتطلب reload منفذ على Render (عقد §3.2).
- `/health/full` حي ويعيد: status, checks.database, monitors_up/total, main_client, queue_size, filter_processed/valid/accepted/review, uptime, fast_capture.*, sender_intel.* (flood_wait_count, flood_wait_remaining, average_resolve_latency, resolve success/failure), mention_intel.*, sender_contact.* — كل مقاييس صفحة الأداء الحية.

---

## 2) تصنيف الوظائف (LIVE / CONTRACT) — الحقيقة الكاملة

### LIVE — تنفيذ حقيقي في هذه المرحلة
| الوظيفة | الآلية الحقيقية | الملاحظة الدقيقة |
|---|---|---|
| حالة المحرك (Running/Degraded/Stopped/Error) | /health + /health/full + Render service API — اشتقاق حالي وليس قيمة مخزنة | §5 |
| إعادة تشغيل المحرك | Render restart endpoint (restartService موجودة ومجربة) | حي |
| إيقاف/استئناف المعالجة (مثابر) | كتابة dashboard:ops.filtering_paused + audit | يُطبق عند إقلاع المحرك — مُعلن في الواجهة |
| إيقاف/استئناف الإرسال (مثابر) | كتابة dashboard:ops.alerts_paused + audit | نفس الملاحظة |
| CRUD المصادر + إضافة متعددة + استيراد/تصدير CSV | /api/db/sources القائم | سطح منح مُعتمد سابقاً |
| CRUD القواعد | /api/db/rules | حي |
| CRUD الحظر/المسموحين | /api/db/blocked_senders, allowed_entities | حي |
| سجل الكلمات (قراءة/إضافة/حذف/بحث/تكرار/مرادفات ضمن المجموعات) | /api/bot/keywords فوق dashboard:keywords_json | كتابة حقيقية — تطبيق حي يتطلب reload (§3.2) |
| إعدادات المحرك KV + ملفات Profile (حفظ/مقارنة/استعادة) | app_settings (أسرار مموّهة) | تطبيق الملف على المحرك = CONTRACT |
| تفاصيل الحسابات (قراءة) + تعديل الاسم/الأولوية/المراقبة/الحالة | /api/db/panel_accounts | الحالة تشمل الفتح/الإيقاف الدائم |
| قراءة التنبيهات + معدل نجاح الإشعارات + الأسباب | alerts (granted) | إحصاء حقيقي من الصفوف |
| قراءة قرارات الفلترة + إحصاءات الجودة | filter_decisions (granted) | Precision/Recall تُعرض «غير متاح» دون بيانات مرجعية — لا اختراع |
| سجل الأوامر والتدقيق | audit_logs + سجل أومام داخلي | أدلة كاملة |
| أداء حي | /health/full + Render + alerts | CPU/Memory غير متاحة من Render API → CONTRACT |
| حساب المالك الجديد + تغيير كلمة المرور الذاتي | Supabase Auth admin API + dashboard_users + audit | بأمر صريح من المالك — إضافي فقط |

### CONTRACT — واجهة جاهزة + عقد دقيق، الاعتماد المفقود موثق (لا نتيجة وهمية)
| الوظيفة | الاعتماد المفقود | العقد |
|---|---|---|
| Start/Stop فوري، Pause/Resume فوري، Maintenance، Emergency Stop فوري، Reload Config حي | منفذ أوامر على Render (مستهلك bot_commands أو نقطة لوحة البوت) | §3.1 |
| إضافة حساب (هاتف→OTP→2FA→جلسة مشفرة)، حذف، إعادة مصادقة، اختبار اتصال | تدفق Telethon تفاعلي على Render | §3.3 |
| فحص وصول المصدر، إعادة اتصال مصدر | نقطة محرك | §3.4 |
| مقعد اختبار التحليل (تحليل رسالة حقيقي) | نقطة محرك | §3.5 |
| إعادة تحليل/تصحيح تصنيف/تعليم FP-FN | جدول message_feedback + نقطة محرك | §3.6 |
| Hot-reload للكلمات/القواعد/الملفات على العملية الحارة | منفذ تنفيذ | §3.2 |
| إعادة محاولة مهام/تنظيف طابور/DLQ | processing_queue/dead_letters + منح + منفذ | §3.7 |
| CPU/Memory/مقاييس نظام | system_health منح + نقطة مقاييس | §3.7 |

---

## 3) العقود الدقيقة للتكامل المستقبلي (Backend على Render)

### §3.1 عقد منفذ الأوامر — `POST /admin/api/commands` على خدمة Render
```json
// Request (من Vercel، مع توكن خدمة مشترك x-br7-command-secret)
{ "command_id": "uuid-v4", "type": "start|stop|restart|pause|resume|reload_config|maintenance_on|maintenance_off|emergency_stop_send|resume_send",
  "requested_by": "email", "params": {}, "issued_at": "ISO8601" }
// Response 200
{ "accepted": true, "state": "processing", "detail": "..." }
// دلالات إلزامية: Idempotency على command_id، Timeout 30s، Ack قبل التنفيذ،
// الحالة النهائية تُكتب في bot_commands (عند إنشائه) أو تُرجَع في الاستجابة.
```

### §3.2 عقد Hot-Reload — `POST /admin/api/reload_registry`
```json
{ "scope": "keywords|rules|profile", "source": "app_settings:dashboard:keywords_json" }
// يُعيد بناء المحرك + FastGate (schedule_rebuild) ويُرجع {rebuilt:true, words:N, ms:T}
```

### §3.3 تدفق الحسابات (Telethon تفاعلي) — v16.6.0: منفذ حياً
```
# بوابة الخدمة على المحرك (Render) — سر مشترك x-br7-command-secret (ENGINE_COMMAND_SECRET):
POST /service/accounts/link_start     {phone}          → {ok, op:{op_id, state:"waiting_code", flood_wait?}}
POST /service/accounts/link_code      {op_id, code}    → {ok, op:{state:"ready"|"waiting_password"|...}}
POST /service/accounts/link_password  {op_id, password}→ {ok, op:{state:"ready", account_row_id}}
POST /service/accounts/link_cancel    {op_id}          → {ok, cancelled}
GET  /service/accounts/link_status?op_id=…             → {ok, op}
POST /service/accounts/row/delete     {row_id}         → حذف صف + إيقاف المراقب (super_admin في اللوحة)
POST /service/accounts/row/toggle     {row_id, enable} → spawn/stop فوري على الحلقة الرئيسية

# الوسيط في اللوحة (Vercel): /api/bot/accounts/link (POST start|code|password|cancel + GET status)
# و/api/bot/accounts/row — جلسة HMAC + RBAC admin/super_admin + CSRF + Zod strict + audit بلا أسرار.
```
قيود المنفذ: OTP/كلمة المرور تعيشان في ذاكرة link_manager حصراً ولا تُسجَّلان في أي audit،
Session مشفرة Fernet (SECRET_KEY_OVERRIDE إلزامي للدم عبر النشرات) ولا تعود للواجهة أبداً.
المتبقي عقداً: اختبار اتصال دوري مجدول + إعادة مصادقة آلية (الإصلاح اليدوي: حذف ثم إعادة إضافة).

### §3.4 عقد المصادر — `POST /admin/api/sources/{chat_id}/check` → {reachable, member_status, error?}
### §3.5 عقد مقعد الاختبار — `POST /admin/api/engine/analyze`
```json
// Request {text, source_id?} — يمر على المحرك الحقيقي بكل مراحله
// Response: {normalized, detected_keywords[], intent, context, scores:{intent,keyword,context,request,negative,spam,final},
//            duplicate:{is_dup,of?}, decision:"accept|reject|review", reasons[]}
```
### §3.6 عقد التغذية الراجعة — جدول مقترح `message_feedback(msg_hash FK filter_decisions, label correct_request|false_positive|false_negative, actor, note, created_at)` + `POST /admin/api/engine/feedback` لإعادة التحليل.
### §3.7 عقد المهام — `POST /admin/api/jobs/{id}/retry` + `POST /admin/api/queue/purge` + منح قراءة processing_queue/dead_letters/system_health.
### §3.8 عقد المقاييس — نقطة `/admin/api/metrics` (CPU/RSS حسب psutil إن توفر) أو منح system_health.

## 4) مخططات مقترحة (لن تُنفذ في هذه المرحلة — صريح)
```sql
bot_commands(id uuid pk, type text check(type in (...)), status text check(status in
  'created','pending','processing','completed','failed','expired','dead_letter'),
  requested_by text, params jsonb, result jsonb, error text, attempts int default 0,
  max_attempts int default 3, timeout_ms int default 30000, idempotency_key text unique,
  created_at, started_at, completed_at, locked_by text, locked_at timestamptz);
bot_runtime_state(k text pk, v jsonb, updated_at);
account_health(account_id fk panel_accounts, last_ok_at, flood_waits_24h int, errors_24h int, last_error);
keyword_categories(id int pk, key text unique, label_ar text, order int);
keyword_synonyms(id int pk, keyword_id fk, synonym text, dialect text);
filter_profiles(id int pk, name text, config jsonb, active bool, created_by, created_at);
routing_rules(id int pk, match jsonb, destination text, priority int, enabled bool);
notification_destinations(id int pk, type check('channel','group'), chat_id bigint, title, priority int, enabled bool, last_test_at, last_test_ok bool);
processing_jobs(id uuid pk, kind, payload jsonb, status, attempts, next_retry_at, last_error, created_at, updated_at);
bot_metrics(ts timestamptz, cpu_pct numeric, rss_mb numeric, queue_size int, mps numeric, latency_ms numeric);
message_feedback(id int pk, msg_hash text, label text, actor text, note text, created_at);
-- Retention: audit_logs 180 يوم، bot_metrics 30 يوم، filter_decisions 90 يوم (سياسة موثقة لمرحلة التنفيذ).
```

## 5) الأمان (مطبق حرفياً فوق النظام القائم — لا نظام موازٍ)
withApi واحد: جلسة HMAC HttpOnly/Secure/SameSite=strict → RBAC حي من dashboard_users → CSRF same-origin → rate-limit → Zod strict → {ok,data|error,request_id} → audit قبل/بعد مموّه. الأدوار: viewer < operator(analyst) < admin(administrator) < super_admin. مركز البوت: قراءة viewer+، محتوى operator+، تحكم تشغيلي admin+، مستخدمون/أسرار super_admin فقط.
