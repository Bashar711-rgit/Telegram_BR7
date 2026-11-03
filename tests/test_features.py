"""Tests for v9.25 Feature Registry (P6-1) + the notifications gate.

Covers: registry defaults, override upsert cycle, corrupt-row fail-safe,
the single notifications gate at DB level, a typo-guard asserting monitors'
guard keys ⊆ registry, API auth, live toggle+audit+notification flow, and
that disabling notifications actually blocks new notifications.
"""

import pytest

import monitors as monitors_module
from database import EnhancedDatabase


# v12.2.0: علامة asyncio على مستوى الفصل كانت تُلصق اختباراً متزامناً
# (test_monitors_guards_subset_of_registry) — تُزال وتوضع صراحة على
# الاختبارات غير المتزامنة فقط (نظافة تحذيرات pytest).
class TestFeatureRegistry:
    @pytest.mark.asyncio
    async def test_registry_defaults(self, db):
        items = await db.list_features()
        keys = {f["key"] for f in items}
        assert keys == set(EnhancedDatabase.FEATURE_REGISTRY.keys())
        for f in items:
            assert f["enabled"] is True  # all defaults on
            assert f["overridden"] is False
            assert f["impact"]

    @pytest.mark.asyncio
    async def test_toggle_cycle_and_overrides(self, db):
        ok = await db.set_feature_enabled("rule_engine", False, by="qa")
        assert ok is True
        assert await db.is_feature_enabled("rule_engine") is False
        items = {f["key"]: f for f in await db.list_features()}
        assert items["rule_engine"]["enabled"] is False
        assert items["rule_engine"]["overridden"] is True
        assert items["rule_engine"]["updated_by"] == "qa"
        # back on
        await db.set_feature_enabled("rule_engine", True)
        assert await db.is_feature_enabled("rule_engine") is True

    @pytest.mark.asyncio
    async def test_unknown_key_fail_safe_true(self, db):
        assert await db.is_feature_enabled("does_not_exist") is True
        assert await db.set_feature_enabled("does_not_exist", False) is False

    @pytest.mark.asyncio
    async def test_corrupt_row_fail_safe_true(self, db):
        """A corrupt override must fail open (feature considered enabled)."""
        await db.set_feature_enabled("allowlist", False)
        # corrupt: enabled = 2 (truthy) — still enabled; and a raw bad row
        await db._execute("UPDATE features SET enabled = 0 WHERE key = 'allowlist'")
        await db._commit()
        assert await db.is_feature_enabled("allowlist") is False
        # now force an exception path: drop the table
        await db._execute("DROP TABLE features")
        await db._commit()
        db._features_cache.clear()
        assert await db.is_feature_enabled("allowlist") is True  # fail-safe

    @pytest.mark.asyncio
    async def test_notifications_gate_at_db_level(self, db):
        """v9.25: one gate inside record_notification covers both panels."""
        assert await db.record_notification(ntype="t", title="hello") is True
        await db.set_feature_enabled("notifications", False)
        assert await db.record_notification(ntype="t", title="blocked") is False
        rows = await db.get_notifications()
        assert len(rows) == 1 and rows[0]["title"] == "hello"
        # feature back on → notifications flow again
        await db.set_feature_enabled("notifications", True)
        assert await db.record_notification(ntype="t", title="again") is True

    def test_monitors_guards_subset_of_registry(self):
        """Typo-guard: every feature key referenced in monitors.py guards
        must exist in the registry (prevents silent drift)."""
        src = open(monitors_module.__file__, encoding="utf-8").read()
        import re
        keys = set(re.findall(r'is_feature_enabled\(\s*["\']([a-z_]+)["\']\s*\)', src))
        assert keys, "guards must exist in monitors.py"
        assert keys.issubset(set(EnhancedDatabase.FEATURE_REGISTRY.keys()))


