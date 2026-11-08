#!/usr/bin/env python3
"""
config.py – Configuration Manager v14.5.1 (IntentEngine-NLP Edition, HARDENED)
Supports: SQLite, PostgreSQL (via DATABASE_URL)
Compatible with: keywords.json v14.0.x, filter_engine.py v14.5.1,
                 monitors.py v9.7 (hardened)

v14.5.1 (this pass) — correction pass aligned with filter_engine.py v14.5.1:
  * Added 9 config fields that filter_engine.py v14.5.x has always read
    via `_cfg(...)` fallbacks but which were never declared in _ConfigData:
        FUZZY_SCORE_CUTOFF, REGEX_TIMEOUT_S, REGEX_GUARD_WORKERS,
        FILTER_EXECUTOR_WORKERS, METRICS_WINDOW, METRICS_Z_THRESHOLD,
        METRICS_ENABLED, ADAPTIVE_ALPHA, HELP_ANCHOR_INTENT_WEIGHT.
    All were silently running at their hard-coded defaults. Each is now
    env-driven with the SAME default as the previous fallback → identical
    out-of-the-box behavior, full runtime tunability from now on.
  * DATABASE_URL: `sslmode=` in the query string is now parsed and exposed
    as CFG.DATABASE_SSL. Previously the entire query string was dropped,
    so a provider that FORCES SSL (Render, Heroku, DO) could either fail
    to connect or connect without TLS. Default = None → identical to
    asyncpg's own default when sslmode is absent (no behavior change).
  * MIN_ARABIC_CONFIDENCE is now the real gate in filter_engine's
    `_is_arabic` (see one-line patch in filter_engine.py changelog).
    Previously the field existed here but NOTHING read it — the filter
    hard-coded 0.12. Its DEFAULT is changed 0.7 → 0.12 so that wiring it
    in does not silently flip the effective threshold from "12% Arabic
    letters" to "70% Arabic letters" and reject every mixed message
    ("ابي حل assignment 2" = ratio 0.33 → would have been killed).
    Operators who had (pointlessly) set MIN_ARABIC_CONFIDENCE=0.7 in
    the environment keep their value — and it now actually does what the
    name says.
  * FUZZY_SCORE_CUTOFF now defaults to float(FUZZY_MATCH_THRESHOLD) so
    the legacy integer and the new float can never silently diverge.
    Setting FUZZY_MATCH_THRESHOLD=90 still works end-to-end.
  * BOT_VERSION and boot log lines now report "14.5.1", synchronized
    with FILTER_ENGINE_VERSION. Previous BOT_VERSION "14.0.0" made every
    deploy carry three contradictory version strings.
  * DEFAULT_CONTACT_TEMPLATES no longer embed the operator's phone number.
    Templates use a {phone} placeholder filled at load time from the
    CONTACT_PHONE env var (accounts.env). If unset, a WARNING is logged
    at boot and the placeholder renders as "—". The number previously in
    this file is still in git history and MUST be rotated by the operator.

v13.1 (previous pass):
  * _KW_CATEGORIES now includes "templates" and "template_patterns" — the
    two top-level keys keywords.json v14.0.x uses for Template-Driven
    Pattern Generation data. Without them the entire Template-Boost
    mechanism was dead on arrival, silently.
  * load_keywords() logs template counts explicitly so a future
    "silently empty category" regression is visible at boot.

v13.0 and earlier: see git history.
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
from urllib.parse import parse_qs, unquote, urlparse

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from loguru import logger

# =============================================================================
# Project root (absolute) — every file path below is anchored here so the app
# behaves identically no matter what the current working directory is.
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
    """Parse a PostgreSQL DATABASE_URL into connection parameters.

    v14.5.1: `sslmode=` in the query string is now extracted and mapped to
    the asyncpg `ssl` value (None/"require"/"verify-ca"/"verify-full").
    Legacy `postgres://` scheme is normalised; percent-encoded credentials
    are decoded; secrets are never logged.
    """
    raw = (url or "").strip()
    if raw.startswith("postgres://"):
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

    # v14.5.1: sslmode extraction
    qs = parse_qs(parsed.query or "")
    sslmode_raw = (qs.get("sslmode", [""])[0] or "").lower()
    ssl_param: Optional[str] = None
    if sslmode_raw in ("require", "verify-ca", "verify-full"):
        ssl_param = sslmode_raw
    elif sslmode_raw in ("disable", "allow", "prefer"):
        # asyncpg default (sslmode unset) = prefer (try plain, fall back to TLS).
        # For "disable" we could map to ssl=False explicitly, but leaving it
        # None lets asyncpg use its default — which is exactly what a URL
        # WITHOUT sslmode= produces today, so both paths behave identically
        # unless the operator specifically asked for strict TLS.
        ssl_param = None

    return {
        "user": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": database,
        "ssl": ssl_param,           # v14.5.1 — was dropped entirely before
        "sslmode_raw": sslmode_raw, # kept for logging / diagnostics only
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
        logger.add(
            sys.stdout,
            level=level,
            format=plain_format,
            colorize=False,
            enqueue=True,
        )

    # v14.5.1: diagnose_enabled is now defined BEFORE the try block so it
    # is always bound even if logger.add fails; the final log line can no
    # longer raise NameError under any reordering.
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
        diagnose_enabled = True

    try:
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
# Load .env — anchored to the project directory, not the CWD
# =============================================================================
load_dotenv(PROJECT_DIR / "accounts.env")

# =============================================================================
# Core Config Dataclass v14.5.1 – جميع المتغيرات
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
    # v14.5.1: SSL mode extracted from DATABASE_URL's ?sslmode=... query.
    # None = asyncpg default (tries plain then TLS). "require" / "verify-ca"
    # / "verify-full" = strict TLS. Cloud providers that force SSL need
    # "require" at minimum — was dropped entirely before v14.5.1.
    DATABASE_SSL: Optional[str]
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

    # ── v9.28-1: سياسات الاحتفاظ ──
    RETENTION_MESSAGES_DAYS: int
    RETENTION_AUDIT_DAYS: int
    RETENTION_NOTIFICATIONS_DAYS: int

    # ── Alert send behaviour ──
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
    MAX_EVENT_AGE_SECONDS: int
    ALERT_CARD_MAX_CHARS: int
    QUEUE_STALE_PURGE_SECONDS: int
    QUEUE_INFLIGHT_MAX_AGE_SECONDS: int
    QUEUE_INFLIGHT_MAX_ATTEMPTS: int
    QUEUE_RECOVER_INTERVAL_SECONDS: int
    REPLY_FIX_ENABLED: bool
    REPLY_FIX_HOURLY_PER_USER: int
    REPLY_FIX_DAILY_CAP: int
    REPLY_FIX_JITTER_MIN: float
    REPLY_FIX_JITTER_MAX: float
    ALERT_BUTTONS_ENABLED: bool
    ALERT_FORWARD_FALLBACK: bool
    TARGET_MEMBERSHIP_CHECK: bool

    # ── v10.8 Alert Bot ──
    ALERT_BOT_TOKEN: Optional[str]
    ALERT_BOT_ENABLED: bool
    ALERT_BOT_TIMEOUT: float
    ALERT_BOT_MAX_RETRIES: int

    # ── Anti-Spam ──
    ANTISPAM_ENABLED: bool
    RULE_ENGINE_ENABLED: bool
    ALLOWLIST_ENABLED: bool
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
    ANTISPAM_CONFIRM_SPAM_INDICATOR_REQUIRED: bool
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

    # ── Fast Capture ──
    FAST_CAPTURE_ENABLED: bool
    CAPTURE_BUFFER_SIZE: int
    CAPTURE_TTL_SECONDS: int

    # ── Alert Dedup ──
    DEDUP_ENABLED: bool
    DEDUP_WINDOW_SECONDS: int
    DEDUP_RECOVERY_STEAL_MIN_AGE_SECONDS: int

    # ── v10.0 Precision ──
    CLAUSE_RESCORE_ENABLED: bool

    # ── Similarity gate ──
    SIMILARITY_ENABLED: bool
    SIMILARITY_THRESHOLD: float
    SIMILARITY_WINDOW_SECONDS: int

    # ── Daily digest ──
    DIGEST_ENABLED: bool
    DIGEST_HOUR_UTC: int

    # ── Sender Intelligence / Resolver ──
    SENDER_INTEL_ENABLED: bool
    SENDER_ENTITY_CACHE_SIZE: int
    SENDER_ENTITY_CACHE_TTL: int
    SENDER_RESOLVE_MAX_RETRIES: int
    SENDER_RESOLVE_BACKOFF_BASE: float
    SENDER_RESOLVE_BACKOFF_MAX: float
    SENDER_FLOOD_MAX_SLEEP: int

    # ── v10.4 Sender Mention Intelligence ──
    SENDER_MENTION_FIX_ENABLED: bool
    MENTION_NEG_TTL_SECONDS: int
    MENTION_MAX_TIERS_PER_CANDIDATE: int

    # ── v10.5 Alert Navigation Resolver ──
    NAV_EXPORT_LINK_ENABLED: bool
    NAV_USER_DISCOVERY_ENABLED: bool
    TELEGRAM_CATCH_UP: bool
    QUEUE_EVENT_WAKE: bool
    ALERT_LATENCY_LOG_EVERY: int
    WARMUP_CHAT_CACHE: bool

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
    # v14.5.1: the effective cutoff that filter_engine._fuzzy_intent_fallback
    # reads. Defaults to float(FUZZY_MATCH_THRESHOLD) so setting the legacy
    # integer flows here automatically. Explicit FUZZY_SCORE_CUTOFF wins.
    FUZZY_SCORE_CUTOFF: float

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

    # ═══════════════════════════════════════════════════════════════════
    # ── v14.5.1 Execution / Regex Guard (filter_engine + security) ──
    # REGEX_TIMEOUT_S: watchdog timeout per regex call inside SafeRegexExecutor.
    # REGEX_GUARD_WORKERS: dedicated thread pool size for the regex guard.
    # FILTER_EXECUTOR_WORKERS: dedicated CPU-bound filter pipeline executor.
    REGEX_TIMEOUT_S: float
    REGEX_GUARD_WORKERS: int
    FILTER_EXECUTOR_WORKERS: int

    # ── v14.5.1 Metrics (BoundedMetrics used by filter_engine) ──
    METRICS_WINDOW: int
    METRICS_Z_THRESHOLD: float
    METRICS_ENABLED: bool

    # ── v14.5.1 Adaptive weights + Help anchor ──
    # ADAPTIVE_ALPHA: learning rate for AdaptiveWeights (feedback-driven).
    # HELP_ANCHOR_INTENT_WEIGHT: intent weight assigned when a bare help
    # verb ("ساعدوني") anchors the intent with no explicit request verb.
    ADAPTIVE_ALPHA: float
    HELP_ANCHOR_INTENT_WEIGHT: float

# =============================================================================
# Config Builder v14.5.1
# =============================================================================
class Config:
    _instance: Optional[_ConfigData] = None

    @classmethod
    def build(cls) -> _ConfigData:
        if cls._instance is not None:
            return cls._instance

        setup_logging(
            log_file=SecretManager.get("LOG_FILE", "bot.log", required=False),
            level=SecretManager.get("LOG_LEVEL", "INFO", required=False),
        )

        tg_raw = SecretManager.get("TARGET_GROUP_ID", None, required=False)
        admin_raw = SecretManager.get("ADMIN_CHAT_ID", None, required=False)
        if tg_raw is None or admin_raw is None:
            logger.critical(
                "TARGET_GROUP_ID/ADMIN_CHAT_ID missing at boot — running in "
                "idle mode (no monitoring/alerts, health server only). If this "
                "is a normal deploy, verify the Render environment variables."
            )
            tg = int(tg_raw) if tg_raw is not None else 0
            admin = int(admin_raw) if admin_raw is not None else 0
        else:
            tg = int(tg_raw)
            admin = int(admin_raw)
            if tg == 0 or admin == 0:
                raise ValueError("TARGET_GROUP_ID and ADMIN_CHAT_ID must be non-zero")

        workers = SecretManager.get_int("PROCESSING_WORKERS", 3, required=False)
        if workers < 1:
            raise ValueError("PROCESSING_WORKERS must be >= 1")

        spam = SecretManager.get_float("SPAM_SCORE_THRESHOLD", 0.7, required=False)
        if not 0 < spam <= 1:
            raise ValueError("SPAM_SCORE_THRESHOLD must be in (0, 1]")

        # v14.5.1: fetch legacy fuzzy int ONCE so FUZZY_SCORE_CUTOFF can
        # default from it without two independent reads that could diverge.
        legacy_fuzzy = SecretManager.get_int("FUZZY_MATCH_THRESHOLD", 85, required=False)

        download_path = Path(SecretManager.get("DOWNLOAD_PATH", "downloads", required=False))
        sessions_dir = Path(SecretManager.get("SESSIONS_DIR", "sessions", required=False))
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

        db_host = None
        db_port = 5432
        db_name = None
        db_user = None
        db_password = None
        db_ssl: Optional[str] = None  # v14.5.1

        database_url = (os.getenv("DATABASE_URL") or "").strip()
        if database_url:
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
                db_ssl = parsed.get("ssl")  # v14.5.1
                _sslmode = parsed.get("sslmode_raw") or "unset"
                logger.info(f"Database config loaded from DATABASE_URL (sslmode={_sslmode})")
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
            DATABASE_SSL=db_ssl,  # v14.5.1
            DB_POOL_MIN=SecretManager.get_int("DB_POOL_MIN", 2, required=False),
            DB_POOL_MAX=SecretManager.get_int("DB_POOL_MAX", 10, required=False),
            DOWNLOAD_PATH=download_path,
            SESSIONS_DIR=sessions_dir,
            SECURE_SESSIONS=SecretManager.get_bool("SECURE_SESSIONS", False),
            # ========== الرسائل ==========
            # v11.5 (مواصفة المستخدم 2026-10 — فلترة الطول): القاعدة المركزية
            # "6 <= len(text) <= 65" على النص الأصلي. NOTE: filter_engine.py's
            # analyzer-local FILTER_MIN_MESSAGE_CHARS (3) is intentionally
            # looser and only applies on the branch where FilterPolicy
            # import fails; the canonical gate is FilterPolicy.check() which
            # reads MIN_MESSAGE_LENGTH from here.
            MAX_MESSAGE_LENGTH=SecretManager.get_int("MAX_MESSAGE_LENGTH", 65, required=False),
            MIN_MESSAGE_LENGTH=SecretManager.get_int("MIN_MESSAGE_LENGTH", 6, required=False),
            MAX_WORDS_COUNT=SecretManager.get_int("MAX_WORDS_COUNT", 20, required=False),
            # ========== الكاش والطابور ==========
            # v14.5.1 (T3.5): 10000 → 5000 — Render Free/Starter 512MB:
            # message_cache = deque(maxlen=min(MAX_CACHE_SIZE, 20000)) في
            # database.py يحمل نصوص رسائل مُطبّعة؛ 5K يكفي لنافذة التكرار
            # ويوفّر عشرات الـMB تحت الضغط (memory_guard يملأ الفراغ عند
            # 85% بSoft Reset يشمل مسح الكاش نفسه).
            MAX_CACHE_SIZE=SecretManager.get_int("MAX_CACHE_SIZE", 5000, required=False),
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
            FUZZY_MATCH_THRESHOLD=legacy_fuzzy,
            SPAM_SCORE_THRESHOLD=spam,
            ALERT_COOLDOWN=SecretManager.get_int("ALERT_COOLDOWN", 300, required=False),
            MAX_EVENT_AGE_SECONDS=SecretManager.get_int("MAX_EVENT_AGE_SECONDS", 1800, required=False),
            ALERT_CARD_MAX_CHARS=SecretManager.get_int("ALERT_CARD_MAX_CHARS", 4000, required=False),
            QUEUE_STALE_PURGE_SECONDS=SecretManager.get_int("QUEUE_STALE_PURGE_SECONDS", 7200, required=False),
            QUEUE_INFLIGHT_MAX_AGE_SECONDS=SecretManager.get_int("QUEUE_INFLIGHT_MAX_AGE_SECONDS", 600, required=False),
            QUEUE_INFLIGHT_MAX_ATTEMPTS=SecretManager.get_int("QUEUE_INFLIGHT_MAX_ATTEMPTS", 3, required=False),
            QUEUE_RECOVER_INTERVAL_SECONDS=SecretManager.get_int("QUEUE_RECOVER_INTERVAL_SECONDS", 120, required=False),
            LANGUAGE_FILTER=SecretManager.get_bool("LANGUAGE_FILTER", True),
            # v14.5.1: default changed 0.7 → 0.12 to match the effective
            # threshold filter_engine hard-coded before this pass. Now that
            # _is_arabic actually reads this field, keeping the old default
            # would silently REJECT every mixed Arabic/Latin message.
            MIN_ARABIC_CONFIDENCE=SecretManager.get_float("MIN_ARABIC_CONFIDENCE", 0.12, required=False),
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
            RETENTION_MESSAGES_DAYS=SecretManager.get_int("RETENTION_MESSAGES_DAYS", 7, required=False),
            RETENTION_AUDIT_DAYS=SecretManager.get_int("RETENTION_AUDIT_DAYS", 90, required=False),
            RETENTION_NOTIFICATIONS_DAYS=SecretManager.get_int("RETENTION_NOTIFICATIONS_DAYS", 30, required=False),
            # ========== Alert send behaviour ==========
            ATTACH_GROUP_PHOTO=SecretManager.get_bool("ATTACH_GROUP_PHOTO", False),
            # ========== Admin Bot ==========
            ADMIN_BOT_ENABLED=SecretManager.get_bool("ADMIN_BOT_ENABLED", False),
            ADMIN_BOT_TOKEN=SecretManager.get("ADMIN_BOT_TOKEN", None, required=False),
            # ========== التنبيهات ==========
            ALERT_WITH_BUTTONS=SecretManager.get_bool("ALERT_WITH_BUTTONS", True),
            ALERT_BUTTONS_ENABLED=SecretManager.get_bool("ALERT_BUTTONS_ENABLED", False),
            ALERT_FORWARD_FALLBACK=SecretManager.get_bool("ALERT_FORWARD_FALLBACK", True),
            TARGET_MEMBERSHIP_CHECK=SecretManager.get_bool("TARGET_MEMBERSHIP_CHECK", True),
            # ══ v10.8 Alert Bot ══
            ALERT_BOT_TOKEN=SecretManager.get("ALERT_BOT_TOKEN", None, required=False),
            ALERT_BOT_ENABLED=SecretManager.get_bool("ALERT_BOT_ENABLED", True),
            ALERT_BOT_TIMEOUT=SecretManager.get_float("ALERT_BOT_TIMEOUT", 12.0, required=False),
            ALERT_BOT_MAX_RETRIES=SecretManager.get_int("ALERT_BOT_MAX_RETRIES", 3, required=False),
            ALERT_SHOW_SCORE=SecretManager.get_bool("ALERT_SHOW_SCORE", True),
            ALERT_MAX_TEXT_LEN=SecretManager.get_int("ALERT_MAX_TEXT_LEN", 350, required=False),
            ALERT_WITH_COPY_BUTTON=SecretManager.get_bool("ALERT_WITH_COPY_BUTTON", True),
            ALERT_WITH_CONTACT_BUTTON=SecretManager.get_bool("ALERT_WITH_CONTACT_BUTTON", True),
            REPLY_FIX_ENABLED=SecretManager.get_bool("REPLY_FIX_ENABLED", True),
            REPLY_FIX_HOURLY_PER_USER=SecretManager.get_int("REPLY_FIX_HOURLY_PER_USER", 1, required=False),
            REPLY_FIX_DAILY_CAP=SecretManager.get_int("REPLY_FIX_DAILY_CAP", 100, required=False),
            REPLY_FIX_JITTER_MIN=SecretManager.get_float("REPLY_FIX_JITTER_MIN", 2.0, required=False),
            REPLY_FIX_JITTER_MAX=SecretManager.get_float("REPLY_FIX_JITTER_MAX", 5.0, required=False),
            # ========== مكافحة السبام ==========
            ANTISPAM_ENABLED=SecretManager.get_bool("ANTISPAM_ENABLED", True),
            RULE_ENGINE_ENABLED=SecretManager.get_bool("RULE_ENGINE_ENABLED", True),
            ALLOWLIST_ENABLED=SecretManager.get_bool("ALLOWLIST_ENABLED", True),
            ANTISPAM_WATCH_DURATION_SECONDS=SecretManager.get_int("ANTISPAM_WATCH_DURATION_SECONDS", 600, required=False),
            ANTISPAM_BURST_MESSAGES=SecretManager.get_int("ANTISPAM_BURST_MESSAGES", 2, required=False),
            ANTISPAM_BURST_WINDOW_SECONDS=SecretManager.get_int("ANTISPAM_BURST_WINDOW_SECONDS", 60, required=False),
            ANTISPAM_SIMILARITY_THRESHOLD=SecretManager.get_float("ANTISPAM_SIMILARITY_THRESHOLD", 0.80, required=False),
            ANTISPAM_SIMILARITY_WINDOW_SECONDS=SecretManager.get_int("ANTISPAM_SIMILARITY_WINDOW_SECONDS", 300, required=False),
            ANTISPAM_CATEGORY_WINDOW_SECONDS=SecretManager.get_int("ANTISPAM_CATEGORY_WINDOW_SECONDS", 300, required=False),
            ANTISPAM_DIRECT_SPAM_GROUPS=SecretManager.get_int("ANTISPAM_DIRECT_SPAM_GROUPS", 4, required=False),
            ANTISPAM_DIRECT_SPAM_WINDOW_SECONDS=SecretManager.get_int("ANTISPAM_DIRECT_SPAM_WINDOW_SECONDS", 300, required=False),
            ANTISPAM_CONFIRM_WINDOW_SECONDS=SecretManager.get_int("ANTISPAM_CONFIRM_WINDOW_SECONDS", 300, required=False),
            ANTISPAM_CONFIRM_MESSAGES=SecretManager.get_int("ANTISPAM_CONFIRM_MESSAGES", 5, required=False),
            ANTISPAM_CONFIRM_GROUPS=SecretManager.get_int("ANTISPAM_CONFIRM_GROUPS", 3, required=False),
            ANTISPAM_CONFIRM_ACTIVITY_WINDOW_SECONDS=SecretManager.get_int("ANTISPAM_CONFIRM_ACTIVITY_WINDOW_SECONDS", 600, required=False),
            ANTISPAM_CONFIRM_SPAM_INDICATOR_REQUIRED=SecretManager.get_bool("ANTISPAM_CONFIRM_SPAM_INDICATOR_REQUIRED", False),
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
            # ========== Fast Capture ==========
            FAST_CAPTURE_ENABLED=SecretManager.get_bool("FAST_CAPTURE_ENABLED", False),
            CAPTURE_BUFFER_SIZE=SecretManager.get_int("CAPTURE_BUFFER_SIZE", 1000, required=False),
            CAPTURE_TTL_SECONDS=SecretManager.get_int("CAPTURE_TTL_SECONDS", 30, required=False),
            # ========== Dedup ==========
            DEDUP_ENABLED=SecretManager.get_bool("DEDUP_ENABLED", True),
            DEDUP_WINDOW_SECONDS=SecretManager.get_int("DEDUP_WINDOW_SECONDS", 86400, required=False),
            DEDUP_RECOVERY_STEAL_MIN_AGE_SECONDS=SecretManager.get_int(
                "DEDUP_RECOVERY_STEAL_MIN_AGE_SECONDS", 120, required=False),
            # ========== Precision ==========
            CLAUSE_RESCORE_ENABLED=SecretManager.get_bool("CLAUSE_RESCORE_ENABLED", True),
            SIMILARITY_ENABLED=SecretManager.get_bool("SIMILARITY_ENABLED", True),
            SIMILARITY_THRESHOLD=SecretManager.get_float("SIMILARITY_THRESHOLD", 0.88, required=False),
            SIMILARITY_WINDOW_SECONDS=SecretManager.get_int("SIMILARITY_WINDOW_SECONDS", 3600, required=False),
            DIGEST_ENABLED=SecretManager.get_bool("DIGEST_ENABLED", True),
            DIGEST_HOUR_UTC=SecretManager.get_int("DIGEST_HOUR_UTC", 20, required=False),
            # ========== Sender Intelligence ==========
            SENDER_INTEL_ENABLED=SecretManager.get_bool("SENDER_INTEL_ENABLED", True),
            SENDER_ENTITY_CACHE_SIZE=SecretManager.get_int("SENDER_ENTITY_CACHE_SIZE", 2000, required=False),
            SENDER_ENTITY_CACHE_TTL=SecretManager.get_int("SENDER_ENTITY_CACHE_TTL", 900, required=False),
            SENDER_RESOLVE_MAX_RETRIES=SecretManager.get_int("SENDER_RESOLVE_MAX_RETRIES", 3, required=False),
            SENDER_RESOLVE_BACKOFF_BASE=SecretManager.get_float("SENDER_RESOLVE_BACKOFF_BASE", 0.5, required=False),
            SENDER_RESOLVE_BACKOFF_MAX=SecretManager.get_float("SENDER_RESOLVE_BACKOFF_MAX", 8.0, required=False),
            SENDER_FLOOD_MAX_SLEEP=SecretManager.get_int("SENDER_FLOOD_MAX_SLEEP", 5, required=False),
            SENDER_MENTION_FIX_ENABLED=SecretManager.get_bool("SENDER_MENTION_FIX_ENABLED", True),
            MENTION_NEG_TTL_SECONDS=SecretManager.get_int("MENTION_NEG_TTL_SECONDS", 300, required=False),
            MENTION_MAX_TIERS_PER_CANDIDATE=SecretManager.get_int("MENTION_MAX_TIERS_PER_CANDIDATE", 3, required=False),
            NAV_EXPORT_LINK_ENABLED=SecretManager.get_bool("NAV_EXPORT_LINK_ENABLED", True),
            NAV_USER_DISCOVERY_ENABLED=SecretManager.get_bool("NAV_USER_DISCOVERY_ENABLED", True),
            TELEGRAM_CATCH_UP=SecretManager.get_bool("TELEGRAM_CATCH_UP", True),
            QUEUE_EVENT_WAKE=SecretManager.get_bool("QUEUE_EVENT_WAKE", True),
            ALERT_LATENCY_LOG_EVERY=SecretManager.get_int("ALERT_LATENCY_LOG_EVERY", 100, required=False),
            WARMUP_CHAT_CACHE=SecretManager.get_bool("WARMUP_CHAT_CACHE", True),
            # ========== Prefilter ==========
            PREFILTER_ENABLED=SecretManager.get_bool("PREFILTER_ENABLED", True),
            PREFILTER_MIN_WORDS=SecretManager.get_int("PREFILTER_MIN_WORDS", 1, required=False),
            PREFILTER_MAX_EMOJIS=SecretManager.get_int("PREFILTER_MAX_EMOJIS", 5, required=False),
            # ========== المراقبة ==========
            PROMETHEUS_ENABLED=SecretManager.get_bool("PROMETHEUS_ENABLED", False),
            PROMETHEUS_PORT=SecretManager.get_int("PROMETHEUS_PORT", 9090, required=False),
            SENTRY_DSN=SecretManager.get("SENTRY_DSN", None, required=False),
            # ═══════════════════════════════════════════════════════════════
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
            # v14.5.1: effective cutoff consumed by filter_engine. Defaults
            # to float(FUZZY_MATCH_THRESHOLD) so the legacy int still drives
            # the actual matching when only that is set. Explicit env wins.
            FUZZY_SCORE_CUTOFF=SecretManager.get_float(
                "FUZZY_SCORE_CUTOFF", float(legacy_fuzzy), required=False
            ),
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
            # ========== v14.5.1 Execution / Regex Guard ==========
            REGEX_TIMEOUT_S=SecretManager.get_float("REGEX_TIMEOUT_S", 0.25, required=False),
            REGEX_GUARD_WORKERS=SecretManager.get_int("REGEX_GUARD_WORKERS", 4, required=False),
            FILTER_EXECUTOR_WORKERS=SecretManager.get_int("FILTER_EXECUTOR_WORKERS", 4, required=False),
            # ========== v14.5.1 Metrics ==========
            METRICS_WINDOW=SecretManager.get_int("METRICS_WINDOW", 2000, required=False),
            METRICS_Z_THRESHOLD=SecretManager.get_float("METRICS_Z_THRESHOLD", 3.5, required=False),
            METRICS_ENABLED=SecretManager.get_bool("METRICS_ENABLED", True),
            # ========== v14.5.1 Adaptive + Help anchor ==========
            ADAPTIVE_ALPHA=SecretManager.get_float("ADAPTIVE_ALPHA", 0.05, required=False),
            HELP_ANCHOR_INTENT_WEIGHT=SecretManager.get_float("HELP_ANCHOR_INTENT_WEIGHT", 0.65, required=False),
        )

        # v14.1 (تدقيق 2-a): fail-fast — REVIEW يجب أن يكون أدنى من ACCEPT حصراً.
        if not (0 < cfg.CONFIDENCE_REVIEW_THRESHOLD < cfg.CONFIDENCE_ACCEPT_THRESHOLD <= 1.0):
            raise ValueError(
                "CONFIDENCE_REVIEW_THRESHOLD must be > 0 and "
                "< CONFIDENCE_ACCEPT_THRESHOLD <= 1.0 "
                f"(got review={cfg.CONFIDENCE_REVIEW_THRESHOLD}, "
                f"accept={cfg.CONFIDENCE_ACCEPT_THRESHOLD})"
            )

        # v14.5.1: fail-fast on Arabic-ratio sanity — a value > 1.0 or
        # negative makes `_is_arabic` behave unpredictably.
        if not (0.0 < cfg.MIN_ARABIC_CONFIDENCE <= 1.0):
            raise ValueError(
                "MIN_ARABIC_CONFIDENCE must be in (0, 1] "
                f"(got {cfg.MIN_ARABIC_CONFIDENCE})"
            )

        cls._instance = cfg
        logger.info(
            f"Config v14.5.1 built: DB={db_type} | "
            f"PROCESSING_WORKERS={workers} | "
            f"FUZZY={'ON' if cfg.FUZZY_MATCHING_ENABLED else 'OFF'} | "
            f"NEGATION={'ON' if cfg.NEGATION_ENABLED else 'OFF'} | "
            f"AD_DETECTION={'ON' if cfg.AD_DETECTION_ENABLED else 'OFF'} | "
            f"METRICS={'ON' if cfg.METRICS_ENABLED else 'OFF'}"
        )
        return cfg

    @classmethod
    def reload(cls) -> _ConfigData:
        SecretManager.clear_cache()
        load_dotenv(PROJECT_DIR / "accounts.env", override=True)
        cls._instance = None
        return cls.build()

# =============================================================================
# Bot version — single source of truth
# =============================================================================
# v14.5.1: aligned with FILTER_ENGINE_VERSION. Other modules (monitors.py
# v9.7, etc.) may carry their own older version strings until their own
# correction pass lands; BOT_VERSION here tracks the config+filter batch.
BOT_VERSION: Final = "14.5.1"
BOT_CODENAME: Final = "No Dashboard"

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
# Keyword Loader v14.5.1 – متوافق مع keywords.json v14.0.x
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
    # v14.0.x Template-Driven Pattern Generation
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

        # Dedicated visibility for the template engine — a future regression
        # shows up immediately in boot logs.
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
                "session_string": SecretManager.get(f"{prefix}_SESSION_STRING", None, required=False),
                "priority": SecretManager.get_int(f"{prefix}_PRIORITY", default_priority, required=False),
                "is_main": is_main,
                "enabled": True,
                "retry_count": 0,
                "last_error": None,
            }
            accounts.append(acc)
            phone = str(acc["phone"])
            masked_phone = ("*" * max(0, len(phone) - 4)) + phone[-4:] if phone else ""
            logger.debug(
                f"Account loaded: {acc['name']} (phone={masked_phone}) "
                f"session_string={'YES' if acc['session_string'] else 'NO'}"
            )
        except (EnvironmentError, ValueError) as e:
            logger.error(f"Skipping account {prefix}: {e}")

    _try_load("MAIN", is_main=True, default_priority=10)

    for i in range(1, 21):
        prefix = f"ACCOUNT_{i}"
        api_id = SecretManager.get(f"{prefix}_API_ID", None, required=False)
        if api_id is None:
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

# =============================================================================
# Utility: fast_hash
# =============================================================================
def fast_hash(data: str) -> str:
    return hashlib.blake2b(data.encode("utf-8"), digest_size=16).hexdigest()

# =============================================================================
# الرسائل الجاهزة — زر «تواصل مع المرسل»
# =============================================================================
# v14.5.1: the operator's phone number is NO LONGER embedded here. Templates
# use a {phone} placeholder filled at load time from CONTACT_PHONE env var
# (accounts.env). The number previously in this file remains in git history
# and MUST be rotated by the operator — it is public.
DEFAULT_CONTACT_TEMPLATES: Final[List[str]] = [
    "السلام عليكم، إذا تبي أحد يسوي لك بحث، أرسل الموضوع وتفاصيل البحث وتبشر بأمور تبيض الوجه، والدفع بعد الإنجاز.",
    "السلام عليكم، إذا تبي أحد يسوي لك CV ثقة والدفع بعد الإنجاز: {phone}",
    "السلام عليكم، إذا تبي مختص يحل لك الواجبات: {phone}",
    "السلام عليكم، تبي مختص ثقة يحل واجبات: {phone}",
    "السلام عليكم، ما يهمك نسوي لك مشروعك والدفع بعد الإنجاز وشغل يبيض الوجه.",
]

CONTACT_TEMPLATE_LABELS: Final[List[str]] = ["بحث", "CV", "واجبات", "واجبات", "مشروع"]


def get_contact_templates() -> List[str]:
    """قائمة الرسائل الجاهزة (CONTACT_TEMPLATES_JSON override + CONTACT_PHONE)."""
    raw = (os.getenv("CONTACT_TEMPLATES_JSON") or "").strip()
    templates: List[str]
    if raw:
        try:
            import json as _json
            data = _json.loads(raw)
            if (
                isinstance(data, list)
                and data
                and all(isinstance(x, str) and x.strip() for x in data)
            ):
                templates = [x.strip() for x in data][:8]
            else:
                templates = list(DEFAULT_CONTACT_TEMPLATES)
        except Exception as e:
            logger.warning(f"CONTACT_TEMPLATES_JSON invalid, using defaults: {e}")
            templates = list(DEFAULT_CONTACT_TEMPLATES)
    else:
        templates = list(DEFAULT_CONTACT_TEMPLATES)

    # v14.5.1: fill {phone} placeholder from env.
    phone = (os.getenv("CONTACT_PHONE") or "").strip()
    if not phone and any("{phone}" in t for t in templates):
        logger.warning(
            "CONTACT_PHONE not set in accounts.env — contact templates will "
            "show '—' where a phone number is expected. Add "
            "CONTACT_PHONE=<number> to restore working contact links."
        )

    out: List[str] = []
    for t in templates:
        if "{phone}" in t:
            t = t.replace("{phone}", phone if phone else "—")
        out.append(t)
    return out


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

# =============================================================================
# Boot diagnostics
# =============================================================================
# v14.5.1: warn at boot if CONTACT_PHONE is unset — a one-line visibility
# win so operators can't ship a bot whose contact templates are empty
# by accident.
if not (os.getenv("CONTACT_PHONE") or "").strip():
    logger.warning(
        "CONTACT_PHONE not set — contact templates will show placeholder '—'. "
        "Set CONTACT_PHONE=<number> in accounts.env to restore live contact links."
    )

logger.info(
    f"config.py v14.5.1 loaded | Accounts: {len(ACCOUNTS)} | Workers: {CFG.PROCESSING_WORKERS} | "
    f"DB_URL: {'YES' if os.getenv('DATABASE_URL') else 'NO'} | "
    f"DB_SSL: {CFG.DATABASE_SSL or 'default'} | "
    f"INTENT_ENGINE: v14.5.1 (full correction batch)"
)
