"""
webadmin/routes.py – REST API layer for the admin dashboard.

Route map (spec-mandated paths are preserved exactly):
  Auth    : POST /admin/api/auth/login, POST /admin/api/auth/logout,
            GET  /admin/api/auth/me,     GET  /admin/api/auth/csrf
  SPA     : GET  /admin
  Stats   : GET  /stats
  Alerts  : GET  /alerts, GET /alerts/export.csv
  Keywords: GET  /keywords, GET /keywords/export, POST /keywords/import,
            POST /keywords/add, POST /keywords/update, POST /keywords/delete
  Logs    : GET  /logs
  Accounts: GET  /accounts, POST /accounts/add, POST /accounts/delete,
            POST /accounts/enable, POST /accounts/disable, POST /accounts/reconnect
  Settings: GET  /settings, POST /settings
  Control : POST /bot/reload, POST /bot/restart,
            POST /bot/backup, GET /bot/backups, POST /bot/restore,
            GET  /bot/backups/{name}/download
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from config import ACCOUNTS, CFG
from webadmin import auth, backup, keywords_store, logs_reader, render_api, settings_store

router = APIRouter()

Protected = Depends(auth.login_required)
CsrfProtected = Depends(auth.csrf_protect)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class KeywordAddBody(BaseModel):
    path: str = Field(min_length=1, max_length=300)
    value: str = Field(min_length=1, max_length=500)


class KeywordUpdateBody(BaseModel):
    path: str = Field(min_length=1, max_length=300)
    old: str = Field(min_length=1, max_length=500)
    new: str = Field(min_length=1, max_length=500)


class KeywordDeleteBody(BaseModel):
    path: str = Field(min_length=1, max_length=300)
    value: str = Field(min_length=1, max_length=500)


class AccountAddBody(BaseModel):
    api_id: int
    api_hash: str = Field(min_length=32, max_length=64)
    phone: str = Field(min_length=5, max_length=32)
    session_name: Optional[str] = Field(default=None, max_length=64)
    priority: int = Field(default=5, ge=0, le=100)


class AccountPrefixBody(BaseModel):
    prefix: str = Field(min_length=1, max_length=32)


class SettingsBody(BaseModel):
    updates: Dict[str, Any]


class RestoreBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _bot(request: Request) -> Any:
    return getattr(request.app.state, "bot_ref", None)


def _db(request: Request) -> Any:
    return getattr(request.app.state, "db", None)


def _account_row(acc: Dict[str, Any], monitors: List[Any]) -> Dict[str, Any]:
    mon = next(
        (m for m in monitors if m.account.get("prefix") == acc.get("prefix")), None
    )
    connected = bool(mon and getattr(mon, "is_connected", False))
    last_error = getattr(mon, "_last_connect_error", None) if mon else None
    stats = getattr(mon, "_stats", {}) if mon else {}
    return {
        "prefix": acc.get("prefix"),
        "name": acc.get("name"),
        "phone": acc.get("phone"),
        "session_name": acc.get("session"),
        "enabled": bool(acc.get("enabled", True)),
        "is_main": bool(acc.get("is_main")),
        "priority": acc.get("priority", 0),
        "connected": connected,
        "status": "متصل" if connected else "غير متصل",
        "last_connection": stats.get("last_connected") or stats.get("last_active"),
        "last_error": last_error or stats.get("last_error"),
        "has_session": bool(acc.get("session_string")),
        "alerts": stats.get("alerts", 0) if mon else 0,
    }


def _telegram_links(row: Dict[str, Any]) -> Dict[str, Optional[str]]:
    username = row.get("username")
    chat_id = row.get("chat_id")
    links: Dict[str, Optional[str]] = {
        "profile": f"https://t.me/{username}" if username else None,
        "message": row.get("last_message_link"),
        "chat": row.get("last_group_link"),
    }
    if not links["profile"] and row.get("sender_id"):
        links["profile"] = f"tg://user?id={row['sender_id']}"
    if not links["message"] and chat_id and row.get("message_link"):
        links["message"] = row["message_link"]
    return links


# ===========================================================================
# Auth
# ===========================================================================
@router.post("/admin/api/auth/login")
async def auth_login(body: LoginBody, request: Request, response: Response):
    if not auth.auth_is_configured():
        raise HTTPException(
            status_code=503,
            detail="بيانات الدخول غير مضبوطة - عرّف DASHBOARD_USERNAME و DASHBOARD_PASSWORD",
        )
    auth._login_guard.check_allowed(request)
    if not auth.verify_credentials(body.username, body.password):
        auth._login_guard.record_failure(request)
        await asyncio.sleep(0.5)  # slow down online guessing
        raise HTTPException(status_code=401, detail="اسم المستخدم أو كلمة المرور غير صحيحة")
    auth._login_guard.record_success(request)
    csrf = auth.create_session(request, response, body.username)
    logger.info(f"Admin login: {body.username}")
    return {"success": True, "username": body.username, "csrf": csrf}


@router.post("/admin/api/auth/logout")
async def auth_logout(response: Response):
    auth.clear_session(response)
    return {"success": True}


@router.get("/admin/api/auth/me")
async def auth_me(session: Dict[str, Any] = Protected):
    return {"authenticated": True, "username": session["u"], "csrf": session["csrf"]}


@router.get("/admin/api/auth/csrf")
async def auth_csrf(session: Dict[str, Any] = Protected):
    return {"csrf": session["csrf"]}


# ===========================================================================
# SPA
# ===========================================================================
@router.get("/admin", response_class=HTMLResponse)
async def admin_spa():
    template_path = os.path.join("templates", "admin.html")
    with open(template_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ===========================================================================
# Stats
# ===========================================================================
@router.get("/stats")
async def get_stats(request: Request, session: Dict[str, Any] = Protected):
    db = _db(request)
    bot = _bot(request)
    out: Dict[str, Any] = {"ok": True, "time": time.time()}

    if bot is not None:
        out["uptime"] = int(time.monotonic() - bot._start_time)
        out["rate_limiter"] = bot.rate_limiter.status()
        try:
            out["memory"] = bot.memory_monitor.check()
        except Exception:
            out["memory"] = {}
        try:
            out["filter"] = await bot.filter.get_telemetry()
        except Exception:
            out["filter"] = {}
        out["monitors"] = [
            _account_row(m.account, bot.monitors) for m in bot.monitors
        ]
    else:
        out.update({"uptime": 0, "rate_limiter": {}, "memory": {}, "filter": {}, "monitors": []})

    if db is not None:
        try:
            out["db"] = await db.get_stats()
        except Exception as e:
            out["db"] = {"error": str(e)}
        try:
            out["summary"] = await db.get_dashboard_summary()
        except Exception:
            out["summary"] = {}
        try:
            out["hourly"] = await db.get_hourly_stats(24)
        except Exception:
            out["hourly"] = []
        try:
            out["recent_alerts"] = await db.get_recent_alerts_for_dashboard(5)
        except Exception:
            out["recent_alerts"] = []
        try:
            out["queue_size"] = await db.queue_size()
        except Exception:
            out["queue_size"] = 0
    return out


# ===========================================================================
# Alerts
# ===========================================================================
async def _enrich_contact_links(db: Any, rows: List[Dict[str, Any]]) -> None:
    """Merge each sender's telegram links (message/chat) from sender_contacts
    into the alert rows (single batched IN-query, no N+1)."""
    sender_ids = sorted({r.get("sender_id") for r in rows if r.get("sender_id")})
    if not sender_ids or db is None:
        return
    placeholders = ",".join("?" for _ in sender_ids)
    try:
        contacts = await db._fetchall(
            f"SELECT sender_id, last_message_link, last_group_link "
            f"FROM sender_contacts WHERE sender_id IN ({placeholders})",
            tuple(sender_ids),
        )
    except Exception as e:
        logger.debug(f"contact link enrichment skipped: {e}")
        return
    by_id = {c["sender_id"]: c for c in contacts}
    for r in rows:
        c = by_id.get(r.get("sender_id"))
        if c:
            r["last_message_link"] = c.get("last_message_link")
            r["last_group_link"] = c.get("last_group_link")


def _serialize_alert(row: Dict[str, Any]) -> Dict[str, Any]:
    text = row.get("alert_text") or ""
    links = _telegram_links(row)
    return {
        "id": row.get("id"),
        "time": row.get("timestamp"),
        "time_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row.get("timestamp") or 0)),
        "sender_id": row.get("sender_id"),
        "sender_name": " ".join(
            p for p in [row.get("first_name"), row.get("last_name")] if p
        ) or None,
        "username": row.get("username"),
        "chat_id": row.get("chat_id"),
        "account": row.get("account_name"),
        "keyword": row.get("keyword"),
        "preview": text[:200],
        "decision": row.get("decision"),
        "confidence": row.get("confidence"),
        "links": links,
    }


@router.get("/alerts")
async def get_alerts(
    request: Request,
    session: Dict[str, Any] = Protected,
    search: Optional[str] = Query(default=None),
    account: Optional[str] = Query(default=None),
    sender_id: Optional[int] = Query(default=None),
    from_ts: Optional[float] = Query(default=None),
    to_ts: Optional[float] = Query(default=None),
    decision: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    db = _db(request)
    if db is None:
        return {"alerts": [], "total": 0}
    rows = await db.get_alerts_with_filters(
        limit=limit,
        offset=offset,
        keyword=search,
        account=account,
        sender_id=sender_id,
        from_date=from_ts,
        to_date=to_ts,
        decision=decision,
    )
    if category:
        terms = set(keywords_store.category_terms(category))
        if terms:
            rows = [r for r in rows if (r.get("keyword") or "") in terms]
    await _enrich_contact_links(db, rows)
    return {"alerts": [_serialize_alert(r) for r in rows], "count": len(rows)}


@router.get("/alerts/export.csv")
async def export_alerts_csv(
    request: Request,
    session: Dict[str, Any] = Protected,
    search: Optional[str] = Query(default=None),
    from_ts: Optional[float] = Query(default=None),
    to_ts: Optional[float] = Query(default=None),
    decision: Optional[str] = Query(default=None),
):
    db = _db(request)
    rows = [] if db is None else await db.get_alerts_with_filters(
        limit=5000, offset=0, keyword=search, from_date=from_ts, to_date=to_ts, decision=decision
    )

    await _enrich_contact_links(db, rows)

    def _render() -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            ["id", "time", "sender_id", "sender_name", "username", "chat_id",
             "account", "keyword", "decision", "confidence", "text", "message_link"]
        )
        for r in rows:
            s = _serialize_alert(r)
            writer.writerow(
                [s["id"], s["time_iso"], s["sender_id"], s["sender_name"], s["username"],
                 s["chat_id"], s["account"], s["keyword"], s["decision"], s["confidence"],
                 s["preview"], s["links"]["message"]]
            )
        return "﻿" + buf.getvalue()  # BOM for Excel UTF-8

    content = await asyncio.to_thread(_render)
    filename = time.strftime("alerts-%Y%m%d-%H%M%S.csv")
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ===========================================================================
# Keywords
# ===========================================================================
@router.get("/keywords")
async def get_keywords(
    session: Dict[str, Any] = Protected,
    q: Optional[str] = Query(default=None),
):
    if q:
        return {"mode": "search", "results": keywords_store.search(q)}
    return {"mode": "index", "categories": keywords_store.get_index()}


@router.get("/keywords/export")
async def keywords_export(session: Dict[str, Any] = Protected):
    import json as _json

    data = keywords_store.export_keywords()
    content = _json.dumps(data, ensure_ascii=False, indent=2)
    return Response(
        content=content,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="keywords.json"'},
    )


@router.post("/keywords/import")
async def keywords_import(request: Request, _: Any = CsrfProtected):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON غير صالح")
    result = await keywords_store.import_keywords(data)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "فشل الاستيراد"))
    return result


@router.post("/keywords/add")
async def keywords_add(body: KeywordAddBody, _: Any = CsrfProtected):
    result = await keywords_store.add_keyword(body.path, body.value)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/keywords/update")
async def keywords_update(body: KeywordUpdateBody, _: Any = CsrfProtected):
    result = await keywords_store.update_keyword(body.path, body.old, body.new)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/keywords/delete")
async def keywords_delete(body: KeywordDeleteBody, _: Any = CsrfProtected):
    result = await keywords_store.delete_keyword(body.path, body.value)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


# ===========================================================================
# Logs
# ===========================================================================
@router.get("/logs")
async def get_logs(
    session: Dict[str, Any] = Protected,
    level: Optional[str] = Query(default=None),
    lines: int = Query(default=200, ge=10, le=1000),
):
    return await asyncio.to_thread(logs_reader.tail, lines, level)


# ===========================================================================
# Accounts
# ===========================================================================
@router.get("/accounts")
async def get_accounts(request: Request, session: Dict[str, Any] = Protected):
    bot = _bot(request)
    monitors = getattr(bot, "monitors", []) if bot else []
    return {
        "accounts": [_account_row(acc, monitors) for acc in ACCOUNTS],
        "render_api_configured": render_api.is_configured(),
    }


@router.post("/accounts/add")
async def accounts_add(body: AccountAddBody, _: Any = CsrfProtected):
    phone = body.phone.strip()
    if not phone.lstrip("+").isdigit():
        raise HTTPException(status_code=400, detail="رقم الهاتف غير صالح")
    used = {acc.get("prefix") for acc in ACCOUNTS}
    prefix = next((f"ACCOUNT_{i}" for i in range(1, 21) if f"ACCOUNT_{i}" not in used), None)
    if prefix is None:
        raise HTTPException(status_code=400, detail="تم بلوغ الحد الأقصى للحسابات (20)")
    session_name = (body.session_name or prefix.lower()).strip()
    pairs = {
        f"{prefix}_API_ID": str(body.api_id),
        f"{prefix}_API_HASH": body.api_hash.strip(),
        f"{prefix}_PHONE": phone,
        f"{prefix}_SESSION_NAME": session_name,
        f"{prefix}_ENABLED": "true",
        f"{prefix}_PRIORITY": str(body.priority),
    }
    result = await render_api.upsert_env_many(pairs)
    if not result.get("saved"):
        raise HTTPException(
            status_code=502,
            detail=f"تعذر حفظ الحساب في متغيرات Render: {result.get('reason')}",
        )
    logger.info(f"Account {prefix} env vars written via dashboard")
    return {
        "success": True,
        "prefix": prefix,
        "message": "تم حفظ الحساب في متغيرات Render - ستعاد إعادة النشر تلقائياً، ثم سجّل جلسته من /login",
    }


@router.post("/accounts/delete")
async def accounts_delete(body: AccountPrefixBody, request: Request, _: Any = CsrfProtected):
    prefix = body.prefix.strip().upper()
    acc = next((a for a in ACCOUNTS if a.get("prefix") == prefix), None)
    if acc is None:
        raise HTTPException(status_code=404, detail="الحساب غير موجود")
    if acc.get("is_main"):
        raise HTTPException(status_code=400, detail="لا يمكن حذف الحساب الرئيسي")

    bot = _bot(request)
    mon = next(
        (m for m in getattr(bot, "monitors", []) if m.account.get("prefix") == prefix), None
    ) if bot else None
    if mon is not None:
        try:
            await mon.disconnect()
        except Exception as e:
            logger.warning(f"disconnect during delete failed: {e}")
        acc["enabled"] = False

    keys = ["API_ID", "API_HASH", "PHONE", "SESSION_NAME", "SESSION_STRING", "ENABLED", "PRIORITY"]
    failures = {}
    for suffix in keys:
        r = await render_api.delete_env(f"{prefix}_{suffix}")
        if not r.get("saved"):
            failures[suffix] = r.get("reason")
    if failures:
        return {"success": False, "error": "بعض المتغيرات لم تحذف", "details": failures}
    return {"success": True, "message": f"حُذف الحساب {prefix} - ستعاد إعادة النشر تلقائياً"}


@router.post("/accounts/enable")
async def accounts_enable(body: AccountPrefixBody, request: Request, _: Any = CsrfProtected):
    return await _set_account_enabled(body.prefix, True, request)


@router.post("/accounts/disable")
async def accounts_disable(body: AccountPrefixBody, request: Request, _: Any = CsrfProtected):
    return await _set_account_enabled(body.prefix, False, request)


async def _set_account_enabled(prefix: str, enabled: bool, request: Request) -> Dict[str, Any]:
    prefix = prefix.strip().upper()
    acc = next((a for a in ACCOUNTS if a.get("prefix") == prefix), None)
    if acc is None:
        raise HTTPException(status_code=404, detail="الحساب غير موجود")

    bot = _bot(request)
    mon = next(
        (m for m in getattr(bot, "monitors", []) if m.account.get("prefix") == prefix), None
    ) if bot else None
    if not enabled and mon is not None:
        try:
            await mon.disconnect()
        except Exception as e:
            logger.warning(f"disconnect during disable failed: {e}")
    acc["enabled"] = enabled
    if mon is not None:
        mon.account["enabled"] = enabled

    result = await render_api.upsert_env(f"{prefix}_ENABLED", "true" if enabled else "false")
    return {
        "success": True,
        "prefix": prefix,
        "enabled": enabled,
        "persisted_on_render": bool(result.get("saved")),
        "note": None if result.get("saved") else f"طبّق محلياً فقط: {result.get('reason')}",
    }


@router.post("/accounts/reconnect")
async def accounts_reconnect(body: AccountPrefixBody, request: Request, _: Any = CsrfProtected):
    prefix = body.prefix.strip().upper()
    bot = _bot(request)
    if bot is None:
        raise HTTPException(status_code=503, detail="البوت غير متاح")
    mon = next(
        (m for m in bot.monitors if m.account.get("prefix") == prefix), None
    )
    if mon is None:
        raise HTTPException(status_code=404, detail="الحساب غير موجود ضمن المراقبين النشطين")

    # Refresh session material from env before reconnecting
    mon.account["session_string"] = os.getenv(f"{prefix}_SESSION_STRING") or None
    try:
        await mon.disconnect()
    except Exception as e:
        logger.warning(f"pre-reconnect disconnect failed: {e}")
    ok = False
    try:
        ok = await asyncio.wait_for(mon.connect(), timeout=120)
    except asyncio.TimeoutError:
        ok = False
    except Exception as e:
        logger.error(f"reconnect error: {e}")
    return {
        "success": bool(ok),
        "prefix": prefix,
        "connected": bool(getattr(mon, "is_connected", False)),
        "last_error": getattr(mon, "_last_connect_error", None),
    }


# ===========================================================================
# Settings
# ===========================================================================
@router.get("/settings")
async def settings_get(session: Dict[str, Any] = Protected):
    return {"settings": settings_store.get_settings(), "schema": settings_store.get_schema()}


@router.post("/settings")
async def settings_update(body: SettingsBody, _: Any = CsrfProtected):
    return await settings_store.update_settings(body.updates)


# ===========================================================================
# Control center
# ===========================================================================
@router.post("/bot/reload")
async def bot_reload(_: Any = CsrfProtected):
    result = await keywords_store.hot_reload()
    return {"success": not result.get("error"), **result}


@router.post("/bot/restart")
async def bot_restart(request: Request, _: Any = CsrfProtected):
    result = await render_api.restart_service()
    if result.get("restarted"):
        return {"success": True, "mode": "render", "message": "طُلبت إعادة التشغيل من Render"}

    # Fallback: graceful local shutdown - Render restarts the exited process
    async def _graceful_exit() -> None:
        bot = getattr(request.app.state, "bot_ref", None)
        await asyncio.sleep(1)
        try:
            if bot is not None:
                await bot.stop()
        except Exception:
            pass
        os._exit(0)

    asyncio.create_task(_graceful_exit())
    return {
        "success": True,
        "mode": "local-exit",
        "message": f"إعادة تشغيل ذاتية (Render API: {result.get('reason')})",
    }


@router.post("/bot/backup")
async def bot_backup(_: Any = CsrfProtected):
    return await backup.create_backup()


@router.get("/bot/backups")
async def bot_backups(session: Dict[str, Any] = Protected):
    return {"backups": backup.list_backups()}


@router.post("/bot/restore")
async def bot_restore(body: RestoreBody, _: Any = CsrfProtected):
    result = await backup.restore_backup(body.name)
    if not result.get("success") and result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/bot/backups/{name}/download")
async def bot_backup_download(name: str, session: Dict[str, Any] = Protected):
    path = backup.backup_zip_path(name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="النسخة غير موجودة")
    return FileResponse(path, media_type="application/zip", filename=os.path.basename(path))
