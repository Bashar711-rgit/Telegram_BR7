#!/usr/bin/env python3
"""
tests/test_alertbot_membership_state.py — v14.9.1 Buttons Heal

الخلفية الإنتاجية (حادثة 21-11-2026): فقد بوت التنبيهات عضويته في قناة
الهدف → كل إرسالات Bot API فشلت بـ403 (bot is not a member) → وصلت
التنبيهات عبر حسابات المستخدمين بلا أزرار (تيليجرام يسقط inline buttons
من حسابات المستخدمين — قيد منصة موثق silent_drop_no_reply_markup).

العقد المُختبَر هنا:
  1) check_membership يحدّث membership_ok/status/error حسب رد تيليجرام.
  2) send() الناجح يثبت العضوية؛ فشل 403 not-member يقلبها ويطلب إصلاحاً
     ذاتياً (_heal_requested) — بلا أي استثناء خروج.
  3) status_snapshot لقطة ثابتة المفاتيح بلا أسرار إطلاقاً.
  4) الإصلاح الذاتي (heal): بوت مشرف أصلاً لا يُرقّى (حماية صلاحياته من
     الاستبدال)، عضو عادي يُرقّى post_messages فقط، فشل كل المرشحين →
     تحذير إداري مباشر، ومفتاح ALERT_BOT_AUTO_ADD=false يعطّل المسار.
  5) حلقة المراقبة تُنهى نفسها فوراً عند الإيقاف (إيقاف نظيف).

كل الاختبارات محاكاة بالكامل (لا شبكة، لا تيليجرام إنتاجي).
"""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

import alert_bot as alert_bot_module
from alert_bot import AlertBot
import main as main_module
from main import EnhancedTelegramBot as BotApp

TARGET = -1001234567890
BOT_UNAME = "alzariqi711r_bot"


# ═══════════════════════════ أدوات المحاكاة ═══════════════════════════

class _FakeClient:
    """عميل Telethon مزيّف — يسجل طلبات TL ويعيد نتائج مضبوطة."""

    def __init__(self, invite_error: Optional[Exception] = None,
                 promote_error: Optional[Exception] = None):
        self.requests: List[Tuple[str, Any]] = []
        self.invite_error = invite_error
        self.promote_error = promote_error
        self.entities: Dict[Any, Any] = {}

    def is_connected(self) -> bool:
        return True

    async def get_input_entity(self, ref: Any) -> Any:
        if ref not in self.entities:
            self.entities[ref] = f"entity({ref})"
        return self.entities[ref]

    async def __call__(self, req: Any) -> Any:
        name = type(req).__name__
        self.requests.append((name, req))
        if name == "InviteToChannelRequest" and self.invite_error:
            raise self.invite_error
        if name == "EditAdminRequest" and self.promote_error:
            raise self.promote_error
        return SimpleNamespace(updates=[])


class _StubApp:
    """BotApp مصغّر — الحقول التي تقرؤها دوال heal حصراً."""

    def __init__(self, bot: AlertBot, monitors: Optional[List[Any]] = None):
        self.alert_bot = bot
        self.alert_sender_client = None
        self.main_client = None
        self.monitors = monitors or []
        self._alertbot_heal_last_ts = 0.0
        self._alertbot_heal_attempts = 0
        self._alertbot_warn_last_ts = 0.0
        self._shutdown_event = asyncio.Event()
        # اربط الدوال الحقيقية من BotApp على هذا الكائن (نفس التنفيذ الفعلي)
        for _m in (
            "_alertbot_admin_notify", "_alertbot_admin_warn",
            "_alertbot_heal_clients", "_alertbot_heal_cooldown_ok",
        ):
            setattr(self, _m, getattr(BotApp, _m).__get__(self))


def _member_status_bot(monkeypatch, statuses: List[str]) -> AlertBot:
    """AlertBot حقيقي بـgetChatMember يعيد الحالات بالتسلسل."""
    bot = AlertBot(token="123:fake", chat_id=TARGET)
    bot.bot_id = 8433129002
    bot.bot_username = BOT_UNAME
    seq = list(statuses)
    state = {"i": 0}

    async def fake_post(method: str, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        assert method == "getChatMember"
        i = min(state["i"], len(seq) - 1)
        st = seq[i]
        state["i"] += 1
        return 200, {"ok": True, "result": {"status": st}}

    monkeypatch.setattr(bot, "_post", fake_post)
    return bot


# ═══════════════ 1) حالة العضوية من check_membership ═══════════════

class TestMembershipState:
    @pytest.mark.asyncio
    async def test_administrator_sets_ok(self, monkeypatch):
        bot = _member_status_bot(monkeypatch, ["administrator"])
        st = await bot.check_membership()
        assert st == "administrator"
        assert bot.membership_ok is True
        assert bot.membership_status == "administrator"
        assert bot.membership_error == ""

    @pytest.mark.asyncio
    async def test_member_sets_ok(self, monkeypatch):
        bot = _member_status_bot(monkeypatch, ["member"])
        st = await bot.check_membership()
        assert st == "member"
        assert bot.membership_ok is True

    @pytest.mark.asyncio
    async def test_left_sets_not_ok(self, monkeypatch):
        bot = _member_status_bot(monkeypatch, ["left"])
        st = await bot.check_membership()
        assert st == "left"
        assert bot.membership_ok is False

    @pytest.mark.asyncio
    async def test_http_403_not_member_sets_false(self, monkeypatch):
        bot = AlertBot(token="123:fake", chat_id=TARGET)
        bot.bot_id = 8433129002
        bot.bot_username = BOT_UNAME

        async def fake_post(method, payload):
            return 403, {
                "ok": False,
                "description": "Forbidden: bot is not a member of the channel chat",
            }

        monkeypatch.setattr(bot, "_post", fake_post)
        st = await bot.check_membership()
        assert st is None
        assert bot.membership_ok is False
        assert "http_403" in bot.membership_error
        assert "not a member" in bot.membership_error

    @pytest.mark.asyncio
    async def test_transient_failure_keeps_last_verified_state(self, monkeypatch):
        """فشل عابر بعد تحقق ناجح لا يقلب آخر حالة مثبتة."""
        bot = AlertBot(token="123:fake", chat_id=TARGET)
        bot.bot_id = 8433129002
        bot.bot_username = BOT_UNAME
        calls = {"n": 0}

        async def fake_post(method, payload):
            calls["n"] += 1
            if calls["n"] == 1:
                return 200, {"ok": True, "result": {"status": "administrator"}}
            return 500, {"ok": False, "description": "Internal Server Error"}

        monkeypatch.setattr(bot, "_post", fake_post)
        await bot.check_membership()
        assert bot.membership_ok is True
        await bot.check_membership()
        assert bot.membership_ok is True  # آخر حالة مثبتة محفوظة
        assert "http_500" in bot.membership_error


# ═══════════════ 2) send() يقلب الحالة ═══════════════

def _fake_built() -> Dict[str, Any]:
    return {
        "text": "نص اختبار",
        "buttons": [],
        "button_kwargs": {
            "sender_id": 0,
            "sender_username": None,
            "sender_name": "مرسل",
            "msg_link": None,
            "group_link": None,
            "contact_url": None,
            "sender_type": "user",
        },
        "contact_method": "text_only",
        "msg_link": None,
        "display": "مرسل",
        "contact_target": None,
    }


class TestSendMembershipFlip:
    @pytest.mark.asyncio
    async def test_send_403_not_member_flips_and_requests_heal(self, monkeypatch):
        bot = AlertBot(token="123:fake", chat_id=TARGET)
        bot.bot_id = 8433129002
        bot.bot_username = BOT_UNAME
        # حالة سابقة خاطئة (فحص قديم قال عضو) — الإرسال 403 يصححها بصدق
        bot._note_membership(True, "administrator", "")

        def fake_build(*a, **kw):
            return _fake_built()

        async def fake_post(method, payload):
            assert method == "sendMessage"
            return 403, {
                "ok": False,
                "description": "Forbidden: bot is not a member of the channel chat",
            }

        monkeypatch.setattr(alert_bot_module, "build_alert_html", fake_build)
        monkeypatch.setattr(bot, "_post", fake_post)
        ok, method, reason = await bot.send({"text": "نص"}, None)
        assert ok is False
        assert "bot_api_403" in reason
        assert bot.membership_ok is False
        assert bot._heal_requested is True
        assert "not a member" in bot.membership_error

    @pytest.mark.asyncio
    async def test_send_success_proves_membership(self, monkeypatch):
        bot = AlertBot(token="123:fake", chat_id=TARGET)
        bot.bot_id = 8433129002
        bot.bot_username = BOT_UNAME
        bot._note_membership(False, "", "http_403:stale")

        def fake_build(*a, **kw):
            return _fake_built()

        async def fake_post(method, payload):
            return 200, {"ok": True, "result": {"message_id": 7, "entities": []}}

        monkeypatch.setattr(alert_bot_module, "build_alert_html", fake_build)
        monkeypatch.setattr(bot, "_post", fake_post)
        ok, method, reason = await bot.send({"text": "نص"}, None)
        assert ok is True
        assert method == "text_only"
        # الإرسال الناجح إلى قناة الهدف = عضوية مثبتة فعلياً
        assert bot.membership_ok is True
        assert bot.membership_status == "verified_by_send"
        assert bot._heal_requested is False


# ═══════════════ 3) لقطة الحالة ═══════════════

class TestStatusSnapshot:
    def test_stable_keys_no_secrets(self):
        bot = AlertBot(token="123:fake", chat_id=TARGET)
        bot.bot_username = BOT_UNAME
        snap = bot.status_snapshot()
        assert set(snap.keys()) == {
            "enabled", "bot_username", "membership_ok", "membership_status",
            "membership_error", "membership_last_check_age_s", "heal_requested",
        }
        assert snap["enabled"] is True
        assert snap["membership_ok"] is None  # لم يُفحص بعد
        # لا أسرار: التوكن لا يظهر بأي شكل
        blob = str(snap).lower()
        assert "fake" not in blob
        assert "token" not in blob

    def test_snapshot_after_state_update(self):
        bot = AlertBot(token="123:fake", chat_id=TARGET)
        bot.bot_username = BOT_UNAME
        bot._note_membership(False, "", "http_403:Forbidden")
        snap = bot.status_snapshot()
        assert snap["membership_ok"] is False
        assert snap["membership_error"].startswith("http_403")
        assert snap["membership_last_check_age_s"] is not None


# ═══════════════ 4) الإصلاح الذاتي (heal) ═══════════════

class TestHeal:
    @pytest.mark.asyncio
    async def test_already_admin_never_promoted(self, monkeypatch):
        """بوت مشرف أصلاً → نجاح بلا EditAdminRequest (حماية صلاحياته)."""
        bot = _member_status_bot(monkeypatch, ["administrator"])
        client = _FakeClient()
        app = _StubApp(bot)
        # تسريع: بلا نوم فعلي
        monkeypatch.setattr(asyncio, "sleep", _zero_sleep)
        ok = await BotApp._heal_alertbot_membership(app, trigger="test", clients=[client])
        assert ok is True
        names = [n for n, _ in client.requests]
        assert "InviteToChannelRequest" in names
        assert "EditAdminRequest" not in names
        assert bot.membership_ok is True

    @pytest.mark.asyncio
    async def test_member_promoted_with_post_messages_only(self, monkeypatch):
        """عضو عادي → ترقية post_messages فقط بلا أي صلاحية إضافية."""
        bot = _member_status_bot(monkeypatch, ["member", "administrator"])
        client = _FakeClient()
        app = _StubApp(bot)
        monkeypatch.setattr(asyncio, "sleep", _zero_sleep)
        ok = await BotApp._heal_alertbot_membership(app, trigger="test", clients=[client])
        assert ok is True
        edits = [req for name, req in client.requests if name == "EditAdminRequest"]
        assert len(edits) == 1
        rights = edits[0].admin_rights
        assert rights.post_messages is True
        assert rights.add_admins is False
        assert rights.edit_messages is False
        assert rights.delete_messages is False
        assert bot.membership_ok is True

    @pytest.mark.asyncio
    async def test_invite_already_participant_tolerated(self, monkeypatch):
        class _Already(Exception):
            pass

        bot = _member_status_bot(monkeypatch, ["administrator"])
        client = _FakeClient(invite_error=_Already("USER_ALREADY_PARTICIPANT"))
        app = _StubApp(bot)
        monkeypatch.setattr(asyncio, "sleep", _zero_sleep)
        ok = await BotApp._heal_alertbot_membership(app, trigger="test", clients=[client])
        assert ok is True

    @pytest.mark.asyncio
    async def test_all_candidates_fail_sends_admin_warn(self, monkeypatch):
        """فشل كل المرشحين → تحذير إداري بخطوات الإضافة اليدوية."""
        bot = AlertBot(token="123:fake", chat_id=TARGET)
        bot.bot_id = 8433129002
        bot.bot_username = BOT_UNAME
        client = _FakeClient(invite_error=Exception("CHAT_ADMIN_REQUIRED"))

        app = _StubApp(bot)
        sent: List[str] = []

        async def fake_send_plain(text, chat_id=None, silent=True):
            sent.append(str(text))
            return True, "ok"

        monkeypatch.setattr(bot, "send_plain", fake_send_plain)
        monkeypatch.setattr(asyncio, "sleep", _zero_sleep)
        ok = await BotApp._heal_alertbot_membership(app, trigger="test", clients=[client])
        assert ok is False
        assert len(sent) == 1
        assert f"@{BOT_UNAME}" in sent[0]
        assert "نشر الرسائل" in sent[0]
        assert app._alertbot_warn_last_ts > 0

    @pytest.mark.asyncio
    async def test_warn_cooldown_suppresses_repeat(self, monkeypatch):
        """ثانية متتالية بلا صلاحيات لا تكرر الرسالة (cooldown 6 ساعات)."""
        bot = AlertBot(token="123:fake", chat_id=TARGET)
        bot.bot_id = 8433129002
        bot.bot_username = BOT_UNAME
        client = _FakeClient(invite_error=Exception("CHAT_ADMIN_REQUIRED"))
        app = _StubApp(bot)
        sent: List[str] = []

        async def fake_send_plain(text, chat_id=None, silent=True):
            sent.append(str(text))
            return True, "ok"

        monkeypatch.setattr(bot, "send_plain", fake_send_plain)
        monkeypatch.setattr(asyncio, "sleep", _zero_sleep)
        await BotApp._heal_alertbot_membership(app, trigger="test", clients=[client])
        await BotApp._heal_alertbot_membership(app, trigger="test", clients=[client])
        assert len(sent) == 1  # الثانية مكتومة بالـcooldown

    @pytest.mark.asyncio
    async def test_auto_add_disabled_skips(self, monkeypatch):
        bot = AlertBot(token="123:fake", chat_id=TARGET)
        bot.bot_id = 8433129002
        bot.bot_username = BOT_UNAME
        client = _FakeClient()
        app = _StubApp(bot)
        fake_cfg = SimpleNamespace(
            TARGET_GROUP_ID=TARGET, ADMIN_CHAT_ID=654321,
            ALERT_BOT_AUTO_ADD=False,
        )
        monkeypatch.setattr(main_module, "CFG", fake_cfg)
        ok = await BotApp._heal_alertbot_membership(app, trigger="test", clients=[client])
        assert ok is False
        assert client.requests == []  # لم تُرسل أي عملية تيليجرام

    @pytest.mark.asyncio
    async def test_heal_sets_cooldown(self, monkeypatch):
        bot = _member_status_bot(monkeypatch, ["administrator"])
        client = _FakeClient()
        app = _StubApp(bot)
        monkeypatch.setattr(asyncio, "sleep", _zero_sleep)
        assert BotApp._alertbot_heal_cooldown_ok(app) is True
        await BotApp._heal_alertbot_membership(app, trigger="test", clients=[client])
        assert app._alertbot_heal_attempts == 1
        assert BotApp._alertbot_heal_cooldown_ok(app) is False  # خلال الـ30 دقيقة

    @pytest.mark.asyncio
    async def test_no_candidates_warns_without_crash(self, monkeypatch):
        bot = AlertBot(token="123:fake", chat_id=TARGET)
        bot.bot_id = 8433129002
        bot.bot_username = BOT_UNAME
        app = _StubApp(bot)
        sent: List[str] = []

        async def fake_send_plain(text, chat_id=None, silent=True):
            sent.append(str(text))
            return True, "ok"

        monkeypatch.setattr(bot, "send_plain", fake_send_plain)
        ok = await BotApp._heal_alertbot_membership(app, trigger="test", clients=[])
        assert ok is False
        assert len(sent) == 1
        assert "اليدوي" in sent[0]


# ═══════════════ 5) حلقة المراقبة ═══════════════

class TestWatchdog:
    @pytest.mark.asyncio
    async def test_exits_immediately_on_shutdown(self):
        """الإيقاف المُعلن قبل البدء = إنهاء نظيف فوري بلا حلقة."""
        bot = AlertBot(token="", chat_id=0)  # معطّل
        app = _StubApp(bot)
        app._shutdown_event.set()
        await asyncio.wait_for(
            BotApp._alertbot_membership_watchdog(app), timeout=2.0
        )

    @pytest.mark.asyncio
    async def test_disabled_bot_loop_noop_exits_on_shutdown(self, monkeypatch):
        """بوت معطّل داخل حلقة تعمل → دورة سلبية ثم خروج نظيف عند الإيقاف."""
        bot = AlertBot(token="", chat_id=0)
        app = _StubApp(bot)
        # دورة سريعة (ثابت وحدة) بدل انتظار 60 ثانية حقيقية
        monkeypatch.setattr(main_module, "_ALERTBOT_WATCHDOG_CYCLE_S", 0.02)
        task = asyncio.create_task(BotApp._alertbot_membership_watchdog(app))
        await asyncio.sleep(0.05)  # دورة سلبية واحدة على الأقل (بوت معطّل)
        app._shutdown_event.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert task.done() and not task.exception()


# ═══════════════ أدوات مساعدة ═══════════════

async def _zero_sleep(_s=None, **kw):
    return None
