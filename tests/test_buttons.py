"""Dynamic alert buttons tests (user requirement #10 — اختبار 10).

يغطي كل الحالات المطلوبة للأزرار [ 💬 مراسلة ] [ 📨 عرض الرسالة ]:
  * Username موجود / غير موجود
  * مجموعة عامة / خاصة
  * بيانات ناقصة → لا زر مكسور
  * الزران في صف واحد
  * مفاتيح التكوين ALERT_WITH_BUTTONS / ALERT_WITH_COPY_BUTTON (حية من CFG)
"""

import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from config import CFG  # noqa: E402
from monitors import EnhancedAccountMonitor, build_dynamic_buttons  # noqa: E402


def _specs(buttons):
    out = []
    for row in buttons or []:
        out.append([
            (type(b).__name__, getattr(b, "text", None), getattr(b, "url", None))
            for b in row
        ])
    return out


@pytest.fixture(scope="module")
def monitor():
    account = {
        "name": "BtnAcc", "api_id": 12345, "api_hash": "h",
        "phone": "+90000000000", "session": "btn", "priority": 1,
    }
    return EnhancedAccountMonitor(account, db=None, flt=None)


class TestBuildDynamicButtons:
    """الدالة المدمجة حرفياً من طلب المستخدم — كل الحالات."""

    def test_username_present(self):
        rows = build_dynamic_buttons(
            sender={"id": 1, "username": "@ahmed_99"},
            chat={"id": None, "message_id": None, "username": None},
        )
        assert _specs(rows) == [[("KeyboardButtonUrl", "💬 مراسلة", "https://t.me/ahmed_99")]]

    def test_username_absent_uses_openmessage(self):
        rows = build_dynamic_buttons(
            sender={"id": 777000222, "username": None},
            chat={"id": None, "message_id": None, "username": None},
        )
        assert _specs(rows) == [[("KeyboardButtonUrl", "💬 مراسلة", "tg://openmessage?user_id=777000222")]]

    def test_public_chat_message_link(self):
        rows = build_dynamic_buttons(
            sender={"id": 1, "username": None},
            chat={"id": -1001234567890, "message_id": 789, "username": "eng_group"},
        )
        assert _specs(rows) == [[
            ("KeyboardButtonUrl", "💬 مراسلة", "tg://openmessage?user_id=1"),
            ("KeyboardButtonUrl", "📨 عرض الرسالة", "https://t.me/eng_group/789"),
        ]]

    def test_private_chat_message_link(self):
        rows = build_dynamic_buttons(
            sender={"id": 1, "username": None},
            chat={"id": -1001234567890, "message_id": 456, "username": None},
        )
        # -100 يُقتطع مرة واحدة فقط → t.me/c/1234567890/456
        assert _specs(rows) == [[
            ("KeyboardButtonUrl", "💬 مراسلة", "tg://openmessage?user_id=1"),
            ("KeyboardButtonUrl", "📨 عرض الرسالة", "https://t.me/c/1234567890/456"),
        ]]

    def test_missing_data_never_renders_broken_buttons(self):
        # لا sender_id ولا username ولا أي بيانات محادثة → لا أزرار إطلاقاً
        assert build_dynamic_buttons(
            sender={"id": None, "username": None},
            chat={"id": None, "message_id": None, "username": None},
        ) is None
        # sender موجود لكن message_id مفقود → مراسلة فقط بدون زر عرض
        rows = build_dynamic_buttons(
            sender={"id": 5, "username": None},
            chat={"id": -100123, "message_id": None, "username": None},
        )
        assert _specs(rows) == [[("KeyboardButtonUrl", "💬 مراسلة", "tg://openmessage?user_id=5")]]
        # message_id موجود لكن لا chat_id ولا username → زر العرض مرفوض
        rows = build_dynamic_buttons(
            sender={"id": 5, "username": None},
            chat={"id": None, "message_id": 10, "username": None},
        )
        assert _specs(rows) == [[("KeyboardButtonUrl", "💬 مراسلة", "tg://openmessage?user_id=5")]]

    def test_single_row_layout(self):
        """الزران في صف واحد دائماً (row واحد داخل القائمة)."""
        rows = build_dynamic_buttons(
            sender={"id": 1, "username": "u1"},
            chat={"id": -1001, "message_id": 2, "username": "g1"},
        )
        assert rows is not None and len(rows) == 1
        assert len(rows[0]) == 2


class TestBuildAlertButtons:
    """التكامل عبر _build_alert — نفس الصف يشمل زر النسخ الاختياري."""

    @pytest.mark.asyncio
    async def test_full_row_with_copy(self, monitor):
        sender = {"id": 555000111, "display": "أ", "username": "ahmed_99", "access_hash": None}
        chat = {"group_link": "https://t.me/g", "title": "ت", "msg_link": "https://t.me/g/7",
                "id": -100123, "message_id": 7, "username": "g"}
        alert, buttons = monitor._build_alert(sender, chat, "ك", "نص", {"msg_hash": "xyz"})
        specs = _specs(buttons)
        assert len(specs) == 1  # صف واحد
        assert specs[0][0] == ("KeyboardButtonUrl", "💬 مراسلة", "https://t.me/ahmed_99")
        assert specs[0][1] == ("KeyboardButtonUrl", "📨 عرض الرسالة", "https://t.me/g/7")
        assert specs[0][2][1] == "📋 نسخ النص"  # callback في نفس الصف

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
            assert len(specs) == 1 and len(specs[0]) == 2  # بدون النسخ
            assert all(t != "📋 نسخ النص" for _, t, _ in specs[0])
        finally:
            object.__setattr__(CFG, "ALERT_WITH_COPY_BUTTON", True)
