"""
webadmin/settings_store.py – Runtime bot configuration.

Persists overrides to data/runtime_settings.json and applies the values
that map to CFG fields live (frozen dataclass is bypassed safely via
object.__setattr__ since CFG fields are re-read at call time). Overrides
without a CFG consumer are persisted and reported as stored-only.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict

from loguru import logger

from config import CFG

SETTINGS_FILE = os.path.join("data", "runtime_settings.json")
_lock = asyncio.Lock()

# key -> (CFG field or None, type, min, max, live-applied)
_SCHEMA: Dict[str, Dict[str, Any]] = {
    "min_message_length":  {"cfg": "MIN_MESSAGE_LENGTH",  "type": int,   "min": 1,   "max": 10000,    "live": True},
    "max_message_length":  {"cfg": "MAX_MESSAGE_LENGTH",  "type": int,   "min": 10,  "max": 50000,    "live": True},
    "max_words_count":     {"cfg": "MAX_WORDS_COUNT",     "type": int,   "min": 1,   "max": 1000,     "live": True},
    "cooldown_per_sender": {"cfg": "ALERT_COOLDOWN",      "type": int,   "min": 0,   "max": 86400,    "live": True},
    "max_lines":           {"cfg": None,                  "type": int,   "min": 1,   "max": 500,      "live": False},
    "max_image_size_mb":   {"cfg": None,                  "type": float, "min": 0.1, "max": 100.0,    "live": False},
    "max_video_size_mb":   {"cfg": None,                  "type": float, "min": 0.1, "max": 2000.0,   "live": False},
}


def _defaults() -> Dict[str, Any]:
    return {
        "min_message_length": CFG.MIN_MESSAGE_LENGTH,
        "max_message_length": CFG.MAX_MESSAGE_LENGTH,
        "max_words_count": CFG.MAX_WORDS_COUNT,
        "cooldown_per_sender": CFG.ALERT_COOLDOWN,
        "max_lines": 50,
        "max_image_size_mb": 10.0,
        "max_video_size_mb": 50.0,
    }


def _read_file() -> Dict[str, Any]:
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_file(data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SETTINGS_FILE)


def get_settings() -> Dict[str, Any]:
    merged = _defaults()
    merged.update({k: v for k, v in _read_file().items() if k in _SCHEMA})
    return merged


def get_schema() -> Dict[str, Any]:
    return {k: {"type": v["type"].__name__, "min": v["min"], "max": v["max"], "live": v["live"]}
            for k, v in _SCHEMA.items()}


def _apply_live(key: str, value: Any) -> bool:
    field = _SCHEMA[key]["cfg"]
    if not field:
        return False
    try:
        object.__setattr__(CFG, field, value)  # frozen dataclass live override
        return True
    except Exception as e:
        logger.error(f"Failed to apply {field} live: {e}")
        return False


async def update_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    applied, stored, errors = {}, {}, {}
    clean: Dict[str, Any] = {}
    for key, raw in (updates or {}).items():
        spec = _SCHEMA.get(key)
        if spec is None:
            errors[key] = "مفتاح غير معروف"
            continue
        try:
            value = spec["type"](raw)
        except (TypeError, ValueError):
            errors[key] = "قيمة غير صالحة"
            continue
        if not (spec["min"] <= value <= spec["max"]):
            errors[key] = f"القيمة خارج النطاق ({spec['min']} - {spec['max']})"
            continue
        clean[key] = value

    if clean:
        async with _lock:
            data = _read_file()
            data.update(clean)
            _write_file(data)
        for key, value in clean.items():
            if _apply_live(key, value):
                applied[key] = value
            else:
                stored[key] = value
        logger.info(f"Runtime settings updated: applied={list(applied)} stored={list(stored)}")

    return {
        "success": not errors,
        "applied_live": applied,
        "stored_only": stored,
        "errors": errors,
        "settings": get_settings(),
    }


def apply_persisted_on_startup() -> None:
    """Re-apply persisted overrides at boot (called once by mount_admin)."""
    for key, value in _read_file().items():
        if key in _SCHEMA:
            _apply_live(key, value)
