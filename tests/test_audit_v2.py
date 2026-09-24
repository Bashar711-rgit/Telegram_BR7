"""Tests for v9.27 audit v2: q search / actor / since_hours filters + CSV export.

The filters are read-only (no schema change). q uses literal LIKE escaping.
since_hours is a str on purpose (alerts style: corrupt values = "all", no 422).
"""

import pytest


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


@pytest.mark.asyncio
class TestAuditAPI:
    async def test_requires_auth(self, api_client):
        ac, _ = api_client
        assert (await ac.get("/api/audit")).status_code == 401
        assert (await ac.get("/api/audit/export")).status_code == 401

    async def test_since_hours_corrupt_no_422(self, api_client):
        ac, h = api_client
        r = await ac.get("/api/audit?since_hours=garbage", headers=h)
        assert r.status_code == 200

    async def test_csv_export_headers_and_rows(self, api_client):
        ac, h = api_client
        # seed 2 operations
        await ac.post("/api/blocked/senders", headers=h, json={"user_id": 555001, "reason": "csv-qa"})
        await ac.post("/api/blocked/senders", headers=h, json={"user_id": 555002, "reason": "csv-qa"})
        from conftest import drain_dashboard_tasks
        await drain_dashboard_tasks()
        r = await ac.get("/api/audit/export", headers=h)
        assert r.status_code == 200
        assert "attachment" in r.headers.get("content-disposition", "")
        assert "br7-audit-" in r.headers.get("content-disposition", "")
        text = r.content.decode("utf-8-sig")  # BOM stripped
        lines = text.strip().split("\n")
        assert lines[0].startswith("id,created_at,actor,action")
        assert sum(1 for ln in lines[1:] if ln.strip()) >= 2
        # cleanup
        await ac.delete("/api/blocked/senders/555001", headers=h)
        await ac.delete("/api/blocked/senders/555002", headers=h)

    async def test_csv_respects_filters(self, api_client):
        ac, h = api_client
        await ac.post("/api/blocked/senders", headers=h, json={"user_id": 556001, "reason": "f1"})
        await ac.post("/api/allowed", headers=h, json={"entity_type": "sender", "entity_id": 556002})
        r = await ac.get("/api/audit/export?action=block.sender", headers=h)
        text = r.content.decode("utf-8-sig")
        assert "block.sender" in text
        assert "allow.add" not in text
        await ac.delete("/api/blocked/senders/556001", headers=h)
        await ac.delete("/api/allowed/sender/556002", headers=h)

    async def test_q_filter_endpoint(self, api_client):
        ac, h = api_client
        await ac.post("/api/blocked/chats", headers=h, json={"chat_id": -1003131313, "reason": "needle-XYZZY"})
        from conftest import drain_dashboard_tasks
        await drain_dashboard_tasks()
        r = await ac.get("/api/audit?q=needle-XYZZY", headers=h)
        items = r.json()["items"]
        assert len(items) == 1 and items[0]["action"] == "block.chat"
        await ac.delete("/api/blocked/chats/-1003131313", headers=h)
