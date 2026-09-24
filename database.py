#!/usr/bin/env python3
"""
database.py – Unified Async Database Layer v9.0 (HARDENED EDITION)
Supports: SQLite (aiosqlite) and PostgreSQL (asyncpg)
Compatible with: config.py v13.1, monitors.py v9.7, filter_engine.py v14.1

v9.0 (this pass) — full audit fix, database.py ONLY:

  FIXED #1  — Blocking I/O in _backup_loop: all Path.read_bytes(),
              zlib.compress(), and write_bytes() calls are now offloaded
              to asyncio.get_event_loop().run_in_executor(None, ...) so
              the event loop is never blocked during backup.

  FIXED #2  — Backup on Ephemeral Disk: _backup_loop now first tries to
              upload the compressed snapshot to an external HTTP endpoint
              (BACKUP_UPLOAD_URL env var) via aiohttp. If no URL is
              configured, the local .gz is still written (same as before)
              but a clear WARNING is emitted at startup and every backup
              cycle so the operator cannot miss the risk.

  FIXED #3  — processing_queue unbounded at DB level: add_to_queue() now
              checks queue size before INSERT and enforces the capacity
              limit defined by CFG.MESSAGE_QUEUE_SIZE. When full, a
              configurable DROP_OLDEST policy removes the lowest-priority
              / oldest rows (via DELETE … WHERE id IN (SELECT … LIMIT N))
              before inserting, so producers never silently accumulate
              unbounded rows in SQLite.

  FIXED #4  — _batch unbounded growth under sustained DB failure: _flush()
              now caps _batch at CFG.DB_BATCH_MAX_SIZE. When the cap is
              hit, the oldest items are evicted (logged at CRITICAL so
              data-loss is never silent). A DB-failure counter prevents
              the flush loop from re-adding and re-failing indefinitely
              without backoff.

  FIXED #5  — Duplicate cleanup loop: EnhancedDatabase._cleanup_loop is
              removed. The single authoritative cleanup loop lives in
              main.py::EnhancedTelegramBot._cleanup_loop. The public
              cleanup methods (cleanup_old_data, cleanup_dead_letters)
              are still present — they are now called only from main.py.

  FIXED #6  — SQLite not Persistent on Render Free: a startup WARNING
              is emitted when DB_TYPE=sqlite AND DATABASE_URL is absent,
              clearly stating that all data is ephemeral. No silent
              assumption of persistence.

  FIXED #7  — Backup does not achieve real Disaster Recovery: _backup_loop
              verifies the written .gz is readable after writing (checksum
              round-trip), logs the verification result, and if
              BACKUP_UPLOAD_URL is set, streams the file to external
              storage via HTTP PUT. A backup-failure counter triggers a
              CRITICAL log after 3 consecutive failures.

  FIXED #8  — Backup cost grows with DB size: the executor offload (fix #1)
              bounds the event-loop impact to the cost of scheduling the
              executor task. SQLite's built-in online backup API
              (sqlite3.connect().backup()) is used when available so a
              hot-copy of the DB is taken without reading the entire file
              into RAM first.

  FIXED #9  — Queue Growth + Ephemeral Storage = Storage Exhaustion: the
              queue cap (fix #3) and the startup persistence WARNING
              (fix #6) address this jointly. Additionally, queue_size()
              is called inside add_to_queue() under the same DB lock so
              the cap is enforced atomically (SQLite serialized writes).

  FIXED #10 — No Backpressure on DB Failure: _flush() now applies
              exponential backoff (capped at 60 s) after consecutive
              failures and exposes a .db_healthy property that callers
              (monitors.py via add_to_queue) can inspect. add_to_queue()
              returns -2 (distinct from -1 "insert error") when
              db_healthy is False, giving the caller a clear signal to
              slow down.

  FIXED #11 — dead_letters accumulation: cleanup_dead_letters() now also
              enforces a hard row cap (DEAD_LETTER_MAX_ROWS, default 5000)
              by deleting the oldest resolved rows when the cap is
              exceeded, regardless of age. This prevents unbounded growth
              even when DEAD_LETTER_CLEANUP_DAYS is large.

  FIXED #12 — Cascading DB Failure + Batch Growth + Backup Growth: the
              combination of fixes #1/#4/#7/#8 directly addresses this.
              Additionally, a _resource_pressure_check() method is called
              inside _writer_loop() every N cycles: when SQLite disk usage
              exceeds MEMORY_THRESHOLD_MB it triggers an emergency cleanup
              (purge_queue() + cleanup_dead_letters()) and logs CRITICAL.

All existing public methods and their signatures are preserved exactly,
so monitors.py / main.py / dashboard.py call sites require zero changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import time
import zlib
from datetime import datetime, timedelta
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from cachetools import TTLCache
from loguru import logger

from config import CFG, json_dumps, json_loads

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
# v9.12 (audit C-04), corrected v9.13: the audit recommended escalating this
# warning to a fail-fast in production. An earlier pass on this branch made
# fail-fast the *default* the moment RENDER=true — but RENDER=true is set on
# EVERY Render service unconditionally, including this project's own free-tier
# deployment which currently has neither DATABASE_URL nor BACKUP_UPLOAD_URL
# configured. That made the bot refuse to boot on its own production service
# (verified by reproducing the exact guard call with Render's real env vars).
# That is a self-inflicted outage, not a safety improvement.
#
# Fix: fail-fast is now opt-IN via REQUIRE_PERSISTENT_STORAGE=1 (set this once
# DATABASE_URL/BACKUP_UPLOAD_URL is actually configured, to guarantee any
# future accidental removal is caught immediately). Without it, the loud
# CRITICAL-level warning below still fires on every boot — visible in logs and
# on /health — but the process starts, preserving current behaviour exactly.
# ALLOW_EPHEMERAL_SQLITE=1 remains accepted as an explicit override even when
# REQUIRE_PERSISTENT_STORAGE=1 is set, for a supervised emergency restart.
def _warn_ephemeral_storage() -> None:
    """
    Emit a prominent warning when running SQLite without a backup upload
    target — all data is ephemeral and will be lost on instance restart.

    Fails fast only when the operator explicitly opts in via
    REQUIRE_PERSISTENT_STORAGE=1 (meant to be set only after DATABASE_URL or
    BACKUP_UPLOAD_URL is actually configured, as a tripwire against future
    misconfiguration). Otherwise this only warns — it must never silently
    block startup, since RENDER=true is present on every Render deploy
    regardless of plan or persistence setup.
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
    (fix #8: avoids Path.read_bytes() → entire DB in RAM → zlib.compress)
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
# Unified Database Class v9.0
# =============================================================================
class EnhancedDatabase:
    """
    Production-grade async database with IntentEngine support.
    All 12 audit issues from the v8.2 report are addressed in this version.
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

        # v9.12 (audit C-01): in-memory mirror of the processing_queue row
        # count, kept in sync under _queue_lock so add_to_queue can enforce
        # the cap WITHOUT a SELECT COUNT(*) on every insert. The mirror is
        # reconciled against the real DB count on connect() and on
        # purge_queue()/eviction paths so drift is impossible across
        # restarts. Each pop decrements; each successful insert increments;
        # each eviction decrements.
        self._queue_size_mirror: int = 0
        self._queue_mirror_dirty: bool = False

        # v9.12 (audit H-01): short-TTL caches for the blocklist hot path.
        # Stored as {id: (blocked_bool, expiry_epoch)}; the TTL is short
        # (default 30s) so a freshly-blocked sender is honored within that
        # window even without an explicit invalidate (which we also do).
        self._blocked_senders_cache: Dict[int, Tuple[bool, float]] = {}
        self._blocked_chats_cache: Dict[int, Tuple[bool, float]] = {}

        # v9.12 (audit H-01): short-TTL cache for can_send_alert() — the
        # cooldown decision only changes slowly (per-sender reputation and
        # last_alert_time), so a 15s cache absorbs most of the per-message
        # SELECT traffic without observable latency in enforcement.
        self._can_send_alert_cache: Dict[int, Tuple[bool, float]] = {}

        # Stats
        self.stats: Dict[str, int] = defaultdict(int)
        self._stats_lock = asyncio.Lock()
        self.start_time = time.time()

        # Alert batch writer (fix #4: bounded + backoff)
        self._batch: List[Tuple[Any, ...]] = []
        self._batch_lock = asyncio.Lock()
        self._flush_failure_count: int = 0
        self._flush_backoff: float = 1.0        # seconds, doubles on each failure
        self._flush_backoff_max: float = 60.0
        self._db_healthy: bool = True           # exposed as property (fix #10)

        # Background tasks (fix #5: _cleanup_loop REMOVED from this class)
        self._writer_task: Optional[asyncio.Task] = None
        self._backup_task: Optional[asyncio.Task] = None
        # Note: _cleanup_task intentionally absent — cleanup is owned by main.py

        # Dashboard query cache
        self._query_cache: TTLCache = TTLCache(maxsize=100, ttl=30)
        self._cache_lock = asyncio.Lock()

        # v9.20 P1: كاش نطاق المصادر — (expiry, set) أو None. مجموعة فارغة
        # تعني "جدول فارغ = مراقبة كل شيء" (لا تخلط مع كاش فارغ منتهٍ).
        self._sources_cache: Optional[Tuple[float, set]] = None

        # Backup health tracking (fix #7)
        self._backup_failure_count: int = 0
        self._backup_failure_threshold: int = 3

        # Resource-pressure check counter (fix #12)
        self._writer_cycle: int = 0

    # ── Public health property (fix #10) ────────────────────────────────────
    @property
    def db_healthy(self) -> bool:
        """False when the DB has been consistently failing writes."""
        return self._db_healthy

    # ─── Connection ──────────────────────────────────────────────────────────
    async def connect(self) -> bool:
        try:
            _warn_ephemeral_storage()  # fix #6: always warn at startup
            if self.db_type == "sqlite":
                await self._connect_sqlite()
            else:
                await self._connect_postgresql()
            await self._create_tables()
            await self._migrate_bigint_ids()
            await self._migrate_sender_intel()
            await self._create_indexes()
            self.is_connected = True
            await self.start_writer()
            # v9.12 (audit C-01): prime the in-memory queue-size mirror
            # so the first add_to_queue doesn't pay a SELECT COUNT(*).
            try:
                row = await self._fetchone("SELECT COUNT(*) AS cnt FROM processing_queue")
                async with self._queue_lock:
                    self._queue_size_mirror = int(row["cnt"]) if row else 0
                    self._queue_mirror_dirty = False
                logger.debug(f"queue_size_mirror primed: {self._queue_size_mirror}")
            except Exception as e:
                logger.debug(f"queue mirror prime failed: {e}")
                self._queue_mirror_dirty = True
            # Note: start_cleanup() intentionally NOT called here (fix #5)
            logger.info(f"Database connected: {self.db_type.upper()} v9.3")
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
            return await self._sqlite_conn.execute(sql, params)
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
            -- v9.20 P1: مصادر المراقبة — دلالة التوافق الذهبية:
            -- جدول فارغ = مراقبة كل شيء (السلوك الأصلي حرفياً)؛
            -- مملوء = المفعّلة فقط. فشل-آمن: أي خلل يفتح المسار.
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
            -- v9.18 P0: سجل التدقيق — كل عملية كتابة من اللوحتين تُسجّل هنا
            -- (fire-and-forget، بلا أسرار، الهواتف مُقنَّعة). يُقرأ من
            -- GET /api/audit (BotPanel) ويُقَلَّم دورياً بـ90 يوماً.
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
        """
        for stmt in stmts.split(";"):
            s = stmt.strip()
            if s:
                if self.db_type == "postgresql":
                    s = (
                        s.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
                        .replace("sender_id INTEGER PRIMARY KEY", "sender_id BIGINT PRIMARY KEY")
                        .replace("chat_id INTEGER PRIMARY KEY", "chat_id BIGINT PRIMARY KEY")
                        # v9.11: عمود عاري غير مفتاحي أيضاً يجب أن يتسع لمعرفات
                        # تيليجرام 64-بت (alert_dedup.sender_id كان يبقى int4).
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
                        .replace("REAL NOT NULL", "DOUBLE PRECISION NOT NULL")
                        .replace("REAL DEFAULT", "DOUBLE PRECISION DEFAULT")
                        .replace("REAL,", "DOUBLE PRECISION,")
                    )
                await self._execute(s)
        await self._commit()

    async def _migrate_bigint_ids(self) -> None:
        """v9.1: widen Telegram-ID columns from INTEGER (int32) to BIGINT.

        Telegram IDs overflow signed 32-bit integers: supergroup chats are
        -100XXXXXXXXXX and modern user IDs reach ~9 billion, while int32
        caps at 2147483647. Older databases were created with INTEGER
        PRIMARY KEY columns (the SQLite->PostgreSQL translation never
        widened bare PK columns), so writes failed with "value out of
        int32 range". Idempotent: ALTER to BIGINT on an already-BIGINT
        column is a cheap no-op on these small tables.
        """
        if self.db_type != "postgresql":
            return
        # v9.11: القائمة الشاملة لكل أعمدة معرفات تيليجرام — ALTER هو no-op
        # رخيص إذا كان العمود BIGINT مسبقاً، لذا تُنفّذ دائماً كأحزمة أمان
        # تشمل الجداول التي كانت تُنشأ صحيحة منذ البداية (messages/alerts)
        # وجدول alert_dedup الذي كان يفلت من قواعد الترجمة ويبقى int4.
        stmts = [
            "ALTER TABLE sender_stats    ALTER COLUMN sender_id       TYPE BIGINT",
            "ALTER TABLE sender_contacts ALTER COLUMN sender_id       TYPE BIGINT",
            "ALTER TABLE sender_contacts ALTER COLUMN access_hash     TYPE BIGINT",
            "ALTER TABLE sender_contacts ALTER COLUMN last_chat_id    TYPE BIGINT",
            "ALTER TABLE sender_contacts ALTER COLUMN last_message_id TYPE BIGINT",
            "ALTER TABLE blocked_senders ALTER COLUMN sender_id       TYPE BIGINT",
            "ALTER TABLE blocked_chats   ALTER COLUMN chat_id         TYPE BIGINT",
            # ── v9.11: إصلاح خطأ DataError المستمر (value out of int32 range) ──
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
            ok = False
            for attempt in (1, 2):  # محاولتان — منافسة الأقفال عابرة غالباً
                try:
                    await self._execute(st)
                    applied += 1
                    ok = True
                    break
                except Exception as e:
                    if attempt == 1:
                        await asyncio.sleep(2.0)  # مهلة قصيرة قبل إعادة المحاولة
                        continue
                    # v9.11: الفشل لم يعد يُبتلع بصمت — سجل صارخ + اسم الجدول/العمود
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
        # تحقق فعلي من أنواع الأعمدة بعد الترحيل — يكشف أي عمود ما زال int4
        await self._verify_bigint_columns(failed)

    async def _verify_bigint_columns(self, failed: list) -> None:
        """v9.11: تحقق من information_schema أن كل أعمدة المعرفات فعلاً BIGINT.

        هذا يكشف الحالة التي كان فيها خطأ الترحيل يُبتلع بصمت (قفل من عملية
        أخرى/مهلة 30 ثانية) فيبقى العمود int4 ويتكرر في السجلات:
            DataError: invalid input for query argument $1 (value out of int32 range)
        """
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
                    continue  # الجدول قد لا يكون موجوداً بعد — إنشاؤه قادم
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

    # ── sender-intel columns (v9.3, backward-compatible) ──────────────────
    _SENDER_INTEL_COLUMNS = [
        # (name, sqlite/pg type suffix) — types valid on BOTH dialects.
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
        """v9.3: enrich sender_contacts with full sender metadata.

        Backward compatible by construction:
          * columns are ADDED, never renamed/dropped (no data loss)
          * existing queries (JOIN on sender_id) are unaffected
          * idempotent — safe on every boot, cheap no-op afterwards
        PostgreSQL uses ADD COLUMN IF NOT EXISTS; SQLite is pragma-checked
        per column (no IF NOT EXISTS support there).
        """
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
                    # normalize DOUBLE PRECISION -> DOUBLE PRECISION (already valid)
                    await self._execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {decl}")
                else:
                    # SQLite has no DOUBLE PRECISION alias issue, but keep native types
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
            # v9.10 dedup: fast expiry-scan for the periodic cleanup
            "CREATE INDEX IF NOT EXISTS idx_dedup_first_seen ON alert_dedup(first_seen)",
            "CREATE INDEX IF NOT EXISTS idx_dedup_sender     ON alert_dedup(sender_id)",
            # v9.11 antispam: فحص سريع لقائمة المراقبة/التجاهل
            "CREATE INDEX IF NOT EXISTS idx_watch_sender_time ON spam_watch(sender_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_watch_until       ON spam_watch(watch_until)",
            # v9.18 P0: فهارس سجل التدقيق (زمن/إجراء/فاعل) — القوائم دائماً
            # بترتيب زمني تنازلي والفلترة بالإجراء أو الفاعل.
            "CREATE INDEX IF NOT EXISTS idx_audit_time   ON audit_logs(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action)",
            "CREATE INDEX IF NOT EXISTS idx_audit_actor  ON audit_logs(actor)",
            # v9.20 P1: فحص نطاق المصادر — دائماً مفعّل فقط
            "CREATE INDEX IF NOT EXISTS idx_sources_enabled ON sources(enabled)",
        ]
        for idx in indexes:
            await self._execute(idx)
        await self._commit()

    # ─── Persistent Queue (fix #3 / #9) ──────────────────────────────────────
    async def add_to_queue(self, event_data: dict, priority: int = 5) -> int:
        """
        Add an event to the persistent processing queue.

        Returns:
          > 0  — newly inserted row id
          -1   — insert error
          -2   — DB is unhealthy (backpressure signal, fix #10)

        When the queue is at capacity (CFG.MESSAGE_QUEUE_SIZE), the oldest
        lowest-priority row is evicted before inserting the new one, so the
        cap is enforced atomically under _queue_lock (fix #3 / #9).

        v9.12 (audit C-01): the per-insert SELECT COUNT(*) is replaced by
        an in-memory mirror (`_queue_size_mirror`) kept in sync under
        _queue_lock. Every N inserts (QUEUE_MIRROR_RECONCILE_EVERY) we
        reconcile against the real DB count to absorb any external drift
        (manual DB edits, restarts mid-flight, etc.). Commits are also
        batched: eviction + insert now share a single commit instead of
        two. Result: 1 SELECT-then-INSERT+COMMIT per message instead of
        SELECT COUNT + DELETE + COMMIT + INSERT + COMMIT (3 commits → 1).
        """
        if not self._db_healthy:
            # Backpressure: tell the caller the DB is unhealthy (fix #10)
            logger.debug("add_to_queue: DB unhealthy, returning -2 (backpressure)")
            return -2

        try:
            async with self._queue_lock:  # serialise cap check + insert (fix #9)
                # ── Enforce capacity cap (fix #3, audit C-01) ────────────
                # Use the in-memory mirror; reconcile against DB every N inserts
                # to absorb drift (manual edits, restarts, etc.).
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
                        # DROP_OLDEST: remove the single oldest lowest-priority row
                        # v9.12 (C-01): batch the eviction + insert into one commit.
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
                    await self._commit()  # single commit covers eviction (if any) + insert
                    self._queue_size_mirror += 1
                    async with self._stats_lock:
                        self.stats["queue_inserts"] = self.stats.get("queue_inserts", 0) + 1
                    return cursor.lastrowid

                else:
                    # PostgreSQL: capacity check + insert in one transaction
                    # v9.12 (C-01): mirror used here too for the cap check.
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
                            return result["id"]

        except Exception as e:
            logger.error(f"add_to_queue failed: {e}")
            # On error the mirror may be stale — flag it for reconciliation.
            self._queue_mirror_dirty = True
            return -1

    async def pop_from_queue(self) -> Optional[dict]:
        try:
            if self.db_type == "sqlite":
                # v9.12 (audit C-01 + M-01): pop no longer takes _hash_lock.
                # The queue is serialised by _queue_lock (cap enforcement)
                # and the underlying aiosqlite connection is single-threaded
                # anyway. Removing the cross-purpose _hash_lock contention
                # lets message dedup (is_duplicate) proceed in parallel with
                # queue pops.
                async with self._queue_lock:
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
                    self._queue_size_mirror = max(0, self._queue_size_mirror - 1)
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
                        await conn.execute(
                            "DELETE FROM processing_queue WHERE id = $1", row["id"]
                        )
                        self._queue_size_mirror = max(0, self._queue_size_mirror - 1)
                        return json_loads(row["event_data"])
        except Exception as e:
            logger.error(f"pop_from_queue failed: {e}")
            self._queue_mirror_dirty = True
            return None

    async def queue_size(self) -> int:
        # v9.12 (audit C-01): prefer the in-memory mirror (O(1)) over a
        # fresh COUNT(*) — the mirror is reconciled periodically inside
        # add_to_queue and on every connect/purge.
        try:
            async with self._queue_lock:
                if not self._queue_mirror_dirty:
                    return self._queue_size_mirror
            # Fall back to a real count if the mirror is flagged dirty.
            row = await self._fetchone("SELECT COUNT(*) AS cnt FROM processing_queue")
            return int(row["cnt"]) if row else 0
        except Exception:
            return 0

    async def purge_queue(self) -> int:
        try:
            if self.db_type == "sqlite":
                # v9.1 fix (audit M-4): total_changes is cumulative; use the
                # DELETE cursor's rowcount so the admin /purge command reports
                # the real number of removed rows.
                cursor = await self._execute("DELETE FROM processing_queue")
                deleted = cursor.rowcount or 0
                await self._commit()
            else:
                result = await self._pool.execute("DELETE FROM processing_queue")
                deleted = int(result.split()[1])
            # v9.12 (audit C-01): keep the mirror in sync after a purge.
            async with self._queue_lock:
                self._queue_size_mirror = 0
                self._queue_mirror_dirty = False
            return deleted
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

    @staticmethod
    def _coerce_created_at(value: Any) -> float:
        """
        Convert dead_letters.created_at to a unix timestamp.

        v9.1 fix (found by the test suite): SQLite's CURRENT_TIMESTAMP
        yields a STRING ("YYYY-MM-DD HH:MM:SS"); the old code called
        str.timestamp() on it — an AttributeError on every row, silently
        swallowed by the except → get_dead_letters() always returned [] on
        SQLite. Handles float/int, ISO strings and SQLite datetime strings.
        """
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
        """
        Remove resolved and old dead letters.
        Also enforces _DEAD_LETTER_MAX_ROWS hard cap (fix #11):
        if the table exceeds the cap, oldest resolved rows are purged first,
        then oldest unresolved rows are purged until under the cap.
        """
        cutoff = time.time() - days * 86400
        total = 0
        try:
            # v9.1 fix (audit M-4): rowcount-based counting — the previous
            # cumulative total_changes arithmetic reported inflated numbers.
            cur = await self._execute("DELETE FROM dead_letters WHERE resolved = 1")
            total += cur.rowcount or 0

            cur = await self._execute(
                "DELETE FROM dead_letters WHERE created_at < ?", (cutoff,)
            )
            total += cur.rowcount or 0

            # Hard row cap (fix #11)
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

    # ─── Messages ─────────────────────────────────────────────────────────────
    async def try_insert_message(self, rec: MessageRecord) -> bool:
        """
        Insert a message, returning True only when the row is genuinely new.

        v9.1 fix (audit C-3, SQLite): the old code read
        ``self._sqlite_conn.total_changes`` — a CUMULATIVE counter over the
        whole connection lifetime — as the "was it inserted?" signal. After
        the very first successful insert of the process, that counter is
        permanently non-zero, so is_new was effectively always True:
        DB-level dedup never fired, duplicates were re-alerted after restart
        and sender_stats.total_messages was inflated on every duplicate.
        The insert result is now taken from cursor.rowcount (1 = inserted,
        0 = ignored by INSERT OR IGNORE).
        """
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
                    # Only count sender activity for genuinely new messages
                    # (previously inflated on every duplicate delivery).
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
                # v9.12 (audit L-04): the old `result == "INSERT 0 1"` string
                # comparison is asyncpg-specific and fragile (any asyncpg
                # version change to the status string would silently break
                # dedup). Use the portable cursor.rowcount check instead —
                # 1 = inserted, 0 = conflict (DO NOTHING). This matches the
                # SQLite branch's `cursor.rowcount or 0` pattern.
                result = await self._pool.execute(
                    "INSERT INTO messages "
                    "(message_hash, chat_id, sender_id, message_text, keyword_found, score, spam_score, timestamp) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                    "ON CONFLICT (message_hash) DO NOTHING",
                    rec.message_hash, rec.chat_id, rec.sender_id,
                    rec.message_text[:500], rec.keyword_found,
                    rec.score, rec.spam_score, rec.timestamp,
                )
                # asyncpg's execute() returns a status string like
                # "INSERT 0 1" or "INSERT 0 0"; parse the tuples-affected
                # count portably instead of an exact string match.
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

    # ─── Sender Contacts ────────────────────────────────────────────────   
    # v9.3 update policy (engineering brief #22/#24):
    #   * identity fields (username/access_hash/names/phone/metadata):
    #       new NON-NULL value → update; new NULL value → KEEP the stored
    #       value (COALESCE) — a transient entity without a username must
    #       never erase the historical one.
    #   * last-seen context (last_chat_id/last_message_id): always the
    #       newest message (these are "where we saw them last" fields).
    #   * last_updated: always refreshed; updated_at: always refreshed.
    _CONTACT_COLS = (
        "sender_id, access_hash, username, first_name, last_name, "
        "last_chat_id, last_message_id, last_message_link, last_group_link, "
        "phone, is_bot, is_verified, is_premium, is_scam, is_fake, "
        "is_restricted, is_deleted, is_contact, is_mutual_contact, "
        "photo_available, restriction_reason, lang_code, status, usernames, "
        "last_seen, last_updated"
    )

    def _contact_params(self, s: Dict[str, Any]) -> tuple:
        """Ordered params for _CONTACT_COLS (SQLite tuple form)."""
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
        )

    async def upsert_sender_contact(self, sender_data: Dict[str, Any]) -> None:
        """persist (requirement #11/#24). Never deletes a row (deleted
        accounts keep their historical identity, requirement #23)."""
        try:
            import time as _time
            data = dict(sender_data)
            data.setdefault("last_updated", _time.time())
            # usernames stored as JSON text when a list is provided
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
                    "last_updated = EXCLUDED.last_updated, "
                    "updated_at = CURRENT_TIMESTAMP"
                )
                # _pg() translates the ?-placeholders to $N form; the SET
                # clause contains no placeholders (EXCLUDED.<col> only).
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
        """DB fallback for the resolver / recovery paths. Returns the full
        stored contact row (or None)."""
        try:
            return await self._fetchone(
                "SELECT * FROM sender_contacts WHERE sender_id = ?",
                (sender_id,),
            )
        except Exception as e:
            logger.debug(f"get_sender_contact error: {e}")
            return None

    async def update_sender_reputation(self, sender_id: int, is_valid: bool) -> None:
        try:
            # v9.2 fix: SQLite's scalar MIN(a, b) / MAX(a, b) do not exist in
            # PostgreSQL (max/min there are aggregates only) — the production
            # log showed "function max(numeric, double precision) does not
            # exist" on every reputation update. The portable PostgreSQL
            # equivalents are LEAST / GREATEST.
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

    # ─── Helpers for contact button (زر «تواصل مع المرسل») ────────────────
    async def get_alert_sender_by_hash(self, msg_hash: str) -> Optional[Dict[str, Any]]:
        """بيانات المرسل الأصلي لتنبيه معيّن (sender_id + بيانات التواصل).

        يستخدمه معالج زر «تواصل مع المرسل» لإرسال الرسالة الجاهزة المختارة
        إلى صاحب الإعلان/الطلب مباشرة. يجمع بيانات alerts + sender_contacts.
        """
        try:
            row = await self._fetchone(
                "SELECT sender_id, chat_id, keyword FROM alerts "
                "WHERE message_hash = ? LIMIT 1",
                (msg_hash,),
            )
            if not row:
                return None
            # كل الأعمدة محددة في الاستعلام أعلاه — الفهرسة المفتاحية تعمل
            # على كلا اللهجتين (aiosqlite.Row و asyncpg.Record).
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

    # ─── Anti-Spam: Watch List + Permanent Ignore (المرحلة الثانية) ────────
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
        """v9.12 (audit H-07): single-row lookup used by AntiSpam's LRU
        fallback. Returns the row (with reason + classified) or None."""
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
        """إضافة إلى قائمة التجاهل الدائم مع تسجيل سبب الحظر بالتفصيل."""
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
        """تسجيل إدراج في قائمة المراقبة (سجل append للأسباب — للمشرف واللوحة)."""
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
        """إزالة سجلات المراقبة الأقدم من `days` أيام (يُستدعى من التنظيف الدوري)."""
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

    # ─── Alert Dedup (v9.10 — cross-account/re-send alert barrier) ───────────
    async def claim_alert_fingerprint(
        self, fingerprint: str, first_seen: float, window_hint: int = 86400
    ) -> bool:
        """
        Atomically claim a content fingerprint. Returns True only for the
        FIRST claim within the window; every concurrent/duplicate claim of
        the same fingerprint returns False (SQLite: single serialized
        connection; PostgreSQL: ON CONFLICT DO NOTHING is atomic).

        A row whose last_seen is older than the window is REFRESHED (the
        new message restarts the window) instead of being treated as a
        duplicate — old fingerprints never block new, legitimate repeats.
        """
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
                # row exists — is it still inside the window?
                row = await self._fetchone(
                    "SELECT last_seen FROM alert_dedup WHERE fingerprint = ?",
                    (fingerprint,),
                )
                if row is None:
                    # raced out from under us (concurrent cleanup) — treat as first
                    return True
                last_seen = float(row.get("last_seen") or 0.0)
                if first_seen - last_seen > max(1, window_hint):
                    # window expired: refresh in place (restart the window)
                    await self._execute(
                        "UPDATE alert_dedup SET first_seen = ?, last_seen = ?, hits = 1 "
                        "WHERE fingerprint = ?",
                        (first_seen, first_seen, fingerprint),
                    )
                    await self._commit()
                    return True
                # still within the window — a real duplicate
                await self._execute(
                    "UPDATE alert_dedup SET last_seen = ?, hits = hits + 1 "
                    "WHERE fingerprint = ?",
                    (first_seen, fingerprint),
                )
                await self._commit()
                return False
            else:
                # PostgreSQL: SELECT … FOR UPDATE inside one transaction —
                # same semantics as the SQLite branch, atomically serialized
                # against concurrent workers/instances.
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        row = await conn.fetchrow(
                            "SELECT last_seen FROM alert_dedup "
                            "WHERE fingerprint = $1 FOR UPDATE",
                            fingerprint,
                        )
                        if row is None:
                            await conn.execute(
                                "INSERT INTO alert_dedup "
                                "(fingerprint, sender_id, first_seen, last_seen, hits) "
                                "VALUES ($1, NULL, $2, $2, 1)",
                                fingerprint, first_seen,
                            )
                            return True
                        last_seen = float(row["last_seen"] or 0.0)
                        if first_seen - last_seen > max(1, window_hint):
                            # window expired: refresh in place (restart it)
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
        """Free a claimed fingerprint after a failed alert send so the DLQ
        retry path can claim it again later."""
        try:
            await self._execute(
                "DELETE FROM alert_dedup WHERE fingerprint = ?", (fingerprint,)
            )
            await self._commit()
        except Exception as e:
            logger.debug(f"release_alert_fingerprint error: {e}")

    async def cleanup_alert_fingerprints(self, max_age_seconds: int = 86400) -> int:
        """Delete fingerprints older than the dedup window (called from
        main.py's single cleanup loop). Returns the number of removed rows."""
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

    # ─── Blocklists ───────────────────────────────────────────────────────────
    async def is_blocked_sender(self, sender_id: int) -> bool:
        # v9.12 (audit H-01): short TTL cache for the hot-path blocklist
        # check. Blocklists rarely change; the cache absorbs the per-message
        # SELECT. block_sender()/unblock_sender() invalidate the entry.
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
            return blocked
        except Exception:
            # On any error fall back to direct DB check.
            row = await self._fetchone(
                "SELECT 1 AS hit FROM blocked_senders WHERE sender_id = ? LIMIT 1",
                (sender_id,),
            )
            return row is not None

    async def is_blocked_chat(self, chat_id: int) -> bool:
        # v9.12 (audit H-01): same TTL cache strategy as is_blocked_sender.
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
            return blocked
        except Exception:
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
            # v9.12 (audit H-01): invalidate the cache so the next check
            # sees the new state immediately.
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
                "INSERT OR IGNORE INTO blocked_chats (chat_id, reason, blocked_by) VALUES (?, ?, ?)",
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
        # v9.12 (audit H-01): short-TTL cache for the per-message cooldown
        # check. The decision only changes when (a) an alert was just sent
        # (which invalidates this entry via _invalidate_can_send_alert) or
        # (b) the reputation changes (rare, also handled). 15s default TTL
        # keeps the cooldown enforcement within one cooldown tick of
        # reality while collapsing most of the per-message SELECT traffic.
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
            # If the sender is on cooldown, cache only for a short window so
            # we re-check close to the cooldown expiry.
            if not allowed:
                ttl = min(ttl, 5.0)
            self._can_send_alert_cache[sender_id] = (allowed, now + ttl)
            return allowed
        except Exception:
            # Conservative fallback: allow the alert (the rate limiter in
            # main.py still caps total throughput per account).
            return True

    def _invalidate_can_send_alert(self, sender_id: int) -> None:
        """Call after recording an alert send for a sender so the next
        can_send_alert() check picks up the new last_alert_time."""
        self._can_send_alert_cache.pop(sender_id, None)

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

            # v9.16: blocked-chats grand total for the BotPanel blocked page
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

    async def cleanup_old_data(self, days: int = 7) -> int:
        """
        Remove old messages and alerts.
        Called exclusively from main.py (fix #5: single cleanup owner).
        """
        cutoff = time.time() - days * 86400
        total = 0
        try:
            # v9.1 fix (audit M-4): rowcount-based counting.
            cur = await self._execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
            total += cur.rowcount or 0
            cur = await self._execute("DELETE FROM alerts WHERE timestamp < ?", (cutoff,))
            total += cur.rowcount or 0
            # v9.11 antispam: تنظيف سجل قائمة المراقبة القديم (سجل تشخيصي فقط)
            try:
                await self._execute("DELETE FROM spam_watch WHERE watch_until < ?", (cutoff,))
            except Exception:
                pass
            # v9.18 P0: تقليم سجل التدقيق (90 يوماً — قابل للضبط من v9.28)
            try:
                await self.cleanup_old_audit_logs(90)
            except Exception:
                pass
            await self._commit()
            if total > 500 and self.db_type == "sqlite":
                await self._execute("PRAGMA optimize")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        return total

    # ─── Resource pressure check (fix #12) ────────────────────────────────────
    # ─── v9.18 P0: سجل التدقيق (Audit Log) ───────────────────
    async def record_audit(self, actor: str, action: str, object_type: str = "",
                           object_id: str = "", old_value: str = "", new_value: str = "",
                           source: str = "botpanel") -> bool:
        """v9.18 P0: تسجيل عملية في سجل التدقيق — فشل-آمن تماماً.

        يُستدعى fire-and-forget من اللوحتين (BotPanel/webadmin) فلا يُسمح له
        أبداً برفع استثناء أو تعطيل المسار الأصلي. لا أسرار ولا كلمات مرور
        تُكتب هنا — والمتصل مسؤول عن إخفاء الهواتف (القناع في dashboard.py).
        """
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

    async def get_audit_logs(self, limit: int = 100, offset: int = 0,
                             action: Optional[str] = None) -> List[Dict[str, Any]]:
        """v9.18 P0: قراءة سجل التدقيق — الأحدث أولاً. فشل-آمن (قائمة فارغة)."""
        try:
            limit = max(1, min(int(limit), 500))
            offset = max(0, int(offset))
        except Exception:
            limit, offset = 100, 0
        try:
            if action:
                return await self._fetchall(
                    "SELECT * FROM audit_logs WHERE action = ?"
                    " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                    (str(action)[:120], limit, offset),
                )
            return await self._fetchall(
                "SELECT * FROM audit_logs ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        except Exception as e:
            logger.debug(f"get_audit_logs skipped: {e}")
            return []

    async def count_audit_logs(self, action: Optional[str] = None) -> int:
        """v9.18 P0: عدّاد سجل التدقيق (كله أو إجراء محدد) — فشل-آمن (0)."""
        try:
            if action:
                row = await self._fetchone(
                    "SELECT COUNT(*) AS n FROM audit_logs WHERE action = ?", (str(action)[:120],)
                )
            else:
                row = await self._fetchone("SELECT COUNT(*) AS n FROM audit_logs")
            return int(row["n"]) if row else 0
        except Exception:
            return 0

    async def cleanup_old_audit_logs(self, days: int = 90) -> int:
        """v9.18 P0: تقليم سجل التدقيق الأقدم من days يوماً (افتراضي 90).

        أرضية يوم واحد فلا تحذف قيمة فاسدة كل التاريخ. يعمل على المحركين
        (SQLite: مقارنة معجمية على الصيغة النصية — PostgreSQL: كائن توقيت).
        """
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

    # ─── v9.20 P1: مصادر المراقبة (Sources) ──────────────────
    def invalidate_sources_cache(self) -> None:
        """v9.20 P1: إبطال كاش المصادر فوراً بعد أي تعديل من اللوحة."""
        self._sources_cache = None

    async def add_source(self, chat_id: int, username: str = "", title: str = "",
                         type_: str = "", added_by: str = "panel",
                         notes: str = "") -> bool:
        """v9.20 P1: إضافة/تحديث مصدر (upsert حسب chat_id). فشل-آمن."""
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
        """v9.20 P1: حذف مصدر نهائياً. فشل-آمن."""
        try:
            cur = await self._execute("DELETE FROM sources WHERE chat_id = ?", (int(chat_id),))
            await self._commit()
            self.invalidate_sources_cache()
            return (cur.rowcount or 0) > 0
        except Exception as e:
            logger.error(f"remove_source error: {e}")
            return False

    async def set_source_enabled(self, chat_id: int, enabled: bool) -> bool:
        """v9.20 P1: تشيل/إيقاف مصدر دون حذفه. فشل-آمن."""
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
        """v9.20 P1: مصدر واحد حسب chat_id — أو None."""
        try:
            return await self._fetchone("SELECT * FROM sources WHERE chat_id = ?", (int(chat_id),))
        except Exception:
            return None

    async def list_sources(self, limit: int = 500) -> List[Dict[str, Any]]:
        """v9.20 P1: قائمة المصادر (الأحدث أولاً). فشل-آمن (قائمة فارغة)."""
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
        """v9.20 P1: هل يُراقَب هذا المصدر؟ — دلالة التوافق الذهبية.

        * جدول فارغ  → True (مراقبة كل شيء — السلوك الأصلي حرفياً)
        * جدول مملوء → المفعّلة فقط
        * أي خلل     → True (فشل-آمن: لا انكسار للمسار أبداً)
        كاش 30 ثانية + إبطال فوري عند أي تعديل من اللوحة.
        """
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
                return True  # جدول فارغ = مراقبة كل شيء
            return int(chat_id) in allowed_set
        except Exception:
            return True  # فشل-آمن: أي خلل = فتح المسار

    async def _resource_pressure_check(self) -> None:
        """
        Inspect SQLite file size. If it exceeds CFG.MEMORY_THRESHOLD_MB,
        trigger an emergency cleanup to relieve storage pressure (fix #12).
        """
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
        # v9.1 fix (audit H-5): a previous close() leaves cancelled/done task
        # objects behind; only checking `is None` meant a reconnect could
        # never restart the writer+backup pair, so batched alerts accumulated
        # in memory until the hard cap evicted them (alert records LOST).
        if self._writer_task is None or self._writer_task.done():
            self._writer_task = asyncio.create_task(self._writer_loop(), name="db_writer")
        if self._backup_task is None or self._backup_task.done():
            self._backup_task = asyncio.create_task(self._backup_loop(), name="db_backup")
        logger.info("Database background tasks started (writer + backup)")

    # Note: start_cleanup() intentionally removed (fix #5). Cleanup is owned
    # by main.py::EnhancedTelegramBot._cleanup_loop exclusively.

    async def _writer_loop(self) -> None:
        """
        Batch-flush writer with:
          - exponential backoff on consecutive failures (fix #4 / #10)
          - periodic ping + reconnect
          - periodic resource-pressure check (fix #12)
        """
        ping_counter = 0
        while self.is_connected:
            try:
                await asyncio.sleep(CFG.DB_BATCH_INTERVAL)
                await self._flush()

                self._writer_cycle += 1

                # Periodic resource-pressure check (fix #12)
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
                        # Reset health/backoff on successful ping
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
        """
        Batch-write queued alerts.
        Fixes applied:
          - Bounded _batch with hard cap (fix #4)
          - Exponential backoff on failure (fix #4 / #10)
          - _db_healthy flag update (fix #10)
          - Does NOT re-add items to _batch after consecutive failures
            beyond backoff_max (evicts instead with CRITICAL log)
        """
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
                # v9.12 (audit M-03): collapse the per-alert UPDATE sender_stats
                # loop into a single executemany() so a batch of N alerts costs
                # 1 INSERT + 1 executemany instead of 1 INSERT + N UPDATEs.
                # We pick the latest timestamp per sender (max) so the
                # last_alert_time reflects the most recent alert in the batch.
                # Group by sender_id: for each sender, find max(timestamp).
                sender_to_latest_ts: Dict[int, float] = {}
                sender_counts: Dict[int, int] = {}
                for data in alerts_data:
                    sid = int(data[2])
                    ts = float(data[6])
                    sender_to_latest_ts[sid] = max(sender_to_latest_ts.get(sid, 0.0), ts)
                    sender_counts[sid] = sender_counts.get(sid, 0) + 1
                # Build the per-sender UPDATE batch.
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
                    " decision, confidence, reasons, intent_verb, academic_object, negation_detected, advert_score) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                )
                params_list = [tuple(data) for data in alerts_data]
                await self._executemany(sql, params_list)
                # v9.12 (audit M-03): same N+1 collapse for SQLite — one
                # executemany for the per-sender UPDATEs instead of N round-trips.
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
                    await self._executemany(
                        "UPDATE sender_stats "
                        "SET alerts_sent = alerts_sent + ?, last_alert_time = ? "
                        "WHERE sender_id = ?",
                        update_params,
                    )
                for sid in sender_to_latest_ts:
                    self._invalidate_can_send_alert(sid)
                await self._commit()

            # Success — reset failure tracking (fix #10)
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

            # Re-queue the failed batch only if under the hard cap (fix #4)
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

            # Exponential backoff sleep (fix #4 / #10)
            self._flush_backoff = min(self._flush_backoff * 2, self._flush_backoff_max)
            await asyncio.sleep(backoff)

    # ─── Backup Loop (fixes #1 / #2 / #7 / #8) ───────────────────────────────
    async def _backup_loop(self) -> None:
        """
        Periodic database backup with:
          - Non-blocking I/O via run_in_executor (fix #1 / #8)
          - SQLite online backup API to avoid full RAM read (fix #8)
          - Post-write integrity verification (fix #7)
          - Optional upload to external HTTP endpoint (fix #2 / #7)
          - Ephemeral-storage warning on every cycle when no external
            target is configured (fix #2)
          - Consecutive-failure counter with CRITICAL alert (fix #7)
        """
        if self.db_type != "sqlite":
            # PostgreSQL has its own backup/replication infrastructure
            return

        loop = asyncio.get_running_loop()

        while self.is_connected:
            try:
                await asyncio.sleep(CFG.SESSION_BACKUP_INTERVAL)

                if not Path(CFG.DB_FILE).exists():
                    continue

                if not _BACKUP_UPLOAD_URL:
                    # Repeat the ephemeral-storage warning on every cycle (fix #2)
                    logger.warning(
                        "⚠️  Backup cycle: no BACKUP_UPLOAD_URL set. "
                        "Local .gz written but data remains ephemeral."
                    )

                backup_path = Path(f"{CFG.DB_FILE}.gz")
                tmp_path = Path(f"{CFG.DB_FILE}.bak.tmp")

                try:
                    # Step 1: hot copy via SQLite backup API (fix #8 — no full RAM read)
                    db_size = await loop.run_in_executor(
                        None, _sqlite_hot_backup, str(CFG.DB_FILE), str(tmp_path)
                    )

                    # Step 2: compress in executor (fix #1 — non-blocking)
                    compressed_size, digest = await loop.run_in_executor(
                        None, _compress_file_to_gz, str(tmp_path), str(backup_path)
                    )

                    # Step 3: verify integrity (fix #7)
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

                    # Step 4: upload to external storage (fix #2 / #7)
                    if _BACKUP_UPLOAD_URL:
                        await self._upload_backup(backup_path, digest)

                    # Reset failure counter on success
                    self._backup_failure_count = 0

                finally:
                    # Always clean up the temp hot-copy
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
        """
        Stream the backup .gz to an external HTTP PUT endpoint (fix #2 / #7).
        Logs success/failure — never raises so a failed upload cannot crash
        the backup loop.
        """
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

    async def count_alerts_with_filters(
        self,
        keyword: Optional[str] = None,
        account: Optional[str] = None,
        sender_id: Optional[int] = None,
        from_date: Optional[float] = None,
        to_date: Optional[float] = None,
        decision: Optional[str] = None,
        min_confidence: Optional[float] = None,
    ) -> int:
        """v9.14: العدد الكلي للتنبيهات مع نفس شروط get_alerts_with_filters.

        كانت /api/alerts تُرجع total=len(rows) أي حجم الصفحة الحالية فقط،
        فتعرض شارة العدّاد في اللوحة قيمة خاطئة بعد تجاوز حد الصفحة.
        """
        sql = "SELECT COUNT(*) AS cnt FROM alerts a WHERE 1=1"
        params: List[Any] = []
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
        row = await self._fetchone(sql, tuple(params))
        return int(row["cnt"]) if row else 0

    async def get_hourly_stats(self, hours: int = 24) -> List[Dict[str, Any]]:
        """
        Hourly aggregates for the last N hours.

        v9.1 fix (audit H-6): the previous single query referenced
        ``decision`` / ``confidence`` columns on the *messages* table — those
        columns only exist on *alerts* — so the query raised
        "no such column" on every call and the admin dashboard's hourly chart
        was permanently empty. Message counts now come from ``messages`` and
        decision/confidence aggregates from ``alerts``; the two result sets
        are merged per hour in Python (works identically on SQLite and PG).
        """
        cutoff = time.time() - (hours * 3600)
        messages_sql = """
            SELECT strftime('%Y-%m-%d %H:00:00', datetime(timestamp, 'unixepoch')) as hour,
                   COUNT(*) as messages_count
            FROM messages
            WHERE timestamp >= ?
            GROUP BY hour
        """ if self.db_type == "sqlite" else """
            SELECT to_char(to_timestamp(timestamp), 'YYYY-MM-DD HH24:00:00') as hour,
                   COUNT(*) as messages_count
            FROM messages
            WHERE timestamp >= $1
            GROUP BY hour
        """
        alerts_sql = """
            SELECT strftime('%Y-%m-%d %H:00:00', datetime(timestamp, 'unixepoch')) as hour,
                   COUNT(*) as alerts_count,
                   SUM(CASE WHEN decision = 'accept' THEN 1 ELSE 0 END) as accepted_count,
                   AVG(CASE WHEN confidence > 0 THEN confidence END) as avg_confidence
            FROM alerts
            WHERE timestamp >= ?
            GROUP BY hour
        """ if self.db_type == "sqlite" else """
            SELECT to_char(to_timestamp(timestamp), 'YYYY-MM-DD HH24:00:00') as hour,
                   COUNT(*) as alerts_count,
                   SUM(CASE WHEN decision = 'accept' THEN 1 ELSE 0 END) as accepted_count,
                   AVG(CASE WHEN confidence > 0 THEN confidence END) as avg_confidence
            FROM alerts
            WHERE timestamp >= $1
            GROUP BY hour
        """
        try:
            msg_rows = await self._fetchall(messages_sql, (cutoff,))
            alert_rows = await self._fetchall(alerts_sql, (cutoff,))
        except Exception as e:
            logger.error(f"get_hourly_stats error: {e}")
            return []

        merged: "Dict[str, Dict[str, Any]]" = {}
        for r in msg_rows:
            merged[str(r["hour"])] = {
                "hour": str(r["hour"]),
                "messages_count": int(r["messages_count"] or 0),
                "alerts_count": 0,
                "accepted_count": 0,
                "avg_confidence": 0.0,
            }
        for r in alert_rows:
            hour = str(r["hour"])
            entry = merged.setdefault(
                hour,
                {"hour": hour, "messages_count": 0, "alerts_count": 0, "accepted_count": 0, "avg_confidence": 0.0},
            )
            entry["alerts_count"] = int(r["alerts_count"] or 0)
            entry["accepted_count"] = int(r["accepted_count"] or 0)
            entry["avg_confidence"] = round(float(r["avg_confidence"] or 0.0), 3)

        return sorted(merged.values(), key=lambda x: x["hour"])

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
        for name in ("_writer_task", "_backup_task"):
            task = getattr(self, name)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            # Reset refs so a subsequent connect() → start_writer() can
            # actually restart them (audit H-5).
            setattr(self, name, None)
        await self._flush()
        if self.db_type == "sqlite" and self._sqlite_conn:
            await self._sqlite_conn.close()
        elif self.db_type == "postgresql" and self._pool:
            await self._pool.close()
        logger.info("Database v9.0 closed")
