# -*- coding: utf-8 -*-
"""════════════════════════════════════════════════════════════════════════
dashboard.server — طبقة HTTP للوحة على خادم الصحة (v15.1.0)
════════════════════════════════════════════════════════════════════════

تُركَّب داخل main.py:_start_health_server_thread على نفس web.Application
(نفس منفذ $PORT) — صفر خيوط جديدة وصفر processes.

الأمان:
  * كل /admin/api/* يتطلب جلسة كوكي (HttpOnly, SameSite=Strict) عدا
    /login و /session.
  * كل طلب تعديلي (POST/PUT/DELETE) يتطلب ترويسة X-CSRF-Token مطابقة
    للتوكن — طبقة ثانية فوق SameSite (لا كوكي خارجي ولا هيدر مخصص
    يمكن وضعهما عبر SITE آخر).
  * Rate Limit لكل IP (الواجهة 120/د، تسجيل الدخول 5/د) + قفل 15 دقيقة
    بعد المحاولات الفاشلة (ذاكرة حصراً — لا غزو DB عند هجمات التخمين).
  * مقارنة بيانات الاعتماد constant-time (security.constant_time_equals).
  * IP العميل عبر security.resolve_client_ip (بروكسي Render موثوق).
  * ترويسات الحماية: nosniff + DENY إطارات + CSP صارم للأصول.

الفصل عن مسار الرسائل:
  * معالجات القراءة السريعة (ops/status) تقرأ الحالة المشتركة من الذاكرة
    مباشرة — صفر جسر.
  * كل ما يحتاج DB أو Telethon أو المحرك يُجسَّر إلى الحلقة الرئيسية عبر
    run_coroutine_threadsafe مع مهلة DASHBOARD_BRIDGE_TIMEOUT_S — الحلقة
    الرئيسية لا تُحجب أبداً، وخيط الصحة يبقى مجيباً لـ/health في كل حال.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import secrets as _secrets
from typing import Any, Callable, Optional

from aiohttp import web

from dashboard.api import DashboardAPI, DashboardHTTPError
from dashboard.state import (
    DEFAULT_API_RATE_PER_MIN,
    DEFAULT_LOGIN_LOCKOUT_SECONDS,
    DEFAULT_LOGIN_RATE_PER_MIN,
    DEFAULT_SESSION_TTL_SECONDS,
    DASH,
)

COOKIE_NAME = "br7dash"
_LOGIN_PUBLIC_PATHS = {"/admin/api/login", "/admin/api/session"}

try:  # أوقات الاختبار قد لا تكون env مضبوطة — قيم state الافتراضية كافية
    from config import CFG as _CFG
except Exception:  # pragma: no cover
    _CFG = None


def _cfg_val(name: str, default: Any) -> Any:
    try:
        return getattr(_CFG, name, default)
    except Exception:
        return default


def _dumps(data: Any) -> str:
    return jsonlib.dumps(data, ensure_ascii=False, default=str)


def _json(data: Any, status: int = 200) -> web.Response:
    resp = web.json_response(data, status=status, dumps=_dumps)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _cfg_secret(name_user: str, name_pass: str) -> tuple:
    u = str(_cfg_val("DASHBOARD_USERNAME", "bashar") or "bashar")
    p = str(_cfg_val("DASHBOARD_PASSWORD", "bashar123r") or "bashar123r")
    return u, p


# ═════════════════════════════════════════════════════════════════════════
# التركيب
# ═════════════════════════════════════════════════════════════════════════
def setup_dashboard(app: web.Application, bot: Any) -> None:
    """يُستدعى من خيط الصحة قبل AppRunner.setup — يضيف المسارات والوسيط."""
    from pathlib import Path

    DASH.version = str(_cfg_val("BOT_VERSION", "15.1.0"))
    DASH.username, _pw = _cfg_secret("DASHBOARD_USERNAME", "DASHBOARD_PASSWORD")

    # أصول الواجهة تُقرأ من القرص مرة واحدة فقط عند التركيب
    assets_dir = Path(__file__).resolve().parent / "assets"
    for name, fallback_mime in (("index.html", "text/html"), ("app.js", "text/javascript"),
                                ("style.css", "text/css")):
        try:
            DASH.assets[name] = (assets_dir / name).read_bytes()
        except Exception:
            DASH.assets[name] = b"/* dashboard asset missing */"

    ttl_seconds = max(300, int(_cfg_val("DASHBOARD_SESSION_TTL_MINUTES",
                                        DEFAULT_SESSION_TTL_SECONDS // 60)) * 60)
    api_rate_per_min = int(_cfg_val("DASHBOARD_API_RATE_PER_MIN", DEFAULT_API_RATE_PER_MIN))
    login_rate_per_min = int(_cfg_val("DASHBOARD_LOGIN_RATE_PER_MIN", DEFAULT_LOGIN_RATE_PER_MIN))
    lockout_s = int(_cfg_val("DASHBOARD_LOGIN_LOCKOUT_SECONDS", DEFAULT_LOGIN_LOCKOUT_SECONDS))
    bridge_timeout = float(_cfg_val("DASHBOARD_BRIDGE_TIMEOUT_S", 25.0))

    DASH.api_rate = type(DASH.api_rate)(api_rate_per_min)
    DASH.login_rate = type(DASH.login_rate)(login_rate_per_min)
    DASH.guard = type(DASH.guard)(max_attempts=login_rate_per_min, lockout_seconds=lockout_s)

    api = DashboardAPI(bot)

    # ── الجسر إلى الحلقة الرئيسية ───────────────────────────────────────
    class _BridgeError(Exception):
        def __init__(self, status: int, payload: dict) -> None:
            super().__init__(str(payload))
            self.status = status
            self.payload = payload

    async def _bridge_call(coro_factory: Callable[[], Any]) -> Any:
        """ينفذ على الحلقة الرئيسية ويعيد الناتج الخام — للأخطاء _BridgeError."""
        loop = getattr(bot, "_main_loop", None)
        if loop is None or loop.is_closed():
            raise _BridgeError(503, {"error": "bot_core_unavailable",
                                     "note": "نواة البوت ليست جاهزة بعد — أعد المحاولة بعد دقيقة"})
        fut = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=bridge_timeout)
        except DashboardHTTPError as e:
            raise _BridgeError(e.status, e.payload)
        except asyncio.TimeoutError:
            raise _BridgeError(504, {"error": "bridge_timeout"})
        except Exception as e:  # noqa: BLE001 — لا تسريب تفاصيل داخلياً
            raise _BridgeError(500, {"error": "internal_error", "detail": type(e).__name__})

    async def bridge(coro_factory: Callable[[], Any]) -> web.Response:
        try:
            return _json(await _bridge_call(coro_factory))
        except _BridgeError as e:
            return _json(e.payload, e.status)

    async def bridge_attach(coro_factory: Callable[[], Any],
                            headers: dict) -> web.Response:
        """نفس bridge مع ترويسات إضافية (تنزيل ملفات: نسخ احتياطي/تصدير)."""
        try:
            resp = _json(await _bridge_call(coro_factory))
            resp.headers.update(headers)
            return resp
        except _BridgeError as e:
            return _json(e.payload, e.status)

    async def body_json(request: web.Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise DashboardHTTPError(400, {"error": "invalid_json"})
        if not isinstance(data, dict):
            raise DashboardHTTPError(400, {"error": "invalid_body"})
        return data

    def _client_ip(request: web.Request) -> str:
        try:
            from security import resolve_client_ip
            return resolve_client_ip(request.remote, request.headers.get("X-Forwarded-For", ""))
        except Exception:
            return str(request.remote or "unknown")[:64]

    def _is_https(request: web.Request) -> bool:
        proto = (request.headers.get("X-Forwarded-Proto") or "").lower()
        return proto == "https" or bool(request.secure)

    def _session_token(request: web.Request) -> Optional[str]:
        return request.cookies.get(COOKIE_NAME)

    def _audit_bg(action: str, object_id: str = "", new_value: str = "") -> None:
        """تدقيق بدون انتظار — فشل-آمن، لا يحجب الاستجابة أبداً."""
        loop = getattr(bot, "_main_loop", None)
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                api_db_audit(api, action, object_id, new_value), loop
            )
        except Exception:
            pass

    # ── الأصول ──────────────────────────────────────────────────────────
    async def admin_index(request: web.Request) -> web.Response:
        resp = web.Response(body=DASH.assets.get("index.html", b""), content_type="text/html",
                            charset="utf-8")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    async def admin_asset(request: web.Request) -> web.Response:
        name = request.match_info.get("name", "")
        if name not in ("app.js", "style.css"):
            return _json({"error": "not_found"}, 404)
        mime = "application/javascript" if name.endswith(".js") else "text/css"
        resp = web.Response(body=DASH.assets.get(name, b""), content_type=mime, charset="utf-8")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    # ── المصادقة ────────────────────────────────────────────────────────
    async def api_session(request: web.Request) -> web.Response:
        token = _session_token(request)
        valid = DASH.sessions.validate(token)
        if valid:
            return _json({"authenticated": True, "username": DASH.username,
                          "version": DASH.version, "ops": DASH.ops.snapshot()})
        return _json({"authenticated": False, "version": DASH.version})

    async def api_login(request: web.Request) -> web.Response:
        ip = _client_ip(request)
        if DASH.guard.is_locked(ip):
            rem = DASH.guard.remaining_lock(ip)
            return _json({
                "error": "locked",
                "note": f"محاولات فاشلة كثيرة — أعد المحاولة بعد {rem} ثانية",
            }, 429)
        if not DASH.login_rate.allow(ip):
            return _json({"error": "rate_limited", "note": "محاولات كثيرة جداً"}, 429)
        try:
            body = await body_json(request)
        except DashboardHTTPError as e:
            return _json(e.payload, e.status)
        username = str(body.get("username") or "")
        password = str(body.get("password") or "")
        try:
            from security import constant_time_equals
            ok_u = constant_time_equals(username, DASH.username)
            ok_p = constant_time_equals(password, _pw)
        except Exception:
            ok_u = ok_p = False
        if not (ok_u and ok_p):
            DASH.guard.record_failure(ip)
            return _json({"error": "invalid_credentials"}, 401)
        DASH.guard.record_success(ip)
        token = _secrets.token_urlsafe(32)
        DASH.sessions.create(token, ip)
        _audit_bg("dashboard.login", "session", ip)
        resp = _json({"ok": True, "username": DASH.username, "version": DASH.version})
        resp.set_cookie(
            COOKIE_NAME, token, max_age=ttl_seconds, httponly=True,
            samesite="Strict", path="/admin", secure=_is_https(request),
        )
        return resp

    async def api_logout(request: web.Request) -> web.Response:
        token = _session_token(request)
        DASH.sessions.drop(token)
        _audit_bg("dashboard.logout", "session", _client_ip(request))
        resp = _json({"ok": True})
        resp.del_cookie(COOKIE_NAME, path="/admin")
        return resp

    # ── الوحدة 1: الحالة ────────────────────────────────────────────────
    async def api_overview(request: web.Request) -> web.Response:
        fresh = DASH.overview_cache.get_if_fresh()
        if fresh is not None:
            return _json(fresh)
        if not DASH.overview_cache.try_begin_refresh():
            stale = DASH.overview_cache.get_stale()
            if stale is not None:
                return _json(stale)
            return _json({"error": "refresh_in_progress"}, 503)
        try:
            data = await _bridge_call(lambda: api.overview())
            DASH.overview_cache.end_refresh(data)
            return _json(data)
        except _BridgeError as e:
            DASH.overview_cache.abort_refresh()
            return _json(e.payload, e.status)
        except Exception:
            DASH.overview_cache.abort_refresh()
            return _json({"error": "overview_failed"}, 500)

    # ── الوحدة 2: الحسابات ──────────────────────────────────────────────
    async def api_accounts(request: web.Request) -> web.Response:
        return await bridge(api.accounts_list)

    async def api_account_test(request: web.Request) -> web.Response:
        body = await body_json(request)
        name = str(body.get("name") or "")[:64]
        return await bridge(lambda: api.account_test(name))

    async def api_account_reconnect(request: web.Request) -> web.Response:
        body = await body_json(request)
        name = str(body.get("name") or "")[:64]
        return await bridge(lambda: api.account_reconnect(name))

    async def api_account_membership(request: web.Request) -> web.Response:
        body = await body_json(request)
        name = str(body.get("name") or "")[:64]
        return await bridge(lambda: api.account_membership(name))

    # ── الوحدة 3: الكلمات + Playground ──────────────────────────────────
    async def api_keywords(request: web.Request) -> web.Response:
        return await bridge(api.keywords_view)

    async def api_keywords_save(request: web.Request) -> web.Response:
        body = await body_json(request)
        return await bridge(lambda: api.keywords_save(body))

    async def api_keywords_import(request: web.Request) -> web.Response:
        body = await body_json(request)
        text = str(body.get("json_text") or "")
        return await bridge(lambda: api.keywords_import(text))

    async def api_keywords_export(request: web.Request) -> web.Response:
        return await bridge_attach(api.keywords_export,
                                   {"Content-Disposition": 'attachment; filename="br7-keywords.json"'})

    async def api_keyword_stats(request: web.Request) -> web.Response:
        return await bridge(api.keyword_stats)

    async def api_playground(request: web.Request) -> web.Response:
        body = await body_json(request)
        text = str(body.get("text") or "")
        return await bridge(lambda: api.playground(text))

    # ── الوحدة 4: المجموعات ─────────────────────────────────────────────
    async def api_groups(request: web.Request) -> web.Response:
        return await bridge(api.groups_list)

    async def api_groups_add(request: web.Request) -> web.Response:
        body = await body_json(request)
        raw = str(body.get("input") or "")[:200]
        return await bridge(lambda: api.group_add(raw))

    async def api_groups_toggle(request: web.Request) -> web.Response:
        body = await body_json(request)
        try:
            chat_id = int(body.get("chat_id"))
            enabled = bool(body.get("enabled"))
        except (TypeError, ValueError):
            return _json({"error": "invalid_chat_id"}, 400)
        return await bridge(lambda: api.group_toggle(chat_id, enabled))

    async def api_groups_remove(request: web.Request) -> web.Response:
        body = await body_json(request)
        try:
            chat_id = int(body.get("chat_id"))
        except (TypeError, ValueError):
            return _json({"error": "invalid_chat_id"}, 400)
        return await bridge(lambda: api.group_remove(chat_id))

    # ── الوحدة 5: التنبيهات ─────────────────────────────────────────────
    async def api_alerts(request: web.Request) -> web.Response:
        try:
            page = int(request.query.get("page", "1"))
        except ValueError:
            page = 1
        status = str(request.query.get("status", "all"))[:16]
        return await bridge(lambda: api.alerts_log(page, status))

    async def api_alerts_stats(request: web.Request) -> web.Response:
        return await bridge(api.alerts_stats)

    async def api_alert_test(request: web.Request) -> web.Response:
        return await bridge(api.alert_test)

    async def api_alert_resend(request: web.Request) -> web.Response:
        body = await body_json(request)
        h = str(body.get("hash") or "")[:80]
        return await bridge(lambda: api.alert_resend(h))

    async def api_alert_template(request: web.Request) -> web.Response:
        return await bridge(api.template_preview)

    # ── الوحدة 6: الإعدادات ─────────────────────────────────────────────
    async def api_ops(request: web.Request) -> web.Response:
        # قراءة ذاكرة مباشرة — صفر جسر (أسرع مسار ممكن)
        return _json(await api.ops_status())

    async def api_ops_toggle(request: web.Request) -> web.Response:
        body = await body_json(request)
        flag = str(body.get("flag") or "")[:16]
        enabled = bool(body.get("enabled"))
        return await bridge(lambda: api.ops_toggle(flag, enabled))

    async def api_ops_restart(request: web.Request) -> web.Response:
        return await bridge(api.request_restart)

    async def api_ops_env(request: web.Request) -> web.Response:
        return await bridge(api.ops_env)

    async def api_ops_errors(request: web.Request) -> web.Response:
        return await bridge(api.ops_errors)

    async def api_ops_audit(request: web.Request) -> web.Response:
        try:
            page = int(request.query.get("page", "1"))
        except ValueError:
            page = 1
        return await bridge(lambda: api.ops_audit(page))

    async def api_ops_backup(request: web.Request) -> web.Response:
        from datetime import datetime
        fname = f"br7-backup-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
        return await bridge_attach(api.ops_backup,
                                   {"Content-Disposition": f'attachment; filename="{fname}"'})

    # ── الوسيط: مصادقة + CSRF + rate limit + ترويسات ────────────────────
    @web.middleware
    async def dash_middleware(request: web.Request, handler: Callable) -> web.StreamResponse:
        path = request.path
        if not path.startswith("/admin"):
            return await handler(request)

        ip = _client_ip(request)

        if path.startswith("/admin/api"):
            if path not in _LOGIN_PUBLIC_PATHS:
                if not DASH.api_rate.allow(ip):
                    resp = _json({"error": "rate_limited"}, 429)
                    resp.headers["Retry-After"] = "30"
                    return resp

            token = _session_token(request)
            valid = DASH.sessions.validate(token)
            public = path in _LOGIN_PUBLIC_PATHS
            if not valid and not public:
                return _json({"error": "unauthorized"}, 401)
            if request.method in ("POST", "PUT", "DELETE") and not public:
                supplied = request.headers.get("X-CSRF-Token", "")
                try:
                    from security import constant_time_equals
                    csrf_ok = bool(token) and constant_time_equals(supplied, token)
                except Exception:
                    csrf_ok = False
                if not csrf_ok:
                    return _json({"error": "csrf_required"}, 403)

        resp = await handler(request)
        try:
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            if path == "/admin" or path.startswith("/admin/assets"):
                resp.headers.setdefault("X-Frame-Options", "DENY")
                resp.headers.setdefault(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
                )
        except Exception:
            pass
        return resp

    app.middlewares.append(dash_middleware)

    # ── المسارات ────────────────────────────────────────────────────────
    r = app.router
    r.add_get("/admin", admin_index)
    r.add_get("/admin/assets/{name}", admin_asset)

    r.add_get("/admin/api/session", api_session)
    r.add_post("/admin/api/login", api_login)
    r.add_post("/admin/api/logout", api_logout)

    r.add_get("/admin/api/overview", api_overview)

    r.add_get("/admin/api/accounts", api_accounts)
    r.add_post("/admin/api/accounts/test", api_account_test)
    r.add_post("/admin/api/accounts/reconnect", api_account_reconnect)
    r.add_post("/admin/api/accounts/membership", api_account_membership)

    r.add_get("/admin/api/keywords", api_keywords)
    r.add_post("/admin/api/keywords/save", api_keywords_save)
    r.add_post("/admin/api/keywords/import", api_keywords_import)
    r.add_get("/admin/api/keywords/export", api_keywords_export)
    r.add_get("/admin/api/keywords/stats", api_keyword_stats)
    r.add_post("/admin/api/playground", api_playground)

    r.add_get("/admin/api/groups", api_groups)
    r.add_post("/admin/api/groups/add", api_groups_add)
    r.add_post("/admin/api/groups/toggle", api_groups_toggle)
    r.add_post("/admin/api/groups/remove", api_groups_remove)

    r.add_get("/admin/api/alerts", api_alerts)
    r.add_get("/admin/api/alerts/stats", api_alerts_stats)
    r.add_post("/admin/api/alerts/test", api_alert_test)
    r.add_post("/admin/api/alerts/resend", api_alert_resend)
    r.add_get("/admin/api/alerts/template", api_alert_template)

    r.add_get("/admin/api/ops", api_ops)
    r.add_post("/admin/api/ops/toggle", api_ops_toggle)
    r.add_post("/admin/api/ops/restart", api_ops_restart)
    r.add_get("/admin/api/ops/env", api_ops_env)
    r.add_get("/admin/api/ops/errors", api_ops_errors)
    r.add_get("/admin/api/ops/audit", api_ops_audit)
    r.add_get("/admin/api/ops/backup", api_ops_backup)


async def api_db_audit(api: DashboardAPI, action: str, object_id: str,
                       new_value: str) -> None:
    """تدقيق على الحلقة الرئيسية (يُستدعى fire-and-forget من خيط الصحة)."""
    from dashboard.api import _audit
    db = getattr(api, "db", None)
    if db is None:
        return
    await _audit(db, action, "session", object_id, new_value=new_value)
