#!/usr/bin/env python3
"""
tests/test_v14_6_activity.py — Activity Maintenance System (v14.6.0)

يغطي متطلبات المواصفة:
  §ثانياً  — رسالة النشاط الدورية (90s) + حارس الخدمة (30s) + استثناء الرسالة
             من الفلاتر/الإحصائيات + التحذيرات الإدارية
  §سادساً  — مفاتيح /health/full الجديدة (activity/monitor_detail/restarts)
  §رابعاً  — عقد استثناء رسالة النشاط من مسار الطلبات (echo guard)

كل الاختبارات محاكاة بالكامل (لا شبكة، لا تيليجرام إنتاجي، لا ضغط DB).
"""

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

from config import CFG
from alert_bot import AlertBot
from monitors import (
    AlertLatencyTracker,
    EnhancedAccountMonitor,
    ReconnectionManager,
    is_activity_message,
)
import monitors as monitors_mod
from unified_alerts import activity as activity_mod
from unified_alerts.activity import ActivityWatchdog


# ═══════════════════════════ أدوات المحاكاة ═══════════════════════════

TARGET = -1001234567890


class _FakeAlertBot:
    """بوت تنبيهات مزيّف — يسجل الاستدعاءات ويعيد نتيجة مضبوطة."""

    def __init__(self, enabled: bool = True, fail: bool = False):
        self.enabled = enabled
        self.fail = fail
        self.calls: List[Dict[str, Any]] = []

    async def send_plain(self, text: str, chat_id=None, silent: bool = True):
        self.calls.append(
            {"text": text, "chat_id": chat_id, "silent": silent, "ts": time.time()}
        )
        if self.fail:
            return False, "http_500:mock_failure"
        return True, "ok"


class _FakeReconnectManager:
    """مدير إعادة اتصال مزيّف — يتتبع start()."""

    def __init__(self, running: bool = False):
        self._running = running
        self.start_calls = 0

    async def start(self):
        self.start_calls += 1
        self._running = True


class _FakeMonitor:
    def __init__(self, connected: bool = True, rm: Optional[_FakeReconnectManager] = None):
        self.is_connected = connected
        self.account = {"name": "ACC_TEST"}
        self._reconnect = rm

    def _register_handler(self):  # pragma: no cover
        pass


class _FakeDB:
    def __init__(self, queue_size: int = 0):
        self._queue_size = queue_size

    async def queue_size(self) -> int:
        return self._queue_size


class _FakeBot:
    def __init__(self, alert_bot=None, monitors=None, db=None):
        self.alert_bot = alert_bot
        self.monitors = monitors or []
        self.db = db
        self.is_running = True


def _activity_cfg(**overrides) -> SimpleNamespace:
    """CFG مزيّف للوحدة النشطة — نفس مفاتيح القراءة الافتراضية."""
    base = dict(
        ACTIVITY_MESSAGE_ENABLED=True,
        ACTIVITY_MESSAGE_INTERVAL_SECONDS=90,
        ACTIVITY_MESSAGE_TEXT="انظر : بوت Engazz - S-bot - طلبات",
        ACTIVITY_CHANNEL_ID=0,
        ACTIVITY_MESSAGE_FALLBACK_MAIN=True,
        ACTIVITY_MESSAGE_SILENT=True,
        TARGET_GROUP_ID=TARGET,
        ADMIN_CHAT_ID=654321,
        MEMORY_THRESHOLD_MB=400,
        ACTIVITY_ADMIN_ALERTS_ENABLED=True,
        ACTIVITY_IDLE_ALERT_MINUTES=15,
        ACTIVITY_ADMIN_ALERT_COOLDOWN_SECONDS=600,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ═════════════════ 1) علامة رسالة النشاط ═════════════════

class TestActivityMessageMarker:
    def test_exact_text_matches(self):
        assert is_activity_message(CFG.ACTIVITY_MESSAGE_TEXT) is True

    def test_whitespace_normalization(self):
        assert is_activity_message("  " + CFG.ACTIVITY_MESSAGE_TEXT + "  ") is True

    def test_zero_width_marks_ignored(self):
        # LRM/RLM/داخل النص — المرسلون قد يضيفون علامات اتجاه
        assert is_activity_message("\u200f" + CFG.ACTIVITY_MESSAGE_TEXT + "\u200e") is True

    def test_inner_multi_space_normalized(self):
        assert is_activity_message("انظر  :  بوت  Engazz") is False  # نص مختلف فعلاً
        assert is_activity_message("\u200fانظر : بوت Engazz - S-bot - طلبات\u200e") is True

    def test_none_and_empty(self):
        assert is_activity_message(None) is False
        assert is_activity_message("") is False

    def test_other_text_is_not_activity(self):
        assert is_activity_message("عندي واجب ابي احد يحله") is False
        assert is_activity_message("انظر : بوت Engazz - S-bot - طلبات!") is False  # إضافة حرف

    def test_custom_text_via_cfg(self, monkeypatch):
        fake_cfg = SimpleNamespace(ACTIVITY_MESSAGE_TEXT="نبض الخدمة")
        monkeypatch.setattr(monitors_mod, "CFG", fake_cfg)
        assert is_activity_message("نبض الخدمة") is True
        assert is_activity_message(CFG.ACTIVITY_MESSAGE_TEXT) is False

    def test_empty_cfg_text_disables_marker(self, monkeypatch):
        fake_cfg = SimpleNamespace(ACTIVITY_MESSAGE_TEXT="")
        monkeypatch.setattr(monitors_mod, "CFG", fake_cfg)
        assert is_activity_message("انظر : بوت Engazz - S-bot - طلبات") is False


# ═════════════════ 2) AlertBot.send_plain ═════════════════

class TestSendPlain:
    @pytest.mark.asyncio
    async def test_disabled_bot_returns_false(self):
        bot = AlertBot(token="", chat_id=TARGET)
        ok, reason = await bot.send_plain("نص")
        assert ok is False
        assert reason == "no_alert_bot_token"

    @pytest.mark.asyncio
    async def test_success_payload_shape(self, monkeypatch):
        bot = AlertBot(token="123456:TEST", chat_id=TARGET)
        calls: List[Dict[str, Any]] = []

        async def fake_post(method, payload):
            calls.append({"method": method, "payload": payload})
            return 200, {"ok": True, "result": {"message_id": 1}}

        monkeypatch.setattr(bot, "_post", fake_post)
        ok, reason = await bot.send_plain("انظر : بوت Engazz - S-bot - طلبات")
        assert ok is True and reason == "ok"
        p = calls[0]["payload"]
        assert calls[0]["method"] == "sendMessage"
        assert p["chat_id"] == TARGET
        assert p["disable_notification"] is True  # صامت افتراضياً
        assert p["link_preview_options"] == {"is_disabled": True}
        assert "parse_mode" not in p  # بلا قوالب — نص خام

    @pytest.mark.asyncio
    async def test_http_failure_honest_reason(self, monkeypatch):
        bot = AlertBot(token="123456:TEST", chat_id=TARGET)

        async def fake_post(method, payload):
            return 400, {"ok": False, "description": "chat not found"}

        monkeypatch.setattr(bot, "_post", fake_post)
        ok, reason = await bot.send_plain("نص")
        assert ok is False
        assert "http_400" in reason and "chat not found" in reason

    @pytest.mark.asyncio
    async def test_no_target_chat(self):
        """بوت بلا قناة هدف = غير مفعّل (عقد enabled: توكن + قناة معاً)."""
        bot = AlertBot(token="123456:TEST", chat_id=0)
        ok, reason = await bot.send_plain("نص")
        assert ok is False and reason == "no_alert_bot_token"


# ═════════════════ 3) بوابة رسالة النشاط ═════════════════

class TestActivityMessageGate:
    def _make(self, monkeypatch, alert_bot, **cfg_over):
        monkeypatch.setattr(activity_mod, "CFG", _activity_cfg(**cfg_over))
        bot = _FakeBot(alert_bot=alert_bot)
        wd = ActivityWatchdog(bot)
        return wd

    @pytest.mark.asyncio
    async def test_recent_real_activity_suppresses(self, monkeypatch):
        wd = self._make(monkeypatch, _FakeAlertBot())
        wd.note_real_activity("ACC", TARGET, 111)  # نشاط الآن
        await wd._maybe_send_activity_message()
        assert wd.activity_messages_sent == 0

    @pytest.mark.asyncio
    async def test_idle_90s_sends(self, monkeypatch):
        ab = _FakeAlertBot()
        wd = self._make(monkeypatch, ab)
        wd._last_real_activity_ts = time.time() - 91
        await wd._maybe_send_activity_message()
        assert wd.activity_messages_sent == 1
        assert len(ab.calls) == 1
        call = ab.calls[0]
        assert call["chat_id"] == TARGET  # fallback إلى قناة الطلبات
        assert call["text"] == "انظر : بوت Engazz - S-bot - طلبات"
        assert call["silent"] is True
        # لا تُرسل مرتين خلال الفترة (سقف إلزامي)
        await wd._maybe_send_activity_message()
        assert len(ab.calls) == 1

    @pytest.mark.asyncio
    async def test_disabled_config_sends_nothing(self, monkeypatch):
        ab = _FakeAlertBot()
        wd = self._make(monkeypatch, ab, ACTIVITY_MESSAGE_ENABLED=False)
        wd._last_real_activity_ts = time.time() - 3600
        await wd._maybe_send_activity_message()
        assert wd.activity_messages_sent == 0 and len(ab.calls) == 0

    @pytest.mark.asyncio
    async def test_no_fallback_no_dedicated_channel(self, monkeypatch):
        ab = _FakeAlertBot()
        wd = self._make(monkeypatch, ab, ACTIVITY_MESSAGE_FALLBACK_MAIN=False)
        wd._last_real_activity_ts = time.time() - 3600
        await wd._maybe_send_activity_message()
        assert wd.activity_messages_sent == 0 and len(ab.calls) == 0

    @pytest.mark.asyncio
    async def test_dedicated_channel_preferred(self, monkeypatch):
        ab = _FakeAlertBot()
        wd = self._make(monkeypatch, ab, ACTIVITY_CHANNEL_ID=-1009999)
        wd._last_real_activity_ts = time.time() - 3600
        await wd._maybe_send_activity_message()
        assert wd.activity_messages_sent == 1
        assert ab.calls[0]["chat_id"] == -1009999

    @pytest.mark.asyncio
    async def test_send_failure_backoff(self, monkeypatch):
        ab = _FakeAlertBot(fail=True)
        wd = self._make(monkeypatch, ab)
        wd._last_real_activity_ts = time.time() - 3600
        await wd._maybe_send_activity_message()
        assert wd.activity_send_failures == 1 and wd.activity_messages_sent == 0
        # backoff: المحاولة التالية (بلا انتظار) لا تضرب Bot API
        await wd._maybe_send_activity_message()
        assert len(ab.calls) == 1

    @pytest.mark.asyncio
    async def test_disabled_alert_bot(self, monkeypatch):
        ab = _FakeAlertBot(enabled=False)
        wd = self._make(monkeypatch, ab)
        wd._last_real_activity_ts = time.time() - 3600
        await wd._maybe_send_activity_message()
        assert wd.activity_messages_sent == 0 and len(ab.calls) == 0


# ═════════════════ 4) التحذيرات الإدارية ═════════════════

class TestAdminAlerts:
    def _make(self, monkeypatch, alert_bot, **cfg_over):
        monkeypatch.setattr(activity_mod, "CFG", _activity_cfg(**cfg_over))
        return ActivityWatchdog(_FakeBot(alert_bot=alert_bot))

    @pytest.mark.asyncio
    async def test_first_send_then_cooldown(self, monkeypatch):
        ab = _FakeAlertBot()
        wd = self._make(monkeypatch, ab)
        await wd._admin_alert("all_monitors_down", "تنبيه أول")
        assert wd._admin_alerts_sent == 1 and len(ab.calls) == 1
        assert ab.calls[0]["chat_id"] == 654321
        assert ab.calls[0]["silent"] is False  # التحذيرات بصوت
        await wd._admin_alert("all_monitors_down", "تنبيه ثانٍ")
        assert wd._admin_alerts_sent == 1 and len(ab.calls) == 1
        assert wd._admin_alerts_cooldown_skipped == 1

    @pytest.mark.asyncio
    async def test_different_kinds_independent_cooldowns(self, monkeypatch):
        ab = _FakeAlertBot()
        wd = self._make(monkeypatch, ab)
        await wd._admin_alert("memory_high", "ذاكرة")
        await wd._admin_alert("queue_stalled", "طابور")
        assert wd._admin_alerts_sent == 2

    @pytest.mark.asyncio
    async def test_admin_alerts_disabled(self, monkeypatch):
        ab = _FakeAlertBot()
        wd = self._make(monkeypatch, ab, ACTIVITY_ADMIN_ALERTS_ENABLED=False)
        await wd._admin_alert("memory_high", "ذاكرة")
        assert len(ab.calls) == 0

    @pytest.mark.asyncio
    async def test_all_monitors_down_alerts(self, monkeypatch):
        ab = _FakeAlertBot()
        wd = self._make(monkeypatch, ab)
        wd._bot.monitors = [_FakeMonitor(connected=False), _FakeMonitor(connected=False)]
        await wd._watchdog_cycle()
        kinds = [c["text"] for c in ab.calls]
        assert any("جميع الحسابات" in t for t in kinds)

    @pytest.mark.asyncio
    async def test_idle_alert_with_connected_monitor(self, monkeypatch):
        ab = _FakeAlertBot()
        wd = self._make(monkeypatch, ab, ACTIVITY_IDLE_ALERT_MINUTES=15)
        wd._bot.monitors = [_FakeMonitor(connected=True)]
        wd._last_real_activity_ts = time.time() - 16 * 60
        await wd._watchdog_cycle()
        assert any("لا أحداث واردة" in c["text"] for c in ab.calls)


# ═════════════════ 5) دورة الحارس ═════════════════

class TestWatchdogCycle:
    @pytest.mark.asyncio
    async def test_dead_monitor_gets_reconnect_armed(self, monkeypatch):
        monkeypatch.setattr(activity_mod, "CFG", _activity_cfg())
        rm = _FakeReconnectManager(running=False)
        dead = _FakeMonitor(connected=False, rm=rm)
        wd = ActivityWatchdog(_FakeBot(monitors=[dead]))
        await wd._watchdog_cycle()
        assert rm.start_calls == 1
        assert rm._running is True
        assert wd._reconnect_arms == 1
        # الحي لا يُمسّ
        rm2 = _FakeReconnectManager(running=True)
        alive = _FakeMonitor(connected=True, rm=rm2)
        wd2 = ActivityWatchdog(_FakeBot(monitors=[alive]))
        await wd2._watchdog_cycle()
        assert rm2.start_calls == 0

    @pytest.mark.asyncio
    async def test_queue_stall_detection_after_3_cycles(self, monkeypatch):
        """الدورة المرجعية + 3 دورات بلا تغيير = توقف (عقد 3-observations)."""
        monkeypatch.setattr(activity_mod, "CFG", _activity_cfg())
        wd = ActivityWatchdog(_FakeBot(db=_FakeDB(queue_size=7)))
        for _ in range(4):
            await wd._watchdog_cycle()
        assert wd.queue_stall_detected is True
        assert wd._queue_stall_cycles == 3
        # إيقاف الازدحام: الطابور أصبح فارغاً → يتصفر العدّاد
        wd._bot.db._queue_size = 0
        await wd._watchdog_cycle()
        assert wd.queue_stall_detected is False
        assert wd._queue_stall_cycles == 0

    @pytest.mark.asyncio
    async def test_progressing_queue_not_stalled(self, monkeypatch):
        monkeypatch.setattr(activity_mod, "CFG", _activity_cfg())
        from unified_alerts.stats import get_unified_stats

        wd = ActivityWatchdog(_FakeBot(db=_FakeDB(queue_size=3)))
        stats = get_unified_stats()
        for _ in range(5):
            stats.inc("filter_ok", 1)  # تقدم فعلي بين الدورات
            await wd._watchdog_cycle()
        assert wd.queue_stall_detected is False

    @pytest.mark.asyncio
    async def test_loop_lag_window_and_snapshot_keys(self, monkeypatch):
        monkeypatch.setattr(activity_mod, "CFG", _activity_cfg())
        wd = ActivityWatchdog(_FakeBot())
        wd._lag_window.extend([5.0, 12.5, 30.0])
        snap = wd.snapshot()
        # مفاتيح ثابتة (عقد /health/full)
        for key in (
            "enabled", "uptime_s", "last_real_activity_age_s",
            "last_real_account", "last_real_chat_id", "real_events_total",
            "activity_message", "event_loop_lag", "watchdog",
        ):
            assert key in snap, f"missing snapshot key: {key}"
        assert snap["event_loop_lag"]["current_ms"] == 30.0
        assert snap["event_loop_lag"]["max_ms"] == 30.0
        assert snap["watchdog"]["reconnect_arms"] == 0

    def test_lag_ms_math(self):
        t0 = 100.0
        planned = 15.0
        assert ActivityWatchdog._lag_ms(t0, planned, now=t0 + planned) == 0.0
        assert ActivityWatchdog._lag_ms(t0, planned, now=t0 + planned + 0.05) == 50.0
        # النوم المبكر غير المنطقي → يُقص عند صفر
        assert ActivityWatchdog._lag_ms(t0, planned, now=t0 + 1) == 0.0

    def test_activity_target_priority(self, monkeypatch):
        monkeypatch.setattr(activity_mod, "CFG", _activity_cfg(ACTIVITY_CHANNEL_ID=-1009999))
        wd = ActivityWatchdog(_FakeBot())
        assert wd._activity_target_chat() == -1009999
        monkeypatch.setattr(activity_mod, "CFG", _activity_cfg())
        assert wd._activity_target_chat() == TARGET
        monkeypatch.setattr(activity_mod, "CFG", _activity_cfg(ACTIVITY_MESSAGE_FALLBACK_MAIN=False))
        assert wd._activity_target_chat() == 0

    def test_note_real_activity_state(self):
        wd = ActivityWatchdog(_FakeBot())
        before = wd._real_events
        wd.note_real_activity("ACC1", 12345, 67890)
        assert wd._real_events == before + 1
        assert wd._last_real_account == "ACC1"
        assert wd._last_real_chat_id == 12345
        assert abs(time.time() - wd._last_real_activity_ts) < 1.0


# ═════════════════ 6) ReconnectionManager — عدادات الاتصال ═════════════════

class TestReconnectSnapshot:
    def _rm(self):
        m = _FakeMonitor()
        rm = ReconnectionManager.__new__(ReconnectionManager)
        rm._mon = m
        rm._backoff = 5.0
        rm._task = None
        rm._running = False
        rm._shutdown_event = asyncio.Event()
        rm._last_error = "AuthKeyDuplicatedError: session conflict"
        rm._consecutive_failures = 2
        rm._max_consecutive_failures = 10
        rm._heartbeat_interval = 90.0
        rm._last_heartbeat_ts = 0.0
        rm.attempts = 3
        rm.last_attempt_ts = time.time() - 10
        rm.last_success_ts = 0.0
        rm.last_authkey_dup_ts = time.time() - 5
        rm.last_flood_wait_ts = 0.0
        rm.last_flood_wait_s = 0.0
        return rm

    def test_snapshot_contract_keys(self):
        snap = self._rm().snapshot()
        for key in (
            "running", "attempts", "backoff_s", "consecutive_failures",
            "last_attempt_age_s", "last_success_age_s", "last_authkey_dup_age_s",
            "last_flood_wait_age_s", "last_flood_wait_s", "last_error",
        ):
            assert key in snap, f"missing reconnect snapshot key: {key}"
        assert snap["attempts"] == 3
        assert snap["running"] is False
        assert snap["last_authkey_dup_age_s"] is not None
        assert snap["last_success_age_s"] is None

    def test_snapshot_fail_safe(self):
        rm = self._rm()
        rm.snapshot.__globals__  # noqa — بديل بسيط للتأكد من الوصول
        rm._mon = None  # كسر مقصود
        snap = rm.snapshot()
        assert "attempts" in snap


# ═════════════════ 7) AlertLatencyTracker — آخر تنبيه ═════════════════

class TestAlertLatencyLastAlert:
    def test_last_alert_age_s_recorded(self):
        tr = AlertLatencyTracker()
        assert tr.snapshot()["last_alert_age_s"] is None
        tr.record(120.0, time.time() - 1.0)
        snap = tr.snapshot()
        age = snap["last_alert_age_s"]
        assert age is not None and 0 <= age <= 2.0

    def test_record_never_raises_on_garbage(self):
        tr = AlertLatencyTracker()
        tr.record(None, None)
        tr.record("not-a-number", "also-not")
        assert tr._n >= 1


# ═════════════════ 8) عدّاد إعادة التشغيل ═════════════════

class _BootFakeDB:
    """DB مزيّف لـ _persist_boot_counter — جدول app_settings في الذاكرة."""

    def __init__(self, store: Optional[Dict[str, str]] = None):
        self.store = store if store is not None else {}

    async def _fetchone(self, sql, params=()):
        key = params[0] if params else None
        val = self.store.get(key)
        return {"value": val} if val is not None else None

    async def _execute(self, sql, params=()):
        key, val = params[0], params[1]
        self.store[key] = val
        return "OK"


class TestBootCounter:
    @pytest.mark.asyncio
    async def test_first_boot_counts_one(self):
        from main import EnhancedTelegramBot

        db = _BootFakeDB()
        fake_self = SimpleNamespace(db=db, _boot_count=0, _prev_boot_ts=None)
        await EnhancedTelegramBot._persist_boot_counter(fake_self)
        assert fake_self._boot_count == 1
        saved = json.loads(db.store["boot:last"])
        assert saved["count"] == 1
        assert "ts" in saved and "version" in saved

    @pytest.mark.asyncio
    async def test_restart_increments_honestly(self):
        from main import EnhancedTelegramBot

        db = _BootFakeDB()
        for expected in (1, 2, 3):
            fake_self = SimpleNamespace(db=db, _boot_count=0, _prev_boot_ts=None)
            await EnhancedTelegramBot._persist_boot_counter(fake_self)
            assert fake_self._boot_count == expected
            # الإقلاع الثاني يرى طابع الأول (prev_boot_ts)
            if expected >= 2:
                assert fake_self._prev_boot_ts is not None
                saved = json.loads(db.store["boot:last"])
                assert saved["count"] == expected

    @pytest.mark.asyncio
    async def test_corrupt_store_fails_safe(self):
        from main import EnhancedTelegramBot

        db = _BootFakeDB(store={"boot:last": "not-json{"})
        fake_self = SimpleNamespace(db=db, _boot_count=0, _prev_boot_ts=None)
        await EnhancedTelegramBot._persist_boot_counter(fake_self)
        assert fake_self._boot_count == 1  # يبدأ من 1 بلا انهيار

    @pytest.mark.asyncio
    async def test_none_db_noop(self):
        from main import EnhancedTelegramBot

        fake_self = SimpleNamespace(db=None, _boot_count=0, _prev_boot_ts=None)
        await EnhancedTelegramBot._persist_boot_counter(fake_self)
        assert fake_self._boot_count == 0


# ═════════════════ 9) استثناء رسالة النشاط من مسار الطلبات ═════════════════

class TestPipelineExclusion:
    @pytest.fixture()
    async def monitor(self, db):
        account = {
            "name": "ACT_ECHO_TEST", "api_id": 12345, "api_hash": "test-hash",
            "phone": "+90000000000", "session": "act_echo_session", "priority": 1,
        }
        return EnhancedAccountMonitor(account, db, None)

    @pytest.mark.asyncio
    async def test_activity_message_rejected_in_pipeline(self, monitor):
        """رسالة النشاط داخل خط الأنابيب → رفض قبل أي فلترة/عدّ طلبات."""
        before = monitor._stats.get("echo_skipped", 0)
        ok = await monitor._validate_event({
            "text": CFG.ACTIVITY_MESSAGE_TEXT,
            "sender_id": 424242, "chat_id": TARGET,
        })
        assert ok is False
        assert monitor._stats["echo_skipped"] == before + 1

    @pytest.mark.asyncio
    async def test_activity_message_with_direction_marks_rejected(self, monitor):
        ok = await monitor._validate_event({
            "text": "\u200f" + CFG.ACTIVITY_MESSAGE_TEXT + "\u200e",
            "sender_id": 424242, "chat_id": TARGET,
        })
        assert ok is False

    @pytest.mark.asyncio
    async def test_real_request_still_passes(self, monitor):
        ok = await monitor._validate_event({
            "text": "عندي واجب ابي احد يحله",
            "sender_id": 424242, "chat_id": TARGET,
        })
        assert ok is True


# ═════════════════ 10) حلقة الحارس — إيقاف نظيف ═════════════════

class TestWatchdogLoopLifecycle:
    @pytest.mark.asyncio
    async def test_run_stops_when_bot_stops(self, monkeypatch):
        """is_running=False → الحلقة تنتهي من نفسها (بلا تسريب مهمة)."""
        monkeypatch.setattr(activity_mod, "CFG", _activity_cfg(
            ACTIVITY_CHECK_INTERVAL_SECONDS=0.05,
            ACTIVITY_WATCHDOG_INTERVAL_SECONDS=0.1,
        ))
        bot = _FakeBot(alert_bot=_FakeAlertBot())
        bot.is_running = False  # لا تدخل الحلقة أصلاً
        wd = ActivityWatchdog(bot)
        await asyncio.wait_for(wd.run(), timeout=2.0)  # يجب أن يعيد فوراً
        assert wd._cycles == 0

    @pytest.mark.asyncio
    async def test_run_cycles_and_reports_lag(self, monkeypatch):
        monkeypatch.setattr(activity_mod, "CFG", _activity_cfg(
            ACTIVITY_CHECK_INTERVAL_SECONDS=1,
            ACTIVITY_WATCHDOG_INTERVAL_SECONDS=1,
        ))
        bot = _FakeBot(alert_bot=_FakeAlertBot())
        wd = ActivityWatchdog(bot)

        async def stop_soon():
            await asyncio.sleep(2.5)
            bot.is_running = False

        stopper = asyncio.create_task(stop_soon())
        await asyncio.wait_for(wd.run(), timeout=10.0)
        stopper.cancel()
        assert wd._cycles >= 1
        assert wd._watchdog_cycles >= 1
        assert len(wd._lag_window) >= 1
