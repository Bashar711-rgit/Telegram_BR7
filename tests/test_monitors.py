"""Unit tests for monitors.py — CircuitBreaker (FloodWait handling),
DeadLetterQueue retry bounds, and handler-support logic. All Telegram
interactions are mocked; nothing touches the real network."""

import asyncio
import time

import pytest
from telethon.errors import FloodWaitError

from monitors import CircuitBreaker, CircuitBreakerOpen, DeadLetterQueue


def _make_floodwait(seconds: int) -> FloodWaitError:
    """Build a real FloodWaitError without network access."""
    err = FloodWaitError(request=None)
    object.__setattr__(err, "seconds", seconds)
    return err


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_success_passthrough(self):
        cb = CircuitBreaker("test")
        async def ok():
            return 42
        assert await cb.call(ok) == 42
        assert cb.state == "closed"

    @pytest.mark.asyncio
    async def test_opens_after_threshold(self):
        cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=60)
        async def boom():
            raise RuntimeError("x")
        with pytest.raises(RuntimeError):
            await cb.call(boom)
        with pytest.raises(RuntimeError):
            await cb.call(boom)
        # Threshold reached — next call must be rejected without executing.
        with pytest.raises(CircuitBreakerOpen):
            await cb.call(boom)

    @pytest.mark.asyncio
    async def test_floodwait_records_exact_duration(self):
        cb = CircuitBreaker("flood", failure_threshold=5, recovery_timeout=1)
        async def flooded():
            raise _make_floodwait(30)
        with pytest.raises(FloodWaitError):
            await cb.call(flooded)
        remaining = cb.flood_wait_remaining
        assert 25 <= remaining <= 30.5, remaining

    @pytest.mark.asyncio
    async def test_floodwait_blocks_until_window(self):
        cb = CircuitBreaker("flood2", failure_threshold=5, recovery_timeout=1)
        async def flooded():
            raise _make_floodwait(5)
        with pytest.raises(FloodWaitError):
            await cb.call(flooded)
        async def never_called():
            raise AssertionError("must not be executed during FloodWait gate")
        with pytest.raises(CircuitBreakerOpen):
            await cb.call(never_called)

    @pytest.mark.asyncio
    async def test_floodwait_never_compounds(self):
        """Repeated FloodWaits must extend, never stack, the wait window."""
        cb = CircuitBreaker("flood3", failure_threshold=100, recovery_timeout=1)
        async def flooded():
            raise _make_floodwait(10)
        with pytest.raises(FloodWaitError):
            await cb.call(flooded)
        await asyncio.sleep(0.05)
        # Second attempt is gated by the active FloodWait window — the call
        # must be rejected (CircuitBreakerOpen) WITHOUT executing the op.
        executed = {"n": 0}
        async def must_not_run():
            executed["n"] += 1
            return None
        with pytest.raises(CircuitBreakerOpen):
            await cb.call(must_not_run)
        assert executed["n"] == 0
        # 10s window minus ~0.05s elapsed; a compounding bug would show ~20s.
        assert cb.flood_wait_remaining <= 10.5, cb.flood_wait_remaining


class TestDeadLetterQueue:
    @pytest.mark.asyncio
    async def test_retry_succeeds_then_stops(self):
        calls = {"n": 0}

        async def reprocess(event, retry_count):
            calls["n"] += 1
            calls["retry"] = retry_count

        dlq = DeadLetterQueue(db=None, max_retries=3, reprocess_callback=reprocess,
                              account_name="t")
        await dlq.push({"chat_id": 1, "message_id": 2},
                       RuntimeError("first failure"), retry_count=0)
        # First backoff delay is 1 s (min(2**0, 60)) — wait past it.
        await asyncio.sleep(1.6)
        assert calls["n"] == 1
        assert calls["retry"] == 1
        await dlq.stop()

    @pytest.mark.asyncio
    async def test_duplicate_push_is_skipped(self):
        calls = {"n": 0}

        async def reprocess(event, retry_count):
            calls["n"] += 1

        dlq = DeadLetterQueue(db=None, max_retries=3, reprocess_callback=reprocess,
                              account_name="t")
        event = {"chat_id": 1, "message_id": 2}
        await dlq.push(event, RuntimeError("e1"), retry_count=0)
        await dlq.push(event, RuntimeError("e2"), retry_count=0)  # dup while pending
        await asyncio.sleep(1.6)
        assert calls["n"] == 1, "duplicate retry scheduled for same event"
        await dlq.stop()

    @pytest.mark.asyncio
    async def test_max_retries_persists_dead_letter(self, db):
        async def always_fails(event, retry_count):
            raise RuntimeError("still broken")

        dlq = DeadLetterQueue(db=db, max_retries=1, reprocess_callback=always_fails,
                              account_name="t")
        # retry_count=1 is already >= max_retries=1 → persisted immediately.
        await dlq.push({"chat_id": 7, "message_id": 8},
                       RuntimeError("e"), retry_count=1)
        await asyncio.sleep(0.3)
        letters = await db.get_dead_letters(limit=10)
        assert len(letters) == 1
        assert letters[0].retry_count >= 1
        await dlq.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_pending_retries(self):
        async def reprocess(event, retry_count):
            await asyncio.sleep(30)

        dlq = DeadLetterQueue(db=None, max_retries=5, reprocess_callback=reprocess,
                              account_name="t")
        await dlq.push({"chat_id": 1, "message_id": 1}, RuntimeError("e"), retry_count=0)
        await asyncio.sleep(0.05)
        start = time.monotonic()
        await asyncio.wait_for(dlq.stop(), timeout=5)
        assert time.monotonic() - start < 3, "stop() should not wait 30s on a sleeping retry"
