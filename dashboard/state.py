# -*- coding: utf-8 -*-
"""════════════════════════════════════════════════════════════════════════
dashboard.state — الحالة المشتركة للوحة (v15.1.0)
════════════════════════════════════════════════════════════════════════

توتر-سيف خالص (threading.Lock فقط — صفر asyncio) حتى تُستخدم بأمان من:
  * خيط خادم الصحة (حلقة aiohttp المستقلة) — معالجات اللوحة.
  * الحلقة الرئيسية للبوت — مسار استقبال الرسائل (فحص مفاتيح الإيقاف).

هذا الملف عمداً لا يستورد aiohttp ولا telethon ولا قاعدة البيانات —
استيراده من monitors.py (المسار الحرج) يجب أن يظل صفر تكلفة.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

# ─────────────────────────────────────────────────────────────────────────
# ثوابت آمنة (لا تُقرأ من CFG هنا — server.py يمرّر قيم CFG عند التركيب
# حتى لا يفرض هذا الملف تحميل config على أي مستخدم منه)
# ─────────────────────────────────────────────────────────────────────────
DEFAULT_SESSION_TTL_SECONDS = 8 * 3600
DEFAULT_MAX_SESSIONS = 10
DEFAULT_API_RATE_PER_MIN = 120
DEFAULT_LOGIN_RATE_PER_MIN = 5
DEFAULT_LOGIN_LOCKOUT_SECONDS = 900
DEFAULT_OVERVIEW_TTL_SECONDS = 15
_IP_SLOTS_MAX = 2000          # سقف ذاكرة خريطة rate-limit (منع تسرب IPs)
_FAILED_LOGIN_MAX_ENTRIES = 500
_TOKEN_BYTES = 32

_SECRETS_NEVER = "••••••"


class DashboardOps:
    """مفاتيح التشغيل الجارية — فحص ذاكرة صفرية التكلفة في المسار الحرج.

    تُقرأ من monitors.py لكل رسالة (خطين فقط بلا أي قفل — bool atomics
    تحت GIL كافية هنا)، وتُحفظ في app_settings (dashboard:ops) عند التبديل
    من اللوحة لتبقى بعد إعادة التشغيل.
    """

    __slots__ = ("filtering_paused", "alerts_paused", "counters", "_lock")

    def __init__(self) -> None:
        self.filtering_paused: bool = False
        self.alerts_paused: bool = False
        # عدّادات المسار الحرج — تُحدَّث بدون قفل (خيط الأحداث الواحد)
        self.counters: Dict[str, int] = {
            "paused_filter_msgs": 0,
            "paused_alert_msgs": 0,
        }
        self._lock = threading.Lock()

    def set_filtering(self, enabled_paused: bool) -> None:
        self.filtering_paused = bool(enabled_paused)

    def set_alerts(self, enabled_paused: bool) -> None:
        self.alerts_paused = bool(enabled_paused)

    def bump(self, counter: str) -> None:
        try:
            self.counters[counter] = self.counters.get(counter, 0) + 1
        except Exception:
            pass

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "filtering_paused": self.filtering_paused,
                "alerts_paused": self.alerts_paused,
                "counters": dict(self.counters),
            }


class RateLimiter:
    """نافذة منزلقة لكل IP — ذاكرة محدودة (سقف _IP_SLOTS_MAX)."""

    __slots__ = ("_windows", "_lock", "_per_minute")

    def __init__(self, per_minute: int) -> None:
        self._per_minute = max(1, int(per_minute))
        self._windows: Dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        ts = time.monotonic() if now is None else now
        with self._lock:
            dq = self._windows.get(key)
            if dq is None:
                if len(self._windows) >= _IP_SLOTS_MAX:
                    # إخلاء أقدم عنصر (خريطة مرتبة بالإدراج — كافٍ هنا)
                    try:
                        oldest = next(iter(self._windows))
                        self._windows.pop(oldest, None)
                    except StopIteration:
                        pass
                dq = deque(maxlen=self._per_minute * 2)
                self._windows[key] = dq
            while dq and (ts - dq[0]) > 60.0:
                dq.popleft()
            if len(dq) >= self._per_minute:
                return False
            dq.append(ts)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._windows.pop(key, None)


class LoginGuard:
    """قفل تسجيل الدخول: N محاولة فاشلة → قفل مؤقت لهذا الـIP."""

    __slots__ = ("_entries", "_lock", "_max_attempts", "_lockout_s")

    def __init__(self, max_attempts: int = 5, lockout_seconds: int = 900) -> None:
        self._max_attempts = max(1, int(max_attempts))
        self._lockout_s = max(1, int(lockout_seconds))
        self._entries: Dict[str, Dict[str, float]] = {}
        self._lock = threading.Lock()

    def is_locked(self, ip: str, now: Optional[float] = None) -> bool:
        ts = time.monotonic() if now is None else now
        with self._lock:
            e = self._entries.get(ip)
            if not e:
                return False
            if ts >= e.get("locked_until", 0.0):
                if e.get("locked_until"):
                    self._entries.pop(ip, None)
                return False
            return True

    def record_failure(self, ip: str, now: Optional[float] = None) -> None:
        ts = time.monotonic() if now is None else now
        with self._lock:
            if len(self._entries) >= _FAILED_LOGIN_MAX_ENTRIES:
                try:
                    oldest = next(iter(self._entries))
                    self._entries.pop(oldest, None)
                except StopIteration:
                    pass
            e = self._entries.setdefault(ip, {"fails": 0.0, "locked_until": 0.0})
            e["fails"] = float(e.get("fails", 0)) + 1
            if e["fails"] >= self._max_attempts:
                e["locked_until"] = ts + self._lockout_s

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._entries.pop(ip, None)

    def remaining_lock(self, ip: str, now: Optional[float] = None) -> int:
        ts = time.monotonic() if now is None else now
        with self._lock:
            e = self._entries.get(ip)
            if not e:
                return 0
            rem = e.get("locked_until", 0.0) - ts
            return int(rem) if rem > 0 else 0


class SessionStore:
    """جلسات في الذاكرة حصراً — بلا كتابة DB لكل قراءة من اللوحة.

    * التوكن عشوائي 256-bit (secrets).
    * TTL انزلاقي، سقف جلسات (أقدمها يُطوى)، طيّ كسول عند الوصول.
    * التوكن نفسه هو CSRF token (double-submit عبر X-CSRF-Token header) —
      كوكي HttpOnly لا يستطيع المهاجم قراءته، والموقع الخارجي لا يستطيع
      وضع هيدر مخصص → هجوم CSRF محجوب بطبقتين (SameSite=Strict + header).
    """

    __slots__ = ("_sessions", "_lock", "_ttl", "_max")

    def __init__(self, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
                 max_sessions: int = DEFAULT_MAX_SESSIONS) -> None:
        self._ttl = max(300, int(ttl_seconds))
        self._max = max(1, int(max_sessions))
        self._sessions: Dict[str, Dict[str, float]] = {}
        self._lock = threading.Lock()

    def create(self, token: str, ip: str) -> Dict[str, float]:
        now = time.monotonic()
        with self._lock:
            if len(self._sessions) >= self._max:
                # طيّ الأقدم انتهاءً (أو الأقدم إدراجاً إن لم ينتهِ شيء)
                oldest_tok = None
                oldest_exp = None
                for tok, meta in self._sessions.items():
                    exp = meta.get("expires", 0.0)
                    if oldest_exp is None or exp < oldest_exp:
                        oldest_exp = exp
                        oldest_tok = tok
                if oldest_tok is not None:
                    self._sessions.pop(oldest_tok, None)
            meta = {"created": now, "expires": now + self._ttl, "ip": str(ip)}
            self._sessions[token] = meta
            return dict(meta)

    def validate(self, token: Optional[str]) -> bool:
        """تحقق + تجديد انزلاقي + طيّ المنتهية."""
        if not token:
            return False
        now = time.monotonic()
        with self._lock:
            meta = self._sessions.get(token)
            if not meta:
                return False
            if now >= meta.get("expires", 0.0):
                self._sessions.pop(token, None)
                return False
            meta["expires"] = now + self._ttl
            return True

    def drop(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with self._lock:
            return self._sessions.pop(token, None) is not None

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def purge_expired(self) -> int:
        now = time.monotonic()
        with self._lock:
            dead = [t for t, m in self._sessions.items() if now >= m.get("expires", 0.0)]
            for t in dead:
                self._sessions.pop(t, None)
            return len(dead)


class OverviewCache:
    """كاش إحصائيات مشترك single-flight — لا مهمة خلفية لكل مستخدم.

    أول طالب يجد الكاش قديماً يحصل على «فتحة التحديث» ويُعاد إليه None
    ليعيد الحساب (عبر الجسر إلى الحلقة الرئيسية)، وبقية الطلبات المتزامنة
    تعيد آخر نسخة فوراً — صفر تكرار استعلامات مهما فتح المستخدمون تبويبات.
    """

    __slots__ = ("_data", "_ts", "_lock", "_refreshing", "_ttl")

    def __init__(self, ttl_seconds: int = DEFAULT_OVERVIEW_TTL_SECONDS) -> None:
        self._ttl = max(5, int(ttl_seconds))
        self._data: Optional[Dict[str, Any]] = None
        self._ts: float = 0.0
        self._refreshing = False
        self._lock = threading.Lock()

    def get_if_fresh(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._data is not None and (time.monotonic() - self._ts) < self._ttl:
                return self._data
            return None

    def get_stale(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._data

    def try_begin_refresh(self) -> bool:
        with self._lock:
            if self._refreshing:
                return False
            self._refreshing = True
            return True

    def end_refresh(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self._data = data
            self._ts = time.monotonic()
            self._refreshing = False

    def abort_refresh(self) -> None:
        with self._lock:
            self._refreshing = False

    def invalidate(self) -> None:
        with self._lock:
            self._data = None
            self._ts = 0.0


class DashboardState:
    """الحالة الجامعة — نسخة واحدة DASH على مستوى العملية."""

    __slots__ = (
        "assets", "ops", "sessions", "api_rate", "login_rate", "guard",
        "overview_cache", "groups_cache", "alerts_cache", "errors_cache",
        "identity_cache", "membership_cache", "cpu_last", "cpu_lock",
        "username", "version",
    )

    def __init__(self) -> None:
        self.assets: Dict[str, bytes] = {}
        self.ops = DashboardOps()
        self.sessions = SessionStore()
        self.api_rate = RateLimiter(DEFAULT_API_RATE_PER_MIN)
        self.login_rate = RateLimiter(DEFAULT_LOGIN_RATE_PER_MIN)
        self.guard = LoginGuard()
        self.overview_cache = OverviewCache()
        # كاشات تبويبات (TTL قصيرة — تُملأ عند فتح التبويب فقط)
        self.groups_cache = OverviewCache(ttl_seconds=45)
        self.alerts_cache = OverviewCache(ttl_seconds=30)
        self.errors_cache = OverviewCache(ttl_seconds=30)
        # هوية حساب get_me (كاش 5 دقائق) + عضوية (كاش 5 دقائق)
        self.identity_cache: Dict[str, Dict[str, Any]] = {}
        self.membership_cache: Dict[str, Dict[str, Any]] = {}
        self.cpu_last: float = 0.0
        self.cpu_lock = threading.Lock()
        self.username: str = "bashar"
        self.version: str = "15.1.0"

    def note_cpu(self, value: float) -> None:
        with self.cpu_lock:
            self.cpu_last = float(value)

    def read_cpu(self) -> float:
        with self.cpu_lock:
            return self.cpu_last


DASH = DashboardState()
