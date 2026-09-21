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
        assert _specs(rows) == [
            [
                ("url", "عرض الرسالة", "https://t.me/eng_group/789"),
                ("callback", "تواصل مع المرسل", f"cnt_{HASH}"),
            ],
            [("url", "مراسلة", "tg://openmessage?user_id=1")],
        ]

    def test_private_chat_message_link(self):
        rows = build_dynamic_buttons(
            sender={"id": 1, "username": None},
            chat={"id": -1001234567890, "message_id": 456, "username": None},
            msg_hash=HASH,
        )
        # -100 يُقتطع مرة واحدة فقط → t.me/c/1234567890/456
        assert _specs(rows) == [
            [
                ("url", "عرض الرسالة", "https://t.me/c/1234567890/456"),
                ("callback", "تواصل مع المرسل", f"cnt_{HASH}"),
            ],
            [("url", "مراسلة", "tg://openmessage?user_id=1")],
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
    """التكامل عبر _build_alert — الصف الثاني يشمل زر النسخ الاختياري."""

    @pytest.mark.asyncio
    async def test_full_rows_with_copy(self, monitor):
        sender = {"id": 555000111, "display": "أ", "username": "ahmed_99", "access_hash": None}
        chat = {"group_link": "https://t.me/g", "title": "ت", "msg_link": "https://t.me/g/7",
                "id": -100123, "message_id": 7, "username": "g"}
        alert, buttons = monitor._build_alert(sender, chat, "ك", "نص", {"msg_hash": "xyz"})
        specs = _specs(buttons)
        assert len(specs) == 2  # صفّان
        assert specs[0][0] == ("url", "عرض الرسالة", "https://t.me/g/7")
        assert specs[0][1] == ("callback", "تواصل مع المرسل", "cnt_xyz")
        assert specs[1][0] == ("url", "مراسلة", "https://t.me/ahmed_99")
        assert specs[1][1] == ("callback", "📋 نسخ النص", "copy_xyz")  # ميزة قائمة

    @pytest.mark.asyncio
    async def test_buttons_disabled_via_cfg(self, monitor):
        sender = {"id": 1, "display": "أ", "username": "u", "access_hash": None}
        chat = {"id": -1001, "message_id": 2, "username": "g"}
        try:
            object.__setattr__(CFG, "ALERT_WITH_BUTTONS", False)
            _, buttons = monitor._build_alert(sender, chat, "ك", "نص", {"msg_hash": "x"})
            assert buttons is None
        finally:
            object.__setattr__(CFG, "ALERT_WITH_BUTTONS", True)

    @pytest.mark.asyncio
    async def test_copy_button_disabled_via_cfg(self, monitor):
        sender = {"id": 1, "display": "أ", "username": "u", "access_hash": None}
        chat = {"id": -1001, "message_id": 2, "username": "g"}
        try:
            object.__setattr__(CFG, "ALERT_WITH_COPY_BUTTON", False)
            _, buttons = monitor._build_alert(sender, chat, "ك", "نص", {"msg_hash": "x"})
            specs = _specs(buttons)
            assert len(specs) == 2
            assert len(specs[1]) == 1  # مراسلة فقط — بدون النسخ
            assert all(t != "📋 نسخ النص" for _, t, _ in specs[1])
        finally:
            object.__setattr__(CFG, "ALERT_WITH_COPY_BUTTON", True)

    @pytest.mark.asyncio
    async def test_contact_button_disabled_via_cfg(self, monitor):
        sender = {"id": 1, "display": "أ", "username": "u", "access_hash": None}
        chat = {"id": -1001, "message_id": 2, "username": "g"}
        try:
            object.__setattr__(CFG, "ALERT_WITH_CONTACT_BUTTON", False)
            _, buttons = monitor._build_alert(sender, chat, "ك", "نص", {"msg_hash": "x"})
            specs = _specs(buttons)
            assert len(specs) == 2
            assert len(specs[0]) == 1  # عرض الرسالة فقط — بدون التواصل
            assert all(t != "تواصل مع المرسل" for _, t, _ in specs[0])
        finally:
            object.__setattr__(CFG, "ALERT_WITH_CONTACT_BUTTON", True)
