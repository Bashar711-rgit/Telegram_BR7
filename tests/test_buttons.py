"""Dynamic alert buttons tests (v9.11 — الأزرار الثلاثة المطلوبة).

يغطي الأزرار الجديدة أسفل كل تنبيه (صفّان):
  الصف 1: [ عرض الرسالة ] [ تواصل مع المرسل ]
  الصف 2: [ مراسلة ] [ 📋 نسخ النص (ميزة قائمة) ]

  * Username موجود / غير موجود
  * مجموعة عامة / خاصة (t.me/c/{inner}/{msg} للمجموعات الخاصة)
  * بيانات ناقصة → لا زر مكسور
  * مفاتيح التكوين ALERT_WITH_BUTTONS / ALERT_WITH_COPY_BUTTON /
    ALERT_WITH_CONTACT_BUTTON (حية من CFG)

ملاحظة توافق: مساعد المواصفات أدناه يعمل مع Telethon القديم
(KeyboardButtonUrl/KeyboardButtonCallback) والجديد (KeyboardInlineButton
مع type=InlineButtonTypeUrl/Callback) على حد سواء.
"""

import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from config import CFG  # noqa: E402
from monitors import EnhancedAccountMonitor, build_dynamic_buttons  # noqa: E402


def _btn_spec(b):
    """(kind, text, url-or-data) — version-agnostic across Telethon generations."""
    t = getattr(b, "type", None)
    if t is not None and hasattr(t, "url") and getattr(t, "url", None):
        return ("url", b.text, t.url)
    if t is not None and hasattr(t, "data"):
        d = t.data
        return ("callback", b.text, d.decode() if isinstance(d, bytes) else d)
    if getattr(b, "url", None):
        return ("url", b.text, b.url)
    d = getattr(b, "data", None)
    if d is not None:
        return ("callback", b.text, d.decode() if isinstance(d, bytes) else d)
    return (type(b).__name__, getattr(b, "text", None), None)


def _specs(buttons):
    out = []
    for row in buttons or []:
        out.append([_btn_spec(b) for b in row])
    return out


@pytest.fixture(scope="module")
def monitor():
    account = {
        "name": "BtnAcc", "api_id": 12345, "api_hash": "h",
        "phone": "+90000000000", "session": "btn", "priority": 1,
    }
    return EnhancedAccountMonitor(account, db=None, flt=None)


HASH = "a" * 32  # بصمة رسالة بطول fast_hash الحقيقي (16 بايت هكس = 32 حرفاً)


class TestBuildDynamicButtons:
    """الأزرار الثلاثة — كل الحالات."""

    def test_username_present_contact_only(self):
        # لا بيانات محادثة → لا زر عرض؛ التواصل يتطلب msg_hash
        rows = build_dynamic_buttons(
            sender={"id": 1, "username": "@ahmed_99"},
            chat={"id": None, "message_id": None, "username": None},
        )
        assert _specs(rows) == [[("url", "مراسلة", "https://t.me/ahmed_99")]]

    def test_username_absent_uses_openmessage(self):
        rows = build_dynamic_buttons(
            sender={"id": 777000222, "username": None},
            chat={"id": None, "message_id": None, "username": None},
        )
        assert _specs(rows) == [[("url", "مراسلة", "tg://openmessage?user_id=777000222")]]

    def test_contact_button_with_hash(self):
        rows = build_dynamic_buttons(
            sender={"id": 777000222, "username": None},
            chat={"id": None, "message_id": None, "username": None},
            msg_hash=HASH,
        )
        # صف 1: زر التواصل فقط (لا بيانات عرض) — صف 2: مراسلة
        assert _specs(rows) == [
            [("callback", "تواصل مع المرسل", f"cnt_{HASH}")],
            [("url", "مراسلة", "tg://openmessage?user_id=777000222")],
        ]

    def test_public_chat_message_link(self):
        rows = build_dynamic_buttons(
            sender={"id": 1, "username": None},
            chat={"id": -1001234567890, "message_id": 789, "username": "eng_group"},
            msg_hash=HASH,
        )
        # v10.6: بلا username، مراسلة تقود لرابط الرسالة المصدر (HTTPS
        # مضمون على كل العملاء → لمس الصورة يفتح ملف المرسل) بدل
        # openmessage الذي يعمل على ديسكتوب فقط.
        assert _specs(rows) == [
            [
                ("url", "عرض الرسالة", "https://t.me/eng_group/789"),
                ("callback", "تواصل مع المرسل", f"cnt_{HASH}"),
            ],
            [("url", "مراسلة", "https://t.me/eng_group/789")],
        ]

    def test_private_chat_message_link(self):
        rows = build_dynamic_buttons(
            sender={"id": 1, "username": None},
            chat={"id": -1001234567890, "message_id": 456, "username": None},
            msg_hash=HASH,
        )
        # -100 يُقتطع مرة واحدة فقط → t.me/c/1234567890/456
        # v10.6: مراسلة = رابط الرسالة المصدر (نفس سلّم الوصول الأقصى)
        assert _specs(rows) == [
            [
                ("url", "عرض الرسالة", "https://t.me/c/1234567890/456"),
                ("callback", "تواصل مع المرسل", f"cnt_{HASH}"),
            ],
            [("url", "مراسلة", "https://t.me/c/1234567890/456")],
        ]

    def test_missing_data_never_renders_broken_buttons(self):
        # لا sender_id ولا username ولا أي بيانات محادثة → لا أزرار إطلاقاً
        assert build_dynamic_buttons(
            sender={"id": None, "username": None},
            chat={"id": None, "message_id": None, "username": None},
            msg_hash=HASH,
        ) is None
        # sender موجود لكن message_id مفقود → لا زر عرض (لا أزرار مكسورة)
        rows = build_dynamic_buttons(
            sender={"id": 5, "username": None},
            chat={"id": -100123, "message_id": None, "username": None},
            msg_hash=HASH,
        )
        assert _specs(rows) == [
            [("callback", "تواصل مع المرسل", f"cnt_{HASH}")],
            [("url", "مراسلة", "tg://openmessage?user_id=5")],
        ]
        # message_id موجود لكن لا chat_id ولا username → زر العرض مرفوض
        rows = build_dynamic_buttons(
            sender={"id": 5, "username": None},
            chat={"id": None, "message_id": 10, "username": None},
            msg_hash=HASH,
        )
        assert _specs(rows) == [
            [("callback", "تواصل مع المرسل", f"cnt_{HASH}")],
            [("url", "مراسلة", "tg://openmessage?user_id=5")],
        ]

    def test_two_row_layout(self):
        """الأزرار موزعة على صفّين: عرض+تواصل ثم مراسلة."""
        rows = build_dynamic_buttons(
            sender={"id": 1, "username": "u1"},
            chat={"id": -1001, "message_id": 2, "username": "g1"},
            msg_hash=HASH,
        )
        assert rows is not None and len(rows) == 2
        assert len(rows[0]) == 2  # عرض الرسالة + تواصل مع المرسل
        assert len(rows[1]) == 1  # مراسلة

    def test_callback_data_within_telegram_limit(self):
        """بيانات الاستدعاء يجب أن تبقى ≤ 64 بايت (حد تيليجرام)."""
        assert len(f"cnt_{HASH}".encode()) <= 64


class TestBuildAlertButtons:
    """v10.8/v10.9: حُذف Button.inline/Button.url (cnt_/copy_) من مسار
    التنبيه بالكامل — _build_alert يعيد buttons=None دائماً (حسابات
    المستخدمين لا ترسل أزراراً). الأزرار الأربعة الموحدة
    [مراسلة][عرض][نسخ][القروب] (نصوص مصغّرة v10.9.1) تُبنى في alert_bot عبر
    build_alert_buttons وتُرسل عبر بوت التنبيهات (Bot API). دالة
    build_dynamic_buttons محفوظة كدالة ميزة (تُختبر في
    TestBuildDynamicButtons أعلاه) ولم تعد تُستدعى من مسار التنبيهات.
    """

    @pytest.mark.asyncio
    async def test_no_buttons_even_when_enabled(self, monitor):
        """حتى مع ALERT_BUTTONS_ENABLED=true لا أزرار من حسابات المستخدمين
        (v10.8 — الإرسال عبر بوت التنبيهات حصراً)."""
        old = getattr(CFG, "ALERT_BUTTONS_ENABLED", False)
        try:
            object.__setattr__(CFG, "ALERT_BUTTONS_ENABLED", True)
            sender = {"id": 555000111, "display": "أ", "username": "ahmed_99", "access_hash": None}
            chat = {"group_link": "https://t.me/g", "title": "ت", "msg_link": "https://t.me/g/7",
                    "id": -100123, "message_id": 7, "username": "g"}
            alert, buttons = monitor._build_alert(sender, chat, "ك", "نص", {"msg_hash": "xyz"})
            assert buttons is None
            assert "cnt_xyz" not in alert and "copy_xyz" not in alert
        finally:
            object.__setattr__(CFG, "ALERT_BUTTONS_ENABLED", old)

    @pytest.mark.asyncio
    async def test_no_buttons_by_default_v10_8(self, monitor):
        """v10.9: بدون أزرار على تنبيهات الحسابات — القالب الموحد (بطاقة
        «نص الرسالة:») بلا سطر رابط؛ الروابط في أزرار البوت."""
        sender = {"id": 555000111, "display": "أ", "username": "ahmed_99", "access_hash": None}
        chat = {"group_link": "https://t.me/g", "title": "ت", "msg_link": "https://t.me/g/7",
                "id": -100123, "message_id": 7, "username": "g"}
        alert, buttons = monitor._build_alert(sender, chat, "ك", "نص", {"msg_hash": "x"})
        assert buttons is None
        # القالب الموحد: رابط المرسل في الترويسة + البطاقة بلا سطر روابط
        assert 'href="https://t.me/ahmed_99"' in alert
        assert '<b>💬 الرسالة:</b>\n<blockquote>نص</blockquote>' in alert
        assert "رابط الرسالة" not in alert

    @pytest.mark.asyncio
    async def test_buttons_disabled_via_cfg(self, monitor):
        """المفتاح القديم محفوظ بقيمته — لكن لم يعد يؤثر على مسار التنبيه
        (الأزرار حصراً من بوت التنبيهات)."""
        sender = {"id": 1, "display": "أ", "username": "u", "access_hash": None}
        chat = {"id": -1001, "message_id": 2, "username": "g"}
        try:
            object.__setattr__(CFG, "ALERT_WITH_BUTTONS", False)
            _, buttons = monitor._build_alert(sender, chat, "ك", "نص", {"msg_hash": "x"})
            assert buttons is None
        finally:
            object.__setattr__(CFG, "ALERT_WITH_BUTTONS", True)

    @pytest.mark.asyncio
    async def test_copy_button_flag_preserved(self, monitor):
        """ميزة زر النسخ (CFG.ALERT_WITH_COPY_BUTTON) محفوظة كعلامة —
        لا أزرار نسخ في مسار الحسابات (v10.8)؛ زر المرسل في مسار البوت."""
        sender = {"id": 1, "display": "أ", "username": "u", "access_hash": None}
        chat = {"id": -1001, "message_id": 2, "username": "g"}
        _, buttons = monitor._build_alert(sender, chat, "ك", "نص", {"msg_hash": "x"})
        assert buttons is None

    @pytest.mark.asyncio
    async def test_contact_button_flag_preserved(self, monitor):
        """ميزة زر التواصل (CFG.ALERT_WITH_CONTACT_BUTTON) محفوظة كعلامة —
        لا أزرار تواصل inline في مسار الحسابات (v10.8)."""
        sender = {"id": 1, "display": "أ", "username": "u", "access_hash": None}
        chat = {"id": -1001, "message_id": 2, "username": "g"}
        _, buttons = monitor._build_alert(sender, chat, "ك", "نص", {"msg_hash": "x"})
        assert buttons is None

    @pytest.mark.asyncio
    async def test_bot_path_buttons_in_one_row(self, monitor):
        """الأزرار الأربعة الموحدة inline في صف واحد عبر بناء البوت
        (v10.9 — الدالة المركزية build_alert_buttons)."""
        from alert_bot import build_alert_html
        built = build_alert_html(
            {"sender_id": 555000111, "sender_username": "ahmed_99", "sender_display": "أ",
             "text": "نص", "chat_id": -100123, "message_id": 7, "chat_username": "g"},
            msg_link="https://t.me/g/7", group_link="https://t.me/g",
        )
        assert built["buttons"] == [[
            {"text": "مراسلة", "url": "https://t.me/ahmed_99"},
            {"text": "عرض الرسالة", "url": "https://t.me/g/7"},
        ]]
