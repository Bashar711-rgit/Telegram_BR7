"""
dashboard_store.py – Direct-to-database persistence for the dashboard (v3.1).

Problem this solves
-------------------
On Render the filesystem is EPHEMERAL: every deploy/restart wipes
keywords.json edits and any runtime tweaks. The old dashboard wrote keyword
edits to a file (lost on restart) and its Settings tab was cosmetic
(hardcoded values, frozen-dataclass rejections).

Design
------
A single key-value table `app_settings` in the SAME database the bot already
uses (PostgreSQL on Render, SQLite in local dev) stores:

  * `keywords_json`        → full keywords.json mirror (restored on boot)
  * one row per setting    → validated overrides applied LIVE via
                             object.__setattr__ on the frozen CFG dataclass
                             (CFG fields are read at call time by
                             monitors.py / filter_engine.py, so overrides
                             take effect immediately, no restart).

Every dashboard mutation writes through to the DB first ("direct save"),
then applies live. On boot the dashboard lifespan calls restore_all() which
re-applies every persisted override, so user edits survive redeploys.

The `webadmin/` file-based stores remain untouched (local-dev convenience);
this module is the Render-safe source of truth.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from loguru import logger

KEYWORDS_KEY = "keywords_json"
_TABLE = "app_settings"

# ---------------------------------------------------------------------------
# Editable settings schema
# ---------------------------------------------------------------------------
# key -> spec
#   label     Arabic label shown in the dashboard
#   type      int | float | bool
#   min/max   inclusive bounds (after coercion)
#   cfg       CFG field applied live (or None)
#   limiter   True → applied to bot.rate_limiter (max_min/_max_hr)
#   group     UI group in the settings tab
#   note      optional operator-facing note (Arabic)
#   validate  optional extra predicate (value) -> error-string or None
SETTINGS_SCHEMA: Dict[str, Dict[str, Any]] = {
    # ── Destinations ──────────────────────────────────────────────────────
    "target_group_id": {
        "label": "مجموعة/قناة التنبيهات (ID)", "type": int,
        "min": -9999999999999, "max": 9999999999999,
        "cfg": "TARGET_GROUP_ID", "limiter": False, "group": "destinations",
        "note": "هنا تصل التنبيهات. يُطبَّق فوراً على الرسائل القادمة.",
        "validate": lambda v: None if v != 0 else "المعرّف لا يمكن أن يكون صفراً",
    },
    "admin_chat_id": {
        "label": "محادثة المشرف (ID)", "type": int,
        "min": -9999999999999, "max": 9999999999999,
        "cfg": "ADMIN_CHAT_ID", "limiter": False, "group": "destinations",
        "note": "إشعارات المشرف تُطبَّق فوراً؛ أوامر البوت /stats تُربط عند الإقلاع فتتطلب إعادة تشغيل.",
        "validate": lambda v: None if v != 0 else "المعرّف لا يمكن أن يكون صفراً",
    },
    # ── Alerts ────────────────────────────────────────────────────────────
    "max_alerts_per_minute": {
        "label": "حد التنبيهات / دقيقة", "type": int, "min": 1, "max": 500,
        "cfg": None, "limiter": True, "group": "alerts",
    },
    "max_alerts_per_hour": {
        "label": "حد التنبيهات / ساعة", "type": int, "min": 1, "max": 5000,
        "cfg": None, "limiter": True, "group": "alerts",
    },
    "alert_cooldown": {
        "label": "فترة التهدئة (ثانية)", "type": int, "min": 0, "max": 86400,
        "cfg": "ALERT_COOLDOWN", "limiter": False, "group": "alerts",
    },
    # ── Filtering ─────────────────────────────────────────────────────────
    "min_message_length": {
        "label": "أقصر رسالة تُفحص (حرف)", "type": int, "min": 1, "max": 10000,
        "cfg": "MIN_MESSAGE_LENGTH", "limiter": False, "group": "filtering",
    },
    "max_message_length": {
        "label": "أطول رسالة تُفحص (حرف)", "type": int, "min": 10, "max": 50000,
        "cfg": "MAX_MESSAGE_LENGTH", "limiter": False, "group": "filtering",
    },
    "prefilter_enabled": {
        "label": "الفحص المبدئي (Prefilter)", "type": bool,
        "min": None, "max": None,
        "cfg": "PREFILTER_ENABLED", "limiter": False, "group": "filtering",
    },
    "language_filter": {
        "label": "فلترة اللغة (العربية فقط)", "type": bool,
        "min": None, "max": None,
        "cfg": "LANGUAGE_FILTER", "limiter": False, "group": "filtering",
    },
    "confidence_accept_threshold": {
        "label": "حد قبول النية (0-1)", "type": float, "min": 0.0, "max": 1.0,
        "cfg": "CONFIDENCE_ACCEPT_THRESHOLD", "limiter": False, "group": "filtering",
        "note": "أعلى = أقل تنبيهات وأكثر دقة.",
    },
    "confidence_review_threshold": {
        "label": "حد المراجعة (0-1)", "type": float, "min": 0.0, "max": 1.0,
        "cfg": "CONFIDENCE_REVIEW_THRESHOLD", "limiter": False, "group": "filtering",
        "note": "يجب أن يكون أقل من حد القبول.",
        "validate": None,  # cross-field check happens in validate_updates
    },
}

_UI_GROUPS = {
    "destinations": "الوجهات",
    "alerts": "التنبيهات",
    "filtering": "الفلترة",
}


# ---------------------------------------------------------------------------
# Low-level DB access (dialect-neutral: SQLite `?` + asyncpg via _pg())
# ---------------------------------------------------------------------------
async def ensure_table(db: Any) -> None:
    await db._execute(
        f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
        "key TEXT PRIMARY KEY, "
        "value TEXT NOT NULL, "
        "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )


async def get_all(db: Any) -> Dict[str, str]:
    rows = await db._fetchall(f"SELECT key, value FROM {_TABLE}")
    return {r["key"]: r["value"] for r in rows}


async def set_many(db: Any, values: Dict[str, str]) -> None:
    """Upsert — ON CONFLICT works on both SQLite (3.24+) and PostgreSQL."""
    for key, value in values.items():
        await db._execute(
            f"INSERT INTO {_TABLE} (key, value, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, updated_at = CURRENT_TIMESTAMP",
            (str(key), str(value)),
        )


async def delete_key(db: Any, key: str) -> None:
    await db._execute(f"DELETE FROM {_TABLE} WHERE key = ?", (key,))


# ---------------------------------------------------------------------------
# Validation + coercion
# ---------------------------------------------------------------------------
def _coerce(spec: Dict[str, Any], raw: Any) -> Any:
    """Coerce raw JSON value (or string from a form) to the schema type."""
    t = spec["type"]
    if t is bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        s = str(raw).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        raise ValueError("قيمة منطقية غير صالحة")
    if isinstance(raw, bool):
        # reject bools where a number is required (True == 1 in Python)
        raise ValueError("قيمة رقمية مطلوبة")
    if t is int:
        if isinstance(raw, str):
            raw = raw.strip()
        value = int(raw)  # raises ValueError on garbage
        if float(value) != float(raw):
            raise ValueError("يجب أن يكون رقماً صحيحاً")
        return value
    return float(raw)  # float


def validate_updates(updates: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Validate against SETTINGS_SCHEMA → (clean, errors)."""
    clean: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for key, raw in (updates or {}).items():
        spec = SETTINGS_SCHEMA.get(key)
        if spec is None:
            errors[key] = "إعداد غير معروف"
            continue
        try:
            value = _coerce(spec, raw)
        except (TypeError, ValueError):
            errors[key] = "قيمة غير صالحة للنوع المطلوب"
            continue
        if spec["min"] is not None and not (spec["min"] <= value <= spec["max"]):
            errors[key] = f"القيمة خارج النطاق ({spec['min']} - {spec['max']})"
            continue
        validator = spec.get("validate")
        if validator:
            err = validator(value)
            if err:
                errors[key] = err
                continue
        clean[key] = value

    # cross-field rule: review threshold must stay below accept threshold
    accept = clean.get("confidence_accept_threshold")
    review = clean.get("confidence_review_threshold")
    if accept is not None and review is not None and review >= accept:
        errors["confidence_review_threshold"] = "يجب أن يكون أقل من حد القبول"
        clean.pop("confidence_review_threshold", None)
    return clean, errors


# ---------------------------------------------------------------------------
# Live application
# ---------------------------------------------------------------------------
def _apply_cfg(field: str, value: Any) -> bool:
    try:
        from config import CFG

        object.__setattr__(CFG, field, value)  # frozen dataclass live override
        return True
    except Exception as e:
        logger.error(f"dashboard_store: failed to apply CFG.{field}: {e}")
        return False


def apply_setting(app: Any, key: str, value: Any) -> str:
    """Apply one validated setting live. Returns 'applied' or 'stored'."""
    spec = SETTINGS_SCHEMA[key]
    applied = False
    if spec["limiter"]:
        bot = getattr(app.state, "bot_ref", None)
        limiter = getattr(bot, "rate_limiter", None) if bot else None
        if limiter is not None:
            try:
                if key == "max_alerts_per_minute":
                    limiter._max_min = int(value)
                else:
                    limiter._max_hr = int(value)
                applied = True
            except Exception as e:
                logger.error(f"rate limiter apply failed for {key}: {e}")
    elif spec["cfg"] is not None:
        applied = _apply_cfg(spec["cfg"], value)
    return "applied" if applied else "stored"


def current_values(app: Any) -> Dict[str, Any]:
    """Effective values for display (CFG/rate limiter, not raw DB)."""
    from config import CFG

    bot = getattr(app.state, "bot_ref", None)
    limiter = getattr(bot, "rate_limiter", None) if bot else None
    out: Dict[str, Any] = {}
    for key, spec in SETTINGS_SCHEMA.items():
        if spec["limiter"]:
            if limiter is not None:
                out[key] = getattr(
                    limiter,
                    "_max_min" if key == "max_alerts_per_minute" else "_max_hr",
                    None,
                )
            else:
                out[key] = getattr(CFG, key.upper(), None)
        else:
            out[key] = getattr(CFG, spec["cfg"], None)
    return out


def schema_for_ui() -> Dict[str, Any]:
    return {
        key: {
            "label": spec["label"],
            "type": spec["type"].__name__,
            "min": spec["min"],
            "max": spec["max"],
            "group": spec["group"],
            "group_label": _UI_GROUPS[spec["group"]],
            "note": spec.get("note"),
        }
        for key, spec in SETTINGS_SCHEMA.items()
    }


# ---------------------------------------------------------------------------
# Keywords mirror
# ---------------------------------------------------------------------------
def _read_keywords_file() -> Optional[Dict[str, Any]]:
    try:
        from dashboard import KEYWORDS_FILE

        with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _kw_version(data: Optional[Dict[str, Any]]):
    """Semantic-ish version tuple from _meta.version ("15.3.0" -> (15, 3, 0))."""
    try:
        raw = str(((data or {}).get("_meta") or {}).get("version", "0"))
        parts = tuple(int(p) for p in raw.split(".") if p.isdigit())
        return parts or (0,)
    except Exception:
        return (0,)


async def persist_keywords(db: Any, data: Dict[str, Any]) -> bool:
    """Mirror the full keywords.json content to the DB (direct save)."""
    try:
        await set_many(db, {KEYWORDS_KEY: json.dumps(data, ensure_ascii=False)})
        return True
    except Exception as e:
        logger.error(f"dashboard_store: keywords persist failed: {e}")
        return False


async def load_keywords(db: Any) -> Optional[Dict[str, Any]]:
    raw = (await get_all(db)).get(KEYWORDS_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        logger.error("dashboard_store: keywords_json in DB is corrupt — ignored")
        return None


# ---------------------------------------------------------------------------
# Boot restore — called once by the dashboard lifespan (both modes)
# ---------------------------------------------------------------------------
async def restore_all(app: Any) -> Dict[str, Any]:
    """Re-apply every persisted override after (re)deploy.

    Runs in dashboard-only mode too (app.state.db owned by the dashboard).
    Never raises — failures are logged and reported.
    """
    db = getattr(app.state, "db", None)
    result = {"settings_applied": [], "settings_stored": [], "keywords_restored": False,
              "keywords_skipped_same": False}
    if db is None:
        logger.warning("dashboard_store.restore_all: database not ready — skipped")
        return result
    try:
        await ensure_table(db)
    except Exception as e:
        logger.error(f"dashboard_store: ensure_table failed: {e}")
        return result

    stored = await get_all(db)

    # 1) settings
    for key, raw in stored.items():
        if key not in SETTINGS_SCHEMA:
            continue
        spec = SETTINGS_SCHEMA[key]
        try:
            value = _coerce(spec, json.loads(raw))
        except (ValueError, TypeError, json.JSONDecodeError):
            logger.warning(f"dashboard_store: ignoring corrupt value for {key}")
            continue
        status = apply_setting(app, key, value)
        (result["settings_applied"] if status == "applied" else result["settings_stored"]).append(key)

    # 2) keywords — v15.3: version-gated restore. A NEWER shipped
    # keywords.json (code deploy) wins once and updates the DB mirror;
    # otherwise the dashboard's DB copy (user's live edits) still wins.
    kw = await load_keywords(db)
    if kw is not None:
        current = _read_keywords_file()
        if current == kw:
            result["keywords_skipped_same"] = True
        elif _kw_version(current) > _kw_version(kw):
            try:
                from dashboard import KEYWORDS_FILE

                await persist_keywords(db, current)
                result["keywords_shipped_newer"] = True
                logger.info(
                    "dashboard_store: shipped keywords.json ({}) is newer than DB copy ({}) — DB mirror refreshed from file",
                    _kw_version(current), _kw_version(kw),
                )
            except Exception as e:
                logger.error(f"dashboard_store: keywords mirror refresh failed: {e}")
        else:
            try:
                from dashboard import KEYWORDS_FILE, _reload_filter_keywords, _write_keywords_file

                await _write_keywords_file(kw, KEYWORDS_FILE)
                result["keywords_restored"] = True
                logger.info("dashboard_store: keywords.json restored from database")
                bot = getattr(app.state, "bot_ref", None)
                if bot is not None and getattr(bot, "filter", None) is not None:
                    reload = await _reload_filter_keywords(app)
                    if not reload.get("applied"):
                        logger.warning(f"post-restore filter reload failed: {reload.get('error')}")
            except Exception as e:
                logger.error(f"dashboard_store: keywords restore failed: {e}")

    if result["settings_applied"] or result["settings_stored"]:
        logger.info(
            f"dashboard_store: restored overrides — "
            f"live={result['settings_applied']} stored={result['settings_stored']}"
        )
    return result
