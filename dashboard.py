#!/usr/bin/env python3
"""
dashboard.py – Telegram Bot Control Panel v3.0 (HARDENED, IntentEngine-compatible)
FastAPI + WebSocket Dashboard for EnhancedTelegramBot
Compatible with: config.py v13.1, filter_engine.py v14.1, database.py v9.0,
                  monitors.py v9.7, main.py v13.1, keywords.json v15.1

v3.0 (this pass) — full audit fix, dashboard.py ONLY:

  FIXED #1  — Non-existent runtime reload call: filter_engine.py v14.1 exposes
              a real reload_keywords() (and a backward-compat alias
              _build_keyword_sets = reload_keywords). Keyword mutation now
              calls the real method, wrapped so failures are never silent.

  FIXED #2  — Schema-unaware keyword editing: keywords.json v15.1 is a deeply
              nested structure (dict-of-dicts/lists), not flat lists per
              category. add/delete keyword now resolve a DOTTED PATH
              (e.g. "ad_blockers", "high_confidence_boost_patterns.patterns",
              "request_phrases.direct_requests") and validate the resolved
              node is actually a list before mutating — never an
              AttributeError/500 on a dict-shaped category.

  FIXED #3  — Inconsistent persisted/runtime state: keywords.json is now
              written atomically (temp file + os.replace) and, if the
              subsequent filter reload fails, the file is ROLLED BACK to its
              previous content so the file and the running filter are never
              left pointing at different keyword sets.

  FIXED #4  — Statistics schema mismatch: monitors.py's get_stats() returns
              "alerts_sent", never "alerts", and has no per-account "queued"
              field (the queue is global, exposed only via db.queue_size()).
              All account-statistics call sites fixed accordingly; a
              fabricated "filter_avg_confidence" field (filter_engine.py's
              telemetry never populates that key) was removed rather than
              silently showing a permanent fake 0.0.

  FIXED #5  — Duplicate DB background loops: dashboard.py previously always
              opened its OWN EnhancedDatabase() instance, meaning a second
              set of _writer_loop/_backup_loop tasks running against the same
              SQLite file as main.py's bot.db — the same class of duplicate-
              ownership bug database.py v9.0 already fixed for cleanup.
              The dashboard's lifespan now REUSES bot.db when a bot_ref is
              already attached (the normal main.py-embedded case), and only
              opens a standalone connection when running dashboard.py alone.

  FIXED #6  — CFG mutation crash: config.py's _ConfigData is a frozen
              dataclass. The old /api/settings handler attempted
              `CFG.PREFILTER_ENABLED = ...` / `CFG.LANGUAGE_FILTER = ...`,
              which raises FrozenInstanceError on every call. These are now
              honestly reported as unsupported at runtime instead of
              crashing the endpoint; only genuinely mutable state
              (AdaptiveRateLimiter's max/min/hour limits) is applied.

  FIXED #7  — Broken /api/restart: called bot.stop() then
              bot._send_startup_message() — stop() disconnects everything
              and closes the DB, so the follow-up message could never send,
              and main.py exposes no in-process restart primitive at all.
              Restart now triggers an honest graceful shutdown and tells the
              caller the hosting platform is expected to restart the
              process, instead of pretending an in-place restart happened.

  FIXED #8  — Timing-unsafe token comparison (verify_token used `!=`) →
              hmac.compare_digest, sourced from CFG.DASHBOARD_AUTH_TOKEN
              (single source of truth) instead of a second os.getenv() read.

  FIXED #9  — CORS: allow_origins=["*"] combined with allow_credentials=True
              is replaced with an env-var-driven allowlist
              (DASHBOARD_CORS_ORIGINS, comma-separated) that defaults to
              same-origin-only (no fabricated origin list — none exists
              anywhere in the provided architecture) and scoped
              methods/headers instead of "*".

  FIXED #10 — WebSocket lifecycle: ConnectionManager.broadcast silently
              swallowed send failures without removing the dead connection
              (unbounded accumulation of dead sockets); the endpoint only
              handled WebSocketDisconnect. Both fixed: broadcast prunes dead
              connections after a failed send, and the endpoint uses
              try/except/finally so every exception path cleans up.

  FIXED #11 — Dead code removed: _broadcast_logs_loop() did nothing and
              logs_cache was never populated — there is no logging sink
              anywhere in config.py that could feed it, and adding one is
              out of scope for this file. Removed rather than left as a
              non-functional stub (documented as a Required External Change
              below).

  FIXED #12 — Blocking file I/O inside async handlers (keywords.json /
              accounts.env read-modify-write) now runs via
              loop.run_in_executor(...), matching the same pattern
              database.py v9.0 already uses for its own file I/O.

  FIXED #13 — New: GET /api/dead-letters exposes the now-functional DLQ
              (monitors.py v9.7 / database.py v9.0) for operational
              visibility. retry/resolve-by-id endpoints are intentionally
              NOT implemented: DeadLetterRecord as returned by
              database.py's get_dead_letters() does not expose the row id,
              so there is no safe way to reference a specific row from this
              file alone (documented as a Required External Change below).

All existing working features (Telegram account login/OTP flow, Render
env-var upsert for SESSION_STRING, blocked senders/chats, messages/alerts
browsing, account creation) are preserved unchanged in behavior.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
)

from config import CFG, ACCOUNTS
from database import EnhancedDatabase

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    logger.warning("psutil not installed. Process memory/CPU stats in dashboard will be limited.")

KEYWORDS_FILE = "keywords.json"

# =============================================================================
# Pydantic Models
# =============================================================================

class AccountCreate(BaseModel):
    name: str
    api_id: int
    api_hash: str
    phone: str
    session_name: str
    priority: int = 5


class AccountUpdate(BaseModel):
    enabled: Optional[bool] = None
    priority: Optional[int] = None
    name: Optional[str] = None


class KeywordCreate(BaseModel):
    # `category` accepts either a bare top-level key ("ad_blockers") or a
    # dotted path into a nested section ("high_confidence_boost_patterns.patterns",
    # "request_phrases.direct_requests", "spam_categories.financial_spam", ...).
    # The resolved node MUST be a JSON list; anything else is rejected with a
    # clear 400 instead of crashing.
    category: str
    keyword: str


class KeywordDelete(BaseModel):
    category: str
    keyword: str


class BlockUser(BaseModel):
    user_id: int
    reason: str = ""


class BlockChat(BaseModel):
    chat_id: int
    reason: str = ""


class SettingsUpdate(BaseModel):
    max_alerts_per_minute: Optional[int] = None
    max_alerts_per_hour: Optional[int] = None
    alert_cooldown: Optional[int] = None
    prefilter_enabled: Optional[bool] = None
    processing_workers: Optional[int] = None
    language_filter: Optional[bool] = None


class LoginSendCode(BaseModel):
    prefix: str


class LoginVerifyCode(BaseModel):
    prefix: str
    code: str


class LoginVerifyPassword(BaseModel):
    prefix: str
    password: str


# =============================================================================
# Security
# =============================================================================

security = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """
    Timing-safe token comparison (fix #8), sourced from CFG.DASHBOARD_AUTH_TOKEN
    — the single source of truth already established by config.py — instead
    of a second, independent os.getenv() read that could theoretically drift.
    """
    token = credentials.credentials
    expected_token = CFG.DASHBOARD_AUTH_TOKEN
    if not hmac.compare_digest(token, expected_token):
        raise HTTPException(status_code=401, detail="Invalid token")
    return token


# =============================================================================
# CORS configuration (fix #9)
# =============================================================================
_cors_env = os.getenv("DASHBOARD_CORS_ORIGINS", "").strip()
if _cors_env:
    _CORS_ALLOW_ORIGINS = [o.strip() for o in _cors_env.split(",") if o.strip()]
    _CORS_ALLOW_CREDENTIALS = True
    logger.info(f"Dashboard CORS enabled for origins: {_CORS_ALLOW_ORIGINS}")
else:
    _CORS_ALLOW_ORIGINS: List[str] = []
    _CORS_ALLOW_CREDENTIALS = False
    logger.warning(
        "DASHBOARD_CORS_ORIGINS not set — cross-origin browser access to the "
        "dashboard API is disabled by default (previously this was "
        "allow_origins=['*'] combined with allow_credentials=True, an unsafe "
        "combination). Set DASHBOARD_CORS_ORIGINS to a comma-separated list "
        "of allowed origins if a separate frontend needs cross-origin access."
    )


# =============================================================================
# FastAPI App
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    bot = getattr(app.state, "bot_ref", None)
    if bot is not None and getattr(bot, "db", None) is not None and bot.db.is_connected:
        # FIX #5: reuse the bot's existing EnhancedDatabase connection instead
        # of opening a second one. A second EnhancedDatabase() instance would
        # run its own _writer_loop/_backup_loop against the same SQLite file
        # — the same class of duplicate-background-ownership problem
        # database.py v9.0 already fixed for cleanup (single-owner
        # principle), just reintroduced here via a second DB object.
        app.state.db = bot.db
        app.state.owns_db = False
        logger.info("Dashboard reusing the bot's existing database connection")
    else:
        app.state.db = EnhancedDatabase()
        await app.state.db.connect()
        app.state.owns_db = True
        logger.info("Dashboard opened its own standalone database connection (no bot_ref available)")

    if getattr(app.state, "bot_ref", None) is None:
        app.state.bot_ref = None

    app.state.stats_cache = {}
    app.state.stats_update_task = asyncio.create_task(_update_stats_loop(app), name="dashboard_stats_loop")

    def _stats_task_done(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception():
            logger.error(f"Dashboard stats loop crashed: {t.exception()}")

    app.state.stats_update_task.add_done_callback(_stats_task_done)

    logger.info("Dashboard v3.0 (hardened) started successfully")
    yield

    # Shutdown
    task = getattr(app.state, "stats_update_task", None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    if getattr(app.state, "owns_db", False):
        await app.state.db.close()

    for t in list(_background_dashboard_tasks):
        if not t.done():
            t.cancel()
    if _background_dashboard_tasks:
        await asyncio.gather(*_background_dashboard_tasks, return_exceptions=True)

    logger.info("Dashboard shutdown complete")


app = FastAPI(
    title="Telegram Bot Dashboard",
    description="لوحة تحكم متقدمة لبوت تيليجرام مع IntentEngine (hardened)",
    version="3.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ALLOW_ORIGINS,
    allow_credentials=_CORS_ALLOW_CREDENTIALS,
    allow_methods=["GET", "POST", "DELETE", "PUT", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Local, tracked background tasks not tied to bot lifecycle (e.g. the
# restart trigger). Kept separate from app.state so lifespan shutdown can
# always find and cancel them even across module reloads in dev.
_background_dashboard_tasks: Set[asyncio.Task] = set()


def _track_local_task(coro: Any, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _background_dashboard_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_dashboard_tasks.discard(t)
        if not t.cancelled() and t.exception():
            logger.error(f"Dashboard background task '{name}' failed: {t.exception()}")

    task.add_done_callback(_done)
    return task


# =============================================================================
# WebSocket Manager (fix #10)
# =============================================================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        async with self._lock:
            connections = list(self.active_connections)
        dead: List[WebSocket] = []
        for conn in connections:
            try:
                await conn.send_json(message)
            except Exception as e:
                logger.debug(f"WebSocket send failed, marking connection dead: {e}")
                dead.append(conn)
        if dead:
            async with self._lock:
                for conn in dead:
                    if conn in self.active_connections:
                        self.active_connections.remove(conn)


manager = ConnectionManager()


# =============================================================================
# Background Tasks
# =============================================================================

async def _update_stats_loop(app: FastAPI):
    """Push live stats over WebSocket every 2s (fix #4: corrected field names)."""
    while True:
        try:
            await asyncio.sleep(2)
            db = app.state.db
            bot = getattr(app.state, "bot_ref", None)
            if not db.is_connected:
                continue

            db_stats = await db.get_stats()
            filter_tele = await bot.filter.get_telemetry() if bot else {}
            queue_size = await db.queue_size()

            connected = 0
            accounts_stats = []
            if bot:
                for m in bot.monitors:
                    s = await m.get_stats()
                    if s.get("connected"):
                        connected += 1
                    dlq = s.get("dlq_stats", {}) or {}
                    accounts_stats.append({
                        "name": s.get("name"),
                        "phone": s.get("phone"),
                        "connected": s.get("connected"),
                        "priority": s.get("priority"),
                        # FIX #4: monitors.py returns "alerts_sent", never
                        # "alerts"; there is no per-account "queued" — the
                        # processing queue is global (db.queue_size()).
                        "alerts_sent": s.get("alerts_sent", 0),
                        "messages_processed": s.get("messages_processed", 0),
                        "errors": s.get("errors", 0),
                        "duplicates": s.get("duplicates", 0),
                        "rate_limited": s.get("rate_limited", 0),
                        "dead_lettered": dlq.get("dead_lettered", 0),
                        "last_error": s.get("last_error"),
                        "accepted": s.get("accepted", 0),
                        "reviewed": s.get("reviewed", 0),
                        "ignored": s.get("ignored", 0),
                        "avg_confidence": s.get("avg_confidence", 0.0),
                    })

            mem_stats: Dict[str, Any] = {}
            if bot and getattr(bot, "memory_monitor", None) is not None:
                try:
                    mem_stats = bot.memory_monitor.check()
                except Exception as e:
                    logger.debug(f"memory_monitor.check() failed: {e}")

            proc_mem_mb = 0
            proc_cpu_percent = 0.0
            sys_mem_total_mb = 0
            if PSUTIL_AVAILABLE:
                try:
                    process = psutil.Process()
                    proc_mem_mb = process.memory_info().rss // (1024 * 1024)
                    proc_cpu_percent = process.cpu_percent()
                    sys_mem_total_mb = psutil.virtual_memory().total // (1024 * 1024)
                except Exception as e:
                    logger.debug(f"psutil stats collection failed: {e}")

            rl_status = bot.rate_limiter.status() if bot else {}
            uptime = (
                int(time.monotonic() - bot._start_time)
                if bot
                else int(time.time() - db.start_time) if hasattr(db, "start_time") else 0
            )

            stats = {
                "total_messages": db_stats.get("total_messages", 0),
                "alerts_sent": db_stats.get("alerts_sent", 0),
                "queue_size": queue_size,
                "queue_evictions": db_stats.get("queue_evictions", 0),
                "db_healthy": db_stats.get("db_healthy", True),
                "connected_accounts": connected,
                "total_accounts": len(ACCOUNTS),
                "uptime": uptime,
                "memory_used_mb": proc_mem_mb,
                "memory_total_mb": sys_mem_total_mb,
                "cpu_percent": proc_cpu_percent,
                "leak_suspected": mem_stats.get("leak_suspected", False),
                "rate_limiter": rl_status,
                "filter_stats": filter_tele,
                "accounts": accounts_stats,
                "unique_senders": db_stats.get("unique_senders", 0),
                "alerts_last_hour": db_stats.get("alerts_last_hour", 0),
                "messages_last_hour": db_stats.get("messages_last_hour", 0),
                "blocked_senders": db_stats.get("blocked_senders", 0),
                "avg_reputation": db_stats.get("avg_reputation", 0),
                "filter_accepted": filter_tele.get("accepted", 0),
                "filter_review": filter_tele.get("review", 0),
                "filter_ignored": filter_tele.get("ignored", 0),
                "filter_processed": filter_tele.get("processed", 0),
                "filter_valid": filter_tele.get("valid", 0),
                "filter_template_patterns_generated": filter_tele.get("template_patterns_generated", 0),
                "filter_keyword_reloads": filter_tele.get("keyword_reloads", 0),
                # FIX #4: "filter_avg_confidence" removed — filter_engine.py's
                # get_telemetry() never populates that key, so it was always
                # a fake 0.0. Real, DB-tracked confidence is below.
                "db_accepted": db_stats.get("decision_accept", 0),
                "db_reviewed": db_stats.get("decision_review", 0),
                "db_ignored": db_stats.get("decision_ignore", 0),
                "db_avg_confidence": db_stats.get("avg_confidence", 0.0),
            }
            app.state.stats_cache = stats
            await manager.broadcast({"type": "stats", "data": stats})

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Stats update loop error: {e}")
            await asyncio.sleep(5)


# =============================================================================
# Keyword file helpers (fixes #1/#2/#3/#12)
# =============================================================================

_keywords_file_lock = asyncio.Lock()


async def _read_keywords_file(path: str = KEYWORDS_FILE) -> Dict[str, Any]:
    loop = asyncio.get_event_loop()

    def _read() -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    return await loop.run_in_executor(None, _read)


async def _write_keywords_file(data: Any, path: str = KEYWORDS_FILE) -> None:
    """Atomic write (temp file + os.replace) so a crash mid-write can never
    corrupt keywords.json, offloaded to an executor so it never blocks the
    event loop (fix #12)."""
    loop = asyncio.get_event_loop()

    def _write() -> None:
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    await loop.run_in_executor(None, _write)


def _resolve_list(data: Dict[str, Any], dotted_path: str) -> Tuple[Dict[str, Any], str, list]:
    """
    Resolve a dotted path (e.g. "ad_blockers" or
    "high_confidence_boost_patterns.patterns") inside `data`, returning
    (parent_dict, last_key, target_list). Raises ValueError with a clear,
    user-facing message if the path doesn't exist or doesn't resolve to a
    JSON list — this is what prevents the AttributeError/500 the old code
    would hit on any dict-shaped category (fix #2).
    """
    parts = [p for p in dotted_path.strip().split(".") if p]
    if not parts:
        raise ValueError("category path must not be empty")
    node: Any = data
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            raise ValueError(f"path segment '{part}' not found in keywords.json")
        node = node[part]
    last = parts[-1]
    if not isinstance(node, dict) or last not in node:
        raise ValueError(f"path segment '{last}' not found in keywords.json")
    target = node[last]
    if not isinstance(target, list):
        raise ValueError(
            f"'{dotted_path}' resolves to a {type(target).__name__}, not a list — "
            "only list-valued sections can be edited via this endpoint "
            "(e.g. 'ad_blockers', 'high_confidence_boost_patterns.patterns', "
            "'request_phrases.direct_requests', 'templates.need')"
        )
    return node, last, target


async def _reload_filter_keywords(app: FastAPI) -> Dict[str, Any]:
    """
    Calls the REAL reload API on the running filter engine
    (filter_engine.py v14.1's EnhancedFilter.reload_keywords(); the
    backward-compat alias _build_keyword_sets also points at it). Never
    raises — failures are reported back so the caller can decide whether to
    roll back the file (fix #1/#3).
    """
    bot = getattr(app.state, "bot_ref", None)
    if not bot or not getattr(bot, "filter", None):
        return {"applied": False, "error": "bot not yet initialized — file updated but runtime not reloaded"}
    try:
        bot.filter.reload_keywords(KEYWORDS_FILE)
        return {"applied": True, "error": None}
    except Exception as e:
        logger.error(f"Filter keyword reload failed: {e}")
        return {"applied": False, "error": str(e)}


# =============================================================================
# API Endpoints
# =============================================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(os.path.dirname(__file__), "templates", "dashboard.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Dashboard not found</h1>")


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """صفحة تسجيل دخول حسابات تيليجرام (إضافة Session Strings)."""
    return HTMLResponse(LOGIN_PAGE_HTML)


@app.get("/health")
async def health(request: Request):
    """Render health check + keep-alive endpoint (no auth — main.py's
    _keep_alive_loop and Render's own health checks hit this unauthenticated)."""
    db = getattr(request.app.state, "db", None)
    db_ok = bool(db and getattr(db, "is_connected", False))
    db_healthy = bool(db and getattr(db, "db_healthy", True))
    bot = getattr(request.app.state, "bot_ref", None)
    monitors_up = 0
    monitors_total = 0
    main_client_ok = False
    if bot:
        monitors_total = len(bot.monitors)
        monitors_up = sum(1 for m in bot.monitors if m.is_connected)
        try:
            attr = getattr(bot.main_client, "is_connected", None) if bot.main_client else None
            main_client_ok = bool(attr() if callable(attr) else attr) if attr is not None else False
        except Exception:
            main_client_ok = False
    uptime = (
        int(time.monotonic() - bot._start_time)
        if bot
        else (int(time.time() - db.start_time) if db_ok and hasattr(db, "start_time") else 0)
    )
    return JSONResponse({
        "status": "ok" if (db_ok and db_healthy) else "degraded",
        "database": "ok" if db_ok else "down",
        "db_healthy": db_healthy,
        "main_client": "up" if main_client_ok else "down",
        "monitors_up": monitors_up,
        "monitors_total": monitors_total,
        "accounts_with_session": sum(1 for a in ACCOUNTS if a.get("session_string")),
        "accounts_total": len(ACCOUNTS),
        "uptime": uptime,
        "time": datetime.utcnow().isoformat() + "Z",
    })


@app.get("/api/stats", dependencies=[Depends(verify_token)])
async def get_stats(request: Request):
    """إحصائيات كاملة مع بيانات IntentEngine."""
    return JSONResponse(request.app.state.stats_cache)


@app.get("/api/accounts", dependencies=[Depends(verify_token)])
async def get_accounts(request: Request):
    """قائمة الحسابات مع إحصائيات صحيحة الأسماء (fix #4)."""
    bot = request.app.state.bot_ref
    if not bot:
        return JSONResponse({"accounts": []})
    accounts = []
    for m in bot.monitors:
        s = await m.get_stats()
        dlq = s.get("dlq_stats", {}) or {}
        accounts.append({
            "name": s.get("name"),
            "phone": s.get("phone"),
            "connected": s.get("connected"),
            "priority": s.get("priority"),
            "alerts_sent": s.get("alerts_sent", 0),
            "send_cb_state": s.get("send_cb_state"),
            "entity_cb_state": s.get("entity_cb_state"),
            "connect_attempts": s.get("connect_attempts"),
            "last_error": s.get("last_error"),
            "accepted": s.get("accepted", 0),
            "reviewed": s.get("reviewed", 0),
            "ignored": s.get("ignored", 0),
            "avg_confidence": s.get("avg_confidence", 0.0),
            "messages_processed": s.get("messages_processed", 0),
            "avg_processing_time_ms": s.get("avg_processing_time_ms", 0),
            "duplicates": s.get("duplicates", 0),
            "errors": s.get("errors", 0),
            "dead_lettered": dlq.get("dead_lettered", 0),
        })
    return JSONResponse({"accounts": accounts})


@app.post("/api/accounts", dependencies=[Depends(verify_token)])
async def add_account(data: AccountCreate, request: Request):
    """إضافة حساب جديد (يكتب في accounts.env؛ يتطلب إعادة تشغيل للتفعيل)."""
    for acc in ACCOUNTS:
        if acc["phone"] == data.phone:
            raise HTTPException(status_code=400, detail="Account already exists")

    env_path = "accounts.env"
    prefix = f"ACCOUNT_{len(ACCOUNTS) + 1}"
    new_account_lines = [
        f"\n# === {data.name} ===\n",
        f"{prefix}_API_ID={data.api_id}\n",
        f"{prefix}_API_HASH={data.api_hash}\n",
        f"{prefix}_PHONE={data.phone}\n",
        f"{prefix}_SESSION_NAME={data.session_name}\n",
        f"{prefix}_PRIORITY={data.priority}\n",
    ]

    def _write() -> None:
        with open(env_path, "a", encoding="utf-8") as f:
            f.writelines(new_account_lines)

    await asyncio.get_event_loop().run_in_executor(None, _write)
    return JSONResponse({"success": True, "message": "Account added. Restart the service to apply."})


@app.get("/api/messages", dependencies=[Depends(verify_token)])
async def get_messages(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    keyword: Optional[str] = None,
):
    """جلب الرسائل مع تصفية اختيارية."""
    db = request.app.state.db
    try:
        rows = await db.get_messages_with_filters(limit=limit, offset=offset, keyword=keyword)
        return JSONResponse({"messages": rows, "total": len(rows)})
    except Exception as e:
        logger.error(f"Error fetching messages: {e}")
        return JSONResponse({"messages": [], "total": 0})


@app.get("/api/alerts", dependencies=[Depends(verify_token)])
async def get_alerts(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    account: Optional[str] = None,
    keyword: Optional[str] = None,
    decision: Optional[str] = None,
    min_confidence: Optional[float] = None,
):
    """جلب التنبيهات مع تصفية متقدمة (بما فيها IntentEngine)."""
    db = request.app.state.db
    try:
        rows = await db.get_alerts_with_filters(
            limit=limit, offset=offset, keyword=keyword, account=account,
            decision=decision, min_confidence=min_confidence,
        )
        return JSONResponse({
            "alerts": rows,
            "total": len(rows),
            "filters": {"decision": decision, "min_confidence": min_confidence},
        })
    except Exception as e:
        logger.error(f"Error fetching alerts: {e}")
        return JSONResponse({"alerts": [], "total": 0})


@app.get("/api/alerts/stats", dependencies=[Depends(verify_token)])
async def get_alerts_stats(request: Request):
    """إحصائيات التنبيهات (db_healthy / queue_evictions مضمّنة الآن)."""
    db = request.app.state.db
    try:
        summary = await db.get_dashboard_summary()
        return JSONResponse(summary)
    except Exception as e:
        logger.error(f"Error fetching alerts stats: {e}")
        return JSONResponse({})


@app.get("/api/dead-letters", dependencies=[Depends(verify_token)])
async def get_dead_letters_endpoint(request: Request, limit: int = 100, only_unresolved: bool = True):
    """
    قراءة فقط. NOTE: database.py's get_dead_letters() / DeadLetterRecord does
    not expose the underlying row id, so a retry/resolve-by-id endpoint
    cannot be implemented safely from dashboard.py alone — see the
    Compatibility Report's Remaining Issues.
    """
    db = request.app.state.db
    try:
        records = await db.get_dead_letters(limit=limit, only_unresolved=only_unresolved)
        return JSONResponse({
            "dead_letters": [
                {
                    "error_text": r.error_text,
                    "retry_count": r.retry_count,
                    "resolved": r.resolved,
                    "timestamp": r.timestamp,
                    "event_summary": {
                        "chat_id": r.event_data.get("chat_id"),
                        "message_id": r.event_data.get("message_id"),
                        "account_name": r.event_data.get("account_name"),
                    },
                }
                for r in records
            ],
            "total": len(records),
        })
    except Exception as e:
        logger.error(f"get_dead_letters failed: {e}")
        return JSONResponse({"dead_letters": [], "total": 0})


@app.get("/api/keywords", dependencies=[Depends(verify_token)])
async def get_keywords(request: Request):
    """
    يعرض حالة الكلمات المفتاحية الفعلية التي يستخدمها الفلتر الآن
    (bot.filter._raw_keywords) بدل الثابت المجمّد KEYWORDS من config.py
    (الذي لا يتغير أبدًا بعد بدء التشغيل)، مع fallback لقراءة الملف مباشرة.
    """
    bot = getattr(request.app.state, "bot_ref", None)
    if bot and getattr(bot, "filter", None) is not None:
        raw = getattr(bot.filter, "_raw_keywords", None)
        if raw is not None:
            return JSONResponse({"keywords": raw, "source": "runtime"})
    try:
        data = await _read_keywords_file()
        return JSONResponse({"keywords": data, "source": "disk"})
    except Exception as e:
        logger.error(f"get_keywords: failed reading {KEYWORDS_FILE}: {e}")
        return JSONResponse({"keywords": {}, "source": "error", "error": str(e)})


@app.post("/api/keywords", dependencies=[Depends(verify_token)])
async def add_keyword(data: KeywordCreate, request: Request):
    """
    إضافة كلمة/عبارة إلى قسم (list) داخل keywords.json عبر مسار منقّط، مع
    كتابة ذرية + إعادة تحميل حقيقية للفلتر + rollback عند الفشل (fixes #1-3).
    """
    keyword = data.keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="keyword must not be empty")
    path = data.category.strip()

    async with _keywords_file_lock:
        try:
            all_data = await _read_keywords_file()
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"{KEYWORDS_FILE} not found")
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=500, detail=f"{KEYWORDS_FILE} is not valid JSON: {e}")

        try:
            _, _, target_list = _resolve_list(all_data, path)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        if keyword in target_list:
            return JSONResponse({"success": False, "error": "Keyword already exists", "category": path})

        original_snapshot = json.loads(json.dumps(all_data, ensure_ascii=False))
        target_list.append(keyword)

        await _write_keywords_file(all_data)
        reload_result = await _reload_filter_keywords(request.app)

        if not reload_result["applied"] and reload_result["error"] is not None:
            # Genuine reload failure (not just "bot not initialized yet") —
            # roll back the file so persisted and runtime state don't diverge.
            await _write_keywords_file(original_snapshot)
            raise HTTPException(
                status_code=500,
                detail=f"keywords.json rolled back — runtime reload failed: {reload_result['error']}",
            )

        return JSONResponse({
            "success": True,
            "keyword": keyword,
            "category": path,
            "runtime_reloaded": reload_result["applied"],
            "note": None if reload_result["applied"] else reload_result["error"],
        })


@app.delete("/api/keywords", dependencies=[Depends(verify_token)])
async def delete_keyword(data: KeywordDelete, request: Request):
    """حذف كلمة/عبارة من قسم (list) داخل keywords.json — نفس ضمانات الإضافة."""
    keyword = data.keyword.strip()
    path = data.category.strip()

    async with _keywords_file_lock:
        try:
            all_data = await _read_keywords_file()
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"{KEYWORDS_FILE} not found")
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=500, detail=f"{KEYWORDS_FILE} is not valid JSON: {e}")

        try:
            _, _, target_list = _resolve_list(all_data, path)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        if keyword not in target_list:
            return JSONResponse({"success": False, "error": "Keyword not found", "category": path})

        original_snapshot = json.loads(json.dumps(all_data, ensure_ascii=False))
        target_list.remove(keyword)

        await _write_keywords_file(all_data)
        reload_result = await _reload_filter_keywords(request.app)

        if not reload_result["applied"] and reload_result["error"] is not None:
            await _write_keywords_file(original_snapshot)
            raise HTTPException(
                status_code=500,
                detail=f"keywords.json rolled back — runtime reload failed: {reload_result['error']}",
            )

        return JSONResponse({
            "success": True,
            "category": path,
            "runtime_reloaded": reload_result["applied"],
            "note": None if reload_result["applied"] else reload_result["error"],
        })


@app.get("/api/blocked/senders", dependencies=[Depends(verify_token)])
async def get_blocked_senders(request: Request):
    # NOTE: database.py has no public list-blocked-senders method; _fetchall
    # is used as it did in the previous version. See Remaining Issues.
    db = request.app.state.db
    rows = await db._fetchall("SELECT * FROM blocked_senders ORDER BY blocked_at DESC LIMIT 100")
    return JSONResponse({"senders": rows})


@app.get("/api/blocked/chats", dependencies=[Depends(verify_token)])
async def get_blocked_chats(request: Request):
    db = request.app.state.db
    rows = await db._fetchall("SELECT * FROM blocked_chats ORDER BY blocked_at DESC LIMIT 100")
    return JSONResponse({"chats": rows})


@app.post("/api/blocked/senders", dependencies=[Depends(verify_token)])
async def block_sender(data: BlockUser, request: Request):
    db = request.app.state.db
    await db.block_sender(data.user_id, data.reason, "dashboard")
    return JSONResponse({"success": True})


@app.delete("/api/blocked/senders/{user_id}", dependencies=[Depends(verify_token)])
async def unblock_sender(user_id: int, request: Request):
    db = request.app.state.db
    await db.unblock_sender(user_id)
    return JSONResponse({"success": True})


@app.post("/api/blocked/chats", dependencies=[Depends(verify_token)])
async def block_chat(data: BlockChat, request: Request):
    db = request.app.state.db
    await db.block_chat(data.chat_id, data.reason, "dashboard")
    return JSONResponse({"success": True})


@app.delete("/api/blocked/chats/{chat_id}", dependencies=[Depends(verify_token)])
async def unblock_chat(chat_id: int, request: Request):
    db = request.app.state.db
    await db.unblock_chat(chat_id)
    return JSONResponse({"success": True})


@app.post("/api/settings", dependencies=[Depends(verify_token)])
async def update_settings(data: SettingsUpdate, request: Request):
    """
    FIX #6: CFG is a frozen dataclass (config.py `@dataclass(frozen=True)`) —
    assigning CFG.PREFILTER_ENABLED / CFG.LANGUAGE_FILTER raises
    FrozenInstanceError on every call. Those are now reported as
    unsupported rather than crashing; only genuinely mutable runtime state
    (AdaptiveRateLimiter's limits) is applied.
    """
    bot = request.app.state.bot_ref
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not yet initialized")

    applied: Dict[str, Any] = {}
    skipped: Dict[str, str] = {}

    if data.max_alerts_per_minute is not None:
        bot.rate_limiter._max_min = data.max_alerts_per_minute
        applied["max_alerts_per_minute"] = data.max_alerts_per_minute
    if data.max_alerts_per_hour is not None:
        bot.rate_limiter._max_hr = data.max_alerts_per_hour
        applied["max_alerts_per_hour"] = data.max_alerts_per_hour
    if data.prefilter_enabled is not None:
        skipped["prefilter_enabled"] = "CFG is an immutable frozen dataclass at runtime — not supported"
    if data.language_filter is not None:
        skipped["language_filter"] = "CFG is an immutable frozen dataclass at runtime — not supported"
    if data.alert_cooldown is not None:
        skipped["alert_cooldown"] = "CFG is an immutable frozen dataclass at runtime — not supported"
    if data.processing_workers is not None:
        skipped["processing_workers"] = "worker pool is created once at startup — requires a process restart"

    return JSONResponse({"success": True, "applied": applied, "skipped": skipped})


@app.post("/api/purge", dependencies=[Depends(verify_token)])
async def purge_queue(request: Request):
    db = request.app.state.db
    count = await db.purge_queue()
    return JSONResponse({"success": True, "purged": count})


@app.post("/api/restart", dependencies=[Depends(verify_token)])
async def restart_bot(request: Request):
    """
    FIX #7: the previous implementation called bot.stop() then
    bot._send_startup_message() — stop() disconnects every account and
    closes the database, so the follow-up message could never actually
    send, and main.py exposes no in-process restart primitive at all
    (workers/background tasks are created exactly once in initialize()).
    This now triggers an honest graceful shutdown and tells the caller the
    hosting platform (Render) is expected to restart the process —
    consistent with what main.py's run()/stop() actually make possible.
    """
    bot = request.app.state.bot_ref
    if not bot:
        raise HTTPException(status_code=503, detail="Bot not yet initialized")

    logger.warning("Restart requested via dashboard — initiating graceful shutdown")
    _track_local_task(_trigger_restart(bot), "dashboard_restart_trigger")

    return JSONResponse({
        "success": True,
        "message": (
            "Graceful shutdown initiated. main.py has no in-process restart "
            "primitive, so this triggers a full graceful stop; the hosting "
            "platform's process supervisor is expected to restart the service."
        ),
    })


async def _trigger_restart(bot) -> None:
    try:
        await bot.stop()
    except Exception as e:
        logger.error(f"Restart: bot.stop() failed: {e}")


# =============================================================================
# Telegram Account Login System (Session String generator for Render)
# — unchanged: self-contained, uses Telethon directly, not affected by the
#   monitors.py/config.py/database.py/main.py architecture changes.
# =============================================================================

class LoginManager:
    """Manages pending Telethon logins (OTP flow) in memory."""

    def __init__(self) -> None:
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def _purge_old(self) -> None:
        now = time.time()
        for prefix in list(self._pending.keys()):
            entry = self._pending.get(prefix)
            if entry and now - entry.get("ts", 0) > 600:
                client = entry.get("client")
                if client:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                self._pending.pop(prefix, None)

    async def start(self, prefix: str, api_id: int, api_hash: str, phone: str) -> Dict[str, Any]:
        async with self._lock:
            await self._purge_old()
            old = self._pending.pop(prefix, None)
            if old and old.get("client"):
                try:
                    await old["client"].disconnect()
                except Exception:
                    pass
            client = TelegramClient(
                StringSession(), api_id=api_id, api_hash=api_hash,
                device_model="Render Cloud", system_version="Linux", app_version="13.1",
                timeout=30, connection_retries=3,
            )
            await client.connect()
            sent = await client.send_code_request(phone)
            self._pending[prefix] = {
                "client": client,
                "phone": phone,
                "phone_code_hash": sent.phone_code_hash,
                "ts": time.time(),
            }
            return {"sent": True, "code_type": str(sent.type)}

    async def _get(self, prefix: str) -> Dict[str, Any]:
        entry = self._pending.get(prefix)
        if not entry:
            raise HTTPException(status_code=400, detail="لا توجد عملية تسجيل دخول نشطة لهذا الحساب - أرسل الكود أولاً")
        return entry

    async def verify_code(self, prefix: str, code: str) -> Dict[str, Any]:
        async with self._lock:
            entry = await self._get(prefix)
            client: TelegramClient = entry["client"]
            try:
                await client.sign_in(entry["phone"], code.strip().replace(" ", ""), phone_code_hash=entry["phone_code_hash"])
            except SessionPasswordNeededError:
                return {"need_password": True}
            return await self._finalize(prefix, client)

    async def verify_password(self, prefix: str, password: str) -> Dict[str, Any]:
        async with self._lock:
            entry = await self._get(prefix)
            client: TelegramClient = entry["client"]
            await client.sign_in(password=password)
            return await self._finalize(prefix, client)

    async def _finalize(self, prefix: str, client: TelegramClient) -> Dict[str, Any]:
        me = await client.get_me()
        session_string = client.session.save()
        try:
            await client.disconnect()
        except Exception:
            pass
        self._pending.pop(prefix, None)
        return {
            "done": True,
            "user": f"@{me.username}" if me.username else (me.first_name or str(me.id)),
            "user_id": me.id,
            "session_string": session_string,
        }


login_manager = LoginManager()

RENDER_API_BASE = "https://api.render.com/v1"


async def render_upsert_env(key: str, value: str) -> Dict[str, Any]:
    """Upsert a single, specific env var on the Render service via Render API
    (triggers redeploy). Scope is already minimal — only ever called to set
    one *_SESSION_STRING key, never broader account/service settings."""
    api_key = (os.getenv("RENDER_API_KEY") or "").strip()
    service_id = (os.getenv("RENDER_SERVICE_ID") or "").strip()
    if not api_key or not service_id:
        return {"saved": False, "reason": "RENDER_API_KEY / RENDER_SERVICE_ID غير مضبوطة"}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.put(f"{RENDER_API_BASE}/services/{service_id}/env-vars/{key}", json={"value": value}) as r:
                if r.status in (200, 201):
                    return {"saved": True}
            async with s.put(f"{RENDER_API_BASE}/services/{service_id}/env-vars", json=[{"key": key, "value": value}]) as r2:
                if r2.status in (200, 201):
                    return {"saved": True}
                body = (await r2.text())[:200]
                return {"saved": False, "reason": f"Render API HTTP {r2.status}: {body}"}
    except Exception as e:
        return {"saved": False, "reason": f"{type(e).__name__}: {e}"}


def _mask_phone(phone: str) -> str:
    if not phone:
        return ""
    return "*" * max(0, len(phone) - 4) + phone[-4:]


@app.get("/api/login/accounts", dependencies=[Depends(verify_token)])
async def login_accounts(request: Request):
    """قائمة الحسابات المهيأة وحالة جلساتها."""
    bot = getattr(request.app.state, "bot_ref", None)
    monitors = {m.account.get("prefix"): m for m in bot.monitors} if bot else {}
    out = []
    for acc in ACCOUNTS:
        prefix = acc.get("prefix", "")
        mon = monitors.get(prefix)
        out.append({
            "prefix": prefix,
            "name": acc.get("name"),
            "phone_masked": _mask_phone(acc.get("phone", "")),
            "has_session_string": bool(acc.get("session_string")),
            "connected": bool(mon and mon.is_connected),
            "last_error": (mon._last_connect_error if mon else None),
        })
    return JSONResponse({"accounts": out})


@app.post("/api/login/send-code", dependencies=[Depends(verify_token)])
async def login_send_code(data: LoginSendCode):
    """إرسال رمز التحقق OTP إلى هاتف الحساب."""
    prefix = data.prefix.strip().upper()
    acc = next((a for a in ACCOUNTS if a.get("prefix") == prefix), None)
    if not acc:
        raise HTTPException(status_code=404, detail=f"الحساب {prefix} غير موجود في الإعدادات")
    try:
        result = await login_manager.start(prefix, acc["api_id"], acc["api_hash"], acc["phone"])
        return JSONResponse({"success": True, "phone_masked": _mask_phone(acc["phone"]), **result})
    except ApiIdInvalidError:
        raise HTTPException(status_code=400, detail="API_ID / API_HASH غير صالحة")
    except PhoneNumberInvalidError:
        raise HTTPException(status_code=400, detail="رقم الهاتف غير صالح")
    except FloodWaitError as e:
        raise HTTPException(status_code=429, detail=f"حظر مؤقت من تيليجرام - انتظر {e.seconds} ثانية")
    except Exception as e:
        logger.error(f"send-code error [{prefix}]: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)[:200]}")


@app.post("/api/login/verify-code", dependencies=[Depends(verify_token)])
async def login_verify_code(data: LoginVerifyCode):
    """التحقق من رمز OTP."""
    prefix = data.prefix.strip().upper()
    try:
        result = await login_manager.verify_code(prefix, data.code)
    except PhoneCodeInvalidError:
        raise HTTPException(status_code=400, detail="رمز التحقق غير صحيح")
    except PhoneCodeExpiredError:
        raise HTTPException(status_code=400, detail="رمز التحقق منتهي - أرسل كوداً جديداً")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"verify-code error [{prefix}]: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)[:200]}")

    if result.get("need_password"):
        return JSONResponse({"success": True, "need_password": True})
    return await _login_success_response(prefix, result)


@app.post("/api/login/verify-password", dependencies=[Depends(verify_token)])
async def login_verify_password(data: LoginVerifyPassword):
    """التحقق من كلمة مرور التحقق بخطوتين (2FA)."""
    prefix = data.prefix.strip().upper()
    try:
        result = await login_manager.verify_password(prefix, data.password)
    except PasswordHashInvalidError:
        raise HTTPException(status_code=400, detail="كلمة المرور غير صحيحة")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"verify-password error [{prefix}]: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)[:200]}")
    return await _login_success_response(prefix, result)


async def _login_success_response(prefix: str, result: Dict[str, Any]) -> JSONResponse:
    """حفظ الجلسة في متغيرات Render وإرجاع النتيجة."""
    env_key = f"{prefix}_SESSION_STRING"
    save = await render_upsert_env(env_key, result["session_string"])
    logger.info(f"Login completed for {prefix} ({result.get('user')}): env {env_key} saved={save.get('saved')}")
    return JSONResponse({
        "success": True,
        "done": True,
        "user": result.get("user"),
        "user_id": result.get("user_id"),
        "env_key": env_key,
        "saved_to_render": save.get("saved", False),
        "save_reason": save.get("reason", ""),
        "note": "تم حفظ الجلسة - ستعيد Render نشر الخدمة تلقائياً وسيتصل الحساب خلال دقائق" if save.get("saved")
                else "تعذر الحفظ التلقائي - انسخ Session String وأضفه يدوياً في متغيرات Render",
        "session_string": result["session_string"] if not save.get("saved") else None,
    })


LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>تسجيل دخول الحسابات - Telegram Bot</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; font-family:'Segoe UI',Tahoma,sans-serif; }
body { background:#0f172a; color:#e2e8f0; min-height:100vh; padding:20px; }
.container { max-width:760px; margin:0 auto; }
h1 { text-align:center; color:#38bdf8; margin:18px 0 4px; font-size:24px; }
.sub { text-align:center; color:#94a3b8; margin-bottom:20px; font-size:14px; }
.card { background:#1e293b; border:1px solid #334155; border-radius:12px; padding:20px; margin-bottom:16px; }
.card h2 { font-size:17px; color:#7dd3fc; margin-bottom:14px; }
label { display:block; font-size:13px; color:#94a3b8; margin:10px 0 4px; }
input, select { width:100%; padding:11px 12px; border-radius:8px; border:1px solid #475569; background:#0f172a; color:#e2e8f0; font-size:15px; }
input:focus, select:focus { outline:none; border-color:#38bdf8; }
button { width:100%; padding:12px; border:none; border-radius:8px; background:#0284c7; color:#fff; font-size:16px; font-weight:bold; cursor:pointer; margin-top:14px; transition:.2s; }
button:hover { background:#0369a1; }
button:disabled { background:#475569; cursor:not-allowed; }
.hidden { display:none; }
.msg { padding:12px; border-radius:8px; margin-top:14px; font-size:14px; line-height:1.7; display:none; }
.msg.ok { display:block; background:#052e16; border:1px solid #16a34a; color:#86efac; }
.msg.err { display:block; background:#450a0a; border:1px solid #dc2626; color:#fca5a5; }
.msg.info { display:block; background:#0c4a6e; border:1px solid #0284c7; color:#7dd3fc; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { padding:9px 8px; text-align:right; border-bottom:1px solid #334155; }
th { color:#7dd3fc; font-weight:600; }
.badge { padding:3px 9px; border-radius:20px; font-size:12px; }
.b-green { background:#052e16; color:#4ade80; }
.b-red { background:#450a0a; color:#f87171; }
.b-yellow { background:#422006; color:#fbbf24; }
.mono { direction:ltr; text-align:left; font-family:monospace; word-break:break-all; }
.steps { display:flex; gap:8px; margin-bottom:16px; }
.step { flex:1; text-align:center; padding:8px; border-radius:8px; background:#0f172a; font-size:13px; color:#64748b; border:1px solid #334155; }
.step.active { color:#38bdf8; border-color:#38bdf8; }
.step.done { color:#4ade80; border-color:#16a34a; }
a { color:#38bdf8; }
</style>
</head>
<body>
<div class="container">
<h1>🔐 تسجيل دخول حسابات تيليجرام</h1>
<p class="sub">أضف Session String لكل حساب - يُحفظ تلقائياً في Render ويعاد نشر البوت</p>

<div class="card">
<h2>1️⃣ مفتاح لوحة التحكم</h2>
<label>DASHBOARD_AUTH_TOKEN</label>
<input type="password" id="token" placeholder="أدخل رمز لوحة التحكم">
</div>

<div class="card">
<h2>📱 حالة الحسابات</h2>
<table id="acctTable"><thead><tr><th>الحساب</th><th>الهاتف</th><th>الجلسة</th><th>الاتصال</th></tr></thead><tbody></tbody></table>
</div>

<div class="card">
<h2>2️⃣ تسجيل حساب جديد</h2>
<div class="steps">
<div class="step" id="st1">إرسال الكود</div>
<div class="step" id="st2">رمز التحقق</div>
<div class="step" id="st3">كلمة المرور</div>
<div class="step" id="st4">تم ✅</div>
</div>

<div id="stepSend">
<label>اختر الحساب</label>
<select id="prefix"></select>
<button id="btnSend" onclick="sendCode()">📨 إرسال رمز التحقق</button>
</div>

<div id="stepCode" class="hidden">
<label>رمز التحقق (من رسائل تيليجرام)</label>
<input type="text" id="code" class="mono" placeholder="12345" inputmode="numeric">
<button id="btnVerify" onclick="verifyCode()">✔️ تحقق من الرمز</button>
</div>

<div id="stepPass" class="hidden">
<label>كلمة مرور التحقق بخطوتين (2FA)</label>
<input type="password" id="password" placeholder="كلمة المرور السحابية">
<button id="btnPass" onclick="verifyPassword()">🔑 تحقق من كلمة المرور</button>
</div>

<div class="msg" id="msg"></div>
</div>

<div class="card" style="text-align:center; font-size:13px; color:#64748b;">
بعد كل تسجيل ناجح، ستعيد Render نشر الخدمة تلقائياً (2-4 دقائق) ثم يتصل الحساب.<br>
<a href="/">← العودة للوحة التحكم</a> | <a href="/health">/health</a>
</div>
</div>

<script>
let currentPrefix = null;
const $ = id => document.getElementById(id);
const token = () => $('token').value.trim();
$('token').value = localStorage.getItem('dash_token') || '';
$('token').addEventListener('change', () => { localStorage.setItem('dash_token', token()); loadAccounts(); });

function show(type, text) { const m = $('msg'); m.className = 'msg ' + type; m.innerHTML = text; }
function setStep(n) {
  [1,2,3,4].forEach(i => { const el = $('st'+i); el.className = 'step' + (i < n ? ' done' : i === n ? ' active' : ''); });
  $('stepSend').classList.toggle('hidden', n !== 1);
  $('stepCode').classList.toggle('hidden', n !== 2);
  $('stepPass').classList.toggle('hidden', n !== 3);
}
async function api(path, body) {
  const r = await fetch(path, { method: body ? 'POST' : 'GET',
    headers: { 'Authorization': 'Bearer ' + token(), 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
  return data;
}
async function loadAccounts() {
  if (!token()) return;
  try {
    const d = await api('/api/login/accounts');
    const tb = document.querySelector('#acctTable tbody'); tb.innerHTML = '';
    const sel = $('prefix'); sel.innerHTML = '';
    d.accounts.forEach(a => {
      const tr = document.createElement('tr');
      const sess = a.has_session_string ? '<span class="badge b-green">موجودة</span>' : '<span class="badge b-yellow">مطلوبة</span>';
      const conn = a.connected ? '<span class="badge b-green">متصل</span>' : '<span class="badge b-red">غير متصل</span>';
      tr.innerHTML = `<td>${a.name}</td><td class="mono">${a.phone_masked}</td><td>${sess}</td><td>${conn}</td>`;
      tb.appendChild(tr);
      const op = document.createElement('option');
      op.value = a.prefix; op.textContent = `${a.name} (${a.phone_masked})${a.connected ? ' ✅' : ''}`;
      sel.appendChild(op);
    });
  } catch (e) { show('err', 'تعذر تحميل الحسابات: ' + e.message); }
}
async function sendCode() {
  currentPrefix = $('prefix').value;
  if (!currentPrefix) return;
  $('btnSend').disabled = true;
  show('info', '⏳ جاري إرسال رمز التحقق عبر تيليجرام...');
  try {
    const d = await api('/api/login/send-code', { prefix: currentPrefix });
    show('ok', `📨 تم إرسال الرمز إلى ${d.phone_masked} - افتح تيليجرام وانسخ الرمز ثم أدخله هنا.`);
    setStep(2);
  } catch (e) { show('err', '❌ ' + e.message); }
  $('btnSend').disabled = false;
}
async function verifyCode() {
  $('btnVerify').disabled = true;
  try {
    const d = await api('/api/login/verify-code', { prefix: currentPrefix, code: $('code').value });
    if (d.need_password) { show('info', '🔐 هذا الحساب يستخدم التحقق بخطوتين - أدخل كلمة المرور السحابية.'); setStep(3); }
    else finishLogin(d);
  } catch (e) { show('err', '❌ ' + e.message); }
  $('btnVerify').disabled = false;
}
async function verifyPassword() {
  $('btnPass').disabled = true;
  try {
    const d = await api('/api/login/verify-password', { prefix: currentPrefix, password: $('password').value });
    finishLogin(d);
  } catch (e) { show('err', '❌ ' + e.message); }
  $('btnPass').disabled = false;
}
function finishLogin(d) {
  setStep(4);
  let html = `✅ تم تسجيل الدخول بنجاح: <b>${d.user}</b><br>`;
  if (d.saved_to_render) {
    html += `💾 حُفظت الجلسة في <span class="mono">${d.env_key}</span><br>🔄 ستعيد Render النشر تلقائياً وسيتصل الحساب خلال دقائق.`;
  } else {
    html += `⚠️ ${d.note}<br>السبب: ${d.save_reason || ''}`;
    if (d.session_string) html += `<br><br>Session String:<br><span class="mono">${d.session_string}</span>`;
  }
  show(d.saved_to_render ? 'ok' : 'err', html);
  setTimeout(loadAccounts, 2000);
}
setStep(1);
if (token()) loadAccounts();
</script>
</body>
</html>
"""

# =============================================================================
# WebSocket Endpoint (fix #10: cleanup on ALL exception paths)
# =============================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        if websocket.app.state.stats_cache:
            await websocket.send_json({"type": "stats", "data": websocket.app.state.stats_cache})
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            command = msg.get("command")
            if command == "ping":
                await websocket.send_json({"type": "pong"})
            elif command == "get_alerts_stats":
                db = websocket.app.state.db
                summary = await db.get_dashboard_summary()
                await websocket.send_json({"type": "alerts_stats", "data": summary})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"WebSocket connection error: {e}")
    finally:
        await manager.disconnect(websocket)


# =============================================================================
# Helper Functions
# =============================================================================

def set_bot_reference(bot):
    app.state.bot_ref = bot
    logger.info("Bot reference set in dashboard")


async def start_dashboard(host: str = "0.0.0.0", port: int = 8080):
    import uvicorn
    try:
        import uvloop  # noqa: F401
        loop = "uvloop"
    except ImportError:
        loop = "asyncio"
    config = uvicorn.Config(app, host=host, port=port, log_level="info", loop=loop)
    server = uvicorn.Server(config)
    await server.serve()


# =============================================================================
# WebAdmin dashboard (modular admin panel: auth, keywords, alerts, logs,
# accounts, settings, backups) - mounted on the same app/single port.
# Existing routes (/api/*, /login, /health) remain untouched. Left as a
# genuinely optional plugin hook — not a silent-failure pattern, since
# webadmin.py is an unknown/optional external module.
# =============================================================================
try:
    from webadmin import mount_admin
    mount_admin(app)
except Exception as _webadmin_err:
    logger.warning(f"webadmin dashboard not loaded: {_webadmin_err}")

# =============================================================================
# Main (standalone)
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)