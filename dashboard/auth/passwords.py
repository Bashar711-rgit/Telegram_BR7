# -*- coding: utf-8 -*-
"""dashboard.auth.passwords — تجزئة كلمات المرور (المرحلة 1).

* Argon2id عبر argon2-cffi إن توفرت (الخيار المفضلة — متطلب §6.3).
* احتياط stdlib scrypt (n=2^14) إن غابت المكتبة — النظام يبقى آمناً
  ويعمل دون تثبيت إضافي، والسلسلة المخزنة تحمل بادئة الخوارزمية.
* لا يُخزن نص صريح إطلاقاً؛ النص الأصلي يُهجر بعد التجزئة/التحقق فوراً.
* كل الفشل يعيد False بلا استثناءات (فشل-آمن).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

_ARGON2 = None
_ARGON_ERRORS: tuple = ()
try:
    from argon2 import PasswordHasher as _ArgonHasher  # type: ignore
    from argon2.exceptions import (  # type: ignore
        InvalidHashError as _ArgonInvalid,
        VerifyMismatchError as _ArgonMismatch,
    )
    _ARGON2 = _ArgonHasher(time_cost=3, memory_cost=65536, parallelism=4,
                           hash_len=32, salt_len=16)
    _ARGON_ERRORS = (_ArgonMismatch, _ArgonInvalid)
except Exception:  # pragma: no cover — بيئة بلا argon2-cffi
    _ARGON2 = None

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
MIN_PASSWORD_LENGTH = 8

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


def backend_name() -> str:
    """اسم الخوارزمية الفعالة — يُخزن في panel_users.password_algo."""
    return "argon2id" if _ARGON2 is not None else "scrypt"


def lib_available() -> bool:
    return _ARGON2 is not None


def username_valid(username: str) -> bool:
    """3-32 حرفاً: حروف لاتينية/أرقام/_ . - فقط (لا مسافات ولا يونيكود)."""
    return bool(_USERNAME_RE.match(str(username or "")))


def hash_password(password: str) -> str:
    pw = str(password or "")
    if _ARGON2 is not None:
        return str(_ARGON2.hash(pw))
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                        p=SCRYPT_P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (
        SCRYPT_N, SCRYPT_R, SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    pw = str(password or "")
    st = str(stored or "")
    try:
        if st.startswith("$argon2"):
            if _ARGON2 is None:
                return False
            try:
                # argon2-cffi: verify(hash, password) — الهاش أولاً
                return bool(_ARGON2.verify(st, pw))
            except _ARGON_ERRORS:
                return False
            except Exception:
                return False
        if st.startswith("scrypt$"):
            parts = st.split("$")
            if len(parts) != 6:
                return False
            n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
            salt = base64.b64decode(parts[4])
            expected = base64.b64decode(parts[5])
            dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                                dklen=len(expected))
            return bool(hmac.compare_digest(dk, expected))
    except Exception:
        return False
    return False


def needs_rehash(stored: str) -> bool:
    """هل تتجاوز معاملات الهاش المخزن الإعدادات الحالية؟ (ترقية تدريجية)."""
    st = str(stored or "")
    if _ARGON2 is not None and st.startswith("$argon2"):
        try:
            return bool(_ARGON2.check_needs_rehash(st))
        except Exception:
            return False
    return False


def password_problem(username: str, current: str, new: str) -> str:
    """سياسة كلمة المرور عند التغيير/الإنشاء — نص خطأ عربي أو '' إذا سليمة."""
    new = str(new or "")
    if len(new) < MIN_PASSWORD_LENGTH:
        return "كلمة المرور قصيرة — 8 أحرف على الأقل"
    if new == str(current or ""):
        return "كلمة المرور الجديدة مطابقة للحالية"
    if new.lower() == str(username or "").lower():
        return "لا يجوز أن تطابق كلمة المرور اسم المستخدم"
    return ""
