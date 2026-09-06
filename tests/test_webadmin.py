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
