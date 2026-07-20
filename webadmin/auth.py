"""
webadmin/auth.py – Production-grade session authentication for the admin SPA.

- Username/password login backed by environment variables
  (DASHBOARD_USERNAME / DASHBOARD_PASSWORD / DASHBOARD_SECRET_KEY).
- Stateless signed session cookies (itsdangerous) - no server-side storage.
- CSRF protection via per-session token + custom header (double-submit).
- Secure cookies: HttpOnly, SameSite=Lax, Secure (behind TLS proxy).
- In-memory brute-force protection: 5 failed attempts -> 10 min lockout.
"""

from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from loguru import logger

COOKIE_NAME = "wa_session"
SESSION_MAX_AGE = 12 * 3600           # 12 hours
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 600           # 10 minutes
CSRF_HEADER = "x-csrf-token"

# ---------------------------------------------------------------------------
# Secret / serializer
# ---------------------------------------------------------------------------
_serializer: Optional[URLSafeTimedSerializer] = None


def _get_serializer() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        secret = (
            (os.getenv("DASHBOARD_SECRET_KEY") or "").strip()
            or (os.getenv("SECRET_KEY_OVERRIDE") or "").strip()
        )
        if not secret:
            secret = secrets.token_urlsafe(32)
            logger.warning(
                "DASHBOARD_SECRET_KEY not set - generated an ephemeral key. "
                "Sessions will be invalidated on every restart."
            )
        _serializer = URLSafeTimedSerializer(secret, salt="webadmin-session-v1")
    return _serializer


def _credentials() -> Tuple[Optional[str], Optional[str]]:
    user = (os.getenv("DASHBOARD_USERNAME") or "").strip() or None
    password = (os.getenv("DASHBOARD_PASSWORD") or "").strip() or None
    return user, password


def auth_is_configured() -> bool:
    user, password = _credentials()
    return bool(user and password)


# ---------------------------------------------------------------------------
# Brute-force protection (thread-safe, in-memory)
# ---------------------------------------------------------------------------
class _LoginGuard:
    def __init__(self) -> None:
        self._fails: Dict[str, Tuple[int, float]] = {}
        self._lock = threading.Lock()

    def _key(self, request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for", "")
        ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")
        return ip

    def check_allowed(self, request: Request) -> None:
        key = self._key(request)
        with self._lock:
            fails, lockout = self._fails.get(key, (0, 0.0))
            if lockout > time.time():
                retry = int(lockout - time.time())
                raise HTTPException(
                    status_code=429,
                    detail=f"محاولات كثيرة - حاول بعد {retry} ثانية",
                    headers={"Retry-After": str(retry)},
                )

    def record_failure(self, request: Request) -> None:
        key = self._key(request)
        with self._lock:
            fails, _ = self._fails.get(key, (0, 0.0))
            fails += 1
            lockout = time.time() + LOGIN_LOCKOUT_SECONDS if fails >= LOGIN_MAX_ATTEMPTS else 0.0
            self._fails[key] = (fails, lockout)
            if lockout:
                logger.warning(f"Login lockout for {key} after {fails} failed attempts")

    def record_success(self, request: Request) -> None:
        key = self._key(request)
        with self._lock:
            self._fails.pop(key, None)


_login_guard = _LoginGuard()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
def create_session(request: Request, response: Response, username: str) -> str:
    """Issue the signed session cookie; returns the session CSRF token."""
    csrf = secrets.token_urlsafe(32)
    payload = {"u": username, "csrf": csrf, "iat": int(time.time())}
    token = _get_serializer().dumps(payload)
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    return csrf


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def read_session(request: Request) -> Optional[Dict[str, Any]]:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        data = _get_serializer().loads(raw, max_age=SESSION_MAX_AGE)
        if isinstance(data, dict) and data.get("u"):
            return data
    except (BadSignature, SignatureExpired):
        pass
    return None


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------
async def login_required(request: Request) -> Dict[str, Any]:
    """Dependency: require a valid admin session (like Flask's @login_required)."""
    session = read_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="الجلسة منتهية - سجّل الدخول من جديد")
    return session


async def csrf_protect(request: Request) -> None:
    """Dependency: mutating requests must carry the session CSRF token."""
    session = read_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="الجلسة منتهية - سجّل الدخول من جديد")
    sent = request.headers.get(CSRF_HEADER, "")
    if not sent or not hmac.compare_digest(sent, str(session.get("csrf", ""))):
        raise HTTPException(status_code=403, detail="رمز CSRF غير صالح")


def verify_credentials(username: str, password: str) -> bool:
    expected_user, expected_pass = _credentials()
    if not expected_user or not expected_pass:
        return False
    return hmac.compare_digest(username, expected_user) and hmac.compare_digest(
        password, expected_pass
    )
