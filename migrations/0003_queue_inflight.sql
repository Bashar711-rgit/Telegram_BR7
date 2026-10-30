-- ═══════════════════════════════════════════════════════════════════
-- 0003_queue_inflight.sql — v12.1.0 التزامن الصلب (claim/ack/recover)
--
-- تطوير قاعدة البيانات المرافق لمحرك التزامن الصلب:
--   processing_queue يكسب ثلاثة أعمدة تحوّل السحب من "حذف فوري" إلى
--   "claim + ack":
--     * status     — pending | in_flight  (الحالة التشغيلية للصف)
--     * claimed_at — طابع سحب الصف (للثواني epoch) — استرداد العالق
--     * attempts   — عدد مرات السحب — سقف المحاولات قبل dead_letters
--
-- لماذا؟ (الثغرة التي أُغلقت):
--   pop_from_queue القديم كان SELECT ثم DELETE في نفس اللحظة — أي
--   انهيار (SIGKILL/OOM/نشر Render) بين السحب واكتمال المعالجة كان
--   يُفقد الحدث للأبد = تنبيه لا يصل أبداً. الآن الصف يبقى حتى
--   اكتمال المعالجة (ack)، والعالق (claimed_at قديم) يُسترد تلقائياً
--   عند الإقلاع ودورياً (كل 120 ثانية) بوسم _recovered لإعادة الإرسال،
--   أو يُودع dead_letters بصدق بعد تجاوز المحاولات (افتراضي 3).
--
-- تنبيهات التشغيل:
--   * ADD COLUMN IF NOT EXISTS — idempotent، آمنة لإعادة التطبيق.
--   * database.py ينفذ نفس الترحيل ذاتياً عند الإقلاع
--     (_migrate_queue_inflight) — هذا الملف للتوثيق والتطبيق اليدوي
--     على Supabase (SQL Editor) إن رغبت قبل النشر.
--   * لا حذف بيانات — لا إعادة تسمية — أعمدة فقط (نفس ضمانات 0001/0002).
--   * الصفوف القديمة الموجودة تاخذ DEFAULT 'pending' وتُعامل كالمعتاد.
-- ═══════════════════════════════════════════════════════════════════

ALTER TABLE processing_queue
    ADD COLUMN IF NOT EXISTS status     TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS claimed_at DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS attempts   INTEGER NOT NULL DEFAULT 0;

-- فحص الاسترداد الدوري: صفوف in_flight العالقة بالعمر
CREATE INDEX IF NOT EXISTS idx_pq_inflight_stale
    ON processing_queue (claimed_at)
    WHERE status = 'in_flight';

-- سحب الأقدم pending أولاً (نفس ترتيب السحب الحالي)
CREATE INDEX IF NOT EXISTS idx_pq_pick
    ON processing_queue (priority DESC, created_at ASC);
