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
