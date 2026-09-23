#!/usr/bin/env python3
"""
config.py – Configuration Manager v13.1 (IntentEngine-NLP Edition, HARDENED)
Supports: SQLite, PostgreSQL (via DATABASE_URL)
Compatible with: keywords.json v14.0.x, filter_engine.py, monitors.py v9.7 (hardened)

v13.1 (this pass) — targeted fix, config.py ONLY:
  * _KW_CATEGORIES was missing "templates" and "template_patterns" — the two
    top-level keys that keywords.json v14.0.x actually stores its
    Template-Driven Pattern Generation data under. Because load_keywords()
    builds the exported KEYWORDS dict *exclusively* from the keys listed in
    _KW_CATEGORIES, any key not listed there was silently dropped, no
    matter how much data existed for it in keywords.json. This meant
    filter_engine.py's _generate_template_patterns() always saw
    KEYWORDS.get("templates", {}) == {} and returned an empty pattern set —
    the entire Template-Boost mechanism (the core of "IntentEngine v14.0")
    was dead on arrival, with no error or warning anywhere.
    Both keys are now included, so keywords.json's template data actually
    reaches the filter engine.
  * load_keywords() now logs a dedicated line reporting how many template
    entries were loaded, specifically so this class of "silently empty
    category" bug is visible at boot instead of requiring a code read to
    catch (mirrors how config.py already logs total keyword counts).
  * No other behavioral changes in this file. Everything monitors.py v9.7
    already relies on from this module (CFG.DEAD_LETTER_MAX_RETRIES,
    CFG.CONNECTION_TIMEOUT, CFG.SESSION_REFRESH_INTERVAL, CFG.*_THRESHOLD,
    CFG.SCORE_WEIGHT_*, etc.) was already present and is unchanged.
  * Note: FLOOD_WAIT_MULTIPLIER is kept for backward compatibility (e.g. if
    other modules still read it), but as of monitors.py v9.7 the
    CircuitBreaker no longer uses it to *guess* a FloodWait duration — it
    honors the exact FloodWaitError.seconds value from Telegram instead.
    This var is effectively legacy/unused by the hardened monitors.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Final, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from loguru import logger

# =============================================================================
# Project root (absolute) — every file path below is anchored here so the app
# behaves identically no matter what the current working directory is
# (Render, systemd, cron and IDE runners all start with different CWDs).
# =============================================================================
PROJECT_DIR: Final[Path] = Path(__file__).resolve().parent

# =============================================================================
# JSON fallback (orjson if available)
# =============================================================================
try:
    import orjson

    def json_loads(s: str | bytes) -> Any:
        return orjson.loads(s)

    def json_dumps(obj: Any, **kw: Any) -> str:
        option = orjson.OPT_NON_STR_KEYS
        if kw.get("indent"):
            option |= orjson.OPT_INDENT_2
        return orjson.dumps(obj, option=option).decode("utf-8")

    def json_load(fp: Any, **kw: Any) -> Any:
        return orjson.loads(fp.read())

    JSON_AVAILABLE = "orjson"
except ImportError:
    import json as _json

    json_loads = _json.loads
    json_dumps = _json.dumps
    json_load = _json.load
    JSON_AVAILABLE = "stdlib"

# =============================================================================
# DATABASE_URL Parser
# =============================================================================
def parse_database_url(url: str) -> Dict[str, Any]:
    """
    Parse a PostgreSQL DATABASE_URL into connection parameters.

    v13.2 fix (audit C-2): the previous regex-based parser failed on real
    Render/database-provider URLs in three ways:
      * legacy ``postgres://`` scheme (older Render/provisioners) was rejected;
      * query strings such as ``?sslmode=require`` leaked into the database
        name, so asyncpg tried to connect to a non-existent database;
      * percent-encoded credentials (e.g. ``p%40ss``) were NOT decoded, so any
        password containing special characters silently mismatched.

    Uses urllib.parse instead of regex; secrets are never logged.
    """
    raw = (url or "").strip()
    if raw.startswith("postgres://"):
        # asyncpg speaks "postgresql://" — normalise the legacy scheme.
        raw = "postgresql://" + raw[len("postgres://"):]

    parsed = urlparse(raw)
    if parsed.scheme != "postgresql":
        raise ValueError(
            "Invalid DATABASE_URL format (expected postgresql:// or postgres://)"
        )
    if not parsed.hostname:
        raise ValueError("Invalid DATABASE_URL: missing host")

    database = (parsed.path or "").lstrip("/")
    if not database:
        raise ValueError("Invalid DATABASE_URL: missing database name")

    return {
        "user": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": database,
    }

# =============================================================================
# Loguru Configuration
# =============================================================================
def setup_logging(
    log_file: str = "bot.log",
    level: str = "INFO",
    rotation: str = "10 MB",
    retention: str = "7 days",
) -> None:
    logger.remove()
    plain_format = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} | {message}"
    console_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )
    try:
        logger.add(
            sys.stdout,
            level=level,
            format=console_format,
            colorize=True,
            enqueue=True,
        )
    except ValueError:
        # Some Python 3.11 builds fail to parse loguru color markup
        # ("Single '}' encountered in format string") - fall back to a
        # plain format so logging setup can never crash the app.
        logger.add(
            sys.stdout,
            level=level,
            format=plain_format,
            colorize=False,
            enqueue=True,
        )
    try:
        # v9.12 (audit M-11): diagnose=True was a major secret-leak vector
        # — it dumps local variables (including session strings, API hashes,
        # tokens, sender phone numbers, message text) into every traceback
        # written to bot.log, and the webadmin logs reader exposes that
        # file to anyone with dashboard access. We now default to
        # diagnose=False in production (RENDER=true or ENVIRONMENT=prod)
        # and only enable it when LOG_DIAGNOSE=true is set explicitly.
        # Local dev keeps diagnose=True for actually useful tracebacks.
        _is_production = (
            os.getenv("RENDER", "").lower() in ("1", "true", "yes")
            or os.getenv("ENVIRONMENT", "").lower() in ("prod", "production")
        )
        _diagnose = os.getenv("LOG_DIAGNOSE", "").lower() in ("1", "true", "yes")
        if _diagnose:
            diagnose_enabled = True
        elif _is_production:
            diagnose_enabled = False
        else:
            diagnose_enabled = True  # local dev default
        logger.add(
            log_file,
            level="DEBUG",
            format=plain_format,
            rotation=rotation,
            retention=retention,
            compression="gz",
            enqueue=True,
            backtrace=True,
            diagnose=diagnose_enabled,
        )
    except (ValueError, OSError) as e:
        logger.warning(f"File log sink disabled: {e}")
    logger.info(f"Logging initialized (JSON: {JSON_AVAILABLE}, diagnose: {diagnose_enabled})")

# =============================================================================
# Secret Manager (Fernet Encryption)
# =============================================================================
class SecretManager:
    _cache: Dict[str, Any] = {}
    _fernet: Optional[Fernet] = None

    @classmethod
    def _get_fernet(cls) -> Fernet:
        if cls._fernet is None:
            env_key = os.getenv("SECRET_KEY_OVERRIDE")
            if env_key:
                try:
                    cls._fernet = Fernet(env_key.encode())
                    logger.info("Loaded Fernet key from SECRET_KEY_OVERRIDE")
                    return cls._fernet
                except Exception as e:
                    logger.warning(f"Invalid SECRET_KEY_OVERRIDE: {e}, falling back to file")

            key_file = PROJECT_DIR / ".secret_key"
            if key_file.exists():
                key = key_file.read_bytes()
            else:
                key = Fernet.generate_key()
                key_file.write_bytes(key)
                key_file.chmod(0o600)
                logger.info("Generated new Fernet secret key")
            cls._fernet = Fernet(key)
        return cls._fernet

    @classmethod
    def encrypt_session(cls, session_path: str, output_path: str) -> bool:
        try:
            data = Path(session_path).read_bytes()
            encrypted = cls._get_fernet().encrypt(data)
            Path(output_path).write_bytes(encrypted)
            logger.info(f"Session encrypted: {session_path} -> {output_path}")
            return True
        except Exception as e:
            logger.error(f"Session encryption failed: {e}")
            return False

    @classmethod
    def decrypt_session(cls, encrypted_path: str, output_path: str) -> bool:
        try:
            data = Path(encrypted_path).read_bytes()
            decrypted = cls._get_fernet().decrypt(data)
            Path(output_path).write_bytes(decrypted)
            return True
        except Exception as e:
            logger.error(f"Session decryption failed: {e}")
            return False

    @classmethod
    def get(cls, key: str, default: Any = None, required: bool = True) -> Any:
        if key in cls._cache:
            return cls._cache[key]
        value = os.getenv(key, default)
        if required and value is None:
            raise EnvironmentError(
                f"[FATAL] Required env-var '{key}' is missing.\n"
                f"        Add it to accounts.env:  {key}=<value>"
            )
        cls._cache[key] = value
        return value

    @classmethod
    def get_int(cls, key: str, default: Optional[int] = None, required: bool = True) -> int:
        raw = cls.get(key, default=str(default) if default is not None else None, required=required)
        if raw is None:
            return 0
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"Env-var '{key}' must be an integer, got: {raw!r}")

    @classmethod
    def get_float(cls, key: str, default: Optional[float] = None, required: bool = True) -> float:
        raw = cls.get(key, default=str(default) if default is not None else None, required=required)
        if raw is None:
            return 0.0
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"Env-var '{key}' must be a float, got: {raw!r}")

    @classmethod
    def get_bool(cls, key: str, default: bool = False) -> bool:
        raw = cls.get(key, default=str(default).lower(), required=False)
        return str(raw).lower() in ("true", "1", "yes", "on")

    @classmethod
    def clear_cache(cls) -> None:
        cls._cache.clear()

# =============================================================================
# Load .env — anchored to the project directory, not the CWD (audit M-3)
# =============================================================================
load_dotenv(PROJECT_DIR / "accounts.env")

# =============================================================================
# Core Config Dataclass v13.1 – جميع المتغيرات
# =============================================================================
@dataclass(frozen=True, slots=True)
class _ConfigData:
    # ── Telegram ──
    TARGET_GROUP_ID: int
    ADMIN_CHAT_ID: int

    # ── Paths ──
    LOG_FILE: str
    DB_TYPE: str
    DB_FILE: str
    DB_HOST: Optional[str]
    DB_PORT: int
    DB_NAME: Optional[str]
    DB_USER: Optional[str]
    DB_PASSWORD: Optional[str]
    DB_POOL_MIN: int
    DB_POOL_MAX: int

    DOWNLOAD_PATH: Path
    SESSIONS_DIR: Path
    SECURE_SESSIONS: bool

    # ── Message limits ──
    MAX_MESSAGE_LENGTH: int
    MIN_MESSAGE_LENGTH: int
    MAX_WORDS_COUNT: int

    # ── Cache & Queue ──
    MAX_CACHE_SIZE: int
    MESSAGE_QUEUE_SIZE: int
    PROCESSING_WORKERS: int
    CACHE_TTL: int
    MAX_RECENT_HASHES: int

    # ── Database Batch ──
    DB_BATCH_SIZE: int
    DB_BATCH_INTERVAL: int
    SQLITE_CACHE_SIZE: int
    DB_POOL_SIZE: int

    # ── Filter Engine (Legacy) ──
    FUZZY_MATCH_THRESHOLD: int
    SPAM_SCORE_THRESHOLD: float
    ALERT_COOLDOWN: int
    LANGUAGE_FILTER: bool
    MIN_ARABIC_CONFIDENCE: float
    BLOOM_FILTER_SIZE: int
    BLOOM_FILTER_FP: float

    # ── Scoring (Legacy) ──
    SCORE_DIRECT_MATCH: int
    SCORE_CONTEXT_MAX: int
    SCORE_INDIRECT: int
    SCORE_URGENCY: int
    SCORE_MIN_VALID: int

    # ── Resilience ──
    MAX_RETRIES: int
    RETRY_DELAY: int
    RETRY_BACKOFF: float
    FLOOD_WAIT_MULTIPLIER: float
    MAX_RECONNECT_BACKOFF: int
    RECONNECT_CHECK_INTERVAL: int
    MAX_WORKER_RESTARTS: int

    # ── Rate Limiting ──
    MAX_ALERTS_PER_MINUTE: int
    MAX_ALERTS_PER_HOUR: int

    # ── Intervals ──
    STATS_INTERVAL: int
    CLEANUP_INTERVAL: int
    HEALTH_CHECK_INTERVAL: int
    HEALTH_MIN_MONITORS_RATIO: float
    SESSION_BACKUP_INTERVAL: int

    # ── Dashboard Settings ──
    DASHBOARD_ENABLED: bool
    DASHBOARD_PORT: int
    DASHBOARD_AUTH_TOKEN: str

    # ── Alert send behaviour (audit H-02) ──
    # When True (legacy behaviour), every text alert tries to attach the
    # group's profile photo via get_profile_photos + send_file. When False
    # (default since v9.12), text alerts go out as plain send_message —
    # half the API calls, half the FloodWait risk, and the same alert text.
    ATTACH_GROUP_PHOTO: bool

    # ── Admin Bot ──
    ADMIN_BOT_ENABLED: bool
    ADMIN_BOT_TOKEN: Optional[str]

    # ── Alerts ──
    ALERT_WITH_BUTTONS: bool
    ALERT_SHOW_SCORE: bool
    ALERT_MAX_TEXT_LEN: int
    ALERT_WITH_COPY_BUTTON: bool
    ALERT_WITH_CONTACT_BUTTON: bool

    # ── Anti-Spam (المرحلة الثانية: Watch List → Confirmation → Permanent Ignore) ──
    ANTISPAM_ENABLED: bool
    ANTISPAM_WATCH_DURATION_SECONDS: int
    ANTISPAM_BURST_MESSAGES: int
    ANTISPAM_BURST_WINDOW_SECONDS: int
    ANTISPAM_SIMILARITY_THRESHOLD: float
    ANTISPAM_SIMILARITY_WINDOW_SECONDS: int
    ANTISPAM_CATEGORY_WINDOW_SECONDS: int
    ANTISPAM_DIRECT_SPAM_GROUPS: int
    ANTISPAM_DIRECT_SPAM_WINDOW_SECONDS: int
    ANTISPAM_CONFIRM_WINDOW_SECONDS: int
    ANTISPAM_CONFIRM_MESSAGES: int
    ANTISPAM_CONFIRM_GROUPS: int
    ANTISPAM_CONFIRM_ACTIVITY_WINDOW_SECONDS: int
    ANTISPAM_MEMORY_PER_USER: int
    ANTISPAM_MAX_TRACKED_USERS: int

    # ── Memory Limits ──
    ENTITY_CACHE_MAX_SIZE: int
    PROCESSED_HASHES_MAX_SIZE: int
    GC_INTERVAL: int
    MEMORY_THRESHOLD_MB: int

    # ── Dead Letter ──
    DEAD_LETTER_MAX_RETRIES: int
    DEAD_LETTER_RETRY_DELAY: float
    DEAD_LETTER_CLEANUP_DAYS: int

    # ── Connection ──
    CONNECTION_TIMEOUT: int
    SESSION_REFRESH_INTERVAL: int

    # ── Fast Capture (deletion-race protection, monitors v9.8) ──
    FAST_CAPTURE_ENABLED: bool
    CAPTURE_BUFFER_SIZE: int
    CAPTURE_TTL_SECONDS: int

    # ── Alert Dedup (v9.10 — cross-account/re-send alert barrier) ──
    DEDUP_ENABLED: bool
    DEDUP_WINDOW_SECONDS: int

    # ── Sender Intelligence / Resolver (backend-only enrichment, v9.9) ──
    SENDER_INTEL_ENABLED: bool
    SENDER_ENTITY_CACHE_SIZE: int
    SENDER_ENTITY_CACHE_TTL: int
    SENDER_RESOLVE_MAX_RETRIES: int
    SENDER_RESOLVE_BACKOFF_BASE: float
    SENDER_RESOLVE_BACKOFF_MAX: float
    SENDER_FLOOD_MAX_SLEEP: int

    # ── Prefilter ──
    PREFILTER_ENABLED: bool
    PREFILTER_MIN_WORDS: int
    PREFILTER_MAX_EMOJIS: int

    # ── Monitoring ──
    PROMETHEUS_ENABLED: bool
    PROMETHEUS_PORT: int
    SENTRY_DSN: Optional[str]

    # ═══════════════════════════════════════════════════════════════════
    # ── Confidence Thresholds ──
    CONFIDENCE_ACCEPT_THRESHOLD: float
    CONFIDENCE_REVIEW_THRESHOLD: float

    # ── Feature Toggles ──
    NEGATION_ENABLED: bool
    FUZZY_MATCHING_ENABLED: bool
    DISTANCE_SCORING_ENABLED: bool
    AD_DETECTION_ENABLED: bool

    # ── Fuzzy Matching ──
    FUZZY_MAX_EDIT_DISTANCE: int
    FUZZY_MIN_TOKEN_LENGTH: int
    FUZZY_SIMILARITY_THRESHOLD: float

    # ── Negation ──
    NEGATION_CLAUSE_BOUNDARIES_ENABLED: bool

    # ── Distance Thresholds ──
    DISTANCE_CLOSE_THRESHOLD: int
    DISTANCE_MEDIUM_THRESHOLD: int
    DISTANCE_FAR_THRESHOLD: int
    DISTANCE_VERY_FAR_THRESHOLD: int

    # ── Length Modifiers ──
    LENGTH_MODIFIER_SHORT: float
    LENGTH_MODIFIER_MEDIUM: float
    LENGTH_MODIFIER_LONG: float
    LENGTH_MODIFIER_VERY_LONG: float

    # ── Scoring Weights ──
    SCORE_WEIGHT_INTENT: float
    SCORE_WEIGHT_ACADEMIC: float
    SCORE_WEIGHT_GRAMMAR: float
    SCORE_WEIGHT_DISTANCE: float
    SCORE_WEIGHT_URGENCY: float
    SCORE_WEIGHT_CONTEXT: float

    # ── Ad Detection ──
    AD_WEAK_PROVIDER_THRESHOLD: int
    AD_EMOJI_THRESHOLD: int

    # ── Cache ──
    TEXT_CACHE_SIZE: int
    TEXT_CACHE_TTL: int

# =============================================================================
# Config Builder v13.1
# =============================================================================
class Config:
    _instance: Optional[_ConfigData] = None

    @classmethod
    def build(cls) -> _ConfigData:
        if cls._instance is not None:
            return cls._instance

        # تحذير أمني لـ DASHBOARD_AUTH_TOKEN
        dash_token = SecretManager.get("DASHBOARD_AUTH_TOKEN", "change-me", required=False)
        if dash_token == "change-me":
            logger.warning(
                "⚠️ DASHBOARD_AUTH_TOKEN is still 'change-me'! "
                "Please change it immediately for security."
            )

        setup_logging(
            log_file=SecretManager.get("LOG_FILE", "bot.log", required=False),
            level=SecretManager.get("LOG_LEVEL", "INFO", required=False),
        )

        tg = SecretManager.get_int("TARGET_GROUP_ID", required=True)
        admin = SecretManager.get_int("ADMIN_CHAT_ID", required=True)
        if tg == 0 or admin == 0:
            raise ValueError("TARGET_GROUP_ID and ADMIN_CHAT_ID must be non-zero")

        workers = SecretManager.get_int("PROCESSING_WORKERS", 3, required=False)
        if workers < 1:
            raise ValueError("PROCESSING_WORKERS must be >= 1")

        spam = SecretManager.get_float("SPAM_SCORE_THRESHOLD", 0.7, required=False)
        if not 0 < spam <= 1:
            raise ValueError("SPAM_SCORE_THRESHOLD must be in (0, 1]")

        download_path = Path(SecretManager.get("DOWNLOAD_PATH", "downloads", required=False))
        sessions_dir = Path(SecretManager.get("SESSIONS_DIR", "sessions", required=False))
        # Relative paths are anchored to the project directory so the app is
        # independent of the process CWD (audit M-3).
        if not download_path.is_absolute():
            download_path = PROJECT_DIR / download_path
        if not sessions_dir.is_absolute():
            sessions_dir = PROJECT_DIR / sessions_dir
        download_path.mkdir(parents=True, exist_ok=True)
        sessions_dir.mkdir(parents=True, exist_ok=True)

        db_type = SecretManager.get("DB_TYPE", "sqlite", required=False).lower()
        if db_type not in ("sqlite", "postgresql"):
            logger.warning(f"Unknown DB_TYPE '{db_type}', falling back to sqlite")
            db_type = "sqlite"

        # ── DATABASE_URL support ──
        db_host = None
        db_port = 5432
        db_name = None
        db_user = None
        db_password = None

        database_url = (os.getenv("DATABASE_URL") or "").strip()
        if database_url:
            # v13.2 fix (audit C-1): a VALID postgres DATABASE_URL now WINS
            # over DB_TYPE. render.yaml ships DB_TYPE=sqlite as a bootstrap
            # default, which previously meant a user who added DATABASE_URL
            # for persistence was silently ignored and the bot kept writing
            # to ephemeral SQLite — the exact data-loss scenario render.yaml's
            # own docs claim cannot happen. If the URL is present but NOT a
            # valid postgres URL, we keep DB_TYPE unchanged and warn loudly.
            try:
                parsed = parse_database_url(database_url)
                if db_type != "postgresql":
                    logger.warning(
                        f"DATABASE_URL is set but DB_TYPE={db_type!r} — "
                        "switching DB_TYPE to 'postgresql' (DATABASE_URL takes precedence)"
                    )
                    db_type = "postgresql"
                db_host = parsed["host"]
                db_port = parsed["port"]
                db_name = parsed["database"]
                db_user = parsed["user"]
                db_password = parsed["password"]
                logger.info("Database config loaded from DATABASE_URL")
            except Exception as e:
                logger.error(
                    f"Failed to parse DATABASE_URL: {e} — keeping DB_TYPE={db_type!r} "
                    "and falling back to individual DB_* vars if set"
                )
                db_host = SecretManager.get("DB_HOST", None, required=False)
                db_port = SecretManager.get_int("DB_PORT", 5432, required=False)
                db_name = SecretManager.get("DB_NAME", None, required=False)
                db_user = SecretManager.get("DB_USER", None, required=False)
                db_password = SecretManager.get("DB_PASSWORD", None, required=False)
        else:
            db_host = SecretManager.get("DB_HOST", None, required=False)
            db_port = SecretManager.get_int("DB_PORT", 5432, required=False)
            db_name = SecretManager.get("DB_NAME", None, required=False)
            db_user = SecretManager.get("DB_USER", None, required=False)
            db_password = SecretManager.get("DB_PASSWORD", None, required=False)

        # ── بناء الكائن مع جميع المتغيرات ──
        # Relative file paths are anchored to PROJECT_DIR so the process CWD
        # never changes where the DB / logs live (audit M-3).
        log_file_raw = SecretManager.get("LOG_FILE", "bot.log", required=False)
        db_file_raw = SecretManager.get("DB_FILE", "telegram_bot.db", required=False)
        log_file_path = str(PROJECT_DIR / log_file_raw) if not os.path.isabs(log_file_raw) else log_file_raw
        db_file_path = str(PROJECT_DIR / db_file_raw) if not os.path.isabs(db_file_raw) else db_file_raw

        cfg = _ConfigData(
            # ========== الأساسيات ==========
            TARGET_GROUP_ID=tg,
            ADMIN_CHAT_ID=admin,
            LOG_FILE=log_file_path,
            DB_TYPE=db_type,
            DB_FILE=db_file_path,
            DB_HOST=db_host,
            DB_PORT=db_port,
            DB_NAME=db_name,
            DB_USER=db_user,
            DB_PASSWORD=db_password,
            DB_POOL_MIN=SecretManager.get_int("DB_POOL_MIN", 2, required=False),
            DB_POOL_MAX=SecretManager.get_int("DB_POOL_MAX", 10, required=False),
            DOWNLOAD_PATH=download_path,
            SESSIONS_DIR=sessions_dir,
            SECURE_SESSIONS=SecretManager.get_bool("SECURE_SESSIONS", False),
            # ========== الرسائل ==========
            MAX_MESSAGE_LENGTH=SecretManager.get_int("MAX_MESSAGE_LENGTH", 5000, required=False),
            MIN_MESSAGE_LENGTH=SecretManager.get_int("MIN_MESSAGE_LENGTH", 10, required=False),
            MAX_WORDS_COUNT=SecretManager.get_int("MAX_WORDS_COUNT", 20, required=False),
            # ========== الكاش والطابور ==========
            MAX_CACHE_SIZE=SecretManager.get_int("MAX_CACHE_SIZE", 10000, required=False),
            MESSAGE_QUEUE_SIZE=SecretManager.get_int("MESSAGE_QUEUE_SIZE", 1000, required=False),
            PROCESSING_WORKERS=workers,
            CACHE_TTL=SecretManager.get_int("CACHE_TTL", 300, required=False),
            MAX_RECENT_HASHES=SecretManager.get_int("MAX_RECENT_HASHES", 500, required=False),
            # ========== قاعدة البيانات ==========
            DB_BATCH_SIZE=SecretManager.get_int("DB_BATCH_SIZE", 50, required=False),
            DB_BATCH_INTERVAL=SecretManager.get_int("DB_BATCH_INTERVAL", 5, required=False),
            SQLITE_CACHE_SIZE=SecretManager.get_int("SQLITE_CACHE_SIZE", -8000, required=False),
            DB_POOL_SIZE=SecretManager.get_int("DB_POOL_SIZE", 2, required=False),
            # ========== الفلترة القديمة (للتوافق) ==========
            FUZZY_MATCH_THRESHOLD=SecretManager.get_int("FUZZY_MATCH_THRESHOLD", 85, required=False),
            SPAM_SCORE_THRESHOLD=spam,
            ALERT_COOLDOWN=SecretManager.get_int("ALERT_COOLDOWN", 300, required=False),
            LANGUAGE_FILTER=SecretManager.get_bool("LANGUAGE_FILTER", True),
            MIN_ARABIC_CONFIDENCE=SecretManager.get_float("MIN_ARABIC_CONFIDENCE", 0.7, required=False),
            BLOOM_FILTER_SIZE=SecretManager.get_int("BLOOM_FILTER_SIZE", 50000, required=False),
            BLOOM_FILTER_FP=SecretManager.get_float("BLOOM_FILTER_FP", 0.001, required=False),
            # ========== التسجيل القديم ==========
            SCORE_DIRECT_MATCH=SecretManager.get_int("SCORE_DIRECT_MATCH", 60, required=False),
            SCORE_CONTEXT_MAX=SecretManager.get_int("SCORE_CONTEXT_MAX", 20, required=False),
            SCORE_INDIRECT=SecretManager.get_int("SCORE_INDIRECT", 25, required=False),
            SCORE_URGENCY=SecretManager.get_int("SCORE_URGENCY", 15, required=False),
            SCORE_MIN_VALID=SecretManager.get_int("SCORE_MIN_VALID", 50, required=False),
            # ========== التحمل ==========
            MAX_RETRIES=SecretManager.get_int("MAX_RETRIES", 3, required=False),
            RETRY_DELAY=SecretManager.get_int("RETRY_DELAY", 5, required=False),
            RETRY_BACKOFF=SecretManager.get_float("RETRY_BACKOFF", 2.0, required=False),
            FLOOD_WAIT_MULTIPLIER=SecretManager.get_float("FLOOD_WAIT_MULTIPLIER", 1.5, required=False),
            MAX_RECONNECT_BACKOFF=SecretManager.get_int("MAX_RECONNECT_BACKOFF", 300, required=False),
            RECONNECT_CHECK_INTERVAL=SecretManager.get_int("RECONNECT_CHECK_INTERVAL", 15, required=False),
            MAX_WORKER_RESTARTS=SecretManager.get_int("MAX_WORKER_RESTARTS", 10, required=False),
            # ========== تحديد المعدل ==========
            MAX_ALERTS_PER_MINUTE=SecretManager.get_int("MAX_ALERTS_PER_MINUTE", 12, required=False),
            MAX_ALERTS_PER_HOUR=SecretManager.get_int("MAX_ALERTS_PER_HOUR", 120, required=False),
            # ========== الفترات ==========
            STATS_INTERVAL=SecretManager.get_int("STATS_INTERVAL", 3600, required=False),
            CLEANUP_INTERVAL=SecretManager.get_int("CLEANUP_INTERVAL", 86400, required=False),
            HEALTH_CHECK_INTERVAL=SecretManager.get_int("HEALTH_CHECK_INTERVAL", 60, required=False),
            HEALTH_MIN_MONITORS_RATIO=SecretManager.get_float("HEALTH_MIN_MONITORS_RATIO", 0.5, required=False),
            SESSION_BACKUP_INTERVAL=SecretManager.get_int("SESSION_BACKUP_INTERVAL", 3600, required=False),
            # ========== Dashboard ==========
            DASHBOARD_ENABLED=SecretManager.get_bool("DASHBOARD_ENABLED", False),
            DASHBOARD_PORT=SecretManager.get_int("DASHBOARD_PORT", 8080, required=False),
            DASHBOARD_AUTH_TOKEN=dash_token,
            # ========== Alert send behaviour (audit H-02) ==========
            # Default False: plain send_message is the fast path. Set
            # ATTACH_GROUP_PHOTO=true to restore the legacy behaviour of
            # attaching the group's profile photo to every text alert.
            ATTACH_GROUP_PHOTO=SecretManager.get_bool("ATTACH_GROUP_PHOTO", False),
            # ========== Admin Bot ==========
            ADMIN_BOT_ENABLED=SecretManager.get_bool("ADMIN_BOT_ENABLED", False),
            ADMIN_BOT_TOKEN=SecretManager.get("ADMIN_BOT_TOKEN", None, required=False),
            # ========== التنبيهات ==========
            ALERT_WITH_BUTTONS=SecretManager.get_bool("ALERT_WITH_BUTTONS", True),
            ALERT_SHOW_SCORE=SecretManager.get_bool("ALERT_SHOW_SCORE", True),
            ALERT_MAX_TEXT_LEN=SecretManager.get_int("ALERT_MAX_TEXT_LEN", 350, required=False),
            ALERT_WITH_COPY_BUTTON=SecretManager.get_bool("ALERT_WITH_COPY_BUTTON", True),
            # زر «تواصل مع المرسل» (المرحلة الأولى): يفتح قائمة الرسائل الجاهزة
            ALERT_WITH_CONTACT_BUTTON=SecretManager.get_bool("ALERT_WITH_CONTACT_BUTTON", True),
            # ========== مكافحة السبام (المرحلة الثانية) ==========
            # Watch List (10 دقائق مراقبة) → Spam Confirmation → Permanent Ignore.
            # لا يتجاهل ولا يحظر أحداً عند المراقبة — فقط بعد تأكيد السبام.
            ANTISPAM_ENABLED=SecretManager.get_bool("ANTISPAM_ENABLED", True),
            ANTISPAM_WATCH_DURATION_SECONDS=SecretManager.get_int("ANTISPAM_WATCH_DURATION_SECONDS", 600, required=False),
            # شرط المراقبة 1: رسالتان خلال 60 ثانية
            ANTISPAM_BURST_MESSAGES=SecretManager.get_int("ANTISPAM_BURST_MESSAGES", 2, required=False),
            ANTISPAM_BURST_WINDOW_SECONDS=SecretManager.get_int("ANTISPAM_BURST_WINDOW_SECONDS", 60, required=False),
            # شرط المراقبة 2: تشابه نصي/دلالي ≥ 80% خلال 5 دقائق
            ANTISPAM_SIMILARITY_THRESHOLD=SecretManager.get_float("ANTISPAM_SIMILARITY_THRESHOLD", 0.80, required=False),
            ANTISPAM_SIMILARITY_WINDOW_SECONDS=SecretManager.get_int("ANTISPAM_SIMILARITY_WINDOW_SECONDS", 300, required=False),
            # شرط المراقبة 3: أكثر من طلب من فئات مختلفة خلال 5 دقائق
            ANTISPAM_CATEGORY_WINDOW_SECONDS=SecretManager.get_int("ANTISPAM_CATEGORY_WINDOW_SECONDS", 300, required=False),
            # تصنيف مباشر كمزعج: نفس الرسالة/المعنى في ≥ 4 مجموعات خلال 5 دقائق
            ANTISPAM_DIRECT_SPAM_GROUPS=SecretManager.get_int("ANTISPAM_DIRECT_SPAM_GROUPS", 4, required=False),
            ANTISPAM_DIRECT_SPAM_WINDOW_SECONDS=SecretManager.get_int("ANTISPAM_DIRECT_SPAM_WINDOW_SECONDS", 300, required=False),
            # شروط تأكيد السبام أثناء المراقبة
            ANTISPAM_CONFIRM_WINDOW_SECONDS=SecretManager.get_int("ANTISPAM_CONFIRM_WINDOW_SECONDS", 300, required=False),
            ANTISPAM_CONFIRM_MESSAGES=SecretManager.get_int("ANTISPAM_CONFIRM_MESSAGES", 5, required=False),
            ANTISPAM_CONFIRM_GROUPS=SecretManager.get_int("ANTISPAM_CONFIRM_GROUPS", 3, required=False),
            ANTISPAM_CONFIRM_ACTIVITY_WINDOW_SECONDS=SecretManager.get_int("ANTISPAM_CONFIRM_ACTIVITY_WINDOW_SECONDS", 600, required=False),
            # حدود الذاكرة لسجل النشاط لكل مستخدم
            ANTISPAM_MEMORY_PER_USER=SecretManager.get_int("ANTISPAM_MEMORY_PER_USER", 50, required=False),
            ANTISPAM_MAX_TRACKED_USERS=SecretManager.get_int("ANTISPAM_MAX_TRACKED_USERS", 5000, required=False),
            # ========== الذاكرة ==========
            ENTITY_CACHE_MAX_SIZE=SecretManager.get_int("ENTITY_CACHE_MAX_SIZE", 1500, required=False),
            PROCESSED_HASHES_MAX_SIZE=SecretManager.get_int("PROCESSED_HASHES_MAX_SIZE", 8000, required=False),
            GC_INTERVAL=SecretManager.get_int("GC_INTERVAL", 60, required=False),
            MEMORY_THRESHOLD_MB=SecretManager.get_int("MEMORY_THRESHOLD_MB", 256, required=False),
            # ========== Dead Letter ==========
            DEAD_LETTER_MAX_RETRIES=SecretManager.get_int("DEAD_LETTER_MAX_RETRIES", 5, required=False),
            DEAD_LETTER_RETRY_DELAY=SecretManager.get_float("DEAD_LETTER_RETRY_DELAY", 2.0, required=False),
            DEAD_LETTER_CLEANUP_DAYS=SecretManager.get_int("DEAD_LETTER_CLEANUP_DAYS", 7, required=False),
            # ========== الاتصال ==========
            CONNECTION_TIMEOUT=SecretManager.get_int("CONNECTION_TIMEOUT", 30, required=False),
            SESSION_REFRESH_INTERVAL=SecretManager.get_int("SESSION_REFRESH_INTERVAL", 43200, required=False),
            # ========== Fast Capture (حماية سباق بوتات الحذف) ==========
            # v9.8: "احفظ أولاً، حلل ثانياً" — text is persisted in RAM the
            # instant the NewMessage handler fires, before ANY await, so a
            # deletion bot that removes the message milliseconds later can
            # never make the pipeline lose it. Code default stays False; the
            # production rollout sets FAST_CAPTURE_ENABLED=true in Render env
            # (documented kill switch — flip to false to disable instantly).
            FAST_CAPTURE_ENABLED=SecretManager.get_bool("FAST_CAPTURE_ENABLED", False),
            CAPTURE_BUFFER_SIZE=SecretManager.get_int("CAPTURE_BUFFER_SIZE", 1000, required=False),
            CAPTURE_TTL_SECONDS=SecretManager.get_int("CAPTURE_TTL_SECONDS", 30, required=False),
            # v9.10 dedup: يمنع تكرار التنبيهات (نفس المرسل بنفس النص، عبر أي
            # حساب من الحسابات الستة أو إعادة إرسال). كلتا القيمتين قابلتان
            # للتعديل الحي من لوحة التحكم (DEDUP_ENABLED / DEDUP_WINDOW_SECONDS).
            DEDUP_ENABLED=SecretManager.get_bool("DEDUP_ENABLED", True),
            DEDUP_WINDOW_SECONDS=SecretManager.get_int("DEDUP_WINDOW_SECONDS", 86400, required=False),
            # Sender Intelligence: passive, failure-safe backend enrichment
            # (never changes alert output). Kill switch: SENDER_INTEL_ENABLED=false.
            SENDER_INTEL_ENABLED=SecretManager.get_bool("SENDER_INTEL_ENABLED", True),
            SENDER_ENTITY_CACHE_SIZE=SecretManager.get_int("SENDER_ENTITY_CACHE_SIZE", 2000, required=False),
            SENDER_ENTITY_CACHE_TTL=SecretManager.get_int("SENDER_ENTITY_CACHE_TTL", 900, required=False),
            SENDER_RESOLVE_MAX_RETRIES=SecretManager.get_int("SENDER_RESOLVE_MAX_RETRIES", 3, required=False),
            SENDER_RESOLVE_BACKOFF_BASE=SecretManager.get_float("SENDER_RESOLVE_BACKOFF_BASE", 0.5, required=False),
            SENDER_RESOLVE_BACKOFF_MAX=SecretManager.get_float("SENDER_RESOLVE_BACKOFF_MAX", 8.0, required=False),
            SENDER_FLOOD_MAX_SLEEP=SecretManager.get_int("SENDER_FLOOD_MAX_SLEEP", 5, required=False),
            # ========== Prefilter ==========
            PREFILTER_ENABLED=SecretManager.get_bool("PREFILTER_ENABLED", True),
            # v14.4 filter: 1 (was 2) — short high-signal requests ("محتاج",
            # "فزعه") are legitimate; junk single words are handled by the
            # ignore/spam tries inside the engine, not by word count.
            PREFILTER_MIN_WORDS=SecretManager.get_int("PREFILTER_MIN_WORDS", 1, required=False),
            PREFILTER_MAX_EMOJIS=SecretManager.get_int("PREFILTER_MAX_EMOJIS", 5, required=False),
            # ========== المراقبة ==========
            PROMETHEUS_ENABLED=SecretManager.get_bool("PROMETHEUS_ENABLED", False),
            PROMETHEUS_PORT=SecretManager.get_int("PROMETHEUS_PORT", 9090, required=False),
            SENTRY_DSN=SecretManager.get("SENTRY_DSN", None, required=False),
            # ═══════════════════════════════════════════════════════════════════
            # ========== عتبات الثقة ==========
            CONFIDENCE_ACCEPT_THRESHOLD=SecretManager.get_float("CONFIDENCE_ACCEPT_THRESHOLD", 0.65, required=False),
            CONFIDENCE_REVIEW_THRESHOLD=SecretManager.get_float("CONFIDENCE_REVIEW_THRESHOLD", 0.40, required=False),
            # ========== تشغيل/إيقاف الميزات ==========
            NEGATION_ENABLED=SecretManager.get_bool("NEGATION_ENABLED", True),
            FUZZY_MATCHING_ENABLED=SecretManager.get_bool("FUZZY_MATCHING_ENABLED", True),
            DISTANCE_SCORING_ENABLED=SecretManager.get_bool("DISTANCE_SCORING_ENABLED", True),
            AD_DETECTION_ENABLED=SecretManager.get_bool("AD_DETECTION_ENABLED", True),
            # ========== المطابقة الضبابية ==========
            FUZZY_MAX_EDIT_DISTANCE=SecretManager.get_int("FUZZY_MAX_EDIT_DISTANCE", 1, required=False),
            FUZZY_MIN_TOKEN_LENGTH=SecretManager.get_int("FUZZY_MIN_TOKEN_LENGTH", 5, required=False),
            FUZZY_SIMILARITY_THRESHOLD=SecretManager.get_float("FUZZY_SIMILARITY_THRESHOLD", 0.92, required=False),
            # ========== النفي ==========
            NEGATION_CLAUSE_BOUNDARIES_ENABLED=SecretManager.get_bool("NEGATION_CLAUSE_BOUNDARIES_ENABLED", True),
            # ========== المسافة ==========
            DISTANCE_CLOSE_THRESHOLD=SecretManager.get_int("DISTANCE_CLOSE_THRESHOLD", 3, required=False),
            DISTANCE_MEDIUM_THRESHOLD=SecretManager.get_int("DISTANCE_MEDIUM_THRESHOLD", 7, required=False),
            DISTANCE_FAR_THRESHOLD=SecretManager.get_int("DISTANCE_FAR_THRESHOLD", 10, required=False),
            DISTANCE_VERY_FAR_THRESHOLD=SecretManager.get_int("DISTANCE_VERY_FAR_THRESHOLD", 15, required=False),
            # ========== معامل الطول ==========
            LENGTH_MODIFIER_SHORT=SecretManager.get_float("LENGTH_MODIFIER_SHORT", 0.75, required=False),
            LENGTH_MODIFIER_MEDIUM=SecretManager.get_float("LENGTH_MODIFIER_MEDIUM", 0.9, required=False),
            LENGTH_MODIFIER_LONG=SecretManager.get_float("LENGTH_MODIFIER_LONG", 1.0, required=False),
            LENGTH_MODIFIER_VERY_LONG=SecretManager.get_float("LENGTH_MODIFIER_VERY_LONG", 0.9, required=False),
            # ========== أوزان التسجيل ==========
            SCORE_WEIGHT_INTENT=SecretManager.get_float("SCORE_WEIGHT_INTENT", 0.30, required=False),
            SCORE_WEIGHT_ACADEMIC=SecretManager.get_float("SCORE_WEIGHT_ACADEMIC", 0.25, required=False),
            SCORE_WEIGHT_GRAMMAR=SecretManager.get_float("SCORE_WEIGHT_GRAMMAR", 0.15, required=False),
            SCORE_WEIGHT_DISTANCE=SecretManager.get_float("SCORE_WEIGHT_DISTANCE", 0.15, required=False),
            SCORE_WEIGHT_URGENCY=SecretManager.get_float("SCORE_WEIGHT_URGENCY", 0.05, required=False),
            SCORE_WEIGHT_CONTEXT=SecretManager.get_float("SCORE_WEIGHT_CONTEXT", 0.10, required=False),
            # ========== كشف الإعلانات ==========
            AD_WEAK_PROVIDER_THRESHOLD=SecretManager.get_int("AD_WEAK_PROVIDER_THRESHOLD", 2, required=False),
            AD_EMOJI_THRESHOLD=SecretManager.get_int("AD_EMOJI_THRESHOLD", 3, required=False),
            # ========== الكاش ==========
            TEXT_CACHE_SIZE=SecretManager.get_int("TEXT_CACHE_SIZE", 5000, required=False),
            TEXT_CACHE_TTL=SecretManager.get_int("TEXT_CACHE_TTL", 300, required=False),
        )

        cls._instance = cfg
        logger.info(
            f"Config v13.1 built: DB={db_type} | "
            f"PROCESSING_WORKERS={workers} | "
            f"DASHBOARD={'ON' if cfg.DASHBOARD_ENABLED else 'OFF'} | "
            f"FUZZY={'ON' if cfg.FUZZY_MATCHING_ENABLED else 'OFF'} | "
            f"NEGATION={'ON' if cfg.NEGATION_ENABLED else 'OFF'} | "
            f"AD_DETECTION={'ON' if cfg.AD_DETECTION_ENABLED else 'OFF'}"
        )
        return cfg

    @classmethod
    def reload(cls) -> _ConfigData:
        SecretManager.clear_cache()
        load_dotenv(PROJECT_DIR / "accounts.env", override=True)
        cls._instance = None
        return cls.build()

# =============================================================================
# Pre-built config instance
# =============================================================================
CFG = Config.build()

# =============================================================================
# Regex Patterns (compiled once, module-level)
# =============================================================================
PHONE_PATTERN: Final = re.compile(
    r"(\+?\d{1,3}[.\-\s]?)?\d{3}[.\-\s]?\d{3}[.\-\s]?\d{4}"
)
URL_PATTERN: Final = re.compile(
    r"https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+|wa\.me/\S+"
    r"|bit\.ly/\S+|tinyurl\.com/\S+|goo\.gl/\S+|linktr\.ee/\S+"
)
EMAIL_PATTERN: Final = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)
EMOJI_PATTERN: Final = re.compile(
    "["
    "\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    "\U00002702-\U000027B0\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE,
)
HTML_ESCAPE: Final = str.maketrans({'"': '&quot;', '&': '&amp;', '<': '&lt;', '>': '&gt;'})
WS_PATTERN: Final = re.compile(r"\s+")
ARABIC_CHARS_PATTERN: Final = re.compile(r"[\u0600-\u06FF]")

# =============================================================================
# Input Sanitizer
# =============================================================================
class InputSanitizer:
    @staticmethod
    def escape_html(text: Optional[str]) -> str:
        if not text:
            return ""
        return text.translate(HTML_ESCAPE)

    @staticmethod
    def truncate(text: str, n: int = 300) -> str:
        if len(text) <= n:
            return text
        return text[: n - 1] + "..."

    @staticmethod
    def validate_message_text(text: Any) -> Optional[str]:
        if not isinstance(text, str):
            return None
        text = text.strip()
        if CFG.MIN_MESSAGE_LENGTH <= len(text) <= CFG.MAX_MESSAGE_LENGTH:
            return text
        return None

    @staticmethod
    def normalize(text: str) -> str:
        return WS_PATTERN.sub(" ", text.lower().strip())

    @staticmethod
    def contains_dangerous_html(text: str) -> bool:
        dangerous = re.compile(r"<\s*(script|iframe|object|embed|form|input)", re.IGNORECASE)
        return bool(dangerous.search(text))

# =============================================================================
# Keyword Loader v13.1 – متوافق مع keywords.json v14.0.x
#
# FIX (critical, see module docstring): "templates" and "template_patterns"
# are now included. These are the two keys keywords.json v14.0.x actually
# uses for its Template-Driven Pattern Generation data. Previously they
# were absent from this list, so load_keywords() silently discarded them
# and filter_engine.py's Template-Boost mechanism always ran on an empty
# dataset — no exception, no log warning, just quietly-degraded filtering.
# =============================================================================
_KW_CATEGORIES: Final[List[str]] = [
    # الفئات القديمة (للتوافق)
    "request", "advertisement", "ignore", "emoji_advertisement",
    "education_providers", "ad_blockers", "request_context",
    "indirect_request", "urgency", "spam_patterns",
    # الفئات v13.0
    "intent_verbs", "academic_objects", "request_phrases",
    "urgency_markers", "negation", "advertisement_signals",
    "spam_categories", "emoji_signals", "ignore_signals",
    "dialect_mapping", "university_context", "distance_scoring_config",
    "length_modifier", "scoring_weights", "explainability",
    "conversation_history_config", "test_cases", "learning_feedback",
    "high_confidence_boost_patterns", "action_verbs", "subject_markers",
    "implicit_request_patterns", "solve_actions", "help_expressions",
    # ── v14.0.x: Template-Driven Pattern Generation (previously missing) ──
    "templates", "template_patterns",
]

def load_keywords(path: Optional[str] = None) -> Dict[str, Any]:
    """Load keywords.json (absolute by default, anchored to PROJECT_DIR)."""
    if path is None:
        path = str(PROJECT_DIR / "keywords.json")
    elif not os.path.isabs(path):
        path = str(PROJECT_DIR / path)
    default: Dict[str, Any] = {c: {} for c in _KW_CATEGORIES}
    if not os.path.exists(path):
        logger.warning(f"Keywords file not found: {path} - using empty defaults")
        return default
    try:
        with open(path, "rb") as f:
            data = json_load(f)
        if not isinstance(data, dict):
            logger.error("Invalid keywords.json: root must be a JSON object")
            return default
        result: Dict[str, Any] = {}
        for key in _KW_CATEGORIES:
            raw = data.get(key, {})
            if isinstance(raw, list):
                result[key] = raw
            elif isinstance(raw, dict):
                result[key] = raw
            else:
                result[key] = {}
        total = sum(len(v) if isinstance(v, (list, dict)) else 0 for v in result.values())
        logger.info(f"Keywords loaded: {total} total across {len(_KW_CATEGORIES)} categories")

        # Dedicated visibility for the template engine specifically, so a
        # future regression (e.g. someone renaming a key in keywords.json)
        # shows up immediately in boot logs instead of as a silent
        # accuracy regression discovered weeks later.
        templates_data = result.get("templates", {})
        template_patterns_data = result.get("template_patterns", {})
        templates_count = len(templates_data) if isinstance(templates_data, (list, dict)) else 0
        template_patterns_count = len(template_patterns_data) if isinstance(template_patterns_data, (list, dict)) else 0
        if templates_count == 0 and template_patterns_count == 0:
            logger.warning(
                "Template-Boost engine: 'templates'/'template_patterns' are EMPTY in "
                f"{path} - template_boost pattern generation will produce zero patterns. "
                "If keywords.json is expected to define templates, check the key names."
            )
        else:
            logger.info(
                f"Template-Boost engine: templates={templates_count} entries, "
                f"template_patterns={template_patterns_count} entries loaded from {path}"
            )
        return result
    except Exception as e:
        logger.error(f"Error loading keywords: {e}")
        return default

KEYWORDS: Final[Dict[str, Any]] = load_keywords()

# =============================================================================
# Account Loader – Dynamic discovery (MAIN + ACCOUNT_1..N)
# =============================================================================
def load_accounts() -> List[Dict[str, Any]]:
    accounts: List[Dict[str, Any]] = []

    def _diagnose_partial(prefix: str) -> None:
        # v9.13.1 diagnostic: when an account is skipped because {prefix}_API_ID
        # is missing, check whether OTHER account vars exist for this prefix.
        # If they do, the operator clearly INTENDED this account to run and it
        # is now silently dead — surface a loud, actionable warning instead
        # (this exact scenario left production running with 0 accounts while
        # *_SESSION_STRING vars were fully configured).
        present: List[str] = []
        for suffix in ("API_HASH", "PHONE", "SESSION_NAME", "SESSION_STRING", "PRIORITY"):
            if SecretManager.get(f"{prefix}_{suffix}", None, required=False):
                present.append(f"{prefix}_{suffix}")
        if present:
            logger.warning(
                f"Account {prefix} SKIPPED: {prefix}_API_ID is missing, but "
                f"{len(present)} other var(s) are set ({', '.join(present)}). "
                f"Set {prefix}_API_ID (and {prefix}_API_HASH / {prefix}_PHONE "
                f"if missing) to activate this account."
            )

    def _try_load(prefix: str, is_main: bool, default_priority: int) -> None:
        enabled_key = f"{prefix}_ENABLED"
        raw_enabled = SecretManager.get(enabled_key, "true", required=False) or "true"
        if raw_enabled.lower() == "false":
            logger.info(f"Skipping disabled account: {prefix}")
            return
        api_id_str = SecretManager.get(f"{prefix}_API_ID", None, required=False)
        if not api_id_str:
            _diagnose_partial(prefix)
            return
        try:
            acc: Dict[str, Any] = {
                "id": len(accounts) + 1,
                "prefix": prefix,
                "name": prefix.replace("_", " ").title(),
                "api_id": int(api_id_str),
                "api_hash": SecretManager.get(f"{prefix}_API_HASH", required=True),
                "phone": SecretManager.get(f"{prefix}_PHONE", required=True),
                "session": SecretManager.get(f"{prefix}_SESSION_NAME", required=True),
                # Render/Cloud: Telethon StringSession read from env (no interactive login)
                "session_string": SecretManager.get(f"{prefix}_SESSION_STRING", None, required=False),
                "priority": SecretManager.get_int(f"{prefix}_PRIORITY", default_priority, required=False),
                "is_main": is_main,
                "enabled": True,
                "retry_count": 0,
                "last_error": None,
            }
            accounts.append(acc)
            # PII: never log the full phone number (audit M-9) — mask middle digits.
            phone = str(acc["phone"])
            masked_phone = ("*" * max(0, len(phone) - 4)) + phone[-4:] if phone else ""
            logger.debug(
                f"Account loaded: {acc['name']} (phone={masked_phone}) "
                f"session_string={'YES' if acc['session_string'] else 'NO'}"
            )
        except (EnvironmentError, ValueError) as e:
            logger.error(f"Skipping account {prefix}: {e}")

    # MAIN account
    _try_load("MAIN", is_main=True, default_priority=10)

    # ACCOUNT_1 .. ACCOUNT_20
    for i in range(1, 21):
        prefix = f"ACCOUNT_{i}"
        api_id = SecretManager.get(f"{prefix}_API_ID", None, required=False)
        if api_id is None:
            # v9.13.1 diagnostic: discovery stops at the first gap. If a LATER
            # ACCOUNT_k still carries an API_ID, that account can never load —
            # warn the operator instead of dying silently.
            later: List[str] = []
            for j in range(i + 1, 21):
                if SecretManager.get(f"ACCOUNT_{j}_API_ID", None, required=False):
                    later.append(f"ACCOUNT_{j}")
            if later:
                logger.warning(
                    f"Account discovery stopped at {prefix} (missing "
                    f"{prefix}_API_ID) but later accounts are configured: "
                    f"{', '.join(later)}. ACCOUNT_N keys must be contiguous — "
                    f"renumber them so discovery reaches all of them."
                )
            break
        _try_load(prefix, is_main=False, default_priority=10 - i)

    logger.info(f"Accounts discovered: {len(accounts)}")
    return accounts

ACCOUNTS: Final[List[Dict[str, Any]]] = load_accounts()

# =============================================================================
# Retry Decorators
# =============================================================================
def async_retry(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[type, ...] = (Exception,),
    on_retry: Any = None,
):
    def decorator(coro: Any) -> Any:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Optional[Exception] = None
            current_delay = delay
            for attempt in range(1, max_retries + 1):
                try:
                    return await coro(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_retries:
                        raise
                    if on_retry:
                        try:
                            on_retry(attempt, e, current_delay)
                        except Exception:
                            pass
                    logger.warning(
                        f"Retry {attempt}/{max_retries} for {coro.__name__}: {e} | "
                        f"next in {current_delay:.1f}s"
                    )
                    await asyncio.sleep(current_delay)
                    current_delay *= backoff
            raise last_exception  # type: ignore[misc]
        return wrapper
    return decorator


# v9.12 (audit L-01): the previous `# type: ignoreisc]` comment in the
# repo was a malformed truncation of `# type: ignore[misc]` — it had no
# effect on type checkers and was just confusing noise. The line above
# now uses the correct `[misc]` code.

# =============================================================================
# Utility: fast_hash
# =============================================================================
def fast_hash(data: str) -> str:
    return hashlib.blake2b(data.encode("utf-8"), digest_size=16).hexdigest()

# =============================================================================
# الرسائل الجاهزة — زر «تواصل مع المرسل» (المرحلة الأولى، طلب المستخدم)
# =============================================================================
# تُعرض القائمة كاملة للمشرف عند الضغط على الزر، ويُرسل النص المختار كما هو
# إلى صاحب الإعلان/الطلب. يمكن تجاوز القائمة عبر متغير البيئة
# CONTACT_TEMPLATES_JSON (قائمة JSON من النصوص) دون تعديل الكود.
DEFAULT_CONTACT_TEMPLATES: Final[List[str]] = [
    "السلام عليكم، إذا تبي أحد يسوي لك بحث، أرسل الموضوع وتفاصيل البحث وتبشر بأمور تبيض الوجه، والدفع بعد الإنجاز.",
    "السلام عليكم، إذا تبي أحد يسوي لك CV ثقة والدفع بعد الإنجاز: 0578568011",
    "السلام عليكم، إذا تبي مختص يحل لك الواجبات: 0578568011",
    "السلام عليكم، تبي مختص ثقة يحل واجبات: 0578568011",
    "السلام عليكم، ما يهمك نسوي لك مشروعك والدفع بعد الإنجاز وشغل يبيض الوجه.",
]

# تسميات مختصرة للأزرار الرقمية في قائمة الاختيار (بنفس ترتيب القائمة أعلاه)
CONTACT_TEMPLATE_LABELS: Final[List[str]] = ["بحث", "CV", "واجبات", "واجبات", "مشروع"]


def get_contact_templates() -> List[str]:
    """قائمة الرسائل الجاهزة (قابلة للتجاوز عبر CONTACT_TEMPLATES_JSON)."""
    raw = (os.getenv("CONTACT_TEMPLATES_JSON") or "").strip()
    if raw:
        try:
            import json as _json
            data = _json.loads(raw)
            if (
                isinstance(data, list)
                and data
                and all(isinstance(x, str) and x.strip() for x in data)
            ):
                return [x.strip() for x in data][:8]  # حد أقصى 8 قوالب (بيانات الزر ≤ 64 بايت)
        except Exception as e:  # pragma: no cover — config fallback
            logger.warning(f"CONTACT_TEMPLATES_JSON invalid, using defaults: {e}")
    return list(DEFAULT_CONTACT_TEMPLATES)


def contact_template_labels(count: int) -> List[str]:
    """تسميات أزرار القائمة — افتراضية أو عامة عند تجاوز عدد القوالب."""
    labels = list(CONTACT_TEMPLATE_LABELS)
    out: List[str] = []
    for i in range(max(1, count)):
        out.append(labels[i] if i < len(labels) else f"رسالة {i + 1}")
    return out

# =============================================================================
# Score bar utility
# =============================================================================
def score_bar(score: int, max_score: int = 100, width: int = 10) -> str:
    filled = min(width, round(score / max_score * width))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {score}"

# =============================================================================
# Memory info utility
# =============================================================================
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

def get_memory_info() -> Dict[str, Any]:
    if PSUTIL_AVAILABLE:
        try:
            mem = psutil.Process().memory_info()
            return {
                "rss_mb": mem.rss // (1024 * 1024),
                "vms_mb": mem.vms // (1024 * 1024),
                "percent": psutil.Process().memory_percent(),
            }
        except Exception:
            pass
    return {"rss_mb": 0, "vms_mb": 0, "percent": 0.0}

logger.info(
    f"config.py v13.1 loaded | Accounts: {len(ACCOUNTS)} | Workers: {CFG.PROCESSING_WORKERS} | "
    f"DB_URL: {'YES' if os.getenv('DATABASE_URL') else 'NO'} | "
    f"DASHBOARD: {'ON' if CFG.DASHBOARD_ENABLED else 'OFF'} | "
    f"INTENT_ENGINE: v13.1 (template-boost fix applied)"
)