# -*- coding: utf-8 -*-
"""dashboard.auth.users — مستودع مستخدمي اللوحة والجلسات (المرحلة 1).

كل الدوال هنا async وتعمل **حصراً على الحلقة الرئيسية للبوت** (تُستدعى
عبر الجسر run_coroutine_threadsafe من معالجات aiohttp) — نفس قاعدة
dashboard/api.py: يستخدم db الموجود (bot.db) بلا pool جديد.

الجداول (ترحيل 0006 — additive بحت):
  panel_users     id, username UNIQUE, password_hash, password_algo,
                  display_name, role, is_active, must_change_password,
                  totp_enabled, totp_secret_encrypted, recovery_codes,
                  created_at, updated_at, last_login_at
  panel_sessions  token_hash PK, csrf_hash, user_id, username, role,
                  must_change, ip, user_agent, created_at REAL,
                  last_seen REAL, expires_at REAL, revoked_at REAL

لا دالة هنا تُعيد قيمة سرية: الهاشات والأسرار المشفرة تبقى داخلياً.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

_USERS_MAX = 50            # سقف مستخدمي اللوحة (حماية منطقية)
_SESSIONS_LIST_MAX = 200

_USER_COLS = ("id, username, password_hash, password_algo, display_name, role, "
              "is_active, must_change_password, totp_enabled, totp_secret_encrypted, "
              "recovery_codes, created_at, updated_at, last_login_at")

_SESSION_COLS = ("token_hash, csrf_hash, user_id, username, role, must_change, "
                 "ip, user_agent, created_at, last_seen, expires_at, revoked_at")


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ═════════════════════════════════════════════════════════════════════════
# المستخدمون
# ═════════════════════════════════════════════════════════════════════════
async def count_users(db: Any) -> int:
    try:
        row = await db._fetchone("SELECT COUNT(*) AS n FROM panel_users", ())
        return _safe_int((row or {}).get("n"), 0)
    except Exception:
        return 0


async def count_active_owners(db: Any) -> int:
    try:
        row = await db._fetchone(
            "SELECT COUNT(*) AS n FROM panel_users WHERE role = 'owner' AND is_active = 1", ())
        return _safe_int((row or {}).get("n"), 0)
    except Exception:
        return 0


async def get_user_by_username(db: Any, username: str) -> Optional[Dict[str, Any]]:
    try:
        row = await db._fetchone(
            f"SELECT {_USER_COLS} FROM panel_users WHERE username = ? LIMIT 1",
            (str(username or "").strip()[:64],))
        return dict(row) if row else None
    except Exception:
        return None


async def get_user_by_id(db: Any, user_id: int) -> Optional[Dict[str, Any]]:
    try:
        row = await db._fetchone(
            f"SELECT {_USER_COLS} FROM panel_users WHERE id = ? LIMIT 1",
            (_safe_int(user_id),))
        return dict(row) if row else None
    except Exception:
        return None


async def list_users(db: Any) -> List[Dict[str, Any]]:
    """قائمة مستخدمين بلا أعمدة أسرار (لا هاشات ولا أسرار TOTP)."""
    try:
        rows = await db._fetchall(
            "SELECT id, username, display_name, role, is_active, must_change_password, "
            "totp_enabled, created_at, updated_at, last_login_at "
            "FROM panel_users ORDER BY id ASC LIMIT ?", (_USERS_MAX,))
        return [dict(r) for r in rows or []]
    except Exception:
        return []


async def create_user(db: Any, username: str, password_hash: str, password_algo: str,
                      role: str, display_name: str = "",
                      must_change: int = 0) -> Optional[Dict[str, Any]]:
    """إنشاء مستخدم — يعيد الصف الكامل أو None عند تعارض اسم/امتلاء سقف."""
    uname = str(username or "").strip()[:64]
    try:
        n = await count_users(db)
        if n >= _USERS_MAX:
            return None
        now = time.time()
        await db._execute(
            "INSERT INTO panel_users (username, password_hash, password_algo, display_name, "
            "role, is_active, must_change_password, totp_enabled, totp_secret_encrypted, "
            "recovery_codes, created_at, updated_at, last_login_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, 0, '', '[]', ?, ?, 0)",
            (uname, str(password_hash), str(password_algo)[:24],
             str(display_name or "")[:80], str(role or "viewer"),
             _safe_int(must_change), now, now))
        await db._commit()
        return await get_user_by_username(db, uname)
    except Exception:
        try:
            await db._commit()
        except Exception:
            pass
        return None


_UPDATABLE = frozenset({
    "password_hash", "password_algo", "display_name", "role", "is_active",
    "must_change_password", "totp_enabled", "totp_secret_encrypted", "recovery_codes",
})


async def update_user_fields(db: Any, user_id: int, **sets: Any) -> bool:
    cols = []
    params: List[Any] = []
    for k, v in (sets or {}).items():
        if k not in _UPDATABLE:
            continue
        cols.append(f"{k} = ?")
        if k == "recovery_codes" and not isinstance(v, str):
            v = "[]"
        params.append(v)
    if not cols:
        return False
    cols.append("updated_at = ?")
    params.append(time.time())
    params.append(_safe_int(user_id))
    try:
        await db._execute(f"UPDATE panel_users SET {', '.join(cols)} WHERE id = ?",
                          tuple(params))
        await db._commit()
        return True
    except Exception:
        return False


async def delete_user_by_id(db: Any, user_id: int) -> bool:
    try:
        await db._execute("DELETE FROM panel_sessions WHERE user_id = ?", (_safe_int(user_id),))
        await db._execute("DELETE FROM panel_users WHERE id = ?", (_safe_int(user_id),))
        await db._commit()
        return True
    except Exception:
        return False


async def bump_last_login(db: Any, user_id: int) -> None:
    try:
        await db._execute("UPDATE panel_users SET last_login_at = ? WHERE id = ?",
                          (time.time(), _safe_int(user_id)))
        await db._commit()
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════
# صفوف الجلسات (panel_sessions)
# ═════════════════════════════════════════════════════════════════════════
async def insert_session_row(db: Any, meta: Dict[str, Any]) -> bool:
    try:
        await db._execute(
            f"INSERT INTO panel_sessions ({_SESSION_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (str(meta.get("token_hash") or "")[:64],
             str(meta.get("csrf_hash") or "")[:64],
             _safe_int(meta.get("user_id")),
             str(meta.get("username") or "")[:64],
             str(meta.get("role") or "viewer")[:16],
             1 if meta.get("must_change") else 0,
             str(meta.get("ip") or "")[:64],
             str(meta.get("ua") or "")[:200],
             float(meta.get("created") or time.time()),
             time.time(),
             float(meta.get("expires") or (time.time() + 8 * 3600))))
        await db._commit()
        return True
    except Exception:
        try:
            await db._commit()
        except Exception:
            pass
        return False


async def revoke_session_rows(db: Any, token_hashes: List[str]) -> int:
    if not token_hashes:
        return 0
    hashes = [str(h)[:64] for h in token_hashes if h][:100]
    n = 0
    for th in hashes:
        try:
            await db._execute(
                "UPDATE panel_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (time.time(), th))
            n += 1
        except Exception:
            continue
    try:
        await db._commit()
    except Exception:
        pass
    return n


async def revoke_all_user_sessions(db: Any, user_id: int, except_hash: str = "") -> int:
    try:
        now = time.time()
        if except_hash:
            await db._execute(
                "UPDATE panel_sessions SET revoked_at = ? WHERE user_id = ? "
                "AND revoked_at IS NULL AND token_hash != ?",
                (now, _safe_int(user_id), str(except_hash)[:64]))
        else:
            await db._execute(
                "UPDATE panel_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (now, _safe_int(user_id)))
        await db._commit()
        return 1
    except Exception:
        return 0


async def list_active_session_rows(db: Any) -> List[Dict[str, Any]]:
    """للترطيب عند الإقلاع — جلسات غير منتهية/غير ملغاة."""
    try:
        rows = await db._fetchall(
            f"SELECT {_SESSION_COLS} FROM panel_sessions "
            "WHERE revoked_at IS NULL AND expires_at > ? LIMIT ?",
            (time.time(), _SESSIONS_LIST_MAX))
        return [dict(r) for r in rows or []]
    except Exception:
        return []


async def list_user_sessions(db: Any, user_id: Optional[int] = None,
                             include_revoked: bool = False) -> List[Dict[str, Any]]:
    """لعرض «الجلسات النشطة» في الواجهة — بلا قيم خام أبداً (هاشات فقط)."""
    try:
        where = [] if include_revoked else ["revoked_at IS NULL", "expires_at > ?"]
        params: List[Any] = [] if include_revoked else [time.time()]
        if user_id is not None:
            where.append("user_id = ?")
            params.append(_safe_int(user_id))
        sql = (f"SELECT {_SESSION_COLS} FROM panel_sessions"
               + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY last_seen DESC LIMIT ?")
        params.append(_SESSIONS_LIST_MAX)
        rows = await db._fetchall(sql, tuple(params))
        return [dict(r) for r in rows or []]
    except Exception:
        return []


async def touch_session_rows(db: Any, items: List[Tuple[float, float, str]]) -> int:
    """تحديث انزلاقي دوري (expires_at, last_seen) — من مسح خيط الصحة."""
    n = 0
    for expires, seen, th in (items or [])[:_SESSIONS_LIST_MAX]:
        try:
            await db._execute(
                "UPDATE panel_sessions SET expires_at = ?, last_seen = ? "
                "WHERE token_hash = ? AND revoked_at IS NULL",
                (float(expires), float(seen), str(th)[:64]))
            n += 1
        except Exception:
            continue
    try:
        await db._commit()
    except Exception:
        pass
    return n


async def purge_expired_session_rows(db: Any, older_than_epoch: float) -> int:
    """حذف المنتهية/الملغاة القديمة — تنظيف حجم الجدول."""
    try:
        await db._execute(
            "DELETE FROM panel_sessions WHERE expires_at < ? "
            "OR (revoked_at IS NOT NULL AND revoked_at < ?)",
            (time.time(), float(older_than_epoch)))
        await db._commit()
        return 1
    except Exception:
        return 0
