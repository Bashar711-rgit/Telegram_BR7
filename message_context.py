"""message_context.py — v11.7.1: طبقة توحيد جمع معلومات المرسل/المحادثة/الرسالة.

المهمة الهندسية §3: طبقة مركزية واحدة تجمع البيانات مرة واحدة ثم تمررها
إلى بناء التنبيه والأزرار — بدل أن تعيد كل دالة الاستخراج المستقل.

التصميم (حاكم):
  * غلاف رقيق فوق الوحدات القائمة حصراً (_event_to_dict data /
    sender_resolver / contact_target / nav_resolver / alert_bot.
    build_message_link) — لا منطق موازٍ ولا إعادة كتابة.
  * أربع حالات صريحة لكل حقل: قيمة مؤكدة | UNAVAILABLE (تعذر الجلب)
    | NOT_APPLICABLE (لا ينطبق) | مفقود مسجل في missing[].
  * user_id=0 وuser_id=None ليسا هوية (قاعدة الدقة رقم 1) — لا هوية
    افتراضية ولا خلط user_id/chat_id/message_id.
  * بلا شبكة لكل حقل: بيانات الحدث والكاش فقط؛ الطلب الوحيد المسموح
    (get_sender للحدث الحي) بمهلة قصيرة محصورة 1.5s.
  * لا هواتف ولا عناوين IP ولا بيانات جلسات — أبداً.
  * الجامع لا يرفع استثناءً أبداً: فشل أي مصدر يعزل نفسه ويبقَى بقية
    البيانات — نقص معلومة لا يُسقط تنبيهاً (§9).

الواجهة العامة:
  collect_message_context(event, *, analysis=None, account_name="",
                          overrides=None) -> dict
  UNAVAILABLE / NOT_APPLICABLE               ثوابت تمثيل الحالات
  is_tg_user_blocked(sid) / mark_tg_user_blocked(sid) / reset_tg_user_blocked()
      ذاكرة سلبية لرفض tg://user (BUTTON_USER_INVALID) — TTL 6 ساعات.
  build_sender_link(ctx) / build_message_link_result(ctx) /
  build_contact_action(ctx)                  دوال اختيار الوجهة (§4)
  context_from_contact_target(target)        توافق مع مسار ContactTarget
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ══ ثوابت تمثيل حالات البيانات (§3.4) ══════════════════════════════════
UNAVAILABLE = "UNAVAILABLE"          # تعذر الجلب (فشل مصدر/غياب بيانات)
NOT_APPLICABLE = "NOT_APPLICABLE"    # لا ينطبق على هذا النوع

_COLLECTOR_VERSION = "1.0.0"
_LIVE_SENDER_TIMEOUT = 1.5           # الطلب الوحيد المسموح — مهلة قصوى

# ══ v11.7.1: ذاكرة سلبية tg://user (إصلاح 3 من بروتوكول المهلة) ════════
# BUTTON_USER_INVALID = قيد Telegram حقيقي (البوت لا يشارك الجلسة مع
# المرسل) — إعادة المحاولة نفسها كل تنبيه هدر (إرسال + تعديل). الذاكرة
# تجعل التنبيه التالي لنفس المرسل يبدأ مباشرة برابط الرسالة المصدر.
_TG_USER_BLOCKED_TTL = 6 * 3600      # 6 ساعات
_TG_USER_BLOCKED_MAX = 8192          # سقف مقيد (LRU بسيط)
_TG_USER_BLOCKED: Dict[int, float] = {}


def mark_tg_user_blocked(sender_id: Any) -> bool:
    """سجّل رفض tg://user لهذا المعرّف (TTL 6h). لا يرفع استثناءً."""
    try:
        sid = int(sender_id or 0)
    except Exception:
        return False
    if sid <= 0:
        return False  # 0/سالب ليست هوية مستخدم — لا تُخزَّن
    now = time.time()
    _TG_USER_BLOCKED[sid] = now
    if len(_TG_USER_BLOCKED) > _TG_USER_BLOCKED_MAX:
        for k in [k for k, v in _TG_USER_BLOCKED.items()
                  if now - v > _TG_USER_BLOCKED_TTL]:
            _TG_USER_BLOCKED.pop(k, None)
    while len(_TG_USER_BLOCKED) > _TG_USER_BLOCKED_MAX:
        _TG_USER_BLOCKED.pop(next(iter(_TG_USER_BLOCKED)))
    return True


def is_tg_user_blocked(sender_id: Any) -> bool:
    """هل رُفض tg://user لهذا المعرّف خلال TTL؟ (قراءة نظيفة بلا تأثير)"""
    try:
        sid = int(sender_id or 0)
    except Exception:
        return False
    if sid <= 0:
        return False
    ts = _TG_USER_BLOCKED.get(sid)
    if ts is None:
        return False
    if time.time() - ts > _TG_USER_BLOCKED_TTL:
        _TG_USER_BLOCKED.pop(sid, None)
        return False
    return True


def reset_tg_user_blocked() -> None:
    """للاختبارات — تصفير كامل للذاكرة السلبية."""
    _TG_USER_BLOCKED.clear()


# ══ أدوات داخلية آمنة ══════════════════════════════════════════════════
def _is_missing(v: Any) -> bool:
    """هل القيمة «غير متاحة/لا تنطبق/غائبة»؟ (لا UNAVAILABLE تسرّب للاستهلاك)"""
    if v is None:
        return True
    if isinstance(v, str) and v in (UNAVAILABLE, NOT_APPLICABLE):
        return True
    return False


def _safe_call(fn: Any, what: str) -> Any:
    """استدعاء معزول — فشل حقل واحد لا يلمس بقية الحقول (§3.5)."""
    try:
        return fn()
    except Exception as e:
        logger.debug(f"[CTX] {what} failed: {type(e).__name__}")
        return None


_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def is_valid_username(value: Any) -> bool:
    """غلاف آمن حول nav_resolver.is_valid_username — فشل الاستيراد لا يكسر."""
    u = str(value or "").strip().lstrip("@")
    if not u:
        return False
    try:
        from nav_resolver import is_valid_username as _nav_valid
        return bool(_nav_valid(u))
    except Exception:
        return bool(_USERNAME_RE.match(u))


def is_valid_tme_url_safe(url: Any) -> bool:
    """غلاف آمن حول nav_resolver.is_valid_tme_url — فشل الاستيراد لا يكسر."""
    try:
        from nav_resolver import is_valid_tme_url
        return bool(is_valid_tme_url(url))
    except Exception:
        u = str(url or "")
        return u.startswith("https://t.me/")


def _ctx_val(section: Any, key: str) -> Any:
    """قراءة حقل من قسم السياق — UNAVAILABLE/NOT_APPLICABLE تعني None."""
    try:
        v = (section or {}).get(key)
    except Exception:
        return None
    if _is_missing(v):
        return None
    return v


def _ctx_sid(sender: Any) -> int:
    """معرّف مستخدم صالح فقط (>0) — 0/None/سالب ليست هوية (قاعدة 3.5)."""
    v = _ctx_val(sender or {}, "user_id")
    try:
        sid = int(v or 0)
    except Exception:
        return 0
    return sid if sid > 0 else 0


def _sender_type_from_obj(obj: Any) -> str:
    """نوع المرسل من كيان Telethon — بلا استنتاج من نصوص."""
    if obj is None:
        return "none"
    try:
        from telethon.tl.types import User as TgUser, Channel as TgChannel
    except Exception:
        TgUser = TgChannel = None  # نوع بالأسماء (mocks/اختبارات)
    name = type(obj).__name__
    if TgUser is not None and isinstance(obj, TgUser):
        if getattr(obj, "bot", False):
            return "bot"
        return "user"
    if TgChannel is not None and isinstance(obj, TgChannel):
        return "channel"
    if "User" in name:
        if getattr(obj, "bot", False):
            return "bot"
        return "user"
    if "Channel" in name:
        return "channel"
    if "Deleted" in name:
        return "deleted"
    if not hasattr(obj, "first_name") and hasattr(obj, "title"):
        return "channel"
    return "other"


def _media_type_of(media_obj: Any) -> Optional[str]:
    """نوع الوسائط — telethon types أولاً ثم استنشاق اسم الصنف (mocks)."""
    if media_obj is None:
        return None
    try:
        from telethon.tl.types import (
            MessageMediaPhoto,
            MessageMediaDocument,
            MessageMediaWebPage,
        )
        if isinstance(media_obj, MessageMediaPhoto):
            return "photo"
        if isinstance(media_obj, MessageMediaWebPage):
            return "webpage"
        if isinstance(media_obj, MessageMediaDocument):
            names = set()
            doc = getattr(media_obj, "document", None)
            for a in (getattr(doc, "attributes", None) or []):
                names.add(type(a).__name__)
            if any(("Video" in n) or ("Gif" in n) for n in names):
                return "video"
            if any(("Audio" in n) or ("Voice" in n) for n in names):
                return "audio"
            if any("Sticker" in n for n in names):
                return "sticker"
            return "document"
    except Exception:
        pass
    name = type(media_obj).__name__
    if "Photo" in name:
        return "photo"
    if "WebPage" in name:
        return "webpage"
    if "Document" in name:
        return "document"
    return "other"


# ══ الجامع المركزي (§3.2) ══════════════════════════════════════════════
async def collect_message_context(
    event: Any,
    *,
    analysis: Optional[Dict[str, Any]] = None,
    account_name: str = "",
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """اجمع سياق الرسالة الموحد — مرة واحدة، بلا إعادة استخراج لاحق.

    يقبل:
      * dict — بيانات _event_to_dict كما هي (مسار monitors الطبيعي).
      * حدث Telethon حي (NewMessage.Event أو ما يشابهه) — قراءة سمات
        آمنة + محاولة get_sender واحدة بمهلة 1.5s عند الغياب.

    لا يرسل تنبيهاً ولا يبني أزراراً ولا يقرر حظراً — جمع فقط (§3.2).
    لا يرفع استثناءً أبداً: الفشل يعيد سياقاً مصغّراً صادقاً (§9).
    """
    ctx: Dict[str, Any] = {
        "sender": {}, "chat": {}, "message": {},
        "classification": {}, "missing": [],
        "meta": {
            "account_name": str(account_name or ""),
            "collector_version": _COLLECTOR_VERSION,
            "collected_at": time.time(),
            "identity_rule": "user_id=0/None ليست هوية (v11.7.1)",
        },
    }
    try:
        if isinstance(event, dict):
            ev_data: Dict[str, Any] = dict(event)
            live_event: Any = None
        else:
            ev_data = {}
            live_event = event
        if overrides:
            try:
                ev_data.update(dict(overrides))
            except Exception as e:
                logger.debug(f"[CTX] overrides merge failed: {type(e).__name__}")

        msg: Any = None
        sender_obj: Any = None
        chat_obj: Any = None
        if live_event is not None:
            msg = _safe_call(lambda: getattr(live_event, "message", None),
                             "event.message") or live_event
            sender_obj = _safe_call(lambda: getattr(live_event, "sender", None),
                                    "event.sender")
            chat_obj = _safe_call(lambda: getattr(live_event, "chat", None),
                                  "event.chat")
            _raw_sid = _safe_call(
                lambda: getattr(live_event, "sender_id", None), "event.sender_id")
            if sender_obj is None and _raw_sid:
                try:
                    sender_obj = await asyncio.wait_for(
                        live_event.get_sender(), timeout=_LIVE_SENDER_TIMEOUT)
                except Exception as e:
                    logger.debug(f"[CTX] event.get_sender failed: {type(e).__name__}")
                    sender_obj = None
            # دمج الحقول المتاحة من الحدث الحي في ev_data (مصدر واحد للبناء)
            if _raw_sid is not None:
                ev_data.setdefault("sender_id", _raw_sid)
            _mid = _safe_call(lambda: getattr(msg, "id", None), "msg.id")
            if _mid is not None:
                ev_data.setdefault("message_id", _mid)
            _txt = _safe_call(lambda: getattr(msg, "text", None), "msg.text")
            _cap = _safe_call(lambda: getattr(msg, "message", None), "msg.message")
            _full = (_txt if _txt is not None else (_cap or ""))
            if _full:
                ev_data.setdefault("text", _full)
            _mt = _safe_call(lambda: _media_type_of(getattr(msg, "media", None)),
                             "msg.media")
            if _mt is not None:
                ev_data.setdefault("media_type", _mt)
            _cu = _safe_call(lambda: getattr(chat_obj, "username", None), "chat.username")
            if _cu:
                ev_data.setdefault("chat_username", _cu)
            _ct = _safe_call(lambda: getattr(chat_obj, "title", None), "chat.title")
            if _ct:
                ev_data.setdefault("chat_title", _ct)
            _rt = _safe_call(lambda: getattr(msg, "reply_to", None), "msg.reply_to")
            _rid = _safe_call(
                lambda: (getattr(_rt, "reply_to_msg_id", None)
                         or getattr(_rt, "reply_to_top_id", None)), "reply id")
            if _rid:
                ev_data.setdefault("reply_to_message_id", int(_rid))
            _topic = None
            if _rt is not None and _safe_call(
                    lambda: bool(getattr(_rt, "forum_topic", False)), "forum_topic"):
                _topic = _rid or None
            if _topic:
                ev_data.setdefault("topic_id", int(_topic))
            _dt = _safe_call(lambda: getattr(msg, "date", None), "msg.date")
            if _dt is not None:
                try:
                    ev_data.setdefault("message_date_iso", _dt.isoformat())
                except Exception:
                    pass

        missing = ctx["missing"]
        _reg = missing.append  # تسجيل كل حقل غير متاح

        # ── 1) المرسل (§3.3-A) ────────────────────────────────────────
        sender = ctx["sender"]
        raw_sid = ev_data.get("sender_id")
        try:
            sid = int(raw_sid) if raw_sid is not None else None
        except Exception:
            sid = None
        if sid is not None and sid > 0:
            sender["user_id"] = sid
            sender["identity_available"] = True
        else:
            # قاعدة الدقة: 0/None/سالب ليست هوية — لا قيمة افتراضية
            sender["user_id"] = UNAVAILABLE
            sender["identity_available"] = False
            _reg("user_id")

        stype = str(ev_data.get("sender_type") or "").strip().lower()
        if not stype and sender_obj is not None:
            stype = _sender_type_from_obj(sender_obj)
        sender["sender_type"] = stype or "none"

        _fn = ev_data.get("sender_first_name")
        if _fn:
            sender["first_name"] = str(_fn)
        else:
            sender["first_name"] = (
                _safe_call(lambda: getattr(sender_obj, "first_name", None),
                           "sender.first_name") or UNAVAILABLE)
            if _is_missing(sender["first_name"]):
                _reg("first_name")

        _ln = ev_data.get("sender_last_name")
        if _ln:
            sender["last_name"] = str(_ln)
        else:
            sender["last_name"] = (
                _safe_call(lambda: getattr(sender_obj, "last_name", None),
                           "sender.last_name") or UNAVAILABLE)
            if _is_missing(sender["last_name"]):
                _reg("last_name")

        full = f"{_ctx_val(sender, 'first_name') or ''} {_ctx_val(sender, 'last_name') or ''}".strip()
        sender["display_name"] = full or UNAVAILABLE
        if not full:
            _reg("display_name")

        _u = ev_data.get("sender_username")
        if not _u and sender_obj is not None:
            _u = _safe_call(lambda: getattr(sender_obj, "username", None),
                            "sender.username")
        _u = str(_u or "").strip().lstrip("@") or None
        sender["username"] = _u if _u else UNAVAILABLE
        if not _u:
            _reg("username")

        _us = ev_data.get("sender_usernames")
        _us_out: Optional[List[str]] = None
        try:
            _ul: List[str] = []
            for x in (_us or []):
                _xn = str(x if isinstance(x, str) else getattr(x, "username", "")).lstrip("@")
                if _xn and _xn not in _ul:
                    _ul.append(_xn)
            _us_out = _ul or None
        except Exception as e:
            # عزل الاستثناء: فشل حقل usernames لا يُسقط بقية الأقسام (§3.5)
            logger.debug(f"[CTX] sender_usernames extract failed: {type(e).__name__}")
            _us_out = None
        sender["usernames"] = _us_out or NOT_APPLICABLE

        if _u and is_valid_username(_u):
            sender["profile_link"] = f"https://t.me/{_u}"
        else:
            sender["profile_link"] = UNAVAILABLE
            _reg("profile_link")

        _bot = ev_data.get("sender_is_bot")
        if _bot is None and sender_obj is not None:
            _bot = _safe_call(lambda: getattr(sender_obj, "bot", None), "sender.bot")
        sender["is_bot"] = bool(_bot) if _bot is not None else False

        # مصدر الهوية — توثيق صادق لمكان المعلومات
        if (sid or 0) > 0:
            if ev_data.get("sender_access_hash") is not None or sender_obj is not None:
                sender["identity_source"] = (
                    "event_entity" if sender_obj is not None else "event_data")
            else:
                sender["identity_source"] = "id_only"
        else:
            sender["identity_source"] = NOT_APPLICABLE

        # عضوية المجموعة/الاسم اللغوي: تُجمع فقط إن كانت متاحة محليًا
        # (بلا طلب شبكة) — حقل لهذا الإصدار: غير مطبق = NOT_APPLICABLE.
        sender["group_membership"] = NOT_APPLICABLE
        # v11.7.1 (تصحيح صدق): لا مصدر في المشروع يوفر لغة حساب المرسل،
        # ولا تُستنتج من لغة الرسالة (قاعدة 3.5) — القيمة الصادقة إذن
        # NOT_APPLICABLE. اسم حساب المراقبة في ctx["meta"]["account_name"]
        # ولا يُحشى في حقل لغة (كان يُعرض كأنه لغة — تضليل للمستهلك).
        sender["account_language"] = (
            str(ev_data.get("account_language")) if ev_data.get("account_language")
            else NOT_APPLICABLE
        )
        # لغة الحساب: لا تُستنتج من لغة الرسالة (قاعدة 5) — الاسم في meta فقط.

        # ── 2) المحادثة (§3.3-B) ─────────────────────────────────────
        chat = ctx["chat"]
        raw_cid = None
        if live_event is not None:
            raw_cid = _safe_call(lambda: getattr(live_event, "chat_id", None),
                                 "event.chat_id")
        if raw_cid is None and msg is not None:
            raw_cid = _safe_call(lambda: getattr(msg, "chat_id", None), "msg.chat_id")
        if raw_cid is None:
            raw_cid = ev_data.get("chat_id")
        try:
            cid = int(raw_cid) if raw_cid is not None else None
        except Exception:
            cid = None
        chat["chat_id"] = cid if cid is not None else UNAVAILABLE
        if cid is None:
            _reg("chat_id")

        _title = ev_data.get("chat_title")
        chat["chat_title"] = str(_title) if _title else UNAVAILABLE
        if not _title:
            _reg("chat_title")

        _cu = ev_data.get("chat_username")
        _cu = str(_cu or "").strip().lstrip("@") or None
        chat["chat_username"] = _cu if _cu else UNAVAILABLE
        if not _cu:
            _reg("chat_username")

        _ctype = ev_data.get("chat_type")
        if not _ctype and chat_obj is not None:
            _ctype = _safe_call(
                lambda: ("channel" if _sender_type_from_obj(chat_obj) == "channel"
                         else "chat"), "chat.type")
        chat["chat_type"] = str(_ctype) if _ctype else UNAVAILABLE
        if not _ctype:
            _reg("chat_type")

        if _cu and is_valid_username(_cu):
            chat["chat_link"] = f"https://t.me/{_cu}"
        else:
            chat["chat_link"] = UNAVAILABLE
            _reg("chat_link")

        _forum = _safe_call(lambda: bool(getattr(chat_obj, "forum", False)),
                            "chat.forum")
        chat["is_forum"] = bool(_forum)

        _topic = ev_data.get("topic_id")
        try:
            _topic = int(_topic) if _topic is not None else None
        except Exception:
            _topic = None
        chat["topic_id"] = _topic if _topic is not None else UNAVAILABLE
        if _topic is None:
            _reg("topic_id")

        # ── 3) الرسالة (§3.3-C) ──────────────────────────────────────
        message = ctx["message"]
        raw_mid = ev_data.get("message_id")
        try:
            mid = int(raw_mid) if raw_mid is not None else None
        except Exception:
            mid = None
        message["message_id"] = mid if mid is not None else UNAVAILABLE
        if mid is None:
            _reg("message_id")

        _d_iso = ev_data.get("message_date_iso")
        if not _d_iso and ev_data.get("msg_date_ts") is not None:
            try:
                import datetime as _dtm
                _d_iso = _dtm.datetime.fromtimestamp(
                    float(ev_data["msg_date_ts"])).isoformat()
            except Exception:
                _d_iso = None
        message["message_date"] = _d_iso if _d_iso else UNAVAILABLE
        if not _d_iso:
            _reg("message_date")

        _txt = ev_data.get("text")
        message["message_text"] = str(_txt) if _txt else UNAVAILABLE
        if not _txt:
            _reg("message_text")

        _mt = ev_data.get("media_type")
        message["media_type"] = str(_mt) if _mt else UNAVAILABLE
        if not _mt:
            _reg("media_type")

        _mt_val = _ctx_val(message, "media_type")
        if _mt_val and not _ctx_val(message, "message_text"):
            # وسائط بلا نص: التسمية التوضيحية غير منفصلة في بيانات الحدث
            message["caption"] = UNAVAILABLE
            _reg("caption")
        elif not _mt_val:
            message["caption"] = NOT_APPLICABLE  # نص خالص — لا caption
        else:
            message["caption"] = UNAVAILABLE
            _reg("caption")

        _rid = ev_data.get("reply_to_message_id")
        try:
            _rid = int(_rid) if _rid is not None else None
        except Exception:
            _rid = None
        message["reply_to_message_id"] = _rid if _rid is not None else UNAVAILABLE
        if _rid is None:
            _reg("reply_to_message_id")

        message["message_link"] = UNAVAILABLE  # يُحسم من بناء الوجهة (§4.2)
        _fwd = ev_data.get("forward_origin")
        if not _fwd and msg is not None:
            _fwd_src = _safe_call(lambda: getattr(msg, "fwd_from", None), "fwd")
            if _fwd_src is not None:
                _otype = type(_fwd_src).__name__
                _ftype = ("user" if "User" in _otype
                          else "channel" if "Channel" in _otype
                          else "hidden" if "Hidden" in _otype else "other")
                _fwd = {"type": _ftype}
        message["forward_origin"] = _fwd if _fwd else NOT_APPLICABLE

        # ── 4) التصنيف (§3.3-D) — لا حساب درجات هنا إطلاقاً ─────────
        cls = ctx["classification"]
        if isinstance(analysis, dict) and analysis:
            _mk = (analysis.get("matched_keywords")
                   or analysis.get("keywords") or NOT_APPLICABLE)
            _ms = (analysis.get("match_score")
                   if analysis.get("match_score") is not None
                   else analysis.get("confidence"))
            _fr = (analysis.get("filter_result")
                   or analysis.get("classification") or NOT_APPLICABLE)
            cls["matched_keywords"] = _mk
            cls["match_score"] = _ms if _ms is not None else NOT_APPLICABLE
            cls["filter_result"] = _fr
        else:
            cls["matched_keywords"] = NOT_APPLICABLE
            cls["match_score"] = NOT_APPLICABLE
            cls["filter_result"] = NOT_APPLICABLE

        return ctx
    except Exception as e:
        # الجامع نقطة فشل واحدة لا يكون أبداً (§9) — سياق مصغر صادق
        logger.warning(f"[CTX] collector degraded: {type(e).__name__}")
        ctx.setdefault("sender", {})
        ctx.setdefault("chat", {})
        ctx.setdefault("message", {})
        ctx.setdefault("classification", {})
        ctx.setdefault("missing", ["collector_degraded"])
        ctx.setdefault("meta", {"collector_version": _COLLECTOR_VERSION,
                                "degraded": True})
        ctx["meta"]["degraded"] = True
        ctx["meta"]["degraded_reason"] = type(e).__name__
        return ctx


# ══ دوال اختيار الوجهة (§4) ════════════════════════════════════════════
def build_sender_link(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """رابط اسم المرسل — الأولويات (بروتوكول المهلة 4.1):

        1. username صالح            → https://t.me/<u>       kind=username
        2. user_id>0 + مستخدم/بوت
           + tg://user غير محجوب    → tg://user?id=<uid>     kind=tg_user
                                     (بتوثيق القيد: قد يرفضه Bot API
                                      BUTTON_USER_INVALID — القرار
                                      يعتمد على الذاكرة والتحقق بعد الإرسال)
        3. رابط الرسالة المصدر      → kind=message_source
                                     (أقصى ما تسمح به تيليجرام لمن لا
                                      username — فتح الملف باللمس على الصورة)
        4. لا شيء موثوق            → kind=none (الاسم نص عادي)

    القناة/المجهول/بلا هوية: لا رابط مستخدم من معرّف الكيان (قاعدة 4.1.5)
    — ينتقل مباشرة للمصدر/النص العادي. يعيد الواجهة نفسها التي يعيد
    resolve_contact_target اختيارها عبر kind.
    """
    out = {"url": None, "kind": "none", "reason_if_none": ""}
    try:
        sender = ctx.get("sender") or {}
        message = ctx.get("message") or {}
        sid = _ctx_sid(sender)
        uname = _ctx_val(sender, "username")
        stype = str(_ctx_val(sender, "sender_type") or "").strip().lower()

        # (1) username صالح — يعمل للجميع
        if uname and is_valid_username(uname):
            out["url"] = f"https://t.me/{uname}"
            out["kind"] = "username"
            return out

        # (5) قناة/مجموعة/محذوف: معرّف الكيان ليس معرّف مستخدم
        if stype in ("channel", "deleted"):
            out["reason_if_none"] = f"sender_type_{stype}_not_a_user"
            return out

        # (2) tg://user?id — بلا ضمان عالمي؛ الذاكرة تحسم ما عُرف برفضه
        if sid > 0 and stype in ("user", "bot", "assumed_user", "", "none"):
            if not is_tg_user_blocked(sid):
                out["url"] = f"tg://user?id={sid}"
                out["kind"] = "tg_user"
                out["reason_if_none"] = ""
                return out
            out["reason_if_none"] = "tg_user_blocked_memory"

        # (3) رابط الرسالة المصدر — الأفضل الموثوق المتبقي
        ml = _ctx_val(message, "message_link")
        if ml and is_valid_tme_url_safe(ml):
            out["url"] = ml
            out["kind"] = "message_source"
            out["reason_if_none"] = ""
            return out

        if not sid:
            out["reason_if_none"] = "no_sender_identity"
        elif not ml:
            out["reason_if_none"] = out["reason_if_none"] or "no_message_link"
        return out
    except Exception as e:
        out["url"] = None
        out["kind"] = "none"
        out["reason_if_none"] = f"error:{type(e).__name__}"
        return out


def _wrap_alert_build_message_link(data_style: Dict[str, Any]) -> Optional[str]:
    """غلاف استدعاء واحد على alert_bot.build_message_link (المصدر الوحيد).
    استيراد مؤجل لكسر دورة الاستيراد (alert_bot يستورد هذه الوحدة)."""
    try:
        from alert_bot import build_message_link as _bml
        return _bml(data_style)
    except Exception as e:
        logger.debug(f"[CTX] build_message_link wrap failed: {type(e).__name__}")
        return None


def build_message_link_result(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """رابط الرسالة — الأولويات (بروتوكول المهلة 4.2) عبر **غلاف** على
    alert_bot.build_message_link الموجودة (المصدر الوحيد — لا منطق موازٍ):

        1. message_link المؤكد (من المحلّل/الحدث)          kind=confirmed
        2. مجموعة عامة (username + message_id) → t.me/u/id  kind=public
        3. مجموعة خاصة -100 → t.me/c/<inner>/<id>           kind=private
           (يعمل لأعضائها فقط — لا يُقدَّم كرابط عام)
        4. موضوع: t.me/u/<topic>/<id> أو t.me/c/…/<topic>/<id> عند توفر topic_id
        5. forward_origin معلومة فقط — لا يستبدل الرابط الأصلي
        6. تعذر الرابط: أعد السبب — لا رابط غير موثوق
    """
    out = {"url": None, "kind": "none", "reason_if_none": ""}
    try:
        message = ctx.get("message") or {}
        chat = ctx.get("chat") or {}

        # (1) رابط مؤكد مُمرر من المحلّل
        confirmed = _ctx_val(message, "message_link")
        if confirmed and is_valid_tme_url_safe(confirmed):
            out["url"] = confirmed
            out["kind"] = "confirmed"
            return out

        # اتساق المعرفات قبل الاعتماد على أي بناء (قاعدة 4.2.7)
        mid = _ctx_val(message, "message_id")
        cid = _ctx_val(chat, "chat_id")
        topic = _ctx_val(chat, "topic_id")
        try:
            mid_i = int(mid) if mid is not None else 0
        except Exception:
            mid_i = 0
        if mid_i <= 0:
            out["reason_if_none"] = "no_message_id"
            return out

        uname = _ctx_val(chat, "chat_username")
        # (2)+(4) عام — والبناء المركزي يضيف <topic> تلقائياً عند توفره
        if uname and is_valid_username(uname):
            url = _wrap_alert_build_message_link(
                {"message_id": mid_i, "topic_id": topic,
                 "chat_username": uname, "chat_id": cid})
            if url:
                out["url"] = url
                out["kind"] = "public" if not topic else "public_topic"
                return out

        # (3)+(4) خاص -100 — المجموعات العادية والأرقام غير القياسية
        # تعيد None من الحارس المركزي (لا روابط ميتة)
        url = _wrap_alert_build_message_link(
            {"message_id": mid_i, "topic_id": topic,
             "chat_username": None, "chat_id": cid})
        if url:
            out["url"] = url
            out["kind"] = "private" if not topic else "private_topic"
            out["reason_if_none"] = ""
            return out

        out["reason_if_none"] = "no_public_username_and_not_supergroup"
        return out
    except Exception as e:
        out["url"] = None
        out["kind"] = "none"
        out["reason_if_none"] = f"error:{type(e).__name__}"
        return out


def build_contact_action(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """وسيلة المراسلة — الأولويات (بروتوكول المهلة 4.3) — الغلاف الوحيد
    فوق resolve_contact_target (المصدر المركزي لاختيار هدف المرسل):

        1. username صالح → t.me/<u>                        kind=username
        2. مستخدم/بوت بمعرّف + tg://user غير محجوب → tg://user?id  kind=tg_user
           (البوت يحصل على الزر مثل المستخدم — سلوك المشروع القائم
            محفوظ: contact_target._USER_ID_CAPABLE_TYPES يتضمن bot)
        3. قناة/مجموعة/محذوف/بلا هوية → لا زر              kind=none
        4. تدفق وسيط عبر البوت (callback/relay): غير منفَّذ في المشروع —
           لا يُستخدم إلا بعد التحقق من وجوده فعلاً (موثق كقيد هنا)
        5. تعذر التواصل → kind=none + سبب داخلي موجز — لا زر معطل
    """
    out = {"url": None, "kind": "none", "reason_if_none": ""}
    try:
        sender = ctx.get("sender") or {}
        sid = _ctx_sid(sender)
        uname = _ctx_val(sender, "username")
        stype = str(_ctx_val(sender, "sender_type") or "").strip().lower()

        if uname and is_valid_username(uname):
            out["url"] = f"https://t.me/{uname}"
            out["kind"] = "username"
            return out

        if stype in ("channel", "deleted"):
            out["reason_if_none"] = f"sender_type_{stype}_not_contactable"
            return out

        if sid <= 0:
            out["reason_if_none"] = "no_sender_identity"
            return out

        # resolve_contact_target هو الحكم — يعيد نفس selected_url الذي
        # يستخدمه الاسم (spec #16 للمشروع)؛ هنا نحترم ذاكرة الحظر فوقه.
        try:
            from contact_target import resolve_contact_target as _rct
            from contact_target import ContactTarget as _CT
        except Exception:
            return {"url": None, "kind": "none",
                    "reason_if_none": "contact_target_unavailable"}
        target: Any = _rct(
            sender_id=sid,
            username=None,  # username فُحص أعلاه — هنا المسار بلا username
            sender_type=stype,
            is_bot=sender.get("is_bot") if not _is_missing(sender.get("is_bot")) else None,
        )
        if target.user_id_url and not is_tg_user_blocked(sid):
            out["url"] = target.user_id_url
            out["kind"] = "tg_user"
            return out
        if target.user_id_url and is_tg_user_blocked(sid):
            out["reason_if_none"] = "tg_user_blocked_memory"
            return out

        out["reason_if_none"] = "relay_flow_not_implemented_no_direct_channel"
        return out
    except Exception as e:
        out["url"] = None
        out["kind"] = "none"
        out["reason_if_none"] = f"error:{type(e).__name__}"
        return out


def context_from_contact_target(target: Optional[Any]) -> Dict[str, Any]:
    """حوّل ContactTarget جاهزاً إلى نتيجة وجهة موحدة (للتوافق مع المسار
    الذي حلّ المرسل عبر monitors/nav_resolver قبل تكوين السياق)."""
    if target is None:
        return {"url": None, "kind": "none", "reason_if_none": "no_target"}
    url = target.selected_url
    kind = {"username": "username", "user_id": "tg_user",
            "openmessage": "openmessage"}.get(target.target_type, "none")
    return {"url": url, "kind": kind if url else "none",
            "reason_if_none": "" if url else "no_reachable_target"}
