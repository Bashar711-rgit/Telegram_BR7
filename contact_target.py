#!/usr/bin/env python3
"""
contact_target.py — ContactTarget v14.5.2 — تجريد وسيلة الوصول إلى المرسل
============================================================================

v14.5.2 (طلب المستخدم — توسيع وسائل الوصول):
  * ContactTarget now carries two new optional fields:
        phone_url            — https://t.me/+<phone>  (from sender_phone)
        temporary_link_url   — https://t.me/+<token>  (from exportContactToken)
    Both are HTTPS (Bot API accepts them as inline button URLs), and
    both work when the sender has NO username. They sit between username
    and openmessage in the priority ladder:
        username > temporary_link > phone > openmessage > user_id > none
  * resolve_contact_target() gained two optional params:
        phone               — raw phone value (passed to build_phone_url)
        temporary_link_url  — pre-computed by the caller (async API call)
    phone is pure-sync (built here); temporary_link_url is optional and
    provided as-is (the exportContactToken call is async, so it lives
    in sender_resolver.export_temporary_link and the caller invokes it
    BEFORE resolve_contact_target — see monitors.py activation note).
  * build_phone_url() imported from sender_resolver — same validation
    for both call paths (no duplicated logic).
  * validate_contact_target() now also accepts tg://openmessage? URLs
    (they are a legit last-resort fallback even if Bot API may reject
    them; callers must handle rejection with the existing sanitize path).
  * Logging (log_sender_resolution) shows phone_target / temporary_target
    availability per alert — same shape as user_id_target / openmessage.

v14.5.1 (سابق):
  * is_bot now recognizes 1, "1", "yes", "on" (via _is_truthy helper).
  * B_Bot removed from the card header (SENDER_LABEL starts with "المرسل:").
  * __version__ = "14.5.1".

الطرق المحفوظة داخليًا (spec #4–#8):
    1. username         → https://t.me/{username}             (الأفضل)
    2. temporary_link   → https://t.me/+{token}               (HTTPS مؤقت)
    3. phone            → https://t.me/+{phone}               (HTTPS عند توفر الرقم)
    4. openmessage      → tg://openmessage?user_id={id}        (قد يُرفض)
    5. user_id          → tg://user?id={id}                    (قد يُرفض)
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

try:
    from telethon.tl.types import MessageEntityTextUrl
    _TL_OK = True
except Exception:
    MessageEntityTextUrl = None
    _TL_OK = False

from nav_resolver import clean_username as _clean_username  # noqa: E402
from nav_resolver import is_valid_username as _is_valid_username  # noqa: E402
# v14.5.2: نفس منطق تطبيع رقم الهاتف المستخدم في sender_resolver.
from sender_resolver import build_phone_url as _build_phone_url  # noqa: E402

__version__ = "14.5.2"

_TG_URL = "https://t.me/"

# شكل التنبيه الفعال (v14.5.1 — B_Bot مخفي):
#     المرسل: [اسم المرسل قابل للنقر] — @username
#     (سطر فارغ)
#     نص الرسالة:
#     (سطر فارغ)
#     [النص الأصلي كاملاً]
BOT_NAME_LINE = "B_Bot"  # مطلوب في monitors.py لفحص البطاقات
SENDER_LABEL = "المرسل: "
MSG_TEXT_LABEL = "نص الرسالة:"
SENDER_DASH = " — "


def build_username_suffix(username: Any = None) -> str:
    try:
        u = str(username or "").strip().lstrip("@")
    except Exception:
        return ""
    if not u or not _is_valid_username(u):
        return ""
    return f"{SENDER_DASH}@{u}"


def build_username_suffix_linked(username: Any = None, url: Any = None) -> str:
    try:
        u = str(username or "").strip().lstrip("@")
        link = str(url or "").strip()
    except Exception:
        return ""
    if not u or not _is_valid_username(u):
        return ""
    if not (link.startswith("https://") or link.startswith("http://")
            or link.startswith("tg://")):
        return ""
    return f'{SENDER_DASH}<a href="{link}">@{u}</a>'


_USER_ID_CAPABLE_TYPES = frozenset({"user", "bot", "assumed_user"})


def _is_truthy(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) == 1
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return False


_CONTACT_METRICS: Dict[str, int] = defaultdict(int)

_METRIC_KEYS = (
    "sender_resolution_success",
    "sender_resolution_failure",
    "username_available",
    "user_id_fallback",
    "temporary_link_used",      # v14.5.2
    "phone_fallback",            # v14.5.2
    "openmessage_fallback",      # v14.5.2
    "clickable_sender_success",
    "clickable_sender_failure",
    "contact_button_success",
    "contact_button_failure",
    "name_N1", "name_N2", "name_N3", "name_N4", "name_N5",
    "tier_T1", "tier_T2", "tier_T3", "tier_T4", "tier_T5",
    "contact_B1", "contact_B2", "contact_B3", "contact_B4", "contact_B5",
    "view_V1", "view_V2", "view_V3", "view_V4",
    "reply_fix_success", "reply_fix_failure", "reply_fix_skipped",
    "user_buttons_dropped",
)


def metric_inc(name: str, by: int = 1) -> None:
    try:
        if name in _METRIC_KEYS:
            _CONTACT_METRICS[name] += int(by)
    except Exception:
        pass


def get_contact_metrics_snapshot() -> Dict[str, Any]:
    snap: Dict[str, Any] = {"enabled": True, "version": __version__}
    for key in _METRIC_KEYS:
        snap[key] = int(_CONTACT_METRICS.get(key, 0))
    return snap


def utf16_len(value: str) -> int:
    try:
        return len(value.encode("utf-16-le")) // 2
    except Exception:
        return len(value or "")


def build_display_name(
    first_name: Any = None,
    last_name: Any = None,
    username: Any = None,
    user_id: Any = 0,
    display: Any = None,
) -> str:
    if display and str(display).strip():
        return str(display).strip()
    full = f"{first_name or ''} {last_name or ''}".strip()
    if full:
        return full
    uname = _clean_username(username)
    if uname:
        return uname
    return "مرسل غير محدد"


@dataclass(slots=True)
class ContactTarget:
    user_id: int = 0
    username: Optional[str] = None
    usernames: Optional[List[str]] = None
    display_name: str = ""
    sender_type: str = "none"
    username_url: Optional[str] = None
    user_id_url: Optional[str] = None
    openmessage_url: Optional[str] = None
    # v14.5.2: مسارات HTTPS إضافية تعمل حتى بلا username
    phone_url: Optional[str] = None
    temporary_link_url: Optional[str] = None
    selected_url: Optional[str] = None
    target_type: str = "none"
    resolution_status: str = "failed"
    reasons: List[str] = field(default_factory=list)

    @property
    def reachable(self) -> bool:
        return bool(self.selected_url)

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id or None,
            "username": self.username,
            "display_name": self.display_name,
            "sender_type": self.sender_type,
            "selected_url": self.selected_url,
            "target_type": self.target_type,
            "targets_available": ",".join(
                [k for k, v in (
                    ("username", self.username_url),
                    ("temporary", self.temporary_link_url),
                    ("phone", self.phone_url),
                    ("openmessage", self.openmessage_url),
                    ("user_id", self.user_id_url),
                ) if v]
            ) or "none",
            "resolution_status": self.resolution_status,
            "reasons": ",".join(self.reasons) or "-",
        }


def resolve_contact_target(
    sender_id: Any = 0,
    username: Any = None,
    usernames: Any = None,
    first_name: Any = None,
    last_name: Any = None,
    display: Any = None,
    sender_type: Any = "",
    is_bot: Any = None,
    is_deleted: Any = None,
    phone: Any = None,                       # v14.5.2: NEW
    temporary_link_url: Optional[str] = None, # v14.5.2: NEW
) -> ContactTarget:
    """
    يبني ContactTarget من بيانات المرسل الملتقطة — **بلا أي I/O**.
    """
    started = time.perf_counter()
    reasons: List[str] = []
    try:
        sid = int(sender_id or 0)
    except Exception:
        sid = 0
    if sid < 0:
        reasons.append("negative_sender_id")
        sid = 0

    uname = _clean_username(username) or None
    all_unames: Optional[List[str]] = None
    if not uname and usernames:
        try:
            for ru in (usernames or []):
                name = ru if isinstance(ru, str) else getattr(ru, "username", None)
                clean = _clean_username(name)
                if clean and _is_valid_username(clean):
                    uname = clean
                    break
        except Exception:
            pass
    if uname and not _is_valid_username(uname):
        reasons.append("invalid_username_shape")
        uname = None
    if uname:
        all_unames = [uname]
        try:
            for ru in (usernames or []):
                name = ru if isinstance(ru, str) else getattr(ru, "username", None)
                clean = _clean_username(name)
                if clean and clean != uname and clean not in all_unames:
                    all_unames.append(clean)
        except Exception:
            pass

    st = str(sender_type or "").strip().lower()
    if st in ("", "unknown", "none"):
        st = "assumed_user" if sid else "none"
    if _is_truthy(is_deleted):
        st = "deleted"
    elif st in ("user", "assumed_user") and _is_truthy(is_bot):
        st = "bot"
    if st == "channel":
        reasons.append("channel_sender_no_user_id")

    # ── المسارات المتاحة ──
    username_url = f"{_TG_URL}{uname}" if uname else None

    # v14.5.2: temporary link من exportContactToken (HTTPS، يعمل بلا username).
    temp_url: Optional[str] = None
    if temporary_link_url:
        _t = str(temporary_link_url).strip()
        if _t.startswith("https://t.me/"):
            temp_url = _t

    # v14.5.2: phone → https://t.me/+<phone> (HTTPS، يعمل بلا username).
    phone_url: Optional[str] = _build_phone_url(phone) if phone else None

    # openmessage: مقيَّد بنفس قيد user_id_url (لا للقنوات/المحذوفين).
    openmessage_url = (
        f"tg://openmessage?user_id={sid}"
        if (sid and st in _USER_ID_CAPABLE_TYPES)
        else None
    )
    user_id_url = (
        f"tg://user?id={sid}"
        if (sid and st in _USER_ID_CAPABLE_TYPES)
        else None
    )

    # ── الاختيار: username > temporary_link > phone > openmessage > user_id ──
    if username_url:
        selected_url, target_type = username_url, "username"
    elif temp_url:
        selected_url, target_type = temp_url, "temporary_link"
    elif phone_url:
        selected_url, target_type = phone_url, "phone"
    elif openmessage_url:
        selected_url, target_type = openmessage_url, "openmessage"
    elif user_id_url:
        selected_url, target_type = user_id_url, "user_id"
    else:
        selected_url, target_type = None, "none"

    if uname:
        metric_inc("username_available")
    if target_type == "user_id":
        metric_inc("user_id_fallback")
    if target_type == "temporary_link":
        metric_inc("temporary_link_used")
    if target_type == "phone":
        metric_inc("phone_fallback")
    if target_type == "openmessage":
        metric_inc("openmessage_fallback")

    if selected_url:
        status = "resolved"
        metric_inc("sender_resolution_success")
    elif sid or uname:
        status = "partial"
        metric_inc("sender_resolution_failure")
        if not reasons:
            reasons.append("no_reachable_target")
    else:
        status = "failed"
        metric_inc("sender_resolution_failure")
        if not reasons:
            reasons.append("no_sender_identity")

    target = ContactTarget(
        user_id=sid,
        username=uname,
        usernames=all_unames,
        display_name=build_display_name(first_name, last_name, uname, sid, display),
        sender_type=st,
        username_url=username_url,
        user_id_url=user_id_url,
        openmessage_url=openmessage_url,
        phone_url=phone_url,
        temporary_link_url=temp_url,
        selected_url=selected_url,
        target_type=target_type,
        resolution_status=status,
        reasons=reasons,
    )
    try:
        logger.debug(
            f"contact_target resolved in {(time.perf_counter() - started) * 1000:.2f}ms | "
            + " ".join(f"{k}={v}" for k, v in target.to_log_dict().items())
        )
    except Exception:
        pass
    return target


def validate_contact_target(target: Optional[ContactTarget]) -> Tuple[bool, str]:
    if target is None:
        return False, "no_target"
    if target.sender_type in ("channel", "deleted", "none") and not target.username_url:
        return False, f"sender_type_{target.sender_type}_not_linkable"
    if not (target.display_name or "").strip():
        return False, "no_display_name"
    if not target.selected_url:
        return False, "no_reachable_target"
    # v14.5.2: مسموح أيضاً tg://openmessage? (احتياطي أخير قد يقبله Bot API
    # أو يُرفض — alert_bot يعالجه بالـsanitize).
    if not (
        target.selected_url.startswith("https://t.me/")
        or target.selected_url.startswith("tg://user?id=")
        or target.selected_url.startswith("tg://openmessage?")
    ):
        return False, "unknown_url_shape"
    if MessageEntityTextUrl is None:
        return False, "entity_class_unavailable"
    return True, ""


def build_alert_entity_pack(
    display_name: str,
    message_text: str,
    target: Optional[ContactTarget],
    *,
    rule_tag: Optional[str] = None,
    plain_budget: int = 3800,
    name_url_override: Optional[str] = None,
) -> Dict[str, Any]:
    display = str(display_name or "").strip() or "مستخدم"
    raw = str(message_text or "")
    if len(raw) > max(64, int(plain_budget)):
        raw = raw[: max(64, int(plain_budget)) - 1].rstrip() + "…"

    uname = getattr(target, "username", None) if target is not None else None
    plain_text = (
        f"{SENDER_LABEL}{display}{build_username_suffix(uname)}"
        f"\n\n{MSG_TEXT_LABEL}\n\n{raw}"
    )
    entities: List[Any] = []

    try:
        name_off = utf16_len(SENDER_LABEL)
        name_len = utf16_len(display)
        name_url = (str(name_url_override).strip() if name_url_override else None) or (
            target.selected_url if target is not None else None
        )
        if target is not None and name_url and MessageEntityTextUrl is not None:
            entities.append(
                MessageEntityTextUrl(offset=name_off, length=name_len, url=name_url)
            )
    except Exception as e:
        logger.debug(f"contact_target entity pack failed: {type(e).__name__}: {e}")
        entities = []

    try:
        uname = getattr(target, "username", None) if target is not None else None
        uname_url = (str(name_url_override).strip() if name_url_override else None) or (
            target.selected_url if target is not None else None
        )
        _u = str(uname or "").strip().lstrip("@")
        if _u and uname_url and _is_valid_username(_u) and MessageEntityTextUrl is not None:
            uname_off = (
                utf16_len(SENDER_LABEL) + utf16_len(display) + utf16_len(SENDER_DASH)
            )
            uname_len = utf16_len(f"@{_u}")
            entities.append(
                MessageEntityTextUrl(offset=uname_off, length=uname_len, url=uname_url)
            )
    except Exception as e:
        logger.debug(f"contact_target username entity failed: {type(e).__name__}: {e}")

    suffix = ""
    if rule_tag:
        suffix = f"\n\n🏷 قاعدة: {str(rule_tag)}"
    if suffix:
        plain_text += suffix

    return {
        "plain_text": plain_text,
        "formatting_entities": entities or None,
        "clickable": bool(
            target is not None
            and ((str(name_url_override).strip() if name_url_override else None)
                 or target.selected_url)
            and any(type(e).__name__ == "MessageEntityTextUrl" for e in entities)
        ),
    }


def log_sender_resolution(
    target: Optional[ContactTarget],
    *,
    message_id: Any = None,
    chat_id: Any = None,
    sender_id: Any = None,
    resolution: str = "event",
    clickable: Optional[bool] = None,
    contact_button: Optional[bool] = None,
) -> None:
    try:
        t = target or ContactTarget()
        fields = [
            f"[SENDER] message_id={message_id if message_id is not None else '-'}",
            f"chat_id={chat_id if chat_id is not None else '-'}",
            f"sender_id={sender_id if sender_id is not None else (t.user_id or '-')}",
            f"username={t.username or 'None'}",
            f"display_name={t.display_name or '-'}",
            f"resolution={resolution}",
            "user_id_target=" + ("AVAILABLE" if t.user_id_url else "UNAVAILABLE"),
            "openmessage_target=" + ("AVAILABLE" if t.openmessage_url else "UNAVAILABLE"),
            # v14.5.2: مسارات جديدة في اللوج
            "phone_target=" + ("AVAILABLE" if t.phone_url else "UNAVAILABLE"),
            "temporary_target=" + ("AVAILABLE" if t.temporary_link_url else "UNAVAILABLE"),
            f"selected_target={t.target_type}",
        ]
        if clickable is not None:
            fields.append("clickable_name=" + ("SUCCESS" if clickable else "FAILED"))
        if contact_button is not None:
            fields.append("contact_button=" + ("SUCCESS" if contact_button else "FAILED"))
        fields.append(f"status={t.resolution_status}")
        if t.reasons:
            fields.append(f"reason={t.reasons[0]}")
        line = " ".join(fields)
        if clickable is False:
            logger.warning(line)
        else:
            logger.info(line)
    except Exception:
        pass
