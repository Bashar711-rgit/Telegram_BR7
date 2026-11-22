"""
panel/accounts.py — بناء ودمج حسابات اللوحة (v14.10.0).

وحدة خفيفة بلا تبعية aiohttp — تُستوردها main.py بأمان.

* build_panel_account_dict(row): يحوّل صف panel_accounts إلى قاموس حساب
  متوافق مع EnhancedAccountMonitor — يفك تشفير Session String/api_hash
  في الذاكرة فقط عند الحاجة (بناء عميل مراقبة)، ولا يُسجّل المحلّ نصاً.
* merge_panel_accounts(rows, env_accounts): دمج حسابات DB مع حسابات env
  عند الإقلاع — env أولوية؛ المكرر بالرقم يُعلَّم ليُحدَّث في DB.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _norm_key(phone: Any) -> str:
    return re.sub(r"\D", "", str(phone or ""))


def _fallback_api_creds() -> Tuple[Optional[int], Optional[str]]:
    """api_id/api_hash احتياطي من حسابات env (الوحدة النيوم تستخدم واحدة)."""
    try:
        from config import ACCOUNTS

        for acc in ACCOUNTS:
            if acc.get("api_id") and acc.get("api_hash"):
                return int(acc["api_id"]), str(acc["api_hash"])
    except Exception:
        pass
    import os

    try:
        api_id = int(os.getenv("PANEL_API_ID", "") or 0)
    except ValueError:
        api_id = 0
    api_hash = (os.getenv("PANEL_API_HASH", "") or "").strip() or None
    return (api_id or None), api_hash


def build_panel_account_dict(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    صف panel_accounts → قاموس حساب EnhancedAccountMonitor.

    يفك تشفير session_encrypted/api_hash_encrypted في الذاكرة فقط.
    فشل فك التشفير (مفتاح تغيّر مثلاً) → None مع تعيين الحالة المناسبة
    على الصف من المتصل (needs_relogin).
    """
    row_id = int(row.get("id") or 0)
    phone = (row.get("phone") or "").strip()
    if not phone or not row_id:
        return None

    session_string: Optional[str] = None
    try:
        from dashboard.crypto import decrypt_secret

        session_string = decrypt_secret(str(row.get("session_encrypted") or ""))
    except Exception as e:
        logger.error(f"panel account {row_id}: session decrypt failed ({type(e).__name__})")
        return None
    if not session_string:
        return None

    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    try:
        raw_api_id = (row.get("api_id") or "").strip()
        raw_api_hash_enc = (row.get("api_hash_encrypted") or "").strip()
        if raw_api_id and raw_api_hash_enc:
            api_id = int(raw_api_id)
            from dashboard.crypto import decrypt_secret as _ds

            api_hash = _ds(raw_api_hash_enc)
    except Exception:
        api_id = None
        api_hash = None
    if not api_id or not api_hash:
        api_id, api_hash = _fallback_api_creds()
    if not api_id or not api_hash:
        logger.error(f"panel account {row_id}: no api creds available (env + row)")
        return None

    display_name = (row.get("display_name") or "").strip() or f"حساب {phone[-4:]}"
    monitoring_enabled = bool(int(row.get("monitoring_enabled") or 0))

    return {
        "id": 1000 + row_id,           # يبدأ بعد حسابات env (أرقامها صغيرة)
        "row_id": row_id,
        "prefix": f"PANEL_{row_id}",
        "name": display_name,
        "api_id": api_id,
        "api_hash": api_hash,
        "phone": phone,
        "session": f"panel_{row_id}",  # اسم جلسة احتياطي (يُستخدم فقط بلا StringSession)
        "session_string": session_string,
        "priority": int(row.get("priority") or 5),
        "is_main": False,
        "enabled": monitoring_enabled,
        "source": "panel",
        "tg_user_id": int(row.get("tg_user_id") or 0) or None,
        "username": (row.get("username") or None),
        "retry_count": 0,
        "last_error": None,
    }


def merge_panel_accounts(
    rows: List[Dict[str, Any]], env_accounts: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[int], List[int]]:
    """
    دمج حسابات اللوحة (DB) مع حسابات env عند الإقلاع.

    يرجع:
      * list[dict]: قواميس حسابات panel جاهزة للاتصال (المراقبة مفعّلة فقط)
      * list[int]: row_ids المكررة مع env (تُحدَّث حالتها في DB)
      * list[int]: row_ids التي فشل فك تشفيرها (needs_relogin/error)
    """
    built: List[Dict[str, Any]] = []
    duplicates: List[int] = []
    undecryptable: List[int] = []
    env_keys = {_norm_key(a.get("phone")) for a in env_accounts if a.get("phone")}

    for row in rows:
        row_id = int(row.get("id") or 0)
        if not int(row.get("monitoring_enabled") or 0):
            continue  # حساب متوقف المراقبة — يبقى محفوظاً بلا اتصال
        if _norm_key(row.get("phone")) in env_keys:
            duplicates.append(row_id)
            continue
        acc = build_panel_account_dict(row)
        if acc is None:
            undecryptable.append(row_id)
            continue
        built.append(acc)
    return built, duplicates, undecryptable
