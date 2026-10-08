"""Alert regression tests (engineering brief requirement #41).

🚨 THE CONTRACT: the user-facing alert output must be stable across
deploys. These tests pin the EXACT expected text/buttons produced by
the current code.

⚠️ v11.3 (2026-10) — GOLDEN UPDATED BY EXPLICIT USER REQUEST (بلا سطر «💬:»):
نمط واجهة موحد لكل التنبيهات دون استثناء (parse_mode=HTML):

    سطر 1:  👤: {SENDER}
    (سطر فارغ)
    <blockquote>{النص الأصلي كاملاً — نسخ بالضغط (Copy Text)}</blockquote>

    [ مراسلة ]   [ عرض الرسالة ]

SENDER:
  - username:  <a href="https://t.me/U">الاسم</a>
  - بدونه:     <a href="tg://user?id=ID">الاسم الكامل</a>
    (إن أُسقط الكيان من الخادم يُصلح تلقائياً برابط الرسالة المصدر —
     v11.3 self-heal، انظر test_bot_api_heals_dropped_name_entity).

الأزرار (v10.9.1): Inline Keyboard صف واحد أسفل كل تنبيه مباشرة، ترتيب
RTL («مراسلة» في أقصى اليمين كما في الصورة المرجعية) — نصوص مصغّرة
(تيليجرام يحسب حجم الزر من طول نصه حصراً — لا تحكم API في الخط/الحاشية):

    [ مراسلة ] [ عرض الرسالة ]

تُبنى حصراً عبر الدالة المركزية alert_bot.build_alert_buttons وترسل عبر
بوت التنبيهات (Bot API). حسابات المستخدمين لا ترسل أزراراً — _build_alert
يعيد buttons=None دائماً. النسخ بالضغط على نص الرسالة (quote → Copy Text).
الزر الذي تنعدم بياناته يُحذف (لا روابط وهمية ولا أزرار معطلة).

If ANY of these tests fail, the alert format changed and the release
must be considered broken. Labels, order and URLs are all verified.
"""

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from config import CFG  # noqa: E402
from monitors import EnhancedAccountMonitor  # noqa: E402
from alert_bot import build_alert_html  # noqa: E402

import pytest  # noqa: E402


# ── GOLDEN OUTPUT (v11.3 — القالب الموحد المطلوب حرفياً) ────────────────
GOLDEN = {
    "S1_username_chat": {
        "alert": (
            '👤: <a href="https://t.me/ahmed_99">أحمد محمد</a>'
            '\n\n'
            '<blockquote>أبي مساعدة في واجب الاحصاء ضروري</blockquote>'
        ),
        # أزرار بوت التنبيهات: صف واحد بترتيب RTL
        "bot_buttons": [
            {"text": "مراسلة", "url": "https://t.me/ahmed_99"},
            {"text": "عرض الرسالة", "url": "https://t.me/mygroup/123"},
        ],
    },
    "S2_private_chat": {
        "alert": (
            '👤: <a href="tg://user?id=777000222">سارة</a>'
            '\n\n'
            '<blockquote>أبي مساعدة في واجب الاحصاء ضروري</blockquote>'
        ),
        "bot_buttons": [
            {"text": "مراسلة", "url": "tg://user?id=777000222"},
            {"text": "عرض الرسالة", "url": "https://t.me/c/1234567890/456"},
        ],
    },
    "S3_no_username_hash": {
        "alert": (
            '👤: <a href="tg://user?id=888000333">خالد</a>'
            '\n\n'
            '<blockquote>أبي مساعدة في واجب الاحصاء ضروري</blockquote>'
        ),
        # بلا روابط → يختفي زر «عرض الرسالة» بصدق (لا روابط وهمية)
        "bot_buttons": [
            {"text": "مراسلة", "url": "tg://user?id=888000333"},
        ],
    },
    "S4_unknown_title": {
        "alert": (
            '👤: <a href="https://t.me/user_x">مستخدم</a>'
            '\n\n'
            '<blockquote>أبي مساعدة في واجب الاحصاء ضروري</blockquote>'
        ),
        "bot_buttons": [
            {"text": "مراسلة", "url": "https://t.me/user_x"},
            {"text": "عرض الرسالة", "url": "https://t.me/eng_group/789"},
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


def _buttons_row_of(built):
    return built["buttons"][0] if built["buttons"] else []


class TestAlertRegression:
    """EXPECTED_ALERT == ACTUAL_ALERT at 100% (brief #41).

    v10.9: القالب الموحد عبر alert_bot.build_alert_html. حسابات
    المستخدمين لا ترسل أزراراً (buttons=None) — الأزرار الأربعة من بوت
    التنبيهات.
    """

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
        assert buttons is None
        built = build_alert_html(
            {"sender_id": 555000111, "sender_username": "ahmed_99", "sender_display": "أحمد محمد", "text": TEXT,
             "chat_id": -1001234567890, "message_id": 123, "chat_username": "mygroup"},
            msg_link="https://t.me/mygroup/123", group_link="https://t.me/mygroup",
        )
        assert _buttons_row_of(built) == GOLDEN["S1_username_chat"]["bot_buttons"]

    @pytest.mark.asyncio
    async def test_s2_private_chat(self, monitor):
        sender = {"id": 777000222, "display": "سارة", "username": None, "access_hash": None}
        chat = {
            "group_link": "https://t.me/c/1234567890", "title": "مجموعة خاصة", "msg_link": "https://t.me/c/1234567890/456",
            "id": -1001234567890, "message_id": 456, "username": None,
        }
        alert, buttons = monitor._build_alert(sender, chat, "واجب", TEXT, {"msg_hash": "abc123"})
        assert alert == GOLDEN["S2_private_chat"]["alert"]
        assert buttons is None
        built = build_alert_html(
            {"sender_id": 777000222, "sender_username": None, "sender_display": "سارة", "text": TEXT,
             "chat_id": -1001234567890, "message_id": 456, "chat_username": None},
            msg_link="https://t.me/c/1234567890/456", group_link="https://t.me/c/1234567890",
        )
        assert _buttons_row_of(built) == GOLDEN["S2_private_chat"]["bot_buttons"]

    @pytest.mark.asyncio
    async def test_s3_no_username_hash(self, monitor):
        sender = {"id": 888000333, "display": "خالد", "username": None, "access_hash": 9998877766655}
        chat = {"group_link": "#", "title": None, "msg_link": "#"}
        alert, buttons = monitor._build_alert(sender, chat, "واجب", TEXT, {"msg_hash": "abc123"})
        assert alert == GOLDEN["S3_no_username_hash"]["alert"]
        assert buttons is None
        # بلا chat_id/message_id في chat → لا رابط قروب → الزران يُحذفان بصدق
        built = build_alert_html(
            {"sender_id": 888000333, "sender_username": None, "sender_display": "خالد", "text": TEXT},
        )
        assert _buttons_row_of(built) == GOLDEN["S3_no_username_hash"]["bot_buttons"]

    @pytest.mark.asyncio
    async def test_s4_unknown_title(self, monitor):
        sender = {"id": 999000444, "display": "مستخدم", "username": "user_x", "access_hash": 111222333}
        chat = {
            "group_link": "https://t.me/eng_group", "title": "غير معروف", "msg_link": "https://t.me/eng_group/789",
            "id": -1009876543210, "message_id": 789, "username": "eng_group",
        }
        alert, buttons = monitor._build_alert(sender, chat, "واجب", TEXT, {"msg_hash": "abc123"})
        assert alert == GOLDEN["S4_unknown_title"]["alert"]
        assert buttons is None
        built = build_alert_html(
            {"sender_id": 999000444, "sender_username": "user_x", "sender_display": "مستخدم", "text": TEXT,
             "chat_id": -1009876543210, "message_id": 789, "chat_username": "eng_group"},
            msg_link="https://t.me/eng_group/789", group_link="https://t.me/eng_group",
        )
        assert _buttons_row_of(built) == GOLDEN["S4_unknown_title"]["bot_buttons"]

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
    async def test_alerts_never_carry_buttons_from_user_accounts(self, monitor):
        """v10.9: حُذف Button.inline/Button.url (cnt_/copy_) من بناء
        التنبيه بالكامل — حسابات المستخدمين لا ترسل أزراراً مهما كانت
        المفاتيح (الأزرار الأربعة حصراً من بوت التنبيهات عبر Bot API)."""
        old = getattr(CFG, "ALERT_BUTTONS_ENABLED", False)
        try:
            object.__setattr__(CFG, "ALERT_BUTTONS_ENABLED", True)
            sender = {"id": 555000111, "display": "أحمد محمد", "username": "ahmed_99", "access_hash": 7234567890123}
            chat = {
                "group_link": "https://t.me/mygroup", "title": "مجموعة الطلاب", "msg_link": "https://t.me/mygroup/123",
                "id": -1001234567890, "message_id": 123, "username": "mygroup",
            }
            alert, buttons = monitor._build_alert(
                sender, chat, "واجب", TEXT,
                {"msg_hash": "abc123", "rule_tag": "قاعدة"},
            )
            assert buttons is None
            assert "cnt_abc123" not in alert
            assert "copy_abc123" not in alert
            # سطر القاعدة (ميزة محفوظة) يُلاحق في النهاية بعد البطاقة
            assert "🏷 قاعدة: قاعدة" in alert
        finally:
            object.__setattr__(CFG, "ALERT_BUTTONS_ENABLED", old)

    @pytest.mark.asyncio
    async def test_bot_api_payload_shape(self, monitor):
        """حمولة Bot API: parse_mode=HTML + link_preview_options disabled
        + صف أزرار واحد [مراسلة][عرض الرسالة]
        (طريقة الإرسال المطلوبة حرفياً).

        v11.3: الاستجابة تحمل كيانات الرسالة الفعلية — عند وجود text_link
        لموضع الاسم لا يحدث أي edit (الاسم قابل للنقر فعلاً)."""
        from alert_bot import AlertBot

        class _CaptureBot(AlertBot):
            def __init__(self):
                super().__init__(token="1:t", chat_id=int(CFG.TARGET_GROUP_ID))
                self.captured = None

            async def _post(self, method, payload):
                self.captured = (method, payload)
                # استجابة واقعية: كيان text_link عند موضع الاسم (offset=4)
                return 200, {"ok": True, "result": {"message_id": 11, "entities": [
                    {"type": "text_link", "offset": 4, "length": 9, "url": "https://t.me/ahmed_99"},
                ]}}

        bot = _CaptureBot()
        ok, method, reason = await bot.send(
            {"sender_id": 555000111, "sender_username": "ahmed_99", "sender_display": "أحمد محمد",
             "text": TEXT, "chat_id": -1001234567890, "message_id": 123, "chat_username": "mygroup"},
            msg_link="https://t.me/mygroup/123", group_link="https://t.me/mygroup",
        )
        assert ok is True and method == "username" and reason == ""
        m, payload = bot.captured
        assert m == "sendMessage"
        assert payload["chat_id"] == int(CFG.TARGET_GROUP_ID)
        assert payload["parse_mode"] == "HTML"
        assert payload["link_preview_options"] == {"is_disabled": True}
        kb = payload["reply_markup"]["inline_keyboard"]
        assert len(kb) == 1 and kb[0] == GOLDEN["S1_username_chat"]["bot_buttons"]

    @pytest.mark.asyncio
    async def test_bot_api_heals_dropped_name_entity(self, monitor):
        """v11.3 — السبب الجذري للشكوى «الاسم غير قابل للنقر»: Bot API يقبل
        الرسالة لكنه يُسقط كيان tg://user?id عندما لا يعرف البوت المرسل.
        الاستجابة بلا text_link عند موضع الاسم → editMessageText فوري:
        نفس النص والأزرار، ورابط الاسم = رابط الرسالة المصدر (يفتح دائماً)."""
        from alert_bot import AlertBot

        class _DroppingBot(AlertBot):
            def __init__(self):
                super().__init__(token="1:t", chat_id=int(CFG.TARGET_GROUP_ID))
                self.methods = []

            async def _post(self, method, payload):
                self.methods.append((method, dict(payload)))
                if method == "sendMessage":
                    # استجابة بدون كيان الاسم — أُسقط من الخادم
                    return 200, {"ok": True, "result": {"message_id": 22, "entities": []}}
                # editMessageText
                return 200, {"ok": True, "result": {"message_id": 22}}

        bot = _DroppingBot()
        ok, method, reason = await bot.send(
            {"sender_id": 777000222, "sender_display": "سارة",
             "text": TEXT, "chat_id": -1001234567890, "message_id": 456},
            msg_link="https://t.me/c/1234567890/456", group_link="https://t.me/c/1234567890",
        )
        assert ok is True and reason == ""
        # الاسم لم يصل قابلاً للنقر → تسجيل صادق
        assert method == "text_only"
        methods = [m for m, _ in bot.methods]
        assert "editMessageText" in methods
        edit_payload = next(p for m, p in bot.methods if m == "editMessageText")
        # نفس النص لكن رابط الاسم صار رابط الرسالة المصدر
        assert edit_payload["text"].startswith('👤: <a href="https://t.me/c/1234567890/456">سارة</a>')
        assert "<blockquote>" in edit_payload["text"]
        # نفس الأزرار بقيت على الرسالة المعدّلة
        assert edit_payload["reply_markup"]["inline_keyboard"][0][0]["url"] == "tg://user?id=777000222"

    @pytest.mark.asyncio
    async def test_bot_api_no_edit_when_name_entity_present(self, monitor):
        """v11.3: وصول كيان الاسم = لا edit إطلاقاً (لا طلبات زائدة)."""
        from alert_bot import AlertBot

        class _HealthyBot(AlertBot):
            def __init__(self):
                super().__init__(token="1:t", chat_id=int(CFG.TARGET_GROUP_ID))
                self.methods = []

            async def _post(self, method, payload):
                self.methods.append(method)
                return 200, {"ok": True, "result": {"message_id": 33, "entities": [
                    {"type": "text_link", "offset": 4, "length": 4, "url": "tg://user?id=777000222"},
                ]}}

        bot = _HealthyBot()
        ok, method, _ = await bot.send(
            {"sender_id": 777000222, "sender_display": "سارة",
             "text": TEXT, "chat_id": -1001234567890, "message_id": 456},
            msg_link="https://t.me/c/1234567890/456",
        )
        assert ok is True and method == "mention_button"
        assert bot.methods == ["sendMessage"]


@pytest.mark.asyncio
async def test_buttons_contract_config():
    """The alert-button contract flags must be consistent (v9.11).
    v10.9: الأزرار عبر بوت التنبيهات — مفاتيح الأزرار القديمة محفوظة
    بقيمها التاريخية (تؤثر فقط على build_dynamic_buttons المحفوظ)."""
    assert getattr(CFG, "ALERT_BUTTONS_ENABLED", None) is False
    assert CFG.ALERT_WITH_BUTTONS is True
    assert CFG.ALERT_WITH_COPY_BUTTON is True
    assert CFG.ALERT_WITH_CONTACT_BUTTON is True


@pytest.mark.asyncio
async def test_alert_bot_config_contract():
    """v10.8: مفاتيح بوت التنبيهات — التوكن اختياري والمفتاح الافتراضي
    مفعّل (غياب التوكن = fallback فقط، بلا أعطال)."""
    assert getattr(CFG, "ALERT_BOT_TOKEN", None) is None  # لا توكن في بيئة الاختبار
    assert getattr(CFG, "ALERT_BOT_ENABLED", None) is True
    assert getattr(CFG, "ALERT_BOT_MAX_RETRIES", None) == 3
