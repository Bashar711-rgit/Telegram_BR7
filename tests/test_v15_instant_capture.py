#!/usr/bin/env python3
"""
test_v15_instant_capture.py — اختبارات v15.0: الالتقاط الفوري + الأزرار لكل تنبيه
==================================================================================

يغطي:
  1) event_sender_gate — رسائل البوتات تُلتقط (طلب المالك)، والهويات
     الذاتية تُتخطى (حاجز الصدى)، والتبديل CAPTURE_FROM_BOTS=false
     يستعيد السلوك القديم بلا نشر.
  2) _pre_send_membership_check — فحص العضوية الاستباقي قبل الإرسال
     (TTL-driven): يفحص فقط عند الشك وانتهاء TTL، ويطلق الإصلاح الفوري
     (callback + _heal_requested) عند تأكيد فقدان العضوية.
  3) _notify_membership_lost — علم الطلب + استدعاء callback المتزامن
     وغير المتزامن بلا استثناءات.
  4) _capture_recent_input_sender — الالتقاط الخلفي يملأ
     _recent_input_senders (بديل await المسار الحرج القديم).
  5) مفاتيح الإعداد الجديدة (config) + مفاتيح المقاييس الجديدة
     (contact_target).

بلا أي I/O شبكي — كل التبعيات الخارجية mock.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Dict, Tuple

import pytest

from config import CFG

# ═══════════════ 1) بوابة هوية المرسل — الالتقاط من البوتات ═══════════════

from monitors import (  # noqa: E402
    _recent_input_senders,
    event_sender_gate,
    is_self_sender,
    register_self_sender,
)


class TestEventSenderGate:
    def test_external_bot_is_captured(self):
        """طلب المالك v15.0: رسائل البوتات الأخرى تُلتقط — لا تجاهل."""
        bot_sender = SimpleNamespace(bot=True, first_name="HelperBot")
        assert event_sender_gate(555000111, bot_sender) is False

    def test_external_bot_no_sender_object_is_captured(self):
        """بوت بكيان غير محمل (sender=None) — يُلتقط أيضاً."""
        assert event_sender_gate(555000222, None) is False

    def test_self_sender_is_skipped(self):
        """حاجز الصدى الذاتي: بوت التنبيهات وحسابات المراقبة لا تُراقب."""
        my_id = 909777001
        assert register_self_sender(my_id) is True
        assert is_self_sender(my_id) is True
        assert event_sender_gate(my_id, None) is True

    def test_normal_user_is_captured(self):
        assert event_sender_gate(444000333, SimpleNamespace(bot=False)) is False

    def test_zero_or_invalid_ids_are_captured(self):
        """فشل-آمن: هوية غير قابلة للتحليل لا تُتخطى أبداً."""
        assert event_sender_gate(0, None) is False
        assert event_sender_gate(None, None) is False
        assert event_sender_gate("not-a-number", None) is False

    def test_toggle_restores_legacy_bot_skip(self, monkeypatch):
        """CAPTURE_FROM_BOTS=false يستعيد السلوك القديم (تجاهل البوتات)."""
        old = CFG.CAPTURE_FROM_BOTS
        object.__setattr__(CFG, "CAPTURE_FROM_BOTS", False)
        try:
            bot_sender = SimpleNamespace(bot=True)
            assert event_sender_gate(555000444, bot_sender) is True
            # غير البوتات تبقى مُلتقطة
            assert event_sender_gate(555000445, SimpleNamespace(bot=False)) is False
        finally:
            object.__setattr__(CFG, "CAPTURE_FROM_BOTS", old)

    def test_toggle_true_keeps_bots(self):
        old = CFG.CAPTURE_FROM_BOTS
        object.__setattr__(CFG, "CAPTURE_FROM_BOTS", True)
        try:
            assert event_sender_gate(555000555, SimpleNamespace(bot=True)) is False
        finally:
            object.__setattr__(CFG, "CAPTURE_FROM_BOTS", old)


# ═══════════════ 2) الفحص الاستباقي للعضوية قبل الإرسال ═══════════════

from alert_bot import AlertBot  # noqa: E402

_TARGET = -1001234567890


def _bot() -> AlertBot:
    b = AlertBot(token="123:fake", chat_id=_TARGET)
    b.bot_id = 8433129002
    b.bot_username = "br7_alert_bot"
    return b


class TestPreSendMembershipCheck:
    @pytest.mark.asyncio
    async def test_known_member_skips_check(self, monkeypatch):
        """membership_ok=True → صفر طلبات شبكية (المسار الساخن بلا تكلفة)."""
        bot = _bot()
        bot._note_membership(True, "administrator", "")
        calls = {"n": 0}

        async def fake_post(method, payload):
            calls["n"] += 1
            return 200, {"ok": True, "result": {"status": "administrator"}}

        monkeypatch.setattr(bot, "_post", fake_post)
        await bot._pre_send_membership_check()
        assert calls["n"] == 0

    @pytest.mark.asyncio
    async def test_fresh_ttl_skips_check(self, monkeypatch):
        """فحص حديث (داخل TTL) → لا فحص جديد حتى لو الحالة غير مؤكدة."""
        bot = _bot()
        bot.membership_ok = None
        bot._membership_last_check_ts = time.time()  # فحص قبل ثوانٍ
        calls = {"n": 0}

        async def fake_post(method, payload):
            calls["n"] += 1
            return 200, {"ok": True, "result": {"status": "left"}}

        monkeypatch.setattr(bot, "_post", fake_post)
        await bot._pre_send_membership_check()
        assert calls["n"] == 0

    @pytest.mark.asyncio
    async def test_unknown_none_state_is_boot_check_responsibility(self, monkeypatch):
        """الحالة None (غير مُفحوصة) → لا فحص في المسار الساخن.
        فحص الإقلاع يملكها، و403 أثناء الإرسال يطلق الإصلاح الفوري فوراً."""
        bot = _bot()
        bot.membership_ok = None
        bot._membership_last_check_ts = 0.0
        calls = {"n": 0}

        async def fake_post(method, payload):
            calls["n"] += 1
            return 200, {"ok": True, "result": {"status": "left"}}

        monkeypatch.setattr(bot, "_post", fake_post)
        await bot._pre_send_membership_check()
        assert calls["n"] == 0
        assert bot._heal_requested is False

    @pytest.mark.asyncio
    async def test_lost_membership_invokes_callback(self, monkeypatch):
        """اكتشاف الفقدان → callback الفوري يُستدعى (الإصلاح الحدوي)."""
        bot = _bot()
        bot.membership_ok = False  # فقدان معروف، TTL منتهٍ
        bot._membership_last_check_ts = 0.0

        async def fake_post(method, payload):
            return 200, {"ok": True, "result": {"status": "left"}}

        monkeypatch.setattr(bot, "_post", fake_post)
        called = {"n": 0}

        def cb() -> None:
            called["n"] += 1

        bot.on_membership_lost = cb
        await bot._pre_send_membership_check()
        assert called["n"] == 1
        assert bot._heal_requested is True

    @pytest.mark.asyncio
    async def test_async_callback_is_spawned_safely(self, monkeypatch):
        """callback غير متزامن → يُطلق خلفياً بلا حجب ولا استثناء."""
        bot = _bot()
        bot.membership_ok = False
        bot._membership_last_check_ts = 0.0

        async def fake_post(method, payload):
            return 200, {"ok": True, "result": {"status": "left"}}

        monkeypatch.setattr(bot, "_post", fake_post)
        flag = asyncio.Event()

        async def acb() -> None:
            flag.set()

        bot.on_membership_lost = acb
        await bot._pre_send_membership_check()
        await asyncio.sleep(0.05)  # فرصة لتنفيذ المهمة الخلفية
        assert flag.is_set()

    @pytest.mark.asyncio
    async def test_broken_callback_never_breaks_precheck(self, monkeypatch):
        """فشل الـcallback لا يمنع الفحص ولا يرفع استثناءً."""
        bot = _bot()
        bot.membership_ok = False
        bot._membership_last_check_ts = 0.0

        async def fake_post(method, payload):
            return 200, {"ok": True, "result": {"status": "left"}}

        monkeypatch.setattr(bot, "_post", fake_post)

        def broken_cb() -> None:
            raise RuntimeError("callback boom")

        bot.on_membership_lost = broken_cb
        # لا استثناء
        await bot._pre_send_membership_check()
        assert bot._heal_requested is True

    @pytest.mark.asyncio
    async def test_precheck_never_raises_on_network_error(self, monkeypatch):
        """فشل شبكي في الفحص → صمت تام (فشل-آمن)، الإرسال مستمر."""
        bot = _bot()
        bot.membership_ok = False
        bot._membership_last_check_ts = 0.0

        async def fake_post(method, payload):
            raise OSError("network down")

        monkeypatch.setattr(bot, "_post", fake_post)
        await bot._pre_send_membership_check()  # لا استثناء
        # الحالة المعروفة بفقدها تبقى كما هي (خطأ عابر لا يقلب الحالة)
        assert bot.membership_ok is False


# ═══════════════ 3) الإشعار الفوري بفقدان العضوية ═══════════════

class TestNotifyMembershipLost:
    def test_sets_flag_and_calls_sync_callback(self):
        bot = _bot()
        bot._heal_requested = False
        called = {"n": 0}

        def cb() -> None:
            called["n"] += 1

        bot.on_membership_lost = cb
        bot._notify_membership_lost()
        assert bot._heal_requested is True
        assert called["n"] == 1

    def test_no_callback_still_sets_flag(self):
        bot = _bot()
        bot._heal_requested = False
        bot.on_membership_lost = None
        bot._notify_membership_lost()
        assert bot._heal_requested is True

    def test_raising_callback_swallowed(self):
        bot = _bot()

        def bad_cb() -> None:
            raise ValueError("x")

        bot.on_membership_lost = bad_cb
        bot._notify_membership_lost()  # لا استثناء
        assert bot._heal_requested is True


# ═══════════════ 4) الالتقاط الخلفي لـinput_sender ═══════════════

class _FakeInputPeer:
    access_hash = 12345678901234567


class _FakeMessage:
    id = 4242


class _FakeEvent:
    chat_id = -1009876543210
    message = _FakeMessage()

    async def get_input_sender(self) -> _FakeInputPeer:
        return _FakeInputPeer()


class TestBackgroundInputSenderCapture:
    @pytest.mark.asyncio
    async def test_fills_recent_input_senders(self):
        from monitors import EnhancedAccountMonitor

        mon = EnhancedAccountMonitor(
            {"name": "test_v15_acc"}, None, None, None
        )
        # نظّف أي إدخال سابق لنفس المفتاح
        key = (_FakeEvent.chat_id, _FakeEvent.message.id)
        _recent_input_senders.pop(key, None)
        old_t = CFG.CAPTURE_INPUT_TIMEOUT_S
        object.__setattr__(CFG, "CAPTURE_INPUT_TIMEOUT_S", 0.2)
        try:
            await mon._capture_recent_input_sender(_FakeEvent())
        finally:
            object.__setattr__(CFG, "CAPTURE_INPUT_TIMEOUT_S", old_t)
        entry = _recent_input_senders.get(key)
        assert entry is not None
        assert entry["account"] == "test_v15_acc"
        assert isinstance(entry["input_sender"], _FakeInputPeer)
        assert time.time() - entry["ts"] < 5.0
        _recent_input_senders.pop(key, None)

    @pytest.mark.asyncio
    async def test_missing_entity_is_silently_ignored(self):
        from monitors import EnhancedAccountMonitor

        class _NoneEvent:
            chat_id = -100999
            message = SimpleNamespace(id=77)

            async def get_input_sender(self):
                return None

        mon = EnhancedAccountMonitor(
            {"name": "test_v15_acc2"}, None, None, None
        )
        old_t = CFG.CAPTURE_INPUT_TIMEOUT_S
        object.__setattr__(CFG, "CAPTURE_INPUT_TIMEOUT_S", 0.2)
        try:
            await mon._capture_recent_input_sender(_NoneEvent())
        finally:
            object.__setattr__(CFG, "CAPTURE_INPUT_TIMEOUT_S", old_t)
        assert (_NoneEvent.chat_id, 77) not in _recent_input_senders


# ═══════════════ 5) مفاتيح الإعداد والمقاييس الجديدة ═══════════════

class TestV15ConfigAndMetrics:
    def test_config_keys_present_with_safe_defaults(self):
        assert bool(getattr(CFG, "CAPTURE_FROM_BOTS", True)) is True
        assert 0.05 <= float(getattr(CFG, "CAPTURE_SENDER_TIMEOUT_S", 0)) <= 5.0
        assert 0.05 <= float(getattr(CFG, "CAPTURE_INPUT_TIMEOUT_S", 0)) <= 5.0
        assert int(getattr(CFG, "ALERT_BOT_MEMBERSHIP_TTL_S", 0)) >= 30

    def test_new_metric_keys_in_snapshot(self):
        from contact_target import get_contact_metrics_snapshot

        snap = get_contact_metrics_snapshot()
        for key in (
            "membership_precheck_run",
            "membership_precheck_heal",
            "t5_buttons_delivered",
        ):
            assert key in snap, f"مفتاح المقاييس مفقود: {key}"
            assert isinstance(snap[key], int) and snap[key] >= 0

    def test_metric_inc_accepts_new_keys(self):
        from contact_target import get_contact_metrics_snapshot, metric_inc

        before = get_contact_metrics_snapshot()
        metric_inc("t5_buttons_delivered", 1)
        metric_inc("membership_precheck_run", 2)
        after = get_contact_metrics_snapshot()
        assert after["t5_buttons_delivered"] == before["t5_buttons_delivered"] + 1
        assert (
            after["membership_precheck_run"]
            == before["membership_precheck_run"] + 2
        )
