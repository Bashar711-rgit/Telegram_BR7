"""Tests for v9.22: critical-data export/import (engine-layer), rule edit,
and analytics pipeline counters structure.

The export/import path must be engine-neutral (aiosqlite/asyncpg), tolerant
of invalid rows, and never raise. Rule edit must never touch enabled/hits.
"""

import json

import pytest


# ---------------------------------------------------------------------------
# DB layer: export / import
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestCriticalDataExportImport:
    async def test_export_contains_core_tables(self, db):
        await db.add_source(-100777, title="qa-src")
        await db.add_rule("qa-rule", {"keyword": "qa"}, action="block")
        await db.add_allowed_entity("sender", 4242)
        payload = await db.export_critical_data()
        assert payload.get("version") == 1
        tables = payload["tables"]
        for name in ("app_settings", "sources", "rules", "allowed_entities"):
            assert name in tables
        assert any(r["chat_id"] == -100777 for r in tables["sources"])
        assert any(r["name"] == "qa-rule" for r in tables["rules"])
        assert any(r["entity_id"] == 4242 for r in tables["allowed_entities"])

    async def test_roundtrip_restore(self, db):
        await db.add_source(-100555, title="keep-me")
        payload = await db.export_critical_data()
        # wipe + re-import
        await db.remove_source(-100555)
        assert await db.get_source(-100555) is None
        counts = await db.import_critical_data(payload, replace=True)
        assert counts.get("sources", 0) >= 1
        row = await db.get_source(-100555)
        assert row is not None and row["title"] == "keep-me"

    async def test_import_tolerates_invalid_rows(self, db):
        payload = {"version": 1, "tables": {
            "sources": [
                {"chat_id": -1001, "title": "ok"},
                "not-a-dict",
                {"title": "missing chat_id"},
                {"chat_id": -1002, "title": "ok2"},
            ],
            "rules": [{"name": "bad", "conditions": {"keyword": "x"}, "action": "block"}],
        }}
        counts = await db.import_critical_data(payload, replace=True)
        assert counts.get("sources") == 2
        assert counts.get("rules") == 1
        assert await db.get_source(-1001) is not None
        assert await db.get_source(-1002) is not None

    async def test_import_conditions_json_encoded(self, db):
        """Conditions arriving as dicts (not strings) are JSON-encoded."""
        payload = {"tables": {"rules": [
            {"name": "c", "conditions": {"keyword": "k"}, "action": "tag",
             "action_value": "t", "priority": 3, "hits": 0, "enabled": 1,
             "created_by": "qa"},
        ]}}
        counts = await db.import_critical_data(payload, replace=True)
        assert counts.get("rules") == 1
        rules = await db.list_rules()
        assert rules and rules[0]["conditions"] == {"keyword": "k"}

    async def test_import_never_raises_on_garbage(self, db):
        counts = await db.import_critical_data({"tables": "garbage"})
        assert counts == {}
        counts = await db.import_critical_data(None)
        assert counts == {}
        counts = await db.import_critical_data({"tables": {"unknown_table": [{"a": 1}]}})
        assert counts == {}

    async def test_import_invalidates_caches(self, db):
        # warm the sources cache with a value
        assert await db.is_source_allowed(-1) is True  # empty table
        payload = {"tables": {"sources": [{"chat_id": -9, "title": "x"}]}}
        await db.import_critical_data(payload, replace=True)
        # cache invalidated → -9 now allowed, -1 not
        assert await db.is_source_allowed(-9) is True
        assert await db.is_source_allowed(-1) is False


# ---------------------------------------------------------------------------
# API: /api/backup/export + /api/backup/import
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
            yield ac, headers


@pytest.mark.asyncio
class TestBackupAPI:
    async def test_requires_auth(self, api_client):
        ac, _ = api_client
        assert (await ac.get("/api/backup/export")).status_code == 401
        assert (await ac.post("/api/backup/import", json={})).status_code == 401

    async def test_export_headers_and_body(self, api_client):
        ac, h = api_client
        r = await ac.get("/api/backup/export", headers=h)
        assert r.status_code == 200
        assert "attachment" in r.headers.get("content-disposition", "")
        body = r.json()
        assert "tables" in body and "app_settings" in body["tables"]

    async def test_import_rejects_bad_format(self, api_client):
        ac, h = api_client
        r = await ac.post("/api/backup/import", headers=h, json={"nope": 1})
        assert r.status_code == 400
        r = await ac.post("/api/backup/import", headers=h,
                          content=b"not-json",
                          headers2=None) if False else await ac.post(
            "/api/backup/import", headers={**h, "content-type": "application/json"},
            content=b"not-json")
        assert r.status_code == 400

    async def test_import_creates_safety_backup(self, api_client, tmp_path, monkeypatch):
        ac, h = api_client
        import dashboard as dash
        monkeypatch.chdir(tmp_path)  # backups/ lands in tmp
        # seed something so the export actually carries rows
        r = await ac.post("/api/sources", headers=h, json={"chat_id": -1004242, "title": "seed"})
        assert r.status_code == 200
        payload = (await ac.get("/api/backup/export", headers=h)).json()
        r = await ac.post("/api/backup/import", headers=h, json=payload)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["safety_backup"].startswith("pre-import-")
        backups = list((tmp_path / "backups").glob("pre-import-*.json"))
        assert backups, "safety backup file must exist"

    async def test_import_nothing_imported_is_400(self, api_client, monkeypatch, tmp_path):
        ac, h = api_client
        monkeypatch.chdir(tmp_path)
        r = await ac.post("/api/backup/import", headers=h,
                          json={"tables": {"sources": []}})
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# API: rule edit (v9.22-2)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestRuleEdit:
    async def test_edit_roundtrip_and_audit(self, api_client):
        ac, h = api_client
        r = await ac.post("/api/rules", headers=h, json={
            "name": "before", "conditions": {"keyword": "old"}, "action": "block"})
        rid = r.json()["rule"]["id"]
        r = await ac.post(f"/api/rules/{rid}/edit", headers=h, json={
            "name": "after", "conditions": {"keyword": "new"}, "action": "tag",
            "action_value": "TAG", "priority": 7})
        assert r.status_code == 200
        rule = r.json()["rule"]
        assert rule["name"] == "after" and rule["conditions"] == {"keyword": "new"}
        assert rule["action"] == "tag" and rule["priority"] == 7
        # enabled/hits untouched
        assert rule["enabled"] == 1 and rule["hits"] == 0
        r = await ac.get("/api/audit?limit=50", headers=h)
        actions = [a["action"] for a in r.json()["items"]]
        assert "rule.edit" in actions
        # before/after recorded as JSON strings
        row = next(a for a in r.json()["items"] if a["action"] == "rule.edit")
        old = json.loads(row["old_value"])
        assert old["name"] == "before"

    async def test_edit_validation_rejects(self, api_client):
        ac, h = api_client
        r = await ac.post("/api/rules", headers=h, json={
            "conditions": {"keyword": "k"}, "action": "block"})
        rid = r.json()["rule"]["id"]
        r = await ac.post(f"/api/rules/{rid}/edit", headers=h, json={
            "conditions": {"bogus": 1}, "action": "block"})
        assert r.status_code == 400
        r = await ac.post(f"/api/rules/999999/edit", headers=h, json={
            "conditions": {"keyword": "k"}, "action": "block"})
        assert r.status_code == 404

    async def test_edit_tag_requires_value(self, api_client):
        ac, h = api_client
        r = await ac.post("/api/rules", headers=h, json={
            "conditions": {"keyword": "k"}, "action": "block"})
        rid = r.json()["rule"]["id"]
        r = await ac.post(f"/api/rules/{rid}/edit", headers=h, json={
            "conditions": {"keyword": "k"}, "action": "tag", "action_value": ""})
        assert r.status_code == 400
