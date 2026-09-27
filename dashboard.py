#!/usr/bin/env python3
"""
dashboard.py – Telegram Bot Control Panel v3.1 (DYNAMIC, DB-PERSISTED)
FastAPI + WebSocket Dashboard for EnhancedTelegramBot
Compatible with: config.py v13.1, filter_engine.py v14.1, database.py v9.0,
                  monitors.py v9.7, main.py v13.1, keywords.json v15.1

v3.1 (this pass) — Dynamic Dashboard with DIRECT persistence (dashboard.py +
dashboard_store.py + templates/dashboard.html):

  NEW  #1  — Direct-to-database persistence: every keyword edit and every
             settings change is written to a new `app_settings` key-value
             table in the SAME database the bot already uses (PostgreSQL on
             Render, SQLite locally). Render's ephemeral filesystem no longer
             wipes user edits on redeploy/restart — on boot the dashboard
             lifespan restores all persisted overrides and re-applies them.

  NEW  #2  — Real dynamic Settings: GET /api/settings exposes effective
             values + a validated schema (11 settings across Destinations /
             Alerts / Filtering groups). POST /api/settings validates,
             writes to the DB FIRST, then applies live: CFG-backed fields
             via object.__setattr__ (CFG is read at call time by
             monitors/filter_engine — no restart), rate-limiter fields via
             bot.rate_limiter. The old "frozen dataclass — not supported"
             rejections are gone.

  NEW  #3  — Dynamic Keywords UI: the category dropdown is now built from
             the REAL keywords.json structure (every list-of-strings leaf,
             dotted paths, live counts) instead of 10 hardcoded entries of
             which 9 were empty lists. Adding/deleting now targets the
             sections the filter actually uses.

  NEW  #4  — Editable alert destination: TARGET_GROUP_ID and ADMIN_CHAT_ID
             can be changed from the Settings tab (target applies live on
             the next alert; admin-command handler rebinds on restart).
             Both persist across redeploys.

  FIXED    — Keyword add/delete used to 500 whenever no bot was attached
             (dashboard-only mode) because "bot not initialized" was treated
             as a reload failure and rolled back. It is now a soft success:
             file + DB stay written, filter loads at boot.

v3.0 — full audit fix (H-2/H-3/H-4, M-6, fixes #1-#12): WebSocket token auth,
XSS-guarded templates, atomic keyword writes with rollback, real filter
reload, single DB ownership, stats throttling. See git history.

v3.0 (audit detail) — full audit fix, dashboard.py ONLY:

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
import base64  # v9.29: توكنات المستخدمين الموقّعة (payload.signature)
import csv
import hashlib  # v9.29: مشتق سر RBAC (sha256)
import hmac
import io
import json
import os
import re
import time
import html as _html
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import HTMLResponse, JSONResponse, Response
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
    # v9.35: أخطاء توصيل رمز التحقق التي تسبّبت بـ«الرمز لا يصل» —
    # SendCodeUnavailableError ظهر فعلياً في سجلات الإنتاج عند verify-code.
    SendCodeUnavailableError,
    PhoneNumberUnoccupiedError,
    PhoneNumberBannedError,
)

# v9.35: خريطة وسائل توصيل رمز التحقق — اللوحة لم تعد تقول «تم إرسال
# الرمز» بشكل أعمى؛ بل تخبر المستخدم بالضبط كيف سيصله الرمز (التطبيق/
# SMS/مكالمة/مكالمة وميض) وتنصحه بالبديل الموثوق حين يكون الوسيط
# غير قابل للاستلام على السيرفر (السبب الحقيقي وراء «الرمز لا يصل»).
_OTP_DELIVERY_AR: Dict[str, Tuple[str, str]] = {
    "SentCodeTypeApp": ("app", "رسالة داخل تطبيق تيليجرام (محادثة Telegram الرسمية)"),
    "SentCodeTypeSms": ("sms", "رسالة SMS على رقم الهاتف"),
    "SentCodeTypeCall": ("call", "مكالمة هاتفية ستقرأ لك الرمز"),
    "SentCodeTypeFlashCall": (
        "flash_call",
        "مكالمة وميض (Flash Call) — الرمز في رقم المتصل، غالباً لا تصل على أرقام افتراضية/سيرفرات",
    ),
    "SentCodeTypeMissedCall": (
        "missed_call",
        "مكالمة فائتة — آخر أرقام من رقم المتصل هي الرمز",
    ),
    "SentCodeTypeSetUpEmailRequired": (
        "email",
        "تيليجرام يطلب إعداد بريد إلكتروني لاستعادة الدخول قبل إرسال الرمز",
    ),
}


def _otp_delivery_info(sent_code: Any) -> Tuple[str, str]:
    """v9.35: (مفتاح الوسيلة، وصفها بالعربية) من كائن auth.SentCode.
    فشل-آمن: أي نوع غير معروف يعيد وصفاً عاماً بدل استثناء."""
    try:
        tname = type(sent_code.type).__name__
        return _OTP_DELIVERY_AR.get(tname, ("unknown", "وسيلة توصيل غير معروفة"))
    except Exception:
        return ("unknown", "وسيلة توصيل غير معروفة")


def _qr_svg(url: str) -> str:
    """v9.37: توليد صورة QR بصيغة SVG لرابط tg://login — فشل-آمن:
    أي خلل يعيد نصاً فارغاً والواجهة تعرض الرابط القابل للنقر فقط."""
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage,
                          box_size=12, border=2)
        return img.to_string(encoding="unicode")
    except Exception as e:
        logger.debug(f"qr svg generation failed: {type(e).__name__}: {e}")
        return ""


def _qr_expires_in(qr: Any) -> int:
    """v9.37: الثواني المتوقعة لانتهاء صلاحية رمز QR (تقديري آمن ≥ 0)."""
    try:
        import datetime as _dt
        exp = getattr(qr, "expires", None)
        if isinstance(exp, _dt.datetime):
            return max(0, int((exp - _dt.datetime.now(tz=_dt.timezone.utc)).total_seconds()))
    except Exception:
        pass
    return 30


OTP_UNAVAILABLE_AR = (
    "تيليجرام رفض إرسال رمز التحقق لهذا الرقم الآن (SEND_CODE_UNAVAILABLE — "
    "جميع وسائل التوصيل المتاحة لنوع هذا الرقم استُخدمت). الحل الموثوق: "
    "ولّد Session String على جهازك الشخصي (نفس رقم API_ID/API_HASH) عبر "
    "الأداة generate_session.py أو تطبيق Telethon-Thon ثم الصقه في "
    "«إعدادات الحسابات» — أو انتظر ساعات وأعد المحاولة."
)

from config import CFG, ACCOUNTS
import dashboard_store  # v3.1: direct-to-DB persistence for settings/keywords
from database import EnhancedDatabase

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    logger.warning("psutil not installed. Process memory/CPU stats in dashboard will be limited.")

KEYWORDS_FILE = "keywords.json"
# v9.33: مسار ملف بيئة الحسابات (يكتبه POST /api/accounts) — متغير بيئة
# للاختبارات حتى لا تلوّث بذور الاختبارات الملف الحقيقي.
ACCOUNTS_ENV_PATH = os.getenv("ACCOUNTS_ENV_PATH", "accounts.env")

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
    # v9.35: البديل الموثوق حين يرفض تيليجرام إرسال رمز التحقق من السيرفر
    # (SEND_CODE_UNAVAILABLE) — جلسة مولّدة محلياً تُلصق مباشرة.
    session_string: Optional[str] = None


class AccountUpdate(BaseModel):
    enabled: Optional[bool] = None
    priority: Optional[int] = None
    name: Optional[str] = None
    # v9.34: تعديل كامل لبيانات الحساب (كلها اختيارية — فقط المُرسل يُحدّث)
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    phone: Optional[str] = None
    session_name: Optional[str] = None
    # v9.35: لصق/تحديث الجلسة مباشرة (بدون مسار OTP) — الفارغ لا يُمسّ
    session_string: Optional[str] = None


class AccountToggle(BaseModel):
    # v9.34: تفعيل/تعطيل صريح (لا تبديل ضمني — النتيجة دائماً ما طلبته)
    enabled: bool


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
    """v9.17: `source` يتتبع مكان الحظر (dashboard/alert) ليظهر في صفحة الحظر
    كشريحة "من تنبيه" بدل أن تُسجَّل كل حظرات اللوحات كمصدر dashboard."""

    user_id: int
    reason: str = ""
    source: str = "dashboard"


class BlockChat(BaseModel):
    chat_id: int
    reason: str = ""


class SourceCreate(BaseModel):
    """v9.20 P1: إضافة/تحديث مصدر مراقبة."""
    chat_id: int
    username: str = ""
    title: str = ""
    type: str = ""
    notes: str = ""


class RuleCreate(BaseModel):
    """v9.21 P2: قاعدة جديدة — شرط واحد على الأقل (تحقق صارم في DB layer)."""
    name: str = ""
    conditions: Dict[str, Any] = {}
    action: str = "tag"
    action_value: str = ""
    priority: int = 100


class AllowedCreate(BaseModel):
    """v9.21 P3: كيان موثوق — sender أو chat."""
    entity_type: str = "sender"
    entity_id: int
    note: str = ""


class SettingsBody(BaseModel):
    """v3.1: flexible settings body — {"updates": {...}} or a flat dict of
    validated setting names. Actual validation happens in dashboard_store."""

    updates: Optional[Dict[str, Any]] = None
    model_config = {"extra": "allow"}


class LoginSendCode(BaseModel):
    prefix: str
    force_sms: bool = False  # v9.37: توجيه تيليجرام لإرسال الرمز SMS للشريحة مباشرة


class LoginVerifyCode(BaseModel):
    prefix: str
    code: str


class LoginVerifyPassword(BaseModel):
    prefix: str
    password: str


class FilterFeedback(BaseModel):
    """v10.0: تغذية راجعة بشرية على قرار فلترة (معمل الفلترة)."""
    id: int
    correct: bool
    note: str = ""


class FilterTestRequest(BaseModel):
    """v10.0: فحص نص عبر محرك الفلترة + التصنيف (معمل الفلترة)."""
    text: str


# =============================================================================
# v9.29 P4 RBAC — مستخدمو اللوحة بأدوار وصلاحيات دقيقة (Master Prompt 8/9)
# =============================================================================

# الأدوار الخمسة من Master Prompt 9.1 — صلاحيات دقيقة Granular (9.2).
# التوكن الرئيسي (DASHBOARD_AUTH_TOKEN) يبقى مفتاح Super Admin المطلق
# عبر "*" — حسابات dashboard_users طبقة إضافية لا تستبدله.
ROLE_PERMISSIONS: Dict[str, Set[str]] = {
    "super_admin": {"*"},
    "admin": {
        "accounts.read", "accounts.write", "accounts.delete",
        "sources.read", "sources.write", "sources.delete",
        "keywords.read", "keywords.write", "keywords.delete",
        "files.read", "files.write", "files.delete",
        "rules.read", "rules.write", "rules.delete",
        "settings.read", "settings.write",
        "deployment.read", "deployment.execute",
        "backup.read", "backup.write",
        "audit.read", "messages.read", "notifications.read",
        "users.read", "users.write",
        "auth.read",
    },
    "supervisor": {
        "accounts.read",
        "sources.read", "sources.write",
        "keywords.read", "keywords.write",
        "rules.read", "rules.write",
        "settings.read",
        "deployment.read",
        "backup.read",
        "audit.read", "messages.read", "notifications.read",
        "auth.read",
    },
    "operator": {
        "accounts.read",
        "sources.read",
        "keywords.read",
        "messages.read", "notifications.read",
        "deployment.read",
        "auth.read",
    },
    "viewer": {
        "accounts.read", "sources.read", "keywords.read", "rules.read",
        "messages.read", "notifications.read", "audit.read",
        "deployment.read",
        "auth.read",
    },
}

# توكن مستخدم صالح 12 ساعة (Master Prompt 9: جلسات محدودة).
USER_TOKEN_TTL_SECONDS = 12 * 3600

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


def _rbac_secret() -> bytes:
    """v9.29: سر توقيع توكنات المستخدمين مشتق من التوكن الرئيسي (HMAC).

    لا سر جديد في البيئة: التوكن الرئيسي هو جذر الثقة (Master Prompt
    10.1 — الأسرار عبر البيئة فقط)، وتغييره يبطل كل جلسات المستخدمين.
    """
    return hashlib.sha256(("rbac-v1|" + CFG.DASHBOARD_AUTH_TOKEN).encode("utf-8")).digest()


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def make_user_token(username: str, role: str) -> Tuple[str, int]:
    """v9.29: توكن مستخدم موقّع payload.signature (base64url، stdlib فقط).

    payload بلا أسرار: username/role/iat/exp فقط. يعيد (token, exp_epoch).
    """
    now = int(time.time())
    payload = {"u": username, "r": role, "iat": now, "exp": now + USER_TOKEN_TTL_SECONDS}
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_rbac_secret(), raw, hashlib.sha256).hexdigest()
    return f"{_b64u_encode(raw)}.{sig}", payload["exp"]


def decode_user_token(token: str) -> Optional[Dict[str, Any]]:
    """v9.29: تحقق timing-safe من توكن مستخدم — None عند أي خلل/انتهاء."""
    try:
        payload_b64, sig = token.rsplit(".", 1)
        raw = _b64u_decode(payload_b64)
        expected = hmac.new(_rbac_secret(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(raw.decode("utf-8"))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        username = str(payload.get("u", ""))
        role = str(payload.get("r", ""))
        if not username or role not in ROLE_PERMISSIONS:
            return None
        return {"username": username, "role": role,
                "exp": int(payload["exp"]), "via": "user_token"}
    except Exception:
        return None


def principal_permissions(role: str) -> Set[str]:
    """v9.29: صلاحيات دور (نسخة — لا تُعدَّل الخريطة بالخطأ)."""
    return set(ROLE_PERMISSIONS.get(role, set()))


def _guard_reject_locked(request: Request) -> None:
    """v9.29: رفض موحّد 429 للـIP المقفل (نفس دلالة verify_token)."""
    ip = _auth_guard.client_ip(request)
    remaining = _auth_guard.locked_seconds_left(ip)
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {remaining}s.",
            headers={"Retry-After": str(remaining)},
        )


def _guard_note_failure(request: Request, source: str) -> None:
    """v9.29: تسجيل فشل مصادقة في حارس القفل + تدقيق/إشعار عند بلوغ القفل.

    مشترك بين verify_token و require_permission — التخمين عبر أي باب
    (توكن رئيسي أو توكن مستخدم أو اسم/كلمة مرور) يغذي نفس العدّاد.
    """
    ip = _auth_guard.client_ip(request)
    lock = _auth_guard.record_failure(ip)
    if lock > 0:
        db = getattr(request.app.state, "db", None)
        if db is not None:
            try:
                _track_local_task(db.record_audit(
                    actor="botpanel-admin", action="auth.lockout", object_type="ip",
                    object_id=ip, new_value=f"locked {lock}s ({source})",
                    source="botpanel",
                ), "audit_lockout")
                _track_local_task(db.record_notification(
                    ntype="auth.lockout", title=f"🔒 محاولة تخمين — قفل {lock}s",
                    body=f"IP {ip} أُقفل بعد {_auth_guard.threshold} فشلات ({source}).",
                    severity="critical", object_type="ip", object_id=ip,
                ), "notify_lockout")
            except Exception:
                pass


class Principal(dict):
    """v9.29: هوية المتصل — master token أو مستخدم dashboard_users."""


def require_permission(permission: Optional[str] = None):
    """v9.29: اعتمادية FastAPI — مصادقة موحدة + صلاحية دقيقة اختيارية.

    - التوكن الرئيسي → super_admin مطلق (كل الاختبارات القديمة تنجو).
    - توكن مستخدم موقّع → يتحقق التوقيع والانتهاء ثم الصلاحية المطلوبة
      (403 عند الغياب) — وينعكس في التدقيق كفاعل panel:<username>.
    - perm=None → مصادقة فقط بلا فحص صلاحية (لـ /api/auth/me).

    v9.32: التوسيع الكامل — كل نقاط BotPanel (47 مساراً) انتقلت من
    verify_token (الرئيسي فقط) إلى مصفوفة صلاحيات دقيقة؛ مسار
    /health/full و/ws احتفظا بـverify_token عمداً (عمق تشخيصي/قناة بث).
    """
    async def _dep(request: Request,
                   credentials: HTTPAuthorizationCredentials = Depends(security)) -> Principal:
        _guard_reject_locked(request)
        token = credentials.credentials
        if hmac.compare_digest(token, CFG.DASHBOARD_AUTH_TOKEN):
            _auth_guard.record_success(_auth_guard.client_ip(request))
            return Principal(username="master", role="super_admin",
                             permissions=["*"], via="master_token")
        payload = decode_user_token(token)
        if payload is None:
            _guard_note_failure(request, source="user_token")
            raise HTTPException(status_code=401, detail="Invalid token")
        _auth_guard.record_success(_auth_guard.client_ip(request))
        perms = principal_permissions(payload["role"])
        if permission is not None and permission not in perms and "*" not in perms:
            raise HTTPException(
                status_code=403,
                detail=f"Missing permission: {permission}",
            )
        return Principal(username=payload["username"], role=payload["role"],
                         permissions=sorted(perms), via="user_token",
                         exp=payload["exp"])
    return _dep


# =============================================================================
# Security
# =============================================================================

security = HTTPBearer()


class _AuthGuard:
    """v9.28-2: حارس قفل ضد تخمين توكن اللوحة (brute-force lockout).

    عدّاد فشل لكل IP (أول قيمة X-Forwarded-For خلف بروكسي Render وإلا
    client.host): AUTH_FAIL_THRESHOLD فشلاً خلال AUTH_FAIL_WINDOW_SECONDS
    = قفل AUTH_LOCKOUT_BASE_SECONDS يتضاعف أُسّياً حتى سقف
    AUTH_LOCKOUT_MAX_SECONDS. لا حظر دائم فلا يمكن لمخرب إغلاق المدير
    الحقيقي، ونجاح واحد بتوكن صحيح يمسح الحالة فوراً. في الذاكرة فقط —
    ينتفي بإعادة التشغيل (مقبول لسقف 15 دقيقة).
    """

    def __init__(self) -> None:
        self._fails: Dict[str, List[float]] = {}
        self._lockout_until: Dict[str, float] = {}
        self._lockout_count: Dict[str, int] = {}
        self.threshold = max(1, int(os.getenv("AUTH_FAIL_THRESHOLD", "10")))
        self.window = max(10, int(os.getenv("AUTH_FAIL_WINDOW_SECONDS", "300")))
        self.base_lock = max(5, int(os.getenv("AUTH_LOCKOUT_BASE_SECONDS", "60")))
        self.max_lock = max(self.base_lock, int(os.getenv("AUTH_LOCKOUT_MAX_SECONDS", "900")))

    @staticmethod
    def client_ip(request: Request) -> str:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first[:64]
        try:
            return request.client.host if request.client else "unknown"
        except Exception:
            return "unknown"

    def locked_seconds_left(self, ip: str) -> int:
        until = self._lockout_until.get(ip, 0.0)
        return max(0, int(until - time.time()))

    def record_failure(self, ip: str) -> int:
        """يسجل فشلاً — يعيد الثواني المتبقية للقفل (0 = غير مقفل)."""
        now = time.time()
        bucket = [t for t in self._fails.get(ip, []) if now - t < self.window]
        bucket.append(now)
        self._fails[ip] = bucket
        if len(bucket) < self.threshold:
            return self.locked_seconds_left(ip)
        # بلوغ العتبة → قفل يتضاعف أُسّياً حتى السقف
        count = min(self._lockout_count.get(ip, 0) + 1, 20)
        self._lockout_count[ip] = count
        lock = min(self.base_lock * (2 ** (count - 1)), self.max_lock)
        self._lockout_until[ip] = now + lock
        self._fails[ip] = []
        return int(lock)

    def record_success(self, ip: str) -> None:
        """نجاح واحد بتوكن صحيح يمسح حالة الـIP فوراً."""
        self._fails.pop(ip, None)
        self._lockout_until.pop(ip, None)
        self._lockout_count.pop(ip, None)


_auth_guard = _AuthGuard()


async def verify_token(request: Request, credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """
    Timing-safe token comparison (fix #8), sourced from CFG.DASHBOARD_AUTH_TOKEN.

    v9.28-2: async (التعريف المتزامن يجريه FastAPI في threadpool بلا event
    loop فتسقط جدولة مهام التدقيق/الإشعارات بصمت) + حارس القفل: الـIP
    المقفل يُرفض 429 مع Retry-After قبل أي مقارنة توكن. الطلبات بلا
    ترويزة تُرفض من HTTPBearer قبل الحارس فلا تُحتسب — الحارس يستهدف
    تخمين التوكن الفعلي فقط.
    """
    ip = _auth_guard.client_ip(request)
    remaining = _auth_guard.locked_seconds_left(ip)
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {remaining}s.",
            headers={"Retry-After": str(remaining)},
        )
    token = credentials.credentials
    expected_token = CFG.DASHBOARD_AUTH_TOKEN
    if hmac.compare_digest(token, expected_token):
        _auth_guard.record_success(ip)
        return token
    lock = _auth_guard.record_failure(ip)
    if lock > 0:
        # الطلب العابر للعتبة يُرفض 401 كطبيعته — القفل يسري على الطلبات
        # التالية (نمط cron-10 الموثق: 10 أخطاء = 401، والقادم = 429).
        db = getattr(request.app.state, "db", None)
        if db is not None:
            try:
                _track_local_task(db.record_audit(
                    actor="botpanel-admin", action="auth.lockout", object_type="ip",
                    object_id=ip, new_value=f"locked {lock}s",
                    source="botpanel",
                ), "audit_lockout")
                _track_local_task(db.record_notification(
                    ntype="auth.lockout", title=f"🔒 محاولة تخمين توكن — قفل {lock}s",
                    body=f"IP {ip} أُقفل بعد {_auth_guard.threshold} فشلات. القفل يتضاعف حتى {_auth_guard.max_lock}s.",
                    severity="critical", object_type="ip", object_id=ip,
                ), "notify_lockout")
            except Exception:
                pass
    raise HTTPException(status_code=401, detail="Invalid token")


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

    # v3.1 — direct persistence: make sure the settings table exists and
    # re-apply every DB-persisted override (settings live-applied to CFG,
    # keywords.json restored from the DB copy). Runs in BOTH modes:
    # bot-attached (bot.db reused) and dashboard-only (own SQLite/PG conn).
    # Never allowed to break startup.
    try:
        await dashboard_store.ensure_table(app.state.db)
        restore = await dashboard_store.restore_all(app)
        if not (
            restore["settings_applied"]
            or restore["settings_stored"]
            or restore["keywords_restored"]
            or restore["keywords_skipped_same"]
        ):
            logger.info("dashboard_store: nothing persisted yet — first boot baseline")
    except Exception as e:
        logger.error(f"dashboard_store boot restore failed (continuing): {e}")

    app.state.stats_cache = {}
    app.state.stats_update_task = asyncio.create_task(_update_stats_loop(app), name="dashboard_stats_loop")

    def _stats_task_done(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception():
            logger.error(f"Dashboard stats loop crashed: {t.exception()}")

    app.state.stats_update_task.add_done_callback(_stats_task_done)

    logger.info("Dashboard v3.1 (dynamic, DB-persisted) started successfully")
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
    description="لوحة تحكم ديناميكية لبوت تيليجرام مع IntentEngine — تعديلات تُحفظ مباشرة في قاعدة البيانات",
    version="3.1.0",
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
    """Push live stats over WebSocket every 2s (fix #4: corrected field names).

    v3.1 (audit M-6): with no WebSocket clients connected the loop now only
    refreshes app.state.stats_cache every 5th cycle (10 s) instead of running
    ~7 COUNT/aggregate queries against the DB every 2 s 24/7. The full-rate
    push resumes automatically as soon as a client connects.

    v9.12 (audit M-10): the no-client cadence is widened to every 15th cycle
    (~30 s) — COUNT/DISTINCT aggregates over messages/sender_stats are the
    most expensive queries in the system and the dashboard isn't even
    visible to anyone when no WS client is connected. With clients the rate
    stays at 2s so the UX is unchanged. We also cache the heavy
    db.get_stats() result on app.state for 5s so concurrent /api/stats HTTP
    pulls don't each issue the 7 aggregates.
    """
    cycle = 0
    last_db_stats_ts: float = 0.0
    last_db_stats: Optional[Dict[str, Any]] = None
    DB_STATS_TTL = 5.0  # seconds — coalesce concurrent pulls
    # v9.14: home "آخر التنبيهات" feed — the WS stats payload never carried
    # recent_alerts, so the feed stayed on "جاري التحميل..." forever. Cached
    # on the same 5 s TTL as the heavy aggregates (cheap LIMIT-5 query, but
    # no reason to re-run it every 2 s).
    recent_alerts_cache: Optional[List[Dict[str, Any]]] = None
    recent_alerts_ts: float = 0.0
    RECENT_ALERTS_TTL = 5.0
    # v9.30: شارة الإشعارات — نفس نمط التخزين المؤقت الخفيف (استعلام COUNT).
    unread_cache: int = 0
    unread_ts: float = 0.0
    UNREAD_TTL = 5.0

    async def _unread_notifications_safe(database: Any) -> int:
        """v9.30: عدّاد غير المقروء مع كاش 5 ثوانٍ وفشل-آمن تام — الشارة
        لا تُعطّل بث الإحصاءات أبداً عند أي خلل."""
        nonlocal unread_cache, unread_ts
        now = time.time()
        if unread_ts and (now - unread_ts) < UNREAD_TTL:
            return unread_cache
        try:
            unread_cache = await database.count_unread_notifications()
            unread_ts = now
        except Exception:
            pass
        return unread_cache

    while True:
        try:
            await asyncio.sleep(2)
            cycle += 1
            db = app.state.db
            bot = getattr(app.state, "bot_ref", None)
            if not db.is_connected:
                continue
            has_clients = bool(manager.active_connections)
            if not has_clients and (cycle % 15) != 1:
                # No WS clients: refresh the /api/stats cache at a ~30 s
                # cadence instead of hammering the DB every 2 s 24/7. The
                # full 2 s rate resumes as soon as a client connects.
                continue

            # v9.12 (M-10): coalesce the heavy db.get_stats() call across
            # concurrent HTTP pulls via a 5s in-memory cache.
            now = time.time()
            if last_db_stats is None or (now - last_db_stats_ts) > DB_STATS_TTL:
                db_stats = await db.get_stats()
                last_db_stats = db_stats
                last_db_stats_ts = now
            else:
                db_stats = last_db_stats

            if recent_alerts_cache is None or (now - recent_alerts_ts) > RECENT_ALERTS_TTL:
                try:
                    recent_alerts_cache = [
                        _clean_alert_row(a, 160)
                        for a in await db.get_recent_alerts_for_dashboard(5)
                    ]
                except Exception as e:
                    logger.debug(f"recent_alerts fetch failed: {e}")
                recent_alerts_ts = now
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
                "blocked_chats": db_stats.get("blocked_chats", 0),   # v9.16
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
                # v9.14: powers the home "آخر التنبيهات" live feed (was missing
                # → feed stuck on "جاري التحميل...").
                "recent_alerts": recent_alerts_cache or [],
                # v9.30: شارة الجرس الحية — عدّاد غير المقروء من طبقة v9.24.
                "notifications_unread": await _unread_notifications_safe(db),
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
        # v3.1: "bot not initialized" is NOT a failure — the file (and DB
        # mirror) stay written; the filter loads keywords.json at boot and
        # boot-restore reloads it. Only genuine reload exceptions roll back.
        return {"applied": False, "error": None,
                "note": "البوت لم يُهيأ بعد — حُفِظ في الملف وقاعدة البيانات وسيُحمَّل عند الإقلاع"}
    try:
        bot.filter.reload_keywords(KEYWORDS_FILE)
        return {"applied": True, "error": None}
    except Exception as e:
        logger.error(f"Filter keyword reload failed: {e}")
        return {"applied": False, "error": str(e)}


# =============================================================================
# API Endpoints
# =============================================================================

# v9.12 (audit L-02): read the dashboard template ONCE at import time
# instead of on every "/" request. The old `open(...)` inside the request
# handler was a blocking sync I/O call on the event loop for every page
# load. The template is static — caching it as a module-level string is
# safe.
_DASHBOARD_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "dashboard.html")
_DASHBOARD_TEMPLATE_CONTENT: Optional[str] = None
try:
    if os.path.exists(_DASHBOARD_TEMPLATE_PATH):
        with open(_DASHBOARD_TEMPLATE_PATH, "r", encoding="utf-8") as _f:
            _DASHBOARD_TEMPLATE_CONTENT = _f.read()
    else:
        logger.warning(f"dashboard template not found at {_DASHBOARD_TEMPLATE_PATH}")
except Exception as _tpl_err:  # noqa: BLE001
    logger.error(f"failed to pre-load dashboard template: {_tpl_err}")
    _DASHBOARD_TEMPLATE_CONTENT = None


# ===========================================================================
# v9.14 alert text sanitation for the BotPanel
# ---------------------------------------------------------------------------
# alert_text stored in the DB carries Telegram formatting (<b>الرسالة:</b>,
# <a href=...>, <blockquote>...). The BotPanel renders values with esc() so
# raw tags showed as literal noise in the alerts log and the home feed.
# Same semantics as webadmin.routes._strip_html — duplicated here on
# purpose: dashboard.py must not import webadmin (deployment coupling).
# ===========================================================================

def _strip_html(text: str, limit: int = 200) -> str:
    """Telegram-HTML → clean single-line preview (entities undone, ws squeezed)."""
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", str(text))
    clean = _html.unescape(clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:limit] if limit and limit > 0 else clean


def _clean_alert_row(row: Dict[str, Any], text_limit: int = 2000) -> Dict[str, Any]:
    """Return a shallow copy of an alerts-table row with a clean alert_text.

    Keeps every original column (frontend + CSV rely on them) and adds
    `text_truncated` so the UI can tell when a preview was cut (the detail
    modal shows the same clean text up to 2000 chars — plenty for a message).
    """
    raw = row.get("alert_text") or ""
    out = dict(row)
    full_clean = _strip_html(raw, 0)          # 0 → no truncation
    out["alert_text"] = full_clean[:text_limit]
    out["text_truncated"] = len(full_clean) > text_limit
    return out


@app.get("/", response_class=HTMLResponse)
async def index():
    if _DASHBOARD_TEMPLATE_CONTENT is None:
        return HTMLResponse("<h1>Dashboard not found</h1>", status_code=503)
    return HTMLResponse(_DASHBOARD_TEMPLATE_CONTENT)


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """صفحة تسجيل دخول حسابات تيليجرام (إضافة Session Strings)."""
    return HTMLResponse(LOGIN_PAGE_HTML)


@app.get("/health")
async def health(request: Request):
    """Render health check + keep-alive endpoint (no auth — main.py's
    _keep_alive_loop and Render's own health checks hit this unauthenticated).

    v9.12 (audit L-08): this endpoint now returns ONLY the minimal status
    needed for liveness probing (HTTP 200 + a single "ok"/"degraded" flag
    + db_ok). The detailed operational data (monitor counts, fast_capture
    snapshot, sender_intel, dedup, antispam, uptime) was moved to
    /health/full which requires dashboard auth — exposing counts, queue
    depths and engine internals to anyone who could reach the service
    was an information-disclosure surface."""
    db = getattr(request.app.state, "db", None)
    db_ok = bool(db and getattr(db, "is_connected", False))
    db_healthy = bool(db and getattr(db, "db_healthy", True))
    return JSONResponse({
        "status": "ok" if (db_ok and db_healthy) else "degraded",
        "database": "ok" if db_ok else "down",
        "db_healthy": db_healthy,
        "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })


@app.get("/health/full", dependencies=[Depends(verify_token)])
async def health_full(request: Request):
    """Detailed health snapshot (audit L-08: requires auth).

    Returns the full operational picture that /health used to expose
    unauthenticated: monitor counts, main client state, fast_capture,
    sender_intel, dedup, antispam, uptime. Use this from the dashboard
    or for admin-driven diagnostics; Render's liveness probe and the
    keep-alive loop use the bare /health endpoint above."""
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
    # v9.8 fast capture: deletion-race protection diagnostics (monitors.py
    # may be absent in dashboard-only mode — degrade gracefully).
    try:
        from monitors import get_capture_snapshot
        fast_capture = get_capture_snapshot()
    except Exception:
        fast_capture = {"enabled": False, "available": False}
    # v9.9 sender intelligence: resolver/cache metrics (additive, degrades
    # gracefully when sender_resolver is absent in dashboard-only mode).
    try:
        from sender_resolver import get_sender_intel_snapshot
        sender_intel = get_sender_intel_snapshot()
    except Exception:
        sender_intel = {"enabled": False, "available": False}
    # v9.10 alert dedup: cross-account/re-send barrier snapshot.
    try:
        from dedup import get_dedup_snapshot
        dedup = get_dedup_snapshot()
    except Exception:
        dedup = {"enabled": False, "window_seconds": 0, "mem_size": 0}
    # v9.11 anti-spam: Watch List → Permanent Ignore snapshot (additive).
    try:
        from antispam import get_antispam_snapshot
        antispam = get_antispam_snapshot()
    except Exception:
        antispam = {"enabled": False, "available": False}
    return JSONResponse({
        "status": "ok" if (db_ok and db_healthy) else "degraded",
        "database": "ok" if db_ok else "down",
        "db_healthy": db_healthy,
        "main_client": "up" if main_client_ok else "down",
        "monitors_up": monitors_up,
        "monitors_total": monitors_total,
        "accounts_with_session": sum(1 for a in ACCOUNTS if a.get("session_string")),
        "accounts_total": len(ACCOUNTS),
        "fast_capture": fast_capture,
        "sender_intel": sender_intel,
        "dedup": dedup,
        "antispam": antispam,
        "uptime": uptime,
        "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })


@app.get("/api/stats", dependencies=[Depends(require_permission("auth.read"))])  # v9.32
async def get_stats(request: Request):
    """إحصائيات كاملة مع بيانات IntentEngine."""
    return JSONResponse(request.app.state.stats_cache)


@app.get("/api/accounts", dependencies=[Depends(require_permission("accounts.read"))])  # v9.32
async def get_accounts(request: Request):
    """قائمة الحسابات مع إحصائيات صحيحة الأسماء (fix #4).

    v9.33: مصدر موحد — صفوف المراقبات الحية (إحصاءات كاملة) + كل حساب
    من المصدر الموحد (_merged_accounts) لا مراقب له بعد (بانتظار إعادة
    نشر Render أو بلا جلسة) بصفوف صفرية صادقة بدل اختفائه من القائمة.
    v9.34: حقول الإدارة الكاملة لكل صف — prefix/id/enabled/status/
    has_session/session_masked/last_connected_at/last_activity_at/
    pending_deploy/is_main + كل إحصاءات المراقب الحي إن وجد.
    الجلسة لا تُعاد كاملة أبداً (قناع عرض فقط)."""
    bot = request.app.state.bot_ref
    mon_stats: Dict[str, Dict[str, Any]] = {}
    if bot:
        for m in bot.monitors:
            try:
                p = (m.account or {}).get("prefix", "")
                s = await m.get_stats()
                dlq = s.get("dlq_stats", {}) or {}
                mon_stats[p] = {
                    "connected": s.get("connected"),
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
                }
            except Exception:
                continue
    accounts = []
    for acc in await _merged_accounts(request):
        p = acc.get("prefix", "")
        ms = mon_stats.pop(p, None)
        pending = bool(acc.get("pending_deploy"))
        row: Dict[str, Any] = {
            "id": acc.get("db_id", acc.get("id", 0)),
            "prefix": p,
            "name": acc.get("name"),
            "phone": acc.get("phone"),
            "priority": acc.get("priority"),
            "is_main": bool(acc.get("is_main")),
            "enabled": bool(acc.get("enabled", True)),
            "pending_deploy": pending,
            "has_session": bool(acc.get("session_string")),
            "session_masked": _mask_session(acc.get("session_string")),
            "last_connected_at": acc.get("last_connected_at"),
            "last_activity_at": acc.get("last_activity_at"),
            "origin": acc.get("origin", "env" if not pending else "panel"),
        }
        if ms is not None:
            row.update(ms)
            if ms.get("connected"):
                row["status"] = "connected"
                row["last_error"] = None
            elif ms.get("last_error"):
                row["status"] = "error"
                row["last_error"] = ms.get("last_error")
            else:
                row["status"] = acc.get("status") or "disconnected"
                row["last_error"] = acc.get("last_error_db")
        else:
            row.update({
                "connected": False,
                "alerts_sent": 0,
                "send_cb_state": None,
                "entity_cb_state": None,
                "connect_attempts": 0,
                "accepted": 0,
                "reviewed": 0,
                "ignored": 0,
                "avg_confidence": 0.0,
                "messages_processed": 0,
                "avg_processing_time_ms": 0,
                "duplicates": 0,
                "errors": 0,
                "dead_lettered": 0,
            })
            db_status = acc.get("status")
            if not row["enabled"]:
                row["status"] = "disabled"
            elif db_status:
                row["status"] = db_status
            elif pending:
                row["status"] = "pending"
            else:
                row["status"] = "disconnected"
            row["last_error"] = acc.get("last_error_db")
        accounts.append(row)
    # مراقبات حية بلا بادئة في المصدر الموحد (حالة حدية نادرة) — لا تختفي
    for p, ms in mon_stats.items():
        accounts.append({
            "id": 0, "prefix": p, "name": p, "phone": "", "priority": 0,
            "is_main": False, "enabled": True, "pending_deploy": False,
            "has_session": True, "session_masked": "", "last_connected_at": None,
            "last_activity_at": None, "origin": "env", **ms,
            "status": "connected" if ms.get("connected") else "error",
        })
    return JSONResponse({"accounts": accounts, "monitoring_ready": _monitoring_ready()})


@app.post("/api/accounts", dependencies=[Depends(require_permission("accounts.write"))])  # v9.32
async def add_account(data: AccountCreate, request: Request):
    """إضافة حساب جديد — v9.33: قاعدة البيانات أولاً (dashboard_accounts =
    مصدر حقيقة اللوحة، الظهور في إدارة الجلسات فوراً دون إعادة تشغيل)،
    ثم مزامنة متغيرات بيئة Render (الديمومة في الإنتاج + تفعيل تلقائي
    بعد إعادة النشر)، ثم إلحاق accounts.env للتشغيل المحلي فقط
    (قرص Render مؤقت — الديمومة من متغيرات البيئة لا من الملف)."""
    merged = await _merged_accounts(request)
    for acc in merged:
        if str(acc.get("phone") or "") == data.phone:
            raise HTTPException(status_code=400, detail="Account already exists")

    # v9.18 P0: 20-account cap — same ceiling webadmin enforces (409).
    if len(merged) >= 20:
        raise HTTPException(status_code=409, detail="Account limit reached (20). Remove an account first.")

    used = {a.get("prefix", "") for a in merged}
    prefix = next((f"ACCOUNT_{i}" for i in range(1, 41) if f"ACCOUNT_{i}" not in used), None)
    if prefix is None:
        raise HTTPException(status_code=409, detail="Account limit reached (20). Remove an account first.")

    # 1) قاعدة البيانات — مصدر الحقيقة: أي فشل هنا = فشل الطلب الصريح
    #    (لا رسالة نجاح وهمية أبداً).
    session_string = (data.session_string or "").strip() or None
    db = getattr(request.app.state, "db", None)
    if db is not None:
        row = await db.upsert_dashboard_account(
            prefix=prefix, name=data.name, api_id=data.api_id, api_hash=data.api_hash,
            phone=data.phone, session_name=data.session_name, priority=data.priority,
            session_string=session_string,
            origin="panel",
        )
        if row is None:
            raise HTTPException(status_code=500, detail="فشل حفظ الحساب في قاعدة البيانات")

    # 2) accounts.env — للتشغيل المحلي (best-effort: قاعدة البيانات محفوظة
    #    بالفعل ولا يعتمد ظهور الحساب في اللوحة على هذا الملف).
    new_account_lines = [
        f"\n# === {data.name} ===\n",
        f"{prefix}_API_ID={data.api_id}\n",
        f"{prefix}_API_HASH={data.api_hash}\n",
        f"{prefix}_PHONE={data.phone}\n",
        f"{prefix}_SESSION_NAME={data.session_name}\n",
        f"{prefix}_PRIORITY={data.priority}\n",
    ]
    try:
        def _write() -> None:
            with open(ACCOUNTS_ENV_PATH, "a", encoding="utf-8") as f:
                f.writelines(new_account_lines)
        await asyncio.get_event_loop().run_in_executor(None, _write)
    except Exception as e:
        logger.warning(f"accounts.env append failed (DB row kept): {e}")

    # 3) مزامنة Render — تخزين دائم + إعادة نشر تلقائية تفعّل الحساب.
    #    v9.35: تتضمن SESSION_STRING عند لصق جلسة جاهزة (ديمومة كاملة).
    render_save: Dict[str, Any] = {"saved": False, "reason": "RENDER_API_KEY / RENDER_SERVICE_ID غير مضبوطة"}
    if (os.getenv("RENDER_API_KEY") or "").strip() and (os.getenv("RENDER_SERVICE_ID") or "").strip():
        render_pairs = [
            (f"{prefix}_API_ID", str(data.api_id)),
            (f"{prefix}_API_HASH", data.api_hash),
            (f"{prefix}_PHONE", data.phone),
            (f"{prefix}_SESSION_NAME", data.session_name),
            (f"{prefix}_PRIORITY", str(data.priority)),
        ]
        if session_string:
            render_pairs.append((f"{prefix}_SESSION_STRING", session_string))
        render_save = await render_upsert_env_many(render_pairs)
        logger.info(f"account {prefix} Render env sync: saved={render_save.get('saved')}")

    await _audit(request, "account.add", object_type="account", object_id=data.name or prefix,
                 new_value=_mask_phone(data.phone or ""))

    # v9.35: جلسة جاهزة = توصيل حي فوري (بلا انتظار إعادة نشر Render)
    # + تحديث نسخة الذاكرة ليعرض البوت الحساب متصلاً في نفس اللحظة.
    connect_scheduled = False
    if session_string:
        for a in ACCOUNTS:
            if (a.get("prefix") or "").upper() == prefix:
                a["session_string"] = session_string
                break
        if getattr(request.app.state, "bot_ref", None) is not None:
            try:
                _track_local_task(
                    _runtime_connect_account(request, prefix),
                    f"account_add_connect_{prefix}",
                )
                connect_scheduled = True
            except Exception as e:
                logger.debug(f"post-add connect spawn skipped [{prefix}]: {e}")

    message = (
        ("تمت إضافة الحساب وحفظ الجلسة" + (" — جاري الاتصال وبدء المراقبة الآن" if connect_scheduled else ""))
        if session_string
        else "تمت إضافة الحساب — سجّل الدخول من صفحة الجلسات لإنشاء الجلسة"
    )
    return JSONResponse({
        "success": True,
        "message": message,
        "prefix": prefix,
        "saved_to_render": bool(render_save.get("saved")),
        "render_reason": render_save.get("reason", ""),
        "connect_scheduled": connect_scheduled,
    })


# ─────────────────── v9.34: إدارة حسابات كاملة (CRUD + Runtime) ───────────────────
# كل نقطة هنا تنفّذ فعلياً على قاعدة البيانات ونظام Telethon — لا واجهات شكلية.
# المعرّف في المسارات هو prefix (المفتاح المستقر في البيئة والمراقبات والDB).

def _find_merged_account(merged: List[Dict[str, Any]], prefix: str) -> Optional[Dict[str, Any]]:
    return next((a for a in merged if (a.get("prefix") or "").upper() == prefix), None)


async def _materialize_env_account(request: Request, prefix: str, acc: Dict[str, Any]) -> None:
    """v9.34: حساب من بيئة الإقلاع لا صف له في dashboard_accounts — أول عملية
    إدارة عليه تُنشئ الصف (origin='env') فتصبح حالته قابلة للتتبع والديمومة.
    فشل-آمن: تعذر الحفظ لا يمنع العملية الحية على الإطلاق."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return
    try:
        if await db.get_dashboard_account(prefix) is None:
            await db.upsert_dashboard_account(
                prefix=prefix, name=acc.get("name") or prefix,
                api_id=int(acc.get("api_id") or 0), api_hash=str(acc.get("api_hash") or ""),
                phone=str(acc.get("phone") or ""), session_name=str(acc.get("session") or ""),
                priority=int(acc.get("priority") or 10),
                session_string=acc.get("session_string"),
                is_main=bool(acc.get("is_main")), origin="env",
            )
    except Exception as e:
        logger.debug(f"materialize env account skipped [{prefix}]: {e}")


async def _runtime_connect_account(request: Request, prefix: str) -> Dict[str, Any]:
    """v9.34: توصيل حساب حياً (يُستخدم من connect/reconnect/toggle/التسجيل).
    يحدّث قاعدة البيانات بالنتيجة الحقيقية ويعيد قاموس النتيجة.
    ترتيب الفحوص: الحساب → الجلسة (بيانات) → البوت (تشغيل) — أخطاء البيانات
    تسبق أخطاء التشغيل فتصل الرسالة الأدق للمستخدم."""
    merged = await _merged_accounts(request)
    acc = _find_merged_account(merged, prefix)
    if acc is None:
        return {"ok": False, "error": f"الحساب {prefix} غير موجود"}
    if not (acc.get("session_string") or "").strip():
        return {"ok": False, "error": "لا توجد جلسة لهذا الحساب — سجّل الدخول من صفحة الجلسات أولاً"}
    bot = getattr(request.app.state, "bot_ref", None)
    if bot is None:
        return {"ok": False, "error": "الخدمة تعمل بوضع اللوحة فقط (بدون بوت) — أعد النشر أولاً"}
    result = await bot.add_runtime_monitor(acc)
    db = getattr(request.app.state, "db", None)
    if db is not None:
        if result.get("ok"):
            await db.mark_dashboard_account_connected(prefix)
        else:
            await db.set_dashboard_account_status(prefix, "error", result.get("error"))
    return result


async def _reconnect_with_new_session(request: Request, prefix: str) -> None:
    """v9.35: إعادة توصيل حي بعد لصق جلسة جديدة من اللوحة — إزالة المراقب
    القديم (إن وجد) ثم إنشاء مراقب بالجلسة الجديدة بنفس مسار الإقلاع.
    فشل-آمن: يُسجّل ولا يُرفع أبداً (الرد نجح بالفعل والجلسة محفوظة)."""
    try:
        bot = getattr(request.app.state, "bot_ref", None)
        if bot is None:
            return
        bot.remove_runtime_monitor(prefix, disconnect_client=True)
        result = await _runtime_connect_account(request, prefix)
        if result.get("ok"):
            logger.info(f"✅ Account {prefix} reconnected live with new session")
        else:
            logger.warning(f"new-session reconnect failed [{prefix}]: {result.get('error')}")
    except Exception as e:
        logger.debug(f"_reconnect_with_new_session failed [{prefix}]: {e}")


@app.get("/api/accounts/{prefix}", dependencies=[Depends(require_permission("accounts.read"))])  # v9.34
async def get_account_detail(prefix: str, request: Request):
    """تفاصيل حساب واحد — بيانات الإعداد + حالة الإدارة + إحصاءات المراقب الحي."""
    p = prefix.strip().upper()
    merged = await _merged_accounts(request)
    acc = _find_merged_account(merged, p)
    if acc is None:
        raise HTTPException(status_code=404, detail=f"الحساب {p} غير موجود")
    bot = getattr(request.app.state, "bot_ref", None)
    mon_stats: Dict[str, Any] = {}
    if bot:
        mon = bot.get_monitor_by_prefix(p)
        if mon is not None:
            try:
                mon_stats = await mon.get_stats()
            except Exception:
                mon_stats = {}
    has_session = bool(acc.get("session_string"))
    return JSONResponse({
        "id": acc.get("db_id", acc.get("id", 0)),
        "prefix": p,
        "name": acc.get("name"),
        "phone": acc.get("phone"),
        "api_id": acc.get("api_id"),
        "session_name": acc.get("session") or acc.get("session_name"),
        "priority": acc.get("priority"),
        "is_main": bool(acc.get("is_main")),
        "enabled": bool(acc.get("enabled", True)),
        "pending_deploy": bool(acc.get("pending_deploy")),
        "has_session": has_session,
        "session_masked": _mask_session(acc.get("session_string")),
        "connected": bool(mon_stats.get("connected", False)),
        "status": ("connected" if mon_stats.get("connected") else None)
                  or acc.get("status") or ("pending" if acc.get("pending_deploy") else "disconnected"),
        "last_error": mon_stats.get("last_error") or acc.get("last_error_db"),
        "last_connected_at": acc.get("last_connected_at"),
        "last_activity_at": acc.get("last_activity_at"),
        "monitor_stats": {
            k: mon_stats.get(k) for k in (
                "messages_processed", "alerts_sent", "errors", "duplicates",
                "accepted", "reviewed", "ignored", "avg_confidence",
                "connect_attempts", "avg_processing_time_ms",
            )
        } if mon_stats else None,
    })


@app.put("/api/accounts/{prefix}", dependencies=[Depends(require_permission("accounts.write"))])  # v9.34
async def update_account(prefix: str, data: AccountUpdate, request: Request):
    """تعديل بيانات حساب — قاعدة البيانات أولاً (فشلها = فشل الطلب)، ثم
    الذاكرة (ACCOUNTS + المراقب الحي)، ثم مزامنة متغيرات بيئة Render.
    التغيير ينعكس فوراً في اللوحة دون إعادة تشغيل."""
    p = prefix.strip().upper()
    session_string = (data.session_string or "").strip() or None
    if not any([data.name, data.api_id, data.api_hash, data.phone, data.session_name,
                data.priority is not None, data.enabled is not None, session_string]):
        raise HTTPException(status_code=400, detail="لا توجد حقول للتحديث")
    merged = await _merged_accounts(request)
    acc = _find_merged_account(merged, p)
    if acc is None:
        raise HTTPException(status_code=404, detail=f"الحساب {p} غير موجود")

    # منع تكرار الهاتف (على حساب آخر)
    if data.phone:
        for other in merged:
            op = (other.get("prefix") or "").upper()
            if op != p and str(other.get("phone") or "") == data.phone.strip():
                raise HTTPException(status_code=400, detail="رقم الهاتف مستخدم بالفعل لحساب آخر")

    db = getattr(request.app.state, "db", None)
    if db is not None:
        await _materialize_env_account(request, p, acc)
        ok = await db.update_dashboard_account(
            p,
            name=data.name, api_id=data.api_id, api_hash=data.api_hash,
            phone=data.phone, session_name=data.session_name, priority=data.priority,
        )
        if not ok and (await db.get_dashboard_account(p)) is None:
            raise HTTPException(status_code=500, detail="فشل حفظ التعديل في قاعدة البيانات")
        if data.enabled is not None:
            await db.set_dashboard_account_enabled(p, bool(data.enabled))
        if session_string:
            # v9.35: لصق جلسة جديدة من اللوحة — تُحفظ فوراً كمصدر حقيقة
            if not await db.set_dashboard_account_session(p, session_string):
                logger.warning(f"session update db persist failed [{p}]")

    # الذاكرة: نسخة الإقلاع + المراقب الحي (تعديل اسم/أولوية ينعكس فوراً)
    reconnect_needed = False
    for a in ACCOUNTS:
        if (a.get("prefix") or "").upper() == p:
            if data.name is not None:
                a["name"] = data.name.strip()
            if data.priority is not None:
                a["priority"] = int(data.priority)
            if session_string:
                a["session_string"] = session_string
                reconnect_needed = True
            break
    bot = getattr(request.app.state, "bot_ref", None)
    if bot is not None:
        mon = bot.get_monitor_by_prefix(p)
        if mon is not None:
            if data.name is not None:
                mon.account["name"] = data.name.strip()
            if data.priority is not None:
                mon.account["priority"] = int(data.priority)
            if session_string:
                # جلسة جديدة على مراقب حي = إزالة ثم توصيل بالجلسة الجديدة
                mon.account["session_string"] = session_string
                reconnect_needed = True

    # مزامنة Render — فقط الحقول الموجودة في البيئة أصلاً
    render_save: Dict[str, Any] = {"saved": False, "reason": ""}
    if (os.getenv("RENDER_API_KEY") or "").strip() and (os.getenv("RENDER_SERVICE_ID") or "").strip():
        pairs = []
        if data.api_id is not None:
            pairs.append((f"{p}_API_ID", str(data.api_id)))
        if data.api_hash is not None:
            pairs.append((f"{p}_API_HASH", data.api_hash.strip()))
        if data.phone is not None:
            pairs.append((f"{p}_PHONE", data.phone.strip()))
        if data.session_name is not None:
            pairs.append((f"{p}_SESSION_NAME", data.session_name.strip()))
        if data.priority is not None:
            pairs.append((f"{p}_PRIORITY", str(data.priority)))
        if session_string:
            pairs.append((f"{p}_SESSION_STRING", session_string))
        if pairs:
            render_save = await render_upsert_env_many(pairs)

    # v9.35: جلسة جديدة = توصيل حي فوري (إنشاء مراقب أو استبدال قديم).
    # لا شرط على reconnect_needed هنا — الحساب المضاف من اللوحة ليس في
    # ACCOUNTS ولا له مراقب بعد، وهذا بالضبط المسار الذي يجب أن يعمل.
    if session_string and bot is not None:
        enabled_now = True
        if db is not None:
            try:
                row = await db.get_dashboard_account(p) if db else None
                enabled_now = bool(row.get("enabled", True)) if row else True
            except Exception:
                pass
        if enabled_now:
            try:
                _track_local_task(
                    _reconnect_with_new_session(request, p),
                    f"account_session_reconnect_{p}",
                )
            except Exception as e:
                logger.debug(f"session reconnect spawn skipped [{p}]: {e}")

    await _audit(request, "account.update", object_type="account", object_id=p,
                 new_value=json.dumps({k: (v if k != "api_hash" else "***") for k, v in {
                     "name": data.name, "api_id": data.api_id, "phone": _mask_phone(data.phone or ""),
                     "session_name": data.session_name, "priority": data.priority,
                     "session_string": bool(session_string),
                     "enabled": data.enabled}.items() if v is not None}, ensure_ascii=False))
    return JSONResponse({
        "success": True,
        "message": ("تم حفظ التعديل"
                    + (" — حُفظت الجلسة الجديدة" + (" وجاري إعادة الاتصال بها" if reconnect_needed else "")
                       if session_string else "")),
        "prefix": p,
        "saved_to_render": bool(render_save.get("saved")),
        "render_reason": render_save.get("reason", ""),
    })


@app.delete("/api/accounts/{prefix}", dependencies=[Depends(require_permission("accounts.write"))])  # v9.34
async def delete_account(prefix: str, request: Request):
    """حذف حساب نهائياً — إزالة المراقب الحي + حذف صف قاعدة البيانات +
    حذف متغيرات بيئة Render (الجلسة والإعدادات). الحساب الرئيسي محمي."""
    p = prefix.strip().upper()
    if p == "MAIN":
        raise HTTPException(status_code=400, detail="لا يمكن حذف الحساب الرئيسي (MAIN) — يمكن تعطيله فقط")
    merged = await _merged_accounts(request)
    acc = _find_merged_account(merged, p)
    if acc is None:
        raise HTTPException(status_code=404, detail=f"الحساب {p} غير موجود")
    if acc.get("is_main"):
        raise HTTPException(status_code=400, detail="لا يمكن حذف الحساب الرئيسي — يمكن تعطيله فقط")

    # 1) إزالة المراقب الحي (قطع اتصال فعلي)
    bot = getattr(request.app.state, "bot_ref", None)
    removed_live = False
    if bot is not None:
        removed_live = await bot.remove_runtime_monitor(p, disconnect_client=True)

    # 2) حذف صف قاعدة البيانات
    db = getattr(request.app.state, "db", None)
    db_deleted = False
    if db is not None:
        db_deleted = await db.delete_dashboard_account(p)

    # 3) حذف متغيرات بيئة Render (best-effort — أسرار لا تبقى معلقة)
    render_delete: Dict[str, Any] = {"saved": False, "reason": "غير مضبوط"}
    if (os.getenv("RENDER_API_KEY") or "").strip() and (os.getenv("RENDER_SERVICE_ID") or "").strip():
        deleted, failed = 0, 0
        for suffix in ("_API_ID", "_API_HASH", "_PHONE", "_SESSION_NAME", "_PRIORITY", "_SESSION_STRING"):
            try:
                from webadmin.render_api import delete_env as _render_delete_env
                res = await _render_delete_env(f"{p}{suffix}")
                if res.get("saved"):
                    deleted += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        render_delete = {"saved": deleted > 0 and failed == 0, "deleted": deleted, "failed": failed}

    # 4) إزالة من نسخة الإقلاع في الذاكرة + ملف accounts.env المحلي
    try:
        ACCOUNTS[:] = [a for a in ACCOUNTS if (a.get("prefix") or "").upper() != p]
    except Exception:
        pass
    try:
        def _rewrite() -> None:
            if not os.path.exists(ACCOUNTS_ENV_PATH):
                return
            with open(ACCOUNTS_ENV_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
            keep = [ln for ln in lines if not any(
                ln.strip().startswith(f"{p}{sfx}=") for sfx in
                ("_API_ID", "_API_HASH", "_PHONE", "_SESSION_NAME", "_PRIORITY", "_SESSION_STRING"))]
            with open(ACCOUNTS_ENV_PATH, "w", encoding="utf-8") as f:
                f.writelines(keep)
        await asyncio.get_event_loop().run_in_executor(None, _rewrite)
    except Exception as e:
        logger.debug(f"accounts.env rewrite skipped [{p}]: {e}")

    await _audit(request, "account.delete", object_type="account", object_id=p,
                 old_value=_mask_phone(str(acc.get("phone") or "")),
                 new_value=f"live_removed={removed_live}, db_deleted={db_deleted}")
    return JSONResponse({
        "success": True,
        "message": "تم حذف الحساب نهائياً",
        "prefix": p,
        "runtime_removed": bool(removed_live),
        "db_deleted": bool(db_deleted),
        "render_deleted": bool(render_delete.get("saved")),
    })


@app.post("/api/accounts/{prefix}/toggle", dependencies=[Depends(require_permission("accounts.write"))])  # v9.34
async def toggle_account(prefix: str, data: AccountToggle, request: Request):
    """تفعيل/تعطيل حساب — قاعدة البيانات مصدر الحقيقة + مزامنة Runtime فورية:
    تعطيل = إيقاف المراقب الحي · تفعيل = توصيل المراقب إن وُجدت جلسة."""
    p = prefix.strip().upper()
    merged = await _merged_accounts(request)
    acc = _find_merged_account(merged, p)
    if acc is None:
        raise HTTPException(status_code=404, detail=f"الحساب {p} غير موجود")

    db = getattr(request.app.state, "db", None)
    if db is not None:
        await _materialize_env_account(request, p, acc)
        ok = await db.set_dashboard_account_enabled(p, bool(data.enabled))
        if not ok and (await db.get_dashboard_account(p)) is None:
            raise HTTPException(status_code=500, detail="فشل حفظ حالة الحساب في قاعدة البيانات")

    bot = getattr(request.app.state, "bot_ref", None)
    runtime: Dict[str, Any] = {"changed": False}
    if bot is not None:
        if data.enabled:
            mon = bot.get_monitor_by_prefix(p)
            if mon is None or not mon.is_connected:
                result = await _runtime_connect_account(request, p)
                runtime = {"changed": True, "connected": bool(result.get("ok")), "error": result.get("error")}
                if not result.get("ok") and not (acc.get("session_string") or "").strip():
                    # لا جلسة — التفعيل صار في DB وسيُطبق عند إعادة النشر بعد التسجيل
                    runtime["note"] = "تم التفعيل في قاعدة البيانات — سجّل الدخول لإكمال الاتصال"
        else:
            runtime["changed"] = await bot.remove_runtime_monitor(p, disconnect_client=True)
            db2 = getattr(request.app.state, "db", None)
            if db2 is not None:
                await db2.set_dashboard_account_status(p, "disconnected", None)

    await _audit(request, "account.toggle", object_type="account", object_id=p,
                 new_value="enabled" if data.enabled else "disabled")
    return JSONResponse({
        "success": True,
        "message": "تم تفعيل الحساب" if data.enabled else "تم تعطيل الحساب",
        "prefix": p,
        "enabled": bool(data.enabled),
        "runtime": runtime,
    })


@app.post("/api/accounts/{prefix}/connect", dependencies=[Depends(require_permission("accounts.write"))])  # v9.34
async def connect_account(prefix: str, request: Request):
    """توصيل الحساب بـTelethon حياً الآن (إنشاء مراقب + اتصال + بدء مراقبة) —
    دون إعادة تشغيل الخدمة. يتطلب جلسة صالحة."""
    p = prefix.strip().upper()
    merged = await _merged_accounts(request)
    acc = _find_merged_account(merged, p)
    if acc is None:
        raise HTTPException(status_code=404, detail=f"الحساب {p} غير موجود")
    if not bool(acc.get("enabled", True)):
        raise HTTPException(status_code=400, detail="الحساب معطّل — فعّله أولاً ثم اتصل")
    await _materialize_env_account(request, p, acc)
    result = await _runtime_connect_account(request, p)
    if not result.get("ok"):
        code = 503 if "وضع اللوحة فقط" in str(result.get("error", "")) else 400
        raise HTTPException(status_code=code, detail=str(result.get("error") or "فشل الاتصال"))
    await _audit(request, "account.connect", object_type="account", object_id=p, new_value="connected")
    return JSONResponse({"success": True, "message": "تم توصيل الحساب وبدأت المراقبة", "prefix": p,
                         "connected": True})


@app.post("/api/accounts/{prefix}/disconnect", dependencies=[Depends(require_permission("accounts.write"))])  # v9.34
async def disconnect_account(prefix: str, request: Request):
    """قطع اتصال الحساب حياً (إزالة مراقبه من الذاكرة) — الحساب يبقى محفوظاً
    في قاعدة البيانات ويمكن إعادة توصيله متى شئت."""
    p = prefix.strip().upper()
    merged = await _merged_accounts(request)
    acc = _find_merged_account(merged, p)
    if acc is None:
        raise HTTPException(status_code=404, detail=f"الحساب {p} غير موجود")
    bot = getattr(request.app.state, "bot_ref", None)
    was_live = False
    if bot is not None:
        was_live = await bot.remove_runtime_monitor(p, disconnect_client=True)
    db = getattr(request.app.state, "db", None)
    if db is not None:
        await _materialize_env_account(request, p, acc)
        await db.set_dashboard_account_status(p, "disconnected", None)
    await _audit(request, "account.disconnect", object_type="account", object_id=p, new_value="disconnected")
    if not was_live:
        return JSONResponse({"success": True, "message": "كان الحساب غير متصل أصلاً", "prefix": p,
                             "connected": False, "was_connected": False})
    return JSONResponse({"success": True, "message": "تم قطع اتصال الحساب", "prefix": p,
                         "connected": False, "was_connected": True})


@app.post("/api/accounts/{prefix}/reconnect", dependencies=[Depends(require_permission("accounts.write"))])  # v9.34
async def reconnect_account(prefix: str, request: Request):
    """إعادة اتصال كاملة (قطع ثم توصيل) — لعلاج تعليق الاتصال أو تحديث الجلسة."""
    p = prefix.strip().upper()
    merged = await _merged_accounts(request)
    acc = _find_merged_account(merged, p)
    if acc is None:
        raise HTTPException(status_code=404, detail=f"الحساب {p} غير موجود")
    bot = getattr(request.app.state, "bot_ref", None)
    if bot is None:
        raise HTTPException(status_code=503, detail="الخدمة تعمل بوضع اللوحة فقط (بدون بوت) — أعد النشر أولاً")
    if bot.get_monitor_by_prefix(p) is None:
        # لا مراقب حي — أعد المسار إلى connect القياسي
        if not bool(acc.get("enabled", True)):
            raise HTTPException(status_code=400, detail="الحساب معطّل — فعّله أولاً")
        await _materialize_env_account(request, p, acc)
        result = await _runtime_connect_account(request, p)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=str(result.get("error") or "فشل الاتصال"))
        await _audit(request, "account.reconnect", object_type="account", object_id=p, new_value="connected(fresh)")
        return JSONResponse({"success": True, "message": "تم توصيل الحساب (لم يكن متصلاً)", "prefix": p,
                             "connected": True})
    result = await bot.reconnect_runtime_monitor(p)
    db = getattr(request.app.state, "db", None)
    if db is not None:
        if result.get("ok"):
            await db.mark_dashboard_account_connected(p)
        else:
            await db.set_dashboard_account_status(p, "error", result.get("error"))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("error") or "فشل إعادة الاتصال"))
    await _audit(request, "account.reconnect", object_type="account", object_id=p, new_value="connected")
    return JSONResponse({"success": True, "message": "تم إعادة الاتصال بنجاح", "prefix": p, "connected": True})


@app.post("/api/accounts/{prefix}/test", dependencies=[Depends(require_permission("accounts.read"))])  # v9.34
async def test_account(prefix: str, request: Request):
    """اختبار اتصال حقيقي مع تيليجرام — يستخدم المراقب الحي إن وجد، وإلا
    يبني عميلاً مؤقتاً من Session String المحفوظ (العميل المؤقت يُغلق دائماً).
    لا يغيّر أي حالة — قراءة تشخيصية فقط."""
    p = prefix.strip().upper()
    merged = await _merged_accounts(request)
    acc = _find_merged_account(merged, p)
    if acc is None:
        raise HTTPException(status_code=404, detail=f"الحساب {p} غير موجود")
    bot = getattr(request.app.state, "bot_ref", None)

    # مسار 1: مراقب حي — فحص حقيقي عبر get_me (نداء فعلي لتيليجرام)
    if bot is not None:
        mon = bot.get_monitor_by_prefix(p)
        if mon is not None and mon.is_connected and mon.client is not None:
            try:
                me = await asyncio.wait_for(mon.client.get_me(), timeout=15)
                return JSONResponse({
                    "success": True, "connected": True, "tested": "live_monitor",
                    "telegram_user": f"@{me.username}" if getattr(me, "username", None) else str(getattr(me, "id", "")),
                    "message": "الاتصال سليم — تيليجرام يستجيب",
                })
            except Exception as e:
                return JSONResponse({
                    "success": False, "connected": mon.is_connected, "tested": "live_monitor",
                    "error": f"{type(e).__name__}: {str(e)[:150]}",
                    "message": "المراقب متصل داخلياً لكن تيليجرام لا يستجيب",
                }, status_code=200)

    # مسار 2: عميل مؤقت من الجلسة المحفوظة
    session_string = (acc.get("session_string") or "").strip()
    if not session_string:
        return JSONResponse({
            "success": False, "connected": False, "tested": "no_session",
            "message": "لا توجد جلسة — أرسل رمز التحقق وسجّل الدخول أولاً",
        }, status_code=200)
    client = None
    try:
        client = TelegramClient(StringSession(session_string), int(acc.get("api_id") or 0),
                                str(acc.get("api_hash") or ""), device_model="BR7-Panel-Test",
                                system_version="Linux", app_version="v9.34")
        await asyncio.wait_for(client.connect(), timeout=30)
        if not await client.is_user_authorized():
            return JSONResponse({
                "success": False, "connected": False, "tested": "temp_client",
                "message": "الجلسة منتهية أو ملغاة — سجّل الدخول من جديد",
            }, status_code=200)
        me = await client.get_me()
        return JSONResponse({
            "success": True, "connected": True, "tested": "temp_client",
            "telegram_user": f"@{me.username}" if getattr(me, "username", None) else str(getattr(me, "id", "")),
            "message": "الجلسة صالحة والاتصال يعمل",
        })
    except asyncio.TimeoutError:
        return JSONResponse({"success": False, "connected": False, "tested": "temp_client",
                             "message": "انتهت المهلة أثناء الاتصال بتيليجرام — تحقق من الشبكة"}, status_code=200)
    except Exception as e:
        return JSONResponse({"success": False, "connected": False, "tested": "temp_client",
                             "error": f"{type(e).__name__}: {str(e)[:150]}",
                             "message": "فشل الاتصال بتيليجرام"}, status_code=200)
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


@app.post("/api/accounts/{prefix}/target-check", dependencies=[Depends(require_permission("accounts.read"))])  # v10.1
async def target_check(prefix: str, request: Request):
    """تشخيص حي شامل لسبب فشل الإرسال للمجموعة الهدف (v10.1).

    يفحص عبر عميل حي (المراقب أولاً — بلا مخاطر AuthKeyDuplicated):
    1) هوية الحساب (get_me)
    2) نوع الهدف: قناة بث أم مجموعة خارقة + العنوان
    3) صلاحيات الحساب: admin_rights / creator / banned_rights
    4) default_banned_rights.send_messages → هل «فقط المشرفون يكتبون» مفعّل
    5) اختياري spambot=true: رسالة /start لـ @SpamBot وقراءة الرد —
       الفحص الحاسم لحظر/تقييد السبام (يشرح ChatAdminRequired الجماعي).
    قراءة فقط عدا رسالة @SpamBot الاختيارية — لا يغيّر أي حالة."""
    p = prefix.strip().upper()
    merged = await _merged_accounts(request)
    acc = _find_merged_account(merged, p)
    if acc is None:
        raise HTTPException(status_code=404, detail=f"الحساب {p} غير موجود")
    try:
        body = await request.json()
    except Exception:
        body = {}
    check_spambot = bool((body or {}).get("spambot", False))

    client = None
    tested_via = None
    temp_client = None
    bot = getattr(request.app.state, "bot_ref", None)
    if bot is not None:
        mon = bot.get_monitor_by_prefix(p)
        if mon is not None and mon.is_connected and mon.client is not None:
            client = mon.client
            tested_via = "live_monitor"
    if client is None:
        session_string = (acc.get("session_string") or "").strip()
        if not session_string:
            return JSONResponse({"success": False, "tested": "no_session",
                                 "message": "لا توجد جلسة لهذا الحساب"}, status_code=200)
        temp_client = TelegramClient(StringSession(session_string), int(acc.get("api_id") or 0),
                                     str(acc.get("api_hash") or ""), device_model="BR7-Diag",
                                     system_version="Linux", app_version="v10.1")
        await asyncio.wait_for(temp_client.connect(), timeout=30)
        if not await temp_client.is_user_authorized():
            return JSONResponse({"success": False, "tested": "temp_client_unauthorized",
                                 "message": "الجلسة منتهية أو ملغاة"}, status_code=200)
        client = temp_client
        tested_via = "temp_client"

    out: Dict[str, Any] = {"success": True, "tested_via": tested_via, "prefix": p}
    try:
        me = await client.get_me()
        out["me"] = {"id": me.id, "username": getattr(me, "username", None),
                     "name": (me.first_name or "") + ((" " + me.last_name) if me.last_name else "")}
        # ── فحص الهدف ──
        target = getattr(CFG, "TARGET_GROUP_ID", None)
        out["target_id"] = target
        if not target:
            out["target_error"] = "TARGET_GROUP_ID غير مضبوط"
        else:
            try:
                ent = await client.get_entity(int(target))
                broadcast = bool(getattr(ent, "broadcast", False))
                megagroup = bool(getattr(ent, "megagroup", False))
                out["target"] = {
                    "title": getattr(ent, "title", None),
                    "kind": "channel" if broadcast else ("megagroup" if megagroup else type(ent).__name__),
                    "creator": bool(getattr(ent, "creator", False)),
                    "has_admin_rights": bool(getattr(ent, "admin_rights", None)),
                    "admin_rights_detail": str(getattr(ent, "admin_rights", None))[:200],
                    "banned_rights": str(getattr(ent, "banned_rights", None))[:200],
                }
                dbr = getattr(ent, "default_banned_rights", None)
                if dbr is not None:
                    out["target"]["default_send_allowed"] = not bool(getattr(dbr, "send_messages", False))
                    out["target"]["default_banned_rights_raw"] = str(dbr)[:200]
            except Exception as e:
                out["target_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        # ── فحص SpamBot (اختياري — الحاسم لحظر السبام) ──
        if check_spambot:
            try:
                sb = await client.get_entity("SpamBot")
                await client.send_message(sb, "/start")
                await asyncio.sleep(5)
                msgs = await client.get_messages(sb, limit=1)
                out["spambot_reply"] = (msgs[0].message[:500] if (msgs and msgs[0]) else "لا يوجد رد")
            except Exception as e:
                out["spambot_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return JSONResponse(out)
    finally:
        if temp_client is not None:
            try:
                await temp_client.disconnect()
            except Exception:
                pass


@app.get("/api/messages", dependencies=[Depends(require_permission("messages.read"))])  # v9.32
async def get_messages(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    keyword: Optional[str] = None,
    exact: bool = False,
):
    """جلب الرسائل مع تصفية اختيارية.

    v9.16: exact=True تفعّل البحث بكلمة كاملة (word-boundary) بدل
    LIKE الجزئي — يدعم العربية (نطاق ‎\\u0600-\\u06FF) وغير حساس لحالة
    الأحرف اللاتينية. يُطبّق كتصفية لاحقة على نتائج LIKE لتقليل عبء SQL.
    """
    db = request.app.state.db
    try:
        rows = await db.get_messages_with_filters(limit=limit, offset=offset, keyword=keyword)
        if exact and keyword:
            pat = re.compile(
                rf"(?<![\w\u0600-\u06FF]){re.escape(keyword)}(?![\w\u0600-\u06FF])",
                re.IGNORECASE,
            )
            rows = [r for r in rows if r.get("message_text") and pat.search(str(r["message_text"]))]
        return JSONResponse({"messages": rows, "total": len(rows)})
    except Exception as e:
        logger.error(f"Error fetching messages: {e}")
        return JSONResponse({"messages": [], "total": 0})


@app.get("/api/alerts", dependencies=[Depends(require_permission("messages.read"))])  # v9.32
async def get_alerts(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    account: Optional[str] = None,
    keyword: Optional[str] = None,
    decision: Optional[str] = None,
    min_confidence: Optional[float] = None,
    hours: Optional[str] = None,
):
    """جلب التنبيهات مع تصفية متقدمة (بما فيها IntentEngine).

    v9.14: alert_text كان يُعاد كما هو من قاعدة البيانات متضمناً وسوم
    تيليجرام (<b>/<a>/<blockquote>) فتظهر حرفياً في لوحة BotPanel؛
    الآن يُنظّف قبل الإرجاع. كما أصبح total العدد الكلي الحقيقي بدل
    حجم الصفحة الحالية (كان يجعل شارة العدّاد خاطئة بعد أول 50 تنبيهاً).
    v9.17: فلتر نطاق زمني `hours` (1/24/168) يستخدم from_date الموجود
    أصلاً في طبقة قاعدة البيانات — سقف 168 ساعة (7 أيام). النوع str عمداً
    حتى لا يرفع FastAPI خطأ 422 على قيم غير رقمية — المعالج نفسه يتغاضى
    عنها بأمان (يرجع النتائج بلا فلتر زمني).
    """
    db = request.app.state.db
    from_ts: Optional[float] = None
    h_clamped: Optional[int] = None
    if hours is not None:
        try:
            h = max(1, min(int(hours), 168))
            from_ts = time.time() - h * 3600
            h_clamped = h
        except (TypeError, ValueError):
            from_ts = None
    try:
        rows = await db.get_alerts_with_filters(
            limit=limit, offset=offset, keyword=keyword, account=account,
            decision=decision, min_confidence=min_confidence,
            from_date=from_ts,
        )
        total = await db.count_alerts_with_filters(
            keyword=keyword, account=account,
            decision=decision, min_confidence=min_confidence,
            from_date=from_ts,
        )
        return JSONResponse({
            "alerts": [_clean_alert_row(r) for r in rows],
            "total": total,
            "count": len(rows),
            "filters": {
                "decision": decision,
                "min_confidence": min_confidence,
                "hours": h_clamped,
            },
        })
    except Exception as e:
        logger.error(f"Error fetching alerts: {e}")
        return JSONResponse({"alerts": [], "total": 0})


@app.get("/api/alerts/export", dependencies=[Depends(require_permission("messages.read"))])  # v9.32
async def export_alerts_csv(
    request: Request,
    account: Optional[str] = None,
    keyword: Optional[str] = None,
    decision: Optional[str] = None,
    hours: Optional[str] = None,
    limit: int = 5000,
):
    """v9.14: تصدير التنبيهات CSV من BotPanel (نفس الفلاتر النشطة).
    v9.17: يرث فلتر النطاق الزمني hours مثل /api/alerts."""
    db = request.app.state.db
    from_ts: Optional[float] = None
    if hours is not None:
        try:
            h = max(1, min(int(hours), 168))
            from_ts = time.time() - h * 3600
        except (TypeError, ValueError):
            from_ts = None
    rows: List[Dict[str, Any]] = []
    if db is not None:
        try:
            rows = await db.get_alerts_with_filters(
                limit=max(1, min(limit, 20000)), offset=0,
                keyword=keyword, account=account, decision=decision,
                from_date=from_ts,
            )
        except Exception as e:
            logger.error(f"Error exporting alerts: {e}")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["id", "timestamp", "keyword", "decision", "confidence",
         "sender_id", "sender_name", "username", "account", "chat_id", "text"]
    )
    for r in rows:
        writer.writerow([
            r.get("id"), r.get("timestamp"), r.get("keyword"),
            r.get("decision"), r.get("confidence"), r.get("sender_id"),
            " ".join(p for p in [r.get("first_name"), r.get("last_name")] if p) or "",
            r.get("username") or "", r.get("account_name") or "",
            r.get("chat_id") or "", _strip_html(r.get("alert_text") or "", 1000),
        ])
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return Response(
        content="\ufeff" + buf.getvalue(),  # BOM حتى يفتح الإكسل العربي سليماً
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="br7_alerts_{stamp}.csv"'},
    )


@app.get("/api/alerts/stats", dependencies=[Depends(require_permission("messages.read"))])  # v9.32
async def get_alerts_stats(request: Request):
    """إحصائيات التنبيهات (db_healthy / queue_evictions مضمّنة الآن)."""
    db = request.app.state.db
    try:
        summary = await db.get_dashboard_summary()
        return JSONResponse(summary)
    except Exception as e:
        logger.error(f"Error fetching alerts stats: {e}")
        return JSONResponse({})


@app.get("/api/dead-letters", dependencies=[Depends(require_permission("messages.read"))])  # v9.32
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


@app.get("/api/analytics", dependencies=[Depends(require_permission("messages.read"))])  # v9.32
async def get_analytics(request: Request, hours: int = 24):
    """Read-only analytics for the dashboard's statistics page.

    Aggregates three EXISTING database queries (no new tables, no writes):
      - get_hourly_stats(hours)   -> per-hour messages/alerts/accepted/conf
      - get_top_keywords(10)      -> keyword usage ranking
      - get_top_senders(10)       -> most active senders + reputation
    Never raises — every block degrades to an empty list.
    """
    db = request.app.state.db
    hours = max(1, min(int(hours or 24), 168))
    try:
        hourly = await db.get_hourly_stats(hours=hours)
    except Exception as e:
        logger.error(f"analytics hourly failed: {e}")
        hourly = []
    try:
        top_keywords = await db.get_top_keywords(limit=10)
    except Exception as e:
        logger.error(f"analytics top_keywords failed: {e}")
        top_keywords = []
    try:
        top_senders = await db.get_top_senders(limit=10)
    except Exception as e:
        logger.error(f"analytics top_senders failed: {e}")
        top_senders = []
    # v9.22-3: عدّادات خط الأنابيب من ذاكرة المراقبين — بلا استعلامات.
    counters = {"rules_blocked": 0, "allowlist_hits": 0, "source_skipped": 0}
    bot = getattr(request.app.state, "bot_ref", None)
    if bot is not None:
        try:
            for m in bot.monitors:
                s = await m.get_stats()
                for key in counters:
                    counters[key] += int(s.get(key, 0) or 0)
        except Exception as e:
            logger.debug(f"analytics counters skipped: {e}")
    return JSONResponse({
        "hours": hours,
        "hourly": hourly,
        "top_keywords": [dict(r) for r in top_keywords] if top_keywords else [],
        "top_senders": [dict(r) for r in top_senders] if top_senders else [],
        "counters": counters,
    })


@app.get("/api/keywords", dependencies=[Depends(require_permission("keywords.read"))])  # v9.32
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
        data = await _read_keywords_file(KEYWORDS_FILE)
        return JSONResponse({"keywords": data, "source": "disk"})
    except Exception as e:
        logger.error(f"get_keywords: failed reading {KEYWORDS_FILE}: {e}")
        return JSONResponse({"keywords": {}, "source": "error", "error": str(e)})


@app.post("/api/keywords", dependencies=[Depends(require_permission("keywords.write"))])  # v9.32
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
            all_data = await _read_keywords_file(KEYWORDS_FILE)
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

        await _write_keywords_file(all_data, KEYWORDS_FILE)
        reload_result = await _reload_filter_keywords(request.app)

        if not reload_result["applied"] and reload_result["error"] is not None:
            # Genuine reload failure (not just "bot not initialized yet") —
            # roll back the file so persisted and runtime state don't diverge.
            await _write_keywords_file(original_snapshot, KEYWORDS_FILE)
            raise HTTPException(
                status_code=500,
                detail=f"keywords.json rolled back — runtime reload failed: {reload_result['error']}",
            )

        # v3.1 — DIRECT SAVE: mirror the full keyword set to the database so
        # the edit survives Render redeploys (file system is ephemeral).
        persisted = False
        db = getattr(request.app.state, "db", None)
        if db is not None and db.is_connected:
            persisted = await dashboard_store.persist_keywords(db, all_data)

        await _audit(request, "keyword.add", object_type="keyword", object_id=path,
                     new_value=keyword)
        return JSONResponse({
            "success": True,
            "keyword": keyword,
            "category": path,
            "runtime_reloaded": reload_result["applied"],
            "persisted_to_db": persisted,
            "note": None if reload_result["applied"] else (reload_result.get("error") or reload_result.get("note")),
        })


@app.delete("/api/keywords", dependencies=[Depends(require_permission("keywords.delete"))])  # v9.32
async def delete_keyword(data: KeywordDelete, request: Request):
    """حذف كلمة/عبارة من قسم (list) داخل keywords.json — نفس ضمانات الإضافة."""
    keyword = data.keyword.strip()
    path = data.category.strip()

    async with _keywords_file_lock:
        try:
            all_data = await _read_keywords_file(KEYWORDS_FILE)
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

        await _write_keywords_file(all_data, KEYWORDS_FILE)
        reload_result = await _reload_filter_keywords(request.app)

        if not reload_result["applied"] and reload_result["error"] is not None:
            await _write_keywords_file(original_snapshot, KEYWORDS_FILE)
            raise HTTPException(
                status_code=500,
                detail=f"keywords.json rolled back — runtime reload failed: {reload_result['error']}",
            )

        # v3.1 — DIRECT SAVE: mirror the deletion to the database too.
        persisted = False
        db = getattr(request.app.state, "db", None)
        if db is not None and db.is_connected:
            persisted = await dashboard_store.persist_keywords(db, all_data)

        await _audit(request, "keyword.remove", object_type="keyword", object_id=path,
                     old_value=keyword)
        return JSONResponse({
            "success": True,
            "category": path,
            "runtime_reloaded": reload_result["applied"],
            "persisted_to_db": persisted,
            "note": None if reload_result["applied"] else (reload_result.get("error") or reload_result.get("note")),
        })


@app.get("/api/blocked/senders", dependencies=[Depends(require_permission("rules.read"))])  # v9.32
async def get_blocked_senders(request: Request):
    # NOTE: database.py has no public list-blocked-senders method; _fetchall
    # is used as it did in the previous version. See Remaining Issues.
    db = request.app.state.db
    rows = await db._fetchall("SELECT * FROM blocked_senders ORDER BY blocked_at DESC LIMIT 100")
    return JSONResponse({"senders": rows})


@app.get("/api/blocked/chats", dependencies=[Depends(require_permission("rules.read"))])  # v9.32
async def get_blocked_chats(request: Request):
    db = request.app.state.db
    rows = await db._fetchall("SELECT * FROM blocked_chats ORDER BY blocked_at DESC LIMIT 100")
    return JSONResponse({"chats": rows})


@app.post("/api/blocked/senders", dependencies=[Depends(require_permission("rules.write"))])  # v9.32
async def block_sender(data: BlockUser, request: Request):
    """v9.17: source يُمرَّر من العميل (dashboard/alert) مع قائمة سماح —
    كانت تُسجَّل كل حظرات اللوحة كمصدر dashboard حتى لو أُرسلت من مودال
    التنبيه فتظهر شرائح المصدر في صفحة الحظر غير دقيقة."""
    db = request.app.state.db
    allowed = {"dashboard", "alert", "system"}
    src = (data.source or "dashboard").strip().lower()
    if src not in allowed:
        src = "dashboard"
    await db.block_sender(data.user_id, data.reason, src)
    await _audit(request, "block.sender", object_type="sender", object_id=str(data.user_id),
                 new_value=data.reason or "")
    return JSONResponse({"success": True, "source": src})


@app.delete("/api/blocked/senders/{user_id}", dependencies=[Depends(require_permission("rules.delete"))])  # v9.32
async def unblock_sender(user_id: int, request: Request):
    db = request.app.state.db
    await db.unblock_sender(user_id)
    await _audit(request, "unblock.sender", object_type="sender", object_id=str(user_id))
    return JSONResponse({"success": True})


@app.post("/api/blocked/chats", dependencies=[Depends(require_permission("rules.write"))])  # v9.32
async def block_chat(data: BlockChat, request: Request):
    db = request.app.state.db
    await db.block_chat(data.chat_id, data.reason, "dashboard")
    await _audit(request, "block.chat", object_type="chat", object_id=str(data.chat_id),
                 new_value=data.reason or "")
    return JSONResponse({"success": True})


@app.delete("/api/blocked/chats/{chat_id}", dependencies=[Depends(require_permission("rules.delete"))])  # v9.32
async def unblock_chat(chat_id: int, request: Request):
    db = request.app.state.db
    await db.unblock_chat(chat_id)
    await _audit(request, "unblock.chat", object_type="chat", object_id=str(chat_id))
    return JSONResponse({"success": True})


@app.get("/api/rules", dependencies=[Depends(require_permission("rules.read"))])  # v9.32
async def get_rules(request: Request):
    """v9.21 P2: قائمة القواعد بترتيب التقييم."""
    db = request.app.state.db
    rows = await db.list_rules() if db else []
    return JSONResponse({
        "items": rows,
        "count": len(rows),
        "limit": 50,
        "enabled_count": sum(1 for r in rows if r.get("enabled")),
    })


@app.post("/api/rules", dependencies=[Depends(require_permission("rules.write"))])  # v9.32
async def add_rule(data: RuleCreate, request: Request):
    """v9.21 P2: إضافة قاعدة — تحقق صارم + حد 50 قاعدة (409)."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    if await db.count_rules() >= 50:
        raise HTTPException(status_code=409, detail="Rule limit reached (50). Remove a rule first.")
    clean, err = db.validate_rule_conditions(data.conditions)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if data.action not in db.RULE_ACTIONS:
        raise HTTPException(status_code=400, detail=f"unknown action: {data.action}")
    if data.action == "tag" and not data.action_value.strip():
        raise HTTPException(status_code=400, detail="tag action requires action_value")
    row = await db.add_rule(data.name, clean, data.action, data.action_value, data.priority)
    if row is None:
        raise HTTPException(status_code=500, detail="Failed to save rule")
    await _audit(request, "rule.add", object_type="rule", object_id=str(row["id"]),
                 new_value=json.dumps({"name": data.name, "conditions": clean,
                                       "action": data.action, "priority": data.priority},
                                      ensure_ascii=False, default=str)[:2000])
    return JSONResponse({"success": True, "rule": row})


@app.post("/api/rules/{rule_id}/edit", dependencies=[Depends(require_permission("rules.write"))])  # v9.32
async def edit_rule(rule_id: int, data: RuleCreate, request: Request):
    """v9.22-2: تعديل قاعدة (جزئي) — نفس تحقق الإضافة، لا يلمس enabled/hits."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    before = await db.get_rule(rule_id)
    if before is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    updates: Dict[str, Any] = {}
    if data.name is not None:
        updates["name"] = data.name
    if data.conditions is not None:
        clean, err = db.validate_rule_conditions(data.conditions)
        if err:
            raise HTTPException(status_code=400, detail=err)
        updates["conditions"] = clean
    if data.action is not None:
        if data.action not in db.RULE_ACTIONS:
            raise HTTPException(status_code=400, detail=f"unknown action: {data.action}")
        if data.action == "tag" and not (data.action_value or "").strip():
            raise HTTPException(status_code=400, detail="tag action requires action_value")
        updates["action"] = data.action
    updates["action_value"] = data.action_value or ""
    updates["priority"] = data.priority
    try:
        after = await db.update_rule(rule_id, updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if after is None:
        raise HTTPException(status_code=500, detail="Failed to update rule")
    await _audit(request, "rule.edit", object_type="rule", object_id=str(rule_id),
                 old_value=json.dumps({"name": before.get("name"), "conditions": before.get("conditions"),
                                       "action": before.get("action"), "priority": before.get("priority")},
                                      ensure_ascii=False, default=str)[:2000],
                 new_value=json.dumps({"name": after.get("name"), "conditions": after.get("conditions"),
                                       "action": after.get("action"), "priority": after.get("priority")},
                                      ensure_ascii=False, default=str)[:2000])
    return JSONResponse({"success": True, "rule": after})


@app.post("/api/rules/{rule_id}/toggle", dependencies=[Depends(require_permission("rules.write"))])  # v9.32
async def toggle_rule(rule_id: int, request: Request):
    """v9.21 P2: تفعيل/إيقاف قاعدة — مدقَّق."""
    db = request.app.state.db
    row = await db.get_rule(rule_id) if db else None
    if row is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    new_state = not bool(row.get("enabled"))
    await db.set_rule_enabled(rule_id, new_state)
    await _audit(request, "rule.toggle", object_type="rule", object_id=str(rule_id),
                 old_value=str(bool(row.get("enabled"))), new_value=str(new_state))
    return JSONResponse({"success": True, "id": rule_id, "enabled": new_state})


@app.delete("/api/rules/{rule_id}", dependencies=[Depends(require_permission("rules.delete"))])  # v9.32
async def delete_rule(rule_id: int, request: Request):
    """v9.21 P2: حذف قاعدة — مدقَّق."""
    db = request.app.state.db
    ok = await db.delete_rule(rule_id) if db else False
    if not ok:
        raise HTTPException(status_code=404, detail="Rule not found")
    await _audit(request, "rule.remove", object_type="rule", object_id=str(rule_id))
    return JSONResponse({"success": True})


@app.get("/api/allowed", dependencies=[Depends(require_permission("rules.read"))])  # v9.32
async def get_allowed(request: Request):
    """v9.21 P3: قائمة الكيانات الموثوقة."""
    db = request.app.state.db
    rows = await db.list_allowed_entities() if db else []
    return JSONResponse({
        "items": rows,
        "count": len(rows),
        "note": "الكيان الموثوق يتجاوز فلترة الكلمات فقط — لا يتجاوز الحظر ولا مكافحة السبام",
    })


@app.post("/api/allowed", dependencies=[Depends(require_permission("rules.write"))])  # v9.32
async def add_allowed(data: AllowedCreate, request: Request):
    """v9.21 P3: إضافة كيان موثوق — مدقَّق."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    ok = await db.add_allowed_entity(data.entity_type, data.entity_id, data.note)
    if not ok:
        raise HTTPException(status_code=400, detail="entity_type must be sender|chat")
    await _audit(request, "allow.add", object_type=data.entity_type,
                 object_id=str(data.entity_id), new_value=data.note or "")
    return JSONResponse({"success": True})


@app.delete("/api/allowed/{entity_type}/{entity_id}", dependencies=[Depends(require_permission("rules.delete"))])  # v9.32
async def remove_allowed(entity_type: str, entity_id: int, request: Request):
    """v9.21 P3: حذف كيان موثوق — مدقَّق."""
    db = request.app.state.db
    ok = await db.remove_allowed_entity(entity_type, entity_id) if db else False
    if not ok:
        raise HTTPException(status_code=404, detail="Entity not found")
    await _audit(request, "allow.remove", object_type=entity_type, object_id=str(entity_id))
    return JSONResponse({"success": True})


@app.get("/api/notifications", dependencies=[Depends(require_permission("notifications.read"))])  # v9.32
async def get_notifications(request: Request, limit: int = 30, offset: int = 0,
                            unread_only: bool = False):
    """v9.24 P5-1: قائمة الإشعارات + عدّاد غير المقروء (للشارة الحية)."""
    db = request.app.state.db
    items = await db.get_notifications(limit=limit, offset=offset, unread_only=unread_only) if db else []
    unread = await db.count_unread_notifications() if db else 0
    return JSONResponse({"items": items, "unread": unread})


class NotificationReadBody(BaseModel):
    id: int


@app.post("/api/notifications/read", dependencies=[Depends(require_permission("notifications.read"))])  # v9.32
async def read_notification(data: NotificationReadBody, request: Request):
    """v9.24: تعليم إشعار واحداً كمقروء — تحقق id رقمي في النموذج."""
    db = request.app.state.db
    ok = await db.mark_notification_read(data.id) if db else False
    if not ok:
        raise HTTPException(status_code=404, detail="Notification not found")
    unread = await db.count_unread_notifications() if db else 0
    return JSONResponse({"success": True, "unread": unread})


@app.post("/api/notifications/read-all", dependencies=[Depends(require_permission("notifications.read"))])  # v9.32
async def read_all_notifications(request: Request):
    """v9.24: تعليم كل الإشعارات كمقروءة."""
    db = request.app.state.db
    changed = await db.mark_all_notifications_read() if db else 0
    return JSONResponse({"success": True, "marked": changed, "unread": 0})


@app.get("/api/features", dependencies=[Depends(require_permission("settings.read"))])  # v9.32
async def get_features(request: Request):
    """v9.25 P6-1: قائمة سجل الميزات مع الحالة الفعلية."""
    db = request.app.state.db
    items = await db.list_features() if db else []
    return JSONResponse({"items": items, "count": len(items)})


@app.post("/api/features/{key}/toggle", dependencies=[Depends(require_permission("settings.write"))])  # v9.32
async def toggle_feature(key: str, request: Request):
    """v9.25 P6-1: تبديل ميزة — 404 خارج السجل، مدقَّق + إشعار عند الإيقاف."""
    db = request.app.state.db
    if db is None or key not in db.FEATURE_REGISTRY:
        raise HTTPException(status_code=404, detail="Unknown feature key")
    meta = db.FEATURE_REGISTRY[key]
    before = await db.is_feature_enabled(key)
    new_state = not before
    ok = await db.set_feature_enabled(key, new_state, by="botpanel-admin")
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to toggle feature")
    await _audit(request, "feature.toggle", object_type="feature", object_id=key,
                 old_value=str(before), new_value=str(new_state))
    if not new_state:
        try:
            await db.record_notification(
                ntype="feature.disabled", title=f"⚙️ الميزة {meta['label']} أوقفت",
                body=meta.get("impact") or "", severity="warn",
                object_type="feature", object_id=key,
            )
        except Exception:
            pass
    return JSONResponse({"success": True, "key": key, "enabled": new_state})


@app.get("/api/sources", dependencies=[Depends(require_permission("sources.read"))])  # v9.32
async def get_sources(request: Request):
    """v9.20 P1: قائمة مصادر المراقبة + شارة الوضع.

    mode: "all" = لا مصادر مفعّلة → مراقبة كل شيء (السلوك الأصلي)؛
    "filtered" = المصادر المفعّلة فقط.
    """
    db = request.app.state.db
    rows = await db.list_sources() if db else []
    enabled = [r for r in rows if r.get("enabled")]
    return JSONResponse({
        "items": rows,
        "count": len(rows),
        "enabled_count": len(enabled),
        "mode": "filtered" if enabled else "all",
        "note": "جدول فارغ أو بلا مصادر مفعّلة = مراقبة كل شيء (السلوك الأصلي)",
    })


@app.post("/api/sources", dependencies=[Depends(require_permission("sources.write"))])  # v9.32
async def add_source(data: SourceCreate, request: Request):
    """v9.20 P1: إضافة/تحديث مصدر (upsert حسب chat_id) — مدقَّق."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    ok = await db.add_source(
        data.chat_id, username=data.username, title=data.title,
        type_=data.type, notes=data.notes,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to save source")
    await _audit(request, "source.add", object_type="source",
                 object_id=str(data.chat_id),
                 new_value=data.title or data.username or "")
    return JSONResponse({"success": True, "chat_id": data.chat_id})


@app.delete("/api/sources/{chat_id}", dependencies=[Depends(require_permission("sources.delete"))])  # v9.32
async def remove_source(chat_id: int, request: Request):
    """v9.20 P1: حذف مصدر بالمفتاح chat_id — مدقَّق."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    ok = await db.remove_source(chat_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Source not found")
    await _audit(request, "source.remove", object_type="source", object_id=str(chat_id))
    return JSONResponse({"success": True})


@app.post("/api/sources/{chat_id}/toggle", dependencies=[Depends(require_permission("sources.write"))])  # v9.32
async def toggle_source(chat_id: int, request: Request):
    """v9.20 P1: تفعيل/إيقاف مصدر — مدقَّق."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    row = await db.get_source(chat_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Source not found")
    new_state = not bool(row.get("enabled"))
    await db.set_source_enabled(chat_id, new_state)
    await _audit(request, "source.toggle", object_type="source", object_id=str(chat_id),
                 old_value=str(bool(row.get("enabled"))), new_value=str(new_state))
    return JSONResponse({"success": True, "chat_id": chat_id, "enabled": new_state})


@app.get("/api/audit", dependencies=[Depends(require_permission("audit.read"))])  # v9.32
async def get_audit(
    request: Request,
    limit: int = 200,
    offset: int = 0,
    action: str = "",
    actor: str = "",
    q: str = "",
    since_hours: Optional[str] = None,
):
    """v9.18 P0: read the audit log (newest first). Read-only, fail-safe.

    v9.27: فلاتر actor/q/since_hours — since_hours نصي عمداً (نمط
    /api/alerts): القيم الفاسدة تُعامل «الكل» دون 422.
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"items": [], "count": 0})
    try:
        rows = await db.get_audit_logs(
            limit=limit, offset=offset, action=action or None,
            actor=actor or None, q=q or None, since_hours=since_hours,
        )
    except Exception:
        rows = []
    count = 0
    try:
        count = await db.count_audit_logs(action=action or None, actor=actor or None,
                                          q=q or None, since_hours=since_hours)
    except Exception:
        count = len(rows)
    return JSONResponse({"items": rows, "count": count})


@app.get("/api/audit/export", dependencies=[Depends(require_permission("audit.read"))])  # v9.32
async def export_audit_csv(
    request: Request,
    action: str = "",
    actor: str = "",
    q: str = "",
    since_hours: Optional[str] = None,
):
    """v9.27: تصدير سجل التدقيق CSV بنفس الفلاتر — 20 ألف صف كحد أعلى،
    BOM للإكسل العربي، اسم ملف مؤرخ، 401 بلا توكن (تلقائياً من HTTPBearer)."""
    db = getattr(request.app.state, "db", None)
    rows = []
    if db is not None:
        try:
            rows = await db.get_audit_logs(
                limit=20000, offset=0, action=action or None,
                actor=actor or None, q=q or None, since_hours=since_hours,
            )
        except Exception:
            rows = []

    def _render() -> str:
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["id", "created_at", "actor", "action", "object_type",
                    "object_id", "old_value", "new_value", "source"])
        for a in rows:
            w.writerow([
                a.get("id", ""), a.get("created_at", ""), a.get("actor", ""),
                a.get("action", ""), a.get("object_type", ""), a.get("object_id", ""),
                a.get("old_value", ""), a.get("new_value", ""), a.get("source", ""),
            ])
        return "\ufeff" + buf.getvalue()

    csv_text = await asyncio.get_event_loop().run_in_executor(None, _render)
    await _audit(request, "audit.export", object_type="audit",
                 new_value=str(len(rows)))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="br7-audit-{stamp}.csv"'},
    )


@app.get("/api/backup/export", dependencies=[Depends(require_permission("backup.read"))])  # v9.32
async def backup_export(request: Request):
    """v9.22-1: تصدير البيانات الحرجة JSON — تنزيل برأس Content-Disposition."""
    db = request.app.state.db
    payload = await db.export_critical_data() if db else {"version": 1, "tables": {}}
    await _audit(request, "data.export", object_type="backup",
                 new_value=json.dumps(list((payload.get("tables") or {}).keys())))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="br7-critical-{stamp}.json"'},
    )


@app.post("/api/backup/import", dependencies=[Depends(require_permission("backup.write"))])  # v9.32
async def backup_import(request: Request):
    """v9.22-1: استيراد البيانات الحرجة — نسخة أمان أولاً، 400 إن لم يُستورد شيء."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON غير صالح")
    if not isinstance(body, dict) or not isinstance(body.get("tables"), dict):
        raise HTTPException(status_code=400, detail="صيغة الملف غير صالحة (توقع tables)")

    # نسخة أمان تلقائية pre-import-*.json في backups/ قبل أي كتابة —
    # نُبقي آخر 20 نسخة. فشل النسخة لا يمنع الاستعادة.
    safety_name = ""
    try:
        import os as _os
        _os.makedirs("backups", exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safety_name = f"pre-import-{stamp}.json"
        safety_payload = await db.export_critical_data()
        with open(_os.path.join("backups", safety_name), "w", encoding="utf-8") as f:
            json.dump(safety_payload, f, ensure_ascii=False)
        # احتفظ بآخر 20
        try:
            old = sorted(
                (n for n in _os.listdir("backups") if n.startswith("pre-import-")),
                reverse=True,
            )
            for n in old[20:]:
                _os.unlink(_os.path.join("backups", n))
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"pre-import safety backup failed: {e}")
        safety_name = ""

    counts = await db.import_critical_data(body, replace=True)
    if not counts:
        raise HTTPException(status_code=400, detail="لم يُستورد أي صف — تحقق من صيغة الملف")
    await _audit(request, "data.import", object_type="backup",
                 new_value=json.dumps(counts, ensure_ascii=False)[:2000])
    return JSONResponse({
        "success": True,
        "imported": counts,
        "safety_backup": safety_name,
    })


@app.get("/api/settings", dependencies=[Depends(require_permission("settings.read"))])  # v9.32
async def get_settings(request: Request):
    """
    v3.1: current effective values + editable schema + persistence status.
    This is what the dynamic Settings tab renders from — no more hardcoded
    placeholder values in the UI.
    """
    db = getattr(request.app.state, "db", None)
    return JSONResponse(
        {
            "success": True,
            "settings": dashboard_store.current_values(request.app),
            "schema": dashboard_store.schema_for_ui(),
            "persistence": {
                "db_type": getattr(db, "db_type", "unknown") if db else "none",
                "enabled": db is not None and db.is_connected,
                "table": "app_settings",
            },
        }
    )


@app.post("/api/settings", dependencies=[Depends(require_permission("settings.write"))])  # v9.32
async def update_settings(data: SettingsBody, request: Request):
    """
    v3.1 — DIRECT SAVE: every validated change is written to the
    `app_settings` table in the SAME database the bot already uses
    (PostgreSQL on Render), BEFORE being applied live. Survives redeploys.

    Live-apply strategy per field:
      * CFG-backed fields  → object.__setattr__ on the frozen dataclass
        (CFG is read at call time by monitors/filter_engine, so changes
        take effect on the next processed message — no restart).
      * Rate-limiter fields → applied to bot.rate_limiter directly.

    Accepts both {"updates": {...}} and the legacy flat body.
    """
    updates = data.updates if isinstance(data.updates, dict) else data.model_dump(exclude={"updates"}, exclude_none=True)
    if not isinstance(updates, dict) or not updates:
        raise HTTPException(status_code=400, detail="لا توجد إعدادات صالحة في الطلب")

    clean, errors = dashboard_store.validate_updates(updates)

    db = getattr(request.app.state, "db", None)
    persisted = False
    if clean and db is not None and db.is_connected:
        try:
            # 1) DIRECT SAVE first — DB is the source of truth
            await dashboard_store.set_many(db, {k: json.dumps(v) for k, v in clean.items()})
            persisted = True
        except Exception as e:
            logger.error(f"settings persist to DB failed: {e}")

    # 2) live-apply after the write-through succeeded (or db unavailable)
    applied: Dict[str, Any] = {}
    stored: Dict[str, Any] = {}
    for key, value in clean.items():
        status = dashboard_store.apply_setting(request.app, key, value)
        (applied if status == "applied" else stored)[key] = value

    if clean:
        await _audit(request, "settings.update", object_type="settings",
                     new_value=json.dumps(clean, ensure_ascii=False, default=str)[:2000])
    return JSONResponse(
        {
            "success": not errors,
            "applied_live": applied,
            "stored_only": stored,
            "errors": errors,
            "persisted_to_db": persisted,
            "settings": dashboard_store.current_values(request.app),
        }
    )


@app.post("/api/purge", dependencies=[Depends(require_permission("settings.write"))])  # v9.32
async def purge_queue(request: Request):
    db = request.app.state.db
    count = await db.purge_queue()
    await _audit(request, "queue.purge", object_type="queue", new_value=str(count))
    return JSONResponse({"success": True, "purged": count})


@app.post("/api/restart", dependencies=[Depends(require_permission("deployment.execute"))])  # v9.32
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
    await _audit(request, "bot.restart", object_type="bot")
    # v9.30: مولّد إشعار — إعادة التشغيل حدث حرج يستحق ظهور الجرس.
    _track_local_task(db_record_restart_note(request), "notify_restart")
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


async def db_record_restart_note(request: Request) -> None:
    """v9.30: إشعار critical عند طلب إعادة تشغيل — فشل-آمن تماماً."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return
    try:
        await db.record_notification(
            ntype="bot.restart", title="🔁 طُلبت إعادة تشغيل البوت",
            body="إيقاف رشيق قيد التنفيذ — المنصة (Render) ستعيد إقلاع العملية.",
            severity="critical", object_type="bot",
        )
    except Exception:
        pass


# =============================================================================
# v9.29 P4 RBAC — مصادقة وإدارة مستخدمي اللوحة (Master Prompt 8/9)
# =============================================================================

class UserLoginBody(BaseModel):
    username: str
    password: str


class UserCreateBody(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class UserPatchBody(BaseModel):
    enabled: Optional[bool] = None
    role: Optional[str] = None
    password: Optional[str] = None


def _actor_of(principal: Dict[str, Any]) -> str:
    """v9.29: فاعل التدقيق — master يبقى botpanel-admin (توافق تاريخي)،
    ومستخدم dashboard_users يوقَّع panel:<username>."""
    if principal.get("via") == "master_token":
        return "botpanel-admin"
    return f"panel:{principal.get('username', 'unknown')}"


@app.post("/api/auth/login")
async def auth_login(body: UserLoginBody, request: Request):
    """v9.29: دخول باسم/كلمة مرور → توكن مستخدم موقّع (12 ساعة).

    الفشل يغذي نفس حارس القفل (نفس عدّاد التوكن الرئيسي) — لا باب جانبي
    للتخمين. النجاح يُدقَّق user.login ويحدّث last_login_at.
    """
    _guard_reject_locked(request)
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not ready")
    username = (body.username or "").strip()[:64]
    # include_hash=True للمصادقة فقط — لا يخرج الـhash في أي استجابة.
    user = await db.get_dashboard_user_by_name(username, include_hash=True)
    ok = (
        user is not None
        and bool(user.get("enabled"))
        and EnhancedDatabase.verify_dashboard_password(
            body.password or "", user.get("password_hash", "")
        )
    )
    if not ok:
        _guard_note_failure(request, source=f"login:{username[:32]}")
        raise HTTPException(status_code=401, detail="Invalid credentials")
    _auth_guard.record_success(_auth_guard.client_ip(request))
    token, exp = make_user_token(user["username"], user["role"])
    _track_local_task(db.touch_dashboard_user_login(user["id"]), "user_login_touch")
    _track_local_task(db.record_audit(
        actor=f"panel:{user['username']}", action="user.login",
        object_type="dashboard_user", object_id=str(user["id"]),
        new_value=user["role"], source="botpanel",
    ), "audit_user_login")
    return JSONResponse({
        "success": True,
        "token": token,
        "token_type": "bearer",
        "expires_at": exp,
        "username": user["username"],
        "role": user["role"],
        "permissions": sorted(principal_permissions(user["role"])),
    })


@app.get("/api/auth/me")
async def auth_me(principal: Principal = Depends(require_permission(None))):
    """v9.29: هوية المتصل الحالي — للواجهات لتعرض الدور والصلاحيات."""
    return JSONResponse({
        "username": principal["username"],
        "role": principal["role"],
        "permissions": principal["permissions"],
        "via": principal["via"],
    })


@app.get("/api/users")
async def users_list(request: Request,
                     principal: Principal = Depends(require_permission("users.read"))):
    """v9.29: قائمة مستخدمي اللوحة (users.read — admin فأعلى)."""
    db = request.app.state.db
    users = await db.list_dashboard_users(include_deleted=True, limit=500)
    roles = {
        r: sorted(perms) if "*" not in perms else ["*"]
        for r, perms in ROLE_PERMISSIONS.items()
    }
    return JSONResponse({"users": users, "roles": roles, "total": len(users)})


@app.post("/api/users")
async def users_create(body: UserCreateBody, request: Request,
                       principal: Principal = Depends(require_permission("users.write"))):
    """v9.29: إنشاء مستخدم لوحة — تدقيق + إشعار + تحقق صارم من المدخلات."""
    db = request.app.state.db
    username = (body.username or "").strip()
    if not _USERNAME_RE.match(username):
        raise HTTPException(
            status_code=400,
            detail="Username must be 3-32 chars: letters, digits, dot, dash, underscore",
        )
    if len(body.password or "") < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if body.role not in EnhancedDatabase.DASHBOARD_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown role. Valid roles: {', '.join(EnhancedDatabase.DASHBOARD_ROLES)}",
        )
    # users.write يكفي لإدارة غير الـsuper_admin — إنشاء حساب super_admin
    # يتطلب الثقة المطلقة (master token أو حساب super_admin آخر).
    if body.role == "super_admin" and principal.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Only super_admin can create super_admin")
    created = await db.create_dashboard_user(username, body.password, body.role)
    if created is None:
        raise HTTPException(status_code=409, detail="Username already exists")
    await _audit_actor(request, principal, "user.create", object_type="dashboard_user",
                       object_id=str(created["id"]),
                       new_value=f"{username}:{body.role}")
    _track_local_task(db.record_notification(
        ntype="user.created", title=f"👤 مستخدم لوحة جديد: {username}",
        body=f"الدور: {body.role} — بواسطة {_actor_of(principal)}",
        severity="info", object_type="dashboard_user", object_id=str(created["id"]),
    ), "notify_user_created")
    return JSONResponse({"success": True, "user": created})


@app.patch("/api/users/{user_id}")
async def users_patch(user_id: int, body: UserPatchBody, request: Request,
                      principal: Principal = Depends(require_permission("users.write"))):
    """v9.29: تعديل مستخدم (تفعيل/تعطيل، دور، كلمة مرور) — تدقيق قبل/بعد."""
    db = request.app.state.db
    target = await db.get_dashboard_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    # حماية الذات: لا تعطيل/حذف حسابك أثناء استخدامه (self-lockout guard).
    is_self = (
        principal.get("via") == "user_token"
        and principal.get("username") == target["username"]
    )
    changes: List[str] = []
    if body.enabled is not None:
        if is_self and not body.enabled:
            raise HTTPException(status_code=400, detail="Cannot disable your own account")
        if not await db.set_dashboard_user_enabled(user_id, bool(body.enabled)):
            raise HTTPException(status_code=500, detail="Failed to update user")
        changes.append(f"enabled={bool(body.enabled)}")
    if body.role is not None:
        if body.role not in EnhancedDatabase.DASHBOARD_ROLES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown role. Valid roles: {', '.join(EnhancedDatabase.DASHBOARD_ROLES)}",
            )
        if body.role == "super_admin" and principal.get("role") != "super_admin":
            raise HTTPException(status_code=403, detail="Only super_admin can grant super_admin")
        if is_self and body.role != "super_admin" and principal.get("role") == "super_admin":
            raise HTTPException(status_code=400, detail="Cannot demote your own account")
        if not await db.set_dashboard_user_role(user_id, body.role):
            raise HTTPException(status_code=500, detail="Failed to update user")
        changes.append(f"role:{target['role']}→{body.role}")
    if body.password is not None:
        if len(body.password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        if not await db.set_dashboard_user_password(user_id, body.password):
            raise HTTPException(status_code=500, detail="Failed to update user")
        changes.append("password=RESET")
    if not changes:
        raise HTTPException(status_code=400, detail="Nothing to update (send enabled/role/password)")
    await _audit_actor(request, principal, "user.update", object_type="dashboard_user",
                       object_id=str(user_id),
                       old_value=f"{target['username']}:{target['role']}",
                       new_value=";".join(changes))
    updated = await db.get_dashboard_user(user_id)
    return JSONResponse({"success": True, "user": updated, "changes": changes})


@app.delete("/api/users/{user_id}")
async def users_delete(user_id: int, request: Request,
                       principal: Principal = Depends(require_permission("users.delete"))):
    """v9.29: حذف ناعم (Soft Delete — Master Prompt 10.2) — يبقى للتدقيق."""
    db = request.app.state.db
    target = await db.get_dashboard_user(user_id)
    if target is None or target.get("deleted_at"):
        raise HTTPException(status_code=404, detail="User not found")
    if (principal.get("via") == "user_token"
            and principal.get("username") == target["username"]):
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if not await db.soft_delete_dashboard_user(user_id):
        raise HTTPException(status_code=500, detail="Failed to delete user")
    await _audit_actor(request, principal, "user.delete", object_type="dashboard_user",
                       object_id=str(user_id),
                       old_value=f"{target['username']}:{target['role']}",
                       new_value="soft-deleted")
    return JSONResponse({"success": True, "deleted": target["username"]})


async def _audit_actor(request: Request, principal: Dict[str, Any], action: str,
                       object_type: str = "", object_id: str = "",
                       old_value: str = "", new_value: str = "") -> None:
    """v9.29: تدقيق بفاعل متغير (master أو panel:<username>) — fire-and-forget."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return
    try:
        _track_local_task(
            db.record_audit(
                actor=_actor_of(principal), action=action, object_type=object_type,
                object_id=object_id, old_value=old_value, new_value=new_value,
                source="botpanel",
            ),
            "audit_write",
        )
    except Exception:
        pass


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

    async def _drop_entry(self, entry: Optional[Dict[str, Any]]) -> None:
        """v9.37: تنظيف موحّد لعملية معلّقة — إلغاء منتظر QR (إن وجد) ثم
        قطع اتصال العميل بأمان. آمن على عمليات OTP العادية (بلا منتظر)."""
        if not entry:
            return
        waiter = entry.get("waiter")
        if waiter is not None:
            try:
                waiter.cancel()
            except Exception:
                pass
        client = entry.get("client")
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _purge_old(self) -> None:
        now = time.time()
        for prefix in list(self._pending.keys()):
            entry = self._pending.get(prefix)
            if entry and now - entry.get("ts", 0) > 600:
                await self._drop_entry(entry)
                self._pending.pop(prefix, None)

    async def start(self, prefix: str, api_id: int, api_hash: str, phone: str,
                    force_sms: bool = False) -> Dict[str, Any]:
        async with self._lock:
            await self._purge_old()
            await self._drop_entry(self._pending.pop(prefix, None))
            client = TelegramClient(
                StringSession(), api_id=api_id, api_hash=api_hash,
                device_model="Render Cloud", system_version="Linux", app_version="13.1",
                timeout=30, connection_retries=3,
            )
            await client.connect()
            # v9.37: force_sms يوجّه تيليجرام لإرسال الرمز كرسالة نصية إلى
            # الشريحة مباشرة — الحل الحاسم حين لا تصلك رسائل التطبيق
            # (جلسات قديمة على أجهزة غير موجودة تستلمها بدلاً منك).
            sent = await client.send_code_request(phone, force_sms=bool(force_sms))
            delivery, delivery_hint = _otp_delivery_info(sent)
            # v9.35: تسجيل وسيلة التوصيل فقط (بدون أي بيانات حساسة) — هذا
            # السطر هو ما يكشف لماذا «لا يصل الرمز» (مثلاً: flash-call).
            logger.info(f"send-code [{prefix}]: delivery={delivery} force_sms={bool(force_sms)}")
            self._pending[prefix] = {
                "client": client,
                "phone": phone,
                "phone_code_hash": sent.phone_code_hash,
                "ts": time.time(),
            }
            return {"sent": True, "code_type": str(sent.type),
                    "delivery": delivery, "delivery_hint": delivery_hint}

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

    # ── v9.37: تسجيل دخول QR — بديل مباشر لا يعتمد على رمز OTP إطلاقاً ──
    # المستخدم يفتح الرابط من تطبيق الحساب نفسه (المؤكد دخوله) أو يمسح
    # صورة QR فيُصرَّح للجلسة الجديدة فوراً — بلا رمز وبلا SMS.

    async def start_qr(self, prefix: str, api_id: int, api_hash: str) -> Dict[str, Any]:
        """بدء عملية دخول QR: عميل جديد غير مُصرَّح يطلب رمز رابط (token).
        مهم (توثيق Telethon): wait() يجب أن تعمل أثناء فتح الرابط/المسح —
        لذا تُشغَّل كمهمة خلفية داخل العملية المعلّقة نفسها."""
        async with self._lock:
            await self._purge_old()
            await self._drop_entry(self._pending.pop(prefix, None))
            client = TelegramClient(
                StringSession(), api_id=api_id, api_hash=api_hash,
                device_model="Render Cloud", system_version="Linux", app_version="13.1",
                timeout=30, connection_retries=3,
            )
            await client.connect()
            qr = await client.qr_login()
            entry: Dict[str, Any] = {
                "type": "qr", "client": client, "qr": qr,
                "phone": None, "phone_code_hash": None, "ts": time.time(),
                "status": "waiting", "result": None, "waiter": None,
            }
            self._pending[prefix] = entry
            entry["waiter"] = asyncio.create_task(self._qr_waiter(entry))
            logger.info(f"qr-login [{prefix}]: token issued (expires in {_qr_expires_in(qr)}s)")
            return {"url": qr.url, "expires_in": _qr_expires_in(qr)}

    async def _qr_waiter(self, entry: Dict[str, Any]) -> None:
        """ينتظر تأكيد الرابط من جهاز مؤكد الدخول. النتيجة تُخزَّن في
        entry — النجاح يحفظ session_string، والأخطاء تُصنَّف للتجديد أو 2FA.
        أي فشل لا يُرفع أبداً (مهمة خلفية)."""
        try:
            me = await entry["qr"].wait()
            if me is None:
                me = await entry["client"].get_me()
            entry["result"] = {
                "done": True,
                "user": (f"@{me.username}" if getattr(me, "username", None)
                         else (getattr(me, "first_name", None) or str(me.id))),
                "user_id": me.id,
                "session_string": entry["client"].session.save(),
            }
            entry["status"] = "authorized"
        except asyncio.CancelledError:
            raise
        except SessionPasswordNeededError:
            entry["status"] = "need_password"
        except Exception as e:
            # انتهاء صلاحية الرمز (TimeoutError) أو غيره — تُصنَّف للتجديد
            entry["status"] = "expired"
            entry["error"] = f"{type(e).__name__}"

    async def qr_status(self, prefix: str) -> Dict[str, Any]:
        """استطلاع حالة عملية QR: النتيجة عند النجاح (مع تنظيف العملية)،
        طلب كلمة المرور عند 2FA، أو رابط مجدَّد تلقائياً عند انتهاء الصلاحية.
        رمز QR قصير العمر (~30-60 ثانية) فيُجدَّد شفافاً مع كل استطلاع متأخر."""
        async with self._lock:
            entry = await self._get(prefix)
            if entry.get("type") != "qr":
                return {"mode": "code"}  # عملية OTP عادية جارية — لا علاقة لـQR
            st = entry.get("status")
            if st == "authorized":
                result = dict(entry.get("result") or {})
                await self._drop_entry(entry)
                self._pending.pop(prefix, None)
                return {"authorized": True, "result": result}
            if st == "need_password":
                entry["ts"] = time.time()
                return {"need_password": True}
            if st == "expired":
                try:
                    await entry["qr"].recreate()
                except Exception as e:
                    await self._drop_entry(entry)
                    self._pending.pop(prefix, None)
                    raise HTTPException(
                        status_code=400,
                        detail=(f"انتهت جلسة QR نهائياً — ابدأ العملية من جديد "
                                f"({type(e).__name__})"))
                entry["status"] = "waiting"
                entry["ts"] = time.time()
                entry["waiter"] = asyncio.create_task(self._qr_waiter(entry))
                logger.info(f"qr-login [{prefix}]: token recreated after expiry")
                return {"waiting": True, "recreated": True,
                        "url": entry["qr"].url,
                        "expires_in": _qr_expires_in(entry["qr"]),
                        "svg": _qr_svg(entry["qr"].url)}
            entry["ts"] = time.time()
            return {"waiting": True, "url": entry["qr"].url,
                    "expires_in": _qr_expires_in(entry["qr"]),
                    "svg": _qr_svg(entry["qr"].url)}


login_manager = LoginManager()

RENDER_API_BASE = "https://api.render.com/v1"


async def render_upsert_env_many(pairs: List[Tuple[str, str]]) -> Dict[str, Any]:
    """v9.35 (حرج): رفع عدة متغيرات بيئة على Render **بدمج آمن**.

    السبب الجذري لاختفاء متغيرات البيئة في الإنتاج: PUT المجمّع على
    `/env-vars` **يستبدل كل متغيرات الخدمة** بما يُرسل له حرفياً — كان
    v9.33 يرسل مفاتيح الحساب الجديد فقط فكان يمحو TARGET_GROUP_ID و
    ADMIN_CHAT_ID وBOT_TOKEN وكل ما عداها في كل إضافة حساب/تسجيل دخول
    (لهذا دخلت الخدمة وضع «اللوحة فقط» منذ 09-24).

    الإصلاح: GET للقائمة الحالية الكاملة ← دمج الأزواج الجديدة فوقها ←
    PUT بالمجموعة الكاملة (استبدال بمجموعة شاملة = لا شيء يفقد). عند أي
    فشل يسقط إلى الرفع الفردي المجرّب عبر render_upsert_env لكل مفتاح.
    فشل-آمن: أي خلل يعيد قاموس سبب ولا يرفع استثناء أبداً."""
    api_key = (os.getenv("RENDER_API_KEY") or "").strip()
    service_id = (os.getenv("RENDER_SERVICE_ID") or "").strip()
    if not api_key or not service_id:
        return {"saved": False, "reason": "RENDER_API_KEY / RENDER_SERVICE_ID غير مضبوطة"}
    if not pairs:
        return {"saved": True}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            # 1) القائمة الحالية الكاملة — بدونها PUT يمحو كل شيء
            current: Dict[str, str] = {}
            url: Optional[str] = f"{RENDER_API_BASE}/services/{service_id}/env-vars?limit=100"
            while url:
                async with s.get(url) as r:
                    if r.status != 200:
                        logger.warning(f"render env GET failed: HTTP {r.status}")
                        break
                    body = await r.json()
                    if isinstance(body, dict):
                        items = body.get("env_vars") or body.get("envVars") or []
                    else:
                        items = body or []
                    for it in items:
                        ev = it.get("envVar", it) if isinstance(it, dict) else {}
                        k = ev.get("key")
                        if k:
                            current[k] = ev.get("value", "")
                    cursor = body.get("cursor") if isinstance(body, dict) else None
                    url = (f"{RENDER_API_BASE}/services/{service_id}/env-vars"
                           f"?limit=100&cursor={cursor}") if cursor else None
            # 2) دمج الجديد فوق القديم (الجديد يفوز)
            merged = dict(current)
            merged.update({k: v for k, v in pairs})
            # 3) استبدال بالمجموعة الكاملة المدموجة — لا خسارة
            async with s.put(
                f"{RENDER_API_BASE}/services/{service_id}/env-vars",
                json=[{"key": k, "value": v} for k, v in merged.items()],
            ) as r:
                if r.status in (200, 201):
                    return {"saved": True, "bulk": True, "merged": len(merged)}
                logger.warning(f"render env bulk merge PUT failed: HTTP {r.status}")
    except Exception as e:
        logger.warning(f"render env bulk merge failed, falling back: {type(e).__name__}: {e}")
    # سقوط رشيق إلى الرفع الفردي المجرّب (لاحقة المفتاح = upsert آمن)
    for k, v in pairs:
        one = await render_upsert_env(k, v)
        if not one.get("saved"):
            return {"saved": False, "reason": one.get("reason", ""), "failed_key": k}
    return {"saved": True, "bulk": False}


async def render_upsert_env(key: str, value: str) -> Dict[str, Any]:
    """Upsert a single, specific env var on the Render service via Render API
    (triggers redeploy). Scope is already minimal — only ever called to set
    one *_SESSION_STRING key, never broader account/service settings.

    v9.35 (حرج): أُزيل السقوط الخاطئ إلى PUT المجمّع بمفتاح واحد — نقطة
    النهاية `/env-vars` (بلا لاحقة مفتاح) **تستبدل كل متغيرات الخدمة**،
    فكان هذا المسار الاحتياطي يمحو كل البيئة عند أي فشل لاحقة المفتاح.
    الفشل الآن يُعاد صادقاً ولا يُمس أي متغير آخر."""
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
                body = (await r.text())[:200]
                return {"saved": False, "reason": f"Render API HTTP {r.status}: {body}"}
    except Exception as e:
        return {"saved": False, "reason": f"{type(e).__name__}: {e}"}


def _mask_phone(phone: str) -> str:
    if not phone:
        return ""
    return "*" * max(0, len(phone) - 4) + phone[-4:]


def _mask_session(s: Optional[str]) -> str:
    """v9.34: إخفاء Session String للعرض فقط (مثال: 1AbC•••••••XYZ) —
    القيمة الكاملة لا تخرج من الخادم أبداً (استمراراً لضمان H-4)."""
    if not s:
        return ""
    s = str(s)
    if len(s) <= 10:
        return "•" * len(s)
    return f"{s[:4]}{'•' * 8}{s[-3:]}"


async def _merged_accounts(request: Request) -> List[Dict[str, Any]]:
    """v9.33: المصدر الموحد للحسابات في اللوحة — ACCOUNTS (الإقلاع/البيئة،
    وهي التكوين النشط فعلياً) ∪ dashboard_accounts (حسابات مضافة من اللوحة
    بانتظار النشر). الاتحاد بمفتاح prefix، والحاضر في البيئة يفوز لمشاركة
    prefix (هو المستوى الحي)، وقاعدة البيانات تضيف الباقي.

    فشل-آمن: أي خلل في قاعدة البيانات يعيد ACCOUNTS وحدها — لا تنكسر
    صفحة الجلسات أبداً. الصفوف القادمة من قاعدة البيانات فقط تحمل
    pending_deploy=True (لم تصل إلى بيئة الإنتاج بعد)."""
    merged: Dict[str, Dict[str, Any]] = {
        a.get("prefix", ""): dict(a) for a in ACCOUNTS if a.get("prefix")
    }
    db = getattr(request.app.state, "db", None)
    if db is not None:
        try:
            rows = await db.list_dashboard_accounts()
            for r in rows:
                p = (r.get("prefix") or "").strip()
                if not p:
                    continue
                if p in merged:
                    # v9.34: تكوين الإقلاع يبقى مصدر بيانات الاتصال (api_id/
                    # api_hash/phone من البيئة — هي التي حُمّل بها البوت)،
                    # لكن حالة الإدارة (تفعيل/تتبع) تُتراكب من قاعدة البيانات
                    # مصدر الحقيقة للوحة.
                    base = merged[p]
                    base["enabled"] = bool(r.get("enabled", True))
                    base["status"] = r.get("status")
                    base["last_error_db"] = r.get("last_error")
                    base["last_connected_at"] = r.get("last_connected_at")
                    base["last_activity_at"] = r.get("last_activity_at")
                    base["db_id"] = r.get("id")
                    continue
                merged[p] = {
                    "id": 0,
                    "prefix": p,
                    "name": (r.get("name") or p.replace("_", " ").title()),
                    "api_id": r.get("api_id"),
                    "api_hash": r.get("api_hash"),
                    "phone": r.get("phone") or "",
                    "session": r.get("session_name") or p.lower(),
                    "session_string": r.get("session_string"),
                    "priority": r.get("priority", 10),
                    "is_main": bool(r.get("is_main")),
                    "enabled": bool(r.get("enabled", True)),
                    "status": r.get("status"),
                    "last_error_db": r.get("last_error"),
                    "last_connected_at": r.get("last_connected_at"),
                    "last_activity_at": r.get("last_activity_at"),
                    "db_id": r.get("id"),
                    "pending_deploy": True,
                    "retry_count": 0,
                    "last_error": None,
                }
        except Exception as e:
            logger.debug(f"_merged_accounts: db branch skipped: {e}")
    return list(merged.values())


async def _audit(request: Request, action: str, object_type: str = "",
                 object_id: str = "", old_value: str = "", new_value: str = "") -> None:
    """v9.18 P0: audit log write from BotPanel — fire-and-forget, fail-safe.

    The request path is never touched: any DB glitch is swallowed. The
    actor is the constant "botpanel-admin" (single-token auth) and the
    source is "botpanel". Callers are responsible for masking phones.
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        return
    try:
        _track_local_task(
            db.record_audit(
                actor="botpanel-admin", action=action, object_type=object_type,
                object_id=object_id, old_value=old_value, new_value=new_value,
                source="botpanel",
            ),
            "audit_write",
        )
    except Exception:
        pass


@app.get("/api/login/accounts", dependencies=[Depends(require_permission("accounts.read"))])  # v9.32
async def login_accounts(request: Request):
    """قائمة الحسابات المهيأة وحالة جلساتها.

    v9.33: المصدر الموحد (بيئة الإقلاع + dashboard_accounts) — الحساب
    المضاف من اللوحة يظهر في القائمة المنسدلة فوراً حتى قبل إعادة
    النشر (pending_deploy=True)، فلا تعود الصفحة فارغة أبداً.
    v9.34: + enabled و has_session مع بقاء الحقول القديمة كما هي."""
    bot = getattr(request.app.state, "bot_ref", None)
    monitors = {m.account.get("prefix"): m for m in bot.monitors} if bot else {}
    out = []
    for acc in await _merged_accounts(request):
        prefix = acc.get("prefix", "")
        mon = monitors.get(prefix)
        out.append({
            "prefix": prefix,
            "name": acc.get("name"),
            "phone_masked": _mask_phone(acc.get("phone", "")),
            "has_session_string": bool(acc.get("session_string")),
            "connected": bool(mon and mon.is_connected),
            "last_error": (mon._last_connect_error if mon else None),
            "pending_deploy": bool(acc.get("pending_deploy")),
            "enabled": bool(acc.get("enabled", True)),  # v9.34
        })
    return JSONResponse({"accounts": out, "monitoring_ready": _monitoring_ready()})


def _monitoring_ready() -> bool:
    """v9.35: هل بيئة التشغيل تكفي لبدء المراقبة والإرسال للقناة الهدف؟
    غياب TARGET_GROUP_ID/ADMIN_CHAT_ID يُدخل وضع «اللوحة فقط» — اللوحة
    تعرض لافتة تحذير واضحة بدل سؤال المستخدم «لماذا لا يراقب ولا يرسل؟»."""
    try:
        return bool(int(CFG.TARGET_GROUP_ID or 0)) and bool(int(CFG.ADMIN_CHAT_ID or 0))
    except Exception:
        return False


@app.post("/api/login/send-code", dependencies=[Depends(require_permission("accounts.write"))])  # v9.32
async def login_send_code(data: LoginSendCode, request: Request):
    """إرسال رمز التحقق OTP إلى هاتف الحساب.

    v9.33: البحث في المصدر الموحد — الحساب المضاف من اللوحة (لم يُنشر
    بعد) يستقبل الكود أيضاً لأن LoginManager لا يحتاج مراقباً حياً بل
    api_id/api_hash/phone فقط."""
    prefix = data.prefix.strip().upper()
    merged = await _merged_accounts(request)
    acc = next((a for a in merged if a.get("prefix") == prefix), None)
    if not acc:
        raise HTTPException(status_code=404, detail=f"الحساب {prefix} غير موجود في الإعدادات")
    try:
        result = await login_manager.start(prefix, acc["api_id"], acc["api_hash"], acc["phone"],
                                           force_sms=bool(getattr(data, "force_sms", False)))
        return JSONResponse({"success": True, "phone_masked": _mask_phone(acc["phone"]), **result})
    except ApiIdInvalidError:
        raise HTTPException(status_code=400, detail="API_ID / API_HASH غير صالحة")
    except PhoneNumberInvalidError:
        raise HTTPException(status_code=400, detail="رقم الهاتف غير صالح — تأكد من كتابته مع رمز الدولة (+966…)")
    except PhoneNumberUnoccupiedError:
        raise HTTPException(status_code=400, detail="هذا الرقم غير مسجل في تيليجرام — أنشئ حساباً به أولاً من التطبيق")
    except PhoneNumberBannedError:
        raise HTTPException(status_code=400, detail="هذا الرقم محظور من تيليجرام")
    except SendCodeUnavailableError:
        # v9.35: السبب الحقيقي لـ«الرمز لا يصل» — يُكتشف هنا مبكراً بدل 200 وهمي
        raise HTTPException(status_code=400, detail=OTP_UNAVAILABLE_AR)
    except FloodWaitError as e:
        raise HTTPException(status_code=429, detail=f"حظر مؤقت من تيليجرام - انتظر {e.seconds} ثانية")
    except Exception as e:
        logger.error(f"send-code error [{prefix}]: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)[:200]}")


@app.post("/api/login/verify-code", dependencies=[Depends(require_permission("accounts.write"))])  # v9.32
async def login_verify_code(data: LoginVerifyCode):
    """التحقق من رمز OTP."""
    prefix = data.prefix.strip().upper()
    try:
        result = await login_manager.verify_code(prefix, data.code)
    except PhoneCodeInvalidError:
        raise HTTPException(status_code=400, detail="رمز التحقق غير صحيح")
    except PhoneCodeExpiredError:
        raise HTTPException(status_code=400, detail="رمز التحقق منتهي - أرسل كوداً جديداً")
    except SendCodeUnavailableError:
        # v9.35: ظهر حرفياً في سجلات الإنتاج (22:37:59) — الرمز لم يُوصل
        # أصلاً (flash-call) فرفضه السيرفر عند sign_in. رسالة واضحة بدل
        # خطأ إنجليزي خام مرة 500.
        raise HTTPException(status_code=400, detail=OTP_UNAVAILABLE_AR)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"verify-code error [{prefix}]: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)[:200]}")

    if result.get("need_password"):
        return JSONResponse({"success": True, "need_password": True})
    return await _login_success_response(prefix, result)


@app.post("/api/login/verify-password", dependencies=[Depends(require_permission("accounts.write"))])  # v9.32
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


@app.post("/api/login/qr-start", dependencies=[Depends(require_permission("accounts.write"))])  # v9.37
async def login_qr_start(data: LoginSendCode, request: Request):
    """بدء دخول QR — الطريقة المباشرة بدون أي رمز OTP:
    يعيد رابط tg://login + صورة QR؛ المستخدم يفتح الرابط من تطبيق الحساب
    نفسه (أو يمسح QR من الإعدادات ← الأجهزة) فيُصرَّح للجلسة فوراً.
    الرمز قصير العمر — الواجهة تستطلم qr-wait ويُجدَّد تلقائياً."""
    prefix = data.prefix.strip().upper()
    merged = await _merged_accounts(request)
    acc = next((a for a in merged if a.get("prefix") == prefix), None)
    if not acc:
        raise HTTPException(status_code=404, detail=f"الحساب {prefix} غير موجود في الإعدادات")
    try:
        result = await login_manager.start_qr(prefix, acc["api_id"], acc["api_hash"])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"qr-start error [{prefix}]: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)[:200]}")
    return JSONResponse({"success": True, "phone_masked": _mask_phone(acc["phone"]),
                         "svg": _qr_svg(result.get("url", "")), **result})


@app.post("/api/login/qr-wait", dependencies=[Depends(require_permission("accounts.write"))])  # v9.37
async def login_qr_wait(data: LoginSendCode, request: Request):
    """استطلاع حالة عملية QR (تستدعيه الواجهة كل 4 ثوانٍ):
    - authorized → نفس مسار نجاح verify-code بالضبط (حفظ Render + DB + اتصال حي)
    - need_password → الواجهة تعرض حقل 2FA
    - waiting → الرابط/الصورة الحالية (مع تجديد تلقائي عند انتهاء الصلاحية)"""
    prefix = data.prefix.strip().upper()
    try:
        st = await login_manager.qr_status(prefix)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"qr-wait error [{prefix}]: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)[:200]}")
    if st.get("authorized"):
        result = dict(st.get("result") or {})
        return await _login_success_response(prefix, result)
    return JSONResponse({"success": True, **st})


async def _connect_after_login(prefix: str, session_string: str) -> None:
    """v9.34: مهمة خلفية بعد نجاح التسجيل — توصيل الحساب حياً فوراً
    (مراقب جديد بنفس مسار الإقلاع). مُعطّل في DB = احترام قرار المشرف
    (لا توصيل تلقائي). أي فشل يُسجل ولا يُرفع أبداً."""
    try:
        bot = getattr(app.state, "bot_ref", None)
        db = getattr(app.state, "db", None)
        if bot is None:
            return
        enabled = True
        if db is not None:
            try:
                row = await db.get_dashboard_account(prefix)
                if row is not None:
                    enabled = bool(row.get("enabled", True))
            except Exception:
                pass
        if not enabled:
            logger.info(f"post-login connect skipped [{prefix}]: account disabled in DB")
            return
        # v9.35: يُبنى الحساب من نسخة الإقلاع أولاً، وإن لم يوجد (حساب
        # مضاف من اللوحة لم يُنشر بعد) يُبنى من قاعدة البيانات — سابقاً كان
        # يُتخطى بصمت هنا فلا يتصل الحساب المضاف حياً أبداً بعد التسجيل
        # (السكوت الكامل الذي أبلّغ عنه المستخدم: «لا يُفعّل ولا يراقب»).
        acc = next((a for a in ACCOUNTS if (a.get("prefix") or "").upper() == prefix), None)
        if acc is None:
            db_row: Optional[Dict[str, Any]] = None
            if db is not None:
                try:
                    db_row = await db.get_dashboard_account(prefix)
                except Exception:
                    db_row = None
            if db_row is None:
                logger.warning(f"post-login connect skipped [{prefix}]: no source row found")
                return
            acc = {
                "id": db_row.get("id", 0),
                "prefix": prefix,
                "name": db_row.get("name") or prefix.replace("_", " ").title(),
                "api_id": db_row.get("api_id"),
                "api_hash": db_row.get("api_hash"),
                "phone": db_row.get("phone"),
                "session": db_row.get("session_name") or prefix.lower(),
                "session_string": session_string,
                "priority": db_row.get("priority", 10),
                "is_main": bool(db_row.get("is_main")),
                "enabled": True,
                "retry_count": 0,
                "last_error": None,
            }
        # الجلسة المُنتجة الآن هي الأحدث دائماً (قد تكون بيئة الإقلاع تحمل
        # قيمة قديمة/فارغة لأن التسجيل تم للتو).
        acc = dict(acc)
        acc["session_string"] = session_string
        result = await bot.add_runtime_monitor(acc)
        if result.get("ok"):
            logger.info(f"✅ Account {prefix} connected live right after login")
            if db is not None:
                try:
                    await db.mark_dashboard_account_connected(prefix)
                except Exception:
                    pass
        else:
            logger.warning(f"post-login connect failed [{prefix}]: {result.get('error')}")
            if db is not None:
                try:
                    await db.set_dashboard_account_status(prefix, "error", result.get("error"))
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"_connect_after_login failed [{prefix}]: {e}")


async def _login_success_response(prefix: str, result: Dict[str, Any]) -> JSONResponse:
    """حفظ الجلسة في متغيرات Render وإرجاع النتيجة.

    v3.1 (audit H-4): the Session String is a full account credential. It is
    no longer ever returned in the API response or displayed in the browser
    (previously it was leaked in the JSON body whenever the Render save
    failed). If the automatic save fails, the user re-runs the /login flow
    or enters the value manually from Render's own dashboard.
    """
    env_key = f"{prefix}_SESSION_STRING"
    save = await render_upsert_env(env_key, result["session_string"])
    logger.info(f"Login completed for {prefix} ({result.get('user')}): env {env_key} saved={save.get('saved')}")
    # v9.33: قاعدة البيانات مصدر حقيقة حالة الجلسة في اللوحة + تحديث نسخة
    # الإقلاع في الذاكرة — شارة الجلسة تتحول «موجودة» فور النجاح دون
    # انتظار إعادة النشر (فشل-آمن: أي خلل لا يمس رد النجاح).
    try:
        db = getattr(app.state, "db", None)
        if db is not None:
            await db.set_dashboard_account_session(prefix, result["session_string"])
    except Exception as e:
        logger.debug(f"login session db update skipped: {e}")
    try:
        for a in ACCOUNTS:
            if a.get("prefix") == prefix:
                a["session_string"] = result["session_string"]
    except Exception:
        pass
    # v9.34: الحساب قابل للاستخدام فوراً بعد نجاح التسجيل — إنشاء مراقب حي
    # في الخلفية (نفس مسار الإقلاع) دون انتظار إعادة نشر Render. الفشل
    # رشيف لا يمس رد النجاح (الجلسة محفوظة وسيتصل عند إعادة النشر على أي حال).
    try:
        bot = getattr(app.state, "bot_ref", None)
        if bot is not None:
            _track_local_task(
                _connect_after_login(prefix, result["session_string"]),
                f"login_connect_{prefix}",
            )
    except Exception as e:
        logger.debug(f"post-login connect spawn skipped [{prefix}]: {e}")
    return JSONResponse({
        "success": True,
        "done": True,
        "user": result.get("user"),
        "user_id": result.get("user_id"),
        "env_key": env_key,
        "saved_to_render": save.get("saved", False),
        "save_reason": save.get("reason", ""),
        "note": "تم حفظ الجلسة - ستعيد Render نشر الخدمة تلقائياً وسيتصل الحساب خلال دقائق" if save.get("saved")
                else "تعذر الحفظ التلقائي في Render - أعد المحاولة لاحقاً أو أدخل الجلسة يدوياً من لوحة Render (الجلسة لا تُعرض هنا لأسباب أمنية)",
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

<div id="monWarn" class="hidden" style="background:#422006;border:1px solid #f59e0b;color:#fcd34d;border-radius:12px;padding:14px 18px;margin-bottom:16px;font-size:13.5px;line-height:1.9">
<b>⚠️ البوت يعمل حالياً بوضع «اللوحة فقط» — لا مراقبة ولا إرسال للقناة الهدف.</b><br>
السبب: متغيرات <span class="mono">TARGET_GROUP_ID</span> و <span class="mono">ADMIN_CHAT_ID</span> غير مضبوطة في بيئة Render.
حتى لو اتصل الحساب بنجاح فلن يراقب المجموعات ولا يرسل التنبيهات.
أضف المتغيرين من Render ← Environment ثم أعد النشر.
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
<button id="btnSms" onclick="sendCode(true)" style="background:#0e7490;margin-top:6px">📱 إرسال الرمز عبر SMS (للشريحة مباشرة)</button>
<button id="btnQr" onclick="startQr()" style="background:#b45309;margin-top:6px">⚡ دخول فوري بدون أي رمز (QR / رابط)</button>

<div id="qrBox" class="hidden" style="margin-top:14px;padding:12px;border:1px solid #334155;border-radius:10px;background:#0f172a">
<h3 style="margin:0 0 8px;font-size:15px;color:#fbbf24">⚡ تأكيد الدخول بدون رمز</h3>
<p style="font-size:13px;color:#94a3b8;line-height:1.9;margin:0 0 10px">
افتح تطبيق تيليجرام <b>لهذا الحساب نفسه</b> واضغط الزر البرتقالي بالأسفل ليفتح تيليجرام ويطلب منك التأكيد — أو من التطبيق:<br>
<b>الإعدادات ← الأجهزة ← ربط جهاز سطح المكتب</b> ثم امسح صورة QR.
</p>
<div id="qrSvg" style="text-align:center;background:#fff;border-radius:8px;padding:8px;margin-bottom:10px"></div>
<a id="qrLink" href="#" style="display:block;text-align:center;padding:12px;background:#b45309;color:#fff;border-radius:8px;font-weight:700;text-decoration:none;font-size:15px">🔗 افتح الرابط في تيليجرام الآن</a>
<div id="qrStatus" style="text-align:center;font-size:13px;color:#94a3b8;margin-top:8px">⏳ بانتظار التأكيد من التطبيق… (يُجدَّد الرمز تلقائياً)</div>
</div>
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

<div class="card">
<h2>3️⃣ تسجيل بجلسة جاهزة (Session String) — البديل الموثوق</h2>
<p style="font-size:13px;color:#94a3b8;line-height:1.9;margin-bottom:8px">
إذا لم يصلك رمز التحقق (تيليجرام غالباً يمنع التوصيل من سيرفرات السحابة أو يستخدم مكالمة)،
ولّد الجلسة على جهازك الشخصي بنفس API_ID/API_HASH عبر:
<span class="mono" style="color:#38bdf8">python generate_session.py</span>
أو موقع Telethon-Thon، ثم الصق الناتج هنا — الحساب يتصل ويراقب فوراً.
</p>
<label>اختر الحساب</label>
<select id="sess-prefix"></select>
<label>Session String</label>
<input type="text" id="sess-string" class="mono" placeholder="1BQANOTEuMTA…" style="direction:ltr">
<button id="btnSess" onclick="pasteSession()">🔗 حفظ الجلسة وتوصيل الحساب</button>
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
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
$('token').value = localStorage.getItem('dashboard_token') || localStorage.getItem('dash_token') || '';
$('token').addEventListener('change', () => { localStorage.setItem('dashboard_token', token()); loadAccounts(); });

function show(type, text) { const m = $('msg'); m.className = 'msg ' + type; m.innerHTML = text; }
function setStep(n) {
  [1,2,3,4].forEach(i => { const el = $('st'+i); el.className = 'step' + (i < n ? ' done' : i === n ? ' active' : ''); });
  $('stepSend').classList.toggle('hidden', n !== 1);
  $('stepCode').classList.toggle('hidden', n !== 2);
  $('stepPass').classList.toggle('hidden', n !== 3);
}
async function api(path, body, method) {
  const r = await fetch(path, { method: method || (body ? 'POST' : 'GET'),
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
    const ssel = $('sess-prefix'); if (ssel) ssel.innerHTML = '';
    d.accounts.forEach(a => {
      const tr = document.createElement('tr');
      const sess = a.has_session_string ? '<span class="badge b-green">موجودة</span>' : '<span class="badge b-yellow">مطلوبة</span>';
      const conn = a.connected ? '<span class="badge b-green">متصل</span>' : '<span class="badge b-red">غير متصل</span>';
      tr.innerHTML = `<td>${esc(a.name)}</td><td class="mono">${esc(a.phone_masked)}</td><td>${sess}</td><td>${conn}</td>`;
      tb.appendChild(tr);
      const op = document.createElement('option');
      op.value = a.prefix; op.textContent = `${a.name} (${a.phone_masked})${a.connected ? ' ✅' : ''}`;
      sel.appendChild(op);
      if (ssel) { const o2 = op.cloneNode(true); ssel.appendChild(o2); }
    });
    const warn = $('monWarn');
    if (warn) warn.classList.toggle('hidden', d.monitoring_ready !== false);
  } catch (e) { show('err', 'تعذر تحميل الحسابات: ' + e.message); }
}
async function sendCode(forceSms) {
  currentPrefix = $('prefix').value;
  if (!currentPrefix) return;
  stopQrPoll();
  $('btnSend').disabled = true; $('btnSms').disabled = true;
  show('info', forceSms ? '⏳ جاري إرسال الرمز كرسالة SMS إلى الشريحة مباشرة...' : '⏳ جاري إرسال رمز التحقق عبر تيليجرام...');
  try {
    const d = await api('/api/login/send-code', { prefix: currentPrefix, force_sms: !!forceSms });
    let msg = `📨 تم إرسال الطلب إلى ${esc(d.phone_masked)}`;
    if (d.delivery_hint) msg += `<br>📬 طريقة التوصيل التي اختارها تيليجرام: <b>${esc(d.delivery_hint)}</b>`;
    if (forceSms) {
      msg += `<br>📱 افتح <b>رسائل الهاتف (Messages/SMS)</b> — الرمز سيصل كرسالة نصية من تيليجرام، ثم أدخله هنا.`;
    } else {
      msg += `<br>افتح تيليجرام وانسخ الرمز ثم أدخله هنا.`;
      msg += `<br>💡 لم يصلك رمز؟ استخدم <b>📱 SMS</b> (للشريحة) أو <b>⚡ دخول بدون رمز</b> بالأعلى.`;
    }
    if (d.delivery && d.delivery !== 'app' && d.delivery !== 'sms') {
      msg += `<br>⚠️ تيليجرام اختار مكالمة لتوصيل الرمز — غالباً لن تصلك رسالة في تيليجرام. استخدم <b>📱 SMS</b> أو <b>⚡ الدخول بدون رمز</b>.`;
    }
    show('ok', msg);
    setStep(2);
  } catch (e) { show('err', '❌ ' + e.message); }
  $('btnSend').disabled = false; $('btnSms').disabled = false;
}

let qrPollTimer = null;
function renderQr(d) {
  if (d.svg) $('qrSvg').innerHTML = d.svg;
  if (d.url) $('qrLink').href = d.url;
}
function stopQrPoll() {
  if (qrPollTimer) { clearInterval(qrPollTimer); qrPollTimer = null; }
  $('qrBox').classList.add('hidden');
}
async function startQr() {
  currentPrefix = $('prefix').value;
  if (!currentPrefix) return;
  const btn = $('btnQr'); btn.disabled = true;
  show('info', '⏳ جاري توليد رمز الدخول السريع...');
  try {
    const d = await api('/api/login/qr-start', { prefix: currentPrefix });
    $('qrBox').classList.remove('hidden');
    renderQr(d);
    show('ok', `⚡ جاهز! اضغط الزر البرتقالي من تطبيق تيليجرام <b>لهذا الحساب (${esc(d.phone_masked)})</b> ووافق على الدخول — سيتم التفعيل والمراقبة فوراً بدون أي رمز.`);
    stopQrPollTimerOnly();
    qrPollTimer = setInterval(pollQr, 4000);
  } catch (e) { show('err', '❌ ' + e.message); }
  btn.disabled = false;
}
function stopQrPollTimerOnly() { if (qrPollTimer) { clearInterval(qrPollTimer); qrPollTimer = null; } }
async function pollQr() {
  if (!currentPrefix) { stopQrPoll(); return; }
  try {
    const d = await api('/api/login/qr-wait', { prefix: currentPrefix });
    if (d.done) { stopQrPoll(); finishLogin(d); return; }
    if (d.need_password) { stopQrPoll(); show('info', '🔐 هذا الحساب يستخدم التحقق بخطوتين - أدخل كلمة المرور السحابية.'); setStep(3); return; }
    if (d.mode === 'code') { stopQrPoll(); return; }
    if (d.waiting) {
      renderQr(d);
      if (d.recreated) $('qrStatus').textContent = '🔄 تم تجديد الرمز — افتح الرابط / امسح الصورة الجديدة.';
    }
  } catch (e) { stopQrPoll(); show('err', '❌ انتهت جلسة الدخول السريع: ' + e.message); }
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
  let html = `✅ تم تسجيل الدخول بنجاح: <b>${esc(d.user)}</b><br>`;
  if (d.saved_to_render) {
    html += `💾 حُفظت الجلسة في <span class="mono">${esc(d.env_key)}</span><br>🔄 ستعيد Render النشر تلقائياً وسيتصل الحساب خلال دقائق — ويبدأ الاتصال الحي الآن مباشرة أيضاً.`;
  } else {
    // SECURITY (audit H-4): the Session String is never shown in the browser.
    html += `⚠️ ${esc(d.note || '')}<br>السبب: ${esc(d.save_reason || '')}`;
  }
  show(d.saved_to_render ? 'ok' : 'err', html);
  setTimeout(loadAccounts, 2000);
}
async function pasteSession() {
  const pfx = $('sess-prefix').value;
  const s = $('sess-string').value.trim();
  if (!pfx) { show('err', '❌ اختر الحساب أولاً'); return; }
  if (!s || s.length < 30) { show('err', '❌ الصق Session String صحيحاً (يبدأ بـ 1Ab… أو 1BQ…)'); return; }
  const btn = $('btnSess'); btn.disabled = true;
  show('info', '⏳ جاري حفظ الجلسة وتوصيل الحساب...');
  try {
    const d = await api(`/api/accounts/${encodeURIComponent(pfx)}`, { session_string: s }, 'PUT');
    show('ok', `✅ ${esc(d.message || 'تم حفظ الجلسة')}<br>🔄 إن لم يتصل فوراً فسيتصل تلقائياً بعد إعادة نشر Render (${d.saved_to_render ? 'حُفظت في Render' : 'لم تُزامن مع Render'}).`);
    $('sess-string').value = '';
    setTimeout(loadAccounts, 2000);
  } catch (e) { show('err', '❌ ' + e.message); }
  btn.disabled = false;
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

def _verify_ws_token(websocket: WebSocket) -> bool:
    """
    v3.1 (audit H-2): /ws used to accept ANY unauthenticated connection and
    stream live stats that include FULL phone numbers and account details.
    Browsers cannot set an Authorization header on a WebSocket handshake, so
    the dashboard client passes the same DASHBOARD_AUTH_TOKEN as a
    `?token=` query parameter; comparison is timing-safe.
    """
    supplied = (websocket.query_params.get("token") or "").strip()
    if not supplied:
        return False
    return hmac.compare_digest(supplied, CFG.DASHBOARD_AUTH_TOKEN)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    ws_ip = _auth_guard.client_ip(websocket)
    if not _verify_ws_token(websocket):
        # v9.28-2: التوكن الخاطئ عبر /ws يُحتسب في نفس عدّاد الحارس
        # (لا باب جانبي لتخمين التوكن خارج مسار API).
        lock = _auth_guard.record_failure(ws_ip)
        # Accept-then-close is the only cross-browser way to deny a WS
        # handshake with a meaningful code (1008 = Policy Violation).
        await websocket.accept()
        await websocket.close(code=1008)
        logger.warning(
            "Rejected unauthenticated WebSocket handshake "
            f"from {websocket.client.host if websocket.client else 'unknown'}"
            + (f" — IP locked for {lock}s" if lock > 0 else "")
        )
        return
    _auth_guard.record_success(ws_ip)
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
# v10.0: معمل الفلترة — فحص حي + سجل القرارات + تغذية راجعة تُغذّي الأوزان
# =============================================================================
@app.get("/api/filter/recent", dependencies=[Depends(require_permission("keywords.read"))])  # v10.0
async def filter_recent(request: Request, limit: int = 60, decision: Optional[str] = None):
    """أحدث قرارات الفلترة (رسائل وصلت لمرحلة الكلمات المفتاحية) مع تصنيفها."""
    db = request.app.state.db
    try:
        rows = await db.get_filter_decisions(limit=limit, decision=decision)
        return JSONResponse({"success": True, "decisions": rows})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)[:200]}")


@app.get("/api/filter/stats", dependencies=[Depends(require_permission("keywords.read"))])  # v10.0
async def filter_stats(request: Request, hours: int = 24):
    """إحصاءات القرارات: الأعداد، متوسط الثقة، المواد، أعلى الكلمات."""
    db = request.app.state.db
    try:
        hours = max(1, min(int(hours), 24 * 30))
        st = await db.filter_decision_stats(hours=hours)
        return JSONResponse({"success": True, "hours": hours, **st})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)[:200]}")


@app.post("/api/filter/feedback", dependencies=[Depends(require_permission("keywords.write"))])  # v10.0
async def filter_feedback(request: Request, data: FilterFeedback):
    """تغذية راجعة بشرية على قرار: ✅ صحيح / ❌ خطأ.

    تحدّث الصف ثم تُغذّي الأوزان التكيفية (EMA) لمفردات القرار
    (فعل النية + المفعول الأكاديمي) — فتتعلم الفلترة من مراجعتك.
    """
    db = request.app.state.db
    row = await db.set_filter_decision_feedback(data.id, data.correct, data.note)
    if row is None:
        raise HTTPException(status_code=404, detail="القرار غير موجود")
    learned: List[str] = []
    bot = getattr(request.app.state, "bot_ref", None)
    flt = getattr(bot, "filter", None) if bot else None
    if flt is not None and hasattr(flt, "record_feedback"):
        try:
            if row.get("intent_verb"):
                await flt.record_feedback(row["intent_verb"], "intent", data.correct)
                learned.append(f"intent:{row['intent_verb']}")
            if row.get("academic_object"):
                await flt.record_feedback(row["academic_object"], "academic", data.correct)
                learned.append(f"academic:{row['academic_object']}")
        except Exception as e:
            logger.debug(f"filter feedback adaptive update skipped: {e}")
    try:
        await _audit(request, "filter.feedback",
                     object_type="decision", object_id=str(data.id),
                     new_value="correct" if data.correct else "incorrect")
    except Exception:
        pass
    return JSONResponse({"success": True, "decision": row, "learned": learned})


@app.post("/api/filter/test", dependencies=[Depends(require_permission("keywords.read"))])  # v10.0
async def filter_test(request: Request, data: FilterTestRequest):
    """فحص نص حي عبر محرك الفلترة: القرار + درجات كل إشارة + التصنيف."""
    text = (data.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="أدخل نصاً للفحص")
    bot = getattr(request.app.state, "bot_ref", None)
    flt = getattr(bot, "filter", None) if bot else None
    if flt is None or not hasattr(flt, "analyze"):
        raise HTTPException(status_code=503, detail="محرك الفلترة غير متاح — البوت غير موصول")
    try:
        analysis = await flt.analyze(text)
        from classifier import classify_text
        cls = classify_text(text)
        analysis.pop("original_text", None)
        return JSONResponse({
            "success": True,
            "analysis": analysis,
            "classification": cls,
            "thresholds": {
                "accept": float(getattr(CFG, "CONFIDENCE_ACCEPT_THRESHOLD", 0.65)),
                "review": float(getattr(CFG, "CONFIDENCE_REVIEW_THRESHOLD", 0.40)),
            },
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)[:200]}")


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