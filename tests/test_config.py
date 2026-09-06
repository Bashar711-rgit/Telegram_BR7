"""Unit tests for config.py — DATABASE_URL parsing (audit C-2), env helpers."""

import pytest

from config import (
    Config,
    SecretManager,
    fast_hash,
    parse_database_url,
    score_bar,
)


class TestParseDatabaseUrl:
    """Regression tests for audit C-2 (regex parser replaced with urlparse)."""

    def test_full_url(self):
        assert parse_database_url("postgresql://u:p@h:5432/db") == {
            "user": "u", "password": "p", "host": "h", "port": 5432, "database": "db",
        }

    def test_no_port_defaults_5432(self):
        r = parse_database_url("postgresql://u:p@h/db")
        assert r["port"] == 5432 and r["database"] == "db"

    def test_legacy_postgres_scheme(self):
        r = parse_database_url("postgres://u:p@h:5433/db")
        assert r["port"] == 5433 and r["database"] == "db"

    def test_query_params_do_not_leak_into_dbname(self):
        """Render URLs carry ?sslmode=require — must not become the db name."""
        r = parse_database_url("postgresql://u:p@h:5432/db?sslmode=require")
        assert r["database"] == "db"

    def test_percent_encoded_credentials_decoded(self):
        r = parse_database_url("postgresql://u:p%40ss%3Aw@h/db")
        assert r["password"] == "p@ss:w"

    def test_encoded_at_in_password(self):
        r = parse_database_url("postgresql://u:p%40ss@h/db")
        assert r["password"] == "p@ss"

    def test_rejects_non_postgres_scheme(self):
        with pytest.raises(ValueError):
            parse_database_url("mysql://u:p@h/db")

    def test_rejects_empty_database(self):
        with pytest.raises(ValueError):
            parse_database_url("postgresql://u:p@h/")

    def test_rejects_missing_host(self):
        with pytest.raises(ValueError):
            parse_database_url("postgresql:///db")

    def test_secrets_never_logged(self):
        """The parser's error strings must not embed the URL credentials."""
        try:
            parse_database_url("postgresql://u:supersecret@h/db")
        except ValueError as e:
            assert "supersecret" not in str(e)


class TestDatabaseUrlPrecedence:
    """Regression test for audit C-1: DATABASE_URL must win over DB_TYPE=sqlite."""

    def test_valid_postgres_url_overrides_db_type(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgres://u:p@dbhost:5432/mydb?sslmode=require")
        monkeypatch.setenv("DB_TYPE", "sqlite")
        SecretManager.clear_cache()
        Config._instance = None
        cfg = Config.build()
        assert cfg.DB_TYPE == "postgresql"
        assert cfg.DB_HOST == "dbhost"
        assert cfg.DB_NAME == "mydb"
        assert cfg.DB_PASSWORD == "p"  # decoded
        monkeypatch.delenv("DATABASE_URL")

    def test_invalid_url_keeps_db_type(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "file:/tmp/not-postgres.db")
        monkeypatch.setenv("DB_TYPE", "sqlite")
        SecretManager.clear_cache()
        Config._instance = None
        cfg = Config.build()
        assert cfg.DB_TYPE == "sqlite"
        monkeypatch.delenv("DATABASE_URL")


class TestSecretManager:
    def test_get_bool_parses(self, monkeypatch):
        for raw, expected in [("true", True), ("1", True), ("yes", True), ("on", True),
                              ("false", False), ("0", False), ("no", False), ("", False)]:
            monkeypatch.setenv("TST_BOOL", raw)
            SecretManager.clear_cache()
            assert SecretManager.get_bool("TST_BOOL") is expected, raw

    def test_get_int_raises_on_garbage(self, monkeypatch):
        monkeypatch.setenv("TST_INT", "not-a-number")
        SecretManager.clear_cache()
        with pytest.raises(ValueError):
            SecretManager.get_int("TST_INT")

    def test_missing_required_var_message_is_clear(self, monkeypatch):
        monkeypatch.delenv("TST_MISSING_VAR", raising=False)
        SecretManager.clear_cache()
        with pytest.raises(EnvironmentError) as exc:
            SecretManager.get("TST_MISSING_VAR")
        assert "TST_MISSING_VAR" in str(exc.value)


class TestUtils:
    def test_fast_hash_stable(self):
        assert fast_hash("abc") == fast_hash("abc")
        assert fast_hash("abc") != fast_hash("abd")

    def test_score_bar_bounds(self):
        assert score_bar(0).startswith("[")
        assert score_bar(100).endswith("100")
        assert score_bar(150).endswith("150")  # clamped bar, value passthrough
