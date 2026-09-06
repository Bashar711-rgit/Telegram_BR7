"""Unit tests for database.py — dedup (C-3), queue counts (M-4),
hourly stats (H-6), writer restart (H-5), dead letters, cooldown."""

import time

import pytest

from config import fast_hash
from database import AlertRecord, DeadLetterRecord, MessageRecord


@pytest.mark.asyncio
class TestMessageDedup:
    async def test_second_insert_is_not_new(self, db):
        """Audit C-3 regression: SQLite dedup must actually dedup."""
        rec = MessageRecord(
            message_hash=fast_hash("chat_1_msg_1_hello world"),
            chat_id=1, sender_id=10, message_text="hello world", timestamp=time.time(),
        )
        assert await db.try_insert_message(rec) is True
        assert await db.try_insert_message(rec) is False

    async def test_sender_stats_not_inflated_by_duplicates(self, db):
        rec = MessageRecord(
            message_hash=fast_hash("chat_1_msg_2_count me once"),
            chat_id=1, sender_id=11, message_text="count me once", timestamp=time.time(),
        )
        await db.try_insert_message(rec)
        await db.try_insert_message(rec)
        await db.try_insert_message(rec)
        row = await db._fetchone(
            "SELECT total_messages FROM sender_stats WHERE sender_id = 11"
        )
        assert row is not None and row["total_messages"] == 1

    async def test_in_memory_cache_short_circuits(self, db):
        rec = MessageRecord(
            message_hash=fast_hash("chat_1_msg_3_cached"),
            chat_id=1, sender_id=12, message_text="cached", timestamp=time.time(),
        )
        assert await db.try_insert_message(rec) is True
        assert rec.message_hash in db.message_cache


@pytest.mark.asyncio
class TestQueue:
    async def test_add_pop_roundtrip(self, db):
        assert await db.add_to_queue({"chat_id": 5, "message_id": 9}, priority=7) > 0
        event = await db.pop_from_queue()
        assert event is not None
        assert event["chat_id"] == 5 and event["message_id"] == 9

    async def test_pop_empty_returns_none(self, db):
        assert await db.pop_from_queue() is None

    async def test_purge_returns_accurate_count(self, db):
        """Audit M-4 regression: purge count must match rows, not cumulative counters."""
        for i in range(3):
            await db.add_to_queue({"i": i})
        assert await db.purge_queue() == 3
        assert await db.purge_queue() == 0  # second purge: exactly zero

    async def test_priority_order(self, db):
        await db.add_to_queue({"p": "low"}, priority=1)
        await db.add_to_queue({"p": "high"}, priority=9)
        first = await db.pop_from_queue()
        assert first["p"] == "high"


@pytest.mark.asyncio
class TestHourlyStats:
    async def test_query_runs_without_sql_error(self, db):
        """Audit H-6 regression: hourly stats previously referenced
        non-existent messages.decision/confidence columns."""
        now = time.time()
        await db.try_insert_message(MessageRecord(
            message_hash=fast_hash("h1"), chat_id=1, sender_id=1,
            message_text="hello", timestamp=now))
        await db.add_alert(AlertRecord(
            message_hash=fast_hash("h1"), chat_id=1, sender_id=1,
            account_name="Main", keyword="طلب", alert_text="x",
            timestamp=now, decision="accept", confidence=0.9))
        await db._flush()  # alerts live in a batch buffer until flushed
        hourly = await db.get_hourly_stats(24)
        assert isinstance(hourly, list) and len(hourly) >= 1
        row = hourly[-1]
        assert row["messages_count"] >= 1
        assert row["alerts_count"] >= 1
        assert row["accepted_count"] >= 1
        assert 0 < row["avg_confidence"] <= 1.0


@pytest.mark.asyncio
class TestDeadLetters:
    async def test_add_and_list(self, db):
        await db.add_dead_letter(DeadLetterRecord(
            event_data={"chat_id": 1, "message_id": 2},
            error_text="boom", retry_count=3, resolved=False, timestamp=time.time()))
        records = await db.get_dead_letters(limit=10)
        assert len(records) == 1
        assert records[0].error_text == "boom"
        assert records[0].retry_count == 3

    async def test_cleanup_removes_resolved(self, db):
        await db.add_dead_letter(DeadLetterRecord(
            event_data={}, error_text="e", retry_count=0, resolved=True, timestamp=time.time()))
        removed = await db.cleanup_dead_letters(days=7)
        assert removed >= 1
        assert await db.get_dead_letters(limit=10) == []

    async def test_cleanup_count_is_accurate(self, db):
        """Audit M-4: cleanup totals must reflect deleted rows only."""
        for i in range(4):
            await db.add_dead_letter(DeadLetterRecord(
                event_data={}, error_text="e", retry_count=0, resolved=True, timestamp=time.time()))
        removed = await db.cleanup_dead_letters(days=7)
        assert removed == 4


@pytest.mark.asyncio
class TestAlertsAndCooldown:
    async def test_can_send_alert_first_time(self, db):
        assert await db.can_send_alert(sender_id=999) is True

    async def test_cooldown_blocks_second_alert(self, db):
        now = time.time()
        await db._execute(
            "INSERT INTO sender_stats (sender_id, reputation_score, last_alert_time, first_seen) "
            "VALUES (?, 50.0, ?, ?)",
            (555, now - 10, now - 100),
        )
        await db._commit()
        # Cooldown for rep=50: max(30, ALERT_COOLDOWN * (1 - 50/200)) — definitely > 10s
        assert await db.can_send_alert(sender_id=555) is False

    async def test_add_alert_batches(self, db):
        rec = AlertRecord(
            message_hash=fast_hash("alert-hash-1"), chat_id=2, sender_id=3,
            account_name="Main", keyword="حل", alert_text="text",
            timestamp=time.time())
        assert await db.add_alert(rec) is True
        async with db._batch_lock:
            assert len(db._batch) == 1

    async def test_flush_persists_alert(self, db):
        rec = AlertRecord(
            message_hash=fast_hash("alert-hash-2"), chat_id=2, sender_id=3,
            account_name="Main", keyword="حل", alert_text="text",
            timestamp=time.time())
        await db.add_alert(rec)
        await db._flush()
        row = await db._fetchone(
            "SELECT COUNT(*) AS cnt FROM alerts WHERE message_hash = ?",
            (rec.message_hash,),
        )
        assert row["cnt"] == 1
        # Double flush must not duplicate (message_hash UNIQUE).
        async with db._batch_lock:
            db._batch.append(("alert", rec.message_hash, 2, 3, "Main", "حل",
                              "text", rec.timestamp, "accept", 0.9, "", None, None, 0, 0.0))
        await db._flush()
        row = await db._fetchone(
            "SELECT COUNT(*) AS cnt FROM alerts WHERE message_hash = ?",
            (rec.message_hash,),
        )
        assert row["cnt"] == 1


@pytest.mark.asyncio
class TestBlocklists:
    async def test_block_unblock_sender(self, db):
        await db.block_sender(42, reason="spam", by="admin")
        assert await db.is_blocked_sender(42) is True
        await db.unblock_sender(42)
        assert await db.is_blocked_sender(42) is False

    async def test_block_chat(self, db):
        await db.block_chat(-100777, reason="test")
        assert await db.is_blocked_chat(-100777) is True
        await db.unblock_chat(-100777)
        assert await db.is_blocked_chat(-100777) is False


@pytest.mark.asyncio
class TestWriterRestart:
    async def test_writer_restarts_after_close(self, db):
        """Audit H-5 regression: start_writer must work after close()."""
        from database import EnhancedDatabase

        db.is_connected = True
        # Call the REAL start_writer (the fixture stubs it out per-instance).
        await EnhancedDatabase.start_writer(db)
        first = db._writer_task
        assert first is not None and not first.done()
        await db.close()
        assert db._writer_task is None and db._backup_task is None
        db.is_connected = True
        await EnhancedDatabase.start_writer(db)
        second = db._writer_task
        assert second is not None and second is not first and not second.done()
        await db.close()


@pytest.mark.asyncio
class TestSchema:
    async def test_no_duplicate_message_rows(self, db):
        h = fast_hash("unique-msg")
        rec = MessageRecord(message_hash=h, chat_id=1, sender_id=1,
                            message_text="unique-msg", timestamp=time.time())
        await db.try_insert_message(rec)
        await db.try_insert_message(rec)
        row = await db._fetchone(
            "SELECT COUNT(*) AS cnt FROM messages WHERE message_hash = ?", (h,))
        assert row["cnt"] == 1
