"""Integration tests for dashboard.py (FastAPI) — health, auth, and the
audit H-2 WebSocket authentication regression.

Runs against the real app with lifespan handled manually (no bot attached →
dashboard opens its own SQLite connection via CFG.DB_FILE → tests/_test_bot.db).
"""

import time

import pytest
from httpx import ASGITransport, AsyncClient

import dashboard as dashboard_module
from config import fast_hash
from dashboard import app
from database import AlertRecord

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
        # v9.12 (audit L-08): /health is now the minimal liveness probe —
        # status + database + db_healthy + time only. The detailed
        # operational fields (uptime, monitors_up, fast_capture, etc.)
        # moved to /health/full which requires auth.
        assert body["status"] in ("ok", "degraded")
        assert "database" in body
        assert "db_healthy" in body
        # uptime is no longer in the public /health response.
        assert "uptime" not in body

    @pytest.mark.asyncio
    async def test_health_full_requires_auth(self, client):
        # /health/full must reject unauthenticated requests.
        r = await client.get("/health/full")
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_health_full_authenticated(self, client):
        # With auth, /health/full returns the full operational picture.
        r = await client.get("/health/full", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] in ("ok", "degraded")
        assert "uptime" in body
        assert "monitors_total" in body

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


class TestMessagesExactSearch:
    """v9.16: /api/messages?exact=1 — word-boundary search (Arabic + Latin),
    plus blocked_chats grand total in /api/stats."""

    @pytest.mark.asyncio
    async def _seed(self, client):
        import time as _t
        db = app.state.db
        await db._execute("DELETE FROM messages WHERE message_hash LIKE 't916_%'", ())
        rows = [
            ('t916_1', 'محتاج تقرير عن الفيزياء'),      # كلمة مستقلة ✓
            ('t916_2', 'لا أحتاج شيئاً الآن'),            # أحتاج ⊂ داخل كلمة ✗
            ('t916_3', 'I need the reports now, Reporting'),  # need ✓ / report جزئي ✗
        ]
        for h, txt in rows:
            await db._execute(
                "INSERT OR IGNORE INTO messages (message_hash, chat_id, sender_id, message_text, timestamp) VALUES (?,?,?,?,?)",
                (h, -1001234, 987654321, txt, _t.time()),
            )

    @pytest.mark.asyncio
    async def test_partial_search_default(self, client):
        await self._seed(client)
        r = await client.get("/api/messages", headers=AUTH, params={"keyword": "محتاج", "limit": 100})
        assert r.status_code == 200
        texts = [m["message_text"] for m in r.json()["messages"]]
        assert any("محتاج تقرير" in t for t in texts)

    @pytest.mark.asyncio
    async def test_exact_word_arabic(self, client):
        await self._seed(client)
        # "احتاج" يوجد فقط داخل "أحتاج" — البحث الكامل يجب ألا يطابقها كـ"احتاج"
        r = await client.get("/api/messages", headers=AUTH, params={"keyword": "محتاج", "exact": "true", "limit": 100})
        assert r.status_code == 200
        texts = [m["message_text"] for m in r.json()["messages"]]
        assert any(t for t in texts if "محتاج" in t)
        assert not any("أحتاج" in t for t in texts)

    @pytest.mark.asyncio
    async def test_exact_word_latin_case_insensitive(self, client):
        await self._seed(client)
        r = await client.get("/api/messages", headers=AUTH, params={"keyword": "need", "exact": "1", "limit": 100})
        assert r.status_code == 200
        texts = [m["message_text"] for m in r.json()["messages"]]
        assert any("I need the reports" in t for t in texts)

    @pytest.mark.asyncio
    async def test_stats_includes_blocked_chats(self, client):
        # stats_cache يُملأ من حلقة البث غير المتزامنة — انتظر حتى أول دفعة
        import asyncio
        import time as _t
        deadline = _t.time() + 10
        body: dict = {}
        while _t.time() < deadline:
            r = await client.get("/api/stats", headers=AUTH)
            assert r.status_code == 200
            body = r.json()
            if body:
                break
            await asyncio.sleep(0.3)
        assert body, "stats_cache was never populated"
        assert "blocked_chats" in body
        assert isinstance(body["blocked_chats"], int)


class TestAlertsHoursFilter:
    """v9.17: فلتر النطاق الزمني hours على /api/alerts و /api/alerts/export."""

    @pytest.mark.asyncio
    async def test_alerts_hours_echoed_in_filters(self, client):
        r = await client.get("/api/alerts", headers=AUTH, params={"hours": 24})
        assert r.status_code == 200
        body = r.json()
        assert body["filters"]["hours"] == 24

    @pytest.mark.asyncio
    async def test_alerts_hours_excludes_old_rows(self, client):
        # أضف تنبيهاً قديماً (سنتان) ثم تحقق أن hours=1 يستبعده من العدد
        db = dashboard_module.app.state.db
        old_ts = time.time() - 2 * 365 * 24 * 3600
        rec = AlertRecord(
            message_hash=fast_hash("old-alert-hours-test"), chat_id=-1000,
            sender_id=900001, account_name="Main", keyword="قديمة",
            alert_text="تنبيه قديم جداً", timestamp=old_ts,
            decision="accept", confidence=0.9)
        assert await db.add_alert(rec) is True
        await db._flush()  # alerts live in a batch buffer until flushed
        r = await client.get("/api/alerts", headers=AUTH, params={"hours": 1, "keyword": "قديمة"})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0 and body["count"] == 0
        # بلا فلتر زمني يظهر نفس التنبيه
        r2 = await client.get("/api/alerts", headers=AUTH, params={"keyword": "قديمة"})
        assert r2.status_code == 200
        assert r2.json()["total"] >= 1

    @pytest.mark.asyncio
    async def test_alerts_hours_out_of_range_clamped(self, client):
        # hours أكبر من السقف (168) أو غير رقمي لا تكسر الطلب
        r = await client.get("/api/alerts", headers=AUTH, params={"hours": 99999})
        assert r.status_code == 200
        r2 = await client.get("/api/alerts", headers=AUTH, params={"hours": "abc"})
        assert r2.status_code == 200


class TestBlockSenderSource:
    """v9.17: source يُسجَّل فعلياً في blocked_by (قائمة سماح dashboard/alert/system)."""

    @pytest.mark.asyncio
    async def test_block_from_alert_records_alert_source(self, client):
        uid = 911001
        r = await client.post("/api/blocked/senders", headers=AUTH,
                              json={"user_id": uid, "reason": "اختبار", "source": "alert"})
        assert r.status_code == 200
        assert r.json()["source"] == "alert"
        rows = await dashboard_module.app.state.db._fetchall(
            "SELECT blocked_by FROM blocked_senders WHERE sender_id = ?", (uid,))
        assert rows and rows[0]["blocked_by"] == "alert"
        # تنظيف
        await client.delete(f"/api/blocked/senders/{uid}", headers=AUTH)

    @pytest.mark.asyncio
    async def test_block_default_source_is_dashboard(self, client):
        uid = 911002
        r = await client.post("/api/blocked/senders", headers=AUTH,
                              json={"user_id": uid, "reason": "اختبار"})
        assert r.status_code == 200
        rows = await dashboard_module.app.state.db._fetchall(
            "SELECT blocked_by FROM blocked_senders WHERE sender_id = ?", (uid,))
        assert rows and rows[0]["blocked_by"] == "dashboard"
        await client.delete(f"/api/blocked/senders/{uid}", headers=AUTH)

    @pytest.mark.asyncio
    async def test_block_invalid_source_falls_back(self, client):
        uid = 911003
        r = await client.post("/api/blocked/senders", headers=AUTH,
                              json={"user_id": uid, "source": "evil-value"})
        assert r.status_code == 200
        assert r.json()["source"] == "dashboard"
        rows = await dashboard_module.app.state.db._fetchall(
            "SELECT blocked_by FROM blocked_senders WHERE sender_id = ?", (uid,))
        assert rows and rows[0]["blocked_by"] == "dashboard"
        await client.delete(f"/api/blocked/senders/{uid}", headers=AUTH)
