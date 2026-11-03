"""Tests for v9.28: retention policies (configurable from the panel) and the
brute-force auth guard (per-IP lockout with exponential backoff).

Retention: each table pruned by its own days; old behavior (no params) fully
preserved; one-day floor; settings validation; live CFG application; the
retention group must appear in the template's renderSettings order map.
Auth guard: 429 + Retry-After, lockout includes valid tokens, IP isolation,
success clears state, auth.lockout audit row with the IP in object_id.
"""

import time

import pytest

from database import EnhancedDatabase


# ---------------------------------------------------------------------------
# Retention: DB layer
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestRetentionDB:
    async def test_each_table_pruned_by_its_days(self, db):
        await db.record_audit("qa", "queue.purge")
        await db.record_notification(ntype="t", title="n")
        # age audit to 100 days and notifications to 40 days
        await db._execute("UPDATE audit_logs SET created_at = '2020-01-01 00:00:00'")
        await db._execute("UPDATE notifications SET created_at = '2020-01-01 00:00:00'")
        await db._commit()
        total = await db.cleanup_old_data(days=7, audit_days=90, notifications_days=30)
        # audit row (100d old > 90) pruned; notifications (40d > 30) pruned
        assert await db.count_audit_logs() == 0
        assert len(await db.get_notifications()) == 0
        assert total == 0  # messages/alerts counts unaffected by seeds

    async def test_old_behavior_without_params(self, db):
        """No params → the historical fixed values (90/30) apply verbatim."""
        await db.record_audit("qa", "queue.purge")
        await db._execute("UPDATE audit_logs SET created_at = '2020-01-01 00:00:00'")
        await db._commit()
        await db.cleanup_old_data(days=7)  # audit_days not passed → 90
        assert await db.count_audit_logs() == 0  # 2020 < 90d ago either way
        # a 60-day-old row survives the default (90) but not audit_days=30
        await db.record_audit("qa2", "queue.purge")
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 60 * 86400))
        await db._execute("UPDATE audit_logs SET created_at = ?", (cutoff,))
        await db._commit()
        await db.cleanup_old_data(days=7)  # default 90 → kept
        assert await db.count_audit_logs() == 1
        await db.cleanup_old_data(days=7, audit_days=30)  # explicit 30 → pruned
        assert await db.count_audit_logs() == 0

    async def test_one_day_floor(self, db):
        """A corrupt value must not wipe all history."""
        await db.record_audit("qa", "queue.purge")
        await db.cleanup_old_data(days=7, audit_days=0)  # floor → 1 day
        assert await db.count_audit_logs() == 1
        await db.cleanup_old_data(days=7, audit_days=-5)  # floor → 1 day
        assert await db.count_audit_logs() == 1

    async def test_notifications_days_param(self, db):
        await db.record_notification(ntype="t", title="keep")
        cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 40 * 86400))
        await db._execute("UPDATE notifications SET created_at = ?", (cutoff,))
        await db._commit()
        await db.cleanup_old_data(days=7, notifications_days=30)
        assert len(await db.get_notifications()) == 0


# ---------------------------------------------------------------------------
# Retention: settings schema + template exclusivity
# ---------------------------------------------------------------------------
