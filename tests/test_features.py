"""Tests for v9.25 Feature Registry (P6-1) + the notifications gate.

Covers: registry defaults, override upsert cycle, corrupt-row fail-safe,
the single notifications gate at DB level, a typo-guard asserting monitors'
guard keys ⊆ registry, API auth, live toggle+audit+notification flow, and
that disabling notifications actually blocks new notifications.
"""

import pytest

import monitors as monitors_module
from database import EnhancedDatabase


@pytest.mark.asyncio
class TestFeatureRegistry:
    async def test_registry_defaults(self, db):
        items = await db.list_features()
        keys = {f["key"] for f in items}
        assert keys == set(EnhancedDatabase.FEATURE_REGISTRY.keys())
        for f in items:
            assert f["enabled"] is True  # all defaults on
            assert f["overridden"] is False
            assert f["impact"]

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

    async def test_unknown_key_fail_safe_true(self, db):
        assert await db.is_feature_enabled("does_not_exist") is True
        assert await db.set_feature_enabled("does_not_exist", False) is False

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


@pytest.fixture()
async def api_client():
    from httpx import ASGITransport, AsyncClient
    import dashboard as dash
    from config import CFG
    headers = {"Authorization": f"Bearer {CFG.DASHBOARD_AUTH_TOKEN}"}
    async with dash.app.router.lifespan_context(dash.app):
        transport = ASGITransport(app=dash.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, headers


@pytest.mark.asyncio
class TestFeaturesAPI:
    async def test_requires_auth(self, api_client):
        ac, _ = api_client
        assert (await ac.get("/api/features")).status_code == 401
        assert (await ac.post("/api/features/rule_engine/toggle")).status_code == 401

    async def test_unknown_key_404(self, api_client):
        ac, h = api_client
        r = await ac.post("/api/features/not_a_feature/toggle", headers=h)
        assert r.status_code == 404

    async def test_toggle_flow_audit_and_notification(self, api_client):
        ac, h = api_client
        r = await ac.get("/api/features", headers=h)
        assert r.status_code == 200
        keys = {f["key"] for f in r.json()["items"]}
        assert "rule_engine" in keys
        # toggle off → audit + warn notification
        r = await ac.post("/api/features/rule_engine/toggle", headers=h)
        assert r.json()["enabled"] is False
        r = await ac.get("/api/audit?limit=20", headers=h)
        row = next(a for a in r.json()["items"] if a["action"] == "feature.toggle")
        assert row["object_id"] == "rule_engine" and row["new_value"] == "False"
        r = await ac.get("/api/notifications?limit=10", headers=h)
        types = [n["ntype"] for n in r.json()["items"]]
        assert "feature.disabled" in types
        # restore on
        r = await ac.post("/api/features/rule_engine/toggle", headers=h)
        assert r.json()["enabled"] is True

    async def test_disable_notifications_blocks_new_ones(self, api_client):
        ac, h = api_client
        # drain: mark everything read so the unread counter starts clean
        await ac.post("/api/notifications/read-all", headers=h)
        base = (await ac.get("/api/notifications?limit=1", headers=h)).json()["unread"]
        # disable notifications feature
        r = await ac.post("/api/features/notifications/toggle", headers=h)
        assert r.json()["enabled"] is False
        try:
            # a live block action would normally generate a notification
            r = await ac.post("/api/blocked/senders", headers=h,
                              json={"user_id": 987654, "reason": "notif-gate-qa"})
            assert r.status_code == 200
            # gate is off → the toggle and the block created NO new unread rows
            after = (await ac.get("/api/notifications?limit=1", headers=h)).json()["unread"]
            assert after == base
        finally:
            r = await ac.post("/api/features/notifications/toggle", headers=h)
            assert r.json()["enabled"] is True
            # cleanup the seeded block (isolation — see worklog note)
            await ac.delete("/api/blocked/senders/987654", headers=h)
