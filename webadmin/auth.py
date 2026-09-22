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
import ipaddress
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

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


# v9.12 (audit M-12): trusted-proxy parsing for the login-guard ban key.
# TRUSTED_PROXIES is a comma-separated list of CIDRs (e.g.
# "10.0.0.0/8,173.245.48.0/20"). When set, X-Forwarded-For entries
# inside these CIDRs are treated as trusted hops; the rightmost
# non-trusted entry is the real client IP used for ban-keying. When
# unset, the guard uses only the direct connection IP (safest).
_TRUSTED_PROXIES_CACHE: Tuple[float, List[ipaddress._BaseNetwork]] = (0.0, [])
_TRUSTED_PROXIES_TTL = 60.0  # seconds


def _trusted_proxies() -> List[ipaddress._BaseNetwork]:
    """Return the parsed list of trusted-proxy CIDRs (cached for 60s)."""
    global _TRUSTED_PROXIES_CACHE
    now = time.time()
    cached_ts, cached = _TRUSTED_PROXIES_CACHE
    if (now - cached_ts) < _TRUSTED_PROXIES_TTL and cached_ts > 0:
        return cached
    raw = os.getenv("TRUSTED_PROXIES", "").strip()
    nets: List[ipaddress._BaseNetwork] = []
    if raw:
        for piece in raw.split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                nets.append(ipaddress.ip_network(piece, strict=False))
            except ValueError:
                logger.warning(f"TRUSTED_PROXIES: invalid CIDR skipped: {piece!r}")
    _TRUSTED_PROXIES_CACHE = (now, nets)
    return nets


def _ip_in_any_cidr(ip: str, cidrs: List[ipaddress._BaseNetwork]) -> bool:
    """True if `ip` is contained in any of the provided CIDRs."""
    if not ip or not cidrs:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in cidrs)


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
        # v9.12 (audit M-12): the ban key no longer trusts the FIRST
        # X-Forwarded-For entry, which is attacker-controlled when the
        # service sits behind a proxy that doesn't overwrite the header.
        # An attacker could rotate the first XFF value on every request
        # and never hit the lockout threshold. We now prefer a list of
        # trusted-proxy-resolved IPs configured via TRUSTED_PROXIES env
        # (comma-separated CIDRs); if the request's direct client IP is
        # inside a trusted CIDR, we walk the XFF chain from RIGHT to LEFT
        # and pick the first IP that is NOT in a trusted CIDR (the
        # "real" client). If no trusted proxies are configured, we fall
        # back to the direct client IP (request.client.host) which is
        # always set by the actual TCP connection and not forgeable.
        direct = request.client.host if request.client else "?"
        fwd = request.headers.get("x-forwarded-for", "")
        trusted_proxies = _trusted_proxies()
        if not trusted_proxies:
            # No trusted proxies configured — use the direct connection
            # IP only (safest: cannot be forged by the client).
            return direct
        # Walk the XFF chain right-to-left, skipping trusted-proxy IPs.
        if fwd:
            parts = [p.strip() for p in fwd.split(",") if p.strip()]
            for ip in reversed(parts):
                if not _ip_in_any_cidr(ip, trusted_proxies):
                    return ip
        # All XFF entries were trusted proxies (or XFF empty) — fall back
        # to the direct connection IP.
        return direct

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
