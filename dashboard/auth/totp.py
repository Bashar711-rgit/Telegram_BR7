# -*- coding: utf-8 -*-
"""dashboard.auth.totp — المصادقة الثنائية ورموز الاسترداد (المرحلة 1، §6.4).

* pyotp إن توفرت؛ غيابها لا يكسر النظام: التفعيل يعيد خطأ واضحاً والدخول
  لحسابات مفعّلة TOTP يبقى ممكناً عبر رموز الاسترداد.
* سر TOTP يُشفَّر بـFernet الموجود (dashboard.crypto) قبل التخزين —
  التشفير يتم في طبقة الاستدعاء (routes) لا هنا.
* رموز الاسترداد: 8 رموز تُعرض مرة واحدة، تُخزن sha256 حصراً، تُستهلك
  لمرة واحدة (consume يحذف الهاش ويحدّث السجل).
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json as _json
import secrets as _secrets
from typing import Any, List, Optional, Tuple

try:
    import pyotp as _pyotp  # type: ignore
except Exception:  # pragma: no cover — بيئة بلا pyotp
    _pyotp = None

LIB_AVAILABLE = _pyotp is not None
RECOVERY_COUNT = 8
_ISSUER = "BR7 Dashboard"


def generate_secret() -> str:
    if _pyotp is None:
        return ""
    try:
        return str(_pyotp.random_base32())
    except Exception:
        return ""


def provisioning_uri(secret: str, account: str) -> str:
    if _pyotp is None or not secret:
        return ""
    try:
        return str(_pyotp.TOTP(secret).provisioning_uri(
            name=str(account or "owner"), issuer_name=_ISSUER))
    except Exception:
        return ""


def verify(secret: str, code: str, window: int = 1) -> bool:
    if _pyotp is None or not secret or not code:
        return False
    try:
        cleaned = str(code).strip().replace(" ", "")
        return bool(_pyotp.TOTP(str(secret)).verify(cleaned, valid_window=max(0, int(window))))
    except Exception:
        return False


# ── رموز الاسترداد ──────────────────────────────────────────────────────
def generate_recovery_codes(count: int = RECOVERY_COUNT) -> List[str]:
    out: List[str] = []
    for _ in range(max(4, min(int(count), 16))):
        raw = _secrets.token_hex(4)  # 8 hex
        out.append(f"{raw[:4]}-{raw[4:]}")
    return out


def _normalize(code: str) -> str:
    return str(code or "").strip().lower().replace(" ", "").replace("-", "")


def _code_hash(code: str) -> str:
    return hashlib.sha256(_normalize(code).encode("utf-8")).hexdigest()


def _const_eq(a: str, b: str) -> bool:
    try:
        return bool(_hmac.compare_digest(str(a or ""), str(b or "")))
    except Exception:
        return False


def hash_recovery_codes(codes: List[str]) -> str:
    """JSON من sha256 لكل رمز — يُخزن في panel_users.recovery_codes."""
    return _json.dumps([_code_hash(c) for c in (codes or [])], separators=(",", ":"))


def consume_recovery_code(stored_json: Any, code: str) -> Tuple[bool, Optional[str], int]:
    """(نجاح، JSON المحدث أو None إن لم يتغير، المتبقي).

    الفحص زمن-ثابت لكل هاش؛ الحذف من القائمة فقط عند النجاح.
    """
    try:
        hashes = _json.loads(str(stored_json or "[]"))
        if not isinstance(hashes, list):
            return False, None, 0
    except Exception:
        return False, None, 0
    target = _code_hash(code)
    matched_idx = -1
    for i, h in enumerate(hashes):
        if h and _const_eq(str(h), target):
            matched_idx = i
            break
    if matched_idx < 0:
        return False, None, len(hashes)
    remaining = [h for i, h in enumerate(hashes) if i != matched_idx]
    return True, _json.dumps(remaining, separators=(",", ":")), len(remaining)
