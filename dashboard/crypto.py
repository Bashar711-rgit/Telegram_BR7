"""
panel/crypto.py — تشفير أسرار اللوحة (Session Strings / API Hash).

المبدأ الأمني (مواصفة المالك):
* تخزين Session String بصورة مشفرة في قاعدة البيانات — لا يُعرض أبداً
  في الواجهة ولا يُكتب في السجلات.
* يستخدم نفس مفتاح Fernet المشروعي (SECRET_KEY_OVERRIDE أو .secret_key)
  عبر SecretManager — بلا مفاتيح مكررة وبلا نُسخ متعددة من المفتاح.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class PanelCryptoError(Exception):
    """فشل تشفير/فك تشفير سر — يُعالج فشل-آمن دون كشف السر."""


def encrypt_secret(plain: str) -> str:
    """تشفير نص سرّي (Session String / API Hash) → Fernet token نصي."""
    if not plain:
        raise PanelCryptoError("empty secret")
    try:
        from config import SecretManager

        token = SecretManager.encrypt_text(plain)
        if not token:
            raise PanelCryptoError("encryptor returned empty token")
        return token
    except PanelCryptoError:
        raise
    except Exception as e:  # فشل-آمن: لا يكشف محتوى السر أبداً
        raise PanelCryptoError(f"encrypt failed: {type(e).__name__}") from e


def decrypt_secret(token: str) -> str:
    """فك تشفير سرّي مخزّن — يُستدعى فقط عند بناء عميل المراقبة."""
    if not token:
        raise PanelCryptoError("empty token")
    try:
        from config import SecretManager

        plain = SecretManager.decrypt_text(token)
        if plain is None:
            raise PanelCryptoError("decryptor returned None")
        return plain
    except PanelCryptoError:
        raise
    except Exception as e:
        raise PanelCryptoError(f"decrypt failed: {type(e).__name__}") from e


def mask_phone(phone: str) -> str:
    """إخفاء الهاتف في الواجهة: +9665XXXXXXX9 → +•••••789 (آخر 3 أرقام)."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) <= 3:
        return "+" + "*" * max(0, len(digits))
    return "+" + "*" * (len(digits) - 3) + digits[-3:]


def safe_last4(value: Optional[str]) -> str:
    """آخر 4 محارف فقط — لأي معرّف يُعرض جزئياً."""
    v = (value or "").strip()
    return v[-4:] if len(v) >= 4 else ("*" * len(v))
