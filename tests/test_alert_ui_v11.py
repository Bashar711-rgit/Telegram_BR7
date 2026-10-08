"""Alert UI Client — اختبارات الأزرار الحقيقية والعملية (v11.0).

المواصفة v11.0 (طلب المستخدم: «أزرار حقيقية وليست وهمية — أزرار عملية»):
  * AlertBotClient (alert_ui.py): عميل بوت Telethon حي بنفس توكن البوت —
    يستقبل CallbackQuery فعلياً (معالج «copy_» يجيب بنص التنبيه من DB)،
    ويحوّل أزرار build_alert_buttons إلى كائنات Telethon حقيقية
    (KeyboardButtonUrl / KeyboardButtonCopy / Callback).
  * سلّم الإرسال في alert_bot.AlertBot.send:
      Bot API ← عميل البوت الحي (بالأزرار) ← حسابات المستخدمين (نص فقط).
  * سلم أخطاء الأزرار: BUTTON_USER_* (مع زر tg://user) → إعادة بدون زر
    «مراسلة» فقط؛ أخطاء الأزرار العامة (BUTTON_URL_INVALID وغيرها) →
    إعادة **بدون أزرار** — التنبيه لا يضيع أبداً بسبب زر.
  * روابط المنتديات (topics): t.me/{u}/{topic}/{id} و t.me/c/{inner}/{topic}/{id}.

كل التفاعلات الشبكية مزيّفة — لا اتصالات حقيقية إطلاقاً.
"""

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from alert_bot import AlertBot, build_message_link
from alert_ui import AlertBotClient, _MODERN_TL
from config import CFG

if _MODERN_TL:
    from telethon.tl.types import (
        InlineButtonTypeCallback,
        InlineButtonTypeCopy,
        InlineButtonTypeUrl,
        KeyboardInlineButton,
    )
else:  # أجيال Telethon الأقدم
    from telethon.tl.types import (  # type: ignore
        KeyboardButtonCallback as _LegacyCallback,
        KeyboardButtonCopy as _LegacyCopy,
        KeyboardButtonUrl as _LegacyUrl,
    )

TARGET = int(CFG.TARGET_GROUP_ID)


def _data(**over) -> Dict[str, Any]:
    base = {
        "sender_id": 6079171409,
        "sender_username": None,
        "sender_display": "هناء العنزي",
        "text": "أبي مساعدة",
        "chat_id": -100111222333,
        "message_id": 4242,
        "chat_username": None,
    }
    base.update(over)
    return base


# ══ ══ ══ العميل الحي: الأساسيات ══ ══ ══
class TestClientBasics:
    @pytest.mark.asyncio
    async def test_no_token_start_returns_false_no_crash(self):
        """بلا توكن → start() تعيد False بلا أي استثناء (السلوك القديم محفوظ)."""
        c = AlertBotClient(api_id=1, api_hash="h", token=None, db=None, target_chat_id=TARGET)
        assert await c.start() is False
        assert c.client is None and c.is_alive is False
        await c.stop()  # لا يرفع حتى بدون تشغيل

    @pytest.mark.asyncio
    async def test_no_api_credentials_start_returns_false(self):
        """توكن بلا api_id/api_hash → False صادقة (لا محاولة شبكة عمياء)."""
        c = AlertBotClient(api_id=0, api_hash="", token="123:ABC", db=None)
        assert await c.start() is False
        assert c.is_alive is False


# ══ ══ ══ تحويل الأزرار — لا أزرار ميتة ══ ══ ══
def _assert_url(btn: Any, text: str, url: str) -> None:
    if _MODERN_TL:
        assert isinstance(btn, KeyboardInlineButton)
        assert btn.text == text and isinstance(btn.type, InlineButtonTypeUrl)
        assert btn.type.url == url
    else:
        assert isinstance(btn, _LegacyUrl)
        assert btn.text == text and btn.url == url


def _assert_copy(btn: Any, text: str, value: str) -> None:
    if _MODERN_TL:
        assert isinstance(btn, KeyboardInlineButton)
        assert btn.text == text and isinstance(btn.type, InlineButtonTypeCopy)
        assert btn.type.copy_text == value
    else:
        assert isinstance(btn, _LegacyCopy)
        assert btn.text == text and btn.copy_text == value


def _assert_callback(btn: Any, text: str, payload: bytes) -> None:
    if _MODERN_TL:
        assert isinstance(btn, KeyboardInlineButton)
        assert btn.text == text and isinstance(btn.type, InlineButtonTypeCallback)
        assert btn.type.data == payload
    else:
        assert isinstance(btn, _LegacyCallback)
        assert btn.text == text and btn.data == payload


class TestButtonConversion:
    def test_url_button_converts(self):
        row = AlertBotClient._convert_row(
            [{"text": "مراسلة", "url": "https://t.me/u1"}]
        )
        assert len(row) == 1
        _assert_url(row[0], "مراسلة", "https://t.me/u1")

    def test_copy_button_converts_to_real_copy(self):
        row = AlertBotClient._convert_row(
            [{"text": "نسخ", "copy_text": {"text": "@u1"}}]
        )
        assert len(row) == 1
        _assert_copy(row[0], "نسخ", "@u1")

    def test_callback_data_converts(self):
        row = AlertBotClient._convert_row(
            [{"text": "نسخ", "callback_data": "copy_abc123"}]
        )
        assert len(row) == 1
        _assert_callback(row[0], "نسخ", b"copy_abc123")

    def test_dead_and_empty_buttons_dropped(self):
        """زر بلا url/copy/callback أو بلا نص → يُسقط بصدق (لا زر وهمي)."""
        row = AlertBotClient._convert_row([
            {"text": "", "url": "https://t.me/x"},
            {"text": "ميت"},
            {"text": "نسخ", "copy_text": {"text": "  "}},
            {},
        ])
        assert row == []

    def test_markup_none_when_no_valid_rows(self):
        assert AlertBotClient._convert_markup([[]]) is None
        assert AlertBotClient._convert_markup([]) is None


# ══ ══ ══ المعالج الحي: نافذة نص التنبيه ══ ══ ══
class _FakeCallbackEvent:
    def __init__(self, data: bytes):
        self.data = data
        self.answered: List[tuple] = []

    async def answer(self, text: str, alert: bool = False):
        self.answered.append((text, alert))


class _FakeDb:
    def __init__(self, alert_text: Optional[str] = None, msg_text: Optional[str] = None,
                 raise_on_alert: bool = False):
        self.alert_text = alert_text
        self.msg_text = msg_text
        self.raise_on_alert = raise_on_alert

    async def get_alert_text_by_hash(self, h: str) -> Optional[str]:
        if self.raise_on_alert:
            raise RuntimeError("db down")
        return self.alert_text

    async def get_message_text_by_hash(self, h: str) -> Optional[str]:
        return self.msg_text


class TestCopyCallbackHandler:
    @pytest.mark.asyncio
    async def test_answers_with_alert_text_from_db(self):
        c = AlertBotClient(api_id=1, api_hash="h", token="123:ABC",
                           db=_FakeDb(alert_text="نص التنبيه الأصلي"))
        ev = _FakeCallbackEvent(b"copy_abc123")
        await c._handle_copy_callback(ev)
        assert ev.answered == [("نص التنبيه الأصلي", True)]

    @pytest.mark.asyncio
    async def test_falls_back_to_message_text(self):
        c = AlertBotClient(api_id=1, api_hash="h", token="123:ABC",
                           db=_FakeDb(msg_text="نص الرسالة الخام"))
        ev = _FakeCallbackEvent(b"copy_hash9")
        await c._handle_copy_callback(ev)
        assert ev.answered == [("نص الرسالة الخام", True)]

    @pytest.mark.asyncio
    async def test_unknown_hash_honest_answer(self):
        c = AlertBotClient(api_id=1, api_hash="h", token="123:ABC", db=_FakeDb())
        ev = _FakeCallbackEvent(b"copy_nope")
        await c._handle_copy_callback(ev)
        assert ev.answered == [("النص غير متوفر", True)]

    @pytest.mark.asyncio
    async def test_db_error_never_raises(self):
        c = AlertBotClient(api_id=1, api_hash="h", token="123:ABC",
                           db=_FakeDb(raise_on_alert=True))
        ev = _FakeCallbackEvent(b"copy_x")
        await c._handle_copy_callback(ev)
        assert ev.answered == [("حدث خطأ", True)]


# ══ ══ ══ روابط المنتديات (topics) الحقيقية ══ ══ ══
class TestTopicLinks:
    def test_private_forum_message_link(self):
        link = build_message_link(_data(topic_id=77))
        assert link == "https://t.me/c/111222333/77/4242"

    def test_public_forum_message_link(self):
        link = build_message_link(_data(chat_username="mygroup", topic_id=77))
        assert link == "https://t.me/mygroup/77/4242"

    def test_no_topic_keeps_legacy_links(self):
        assert build_message_link(_data()) == "https://t.me/c/111222333/4242"
        assert build_message_link(_data(chat_username="mygroup")) == "https://t.me/mygroup/4242"

    def test_zero_topic_ignored(self):
        assert build_message_link(_data(topic_id=0)) == "https://t.me/c/111222333/4242"


# ══ ══ ══ سلّم الإرسال: Bot API ← العميل الحي ← fallback ══ ══ ══
class _FakeAlertBot(AlertBot):
    def __init__(self, responses: List[tuple], **kw):
        super().__init__(token="123456:TEST-TOKEN", chat_id=TARGET, **kw)
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    async def _post(self, method: str, payload: Dict[str, Any]):
        self.calls.append({"method": method, "payload": payload})
        if self._responses:
            return self._responses.pop(0)
        return 200, {"ok": True}


class _FakeLiveClient:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.sent: List[tuple] = []

    @property
    def is_alive(self) -> bool:
        return True

    async def send_buttons(self, text_html: str, buttons: List[List[Dict[str, Any]]],
                           target: Any = None, **kwargs) -> bool:
        # v11.2: formatting_entities تُمرَّر من طبقة الإرسال الاحتياطية — تُسجَّل
        # للتحقق من أن الاسم قابل للنقر عبر كيانات جاهزة (بلا HTML parse).
        self.sent.append((text_html, buttons, kwargs))
        return self.ok


class TestSendLadder:
    @pytest.mark.asyncio
    async def test_generic_button_error_retries_text_only(self):
        """BUTTON_URL_INVALID → إعادة بدون أزرار عبر Bot API — التنبيه يُسلَّم."""
        bot = _FakeAlertBot([
            (400, {"ok": False, "description": "Bad Request: BUTTON_URL_INVALID"}),
            (200, {"ok": True}),
        ])
        ok, method, reason = await bot.send(_data(sender_username="user1"))
        assert ok is True and method == "text_only" and reason == ""
        assert len(bot.calls) == 2
        assert "reply_markup" not in bot.calls[1]["payload"]
        assert "reply_markup" in bot.calls[0]["payload"]

    @pytest.mark.asyncio
    async def test_persistent_button_error_uses_live_client(self):
        """خطأ زر مستمر → العميل الحي يرسل بنفس النص والأزرار."""
        bot = _FakeAlertBot([
            (400, {"ok": False, "description": "Bad Request: REPLY_MARKUP_INVALID"}),
            (400, {"ok": False, "description": "Bad Request: REPLY_MARKUP_INVALID"}),
        ])
        fake = _FakeLiveClient(ok=True)
        bot.set_client(fake)
        ok, method, reason = await bot.send(_data(sender_username="user1"))
        assert ok is True and method == "text_only"
        assert len(fake.sent) == 1
        # الأزرار الأربعة وصلت للعميل نفسها (بنية build_alert_buttons)
        row = fake.sent[0][1][0]
        assert [b["text"] for b in row] == ["مراسلة", "عرض الرسالة"]

    @pytest.mark.asyncio
    async def test_bot_api_failure_client_fallback_success(self):
        """فشل Bot API (غير أزرار) → العميل الحي يسلّم بالأزرار."""
        bot = _FakeAlertBot([(500, {"ok": False, "description": "Bad Gateway"})])
        bot.set_client(_FakeLiveClient(ok=True))
        ok, method, reason = await bot.send(_data(sender_username="user1"))
        assert ok is True and method == "username" and reason == ""

    @pytest.mark.asyncio
    async def test_client_failure_returns_reason_for_user_fallback(self):
        """فشل العميل أيضاً → ok=False بالسبب نفسه (حسابات المستخدمين تتكفل)."""
        bot = _FakeAlertBot([(500, {"ok": False, "description": "Bad Gateway"})])
        bot.set_client(_FakeLiveClient(ok=False))
        ok, method, reason = await bot.send(_data(sender_username="user1"))
        assert ok is False and method == ""
        assert reason == "bot_api_500:Bad Gateway"

    @pytest.mark.asyncio
    async def test_no_client_fails_as_before(self):
        """بلا عميل حي → العقد القديم حرفياً (ok=False والسبب محفوظ)."""
        bot = _FakeAlertBot([(500, {"ok": False, "description": "Bad Gateway"})])
        ok, method, reason = await bot.send(_data(sender_username="user1"))
        assert ok is False and reason == "bot_api_500:Bad Gateway"

    @pytest.mark.asyncio
    async def test_button_user_error_with_tme_button_never_text_retries(self):
        """العقد v10.8 محفوظ: BUTTON_USER_* بلا زر tg://user → لا إعادة نص —
        client fallback ثم فشل (حسابات المستخدمين)."""
        bot = _FakeAlertBot([
            (400, {"ok": False, "description": "Bad Request: BUTTON_USER_INVALID"}),
        ])
        ok, method, reason = await bot.send(_data(sender_username="user1"))
        assert ok is False and "BUTTON_USER_INVALID" in reason
        assert len(bot.calls) == 1  # لا إعادة عبر Bot API


# ══ ══ ══ v11.3: فحص كيان اسم المرسل + الإصلاح الذاتي ══ ══ ══
class TestNameEntityHeal:
    """السبب الجذري للشكوى «الاسم غير قابل للنقر»: الخادم يُسقط كيان
    tg://user?id من رسالة البوت حين لا يعرف المرسل — الاسم يصل نصاً.
    send_buttons يفحص كيانات الرسالة المُرسلة فعلاً ويُصلح فوراً بـedit
    (رابط الاسم = رابط الرسالة المصدر — يفتح دائماً)."""

    def _client(self, fake) -> AlertBotClient:
        c = AlertBotClient(api_id=1, api_hash="h", token="123:ABC", db=None,
                           target_chat_id=TARGET)
        c.client = fake
        c._started = True
        return c

    @pytest.mark.asyncio
    async def test_heals_when_name_entity_dropped(self):
        from types import SimpleNamespace

        edits: List[Dict[str, Any]] = []

        class _Fake:
            calls: List[Any] = []

            def is_connected(self):
                return True

            async def send_message(self, chat, text, **kw):
                # الخادم أرجع الرسالة بلا كيان اسم (أُسقط)
                return SimpleNamespace(id=55, entities=[])

            async def edit_message(self, chat, **kw):
                edits.append({"chat": chat, **kw})
                return SimpleNamespace(id=55, entities=[])

        pack_text = "👤: هناء العنزي\n\nأبي مساعدة"
        c = self._client(_Fake())
        ok = await c.send_buttons(
            pack_text, [[{"text": "مراسلة", "url": "tg://user?id=6079171409"}]],
            formatting_entities=[object()],
            heal_pack=(pack_text, None),
        )
        assert ok is True
        assert len(edits) == 1
        assert edits[0]["chat"] == TARGET
        assert edits[0]["message"] == 55
        assert edits[0]["text"] == pack_text

    @pytest.mark.asyncio
    async def test_no_edit_when_name_entity_present(self):
        from types import SimpleNamespace

        from telethon.tl.types import MessageEntityTextUrl

        class _Fake:
            def __init__(self):
                self.edits = 0

            def is_connected(self):
                return True

            async def send_message(self, chat, text, **kw):
                return SimpleNamespace(id=56, entities=[
                    MessageEntityTextUrl(offset=4, length=11, url="tg://user?id=6079171409"),
                ])

            async def edit_message(self, chat, **kw):
                self.edits += 1
                return SimpleNamespace(id=56)

        fake = _Fake()
        c = self._client(fake)
        ok = await c.send_buttons(
            "👤: هناء العنزي\n\nأبي مساعدة",
            [[{"text": "مراسلة", "url": "tg://user?id=6079171409"}]],
            formatting_entities=[object()],
            heal_pack=("x", None),
        )
        assert ok is True
        assert fake.edits == 0  # الاسم وصل قابلاً للنقر — لا edit إطلاقاً

    @pytest.mark.asyncio
    async def test_heal_failure_never_breaks_send(self):
        from types import SimpleNamespace

        class _Fake:
            def is_connected(self):
                return True

            async def send_message(self, chat, text, **kw):
                return SimpleNamespace(id=57, entities=[])

            async def edit_message(self, chat, **kw):
                raise RuntimeError("edit boom")

        c = self._client(_Fake())
        ok = await c.send_buttons(
            "👤: هناء\n\nنص", [[{"text": "مراسلة", "url": "tg://user?id=1"}]],
            formatting_entities=[object()],
            heal_pack=("👤: هناء\n\nنص", None),
        )
        assert ok is True  # الإصلاح ميزة — فشله لا يكسر الإرسال
