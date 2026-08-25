#!/usr/bin/env python3
"""
database.py – Unified Async Database Layer v9.0.1 (HARDENED EDITION)
Supports: SQLite (aiosqlite) and PostgreSQL (asyncpg)
Compatible with: config.py v13.1, monitors.py v9.7.1, filter_engine.py v14.3.2

v9.0.1 (this pass) — targeted fixes on top of v9.0:

  FIXED #13 — get_hourly_stats SQL error: the previous implementation
              referenced `decision` and `confidence` columns inside the
              `messages` table, but those columns exist only in `alerts`.
              This caused an OperationalError whenever the dashboard called
              the hourly stats endpoint. The query has been rewritten to
              source all alert-related aggregations from the `alerts` table
              while preserving the expected output shape.

  FIXED #14 — cleanup_dead_letters row-count accuracy: the method now uses
              explicit `rowcount` values from cursor executions instead of
              relying on the cumulative `total_changes` counter, which was
              inaccurate across multiple DELETE statements.

All other v9.0 fixes remain intact.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import sqlite3
import time
import zlib
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from cachetools import TTLCache
from loguru import logger

from config import CFG, fast_hash, json_dumps, json_loads

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

# Optional: aiohttp for backup upload (fix #2 / #7)
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

# =============================================================================
# Runtime constants (from env — not added to frozen CFG to avoid reload)
# =============================================================================
_BACKUP_UPLOAD_URL: Optional[str] = os.getenv("BACKUP_UPLOAD_URL")
_BACKUP_UPLOAD_TOKEN: Optional[str] = os.getenv("BACKUP_UPLOAD_TOKEN")

# Hard cap on the in-memory alert batch before eviction (fix #4)
_DB_BATCH_MAX_SIZE: int = int(os.getenv("DB_BATCH_MAX_SIZE", "500"))

# Hard cap on dead_letters rows (fix #11)
_DEAD_LETTER_MAX_ROWS: int = int(os.getenv("DEAD_LETTER_MAX_ROWS", "5000"))

# Resource pressure check interval (every N writer-loop cycles, fix #12)
_PRESSURE_CHECK_EVERY: int = int(os.getenv("DB_PRESSURE_CHECK_EVERY", "12"))


# =============================================================================
# Dataclasses (unchanged from v8.2 — full backward compat)
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
def _pg(sql: str) -> str:
    """Convert SQLite ?-placeholders to PostgreSQL $N placeholders."""
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
# Ephemeral-storage warning helper (fix #6)
# =============================================================================
def _warn_ephemeral_storage() -> None:
    """
    Emit a prominent warning when running SQLite without a backup upload
    target — all data is ephemeral and will be lost on instance restart.
    """
    if CFG.DB_TYPE != "sqlite":
        return
    if os.getenv("DATABASE_URL"):
        return  # PostgreSQL via DATABASE_URL — persistent
    if _BACKUP_UPLOAD_URL:
        logger.info(
            "SQLite mode: external backup upload configured → "
            f"BACKUP_UPLOAD_URL={_BACKUP_UPLOAD_URL[:40]}…"
        )
        return
    logger.warning(
        "⚠️  EPHEMERAL STORAGE WARNING: DB_TYPE=sqlite with no DATABASE_URL "
        "and no BACKUP_UPLOAD_URL. All data (messages, alerts, queue, "
        "dead-letters) WILL BE LOST on Render instance restart/redeploy. "
        "Set DATABASE_URL (PostgreSQL) for persistence, or set "
        "BACKUP_UPLOAD_URL to enable external backup uploads."
    )


# =============================================================================
# Blocking I/O helpers — run in executor to keep event loop free (fix #1/#8)
# =============================================================================
def _compress_bytes(data: bytes, level: int = 6) -> bytes:
    """CPU-bound compression — called via run_in_executor."""
    return zlib.compress(data, level)


def _decompress_bytes(data: bytes) -> bytes:
    return zlib.decompress(data)


def _sqlite_hot_backup(src_path: str, dst_path: str) -> int:
    """
    Use SQLite's built-in online backup API (sqlite3.connect().backup())
    to take a hot copy without reading the whole file into RAM first.
    Returns the size of the source DB file in bytes.
    """
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return os.path.getsize(src_path)


def _compress_file_to_gz(src_path: str, dst_path: str, level: int = 6) -> Tuple[int, str]:
    """
    Reads src_path, compresses with zlib, writes to dst_path.
    Returns (compressed_size, sha256_hex).
    Called via run_in_executor (fix #1).
    """
    data = Path(src_path).read_bytes()
    compressed = zlib.compress(data, level)
    Path(dst_path).write_bytes(compressed)
    digest = hashlib.sha256(compressed).hexdigest()
    return len(compressed), digest


def _verify_gz(path: str, expected_digest: str) -> bool:
    """Verify a .gz file is readable and matches the expected SHA-256 (fix #7)."""
    try:
        data = Path(path).read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_digest:
            return False
        zlib.decompress(data)  # Ensure it decompresses without error
        return True
    except Exception:
        return False


# =============================================================================
# Unified Database Class v9.0.1
# =============================================================================
class EnhancedDatabase:
    """
    Production-grade async database with IntentEngine support.
    All fixes from v9.0 plus targeted corrections for hourly stats and
    dead-letter cleanup row counts.
    """

    def __init__(self) -> None:
        self.db_type = CFG.DB_TYPE
        self._pool: Any = None
        self._sqlite_conn: Optional[Any] = None
        self.is_connected = False

        # Message dedup cache (bounded deque — not a leak source)
        self.message_cache: Deque[str] = deque(maxlen=min(CFG.MAX_CACHE_SIZE, 20_000))
        self._hash_lock = asyncio.Lock()

        # Queue access serialization (fix #9: cap enforcement under lock)
        self._queue_lock = asyncio.Lock()

        # Stats
        self.stats: Dict[str, int] = defaultdict(int)
        self._stats_lock = asyncio.Lock()
        self.start_time = time.time()

        # Alert batch writer (fix #4: bounded + backoff)
        self._batch: List[Tuple[Any, ...]] = []
        self._batch_lock = asyncio.Lock()
        self._flush_failure_count: int = 0
        self._flush_backoff: float = 1.0
        self._flush_backoff_max: float = 60.0
        self._db_healthy: bool = True

        # Background tasks
        self._writer_task: Optional[asyncio.Task] = None
        self._backup_task: Optional[asyncio.Task] = None

        # Dashboard query cache
        self._query_cache: TTLCache = TTLCache(maxsize=100, ttl=30)
        self._cache_lock = asyncio.Lock()

        # Backup health tracking
        self._backup_failure_count: int = 0
        self._backup_failure_threshold: int = 3

        # Resource-pressure check counter
        self._writer_cycle: int = 0

    # ── Public health property (fix #10) ────────────────────────────────────
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
            await self._create_indexes()
            self.is_connected = True
            await self.start_writer()
            logger.info(f"Database connected: {self.db_type.upper()} v9.0.1")
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
        dsn = os.getenv("DATABASE_URL")
        if dsn:
            self._pool = await asyncpg.create_pool(
                dsn=dsn,
                min_size=CFG.DB_POOL_MIN,
                max_size=CFG.DB_POOL_MAX,
                command_timeout=CFG.CONNECTION_TIMEOUT,
                server_settings={
                    "statement_timeout": "30000",
                    "idle_in_transaction_session_timeout": "60000",
                },
            )
        else:
            self._pool = await asyncpg.create_pool(
                host=CFG.DB_HOST,
                port=CFG.DB_PORT,
                database=CFG.DB_NAME,
                user=CFG.DB_USER,
                password=CFG.DB_PASSWORD,
                min_size=CFG.DB_POOL_MIN,
                max_size=CFG.DB_POOL_MAX,
                command_timeout=CFG.CONNECTION_TIMEOUT,
            )

    async def _ping(self) -> bool:
        try:
            await self._fetchone("SELECT 1 AS ping")
            return True
        except Exception:
            return False

    async def _reconnect(self) -> bool:
        logger.warning("Database reconnecting…")
        await self.close()
        await asyncio.sleep(2)
        return await self.connect()

    # ─── Low-level exec ───────────────────────────────────────────────────────
    async def _execute(self, sql: str, params: tuple = ()) -> Any:
        if self.db_type == "sqlite":
            cursor = await self._sqlite_conn.execute(sql, params)
            return cursor
        else:
            return await self._pool.execute(_pg(sql), *params)

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

    # ─── Schema v9.0 ─────────────────────────────────────────────────────────
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
                advert_score REAL DEFAULT 0.0
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
        """
        for stmt in stmts.split(";"):
            s = stmt.strip()
            if s:
                if self.db_type == "postgresql":
                    s = (
                        s.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
                        .replace("INTEGER NOT NULL", "BIGINT NOT NULL")
                        .replace("REAL DEFAULT (unixepoch())", "DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())")
                        .replace("DATETIME DEFAULT CURRENT_TIMESTAMP", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
                        .replace("REAL NOT NULL", "DOUBLE PRECISION NOT NULL")
                        .replace("REAL DEFAULT", "DOUBLE PRECISION DEFAULT")
                        .replace("REAL,", "DOUBLE PRECISION,")
                    )
                await self._execute(s)
        await self._commit()

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
        ]
        for idx in indexes:
            await self._execute(idx)
        await self._commit()

    # ─── Persistent Queue (fix #3 / #9) ──────────────────────────────────────
    async def add_to_queue(self, event_data: dict, priority: int = 5) -> int:
        if not self._db_healthy:
            logger.debug("add_to_queue: DB unhealthy, returning -2 (backpressure)")
            return -2

        try:
            async with self._queue_lock:
                if self.db_type == "sqlite":
                    row = await self._fetchone("SELECT COUNT(*) AS cnt FROM processing_queue")
                    current_size = int(row["cnt"]) if row else 0

                    if current_size >= CFG.MESSAGE_QUEUE_SIZE:
                        await self._execute(
                            "DELETE FROM processing_queue WHERE id = ("
                            "  SELECT id FROM processing_queue"
                            "  ORDER BY priority ASC, created_at ASC LIMIT 1"
                            ")"
                        )
                        await self._commit()
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
                    return cursor.lastrowid

                else:
                    async with self._pool.acquire() as conn:
                        async with conn.transaction():
                            row = await conn.fetchrow("SELECT COUNT(*) AS cnt FROM processing_queue")
                            current_size = int(row["cnt"]) if row else 0
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
                            return result["id"]

        except Exception as e:
            logger.error(f"add_to_queue failed: {e}")
            return -1

    async def pop_from_queue(self) -> Optional[dict]:
        try:
            if self.db_type == "sqlite":
                async with self._hash_lock:
                    cursor = await self._sqlite_conn.execute(
                        "SELECT id, event_data FROM processing_queue "
                        "ORDER BY priority DESC, created_at ASC LIMIT 1"
                    )
                    row = await cursor.fetchone()
                    if not row:
                        return None
                    await self._sqlite_conn.execute(
                        "DELETE FROM processing_queue WHERE id = ?", (row[0],)
                    )
                    await self._sqlite_conn.commit()
                return json_loads(row[1])
            else:
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        row = await conn.fetchrow(
                            "SELECT id, event_data FROM processing_queue "
                            "ORDER BY priority DESC, created_at ASC "
                            "LIMIT 1 FOR UPDATE SKIP LOCKED"
                        )
                        if not row:
                            return None
                        await conn.execute("DELETE FROM processing_queue WHERE id = $1", row["id"])
                        return json_loads(row["event_data"])
        except Exception as e:
            logger.error(f"pop_from_queue failed: {e}")
            return None

    async def queue_size(self) -> int:
        try:
            row = await self._fetchone("SELECT COUNT(*) AS cnt FROM processing_queue")
            return int(row["cnt"]) if row else 0
        except Exception:
            return 0

    async def purge_queue(self) -> int:
        try:
            if self.db_type == "sqlite":
                cursor = await self._execute("DELETE FROM processing_queue")
                count = cursor.rowcount if cursor else 0
                await self._commit()
                return count
            else:
                result = await self._pool.execute("DELETE FROM processing_queue")
                return int(result.split()[1])
        except Exception as e:
            logger.error(f"purge_queue error: {e}")
            return 0

    # ─── Dead Letters (fix #11) ───────────────────────────────────────────────
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
                    timestamp=(
                        r["created_at"]
                        if isinstance(r["created_at"], (int, float))
                        else r["created_at"].timestamp()
                    ),
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
        """
        Remove resolved and old dead letters.
        Also enforces _DEAD_LETTER_MAX_ROWS hard cap (fix #11).
        Row counts are now taken from cursor.rowcount instead of
        cumulative total_changes (fix #14).
        """
        cutoff = time.time() - days * 86400
        total = 0
        try:
            # 1) Remove all resolved rows
            cursor = await self._execute("DELETE FROM dead_letters WHERE resolved = 1")
            if self.db_type == "sqlite" and cursor:
                total += cursor.rowcount
            elif self.db_type == "postgresql":
                # In _execute for postgresql, we already execute; we ignore rowcount here
                pass

            # 2) Remove old unresolved rows
            cursor = await self._execute(
                "DELETE FROM dead_letters WHERE created_at < ?", (cutoff,)
            )
            if self.db_type == "sqlite" and cursor:
                total += cursor.rowcount

            # 3) Enforce hard row cap
            row = await self._fetchone("SELECT COUNT(*) AS cnt FROM dead_letters")
            current_count = int(row["cnt"]) if row else 0
            if current_count > _DEAD_LETTER_MAX_ROWS:
                excess = current_count - _DEAD_LETTER_MAX_ROWS
                cursor = await self._execute(
                    "DELETE FROM dead_letters WHERE id IN ("
                    "  SELECT id FROM dead_letters ORDER BY resolved DESC, created_at ASC LIMIT ?"
                    ")",
                    (excess,),
                )
                if self.db_type == "sqlite" and cursor:
                    total += cursor.rowcount
                logger.warning(
                    f"dead_letters exceeded hard cap ({_DEAD_LETTER_MAX_ROWS}), "
                    f"evicted {excess} rows"
                )

            await self._commit()
            if total > 0:
                logger.info(f"cleanup_dead_letters: {total} rows removed")
            return total
        except Exception as e:
            logger.error(f"cleanup_dead_letters error: {e}")
            return 0

    # ─── Messages ─────────────────────────────────────────────────────────────
    async def try_insert_message(self, rec: MessageRecord) -> bool:
        async with self._hash_lock:
            if rec.message_hash in self.message_cache:
                return False
            self.message_cache.append(rec.message_hash)

        try:
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
                changed = cursor.rowcount if cursor else 0
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
                changed = 1 if result == "INSERT 0 1" else 0
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

    # ─── Sender Contacts ──────────────────────────────────────────────────────
    async def upsert_sender_contact(self, sender_data: Dict[str, Any]) -> None:
        try:
            if self.db_type == "sqlite":
                await self._execute(
                    "INSERT INTO sender_contacts "
                    "(sender_id, access_hash, username, first_name, last_name, "
                    " last_chat_id, last_message_id, last_message_link, last_group_link) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(sender_id) DO UPDATE SET "
                    "access_hash = excluded.access_hash, "
                    "username = excluded.username, "
                    "first_name = excluded.first_name, "
                    "last_name = excluded.last_name, "
                    "last_chat_id = excluded.last_chat_id, "
                    "last_message_id = excluded.last_message_id, "
                    "last_message_link = excluded.last_message_link, "
                    "last_group_link = excluded.last_group_link, "
                    "updated_at = CURRENT_TIMESTAMP",
                    (
                        sender_data["sender_id"], sender_data.get("access_hash"),
                        sender_data.get("username"), sender_data.get("first_name"),
                        sender_data.get("last_name"), sender_data.get("chat_id"),
                        sender_data.get("message_id"), sender_data.get("msg_link"),
                        sender_data.get("group_link"),
                    ),
                )
                await self._commit()
            else:
                await self._pool.execute(
                    "INSERT INTO sender_contacts "
                    "(sender_id, access_hash, username, first_name, last_name, "
                    " last_chat_id, last_message_id, last_message_link, last_group_link) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
                    "ON CONFLICT (sender_id) DO UPDATE SET "
                    "access_hash = EXCLUDED.access_hash, "
                    "username = EXCLUDED.username, "
                    "first_name = EXCLUDED.first_name, "
                    "last_name = EXCLUDED.last_name, "
                    "last_chat_id = EXCLUDED.last_chat_id, "
                    "last_message_id = EXCLUDED.last_message_id, "
                    "last_message_link = EXCLUDED.last_message_link, "
                    "last_group_link = EXCLUDED.last_group_link, "
                    "updated_at = CURRENT_TIMESTAMP",
                    sender_data["sender_id"], sender_data.get("access_hash"),
                    sender_data.get("username"), sender_data.get("first_name"),
                    sender_data.get("last_name"), sender_data.get("chat_id"),
                    sender_data.get("message_id"), sender_data.get("msg_link"),
                    sender_data.get("group_link"),
                )
        except Exception as e:
            logger.error(f"upsert_sender_contact error: {e}")

    async def update_sender_reputation(self, sender_id: int, is_valid: bool) -> None:
        try:
            if is_valid:
                await self._execute(
                    "UPDATE sender_stats SET "
                    "valid_requests = valid_requests + 1, "
                    "reputation_score = MIN(100.0, reputation_score + 2.0), "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE sender_id = ?",
                    (sender_id,),
                )
            else:
                await self._execute(
                    "UPDATE sender_stats SET "
                    "invalid_requests = invalid_requests + 1, "
                    "reputation_score = MAX(0.0, reputation_score - 1.0), "
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

    # ─── Helpers for copy button ────────────────────────────────────────────
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

    # ─── Blocklists ───────────────────────────────────────────────────────────
    async def is_blocked_sender(self, sender_id: int) -> bool:
        row = await self._fetchone(
            "SELECT 1 AS hit FROM blocked_senders WHERE sender_id = ? LIMIT 1",
            (sender_id,),
        )
        return row is not None

    async def is_blocked_chat(self, chat_id: int) -> bool:
        row = await self._fetchone(
            "SELECT 1 AS hit FROM blocked_chats WHERE chat_id = ? LIMIT 1",
            (chat_id,),
        )
        return row is not None

    async def block_sender(self, sender_id: int, reason: str = "", by: str = "system") -> None:
        try:
            await self._execute(
                "INSERT OR IGNORE INTO blocked_senders (sender_id, reason, blocked_by) VALUES (?, ?, ?)",
                (sender_id, reason, by),
            )
            await self._commit()
            logger.info(f"Sender {sender_id} blocked: {reason}")
        except Exception as e:
            logger.error(f"block_sender error: {e}")

    async def unblock_sender(self, sender_id: int) -> None:
        await self._execute("DELETE FROM blocked_senders WHERE sender_id = ?", (sender_id,))
        await self._commit()

    async def block_chat(self, chat_id: int, reason: str = "", by: str = "system") -> None:
        try:
            await self._execute(
                "INSERT OR IGNORE INTO blocked_chats (chat_id, reason, blocked_by) VALUES (?, ?, ?)",
                (chat_id, reason, by),
            )
            await self._commit()
            logger.info(f"Chat {chat_id} blocked: {reason}")
        except Exception as e:
            logger.error(f"block_chat error: {e}")

    async def unblock_chat(self, chat_id: int) -> None:
        await self._execute("DELETE FROM blocked_chats WHERE chat_id = ?", (chat_id,))
        await self._commit()

    # ─── Alerts ───────────────────────────────────────────────────────────────
    async def add_alert(self, rec: AlertRecord) -> bool:
        """Queue an alert for batch write with IntentEngine fields."""
        async with self._stats_lock:
            self.stats["alerts_sent"] += 1

        async with self._batch_lock:
            # Enforce batch cap (fix #4): evict oldest when full
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
            ))
        return True

    async def can_send_alert(self, sender_id: int) -> bool:
        row = await self._fetchone(
            "SELECT last_alert_time, reputation_score FROM sender_stats WHERE sender_id = ?",
            (sender_id,),
        )
        if row and row.get("last_alert_time"):
            elapsed = time.time() - float(row["last_alert_time"])
            rep = float(row.get("reputation_score") or 50.0)
            cooldown = max(30, CFG.ALERT_COOLDOWN * (1.0 - rep / 200.0))
            return elapsed >= cooldown
        return True

    async def is_duplicate(self, h: str) -> bool:
        async with self._hash_lock:
            if h in self.message_cache:
                return True
        row = await self._fetchone(
            "SELECT 1 AS hit FROM messages WHERE message_hash = ? LIMIT 1", (h,)
        )
        return row is not None

    # ─── Stats & Maintenance ──────────────────────────────────────────────────
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

    async def cleanup_old_data(self, days: int = 7) -> int:
        """Remove old messages and alerts. Called exclusively from main.py."""
        cutoff = time.time() - days * 86400
        total = 0
        try:
            cursor = await self._execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
            if self.db_type == "sqlite" and cursor:
                total += cursor.rowcount
            cursor = await self._execute("DELETE FROM alerts WHERE timestamp < ?", (cutoff,))
            if self.db_type == "sqlite" and cursor:
                total += cursor.rowcount
            await self._commit()
            if total > 500 and self.db_type == "sqlite":
                await self._execute("PRAGMA optimize")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        return total

    # ─── Resource pressure check (fix #12) ────────────────────────────────────
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

    # ─── Background Tasks ─────────────────────────────────────────────────────
    async def start_writer(self) -> None:
        if self._writer_task is None:
            self._writer_task = asyncio.create_task(self._writer_loop(), name="db_writer")
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

                ping_counter += 1
                if ping_counter >= (60 // max(CFG.DB_BATCH_INTERVAL, 1)):
                    ping_counter = 0
                    if not await self._ping():
                        logger.warning("Database ping failed, attempting reconnect…")
                        self._db_healthy = False
                        await self._reconnect()
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
                        f"${idx+10}, ${idx+11}, ${idx+12}, ${idx+13})"
                    )
                    params.extend(data)
                    idx += 14
                sql = (
                    "INSERT INTO alerts "
                    "(message_hash, chat_id, sender_id, account_name, keyword, alert_text, timestamp, "
                    " decision, confidence, reasons, intent_verb, academic_object, negation_detected, advert_score) "
                    f"VALUES {','.join(values)} ON CONFLICT (message_hash) DO NOTHING"
                )
                await self._pool.execute(sql, *params)
                for data in alerts_data:
                    await self._pool.execute(
                        "UPDATE sender_stats SET alerts_sent = alerts_sent + 1, last_alert_time = $1 "
                        "WHERE sender_id = $2",
                        data[6], data[2],
                    )
            else:
                sql = (
                    "INSERT OR IGNORE INTO alerts "
                    "(message_hash, chat_id, sender_id, account_name, keyword, alert_text, timestamp, "
                    " decision, confidence, reasons, intent_verb, academic_object, negation_detected, advert_score) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                )
                params_list = [tuple(data) for data in alerts_data]
                await self._executemany(sql, params_list)
                for data in alerts_data:
                    await self._execute(
                        "UPDATE sender_stats SET alerts_sent = alerts_sent + 1, last_alert_time = ? "
                        "WHERE sender_id = ?",
                        (data[6], data[2]),
                    )
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

    # ─── Backup Loop (fixes #1 / #2 / #7 / #8) ───────────────────────────────
    async def _backup_loop(self) -> None:
        if self.db_type != "sqlite":
            return

        loop = asyncio.get_event_loop()

        while self.is_connected:
            try:
                await asyncio.sleep(CFG.SESSION_BACKUP_INTERVAL)

                if not Path(CFG.DB_FILE).exists():
                    continue

                if not _BACKUP_UPLOAD_URL:
                    logger.warning(
                        "⚠️  Backup cycle: no BACKUP_UPLOAD_URL set. "
                        "Local .gz written but data remains ephemeral."
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
            data = backup_path.read_bytes()
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.put(
                    _BACKUP_UPLOAD_URL,
                    data=data,
                    headers=headers,
                ) as resp:
                    if resp.status in (200, 201, 204):
                        logger.info(
                            f"Backup uploaded to {_BACKUP_UPLOAD_URL[:40]}… "
                            f"(status={resp.status}, size={len(data):,}B)"
                        )
                    else:
                        logger.error(
                            f"Backup upload failed: HTTP {resp.status} from "
                            f"{_BACKUP_UPLOAD_URL[:40]}…"
                        )
        except Exception as e:
            logger.error(f"Backup upload error: {e}")

    # ─── Dashboard Queries ────────────────────────────────────────────────────
    async def get_messages_with_filters(
        self,
        limit: int = 50,
        offset: int = 0,
        keyword: Optional[str] = None,
        account: Optional[str] = None,
        chat_id: Optional[int] = None,
        sender_id: Optional[int] = None,
        from_date: Optional[float] = None,
        to_date: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        sql = """
            SELECT m.*, s.username, s.first_name, s.last_name
            FROM messages m
            LEFT JOIN sender_contacts s ON m.sender_id = s.sender_id
            WHERE 1=1
        """
        params = []
        if keyword:
            sql += " AND m.message_text LIKE ?"
            params.append(f"%{keyword}%")
        if chat_id:
            sql += " AND m.chat_id = ?"
            params.append(chat_id)
        if sender_id:
            sql += " AND m.sender_id = ?"
            params.append(sender_id)
        if from_date:
            sql += " AND m.timestamp >= ?"
            params.append(from_date)
        if to_date:
            sql += " AND m.timestamp <= ?"
            params.append(to_date)
        sql += " ORDER BY m.timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return await self._fetchall(sql, tuple(params))

    async def get_alerts_with_filters(
        self,
        limit: int = 50,
        offset: int = 0,
        keyword: Optional[str] = None,
        account: Optional[str] = None,
        sender_id: Optional[int] = None,
        from_date: Optional[float] = None,
        to_date: Optional[float] = None,
        decision: Optional[str] = None,
        min_confidence: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        sql = """
            SELECT a.*, s.username, s.first_name, s.last_name
            FROM alerts a
            LEFT JOIN sender_contacts s ON a.sender_id = s.sender_id
            WHERE 1=1
        """
        params = []
        if keyword:
            sql += " AND a.keyword LIKE ?"
            params.append(f"%{keyword}%")
        if account:
            sql += " AND a.account_name = ?"
            params.append(account)
        if sender_id:
            sql += " AND a.sender_id = ?"
            params.append(sender_id)
        if from_date:
            sql += " AND a.timestamp >= ?"
            params.append(from_date)
        if to_date:
            sql += " AND a.timestamp <= ?"
            params.append(to_date)
        if decision:
            sql += " AND a.decision = ?"
            params.append(decision)
        if min_confidence is not None:
            sql += " AND a.confidence >= ?"
            params.append(min_confidence)
        sql += " ORDER BY a.timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return await self._fetchall(sql, tuple(params))

    async def get_hourly_stats(self, hours: int = 24) -> List[Dict[str, Any]]:
        """
        Hourly statistics sourced from the `alerts` table (fix #13).
        Returns messages_count as the total number of alerts in that hour,
        alerts_count as the number with keyword_found set (if we add it later),
        and accepted_count / avg_confidence from decision/confidence columns.
        """
        cutoff = time.time() - (hours * 3600)
        if self.db_type == "sqlite":
            sql = """
                SELECT
                    strftime('%Y-%m-%d %H:00:00', datetime(timestamp, 'unixepoch')) as hour,
                    COUNT(*) as messages_count,
                    SUM(CASE WHEN keyword IS NOT NULL AND keyword != '' THEN 1 ELSE 0 END) as alerts_count,
                    SUM(CASE WHEN decision = 'accept' THEN 1 ELSE 0 END) as accepted_count,
                    AVG(CASE WHEN confidence > 0 THEN confidence END) as avg_confidence
                FROM alerts
                WHERE timestamp >= ?
                GROUP BY hour
                ORDER BY hour ASC
            """
        else:
            sql = """
                SELECT
                    to_char(to_timestamp(timestamp), 'YYYY-MM-DD HH24:00:00') as hour,
                    COUNT(*) as messages_count,
                    SUM(CASE WHEN keyword IS NOT NULL AND keyword != '' THEN 1 ELSE 0 END) as alerts_count,
                    SUM(CASE WHEN decision = 'accept' THEN 1 ELSE 0 END) as accepted_count,
                    AVG(CASE WHEN confidence > 0 THEN confidence END) as avg_confidence
                FROM alerts
                WHERE timestamp >= $1
                GROUP BY hour
                ORDER BY hour ASC
            """
        return await self._fetchall(sql, (cutoff,))

    async def get_top_keywords(self, limit: int = 20) -> List[Dict[str, Any]]:
        sql = """
            SELECT keyword, COUNT(*) as count
            FROM alerts
            WHERE keyword IS NOT NULL AND keyword != ''
            GROUP BY keyword
            ORDER BY count DESC
            LIMIT ?
        """
        return await self._fetchall(sql, (limit,))

    async def get_top_senders(self, limit: int = 20) -> List[Dict[str, Any]]:
        sql = """
            SELECT m.sender_id,
                   COUNT(*) as total_messages,
                   SUM(CASE WHEN m.keyword_found IS NOT NULL THEN 1 ELSE 0 END) as alerts_count,
                   c.username, c.first_name, c.last_name,
                   s.reputation_score
            FROM messages m
            LEFT JOIN sender_contacts c ON m.sender_id = c.sender_id
            LEFT JOIN sender_stats s ON m.sender_id = s.sender_id
            GROUP BY m.sender_id
            ORDER BY total_messages DESC
            LIMIT ?
        """
        return await self._fetchall(sql, (limit,))

    async def get_dashboard_summary(self) -> Dict[str, Any]:
        cache_key = "dashboard_summary"
        async with self._cache_lock:
            if cache_key in self._query_cache:
                return dict(self._query_cache[cache_key])

        summary: Dict[str, Any] = {
            "total_messages": 0,
            "total_alerts": 0,
            "unique_senders": 0,
            "unique_chats": 0,
            "avg_reputation": 0.0,
            "blocked_senders": 0,
            "accepted_count": 0,
            "reviewed_count": 0,
            "ignored_count": 0,
            "avg_confidence": 0.0,
            "queue_evictions": self.stats.get("queue_evictions", 0),
            "db_healthy": self._db_healthy,
        }
        try:
            row = await self._fetchone("SELECT COUNT(*) as cnt FROM messages")
            summary["total_messages"] = int(row["cnt"]) if row else 0
            row = await self._fetchone("SELECT COUNT(*) as cnt FROM alerts")
            summary["total_alerts"] = int(row["cnt"]) if row else 0
            row = await self._fetchone("SELECT COUNT(DISTINCT sender_id) as cnt FROM messages")
            summary["unique_senders"] = int(row["cnt"]) if row else 0
            row = await self._fetchone("SELECT COUNT(DISTINCT chat_id) as cnt FROM messages")
            summary["unique_chats"] = int(row["cnt"]) if row else 0
            row = await self._fetchone(
                "SELECT AVG(reputation_score) as avg FROM sender_stats WHERE total_messages > 0"
            )
            summary["avg_reputation"] = float(row["avg"]) if row and row["avg"] else 0.0
            row = await self._fetchone("SELECT COUNT(*) as cnt FROM blocked_senders")
            summary["blocked_senders"] = int(row["cnt"]) if row else 0
            row = await self._fetchone("SELECT COUNT(*) as cnt FROM alerts WHERE decision = 'accept'")
            summary["accepted_count"] = int(row["cnt"]) if row else 0
            row = await self._fetchone("SELECT COUNT(*) as cnt FROM alerts WHERE decision = 'review'")
            summary["reviewed_count"] = int(row["cnt"]) if row else 0
            row = await self._fetchone("SELECT COUNT(*) as cnt FROM alerts WHERE decision = 'ignore'")
            summary["ignored_count"] = int(row["cnt"]) if row else 0
            row = await self._fetchone("SELECT AVG(confidence) as avg FROM alerts WHERE confidence > 0")
            summary["avg_confidence"] = float(row["avg"]) if row and row["avg"] else 0.0

            async with self._cache_lock:
                self._query_cache[cache_key] = summary
        except Exception as e:
            logger.error(f"Dashboard summary error: {e}")
        return summary

    async def get_recent_alerts_for_dashboard(self, limit: int = 10) -> List[Dict[str, Any]]:
        sql = """
            SELECT a.*, s.username, s.first_name, s.last_name
            FROM alerts a
            LEFT JOIN sender_contacts s ON a.sender_id = s.sender_id
            ORDER BY a.timestamp DESC
            LIMIT ?
        """
        return await self._fetchall(sql, (limit,))

    async def get_recent_messages_for_dashboard(self, limit: int = 10) -> List[Dict[str, Any]]:
        sql = """
            SELECT m.*, s.username, s.first_name, s.last_name
            FROM messages m
            LEFT JOIN sender_contacts s ON m.sender_id = s.sender_id
            ORDER BY m.timestamp DESC
            LIMIT ?
        """
        return await self._fetchall(sql, (limit,))

    async def fetch_recent_alerts(self, limit: int = 50) -> List[Dict[str, Any]]:
        return await self.get_recent_alerts_for_dashboard(limit)

    async def fetch_top_senders(self, limit: int = 20) -> List[Dict[str, Any]]:
        return await self.get_top_senders(limit)

    # ─── Close ────────────────────────────────────────────────────────────────
    async def close(self) -> None:
        self.is_connected = False
        for task in (self._writer_task, self._backup_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self._flush()
        if self.db_type == "sqlite" and self._sqlite_conn:
            await self._sqlite_conn.close()
        elif self.db_type == "postgresql" and self._pool:
            await self._pool.close()
        logger.info("Database v9.0.1 closed")