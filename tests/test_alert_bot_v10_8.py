"""v10.8 Alert Bot — اختبارات القبول الستة + وحدات البناء والإرسال.

المواصفة: تغيير شكل وطريقة إرسال التنبيهات إلى TARGET_GROUP_ID:
  * النمط الجديد (parse_mode=HTML):
      سطر 1: 👤 {SENDER}    — username: <a href="https://t.me/U">@U</a>
                              بدونه: <a href="tg://user?id=ID">الاسم</a>
      سطر 2: <b>المرسل :</b> ID {id}
      (فارغ) <b>نص الرسالة :</b>\n{text escape + truncate(400)}
      <b>رابط الرسالة :</b> {t.me/{u}/{id} | t.me/c/{inner}/{id} | غير متاح}
  * أزرار inline URL في صف واحد: [زر المرسل][جروب] — يُحذف ما انعدمت
    بياناته. زر المرسل @USERNAME أو الاسم (tg://user?id=).
  * الإرسال عبر Bot API sendMessage بـ aiohttp + link_preview_options
    disabled؛ 429 → retry_after (≤3)؛ BUTTON_USER_INVALID /
    BUTTON_USER_PRIVACY_RESTRICTED → إعادة بدون زر المرسل؛ أي فشل/غياب
    ALERT_BOT_TOKEN → fallback لحسابات المستخدمين بنفس النص بدون أزرار.
  * contact_method = username | mention_button | text_only.

كل تفاعلات الشبكة مزيّفة — لا نداءات حقيقية إطلاقاً.
"""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import alert_bot as ab
from alert_bot import AlertBot, build_alert_html, MSG_LINK_UNAVAILABLE

import monitors as mon
from config import CFG
from database import AlertRecord

SENDER_ID = 6079171409
CHAT_ID = -100111222333
INNER = "111222333"
MSG_ID = 4242
TARGET = int(CFG.TARGET_GROUP_ID)


@pytest.fixture(autouse=True)
def _clean_shared_module_state():
    yield
    mon._post_capable_accounts.clear()
    try:
        from dedup import get_deduplicator as _gd
        _gd()._mem.clear()
    except Exception:
        pass
    try:
        from similarity import get_similarity_gate as _gs
        _gs()._recent.clear()
    except Exception:
        pass


def _data(**kw) -> Dict[str, Any]:
    base = {
        "chat_id": CHAT_ID, "message_id": MSG_ID, "sender_id": SENDER_ID,
        "sender_username": None, "sender_first_name": "هناء", "sender_last_name": "العنزي",
        "sender_access_hash": 7234567890123, "chat_access_hash": 4242,
        "chat_username": None, "chat_title": "مجموعة الطلاب",
        "text": "عندي    مشروع حلو وعمل ممتاز من يبي يدخل خاص",
        "account_name": "Account 1", "owner_account": "Account 1",
        "sender_type": "user", "sender_phone": None, "sender_usernames": None,
        "msg_date_ts": time.time(), "receive_lag_ms": 120.0,
    }
    base.update(kw)
    return base


# ═════════════════ build_alert_html — البناء ═════════════════

class TestBuildAlertHtml:
    def test_acceptance_1_username_sender(self):
        """مرسل بـ username: سطر 👤 @user + زر @user + زر جروب."""
        built = build_alert_html(
            _data(sender_username="Lara507", chat_username="sultanu1999"),
            msg_link="https://t.me/sultanu1999/531011",
            group_link="https://t.me/sultanu1999",
        )
        assert built["contact_method"] == "username"
        assert built["text"].startswith('👤 <a href="https://t.me/Lara507">@Lara507</a>\n')
        assert "<b>المرسل :</b> ID 6079171409" in built["text"]
        assert "\n\n<b>نص الرسالة :</b>\n" in built["text"]
        assert '<b>رابط الرسالة :</b> <a href="https://t.me/sultanu1999/531011">https://t.me/sultanu1999/531011</a>' in built["text"]
        assert built["sender_button"] == {"text": "@Lara507", "url": "https://t.me/Lara507"}
        assert built["group_button"] == {"text": "جروب", "url": "https://t.me/sultanu1999"}

    def test_acceptance_2_no_username_name_button(self):
        """مرسل بلا username: الاسم رابط tg://user في النص + زر بالاسم."""
        built = build_alert_html(_data(), msg_link=None, group_link=None)
        assert built["contact_method"] == "mention_button"
        assert built["text"].startswith('👤 <a href="tg://user?id=6079171409">هناء العنزي</a>\n')
        assert built["sender_button"] == {"text": "هناء العنزي", "url": "tg://user?id=6079171409"}

    def test_acceptance_3_private_group_message_link(self):
        """قروب خاص (id يبدأ -100): رابط الرسالة بصيغة t.me/c/{inner}/{id}."""
        built = build_alert_html(_data())  # بلا username للقروب، بلا overrides
        assert f"https://t.me/c/{INNER}/{MSG_ID}" in built["text"]
        assert built["group_button"] == {"text": "جروب", "url": f"https://t.me/c/{INNER}"}
        assert built["msg_link"] == f"https://t.me/c/{INNER}/{MSG_ID}"

    def test_public_group_message_link(self):
        built = build_alert_html(_data(chat_username="mygroup", message_id=789))
        assert "https://t.me/mygroup/789" in built["text"]
        assert built["group_button"]["url"] == "https://t.me/mygroup"

    def test_acceptance_4_no_link_unavailable_no_group_button(self):
        """قروب بلا رابط: تظهر «غير متاح» ويختفي زر جروب."""
        built = build_alert_html(_data(chat_id=-987654321))  # مجموعة عادية
        assert MSG_LINK_UNAVAILABLE in built["text"]
        assert built["group_button"] is None
        assert built["msg_link"] is None
        # سطر الرابط لا يحمل أي anchor
        tail = built["text"].rsplit("\n", 1)[-1]
        assert tail == f"<b>رابط الرسالة :</b> {MSG_LINK_UNAVAILABLE}"

    def test_text_escape_and_truncate_400(self):
        long_text = "ع" * 600
        built = build_alert_html(_data(text=long_text))
        body = built["text"].split("<b>نص الرسالة :</b>\n")[1].split("\n")[0]
        # truncate(400) = 399 حرفاً + "..." (سلوك InputSanitizer القائم)
        assert len(body) == 402 and body.endswith("...")
        assert len(body.rstrip(".")) == 399
        esc = build_alert_html(_data(text="<b>خط & إهمال</b>"))
        assert "&lt;b&gt;خط &amp; إهمال&lt;/b&gt;" in esc["text"]

    def test_html_escaping_in_display_name(self):
        built = build_alert_html(_data(sender_first_name="<سك>rip", sender_last_name=None))
        assert '<a href="tg://user?id=6079171409">&lt;سك&gt;rip</a>' in built["text"]
        assert built["sender_button"]["text"] == "<سك>rip"  # نص الزر JSON خام

    def test_sender_usernames_fallback(self):
        """بلا username أساسي → أول usernames النشطة يُستخدم."""
        ru = SimpleNamespace(username="fallback_u", active=True)
        built = build_alert_html(_data(sender_usernames=[ru]))
        assert built["contact_method"] == "username"
        assert built["sender_button"]["url"] == "https://t.me/fallback_u"

    def test_multiple_active_usernames(self):
        ru1 = SimpleNamespace(username="main_u", active=True)
        ru2 = SimpleNamespace(username="second_u", active=True)
        built = build_alert_html(_data(sender_username="main_u", sender_usernames=[ru1, ru2]))
        assert built["sender_button"]["text"] == "@main_u"

    def test_rule_tag_feature_preserved(self):
        built = build_alert_html(_data(), {"rule_tag": "قاعدة تجريبية"})
        assert "\n\n🏷 قاعدة: قاعدة تجريبية" in built["text"]

    def test_no_rule_tag_by_default(self):
        built = build_alert_html(_data(), {"rule_tag": None})
        assert "🏷" not in built["text"]

    def test_never_raises_on_garbage(self):
        built = build_alert_html({"sender_id": "x", "chat_id": None, "message_id": None, "text": None})
        assert "👤 " in built["text"] and MSG_LINK_UNAVAILABLE in built["text"]


# ═════════════════ AlertBot — الإرسال عبر Bot API ═════════════════

class _FakeAlertBot(AlertBot):
    """AlertBot مع _post مزيّف — قائمة ردود (status, body) بالترتيب."""

    def __init__(self, responses: List[Tuple[int, Dict[str, Any]]], **kw):
        super().__init__(token="123456:TEST-TOKEN", chat_id=TARGET, **kw)
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    async def _post(self, method: str, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        self.calls.append({"method": method, "payload": payload})
        if self._responses:
            return self._responses.pop(0)
        return 200, {"ok": True, "result": {"message_id": 777}}


def _buttons_of(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    rm = payload.get("reply_markup") or {}
    kb = rm.get("inline_keyboard") or []
    return kb[0] if kb else []


class TestAlertBotSend:
    @pytest.mark.asyncio
    async def test_send_message_payload_shape(self):
        """حمولة sendMessage: chat_id/text/parse_mode=HTML/link_preview_options
        disabled + reply_markup صف واحد."""
        bot = _FakeAlertBot([(200, {"ok": True})])
        ok, method, reason = await bot.send(
            _data(sender_username="Lara507", chat_username="sultanu1999"),
            msg_link="https://t.me/sultanu1999/531011",
            group_link="https://t.me/sultanu1999",
        )
        assert ok is True and method == "username" and reason == ""
        assert len(bot.calls) == 1
        p = bot.calls[0]["payload"]
        assert p["chat_id"] == TARGET
        assert p["parse_mode"] == "HTML"
        assert p["link_preview_options"] == {"is_disabled": True}
        kb = _buttons_of(p)
        assert kb == [
            {"text": "@Lara507", "url": "https://t.me/Lara507"},
            {"text": "جروب", "url": "https://t.me/sultanu1999"},
        ]
        assert bot.calls[0]["method"] == "sendMessage"

    @pytest.mark.asyncio
    async def test_429_retry_then_success(self, monkeypatch):
        sleeps: List[float] = []

        async def _fake_sleep(s):
            sleeps.append(s)

        monkeypatch.setattr(ab.asyncio, "sleep", _fake_sleep)
        bot = _FakeAlertBot([
            (429, {"ok": False, "description": "Too Many Requests",
                   "parameters": {"retry_after": 2}}),
            (200, {"ok": True}),
        ])
        ok, method, reason = await bot.send(_data(sender_username="u1"))
        assert ok is True and method == "username"
        assert len(bot.calls) == 2 and sleeps == [2.3]

    @pytest.mark.asyncio
    async def test_429_exhausted_returns_failure(self, monkeypatch):
        async def _fake_sleep(s):
            pass

        monkeypatch.setattr(ab.asyncio, "sleep", _fake_sleep)
        bot = _FakeAlertBot([(429, {"ok": False, "parameters": {"retry_after": 1}})] * 3)
        ok, method, reason = await bot.send(_data(sender_username="u1"))
        assert ok is False and reason == "rate_limited_after_3_attempts"
        assert len(bot.calls) == 3

    @pytest.mark.asyncio
    async def test_acceptance_5_button_user_invalid_resends_without_sender_button(self):
        """خصوصية المرسل تمنع زر tg://user → يُعاد الإرسال بدون الزر
        (رابط الاسم يبقى في النص وزر «جروب» يبقى) ولا تضيع الرسالة."""
        bot = _FakeAlertBot([
            (400, {"ok": False, "description": "Bad Request: BUTTON_USER_INVALID"}),
            (200, {"ok": True}),
        ])
        ok, method, reason = await bot.send(
            _data(chat_username="g1"),
            msg_link="https://t.me/g1/9",
            group_link="https://t.me/g1",
        )
        assert ok is True and method == "text_only"
        assert len(bot.calls) == 2
        first_kb = _buttons_of(bot.calls[0]["payload"])
        assert len(first_kb) == 2  # [زر المرسل بالاسم، جروب]
        second_kb = _buttons_of(bot.calls[1]["payload"])
        assert second_kb == [{"text": "جروب", "url": "https://t.me/g1"}]
        # رابط الاسم يبقى في النص حرفياً
        assert 'tg://user?id=6079171409' in bot.calls[1]["payload"]["text"]

    @pytest.mark.asyncio
    async def test_button_user_privacy_restricted_variant(self):
        bot = _FakeAlertBot([
            (400, {"ok": False, "description": "Bad Request: BUTTON_USER_PRIVACY_RESTRICTED"}),
            (200, {"ok": True}),
        ])
        ok, method, _ = await bot.send(_data())
        assert ok is True and method == "text_only"

    @pytest.mark.asyncio
    async def test_username_sender_never_hits_button_error_path(self):
        """زر username (t.me) لا يُرفض أبداً كزر مستخدم — يسلم مباشرة."""
        bot = _FakeAlertBot([
            (400, {"ok": False, "description": "Bad Request: BUTTON_USER_INVALID"}),
        ])
        ok, method, reason = await bot.send(_data(sender_username="u1", chat_id=-987654321))
        # لا زر tg://user ليُسقط — الخطأ ليس زر المرسل → فشل → fallback
        assert ok is False and "BUTTON_USER_INVALID" in reason

    @pytest.mark.asyncio
    async def test_other_failure_returns_reason_for_fallback(self):
        bot = _FakeAlertBot([
            (403, {"ok": False, "description": "Bad Request: bot was blocked"}),
        ])
        ok, method, reason = await bot.send(_data(sender_username="u1"))
        assert ok is False and method == "" and reason == "bot_api_403:Bad Request: bot was blocked"

    @pytest.mark.asyncio
    async def test_network_error_returns_reason(self):
        class _Broken(AlertBot):
            async def _post(self, method, payload):
                raise ConnectionError("no network")

        bot = _Broken(token="1:t", chat_id=TARGET)
        ok, method, reason = await bot.send(_data(sender_username="u1"))
        assert ok is False and reason == "network_error:ConnectionError"

    @pytest.mark.asyncio
    async def test_no_token_send_returns_not_ok(self):
        bot = AlertBot(token=None, chat_id=TARGET)
        assert bot.enabled is False
        ok, method, reason = await bot.send(_data(sender_username="u1"))
        assert ok is False and method == "" and reason == "no_alert_bot_token"

    @pytest.mark.asyncio
    async def test_no_chat_id_disabled(self):
        bot = AlertBot(token="1:t", chat_id=0)
        assert bot.enabled is False


# ═════════════════ AlertBot — فحص العضوية عند الإقلاع ═════════════════

class TestAlertBotMembershipCheck:
    @pytest.mark.asyncio
    async def test_member_status_ok(self, caplog):
        bot = _FakeAlertBot([
            (200, {"ok": True, "result": {"status": "administrator"}}),
        ])
        bot.bot_id = 123456
        bot.bot_username = "alzariqi711r_bot"
        st = await bot.check_membership()
        assert st == "administrator"
        assert bot.calls[0]["payload"] == {"chat_id": TARGET, "user_id": 123456}

    @pytest.mark.asyncio
    async def test_left_status_warns(self):
        bot = _FakeAlertBot([
            (200, {"ok": True, "result": {"status": "left"}}),
        ])
        bot.bot_id = 123456
        bot.bot_username = "alzariqi711r_bot"
        st = await bot.check_membership()
        assert st == "left"

    @pytest.mark.asyncio
    async def test_failed_check_does_not_raise(self):
        class _Broken(AlertBot):
            async def _post(self, method, payload):
                raise RuntimeError("boom")

        bot = _Broken(token="1:t", chat_id=TARGET)
        bot.bot_id = 1
        assert await bot.check_membership() is None

    @pytest.mark.asyncio
    async def test_get_me_failure_warns_and_skips(self):
        bot = _FakeAlertBot([
            (401, {"ok": False, "description": "Unauthorized"}),
        ])
        st = await bot.check_membership()
        assert st is None
        assert bot.calls[0]["method"] == "getMe"


# ═════════════════ التكامل: _send_alert → AlertBot → fallback ═════════════════

class _FakeClient:
    """عميل تيليجرام مزيّف لمسار الـfallback (نفس نمط اختبارات v10.7)."""

    def __init__(self, name: str = "Account 1"):
        self.name = name
        self.is_connected = True
        self.calls: List[Dict[str, Any]] = []
        self.forward_calls: List[Dict[str, Any]] = []
        self._chat_entities: Dict[int, Any] = {}

    def set_chat(self, chat_id: int, peer: Any) -> None:
        self._chat_entities[int(chat_id)] = peer

    async def get_input_entity(self, key: Any):
        if isinstance(key, int) and key in self._chat_entities:
            return self._chat_entities[key]
        raise ValueError(f"unresolvable: {key!r}")

    async def send_message(self, entity, message, **kwargs):
        self.calls.append({"kind": "message", "text": message, **kwargs})
        return SimpleNamespace(id=999, chat_id=entity, entities=[])

    async def send_file(self, entity, file=None, caption=None, **kwargs):
        self.calls.append({"kind": "file", "text": caption, **kwargs})
        return SimpleNamespace(id=998, chat_id=entity, entities=[])

    async def forward_messages(self, entity, message_id, *, from_peer=None, **kwargs):
        self.forward_calls.append({"to": entity, "msg_id": message_id, "from_peer": from_peer})
        return SimpleNamespace(id=1000, chat_id=entity)


class _RecordingBot(SimpleNamespace):
    """bot_ref يحمل AlertBot مزيّفاً + rate_limiter + مرشحي إرسال."""


def _monitor(db, name: str, client: Optional[_FakeClient]) -> mon.EnhancedAccountMonitor:
    account = {
        "id": 1, "prefix": "ACCOUNT_1", "name": name, "session": f"s_{name}",
        "api_id": 12345, "api_hash": "h" * 32, "phone": "+966500000000", "priority": 20,
    }
    m = mon.EnhancedAccountMonitor(account, db, None)
    m.client = client
    m.is_connected = True
    return m


def _wire(db, m, client, alert_bot=None):
    async def _can_proceed(account_name):
        return True

    m._bot_ref = _RecordingBot(
        alert_sender_client=None,
        main_client=client,
        monitors=[],
        rate_limiter=SimpleNamespace(can_proceed=_can_proceed),
        _note_alert_message=lambda *a, **k: None,
        alert_bot=alert_bot,
    )
    m._bot_ref.monitors.append(
        SimpleNamespace(account={"name": m.account["name"]}, client=client, is_connected=True)
    )

    async def _fake_chat_info(cl, chat_id, message_id, chat_access_hash=None, chat_username=None):
        return {"entity": None, "title": "مجموعة الطلاب", "group_link": "#", "msg_link": "#"}

    m._chat_info = _fake_chat_info


class _StubAlertBot:
    """بديل كامل عن AlertBot للتكامل — بلا شبكة."""

    def __init__(self, result: Tuple[bool, str, str], enabled: bool = True):
        self._result = result
        self._enabled = enabled
        self.calls: List[Dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(self, data, analysis=None, **nav) -> Tuple[bool, str, str]:
        self.calls.append({"data": data, "analysis": analysis, **nav})
        return self._result


class TestMonitorIntegration:
    @pytest.mark.asyncio
    async def test_bot_success_short_circuits_user_accounts(self, db):
        """نجاح البوت → لا إرسال من حسابات المستخدمين + contact_method مُسجّل."""
        m = _monitor(db, "Account 1", _FakeClient("Account 1"))
        stub = _StubAlertBot((True, "mention_button", ""))
        _wire(db, m, m.client, alert_bot=stub)
        await m._send_alert(
            _data(), keyword="طلب", text=_data()["text"],
            msg_hash="h_v108_ok", analysis={"valid": True, "decision": "accept"},
        )
        assert len(stub.calls) == 1
        # بيانات البوت تحمل روابط المحلّل (فارغة هنا من _fake_chat_info)
        assert "msg_link" in stub.calls[0]
        await db._flush()  # add_alert دفعي — الصف يظهر بعد flush
        row = await db._fetchone(
            "SELECT contact_method FROM alerts WHERE message_hash = 'h_v108_ok'"
        )
        assert row is not None and row["contact_method"] == "mention_button"
        # الحسابات لم تُرسل شيئاً (المراقب يبني البيانات فقط)
        assert m.client.calls == []

    @pytest.mark.asyncio
    async def test_bot_failure_falls_back_to_user_account(self, db):
        """فشل البوت → fallback لحساب المستخدم بنفس النص بدون أزرار."""
        m = _monitor(db, "Account 1", _FakeClient("Account 1"))
        stub = _StubAlertBot((False, "", "bot_api_400:test"))
        _wire(db, m, m.client, alert_bot=stub)
        await m._send_alert(
            _data(), keyword="طلب", text=_data()["text"],
            msg_hash="h_v108_fb", analysis={"valid": True, "decision": "accept"},
        )
        assert len(stub.calls) == 1
        assert len(m.client.calls) >= 1  # أُرسل من حساب المستخدم
        call = m.client.calls[0]
        assert "buttons" not in call or call.get("buttons") is None
        # النص بنمط v10.8 (بعد تحليل HTML لمسار mention — بلا وسوم ظاهرة)
        assert "المرسل : ID 6079171409" in call["text"]
        assert "نص الرسالة :" in call["text"]
        await db._flush()  # add_alert دفعي — الصف يظهر بعد flush
        row = await db._fetchone(
            "SELECT contact_method FROM alerts WHERE message_hash = 'h_v108_fb'"
        )
        assert row is not None

    @pytest.mark.asyncio
    async def test_no_alert_bot_ref_falls_back_cleanly(self, db):
        """غياب alert_bot من bot_ref تماماً → مسار الحسابات كما هو."""
        m = _monitor(db, "Account 1", _FakeClient("Account 1"))
        _wire(db, m, m.client, alert_bot=None)
        await m._send_alert(
            _data(), keyword="طلب", text=_data()["text"],
            msg_hash="h_v108_none", analysis={"valid": True, "decision": "accept"},
        )
        assert len(m.client.calls) >= 1

    @pytest.mark.asyncio
    async def test_bot_success_with_media_triggers_forward(self, db):
        """تنبيه بوسائط عبر البوت → forward supplement يحمل الوسائط."""
        m = _monitor(db, "Account 1", _FakeClient("Account 1"))
        stub = _StubAlertBot((True, "username", ""))
        _wire(db, m, m.client, alert_bot=stub)
        data = _data(sender_username="u_media", media_object=object())
        await m._send_alert(
            data, keyword="طلب", text=data["text"],
            msg_hash="h_v108_media", analysis={"valid": True, "decision": "accept"},
        )
        assert len(stub.calls) == 1
        assert len(m.client.forward_calls) == 1  # الوسائط وصلت عبر forward
        assert m.client.calls == []

    @pytest.mark.asyncio
    async def test_alertbot_exception_falls_back(self, db):
        """استثناء من AlertBot نفسه → الـfallback يعمل والتنبيه لا يضيع."""
        m = _monitor(db, "Account 1", _FakeClient("Account 1"))

        class _Boom:
            enabled = True

            async def send(self, *a, **k):
                raise RuntimeError("boom")

        _wire(db, m, m.client, alert_bot=_Boom())
        await m._send_alert(
            _data(), keyword="طلب", text=_data()["text"],
            msg_hash="h_v108_boom", analysis={"valid": True, "decision": "accept"},
        )
        assert len(m.client.calls) >= 1
