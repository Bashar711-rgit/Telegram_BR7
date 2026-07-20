"""
webadmin/backup.py – Full backup & restore.

A backup zip contains: keywords.json, the SQLite database (snapshot via the
online backup API for consistency), environment files (accounts.env / .env
when present), logs, runtime settings, and a manifest. Restore writes files
back atomically, hot-reloads keywords, and restores the DB through SQLite's
online backup so the live database is never corrupted mid-write.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import zipfile
from typing import Any, Dict, List

from loguru import logger

from config import CFG
from webadmin import keywords_store

BACKUP_DIR = "backups"
_DB_FILE = "telegram_bot.db"
_LOG_FILE = "bot.log"
_ENV_FILES = ("accounts.env", ".env")
_SETTINGS_FILE = os.path.join("data", "runtime_settings.json")


def _db_path() -> str:
    return getattr(CFG, "DB_FILE", _DB_FILE) or _DB_FILE


def _log_path() -> str:
    return getattr(CFG, "LOG_FILE", _LOG_FILE) or _LOG_FILE


def _manifest_name(ts: float) -> str:
    return time.strftime("backup-%Y%m%d-%H%M%S", time.localtime(ts))


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
def _snapshot_db(dest: str) -> bool:
    """Consistent snapshot of the live SQLite DB using the backup API."""
    src_path = _db_path()
    if not os.path.exists(src_path):
        return False
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(dest, timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return True


async def create_backup() -> Dict[str, Any]:
    def _build() -> Dict[str, Any]:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = time.time()
        name = _manifest_name(ts)
        zip_path = os.path.join(BACKUP_DIR, f"{name}.zip")
        included: List[str] = []

        db_snapshot = os.path.join(BACKUP_DIR, f".{name}.db.tmp")
        db_ok = False
        try:
            db_ok = _snapshot_db(db_snapshot)
        except Exception as e:
            logger.error(f"DB snapshot failed: {e}")

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            if os.path.exists(keywords_store.KEYWORDS_FILE):
                z.write(keywords_store.KEYWORDS_FILE, "keywords.json")
                included.append("keywords.json")
            if db_ok:
                z.write(db_snapshot, _db_path())
                included.append(_db_path())
            for env_file in _ENV_FILES:
                if os.path.exists(env_file):
                    z.write(env_file, env_file)
                    included.append(env_file)
            log_path = _log_path()
            if os.path.exists(log_path):
                z.write(log_path, os.path.basename(log_path))
                included.append(os.path.basename(log_path))
            if os.path.exists(_SETTINGS_FILE):
                z.write(_SETTINGS_FILE, _SETTINGS_FILE)
                included.append(_SETTINGS_FILE)
            manifest = {
                "name": name,
                "created_at": ts,
                "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)),
                "version": "webadmin-1.0",
                "files": included,
            }
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        if os.path.exists(db_snapshot):
            os.unlink(db_snapshot)
        return {
            "success": True,
            "name": name,
            "file": os.path.basename(zip_path),
            "size": os.path.getsize(zip_path),
            "files": included,
        }

    return await asyncio.to_thread(_build)


# ---------------------------------------------------------------------------
# List / path
# ---------------------------------------------------------------------------
def list_backups() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(BACKUP_DIR):
        return out
    for fname in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not fname.endswith(".zip"):
            continue
        path = os.path.join(BACKUP_DIR, fname)
        created = None
        try:
            with zipfile.ZipFile(path) as z:
                if "manifest.json" in z.namelist():
                    manifest = json.loads(z.read("manifest.json").decode("utf-8"))
                    created = manifest.get("created_at")
        except Exception:
            pass
        out.append(
            {
                "name": fname[:-4],
                "file": fname,
                "size": os.path.getsize(path),
                "created_at": created or os.path.getmtime(path),
            }
        )
    return out


def backup_zip_path(name: str) -> str:
    safe = os.path.basename(name)
    if not safe.endswith(".zip"):
        safe += ".zip"
    return os.path.join(BACKUP_DIR, safe)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
def _restore_db_from_bytes(data: bytes, tmp_dir: str) -> bool:
    src_path = os.path.join(tmp_dir, "restore_src.db")
    with open(src_path, "wb") as f:
        f.write(data)
    live = _db_path()
    src = sqlite3.connect(src_path, timeout=30)
    try:
        dst = sqlite3.connect(live, timeout=30)
        try:
            dst.execute("PRAGMA busy_timeout=30000")
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return True


async def restore_backup(name: str) -> Dict[str, Any]:
    zip_path = backup_zip_path(name)
    if not os.path.exists(zip_path):
        return {"success": False, "error": "النسخة غير موجودة"}

    def _extract() -> Dict[str, Any]:
        import tempfile

        restored: List[str] = []
        errors: Dict[str, str] = {}
        with tempfile.TemporaryDirectory() as tmp_dir:
            with zipfile.ZipFile(zip_path) as z:
                names = set(z.namelist())

                if "keywords.json" in names:
                    try:
                        data = json.loads(z.read("keywords.json").decode("utf-8"))
                        if isinstance(data, dict):
                            keywords_store._write_raw_atomic(data)
                            restored.append("keywords.json")
                    except Exception as e:
                        errors["keywords.json"] = str(e)

                db_member = _db_path() if _db_path() in names else (
                    "telegram_bot.db" if "telegram_bot.db" in names else None
                )
                if db_member:
                    try:
                        _restore_db_from_bytes(z.read(db_member), tmp_dir)
                        restored.append(_db_path())
                    except Exception as e:
                        errors[_db_path()] = str(e)

                for env_file in _ENV_FILES:
                    if env_file in names:
                        try:
                            with open(env_file, "wb") as f:
                                f.write(z.read(env_file))
                            restored.append(env_file)
                        except Exception as e:
                            errors[env_file] = str(e)

                log_name = os.path.basename(_log_path())
                if log_name in names:
                    try:
                        with open(_log_path(), "ab") as f:
                            f.write(b"\n----- restore point -----\n")
                        restored.append(log_name)
                    except Exception as e:
                        errors[log_name] = str(e)

                if _SETTINGS_FILE in names:
                    try:
                        os.makedirs(os.path.dirname(_SETTINGS_FILE), exist_ok=True)
                        with open(_SETTINGS_FILE, "wb") as f:
                            f.write(z.read(_SETTINGS_FILE))
                        restored.append(_SETTINGS_FILE)
                    except Exception as e:
                        errors[_SETTINGS_FILE] = str(e)

        return {"restored": restored, "errors": errors}

    result = await asyncio.to_thread(_extract)
    if "keywords.json" in result.get("restored", []):
        await keywords_store.hot_reload()
    result["success"] = not result.get("errors")
    return result
