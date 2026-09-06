"""Tests for dashboard_store.py (v3.1) — direct-to-DB persistence.

Covers: app_settings table roundtrip, settings validation/coercion,
live-apply to the frozen CFG dataclass, boot restore semantics, and the
/api/settings + /api/keywords direct-save contract.
"""

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import dashboard as dashboard_module
import dashboard_store
from dashboard import app

TOKEN = "test-dashboard-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
async def db():
    """Fresh EnhancedDatabase (SQLite) without background loops."""
    database = dashboard_module.EnhancedDatabase()
    database._writer_task = None

    async def _no_start_writer():
        return None

    database.start_writer = _no_start_writer
    assert await database.connect() is True
    await dashboard_store.ensure_table(database)
    yield database
    await database.close()


@pytest.fixture()
async def clean_store(db):
    """DB with an empty app_settings table for hermetic store tests."""
    await db._execute("DELETE FROM app_settings")
    yield db
    await db._execute("DELETE FROM app_settings")


class FakeState:
    def __init__(self, database):
        self.db = database
        self.bot_ref = None


class FakeApp:
    def __init__(self, database):
        self.state = FakeState(database)


# ---------------------------------------------------------------------------
# Low-level store
# ---------------------------------------------------------------------------
class TestStoreRoundtrip:
    @pytest.mark.asyncio
    async def test_set_get_delete(self, clean_store):
        await dashboard_store.set_many(clean_store, {"alert_cooldown": "300", "prefilter_enabled": "true"})
        data = await dashboard_store.get_all(clean_store)
        assert data["alert_cooldown"] == "300"
        assert data["prefilter_enabled"] == "true"

        await dashboard_store.set_many(clean_store, {"alert_cooldown": "600"})
        data = await dashboard_store.get_all(clean_store)
        assert data["alert_cooldown"] == "600"  # upsert, not duplicate

        await dashboard_store.delete_key(clean_store, "alert_cooldown")
        data = await dashboard_store.get_all(clean_store)
        assert "alert_cooldown" not in data

    @pytest.mark.asyncio
    async def test_ensure_table_idempotent(self, clean_store):
        await dashboard_store.ensure_table(clean_store)
        await dashboard_store.ensure_table(clean_store)  # must not raise
        assert await dashboard_store.get_all(clean_store) == {}

    @pytest.mark.asyncio
    async def test_persist_and_load_keywords(self, clean_store):
        sample = {"request_phrases": {"direct_requests": ["ابغى حل", "مطلوب باحث"]}, "ad_blockers": ["x.com"]}
        assert await dashboard_store.persist_keywords(clean_store, sample) is True
        loaded = await dashboard_store.load_keywords(clean_store)
        assert loaded == sample

    @pytest.mark.asyncio
    async def test_load_keywords_missing(self, clean_store):
        assert await dashboard_store.load_keywords(clean_store) is None


# ---------------------------------------------------------------------------
# Validation & coercion
# ---------------------------------------------------------------------------
class TestValidation:
    def test_string_coerced_to_int(self):
        clean, errors = dashboard_store.validate_updates({"alert_cooldown": "450"})
        assert errors == {}
        assert clean["alert_cooldown"] == 450

    def test_bool_coercion_variants(self):
        for raw, expected in [("true", True), ("0", False), (True, True), (1, True)]:
            clean, errors = dashboard_store.validate_updates({"prefilter_enabled": raw})
            assert errors == {}
            assert clean["prefilter_enabled"] is expected

    def test_reject_bool_for_int(self):
        clean, errors = dashboard_store.validate_updates({"alert_cooldown": True})
        assert "alert_cooldown" in errors
        assert "alert_cooldown" not in clean

    def test_out_of_range(self):
        clean, errors = dashboard_store.validate_updates({"min_message_length": 99999})
        assert "min_message_length" in errors

    def test_unknown_key(self):
        clean, errors = dashboard_store.validate_updates({"hacker_key": 1})
        assert "hacker_key" in errors
        assert clean == {}

    def test_zero_target_group_rejected(self):
        clean, errors = dashboard_store.validate_updates({"target_group_id": 0})
        assert "target_group_id" in errors

    def test_negative_target_group_allowed(self):
        clean, errors = dashboard_store.validate_updates({"target_group_id": "-1001234567890"})
        assert errors == {}
        assert clean["target_group_id"] == -1001234567890

    def test_review_threshold_must_be_below_accept(self):
        clean, errors = dashboard_store.validate_updates(
            {"confidence_accept_threshold": 0.4, "confidence_review_threshold": 0.5}
        )
        assert "confidence_review_threshold" in errors
        assert "confidence_review_threshold" not in clean
        assert clean["confidence_accept_threshold"] == 0.4  # valid part kept

    def test_float_coercion(self):
        clean, errors = dashboard_store.validate_updates({"confidence_accept_threshold": "0.75"})
        assert errors == {}
        assert clean["confidence_accept_threshold"] == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Live application
# ---------------------------------------------------------------------------
class TestLiveApply:
    @pytest.mark.asyncio
    async def test_cfg_live_apply_and_restore(self, clean_store):
        from config import CFG

        original = CFG.ALERT_COOLDOWN
        try:
            assert dashboard_store.apply_setting(FakeApp(clean_store), "alert_cooldown", 777) == "applied"
            assert CFG.ALERT_COOLDOWN == 777
        finally:
            object.__setattr__(CFG, "ALERT_COOLDOWN", original)
        assert CFG.ALERT_COOLDOWN == original

    @pytest.mark.asyncio
    async def test_rate_limiter_apply_with_bot(self, clean_store):
        class FakeLimiter:
            _max_min = 10
            _max_hr = 100

        class FakeBot:
            rate_limiter = FakeLimiter()

        class FakeBotState:
            bot_ref = FakeBot()

        class FakeBotApp:
            state = FakeBotState()

        status = dashboard_store.apply_setting(FakeBotApp(), "max_alerts_per_minute", 42)
        assert status == "applied"
        assert FakeBot.rate_limiter._max_min == 42

    @pytest.mark.asyncio
    async def test_rate_limiter_stored_without_bot(self, clean_store):
        status = dashboard_store.apply_setting(FakeApp(clean_store), "max_alerts_per_minute", 42)
        assert status == "stored"  # no bot → persisted but not applied


# ---------------------------------------------------------------------------
# Boot restore
# ---------------------------------------------------------------------------
class TestRestoreAll:
    @pytest.mark.asyncio
    async def test_restore_applies_settings(self, clean_store):
        from config import CFG

        await dashboard_store.set_many(clean_store, {"alert_cooldown": json.dumps(555)})
        original = CFG.ALERT_COOLDOWN
        try:
            result = await dashboard_store.restore_all(FakeApp(clean_store))
            assert "alert_cooldown" in result["settings_applied"]
            assert CFG.ALERT_COOLDOWN == 555
        finally:
            object.__setattr__(CFG, "ALERT_COOLDOWN", original)

    @pytest.mark.asyncio
    async def test_restore_restores_keywords_file(self, clean_store, tmp_path, monkeypatch):
        current = await dashboard_module._read_keywords_file()
        assert current is not None  # repo file must be valid JSON for this test

        db_copy = json.loads(json.dumps(current, ensure_ascii=False))
        db_copy["ad_blockers"].append("__test_marker_keyword__")
        await dashboard_store.persist_keywords(clean_store, db_copy)

        target = tmp_path / "keywords.json"
        target.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(dashboard_module, "KEYWORDS_FILE", str(target))

        result = await dashboard_store.restore_all(FakeApp(clean_store))
        assert result["keywords_restored"] is True
        restored = json.loads(target.read_text(encoding="utf-8"))
        assert "__test_marker_keyword__" in restored["ad_blockers"]

    @pytest.mark.asyncio
    async def test_restore_skips_identical_keywords(self, clean_store, tmp_path, monkeypatch):
        current = await dashboard_module._read_keywords_file()
        await dashboard_store.persist_keywords(clean_store, current)
        target = tmp_path / "keywords.json"
        target.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(dashboard_module, "KEYWORDS_FILE", str(target))

        result = await dashboard_store.restore_all(FakeApp(clean_store))
        assert result["keywords_skipped_same"] is True
        assert result["keywords_restored"] is False

    @pytest.mark.asyncio
    async def test_restore_without_db_is_safe(self):
        class EmptyState:
            db = None
            bot_ref = None

        class EmptyApp:
            state = EmptyState()

        result = await dashboard_store.restore_all(EmptyApp())
        assert result["settings_applied"] == []
        assert result["keywords_restored"] is False


# ---------------------------------------------------------------------------
# API contract
# ---------------------------------------------------------------------------
@pytest.fixture()
async def client():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.fixture()
async def clean_app_settings(client):
    db = dashboard_module.app.state.db
    await db._execute("DELETE FROM app_settings")
    yield db
    await db._execute("DELETE FROM app_settings")


class TestSettingsAPI:
    @pytest.mark.asyncio
    async def test_get_settings_requires_token(self, client):
        r = await client.get("/api/settings")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_get_settings_shape(self, client, clean_app_settings):
        r = await client.get("/api/settings", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert "alert_cooldown" in body["settings"]
        assert body["schema"]["alert_cooldown"]["type"] == "int"
        assert body["schema"]["alert_cooldown"]["label"]
        assert body["persistence"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_post_settings_direct_save(self, client, clean_app_settings):
        from config import CFG

        original = CFG.ALERT_COOLDOWN
        try:
            r = await client.post(
                "/api/settings",
                headers=AUTH,
                json={"updates": {"alert_cooldown": 921}},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True
            assert body["persisted_to_db"] is True
            assert body["applied_live"]["alert_cooldown"] == 921
            assert CFG.ALERT_COOLDOWN == 921  # live-applied

            # direct save verified in the table
            stored = await dashboard_store.get_all(clean_app_settings)
            assert json.loads(stored["alert_cooldown"]) == 921

            # and re-read shows the new effective value
            r2 = await client.get("/api/settings", headers=AUTH)
            assert r2.json()["settings"]["alert_cooldown"] == 921
        finally:
            object.__setattr__(CFG, "ALERT_COOLDOWN", original)

    @pytest.mark.asyncio
    async def test_post_settings_legacy_flat_body(self, client, clean_app_settings):
        from config import CFG

        original = CFG.MIN_MESSAGE_LENGTH
        try:
            r = await client.post(
                "/api/settings",
                headers=AUTH,
                json={"min_message_length": 25},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["applied_live"]["min_message_length"] == 25
            assert CFG.MIN_MESSAGE_LENGTH == 25
        finally:
            object.__setattr__(CFG, "MIN_MESSAGE_LENGTH", original)

    @pytest.mark.asyncio
    async def test_post_settings_validation_errors(self, client, clean_app_settings):
        r = await client.post(
            "/api/settings",
            headers=AUTH,
            json={"updates": {"min_message_length": 999999, "unknown_key": 1}},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False
        assert "min_message_length" in body["errors"]
        assert "unknown_key" in body["errors"]

    @pytest.mark.asyncio
    async def test_post_settings_empty_body_rejected(self, client, clean_app_settings):
        r = await client.post("/api/settings", headers=AUTH, json={})
        assert r.status_code == 400


class TestKeywordsPersistenceAPI:
    @pytest.mark.asyncio
    async def test_add_keyword_persists_to_db(self, client, clean_app_settings, tmp_path, monkeypatch):
        # Redirect the keywords file to a temp copy so the repo file is
        # never mutated by the test run.
        current = await dashboard_module._read_keywords_file()
        target = tmp_path / "keywords.json"
        target.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(dashboard_module, "KEYWORDS_FILE", str(target))

        r = await client.post(
            "/api/keywords",
            headers=AUTH,
            json={"category": "ad_blockers", "keyword": "__api_marker__"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["persisted_to_db"] is True

        # file got the word…
        file_data = json.loads(target.read_text(encoding="utf-8"))
        assert "__api_marker__" in file_data["ad_blockers"]
        # …and the DB mirror holds the same content
        mirrored = await dashboard_store.load_keywords(clean_app_settings)
        assert "__api_marker__" in mirrored["ad_blockers"]
