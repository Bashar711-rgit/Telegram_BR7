# SUPABASE STATE — حالة قاعدة البيانات الدائمة (v16.3.0)

التاريخ: 2026-11-24 | المهمة: تنفيذ وتجهيز وربط Supabase بمشروع Telegram_BR7

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

## 5. الإجراء الحي المتبقي (يحتاج DATABASE_URL من المالك فقط)

الأدوات جاهزة ومجربة؛ للتنفيذ على Supabase الحية:

```bash
# 1) النسخة الاحتياطية قبل أي تعديل
DATABASE_URL='<pooler-dsn>' python scripts/supabase_backup.py --out /tmp/br7_backup

# 2) التدقيق القرائي الحي (المخطط/الأعمدة/الفهارس/RLS/الأعداد)
DATABASE_URL='<pooler-dsn>' python scripts/supabase_verify.py

# 3) فرض RLS على الجداول الثلاثين + القديمة (مرة واحدة — idempotent)
DATABASE_URL='<pooler-dsn>' python -c "import asyncio,asyncpg; ..."
# (أو عبر Supabase SQL Editor بلصق محتوى scripts/supabase_rls_v16.sql)

# 4) (اختياري) ترحيل أي SQLite محلي — الافتراضي DRY_RUN
DATABASE_URL='<pooler-dsn>' python scripts/supabase_migrate_sqlite.py --sqlite telegram_bot.db
```

## 6. نشر Render (المرحلة 9)

- `DATABASE_URL` **مضبوط فعلاً في Render** ومتصل بـSupabase — لا متغيرات جديدة مطلوبة.
- هذه المرحلة لم تغيّر أي كود تشغيلي (scripts/docs فقط) — لا نشر إجباري؛ أي دفع لاحق سيبني نفس السلوك.

## 7. مواضع صادقة غير منجزة/معلقة

1. **D-1 (قرار مالك)**: مخطط `br7` الموازي (44 جدولاً، بلا صلاحيات لـbr7_app) — الحذف تعديل مدمر يتطلب نسخة احتياطية + موافقة صريحة؛ لم يُمس.
2. **RLS v16 على الحي**: السكربت جاهز ومجرب على PG16 — تنفيذه الفعلي على Supabase يحتاج DSN المالك (أو لصقه في SQL Editor).
3. **النسخة الاحتياطية الحية**: نفس الشيء — الأداة جاهزة، DSN المالك هو المتغير الوحيد الغائب.
4. PG المحلي 16.2 مقابل Supabase 17 — لا فروق مطلوبة في هذا المخطط (لا اعتماد على ميزات 17).
