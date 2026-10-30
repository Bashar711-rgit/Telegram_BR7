# RECOVERY AND BACKUP

## مصادر الحقيقة

| البيانات | المخزن | الدوام |
|---|---|---|
| الرسائل/التنبيهات/dedup/طوابير | Supabase PG (`public`) عبر DATABASE_URL | دائم |
| جلسات تيليجرام | SESSION_STRINGs في env الخدمة + جدول telegram_account_sessions | دائم (env) |
| إعدادات اللوحة | dashboard_store + system_settings | دائم (PG) |
| SQLite المحلي | `DB_FILE` (بدون DATABASE_URL فقط) | **فانٍ مع كل نشر** — تحذير CRITICAL مقصود عند الإقلاع |

## إجراءات الاسترداد

### 1. انهيار عملية أثناء معالجة حدث (SIGKILL/OOM/نشر)
تلقائي: الصف يبقى `in_flight` → `recover_stale_inflight` (كل 120s + عند الإقلاع) يعيده pending
بوسم `_recovered` → المسار يقرر بالإيصال (`alerts.message_hash`): إيصال = تخطٍّ؛ لا إيصال =
إعادة إرسال (فك حجز dedup المنهار أولاً). بعد 3 محاولات → dead_letters بصدق.
**لا تدخل بشري مطلوب.**

### 2. فقد DATABASE_URL (نشر خاطئ/حذف متغير)
البوت يعمل على SQLite بفارغ + تحذير CRITICAL مرئي. الاسترداد: إعادة المتغير من لوحة Render →
إعادة نشر → كل البيانات التاريخية سليمة في Supabase (لم تُمس).

### 3. طابور مسموم / dead_letters متراكمة
* فحص: `/health/full` → `dead_letters_total` + `db_writer.unresolved`.
* تنظيف تنفيذي: أمر `/purge` الإداري (purge_queue) — يمسح processing_queue فقط (ليس بيانات مستخدم).
* dead_letters: مراجعة `error_text` ثم حذف انتقائي SQL (قراءة الحدث أولاً).

### 4. جلسة حساب منتهية (Session expired)
لوحة → `/login` → إعادة دخول ذلك الحساب (كود OTP/QR). البوت يستمر بالبقية (سلّم مراقبة متعدد الحسابات).

### 5. استعادة بيانات Supabase
Supabase Dashboard → Database → Backups (حسب خطة المشروع) — خارج سيطرة الكود؛ لا نسخ احتياطي تطبيقي إضافي حالياً (مؤشر مستقبلي: BACKUP_UPLOAD_URL مدعوم بالكود لكن غير مضبوط — قرار تكلفة على المالك).

## تمارين دورية موصى بها (تشغيلية)

1. أسبوعياً: `GET /health` + `GET /health/full` (لمسة صحة + عدادات الاسترداد).
2. شهرياً: مراجعة dead_letters + نمو alert_dedup (التنظيف الدوري يغطيه — تدخل يدوي فقط عند تجاوز الحدود).
3. بعد كل نشر: تأكيد لوج اتصال الحسابات + خلو اللوج من `webadmin dashboard not loaded`.
