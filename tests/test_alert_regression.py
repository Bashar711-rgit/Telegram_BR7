"""Alert regression tests (engineering brief requirement #41).

🚨 THE CONTRACT: the user-facing alert output must be stable across
deploys. These tests pin the EXACT expected strings/buttons produced by
the current code.

⚠️ v9.11 (2026-09) — GOLDEN UPDATED BY EXPLICIT USER REQUEST (المرحلة الأولى):
الأزرار تغيّرت من صف واحد
    [ 💬 مراسلة ] [ 📨 عرض الرسالة ] [ 📋 نسخ النص ]
إلى صفّين بالتسميات المطلوبة حرفياً:
    الصف 1: [ عرض الرسالة ] [ تواصل مع المرسل ]
    الصف 2: [ مراسلة ] [ 📋 نسخ النص ]
بقواعد:
  * عرض الرسالة: t.me/{chat}/{msg} عامة أو t.me/c/{inner}/{msg} خاصة
  * تواصل مع المرسل: inline callback (cnt_{msg_hash}) → قائمة الرسائل الجاهزة
  * مراسلة: t.me/{username} عند توفره، وإلا tg://openmessage?user_id=
  * الزر الذي تفتقر بياناته لا يُعرض إطلاقاً (لا أزرار مكسورة)
  * زر النسخ (ميزة قائمة من v9.10) انتقل للصف الثاني — لم يُحذف
نص التنبيه HTML نفسه لم يتغير إطلاقاً (نفس الحقول، نفس الترتيب، نفس
الروابط داخل النص).

If ANY of these tests fail, the alert format changed and the release
must be considered broken. Labels, order, URLs and callback data are
all verified.

ملاحظة توافق: مساعد مواصفات الأزرار يعمل مع Telethon القديم
(KeyboardButtonUrl/KeyboardButtonCallback) والجديد (KeyboardInlineButton).
"""

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from telethon import Button  # noqa: E402

from config import CFG  # noqa: E402
from monitors import EnhancedAccountMonitor  # noqa: E402

import pytest  # noqa: E402


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


def _button_specs(buttons):
    """[(kind, text, url, callback-data)] for every row, order-preserved."""
    specs = []
    for row in buttons or []:
        specs.append([_btn_spec(b) for b in row])
    return specs


# ── GOLDEN OUTPUT (v9.11 three requested buttons — two rows) ────────────
GOLDEN = {
    "S1_username_chat": {
        "alert": (
            '<b>الرسالة:</b>\nأبي مساعدة في واجب الاحصاء ضروري\n\n'
            '👤: <a href="https://t.me/ahmed_99">أحمد محمد</a>\n\n'
            '<blockquote dir="rtl"><a href="https://t.me/mygroup">مجموعة الطلاب</a>\n\n'
            '<a href="https://t.me/mygroup/123"><b>عرض الرسالة الأصلية</b></a></blockquote>'
        ),
        # username + مجموعة عامة → الصفان الكاملان
        "buttons": [
            [
                ("url", "عرض الرسالة", "https://t.me/mygroup/123"),
                ("callback", "تواصل مع المرسل", "cnt_abc123"),
            ],
            [
                ("url", "مراسلة", "https://t.me/ahmed_99"),
                ("callback", "📋 نسخ النص", "copy_abc123"),
            ],
        ],
    },
    "S2_private_chat": {
        "alert": (
            '<b>الرسالة:</b>\nأبي مساعدة في واجب الاحصاء ضروري\n\n'
            '👤: <a href="tg://user?id=777000222">سارة</a>\n\n'
            '<blockquote dir="rtl"><a href="https://t.me/c/1234567890">مجموعة خاصة</a>\n\n'
            '<a href="https://t.me/c/1234567890/456"><b>عرض الرسالة الأصلية</b></a></blockquote>'
        ),
        # لا username → مراسلة عبر openmessage + مجموعة خاصة → t.me/c/inner/msg
        "buttons": [
            [
                ("url", "عرض الرسالة", "https://t.me/c/1234567890/456"),
                ("callback", "تواصل مع المرسل", "cnt_abc123"),
            ],
            [
                ("url", "مراسلة", "tg://openmessage?user_id=777000222"),
                ("callback", "📋 نسخ النص", "copy_abc123"),
            ],
        ],
    },
    "S3_no_username_hash": {
        "alert": (
            '<b>الرسالة:</b>\nأبي مساعدة في واجب الاحصاء ضروري\n\n'
            '👤: <a href="tg://openmessage?user_id=888000333">خالد</a>\n\n'
            '<blockquote dir="rtl">الرابط غير متاح</blockquote>'
        ),
        # بيانات ناقصة: لا chat_id/message_id → زر العرض لا يُعرض إطلاقاً
        # (لا أزرار مكسورة) — يبقى التواصل + مراسلة + النسخ.
        "buttons": [
            [
                ("callback", "تواصل مع المرسل", "cnt_abc123"),
            ],
            [
                ("url", "مراسلة", "tg://openmessage?user_id=888000333"),
                ("callback", "📋 نسخ النص", "copy_abc123"),
            ],
        ],
    },
    "S4_unknown_title": {
        "alert": (
            '<b>الرسالة:</b>\nأبي مساعدة في واجب الاحصاء ضروري\n\n'
            '👤: <a href="https://t.me/user_x">مستخدم</a>\n\n'
            '<blockquote dir="rtl"><a href="https://t.me/eng_group/789">'
            '<b>عرض الرسالة الأصلية</b></a></blockquote>'
        ),
        # username + مجموعة عامة → الصفان الكاملان (حتى مع title="غير معروف")
        "buttons": [
            [
                ("url", "عرض الرسالة", "https://t.me/eng_group/789"),
                ("callback", "تواصل مع المرسل", "cnt_abc123"),
            ],
            [
                ("url", "مراسلة", "https://t.me/user_x"),
                ("callback", "📋 نسخ النص", "copy_abc123"),
            ],
        ],
    },
}

TEXT = "أبي مساعدة في واجب الاحصاء ضروري"


@pytest.fixture(scope="module")
def monitor():
    account = {
        "name": "GoldenAcc", "api_id": 12345, "api_hash": "h",
        "phone": "+90000000000", "session": "golden", "priority": 1,
    }
    return EnhancedAccountMonitor(account, db=None, flt=None)


class TestAlertRegression:
    """EXPECTED_ALERT == ACTUAL_ALERT at 100% (brief #41)."""

    @pytest.mark.asyncio
    async def test_s1_username_chat(self, monitor):
        sender = {"id": 555000111, "display": "أحمد محمد", "username": "ahmed_99", "access_hash": 7234567890123}
        # _send_alert يضيف id/message_id/username إلى chat_info —
        # الاختبار يحاكي نفس البنية الفعلية بعد الدمج.
        chat = {
            "group_link": "https://t.me/mygroup", "title": "مجموعة الطلاب", "msg_link": "https://t.me/mygroup/123",
            "id": -1001234567890, "message_id": 123, "username": "mygroup",
        }
        alert, buttons = monitor._build_alert(sender, chat, "واجب", TEXT, {"msg_hash": "abc123"})
        assert alert == GOLDEN["S1_username_chat"]["alert"]
        assert _button_specs(buttons) == GOLDEN["S1_username_chat"]["buttons"]

    @pytest.mark.asyncio
    async def test_s2_private_chat(self, monitor):
        sender = {"id": 777000222, "display": "سارة", "username": None, "access_hash": None}
        chat = {
            "group_link": "https://t.me/c/1234567890", "title": "مجموعة خاصة", "msg_link": "https://t.me/c/1234567890/456",
            "id": -1001234567890, "message_id": 456, "username": None,
        }
        alert, buttons = monitor._build_alert(sender, chat, "واجب", TEXT, {"msg_hash": "abc123"})
        assert alert == GOLDEN["S2_private_chat"]["alert"]
        assert _button_specs(buttons) == GOLDEN["S2_private_chat"]["buttons"]

    @pytest.mark.asyncio
    async def test_s3_no_username_hash(self, monitor):
        sender = {"id": 888000333, "display": "خالد", "username": None, "access_hash": 9998877766655}
        chat = {"group_link": "#", "title": None, "msg_link": "#"}
        alert, buttons = monitor._build_alert(sender, chat, "واجب", TEXT, {"msg_hash": "abc123"})
        assert alert == GOLDEN["S3_no_username_hash"]["alert"]
        assert _button_specs(buttons) == GOLDEN["S3_no_username_hash"]["buttons"]

    @pytest.mark.asyncio
    async def test_s4_unknown_title(self, monitor):
        sender = {"id": 999000444, "display": "مستخدم", "username": "user_x", "access_hash": 111222333}
        chat = {
            "group_link": "https://t.me/eng_group", "title": "غير معروف", "msg_link": "https://t.me/eng_group/789",
            "id": -1009876543210, "message_id": 789, "username": "eng_group",
        }
        alert, buttons = monitor._build_alert(sender, chat, "واجب", TEXT, {"msg_hash": "abc123"})
        assert alert == GOLDEN["S4_unknown_title"]["alert"]
        assert _button_specs(buttons) == GOLDEN["S4_unknown_title"]["buttons"]

    @pytest.mark.asyncio
    async def test_sender_intel_does_not_leak_into_alert(self, monitor):
        """The enriched sender dict (extra keys) must render the SAME alert:
        the resolver feeds data through existing keys only."""
        sender_rich = {
            "id": 555000111, "display": "أحمد محمد", "username": "ahmed_99",
            "access_hash": 7234567890123,
            # extra keys that v9.9 internals may add — must be ignored by _build_alert
            "is_premium": True, "is_verified": True, "phone": "+966500000000",
        }
        chat = {
            "group_link": "https://t.me/mygroup", "title": "مجموعة الطلاب", "msg_link": "https://t.me/mygroup/123",
            "id": -1001234567890, "message_id": 123, "username": "mygroup",
        }
        alert, _ = monitor._build_alert(sender_rich, chat, "واجب", TEXT, {"msg_hash": "abc123"})
        assert alert == GOLDEN["S1_username_chat"]["alert"]

    @pytest.mark.asyncio
    async def test_buttons_contract_config(self):
        """The alert-button contract flags must be consistent (v9.11)."""
        assert CFG.ALERT_WITH_BUTTONS is True
        assert CFG.ALERT_WITH_COPY_BUTTON is True
        assert CFG.ALERT_WITH_CONTACT_BUTTON is True
