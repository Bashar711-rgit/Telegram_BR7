# DATABASE AUDIT — جلسة v12.2.0

## 1. الحقيقة الإنتاجية (فحص قراءة-فقط على Supabase الحي — بتاريخ الجلسة)

الاتصال: `br7_app` عبر Session Pooler (`aws-0-ap-northeast-1.pooler.supabase.com:5432`, sslmode=require, `statement_cache_size=0` إلزامي لـpgbouncer).

### المخطط الفعلي الذي يعمل عليه التطبيق: `public`

| الجدول | العدد الحي |
|---|---|
| alerts | 336 (نمَ منذ آخر جلسة: 321 → 336 — حركة حقيقية ✓) |
| messages | 34012 |
| alert_dedup | 77 |
| dead_letters | 0 |
| processing_queue | 6 |

### ترحيل التزامن الصلب (v12.1.0) — مُتحقق منه على الحي ✓

`public.processing_queue` يملك الأعمدة: `id, event_data, priority, created_at, status, claimed_at, attempts` — عقد claim/ack/recover مشغّل فعلياً على الإنتاج.

### D-1: مخطط `br7` الموازي (انحراف مخطط)

* `br7` يضم **44 جدولاً** (نسخة ترحيل v11.8.2 «br7 unified schema v1»).
* `br7_app` **لا يملك USAGE** على المخطط — كل استعلام يفشل `InsufficientPrivilegeError` (أُثبت على 44/44 جدولاً).
* `search_path` الافتراضي `"$user", public` — التطبيق ينشئ/يستخدم جداوله في public منذ البداية.
* **الخطر**: تشغيلي/توثيقي فقط — لا خلل سلوك. الحل يتطلب قرار مالك (انظر FINDINGS_REGISTER D-1).

## 2. توافق المحركات (SQLite ↔ PostgreSQL) — مراجعة كود

مواضع `rowcount` في `database.py` مدققة نصياً (مواصفة §10 — «normalize helper behavior»):

| الموضع | الاستخدام | التقييم |
|---|---|---|
| `ack_queue_event` (sqlite) | `cursor.rowcount` بعد DELETE | صحيح — aiosqlite rowcount موثوق للـDELETE |
| `ack_queue_event` (pg) | `DELETE ... RETURNING id` + fetchrow | صحيح — يتجنب فرق دلالة rowcount تماماً |
| `purge_queue` | sqlite rowcount / pg `result.split()[1]` من DELETE status string | صحيح (نمط v9.1 audit M-4) |
| `purge_stale_queue_rows` | نفس النمط الثنائي | صحيح |
| كاتب الـqueue (evictions) | `cur.rowcount or 0` مجمعة | صحيح للاثنين |
| `try_insert_message` | عقد rowcount المحمول (v9.1) موثق داخل الملف | صحيح |

* Placeholders: تحويل مركزي `? → $n` للـPG عبر المساعدات `_execute/_fetchone/_fetchall` — لا استعلام خام يفلت خارجها في مسارات الطابور/التنبيهات المدققة.
* `recover_stale_inflight`: SELECT ثم معالجة صف-بصف مع حارس `WHERE ... AND status='in_flight'` في كل UPDATE/DELETE — double-reclaim محروس؛ السباق المتبقي الموثق: ازدواج dead-letter نظري (V12-D).
* ترحيلات idempotent: `_migrate_queue_inflight` (sqlite) و`ADD COLUMN IF NOT EXISTS` (pg) + `migrations/0003_queue_inflight.sql` — مثبتة حية.

## 3. نزاهة العمليات متعددة الكتابة

* claim → معالجة → ack: الطابور مخزن تشغيلي؛ الجداول الدائمة (messages/alerts/dead_letters) تُكتب بمسؤولية واحدة لكل مسار، والإيصال (`alerts.message_hash`) هو مصدر قرار الاسترداد — منطق «إيصال = تخطٍّ، لا إيصال = إعادة إرسال» مُغطى باختبارات (test_unified_v12_1.py).
* حجز dedup (alert_dedup): INSERT-on-PK آتومي على المحركين؛ الفك عند الفشل معرف بالفينغربرنت.

## 4. ما يلزم لإغلاق الصورة لاحقاً (موثق بصدق)

1. قرار D-1 (توحيد المخطط) — قرار مالك.
2. مثيل PG معزول لاختبارات تشغيلية فعلية (الحد المذكور في TEST_REPORT §لم يُختبَر).
3. مراقبة نمو `alert_dedup` (77 يوماً) — التنظيف الدوري موجود (`cleanup_expired`؛ نافذة 86400s) — لا فعل مطلوب الآن.
