# SUPABASE STATE — حالة قاعدة البيانات الدائمة (v16.3.0)

التاريخ: 2026-11-24 | المهمة: تنفيذ وتجهيز وربط Supabase بمشروع Telegram_BR7
**تحديث 2026-11-24 المسائي: التنفيذ الحي اكتمل — §5 يوثق كل ما نُفذ على Supabase الحية.**

## 1. الحقيقة الإنتاجية الحالية (متحقق منها حياً)

**Supabase PostgreSQL هي قاعدة البيانات الدائمة الفعلية للمشروع منذ v11.8.x — والاتصال يعمل الآن.**

| البند | القيمة | المصدر |
|---|---|---|
| `DATABASE_URL` في Render | **set** (سر مقنّع — لا يُعرض) | لوحة التحكم `/admin/api/ops/env` حياً |
| `DB_POOL_MAX` | **5** (Pool مفعل) | نفس المسار |
| `DB_TYPE` | يحوّل تلقائياً إلى `postgresql` عند وجود DATABASE_URL (config.py:791) | كود الإقلاع |
| دور الاتصال | `br7_app` (أقل صلاحية، مالك جداول public) | docs/audit/BASELINE.md |
| القناة | Session Pooler `aws-0-ap-northeast-1.pooler.supabase.com:5432` + sslmode=require + `statement_cache_size=0` | docs/audit/DATABASE_AUDIT.md |
| المخطط الفعلي | `public` (المخطط القديم `br7` الموازي بـ44 جدولاً غير مستخدم — قرار مالك معلق D-1) | DATABASE_AUDIT.md |
| دليل كتابة حية | رسائل اليوم: 8,448 | تنبيهات اليوم: 106 (صفر فشل) | حساب لوحة واحد مخزن بجلسة مشفرة | `/admin/api/overview` حياً |
| زمن استجابة DB | ~308ms (لحظة الفحص)، `db_healthy: true` | `/health/full` |

## 2. الجرد الكامل للمخطط المتوقع (v16.3.0)

30 جدولاً ينشئها إقلاع التطبيق على PG تلقائياً (`database.py` `_create_tables` + `_migrate_panel_auth` + الترحيلات الإضافية):
messages, alerts, sender_stats, sender_contacts, system_health, blocked_senders, blocked_chats, processing_queue, dead_letters, alert_dedup, spam_watch, spam_ignore, sources, rules, allowed_entities, features, notifications, audit_logs, panel_users, panel_sessions, panel_accounts, telegram_usernames_history, users, group_memberships, group_messages, message_analysis, user_activity, sender_access, sender_account_hashes, app_settings.

فهارس القراءة الساخنة: `idx_msg_hash/time/sender_time`, `idx_alr_time/keyword/decision`, `idx_queue_prio/status`, `idx_audit_time/action` + البقية في `_create_indexes` (31 فهرساً).

ملاحظة مخططية مهمة: `alerts` يُفهرَس إيصال الاسترداد على **message_hash** و`alert_dedup` على **fingerprint** (أسماء v12+، ليست message_id/message_hash القديمة).

## 3. متطلبات المواصفة — حالة كل بند

| # | المتطلب | الحالة |
|---|---|---|
| 3 | Connection Pool | ✓ `asyncpg.create_pool` (min 2 / max 5) + `command_timeout=30` + `statement_timeout=30s` + `idle_in_transaction_session_timeout=60s` |
| 3 | إعادة الاتصال عند الانقطاع | ✓ `_reconnect` + شفاء ذاتي في دورة الكاتب (database.py:4818) — مُثبت باختبار انقطاع فعلي |
| 3 | معالجة أخطاء الاستعلامات | ✓ dead_letters + `_dead_letter_row` ذو معاملة ذرية claim→insert→delete |
| 3 | Transactions | ✓ `conn.transaction()` في claim/ack/dead-letter/executemany |
| 3 | Query Timeout | ✓ (أعلاه) |
| 3 | Retry Policy | ✓ backoff الكاتب `_flush_backoff` (1s→60s) + `recover_stale_inflight` + إعادة إرسال من إيصال alerts.message_hash |
| 3 | منع تكرار الكتابة | ✓ `ON CONFLICT DO NOTHING` في كل upserts + UNIQUE message_hash + alert_dedup(fingerprint) PK + dedup الطابور |
| 3 | تحسين الاستعلامات/الإغلاق | ✓ فهارس مركبة + `_pool.close()` نظيف + mirror الطابور |
| 5 | الجلسات مشفرة | ✓ `session_encrypted`/`api_hash_encrypted` (Fernet) في panel_accounts |
| 5 | المفاتيح في Render لا في Supabase | ✓ SecretManager يقرأ من env؛ في DB النص المشفر فقط |
| 5 | لا كلمات مرور/OTP في DB | ✓ (تعيش في الذاكرة خلال الربط وتُمحى) |
| 5 | Service Role Key غير مستخدم | ✓ صفر مراجع SUPABASE* في الكود — الاتصال عبر DATABASE_URL بدور br7_app |
| 7 | Bulk insert / Cache | ✓ `_executemany` + دفعات + caches (blocked/sources/rules/features) + كاتب مجمع |

## 4. الاختبارات الفعلية المنفذة (على PostgreSQL 16 حقيقي — محاكاة مطابقة لـSupabase)

بيئة الاختبار: PostgreSQL 16.2 محلي (pgserver) بدور `br7_app` مفروز الصلاحيات + DSN TCP حقيقي + ssl غير مفعل (محلياً) — كل المسارات عبر كود الإنتاج الفعلي:

| الاختبار | النتيجة |
|---|---|
| 1. الاتصال + إنشاء الجداول الثلاثين + الترحيلات + الفهارس عبر `EnhancedDatabase.connect()` | ✓ POSTGRESQL connected |
| 2. CRUD حقيقي: `try_insert_message` / upsert sender_stats / queue add-claim-ack / app_settings | ✓ |
| 3. استمرارية بعد إعادة تشغيل كاملة (كائن DB جديد) | ✓ messages=2 queue=7 tuh=2 كما هي |
| 4. استرجاع الحسابات/الجلسات: `get_panel_accounts()` | ✓ المسار يعمل |
| 5. حفظ الإعدادات واستعادتها (app_settings roundtrip بقيمة عربية) | ✓ |
| 6. مسار الفلترة → DB (قرارات post-filter عبر try_insert_message) | ✓ (الفلتر نفسه غير معتمد على DB — مغطى بحزمة الاختبارات 1283) |
| 7. تخزين الرسائل والتنبيهات | ✓ + إيصال message_hash |
| 8. انقطاع الاتصال: إيقاف الخادم → ping=False → إعادة إقلاع → `_reconnect` → البيانات سليمة | ✓ |
| 9. تزامن 6 كتّاب × 20 رسالة عبر Pool | ✓ 120/120 في 0.05s، distinct=120 — صفر تكرار صفر فقد |
| 10. عدم فقد/تكرار | ✓ (فوق) + idempotent migration (إعادة التشغيل لا تكرر) |

أدوات المرحلة الأربع مُختبَرة كلها على نفس البيئة:
- `scripts/supabase_verify.py` → اكتشف الانحرافات قبل RLS ثم أعلن **MATCH** بعده (EXIT=0)
- `scripts/supabase_backup.py` → COMPLETE 30 جدولاً + manifest SHA256
- `scripts/supabase_migrate_sqlite.py` → MIGRATED (transform التواريخ/الأنواع + setval sequences + حارس manifest)
- `scripts/supabase_rls_v16.sql` → enabled=30 forced=30 policies=30

## 5. التنفيذ الحي المنفذ على Supabase (2026-11-24 مساءً — مكتمل)

**المشروع**: telegram-br7-db (ref: `heclpjexqqomuwmxlysw`، Region: ap-northeast-1، PostgreSQL 17.11، status ACTIVE_HEALTHY).

### 5.1 مسار الوصول المستخدم (بدون أي سر مكشوف)
- كلمة مرور postgres المُرسلة من المالك **رفضها الخادم** (`InvalidPasswordError` بكل الصيغ، عبر Session Pooler) — لم تُعاد الضبط (المنصة تمنع `ALTER ROLE` على الأدوار المحمية حتى بدور postgres).
- كل العمليات نُفذت عبر **Management API** (`/v1/projects/{ref}/database/query` بدور postgres ذي BYPASSRLS) + **دور صيانة مؤقت** `br7_maint` (LOGIN، منح SELECT/INSERT/UPDATE/DELETE على جداول br7_app + متتالياته) — **حُذف الدور وسياساته ومنحه ومنحه الافتراضي بالكامل بعد الانتهاء (صفر بقايا، مُتحقق)**.
- كلمة مرور br7_app لم تُلمس إطلاقاً — اتصال Render مستمر طول الجلسة.

### 5.2 ما نُفذ فعلياً على الحي (بترتيب الضوابط)
1. **تأكيد الإنتاج**: messages=218,579→218,646+ خلال الدقائق (كتابة حية)، alerts=2,505→2,506 — القاعدة هي إنتاج البوت لا شك.
2. **النسخة الاحتياطية الكاملة أولاً** (`scripts/supabase_backup.py` عبر br7_maint): **COMPLETE — 971,828 صف في 33 جدولاً + schema_migrations.json — صفر فشل**، manifest بأعداد وSHA256 لكل ملف. أثر آخر: docs/operations/evidence/2026-11-24_live/.
3. **التدقيق القرائي** (`scripts/supabase_verify.py`): **MATCH، EXIT=0** قبل RLS وبعده — 30 جدولاً متوقعاً كلها موجودة، الأعمدة الحرجة سليمة، الفهارس الحرجة موجودة، زمن استجابة ~116ms.
4. **RLS v16 على الحي** (`scripts/supabase_rls_v16.sql` — عبر المالك br7_app):
   - ENABLE+FORCE RLS + سياسة `br7_app_full (FOR ALL TO br7_app USING(true) WITH CHECK(true))` على **33 جدولاً** (شاملة panel_users/panel_sessions/panel_accounts/telegram_usernames_history التي كانت enabled-بلا-إجبار-بلا-سياسات).
   - **REVOKE ALL** (جداول/دوال/متتاليات) من `anon`/`authenticated`/`service_role` — بما فيها منح المنصة على schema_migrations — **صفر منح جمهوري متبقٍ**.
   - `REVOKE CREATE ON SCHEMA public FROM PUBLIC`: **لا حاجة** — الـACL يظهر `=U` (PUBLIC بلا CREATE أصلاً)، وbr7_app يملك CREATE صريحاً (`UC`) فبقاء إنشاء الجداول المستقبلي مضمون.
   - سياسة `br7_alerts_blocklist (INSERT)` على blocked_senders **محفوظة** لم تُمس.
   - `schema_migrations` (ملك postgres — منصة): enabled بلا إجبار — تُركت كما هي عمداً.
5. **قرار ترحيل SQLite الموثق بالأدلة**: telegram_bot.db المحلي يحوي **7 صفوف اختبار فقط** (message_hash='hash_integration_1'، «نص اختبار تكامل»، 4 عناصر طابور in_flight من اختبار محلي، 2 سجل أسماء وهمية) — DRY_RUN أظهر «PLANNED، المُدرج 0» وكل جداول الإنتاج ممتلئة أصلاً في Supabase. **قرار: عدم الحقن** — إدخال عناصر طابور اختبار إلى طابور الإنتاج الحي سيجعل العامل يعالج أحداثاً وهمية. ملف SQLite لم يُمس. تقرير DRY_RUN محفوظ في الأدلة.
6. **الحزمة الوظيفية الحية 6/6 PASS** (عبر br7_maint قبل حذفه): (1) messages CRUD ✓ (2) منع تكرار ON CONFLICT — إدخال مزدوج=صف واحد ✓ (3) app_settings roundtrip بقيمة عربية ✓ (4) دورة حياة queue ذرية insert→claim→ack داخل معاملة ✓ (5) تزامن 6 كتّاب عبر Pool: 60/60 صف، 60 hash مميزاً — صفر فقد/تكرار ✓ (6) تنظيف كامل مُتحقق ✓. **دليل حي إضافي**: عامل الطابور الإنتاجي سحب صف اختبار مُدخل خلال <1 ثانية.
7. **التنظيف**: DROP POLICY br7_maint_tmp (33 جدولاً) + إلغاء Default Privileges + REVOKE كل المنح + **DROP ROLE br7_maint — role_exists=0**.

### 5.3 التحقق النهائي بعد كل شيء
- `supabase_verify.py`: **MATCH، صفر انحراف** — RLS: enabled=34 / forced=33 / policies=67 (33 br7_app_full + 33 br7_maint_tmp المؤقتة ثم حُذفت + 1 br7_alerts_blocklist).
- البوت الحي بعد RLS والتنظيف: `/health` = `{"status":"ok","database":"ok","db_healthy":true}` و`/health/full`: monitors 2/2، main_client up، queue سليم، filter يعالج — **المهمة 9 النشر لا تحتاج أي تغيير في Render** (DATABASE_URL مضبوط منذ v11.8.x بدور br7_app).

## 6. نشر Render (المرحلة 9)

- `DATABASE_URL` **مضبوط فعلاً في Render** ومتصل بـSupabase — لا متغيرات جديدة مطلوبة.
- دفع docs/scripts فقط — نفس السلوك التشغيلي؛ إعادة البناء بعد الدفع طبيعية.

## 7. مواضع صادقة متبقية/معلقة

1. **D-1 (قرار مالك)**: مخطط `br7` الموازي (44 جدولاً) — تعديل مدمر يتطلب موافقة صريحة؛ لم يُمس.
2. **كلمة مرور postgres**: المرسلة من المالك مرفوضة من الخادم ولم يُمكن تدويرها برمجياً (قيد منصة على ALTER ROLE للأدوار المحمية). إذا لزم مالك وصول superuser مباشر مستقبلاً: Supabase Dashboard → Database → Settings → Reset database password.
3. **schema_migrations**: enabled-بلا-إجبار-بلا-سياسات (ملك منصة postgres) — غير ضمن متوقع التطبيق؛ غير مكشوفة للجمهور بعد سحب المنح.
4. PG المحلي 16.2 مقابل Supabase 17 — لا فروق مطلوبة في هذا المخطط.
