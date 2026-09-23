"""Integration tests for webadmin (modular admin panel) — auth gate,
brute-force lockout, CSRF, and path-traversal safety in backups."""

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard import app


class TestWebadminAuthGate:
    @pytest.mark.asyncio
    async def test_admin_api_requires_session(self):
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                for path in ("/stats", "/alerts", "/keywords", "/logs",
                             "/accounts", "/settings", "/bot/backups"):
                    r = await ac.get(path)
                    assert r.status_code in (401, 403), f"{path} -> {r.status_code}"

    @pytest.mark.asyncio
    async def test_admin_spa_page_served(self):
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.get("/admin")
                assert r.status_code == 200
                assert "<!DOCTYPE html>" in r.text or "<html" in r.text


class TestWebadminLogin:
    @pytest.mark.asyncio
    async def test_login_503_when_not_configured(self, monkeypatch):
        monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
        monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post("/admin/api/auth/login",
                                  json={"username": "x", "password": "y"})
                assert r.status_code == 503

    @pytest.mark.asyncio
    async def test_login_success_sets_cookie_and_csrf(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
        monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret-password")
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post("/admin/api/auth/login",
                                  json={"username": "admin", "password": "s3cret-password"})
                assert r.status_code == 200
                body = r.json()
                assert body["success"] and body["csrf"]
                assert "wa_session" in r.cookies

    @pytest.mark.asyncio
    async def test_wrong_password_rejected(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_USERNAME", "admin")
        monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret-password")
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                r = await ac.post("/admin/api/auth/login",
                                  json={"username": "admin", "password": "wrong"})
                assert r.status_code == 401


class TestBackupPathSafety:
    def test_backup_zip_path_blocks_traversal(self):
        from webadmin.backup import backup_zip_path
        result = backup_zip_path("../../etc/passwd")
        # basename() strips directories — result must stay inside backups dir.
        assert "backups" in result
        assert ".." not in result
        assert result.endswith(".zip")


class TestAccountRowCounters:
    def test_account_row_exposes_monitor_counters(self):
        """v9.13.3: dashboard mini-stats need messages/errors/duplicates."""
        from webadmin.routes import _account_row

        class FakeMonitor:
            account = {"prefix": "MAIN"}
            is_connected = True
            _last_connect_error = None
            _stats = {"messages_processed": 7, "errors": 2, "duplicates": 3,
                      "alerts": 1, "last_connected": 1234}

        acc = {"prefix": "MAIN", "name": "Main", "phone": "+966...", "priority": 10}
        row = _account_row(acc, [FakeMonitor()])
        assert row["connected"] is True
        assert row["messages_processed"] == 7
        assert row["errors"] == 2
        assert row["duplicates"] == 3

    def test_account_row_counters_default_without_monitor(self):
        from webadmin.routes import _account_row
        row = _account_row({"prefix": "X", "name": "X"}, [])
        assert row["messages_processed"] == 0
        assert row["errors"] == 0
        assert row["duplicates"] == 0
        assert row["connected"] is False
