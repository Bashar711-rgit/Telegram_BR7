#!/usr/bin/env python3
"""
database.py – Unified Async Database Layer v14.5.1 (HARDENED EDITION)
Supports: SQLite (aiosqlite) and PostgreSQL (asyncpg)
Compatible with: config.py v14.5.1, monitors.py v9.7, filter_engine.py v14.5.1

v14.5.1 (this pass) — database-layer correction pass:
  * CFG.DATABASE_SSL is now ACTUALLY consumed. _connect_postgresql() builds
    an ssl.SSLContext from the parsed sslmode (require/verify-ca/verify-full)
    and passes it to asyncpg.create_pool in BOTH code branches (DSN and
    discrete fields). Previously config.py parsed sslmode into DATABASE_SSL
    and it was silently dropped — SSL only worked by accident because
    asyncpg itself re-parses ?sslmode= in the DSN. That implicit path is
    version-fragile; this fix makes the connection secure regardless of
    asyncpg's internal behavior.
  * _flush(): SQLite branch had swapped UPDATE parameters — alerts_sent got
    incremented by the epoch timestamp while last_alert_time got the count.
    Data corruption on SQLite-only deployments. Reordered the SET clause to
    match the PG branch's semantics exactly.
  * _queue_size_mirror: decrements in ack_queue_event() (PG branch) and in
    recover_stale_inflight() dead-letter path were OUTSIDE _queue_lock —
    concurrent decrements raced, drifting the mirror until the next
    reconcile. Both are now guarded by _queue_lock.
  * is_blocked_sender() / is_blocked_chat(): the fallback query inside the
    except block re-ran the SAME query that just failed, propagating the
    exception to the caller. Now wrapped in try/except with fail-open
    (return False) on DB failure. This matches the "never block on error"
    intent of the primary path.
  * recent_chat_ids(): "SELECT DISTINCT chat_id ... ORDER BY id" is
    dialect-ambiguous (id not in SELECT list); SQLite and PostgreSQL may
    return different orderings. Replaced with explicit GROUP BY chat_id
    ORDER BY MAX(id) DESC — deterministic on both engines.
  * _upload_backup(): was loading the ENTIRE backup file into RAM
    (path.read_bytes()) before streaming. Now streams the file handle
    directly. Matters for large SQLite DBs; no behavior change for small
    ones.
  * _sqlite_hot_backup(): returned os.path.getsize(src) (source .db) rather
    than the actual backup file size (which includes checkpointed WAL).
    Now returns dst size. Cosmetic — the log line now reports the real size
    of what was copied.
  * _backup_loop(): the "no BACKUP_UPLOAD_URL" warning was logged every
    cycle (hourly noise). Now logged once at the first cycle, then at DEBUG.

v10.7 / v9.x fixes retained in full from the previous version — every
idempotent migration (BIGINT widening, sender_intel, sender_access,
intel entities, queue inflight columns), the CAS-based dead-letter writer,
the v12.1.0 claim/ack/recover queue semantics, exponential backoff on
flush failure, and every hardening note in the original header are all
preserved verbatim.

All existing public methods and their signatures are preserved exactly,
so monitors.py / main.py call sites require zero changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import time
import zlib
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from cachetools import TTLCache
from loguru import logger

from config import CFG, json_dumps, json_loads
from task_util import spawn_bg  # v14.5.1 (T4): بديل آمن من GC لـloop.create_task العارية

# =============================================================================
# Conditional imports
# =============================================================================
if CFG.DB_TYPE == "postgresql":
    try:
        import asyncpg
        PG_AVAILABLE = True
    except ImportError:
        PG_AVAILABLE = False
        logger.error("asyncpg not installed but DB_TYPE=postgresql.")
        raise
else:
    PG_AVAILABLE = False

if CFG.DB_TYPE == "sqlite":
    try:
        import aiosqlite
        SQLITE_AVAILABLE = True
    except ImportError:
        SQLITE_AVAILABLE = False
        logger.error("aiosqlite not installed but DB_TYPE=sqlite.")
        raise
else:
    SQLITE_AVAILABLE = False

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

# =============================================================================
# Runtime constants
# =============================================================================
_BACKUP_UPLOAD_URL: Optional[str] = os.getenv("BACKUP_UPLOAD_URL")
_BACKUP_UPLOAD_TOKEN: Optional[str] = os.getenv("BACKUP_UPLOAD_TOKEN")
_DB_BATCH_MAX_SIZE: int = int(os.getenv("DB_BATCH_MAX_SIZE", "500"))
_DEAD_LETTER_MAX_ROWS: int = int(os.getenv("DEAD_LETTER_MAX_ROWS", "5000"))
_PRESSURE_CHECK_EVERY: int = int(os.getenv("DB_PRESSURE_CHECK_EVERY", "12"))
_RULE_HITS_FLUSH_EVERY: int = max(1, int(os.getenv("DB_RULE_HITS_FLUSH_CYCLES", "6")))


# =============================================================================
# v14.5.1: SSL context builder — bridges CFG.DATABASE_SSL to asyncpg.
# PostgreSQL sslmode semantics → Python ssl.SSLContext:
#   require      → encrypt only, no cert verify, no hostname check
#   verify-ca    → encrypt + verify cert chain, no hostname check
#   verify-full  → encrypt + verify cert chain + verify hostname
# None / "" → no explicit context; asyncpg uses its own DSN handling.
# =============================================================================
def _build_ssl_context(sslmode: Optional[str]) -> Optional[Any]:
    """v14.5.1: build ssl.SSLContext from CFG.DATABASE_SSL.

    Returns None when sslmode is unset/unknown — caller then leaves the
    `ssl` kwarg out of create_pool() entirely, letting asyncpg use its
    own DSN parsing (identical to pre-v14.5.1 behavior).
    Never raises; a malformed context logs a warning and returns None.
    """
    if not sslmode:
        return None
    try:
        import ssl as _ssl
        mode = str(sslmode).strip().lower()
        if mode not in ("require", "verify-ca", "verify-full"):
            return None
        ctx = _ssl.create_default_context()
        if mode == "verify-full":
            # default context: check_hostname=True, verify_mode=CERT_REQUIRED
            pass
        elif mode == "verify-ca":
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_REQUIRED
        elif mode == "require":
            # PostgreSQL "require" is encrypt-only (no identity verification);
            # this is what Supabase/Render/Heroku actually expect.
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
        logger.debug(f"SSL context built for sslmode={mode}")
        return ctx
    except Exception as e:
        logger.warning(f"_build_ssl_context failed for sslmode={sslmode!r}: {e}")
        return None


# =============================================================================
# Dataclasses
# =============================================================================
@dataclass(slots=True)
class MessageRecord:
    message_hash: str
    chat_id: int
    sender_id: int
    message_text: str
    keyword_found: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    score: int = 0
    spam_score: float = 0.0


@dataclass(slots=True)
class AlertRecord:
    message_hash: str
    chat_id: int
    sender_id: int
    account_name: str
    keyword: str
    alert_text: str
    timestamp: float = field(default_factory=time.time)
    decision: str = "accept"
    confidence: float = 0.0
    reasons: str = ""
    intent_verb: Optional[str] = None
    academic_object: Optional[str] = None
    negation_detected: int = 0
    advert_score: float = 0.0
    contact_method: str = ""


@dataclass(slots=True)
class SenderProfile:
    sender_id: int
    access_hash: Optional[int] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    reputation_score: float = 0.0
    total_requests: int = 0
    valid_requests: int = 0
    last_alert_time: float = 0.0
    first_seen: float = field(default_factory=time.time)


@dataclass(slots=True)
class DeadLetterRecord:
    event_data: Dict[str, Any]
    error_text: str
    retry_count: int = 0
    resolved: bool = False
    timestamp: float = field(default_factory=time.time)


# =============================================================================
# SQL helpers
# =============================================================================
CRITICAL_EXPORT_TABLES: Dict[str, Tuple[str, ...]] = {
    "app_settings": ("key", "value"),
    "sources": ("chat_id", "username", "title", "type", "enabled", "added_by", "notes"),
    "rules": ("name", "conditions", "action", "action_value", "priority", "hits",
              "enabled", "created_by"),
    "allowed_entities": ("entity_type", "entity_id", "note"),
}


_IMPORT_COLUMN_DEFAULTS: Dict[str, Any] = {
    "enabled": 1, "priority": 100, "hits": 0, "action": "tag",
    "entity_type": "sender", "added_by": "import", "created_by": "import",
}


def _escape_like(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pg(sql: str) -> str:
    counter = 0
    result = []
    for ch in sql:
        if ch == "?":
            counter += 1
            result.append(f"${counter}")
        else:
            result.append(ch)
    return "".join(result)


# =============================================================================
# Ephemeral-storage warning
# =============================================================================
def _warn_ephemeral_storage() -> None:
    if CFG.DB_TYPE != "sqlite":
        return
    if os.getenv("DATABASE_URL"):
        return
    if _BACKUP_UPLOAD_URL:
        logger.info(
            "SQLite mode: external backup upload configured → "
            f"BACKUP_UPLOAD_URL={_BACKUP_UPLOAD_URL[:40]}…"
        )
        return

    msg = (
        "⚠️  EPHEMERAL STORAGE WARNING: DB_TYPE=sqlite with no DATABASE_URL "
        "and no BACKUP_UPLOAD_URL. All data (messages, alerts, queue, "
        "dead-letters) WILL BE LOST on Render instance restart/redeploy. "
        "Set DATABASE_URL (PostgreSQL) for persistence, or set "
        "BACKUP_UPLOAD_URL to enable external backup uploads."
    )

    require_persistent = os.getenv("REQUIRE_PERSISTENT_STORAGE", "").lower() in ("1", "true", "yes")
    allow_ephemeral = os.getenv("ALLOW_EPHEMERAL_SQLITE", "").lower() in ("1", "true", "yes")

    if require_persistent and not allow_ephemeral:
        logger.critical(
            "🛑 EPHEMERAL STORAGE: REQUIRE_PERSISTENT_STORAGE=1 is set but no "
            "DATABASE_URL/BACKUP_UPLOAD_URL is configured — refusing to start. "
            "Set DATABASE_URL (PostgreSQL) or BACKUP_UPLOAD_URL, or unset "
            "REQUIRE_PERSISTENT_STORAGE / set ALLOW_EPHEMERAL_SQLITE=1 to override."
        )
        raise RuntimeError(
            "Refusing to start: REQUIRE_PERSISTENT_STORAGE=1 with ephemeral "
            "SQLite storage and no backup target configured."
        )

    logger.critical(msg)


# =============================================================================
# Blocking I/O helpers
# =============================================================================
def _compress_bytes(data: bytes, level: int = 6) -> bytes:
    return zlib.compress(data, level)


def _decompress_bytes(data: bytes) -> bytes:
    return zlib.decompress(data)


def _sqlite_hot_backup(src_path: str, dst_path: str) -> int:
    """v14.5.1: return the ACTUAL backup file size (dst_path), not the source
    .db size. The source size excludes WAL content that gets checkpointed
    into the backup — logging it under-reports what was actually copied."""
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return os.path.getsize(dst_path)


def _compress_file_to_gz(src_path: str, dst_path: str, level: int = 6) -> Tuple[int, str]:
    data = Path(src_path).read_bytes()
    compressed = zlib.compress(data, level)
    Path(dst_path).write_bytes(compressed)
    digest = hashlib.sha256(compressed).hexdigest()
    return len(compressed), digest


def _verify_gz(path: str, expected_digest: str) -> bool:
    try:
        data = Path(path).read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_digest:
            return False
        zlib.decompress(data)
        return True
    except Exception:
        return False


# =============================================================================
# Unified Database Class
# =============================================================================
class EnhancedDatabase:
    def __init__(self) -> None:
        self.db_type = CFG.DB_TYPE
        self._pool: Any = None
        self._sqlite_conn: Optional[Any] = None
        self.is_connected = False

        self.message_cache: Deque[str] = deque(maxlen=min(CFG.MAX_CACHE_SIZE, 20_000))
        self._hash_lock = asyncio.Lock()

        self._queue_lock = asyncio.Lock()

        self._queue_size_mirror: int = 0
        self._queue_mirror_dirty: bool = False

        self._blocked_senders_cache: Dict[int, Tuple[bool, float]] = {}
        self._blocked_chats_cache: Dict[int, Tuple[bool, float]] = {}
        self._can_send_alert_cache: Dict[int, Tuple[bool, float]] = {}

        self._queue_notify: Optional[asyncio.Event] = None

        self.stats: Dict[str, int] = defaultdict(int)
        self._stats_lock = asyncio.Lock()
        self.start_time = time.time()

        self._batch: List[Tuple[Any, ...]] = []
        self._batch_lock = asyncio.Lock()
        self._flush_failure_count: int = 0
        self._flush_backoff: float = 1.0
        self._flush_backoff_max: float = 60.0
        self._db_healthy: bool = True

        self._writer_task: Optional[asyncio.Task] = None
        self._backup_task: Optional[asyncio.Task] = None

        self._sources_cache: Optional[Tuple[float, set]] = None
        self._rules_cache: Optional[Tuple[float, List[Dict[str, Any]]]] = None
        self._rule_hits_pending: Dict[int, int] = {}
        self._allowed_cache: Optional[Tuple[float, set, set]] = None
        self._features_cache: Dict[str, Tuple[float, bool]] = {}

        self._backup_failure_count: int = 0
        self._backup_failure_threshold: int = 3

        self._writer_cycle: int = 0

        # v14.5.1: per-session warning flag — _backup_loop only logs the
        # "no BACKUP_UPLOAD_URL" warning once, then at debug.
        self._backup_warning_logged: bool = False

    @property
    def db_healthy(self) -> bool:
        return self._db_healthy

    # ─── Connection ──────────────────────────────────────────────────────────
    async def connect(self) -> bool:
        try:
            _warn_ephemeral_storage()
            if self.db_type == "sqlite":
                await self._connect_sqlite()
            else:
                await self._connect_postgresql()
            await self._create_tables()
            await self._migrate_bigint_ids()
            await self._migrate_sender_intel()
            await self._migrate_sender_account_hashes()
            await self._migrate_sender_access()
            await self._migrate_intel_entities()
            await self._migrate_queue_inflight()
            await self._create_indexes()
            self.is_connected = True
            await self.start_writer()
            try:
                row = await self._fetchone("SELECT COUNT(*) AS cnt FROM processing_queue")
                async with self._queue_lock:
                    self._queue_size_mirror = int(row["cnt"]) if row else 0
                    self._queue_mirror_dirty = False
                logger.debug(f"queue_size_mirror primed: {self._queue_size_mirror}")
            except Exception as e:
                logger.debug(f"queue mirror prime failed: {e}")
                self._queue_mirror_dirty = True
            logger.info(f"Database connected: {self.db_type.upper()} v14.5.1")
            return True
        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            return False

    async def _connect_sqlite(self) -> None:
        self._sqlite_conn = await aiosqlite.connect(
            CFG.DB_FILE,
            timeout=60.0,
            isolation_level=None,
        )
        self._sqlite_conn.row_factory = aiosqlite.Row
        pragmas = [
            "PRAGMA journal_mode=WAL",
            "PRAGMA locking_mode=NORMAL",
            "PRAGMA busy_timeout=30000",
            "PRAGMA synchronous=NORMAL",
            f"PRAGMA cache_size={CFG.SQLITE_CACHE_SIZE}",
            "PRAGMA temp_store=MEMORY",
            "PRAGMA mmap_size=268435456",
            "PRAGMA page_size=4096",
            "PRAGMA foreign_keys=ON",
        ]
        for p in pragmas:
            await self._sqlite_conn.execute(p)

    async def _connect_postgresql(self) -> None:
        # v14.5.1: build SSL context from CFG.DATABASE_SSL and pass it in
        # BOTH branches. When sslmode is unset, we DON'T include the ssl
        # kwarg — asyncpg then applies its own DSN parsing (identical to
        # pre-v14.5.1 behavior).
        ssl_ctx = _build_ssl_context(getattr(CFG, "DATABASE_SSL", None))
        if ssl_ctx is not None:
            logger.info(f"PostgreSQL SSL enforced: sslmode={CFG.DATABASE_SSL}")

        dsn = os.getenv("DATABASE_URL")
        if dsn:
            connect_kwargs: Dict[str, Any] = {
                "dsn": dsn,
                "min_size": CFG.DB_POOL_MIN,
                "max_size": CFG.DB_POOL_MAX,
                "command_timeout": CFG.CONNECTION_TIMEOUT,
                "statement_cache_size": max(
                    0, int(os.getenv("DB_STATEMENT_CACHE_SIZE", "0") or 0)
                ),
                "server_settings": {
                    "statement_timeout": "30000",
                    "idle_in_transaction_session_timeout": "60000",
                },
            }
            if ssl_ctx is not None:
                connect_kwargs["ssl"] = ssl_ctx
            self._pool = await asyncpg.create_pool(**connect_kwargs)
        else:
            connect_kwargs = {
                "host": CFG.DB_HOST,
                "port": CFG.DB_PORT,
                "database": CFG.DB_NAME,
                "user": CFG.DB_USER,
                "password": CFG.DB_PASSWORD,
                "min_size": CFG.DB_POOL_MIN,
                "max_size": CFG.DB_POOL_MAX,
                "command_timeout": CFG.CONNECTION_TIMEOUT,
            }
            if ssl_ctx is not None:
                connect_kwargs["ssl"] = ssl_ctx
            self._pool = await asyncpg.create_pool(**connect_kwargs)

    async def _ping(self) -> bool:
        try:
            await self._fetchone("SELECT 1 AS ping")
            return True
        except Exception:
            return False

    async def _close_resources(self) -> None:
        current = asyncio.current_task()
        for name in ("_backup_task", "_writer_task"):
            task = getattr(self, name, None)
            if task and task is not current:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
            setattr(self, name, None)
        self.is_connected = False
        try:
            await self._flush()
        except Exception:
            pass
        try:
            if self.db_type == "sqlite" and self._sqlite_conn:
                await self._sqlite_conn.close()
            elif self.db_type == "postgresql" and self._pool:
                await self._pool.close()
        except Exception:
            pass
        self._sqlite_conn = None
        self._pool = None

    async def _reconnect(self) -> bool:
        logger.warning("Database reconnecting…")
        await self._close_resources()
        await asyncio.sleep(2)
        return await self.connect()

    # ─── SQL dialect helpers ────────────────────────────────────────────────
    def _sql_scalar_max(self, sql: str) -> str:
        try:
            if self.db_type == "postgresql":
                return sql.replace("MAX(COALESCE(", "GREATEST(COALESCE(")
        except Exception:
            pass
        return sql

    def _sql_insert_ignore(self, sql: str) -> str:
        try:
            if self.db_type == "postgresql" and "INSERT OR IGNORE INTO" in sql:
                converted = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO", 1).rstrip()
                if "ON CONFLICT" not in converted.upper():
                    return converted + " ON CONFLICT DO NOTHING"
                return converted
        except Exception:
            pass
        return sql

    # ─── Low-level exec ─────────────────────────────────────────────────────
    async def _execute(self, sql: str, params: tuple = ()) -> Any:
        if self.db_type == "sqlite":
            return await self._sqlite_conn.execute(sql, params)
        else:
            return await self._pool.execute(_pg(sql), *params)

    async def _execute_affected(self, sql: str, params: tuple = ()) -> int:
        try:
            if self.db_type == "sqlite":
                cursor = await self._sqlite_conn.execute(sql, params)
                return int(cursor.rowcount or 0)
            tag = await self._pool.execute(_pg(sql), *params)
            try:
                return int(str(tag).split()[-1])
            except Exception:
                return 0
        except Exception:
            return 0

    async def _dead_letter_row(self, rid: Any, attempts: int, raw: Any, err: str) -> bool:
        payload = raw if isinstance(raw, (str, bytes)) else json_dumps(raw or {})
        retry_count = int(attempts)
        claim_sql = (
            "UPDATE processing_queue SET attempts = attempts + 1 "
            "WHERE id = ? AND status = 'in_flight' AND attempts = ?"
        )
        insert_sql = (
            "INSERT INTO dead_letters (event_data, error_text, retry_count) "
            "VALUES (?, ?, ?)"
        )
        delete_sql = "DELETE FROM processing_queue WHERE id = ? AND status = 'in_flight'"
        try:
            if self.db_type == "sqlite":
                async with self._queue_lock:
                    cursor = await self._sqlite_conn.execute(claim_sql, (rid, attempts))
                    if not (cursor.rowcount or 0):
                        return False
                    await self._sqlite_conn.execute(insert_sql, (payload, err, retry_count))
                    await self._sqlite_conn.execute(delete_sql, (rid,))
                    await self._sqlite_conn.commit()
                return True
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    tag = str(await conn.execute(_pg(claim_sql), rid, attempts))
                    won = tag.startswith("UPDATE") and tag.split()[-1] == "1"
                    if not won:
                        return False
                    await conn.execute(_pg(insert_sql), payload, err, retry_count)
                    await conn.execute(_pg(delete_sql), rid)
            return True
        except Exception as e:
            logger.debug(
                f"_dead_letter_row {rid} failed (no DL written, row stays "
                f"in_flight): {type(e).__name__}: {e}"
            )
            return False

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        if self.db_type == "sqlite":
            cursor = await self._sqlite_conn.execute(sql, params)
            row = await cursor.fetchone()
            return dict(row) if row else None
        else:
            row = await self._pool.fetchrow(_pg(sql), *params)
            return dict(row) if row else None

    async def _fetchall(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        if self.db_type == "sqlite":
            cursor = await self._sqlite_conn.execute(sql, params)
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        else:
            rows = await self._pool.fetch(_pg(sql), *params)
            return [dict(r) for r in rows]

    async def _executemany(self, sql: str, params_list: List[tuple]) -> None:
        if self.db_type == "sqlite":
            await self._sqlite_conn.executemany(sql, params_list)
        else:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    for params in params_list:
                        await conn.execute(_pg(sql), *params)

    async def _commit(self) -> None:
        if self.db_type == "sqlite":
            await self._sqlite_conn.commit()

    # ─── Schema ─────────────────────────────────────────────────────────────
    async def _create_tables(self) -> None:
        stmts = """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_hash TEXT UNIQUE NOT NULL,
                chat_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                message_text TEXT,
                keyword_found TEXT,
                score INTEGER DEFAULT 0,
                spam_score REAL DEFAULT 0.0,
                timestamp REAL NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_hash TEXT UNIQUE NOT NULL,
                chat_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                account_name TEXT NOT NULL,
                keyword TEXT NOT NULL,
                alert_text TEXT,
                timestamp REAL NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                decision TEXT DEFAULT 'accept',
                confidence REAL DEFAULT 0.0,
                reasons TEXT DEFAULT '',
                intent_verb TEXT,
                academic_object TEXT,
                negation_detected INTEGER DEFAULT 0,
                advert_score REAL DEFAULT 0.0,
                contact_method TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS sender_stats (
                sender_id INTEGER PRIMARY KEY,
                total_messages INTEGER DEFAULT 0,
                alerts_sent INTEGER DEFAULT 0,
                valid_requests INTEGER DEFAULT 0,
                invalid_requests INTEGER DEFAULT 0,
                reputation_score REAL DEFAULT 50.0,
                last_alert_time REAL,
                first_seen REAL NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sender_contacts (
                sender_id INTEGER PRIMARY KEY,
                access_hash INTEGER,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                last_chat_id INTEGER,
                last_message_id INTEGER,
                last_message_link TEXT,
                last_group_link TEXT,
                phone TEXT,
                is_bot BOOLEAN DEFAULT FALSE,
                is_verified BOOLEAN DEFAULT FALSE,
                is_premium BOOLEAN DEFAULT FALSE,
                is_scam BOOLEAN DEFAULT FALSE,
                is_fake BOOLEAN DEFAULT FALSE,
                is_restricted BOOLEAN DEFAULT FALSE,
                is_deleted BOOLEAN DEFAULT FALSE,
                is_contact BOOLEAN DEFAULT FALSE,
                is_mutual_contact BOOLEAN DEFAULT FALSE,
                photo_available BOOLEAN,
                restriction_reason TEXT,
                lang_code TEXT,
                status TEXT,
                usernames TEXT,
                last_seen REAL,
                last_updated REAL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS system_health (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                component TEXT NOT NULL,
                status TEXT NOT NULL,
                details TEXT,
                timestamp REAL DEFAULT (unixepoch())
            );
            CREATE TABLE IF NOT EXISTS blocked_senders (
                sender_id INTEGER PRIMARY KEY,
                reason TEXT,
                blocked_by TEXT DEFAULT 'system',
                blocked_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS blocked_chats (
                chat_id INTEGER PRIMARY KEY,
                reason TEXT,
                blocked_by TEXT DEFAULT 'system',
                blocked_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS processing_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_data TEXT NOT NULL,
                priority INTEGER DEFAULT 5,
                status TEXT DEFAULT 'pending',
                claimed_at REAL,
                attempts INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS dead_letters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_data TEXT NOT NULL,
                error_text TEXT NOT NULL,
                retry_count INTEGER DEFAULT 0,
                resolved INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS alert_dedup (
                fingerprint TEXT PRIMARY KEY,
                sender_id INTEGER,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                hits INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS spam_watch (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                reason TEXT,
                evidence TEXT,
                watch_until REAL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS spam_ignore (
                sender_id INTEGER PRIMARY KEY,
                reason TEXT,
                evidence TEXT,
                classified TEXT DEFAULT 'cross_group_spam',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL UNIQUE,
                username TEXT DEFAULT '',
                title TEXT DEFAULT '',
                type TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                added_by TEXT DEFAULT 'panel',
                notes TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT DEFAULT '',
                conditions TEXT NOT NULL DEFAULT '{}',
                action TEXT NOT NULL DEFAULT 'tag',
                action_value TEXT DEFAULT '',
                priority INTEGER DEFAULT 100,
                hits INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                created_by TEXT DEFAULT 'panel',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS allowed_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL DEFAULT 'sender',
                entity_id INTEGER NOT NULL,
                note TEXT DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(entity_type, entity_id)
            );
            CREATE TABLE IF NOT EXISTS features (
                key TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_by TEXT DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ntype TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                body TEXT DEFAULT '',
                severity TEXT DEFAULT 'info',
                object_type TEXT DEFAULT '',
                object_id TEXT DEFAULT '',
                is_read INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                object_type TEXT,
                object_id TEXT,
                old_value TEXT,
                new_value TEXT,
                source TEXT DEFAULT 'botpanel',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """
        for stmt in stmts.split(";"):
            s = stmt.strip()
            if s:
                if self.db_type == "postgresql":
                    s = (
                        s.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
                        .replace("sender_id INTEGER PRIMARY KEY", "sender_id BIGINT PRIMARY KEY")
                        .replace("chat_id INTEGER PRIMARY KEY", "chat_id BIGINT PRIMARY KEY")
                        .replace("sender_id INTEGER,", "sender_id BIGINT,")
                        .replace("chat_id INTEGER,", "chat_id BIGINT,")
                        .replace("INTEGER NOT NULL", "BIGINT NOT NULL")
                        .replace("access_hash INTEGER,", "access_hash BIGINT,")
                        .replace("last_chat_id INTEGER,", "last_chat_id BIGINT,")
                        .replace("last_message_id INTEGER,", "last_message_id BIGINT,")
                        .replace("last_seen REAL,", "last_seen DOUBLE PRECISION,")
                        .replace("last_updated REAL,", "last_updated DOUBLE PRECISION,")
                        .replace("REAL DEFAULT (unixepoch())", "DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())")
                        .replace("DATETIME DEFAULT CURRENT_TIMESTAMP", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                        .replace("DATETIME", "TIMESTAMP")
                        .replace("REAL NOT NULL", "DOUBLE PRECISION NOT NULL")
                        .replace("REAL DEFAULT", "DOUBLE PRECISION DEFAULT")
                        .replace("REAL,", "DOUBLE PRECISION,")
                    )
                try:
                    await self._execute(s)
                except Exception as stmt_err:
                    logger.error(f"_create_tables statement failed ({self.db_type}): {type(stmt_err).__name__}: {str(stmt_err)[:200]} | stmt={s[:120]}")
        await self._commit()

    async def _migrate_bigint_ids(self) -> None:
        if self.db_type != "postgresql":
            return
        stmts = [
            "ALTER TABLE sender_stats    ALTER COLUMN sender_id       TYPE BIGINT",
            "ALTER TABLE sender_contacts ALTER COLUMN sender_id       TYPE BIGINT",
            "ALTER TABLE sender_contacts ALTER COLUMN access_hash     TYPE BIGINT",
            "ALTER TABLE sender_contacts ALTER COLUMN last_chat_id    TYPE BIGINT",
            "ALTER TABLE sender_contacts ALTER COLUMN last_message_id TYPE BIGINT",
            "ALTER TABLE blocked_senders ALTER COLUMN sender_id       TYPE BIGINT",
            "ALTER TABLE blocked_chats   ALTER COLUMN chat_id         TYPE BIGINT",
            "ALTER TABLE messages        ALTER COLUMN chat_id         TYPE BIGINT",
            "ALTER TABLE messages        ALTER COLUMN sender_id       TYPE BIGINT",
            "ALTER TABLE alerts          ALTER COLUMN chat_id         TYPE BIGINT",
            "ALTER TABLE alerts          ALTER COLUMN sender_id       TYPE BIGINT",
            "ALTER TABLE alert_dedup     ALTER COLUMN sender_id       TYPE BIGINT",
            "ALTER TABLE spam_watch      ALTER COLUMN sender_id       TYPE BIGINT",
            "ALTER TABLE spam_ignore     ALTER COLUMN sender_id       TYPE BIGINT",
        ]
        applied = 0
        failed: list = []
        for st in stmts:
            table = st.split()[2]
            column = st.split("ALTER COLUMN ")[1].split()[0]
            for attempt in (1, 2):
                try:
                    await self._execute(st)
                    applied += 1
                    break
                except Exception as e:
                    if attempt == 1:
                        await asyncio.sleep(2.0)
                        continue
                    failed.append((table, column))
                    logger.error(
                        f"bigint migration FAILED: {table}.{column} stays int4 → "
                        f"writes of Telegram 64-bit IDs will raise DataError "
                        f"(value out of int32 range): {type(e).__name__}: {str(e)[:200]}"
                    )
        await self._commit()
        logger.info(
            f"Database migration v9.11: Telegram-ID columns widened to BIGINT "
            f"({applied}/{len(stmts)} applied, {len(failed)} failed)"
        )
        await self._verify_bigint_columns(failed)

    async def _verify_bigint_columns(self, failed: list) -> None:
        if self.db_type != "postgresql":
            return
        columns = [
            ("messages", "chat_id"), ("messages", "sender_id"),
            ("alerts", "chat_id"), ("alerts", "sender_id"),
            ("sender_stats", "sender_id"),
            ("sender_contacts", "sender_id"), ("sender_contacts", "access_hash"),
            ("sender_contacts", "last_chat_id"), ("sender_contacts", "last_message_id"),
            ("blocked_senders", "sender_id"), ("blocked_chats", "chat_id"),
            ("alert_dedup", "sender_id"),
            ("spam_watch", "sender_id"), ("spam_ignore", "sender_id"),
        ]
        try:
            still_int4 = []
            for table, column in columns:
                try:
                    row = await self._fetchone(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_name = ? AND column_name = ?",
                        (table, column),
                    )
                    dtype = (row.get("data_type") if row else None) or ""
                    if dtype and dtype.lower() in ("integer", "smallint", "int4", "int2"):
                        still_int4.append(f"{table}.{column}({dtype})")
                except Exception:
                    continue
            if still_int4:
                logger.critical(
                    "⚠️ INT32 OVERFLOW RISK — هذه الأعمدة ما زالت int4 وستفشل كتابة "
                    "معرفات تيليجرام 64-بت (DataError: value out of int32 range): "
                    + ", ".join(still_int4)
                    + " — غالباً قفل من عملية أخرى؛ سيتكرر المحاولة عند الإقلاع القادم."
                )
            else:
                logger.info(
                    f"bigint verify OK: all {len(columns)} Telegram-ID columns are 64-bit safe"
                )
        except Exception as e:
            logger.debug(f"bigint verify skipped: {e}")

    _SENDER_INTEL_COLUMNS = [
        ("phone", "TEXT"),
        ("is_bot", "BOOLEAN DEFAULT FALSE"),
        ("is_verified", "BOOLEAN DEFAULT FALSE"),
        ("is_premium", "BOOLEAN DEFAULT FALSE"),
        ("is_scam", "BOOLEAN DEFAULT FALSE"),
        ("is_fake", "BOOLEAN DEFAULT FALSE"),
        ("is_restricted", "BOOLEAN DEFAULT FALSE"),
        ("is_deleted", "BOOLEAN DEFAULT FALSE"),
        ("is_contact", "BOOLEAN DEFAULT FALSE"),
        ("is_mutual_contact", "BOOLEAN DEFAULT FALSE"),
        ("photo_available", "BOOLEAN"),
        ("restriction_reason", "TEXT"),
        ("lang_code", "TEXT"),
        ("status", "TEXT"),
        ("usernames", "TEXT"),
        ("last_seen", "DOUBLE PRECISION"),
        ("last_updated", "DOUBLE PRECISION"),
    ]

    async def _migrate_sender_intel(self) -> None:
        table = "sender_contacts"
        existing: set = set()
        if self.db_type == "postgresql":
            rows = await self._fetchall(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = ?",
                (table,),
            )
            existing = {r["column_name"] for r in rows}
        else:
            rows = await self._fetchall(f"PRAGMA table_info({table})", ())
            existing = {r["name"] for r in rows}

        added = 0
        for name, decl in self._SENDER_INTEL_COLUMNS:
            if name in existing:
                continue
            try:
                if self.db_type == "postgresql":
                    await self._execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {decl}")
                else:
                    sqlite_decl = decl.replace("DOUBLE PRECISION", "REAL")
                    await self._execute(f"ALTER TABLE {table} ADD COLUMN {name} {sqlite_decl}")
                added += 1
            except Exception as e:
                logger.warning(
                    f"sender-intel migration: {table}.{name} skipped: "
                    f"{type(e).__name__}: {str(e)[:120]}"
                )
        await self._commit()
        if added:
            logger.info(f"Database migration v9.3: sender_contacts enriched (+{added} columns)")

    async def _create_indexes(self) -> None:
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_msg_hash     ON messages(message_hash)",
            "CREATE INDEX IF NOT EXISTS idx_msg_sender   ON messages(sender_id)",
            "CREATE INDEX IF NOT EXISTS idx_msg_chat     ON messages(chat_id)",
            "CREATE INDEX IF NOT EXISTS idx_msg_time     ON messages(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_alr_time     ON alerts(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_alr_sender   ON alerts(sender_id)",
            "CREATE INDEX IF NOT EXISTS idx_snd_rep      ON sender_stats(reputation_score)",
            "CREATE INDEX IF NOT EXISTS idx_queue_prio   ON processing_queue(priority DESC, created_at ASC)",
            "CREATE INDEX IF NOT EXISTS idx_dead_resolved ON dead_letters(resolved)",
            "CREATE INDEX IF NOT EXISTS idx_dead_created  ON dead_letters(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_alr_keyword   ON alerts(keyword)",
            "CREATE INDEX IF NOT EXISTS idx_alr_account   ON alerts(account_name)",
            "CREATE INDEX IF NOT EXISTS idx_msg_keyword   ON messages(keyword_found)",
            "CREATE INDEX IF NOT EXISTS idx_msg_sender_time ON messages(sender_id, timestamp DESC)",
            "CREATE INDEX IF NOT EXISTS idx_alr_time_sender ON alerts(timestamp DESC, sender_id)",
            "CREATE INDEX IF NOT EXISTS idx_alr_decision  ON alerts(decision)",
            "CREATE INDEX IF NOT EXISTS idx_alr_confidence ON alerts(confidence)",
            "CREATE INDEX IF NOT EXISTS idx_dedup_first_seen ON alert_dedup(first_seen)",
            "CREATE INDEX IF NOT EXISTS idx_dedup_sender     ON alert_dedup(sender_id)",
            "CREATE INDEX IF NOT EXISTS idx_watch_sender_time ON spam_watch(sender_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_watch_until       ON spam_watch(watch_until)",
            "CREATE INDEX IF NOT EXISTS idx_audit_time   ON audit_logs(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action)",
            "CREATE INDEX IF NOT EXISTS idx_audit_actor  ON audit_logs(actor)",
            "CREATE INDEX IF NOT EXISTS idx_sources_enabled ON sources(enabled)",
            "CREATE INDEX IF NOT EXISTS idx_rules_eval ON rules(enabled, priority, id)",
            "CREATE INDEX IF NOT EXISTS idx_allowed_lookup ON allowed_entities(entity_type, entity_id)",
            "CREATE INDEX IF NOT EXISTS idx_notif_unread_time ON notifications(is_read, created_at DESC)",
        ]
        for idx in indexes:
            await self._execute(idx)
        await self._commit()

    # ─── Queue ─────────────────────────────────────────────────────────────
    @property
    def queue_notify(self) -> asyncio.Event:
        ev = self._queue_notify
        if ev is None:
            ev = asyncio.Event()
            self._queue_notify = ev
        return ev

    def _notify_queue_wakeup(self) -> None:
        try:
            self.queue_notify.set()
        except Exception:
            pass

    async def add_to_queue(self, event_data: dict, priority: int = 5) -> int:
        if not self._db_healthy:
            logger.debug("add_to_queue: DB unhealthy, returning -2 (backpressure)")
            return -2

        try:
            async with self._queue_lock:
                reconcile_every = max(1, int(os.getenv("QUEUE_MIRROR_RECONCILE_EVERY", "64")))
                if self._queue_mirror_dirty or (self.stats.get("queue_inserts", 0) % reconcile_every == 0):
                    try:
                        row = await self._fetchone("SELECT COUNT(*) AS cnt FROM processing_queue")
                        self._queue_size_mirror = int(row["cnt"]) if row else 0
                        self._queue_mirror_dirty = False
                    except Exception as e:
                        logger.debug(f"queue mirror reconcile failed: {e}")
                current_size = self._queue_size_mirror

                if self.db_type == "sqlite":
                    if current_size >= CFG.MESSAGE_QUEUE_SIZE:
                        await self._execute(
                            "DELETE FROM processing_queue WHERE id = ("
                            "  SELECT id FROM processing_queue"
                            "  ORDER BY priority ASC, created_at ASC LIMIT 1"
                            ")"
                        )
                        self._queue_size_mirror = max(0, self._queue_size_mirror - 1)
                        async with self._stats_lock:
                            self.stats["queue_evictions"] += 1
                        logger.warning(
                            f"processing_queue at capacity ({CFG.MESSAGE_QUEUE_SIZE}), "
                            "evicted oldest/lowest-priority row (DROP_OLDEST policy)"
                        )

                    cursor = await self._execute(
                        "INSERT INTO processing_queue (event_data, priority) VALUES (?, ?)",
                        (json_dumps(event_data), priority),
                    )
                    await self._commit()
                    self._queue_size_mirror += 1
                    async with self._stats_lock:
                        self.stats["queue_inserts"] = self.stats.get("queue_inserts", 0) + 1
                    self._notify_queue_wakeup()
                    return cursor.lastrowid

                else:
                    async with self._pool.acquire() as conn:
                        async with conn.transaction():
                            if current_size >= CFG.MESSAGE_QUEUE_SIZE:
                                await conn.execute(
                                    "DELETE FROM processing_queue WHERE id = ("
                                    "  SELECT id FROM processing_queue"
                                    "  ORDER BY priority ASC, created_at ASC LIMIT 1"
                                    ")"
                                )
                                async with self._stats_lock:
                                    self.stats["queue_evictions"] += 1
                                logger.warning(
                                    f"processing_queue at capacity ({CFG.MESSAGE_QUEUE_SIZE}), "
                                    "evicted oldest/lowest-priority row"
                                )
                            result = await conn.fetchrow(
                                "INSERT INTO processing_queue (event_data, priority) "
                                "VALUES ($1, $2) RETURNING id",
                                json_dumps(event_data), priority,
                            )
                            self._queue_size_mirror += 1
                            async with self._stats_lock:
                                self.stats["queue_inserts"] = self.stats.get("queue_inserts", 0) + 1
                            self._notify_queue_wakeup()
                            return result["id"]

        except Exception as e:
            logger.error(f"add_to_queue failed: {e}")
            self._queue_mirror_dirty = True
            return -1

    _QUEUE_INFLIGHT_COLUMNS = [
        ("status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("claimed_at", "DOUBLE PRECISION"),
        ("attempts", "INTEGER DEFAULT 0"),
    ]

    async def _migrate_queue_inflight(self) -> None:
        table = "processing_queue"
        existing: set = set()
        if self.db_type == "postgresql":
            rows = await self._fetchall(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = ?",
                (table,),
            )
            existing = {r["column_name"] for r in rows}
        else:
            rows = await self._fetchall(f"PRAGMA table_info({table})", ())
            existing = {r["name"] for r in rows}
        added = 0
        for name, decl in self._QUEUE_INFLIGHT_COLUMNS:
            if name in existing:
                continue
            try:
                if self.db_type == "postgresql":
                    await self._execute(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {decl}"
                    )
                else:
                    sqlite_decl = decl.replace("DOUBLE PRECISION", "REAL")
                    await self._execute(f"ALTER TABLE {table} ADD COLUMN {name} {sqlite_decl}")
                added += 1
            except Exception as e:
                logger.warning(
                    f"queue-inflight migration: {table}.{name} skipped: "
                    f"{type(e).__name__}: {str(e)[:120]}"
                )
        await self._commit()
        if added:
            logger.info(
                f"Database migration v12.1.0: processing_queue inflight (+{added} columns)"
            )

    @staticmethod
    def _claim_payload(event_id: Any, raw: Any) -> dict:
        try:
            if isinstance(raw, (str, bytes)):
                data = json_loads(raw)
            elif isinstance(raw, dict):
                data = dict(raw)
            else:
                data = {}
        except Exception:
            data = {}
        try:
            if isinstance(data, dict):
                data.setdefault("_queue_id", int(event_id))
        except Exception:
            pass
        return data if isinstance(data, dict) else {}

    async def pop_from_queue(self) -> Optional[dict]:
        try:
            now_ts = time.time()
            if self.db_type == "sqlite":
                async with self._queue_lock:
                    cursor = await self._sqlite_conn.execute(
                        "SELECT id, event_data FROM processing_queue "
                        "WHERE status IS NULL OR status != 'in_flight' "
                        "ORDER BY priority DESC, created_at ASC LIMIT 1"
                    )
                    row = await cursor.fetchone()
                    if not row:
                        return None
                    await self._sqlite_conn.execute(
                        "UPDATE processing_queue SET status = 'in_flight', "
                        "claimed_at = ?, attempts = attempts + 1 WHERE id = ?",
                        (now_ts, row[0]),
                    )
                    await self._sqlite_conn.commit()
                return self._claim_payload(row[0], row[1])
            else:
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        row = await conn.fetchrow(
                            "SELECT id, event_data FROM processing_queue "
                            "WHERE (status IS NULL OR status <> 'in_flight') "
                            "ORDER BY priority DESC, created_at ASC "
                            "LIMIT 1 FOR UPDATE SKIP LOCKED"
                        )
                        if not row:
                            return None
                        await conn.execute(
                            "UPDATE processing_queue SET status = 'in_flight', "
                            "claimed_at = $1, attempts = attempts + 1 WHERE id = $2",
                            now_ts, row["id"],
                        )
                return self._claim_payload(row["id"], row["event_data"])
        except Exception as e:
            logger.error(f"pop_from_queue failed: {e}")
            self._queue_mirror_dirty = True
            return None

    async def ack_queue_event(self, event_id: Any) -> bool:
        try:
            eid = int(event_id)
        except Exception:
            return False
        try:
            if self.db_type == "sqlite":
                async with self._queue_lock:
                    cursor = await self._sqlite_conn.execute(
                        "DELETE FROM processing_queue WHERE id = ?", (eid,)
                    )
                    deleted = cursor.rowcount or 0
                    await self._sqlite_conn.commit()
                    if deleted:
                        self._queue_size_mirror = max(0, self._queue_size_mirror - 1)
                return bool(deleted)
            else:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "DELETE FROM processing_queue WHERE id = $1 RETURNING id", eid
                    )
                # v14.5.1: mirror decrement moved INSIDE _queue_lock — two
                # concurrent acks used to read N, both decrement, drift N-2.
                if row:
                    async with self._queue_lock:
                        self._queue_size_mirror = max(0, self._queue_size_mirror - 1)
                return bool(row)
        except Exception as e:
            from unified_alerts.stats import get_unified_stats as _gus
            _gus().inc("inflight_errors")
            logger.debug(f"ack_queue_event({event_id}) failed: {type(e).__name__}: {e}")
            self._queue_mirror_dirty = True
            return False

    async def queue_size(self) -> int:
        try:
            async with self._queue_lock:
                if not self._queue_mirror_dirty:
                    return self._queue_size_mirror
            row = await self._fetchone("SELECT COUNT(*) AS cnt FROM processing_queue")
            return int(row["cnt"]) if row else 0
        except Exception:
            return 0

    async def recover_stale_inflight(self, max_age_seconds: int = 600) -> Dict[str, int]:
        stats_out = {"recovered": 0, "dead": 0}
        try:
            try:
                max_attempts = max(1, int(
                    getattr(CFG, "QUEUE_INFLIGHT_MAX_ATTEMPTS", 3) or 3
                ))
            except Exception:
                max_attempts = 3
            cutoff = time.time() - max(5, int(max_age_seconds))
            rows = await self._fetchall(
                "SELECT id, event_data, attempts FROM processing_queue "
                "WHERE status = 'in_flight' AND claimed_at IS NOT NULL "
                "AND claimed_at < ?",
                (cutoff,),
            )
            if not rows:
                return stats_out
            for r in rows:
                rid = r.get("id")
                attempts = int(r.get("attempts") or 0)
                raw = r.get("event_data")
                try:
                    if attempts >= max_attempts:
                        err = (
                            f"inflight_expired: no ack within {max_age_seconds}s "
                            f"(attempts={attempts})"
                        )
                        if not await self._dead_letter_row(rid, attempts, raw, err):
                            continue
                        stats_out["dead"] += 1
                        # v14.5.1: mirror decrement inside _queue_lock.
                        async with self._queue_lock:
                            self._queue_size_mirror = max(0, self._queue_size_mirror - 1)
                    else:
                        try:
                            if isinstance(raw, (str, bytes)):
                                data = json_loads(raw)
                            elif isinstance(raw, dict):
                                data = dict(raw)
                            else:
                                data = {}
                        except Exception:
                            data = {}
                        if isinstance(data, dict):
                            data["_recovered"] = 1
                            payload = json_dumps(data)
                        else:
                            payload = raw if isinstance(raw, (str, bytes)) else "{}"
                        claimed = await self._execute_affected(
                            "UPDATE processing_queue SET status = 'pending', "
                            "claimed_at = NULL, event_data = ?, attempts = attempts + 1 "
                            "WHERE id = ? AND status = 'in_flight' AND attempts = ?",
                            (payload, rid, attempts),
                        )
                        if claimed == 1:
                            stats_out["recovered"] += 1
                except Exception as row_err:
                    from unified_alerts.stats import get_unified_stats as _gus
                    _gus().inc("inflight_errors")
                    logger.debug(
                        f"recover_stale_inflight row {rid} skipped: "
                        f"{type(row_err).__name__}: {row_err}"
                    )
            await self._commit()
        except Exception as e:
            from unified_alerts.stats import get_unified_stats as _gus
            _gus().inc("inflight_errors")
            logger.debug(f"recover_stale_inflight failed (fail-safe): {type(e).__name__}: {e}")
        return stats_out

    async def alert_exists(self, msg_hash: str) -> bool:
        h = str(msg_hash or "")
        try:
            async with self._batch_lock:
                for item in self._batch:
                    try:
                        if item and item[0] == "alert" and item[1] == h:
                            return True
                    except Exception:
                        continue
        except Exception:
            pass
        try:
            row = await self._fetchone(
                "SELECT 1 AS one FROM alerts WHERE message_hash = ? LIMIT 1",
                (h,),
            )
            return row is not None
        except Exception:
            return False

    async def purge_queue(self) -> int:
        try:
            if self.db_type == "sqlite":
                cursor = await self._execute("DELETE FROM processing_queue")
                deleted = cursor.rowcount or 0
                await self._commit()
            else:
                result = await self._pool.execute("DELETE FROM processing_queue")
                deleted = int(result.split()[1])
            async with self._queue_lock:
                self._queue_size_mirror = 0
                self._queue_mirror_dirty = False
            return deleted
        except Exception as e:
            logger.error(f"purge_queue error: {e}")
            return 0

    async def purge_stale_queue_rows(self, max_age_seconds: int = 7200) -> int:
        try:
            try:
                max_age_seconds = max(60, int(max_age_seconds))
            except Exception:
                max_age_seconds = 7200
            if self.db_type == "sqlite":
                cursor = await self._execute(
                    "DELETE FROM processing_queue WHERE created_at < datetime('now', ?)",
                    (f"-{max_age_seconds} seconds",),
                )
                deleted = cursor.rowcount or 0
                await self._commit()
            else:
                async with self._pool.acquire() as conn:
                    status = await conn.execute(
                        "DELETE FROM processing_queue WHERE created_at < now() - ($1 || ' seconds')::interval",
                        str(max_age_seconds),
                    )
                    try:
                        deleted = int(str(status).split()[1])
                    except Exception:
                        deleted = 0
            if deleted:
                async with self._queue_lock:
                    self._queue_size_mirror = max(0, self._queue_size_mirror - deleted)
                    self._queue_mirror_dirty = True
                logger.info(
                    f"purge_stale_queue_rows: removed {deleted} rows older than {max_age_seconds}s"
                )
            return deleted
        except Exception as e:
            logger.debug(f"purge_stale_queue_rows failed (fail-safe): {type(e).__name__}: {e}")
            return 0

    # ─── Dead Letters ──────────────────────────────────────────────────────
    async def add_dead_letter(self, record: DeadLetterRecord) -> bool:
        try:
            event_data_json = json_dumps(record.event_data)
            if self.db_type == "sqlite":
                await self._execute(
                    "INSERT INTO dead_letters (event_data, error_text, retry_count, resolved) "
                    "VALUES (?, ?, ?, ?)",
                    (event_data_json, record.error_text, record.retry_count, 1 if record.resolved else 0),
                )
                await self._commit()
            else:
                await self._pool.execute(
                    "INSERT INTO dead_letters (event_data, error_text, retry_count, resolved) "
                    "VALUES ($1, $2, $3, $4)",
                    event_data_json, record.error_text, record.retry_count, 1 if record.resolved else 0,
                )
            return True
        except Exception as e:
            logger.error(f"add_dead_letter error: {e}")
            return False

    @staticmethod
    def _coerce_created_at(value: Any) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, datetime):
            return value.timestamp()
        text = str(value).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).timestamp()
            except ValueError:
                continue
        try:
            return float(text)
        except ValueError:
            return time.time()

    async def get_dead_letter_counts(self) -> Dict[str, int]:
        out = {"total": 0, "unresolved": 0}
        try:
            row = await self._fetchone("SELECT COUNT(*) AS cnt FROM dead_letters")
            out["total"] = int(row["cnt"]) if row and row["cnt"] is not None else 0
            row = await self._fetchone(
                "SELECT COUNT(*) AS cnt FROM dead_letters WHERE resolved = 0"
            )
            out["unresolved"] = int(row["cnt"]) if row and row["cnt"] is not None else 0
        except Exception as e:
            logger.debug(f"get_dead_letter_counts deferred: {e}")
        return out

    async def get_dead_letters(self, limit: int = 100, only_unresolved: bool = True) -> List[DeadLetterRecord]:
        try:
            condition = "WHERE resolved = 0" if only_unresolved else ""
            rows = await self._fetchall(
                f"SELECT id, event_data, error_text, retry_count, resolved, created_at "
                f"FROM dead_letters {condition} ORDER BY created_at ASC LIMIT ?",
                (limit,),
            )
            records = []
            for r in rows:
                records.append(DeadLetterRecord(
                    event_data=json_loads(r["event_data"]),
                    error_text=r["error_text"],
                    retry_count=r["retry_count"],
                    resolved=bool(r["resolved"]),
                    timestamp=self._coerce_created_at(r["created_at"]),
                ))
            return records
        except Exception as e:
            logger.error(f"get_dead_letters error: {e}")
            return []

    async def resolve_dead_letter(self, record_id: int) -> bool:
        try:
            await self._execute(
                "UPDATE dead_letters SET resolved = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (record_id,),
            )
            await self._commit()
            return True
        except Exception as e:
            logger.error(f"resolve_dead_letter error: {e}")
            return False

    async def retry_dead_letter(self, record_id: int) -> Optional[dict]:
        try:
            row = await self._fetchone(
                "SELECT id, event_data, retry_count FROM dead_letters WHERE id = ? AND resolved = 0",
                (record_id,),
            )
            if not row:
                logger.warning(f"Dead letter {record_id} not found or already resolved")
                return None
            event_data = json_loads(row["event_data"])
            retry_count = row["retry_count"] + 1

            if retry_count > CFG.DEAD_LETTER_MAX_RETRIES:
                logger.error(
                    f"Dead letter {record_id} exceeded max retries ({CFG.DEAD_LETTER_MAX_RETRIES}). "
                    "Marking as resolved."
                )
                await self.resolve_dead_letter(record_id)
                return None

            new_id = await self.add_to_queue(event_data, priority=1)
            if new_id not in (-1, -2):
                await self._execute(
                    "UPDATE dead_letters SET retry_count = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (retry_count, record_id),
                )
                await self._commit()
                logger.info(f"Dead letter {record_id} re-queued (retry {retry_count})")
                return event_data
            else:
                logger.error(f"Failed to re-queue dead letter {record_id} (add_to_queue returned {new_id})")
                return None
        except Exception as e:
            logger.error(f"retry_dead_letter error: {e}")
            return None

    async def cleanup_dead_letters(self, days: int = 7) -> int:
        cutoff = time.time() - days * 86400
        total = 0
        try:
            cur = await self._execute("DELETE FROM dead_letters WHERE resolved = 1")
            total += cur.rowcount or 0

            cur = await self._execute(
                "DELETE FROM dead_letters WHERE created_at < ?", (cutoff,)
            )
            total += cur.rowcount or 0

            row = await self._fetchone(
                "SELECT COUNT(*) AS cnt FROM dead_letters"
            )
            current_count = int(row["cnt"]) if row else 0
            if current_count > _DEAD_LETTER_MAX_ROWS:
                excess = current_count - _DEAD_LETTER_MAX_ROWS
                cur = await self._execute(
                    "DELETE FROM dead_letters WHERE id IN ("
                    "  SELECT id FROM dead_letters ORDER BY resolved DESC, created_at ASC LIMIT ?"
                    ")",
                    (excess,),
                )
                total += cur.rowcount or 0
                logger.warning(
                    f"dead_letters exceeded hard cap ({_DEAD_LETTER_MAX_ROWS}), "
                    f"evicted {cur.rowcount or 0} rows"
                )

            await self._commit()
            if total > 0:
                logger.info(f"cleanup_dead_letters: {total} rows removed")
            return total
        except Exception as e:
            logger.error(f"cleanup_dead_letters error: {e}")
            return 0

    # ─── Messages ──────────────────────────────────────────────────────────
    async def try_insert_message(self, rec: MessageRecord) -> bool:
        async with self._hash_lock:
            if rec.message_hash in self.message_cache:
                return False
            self.message_cache.append(rec.message_hash)

        try:
            changed = 0
            if self.db_type == "sqlite":
                cursor = await self._execute(
                    "INSERT OR IGNORE INTO messages "
                    "(message_hash, chat_id, sender_id, message_text, keyword_found, score, spam_score, timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rec.message_hash, rec.chat_id, rec.sender_id,
                        rec.message_text[:500], rec.keyword_found,
                        rec.score, rec.spam_score, rec.timestamp,
                    ),
                )
                changed = cursor.rowcount or 0
                if changed:
                    await self._execute(
                        "INSERT INTO sender_stats (sender_id, total_messages, first_seen) "
                        "VALUES (?, 1, ?) "
                        "ON CONFLICT(sender_id) DO UPDATE SET "
                        "total_messages = total_messages + 1, "
                        "updated_at = CURRENT_TIMESTAMP",
                        (rec.sender_id, rec.timestamp),
                    )
                await self._commit()
            else:
                result = await self._pool.execute(
                    "INSERT INTO messages "
                    "(message_hash, chat_id, sender_id, message_text, keyword_found, score, spam_score, timestamp) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                    "ON CONFLICT (message_hash) DO NOTHING",
                    rec.message_hash, rec.chat_id, rec.sender_id,
                    rec.message_text[:500], rec.keyword_found,
                    rec.score, rec.spam_score, rec.timestamp,
                )
                try:
                    parts = str(result).split()
                    changed = int(parts[-1]) if parts and parts[-1].isdigit() else 0
                except Exception:
                    changed = 0
                if changed:
                    await self._pool.execute(
                        "INSERT INTO sender_stats (sender_id, total_messages, first_seen) "
                        "VALUES ($1, 1, $2) "
                        "ON CONFLICT (sender_id) DO UPDATE SET "
                        "total_messages = sender_stats.total_messages + 1, "
                        "updated_at = CURRENT_TIMESTAMP",
                        rec.sender_id, rec.timestamp,
                    )

            if changed:
                async with self._stats_lock:
                    self.stats["total_messages"] += 1
                    if rec.keyword_found:
                        self.stats["keywords_found"] += 1
                return True
            return False

        except Exception as e:
            logger.error(f"try_insert_message error: {e}")
            return False

    # ─── Sender Contacts ───────────────────────────────────────────────────
    _CONTACT_COLS = (
        "sender_id, access_hash, username, first_name, last_name, "
        "last_chat_id, last_message_id, last_message_link, last_group_link, "
        "phone, is_bot, is_verified, is_premium, is_scam, is_fake, "
        "is_restricted, is_deleted, is_contact, is_mutual_contact, "
        "photo_available, restriction_reason, lang_code, status, usernames, "
        "last_seen, last_updated, owner_account"
    )

    def _contact_params(self, s: Dict[str, Any]) -> tuple:
        return (
            s["sender_id"], s.get("access_hash"), s.get("username"),
            s.get("first_name"), s.get("last_name"),
            s.get("chat_id"), s.get("message_id"),
            s.get("msg_link"), s.get("group_link"),
            s.get("phone"), s.get("is_bot"), s.get("is_verified"),
            s.get("is_premium"), s.get("is_scam"), s.get("is_fake"),
            s.get("is_restricted"), s.get("is_deleted"), s.get("is_contact"),
            s.get("is_mutual_contact"), s.get("photo_available"),
            s.get("restriction_reason"), s.get("lang_code"), s.get("status"),
            s.get("usernames"), s.get("last_seen"), s.get("last_updated"),
            s.get("owner_account"),
        )

    async def upsert_sender_contact(self, sender_data: Dict[str, Any]) -> None:
        try:
            import time as _time
            data = dict(sender_data)
            data.setdefault("last_updated", _time.time())
            if isinstance(data.get("usernames"), (list, tuple)):
                import json as _json
                data["usernames"] = _json.dumps(list(data["usernames"]), ensure_ascii=False)
            p = self._contact_params(data)
            n = len(p)
            if self.db_type == "sqlite":
                placeholders = ", ".join(["?"] * n)
                set_clause = (
                    "access_hash = COALESCE(excluded.access_hash, sender_contacts.access_hash), "
                    "username = COALESCE(excluded.username, sender_contacts.username), "
                    "first_name = COALESCE(excluded.first_name, sender_contacts.first_name), "
                    "last_name = COALESCE(excluded.last_name, sender_contacts.last_name), "
                    "last_chat_id = excluded.last_chat_id, "
                    "last_message_id = excluded.last_message_id, "
                    "last_message_link = COALESCE(excluded.last_message_link, sender_contacts.last_message_link), "
                    "last_group_link = COALESCE(excluded.last_group_link, sender_contacts.last_group_link), "
                    "phone = COALESCE(excluded.phone, sender_contacts.phone), "
                    "is_bot = COALESCE(excluded.is_bot, sender_contacts.is_bot), "
                    "is_verified = COALESCE(excluded.is_verified, sender_contacts.is_verified), "
                    "is_premium = COALESCE(excluded.is_premium, sender_contacts.is_premium), "
                    "is_scam = COALESCE(excluded.is_scam, sender_contacts.is_scam), "
                    "is_fake = COALESCE(excluded.is_fake, sender_contacts.is_fake), "
                    "is_restricted = COALESCE(excluded.is_restricted, sender_contacts.is_restricted), "
                    "is_deleted = COALESCE(excluded.is_deleted, sender_contacts.is_deleted), "
                    "is_contact = COALESCE(excluded.is_contact, sender_contacts.is_contact), "
                    "is_mutual_contact = COALESCE(excluded.is_mutual_contact, sender_contacts.is_mutual_contact), "
                    "photo_available = COALESCE(excluded.photo_available, sender_contacts.photo_available), "
                    "restriction_reason = COALESCE(excluded.restriction_reason, sender_contacts.restriction_reason), "
                    "lang_code = COALESCE(excluded.lang_code, sender_contacts.lang_code), "
                    "status = COALESCE(excluded.status, sender_contacts.status), "
                    "usernames = COALESCE(excluded.usernames, sender_contacts.usernames), "
                    "last_seen = COALESCE(excluded.last_seen, sender_contacts.last_seen), "
                    "owner_account = COALESCE(excluded.owner_account, sender_contacts.owner_account), "
                    "last_updated = excluded.last_updated, "
                    "updated_at = CURRENT_TIMESTAMP"
                )
                await self._execute(
                    f"INSERT INTO sender_contacts ({self._CONTACT_COLS}) "
                    f"VALUES ({placeholders}) "
                    "ON CONFLICT(sender_id) DO UPDATE SET " + set_clause,
                    p,
                )
                await self._commit()
            else:
                placeholders = ", ".join(["?"] * n)
                set_clause = (
                    "access_hash = COALESCE(EXCLUDED.access_hash, sender_contacts.access_hash), "
                    "username = COALESCE(EXCLUDED.username, sender_contacts.username), "
                    "first_name = COALESCE(EXCLUDED.first_name, sender_contacts.first_name), "
                    "last_name = COALESCE(EXCLUDED.last_name, sender_contacts.last_name), "
                    "last_chat_id = EXCLUDED.last_chat_id, "
                    "last_message_id = EXCLUDED.last_message_id, "
                    "last_message_link = COALESCE(EXCLUDED.last_message_link, sender_contacts.last_message_link), "
                    "last_group_link = COALESCE(EXCLUDED.last_group_link, sender_contacts.last_group_link), "
                    "phone = COALESCE(EXCLUDED.phone, sender_contacts.phone), "
                    "is_bot = COALESCE(EXCLUDED.is_bot, sender_contacts.is_bot), "
                    "is_verified = COALESCE(EXCLUDED.is_verified, sender_contacts.is_verified), "
                    "is_premium = COALESCE(EXCLUDED.is_premium, sender_contacts.is_premium), "
                    "is_scam = COALESCE(EXCLUDED.is_scam, sender_contacts.is_scam), "
                    "is_fake = COALESCE(EXCLUDED.is_fake, sender_contacts.is_fake), "
                    "is_restricted = COALESCE(EXCLUDED.is_restricted, sender_contacts.is_restricted), "
                    "is_deleted = COALESCE(EXCLUDED.is_deleted, sender_contacts.is_deleted), "
                    "is_contact = COALESCE(EXCLUDED.is_contact, sender_contacts.is_contact), "
                    "is_mutual_contact = COALESCE(EXCLUDED.is_mutual_contact, sender_contacts.is_mutual_contact), "
                    "photo_available = COALESCE(EXCLUDED.photo_available, sender_contacts.photo_available), "
                    "restriction_reason = COALESCE(EXCLUDED.restriction_reason, sender_contacts.restriction_reason), "
                    "lang_code = COALESCE(EXCLUDED.lang_code, sender_contacts.lang_code), "
                    "status = COALESCE(EXCLUDED.status, sender_contacts.status), "
                    "usernames = COALESCE(EXCLUDED.usernames, sender_contacts.usernames), "
                    "last_seen = COALESCE(EXCLUDED.last_seen, sender_contacts.last_seen), "
                    "owner_account = COALESCE(EXCLUDED.owner_account, sender_contacts.owner_account), "
                    "last_updated = EXCLUDED.last_updated, "
                    "updated_at = CURRENT_TIMESTAMP"
                )
                await self._pool.execute(
                    _pg(
                        f"INSERT INTO sender_contacts ({self._CONTACT_COLS}) "
                        f"VALUES ({placeholders}) "
                        "ON CONFLICT (sender_id) DO UPDATE SET " + set_clause
                    ),
                    *p,
                )
        except Exception as e:
            logger.error(f"upsert_sender_contact error: {e}")

    async def get_sender_contact(self, sender_id: int) -> Optional[Dict[str, Any]]:
        try:
            return await self._fetchone(
                "SELECT * FROM sender_contacts WHERE sender_id = ?",
                (sender_id,),
            )
        except Exception as e:
            logger.debug(f"get_sender_contact error: {e}")
            return None

    _INTEL_ALERT_COLUMNS = [
        ("alert_status", "TEXT"),
        ("alert_sent_at", "DOUBLE PRECISION"),
        ("alerted_to", "TEXT"),
        ("notification_attempts", "INTEGER DEFAULT 0"),
        ("notification_error", "TEXT"),
        ("button_message_url", "TEXT"),
        ("button_user_url", "TEXT"),
    ]

    _INTEL_ENTITY_STATEMENTS = (
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            access_hash INTEGER,
            first_name TEXT,
            last_name TEXT,
            full_name TEXT,
            username TEXT,
            phone TEXT,
            bio TEXT,
            profile_photo TEXT,
            is_bot INTEGER DEFAULT 0,
            is_verified INTEGER DEFAULT 0,
            is_premium INTEGER DEFAULT 0,
            is_scam INTEGER DEFAULT 0,
            is_fake INTEGER DEFAULT 0,
            is_deleted INTEGER DEFAULT 0,
            status TEXT,
            last_seen REAL,
            created_at REAL,
            updated_at REAL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS group_memberships (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            chat_title TEXT,
            chat_username TEXT,
            chat_type TEXT,
            member_status TEXT,
            member_role TEXT,
            admin_permissions TEXT,
            joined_at REAL,
            is_member INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            is_owner INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            is_restricted INTEGER DEFAULT 0,
            is_muted INTEGER DEFAULT 0,
            updated_at REAL,
            PRIMARY KEY (chat_id, user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS group_messages (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            user_id INTEGER,
            message_text TEXT,
            message_date REAL,
            edit_date REAL,
            message_link TEXT,
            reply_to_message_id INTEGER,
            reply_to_user_id INTEGER,
            is_reply INTEGER DEFAULT 0,
            is_forward INTEGER DEFAULT 0,
            is_edited INTEGER DEFAULT 0,
            is_deleted INTEGER DEFAULT 0,
            is_pinned INTEGER DEFAULT 0,
            views INTEGER,
            forwards INTEGER,
            replies_count INTEGER,
            media_type TEXT,
            media_id TEXT,
            file_name TEXT,
            file_size INTEGER,
            entities TEXT,
            urls TEXT,
            mentions TEXT,
            hashtags TEXT,
            phone_numbers TEXT,
            msg_hash TEXT,
            created_at REAL,
            updated_at REAL,
            PRIMARY KEY (chat_id, message_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS message_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            user_id INTEGER,
            msg_hash TEXT,
            intent TEXT,
            intent_confidence REAL,
            matched_keywords TEXT,
            matched_patterns TEXT,
            academic_context TEXT,
            action_score REAL,
            urgency_score REAL,
            negation_score REAL,
            spam_score REAL,
            advertisement_score REAL,
            similarity_score REAL,
            fuzzy_score REAL,
            final_score REAL,
            classification TEXT,
            accepted INTEGER DEFAULT 0,
            rejected INTEGER DEFAULT 0,
            duplicate INTEGER DEFAULT 0,
            rejection_reason TEXT,
            detection_reason TEXT,
            processing_time_ms REAL,
            engine_version TEXT,
            filter_version TEXT,
            created_at REAL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS user_activity (
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            first_seen_at REAL,
            last_seen_at REAL,
            total_messages INTEGER DEFAULT 0,
            first_message_id INTEGER,
            last_message_id INTEGER,
            activity_hours TEXT,
            activity_days TEXT,
            updated_at REAL,
            PRIMARY KEY (user_id, chat_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_group_messages_user ON group_messages (user_id, chat_id, message_date)",
        "CREATE INDEX IF NOT EXISTS idx_group_messages_date ON group_messages (message_date)",
        "CREATE INDEX IF NOT EXISTS idx_message_analysis_msg ON message_analysis (chat_id, message_id)",
        "CREATE INDEX IF NOT EXISTS idx_users_username ON users (username)",
    )

    @staticmethod
    def _intel_pg_sql(sql: str) -> str:
        return (
            sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
            .replace("INTEGER PRIMARY KEY", "BIGINT PRIMARY KEY")
            .replace("INTEGER NOT NULL", "BIGINT NOT NULL")
            .replace("INTEGER,", "BIGINT,")
            .replace("INTEGER)", "BIGINT)")
        )

    async def _migrate_intel_entities(self) -> None:
        created = 0
        for stmt in self._INTEL_ENTITY_STATEMENTS:
            try:
                sql = self._intel_pg_sql(stmt) if self.db_type == "postgresql" else stmt
                await self._execute(sql)
                created += 1
            except Exception as e:
                logger.warning(f"intel-entities migration statement skipped: {type(e).__name__}: {str(e)[:120]}")
        if self._INTEL_ALERT_COLUMNS:
            existing: set = set()
            try:
                if self.db_type == "postgresql":
                    rows = await self._fetchall(
                        "SELECT column_name FROM information_schema.columns WHERE table_name = 'alerts'", ()
                    )
                    existing = {r["column_name"] for r in rows}
                else:
                    rows = await self._fetchall("PRAGMA table_info(alerts)", ())
                    existing = {r["name"] for r in rows}
            except Exception:
                existing = set()
            added = 0
            for name, decl in self._INTEL_ALERT_COLUMNS:
                if name in existing:
                    continue
                try:
                    if self.db_type == "postgresql":
                        await self._execute(f"ALTER TABLE alerts ADD COLUMN {name} {decl}")
                    else:
                        await self._execute(
                            f"ALTER TABLE alerts ADD COLUMN {name} "
                            f"{decl.replace('DOUBLE PRECISION', 'REAL')}"
                        )
                    added += 1
                except Exception as e:
                    logger.warning(f"alerts dispatch column {name} skipped: {type(e).__name__}")
            if added:
                logger.info(f"Database migration v11.1: alerts dispatch columns (+{added})")
        await self._commit()
        if created:
            logger.info(f"Database migration v11.1: intel entities ready ({created} statements)")

    async def upsert_tg_user(self, d: Dict[str, Any]) -> bool:
        try:
            full_name = d.get("full_name")
            if full_name is None:
                fn, ln = d.get("first_name"), d.get("last_name")
                full_name = f"{fn or ''} {ln or ''}".strip() or None
            await self._execute(
                """
                INSERT INTO users (user_id, access_hash, first_name, last_name, full_name,
                    username, phone, bio, profile_photo, is_bot, is_verified, is_premium,
                    is_scam, is_fake, is_deleted, status, last_seen, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (user_id) DO UPDATE SET
                    access_hash = COALESCE(EXCLUDED.access_hash, users.access_hash),
                    first_name = COALESCE(EXCLUDED.first_name, users.first_name),
                    last_name = COALESCE(EXCLUDED.last_name, users.last_name),
                    full_name = COALESCE(EXCLUDED.full_name, users.full_name),
                    username = COALESCE(EXCLUDED.username, users.username),
                    phone = COALESCE(EXCLUDED.phone, users.phone),
                    bio = COALESCE(EXCLUDED.bio, users.bio),
                    profile_photo = COALESCE(EXCLUDED.profile_photo, users.profile_photo),
                    is_bot = COALESCE(EXCLUDED.is_bot, users.is_bot),
                    is_verified = COALESCE(EXCLUDED.is_verified, users.is_verified),
                    is_premium = COALESCE(EXCLUDED.is_premium, users.is_premium),
                    is_scam = COALESCE(EXCLUDED.is_scam, users.is_scam),
                    is_fake = COALESCE(EXCLUDED.is_fake, users.is_fake),
                    is_deleted = COALESCE(EXCLUDED.is_deleted, users.is_deleted),
                    status = COALESCE(EXCLUDED.status, users.status),
                    last_seen = COALESCE(EXCLUDED.last_seen, users.last_seen),
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    int(d.get("user_id") or 0), d.get("access_hash"), d.get("first_name"),
                    d.get("last_name"), full_name, d.get("username"), d.get("phone"),
                    d.get("bio"), d.get("profile_photo"),
                    1 if d.get("is_bot") else 0 if d.get("is_bot") is not None else None,
                    1 if d.get("is_verified") else 0 if d.get("is_verified") is not None else None,
                    1 if d.get("is_premium") else 0 if d.get("is_premium") is not None else None,
                    1 if d.get("is_scam") else 0 if d.get("is_scam") is not None else None,
                    1 if d.get("is_fake") else 0 if d.get("is_fake") is not None else None,
                    1 if d.get("is_deleted") else 0 if d.get("is_deleted") is not None else None,
                    d.get("status"), d.get("last_seen"), time.time(), time.time(),
                ),
            )
            await self._commit()
            return True
        except Exception as e:
            logger.debug(f"upsert_tg_user skipped: {type(e).__name__}: {str(e)[:120]}")
            return False

    async def upsert_group_membership(self, d: Dict[str, Any]) -> bool:
        try:
            await self._execute(
                self._sql_scalar_max("""
                INSERT INTO group_memberships (chat_id, user_id, chat_title, chat_username,
                    chat_type, member_status, member_role, admin_permissions, joined_at,
                    is_member, is_admin, is_owner, is_banned, is_restricted, is_muted, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (chat_id, user_id) DO UPDATE SET
                    chat_title = COALESCE(EXCLUDED.chat_title, group_memberships.chat_title),
                    chat_username = COALESCE(EXCLUDED.chat_username, group_memberships.chat_username),
                    chat_type = COALESCE(EXCLUDED.chat_type, group_memberships.chat_type),
                    member_status = COALESCE(EXCLUDED.member_status, group_memberships.member_status),
                    member_role = COALESCE(EXCLUDED.member_role, group_memberships.member_role),
                    admin_permissions = COALESCE(EXCLUDED.admin_permissions, group_memberships.admin_permissions),
                    joined_at = COALESCE(EXCLUDED.joined_at, group_memberships.joined_at),
                    is_member = MAX(COALESCE(group_memberships.is_member, 0), COALESCE(EXCLUDED.is_member, 0)),
                    is_admin = MAX(COALESCE(group_memberships.is_admin, 0), COALESCE(EXCLUDED.is_admin, 0)),
                    is_owner = MAX(COALESCE(group_memberships.is_owner, 0), COALESCE(EXCLUDED.is_owner, 0)),
                    is_banned = MAX(COALESCE(group_memberships.is_banned, 0), COALESCE(EXCLUDED.is_banned, 0)),
                    is_restricted = MAX(COALESCE(group_memberships.is_restricted, 0), COALESCE(EXCLUDED.is_restricted, 0)),
                    is_muted = MAX(COALESCE(group_memberships.is_muted, 0), COALESCE(EXCLUDED.is_muted, 0)),
                    updated_at = EXCLUDED.updated_at
                """),
                (
                    int(d.get("chat_id") or 0), int(d.get("user_id") or 0),
                    d.get("chat_title"), d.get("chat_username"), d.get("chat_type"),
                    d.get("member_status"), d.get("member_role"), d.get("admin_permissions"),
                    d.get("joined_at"),
                    1 if d.get("is_member") else 0,
                    1 if d.get("is_admin") else 0,
                    1 if d.get("is_owner") else 0,
                    1 if d.get("is_banned") else 0,
                    1 if d.get("is_restricted") else 0,
                    1 if d.get("is_muted") else 0,
                    time.time(),
                ),
            )
            await self._commit()
            return True
        except Exception as e:
            logger.debug(f"upsert_group_membership skipped: {type(e).__name__}: {str(e)[:120]}")
            return False

    async def record_group_message(self, d: Dict[str, Any]) -> bool:
        try:
            await self._execute(
                self._sql_scalar_max("""
                INSERT INTO group_messages (chat_id, message_id, user_id, message_text,
                    message_date, edit_date, message_link, reply_to_message_id, reply_to_user_id,
                    is_reply, is_forward, is_edited, is_deleted, is_pinned, views, forwards,
                    replies_count, media_type, media_id, file_name, file_size, entities,
                    urls, mentions, hashtags, phone_numbers, msg_hash, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (chat_id, message_id) DO UPDATE SET
                    user_id = COALESCE(EXCLUDED.user_id, group_messages.user_id),
                    message_text = COALESCE(EXCLUDED.message_text, group_messages.message_text),
                    edit_date = COALESCE(EXCLUDED.edit_date, group_messages.edit_date),
                    message_link = COALESCE(EXCLUDED.message_link, group_messages.message_link),
                    reply_to_message_id = COALESCE(EXCLUDED.reply_to_message_id, group_messages.reply_to_message_id),
                    reply_to_user_id = COALESCE(EXCLUDED.reply_to_user_id, group_messages.reply_to_user_id),
                    is_reply = MAX(COALESCE(group_messages.is_reply, 0), COALESCE(EXCLUDED.is_reply, 0)),
                    is_forward = MAX(COALESCE(group_messages.is_forward, 0), COALESCE(EXCLUDED.is_forward, 0)),
                    is_edited = MAX(COALESCE(group_messages.is_edited, 0), COALESCE(EXCLUDED.is_edited, 0)),
                    is_deleted = COALESCE(EXCLUDED.is_deleted, group_messages.is_deleted),
                    is_pinned = MAX(COALESCE(group_messages.is_pinned, 0), COALESCE(EXCLUDED.is_pinned, 0)),
                    views = COALESCE(EXCLUDED.views, group_messages.views),
                    forwards = COALESCE(EXCLUDED.forwards, group_messages.forwards),
                    replies_count = COALESCE(EXCLUDED.replies_count, group_messages.replies_count),
                    media_type = COALESCE(EXCLUDED.media_type, group_messages.media_type),
                    media_id = COALESCE(EXCLUDED.media_id, group_messages.media_id),
                    file_name = COALESCE(EXCLUDED.file_name, group_messages.file_name),
                    file_size = COALESCE(EXCLUDED.file_size, group_messages.file_size),
                    entities = COALESCE(EXCLUDED.entities, group_messages.entities),
                    urls = COALESCE(EXCLUDED.urls, group_messages.urls),
                    mentions = COALESCE(EXCLUDED.mentions, group_messages.mentions),
                    hashtags = COALESCE(EXCLUDED.hashtags, group_messages.hashtags),
                    phone_numbers = COALESCE(EXCLUDED.phone_numbers, group_messages.phone_numbers),
                    msg_hash = COALESCE(EXCLUDED.msg_hash, group_messages.msg_hash),
                    updated_at = EXCLUDED.updated_at
                """),
                (
                    int(d.get("chat_id") or 0), int(d.get("message_id") or 0), d.get("user_id"),
                    d.get("message_text"), d.get("message_date"), d.get("edit_date"),
                    d.get("message_link"), d.get("reply_to_message_id"), d.get("reply_to_user_id"),
                    1 if d.get("is_reply") else 0,
                    1 if d.get("is_forward") else 0,
                    1 if d.get("is_edited") else 0,
                    1 if d.get("is_deleted") else 0,
                    1 if d.get("is_pinned") else 0,
                    d.get("views"), d.get("forwards"), d.get("replies_count"),
                    d.get("media_type"), d.get("media_id"), d.get("file_name"), d.get("file_size"),
                    d.get("entities"), d.get("urls"), d.get("mentions"), d.get("hashtags"),
                    d.get("phone_numbers"), d.get("msg_hash"), time.time(), time.time(),
                ),
            )
            await self._commit()
            return True
        except Exception as e:
            logger.debug(f"record_group_message skipped: {type(e).__name__}: {str(e)[:120]}")
            return False

    async def record_message_analysis(self, d: Dict[str, Any]) -> bool:
        try:
            await self._execute(
                """
                INSERT INTO message_analysis (chat_id, message_id, user_id, msg_hash,
                    intent, intent_confidence, matched_keywords, matched_patterns,
                    academic_context, action_score, urgency_score, negation_score,
                    spam_score, advertisement_score, similarity_score, fuzzy_score,
                    final_score, classification, accepted, rejected, duplicate,
                    rejection_reason, detection_reason, processing_time_ms,
                    engine_version, filter_version, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(d.get("chat_id") or 0), int(d.get("message_id") or 0), d.get("user_id"),
                    d.get("msg_hash"), d.get("intent"), d.get("intent_confidence"),
                    d.get("matched_keywords"), d.get("matched_patterns"), d.get("academic_context"),
                    d.get("action_score"), d.get("urgency_score"), d.get("negation_score"),
                    d.get("spam_score"), d.get("advertisement_score"), d.get("similarity_score"),
                    d.get("fuzzy_score"), d.get("final_score"), d.get("classification"),
                    1 if d.get("accepted") else 0,
                    1 if d.get("rejected") else 0,
                    1 if d.get("duplicate") else 0,
                    d.get("rejection_reason"), d.get("detection_reason"),
                    d.get("processing_time_ms"), d.get("engine_version"), d.get("filter_version"),
                    time.time(),
                ),
            )
            await self._commit()
            return True
        except Exception as e:
            logger.debug(f"record_message_analysis skipped: {type(e).__name__}: {str(e)[:120]}")
            return False

    async def record_user_activity(
        self, user_id: int, chat_id: int, message_id: Optional[int], ts: Optional[float]
    ) -> bool:
        try:
            ts = float(ts or time.time())
            row = await self._fetchone(
                "SELECT activity_hours, activity_days FROM user_activity WHERE user_id = ? AND chat_id = ?",
                (int(user_id or 0), int(chat_id or 0)),
            )
            hours: Dict[str, int] = {}
            days: Dict[str, int] = {}
            if row is not None:
                try:
                    hours = dict(json.loads(row["activity_hours"])) if row["activity_hours"] else {}
                except Exception:
                    hours = {}
                try:
                    days = dict(json.loads(row["activity_days"])) if row["activity_days"] else {}
                except Exception:
                    days = {}
            hkey = str(datetime.fromtimestamp(ts, tz=timezone.utc).hour)
            dkey = str(datetime.fromtimestamp(ts, tz=timezone.utc).weekday())
            hours[hkey] = int(hours.get(hkey, 0)) + 1
            days[dkey] = int(days.get(dkey, 0)) + 1
            await self._execute(
                """
                INSERT INTO user_activity (user_id, chat_id, first_seen_at, last_seen_at,
                    total_messages, first_message_id, last_message_id, activity_hours,
                    activity_days, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (user_id, chat_id) DO UPDATE SET
                    last_seen_at = EXCLUDED.last_seen_at,
                    total_messages = user_activity.total_messages + 1,
                    last_message_id = EXCLUDED.last_message_id,
                    activity_hours = EXCLUDED.activity_hours,
                    activity_days = EXCLUDED.activity_days,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    int(user_id or 0), int(chat_id or 0), ts, ts, 1,
                    message_id, message_id,
                    json.dumps(hours), json.dumps(days),
                    time.time(),
                ),
            )
            await self._commit()
            return True
        except Exception as e:
            logger.debug(f"record_user_activity skipped: {type(e).__name__}: {str(e)[:120]}")
            return False

    async def get_user_activity(self, user_id: int, chat_id: int) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "user_id": user_id, "chat_id": chat_id,
            "first_seen_at": None, "last_seen_at": None,
            "total_messages": 0, "messages_today": 0, "messages_week": 0, "messages_month": 0,
            "first_message_id": None, "last_message_id": None,
            "average_messages_per_day": None,
            "activity_hours": {}, "activity_days": {},
        }
        try:
            row = await self._fetchone(
                "SELECT * FROM user_activity WHERE user_id = ? AND chat_id = ?",
                (int(user_id or 0), int(chat_id or 0)),
            )
            if row is not None:
                out["first_seen_at"] = row["first_seen_at"]
                out["last_seen_at"] = row["last_seen_at"]
                out["total_messages"] = int(row["total_messages"] or 0)
                out["first_message_id"] = row["first_message_id"]
                out["last_message_id"] = row["last_message_id"]
                for key, col in (("activity_hours", "activity_hours"), ("activity_days", "activity_days")):
                    try:
                        out[key] = dict(json.loads(row[col])) if row[col] else {}
                    except Exception:
                        out[key] = {}
                if out["first_seen_at"]:
                    span_days = max((time.time() - float(out["first_seen_at"])) / 86400.0, 1.0 / 24.0)
                    out["average_messages_per_day"] = round(out["total_messages"] / span_days, 3)
            now = time.time()
            for key, secs in (("messages_today", 86400), ("messages_week", 7 * 86400), ("messages_month", 30 * 86400)):
                try:
                    r = await self._fetchone(
                        "SELECT COUNT(*) AS cnt FROM group_messages "
                        "WHERE user_id = ? AND chat_id = ? AND message_date >= ?",
                        (int(user_id or 0), int(chat_id or 0), now - secs),
                    )
                    out[key] = int(r["cnt"]) if r is not None else 0
                except Exception:
                    out[key] = 0
        except Exception as e:
            logger.debug(f"get_user_activity skipped: {type(e).__name__}")
        return out

    async def mark_alert_dispatched(
        self,
        msg_hash: str,
        status: str = "sent",
        alerted_to: Optional[str] = None,
        error: Optional[str] = None,
        attempts: int = 1,
        button_message_url: Optional[str] = None,
        button_user_url: Optional[str] = None,
    ) -> bool:
        try:
            await self._execute(
                """
                UPDATE alerts SET
                    alert_status = ?,
                    alert_sent_at = CASE WHEN ? = 'sent' THEN ? ELSE alert_sent_at END,
                    alerted_to = COALESCE(?, alerted_to),
                    notification_attempts = COALESCE(notification_attempts, 0) + ?,
                    notification_error = ?,
                    button_message_url = COALESCE(?, button_message_url),
                    button_user_url = COALESCE(?, button_user_url)
                WHERE message_hash = ?
                """,
                (status, status, time.time(), alerted_to, int(attempts or 1),
                 error, button_message_url, button_user_url, msg_hash),
            )
            await self._commit()
            return True
        except Exception as e:
            logger.debug(f"mark_alert_dispatched skipped: {type(e).__name__}")
            return False

    async def _migrate_sender_account_hashes(self) -> None:
        try:
            await self._execute(
                "CREATE TABLE IF NOT EXISTS sender_account_hashes ("
                "account_name TEXT NOT NULL, "
                "sender_id BIGINT NOT NULL, "
                "access_hash BIGINT NOT NULL, "
                "updated_at DOUBLE PRECISION NOT NULL, "
                "PRIMARY KEY (account_name, sender_id))"
            )
            try:
                await self._execute(
                    "CREATE INDEX IF NOT EXISTS idx_sender_account_hashes_sender "
                    "ON sender_account_hashes (sender_id)"
                )
            except Exception:
                pass
            await self._commit()
            logger.debug("sender_account_hashes table ready (v10.4)")
        except Exception as e:
            logger.warning(f"sender_account_hashes migration skipped: {type(e).__name__}: {str(e)[:120]}")

    async def upsert_sender_account_hash(self, account_name: str, sender_id: int, access_hash: int) -> bool:
        return await self.upsert_sender_access(sender_id, account_name, access_hash)

    async def get_sender_account_hash(self, account_name: str, sender_id: int) -> Optional[int]:
        return await self.get_sender_access_hash(account_name, sender_id)

    async def _migrate_sender_access(self) -> None:
        try:
            await self._execute(
                "CREATE TABLE IF NOT EXISTS sender_access ("
                "sender_id BIGINT NOT NULL, "
                "account_name TEXT NOT NULL, "
                "access_hash BIGINT NOT NULL, "
                "updated_at DOUBLE PRECISION NOT NULL, "
                "PRIMARY KEY (sender_id, account_name))"
            )
        except Exception as e:
            logger.warning(f"sender_access table migration skipped: {type(e).__name__}: {str(e)[:120]}")
        try:
            if self.db_type == "postgresql":
                await self._execute(
                    "INSERT INTO sender_access (sender_id, account_name, access_hash, updated_at) "
                    "SELECT sender_id, account_name, access_hash, updated_at "
                    "FROM sender_account_hashes "
                    "ON CONFLICT (sender_id, account_name) DO NOTHING"
                )
            else:
                await self._execute(
                    "INSERT OR IGNORE INTO sender_access (sender_id, account_name, access_hash, updated_at) "
                    "SELECT sender_id, account_name, access_hash, updated_at "
                    "FROM sender_account_hashes"
                )
            await self._commit()
        except Exception as e:
            logger.debug(f"sender_access legacy copy skipped: {type(e).__name__}")
        try:
            await self._execute(
                "CREATE INDEX IF NOT EXISTS idx_sender_access_sender "
                "ON sender_access (sender_id)"
            )
        except Exception:
            pass
        try:
            if self.db_type == "postgresql":
                rows = await self._fetchall(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'alerts'", ()
                )
                if rows and "contact_method" not in {r["column_name"] for r in rows}:
                    await self._execute("ALTER TABLE alerts ADD COLUMN contact_method TEXT DEFAULT ''")
                    await self._commit()
            else:
                rows = await self._fetchall("PRAGMA table_info(alerts)", ())
                if rows and "contact_method" not in {r["name"] for r in rows}:
                    await self._execute("ALTER TABLE alerts ADD COLUMN contact_method TEXT DEFAULT ''")
                    await self._commit()
        except Exception as e:
            logger.warning(f"alerts.contact_method migration skipped: {type(e).__name__}: {str(e)[:120]}")
        try:
            if self.db_type == "postgresql":
                rows = await self._fetchall(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'sender_contacts'", ()
                )
                if rows and "owner_account" not in {r["column_name"] for r in rows}:
                    await self._execute("ALTER TABLE sender_contacts ADD COLUMN owner_account TEXT")
                    await self._commit()
            else:
                rows = await self._fetchall("PRAGMA table_info(sender_contacts)", ())
                if rows and "owner_account" not in {r["name"] for r in rows}:
                    await self._execute("ALTER TABLE sender_contacts ADD COLUMN owner_account TEXT")
                    await self._commit()
        except Exception as e:
            logger.warning(f"sender_contacts.owner_account migration skipped: {type(e).__name__}: {str(e)[:120]}")
        logger.debug("sender_access registry ready (v10.7)")

    async def upsert_sender_access(self, sender_id: int, account_name: str, access_hash: int) -> bool:
        try:
            if not sender_id or not account_name or not isinstance(access_hash, int) or access_hash == 0:
                return False
            if self.db_type == "postgresql":
                await self._execute(
                    "INSERT INTO sender_access (sender_id, account_name, access_hash, updated_at) "
                    "VALUES ($1, $2, $3, $4) "
                    "ON CONFLICT (sender_id, account_name) DO UPDATE SET "
                    "access_hash = EXCLUDED.access_hash, updated_at = EXCLUDED.updated_at",
                    (int(sender_id), str(account_name), int(access_hash), time.time()),
                )
            else:
                await self._execute(
                    "INSERT INTO sender_access (sender_id, account_name, access_hash, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (sender_id, account_name) DO UPDATE SET "
                    "access_hash = excluded.access_hash, updated_at = excluded.updated_at",
                    (int(sender_id), str(account_name), int(access_hash), time.time()),
                )
            await self._commit()
            return True
        except Exception as e:
            logger.debug(f"upsert_sender_access error: {type(e).__name__}")
            return False

    async def get_sender_access_hash(self, account_name: str, sender_id: int) -> Optional[int]:
        try:
            row = await self._fetchone(
                "SELECT access_hash FROM sender_access "
                "WHERE account_name = ? AND sender_id = ?",
                (str(account_name), int(sender_id)),
            )
            if row and isinstance(row.get("access_hash"), int):
                return int(row["access_hash"])
        except Exception as e:
            logger.debug(f"get_sender_access_hash error: {type(e).__name__}")
        return None

    async def recent_chat_ids(self, limit: int = 30) -> List[int]:
        # v14.5.1: replaced "SELECT DISTINCT chat_id ... ORDER BY id" (which
        # is dialect-ambiguous because id is not in the SELECT list) with an
        # explicit GROUP BY that is deterministic on both SQLite and PG.
        try:
            rows = await self._fetchall(
                "SELECT chat_id FROM messages "
                "GROUP BY chat_id ORDER BY MAX(id) DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            )
            return [int(r["chat_id"]) for r in rows if r.get("chat_id") is not None]
        except Exception as e:
            logger.debug(f"recent_chat_ids error: {type(e).__name__}")
            return []

    async def update_sender_reputation(self, sender_id: int, is_valid: bool) -> None:
        try:
            if self.db_type == "postgresql":
                bump_expr = "LEAST(100.0, reputation_score + 2.0)"
                drop_expr = "GREATEST(0.0, reputation_score - 1.0)"
            else:
                bump_expr = "MIN(100.0, reputation_score + 2.0)"
                drop_expr = "MAX(0.0, reputation_score - 1.0)"
            if is_valid:
                await self._execute(
                    "UPDATE sender_stats SET "
                    "valid_requests = valid_requests + 1, "
                    f"reputation_score = {bump_expr}, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE sender_id = ?",
                    (sender_id,),
                )
            else:
                await self._execute(
                    "UPDATE sender_stats SET "
                    "invalid_requests = invalid_requests + 1, "
                    f"reputation_score = {drop_expr}, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE sender_id = ?",
                    (sender_id,),
                )
            await self._commit()
        except Exception as e:
            logger.error(f"update_sender_reputation error: {e}")

    async def get_sender_reputation(self, sender_id: int) -> float:
        try:
            row = await self._fetchone(
                "SELECT reputation_score FROM sender_stats WHERE sender_id = ?",
                (sender_id,),
            )
            return float(row["reputation_score"]) if row else 50.0
        except Exception:
            return 50.0

    async def get_alert_text_by_hash(self, msg_hash: str) -> Optional[str]:
        try:
            row = await self._fetchone(
                "SELECT alert_text FROM alerts WHERE message_hash = ? LIMIT 1",
                (msg_hash,),
            )
            return row["alert_text"] if row else None
        except Exception:
            return None

    async def get_message_text_by_hash(self, msg_hash: str) -> Optional[str]:
        try:
            row = await self._fetchone(
                "SELECT message_text FROM messages WHERE message_hash = ? LIMIT 1",
                (msg_hash,),
            )
            return row["message_text"] if row else None
        except Exception:
            return None

    async def get_alert_sender_by_hash(self, msg_hash: str) -> Optional[Dict[str, Any]]:
        try:
            row = await self._fetchone(
                "SELECT sender_id, chat_id, keyword FROM alerts "
                "WHERE message_hash = ? LIMIT 1",
                (msg_hash,),
            )
            if not row:
                return None
            out = {
                "sender_id": row["sender_id"],
                "chat_id": row["chat_id"],
                "keyword": row["keyword"],
            }
            try:
                contact = await self.get_sender_contact(int(row["sender_id"] or 0))
            except Exception:
                contact = None
            if contact:
                out["username"] = contact.get("username")
                out["access_hash"] = contact.get("access_hash")
                out["first_name"] = contact.get("first_name")
            return out
        except Exception:
            return None

    # ─── Anti-Spam ─────────────────────────────────────────────────────────
    async def is_spam_ignored(self, sender_id: int) -> bool:
        try:
            row = await self._fetchone(
                "SELECT 1 AS found FROM spam_ignore WHERE sender_id = ? LIMIT 1",
                (int(sender_id),),
            )
            return bool(row)
        except Exception:
            return False

    async def get_spam_ignored_entry(self, sender_id: int) -> Optional[Dict[str, Any]]:
        try:
            return await self._fetchone(
                "SELECT sender_id, reason, classified, created_at "
                "FROM spam_ignore WHERE sender_id = ? LIMIT 1",
                (int(sender_id),),
            )
        except Exception:
            return None

    async def add_spam_ignore(
        self, sender_id: int, reason: str = "", evidence: Optional[Dict] = None, classified: str = "cross_group_spam"
    ) -> None:
        try:
            ev = json_dumps(evidence or {})
            await self._execute(
                "INSERT INTO spam_ignore (sender_id, reason, evidence, classified) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(sender_id) DO UPDATE SET "
                "reason = excluded.reason, evidence = excluded.evidence, classified = excluded.classified",
                (int(sender_id), reason or "cross_group_spam", ev, classified),
            )
            await self._commit()
        except Exception as e:
            logger.error(f"add_spam_ignore failed for {sender_id}: {e}")
            raise

    async def remove_spam_ignore(self, sender_id: int) -> None:
        try:
            await self._execute("DELETE FROM spam_ignore WHERE sender_id = ?", (int(sender_id),))
            await self._commit()
        except Exception as e:
            logger.error(f"remove_spam_ignore failed for {sender_id}: {e}")
            raise

    async def get_spam_ignored(self, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            rows = await self._fetchall(
                "SELECT sender_id, reason, classified, created_at FROM spam_ignore "
                "ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            )
            return [dict(r) for r in rows] if rows else []
        except Exception:
            return []

    async def record_watch(
        self, sender_id: int, reason: str = "", evidence: Optional[Dict] = None, watch_until: float = 0.0
    ) -> None:
        try:
            ev = json_dumps(evidence or {})
            await self._execute(
                "INSERT INTO spam_watch (sender_id, reason, evidence, watch_until) "
                "VALUES (?, ?, ?, ?)",
                (int(sender_id), reason or "", ev, float(watch_until)),
            )
            await self._commit()
        except Exception as e:
            logger.debug(f"record_watch failed for {sender_id}: {e}")

    async def get_recent_watchlist(self, limit: int = 50, active_only: bool = False) -> List[Dict[str, Any]]:
        try:
            if active_only:
                rows = await self._fetchall(
                    "SELECT sender_id, reason, watch_until, created_at FROM spam_watch "
                    "WHERE watch_until > ? ORDER BY created_at DESC LIMIT ?",
                    (time.time(), int(limit)),
                )
            else:
                rows = await self._fetchall(
                    "SELECT sender_id, reason, watch_until, created_at FROM spam_watch "
                    "ORDER BY created_at DESC LIMIT ?",
                    (int(limit),),
                )
            return [dict(r) for r in rows] if rows else []
        except Exception:
            return []

    async def cleanup_old_watchlist(self, days: int = 7) -> int:
        try:
            cutoff = time.time() - (int(days) * 86400)
            cursor = await self._execute(
                "DELETE FROM spam_watch WHERE watch_until < ?", (cutoff,)
            )
            await self._commit()
            return cursor.rowcount if cursor and cursor.rowcount and cursor.rowcount > 0 else 0
        except Exception as e:
            logger.debug(f"cleanup_old_watchlist failed: {e}")
            return 0

    async def claim_alert_fingerprint(
        self, fingerprint: str, first_seen: float, window_hint: int = 86400
    ) -> bool:
        try:
            if self.db_type == "sqlite":
                cursor = await self._execute(
                    "INSERT OR IGNORE INTO alert_dedup "
                    "(fingerprint, sender_id, first_seen, last_seen, hits) "
                    "VALUES (?, ?, ?, ?, 1)",
                    (fingerprint, None, first_seen, first_seen),
                )
                inserted = bool(cursor.rowcount)
                if inserted:
                    await self._commit()
                    return True
                row = await self._fetchone(
                    "SELECT last_seen FROM alert_dedup WHERE fingerprint = ?",
                    (fingerprint,),
                )
                if row is None:
                    return True
                last_seen = float(row.get("last_seen") or 0.0)
                if first_seen - last_seen > max(1, window_hint):
                    await self._execute(
                        "UPDATE alert_dedup SET first_seen = ?, last_seen = ?, hits = 1 "
                        "WHERE fingerprint = ?",
                        (first_seen, first_seen, fingerprint),
                    )
                    await self._commit()
                    return True
                await self._execute(
                    "UPDATE alert_dedup SET last_seen = ?, hits = hits + 1 "
                    "WHERE fingerprint = ?",
                    (first_seen, fingerprint),
                )
                await self._commit()
                return False
            else:
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        inserted = await conn.fetchval(
                            "INSERT INTO alert_dedup "
                            "(fingerprint, sender_id, first_seen, last_seen, hits) "
                            "VALUES ($1, NULL, $2, $2, 1) "
                            "ON CONFLICT (fingerprint) DO NOTHING "
                            "RETURNING fingerprint",
                            fingerprint, first_seen,
                        )
                        if inserted is not None:
                            return True
                        row = await conn.fetchrow(
                            "SELECT last_seen FROM alert_dedup "
                            "WHERE fingerprint = $1 FOR UPDATE",
                            fingerprint,
                        )
                        if row is None:
                            return True
                        last_seen = float(row["last_seen"] or 0.0)
                        if first_seen - last_seen > max(1, window_hint):
                            await conn.execute(
                                "UPDATE alert_dedup SET first_seen = $2, last_seen = $2, "
                                "hits = 1 WHERE fingerprint = $1",
                                fingerprint, first_seen,
                            )
                            return True
                        await conn.execute(
                            "UPDATE alert_dedup SET last_seen = $2, hits = hits + 1 "
                            "WHERE fingerprint = $1",
                            fingerprint, first_seen,
                        )
                        return False
        except Exception as e:
            logger.error(f"claim_alert_fingerprint error: {e}")
            raise

    async def release_alert_fingerprint(self, fingerprint: str) -> None:
        try:
            await self._execute(
                "DELETE FROM alert_dedup WHERE fingerprint = ?", (fingerprint,)
            )
            await self._commit()
        except Exception as e:
            logger.debug(f"release_alert_fingerprint error: {e}")

    async def steal_stale_alert_fingerprint(
        self, fingerprint: str, min_age_seconds: float = 120.0
    ) -> bool:
        try:
            cutoff = time.time() - max(1.0, float(min_age_seconds))
            if self.db_type == "sqlite":
                cursor = await self._execute(
                    "DELETE FROM alert_dedup WHERE fingerprint = ? AND last_seen < ?",
                    (fingerprint, cutoff),
                )
                stolen = bool(cursor.rowcount)
                if stolen:
                    await self._commit()
                return stolen
            result = await self._pool.execute(
                "DELETE FROM alert_dedup WHERE fingerprint = $1 AND last_seen < $2",
                fingerprint, cutoff,
            )
            try:
                return int(str(result).split()[-1]) > 0
            except Exception:
                return False
        except Exception as e:
            logger.error(f"steal_stale_alert_fingerprint error: {e}")
            return False

    async def cleanup_alert_fingerprints(self, max_age_seconds: int = 86400) -> int:
        cutoff = time.time() - max(1, max_age_seconds)
        try:
            cur = await self._execute(
                "DELETE FROM alert_dedup WHERE last_seen < ?", (cutoff,)
            )
            deleted = cur.rowcount or 0
            await self._commit()
            if deleted:
                logger.debug(f"alert_dedup cleanup: {deleted} expired fingerprints removed")
            return deleted
        except Exception as e:
            logger.error(f"cleanup_alert_fingerprints error: {e}")
            return 0

    async def dedup_row_count(self) -> int:
        try:
            row = await self._fetchone("SELECT COUNT(*) AS cnt FROM alert_dedup")
            return int(row["cnt"]) if row else 0
        except Exception:
            return 0

    # ─── Blocklists ────────────────────────────────────────────────────────
    _BLOCKED_CACHE_PRUNE_SIZE = 5000

    @staticmethod
    def _prune_blocked_cache(cache: Dict[int, Tuple[bool, float]]) -> None:
        _cap = EnhancedDatabase._BLOCKED_CACHE_PRUNE_SIZE
        if len(cache) <= _cap:
            return
        now = time.time()
        for k in [k for k, (_, exp) in cache.items() if exp <= now]:
            cache.pop(k, None)
        if len(cache) > _cap:
            keep = sorted(cache.items(), key=lambda kv: kv[1][1], reverse=True)[: _cap // 2]
            cache.clear()
            cache.update(keep)

    async def is_blocked_sender(self, sender_id: int) -> bool:
        try:
            now = time.time()
            cached = self._blocked_senders_cache.get(sender_id)
            if cached is not None and now < cached[1]:
                return cached[0]
            row = await self._fetchone(
                "SELECT 1 AS hit FROM blocked_senders WHERE sender_id = ? LIMIT 1",
                (sender_id,),
            )
            blocked = row is not None
            ttl = max(5.0, float(os.getenv("BLOCKLIST_CACHE_TTL", "30")))
            self._blocked_senders_cache[sender_id] = (blocked, now + ttl)
            self._prune_blocked_cache(self._blocked_senders_cache)
            return blocked
        except Exception:
            # v14.5.1: fallback query was propagating the SAME exception that
            # triggered the fallback. Fail-open explicitly: a DB hiccup must
            # never cause the caller (monitors loop) to crash.
            try:
                row = await self._fetchone(
                    "SELECT 1 AS hit FROM blocked_senders WHERE sender_id = ? LIMIT 1",
                    (sender_id,),
                )
                return row is not None
            except Exception:
                return False

    async def is_blocked_chat(self, chat_id: int) -> bool:
        try:
            now = time.time()
            cached = self._blocked_chats_cache.get(chat_id)
            if cached is not None and now < cached[1]:
                return cached[0]
            row = await self._fetchone(
                "SELECT 1 AS hit FROM blocked_chats WHERE chat_id = ? LIMIT 1",
                (chat_id,),
            )
            blocked = row is not None
            ttl = max(5.0, float(os.getenv("BLOCKLIST_CACHE_TTL", "30")))
            self._blocked_chats_cache[chat_id] = (blocked, now + ttl)
            self._prune_blocked_cache(self._blocked_chats_cache)
            return blocked
        except Exception:
            try:
                row = await self._fetchone(
                    "SELECT 1 AS hit FROM blocked_chats WHERE chat_id = ? LIMIT 1",
                    (chat_id,),
                )
                return row is not None
            except Exception:
                return False

    async def block_sender(self, sender_id: int, reason: str = "", by: str = "system") -> None:
        try:
            await self._execute(
                self._sql_insert_ignore(
                    "INSERT OR IGNORE INTO blocked_senders (sender_id, reason, blocked_by) VALUES (?, ?, ?)"
                ),
                (sender_id, reason, by),
            )
            await self._commit()
            self._blocked_senders_cache.pop(sender_id, None)
            logger.info(f"Sender {sender_id} blocked: {reason}")
        except Exception as e:
            logger.error(f"block_sender error: {e}")

    async def unblock_sender(self, sender_id: int) -> None:
        await self._execute("DELETE FROM blocked_senders WHERE sender_id = ?", (sender_id,))
        await self._commit()
        self._blocked_senders_cache.pop(sender_id, None)

    async def block_chat(self, chat_id: int, reason: str = "", by: str = "system") -> None:
        try:
            await self._execute(
                self._sql_insert_ignore(
                    "INSERT OR IGNORE INTO blocked_chats (chat_id, reason, blocked_by) VALUES (?, ?, ?)"
                ),
                (chat_id, reason, by),
            )
            await self._commit()
            self._blocked_chats_cache.pop(chat_id, None)
            logger.info(f"Chat {chat_id} blocked: {reason}")
        except Exception as e:
            logger.error(f"block_chat error: {e}")

    async def unblock_chat(self, chat_id: int) -> None:
        await self._execute("DELETE FROM blocked_chats WHERE chat_id = ?", (chat_id,))
        await self._commit()
        self._blocked_chats_cache.pop(chat_id, None)

    # ─── Alerts ────────────────────────────────────────────────────────────
    async def add_alert(self, rec: AlertRecord) -> bool:
        async with self._stats_lock:
            self.stats["alerts_sent"] += 1

        async with self._batch_lock:
            if len(self._batch) >= _DB_BATCH_MAX_SIZE:
                evict_count = max(1, _DB_BATCH_MAX_SIZE // 10)
                del self._batch[:evict_count]
                logger.critical(
                    f"_batch hit hard cap ({_DB_BATCH_MAX_SIZE}); evicted {evict_count} oldest "
                    "alert records — these alerts are LOST. Check DB health immediately."
                )
            self._batch.append((
                "alert",
                rec.message_hash,
                rec.chat_id,
                rec.sender_id,
                rec.account_name,
                rec.keyword,
                rec.alert_text,
                rec.timestamp,
                rec.decision,
                rec.confidence,
                (rec.reasons or "")[:500],
                rec.intent_verb,
                rec.academic_object,
                rec.negation_detected,
                rec.advert_score,
                (rec.contact_method or "")[:20],
            ))
        return True

    async def add_alert_immediate(self, rec: AlertRecord) -> bool:
        try:
            async with self._stats_lock:
                self.stats["alerts_sent"] += 1
            params = (
                rec.message_hash, rec.chat_id, rec.sender_id, rec.account_name,
                rec.keyword, rec.alert_text, rec.timestamp, rec.decision,
                rec.confidence, (rec.reasons or "")[:500], rec.intent_verb,
                rec.academic_object, rec.negation_detected, rec.advert_score,
                (rec.contact_method or "")[:20],
            )
            changed = 0
            if self.db_type == "sqlite":
                cursor = await self._execute(
                    "INSERT OR IGNORE INTO alerts "
                    "(message_hash, chat_id, sender_id, account_name, keyword, alert_text, timestamp, "
                    " decision, confidence, reasons, intent_verb, academic_object, negation_detected, advert_score, "
                    " contact_method) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    params,
                )
                changed = cursor.rowcount or 0
            else:
                result = await self._pool.execute(
                    "INSERT INTO alerts "
                    "(message_hash, chat_id, sender_id, account_name, keyword, alert_text, timestamp, "
                    " decision, confidence, reasons, intent_verb, academic_object, negation_detected, advert_score, "
                    " contact_method) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15) "
                    "ON CONFLICT (message_hash) DO NOTHING",
                    *params,
                )
                try:
                    parts = str(result).split()
                    changed = int(parts[-1]) if parts and parts[-1].isdigit() else 0
                except Exception:
                    changed = 0
            if changed:
                try:
                    sid = int(rec.sender_id or 0)
                    ts = float(rec.timestamp or time.time())
                    if self.db_type == "sqlite":
                        await self._execute(
                            "UPDATE sender_stats "
                            "SET alerts_sent = alerts_sent + 1, last_alert_time = ? "
                            "WHERE sender_id = ?",
                            (ts, sid),
                        )
                    else:
                        await self._pool.execute(
                            "UPDATE sender_stats "
                            "SET alerts_sent = alerts_sent + 1, last_alert_time = $1 "
                            "WHERE sender_id = $2",
                            ts, sid,
                        )
                except Exception as ss_err:
                    logger.debug(f"add_alert_immediate sender_stats skipped: {type(ss_err).__name__}")
                self._invalidate_can_send_alert(int(rec.sender_id or 0))
                try:
                    from unified_alerts.stats import get_unified_stats as _gus
                    _gus().inc("receipts_immediate")
                except Exception:
                    pass
            await self._commit()
            return bool(changed)
        except Exception as e:
            logger.error(f"add_alert_immediate failed (receipt may be delayed to batch): {type(e).__name__}: {str(e)[:120]}")
            try:
                await self.add_alert(rec)
            except Exception:
                pass
            return False

    async def can_send_alert(self, sender_id: int) -> bool:
        try:
            now = time.time()
            cached = self._can_send_alert_cache.get(sender_id)
            if cached is not None and now < cached[1]:
                return cached[0]
            row = await self._fetchone(
                "SELECT last_alert_time, reputation_score FROM sender_stats WHERE sender_id = ?",
                (sender_id,),
            )
            if row and row.get("last_alert_time"):
                elapsed = now - float(row["last_alert_time"])
                rep = float(row.get("reputation_score") or 50.0)
                cooldown = max(30, CFG.ALERT_COOLDOWN * (1.0 - rep / 200.0))
                allowed = elapsed >= cooldown
            else:
                allowed = True
            ttl = max(2.0, float(os.getenv("CAN_SEND_ALERT_CACHE_TTL", "15")))
            if not allowed:
                ttl = min(ttl, 5.0)
            if len(self._can_send_alert_cache) >= 4096:
                try:
                    expired = [k for k, (_a, exp) in self._can_send_alert_cache.items() if exp <= now]
                    for k in expired:
                        self._can_send_alert_cache.pop(k, None)
                except Exception:
                    pass
                if len(self._can_send_alert_cache) >= 8192:
                    self._can_send_alert_cache.clear()
            self._can_send_alert_cache[sender_id] = (allowed, now + ttl)
            return allowed
        except Exception:
            return True

    def _invalidate_can_send_alert(self, sender_id: int) -> None:
        self._can_send_alert_cache.pop(sender_id, None)

    async def is_duplicate(self, h: str) -> bool:
        async with self._hash_lock:
            if h in self.message_cache:
                return True
        row = await self._fetchone(
            "SELECT 1 AS hit FROM messages WHERE message_hash = ? LIMIT 1", (h,)
        )
        return row is not None

    # ─── Stats ─────────────────────────────────────────────────────────────
    async def get_stats(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "total_messages": self.stats["total_messages"],
            "keywords_found": self.stats["keywords_found"],
            "alerts_sent": self.stats["alerts_sent"],
            "queue_evictions": self.stats.get("queue_evictions", 0),
            "uptime": time.time() - self.start_time,
            "db_healthy": self._db_healthy,
        }
        try:
            row = await self._fetchone("SELECT COUNT(DISTINCT sender_id) AS cnt FROM sender_stats")
            out["unique_senders"] = int(row["cnt"]) if row else 0

            row = await self._fetchone("SELECT COUNT(DISTINCT chat_id) AS cnt FROM messages")
            out["unique_chats"] = int(row["cnt"]) if row else 0

            row = await self._fetchone(
                "SELECT AVG(reputation_score) AS avg FROM sender_stats WHERE total_messages > 0"
            )
            out["avg_reputation"] = round(float(row["avg"]), 2) if row and row["avg"] else 0.0

            row = await self._fetchone("SELECT COUNT(*) AS cnt FROM blocked_senders")
            out["blocked_senders"] = int(row["cnt"]) if row else 0

            row = await self._fetchone("SELECT COUNT(*) AS cnt FROM blocked_chats")
            out["blocked_chats"] = int(row["cnt"]) if row else 0

            cutoff = time.time() - 3600
            row = await self._fetchone(
                "SELECT COUNT(*) AS cnt FROM messages WHERE timestamp > ?", (cutoff,)
            )
            out["messages_last_hour"] = int(row["cnt"]) if row else 0

            row = await self._fetchone(
                "SELECT COUNT(*) AS cnt FROM alerts WHERE timestamp > ?", (cutoff,)
            )
            out["alerts_last_hour"] = int(row["cnt"]) if row else 0

            decision_rows = await self._fetchall(
                "SELECT decision, COUNT(*) as count FROM alerts GROUP BY decision"
            )
            for r in decision_rows:
                out[f"decision_{r['decision']}"] = r["count"]

            row = await self._fetchone(
                "SELECT AVG(confidence) as avg_confidence FROM alerts WHERE confidence > 0"
            )
            out["avg_confidence"] = round(float(row["avg_confidence"]), 2) if row and row["avg_confidence"] else 0.0

        except Exception as e:
            logger.error(f"Stats query error: {e}")
        return out

    async def cleanup_old_data(self, days: int = 7, audit_days: Optional[int] = None,
                               notifications_days: Optional[int] = None) -> int:
        cutoff = time.time() - days * 86400
        total = 0
        try:
            cur = await self._execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
            total += cur.rowcount or 0
            cur = await self._execute("DELETE FROM alerts WHERE timestamp < ?", (cutoff,))
            total += cur.rowcount or 0
            try:
                await self._execute("DELETE FROM spam_watch WHERE watch_until < ?", (cutoff,))
            except Exception:
                pass
            try:
                await self.cleanup_old_audit_logs(90 if audit_days is None else max(1, int(audit_days)))
            except Exception:
                pass
            try:
                await self.cleanup_old_notifications(30 if notifications_days is None else max(1, int(notifications_days)))
            except Exception:
                pass
            try:
                intel_days = int(os.getenv("RETENTION_INTEL_DAYS", "90"))
                if intel_days > 0:
                    total += await self._cleanup_intel_tables(intel_days)
            except Exception as intel_err:
                logger.debug(f"intel retention skipped: {intel_err}")
            await self._commit()
            if total > 500 and self.db_type == "sqlite":
                await self._execute("PRAGMA optimize")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        return total

    async def _cleanup_intel_tables(self, days: int) -> int:
        from datetime import datetime as _dt, timezone as _tz

        cutoff = time.time() - max(1, int(days)) * 86400
        cutoff_ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(cutoff))
        cutoff_dt = _dt.fromtimestamp(cutoff, tz=_tz.utc)
        removed = 0
        plan = [
            ("group_messages", "message_date", "epoch"),
            ("message_analysis", "created_at", "epoch"),
            ("sender_contacts", "updated_at", "ts"),
        ]
        for table, col, kind in plan:
            try:
                if self.db_type == "postgresql":
                    cmp_val = cutoff_dt
                else:
                    cmp_val = cutoff if kind == "epoch" else cutoff_ts_str
                cur = await self._execute(
                    f"DELETE FROM {table} WHERE {col} IS NOT NULL AND {col} < ?",
                    (cmp_val,),
                )
                n = cur.rowcount or 0
                if n:
                    logger.info(f"Intel retention: {table} −{n} rows (> {days}d)")
                removed += n
            except Exception as e:
                logger.debug(f"intel retention {table} skipped: {type(e).__name__}: {str(e)[:100]}")
        return removed

    # ─── Notifications ─────────────────────────────────────────────────────
    _NOTIF_SEVERITIES = ("info", "success", "warn", "critical")

    async def record_notification(self, ntype: str = "", title: str = "",
                                  body: str = "", severity: str = "info",
                                  object_type: str = "", object_id: str = "") -> bool:
        try:
            if not await self.is_feature_enabled("notifications"):
                return False
        except Exception:
            pass
        try:
            sev = str(severity or "info").lower()
            if sev not in self._NOTIF_SEVERITIES:
                sev = "info"
            await self._execute(
                "INSERT INTO notifications (ntype, title, body, severity, object_type, object_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(ntype or "")[:80],
                    str(title or "")[:200],
                    str(body or "")[:500],
                    sev,
                    str(object_type or "")[:80],
                    str(object_id or "")[:200],
                ),
            )
            await self._commit()
            return True
        except Exception as e:
            logger.debug(f"record_notification skipped: {e}")
            return False

    async def get_notifications(self, limit: int = 30, offset: int = 0,
                                unread_only: bool = False) -> List[Dict[str, Any]]:
        try:
            limit = max(1, min(int(limit), 100))
            offset = max(0, int(offset))
        except Exception:
            limit, offset = 30, 0
        try:
            if unread_only:
                return await self._fetchall(
                    "SELECT * FROM notifications WHERE is_read = 0"
                    " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            return await self._fetchall(
                "SELECT * FROM notifications ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        except Exception as e:
            logger.debug(f"get_notifications skipped: {e}")
            return []

    async def count_unread_notifications(self) -> int:
        try:
            row = await self._fetchone("SELECT COUNT(*) AS n FROM notifications WHERE is_read = 0")
            return int(row["n"]) if row else 0
        except Exception:
            return 0

    async def mark_notification_read(self, notification_id: int) -> bool:
        try:
            cur = await self._execute(
                "UPDATE notifications SET is_read = 1 WHERE id = ?", (int(notification_id),)
            )
            await self._commit()
            return (cur.rowcount or 0) > 0
        except Exception:
            return False

    async def mark_all_notifications_read(self) -> int:
        try:
            cur = await self._execute("UPDATE notifications SET is_read = 1 WHERE is_read = 0")
            await self._commit()
            return cur.rowcount or 0
        except Exception:
            return 0

    async def cleanup_old_notifications(self, days: int = 30) -> int:
        try:
            days = max(1, int(days))
        except Exception:
            days = 30
        try:
            cutoff_dt = datetime.utcnow() - timedelta(days=days)
            if self.db_type == "sqlite":
                cur = await self._execute(
                    "DELETE FROM notifications WHERE created_at < ?",
                    (cutoff_dt.strftime("%Y-%m-%d %H:%M:%S"),),
                )
            else:
                cur = await self._execute(
                    "DELETE FROM notifications WHERE created_at < $1", (cutoff_dt,)
                )
            await self._commit()
            return cur.rowcount or 0
        except Exception as e:
            logger.debug(f"cleanup_old_notifications skipped: {e}")
            return 0

    # ─── Feature Registry ─────────────────────────────────────────────────
    FEATURE_REGISTRY: Dict[str, Dict[str, str]] = {
        "sources_scope": {"label": "🎯 نطاق المصادر", "category": "filtering",
                          "default": "1",
                          "impact": "الإيقاف = مراقبة كل شيء حتى مع قائمة مصادر مملوءة (سلوك ما قبل v9.20)."},
        "allowlist": {"label": "✅ قائمة السماح", "category": "filtering",
                      "default": "1",
                      "impact": "الإيقاف = فلترة عادية للجميع — الكيانات الموثوقة تفقد تمييزها (سلوك ما قبل v9.21)."},
        "rule_engine": {"label": "📏 محرك القواعد", "category": "filtering",
                        "default": "1",
                        "impact": "الإيقاف = لا تقييم للقواعد — القواعد المحفوظة سليمة لكنها لا تُطبَّق (سلوك ما قبل v9.21)."},
        "notifications": {"label": "🔔 مركز الإشعارات", "category": "operations",
                          "default": "1",
                          "impact": "الإيقاف = لا إشعارات جديدة من أي حدث (لوحتا البوت والويب معاً)."},
        "account_health": {"label": "❤️ صحة الحسابات", "category": "operations",
                           "default": "1",
                           "impact": "الإيقاف = لا رصد آلي لانقطاع الحسابات ولا إشعارات عودة."},
    }

    def _features_defaults(self) -> Dict[str, bool]:
        return {k: v["default"] == "1" for k, v in self.FEATURE_REGISTRY.items()}

    async def is_feature_enabled(self, key: str) -> bool:
        try:
            if key not in self.FEATURE_REGISTRY:
                return True
            now = time.time()
            cached = self._features_cache.get(key)
            if cached is not None and now < cached[0]:
                return cached[1]
            row = await self._fetchone("SELECT enabled FROM features WHERE key = ?", (key,))
            enabled = bool(row["enabled"]) if row else self._features_defaults()[key]
            ttl = max(5.0, float(os.getenv("FEATURES_CACHE_TTL", "30")))
            self._features_cache[key] = (now + ttl, enabled)
            return enabled
        except Exception:
            return True

    async def list_features(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        defaults = self._features_defaults()
        for key, meta in self.FEATURE_REGISTRY.items():
            try:
                row = await self._fetchone("SELECT enabled, updated_by, updated_at FROM features WHERE key = ?", (key,))
                enabled = bool(row["enabled"]) if row else defaults[key]
                overridden = row is not None
                updated_by = (row["updated_by"] if row else "") or ""
                updated_at = (row["updated_at"] if row else "") or ""
            except Exception:
                enabled, overridden, updated_by, updated_at = defaults[key], False, "", ""
            out.append({
                "key": key, "label": meta["label"], "category": meta["category"],
                "impact": meta["impact"], "enabled": enabled,
                "overridden": overridden, "updated_by": updated_by,
                "updated_at": updated_at,
            })
        return out

    async def set_feature_enabled(self, key: str, enabled: bool, by: str = "panel") -> bool:
        if key not in self.FEATURE_REGISTRY:
            return False
        try:
            await self._execute(
                "INSERT INTO features (key, enabled, updated_by) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET enabled=excluded.enabled,"
                " updated_by=excluded.updated_by, updated_at=CURRENT_TIMESTAMP",
                (key, 1 if enabled else 0, str(by or "panel")[:80]),
            )
            await self._commit()
            self._features_cache.pop(key, None)
            return True
        except Exception as e:
            logger.error(f"set_feature_enabled error: {e}")
            return False

    # ─── Audit Log ─────────────────────────────────────────────────────────
    async def record_audit(self, actor: str, action: str, object_type: str = "",
                           object_id: str = "", old_value: str = "", new_value: str = "",
                           source: str = "botpanel") -> bool:
        try:
            await self._execute(
                "INSERT INTO audit_logs (actor, action, object_type, object_id, old_value, new_value, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(actor or "unknown")[:120],
                    str(action or "unknown")[:120],
                    str(object_type or "")[:120],
                    str(object_id or "")[:200],
                    str(old_value or "")[:2000],
                    str(new_value or "")[:2000],
                    str(source or "botpanel")[:40],
                ),
            )
            await self._commit()
            return True
        except Exception as e:
            logger.debug(f"record_audit skipped: {e}")
            return False

    @staticmethod
    def _audit_filters(where: List[str], params: List[Any], action: Optional[str] = None,
                       actor: Optional[str] = None, q: Optional[str] = None,
                       since_hours: Optional[int] = None) -> None:
        if action:
            where.append("action = ?")
            params.append(str(action)[:120])
        if actor:
            where.append("actor = ?")
            params.append(str(actor)[:120])
        if q:
            needle = f"%{_escape_like(q)}%"
            where.append(
                "(action LIKE ? ESCAPE '\\' OR object_id LIKE ? ESCAPE '\\'"
                " OR actor LIKE ? ESCAPE '\\' OR old_value LIKE ? ESCAPE '\\'"
                " OR new_value LIKE ? ESCAPE '\\')"
            )
            params.extend([needle] * 5)
        if since_hours:
            try:
                hours = max(1, min(int(since_hours), 90 * 24))
            except Exception:
                hours = 0
            if hours > 0:
                cutoff = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
                where.append("created_at >= ?")
                params.append(cutoff)

    async def get_audit_logs(self, limit: int = 100, offset: int = 0,
                             action: Optional[str] = None,
                             actor: Optional[str] = None, q: Optional[str] = None,
                             since_hours: Optional[int] = None) -> List[Dict[str, Any]]:
        try:
            limit = max(1, min(int(limit), 500))
            offset = max(0, int(offset))
        except Exception:
            limit, offset = 100, 0
        try:
            where: List[str] = []
            params: List[Any] = []
            self._audit_filters(where, params, action=action, actor=actor, q=q,
                                since_hours=since_hours)
            sql = "SELECT * FROM audit_logs"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            return await self._fetchall(sql, tuple(params))
        except Exception as e:
            logger.debug(f"get_audit_logs skipped: {e}")
            return []

    async def count_audit_logs(self, action: Optional[str] = None,
                               actor: Optional[str] = None, q: Optional[str] = None,
                               since_hours: Optional[int] = None) -> int:
        try:
            where: List[str] = []
            params: List[Any] = []
            self._audit_filters(where, params, action=action, actor=actor, q=q,
                                since_hours=since_hours)
            sql = "SELECT COUNT(*) AS n FROM audit_logs"
            if where:
                sql += " WHERE " + " AND ".join(where)
            row = await self._fetchone(sql, tuple(params))
            return int(row["n"]) if row else 0
        except Exception:
            return 0

    async def cleanup_old_audit_logs(self, days: int = 90) -> int:
        try:
            days = max(1, int(days))
        except Exception:
            days = 90
        try:
            cutoff_dt = datetime.utcnow() - timedelta(days=days)
            if self.db_type == "sqlite":
                cur = await self._execute(
                    "DELETE FROM audit_logs WHERE created_at < ?",
                    (cutoff_dt.strftime("%Y-%m-%d %H:%M:%S"),),
                )
            else:
                cur = await self._execute(
                    "DELETE FROM audit_logs WHERE created_at < $1", (cutoff_dt,)
                )
            await self._commit()
            return cur.rowcount or 0
        except Exception as e:
            logger.debug(f"cleanup_old_audit_logs skipped: {e}")
            return 0

    # ─── Sources ───────────────────────────────────────────────────────────
    def invalidate_sources_cache(self) -> None:
        self._sources_cache = None

    async def add_source(self, chat_id: int, username: str = "", title: str = "",
                         type_: str = "", added_by: str = "panel",
                         notes: str = "") -> bool:
        try:
            await self._execute(
                "INSERT INTO sources (chat_id, username, title, type, enabled, added_by, notes)"
                " VALUES (?, ?, ?, ?, 1, ?, ?)"
                " ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username,"
                " title=excluded.title, type=excluded.type, notes=excluded.notes",
                (int(chat_id), str(username or "")[:80], str(title or "")[:200],
                 str(type_ or "")[:40], str(added_by or "panel")[:80],
                 str(notes or "")[:500]),
            )
            await self._commit()
            self.invalidate_sources_cache()
            return True
        except Exception as e:
            logger.error(f"add_source error: {e}")
            return False

    async def remove_source(self, chat_id: int) -> bool:
        try:
            cur = await self._execute("DELETE FROM sources WHERE chat_id = ?", (int(chat_id),))
            await self._commit()
            self.invalidate_sources_cache()
            return (cur.rowcount or 0) > 0
        except Exception as e:
            logger.error(f"remove_source error: {e}")
            return False

    async def set_source_enabled(self, chat_id: int, enabled: bool) -> bool:
        try:
            cur = await self._execute(
                "UPDATE sources SET enabled = ? WHERE chat_id = ?",
                (1 if enabled else 0, int(chat_id)),
            )
            await self._commit()
            self.invalidate_sources_cache()
            return (cur.rowcount or 0) > 0
        except Exception as e:
            logger.error(f"set_source_enabled error: {e}")
            return False

    async def get_source(self, chat_id: int) -> Optional[Dict[str, Any]]:
        try:
            return await self._fetchone("SELECT * FROM sources WHERE chat_id = ?", (int(chat_id),))
        except Exception:
            return None

    async def list_sources(self, limit: int = 500) -> List[Dict[str, Any]]:
        try:
            limit = max(1, min(int(limit), 1000))
        except Exception:
            limit = 500
        try:
            return await self._fetchall(
                "SELECT * FROM sources ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            )
        except Exception as e:
            logger.debug(f"list_sources skipped: {e}")
            return []

    async def is_source_allowed(self, chat_id: int) -> bool:
        try:
            now = time.time()
            if self._sources_cache is not None and now < self._sources_cache[0]:
                allowed_set = self._sources_cache[1]
            else:
                rows = await self._fetchall("SELECT chat_id FROM sources WHERE enabled = 1")
                allowed_set = {int(r["chat_id"]) for r in rows}
                ttl = max(5.0, float(os.getenv("SOURCES_CACHE_TTL", "30")))
                self._sources_cache = (now + ttl, allowed_set)
            if not allowed_set:
                return True
            return int(chat_id) in allowed_set
        except Exception:
            return True

    # ─── Rule Engine ───────────────────────────────────────────────────────
    RULE_CONDITION_KEYS = ("keyword", "source_chat_id", "sender_id", "min_confidence")
    RULE_ACTIONS = ("allow", "block", "tag")

    @staticmethod
    def validate_rule_conditions(conditions: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        if not isinstance(conditions, dict) or not conditions:
            return None, "conditions must be a non-empty object"
        clean: Dict[str, Any] = {}
        for key, value in conditions.items():
            if key not in EnhancedDatabase.RULE_CONDITION_KEYS:
                return None, f"unknown condition key: {key}"
            if key == "keyword":
                kw = str(value or "").strip()
                if not kw:
                    return None, "keyword must not be empty"
                if len(kw) > 120:
                    return None, "keyword too long (max 120)"
                clean[key] = kw
            elif key in ("source_chat_id", "sender_id"):
                try:
                    iv = int(value)
                except Exception:
                    return None, f"{key} must be an integer"
                clean[key] = iv
            else:
                try:
                    fv = float(value)
                except Exception:
                    return None, "min_confidence must be a number"
                if not (0.0 <= fv <= 1.0):
                    return None, "min_confidence must be within 0..1"
                clean[key] = fv
        if not clean:
            return None, "at least one condition is required"
        return clean, None

    async def add_rule(self, name: str, conditions: Dict[str, Any], action: str,
                       action_value: str = "", priority: int = 100,
                       created_by: str = "panel") -> Optional[Dict[str, Any]]:
        try:
            cur = await self._execute(
                "INSERT INTO rules (name, conditions, action, action_value, priority, enabled, created_by)"
                " VALUES (?, ?, ?, ?, ?, 1, ?)",
                (
                    str(name or "")[:120],
                    json.dumps(conditions, ensure_ascii=False),
                    str(action)[:20],
                    str(action_value or "")[:200],
                    max(1, min(int(priority), 100000)),
                    str(created_by or "panel")[:80],
                ),
            )
            await self._commit()
            self.invalidate_rules_cache()
            return await self.get_rule(cur.lastrowid)
        except Exception as e:
            logger.error(f"add_rule error: {e}")
            return None

    async def list_rules(self, limit: int = 200) -> List[Dict[str, Any]]:
        try:
            limit = max(1, min(int(limit), 500))
        except Exception:
            limit = 200
        try:
            rows = await self._fetchall(
                "SELECT * FROM rules ORDER BY priority ASC, id ASC LIMIT ?", (limit,)
            )
            for r in rows:
                try:
                    r["conditions"] = json.loads(r.get("conditions") or "{}")
                except Exception:
                    r["conditions"] = {}
            return rows
        except Exception as e:
            logger.debug(f"list_rules skipped: {e}")
            return []

    async def count_rules(self) -> int:
        try:
            row = await self._fetchone("SELECT COUNT(*) AS n FROM rules")
            return int(row["n"]) if row else 0
        except Exception:
            return 0

    async def get_rule(self, rule_id: int) -> Optional[Dict[str, Any]]:
        try:
            row = await self._fetchone("SELECT * FROM rules WHERE id = ?", (int(rule_id),))
            if row:
                try:
                    row["conditions"] = json.loads(row.get("conditions") or "{}")
                except Exception:
                    row["conditions"] = {}
            return row
        except Exception:
            return None

    async def update_rule(self, rule_id: int, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        allowed = {"name", "conditions", "action", "action_value", "priority"}
        sets: List[str] = []
        params: List[Any] = []
        for key, value in (updates or {}).items():
            if key not in allowed:
                continue
            if key == "conditions":
                clean, err = self.validate_rule_conditions(value)
                if err:
                    raise ValueError(err)
                sets.append("conditions = ?")
                params.append(json.dumps(clean, ensure_ascii=False))
            elif key == "priority":
                sets.append("priority = ?")
                params.append(max(1, min(int(value), 100000)))
            elif key == "action":
                if value not in self.RULE_ACTIONS:
                    raise ValueError(f"unknown action: {value}")
                sets.append("action = ?")
                params.append(str(value)[:20])
            else:
                sets.append(f"{key} = ?")
                params.append(str(value or "")[:200])
        if not sets:
            return await self.get_rule(rule_id)
        try:
            params.append(int(rule_id))
            await self._execute(f"UPDATE rules SET {', '.join(sets)} WHERE id = ?", tuple(params))
            await self._commit()
            self.invalidate_rules_cache()
            return await self.get_rule(rule_id)
        except ValueError:
            raise
        except Exception as e:
            logger.error(f"update_rule error: {e}")
            return None

    async def set_rule_enabled(self, rule_id: int, enabled: bool) -> bool:
        try:
            cur = await self._execute(
                "UPDATE rules SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, int(rule_id)),
            )
            await self._commit()
            self.invalidate_rules_cache()
            return (cur.rowcount or 0) > 0
        except Exception as e:
            logger.error(f"set_rule_enabled error: {e}")
            return False

    async def delete_rule(self, rule_id: int) -> bool:
        try:
            cur = await self._execute("DELETE FROM rules WHERE id = ?", (int(rule_id),))
            await self._commit()
            self.invalidate_rules_cache()
            return (cur.rowcount or 0) > 0
        except Exception as e:
            logger.error(f"delete_rule error: {e}")
            return False

    def invalidate_rules_cache(self) -> None:
        self._rules_cache = None
        try:
            pending = self._rule_hits_pending
            if pending:
                import asyncio as _aio
                try:
                    loop = _aio.get_running_loop()
                    # v14.5.1 (T4): كان loop.create_task بلا مرجع قوي —
                    # الـevent loop يحفظ مراجع ضعيفة فقط فربما تُجمع المهمة
                    # قبل إتمام كتابة rule_hits. spawn_bg يحفظ مرجعاً قوياً
                    # في task_util._tasks ويسترجع الاستثناء عند الفشل.
                    spawn_bg(self.flush_rule_hits(), name="db_flush_rule_hits")
                except RuntimeError:
                    pass
        except Exception:
            pass

    async def flush_rule_hits(self) -> int:
        if not self._rule_hits_pending:
            return 0
        snapshot = dict(self._rule_hits_pending)
        self._rule_hits_pending.clear()
        pushed = 0
        try:
            for rule_id, n in snapshot.items():
                if n <= 0:
                    continue
                cur = await self._execute(
                    "UPDATE rules SET hits = hits + ? WHERE id = ?", (int(n), int(rule_id))
                )
                pushed += cur.rowcount or 0
            await self._commit()
        except Exception as e:
            logger.debug(f"flush_rule_hits skipped: {e}")
            for rule_id, n in snapshot.items():
                self._rule_hits_pending[rule_id] = self._rule_hits_pending.get(rule_id, 0) + n
        return pushed

    async def apply_rules(self, text: str, source_chat_id: int = 0,
                          sender_id: int = 0, confidence: float = 0.0) -> Optional[Dict[str, Any]]:
        try:
            now = time.time()
            if self._rules_cache is not None and now < self._rules_cache[0]:
                rules = self._rules_cache[1]
            else:
                rules = await self.list_rules(limit=200)
                rules = [r for r in rules if r.get("enabled")]
                ttl = max(5.0, float(os.getenv("RULES_CACHE_TTL", "30")))
                self._rules_cache = (now + ttl, rules)
            if not rules:
                return None
            haystack = (text or "").lower()
            for rule in rules:
                conds = rule.get("conditions") or {}
                if not isinstance(conds, dict) or not conds:
                    continue
                matched = True
                for key, value in conds.items():
                    if key == "keyword":
                        if str(value).lower() not in haystack:
                            matched = False; break
                    elif key == "source_chat_id":
                        try:
                            if int(value) != int(source_chat_id): matched = False; break
                        except Exception: matched = False; break
                    elif key == "sender_id":
                        try:
                            if int(value) != int(sender_id): matched = False; break
                        except Exception: matched = False; break
                    elif key == "min_confidence":
                        try:
                            if float(confidence) < float(value): matched = False; break
                        except Exception: matched = False; break
                if matched:
                    rid = int(rule.get("id") or 0)
                    if rid > 0:
                        self._rule_hits_pending[rid] = self._rule_hits_pending.get(rid, 0) + 1
                    return {
                        "rule": rule,
                        "action": rule.get("action") or "tag",
                        "action_value": rule.get("action_value") or "",
                    }
            return None
        except Exception as e:
            logger.debug(f"apply_rules skipped: {e}")
            return None

    # ─── Allowlist ─────────────────────────────────────────────────────────
    def _invalidate_allowed_cache(self) -> None:
        self._allowed_cache = None

    async def add_allowed_entity(self, entity_type: str, entity_id: int,
                                 note: str = "") -> bool:
        etype = str(entity_type or "").strip().lower()
        if etype not in ("sender", "chat"):
            return False
        try:
            await self._execute(
                "INSERT INTO allowed_entities (entity_type, entity_id, note)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(entity_type, entity_id) DO UPDATE SET note=excluded.note",
                (etype, int(entity_id), str(note or "")[:200]),
            )
            await self._commit()
            self._invalidate_allowed_cache()
            return True
        except Exception as e:
            logger.error(f"add_allowed_entity error: {e}")
            return False

    async def remove_allowed_entity(self, entity_type: str, entity_id: int) -> bool:
        try:
            cur = await self._execute(
                "DELETE FROM allowed_entities WHERE entity_type = ? AND entity_id = ?",
                (str(entity_type).lower(), int(entity_id)),
            )
            await self._commit()
            self._invalidate_allowed_cache()
            return (cur.rowcount or 0) > 0
        except Exception as e:
            logger.error(f"remove_allowed_entity error: {e}")
            return False

    async def list_allowed_entities(self, limit: int = 500) -> List[Dict[str, Any]]:
        try:
            limit = max(1, min(int(limit), 1000))
        except Exception:
            limit = 500
        try:
            return await self._fetchall(
                "SELECT * FROM allowed_entities ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            )
        except Exception as e:
            logger.debug(f"list_allowed_entities skipped: {e}")
            return []

    async def _allowed_sets(self) -> Tuple[set, set]:
        now = time.time()
        if self._allowed_cache is not None and now < self._allowed_cache[0]:
            return self._allowed_cache[1], self._allowed_cache[2]
        rows = await self._fetchall("SELECT entity_type, entity_id FROM allowed_entities")
        senders = {int(r["entity_id"]) for r in rows if r["entity_type"] == "sender"}
        chats = {int(r["entity_id"]) for r in rows if r["entity_type"] == "chat"}
        ttl = max(5.0, float(os.getenv("ALLOWED_CACHE_TTL", "30")))
        self._allowed_cache = (now + ttl, senders, chats)
        return senders, chats

    async def is_sender_allowed(self, sender_id: int) -> bool:
        try:
            senders, _ = await self._allowed_sets()
            return int(sender_id) in senders
        except Exception:
            return False

    async def is_chat_allowed(self, chat_id: int) -> bool:
        try:
            _, chats = await self._allowed_sets()
            return int(chat_id) in chats
        except Exception:
            return False

    # ─── Export / Import ───────────────────────────────────────────────────
    async def export_critical_data(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"version": 1, "exported_at": time.time(), "tables": {}}
        for table, columns in CRITICAL_EXPORT_TABLES.items():
            if not columns:
                continue
            try:
                cols = ", ".join(columns)
                rows = await self._fetchall(f"SELECT {cols} FROM {table}")
                payload["tables"][table] = [dict(r) for r in rows]
            except Exception as e:
                logger.debug(f"export_critical_data skip {table}: {e}")
                payload["tables"][table] = []
        return payload

    async def import_critical_data(self, payload: Dict[str, Any],
                                   replace: bool = False) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        tables = (payload or {}).get("tables")
        if not isinstance(tables, dict):
            return counts
        cleared: set = set()
        try:
            for table, columns in CRITICAL_EXPORT_TABLES.items():
                if not columns:
                    continue
                rows = tables.get(table)
                if not isinstance(rows, list) or not rows:
                    continue
                imported = 0
                if replace and table not in cleared:
                    try:
                        await self._execute(f"DELETE FROM {table}")
                    except Exception:
                        pass
                    cleared.add(table)
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    values = []
                    ok = True
                    for col in columns:
                        val = row.get(col)
                        if val is None and col in _IMPORT_COLUMN_DEFAULTS:
                            val = _IMPORT_COLUMN_DEFAULTS[col]
                        if val is None and col in ("chat_id", "entity_id", "conditions"):
                            ok = False
                            break
                        if col == "conditions" and not isinstance(val, str):
                            val = json.dumps(val if isinstance(val, dict) else {},
                                             ensure_ascii=False)
                        values.append(val)
                    if not ok:
                        continue
                    try:
                        placeholders = ", ".join("?" for _ in columns)
                        await self._execute(
                            f"INSERT INTO {table} ({', '.join(columns)})"
                            f" VALUES ({placeholders})",
                            tuple(values),
                        )
                        imported += 1
                    except Exception:
                        continue
                counts[table] = imported
            await self._commit()
        except Exception as e:
            logger.debug(f"import_critical_data skipped: {e}")
        finally:
            self.invalidate_sources_cache()
            self.invalidate_rules_cache()
            self._invalidate_allowed_cache()
        return counts

    async def _resource_pressure_check(self) -> None:
        if self.db_type != "sqlite":
            return
        try:
            db_path = Path(CFG.DB_FILE)
            if not db_path.exists():
                return
            size_mb = db_path.stat().st_size / (1024 * 1024)
            if size_mb > CFG.MEMORY_THRESHOLD_MB:
                logger.critical(
                    f"⚠️  SQLite DB size {size_mb:.1f} MB exceeds threshold "
                    f"{CFG.MEMORY_THRESHOLD_MB} MB — triggering emergency cleanup"
                )
                await self.purge_queue()
                await self.cleanup_dead_letters(days=1)
                await self.cleanup_old_data(days=1)
        except Exception as e:
            logger.error(f"_resource_pressure_check error: {e}")

    # ─── Background Tasks ─────────────────────────────────────────────────
    async def start_writer(self) -> None:
        if self._writer_task is None or self._writer_task.done():
            self._writer_task = asyncio.create_task(self._writer_loop(), name="db_writer")
        if self._backup_task is None or self._backup_task.done():
            self._backup_task = asyncio.create_task(self._backup_loop(), name="db_backup")
        logger.info("Database background tasks started (writer + backup)")

    async def _writer_loop(self) -> None:
        ping_counter = 0
        while self.is_connected:
            try:
                await asyncio.sleep(CFG.DB_BATCH_INTERVAL)
                await self._flush()

                self._writer_cycle += 1

                if self._writer_cycle % _PRESSURE_CHECK_EVERY == 0:
                    await self._resource_pressure_check()
                if self._writer_cycle % _RULE_HITS_FLUSH_EVERY == 0:
                    await self.flush_rule_hits()

                ping_counter += 1
                if ping_counter >= (60 // max(CFG.DB_BATCH_INTERVAL, 1)):
                    ping_counter = 0
                    if not await self._ping():
                        logger.warning("Database ping failed, attempting reconnect…")
                        self._db_healthy = False
                        _ok_reconnect = await self._reconnect()
                        if _ok_reconnect:
                            break
                        self.is_connected = True
                        await asyncio.sleep(5)
                    else:
                        if not self._db_healthy:
                            logger.info("Database ping recovered — marking DB healthy")
                        self._db_healthy = True
                        self._flush_failure_count = 0
                        self._flush_backoff = 1.0

            except asyncio.CancelledError:
                await self._flush()
                break
            except Exception as e:
                logger.error(f"DB writer loop error: {e}")
                await asyncio.sleep(1)

    async def _flush(self) -> None:
        async with self._batch_lock:
            if not self._batch:
                return
            batch, self._batch = list(self._batch), []

        alerts_data = [item[1:] for item in batch if item[0] == "alert"]
        if not alerts_data:
            return

        try:
            if self.db_type == "postgresql":
                values = []
                params = []
                idx = 1
                for data in alerts_data:
                    values.append(
                        f"(${idx}, ${idx+1}, ${idx+2}, ${idx+3}, ${idx+4}, "
                        f"${idx+5}, ${idx+6}, ${idx+7}, ${idx+8}, ${idx+9}, "
                        f"${idx+10}, ${idx+11}, ${idx+12}, ${idx+13}, ${idx+14})"
                    )
                    params.extend(data)
                    idx += 15
                sql = (
                    "INSERT INTO alerts "
                    "(message_hash, chat_id, sender_id, account_name, keyword, alert_text, timestamp, "
                    " decision, confidence, reasons, intent_verb, academic_object, negation_detected, advert_score, "
                    " contact_method) "
                    f"VALUES {','.join(values)} ON CONFLICT (message_hash) DO NOTHING"
                )
                await self._pool.execute(sql, *params)
                sender_to_latest_ts: Dict[int, float] = {}
                sender_counts: Dict[int, int] = {}
                for data in alerts_data:
                    sid = int(data[2])
                    ts = float(data[6])
                    sender_to_latest_ts[sid] = max(sender_to_latest_ts.get(sid, 0.0), ts)
                    sender_counts[sid] = sender_counts.get(sid, 0) + 1
                update_params = [
                    (sender_to_latest_ts[sid], sender_counts[sid], sid)
                    for sid in sender_to_latest_ts
                ]
                if update_params:
                    await self._pool.executemany(
                        "UPDATE sender_stats "
                        "SET alerts_sent = alerts_sent + $2, last_alert_time = $1 "
                        "WHERE sender_id = $3",
                        update_params,
                    )
                for sid in sender_to_latest_ts:
                    self._invalidate_can_send_alert(sid)
            else:
                sql = (
                    "INSERT OR IGNORE INTO alerts "
                    "(message_hash, chat_id, sender_id, account_name, keyword, alert_text, timestamp, "
                    " decision, confidence, reasons, intent_verb, academic_object, negation_detected, advert_score, "
                    " contact_method) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                )
                params_list = [tuple(data) for data in alerts_data]
                await self._executemany(sql, params_list)
                sender_to_latest_ts: Dict[int, float] = {}
                sender_counts: Dict[int, int] = {}
                for data in alerts_data:
                    sid = int(data[2])
                    ts = float(data[6])
                    sender_to_latest_ts[sid] = max(sender_to_latest_ts.get(sid, 0.0), ts)
                    sender_counts[sid] = sender_counts.get(sid, 0) + 1
                update_params = [
                    (sender_to_latest_ts[sid], sender_counts[sid], sid)
                    for sid in sender_to_latest_ts
                ]
                if update_params:
                    # v14.5.1: SQLite clause reordered to match PG semantics.
                    # Previously the SQL was "+ ?, last_alert_time = ?" but the
                    # params were (latest_ts, count, sid) — the count went into
                    # last_alert_time and the epoch timestamp went into
                    # alerts_sent. Data corruption on SQLite-only deployments.
                    await self._executemany(
                        "UPDATE sender_stats "
                        "SET last_alert_time = ?, alerts_sent = alerts_sent + ? "
                        "WHERE sender_id = ?",
                        update_params,
                    )
                for sid in sender_to_latest_ts:
                    self._invalidate_can_send_alert(sid)
                await self._commit()

            self._flush_failure_count = 0
            self._flush_backoff = 1.0
            self._db_healthy = True
            logger.debug(f"Batch flush: {len(alerts_data)} alerts written")

        except Exception as e:
            self._flush_failure_count += 1
            self._db_healthy = self._flush_failure_count < 3
            backoff = min(self._flush_backoff, self._flush_backoff_max)
            logger.error(
                f"DB flush error (failure #{self._flush_failure_count}, "
                f"backoff {backoff:.1f}s): {e}"
            )

            async with self._batch_lock:
                available = _DB_BATCH_MAX_SIZE - len(self._batch)
                re_add = alerts_data[:available]
                evicted = len(alerts_data) - len(re_add)
                for data in re_add:
                    self._batch.append(("alert",) + tuple(data))
                if evicted > 0:
                    logger.critical(
                        f"DB flush: _batch cap reached — {evicted} alert records DROPPED "
                        "(DB failure, cannot buffer further). Check DB connectivity."
                    )

            self._flush_backoff = min(self._flush_backoff * 2, self._flush_backoff_max)
            await asyncio.sleep(backoff)

    # ─── Backup Loop ───────────────────────────────────────────────────────
    async def _backup_loop(self) -> None:
        if self.db_type != "sqlite":
            return

        loop = asyncio.get_running_loop()

        while self.is_connected:
            try:
                await asyncio.sleep(CFG.SESSION_BACKUP_INTERVAL)

                if not Path(CFG.DB_FILE).exists():
                    continue

                # v14.5.1: warn ONCE per session (was every cycle — hourly
                # noise). First cycle logs at WARNING; subsequent cycles at
                # DEBUG unless BACKUP_UPLOAD_URL is set.
                if not _BACKUP_UPLOAD_URL:
                    if not self._backup_warning_logged:
                        logger.warning(
                            "⚠️  Backup cycle: no BACKUP_UPLOAD_URL set. "
                            "Local .gz written but data remains ephemeral."
                        )
                        self._backup_warning_logged = True
                    else:
                        logger.debug(
                            "Backup cycle: no BACKUP_UPLOAD_URL — local-only backup"
                        )

                backup_path = Path(f"{CFG.DB_FILE}.gz")
                tmp_path = Path(f"{CFG.DB_FILE}.bak.tmp")

                try:
                    db_size = await loop.run_in_executor(
                        None, _sqlite_hot_backup, str(CFG.DB_FILE), str(tmp_path)
                    )

                    compressed_size, digest = await loop.run_in_executor(
                        None, _compress_file_to_gz, str(tmp_path), str(backup_path)
                    )

                    is_valid = await loop.run_in_executor(
                        None, _verify_gz, str(backup_path), digest
                    )
                    if not is_valid:
                        raise RuntimeError(
                            f"Backup integrity check FAILED for {backup_path}"
                        )

                    logger.info(
                        f"DB backup: {backup_path} "
                        f"(source={db_size:,}B → compressed={compressed_size:,}B, "
                        f"sha256={digest[:16]}…, verified=OK)"
                    )

                    if _BACKUP_UPLOAD_URL:
                        await self._upload_backup(backup_path, digest)

                    self._backup_failure_count = 0

                finally:
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except Exception:
                            pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._backup_failure_count += 1
                if self._backup_failure_count >= self._backup_failure_threshold:
                    logger.critical(
                        f"🚨 DB backup FAILED {self._backup_failure_count} consecutive times: {e}. "
                        "Data recovery may be impossible if instance restarts."
                    )
                else:
                    logger.error(f"Backup error (#{self._backup_failure_count}): {e}")
                await asyncio.sleep(60)

    async def _upload_backup(self, backup_path: Path, digest: str) -> None:
        if not AIOHTTP_AVAILABLE:
            logger.warning(
                "BACKUP_UPLOAD_URL is set but aiohttp is not installed. "
                "pip install aiohttp to enable external backup uploads."
            )
            return

        headers = {"X-Backup-SHA256": digest}
        if _BACKUP_UPLOAD_TOKEN:
            headers["Authorization"] = f"Bearer {_BACKUP_UPLOAD_TOKEN}"

        try:
            # v14.5.1: stream the file handle instead of loading the entire
            # backup into RAM. On large SQLite DBs the previous
            # read_bytes() approach could spike memory by hundreds of MB.
            file_size = backup_path.stat().st_size
            timeout = aiohttp.ClientTimeout(total=120)
            with open(backup_path, "rb") as f:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.put(
                        _BACKUP_UPLOAD_URL,
                        data=f,
                        headers=headers,
                    ) as resp:
                        if resp.status in (200, 201, 204):
                            logger.info(
                                f"Backup uploaded to {_BACKUP_UPLOAD_URL[:40]}… "
                                f"(status={resp.status}, size={file_size:,}B)"
                            )
                        else:
                            logger.error(
                                f"Backup upload failed: HTTP {resp.status} from "
                                f"{_BACKUP_UPLOAD_URL[:40]}…"
                            )
        except Exception as e:
            logger.error(f"Backup upload error: {e}")

    # ─── Close ─────────────────────────────────────────────────────────────
    async def close(self) -> None:
        self.is_connected = False
        for name in ("_writer_task", "_backup_task"):
            task = getattr(self, name)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            setattr(self, name, None)
        await self._flush()
        if self.db_type == "sqlite" and self._sqlite_conn:
            await self._sqlite_conn.close()
        elif self.db_type == "postgresql" and self._pool:
            await self._pool.close()
        logger.info("Database v14.5.1 closed")
