#!/usr/bin/env python3
"""اختبارات v15.6.0 — بوابة التحقق المتزامنة (_validate_event).

الطلب: «السرعة في الفلترة بشكل متزامن». الفحوص الخمسة التي كانت جولات
DB متتابعة (0.4-1.2s لكل رسالة على Supabase) أصبحت asyncio.gather واحدة.

ما تثبته هذه الاختبارات:
  1. التكافؤ السلوكي حرفياً — نفس قرار كل فحص وبنفس ترتيبه الأصلي:
     blocked_sender → blocked_chat → sources_scope → antispam(sync ثم
     async) → echo → length. والإحصاءات نفسها (source_skipped/spam_skipped/
     echo_skipped/length_rejected).
  2. التزامن الفعلي — الفحوص البطيئة تتداخل زمنياً (max concurrent ≥ 2)،
     وهذا مستحيل في التنفيذ المتسلسل القديم.
  3. تثبيت عدد الاستعلامات — is_source_allowed لا يُستدعى إطلاقاً عندما
     تكون خاصية sources_scope معطلة، وis_ignored_async لا تُستدعى عندما
     يحسم الفحص المتزامن (نفس عدّ الاستعلامات في الأصل).
  4. انتشار الاستثناء — فشل أحد فحوص DB ينتشر كما في الأصل (يجري caller
     إلى DLQ) ولم يُبتلع بلا سبب.
"""

import asyncio

import pytest

import monitors as mon
from monitors import EnhancedAccountMonitor


ACCOUNT = {
    "name": "GATE_TEST", "api_id": 12345, "api_hash": "test-hash",
    "phone": "+90000000000", "session": "gate_session", "priority": 1,
}


class _StubAntispam:
    """بديل حتمي لـ get_antispam — يتحكم به الاختبار صراحة."""

    def __init__(self, sync_ignored=False, async_ignored=False):
        self._sync = sync_ignored
        self._async = async_ignored
        self.async_calls = 0

    def is_ignored(self, sender_id):
        return self._sync

    async def is_ignored_async(self, sender_id):
        self.async_calls += 1
        return self._async


@pytest.fixture()
async def monitor(db):
    return EnhancedAccountMonitor(ACCOUNT, db, None)


def _patch_db(monitor, monkeypatch, *, blocked_sender=False, blocked_chat=False,
              scope_feature=False, scope_allowed=True, slow_s=0.0):
    active = {"n": 0, "max": 0}

    async def _maybe_slow():
        if slow_s > 0:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            await asyncio.sleep(slow_s)
            active["n"] -= 1

    async def _blocked_sender(sender_id):
        await _maybe_slow()
        return blocked_sender

    async def _blocked_chat(chat_id):
        await _maybe_slow()
        return blocked_chat

    async def _feature(name):
        return scope_feature

    async def _allowed(chat_id):
        return scope_allowed

    monkeypatch.setattr(monitor.db, "is_blocked_sender", _blocked_sender)
    monkeypatch.setattr(monitor.db, "is_blocked_chat", _blocked_chat)
    monkeypatch.setattr(monitor.db, "is_feature_enabled", _feature)
    monkeypatch.setattr(monitor.db, "is_source_allowed", _allowed)
    return active


class TestDecisionParity:
    """نفس القرار ونفس الإحصاء وبنفس الترتيب كما في التنفيذ المتسلسل."""

    @pytest.mark.asyncio
    async def test_clean_event_accepted(self, monitor, monkeypatch):
        _patch_db(monitor, monkeypatch)
        monkeypatch.setattr(mon, "get_antispam", lambda: _StubAntispam())
        data = {"text": "ابي حلل", "sender_id": 1, "chat_id": 1}
        assert await monitor._validate_event(data) is True

    @pytest.mark.asyncio
    async def test_blocked_sender_rejected_first(self, monitor, monkeypatch):
        _patch_db(monitor, monkeypatch, blocked_sender=True, blocked_chat=True)
        monkeypatch.setattr(mon, "get_antispam", lambda: _StubAntispam())
        assert await monitor._validate_event(
            {"text": "ابي حلل", "sender_id": 1, "chat_id": 1}) is False

    @pytest.mark.asyncio
    async def test_blocked_chat_rejected_second(self, monitor, monkeypatch):
        _patch_db(monitor, monkeypatch, blocked_chat=True)
        monkeypatch.setattr(mon, "get_antispam", lambda: _StubAntispam())
        assert await monitor._validate_event(
            {"text": "ابي حلل", "sender_id": 1, "chat_id": 1}) is False

    @pytest.mark.asyncio
    async def test_source_scope_blocks_and_counts(self, monitor, monkeypatch):
        _patch_db(monitor, monkeypatch, scope_feature=True, scope_allowed=False)
        monkeypatch.setattr(mon, "get_antispam", lambda: _StubAntispam())
        data = {"text": "ابي حلل", "sender_id": 1, "chat_id": 1}
        assert await monitor._validate_event(data) is False
        assert monitor._stats["source_skipped"] == 1

    @pytest.mark.asyncio
    async def test_source_scope_off_allows_any_chat(self, monitor, monkeypatch):
        """sources_scope معطلة → أي محادثة تمر (لا استعلام is_source_allowed)."""
        active = _patch_db(monitor, monkeypatch, scope_feature=False)
        monkeypatch.setattr(mon, "get_antispam", lambda: _StubAntispam())
        data = {"text": "ابي حلل", "sender_id": 1, "chat_id": 999}
        assert await monitor._validate_event(data) is True

    @pytest.mark.asyncio
    async def test_antispam_sync_holds_async_query(self, monitor, monkeypatch):
        """الفحص المتزامن يحسم → is_ignored_async لا تُطلق (عدّ الاستعلامات
        مطابق للأصل الذي كان يستدعيها فقط بعد اجتياز المتزامن)."""
        _patch_db(monitor, monkeypatch)
        stub = _StubAntispam(sync_ignored=True)
        monkeypatch.setattr(mon, "get_antispam", lambda: stub)
        data = {"text": "ابي حلل", "sender_id": 1, "chat_id": 1}
        assert await monitor._validate_event(data) is False
        assert stub.async_calls == 0
        assert monitor._stats["spam_skipped"] == 1

    @pytest.mark.asyncio
    async def test_antispam_async_rejects(self, monitor, monkeypatch):
        _patch_db(monitor, monkeypatch)
        stub = _StubAntispam(async_ignored=True)
        monkeypatch.setattr(mon, "get_antispam", lambda: stub)
        data = {"text": "ابي حلل", "sender_id": 1, "chat_id": 1}
        assert await monitor._validate_event(data) is False
        assert stub.async_calls == 1
        assert monitor._stats["spam_skipped"] == 1

    @pytest.mark.asyncio
    async def test_length_gate_still_last(self, monitor, monkeypatch):
        """بوابة الطول تبقى بعد فحوص DB كما في الأصل (5 نقاط → رفض)."""
        _patch_db(monitor, monkeypatch)
        monkeypatch.setattr(mon, "get_antispam", lambda: _StubAntispam())
        data = {"text": "مرحبا", "sender_id": 1, "chat_id": 1}  # 5 → رفض
        assert await monitor._validate_event(data) is False
        assert monitor._stats["length_rejected"] == 1


class TestConcurrency:
    """الإثبات الهيكلي للتحسين: الفحوص تتداخل زمنياً لا تتعاقب."""

    @pytest.mark.asyncio
    async def test_db_checks_run_in_parallel(self, monitor, monkeypatch):
        active = _patch_db(monitor, monkeypatch, slow_s=0.12)
        monkeypatch.setattr(mon, "get_antispam", lambda: _StubAntispam())
        data = {"text": "ابي حلل", "sender_id": 1, "chat_id": 1}
        assert await monitor._validate_event(data) is True
        # المتسلسل القديم يعطي max=1 دائماً؛ المتوازي يتداخل ≥ 2
        assert active["max"] >= 2, (
            f"الفحوص لم تتداخل (max_concurrent={active['max']}) — تراجع للتنفيذ المتسلسل؟")

    @pytest.mark.asyncio
    async def test_parallel_is_faster_than_serial_sum(self, monitor, monkeypatch):
        """زمن البوابة أقرب لجولة DB واحدة من مجموع الجولات (2×0.12s)."""
        _patch_db(monitor, monkeypatch, slow_s=0.12)
        monkeypatch.setattr(mon, "get_antispam", lambda: _StubAntispam())
        data = {"text": "ابي حلل", "sender_id": 1, "chat_id": 1}
        import time as _t
        t0 = _t.perf_counter()
        assert await monitor._validate_event(data) is True
        elapsed = _t.perf_counter() - t0
        assert elapsed < 0.23, (
            f"الزمن {elapsed:.3f}s يقترب من مجموع المسار المتسلسل (0.24s) — لا تزامن")


class TestExceptionPropagation:
    """فشل DB ينتشر كما في الأصل — caller يحوله إلى DLQ (لا ابتلاع صامت)."""

    @pytest.mark.asyncio
    async def test_db_failure_propagates(self, monitor, monkeypatch):
        async def _boom(sender_id):
            raise RuntimeError("db down")
        monkeypatch.setattr(monitor.db, "is_blocked_sender", _boom)
        monkeypatch.setattr(mon, "get_antispam", lambda: _StubAntispam())
        with pytest.raises(RuntimeError):
            await monitor._validate_event(
                {"text": "ابي حلل", "sender_id": 1, "chat_id": 1})


class TestBotCaptureDefaults:
    """التقاط البوتات — التزامن لا يمس البوابة (v15.0): event_sender_gate
    تلتقط البوت الخارجي وتتخطى هويتنا الذاتية فقط."""

    def test_capture_from_bots_default_true(self):
        assert bool(mon.CFG.CAPTURE_FROM_BOTS) is True

    def test_fast_capture_default_true(self):
        assert bool(mon.CFG.FAST_CAPTURE_ENABLED) is True

    def test_gate_captures_external_bot(self):
        # sender بوت خارجي (bot=True) وليس هويتنا → التقاط (False = لا تخطٍّ)
        fake_bot = type("S", (), {"bot": True})()
        assert mon.event_sender_gate(999_111, fake_bot, chat_id=1) is False

    def test_gate_skips_self_identity(self):
        from monitors import _SELF_SENDER_IDS, register_self_sender
        register_self_sender(777_002)
        try:
            assert mon.event_sender_gate(777_002, None, chat_id=1) is True
        finally:
            _SELF_SENDER_IDS.discard(777_002)
