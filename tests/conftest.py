"""Shared pytest fixtures for the Telegram_BR7 test suite.

The project's config.py is imported ONCE by the session; it reads env vars
at import time, so this conftest sets a deterministic, safe environment
BEFORE any project module is imported (pytest loads conftest first).
"""

import os
import sys
from pathlib import Path

import pytest

# The project directory (tests live in <project>/tests).
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# Deterministic, safe test environment — must be set BEFORE config import.
os.environ.setdefault("TARGET_GROUP_ID", "-1001234567890")
os.environ.setdefault("ADMIN_CHAT_ID", "654321")
os.environ.setdefault("DASHBOARD_AUTH_TOKEN", "test-dashboard-token-0123456789")
os.environ.setdefault("DB_TYPE", "sqlite")
# Do NOT let a sandbox-wide DATABASE_URL flip the DB engine mid-suite.
os.environ.pop("DATABASE_URL", None)
os.environ["DB_FILE"] = str(PROJECT_DIR / "tests" / "_test_bot.db")
os.environ["LOG_FILE"] = str(PROJECT_DIR / "tests" / "_test_bot.log")
os.environ["DASHBOARD_ENABLED"] = "false"

_TEST_DB = Path(PROJECT_DIR / "tests" / "_test_bot.db")
if _TEST_DB.exists():
    _TEST_DB.unlink()

from database import EnhancedDatabase  # noqa: E402


@pytest.fixture()
async def db():
    """Fresh EnhancedDatabase (SQLite) per test, without background loops.

    The writer/backup loops are intentionally NOT started: unit tests call
    the write paths directly and must not race a flush loop.
    """
    database = EnhancedDatabase()
    # Prevent start_writer() from spawning loops during connect().
    database._writer_task = None
    _orig_start_writer = database.start_writer

    async def _no_start_writer():
        return None

    database.start_writer = _no_start_writer
    assert await database.connect() is True

    yield database

    try:
        await database.close()
    except Exception:
        pass
    if _TEST_DB.exists():
        try:
            _TEST_DB.unlink()
        except OSError:
            pass
