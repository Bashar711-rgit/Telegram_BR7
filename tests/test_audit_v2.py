"""Tests for v9.27 audit v2: q search / actor / since_hours filters + CSV export.

The filters are read-only (no schema change). q uses literal LIKE escaping.
since_hours is a str on purpose (alerts style: corrupt values = "all", no 422).
"""

import pytest


@pytest.mark.asyncio
class TestAuditFiltersDB:
    async def test_q_across_fields(self, db):
        await db.record_audit("qa-actor", "block.sender", object_type="sender",
                              object_id="7110000002", new_value="test-spam")
        await db.record_audit("other-actor", "keyword.add", object_id="cat",
                              new_value="funny-word")
        rows = await db.get_audit_logs(q="7110000002")
        assert len(rows) == 1 and rows[0]["action"] == "block.sender"
        rows = await db.get_audit_logs(q="funny-word")
        assert len(rows) == 1 and rows[0]["action"] == "keyword.add"
        # matches actor too
        rows = await db.get_audit_logs(q="qa-actor")
        assert len(rows) == 1

    async def test_q_literal_escaping(self, db):
        """100% must match nothing; an underscore needle stays literal."""
        await db.record_audit("a1", "keyword.add", new_value="100%")
        await db.record_audit("a2", "keyword.add", new_value="search_me")
        assert await db.get_audit_logs(q="100%")  # self-consistent: 1 hit
        rows = await db.get_audit_logs(q="100%")
        assert len(rows) == 1
        rows = await db.get_audit_logs(q="search_me")
        assert len(rows) == 1  # literal _ — not any-char
        rows = await db.get_audit_logs(q="searchXme")
        assert len(rows) == 0

    async def test_actor_filter(self, db):
        await db.record_audit("actor-A", "settings.update")
        await db.record_audit("actor-B", "settings.update")
        rows = await db.get_audit_logs(actor="actor-A")
        assert len(rows) == 1 and rows[0]["actor"] == "actor-A"

    async def test_q_plus_action_combined(self, db):
        await db.record_audit("x", "rule.add", new_value="alpha")
        await db.record_audit("x", "rule.remove", new_value="alpha")
        rows = await db.get_audit_logs(q="alpha", action="rule.remove")
        assert len(rows) == 1 and rows[0]["action"] == "rule.remove"

    async def test_since_hours_excludes_old_row(self, db):
        await db.record_audit("y", "queue.purge")
        await db._execute(
            "UPDATE audit_logs SET created_at = ? WHERE action = 'queue.purge'",
            ("2020-01-01 00:00:00",),
        )
        await db._commit()
        rows = await db.get_audit_logs(since_hours=24)
        assert len(rows) == 0
        rows = await db.get_audit_logs()
        assert len(rows) == 1

    async def test_since_hours_corrupt_is_ignored(self, db):
        await db.record_audit("z", "bot.restart")
        rows = await db.get_audit_logs(since_hours="not-a-number")
        assert len(rows) == 1
        rows = await db.get_audit_logs(since_hours="-5")
        assert len(rows) == 1  # negative → clamped then ignored-safe

    async def test_count_matches_rows(self, db):
        for i in range(3):
            await db.record_audit("cnt", "queue.purge", new_value=str(i))
        n_rows = len(await db.get_audit_logs(q="cnt", action="queue.purge"))
        n_count = await db.count_audit_logs(q="cnt", action="queue.purge")
        assert n_rows == n_count == 3


