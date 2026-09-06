"""Integration tests for dashboard.py (FastAPI) — health, auth, and the
audit H-2 WebSocket authentication regression.

Runs against the real app with lifespan handled manually (no bot attached →
dashboard opens its own SQLite connection via CFG.DB_FILE → tests/_test_bot.db).
"""

import pytest
from httpx import ASGITransport, AsyncClient

import dashboard as dashboard_module
from dashboard import app

TOKEN = "test-dashboard-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
async def client():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


class TestPublicEndpoints:
    @pytest.mark.asyncio
    async def test_health_ok(self, client):
        r = await client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] in ("ok", "degraded")
        assert "database" in body and "uptime" in body

    @pytest.mark.asyncio
    async def test_login_page_served(self, client):
        r = await client.get("/login")
        assert r.status_code == 200
        assert "تسجيل دخول" in r.text

    @pytest.mark.asyncio
    async def test_dashboard_index_served(self, client):
        r = await client.get("/")
        assert r.status_code == 200
        assert "<!DOCTYPE html>" in r.text or "<html" in r.text


class TestApiAuth:
    @pytest.mark.asyncio
    async def test_stats_requires_token(self, client):
        r = await client.get("/api/stats")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_stats_with_token(self, client):
        r = await client.get("/api/stats", headers=AUTH)
        assert r.status_code == 200
        assert isinstance(r.json(), dict)

    @pytest.mark.asyncio
    async def test_wrong_token_rejected(self, client):
        r = await client.get("/api/stats", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_accounts_requires_token(self, client):
        r = await client.get("/api/accounts")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_keywords_requires_token(self, client):
        r = await client.get("/api/keywords")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_dead_letters_requires_token(self, client):
        r = await client.get("/api/dead-letters")
        assert r.status_code == 401


class TestLoginResponseSecurity:
    """Audit H-4: the Session String must never appear in an API response."""

    @pytest.mark.asyncio
    async def test_no_session_string_key_in_success_response(self, client, monkeypatch):
        async def fake_upsert(key, value):
            return {"saved": False, "reason": "RENDER_API_KEY / RENDER_SERVICE_ID غير مضبوطة"}

        monkeypatch.setattr(dashboard_module, "render_upsert_env", fake_upsert)
        r = await client.post("/api/login/verify-code", headers=AUTH,
                              json={"prefix": "MAIN", "code": "12345"})
        # Either 400 (no pending login) or 200 — the assertion is about the body.
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        assert "session_string" not in str(body.get("detail", "")) and \
               body.get("session_string") is None if isinstance(body, dict) else True


class TestWebSocketAuth:
    """Audit H-2: /ws must reject unauthenticated clients (close 1008) and
    accept authenticated ones. httpx has no WS support, so these use the
    starlette TestClient (which does)."""

    def test_ws_rejected_without_token(self):
        from starlette.testclient import TestClient

        with TestClient(app) as tc:
            with pytest.raises(Exception):
                with tc.websocket_connect("/ws") as ws:
                    ws.receive_json()

    def test_ws_rejected_with_wrong_token(self):
        from starlette.testclient import TestClient

        with TestClient(app) as tc:
            with pytest.raises(Exception):
                with tc.websocket_connect("/ws?token=wrong-token") as ws:
                    ws.receive_json()

    def test_ws_accepted_with_valid_token(self):
        from starlette.testclient import TestClient

        with TestClient(app) as tc:
            with tc.websocket_connect(f"/ws?token={TOKEN}") as ws:
                ws.send_json({"command": "ping"})
                # The stats loop may interleave a "stats" push; wait for pong.
                import time

                pong = None
                deadline = time.time() + 5
                while time.time() < deadline:
                    msg = ws.receive_json()
                    if msg.get("type") == "pong":
                        pong = msg
                        break
                assert pong is not None, "did not receive pong from authenticated WS"
