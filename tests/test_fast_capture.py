"""Unit tests for monitors.py v9.8 — FastCaptureBuffer (deletion-race
protection), the NewMessage save-first hook, the MessageDeleted
diagnostics/recovery handler, and the alert-time text fallback.

All Telegram interactions are mocked (SimpleNamespace fakes); nothing
touches the real network. The DB fixture is the shared SQLite test
database from conftest.py.
"""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from config import CFG
from monitors import (
    EnhancedAccountMonitor,
    FastCaptureBuffer,
    _capture,
    get_capture_snapshot,
)


# ─────────────────────────── helpers / fixtures ───────────────────────────

def _fake_event(
    chat_id: int = -1001234567890,
    msg_id: int = 42,
    text: Optional[str] = "أبي مساعدة في واجب الاحصاء",
    sender_id: int = 555000111,
    first_name: str = "أحمد",
    out: bool = False,
):
    """Fake Telethon NewMessage event (only the attributes the hook reads)."""
    msg = SimpleNamespace(id=msg_id, text=text, message=None, date=None)
    sender = SimpleNamespace(first_name=first_name, bot=False) if first_name else None
    return SimpleNamespace(
        out=out, chat_id=chat_id, message=msg, sender=sender, sender_id=sender_id
    )


def _fake_deleted_event(chat_id: Optional[int], msg_ids: List[int]):
    """Fake Telethon MessageDeleted event (deleted_ids may be absent)."""
    return SimpleNamespace(chat_id=chat_id, deleted_ids=list(msg_ids))


@pytest.fixture()
def buf():
    return FastCaptureBuffer(maxsize=10, ttl=30)


@pytest.fixture()
def enable_capture():
    """Temporarily flip CFG.FAST_CAPTURE_ENABLED (frozen dataclass →
    object.__setattr__, same live-apply pattern as dashboard_store)."""
    old = CFG.FAST_CAPTURE_ENABLED
    object.__setattr__(CFG, "FAST_CAPTURE_ENABLED", True)
    _capture.clear()
    for k in ("saved", "expired", "evicted", "deleted_captured", "deleted_recovered", "fallback_used"):
        _capture._stats.pop(k, None)
    yield
    object.__setattr__(CFG, "FAST_CAPTURE_ENABLED", old)
    _capture.clear()


@pytest.fixture()
async def monitor(db):
    account: Dict[str, Any] = {
        "name": "TestAcc",
        "api_id": 12345,
        "api_hash": "test-hash",
        "phone": "+90000000000",
        "session": "test_session",
        "priority": 1,
    }
    mon = EnhancedAccountMonitor(account, db, None)
    yield mon


ACCOUNT = "TestAcc"
CHAT = -1001234567890


# ─────────────────────────── FastCaptureBuffer core ───────────────────────

class TestFastCaptureBufferCore:
    async def test_save_get_roundtrip(self, buf):
        buf.save(CHAT, 7, "نص تجريبي", sender_id=11, sender_name="سعد", account=ACCOUNT)
        e = buf.get(CHAT, 7)
        assert e is not None
        assert e["text"] == "نص تجريبي"
        assert e["sender_id"] == 11
        assert e["sender_name"] == "سعد"
        assert e["account"] == ACCOUNT
        assert e["queued"] is False and e["alerted"] is False
        assert e["deleted_claimed"] is False and e["recovered"] is False
        assert buf.size() == 1
        assert buf.stats()["saved"] == 1

    async def test_multi_account_same_key_keeps_first_ts(self, buf):
        buf.save(CHAT, 9, "hello", sender_id=1, account="acc1")
        ts_first = buf.get(CHAT, 9)["ts"]
        await asyncio.sleep(0.01)
        buf.save(CHAT, 9, "hello", sender_id=1, account="acc2")
        e = buf.get(CHAT, 9)
        assert e["ts"] == ts_first          # first-seen timestamp preserved
        assert buf.size() == 1              # still ONE entry
        assert buf.stats()["saved"] == 2    # both deliveries counted

    async def test_ttl_expiry(self):
        b = FastCaptureBuffer(maxsize=10, ttl=1)
        b.save(CHAT, 1, "x")
        assert b.get(CHAT, 1) is not None
        await asyncio.sleep(1.05)
        assert b.get(CHAT, 1) is None       # expired + evicted
        assert b.stats()["expired"] == 1
        assert b.size() == 0

    async def test_maxsize_fifo_eviction(self):
        b = FastCaptureBuffer(maxsize=3, ttl=30)
        for i in range(5):
            b.save(CHAT, i, f"m{i}")
        assert b.size() == 3
        assert b.get(CHAT, 0) is None       # oldest evicted
        assert b.get(CHAT, 1) is None
        assert b.get(CHAT, 4) is not None   # newest kept
        assert b.stats()["evicted"] == 2

    async def test_lazy_gc_sweeps_expired(self):
        b = FastCaptureBuffer(maxsize=10, ttl=1)
        b.save(CHAT, 1, "a")
        b.save(CHAT, 2, "b")
        await asyncio.sleep(1.05)
        b.save(CHAT, 3, "c")                # triggers the lazy sweep
        assert b.get(CHAT, 1) is None
        assert b.get(CHAT, 2) is None
        assert b.get(CHAT, 3) is not None
        assert b.stats()["expired"] >= 2

    async def test_find_by_msg_id_and_lookup_none_chat(self, buf):
        buf.save(CHAT, 77, "ومين يقدر يساعد", sender_id=2, account=ACCOUNT)
        assert buf.find_by_msg_id(77) is not None
        # MessageDeleted without chat_id (private/small groups) → scan path
        assert buf.lookup(None, 77) is not None
        assert buf.lookup(None, 999) is None

    async def test_mark_queued_and_alerted(self, buf):
        buf.save(CHAT, 5, "t")
        buf.mark_queued(CHAT, 5)
        buf.mark_alerted(CHAT, 5)
        e = buf.get(CHAT, 5)
        assert e["queued"] is True and e["alerted"] is True

    async def test_mark_on_missing_entry_is_noop(self, buf):
        buf.mark_queued(CHAT, 404)
        buf.mark_alerted(CHAT, 404)
        assert buf.size() == 0

    async def test_claim_deleted_exactly_once(self, buf):
        buf.save(CHAT, 6, "t")
        first = buf.claim_deleted(CHAT, 6)
        assert first is not None and first["deleted_claimed"] is True
        assert buf.claim_deleted(CHAT, 6) is None      # second claimer loses
        assert buf.claim_deleted(CHAT, 7) is None      # unknown msg

    async def test_clear(self, buf):
        buf.save(CHAT, 1, "t")
        buf.clear()
        assert buf.size() == 0


# ─────────────────────────── global singleton ─────────────────────────────

class TestGlobalCaptureSingleton:
    async def test_snapshot_shape(self, enable_capture):
        _capture.save(CHAT, 1, "نص", sender_id=3, account=ACCOUNT)
        snap = get_capture_snapshot()
        assert snap["enabled"] is True
        assert snap["size"] == 1
        assert snap["saved"] == 1
        for k in ("expired", "evicted", "deleted_captured", "deleted_recovered", "fallback_used"):
            assert k in snap

    async def test_disabled_flag(self, buf):
        # default in code is False — kill switch semantics
        object.__setattr__(CFG, "FAST_CAPTURE_ENABLED", False)
        try:
            assert buf.enabled is False
            assert get_capture_snapshot()["enabled"] is False
        finally:
            object.__setattr__(CFG, "FAST_CAPTURE_ENABLED", True)


# ─────────────────────────── monitor integration ──────────────────────────

class TestMonitorFastCaptureHook:
    async def test_capture_saves_text(self, monitor, enable_capture):
        monitor._fast_capture(_fake_event())
        e = _capture.get(CHAT, 42)
        assert e is not None
        assert e["text"] == "أبي مساعدة في واجب الاحصاء"
        assert e["sender_id"] == 555000111
        assert e["sender_name"] == "أحمد"
        assert e["account"] == ACCOUNT

    async def test_capture_ignores_voice_photo(self, monitor, enable_capture):
        monitor._fast_capture(_fake_event(text=None))     # voice note
        monitor._fast_capture(_fake_event(text=""))       # empty
        assert _capture.size() == 0

    async def test_capture_swallows_exceptions(self, monitor, enable_capture):
        class Boom:
            def __getattr__(self, name):
                raise RuntimeError("boom")
        bad = SimpleNamespace(out=False, chat_id=CHAT, message=Boom(), sender=None, sender_id=1)
        monitor._fast_capture(bad)                        # must NOT raise
        assert _capture.size() == 0

    async def test_capture_never_awaits(self, monitor, enable_capture):
        """save-first is synchronous: _fast_capture must be a plain function
        (not a coroutine function) so no yield point exists before the text
        is stored."""
        import inspect
        assert inspect.iscoroutinefunction(monitor._fast_capture) is False
        ret = monitor._fast_capture(_fake_event())
        assert ret is None


class TestMonitorDeletedHandler:
    async def test_deleted_never_queued_triggers_recovery(self, monitor, enable_capture, monkeypatch):
        monitor._fast_capture(_fake_event())              # captured, NOT queued
        recovered: List[Dict[str, Any]] = []

        async def _rec(entry):
            recovered.append(entry)

        monkeypatch.setattr(monitor, "_spawn_recovery_task", _rec)
        await monitor._handle_deleted_event(_fake_deleted_event(CHAT, [42]))
        assert len(recovered) == 1
        entry = recovered[0]
        assert entry["text"] == "أبي مساعدة في واجب الاحصاء"
        assert entry["msg_id"] == 42
        assert entry["chat_id"] == CHAT
        assert _capture.stats()["deleted_captured"] == 1
        assert _capture.stats()["deleted_recovered"] == 1

    async def test_deleted_recovery_fires_once_across_clients(self, monitor, enable_capture, monkeypatch):
        monitor._fast_capture(_fake_event())
        calls: List[int] = []

        async def _rec(entry):
            calls.append(entry["msg_id"])

        monkeypatch.setattr(monitor, "_spawn_recovery_task", _rec)
        # Same deletion notice arriving on several account clients → claim
        # dedupes: exactly one recovery.
        await monitor._handle_deleted_event(_fake_deleted_event(CHAT, [42]))
        await monitor._handle_deleted_event(_fake_deleted_event(CHAT, [42]))
        assert calls == [42]

    async def test_deleted_already_queued_no_recovery(self, monitor, enable_capture, monkeypatch):
        monitor._fast_capture(_fake_event())
        _capture.mark_queued(CHAT, 42)                    # normal pipeline owns it
        calls: List[int] = []

        async def _rec(entry):
            calls.append(entry["msg_id"])

        monkeypatch.setattr(monitor, "_spawn_recovery_task", _rec)
        await monitor._handle_deleted_event(_fake_deleted_event(CHAT, [42]))
        assert calls == []                                # logged, not recovered
        assert _capture.stats()["deleted_captured"] == 1

    async def test_deleted_alerted_no_recovery(self, monitor, enable_capture, monkeypatch):
        monitor._fast_capture(_fake_event())
        _capture.mark_queued(CHAT, 42)
        _capture.mark_alerted(CHAT, 42)
        calls: List[int] = []

        async def _rec(entry):
            calls.append(entry["msg_id"])

        monkeypatch.setattr(monitor, "_spawn_recovery_task", _rec)
        await monitor._handle_deleted_event(_fake_deleted_event(CHAT, [42]))
        assert calls == []

    async def test_deleted_without_chat_id_uses_msg_id_scan(self, monitor, enable_capture, monkeypatch):
        monitor._fast_capture(_fake_event(msg_id=99))
        calls: List[int] = []

        async def _rec(entry):
            calls.append(entry["msg_id"])

        monkeypatch.setattr(monitor, "_spawn_recovery_task", _rec)
        await monitor._handle_deleted_event(_fake_deleted_event(None, [99]))
        assert calls == [99]

    async def test_deleted_batch_ids(self, monitor, enable_capture, monkeypatch):
        for i in (1, 2, 3):
            monitor._fast_capture(_fake_event(msg_id=i))
        calls: List[int] = []

        async def _rec(entry):
            calls.append(entry["msg_id"])

        monkeypatch.setattr(monitor, "_spawn_recovery_task", _rec)
        await monitor._handle_deleted_event(_fake_deleted_event(CHAT, [1, 2, 3]))
        assert sorted(calls) == [1, 2, 3]

    async def test_deleted_unknown_message_is_silent(self, monitor, enable_capture):
        await monitor._handle_deleted_event(_fake_deleted_event(CHAT, [404]))
        assert _capture.stats().get("deleted_captured", 0) == 0

    async def test_deleted_disabled_is_noop(self, monitor, monkeypatch):
        object.__setattr__(CFG, "FAST_CAPTURE_ENABLED", False)
        try:
            monitor._fast_capture(_fake_event())
            assert _capture.size() == 0                   # hook gated by flag
            calls: List[int] = []

            async def _rec(entry):
                calls.append(entry["msg_id"])

            monkeypatch.setattr(monitor, "_spawn_recovery_task", _rec)
            await monitor._handle_deleted_event(_fake_deleted_event(CHAT, [42]))
            assert calls == []
        finally:
            object.__setattr__(CFG, "FAST_CAPTURE_ENABLED", True)


class TestMonitorRecoveryTask:
    async def test_recovery_task_runs_pipeline(self, monitor, enable_capture, monkeypatch):
        seen: List[Dict[str, Any]] = []
        done = asyncio.Event()

        async def _pipeline(event_data, retry_count=0):
            seen.append(event_data)
            done.set()

        monkeypatch.setattr(monitor, "process_event_from_queue", _pipeline)
        monitor._fast_capture(_fake_event())
        entry = dict(_capture.get(CHAT, 42))
        await monitor._spawn_recovery_task(entry)
        await asyncio.wait_for(done.wait(), timeout=2)
        data = seen[0]
        assert data["chat_id"] == CHAT
        assert data["message_id"] == 42
        assert data["text"] == "أبي مساعدة في واجب الاحصاء"
        assert data["has_text"] is True
        assert data["has_media"] is False
        assert data["_fast_capture_recovery"] is True


class TestAlertTimeFallback:
    async def test_recover_captured_text(self, monitor, enable_capture):
        monitor._fast_capture(_fake_event())
        data = {"chat_id": CHAT, "message_id": 42, "text": "", "has_media": False, "media_object": None}
        assert monitor._recover_captured_text(data) == "أبي مساعدة في واجب الاحصاء"
        assert _capture.stats()["fallback_used"] == 1

    async def test_no_recovery_when_pipeline_has_text(self, monitor, enable_capture):
        monitor._fast_capture(_fake_event())
        data = {"chat_id": CHAT, "message_id": 42, "text": "نص موجود", "has_media": False}
        assert monitor._recover_captured_text(data) is None

    async def test_no_recovery_out_of_bounds(self, monitor, enable_capture):
        long_text = "ا" * (CFG.MAX_MESSAGE_LENGTH + 10)
        monitor._fast_capture(_fake_event(text=long_text))
        data = {"chat_id": CHAT, "message_id": 42, "text": "", "has_media": False}
        assert monitor._recover_captured_text(data) is None

    async def test_no_recovery_when_not_captured(self, monitor, enable_capture):
        data = {"chat_id": CHAT, "message_id": 424242, "text": "", "has_media": False}
        assert monitor._recover_captured_text(data) is None

    async def test_disabled_fallback(self, monitor):
        object.__setattr__(CFG, "FAST_CAPTURE_ENABLED", False)
        try:
            data = {"chat_id": CHAT, "message_id": 42, "text": "", "has_media": False}
            assert monitor._recover_captured_text(data) is None
        finally:
            object.__setattr__(CFG, "FAST_CAPTURE_ENABLED", True)


class TestPipelineWiring:
    async def test_pipeline_uses_fallback_before_analyze(self, monitor, enable_capture, monkeypatch):
        """_process_event_pipeline must consult the capture buffer when the
        stored text is empty, BEFORE calling _analyze_and_alert."""
        monitor._fast_capture(_fake_event())
        analyzed: List[Optional[str]] = []

        async def _fake_validate(data):
            return True

        async def _fake_store(data):
            # simulate a pipeline whose own copy of the text was lost
            return ("hash_x", None, True)

        async def _fake_analyze(data, msg_hash, validated_text):
            analyzed.append(validated_text)

        monkeypatch.setattr(monitor, "_validate_event", _fake_validate)
        monkeypatch.setattr(monitor, "_store_message", _fake_store)
        monkeypatch.setattr(monitor, "_analyze_and_alert", _fake_analyze)
        await monitor._process_event_pipeline({"chat_id": CHAT, "message_id": 42, "text": ""})
        assert analyzed == ["أبي مساعدة في واجب الاحصاء"]
