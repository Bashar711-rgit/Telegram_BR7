#!/usr/bin/env python3
"""
alert_bot.py - v11.2 Alert Bot - نظام مراسلة المرسل + واجهة التنبيهات الموحدة.

v11.2 (طلب المستخدم — نظام مراسلة المرسل): ContactTarget هو المصدر الوحيد
لاختيار رابط المرسل (username ← user_id ← لا شيء) — الاسم والزر يشتركان في
نفس selected_url، وزر «مراسلة» يعمل حتى بلا username عبر tg://user?id،
والقنوات/المحذوفون لا تُخترق لهم وسيلة وصول، وطبقة الإرسال الاحتياطية
(MTProto) تمرر الكيانات جاهزة عبر formatting_entities فيبقى الاسم قابلًا
للنقر بعد أن كان تيليثون يحذف tg://user?id صامتاً عند الـparse.

القالب (مواصفة المستخدم النهائية):

    👤: {اسم المرسل قابل للنقر}
    (سطر فارغ)
    <b>💬:</b>
    <blockquote>{النص الأصلي كاملاً - بلا حذف أجزاء}</blockquote>

    [ مراسلة ] [ عرض الرسالة ]

  * النسخ بالضغط على النص: النص داخل <blockquote> - في عملاء تيليجرام
    الرسمية الضغط على منطقة quote يفتح قائمة فيها «Copy Text» (نسخ فعلي
    للحافظة). هذه هي الآلية الرسمية الوحيدة لـ«الضغط على النص ليُنسخ» -
    لا توجد أي API تجعل النص الحر يُنسخ بلمسة واحدة مباشرة.
  * البطاقة: عنوان «💬 الرسالة:» أعلى ومحتوى الرسالة الأصلي كاملاً أسفله
    داخل <blockquote>، مع دعم العربية/RTL والحفاظ على الأسطر والفقرات.
  * النص الأصلي يُحفظ كاملاً (لا truncate(400)) - التقصير الوحيد المسموح
    هو حاجز حد تيليجرام 4096 حرفاً للرسالة كلها (مع "…" وتحذير في اللوج).
  * SENDER: username -> <a href="https://t.me/U">@U</a>؛ بدونه
    <a href="tg://user?id=ID">الاسم الكامل</a>.

الأزرار - Inline Keyboard في صف واحد (الدالة المركزية build_alert_buttons -
لا يُبنى أي زر يدوياً في أي مكان آخر):

    [ مراسلة ] [ عرض الرسالة ]

  * مراسلة: فتح محادثة المستخدم فعلياً - username -> https://t.me/U؛
    بدونه أفضل رابط مباشر متاح (tg://user?id=ID). يُحذف الزر إن لم يوجد
    أي منهما.
  * عرض الرسالة: رابط الرسالة الحقيقي (t.me/{u}/{id} للعام،
    t.me/c/{inner}/{id} للخاص - ومع topics t.me/{u}/{topic}/{id})؛ إن
    تعذر رابط مباشر -> أفضل آلية متاحة (رابط القروب) بدل رابط وهمي؛
    بلا أي رابط -> يُحذف الزر.
  * v11.1: حُذف زرا «نسخ» و«القروب» من الصف حسب المواصفة الجديدة -
    النسخ بالضغط على نص الرسالة (quote -> Copy Text).

طريقة الإرسال:
  POST https://api.telegram.org/bot{TOKEN}/sendMessage
  json: chat_id=TARGET_GROUP_ID, text, parse_mode="HTML",
        link_preview_options={"is_disabled": true},
        reply_markup={"inline_keyboard": [[صف الزرين]]}

معالجة الأخطاء:
  * 429 -> انتظار retry_after ثم إعادة المحاولة (بحد أقصى 3).
  * BUTTON_USER_INVALID أو BUTTON_USER_PRIVACY_RESTRICTED -> إعادة
    الإرسال بدون زر «مراسلة» فقط (يبقى رابط الاسم في النص وتبقى بقية
    الأزرار) - الرسالة لا تضيع.
  * v11.0: أخطاء أزرار عامة (BUTTON_URL/TYPE/DATA/TEXT_INVALID،
    REPLY_MARKUP_INVALID) -> إعادة الإرسال بدون أزرار إطلاقاً - النص
    يبقى بروابطه القابلة للنقر.
  * أي فشل آخر أو عدم وجود ALERT_BOT_TOKEN -> العودة لمسار الإرسال من
    حساب المستخدم (fallback في monitors.py) بنفس القالب الموحد بدون
    أزرار مع تسجيل السبب في اللوج.

المراقبون (حسابات المستخدمين) يستمرون في الاستماع وبناء البيانات فقط -
الإرسال كله يمر عبر AlertBot.send(data, analysis) من _send_alert، مع
الحفاظ على مرور الرسالة عبر rate_limiter والـ circuit breaker.

شروط بيئية: البوت يجب أن يكون عضواً (يفضل أدمن) في TARGET_GROUP_ID -
check_membership() (getChatMember) تسجل تحذيراً واضحاً عند الإقلاع إن
لم يكن عضواً.

تسجيل النتيجة: alerts.contact_method = username | mention_button |
text_only (عمود موجودة منذ v10.7 - قيم بنفس العقد <=20 حرفاً).
v11.1: تُسجل أيضاً حالة الإرسال وروابط الأزرار في أعمدة alerts الجديدة
(alert_status/alert_sent_at/alerted_to/notification_attempts/
notification_error/button_message_url/button_user_url).

حفاظاً على الميزات: سطر «🏷 قاعدة: …» (محرك القواعد) يُلاحق في نهاية
التنبيه عند وجوده فقط.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from loguru import logger

from config import InputSanitizer
# v11.2: نظام مراسلة المرسل — المصدر الوحيد لاختيار رابط المرسل (spec #8).
# لا يُبنى رابط مرسل يدوياً في هذا الملف ولا في أي ملف آخر — ContactTarget
# يقرر (username ← user_id ← لا شيء) ويستخدم الاسم والزر نفس selected_url.
from contact_target import (  # noqa: E402
    ContactTarget,
    SENDER_LABEL,
    build_alert_entity_pack,
    metric_inc as _contact_metric,
    resolve_contact_target as _resolve_contact_target,
)

BOT_API_BASE = "https://api.telegram.org"
# حد تيليجرام الأقصى لنص الرسالة — الحاجز الوحيد المسموح لتقصير المحتوى
TELEGRAM_MAX_TEXT_LEN = 4096

# ══ القالب الموحد — نصوص ثابتة (لا تُغيَّر: عقد واجهة) ══
# v11.2 (مواصفة المستخدم): «👤: الاسم» و«💬:» — الاسم نفسه رابط داخلي،
# والنسخ بالضغط على نص الرسالة (blockquote → Copy Text).
CARD_TITLE_HTML = "<b>💬:</b>"
MSG_LINK_UNAVAILABLE = "غير متاح"  # محتفظ به للتوافق الخلفي (لم يعد في النص)

# نصوص الزرين (v11.1) — صف واحد: [ مراسلة ] [ عرض الرسالة ]
BTN_CONTACT = "مراسلة"
BTN_VIEW = "عرض الرسالة"

# أخطاء زر tg://user — يُعاد الإرسال بدون زر «مراسلة» فقط (بقية الأزرار
# تبقى، ورابط الاسم يبقى في النص).
BUTTON_USER_ERRORS = ("BUTTON_USER_INVALID", "BUTTON_USER_PRIVACY_RESTRICTED")

# v11.0: أخطاء أزرار عامة أخرى — يُعاد الإرسال **بدون أزرار** (نص التنبيه
# يبقى بروابطه القابلة للنقر في الترويسة) بدل إسقاط التنبيه كله على
# المسار الاحتياطي. الهدف: التنبيه لا يضيع أبداً بسبب زر.
GENERIC_BUTTON_ERRORS = (
    "BUTTON_URL_INVALID",
    "BUTTON_DATA_INVALID",
    "BUTTON_TYPE_INVALID",
    "BUTTON_TEXT_INVALID",
    "REPLY_MARKUP_INVALID",
)

# أنواع المرسل التي لا يُبنى لها زر tg://user?id إطلاقاً (spec #18 —
# لا اختلاق معلومات: قناة/محذوف/بلا هوية ليسوا مستخدمين reachable).
# ملاحظة: sender_type فاضي أو "none" مع sender_id يصبح assumed_user في
# resolve_contact_target — فزر tg://user?id يبقى متاحاً للمسار التاريخي.
_NO_USER_BUTTON_TYPES = ("channel", "deleted")

_TG_URL = "https://t.me/"
_PARTIAL_ENTITY_RE = re.compile(r"&[a-zA-Z#0-9]{0,10}$")


def _clean_username(value: Any) -> str:
    """@User / user / None → 'user' (نظيف، بلا @)."""
    try:
        return str(value or "").strip().lstrip("@")
    except Exception:
        return ""


def _tme_inner_id(chat_id: Any) -> Optional[str]:
    """الرقم الداخلي لمجموعة خاصة -100xxx (نفس حارس v10.5 — المجموعات
    العادية والأرقام غير القياسية تعيد None ولا تُبنى روابط ميتة)."""
    s = str(chat_id or "")
    if not s.startswith("-100"):
        return None
    inner = s[4:]
    return inner if inner.isdigit() else None


def build_message_link(data: Dict[str, Any], chat_username: Optional[str] = None) -> Optional[str]:
    """رابط الرسالة الحقيقي: عام → t.me/{u}/{id}؛ خاص -100 → t.me/c/{inner}/{id}.

    v11.0: مجموعات المنتديات (topics) → t.me/{u}/{topic}/{id} و
    t.me/c/{inner}/{topic}/{id} — topic_id يُقرأ من data (يلتقطه
    _event_to_dict) فيفتح الرابط داخل الموضوع الصحيح فعلياً."""
    message_id = data.get("message_id")
    if not message_id:
        return None
    topic = data.get("topic_id")
    mid = f"{topic}/{message_id}" if topic else f"{message_id}"
    uname = _clean_username(chat_username) or _clean_username(data.get("chat_username"))
    if uname:
        return f"{_TG_URL}{uname}/{mid}"
    inner = _tme_inner_id(data.get("chat_id"))
    if inner:
        return f"{_TG_URL}c/{inner}/{mid}"
    return None


def build_group_link(data: Dict[str, Any], chat_username: Optional[str] = None,
                     invite_link: Optional[str] = None) -> Optional[str]:
    """رابط المجموعة: رابط دعوة متاح → t.me/{u} → t.me/c/{inner} — لا روابط وهمية."""
    il = str(invite_link or "").strip()
    if il and il != "#":
        return il
    uname = _clean_username(chat_username) or _clean_username(data.get("chat_username"))
    if uname:
        return f"{_TG_URL}{uname}"
    inner = _tme_inner_id(data.get("chat_id"))
    if inner:
        return f"{_TG_URL}c/{inner}"
    return None


def build_alert_buttons(
    sender_id: Any = 0,
    sender_username: Any = None,
    sender_name: str = "",
    msg_link: Optional[str] = None,
    group_link: Optional[str] = None,
    include_user_button: bool = True,
    copy_text: Optional[str] = None,
    contact_url: Optional[str] = None,
    sender_type: Optional[str] = None,
) -> List[List[Dict[str, Any]]]:
    """══ الدالة المركزية الوحيدة لبناء أزرار التنبيه (v11.2) ══

    صف واحد بترتيب RTL («مراسلة» تظهر في أقصى اليمين في عملاء تيليجرام
    العربية) — زران حسب مواصفة v11.1 + مصدر موحد v11.2:

        [ مراسلة ] [ عرض الرسالة ]

    * الزر الذي تنعدم بياناته يُحذف كلياً (لا أزرار معطلة ولا روابط وهمية).
    * v11.2 (spec #16): contact_url من ContactTarget.selected_url — الاسم
      والزر يشتركان في نفس الهدف المختار (username ← user_id). تمريره
      يتفوق على بناء الرابط من sender_id/username مباشرة.
    * v11.2 (spec #18): sender_type ∈ (channel, deleted) يمنع زر
      tg://user?id — لا اختلاق وسيلة وصول لغير المستخدمين.
    * include_user_button=False بعد رفض tg://user (خصوصية) — يُسقط زر
      «مراسلة» فقط ويبقى زر العرض.
    * copy_text: معامل محفوظ للتوافق الخلفي (v10.9.1) — مُتجاهَل منذ v11.1.
    * كل تنبيه جديد مستقبلاً يستخدم هذه الدالة تلقائياً — لا تُبنى أزرار
      يدوياً في أي مسار آخر.
    """
    row: List[Dict[str, Any]] = []
    username = _clean_username(sender_username)
    try:
        sid = int(sender_id or 0)
    except Exception:
        sid = 0

    # 1) مراسلة — فتح محادثة المستخدم مباشرة (يمين الصف في عملاء RTL)
    #    الزر لا يعتمد على username: بلا username يُستخدم tg://user?id
    #    (spec #15) أو الهدف الذي اختاره ContactTarget إن مرّر (spec #16).
    if include_user_button:
        url: Optional[str] = None
        if contact_url:
            url = str(contact_url)
        elif username:
            url = f"{_TG_URL}{username}"
        elif sid and str(sender_type or "").strip().lower() not in _NO_USER_BUTTON_TYPES:
            # أفضل رابط مباشر ممكن لمن لا يملك username
            url = f"tg://user?id={sid}"
        if url:
            row.append({"text": BTN_CONTACT, "url": url})

    # 2) عرض الرسالة — الرسالة نفسها لا الصفحة الرئيسية؛ بدون رابط مباشر
    #    نستخدم أفضل آلية متاحة (رابط القروب) بدل رابط وهمي.
    view_url = str(msg_link or "").strip()
    if view_url in ("", "#"):
        view_url = str(group_link or "").strip()
    if view_url in ("", "#"):
        view_url = ""
    if view_url:
        row.append({"text": BTN_VIEW, "url": view_url})

    return [row] if row else []


def _has_tg_user_button(built: Dict[str, Any]) -> bool:
    """هل صف الأزرار يحتوي زر «مراسلة» بنمط tg://user (القابل لرفض الخصوصية)."""
    kb = built.get("buttons") or []
    if not kb:
        return False
    return any(
        str(b.get("url") or "").startswith("tg://user?id=")
        for b in kb[0]
    )


def build_alert_html(
    data: Dict[str, Any],
    analysis: Optional[Dict[str, Any]] = None,
    *,
    msg_link: Optional[str] = None,
    group_link: Optional[str] = None,
    chat_username: Optional[str] = None,
    sender_username: Optional[str] = None,
    contact_target: Optional[ContactTarget] = None,
) -> Dict[str, Any]:
    """يبني القالب الموحد للتنبيه (HTML) + الأزرار الأربعة (صف واحد).

    يعيد dict:
      text           — نص التنبيه الكامل (parse_mode="HTML") بالبطاقة
      buttons        — Inline keyboard كامل: [[صف الأزرار الأربعة]] | []
      button_kwargs  — معاملات build_alert_buttons (لإعادة البناء بدون
                       زر «مراسلة» عند رفض tg://user)
      contact_method — username | mention_button | text_only
      msg_link       — رابط الرسالة المستخدم | None
      display        — الاسم المعروض للمرسل

    القيم الواردة من msg_link/group_link/chat_username/sender_username
    تتفوق على قيم data (هي نتيجة محلل v10.5 المُحسَّن). لا يرفع استثناء
    أبداً — فشل-آمن: بيانات ناقصة تعني أزراراً محذوفة بصدق.
    """
    try:
        sender_id = int(data.get("sender_id") or 0)
    except Exception:
        sender_id = 0

    # ── username المرسل: قيمة المحلّل أولاً ثم الحدث ثم usernames المتعددة ──
    username = _clean_username(sender_username) or _clean_username(data.get("sender_username"))
    if not username:
        for _ru in (data.get("sender_usernames") or []):
            _raw = _ru if isinstance(_ru, str) else getattr(_ru, "username", None)
            _c = _clean_username(_raw)
            if _c:
                username = _c
                break

    # ── v11.2: ContactTarget — الحل المركزي الوحيد لرابط المرسل (spec #7/#8) ──
    # الطرق الثلاث تُبنى هنا داخلياً عند عدم تمرير هدف جاهز من monitors
    # (المرسل هناك قد حلّ عبر nav resolver: حدث ← كاش الكيانات ← DB).
    # الاسم والزر سيستخدمان نفس selected_url حصراً (spec #16).
    target = contact_target
    if target is None:
        target = _resolve_contact_target(
            sender_id=sender_id,
            username=username,
            usernames=data.get("sender_usernames"),
            first_name=data.get("sender_first_name"),
            last_name=data.get("sender_last_name"),
            display=data.get("sender_display"),
            sender_type=data.get("sender_type") or "",
            is_bot=data.get("sender_is_bot"),
            is_deleted=data.get("sender_is_deleted"),
        )

    # ── الاسم المعروض (سقف 128 حرفاً — حماية حد 4096) ──
    # ── الاسم المعروض (spec #17) — الأولوية: display ← أول+أخير ←
    #    username ← fallback؛ سقف 128 حرفاً حمايةً لحد 4096 ──
    display = (
        str(data.get("sender_display") or "").strip()
        or (target.display_name or "").strip()
        or (f"مستخدم ({sender_id})" if sender_id else "مستخدم")
    )
    if len(display) > 128:
        display = display[:127] + "…"

    # ── SENDER: سطر 1 ──
    # النص الظاهر ≠ الرابط (spec #14): لا يظهر للمستخدم أي https://t.me
    # أو tg:// — الرابط يُحمل ككيان داخل الاسم فقط.
    if target.username_url:
        sender_html = f'<a href="{target.username_url}">{InputSanitizer.escape_html(display)}</a>'
        contact_method = "username"
    elif target.user_id_url:
        sender_html = f'<a href="{target.user_id_url}">{InputSanitizer.escape_html(display)}</a>'
        contact_method = "mention_button"
    else:
        sender_html = InputSanitizer.escape_html(display)
        contact_method = "text_only"

    # ── سطر «🏷 قاعدة» (ميزة محرك القواعد) — عند وجوده فقط ──
    rule_tag = (analysis or {}).get("rule_tag") if isinstance(analysis, dict) else None
    rule_part = ""
    if rule_tag:
        rule_part = f"\n\n🏷 قاعدة: {InputSanitizer.escape_html(str(rule_tag))}"

    # ── بطاقة نص الرسالة: المحتوى الأصلي كاملاً (لا حذف أجزاء) ──
    # الحاجز الوحيد: حد تيليجرام 4096 للرسالة كلها — يُقص المتجاوز فقط
    # مع "…" وتحذير لوج (لا نقسم كيان HTML نصف مكتمل).
    header = "\n".join([
        f"{SENDER_LABEL}{sender_html}",
        "",
        CARD_TITLE_HTML,
    ])
    overhead = len(header) + len("<blockquote></blockquote>") + len(rule_part) + 1 + 16
    budget = max(TELEGRAM_MAX_TEXT_LEN - overhead, 256)
    safe_text = InputSanitizer.escape_html(str(data.get("text") or ""))
    if len(safe_text) > budget:
        cut = safe_text[: budget - 1].rstrip()
        cut = _PARTIAL_ENTITY_RE.sub("", cut)
        safe_text = cut + "…"
        logger.warning(
            f"⚠️ alert text truncated to Telegram 4096 limit "
            f"(escaped_len={len(safe_text)} budget={budget})"
        )
    text = f"{header}\n<blockquote>{safe_text}</blockquote>{rule_part}"

    # ── روابط الرسالة/القروب الحقيقية (قيم المحلّل أولاً ثم البناء) ──
    link = str(msg_link or "").strip()
    if link in ("", "#"):
        link = build_message_link(data, chat_username) or ""
    glink = str(group_link or "").strip()
    if glink in ("", "#"):
        glink = build_group_link(data, chat_username) or ""

    button_kwargs: Dict[str, Any] = {
        "sender_id": sender_id,
        "sender_username": username or None,
        "sender_name": display,
        "msg_link": link or None,
        "group_link": glink or None,
        # v11.2 (spec #16): الاسم والزر من نفس ContactTarget — selected_url
        # هو الحكم الوحيد؛ sender_type يمنع اختلاق زر tg://user للقنوات.
        "contact_url": target.selected_url,
        "sender_type": target.sender_type,
    }
    buttons = build_alert_buttons(**button_kwargs)

    return {
        "text": text,
        "buttons": buttons,
        "button_kwargs": button_kwargs,
        "contact_method": contact_method,
        "msg_link": link or None,
        "display": display,
        "contact_target": target,
    }


class AlertBot:
    """مُرسِل التنبيهات عبر Telegram Bot API (aiohttp).

    * بلا توكن → enabled=False → مسار حسابات المستخدمين (fallback) يعمل
      كما هو بلا أعطال (شرط القبول رقم 6).
    * كل استدعاء send() يبني الحمولة من بيانات الرسالة نفسها عبر الدالة
      المركزية build_alert_buttons ويُرسل sendMessage بـ parse_mode="HTML"
      + link_preview_options معطّل + صف الأزرار الأربعة في صف واحد.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        chat_id: Any = None,
        timeout: float = 12.0,
        max_retries: int = 3,
    ) -> None:
        self.token = str(token or "").strip()
        try:
            self.chat_id = int(chat_id) if chat_id else 0
        except Exception:
            self.chat_id = 0
        self.timeout = float(timeout or 12.0)
        self.max_retries = max(1, int(max_retries or 3))
        self.bot_id: Optional[int] = None
        self.bot_username: Optional[str] = None
        self.last_error: Optional[str] = None
        # v11.0: عميل البوت الحي (alert_ui.AlertBotClient) — طبقة إرسال
        # احتياطية بالأزرار عند فشل Bot API. يُربط من main.initialize.
        self._client: Any = None

    # ── الحالة ──
    @property
    def enabled(self) -> bool:
        """توكن + قناة هدف = البوت جاهز. غير ذلك → fallback مباشرة."""
        return bool(self.token) and bool(self.chat_id)

    def set_client(self, client: Any) -> None:
        """v11.0: ربط عميل البوت الحي (AlertBotClient) كطبقة إرسال احتياطية
        بالأزرار — يُستدعى من main.initialize بعد start() الناجح فقط."""
        self._client = client

    # ── Bot API primitives ──
    async def _post(self, method: str, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """POST واحد إلى Bot API — يعيد (http_status, json_body).
        الرد غير-JSON لا يرفع استثناء (يُغلف في description)."""
        url = f"{BOT_API_BASE}/bot{self.token}/{method}"
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                try:
                    body = await resp.json(content_type=None)
                    if not isinstance(body, dict):
                        body = {"description": str(body)[:200]}
                except Exception:
                    try:
                        raw = await resp.text()
                    except Exception:
                        raw = ""
                    body = {"description": raw[:200]}
                return resp.status, body

    async def get_me(self) -> Optional[Dict[str, Any]]:
        """getMe — يتحقق من صلاحية التوكن ويخزّن هوية البوت (id/username)."""
        if not self.token:
            return None
        try:
            status, body = await self._post("getMe", {})
            if status == 200 and body.get("ok"):
                result = body.get("result") or {}
                self.bot_id = result.get("id")
                self.bot_username = result.get("username")
                return result
            self.last_error = f"getMe_{status}:{str(body.get('description') or '')[:120]}"
            logger.warning(f"⚠️ AlertBot getMe failed: {self.last_error}")
        except Exception as e:
            self.last_error = f"getMe_error:{type(e).__name__}"
            logger.warning(f"⚠️ AlertBot getMe error: {type(e).__name__}: {e}")
        return None

    async def check_membership(self) -> Optional[str]:
        """فحص عضوية البوت في TARGET_GROUP_ID عبر getChatMember.

        يُستدعى عند الإقلاع: البوت غير العضو سيفشل sendMessage — يُسجَّل
        تحذير واضح (أضف البوت للقناة، يفضل أدمن). لا يرفع استثناءً
        ولا يوقف الإقلاع أبداً. يعيد status العضوية أو None."""
        if not self.enabled:
            return None
        if self.bot_id is None:
            if await self.get_me() is None:
                logger.warning(
                    "⚠️ ALERT_BOT_TOKEN غير صالح — بوت التنبيهات معطّل وستُرسل "
                    "التنبيهات من حسابات المستخدمين (fallback) بدون أزرار"
                )
                return None
        try:
            status, body = await self._post(
                "getChatMember", {"chat_id": self.chat_id, "user_id": self.bot_id}
            )
            if status == 200 and body.get("ok"):
                st = str((body.get("result") or {}).get("status") or "")
                if st in ("member", "administrator", "creator"):
                    logger.info(
                        f"✅ alert-bot membership ok: @{self.bot_username} عضو في "
                        f"قناة الهدف (status={st})"
                    )
                else:
                    logger.warning(
                        f"⚠️ بوت التنبيهات @{self.bot_username} ليس عضواً في "
                        f"TARGET_GROUP_ID ({self.chat_id}) — status={st or 'unknown'}. "
                        f"أضف البوت إلى قناة الهدف (يفضل رفعه أدمن) وإلا ستفشل "
                        "تنبيهاته وسيُستخدم مسار حسابات المستخدمين بدون أزرار."
                    )
                return st or None
            logger.warning(
                f"⚠️ alert-bot membership check failed: HTTP {status} "
                f"{str(body.get('description') or '')[:120]} — تأكد أن البوت "
                "عضو في TARGET_GROUP_ID (يفضل أدمن)"
            )
        except Exception as e:
            logger.warning(f"⚠️ alert-bot membership check error: {type(e).__name__}: {e}")
        return None

    # ── الإرسال ──
    def _build_payload(
        self, built: Dict[str, Any], include_user_button: bool = True
    ) -> Dict[str, Any]:
        """حمولة sendMessage — الدالة المركزية build_alert_buttons تبني
        صف الأزرار الأربعة المصغّرة [مراسلة][عرض][نسخ][القروب] (v10.9.1).
        الزر بلا بيانات لا يُعرض إطلاقاً؛ include_user_button=False بعد
        رفض tg://user (يسقط زر «مراسلة» فقط)."""
        kwargs = dict(built.get("button_kwargs") or {})
        kb = build_alert_buttons(include_user_button=include_user_button, **kwargs)
        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": built["text"],
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if kb:
            payload["reply_markup"] = {"inline_keyboard": kb}
        return payload

    async def send(
        self,
        data: Dict[str, Any],
        analysis: Optional[Dict[str, Any]] = None,
        *,
        msg_link: Optional[str] = None,
        group_link: Optional[str] = None,
        chat_username: Optional[str] = None,
        sender_username: Optional[str] = None,
        contact_target: Optional[ContactTarget] = None,
    ) -> Tuple[bool, str, str]:
        """يرسل تنبيهاً واحداً عبر Bot API بالقالب الموحد.

        يعيد (ok, contact_method, reason):
          ok=True  → contact_method = username | mention_button | text_only
                     (username: زر مراسلة t.me؛ mention_button: زر
                      tg://user مُقبل؛ text_only: أُعيد الإرسال بدون زر
                      «مراسلة» بسبب الخصوصية أو لا يوجد مرسل قابل للزر)
          ok=False → reason = سبب الفشل (المستدعي يعود لمسار حسابات
                     المستخدمين بنفس القالب بدون أزرار ويُسجل السبب).
        """
        if not self.enabled:
            return False, "", "no_alert_bot_token"
        try:
            built = build_alert_html(
                data, analysis,
                msg_link=msg_link, group_link=group_link,
                chat_username=chat_username, sender_username=sender_username,
                contact_target=contact_target,
            )
        except Exception as e:
            return False, "", f"build_error:{type(e).__name__}"

        base_method = built["contact_method"]
        sender_dropped = False
        buttons_dropped = False
        payload = self._build_payload(built, include_user_button=True)

        for attempt in range(1, self.max_retries + 1):
            try:
                status, body = await self._post("sendMessage", payload)
            except asyncio.TimeoutError:
                return False, "", f"timeout_attempt_{attempt}"
            except Exception as e:
                return False, "", f"network_error:{type(e).__name__}"

            if status == 200 and body.get("ok"):
                method = "text_only" if (sender_dropped or buttons_dropped) else base_method
                # v11.2 (spec #23): نتيجة زر المراسلة الفعلية — مرة واحدة لكل تنبيه
                _contact_metric(
                    "contact_button_success" if method in ("username", "mention_button")
                    else "contact_button_failure"
                )
                return True, method, ""

            desc = str(body.get("description") or "")

            # ── 429: انتظار retry_after الفعلي ثم إعادة المحاولة (≤3) ──
            if status == 429:
                try:
                    retry_after = int((body.get("parameters") or {}).get("retry_after") or 1)
                except Exception:
                    retry_after = 1
                logger.warning(
                    f"⏳ AlertBot 429 — انتظار {retry_after}s "
                    f"(المحاولة {attempt}/{self.max_retries})"
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(min(max(retry_after, 1), 30) + 0.3)
                    continue
                self.last_error = f"rate_limited_after_{self.max_retries}_attempts"
                return False, "", self.last_error

            # ── زر tg://user المرفوض (خصوصية/صلاحية) → إعادة بدون زر
            # «مراسلة» فقط — بقية الأزرار (عرض/نسخ/القروب)
            # تبقى. الخطآن يخصان زر tg://user حصراً — زر t.me لا يسببهما
            # فيُسلَّم فشله لمسار الـfallback مباشرة.
            if (
                not sender_dropped
                and _has_tg_user_button(built)
                and any(b in desc.upper() for b in BUTTON_USER_ERRORS)
            ):
                sender_dropped = True
                payload = self._build_payload(built, include_user_button=False)
                logger.warning(
                    f"⚠️ AlertBot sender button rejected ({desc[:80]}) — إعادة "
                    "الإرسال بدون زر «مراسلة» (رابط الاسم يبقى في النص وتبقى "
                    "بقية الأزرار)"
                )
                continue

            # ── v11.0: خطأ زر عام (URL/نوع/markup) → إعادة بدون أزرار —
            # نص التنبيه يبقى بروابطه القابلة للنقر في الترويسة؛ التنبيه
            # لا يُسلَّم للمسار الاحتياطي إلا إن فشلت هذه الإعادة أيضاً.
            if not buttons_dropped and any(b in desc.upper() for b in GENERIC_BUTTON_ERRORS):
                buttons_dropped = True
                payload = self._build_payload(built)
                payload.pop("reply_markup", None)
                logger.warning(
                    f"⚠️ AlertBot button rejected ({desc[:80]}) — إعادة الإرسال "
                    "بدون أزرار (روابط الترويسة تبقى قابلة للنقر في النص)"
                )
                continue

            self.last_error = f"bot_api_{status}:{desc[:120]}"
            # ── v11.0: طبقة الإرسال الاحتياطية بالأزرار — عميل البوت الحي
            # (Telethon) بنفس النص والأزرار قبل الاستسلام لحسابات المستخدمين.
            if self._client is not None:
                try:
                    # v11.2 (سبب جذري): تمرير الكيانات جاهزة عبر
                    # formatting_entities يتجاوز مسار HTML parse الذي يحذف
                    # روابط tg://user?id صامتاً في Telethon 1.45 عند فشل حلّ
                    # المرسل من كاش الجلسة — الاسم يبقى قابلاً للنقر في
                    # الطبقة الاحتياطية أيضاً (spec #14).
                    _tgt = built.get("contact_target")
                    _rule = (analysis or {}).get("rule_tag") if isinstance(analysis, dict) else None
                    _pack = build_alert_entity_pack(
                        built.get("display") or "",
                        str(data.get("text") or ""),
                        _tgt,
                        rule_tag=_rule,
                    )
                    if await self._client.send_buttons(
                        _pack["plain_text"], built.get("buttons") or [],
                        formatting_entities=_pack.get("formatting_entities"),
                    ):
                        method = "text_only" if (sender_dropped or buttons_dropped) else base_method
                        _contact_metric(
                            "contact_button_success" if method in ("username", "mention_button")
                            else "contact_button_failure"
                        )
                        logger.info(
                            "🤖 Alert delivered via AlertBotClient (Bot API failed) | "
                            f"contact_method={method} | bot_api_reason={self.last_error[:80]}"
                        )
                        return True, method, ""
                except Exception as _cl_err:
                    logger.debug(
                        f"AlertBotClient fallback error: {type(_cl_err).__name__}"
                    )
            return False, "", self.last_error

        return False, "", "exhausted_retries"
