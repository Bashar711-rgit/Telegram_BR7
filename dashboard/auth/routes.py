# -*- coding: utf-8 -*-
"""dashboard.auth.routes — مسارات المصادقة والمستخدمين والأمان (المرحلة 1).

يُركَّب من dashboard/server.py عبر setup_auth_routes(app, ctx) — ctx يحمل
الجسر والمساعدات المعرّفة هناك (بلا إعادة تعريف). كل العمليات على DB تمر
عبر الجسر إلى الحلقة الرئيسية حصراً (نمط api.py نفسه).

المسارات الجديدة (شكل استجابة موحد §15):
  POST /admin/api/login                    (متوافق مع الشكل القديم + حقول جديدة)
  POST /admin/api/logout                   (كما هو — يبطل الجلسة في DB أيضاً)
  GET  /admin/api/session                  (كما هو + role/permissions/must_change)
  GET  /admin/api/auth/me                  {success,data,error,request_id}
  POST /admin/api/auth/change-password     — يدوّر كل الجلسات (§6.3)
  GET  /admin/api/auth/sessions            (?scope=all للـOwner)
  POST /admin/api/auth/sessions/revoke     {token_hash}
  POST /admin/api/auth/sessions/revoke-all
  POST /admin/api/auth/totp/setup          → otpauth_uri + secret (مرة واحدة)
  POST /admin/api/auth/totp/enable         {code} → رموز استرداد (مرة واحدة)
  POST /admin/api/auth/totp/disable        {password} أو {user_id} للـOwner
  GET  /admin/api/users                    (Owner)
  POST /admin/api/users/create             (Owner)
  POST /admin/api/users/update             (Owner) role/display_name/is_active
  POST /admin/api/users/reset-password     (Owner)
  POST /admin/api/users/delete             (Owner)

ممنوع في كل الاستجابات: هاشات كلمات المرور، أسرار TOTP، قيم توكنات خام.
"""
from __future__ import annotations

import asyncio
import secrets as _secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from aiohttp import web

from dashboard.auth import passwords, permissions, totp
from dashboard.auth import users as auth_users
from dashboard.auth.sessions import new_token, token_hash
from dashboard.state import DASH

try:  # Fernet الموجود — تشفير سر TOTP قبل التخزين
    from dashboard.crypto import decrypt_secret as _fernet_decrypt
    from dashboard.crypto import encrypt_secret as _fernet_encrypt
except Exception:  # pragma: no cover
    _fernet_encrypt = None
    _fernet_decrypt = None


def _iso(epoch: Any) -> str:
    try:
        e = float(epoch or 0)
        if e <= 0:
            return ""
        return datetime.fromtimestamp(e, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except Exception:
        return ""


def _safe_user(u: Dict[str, Any]) -> Dict[str, Any]:
    """قاموس مستخدم آمن للواجهة — بلا هاشات ولا أسرار."""
    return {
        "id": u.get("id"),
        "username": u.get("username"),
        "display_name": u.get("display_name") or "",
        "role": permissions.normalize_role(str(u.get("role") or "viewer")),
        "is_active": bool(int(u.get("is_active") or 0)),
        "must_change_password": bool(int(u.get("must_change_password") or 0)),
        "totp_enabled": bool(int(u.get("totp_enabled") or 0)),
        "created_at": _iso(u.get("created_at")),
        "updated_at": _iso(u.get("updated_at")),
        "last_login_at": _iso(u.get("last_login_at")),
    }


def _safe_session_row(r: Dict[str, Any], current_hash: str) -> Dict[str, Any]:
    th = str(r.get("token_hash") or "")
    revoked = r.get("revoked_at")
    return {
        # الهاش الكامل آمن للعرض لصاحب الجلسة (sha256 غير قابل للعكس) —
        # هو المفتاح المطلوب لعملية الإبطال.
        "token_hash": th,
        "token_hash8": th[:8],
        "username": r.get("username"),
        "role": r.get("role"),
        "ip": r.get("ip") or "",
        "user_agent": (r.get("user_agent") or "")[:120],
        "created_at": _iso(r.get("created_at")),
        "last_seen": _iso(r.get("last_seen")),
        "expires_at": _iso(r.get("expires_at")),
        "revoked": revoked is not None,
        "current": bool(th and th == current_hash),
    }


def setup_auth_routes(app: "web.Application", ctx: Any) -> None:
    """ctx: bot/api/dash/bridge_call/bridge_error/body_json/client_ip/
    is_https/json/cookie_name/csrf_cookie_name/ttl_seconds/version/audit_bg"""
    api = ctx.api
    bot = ctx.bot
    _json = ctx.json

    def _rid() -> str:
        return _secrets.token_hex(6)

    def _ok(data: Any, rid: str) -> web.Response:
        return _json({"success": True, "data": data, "error": None,
                      "request_id": rid})

    def _err(code: str, status: int, rid: str, note: str = "") -> web.Response:
        payload: Dict[str, Any] = {"success": False, "data": None,
                                   "error": code, "request_id": rid}
        if note:
            payload["note"] = note
        return _json(payload, status)

    def _sess(request: "web.Request") -> Dict[str, Any]:
        return request.get("dash_session") or {}

    def _ua(request: "web.Request") -> str:
        return (request.headers.get("User-Agent") or "")[:200]

    def _require(request: "web.Request", perm: str, rid: str) -> Optional[web.Response]:
        s = _sess(request)
        if not s:
            return _err("unauthorized", 401, rid)
        if perm and not permissions.role_allows(str(s.get("role") or "viewer"), perm):
            return _err("forbidden", 403, rid, "لا تملك صلاحية لهذه العملية")
        return None

    # ═════════════════════════════════════════════════════════════════════
    # الدخول/الخروج/فحص الجلسة — مسارات متوافقة مع v15.1
    # ═════════════════════════════════════════════════════════════════════
    async def api_session(request: "web.Request") -> web.Response:
        token = request.cookies.get(ctx.cookie_name)
        sess = DASH.sessions.validate(token)
        if sess:
            return _json({
                "authenticated": True,
                "username": sess.get("username"),
                "role": sess.get("role"),
                "permissions": permissions.permissions_list(str(sess.get("role") or "viewer")),
                "must_change_password": bool(sess.get("must_change")),
                "version": ctx.version,
                "ops": DASH.ops.snapshot(),
            })
        return _json({"authenticated": False, "version": ctx.version})

    async def api_login(request: "web.Request") -> web.Response:
        ip = ctx.client_ip(request)
        if DASH.guard.is_locked(ip):
            rem = DASH.guard.remaining_lock(ip)
            return _json({"error": "locked",
                          "note": f"محاولات فاشلة كثيرة — أعد المحاولة بعد {rem} ثانية"}, 429)
        if not DASH.login_rate.allow(ip):
            return _json({"error": "rate_limited", "note": "محاولات كثيرة جداً"}, 429)
        try:
            body = await ctx.body_json(request)
        except Exception as e:
            payload = getattr(e, "payload", None) or {"error": "invalid_body"}
            return _json(payload, getattr(e, "status", 400))
        username = str(body.get("username") or "").strip()[:64]
        password = str(body.get("password") or "")
        totp_code = str(body.get("totp_code") or "").strip()[:24]
        ukey = username.lower()
        if ukey and DASH.guard_user.is_locked(ukey):
            rem = DASH.guard_user.remaining_lock(ukey)
            return _json({"error": "locked",
                          "note": f"الحساب مقفل مؤقتاً — أعد المحاولة بعد {rem} ثانية"}, 429)

        user: Optional[Dict[str, Any]] = None

        async def _q():
            return await auth_users.get_user_by_username(api.db, username)

        try:
            user = await ctx.bridge_call(_q)
        except ctx.bridge_error as e:
            return _json(e.payload, e.status)
        except Exception:
            return _json({"error": "internal_error"}, 500)

        ok = False
        if user and int(user.get("is_active") or 0) == 1:
            ok = passwords.verify_password(password, str(user.get("password_hash") or ""))
        if not ok:
            DASH.guard.record_failure(ip)
            if ukey:
                DASH.guard_user.record_failure(ukey)
            ctx.audit_bg("dashboard.login_failed", "session", username[:32],
                         actor="dashboard:" + (username[:32] or "?"), ip=ip, ua=_ua(request))
            return _json({"error": "invalid_credentials"}, 401)

        # ── TOTP إن كان مفعّلاً ────────────────────────────────────────────
        recovery_update: Optional[str] = None
        if int(user.get("totp_enabled") or 0) == 1:
            verified = False
            if totp_code:
                secret = ""
                try:
                    secret = _fernet_decrypt(str(user.get("totp_secret_encrypted") or ""))
                except Exception:
                    secret = ""
                if secret and totp.verify(secret, totp_code):
                    verified = True
                else:
                    rc_ok, new_json, _rem = totp.consume_recovery_code(
                        user.get("recovery_codes"), totp_code)
                    if rc_ok:
                        verified = True
                        recovery_update = new_json
            if not verified:
                if not totp_code:
                    return _json({"error": "totp_required",
                                  "note": "أدخل رمز المصادقة الثنائية"}, 401)
                DASH.guard.record_failure(ip)
                DASH.guard_user.record_failure(ukey)
                ctx.audit_bg("dashboard.login_totp_failed", "session", username[:32],
                             actor="dashboard:" + username[:32], ip=ip, ua=_ua(request))
                return _json({"error": "invalid_credentials"}, 401)

        DASH.guard.record_success(ip)
        if ukey:
            DASH.guard_user.record_success(ukey)
        token = new_token()
        csrf = new_token()
        role = permissions.normalize_role(str(user.get("role") or "viewer"))
        must_change = bool(int(user.get("must_change_password") or 0))
        meta = DASH.sessions.create(token, csrf, int(user.get("id") or 0),
                                    username, role, ip=ip, ua=_ua(request),
                                    must_change=must_change)

        async def _persist():
            await auth_users.insert_session_row(api.db, meta)
            await auth_users.bump_last_login(api.db, int(user.get("id") or 0))
            if recovery_update is not None:
                await auth_users.update_user_fields(api.db, int(user.get("id") or 0),
                                                    recovery_codes=recovery_update)

        try:
            await ctx.bridge_call(_persist)
        except ctx.bridge_error:
            pass  # الجلسة صالحة من الذاكرة — المسح الدوري سيعيد المحاولة
        ctx.audit_bg("dashboard.login", "session", ip,
                     actor="dashboard:" + username[:32], ip=ip, ua=_ua(request))

        resp = _json({"ok": True, "username": username, "role": role,
                      "permissions": permissions.permissions_list(role),
                      "must_change_password": must_change,
                      "version": ctx.version, "csrf": csrf})
        secure = ctx.is_https(request)
        resp.set_cookie(ctx.cookie_name, token, max_age=ctx.ttl_seconds,
                        httponly=True, samesite="Strict", path="/admin", secure=secure)
        resp.set_cookie(ctx.csrf_cookie_name, csrf, max_age=ctx.ttl_seconds,
                        httponly=False, samesite="Strict", path="/admin", secure=secure)
        return resp

    async def api_logout(request: "web.Request") -> web.Response:
        token = request.cookies.get(ctx.cookie_name)
        sess = DASH.sessions.validate(token)
        th = str((sess or {}).get("token_hash") or "")
        if th:
            DASH.sessions.drop_by_hash(th)

            async def _q():
                return await auth_users.revoke_session_rows(api.db, [th])

            try:
                await ctx.bridge_call(_q)
            except ctx.bridge_error:
                pass
            ctx.audit_bg("dashboard.logout", "session",
                         str((sess or {}).get("username") or ""),
                         actor="dashboard:" + str((sess or {}).get("username") or ""),
                         ip=ctx.client_ip(request), ua=_ua(request))
        resp = _json({"ok": True})
        resp.del_cookie(ctx.cookie_name, path="/admin")
        resp.del_cookie(ctx.csrf_cookie_name, path="/admin")
        return resp

    # ═════════════════════════════════════════════════════════════════════
    # /auth/* — الحساب الشخصي والجلسات وTOTP
    # ═════════════════════════════════════════════════════════════════════
    async def api_auth_me(request: "web.Request") -> web.Response:
        rid = _rid()
        blocked = _require(request, "", rid)
        if blocked:
            return blocked
        s = _sess(request)

        async def _q():
            return await auth_users.get_user_by_id(api.db, int(s.get("user_id") or 0))

        try:
            user = await ctx.bridge_call(_q)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid, str(e.payload.get("error", "")))
        if not user:
            return _err("user_not_found", 404, rid)
        return _ok({
            "user": _safe_user(user),
            "permissions": permissions.permissions_list(str(user.get("role") or "viewer")),
            "session": {"ip": s.get("ip"), "created_at": _iso(s.get("created"))},
        }, rid)

    async def api_change_password(request: "web.Request") -> web.Response:
        rid = _rid()
        s = _sess(request)
        if not s:
            return _err("unauthorized", 401, rid)
        try:
            body = await ctx.body_json(request)
        except Exception as e:
            return _err("invalid_body", getattr(e, "status", 400), rid)
        current = str(body.get("current_password") or "")
        new = str(body.get("new_password") or "")

        async def _q():
            return await auth_users.get_user_by_id(api.db, int(s.get("user_id") or 0))

        try:
            user = await ctx.bridge_call(_q)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid)
        if not user:
            return _err("user_not_found", 404, rid)
        if not passwords.verify_password(current, str(user.get("password_hash") or "")):
            ctx.audit_bg("dashboard.password_change_failed", "panel_user",
                         str(user.get("username") or ""),
                         actor="dashboard:" + str(user.get("username") or ""),
                         ip=ctx.client_ip(request), ua=_ua(request))
            return _err("invalid_current_password", 400, rid)
        problem = passwords.password_problem(str(user.get("username") or ""),
                                             current, new)
        if problem:
            return _err("weak_password", 400, rid, problem)
        new_hash = passwords.hash_password(new)
        algo = passwords.backend_name()
        uid = int(user.get("id") or 0)

        async def _apply():
            await auth_users.update_user_fields(
                api.db, uid, password_hash=new_hash, password_algo=algo,
                must_change_password=0)
            await auth_users.revoke_all_user_sessions(api.db, uid)

        try:
            await ctx.bridge_call(_apply)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid)
        DASH.sessions.revoke_user(uid)  # كل الجلسات بما فيها الحالية — دورة إلزامية
        ctx.audit_bg("dashboard.password_change", "panel_user",
                     str(user.get("username") or ""), "ok",
                     actor="dashboard:" + str(user.get("username") or ""),
                     ip=ctx.client_ip(request), ua=_ua(request))
        resp = _ok({"message": "تم تغيير كلمة المرور — سجّل الدخول من جديد"}, rid)
        resp.del_cookie(ctx.cookie_name, path="/admin")
        resp.del_cookie(ctx.csrf_cookie_name, path="/admin")
        return resp

    async def api_sessions_list(request: "web.Request") -> web.Response:
        rid = _rid()
        s = _sess(request)
        if not s:
            return _err("unauthorized", 401, rid)
        scope = str(request.query.get("scope", "self"))[:8]
        is_owner = permissions.role_allows(str(s.get("role") or "viewer"), "users.manage")
        uid = None if (scope == "all" and is_owner) else int(s.get("user_id") or 0)

        async def _q():
            return await auth_users.list_user_sessions(api.db, uid)

        try:
            rows = await ctx.bridge_call(_q)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid)
        cur = str(s.get("token_hash") or "")
        return _ok({"sessions": [_safe_session_row(r, cur) for r in rows or []],
                    "scope": "all" if uid is None else "self"}, rid)

    async def api_sessions_revoke(request: "web.Request") -> web.Response:
        rid = _rid()
        s = _sess(request)
        if not s:
            return _err("unauthorized", 401, rid)
        try:
            body = await ctx.body_json(request)
        except Exception as e:
            return _err("invalid_body", getattr(e, "status", 400), rid)
        th = str(body.get("token_hash") or "")[:64]
        if not th:
            return _err("missing_token_hash", 400, rid)
        is_owner = permissions.role_allows(str(s.get("role") or "viewer"), "users.manage")
        uid_own = int(s.get("user_id") or 0)

        async def _q():
            rows = await auth_users.list_user_sessions(
                api.db, None if is_owner else uid_own, include_revoked=False)
            for r in rows:
                if str(r.get("token_hash") or "") == th:
                    return True
            return False

        try:
            owned = await ctx.bridge_call(_q)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid)
        if not owned:
            return _err("session_not_found", 404, rid)
        DASH.sessions.drop_by_hash(th)

        async def _rv():
            return await auth_users.revoke_session_rows(api.db, [th])

        try:
            await ctx.bridge_call(_rv)
        except ctx.bridge_error:
            pass
        ctx.audit_bg("dashboard.session_revoke", "session", th[:8],
                     actor="dashboard:" + str(s.get("username") or ""),
                     ip=ctx.client_ip(request), ua=_ua(request))
        return _ok({"revoked": th[:8]}, rid)

    async def api_sessions_revoke_all(request: "web.Request") -> web.Response:
        rid = _rid()
        s = _sess(request)
        if not s:
            return _err("unauthorized", 401, rid)
        uid = int(s.get("user_id") or 0)
        cur = str(s.get("token_hash") or "")
        hashes = DASH.sessions.revoke_user(uid, except_hash=cur)

        async def _q():
            await auth_users.revoke_session_rows(api.db, hashes)
            return await auth_users.revoke_all_user_sessions(api.db, uid, except_hash=cur)

        try:
            await ctx.bridge_call(_q)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid)
        ctx.audit_bg("dashboard.sessions_revoke_all", "session",
                     str(s.get("username") or ""),
                     actor="dashboard:" + str(s.get("username") or ""),
                     ip=ctx.client_ip(request), ua=_ua(request))
        return _ok({"revoked_count": len(hashes), "kept_current": True}, rid)

    # ── TOTP ─────────────────────────────────────────────────────────────
    async def api_totp_setup(request: "web.Request") -> web.Response:
        rid = _rid()
        s = _sess(request)
        if not s:
            return _err("unauthorized", 401, rid)
        if not totp.LIB_AVAILABLE:
            return _err("totp_unavailable", 503, rid,
                        "مكتبة pyotp غير مثبتة على الخادم — أضفها في requirements")
        uid = int(s.get("user_id") or 0)

        async def _q():
            return await auth_users.get_user_by_id(api.db, uid)

        try:
            user = await ctx.bridge_call(_q)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid)
        if not user:
            return _err("user_not_found", 404, rid)
        if int(user.get("totp_enabled") or 0) == 1:
            return _err("already_enabled", 409, rid)
        secret = totp.generate_secret()
        if not secret:
            return _err("totp_unavailable", 503, rid)
        enc = ""
        if _fernet_encrypt is not None:
            enc = _fernet_encrypt(secret)

        async def _sv():
            return await auth_users.update_user_fields(
                api.db, uid, totp_secret_encrypted=enc, totp_enabled=0)

        try:
            await ctx.bridge_call(_sv)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid)
        uri = totp.provisioning_uri(secret, str(user.get("username") or "owner"))
        return _ok({"secret": secret, "otpauth_uri": uri,
                    "issuer": "BR7 Dashboard"}, rid)

    async def api_totp_enable(request: "web.Request") -> web.Response:
        rid = _rid()
        s = _sess(request)
        if not s:
            return _err("unauthorized", 401, rid)
        if not totp.LIB_AVAILABLE:
            return _err("totp_unavailable", 503, rid)
        try:
            body = await ctx.body_json(request)
        except Exception as e:
            return _err("invalid_body", getattr(e, "status", 400), rid)
        code = str(body.get("code") or "").strip()
        uid = int(s.get("user_id") or 0)

        async def _q():
            return await auth_users.get_user_by_id(api.db, uid)

        try:
            user = await ctx.bridge_call(_q)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid)
        if not user:
            return _err("user_not_found", 404, rid)
        if int(user.get("totp_enabled") or 0) == 1:
            return _err("already_enabled", 409, rid)
        secret = ""
        try:
            secret = _fernet_decrypt(str(user.get("totp_secret_encrypted") or ""))
        except Exception:
            secret = ""
        if not secret:
            return _err("setup_required", 400, rid, "ابدأ بخطوة الإعداد أولاً")
        if not totp.verify(secret, code):
            return _err("invalid_code", 400, rid)
        codes = totp.generate_recovery_codes()
        stored = totp.hash_recovery_codes(codes)

        async def _sv():
            return await auth_users.update_user_fields(
                api.db, uid, totp_enabled=1, recovery_codes=stored)

        try:
            await ctx.bridge_call(_sv)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid)
        ctx.audit_bg("dashboard.totp_enable", "panel_user",
                     str(user.get("username") or ""), "ok",
                     actor="dashboard:" + str(user.get("username") or ""),
                     ip=ctx.client_ip(request), ua=_ua(request))
        return _ok({"enabled": True, "recovery_codes": codes}, rid)

    async def api_totp_disable(request: "web.Request") -> web.Response:
        rid = _rid()
        s = _sess(request)
        if not s:
            return _err("unauthorized", 401, rid)
        try:
            body = await ctx.body_json(request)
        except Exception as e:
            return _err("invalid_body", getattr(e, "status", 400), rid)
        target_id = int(body.get("user_id") or 0) or int(s.get("user_id") or 0)
        self_op = target_id == int(s.get("user_id") or 0)
        if not self_op and not permissions.role_allows(
                str(s.get("role") or "viewer"), "security.core"):
            return _err("forbidden", 403, rid)

        async def _q():
            return await auth_users.get_user_by_id(api.db, target_id)

        try:
            user = await ctx.bridge_call(_q)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid)
        if not user:
            return _err("user_not_found", 404, rid)
        if int(user.get("totp_enabled") or 0) != 1:
            return _err("not_enabled", 409, rid)
        if self_op:
            pw = str(body.get("password") or "")
            if not passwords.verify_password(pw, str(user.get("password_hash") or "")):
                return _err("invalid_current_password", 400, rid)

        async def _sv():
            return await auth_users.update_user_fields(
                api.db, target_id, totp_enabled=0, totp_secret_encrypted="",
                recovery_codes="[]")

        try:
            await ctx.bridge_call(_sv)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid)
        ctx.audit_bg("dashboard.totp_disable", "panel_user",
                     str(user.get("username") or ""), "ok",
                     actor="dashboard:" + str(s.get("username") or ""),
                     ip=ctx.client_ip(request), ua=_ua(request))
        return _ok({"disabled": True}, rid)

    # ═════════════════════════════════════════════════════════════════════
    # /users/* — إدارة المستخدمين (Owner حصراً + حماية داخلية مزدوجة)
    # ═════════════════════════════════════════════════════════════════════
    async def api_users_list(request: "web.Request") -> web.Response:
        rid = _rid()
        blocked = _require(request, "users.manage", rid)
        if blocked:
            return blocked

        async def _q():
            return await auth_users.list_users(api.db)

        try:
            rows = await ctx.bridge_call(_q)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid)
        return _ok({"users": [_safe_user(u) for u in rows or []],
                    "roles": list(permissions.ROLES)}, rid)

    async def api_users_create(request: "web.Request") -> web.Response:
        rid = _rid()
        blocked = _require(request, "users.manage", rid)
        if blocked:
            return blocked
        try:
            body = await ctx.body_json(request)
        except Exception as e:
            return _err("invalid_body", getattr(e, "status", 400), rid)
        uname = str(body.get("username") or "").strip()
        pw = str(body.get("password") or "")
        role = permissions.normalize_role(str(body.get("role") or "viewer"))
        display = str(body.get("display_name") or "")[:80]
        if not passwords.username_valid(uname):
            return _err("invalid_username", 400, rid,
                        "اسم المستخدم: 3-32 حرفاً لاتينياً/أرقام/_ . - فقط")
        problem = passwords.password_problem(uname, "", pw)
        if problem:
            return _err("weak_password", 400, rid, problem)

        async def _q():
            existing = await auth_users.get_user_by_username(api.db, uname)
            if existing:
                return {"error": "username_taken"}
            u = await auth_users.create_user(
                api.db, uname, passwords.hash_password(pw),
                passwords.backend_name(), role, display, must_change=1)
            if u is None:
                return {"error": "create_failed"}
            return {"user": _safe_user(u)}

        try:
            res = await ctx.bridge_call(_q)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid)
        if res.get("error"):
            return _err(res["error"], 409 if res["error"] == "username_taken" else 500, rid)
        s = _sess(request)
        ctx.audit_bg("dashboard.user_create", "panel_user", uname, str(res["user"]["role"]),
                     actor="dashboard:" + str(s.get("username") or ""),
                     ip=ctx.client_ip(request), ua=_ua(request))
        return _ok(res, rid)

    async def api_users_update(request: "web.Request") -> web.Response:
        rid = _rid()
        blocked = _require(request, "users.manage", rid)
        if blocked:
            return blocked
        try:
            body = await ctx.body_json(request)
        except Exception as e:
            return _err("invalid_body", getattr(e, "status", 400), rid)
        uid = int(body.get("user_id") or 0)
        if uid <= 0:
            return _err("invalid_user_id", 400, rid)
        sets: Dict[str, Any] = {}
        role_changed = False
        deactivated = False
        if "role" in body:
            sets["role"] = permissions.normalize_role(str(body.get("role") or ""))
            role_changed = True
        if "display_name" in body:
            sets["display_name"] = str(body.get("display_name") or "")[:80]
        if "is_active" in body:
            sets["is_active"] = 1 if body.get("is_active") else 0
            deactivated = not bool(body.get("is_active"))

        async def _q():
            target = await auth_users.get_user_by_id(api.db, uid)
            if not target:
                return {"error": "user_not_found"}
            if (role_changed or deactivated) and str(target.get("role")) == "owner":
                owners = await auth_users.count_active_owners(api.db)
                if owners <= 1:
                    return {"error": "last_owner_protected"}
            if not sets:
                return {"user": _safe_user(target)}
            await auth_users.update_user_fields(api.db, uid, **sets)
            updated = await auth_users.get_user_by_id(api.db, uid)
            return {"user": _safe_user(updated or target)}

        try:
            res = await ctx.bridge_call(_q)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid)
        if res.get("error"):
            code = res["error"]
            return _err(code, 404 if code == "user_not_found" else 409, rid)
        if role_changed or deactivated:
            hashes = DASH.sessions.revoke_user(uid)

            async def _rv():
                await auth_users.revoke_session_rows(api.db, hashes)
                return await auth_users.revoke_all_user_sessions(api.db, uid)

            try:
                await ctx.bridge_call(_rv)
            except ctx.bridge_error:
                pass
        s = _sess(request)
        ctx.audit_bg("dashboard.user_update", "panel_user",
                     str((res.get("user") or {}).get("username") or uid),
                     str(sets), actor="dashboard:" + str(s.get("username") or ""),
                     ip=ctx.client_ip(request), ua=_ua(request))
        return _ok(res, rid)

    async def api_users_reset_password(request: "web.Request") -> web.Response:
        rid = _rid()
        blocked = _require(request, "users.manage", rid)
        if blocked:
            return blocked
        try:
            body = await ctx.body_json(request)
        except Exception as e:
            return _err("invalid_body", getattr(e, "status", 400), rid)
        uid = int(body.get("user_id") or 0)
        new = str(body.get("new_password") or "")
        if uid <= 0:
            return _err("invalid_user_id", 400, rid)

        async def _q():
            target = await auth_users.get_user_by_id(api.db, uid)
            if not target:
                return {"error": "user_not_found"}
            problem = passwords.password_problem(str(target.get("username") or ""), "", new)
            if problem:
                return {"error": "weak_password", "note": problem}
            await auth_users.update_user_fields(
                api.db, uid, password_hash=passwords.hash_password(new),
                password_algo=passwords.backend_name(), must_change_password=1)
            return {"ok": True, "username": target.get("username")}

        try:
            res = await ctx.bridge_call(_q)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid)
        if res.get("error"):
            code = res["error"]
            return _err(code, 404 if code == "user_not_found" else 400, rid,
                        res.get("note", ""))
        hashes = DASH.sessions.revoke_user(uid)

        async def _rv():
            return await auth_users.revoke_all_user_sessions(api.db, uid)

        try:
            await ctx.bridge_call(_rv)
        except ctx.bridge_error:
            pass
        s = _sess(request)
        ctx.audit_bg("dashboard.user_reset_password", "panel_user",
                     str(res.get("username") or uid), "must_change=1",
                     actor="dashboard:" + str(s.get("username") or ""),
                     ip=ctx.client_ip(request), ua=_ua(request))
        return _ok({"reset": True, "must_change_password": True}, rid)

    async def api_users_delete(request: "web.Request") -> web.Response:
        rid = _rid()
        blocked = _require(request, "users.manage", rid)
        if blocked:
            return blocked
        try:
            body = await ctx.body_json(request)
        except Exception as e:
            return _err("invalid_body", getattr(e, "status", 400), rid)
        uid = int(body.get("user_id") or 0)
        s = _sess(request)
        if uid <= 0 or uid == int(s.get("user_id") or 0):
            return _err("invalid_target", 400, rid, "لا يمكن حذف حسابك بنفسك")

        async def _q():
            target = await auth_users.get_user_by_id(api.db, uid)
            if not target:
                return {"error": "user_not_found"}
            if str(target.get("role")) == "owner":
                owners = await auth_users.count_active_owners(api.db)
                if owners <= 1:
                    return {"error": "last_owner_protected"}
            await auth_users.delete_user_by_id(api.db, uid)
            return {"ok": True, "username": target.get("username")}

        try:
            res = await ctx.bridge_call(_q)
        except ctx.bridge_error as e:
            return _err("bridge_error", e.status, rid)
        if res.get("error"):
            code = res["error"]
            return _err(code, 404 if code == "user_not_found" else 409, rid)
        DASH.sessions.revoke_user(uid)
        ctx.audit_bg("dashboard.user_delete", "panel_user",
                     str(res.get("username") or uid), "deleted",
                     actor="dashboard:" + str(s.get("username") or ""),
                     ip=ctx.client_ip(request), ua=_ua(request))
        return _ok({"deleted": True}, rid)

    # ═════════════════════════════════════════════════════════════════════
    # المسح الدوري — مزامنة expiry/last_seen مع DB + تنظيف الصفوف القديمة
    # ═════════════════════════════════════════════════════════════════════
    async def _session_sweeper() -> None:
        while True:
            await asyncio.sleep(120)
            try:
                rows, dead = DASH.sessions.touch_snapshot()
                if not rows and not dead:
                    continue

                async def _q():
                    await auth_users.touch_session_rows(api.db, rows)
                    if dead:
                        await auth_users.revoke_session_rows(api.db, dead)
                    await auth_users.purge_expired_session_rows(
                        api.db, time.time() - 7 * 86400)
                    return True

                loop = getattr(bot, "_main_loop", None)
                if loop is not None and not loop.is_closed():
                    asyncio.run_coroutine_threadsafe(_q(), loop)
            except Exception:
                continue

    # ═════════════════════════════════════════════════════════════════════
    # التسجيل
    # ═════════════════════════════════════════════════════════════════════
    r = app.router
    r.add_get("/admin/api/session", api_session)
    r.add_post("/admin/api/login", api_login)
    r.add_post("/admin/api/logout", api_logout)

    r.add_get("/admin/api/auth/me", api_auth_me)
    r.add_post("/admin/api/auth/change-password", api_change_password)
    r.add_get("/admin/api/auth/sessions", api_sessions_list)
    r.add_post("/admin/api/auth/sessions/revoke", api_sessions_revoke)
    r.add_post("/admin/api/auth/sessions/revoke-all", api_sessions_revoke_all)
    r.add_post("/admin/api/auth/totp/setup", api_totp_setup)
    r.add_post("/admin/api/auth/totp/enable", api_totp_enable)
    r.add_post("/admin/api/auth/totp/disable", api_totp_disable)

    r.add_get("/admin/api/users", api_users_list)
    r.add_post("/admin/api/users/create", api_users_create)
    r.add_post("/admin/api/users/update", api_users_update)
    r.add_post("/admin/api/users/reset-password", api_users_reset_password)
    r.add_post("/admin/api/users/delete", api_users_delete)

    try:
        asyncio.ensure_future(_session_sweeper())
    except Exception:
        pass
