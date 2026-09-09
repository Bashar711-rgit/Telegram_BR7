"""Deduplication tests (user requirements #2/#3 — اختبارات 3-7).

يغطي كل حالات منع التكرار المطلوبة:
  اختبار 3 — نفس الرسالة من نفس الحساب (إعادة إرسال) → تنبيه واحد
  اختبار 4 — نفس الرسالة من حسابين → تنبيه واحد
  اختبار 5 — نفس الرسالة من الحسابات الستة → تنبيه واحد فقط
  اختبار 6 — رسائل مختلفة من نفس المرسل → ليست مكررة
  اختبار 7 — رسائل متشابهة جزئياً → لا تُحذف بالخطأ
  + البصمة عبر عمليات (DB claim) + فك الحجز + النافذة + الإعدادات الحية
"""

import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from config import CFG  # noqa: E402
from dedup import (  # noqa: E402
    AlertDeduplicator,
    content_fingerprint,
    normalize_for_fingerprint,
)


@pytest.fixture()
async def clean_db(db):
    """connftest `db` + جدول dedup نظيف."""
    await db._execute("DELETE FROM alert_dedup")
    yield db
    await db._execute("DELETE FROM alert_dedup")


@pytest.fixture()
def dd(clean_db):
    return AlertDeduplicator(db=clean_db)


class TestFingerprint:
    def test_same_sender_same_text_same_fp(self):
        a = content_fingerprint(123456, "السلام عليكم")
        b = content_fingerprint(123456, "السلام عليكم")
        assert a == b

    def test_different_sender_different_fp(self):
        """مرسلان مختلفان بنفس النص = طلبان مختلفان (يجب ألا يُحذفا)."""
        a = content_fingerprint(111, "ابي حل واجب الاحصاء")
        b = content_fingerprint(222, "ابي حل واجب الاحصاء")
        assert a != b

    def test_different_text_different_fp(self):
        a = content_fingerprint(111, "ابي حل واجب الاحصاء")
        b = content_fingerprint(111, "ابي حل واجب الفيزياء")
        assert a != b

    def test_partial_similarity_not_deduped(self):
        """المتشابهة جزئياً تبقى مختلفة (متطلب صريح)."""
        a = content_fingerprint(111, "ابي حل")
        b = content_fingerprint(111, "ابي حلل")
        c = content_fingerprint(111, "ابي الحل")
        assert len({a, b, c}) == 3

    def test_normalization_folds_arabic_noise(self):
        """المدّ/التشكيل/الأشكال لا تُنتج بصمات مختلفة لنفس الرسالة."""
        base = content_fingerprint(111, "ابي حل واجب")
        stretched = content_fingerprint(111, "ااااابي حـــل واجب")  # تطويل + مدّ
        diacritics = content_fingerprint(111, "أَبِي حَلْ وَاجِب")
        assert base == stretched == diacritics

    def test_arabic_indic_digits_folded(self):
        assert content_fingerprint(1, "الواجب ٥") == content_fingerprint(1, "الواجب 5")

    def test_normalize_strips_and_collapses(self):
        assert normalize_for_fingerprint("  هههههه  ") == "ه"
        assert normalize_for_fingerprint("مررررررا") == "مرا"
        assert normalize_for_fingerprint("حلل") == "حلل"  # التكرار المزدوج يبقى (3+ فقط يُقلص)
        # ؤ→و توحيد مقصود + الترقيم لا يُمس
        assert normalize_for_fingerprint("سؤال؟؟") == "سوال؟؟"


class TestDedupBehavior:
    @pytest.mark.asyncio
    async def test_same_sender_same_message_claimed_once(self, dd):
        """اختبار 3: نفس الرسالة من نفس المرسل → أول claim فقط."""
        fp = content_fingerprint(123456, "السلام عليكم")
        assert await dd.check_and_claim(fp) is True   # الأولى: مسموحة
        assert await dd.check_and_claim(fp) is False  # مكرر
        assert await dd.check_and_claim(fp) is False  # مكرر
        assert dd.stats()["blocked"] == 2

    @pytest.mark.asyncio
    async def test_two_accounts_same_message_one_alert(self, dd):
        """اختبار 4: نفس الرسالة من حسابين → claim واحد فقط."""
        fp = content_fingerprint(123456, "ابي حل واجب الاحصاء")
        assert await dd.check_and_claim(fp) is True   # Account 1
        assert await dd.check_and_claim(fp) is False  # Account 2

    @pytest.mark.asyncio
    async def test_six_accounts_same_message_single_alert(self, dd):
        """اختبار 5: الحسابات الستة تلتقط نفس الرسالة → تنبيه واحد فقط."""
        fp = content_fingerprint(123456, "مطلوب باحث حل واجب")
        results = [await dd.check_and_claim(fp) for _ in range(6)]
        assert results == [True, False, False, False, False, False]

    @pytest.mark.asyncio
    async def test_cross_chat_resend_same_sender(self, dd):
        """نفس المرسل يعيد نفس النص في مجموعة أخرى (رسالة مختلفة المعرف) —
        الـ msg_hash القديم لا يمنعها لكن dedup المحتوى يمنع التنبيه المزدوج."""
        fp = content_fingerprint(123456, "عندي مشروع تخرج محتاج مساعدة")
        assert await dd.check_and_claim(fp) is True
        assert await dd.check_and_claim(fp) is False

    @pytest.mark.asyncio
    async def test_different_messages_not_blocked(self, dd):
        """اختبار 6: رسائل مختلفة من نفس المرسل تمر كلها."""
        fps = [
            content_fingerprint(123456, "ابي حل واجب الاحصاء"),
            content_fingerprint(123456, "ابي حل واجب الفيزياء"),
            content_fingerprint(123456, "محتاج مساعدة في الرياضيات"),
        ]
        results = [await dd.check_and_claim(fp) for fp in fps]
        assert results == [True, True, True]

    @pytest.mark.asyncio
    async def test_release_allows_retry(self, dd):
        """فشل الإرسال → release → إعادة المحاولة مسموحة."""
        fp = content_fingerprint(9, "رسالة فاشلة")
        assert await dd.check_and_claim(fp) is True
        await dd.release(fp)
        assert await dd.check_and_claim(fp) is True

    @pytest.mark.asyncio
    async def test_db_persistence_across_instances(self, clean_db):
        """الحماية تنجو من إعادة تشغيل العملية (نسخة جديدة بنفس DB)."""
        fp = content_fingerprint(42, "رسالة عبر إعادة التشغيل")
        first = AlertDeduplicator(db=clean_db)
        assert await first.check_and_claim(fp) is True
        # "restart": نسخة جديدة تماماً (ذاكرة فارغة) لكن نفس قاعدة البيانات
        second = AlertDeduplicator(db=clean_db)
        assert await second.check_and_claim(fp) is False

    @pytest.mark.asyncio
    async def test_window_expiry_restarts(self, dd):
        """بعد انتهاء النافذة، نفس الرسالة تُقبل من جديد (رسالة مشروعة)."""
        fp = content_fingerprint(7, "رسالة بعد النافذة")
        object.__setattr__(CFG, "DEDUP_WINDOW_SECONDS", 1)
        try:
            assert await dd.check_and_claim(fp) is True
            # تسريب زمني: نجعل الصف قديماً في DB وأفرغ الذاكرة (محاكاة مرور الوقت)
            await dd.db._execute("UPDATE alert_dedup SET first_seen = ?, last_seen = ? WHERE fingerprint = ?",
                                 (0.0, 0.0, fp))
            dd._mem.clear()
            import asyncio as _aio
            await _aio.sleep(0)
            assert await dd.check_and_claim(fp) is True
        finally:
            object.__setattr__(CFG, "DEDUP_WINDOW_SECONDS", 86400)

    @pytest.mark.asyncio
    async def test_disabled_via_cfg(self, dd):
        """DEDUP_ENABLED=false (من اللوحة) → تعطيل فوري."""
        fp = content_fingerprint(5, "رسالة والدي.dup معطل")
        assert await dd.check_and_claim(fp) is True
        object.__setattr__(CFG, "DEDUP_ENABLED", False)
        try:
            assert await dd.check_and_claim(fp) is True  # لم يُمنع
        finally:
            object.__setattr__(CFG, "DEDUP_ENABLED", True)

    @pytest.mark.asyncio
    async def test_db_failure_fail_open(self):
        """خطأ DB → فشل-آمن: التنبيه الأول يمر (حماية بالذاكرة فقط)."""

        class BrokenDB:
            async def claim_alert_fingerprint(self, *a, **k):
                raise RuntimeError("db down")

            async def release_alert_fingerprint(self, *a, **k):
                raise RuntimeError("db down")

        dd = AlertDeduplicator(db=BrokenDB())
        fp = content_fingerprint(3, "رسالة والـ DB معطل")
        assert await dd.check_and_claim(fp) is True   # الفشل-آمن يسمح بالأولى
        assert dd.stats()["db_errors"] == 1
        assert await dd.check_and_claim(fp) is False  # الذاكرة تمنع البقية

    @pytest.mark.asyncio
    async def test_cleanup_expired_rows(self, dd):
        fp = content_fingerprint(11, "رسالة تنظيف")
        await dd.check_and_claim(fp)
        await dd.db._execute("UPDATE alert_dedup SET first_seen = 0, last_seen = 0 WHERE fingerprint = ?", (fp,))
        deleted = await dd.cleanup_expired()
        assert deleted == 1
