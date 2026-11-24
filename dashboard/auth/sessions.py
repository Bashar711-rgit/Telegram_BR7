# -*- coding: utf-8 -*-
"""dashboard.auth.sessions — جلسات اللوحة: ذاكرة حية + مثابرة DB (المرحلة 1).

التصميم:
  * التحقق في كل طلب من الذاكرة حصراً — صفر جسر/DB على المسار الساخن (§17).
  * الإنشاء/الإبطال يُثبت في panel_sessions عبر الجسر (الوحدة users.py).
  * عند الإقلاع تُحمَّل الجلسات النشطة من DB (hydrate) — لا خروج جماعي
    بعد كل نشر (كانت الجلسات تفنى مع الذاكرة في v15.1).
  * توكن الجلسة وتوكن CSRF مستقلان (§6.3) — كلاهما sha256 في الصفوف،
    والقيمة الخام لا تصل قاعدة البيانات إطلاقاً.
  * TTL انزلاقي + سقف جلسات لكل مستخدم (الأقدم يُطوى) + إبطال عند تغيير
    كلمة المرور أو تعطيل المستخدم أو تغيير الدور.

هذه الوحدة stdlib حصراً (بلا aiohttp/telethon/DB) — تُستورد من dashboard.state
الذي يعيش على المسار الحرج للبوت.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_SESSION_TTL_SECONDS = 8 * 3600
DEFAULT_MAX_SESSIONS_PER_USER = 10


def new_token() -> str:
    """توكن عشوائي 256-bit (urlsafe)."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """sha256 hex للتوكن — هو ما يُخزن في الذاكرة/DB، لا القيمة الخام."""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _const_eq(a: str, b: str) -> bool:
    try:
        return bool(hmac.compare_digest(str(a or ""), str(b or "")))
    except Exception:
        return False


class AuthSessionStore:
    """خريطة token_hash → meta في الذاكرة + سقف لكل مستخدم + TTL انزلاقي.

    القيم المخزنة: هاش التوكن، هاش CSRF، هوية المستخدم ودوره، IP/UA،
    طوابع زمنية (epoch حقيقية — متوافقة مع أعمدة panel_sessions REAL)،
    ولقطة must_change عند الإنشاء.
    """

    __slots__ = ("_by_token", "_by_user", "_lock", "_ttl", "_max")

    def __init__(self, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
                 max_sessions: int = DEFAULT_MAX_SESSIONS_PER_USER) -> None:
        self._ttl = max(300, int(ttl_seconds))
        self._max = max(1, int(max_sessions))
        self._by_token: Dict[str, Dict[str, Any]] = {}
        self._by_user: Dict[int, set] = {}
        self._lock = threading.Lock()

    # ── الضبط الحي من إعدادات CFG عند التركيب ────────────────────────────
    def configure(self, ttl_seconds: int, max_sessions: int) -> None:
        with self._lock:
            self._ttl = max(300, int(ttl_seconds))
            self._max = max(1, int(max_sessions))

    # ── الإنشاء ──────────────────────────────────────────────────────────
    def create(self, token: str, csrf: str, user_id: int, username: str,
               role: str, ip: str = "", ua: str = "",
               must_change: bool = False) -> Dict[str, Any]:
        th = token_hash(token)
        now = time.time()
        meta: Dict[str, Any] = {
            "token_hash": th,
            "csrf_hash": token_hash(csrf),
            "user_id": int(user_id),
            "username": str(username),
            "role": str(role or "viewer"),
            "created": now,
            "expires": now + self._ttl,
            "ip": str(ip or "")[:64],
            "ua": str(ua or "")[:200],
            "must_change": bool(must_change),
        }
        with self._lock:
            self._evict_overflow_locked(int(user_id), now)
            self._by_token[th] = meta
            self._by_user.setdefault(int(user_id), set()).add(th)
        return dict(meta)

    def _evict_overflow_locked(self, user_id: int, now: float) -> None:
        ths = self._by_user.get(user_id)
        if not ths:
            return
        live = [t for t in ths if t in self._by_token and self._by_token[t]["expires"] > now]
        while len(live) >= self._max:
            oldest = min(live, key=lambda t: self._by_token[t]["expires"])
            self._drop_locked(oldest)
            live.remove(oldest)

    def _drop_locked(self, token_hash_value: str) -> Optional[Dict[str, Any]]:
        meta = self._by_token.pop(token_hash_value, None)
        if meta:
            bucket = self._by_user.get(int(meta.get("user_id") or 0))
            if bucket is not None:
                bucket.discard(token_hash_value)
                if not bucket:
                    self._by_user.pop(int(meta.get("user_id") or 0), None)
        return meta

    # ── التحقق (المسار الساخن) ───────────────────────────────────────────
    def validate(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        """يعيد نسخة من meta إن كانت الجلسة صالحة، أو None — مع تجديد انزلاقي."""
        if not token:
            return None
        th = token_hash(token)
        now = time.time()
        with self._lock:
            meta = self._by_token.get(th)
            if not meta:
                return None
            if now >= float(meta.get("expires") or 0.0):
                self._drop_locked(th)
                return None
            meta["expires"] = now + self._ttl
            return dict(meta)

    def csrf_matches(self, token: Optional[str], supplied: str) -> bool:
        """مطابقة توكن CSRF المرسل ضد هاش الجلسة المخزّن (زمن-ثابت)."""
        if not token or not supplied:
            return False
        th = token_hash(token)
        with self._lock:
            meta = self._by_token.get(th)
            if not meta:
                return False
            return _const_eq(token_hash(str(supplied)), str(meta.get("csrf_hash") or ""))

    # ── الإبطال ──────────────────────────────────────────────────────────
    def drop(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with self._lock:
            return self._drop_locked(token_hash(token)) is not None

    def drop_by_hash(self, token_hash_value: str) -> bool:
        with self._lock:
            return self._drop_locked(str(token_hash_value or "")) is not None

    def revoke_user(self, user_id: int, except_hash: str = "") -> List[str]:
        """إبطال كل جلسات المستخدم (ذاكرة) — يعيد الهاشات لإبطالها في DB."""
        out: List[str] = []
        with self._lock:
            for th in list(self._by_user.get(int(user_id)) or ()):
                if except_hash and th == str(except_hash):
                    continue
                if self._drop_locked(th) is not None:
                    out.append(th)
        return out

    # ── المثابرة ─────────────────────────────────────────────────────────
    def hydrate(self, rows: List[Dict[str, Any]]) -> int:
        """تحميل الجلسات النشطة من panel_sessions عند الإقلاع."""
        now = time.time()
        loaded = 0
        with self._lock:
            for r in rows or []:
                try:
                    th = str(r.get("token_hash") or "")
                    if not th or th in self._by_token:
                        continue
                    exp = float(r.get("expires_at") or 0.0)
                    if exp <= now:
                        continue
                    meta: Dict[str, Any] = {
                        "token_hash": th,
                        "csrf_hash": str(r.get("csrf_hash") or ""),
                        "user_id": int(r.get("user_id") or 0),
                        "username": str(r.get("username") or ""),
                        "role": str(r.get("role") or "viewer"),
                        "created": float(r.get("created_at") or now),
                        "expires": exp,
                        "ip": str(r.get("ip") or "")[:64],
                        "ua": str(r.get("ua") or "")[:200],
                        "must_change": bool(int(r.get("must_change") or 0)),
                    }
                    self._by_token[th] = meta
                    self._by_user.setdefault(meta["user_id"], set()).add(th)
                    loaded += 1
                except Exception:
                    continue
        return loaded

    def touch_snapshot(self) -> Tuple[List[Tuple[float, float, str]], List[str]]:
        """(صفوف للتحديث في DB، الهاشات الميتة محلياً) — للمسح الدوري."""
        now = time.time()
        rows: List[Tuple[float, float, str]] = []
        dead: List[str] = []
        with self._lock:
            for th, m in self._by_token.items():
                if now >= float(m.get("expires") or 0.0):
                    dead.append(th)
                    continue
                rows.append((float(m["expires"]), now, th))
            for th in dead:
                self._drop_locked(th)
        return rows, dead

    def current_hashes(self, user_id: Optional[int] = None) -> List[str]:
        with self._lock:
            if user_id is None:
                return list(self._by_token.keys())
            return list(self._by_user.get(int(user_id)) or ())

    def purge_expired(self) -> int:
        _, dead = self.touch_snapshot()
        return len(dead)

    def count(self) -> int:
        with self._lock:
            return len(self._by_token)
