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
from dashboard import _AuthGuard, _auth_guard


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
class TestRetentionSettings:
    def test_schema_validation_and_limits(self):
        from dashboard_store import validate_updates
        # below the 30-day audit floor → error
        clean, errors = validate_updates({"retention_audit_days": 7})
        assert "retention_audit_days" in errors
        clean, errors = validate_updates({"retention_audit_days": 180})
        assert not errors and clean["retention_audit_days"] == 180
        clean, errors = validate_updates({"retention_messages_days": 0})
        assert errors

    def test_template_renders_retention_group(self):
        """Exclusivity test: every group in the schema must appear in the
        template's renderSettings order map — prevents silent drift."""
        import re
        import dashboard_store as store
        from config import CFG  # noqa: F401 (schema import side effects)
        template = open("templates/dashboard.html", encoding="utf-8").read()
        m = re.search(
            r"const order = \{\s*([^}]*)\};", template
        )
        assert m, "order map missing"
        order_keys = set(re.findall(r"(\w+):\s*'", m.group(1)))
        schema_groups = {spec["group"] for spec in store.SETTINGS_SCHEMA.values()}
        missing = schema_groups - order_keys
        assert not missing, f"groups missing from template order map: {missing}"

    def test_current_values_includes_retention(self):
        import dashboard_store as store
        from config import CFG
        vals = store.current_values(type("App", (), {"state": object()})()) \
            if False else store.current_values.__wrapped__ if hasattr(store.current_values, "__wrapped__") else None
        # simpler: call with a stub app whose state has no bot
        class _State:
            bot_ref = None
        class _App:
            state = _State()
        vals = store.current_values(_App())
        assert "retention_audit_days" in vals
        assert vals["retention_messages_days"] == CFG.RETENTION_MESSAGES_DAYS


# ---------------------------------------------------------------------------
# Auth guard: unit
# ---------------------------------------------------------------------------
class TestAuthGuardUnit:
    def setup_method(self):
        self.g = _AuthGuard()
        self.g.threshold = 3
        self.g.window = 300
        self.g.base_lock = 60
        self.g.max_lock = 900

    def test_no_lock_before_threshold(self):
        assert self.g.record_failure("1.1.1.1") == 0
        assert self.g.record_failure("1.1.1.1") == 0
        assert self.g.locked_seconds_left("1.1.1.1") == 0

    def test_lock_at_threshold_and_backoff(self):
        for _ in range(3):
            lock = self.g.record_failure("1.1.1.1")
        assert lock == 60
        # after the lock window, the next threshold crossing doubles
        self.g._lockout_until["1.1.1.1"] = time.time() - 1  # force expiry
        for _ in range(3):
            lock = self.g.record_failure("1.1.1.1")
        assert lock == 120

    def test_success_clears_state(self):
        for _ in range(3):
            self.g.record_failure("1.1.1.1")
        self.g.record_success("1.1.1.1")
        assert self.g.locked_seconds_left("1.1.1.1") == 0
        assert self.g.record_failure("1.1.1.1") == 0  # counter restarted

    def test_ip_isolation(self):
        for _ in range(3):
            self.g.record_failure("1.1.1.1")
        assert self.g.locked_seconds_left("2.2.2.2") == 0

    def test_cap_at_max(self):
        g = _AuthGuard()
        g.threshold = 1
        g.base_lock = 900
        g.max_lock = 900
        assert g.record_failure("3.3.3.3") == 900
        g._lockout_until["3.3.3.3"] = time.time() - 1
        assert g.record_failure("3.3.3.3") == 900  # never exceeds max

    def test_client_ip_prefers_xff_first_hop(self):
        from starlette.requests import Request as StarletteRequest

        def make(headers):
            scope = {
                "type": "http", "method": "GET", "path": "/", "headers": headers,
                "query_string": b"", "client": ("9.9.9.9", 1000), "server": ("s", 80),
            }
            return StarletteRequest(scope)

        req = make([(b"x-forwarded-for", b"1.2.3.4, 5.6.7.8")])
        assert _AuthGuard.client_ip(req) == "1.2.3.4"
        req2 = make([])
        assert _AuthGuard.client_ip(req2) == "9.9.9.9"


# ---------------------------------------------------------------------------
# Auth guard: integration over HTTP
# ---------------------------------------------------------------------------
@pytest.fixture()
async def api_client():
    from httpx import ASGITransport, AsyncClient
    import dashboard as dash
    from config import CFG
    headers = {"Authorization": f"Bearer {CFG.DASHBOARD_AUTH_TOKEN}"}
    async with dash.app.router.lifespan_context(dash.app):
        transport = ASGITransport(app=dash.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, headers, dash


@pytest.mark.asyncio
class TestAuthGuardIntegration:
    async def test_lockout_includes_valid_token_and_429(self, api_client):
        ac, h, dash = api_client
        ip = "198.51.100.77"
        dash._auth_guard.threshold = 3
        dash._auth_guard.window = 300
        dash._auth_guard.base_lock = 60
        dash._auth_guard.max_lock = 900
        try:
            bad = {"Authorization": "Bearer wrong-token", "X-Forwarded-For": ip}
            for _ in range(3):
                r = await ac.get("/api/stats", headers=bad)
                assert r.status_code == 401
            # even a VALID token from the locked IP gets 429 + Retry-After
            r = await ac.get("/api/stats", headers={**h, "X-Forwarded-For": ip})
            assert r.status_code == 429
            assert int(r.headers["retry-after"]) > 0
            # another IP is unaffected
            r = await ac.get("/api/stats", headers={**h, "X-Forwarded-For": "203.0.113.9"})
            assert r.status_code == 200
        finally:
            dash._auth_guard.record_success(ip)

    async def test_lockout_writes_audit_row_and_notification(self, api_client):
        ac, h, dash = api_client
        ip = "198.51.100.78"
        dash._auth_guard.threshold = 2
        dash._auth_guard.base_lock = 60
        try:
            bad = {"Authorization": "Bearer nope", "X-Forwarded-For": ip}
            await ac.get("/api/stats", headers=bad)
            await ac.get("/api/stats", headers=bad)  # threshold reached → 429
            r = await ac.get("/api/stats", headers=bad)
            assert r.status_code == 429
            from conftest import drain_dashboard_tasks
            await drain_dashboard_tasks()
            r = await ac.get("/api/audit?q=auth.lockout&limit=10",
                             headers={**h, "X-Forwarded-For": "203.0.113.55"})
            items = r.json()["items"]
            row = next((a for a in items if a["action"] == "auth.lockout"), None)
            assert row is not None and row["object_id"] == ip
            r = await ac.get("/api/notifications?limit=10",
                             headers={**h, "X-Forwarded-For": "203.0.113.55"})
            types = [n["ntype"] for n in r.json()["items"]]
            assert "auth.lockout" in types
        finally:
            dash._auth_guard.record_success(ip)
            # clean the seeded notification/audit rows is unnecessary — read-only suite

    async def test_success_after_lockout_clears(self, api_client):
        ac, h, dash = api_client
        ip = "198.51.100.79"
        dash._auth_guard.threshold = 2
        dash._auth_guard.base_lock = 60
        try:
            bad = {"Authorization": "Bearer nope", "X-Forwarded-For": ip}
            await ac.get("/api/stats", headers=bad)
            await ac.get("/api/stats", headers=bad)
            assert (await ac.get("/api/stats", headers={**h, "X-Forwarded-For": ip})).status_code == 429
            dash._auth_guard.record_success(ip)  # e.g. IP rotation window passed
            assert (await ac.get("/api/stats", headers={**h, "X-Forwarded-For": ip})).status_code == 200
        finally:
            dash._auth_guard.record_success(ip)
