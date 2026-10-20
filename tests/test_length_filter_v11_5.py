#!/usr/bin/env python3
"""اختبارات فلترة طول النص الأصلي — v11.5 (مواصفة المستخدم §4/§6/§17-أ).

القاعدة المركزية: 6 <= len(text) <= 65 (الحدان شاملان) على **نص المصدر
الأصلي** — لا نص التنبيه، ولا الاسم، ولا الأزرار، ولا وسوم HTML.

معيار Unicode الموثق: ``len(text)`` في بايثون يحصي **نقاط Unicode
البرمجية** (code points) — لا وحدات UTF-16 ولا المحارف المرئية. الرمز
الإيموجي خارج BMP (مثل 😀 U+1F600) = نقطة واحدة بواقع بايثون ووحدتان
UTF-16 — الفلترة تعتمد معيار بايثون كما كتبت المواصفة حرفياً.

المسارات المغطاة:
  * بوابة config (القيم الافتراضية 6/65).
  * InputSanitizer.validate_message_text (config.py).
  * بوابة monitors._validate_event (monitors.py — تُستدعى من
    _process_event_pipeline حصراً، أي أنها نفس بوابة استعادة الطابور
    وإعادة محاولة DLQ — monitors.py:2198 ← 2235).
  * إثبات الترتيب: النص المرفوض لا يصل إلى _analyze_and_alert (مسار
    إنشاء التنبيه) إطلاقاً.

لا تُغيَّر هنا بقية الفلاتر: الكلمات/الحظر/dedup/محدد المعدل خارج
النطاق — هذه الاختبارات تغطي بوابة الطول فقط.
"""

import pytest

from config import CFG, InputSanitizer

# ═══════════════════════ جدول القبول (مواصفة §4) ═══════════════════════
# 0/1/5 → رفض | 6/7/30/64/65 → قبول | 66/100/500 → رفض
ACCEPT_LENGTHS = [6, 7, 30, 64, 65]
REJECT_LENGTHS = [0, 1, 5, 66, 100, 500]


def _text_of_length(n: int) -> str:
    """نص عربي بطول n نقطة Unicode بالضبط (بلا مسافات طرفية)."""
    base = "ابجد"
    if n <= 0:
        return ""
    return (base * (n // 4 + 1))[:n]


class TestConfigDefaults:
    """القيم الافتراضية المركزية — سبب جذري العطل (كانت 10/5000)."""

    def test_defaults_are_6_and_65(self):
        assert CFG.MIN_MESSAGE_LENGTH == 6
        assert CFG.MAX_MESSAGE_LENGTH == 65


class TestSanitizerBoundaries:
    """بوابة InputSanitizer.validate_message_text — الحدود الشاملة."""

    @pytest.mark.parametrize("n", ACCEPT_LENGTHS)
    def test_accept_lengths(self, n):
        text = _text_of_length(n)
        assert len(text) == n, f"إعداد الاختبار: الطول {len(text)} != {n}"
        out = InputSanitizer.validate_message_text(text)
        assert out == text, f"طول {n} يجب أن يُقبل (الحدان شاملان)"

    @pytest.mark.parametrize("n", REJECT_LENGTHS)
    def test_reject_lengths(self, n):
        text = _text_of_length(n)
        assert len(text) == n
        assert InputSanitizer.validate_message_text(text) is None

    def test_arabic_boundaries(self):
        """توثيق العد: «ابي حل» = 6 نقاط (با مسافة داخلية) → مقبول.
        «مرحبا» = 5 نقاط → مرفوض (جدول §4)."""
        assert len("ابي حل") == 6
        assert InputSanitizer.validate_message_text("ابي حل") == "ابي حل"
        assert len("مرحبا") == 5
        assert InputSanitizer.validate_message_text("مرحبا") is None

    def test_english_boundaries(self):
        assert InputSanitizer.validate_message_text("helpme") == "helpme"  # 6
        assert InputSanitizer.validate_message_text("help") is None        # 4
        long65 = "a" * 65
        assert InputSanitizer.validate_message_text(long65) == long65
        assert InputSanitizer.validate_message_text("a" * 66) is None

    def test_emoji_counted_as_code_points(self):
        """6 إيموجي = 6 نقاط Unicode → مقبول (معيار بايثون الموثق)."""
        six_emoji = "😀" * 6
        assert len(six_emoji) == 6
        assert InputSanitizer.validate_message_text(six_emoji) == six_emoji
        assert InputSanitizer.validate_message_text("😀" * 66) is None

    def test_multiline_text(self):
        """النص متعدد الأسطر: الأسطر الجديدة نقاط تُحصى كالمعتاد."""
        multi = "سطر1\nسطر2\nسطر3"  # 4+1+4+1+4 = 14
        assert len(multi) == 14
        assert InputSanitizer.validate_message_text(multi) == multi

    def test_spaces_only_rejected(self):
        """المسافات فقط → نظيفة → فارغة → مرفوضة (لا رسالة فارغة)."""
        assert InputSanitizer.validate_message_text("      ") is None
        assert InputSanitizer.validate_message_text("\n\t\n") is None

    def test_empty_and_none(self):
        assert InputSanitizer.validate_message_text("") is None
        assert InputSanitizer.validate_message_text(None) is None
        assert InputSanitizer.validate_message_text(12345) is None

    def test_strip_is_legacy_behavior_documented(self):
        """موثق: المدقق يقص المسافات الطرفية قبل الفحص (سلوك قائم من
        الإصدارات السابقة — لم يُغيَّر في v11.5). «حلّ اب» 7 محارف مع
        مسافة طرفية → 6 بعد القص → مقبول."""
        assert InputSanitizer.validate_message_text("ابي حلل ") == "ابي حلل"


class TestValidateEventGate:
    """بوابة monitors._validate_event — قبل dedup/الطابور/التنبيه."""

    @pytest.fixture()
    async def monitor(self, db):
        from monitors import EnhancedAccountMonitor
        account = {
            "name": "LEN_TEST", "api_id": 12345, "api_hash": "test-hash",
            "phone": "+90000000000", "session": "len_session", "priority": 1,
        }
        return EnhancedAccountMonitor(account, db, None)

    @pytest.mark.asyncio
    async def test_gate_accepts_6_and_65(self, monitor):
        assert await monitor._validate_event({"text": "ابي حلل", "sender_id": 1, "chat_id": 1}) is True
        assert await monitor._validate_event({"text": "ا" * 65, "sender_id": 1, "chat_id": 1}) is True

    @pytest.mark.asyncio
    async def test_gate_rejects_5_and_66_and_500(self, monitor):
        for bad in ("مرحبا", "ا" * 66, "ا" * 500):  # 5/66/500
            assert await monitor._validate_event({"text": bad, "sender_id": 1, "chat_id": 1}) is False

    @pytest.mark.asyncio
    async def test_gate_passes_empty_text_for_media(self, monitor):
        """text فارغ (وسائط بلا تعليق) — البوابة لا ترفضه (يكمل للوسائط
        بعلامة [media] اللاحقة) — السلوك القائم لم يتغير."""
        assert await monitor._validate_event({"text": "", "sender_id": 1, "chat_id": 1}) is True


class TestGateBeforeAlertBuild:
    """إثبات الترتيب: المرفوض لا يصل إلى مسار إنشاء التنبيه إطلاقاً،
    والقبول يصل — عبر مسار الطابور نفسه (process_event_from_queue =
    مسار الاستعادة/إعادة المحاولة أيضاً)."""

    @pytest.fixture()
    async def monitor(self, db):
        from monitors import EnhancedAccountMonitor
        account = {
            "name": "LEN_PIPE", "api_id": 12345, "api_hash": "test-hash",
            "phone": "+90000000000", "session": "len_pipe", "priority": 1,
        }
        return EnhancedAccountMonitor(account, db, None)

    def _event(self, text: str) -> dict:
        return {
            "text": text, "sender_id": 4242, "chat_id": -1001234567890,
            "message_id": 77, "account_name": "LEN_PIPE",
            "sender_type": "user", "media_type": "none",
        }

    @pytest.mark.asyncio
    async def test_rejected_length_never_reaches_alert_build(self, monitor, monkeypatch):
        calls = {"alert": 0}

        async def _no_alert(*a, **kw):
            calls["alert"] += 1

        monkeypatch.setattr(monitor, "_analyze_and_alert", _no_alert)
        for bad in ("مرحبا", "ا" * 66, "ا" * 500):  # 5/66/500
            await monitor.process_event_from_queue(self._event(bad))
        assert calls["alert"] == 0, "نص مرفوض الطول وصل لمسار إنشاء التنبيه!"

    @pytest.mark.asyncio
    async def test_accepted_length_reaches_pipeline(self, monitor, monkeypatch):
        calls = {"alert": 0}

        async def _alert(*a, **kw):
            calls["alert"] += 1

        monkeypatch.setattr(monitor, "_analyze_and_alert", _alert)
        await monitor.process_event_from_queue(self._event("ابي حل"))  # 6 ✓
        assert calls["alert"] == 1, "نص مقبول الطول لم يكمل خط المعالجة"

    @pytest.mark.asyncio
    async def test_queue_restore_reruns_gate(self, monitor, monkeypatch):
        """الاستعادة بعد إعادة التشغيل تعيد فحص الطول — البوابة في
        _process_event_pipeline (نفس نقطة 2198) وليست في الالتقاط فقط."""
        seen = {"validated": 0}
        original = monitor._validate_event

        async def _spy(data):
            seen["validated"] += 1
            return await original(data)

        monkeypatch.setattr(monitor, "_validate_event", _spy)
        await monitor.process_event_from_queue(self._event("ابي حل"))
        assert seen["validated"] == 1, "الاستعادة لم تعِد فحص البوابة"


class TestFilterEngineMaxRespectsConfig:
    """محرك الفلتر يقرأ نفس CFG.MAX_MESSAGE_LENGTH (نقطة filter_engine)."""

    @pytest.mark.asyncio
    async def test_66_chars_ignored_too_long(self):
        from filter_engine import EnhancedFilter
        flt = EnhancedFilter()
        res = await flt.analyze("ا" * 66)
        assert res.get("decision") == "ignore"
        assert "too_long" in (res.get("reasons") or [])

    @pytest.mark.asyncio
    async def test_65_chars_not_rejected_for_length(self):
        from filter_engine import EnhancedFilter
        flt = EnhancedFilter()
        res = await flt.analyze("ا" * 65)
        # 65 داخل الحد — لا يجوز أن يكون السبب too_long (قد يُرفض لسبب
        # آخر من بقية الفلاتر وهو خارج نطاق هذه المهمة)
        assert "too_long" not in (res.get("reasons") or [])
