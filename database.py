"""
Database v8.2 – INTENT ENGINE EDITION (FINAL, FULLY PATCHED)
✅ أعيد بناؤها بالكامل لتتكامل مع FilterEngine v13.0 (IntentEngine)
✅ لا تغييرات على الواجهات العامة (Backward Compatible)
✅ دعم حقول IntentEngine (decision, confidence, reasons)
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import zlib
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite
import orjson
from loguru import logger

from config import CFG

# =============================================================================
# Optional PostgreSQL Support
# =============================================================================

_POSTGRES_AVAILABLE = False
try:
    import asyncpg
    _POSTGRES_AVAILABLE = True
except ImportError:
    asyncpg = None  # type: ignore
    logger.debug("asyncpg not available - PostgreSQL support disabled")


# =============================================================================
# JSONB Type (for PostgreSQL compatibility)
# =============================================================================

class JSONB:
    """JSONB type placeholder for SQLAlchemy compatibility."""
    pass


# =============================================================================
# Dead Letter Queue Item
# =============================================================================

class DeadLetterItem:
    """عنصر في قائمة الرسائل الميتة."""
    __slots__ = ("data", "error", "timestamp", "retry_count", "next_retry")

    def __init__(self, data: Any, error: str):
        self.data = data
        self.error = error
        self.timestamp = time.time()
        self.retry_count = 0
        self.next_retry = 0.0

    def should_retry(self) -> bool:
        return time.time() >= self.next_retry

    def mark_retry(self) -> None:
        self.retry_count += 1
        self.next_retry = time.time() + (2 ** self.retry_count)


# =============================================================================
# Database Class
# =============================================================================

class EnhancedDatabase:
    """قاعدة بيانات محسّنة مع دعم IntentEngine v13.0."""

    def __init__(self):
        self.db_type: str = CFG.DB_TYPE.lower()
        self._sqlite_conn: Optional[aiosqlite.Connection] = None
        self._pool = None
        self.is_connected: bool = False
        self._init_lock = asyncio.Lock()

        # Dead Letter Queue
        self._dead_letter: deque = deque(maxlen=CFG.DEAD_LETTER_MAX_SIZE)
        self._dead_letter_lock = asyncio.Lock()

        # Batch processing
        self._batch: List[Tuple] = []
        self._batch_lock = asyncio.Lock()
        self._batch_size: int = CFG.BATCH_SIZE
        self._batch_timeout: float = CFG.BATCH_TIMEOUT
        self._last_flush: float = time.time()

        # Tasks
        self._writer_task: Optional[asyncio.Task] = None
        self._backup_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None

        # Prepared statements cache
        self._prepared_cache: Dict[str, Any] = {}

        # Query cache
        self._query_cache: Dict[str, Any] = {}
        self._cache_lock = asyncio.Lock()

        # Stats
        self.start_time = time.time()
        self.queries_executed: int = 0
        self.queries_failed: int = 0

        logger.info(f"Database v8.2 initialized (type={self.db_type}, pool={CFG.DB_POOL_MIN}-{CFG.DB_POOL_MAX})")

    # ─── Connection ───────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """الاتصال بقاعدة البيانات."""
        async with self._init_lock:
            if self.is_connected:
                return
            try:
                if self.db_type == "sqlite":
                    await self._connect_sqlite()
                elif self.db_type == "postgresql":
                    await self._connect_postgresql()
                else:
                    raise ValueError(f"Unsupported DB_TYPE: {self.db_type}")
                await self._init_schema()
                await self._load_dead_letter_queue()
                self.is_connected = True
                self._writer_task = asyncio.create_task(self._writer_loop())
                if CFG.DB_AUTO_BACKUP:
                    self._backup_task = asyncio.create_task(self._backup_loop())
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())
                logger.info(f"✅ Database v8.2 connected ({self.db_type})")
            except Exception as e:
                logger.error(f"❌ Database connection failed: {e}")
                raise

    async def _connect_sqlite(self) -> None:
        """الاتصال بـ SQLite مع تحسينات الأداء."""
        self._sqlite_conn = await aiosqlite.connect(
            CFG.DB_FILE,
            timeout=30,
            isolation_level=None,
        )
        self._sqlite_conn.row_factory = aiosqlite.Row
        await self._sqlite_conn.execute("PRAGMA journal_mode=WAL")
        await self._sqlite_conn.execute("PRAGMA synchronous=NORMAL")
        await self._sqlite_conn.execute(f"PRAGMA cache_size={CFG.SQLITE_CACHE_SIZE}")
        await self._sqlite_conn.execute(f"PRAGMA mmap_size={CFG.SQLITE_MMAP_SIZE}")
        await self._sqlite_conn.execute("PRAGMA temp_store=MEMORY")
        await self._sqlite_conn.execute("PRAGMA foreign_keys=ON")
        await self._sqlite_conn.execute("PRAGMA auto_vacuum=INCREMENTAL")

    async def _connect_postgresql(self) -> None:
        """الاتصال بـ PostgreSQL مع pool."""
        if not _POSTGRES_AVAILABLE:
            raise RuntimeError("asyncpg not installed")
        self._pool = await asyncpg.create_pool(
            dsn=CFG.DB_URL,
            min_size=CFG.DB_POOL_MIN,
            max_size=CFG.DB_POOL_MAX,
            max_inactive_connection_lifetime=300,
        )

    # ─── Schema ───────────────────────────────────────────────────────────────

    async def _init_schema(self) -> None:
        """تهيئة مخطط قاعدة البيانات (مع IntentEngine)."""
        if self.db_type == "sqlite":
            await self._init_sqlite_schema()
        else:
            await self._init_postgresql_schema()

    async def _init_sqlite_schema(self) -> None:
        """إنشاء جداول SQLite."""
        async with self._sqlite_conn.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_hash TEXT UNIQUE NOT NULL,
                chat_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                account_name TEXT NOT NULL,
                message_text TEXT NOT NULL,
                timestamp REAL NOT NULL,
                keyword_found TEXT,
                decision TEXT,
                confidence REAL,
                reasons TEXT,
                has_media BOOLEAN DEFAULT 0,
                media_type TEXT,
                processing_time_ms REAL,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_hash TEXT UNIQUE NOT NULL,
                chat_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                account_name TEXT NOT NULL,
                keyword TEXT NOT NULL,
                alert_text TEXT NOT NULL,
                timestamp REAL NOT NULL,
                decision TEXT,
                confidence REAL,
                reasons TEXT,
                intent_verb TEXT,
                academic_object TEXT,
                negation_detected BOOLEAN DEFAULT 0,
                advert_score REAL,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE TABLE IF NOT EXISTS sender_stats (
                sender_id INTEGER PRIMARY KEY,
                total_messages INTEGER DEFAULT 0,
                alerts_sent INTEGER DEFAULT 0,
                last_message_time REAL,
                last_alert_time REAL,
                reputation_score REAL DEFAULT 0.0,
                is_blocked BOOLEAN DEFAULT 0,
                created_at REAL DEFAULT (strftime('%s', 'now')),
                updated_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE TABLE IF NOT EXISTS account_stats (
                account_name TEXT PRIMARY KEY,
                total_messages INTEGER DEFAULT 0,
                alerts_sent INTEGER DEFAULT 0,
                rate_limit_hits INTEGER DEFAULT 0,
                flood_waits INTEGER DEFAULT 0,
                last_message_time REAL,
                created_at REAL DEFAULT (strftime('%s', 'now')),
                updated_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE TABLE IF NOT EXISTS keyword_stats (
                keyword TEXT PRIMARY KEY,
                match_count INTEGER DEFAULT 0,
                last_match_time REAL,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE TABLE IF NOT EXISTS alert_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload TEXT NOT NULL,
                attempts INTEGER DEFAULT 0,
                next_attempt REAL,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE TABLE IF NOT EXISTS blocked_senders (
                sender_id INTEGER PRIMARY KEY,
                reason TEXT,
                blocked_by TEXT,
                blocked_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE TABLE IF NOT EXISTS blocked_chats (
                chat_id INTEGER PRIMARY KEY,
                reason TEXT,
                blocked_by TEXT,
                blocked_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE TABLE IF NOT EXISTS sender_contacts (
                sender_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                phone TEXT,
                is_premium BOOLEAN DEFAULT 0,
                is_verified BOOLEAN DEFAULT 0,
                is_scam BOOLEAN DEFAULT 0,
                is_fake BOOLEAN DEFAULT 0,
                is_bot BOOLEAN DEFAULT 0,
                last_message_link TEXT,
                last_group_link TEXT,
                created_at REAL DEFAULT (strftime('%s', 'now')),
                updated_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE TABLE IF NOT EXISTS dead_letter_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT NOT NULL,
                error TEXT NOT NULL,
                retry_count INTEGER DEFAULT 0,
                next_retry REAL,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );

            -- Indexes
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id);
            CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
            CREATE INDEX IF NOT EXISTS idx_messages_keyword ON messages(keyword_found);
            CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp);
            CREATE INDEX IF NOT EXISTS idx_alerts_sender ON alerts(sender_id);
            CREATE INDEX IF NOT EXISTS idx_alerts_keyword ON alerts(keyword);
            CREATE INDEX IF NOT EXISTS idx_sender_stats_score ON sender_stats(reputation_score);
            CREATE INDEX IF NOT EXISTS idx_alert_queue_next ON alert_queue(next_attempt);
            CREATE INDEX IF NOT EXISTS idx_dead_letter_next ON dead_letter_queue(next_retry);
        """):
            pass

        await self._migrate_schema()
        await self._sqlite_conn.commit()

    async def _init_postgresql_schema(self) -> None:
        """إنشاء جداول PostgreSQL."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    message_hash TEXT UNIQUE NOT NULL,
                    chat_id BIGINT NOT NULL,
                    sender_id BIGINT NOT NULL,
                    account_name TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    timestamp DOUBLE PRECISION NOT NULL,
                    keyword_found TEXT,
                    decision TEXT,
                    confidence REAL,
                    reasons JSONB,
                    has_media BOOLEAN DEFAULT FALSE,
                    media_type TEXT,
                    processing_time_ms REAL,
                    created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
                );

                CREATE TABLE IF NOT EXISTS alerts (
                    id SERIAL PRIMARY KEY,
                    message_hash TEXT UNIQUE NOT NULL,
                    chat_id BIGINT NOT NULL,
                    sender_id BIGINT NOT NULL,
                    account_name TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    alert_text TEXT NOT NULL,
                    timestamp DOUBLE PRECISION NOT NULL,
                    decision TEXT,
                    confidence REAL,
                    reasons JSONB,
                    intent_verb TEXT,
                    academic_object TEXT,
                    negation_detected BOOLEAN DEFAULT FALSE,
                    advert_score REAL,
                    created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
                );

                CREATE TABLE IF NOT EXISTS sender_stats (
                    sender_id BIGINT PRIMARY KEY,
                    total_messages INTEGER DEFAULT 0,
                    alerts_sent INTEGER DEFAULT 0,
                    last_message_time DOUBLE PRECISION,
                    last_alert_time DOUBLE PRECISION,
                    reputation_score REAL DEFAULT 0.0,
                    is_blocked BOOLEAN DEFAULT FALSE,
                    created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
                    updated_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
                );

                CREATE TABLE IF NOT EXISTS account_stats (
                    account_name TEXT PRIMARY KEY,
                    total_messages INTEGER DEFAULT 0,
                    alerts_sent INTEGER DEFAULT 0,
                    rate_limit_hits INTEGER DEFAULT 0,
                    flood_waits INTEGER DEFAULT 0,
                    last_message_time DOUBLE PRECISION,
                    created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
                    updated_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
                );

                CREATE TABLE IF NOT EXISTS keyword_stats (
                    keyword TEXT PRIMARY KEY,
                    match_count INTEGER DEFAULT 0,
                    last_match_time DOUBLE PRECISION,
                    created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
                );

                CREATE TABLE IF NOT EXISTS alert_queue (
                    id SERIAL PRIMARY KEY,
                    payload TEXT NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    next_attempt DOUBLE PRECISION,
                    created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
                );

                CREATE TABLE IF NOT EXISTS blocked_senders (
                    sender_id BIGINT PRIMARY KEY,
                    reason TEXT,
                    blocked_by TEXT,
                    blocked_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
                );

                CREATE TABLE IF NOT EXISTS blocked_chats (
                    chat_id BIGINT PRIMARY KEY,
                    reason TEXT,
                    blocked_by TEXT,
                    blocked_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
                );

                CREATE TABLE IF NOT EXISTS sender_contacts (
                    sender_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    phone TEXT,
                    is_premium BOOLEAN DEFAULT FALSE,
                    is_verified BOOLEAN DEFAULT FALSE,
                    is_scam BOOLEAN DEFAULT FALSE,
                    is_fake BOOLEAN DEFAULT FALSE,
                    is_bot BOOLEAN DEFAULT FALSE,
                    last_message_link TEXT,
                    last_group_link TEXT,
                    created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
                    updated_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
                );

                CREATE TABLE IF NOT EXISTS dead_letter_queue (
                    id SERIAL PRIMARY KEY,
                    data TEXT NOT NULL,
                    error TEXT NOT NULL,
                    retry_count INTEGER DEFAULT 0,
                    next_retry DOUBLE PRECISION,
                    created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
                );

                -- Indexes
                CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
                CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id);
                CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
                CREATE INDEX IF NOT EXISTS idx_messages_keyword ON messages(keyword_found);
                CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp);
                CREATE INDEX IF NOT EXISTS idx_alerts_sender ON alerts(sender_id);
                CREATE INDEX IF NOT EXISTS idx_alerts_keyword ON alerts(keyword);
                CREATE INDEX IF NOT EXISTS idx_sender_stats_score ON sender_stats(reputation_score);
                CREATE INDEX IF NOT EXISTS idx_alert_queue_next ON alert_queue(next_attempt);
                CREATE INDEX IF NOT EXISTS idx_dead_letter_next ON dead_letter_queue(next_retry);
            """)

        await self._migrate_schema()

    # ─── Schema Migration ─────────────────────────────────────────────────────

    async def _migrate_schema(self) -> None:
        """ترحيل المخطط لدعم IntentEngine."""
        logger.info("🔄 Checking schema migrations for IntentEngine...")
        try:
            if self.db_type == "sqlite":
                await self._migrate_sqlite_schema()
            else:
                await self._migrate_postgresql_schema()
            logger.info("✅ Schema migrations completed")
        except Exception as e:
            logger.error(f"❌ Schema migration failed: {e}")
            raise

    async def _migrate_sqlite_schema(self) -> None:
        """ترحيل مخطط SQLite."""
        # messages table
        cursor = await self._sqlite_conn.execute("PRAGMA table_info(messages)")
        columns = {row[1] for row in await cursor.fetchall()}
        new_columns = {
            "decision": "TEXT",
            "confidence": "REAL",
            "reasons": "TEXT",
            "has_media": "BOOLEAN DEFAULT 0",
            "media_type": "TEXT",
            "processing_time_ms": "REAL",
        }
        for col, col_type in new_columns.items():
            if col not in columns:
                await self._sqlite_conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {col_type}")
                logger.info(f"Added column messages.{col}")

        # alerts table
        cursor = await self._sqlite_conn.execute("PRAGMA table_info(alerts)")
        columns = {row[1] for row in await cursor.fetchall()}
        new_columns = {
            "decision": "TEXT",
            "confidence": "REAL",
            "reasons": "TEXT",
            "intent_verb": "TEXT",
            "academic_object": "TEXT",
            "negation_detected": "BOOLEAN DEFAULT 0",
            "advert_score": "REAL",
        }
        for col, col_type in new_columns.items():
            if col not in columns:
                await self._sqlite_conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} {col_type}")
                logger.info(f"Added column alerts.{col}")

    async def _migrate_postgresql_schema(self) -> None:
        """ترحيل مخطط PostgreSQL."""
        async with self._pool.acquire() as conn:
            # messages table
            result = await conn.fetch("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'messages'
            """)
            columns = {row['column_name'] for row in result}
            new_columns = {
                "decision": "TEXT",
                "confidence": "REAL",
                "reasons": "JSONB",
                "has_media": "BOOLEAN DEFAULT FALSE",
                "media_type": "TEXT",
                "processing_time_ms": "REAL",
            }
            for col, col_type in new_columns.items():
                if col not in columns:
                    await conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {col_type}")
                    logger.info(f"Added column messages.{col}")

            # alerts table
            result = await conn.fetch("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'alerts'
            """)
            columns = {row['column_name'] for row in result}
            new_columns = {
                "decision": "TEXT",
                "confidence": "REAL",
                "reasons": "JSONB",
                "intent_verb": "TEXT",
                "academic_object": "TEXT",
                "negation_detected": "BOOLEAN DEFAULT FALSE",
                "advert_score": "REAL",
            }
            for col, col_type in new_columns.items():
                if col not in columns:
                    await conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} {col_type}")
                    logger.info(f"Added column alerts.{col}")

    # ─── Dead Letter Queue ────────────────────────────────────────────────────

    async def _load_dead_letter_queue(self) -> None:
        """تحميل قائمة الرسائل الميتة من قاعدة البيانات."""
        try:
            if self.db_type == "sqlite":
                cursor = await self._sqlite_conn.execute(
                    "SELECT data, error, retry_count, next_retry FROM dead_letter_queue ORDER BY created_at"
                )
                rows = await cursor.fetchall()
                for row in rows:
                    data = orjson.loads(row[0])
                    item = DeadLetterItem(data, row[1])
                    item.retry_count = row[2]
                    item.next_retry = row[3]
                    self._dead_letter.append(item)
            else:
                async with self._pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT data, error, retry_count, next_retry FROM dead_letter_queue ORDER BY created_at"
                    )
                    for row in rows:
                        data = orjson.loads(row['data'])
                        item = DeadLetterItem(data, row['error'])
                        item.retry_count = row['retry_count']
                        item.next_retry = row['next_retry']
                        self._dead_letter.append(item)
            logger.info(f"Loaded {len(self._dead_letter)} dead letter items")
        except Exception as e:
            logger.error(f"Failed to load dead letter queue: {e}")

    async def _save_dead_letter_item(self, item: DeadLetterItem) -> None:
        """حفظ عنصر في قائمة الرسائل الميتة."""
        try:
            if self.db_type == "sqlite":
                await self._sqlite_conn.execute(
                    "INSERT INTO dead_letter_queue (data, error, retry_count, next_retry) VALUES (?, ?, ?, ?)",
                    (orjson.dumps(item.data).decode(), item.error, item.retry_count, item.next_retry),
                )
                await self._sqlite_conn.commit()
            else:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO dead_letter_queue (data, error, retry_count, next_retry) VALUES ($1, $2, $3, $4)",
                        orjson.dumps(item.data).decode(), item.error, item.retry_count, item.next_retry,
                    )
        except Exception as e:
            logger.error(f"Failed to save dead letter item: {e}")

    async def _process_dead_letter_queue(self) -> None:
        """معالجة قائمة الرسائل الميتة."""
        if not self._dead_letter:
            return
        items_to_retry = []
        async with self._dead_letter_lock:
            for _ in range(len(self._dead_letter)):
                item = self._dead_letter.popleft()
                if item.should_retry() and item.retry_count < CFG.DEAD_LETTER_MAX_RETRIES:
                    items_to_retry.append(item)
                elif item.retry_count >= CFG.DEAD_LETTER_MAX_RETRIES:
                    logger.warning(f"Dead letter item exceeded max retries: {item.error}")
                else:
                    self._dead_letter.append(item)
        for item in items_to_retry:
            try:
                await self._retry_dead_letter_item(item)
            except Exception as e:
                item.mark_retry()
                await self._save_dead_letter_item(item)
                logger.error(f"Dead letter retry failed: {e}")

    async def _retry_dead_letter_item(self, item: DeadLetterItem) -> None:
        """إعادة محاولة معالجة عنصر ميت."""
        data = item.data
        if data.get("type") == "message":
            await self.log_message(**data["kwargs"])
        elif data.get("type") == "alert":
            await self.log_alert(**data["kwargs"])

    # ─── Writer Loop ──────────────────────────────────────────────────────────

    async def _writer_loop(self) -> None:
        """حلقة الكتابة الرئيسية."""
        while self.is_connected:
            try:
                await asyncio.sleep(1)
                await self._flush_if_needed()
                await self._process_dead_letter_queue()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Writer loop error: {e}")
                await asyncio.sleep(5)

    async def _flush_if_needed(self) -> None:
        """تفريغ الدفعة إذا لزم الأمر."""
        async with self._batch_lock:
            should_flush = (
                len(self._batch) >= self._batch_size or
                (self._batch and time.time() - self._last_flush >= self._batch_timeout)
            )
            if not should_flush:
                return
            batch_to_flush = self._batch[:]
            self._batch.clear()
            self._last_flush = time.time()

        try:
            await self._flush_batch(batch_to_flush)
        except Exception as e:
            logger.error(f"Batch flush error: {e}")
            async with self._batch_lock:
                self._batch.extend(batch_to_flush)

    async def _flush_batch(self, batch: List[Tuple]) -> None:
        """تفريغ دفعة من العمليات."""
        if not batch:
            return
        messages_data = []
        alerts_data = []
        for item in batch:
            if item[0] == "message":
                messages_data.append(item[1:])
            elif item[0] == "alert":
                alerts_data.append(item[1:])

        if messages_data:
            await self._flush_messages(messages_data)
        if alerts_data:
            await self._flush_alerts(alerts_data)

    # ─── Core Logging Methods ─────────────────────────────────────────────────

    async def log_message(
        self,
        chat_id: int,
        sender_id: int,
        account_name: str,
        message_text: str,
        timestamp: float,
        keyword_found: Optional[str] = None,
        decision: Optional[str] = None,
        confidence: Optional[float] = None,
        reasons: Optional[List[str]] = None,
        has_media: bool = False,
        media_type: Optional[str] = None,
        processing_time_ms: Optional[float] = None,
        **kwargs,
    ) -> None:
        """تسجيل رسالة (مع دعم IntentEngine)."""
        message_hash = hashlib.sha256(
            f"{chat_id}:{sender_id}:{message_text}:{timestamp}".encode()
        ).hexdigest()
        async with self._batch_lock:
            self._batch.append((
                "message",
                message_hash, chat_id, sender_id, account_name, message_text, timestamp,
                keyword_found, decision, confidence,
                orjson.dumps(reasons or []).decode(), has_media, media_type, processing_time_ms,
            ))

    async def log_alert(
        self,
        chat_id: int,
        sender_id: int,
        account_name: str,
        keyword: str,
        alert_text: str,
        timestamp: float,
        decision: Optional[str] = None,
        confidence: Optional[float] = None,
        reasons: Optional[List[str]] = None,
        intent_verb: Optional[str] = None,
        academic_object: Optional[str] = None,
        negation_detected: bool = False,
        advert_score: Optional[float] = None,
        **kwargs,
    ) -> None:
        """تسجيل تنبيه (مع دعم IntentEngine)."""
        message_hash = hashlib.sha256(
            f"alert:{chat_id}:{sender_id}:{keyword}:{timestamp}".encode()
        ).hexdigest()
        async with self._batch_lock:
            self._batch.append((
                "alert",
                message_hash, chat_id, sender_id, account_name, keyword, alert_text, timestamp,
                decision, confidence, orjson.dumps(reasons or []).decode(),
                intent_verb, academic_object, negation_detected, advert_score,
            ))

    # ─── Flush Methods ────────────────────────────────────────────────────────

    async def _flush_messages(self, messages_data: List[Tuple]) -> None:
        """تفريغ رسائل."""
        if self.db_type == "sqlite":
            sql = """
                INSERT OR IGNORE INTO messages
                (message_hash, chat_id, sender_id, account_name, message_text, timestamp,
                 keyword_found, decision, confidence, reasons, has_media, media_type, processing_time_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            await self._sqlite_conn.executemany(sql, messages_data)
            await self._sqlite_conn.commit()
        else:
            sql = """
                INSERT INTO messages
                (message_hash, chat_id, sender_id, account_name, message_text, timestamp,
                 keyword_found, decision, confidence, reasons, has_media, media_type, processing_time_ms)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (message_hash) DO NOTHING
            """
            async with self._pool.acquire() as conn:
                await conn.executemany(sql, messages_data)

    async def _flush_alerts(self, alerts_data: List[Tuple]) -> None:
        """تفريغ تنبيهات."""
        if self.db_type == "sqlite":
            sql = """
                INSERT OR IGNORE INTO alerts
                (message_hash, chat_id, sender_id, account_name, keyword, alert_text, timestamp,
                 decision, confidence, reasons, intent_verb, academic_object, negation_detected, advert_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            await self._sqlite_conn.executemany(sql, alerts_data)
            await self._sqlite_conn.commit()
        else:
            sql = """
                INSERT INTO alerts
                (message_hash, chat_id, sender_id, account_name, keyword, alert_text, timestamp,
                 decision, confidence, reasons, intent_verb, academic_object, negation_detected, advert_score)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                ON CONFLICT (message_hash) DO NOTHING
            """
            async with self._pool.acquire() as conn:
                await conn.executemany(sql, alerts_data)

    # ─── Query Methods ────────────────────────────────────────────────────────

    async def get_stats(self) -> Dict[str, Any]:
        """جلب إحصائيات عامة."""
        out = {
            "total_messages": 0,
            "alerts_sent": 0,
            "unique_senders": 0,
            "blocked_senders": 0,
            "avg_reputation": 0.0,
            "alerts_last_hour": 0,
            "messages_last_hour": 0,
            "decision_accept": 0,
            "decision_review": 0,
            "decision_ignore": 0,
            "avg_confidence": 0.0,
        }
        try:
            row = await self._fetchone("SELECT COUNT(*) as cnt FROM messages")
            out["total_messages"] = int(row["cnt"]) if row else 0

            row = await self._fetchone("SELECT COUNT(*) as cnt FROM alerts")
            out["alerts_sent"] = int(row["cnt"]) if row else 0

            row = await self._fetchone("SELECT COUNT(DISTINCT sender_id) as cnt FROM messages")
            out["unique_senders"] = int(row["cnt"]) if row else 0

            row = await self._fetchone("SELECT COUNT(*) as cnt FROM blocked_senders")
            out["blocked_senders"] = int(row["cnt"]) if row else 0

            row = await self._fetchone("SELECT AVG(reputation_score) as avg FROM sender_stats WHERE total_messages > 0")
            out["avg_reputation"] = float(row["avg"]) if row and row["avg"] else 0.0

            cutoff = time.time() - 3600
            row = await self._fetchone("SELECT COUNT(*) as cnt FROM alerts WHERE timestamp >= ?", (cutoff,))
            out["alerts_last_hour"] = int(row["cnt"]) if row else 0

            row = await self._fetchone("SELECT COUNT(*) as cnt FROM messages WHERE timestamp >= ?", (cutoff,))
            out["messages_last_hour"] = int(row["cnt"]) if row else 0

            # NEW v8.2: إحصائيات القرارات (GROUP BY -> multiple rows: use _fetchall)
            decision_rows = await self._fetchall(
                "SELECT decision, COUNT(*) as count FROM alerts GROUP BY decision"
            )
            for r in decision_rows:
                out[f"decision_{r['decision']}"] = r["count"]

            row = await self._fetchone("SELECT AVG(confidence) as avg FROM alerts WHERE confidence > 0")
            out["avg_confidence"] = float(row["avg"]) if row and row["avg"] else 0.0

        except Exception as e:
            logger.error(f"Stats query error: {e}")
        return out

    async def queue_size(self) -> int:
        """حجم طابور التنبيهات."""
        row = await self._fetchone("SELECT COUNT(*) as cnt FROM alert_queue")
        return int(row["cnt"]) if row else 0

    async def purge_queue(self) -> int:
        """تفريغ طابور التنبيهات."""
        row = await self._fetchone("SELECT COUNT(*) as cnt FROM alert_queue")
        count = int(row["cnt"]) if row else 0
        await self._execute("DELETE FROM alert_queue")
        await self._commit()
        return count

    # ─── Helper Methods ───────────────────────────────────────────────────────

    async def _fetchone(self, sql: str, params: Tuple = ()) -> Optional[Dict[str, Any]]:
        """جلب صف واحد."""
        self.queries_executed += 1
        try:
            if self.db_type == "sqlite":
                cursor = await self._sqlite_conn.execute(sql, params)
                row = await cursor.fetchone()
                return dict(row) if row else None
            else:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(sql, *params)
                    return dict(row) if row else None
        except Exception as e:
            self.queries_failed += 1
            logger.error(f"Fetchone error: {e}")
            return None

    async def _fetchall(self, sql: str, params: Tuple = ()) -> List[Dict[str, Any]]:
        """جلب جميع الصفوف."""
        self.queries_executed += 1
        try:
            if self.db_type == "sqlite":
                cursor = await self._sqlite_conn.execute(sql, params)
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
            else:
                async with self._pool.acquire() as conn:
                    rows = await conn.fetch(sql, *params)
                    return [dict(row) for row in rows]
        except Exception as e:
            self.queries_failed += 1
            logger.error(f"Fetchall error: {e}")
            return []

    async def _execute(self, sql: str, params: Tuple = ()) -> None:
        """تنفيذ استعلام."""
        self.queries_executed += 1
        try:
            if self.db_type == "sqlite":
                await self._sqlite_conn.execute(sql, params)
            else:
                async with self._pool.acquire() as conn:
                    await conn.execute(sql, *params)
        except Exception as e:
            self.queries_failed += 1
            logger.error(f"Execute error: {e}")

    async def _executemany(self, sql: str, params_list: List[Tuple]) -> None:
        """تنفيذ استعلام متعدد."""
        self.queries_executed += 1
        try:
            if self.db_type == "sqlite":
                await self._sqlite_conn.executemany(sql, params_list)
            else:
                async with self._pool.acquire() as conn:
                    await conn.executemany(sql, params_list)
        except Exception as e:
            self.queries_failed += 1
            logger.error(f"Executemany error: {e}")

    async def _commit(self) -> None:
        """تأكيد المعاملة."""
        if self.db_type == "sqlite":
            await self._sqlite_conn.commit()

    # ─── Block Methods ────────────────────────────────────────────────────────

    async def block_sender(self, sender_id: int, reason: str, blocked_by: str) -> None:
        """حظر مرسل."""
        await self._execute(
            "INSERT OR REPLACE INTO blocked_senders (sender_id, reason, blocked_by) VALUES (?, ?, ?)",
            (sender_id, reason, blocked_by),
        )
        await self._commit()

    async def unblock_sender(self, sender_id: int) -> None:
        """إلغاء حظر مرسل."""
        await self._execute("DELETE FROM blocked_senders WHERE sender_id = ?", (sender_id,))
        await self._commit()

    async def is_sender_blocked(self, sender_id: int) -> bool:
        """التحقق من حظر مرسل."""
        row = await self._fetchone("SELECT 1 FROM blocked_senders WHERE sender_id = ?", (sender_id,))
        return row is not None

    async def block_chat(self, chat_id: int, reason: str, blocked_by: str) -> None:
        """حظر محادثة."""
        await self._execute(
            "INSERT OR REPLACE INTO blocked_chats (chat_id, reason, blocked_by) VALUES (?, ?, ?)",
            (chat_id, reason, blocked_by),
        )
        await self._commit()

    async def unblock_chat(self, chat_id: int) -> None:
        """إلغاء حظر محادثة."""
        await self._execute("DELETE FROM blocked_chats WHERE chat_id = ?", (chat_id,))
        await self._commit()

    async def is_chat_blocked(self, chat_id: int) -> bool:
        """التحقق من حظر محادثة."""
        row = await self._fetchone("SELECT 1 FROM blocked_chats WHERE chat_id = ?", (chat_id,))
        return row is not None

    # ─── Cleanup Methods ──────────────────────────────────────────────────────

    async def cleanup_old_data(self, days: int = 30) -> int:
        """تنظيف البيانات القديمة."""
        cutoff = time.time() - (days * 86400)
        deleted = 0
        try:
            if self.db_type == "sqlite":
                cursor = await self._sqlite_conn.execute(
                    "DELETE FROM messages WHERE timestamp < ?", (cutoff,)
                )
                deleted += cursor.rowcount
                cursor = await self._sqlite_conn.execute(
                    "DELETE FROM alerts WHERE timestamp < ?", (cutoff,)
                )
                deleted += cursor.rowcount
                await self._sqlite_conn.commit()
            else:
                async with self._pool.acquire() as conn:
                    result = await conn.execute(
                        "DELETE FROM messages WHERE timestamp < $1", cutoff
                    )
                    deleted += int(result.split()[-1])
                    result = await conn.execute(
                        "DELETE FROM alerts WHERE timestamp < $1", cutoff
                    )
                    deleted += int(result.split()[-1])
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        return deleted

    async def cleanup_dead_letters(self, days: int = 7) -> None:
        """تنظيف قائمة الرسائل الميتة."""
        cutoff = time.time() - (days * 86400)
        try:
            if self.db_type == "sqlite":
                await self._sqlite_conn.execute(
                    "DELETE FROM dead_letter_queue WHERE created_at < ?", (cutoff,)
                )
                await self._sqlite_conn.commit()
            else:
                async with self._pool.acquire() as conn:
                    await conn.execute(
                        "DELETE FROM dead_letter_queue WHERE created_at < $1", cutoff
                    )
        except Exception as e:
            logger.error(f"Dead letter cleanup error: {e}")

    # ─── Batch Operations (Legacy Support) ────────────────────────────────────

    async def _flush(self) -> None:
        """تفريغ جميع العمليات المعلقة."""
        async with self._batch_lock:
            if not self._batch:
                return
            batch_to_flush = self._batch[:]
            self._batch.clear()
            self._last_flush = time.time()
        try:
            await self._flush_batch(batch_to_flush)
        except Exception as e:
            logger.error(f"Final flush error: {e}")

    async def _flush_batch_legacy(self, alerts_data: List[Tuple]) -> None:
        """تفريغ دفعة من التنبيهات (للتوافق مع الإصدارات القديمة)."""
        if not alerts_data:
            return
        try:
            if self.db_type == "postgresql":
                values = []
                params = []
                for i, data in enumerate(alerts_data):
                    base = i * 14
                    values.append(
                        f"(${base+1}, ${base+2}, ${base+3}, ${base+4}, ${base+5}, ${base+6}, ${base+7}, "
                        f"${base+8}, ${base+9}, ${base+10}, ${base+11}, ${base+12}, ${base+13}, ${base+14})"
                    )
                    params.extend(data)
                sql = f"""
                    INSERT INTO alerts
                    (message_hash, chat_id, sender_id, account_name, keyword, alert_text, timestamp,
                     decision, confidence, reasons, intent_verb, academic_object, negation_detected, advert_score)
                    VALUES {','.join(values)}
                    ON CONFLICT (message_hash) DO NOTHING
                """
                await self._pool.execute(sql, *params)

                # تحديث sender_stats
                for data in alerts_data:
                    await self._pool.execute(
                        "UPDATE sender_stats SET alerts_sent = alerts_sent + 1, last_alert_time = $1 "
                        "WHERE sender_id = $2",
                        data[6], data[2],  # timestamp, sender_id
                    )
            else:
                # SQLite
                sql = """
                    INSERT OR IGNORE INTO alerts
                    (message_hash, chat_id, sender_id, account_name, keyword, alert_text, timestamp,
                     decision, confidence, reasons, intent_verb, academic_object, negation_detected, advert_score)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                params_list = [
                    (
                        data[0], data[1], data[2], data[3], data[4],  # msg_hash, chat_id, sender_id, account_name, keyword
                        data[5], data[6],  # alert_text, timestamp
                        data[7], data[8], data[9],  # decision, confidence, reasons
                        data[10], data[11], data[12], data[13]  # intent_verb, academic_object, negation_detected, advert_score
                    )
                    for data in alerts_data
                ]
                await self._executemany(sql, params_list)

                for data in alerts_data:
                    await self._execute(
                        "UPDATE sender_stats SET alerts_sent = alerts_sent + 1, last_alert_time = ? "
                        "WHERE sender_id = ?",
                        (data[6], data[2]),
                    )
                await self._commit()

            logger.debug(f"Batch flush: {len(alerts_data)} alerts written (with IntentEngine fields)")

        except Exception as e:
            logger.error(f"DB flush error: {e}")
            # إعادة الأحداث إلى الدفعة
            async with self._batch_lock:
                for data in alerts_data:
                    self._batch.append(("alert",) + data)

    async def _backup_loop(self) -> None:
        if self.db_type != "sqlite":
            return
        while self.is_connected:
            try:
                await asyncio.sleep(CFG.SESSION_BACKUP_INTERVAL)
                backup_path = Path(f"{CFG.DB_FILE}.gz")
                raw = Path(CFG.DB_FILE).read_bytes()
                compressed = zlib.compress(raw, level=6)
                backup_path.write_bytes(compressed)
                logger.info(f"DB backup: {backup_path} ({len(compressed):,} bytes)")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Backup error: {e}")
                await asyncio.sleep(60)

    async def _cleanup_loop(self) -> None:
        while self.is_connected:
            try:
                await asyncio.sleep(CFG.CLEANUP_INTERVAL)
                deleted = await self.cleanup_old_data(days=CFG.DEAD_LETTER_CLEANUP_DAYS)
                if deleted:
                    logger.info(f"Cleanup: {deleted} old rows removed")
                await self.cleanup_dead_letters(days=CFG.DEAD_LETTER_CLEANUP_DAYS)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup loop error: {e}")
                await asyncio.sleep(60)

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
        """Dashboard: جلب التنبيهات مع تصفية متقدمة (بما فيها حقول IntentEngine)."""
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
        cutoff = time.time() - (hours * 3600)

        if self.db_type == "sqlite":
            sql = """
                SELECT
                    strftime('%Y-%m-%d %H:00:00', datetime(timestamp, 'unixepoch')) as hour,
                    COUNT(*) as messages_count,
                    SUM(CASE WHEN keyword_found IS NOT NULL THEN 1 ELSE 0 END) as alerts_count,
                    SUM(CASE WHEN decision = 'accept' THEN 1 ELSE 0 END) as accepted_count,
                    AVG(CASE WHEN confidence > 0 THEN confidence END) as avg_confidence
                FROM messages
                WHERE timestamp >= ?
                GROUP BY hour
                ORDER BY hour ASC
            """
        else:
            sql = """
                SELECT
                    to_char(to_timestamp(timestamp), 'YYYY-MM-DD HH24:00:00') as hour,
                    COUNT(*) as messages_count,
                    SUM(CASE WHEN keyword_found IS NOT NULL THEN 1 ELSE 0 END) as alerts_count,
                    SUM(CASE WHEN decision = 'accept' THEN 1 ELSE 0 END) as accepted_count,
                    AVG(CASE WHEN confidence > 0 THEN confidence END) as avg_confidence
                FROM messages
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

        summary = {
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

            row = await self._fetchone("SELECT AVG(reputation_score) as avg FROM sender_stats WHERE total_messages > 0")
            summary["avg_reputation"] = float(row["avg"]) if row and row["avg"] else 0.0

            row = await self._fetchone("SELECT COUNT(*) as cnt FROM blocked_senders")
            summary["blocked_senders"] = int(row["cnt"]) if row else 0

            # NEW v8.2: إحصائيات القرارات
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
        for task in (self._writer_task, self._backup_task, self._cleanup_task):
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
        logger.info("Database v8.2 closed")
