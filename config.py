#!/usr/bin/env python3
"""
config.py – Configuration Manager v13.0 (IntentEngine-NLP Edition)
Supports: SQLite, PostgreSQL (via DATABASE_URL)
Compatible with: keywords.json v13.0.2, filter_engine.py v13.0
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Final, List, Optional, Tuple

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from loguru import logger

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
    """Parse PostgreSQL DATABASE_URL into connection parameters."""
    pattern = r"postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)$"
    match = re.match(pattern, url)
    if not match:
        pattern_no_port = r"postgresql://([^:]+):([^@]+)@([^/]+)/(.+)$"
        match = re.match(pattern_no_port, url)
        if not match:
            raise ValueError(f"Invalid DATABASE_URL format")
        return {
            "user": match.group(1),
            "password": match.group(2),
            "host": match.group(3),
            "port": 5432,
            "database": match.group(4),
        }
    return {
        "user": match.group(1),
        "password": match.group(2),
        "host": match.group(3),
        "port": int(match.group(4)),
        "database": match.group(5),
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
    logger.add(
        sys.stdout,
        level=level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green} | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
        enqueue=True,
    )
    logger.add(
        log_file,
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} | {message}",
        rotation=rotation,
        retention=retention,
        compression="gz",
        enqueue=True,
        backtrace=True,
        diagnose=True,
    )
    logger.info(f"Logging initialized (JSON: {JSON_AVAILABLE})")

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

            key_file = Path(".secret_key")
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
# Load .env
# =============================================================================
load_dotenv("accounts.env")

# =============================================================================
# Core Config Dataclass v13.0 – جميع المتغيرات الجديدة
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

    # ── Admin Bot ──
    ADMIN_BOT_ENABLED: bool
    ADMIN_BOT_TOKEN: Optional[str]

    # ── Alerts ──
    ALERT_WITH_BUTTONS: bool
    ALERT_SHOW_SCORE: bool
    ALERT_MAX_TEXT_LEN: int
    ALERT_WITH_COPY_BUTTON: bool

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

    # ── Prefilter ──
    PREFILTER_ENABLED: bool
    PREFILTER_MIN_WORDS: int
    PREFILTER_MAX_EMOJIS: int

    # ── Monitoring ──
    PROMETHEUS_ENABLED: bool
    PROMETHEUS_PORT: int
    SENTRY_DSN: Optional[str]

    # ═══════════════════════════════════════════════════════════════════
    # ── NEW v13.0: Confidence Thresholds ──
    CONFIDENCE_ACCEPT_THRESHOLD: float
    CONFIDENCE_REVIEW_THRESHOLD: float

    # ── NEW v13.0: Feature Toggles ──
    NEGATION_ENABLED: bool
    FUZZY_MATCHING_ENABLED: bool
    DISTANCE_SCORING_ENABLED: bool
    AD_DETECTION_ENABLED: bool

    # ── NEW v13.0: Fuzzy Matching ──
    FUZZY_MAX_EDIT_DISTANCE: int
    FUZZY_MIN_TOKEN_LENGTH: int
    FUZZY_SIMILARITY_THRESHOLD: float

    # ── NEW v13.0: Negation ──
    NEGATION_CLAUSE_BOUNDARIES_ENABLED: bool

    # ── NEW v13.0: Distance Thresholds ──
    DISTANCE_CLOSE_THRESHOLD: int
    DISTANCE_MEDIUM_THRESHOLD: int
    DISTANCE_FAR_THRESHOLD: int
    DISTANCE_VERY_FAR_THRESHOLD: int

    # ── NEW v13.0: Length Modifiers ──
    LENGTH_MODIFIER_SHORT: float
    LENGTH_MODIFIER_MEDIUM: float
    LENGTH_MODIFIER_LONG: float
    LENGTH_MODIFIER_VERY_LONG: float

    # ── NEW v13.0: Scoring Weights ──
    SCORE_WEIGHT_INTENT: float
    SCORE_WEIGHT_ACADEMIC: float
    SCORE_WEIGHT_GRAMMAR: float
    SCORE_WEIGHT_DISTANCE: float
    SCORE_WEIGHT_URGENCY: float
    SCORE_WEIGHT_CONTEXT: float

    # ── NEW v13.0: Ad Detection ──
    AD_WEAK_PROVIDER_THRESHOLD: int
    AD_EMOJI_THRESHOLD: int

    # ── NEW v13.0: Cache ──
    TEXT_CACHE_SIZE: int
    TEXT_CACHE_TTL: int

# =============================================================================
# Config Builder v13.0
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

        database_url = os.getenv("DATABASE_URL")
        if database_url:
            try:
                parsed = parse_database_url(database_url)
                db_host = parsed["host"]
                db_port = parsed["port"]
                db_name = parsed["database"]
                db_user = parsed["user"]
                db_password = parsed["password"]
                logger.info("Database config loaded from DATABASE_URL")
            except Exception as e:
                logger.error(f"Failed to parse DATABASE_URL: {e}, falling back to individual vars")
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
        cfg = _ConfigData(
            # ========== الأساسيات ==========
            TARGET_GROUP_ID=tg,
            ADMIN_CHAT_ID=admin,
            LOG_FILE=SecretManager.get("LOG_FILE", "bot.log", required=False),
            DB_TYPE=db_type,
            DB_FILE=SecretManager.get("DB_FILE", "telegram_bot.db", required=False),
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
            # ========== Admin Bot ==========
            ADMIN_BOT_ENABLED=SecretManager.get_bool("ADMIN_BOT_ENABLED", False),
            ADMIN_BOT_TOKEN=SecretManager.get("ADMIN_BOT_TOKEN", None, required=False),
            # ========== التنبيهات ==========
            ALERT_WITH_BUTTONS=SecretManager.get_bool("ALERT_WITH_BUTTONS", True),
            ALERT_SHOW_SCORE=SecretManager.get_bool("ALERT_SHOW_SCORE", True),
            ALERT_MAX_TEXT_LEN=SecretManager.get_int("ALERT_MAX_TEXT_LEN", 350, required=False),
            ALERT_WITH_COPY_BUTTON=SecretManager.get_bool("ALERT_WITH_COPY_BUTTON", True),
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
            # ========== Prefilter ==========
            PREFILTER_ENABLED=SecretManager.get_bool("PREFILTER_ENABLED", True),
            PREFILTER_MIN_WORDS=SecretManager.get_int("PREFILTER_MIN_WORDS", 2, required=False),
            PREFILTER_MAX_EMOJIS=SecretManager.get_int("PREFILTER_MAX_EMOJIS", 5, required=False),
            # ========== المراقبة ==========
            PROMETHEUS_ENABLED=SecretManager.get_bool("PROMETHEUS_ENABLED", False),
            PROMETHEUS_PORT=SecretManager.get_int("PROMETHEUS_PORT", 9090, required=False),
            SENTRY_DSN=SecretManager.get("SENTRY_DSN", None, required=False),
            # ═══════════════════════════════════════════════════════════════════
            # ========== NEW v13.0: عتبات الثقة ==========
            CONFIDENCE_ACCEPT_THRESHOLD=SecretManager.get_float("CONFIDENCE_ACCEPT_THRESHOLD", 0.65, required=False),
            CONFIDENCE_REVIEW_THRESHOLD=SecretManager.get_float("CONFIDENCE_REVIEW_THRESHOLD", 0.40, required=False),
            # ========== NEW v13.0: تشغيل/إيقاف الميزات ==========
            NEGATION_ENABLED=SecretManager.get_bool("NEGATION_ENABLED", True),
            FUZZY_MATCHING_ENABLED=SecretManager.get_bool("FUZZY_MATCHING_ENABLED", True),
            DISTANCE_SCORING_ENABLED=SecretManager.get_bool("DISTANCE_SCORING_ENABLED", True),
            AD_DETECTION_ENABLED=SecretManager.get_bool("AD_DETECTION_ENABLED", True),
            # ========== NEW v13.0: المطابقة الضبابية ==========
            FUZZY_MAX_EDIT_DISTANCE=SecretManager.get_int("FUZZY_MAX_EDIT_DISTANCE", 1, required=False),
            FUZZY_MIN_TOKEN_LENGTH=SecretManager.get_int("FUZZY_MIN_TOKEN_LENGTH", 5, required=False),
            FUZZY_SIMILARITY_THRESHOLD=SecretManager.get_float("FUZZY_SIMILARITY_THRESHOLD", 0.92, required=False),
            # ========== NEW v13.0: النفي ==========
            NEGATION_CLAUSE_BOUNDARIES_ENABLED=SecretManager.get_bool("NEGATION_CLAUSE_BOUNDARIES_ENABLED", True),
            # ========== NEW v13.0: المسافة ==========
            DISTANCE_CLOSE_THRESHOLD=SecretManager.get_int("DISTANCE_CLOSE_THRESHOLD", 3, required=False),
            DISTANCE_MEDIUM_THRESHOLD=SecretManager.get_int("DISTANCE_MEDIUM_THRESHOLD", 7, required=False),
            DISTANCE_FAR_THRESHOLD=SecretManager.get_int("DISTANCE_FAR_THRESHOLD", 10, required=False),
            DISTANCE_VERY_FAR_THRESHOLD=SecretManager.get_int("DISTANCE_VERY_FAR_THRESHOLD", 15, required=False),
            # ========== NEW v13.0: معامل الطول ==========
            LENGTH_MODIFIER_SHORT=SecretManager.get_float("LENGTH_MODIFIER_SHORT", 0.75, required=False),
            LENGTH_MODIFIER_MEDIUM=SecretManager.get_float("LENGTH_MODIFIER_MEDIUM", 0.9, required=False),
            LENGTH_MODIFIER_LONG=SecretManager.get_float("LENGTH_MODIFIER_LONG", 1.0, required=False),
            LENGTH_MODIFIER_VERY_LONG=SecretManager.get_float("LENGTH_MODIFIER_VERY_LONG", 0.9, required=False),
            # ========== NEW v13.0: أوزان التسجيل ==========
            SCORE_WEIGHT_INTENT=SecretManager.get_float("SCORE_WEIGHT_INTENT", 0.30, required=False),
            SCORE_WEIGHT_ACADEMIC=SecretManager.get_float("SCORE_WEIGHT_ACADEMIC", 0.25, required=False),
            SCORE_WEIGHT_GRAMMAR=SecretManager.get_float("SCORE_WEIGHT_GRAMMAR", 0.15, required=False),
            SCORE_WEIGHT_DISTANCE=SecretManager.get_float("SCORE_WEIGHT_DISTANCE", 0.15, required=False),
            SCORE_WEIGHT_URGENCY=SecretManager.get_float("SCORE_WEIGHT_URGENCY", 0.05, required=False),
            SCORE_WEIGHT_CONTEXT=SecretManager.get_float("SCORE_WEIGHT_CONTEXT", 0.10, required=False),
            # ========== NEW v13.0: كشف الإعلانات ==========
            AD_WEAK_PROVIDER_THRESHOLD=SecretManager.get_int("AD_WEAK_PROVIDER_THRESHOLD", 2, required=False),
            AD_EMOJI_THRESHOLD=SecretManager.get_int("AD_EMOJI_THRESHOLD", 3, required=False),
            # ========== NEW v13.0: الكاش ==========
            TEXT_CACHE_SIZE=SecretManager.get_int("TEXT_CACHE_SIZE", 5000, required=False),
            TEXT_CACHE_TTL=SecretManager.get_int("TEXT_CACHE_TTL", 300, required=False),
        )

        cls._instance = cfg
        logger.info(
            f"Config v13.0 built: DB={db_type} | "
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
        load_dotenv("accounts.env", override=True)
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
# Keyword Loader v13.0 – متوافق مع الهيكل الجديد
# =============================================================================
_KW_CATEGORIES: Final[List[str]] = [
    # الفئات القديمة (للتوافق)
    "request", "advertisement", "ignore", "emoji_advertisement",
    "education_providers", "ad_blockers", "request_context",
    "indirect_request", "urgency", "spam_patterns",
    # الفئات الجديدة v13.0
    "intent_verbs", "academic_objects", "request_phrases",
    "urgency_markers", "negation", "advertisement_signals",
    "spam_categories", "emoji_signals", "ignore_signals",
    "dialect_mapping", "university_context", "distance_scoring_config",
    "length_modifier", "scoring_weights", "explainability",
    "conversation_history_config", "test_cases", "learning_feedback",
    "high_confidence_boost_patterns", "action_verbs", "subject_markers",
    "implicit_request_patterns", "solve_actions", "help_expressions",
]

def load_keywords(path: str = "keywords.json") -> Dict[str, Any]:
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

    def _try_load(prefix: str, is_main: bool, default_priority: int) -> None:
        enabled_key = f"{prefix}_ENABLED"
        raw_enabled = SecretManager.get(enabled_key, "true", required=False) or "true"
        if raw_enabled.lower() == "false":
            logger.info(f"Skipping disabled account: {prefix}")
            return
        api_id_str = SecretManager.get(f"{prefix}_API_ID", None, required=False)
        if not api_id_str:
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
            logger.debug(
                f"Account loaded: {acc['name']} (phone={acc['phone']}) "
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
    f"config.py v13.0 loaded | Accounts: {len(ACCOUNTS)} | Workers: {CFG.PROCESSING_WORKERS} | "
    f"DB_URL: {'YES' if os.getenv('DATABASE_URL') else 'NO'} | "
    f"DASHBOARD: {'ON' if CFG.DASHBOARD_ENABLED else 'OFF'} | "
    f"INTENT_ENGINE: v13.0"
)
