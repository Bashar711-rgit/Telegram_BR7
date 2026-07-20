#!/usr/bin/env python3
"""
dashboard.py – Telegram Bot Control Panel v2.1 (INTENT ENGINE EDITION)
FastAPI + WebSocket Dashboard for EnhancedTelegramBot
ميزات متقدمة: إدارة حسابات، كلمات مفتاحية، حظر، إحصائيات لحظية، سجلات، إعادة تشغيل
مدعوم بالكامل بـ IntentEngine v13.0 (decision, confidence, reasons)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

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

from config import CFG, ACCOUNTS, KEYWORDS, load_keywords
from database import EnhancedDatabase

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

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    expected_token = os.getenv("DASHBOARD_AUTH_TOKEN", "change-me")
    if token != expected_token:
        raise HTTPException(status_code=401, detail="Invalid token")
    return token

# =============================================================================
# FastAPI App
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    app.state.db = EnhancedDatabase()
    await app.state.db.connect()
    # Keep an existing bot reference: main.py calls set_bot_reference() BEFORE
    # uvicorn fires this lifespan startup - resetting to None here would wipe it.
    if getattr(app.state, "bot_ref", None) is None:
        app.state.bot_ref = None
    app.state.stats_cache = {}
    app.state.logs_cache = []
    app.state.stats_update_task = asyncio.create_task(_update_stats_loop(app))
    app.state.logs_broadcast_task = asyncio.create_task(_broadcast_logs_loop(app))
    logger.info("Dashboard v2.1 (IntentEngine Edition) started successfully")
    yield
    # Shutdown
    if app.state.stats_update_task:
        app.state.stats_update_task.cancel()
    if app.state.logs_broadcast_task:
        app.state.logs_broadcast_task.cancel()
    await app.state.db.close()
    logger.info("Dashboard shutdown complete")

app = FastAPI(
    title="Telegram Bot Dashboard",
    description="لوحة تحكم متقدمة لبوت تيليجرام مع IntentEngine",
    version="2.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================================
# WebSocket Manager
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
            for conn in self.active_connections:
                try:
                    await conn.send_json(message)
                except Exception:
                    pass

manager = ConnectionManager()

# =============================================================================
# Background Tasks (محسّنة مع IntentEngine)
# =============================================================================

async def _update_stats_loop(app: FastAPI):
    """تحديث الإحصائيات كل 2 ثانية وإرسالها عبر WebSocket (مع IntentEngine)."""
    while True:
        try:
            await asyncio.sleep(2)
            db = app.state.db
            bot = app.state.bot_ref
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
                    accounts_stats.append({
                        "name": s.get("name"),
                        "phone": s.get("phone"),
                        "connected": s.get("connected"),
                        "priority": s.get("priority"),
                        "alerts": s.get("alerts", 0),
                        "queued": s.get("queued", 0),
                        "last_error": s.get("last_error"),
                        # NEW v2.1: إحصائيات القرارات من IntentEngine
                        "accepted": s.get("accepted", 0),
                        "reviewed": s.get("reviewed", 0),
                        "ignored": s.get("ignored", 0),
                        "avg_confidence": s.get("avg_confidence", 0.0),
                    })

            # ذاكرة
            import psutil
            process = psutil.Process()
            mem = psutil.virtual_memory()

            stats = {
                "total_messages": db_stats.get("total_messages", 0),
                "alerts_sent": db_stats.get("alerts_sent", 0),
                "queue_size": queue_size,
                "connected_accounts": connected,
                "total_accounts": len(ACCOUNTS),
                "uptime": int(time.time() - db.start_time) if hasattr(db, "start_time") else 0,
                "memory_used": process.memory_info().rss // (1024 * 1024),
                "memory_total": mem.total // (1024 * 1024),
                "cpu_percent": process.cpu_percent(),
                "filter_stats": filter_tele,
                "accounts": accounts_stats,
                "unique_senders": db_stats.get("unique_senders", 0),
                "alerts_last_hour": db_stats.get("alerts_last_hour", 0),
                "messages_last_hour": db_stats.get("messages_last_hour", 0),
                "blocked_senders": db_stats.get("blocked_senders", 0),
                "avg_reputation": db_stats.get("avg_reputation", 0),
                # NEW v2.1: IntentEngine إحصائيات القرارات
                "filter_accepted": filter_tele.get("accepted", 0),
                "filter_review": filter_tele.get("review", 0),
                "filter_ignored": filter_tele.get("ignored", 0),
                "filter_avg_confidence": filter_tele.get("avg_confidence", 0.0),
                "filter_processed": filter_tele.get("processed", 0),
                "filter_valid": filter_tele.get("valid", 0),
                # NEW v2.1: إحصائيات من قاعدة البيانات
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

async def _broadcast_logs_loop(app: FastAPI):
    """إرسال السجلات الجديدة عبر WebSocket."""
    last_log_count = 0
    while True:
        try:
            await asyncio.sleep(3)
            pass
        except asyncio.CancelledError:
            break
        except Exception:
            pass

# =============================================================================
# API Endpoints (محسّنة مع IntentEngine v2.1)
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
    """Render health check + keep-alive endpoint (no auth)."""
    db = getattr(request.app.state, "db", None)
    db_ok = bool(db and getattr(db, "is_connected", False))
    bot = getattr(request.app.state, "bot_ref", None)
    monitors_up = 0
    monitors_total = 0
    if bot:
        monitors_total = len(bot.monitors)
        monitors_up = sum(1 for m in bot.monitors if m.is_connected)
    uptime = 0
    if db_ok and hasattr(db, "start_time"):
        uptime = int(time.time() - db.start_time)
    return JSONResponse({
        "status": "ok",
        "database": "ok" if db_ok else "down",
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
    """قائمة الحسابات مع إحصائيات IntentEngine."""
    bot = request.app.state.bot_ref
    if not bot:
        return JSONResponse({"accounts": []})
    accounts = []
    for m in bot.monitors:
        s = await m.get_stats()
        accounts.append({
            "name": s.get("name"),
            "phone": s.get("phone"),
            "connected": s.get("connected"),
            "priority": s.get("priority"),
            "alerts": s.get("alerts", 0),
            "queued": s.get("queued", 0),
            "send_cb_state": s.get("send_cb_state"),
            "connect_attempts": s.get("connect_attempts"),
            "last_error": s.get("last_error"),
            # NEW v2.1: IntentEngine
            "accepted": s.get("accepted", 0),
            "reviewed": s.get("reviewed", 0),
            "ignored": s.get("ignored", 0),
            "avg_confidence": s.get("avg_confidence", 0.0),
            "messages_processed": s.get("messages_processed", 0),
            "avg_processing_time_ms": s.get("avg_processing_time_ms", 0),
        })
    return JSONResponse({"accounts": accounts})

@app.post("/api/accounts", dependencies=[Depends(verify_token)])
async def add_account(data: AccountCreate, request: Request):
    """إضافة حساب جديد."""
    for acc in ACCOUNTS:
        if acc["phone"] == data.phone:
            raise HTTPException(status_code=400, detail="Account already exists")

    env_path = "accounts.env"
    prefix = f"ACCOUNT_{len(ACCOUNTS)+1}"
    new_account_lines = [
        f"\n# === {data.name} ===\n",
        f"{prefix}_API_ID={data.api_id}\n",
        f"{prefix}_API_HASH={data.api_hash}\n",
        f"{prefix}_PHONE={data.phone}\n",
        f"{prefix}_SESSION_NAME={data.session_name}\n",
        f"{prefix}_PRIORITY={data.priority}\n",
    ]
    with open(env_path, "a", encoding="utf-8") as f:
        f.writelines(new_account_lines)

    return JSONResponse({"success": True, "message": "Account added. Restart bot to apply."})

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
        rows = await db.get_messages_with_filters(
            limit=limit,
            offset=offset,
            keyword=keyword,
        )
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
            limit=limit,
            offset=offset,
            keyword=keyword,
            account=account,
            decision=decision,
            min_confidence=min_confidence,
        )
        return JSONResponse({
            "alerts": rows,
            "total": len(rows),
            "filters": {
                "decision": decision,
                "min_confidence": min_confidence,
            }
        })
    except Exception as e:
        logger.error(f"Error fetching alerts: {e}")
        return JSONResponse({"alerts": [], "total": 0})

@app.get("/api/alerts/stats", dependencies=[Depends(verify_token)])
async def get_alerts_stats(request: Request):
    """إحصائيات التنبيهات مع تحليل IntentEngine."""
    db = request.app.state.db
    try:
        summary = await db.get_dashboard_summary()
        return JSONResponse(summary)
    except Exception as e:
        logger.error(f"Error fetching alerts stats: {e}")
        return JSONResponse({})

@app.get("/api/keywords", dependencies=[Depends(verify_token)])
async def get_keywords():
    """جلب جميع الكلمات المفتاحية."""
    return JSONResponse({"keywords": KEYWORDS})

@app.post("/api/keywords", dependencies=[Depends(verify_token)])
async def add_keyword(data: KeywordCreate, request: Request):
    """إضافة كلمة مفتاحية جديدة."""
    category = data.category
    keyword = data.keyword.strip().lower()
    if category not in KEYWORDS:
        raise HTTPException(status_code=400, detail=f"Category '{category}' not found")
    if keyword in KEYWORDS[category]:
        return JSONResponse({"success": False, "error": "Keyword already exists"})
    KEYWORDS[category].append(keyword)

    with open("keywords.json", "r", encoding="utf-8") as f:
        all_data = json.load(f)
    all_data[category].append(keyword)
    with open("keywords.json", "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)

    bot = request.app.state.bot_ref
    if bot:
        bot.filter._build_keyword_sets()
    return JSONResponse({"success": True, "keyword": keyword})

@app.delete("/api/keywords", dependencies=[Depends(verify_token)])
async def delete_keyword(data: KeywordDelete, request: Request):
    """حذف كلمة مفتاحية."""
    category = data.category
    keyword = data.keyword.strip()
    if category not in KEYWORDS:
        raise HTTPException(status_code=400, detail=f"Category '{category}' not found")
    if keyword not in KEYWORDS[category]:
        return JSONResponse({"success": False, "error": "Keyword not found"})
    KEYWORDS[category].remove(keyword)

    with open("keywords.json", "r", encoding="utf-8") as f:
        all_data = json.load(f)
    if keyword in all_data[category]:
        all_data[category].remove(keyword)
    with open("keywords.json", "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)

    bot = request.app.state.bot_ref
    if bot:
        bot.filter._build_keyword_sets()
    return JSONResponse({"success": True})

@app.get("/api/blocked/senders", dependencies=[Depends(verify_token)])
async def get_blocked_senders(request: Request):
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
    bot = request.app.state.bot_ref
    if bot:
        if data.max_alerts_per_minute is not None:
            bot.rate_limiter._max_min = data.max_alerts_per_minute
        if data.max_alerts_per_hour is not None:
            bot.rate_limiter._max_hr = data.max_alerts_per_hour
        if data.prefilter_enabled is not None:
            CFG.PREFILTER_ENABLED = data.prefilter_enabled
        if data.language_filter is not None:
            CFG.LANGUAGE_FILTER = data.language_filter
    return JSONResponse({"success": True, "message": "Settings updated"})

@app.post("/api/purge", dependencies=[Depends(verify_token)])
async def purge_queue(request: Request):
    db = request.app.state.db
    count = await db.purge_queue()
    return JSONResponse({"success": True, "purged": count})

@app.post("/api/restart", dependencies=[Depends(verify_token)])
async def restart_bot(request: Request):
    bot = request.app.state.bot_ref
    if bot:
        asyncio.create_task(_safe_restart(bot))
    return JSONResponse({"success": True, "message": "Restart initiated"})

async def _safe_restart(bot):
    """إعادة تشغيل البوت بأمان."""
    logger.warning("Restarting bot via dashboard...")
    await bot.stop()
    await asyncio.sleep(2)
    await bot._send_startup_message()
    logger.info("Bot restarted successfully")

# =============================================================================
# Telegram Account Login System (Session String generator for Render)
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
                device_model="Render Cloud", system_version="Linux", app_version="13.0",
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
    """Upsert an env var on the Render service via Render API (triggers redeploy)."""
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
# WebSocket Endpoint
# =============================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        if hasattr(websocket.app, "state") and websocket.app.state.stats_cache:
            await websocket.send_json({"type": "stats", "data": websocket.app.state.stats_cache})
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("command") == "ping":
                    await websocket.send_json({"type": "pong"})
                elif msg.get("command") == "get_alerts_stats":
                    db = websocket.app.state.db
                    summary = await db.get_dashboard_summary()
                    await websocket.send_json({"type": "alerts_stats", "data": summary})
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
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
# Existing routes (/api/*, /login, /health) remain untouched.
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
