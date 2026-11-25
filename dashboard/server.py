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
import hmac
import json as jsonlib
import os
import secrets as _secrets
import time
from typing import Any, Callable, Optional

from aiohttp import web

from dashboard.api import DashboardAPI, DashboardHTTPError, KW_FILE
from dashboard.state import (
    DEFAULT_API_RATE_PER_MIN,
    DEFAULT_LOGIN_LOCKOUT_SECONDS,
    DEFAULT_LOGIN_RATE_PER_MIN,
    DEFAULT_SESSION_TTL_SECONDS,
    DEFAULT_MAX_SESSIONS,
    DASH,
)
from dashboard.auth.routes import setup_auth_routes

COOKIE_NAME = "br7dash"
# v15.2.0-fix: مرآة CSRF غير HttpOnly — النمط المزدوج يتطلب أن يقرأ JS التوكِن
# ليرسله في الترويسة؛ HttpOnly وحدها تجعل كل عمليات التعديل 403 في المتصفح الحقيقي.
CSRF_COOKIE_NAME = "br7dash_csrf"
_LOGIN_PUBLIC_PATHS = {"/admin/api/login", "/admin/api/session"}

# v16.1.0 (المرحلة 1): مسارات مسموحة أثناء بوابة «كلمة المرور الافتراضية»
_MUST_CHANGE_ALLOWED = frozenset({
    "/admin/api/auth/me",
    "/admin/api/auth/change-password",
    "/admin/api/logout",
    "/admin/api/session",
    "/admin/api/login",
})

# v16.1.0: خريطة صلاحيات المسارات على الـBackend (§6.2) — القراءة للكل
# (عرض البيانات)، والتعديل حسب الوحدة؛ المصادقة تدير نفسها داخلياً.
_ROUTE_PERM_PREFIX = (
    ("/admin/api/accounts", "accounts.manage"),
    ("/admin/api/groups", "sources.manage"),
    ("/admin/api/blocked", "sources.manage"),
    ("/admin/api/people", "sources.manage"),
    ("/admin/api/keywords", "filtering.manage"),
    ("/admin/api/playground", "filtering.manage"),
    ("/admin/api/rules", "filtering.manage"),
    ("/admin/api/alerts", "alerts.manage"),
    ("/admin/api/users", "users.manage"),
)


def _route_perm(path: str, method: str) -> str:
    """الصلاحية المطلوبة لمسار — '' تعني أي جلسة مصادقة (عرض)."""
    m = (method or "").upper()
    if (path.startswith("/admin/api/auth/") or path.startswith("/admin/api/login")
            or path.startswith("/admin/api/session")):
        return ""
    if path.startswith("/admin/api/ops/backup"):
        return "data.manage"
    if path.startswith("/admin/api/ops"):
        return "ops.dangerous" if m in ("POST", "PUT", "DELETE") else ""
    if m in ("POST", "PUT", "DELETE"):
        for prefix, perm in _ROUTE_PERM_PREFIX:
            if path.startswith(prefix):
                return perm
    return ""


def _role_allows(role: str, perm: str) -> bool:
    try:
        from dashboard.auth.permissions import role_allows as _ra
        return bool(_ra(role, perm))
    except Exception:
        return False

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


def _cfg_username() -> str:
    """اسم المستخدم للعرض/الممثل التدقيقي القديم فقط — التحقق فعلياً عبر
    panel_users (Argon2id) منذ v16.1.0؛ لا كلمات مرور هنا إطلاقاً."""
    return str(_cfg_val("DASHBOARD_USERNAME", "bashar") or "bashar")


# ═════════════════════════════════════════════════════════════════════════
# التركيب
# ═════════════════════════════════════════════════════════════════════════
def setup_dashboard(app: web.Application, bot: Any) -> None:
    """يُستدعى من خيط الصحة قبل AppRunner.setup — يضيف المسارات والوسيط."""
    from pathlib import Path

    # v15.2.0-fix: BOT_VERSION ثابت على مستوى الوحدة (ليس على CFG) — كانت
    # الشارة تعرض قيمة افتراضية قديمة دائماً.
    try:
        from config import BOT_VERSION as _dash_bot_version
    except Exception:  # pragma: no cover
        _dash_bot_version = "15.2.0"
    DASH.version = str(_dash_bot_version)
    DASH.username = _cfg_username()

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
    # v16.1.0: توصيل إعدادات الجلسة الفعلية بالمخزن (كانت منفصلة في v15.1)
    try:
        DASH.sessions.configure(
            ttl_seconds,
            int(_cfg_val("DASHBOARD_MAX_SESSIONS", DEFAULT_MAX_SESSIONS)))
    except Exception:
        pass
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

    async def _bridge_call_long(coro_factory: Callable[[], Any], timeout: float = 180.0) -> Any:
        """جسر بمهلة طويلة للعمليات التي تتصل بتيليجرام فعلياً (spawn/إعادة
        اتصال مراقب جديد — حتى 120s connect + هامش)."""
        loop = getattr(bot, "_main_loop", None)
        if loop is None or loop.is_closed():
            raise _BridgeError(503, {"error": "bot_core_unavailable",
                                     "note": "نواة البوت ليست جاهزة بعد — أعد المحاولة بعد دقيقة"})
        fut = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=timeout)
        except DashboardHTTPError as e:
            raise _BridgeError(e.status, e.payload)
        except asyncio.TimeoutError:
            raise _BridgeError(504, {"error": "bridge_timeout"})
        except Exception as e:  # noqa: BLE001
            raise _BridgeError(500, {"error": "internal_error", "detail": type(e).__name__})

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

    def _audit_bg(action: str, object_id: str = "", new_value: str = "",
                  actor: str = "", ip: str = "", ua: str = "") -> None:
        """تدقيق بدون انتظار — فشل-آمن، لا يحجب الاستجابة أبداً.
        v16.1.0: دعم actor صريح + إثراء IP/UA (§13)."""
        loop = getattr(bot, "_main_loop", None)
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                api_db_audit(api, action, object_id, new_value,
                             actor=actor, ip=ip, user_agent=ua), loop
            )
        except Exception:
            pass

    # ── الأصول ──────────────────────────────────────────────────────────
    async def admin_index(request: web.Request) -> web.Response:
        body = DASH.assets.get("index.html", b"")
        # v15.2.0: بوابة بمنفذ مُحوَّل (XTransformPort) — تُلحق المعلمة بروابط
        # الأصول لتبلو اللوحة كاملة عبر البوابة؛ في الإنتاج لا معلمة فلا تغيير.
        qport = request.query.get("XTransformPort", "")
        if qport and body:
            try:
                html = body.decode("utf-8").replace(
                    '/admin/assets/style.css',
                    f'/admin/assets/style.css?XTransformPort={qport}',
                ).replace(
                    '/admin/assets/app.js',
                    f'/admin/assets/app.js?XTransformPort={qport}',
                )
                body = html.encode("utf-8")
            except Exception:
                pass
        resp = web.Response(body=body, content_type="text/html", charset="utf-8")
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

    # v16.1.0: المصادقة نُقلت إلى dashboard/auth/routes.py
    # (جلسات DB + أدوار RBAC + TOTP + CSRF مفصول) — يُسجَّل أسفل setup_dashboard

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

    # ── الوحدة 2-ب: ربط حساب جديد (v15.2.0) ─────────────────────────────
    # AccountLinkManager يعيش على حلقة الصحة؛ كل عمليات DB/Telethon للمراقبة
    # تُجسَّر إلى الحلقة الرئيسية عبر الجسر أعلاه. المصادقة وCSRF والحد
    # الزمني لهذه المسارات يأتيان من dash_middleware تلقائياً.
    from dashboard.crypto import encrypt_secret as _link_encrypt
    from dashboard.link_flow import AccountLinkManager as _LinkManager

    async def _link_find_existing(phone: str, tg_user_id):
        async def _q():
            return await api.db.get_panel_account_duplicate(phone, tg_user_id)
        return await _bridge_call(_q)

    async def _link_save_account(payload: dict):
        async def _q():
            return await api.db.insert_panel_account(
                phone=payload["phone"],
                display_name=payload.get("display_name") or "",
                username=payload.get("username") or "",
                tg_user_id=int(payload.get("tg_user_id") or 0),
                api_id=str(payload.get("api_id") or ""),
                api_hash_encrypted=_link_encrypt(str(payload.get("api_hash") or "")) if payload.get("api_hash") else "",
                session_encrypted=_link_encrypt(payload["session_string"]),
            )
        return await _bridge_call(_q)

    async def _link_activate(row_id: int, payload: dict):
        async def _q():
            row = await api.db.get_panel_account(int(row_id))
            if not row:
                return {"ok": False, "error": "الحساب المحفوظ غير موجود"}
            return await bot.spawn_panel_account(row)
        res = await _bridge_call_long(_q)
        if res and res.get("ok"):
            link_manager.mark_active(int(row_id))
        else:
            link_manager.mark_error(int(row_id), (res or {}).get("error") or "فشل تفعيل الحساب")

    link_manager = _LinkManager(
        _link_find_existing, _link_save_account, _link_activate,
        excluded_env_phones_cb=lambda: list(
            getattr(bot, "_excluded_env_phones", set()) or set()),
    )
    link_manager.start_sweeper()

    async def api_account_add_start(request: web.Request) -> web.Response:
        body = await body_json(request)
        phone = str(body.get("phone") or "")
        result = await link_manager.start(bot, phone)
        _audit_bg(
            "dashboard.account_add_start",
            object_id=str(phone)[:3] + "***" + str(phone)[-2:] if phone else "",
            new_value="ok" if result.get("ok") else str(result.get("error_code") or ""),
        )
        return _json(result)

    async def api_account_add_code(request: web.Request) -> web.Response:
        body = await body_json(request)
        result = await link_manager.submit_code(str(body.get("op_id") or ""), str(body.get("code") or ""))
        return _json(result)

    async def api_account_add_password(request: web.Request) -> web.Response:
        body = await body_json(request)
        result = await link_manager.submit_password(str(body.get("op_id") or ""), str(body.get("password") or ""))
        return _json(result)

    async def api_account_add_cancel(request: web.Request) -> web.Response:
        body = await body_json(request)
        result = await link_manager.cancel(str(body.get("op_id") or ""))
        _audit_bg("dashboard.account_add_cancel", object_id=str(body.get("op_id") or "")[:8])
        return _json(result)

    async def api_account_add_status(request: web.Request) -> web.Response:
        op_id = request.query.get("op_id", "")
        status = link_manager.status(op_id)
        if status is None:
            return _json({"error": "op_not_found", "note": "لا توجد عملية ربط بهذا المعرّف"}, 404)
        return _json({"ok": True, "op": status})

    # ── v16.6.0: بوابة الخدمة Vercel→المحرك لتدفق ربط الحسابات (§3.1/§3.3) ──
    # لوحة Vercel (بعد جلسة super_admin/admin + CSRF خاصة بها) تنداء مباشرة
    # مع link_manager عبر سر مشترك x-br7-command-secret (ENGINE_COMMAND_SECRET).
    # فشل-مغلق: بلا متغير البيئة → 503 لكل مسارات الخدمة. OTP/كلمة المرور
    # تعيش في ذاكرة link_manager حصراً ولا تُسجَّل هنا ولا تُعيد أبداً.
    # المسارات تحت /service/* خارج وسيط /admin — البوابة أدناه هي حمايتها.
    _svc_secret = str(os.getenv("ENGINE_COMMAND_SECRET", "") or "")
    _svc_fail = type(DASH.api_rate)(15)  # إخفاقات مصادقة الخدمة/دقيقة لكل IP

    async def _svc_guard(request: web.Request) -> Optional[web.Response]:
        if not _svc_secret:
            return _json({"ok": False, "error": "service_disabled",
                          "note": "ENGINE_COMMAND_SECRET غير مضبوط على المحرك"}, 503)
        supplied = request.headers.get("x-br7-command-secret", "")
        try:
            auth_ok = bool(supplied) and hmac.compare_digest(supplied, _svc_secret)
        except Exception:
            auth_ok = False
        if not auth_ok:
            ip = _client_ip(request)
            if not _svc_fail.allow(ip):
                resp = _json({"ok": False, "error": "rate_limited"}, 429)
                resp.headers["Retry-After"] = "60"
                return resp
            _audit_bg("service.auth_denied", object_id=ip[:64])
            return _json({"ok": False, "error": "forbidden"}, 403)
        return None

    async def _svc_body(request: web.Request) -> Optional[dict]:
        try:
            data = await request.json()
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _svc_mask_phone(phone: str) -> str:
        return (phone[:3] + "***" + phone[-2:]) if len(phone) >= 5 else "***"

    async def svc_link_start(request: web.Request) -> web.Response:
        g = await _svc_guard(request)
        if g:
            return g
        body = await _svc_body(request)
        if body is None:
            return _json({"ok": False, "error": "invalid_body"}, 400)
        phone = str(body.get("phone") or "")
        result = await link_manager.start(bot, phone)
        _audit_bg("service.account_add_start",
                  object_id=_svc_mask_phone(phone) if phone else "",
                  new_value="ok" if result.get("ok") else str(result.get("error_code") or ""))
        return _json(result)

    async def svc_link_code(request: web.Request) -> web.Response:
        g = await _svc_guard(request)
        if g:
            return g
        body = await _svc_body(request)
        if body is None:
            return _json({"ok": False, "error": "invalid_body"}, 400)
        result = await link_manager.submit_code(str(body.get("op_id") or ""), str(body.get("code") or ""))
        return _json(result)

    async def svc_link_password(request: web.Request) -> web.Response:
        g = await _svc_guard(request)
        if g:
            return g
        body = await _svc_body(request)
        if body is None:
            return _json({"ok": False, "error": "invalid_body"}, 400)
        result = await link_manager.submit_password(str(body.get("op_id") or ""), str(body.get("password") or ""))
        return _json(result)

    async def svc_link_cancel(request: web.Request) -> web.Response:
        g = await _svc_guard(request)
        if g:
            return g
        body = await _svc_body(request)
        if body is None:
            return _json({"ok": False, "error": "invalid_body"}, 400)
        op_id = str(body.get("op_id") or "")
        result = await link_manager.cancel(op_id)
        _audit_bg("service.account_add_cancel", object_id=op_id[:8])
        return _json(result)

    async def svc_link_status(request: web.Request) -> web.Response:
        g = await _svc_guard(request)
        if g:
            return g
        op_id = request.query.get("op_id", "")
        st = link_manager.status(op_id)
        if st is None:
            return _json({"ok": False, "error": "op_not_found"}, 404)
        return _json({"ok": True, "op": st})

    async def svc_row_delete(request: web.Request) -> web.Response:
        g = await _svc_guard(request)
        if g:
            return g
        body = await _svc_body(request)
        if body is None:
            return _json({"ok": False, "error": "invalid_body"}, 400)
        try:
            row_id = int(body.get("row_id"))
        except (TypeError, ValueError):
            return _json({"ok": False, "error": "invalid_row_id"}, 400)

        async def _do():
            return await bot.delete_panel_account(row_id)

        try:
            result = await _bridge_call(_do)
        except _BridgeError as e:
            return _json(e.payload, e.status)
        if result.get("ok"):
            _audit_bg("service.account_delete", object_id=str(row_id))
        return _json(result)

    async def svc_row_toggle(request: web.Request) -> web.Response:
        g = await _svc_guard(request)
        if g:
            return g
        body = await _svc_body(request)
        if body is None:
            return _json({"ok": False, "error": "invalid_body"}, 400)
        try:
            row_id = int(body.get("row_id"))
            enable = bool(body.get("enable"))
        except (TypeError, ValueError):
            return _json({"ok": False, "error": "invalid_row_id"}, 400)

        async def _do():
            row = await api.db.get_panel_account(row_id)
            if not row:
                return {"ok": False, "error": "الحساب غير موجود"}
            await api.db.update_panel_account(row_id, monitoring_enabled=enable)
            if not enable:
                await bot.stop_panel_account(row_id)
            else:
                row2 = dict(row)
                row2["monitoring_enabled"] = 1
                return await bot.spawn_panel_account(row2)
            return {"ok": True, "enable": enable}

        try:
            result = await _bridge_call_long(_do)
        except _BridgeError as e:
            return _json(e.payload, e.status)
        _audit_bg("service.account_toggle", object_id=str(row_id), new_value=str(enable))
        return _json(result)

    async def svc_row_stop(request: web.Request) -> web.Response:
        # v17.1.0: إيقاف مراقب صف حسب row_id — idempotent (صحة التسوية):
        # مراقب غير موجود = نجاح صامت (لا حالة فاشلة وهمية ولا خطأ زائف).
        g = await _svc_guard(request)
        if g:
            return g
        body = await _svc_body(request)
        if body is None:
            return _json({"ok": False, "error": "invalid_body"}, 400)
        try:
            row_id = int(body.get("row_id"))
        except (TypeError, ValueError):
            return _json({"ok": False, "error": "invalid_row_id"}, 400)

        async def _do():
            stopped = await bot.stop_panel_account(int(row_id))
            return {"ok": True, "stopped": bool(stopped)}

        try:
            result = await _bridge_call(_do)
        except _BridgeError as e:
            return _json(e.payload, e.status)
        _audit_bg("service.account_stop", object_id=str(row_id))
        return _json(result)

    # ── v17.2.0: التنفيذ الفوري على العملية الحارة (§3.1/§3.2) + فحص المصادر (§3.4) + مقعد الاختبار (§3.5) ──

    async def svc_bot_ops(request: web.Request) -> web.Response:
        """§3.1 — تطبيق فوري: إيقاف/استئناف الفلترة أو التنبيهات على العملية
        الحارة + تثبيت دائم عبر قاعدة المحرك (نفس مسار ops_toggle)."""
        g = await _svc_guard(request)
        if g:
            return g
        body = await _svc_body(request)
        if body is None:
            return _json({"ok": False, "error": "invalid_body"}, 400)
        flag = str(body.get("flag") or "")
        paused = bool(body.get("paused"))
        if flag not in ("filtering", "alerts"):
            return _json({"ok": False, "error": "unknown_flag"}, 400)
        if flag == "filtering":
            DASH.ops.set_filtering(paused)
        else:
            DASH.ops.set_alerts(paused)

        async def _do():
            payload = jsonlib.dumps({
                "filtering_paused": DASH.ops.filtering_paused,
                "alerts_paused": DASH.ops.alerts_paused,
            })
            await bot.db._execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                "updated_at = CURRENT_TIMESTAMP",
                ("dashboard:ops", payload),
            )
            await bot.db._commit()
            return True

        persisted = False
        try:
            persisted = bool(await _bridge_call(_do))
        except _BridgeError:
            persisted = False
        _audit_bg("service.bot_ops_instant", object_id=flag,
                  new_value="paused" if paused else "active")
        return _json({"ok": True, "flag": flag, "paused": paused,
                      "applied": "instant", "persisted": persisted,
                      "ops": DASH.ops.snapshot()})

    async def svc_keywords_reload(request: web.Request) -> web.Response:
        """§3.2 — إعادة بناء حية لسجل الكلمات: قراءة dashboard:keywords_json
        → كتابة ملف المخصصة → reload_keywords → إعادة بناء FastGate."""
        g = await _svc_guard(request)
        if g:
            return g

        async def _do():
            val = await DashboardAPI(bot)._custom_keywords_row()
            if not val:
                return {"ok": False, "error": "no_custom_keywords",
                        "note": "لا يوجد سجل كلمات مخصص في app_settings"}
            data = jsonlib.loads(val)
            if not isinstance(data, dict):
                return {"ok": False, "error": "invalid_keywords_json"}
            KW_FILE.write_text(jsonlib.dumps(data, ensure_ascii=False), encoding="utf-8")
            flt = getattr(bot, "filter", None)
            reloaded = False
            reload = getattr(flt, "reload_keywords", None) if flt is not None else None
            if reload is not None:
                ret = reload(str(KW_FILE))
                if asyncio.iscoroutine(ret):
                    ret = await ret
                reloaded = True
            try:
                from fast_gate import get_fast_gate as _fgg
                _fg = _fgg()
                if _fg is not None:
                    _fg.schedule_rebuild()
            except Exception:
                pass
            gate_stats = None
            try:
                from fast_gate import get_fast_gate as _fgg2
                _fg2 = _fgg2()
                if _fg2 is not None:
                    gate_stats = {"patterns": _fg2.stats()[0], "engine": _fg2.stats()[1]}
            except Exception:
                pass
            return {"ok": True, "reloaded": reloaded, "groups": len(data),
                    "fast_gate": gate_stats}

        try:
            result = await _bridge_call_long(_do, timeout=30.0)
        except _BridgeError as e:
            return _json(e.payload, e.status)
        _audit_bg("service.keywords_hot_reload",
                  new_value="ok" if result.get("ok") else str(result.get("error") or ""))
        return _json(result)

    async def svc_sources_check(request: web.Request) -> web.Response:
        """§3.4 — فحص وصول حقيقي لمصدر عبر عميل تيليجرام الحي:
        الوصول للكيان + حالة العضوية + صلاحية الإرسال."""
        g = await _svc_guard(request)
        if g:
            return g
        body = await _svc_body(request)
        if body is None:
            return _json({"ok": False, "error": "invalid_body"}, 400)
        chat_id_raw = str(body.get("chat_id") or "").strip()
        username = str(body.get("username") or "").strip().lstrip("@")
        if not chat_id_raw and not username:
            return _json({"ok": False, "error": "missing_target"}, 400)

        data: dict = {}
        if chat_id_raw:
            try:
                data["chat_id"] = int(chat_id_raw)
            except ValueError:
                # قيمة غير رقمية في حقل الرقم تُعامل كاسم مستخدم (صادق لا صامت)
                data["username"] = chat_id_raw.lstrip("@")
        if username:
            data["username"] = username

        async def _do():
            from monitors import resolve_chat_entity as _rce
            client = getattr(bot, "main_client", None)
            if client is None:
                for mon in getattr(bot, "monitors", []) or []:
                    if getattr(mon, "is_connected", False) and getattr(mon, "client", None):
                        client = mon.client
                        break
            if client is None:
                return {"ok": False, "error": "no_live_client",
                        "note": "لا يوجد عميل تيليجرام متصل حالياً"}
            try:
                entity = await asyncio.wait_for(_rce(client, data), timeout=25.0)
            except asyncio.TimeoutError:
                return {"ok": False, "error": "resolve_timeout"}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": "resolve_failed",
                        "error_message": f"{type(e).__name__}: {str(e)[:150]}"}
            if entity is None:
                return {"ok": True, "reachable": False, "member_status": "unknown",
                        "note": "تعذر الوصول للكيان من الحساب المراقب الحي"}
            ent_info = {
                "id": getattr(entity, "id", None),
                "title": getattr(entity, "title", None) or getattr(entity, "first_name", None) or "",
                "username": ("@" + entity.username) if getattr(entity, "username", None) else "",
                "type": type(entity).__name__,
            }
            member_status = "unknown"
            can_send = None
            err = None
            try:
                perms = await asyncio.wait_for(client.get_permissions(entity, "me"), timeout=15.0)
                member_status = "member" if getattr(perms, "is_member", False) else "restricted"
                rights = getattr(perms, "rights", None)
                if rights is not None:
                    can_send = not bool(getattr(rights, "send_messages", False))
            except Exception as e:  # noqa: BLE001
                name = type(e).__name__
                if "UserNotParticipant" in name:
                    member_status = "not_member"
                else:
                    err = f"{name}: {str(e)[:150]}"
            return {"ok": True, "reachable": True, "entity": ent_info,
                    "member_status": member_status, "can_send": can_send, "error": err}

        try:
            result = await _bridge_call_long(_do, timeout=45.0)
        except _BridgeError as e:
            return _json(e.payload, e.status)
        if result.get("ok") and result.get("reachable"):
            _audit_bg("service.source_check",
                      object_id=str((result.get("entity") or {}).get("id") or username or chat_id_raw)[:64],
                      new_value=str(result.get("member_status") or ""))
        return _json(result)

    async def svc_engine_analyze(request: web.Request) -> web.Response:
        """§3.5 — مقعد اختبار التحليل: تحليل حقيقي عبر الفلتر الحي + المصنّف
        + البوابة السريعة. قراءة-حسابية بحتة بلا أي أثر جانبي."""
        g = await _svc_guard(request)
        if g:
            return g
        body = await _svc_body(request)
        if body is None:
            return _json({"ok": False, "error": "invalid_body"}, 400)
        text = str(body.get("text") or "")
        if not text.strip():
            return _json({"ok": False, "error": "empty_text"}, 400)
        text = text[:2000]

        async def _do():
            t0 = time.perf_counter()
            flt = getattr(bot, "filter", None)
            if flt is None or not hasattr(flt, "analyze"):
                return {"ok": False, "error": "filter_unavailable"}
            verdict = await asyncio.wait_for(flt.analyze(text), timeout=8.0)
            cls: dict = {"classified": False}
            try:
                from classifier import classify_text as _ct
                cls = _ct(text)
            except Exception:
                pass
            gate = None
            try:
                from fast_gate import get_fast_gate as _fgg
                _fg = _fgg()
                if _fg is not None:
                    gate = {"any_hit": bool(_fg.any_hit(text)),
                            "patterns": _fg.stats()[0], "engine": _fg.stats()[1]}
            except Exception:
                pass
            latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            v = dict(verdict) if isinstance(verdict, dict) else {}
            return {
                "ok": True,
                "verdict": {
                    "decision": v.get("decision"),
                    "valid": v.get("valid"),
                    "confidence": v.get("confidence"),
                    "score": v.get("score"),
                    "reasons": v.get("reasons"),
                    "keyword": v.get("keyword"),
                    "indirect": v.get("indirect"),
                    "urgent": v.get("urgent"),
                    "negation_detected": v.get("negation_detected"),
                    "context_type": v.get("context_type"),
                    "intent_verb": v.get("intent_verb"),
                    "academic_object": v.get("academic_object"),
                    "urgency_marker": v.get("urgency_marker"),
                    "analysis_time_ms": v.get("analysis_time_ms"),
                },
                "classification": cls,
                "fast_gate": gate,
                "latency_ms": latency_ms,
            }

        try:
            result = await _bridge_call_long(_do, timeout=15.0)
        except _BridgeError as e:
            return _json(e.payload, e.status)
        return _json(result)

    async def svc_db_grant_read(request: web.Request) -> web.Response:
        """v17.2.0 — منح قراءة للوحة على جداول محددة بقائمة بيضاء صارمة.
        لا SQL حر إطلاقاً: `GRANT SELECT ON TABLE public.<t> TO service_role`
        على أسماء جداول من ALLOWED فقط + تحقق حي. idempotent (GRANT متكرر آمن)."""
        g = await _svc_guard(request)
        if g:
            return g
        body = await _svc_body(request)
        if body is None:
            return _json({"ok": False, "error": "invalid_body"}, 400)
        ALLOWED = ("sender_stats", "blocked_chats", "notifications")
        tables = body.get("tables") if isinstance(body.get("tables"), list) else [body.get("table")]
        tables = [str(t).strip() for t in tables if t]
        if not tables or any(t not in ALLOWED for t in tables):
            return _json({"ok": False, "error": "table_not_allowed",
                          "allowed": list(ALLOWED)}, 400)

        async def _do():
            out = []
            for t in tables:
                try:
                    await bot.db._execute(f'GRANT SELECT ON TABLE public."{t}" TO service_role')
                    await bot.db._commit()
                    out.append({"table": t, "granted": True})
                except Exception as e:  # noqa: BLE001
                    out.append({"table": t, "granted": False,
                                "error": f"{type(e).__name__}: {str(e)[:150]}"})
            return {"ok": True, "results": out}

        try:
            result = await _bridge_call(_do)
        except _BridgeError as e:
            return _json(e.payload, e.status)
        _audit_bg("service.db_grant_read", new_value=",".join(tables)[:120])
        return _json(result)

    async def api_account_row_toggle(request: web.Request) -> web.Response:
        body = await body_json(request)
        try:
            row_id = int(body.get("row_id"))
            enable = bool(body.get("enable"))
        except (TypeError, ValueError):
            return _json({"error": "invalid_row_id"}, 400)

        async def _do():
            row = await api.db.get_panel_account(row_id)
            if not row:
                return {"ok": False, "error": "الحساب غير موجود"}
            await api.db.update_panel_account(row_id, monitoring_enabled=enable)
            if not enable:
                await bot.stop_panel_account(row_id)
            else:
                row2 = dict(row)
                row2["monitoring_enabled"] = 1
                return await bot.spawn_panel_account(row2)
            return {"ok": True, "enable": enable}

        try:
            result = await _bridge_call_long(_do)
        except _BridgeError as e:
            return _json(e.payload, e.status)
        _audit_bg("dashboard.account_toggle", object_id=str(row_id), new_value=str(enable))
        return _json(result)

    async def api_account_row_reconnect(request: web.Request) -> web.Response:
        body = await body_json(request)
        try:
            row_id = int(body.get("row_id"))
        except (TypeError, ValueError):
            return _json({"error": "invalid_row_id"}, 400)

        async def _do():
            return await bot.reconnect_account("panel", row_id)

        try:
            result = await _bridge_call_long(_do)
        except _BridgeError as e:
            return _json(e.payload, e.status)
        _audit_bg("dashboard.account_reconnect_row", object_id=str(row_id))
        return _json(result)

    async def api_account_row_delete(request: web.Request) -> web.Response:
        body = await body_json(request)
        try:
            row_id = int(body.get("row_id"))
        except (TypeError, ValueError):
            return _json({"error": "invalid_row_id"}, 400)

        async def _do():
            return await bot.delete_panel_account(row_id)

        try:
            result = await _bridge_call(_do)
        except _BridgeError as e:
            return _json(e.payload, e.status)
        if result.get("ok"):
            _audit_bg("dashboard.account_delete", object_id=str(row_id))
        return _json(result)

    # v16.2.0: حذف حساب env نهائياً (استبعاد دائم يدوم عبر النشرات)
    async def api_account_env_exclude(request: web.Request) -> web.Response:
        body = await body_json(request)
        prefix = str(body.get("prefix") or "")[:64]
        if not prefix:
            return _json({"error": "invalid_prefix"}, 400)

        async def _do():
            return await bot.exclude_env_account(prefix)

        try:
            result = await _bridge_call(_do)
        except _BridgeError as e:
            return _json(e.payload, e.status)
        if result.get("ok"):
            _audit_bg("dashboard.account_env_exclude",
                      str(prefix), new_value="excluded_permanently")
        return _json(result)

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

    # v15.7.1: عمليات الطابور والذاكرة — نفس الجسر والمصادقة والـCSRF
    async def api_ops_purge_queue(request: web.Request) -> web.Response:
        return await bridge(api.ops_purge_queue)

    async def api_ops_memory_cleanup(request: web.Request) -> web.Response:
        return await bridge(api.ops_memory_cleanup)

    async def api_ops_env(request: web.Request) -> web.Response:
        return await bridge(api.ops_env)

    async def api_ops_errors(request: web.Request) -> web.Response:
        return await bridge(api.ops_errors)

    async def api_ops_audit(request: web.Request) -> web.Response:
        try:
            page = int(request.query.get("page", "1"))
        except ValueError:
            page = 1
        q = str(request.query.get("q", ""))[:64]
        action = str(request.query.get("action", ""))[:60]
        try:
            since_hours = int(request.query.get("hours", "0"))
        except ValueError:
            since_hours = 0
        return await bridge(lambda: api.ops_audit(page, q=q, action=action,
                                                  since_hours=since_hours))

    async def api_ops_backup(request: web.Request) -> web.Response:
        from datetime import datetime
        fname = f"br7-backup-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
        return await bridge_attach(api.ops_backup,
                                   {"Content-Disposition": f'attachment; filename="{fname}"'})

    # ── الوحدة 7: الحظر (v15.7.0) ────────────────────────────────────────
    async def api_blocked(request: web.Request) -> web.Response:
        try:
            page = int(request.query.get("page", "1"))
        except ValueError:
            page = 1
        kind = str(request.query.get("kind", "sender"))[:10]
        q = str(request.query.get("q", ""))[:64]
        return await bridge(lambda: api.blocked_list(kind, q, page))

    async def api_blocked_add(request: web.Request) -> web.Response:
        body = await body_json(request)
        raw = str(body.get("input") or "")[:200]
        kind = str(body.get("kind") or "sender")[:10]
        reason = str(body.get("reason") or "")[:200]
        return await bridge(lambda: api.blocked_add(raw, kind, reason))

    async def api_blocked_remove(request: web.Request) -> web.Response:
        body = await body_json(request)
        kind = str(body.get("kind") or "")[:10]
        try:
            entity_id = int(body.get("entity_id"))
        except (TypeError, ValueError):
            return _json({"error": "invalid_entity_id"}, 400)
        return await bridge(lambda: api.blocked_remove(kind, entity_id))

    async def api_blocked_bulk(request: web.Request) -> web.Response:
        body = await body_json(request)
        kind = str(body.get("kind") or "")[:10]
        ids = body.get("ids") if isinstance(body.get("ids"), list) else []
        return await bridge(lambda: api.blocked_bulk_remove(kind, ids))

    # ── الوحدة 8: الأشخاص (v15.7.0) ──────────────────────────────────────
    async def api_people(request: web.Request) -> web.Response:
        try:
            page = int(request.query.get("page", "1"))
        except ValueError:
            page = 1
        q = str(request.query.get("q", ""))[:64]
        flag = str(request.query.get("flag", "all"))[:10]
        return await bridge(lambda: api.people_list(q, flag, page))

    async def api_people_action(request: web.Request) -> web.Response:
        body = await body_json(request)
        action = str(body.get("action") or "")[:10]
        note = str(body.get("note") or "")[:200]
        try:
            sender_id = int(body.get("sender_id"))
        except (TypeError, ValueError):
            return _json({"error": "invalid_sender_id"}, 400)
        return await bridge(lambda: api.person_action(action, sender_id, note))

    # ── الوحدة 9: القواعد (v15.7.0) ──────────────────────────────────────
    async def api_rules(request: web.Request) -> web.Response:
        return await bridge(api.rules_list)

    async def api_rules_add(request: web.Request) -> web.Response:
        body = await body_json(request)
        return await bridge(lambda: api.rule_add(body))

    async def api_rules_edit(request: web.Request) -> web.Response:
        body = await body_json(request)
        try:
            rule_id = int(body.get("rule_id"))
        except (TypeError, ValueError):
            return _json({"error": "invalid_rule_id"}, 400)
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        return await bridge(lambda: api.rule_edit(rule_id, payload))

    async def api_rules_toggle(request: web.Request) -> web.Response:
        body = await body_json(request)
        try:
            rule_id = int(body.get("rule_id"))
            enabled = bool(body.get("enabled"))
        except (TypeError, ValueError):
            return _json({"error": "invalid_rule_id"}, 400)
        return await bridge(lambda: api.rule_toggle(rule_id, enabled))

    async def api_rules_delete(request: web.Request) -> web.Response:
        body = await body_json(request)
        try:
            rule_id = int(body.get("rule_id"))
        except (TypeError, ValueError):
            return _json({"error": "invalid_rule_id"}, 400)
        return await bridge(lambda: api.rule_delete(rule_id))

    async def api_rules_export(request: web.Request) -> web.Response:
        return await bridge_attach(api.rules_export,
                                   {"Content-Disposition": 'attachment; filename="br7-rules.json"'})

    async def api_rules_import(request: web.Request) -> web.Response:
        body = await body_json(request)
        text = str(body.get("json_text") or "")
        return await bridge(lambda: api.rules_import(text))

    # ── الوحدة 10: الميزات (v15.7.0) ─────────────────────────────────────
    async def api_features(request: web.Request) -> web.Response:
        return await bridge(api.features_list)

    async def api_features_toggle(request: web.Request) -> web.Response:
        body = await body_json(request)
        key = str(body.get("key") or "")[:60]
        enabled = bool(body.get("enabled"))
        return await bridge(lambda: api.feature_toggle(key, enabled))

    # ── حسابات اللوحة: تعديل الاسم (v15.7.0) ─────────────────────────────
    async def api_account_row_rename(request: web.Request) -> web.Response:
        body = await body_json(request)
        try:
            row_id = int(body.get("row_id"))
        except (TypeError, ValueError):
            return _json({"error": "invalid_row_id"}, 400)
        name = str(body.get("display_name") or "")[:80]
        return await bridge(lambda: api.panel_rename(row_id, name))

    # ── الوسيط: مصادقة + CSRF + rate limit + ترويسات ────────────────────
    @web.middleware
    async def dash_middleware(request: web.Request, handler: Callable) -> web.StreamResponse:
        path = request.path
        if not path.startswith("/admin"):
            return await handler(request)

        ip = _client_ip(request)
        request_id = _secrets.token_hex(6)

        if path.startswith("/admin/api"):
            # v16.1.0: حد المعدل يشمل المسارات العامة أيضاً (إلا login الذي
            # له حد خاص داخل المعالج) — إغلاق ثغرة /session غير المحدودة.
            if path != "/admin/api/login":
                if not DASH.api_rate.allow(ip):
                    resp = _json({"error": "rate_limited"}, 429)
                    resp.headers["Retry-After"] = "30"
                    resp.headers["X-Request-Id"] = request_id
                    return resp

            token = _session_token(request)
            sess = DASH.sessions.validate(token)
            request["dash_session"] = sess or {}
            public = path in _LOGIN_PUBLIC_PATHS
            if sess is None and not public:
                return _json({"error": "unauthorized"}, 401)
            if sess is not None:
                # بوابة «كلمة المرور الافتراضية»: لا وصول إلا لمسارات الحساب
                if sess.get("must_change") and path not in _MUST_CHANGE_ALLOWED:
                    resp = _json({"error": "password_change_required",
                                  "note": "غيّر كلمة المرور الافتراضية قبل أي عملية أخرى"},
                                 428)
                    resp.headers["X-Request-Id"] = request_id
                    return resp
                # v16.1.0: CSRF مفصول عن الجلسة (§6.3) — مزدوج الإرسال +
                # هاش مخزّن في الجلسة، مقارنة زمن-ثابت للطبقتين.
                if request.method in ("POST", "PUT", "DELETE") and not public:
                    supplied = request.headers.get("X-CSRF-Token", "")
                    cookie_c = request.cookies.get(CSRF_COOKIE_NAME, "")
                    try:
                        from security import constant_time_equals
                        double_ok = bool(supplied) and constant_time_equals(cookie_c, supplied)
                    except Exception:
                        double_ok = False
                    server_ok = DASH.sessions.csrf_matches(token, supplied)
                    if not (double_ok and server_ok):
                        resp = _json({"error": "csrf_required"}, 403)
                        resp.headers["X-Request-Id"] = request_id
                        return resp
                # RBAC على الـBackend (§6.2) — إخفاء الواجهة ليس حماية
                perm = _route_perm(path, request.method)
                if perm and not _role_allows(str(sess.get("role") or "viewer"), perm):
                    resp = _json({"error": "forbidden",
                                  "note": "لا تملك صلاحية لهذه العملية"}, 403)
                    resp.headers["X-Request-Id"] = request_id
                    return resp

        resp = await handler(request)
        try:
            resp.headers.setdefault("X-Request-Id", request_id)
        except Exception:
            pass
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

    # ── v16.1.0: مسارات المصادقة/المستخدمين/الأمان (المرحلة 1) ──────────
    class _AuthCtx:
        __slots__ = ("bot", "api", "dash", "bridge_call", "bridge_error",
                     "body_json", "client_ip", "is_https", "json",
                     "cookie_name", "csrf_cookie_name", "ttl_seconds",
                     "version", "audit_bg",
                     "env_username", "env_password")  # v16.2.0 شفاء المالك

    _actx = _AuthCtx()
    _actx.bot = bot
    _actx.api = api
    _actx.dash = DASH
    _actx.bridge_call = _bridge_call
    _actx.bridge_error = _BridgeError
    _actx.body_json = body_json
    _actx.client_ip = _client_ip
    _actx.is_https = _is_https
    _actx.json = _json
    _actx.cookie_name = COOKIE_NAME
    _actx.csrf_cookie_name = CSRF_COOKIE_NAME
    _actx.ttl_seconds = ttl_seconds
    _actx.version = DASH.version
    _actx.audit_bg = _audit_bg
    # v16.2.0: بيانات env لشفاء المالك الذاتي — تعيش في ذاكرة العملية فقط
    # (هي أصلاً في CFG بنفس العملية)، ولا تعود في أي استجابة HTTP.
    _actx.env_username = str(_cfg_val("DASHBOARD_USERNAME", "") or "").strip()
    _actx.env_password = str(_cfg_val("DASHBOARD_PASSWORD", "") or "")
    setup_auth_routes(app, _actx)

    app.middlewares.append(dash_middleware)

    # ── المسارات ────────────────────────────────────────────────────────
    r = app.router
    r.add_get("/admin", admin_index)
    r.add_get("/admin/assets/{name}", admin_asset)

    # v16.1.0: session/login/logout سُجلت داخل setup_auth_routes (أدناه)

    r.add_get("/admin/api/overview", api_overview)

    r.add_get("/admin/api/accounts", api_accounts)
    r.add_post("/admin/api/accounts/test", api_account_test)
    r.add_post("/admin/api/accounts/reconnect", api_account_reconnect)
    r.add_post("/admin/api/accounts/membership", api_account_membership)

    # v15.2.0: ربط حساب جديد + إدارة صفوف حسابات اللوحة
    r.add_post("/admin/api/accounts/add/start", api_account_add_start)
    r.add_post("/admin/api/accounts/add/code", api_account_add_code)
    r.add_post("/admin/api/accounts/add/password", api_account_add_password)
    r.add_post("/admin/api/accounts/add/cancel", api_account_add_cancel)
    r.add_get("/admin/api/accounts/add/status", api_account_add_status)
    r.add_post("/admin/api/accounts/row/toggle", api_account_row_toggle)
    r.add_post("/admin/api/accounts/row/reconnect", api_account_row_reconnect)
    r.add_post("/admin/api/accounts/row/delete", api_account_row_delete)
    r.add_post("/admin/api/accounts/env/exclude", api_account_env_exclude)

    # v16.6.0: مسارات الخدمة للوحة Vercel — بوابة السر الخاصة (خارج /admin)
    r.add_post("/service/accounts/link_start", svc_link_start)
    r.add_post("/service/accounts/link_code", svc_link_code)
    r.add_post("/service/accounts/link_password", svc_link_password)
    r.add_post("/service/accounts/link_cancel", svc_link_cancel)
    r.add_get("/service/accounts/link_status", svc_link_status)
    r.add_post("/service/accounts/row/delete", svc_row_delete)
    r.add_post("/service/accounts/row/toggle", svc_row_toggle)
    r.add_post("/service/accounts/row/stop", svc_row_stop)

    # v17.2.0: تنفيذ فوري على العملية الحارة + فحص مصادر + مقعد اختبار (§3.1/§3.2/§3.4/§3.5)
    r.add_post("/service/bot/ops", svc_bot_ops)
    r.add_post("/service/keywords/reload", svc_keywords_reload)
    r.add_post("/service/sources/check", svc_sources_check)
    r.add_post("/service/engine/analyze", svc_engine_analyze)
    r.add_post("/service/db/grant_read", svc_db_grant_read)

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
    r.add_post("/admin/api/ops/purge_queue", api_ops_purge_queue)
    r.add_post("/admin/api/ops/memory_cleanup", api_ops_memory_cleanup)
    r.add_get("/admin/api/ops/env", api_ops_env)
    r.add_get("/admin/api/ops/errors", api_ops_errors)
    r.add_get("/admin/api/ops/audit", api_ops_audit)
    r.add_get("/admin/api/ops/backup", api_ops_backup)

    # v15.7.0: مركز الإدارة المتكامل — الحظر/الأشخاص/القواعد/الميزات
    r.add_get("/admin/api/blocked", api_blocked)
    r.add_post("/admin/api/blocked/add", api_blocked_add)
    r.add_post("/admin/api/blocked/remove", api_blocked_remove)
    r.add_post("/admin/api/blocked/bulk", api_blocked_bulk)

    r.add_get("/admin/api/people", api_people)
    r.add_post("/admin/api/people/action", api_people_action)

    r.add_get("/admin/api/rules", api_rules)
    r.add_post("/admin/api/rules/add", api_rules_add)
    r.add_post("/admin/api/rules/edit", api_rules_edit)
    r.add_post("/admin/api/rules/toggle", api_rules_toggle)
    r.add_post("/admin/api/rules/delete", api_rules_delete)
    r.add_get("/admin/api/rules/export", api_rules_export)
    r.add_post("/admin/api/rules/import", api_rules_import)

    r.add_get("/admin/api/features", api_features)
    r.add_post("/admin/api/features/toggle", api_features_toggle)

    r.add_post("/admin/api/accounts/row/rename", api_account_row_rename)


async def api_db_audit(api: DashboardAPI, action: str, object_id: str,
                       new_value: str, actor: str = "",
                       ip: str = "", user_agent: str = "") -> None:
    """تدقيق على الحلقة الرئيسية (يُستدعى fire-and-forget من خيط الصحة).
    v16.1.0: actor صريح + إثراء IP/UA."""
    from dashboard.api import _audit
    db = getattr(api, "db", None)
    if db is None:
        return
    await _audit(db, action, "session", object_id, new_value=new_value,
                 actor=actor or None, ip=ip or None,
                 user_agent=user_agent or None)
