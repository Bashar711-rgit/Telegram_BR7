#!/usr/bin/env python3
"""
contact_target.py — ContactTarget v11.2 — تجريد وسيلة الوصول إلى المرسل
========================================================================

المهمة الوحيدة (طلب المستخدم — نظام مراسلة المرسل):
    «كل تنبيه يعرض اسم المرسل قابلًا للنقر + زر 📩 مراسلة المرسل،
     بلا أي رابط خام يظهر للمستخدم النهائي».

هذا الملف هو **المكان الوحيد** في المشروع الذي يُختار فيه رابط المرسل
(requirement: لا يوجد منطق مختلف لبناء رابط المرسل في ملفات متعددة).
كل مستهلك — alert_bot (الاسم + زر مراسلة)، alert_ui (الإرسال الاحتياطي
MTProto)، monitors (سلّم الإرسال الاحتياطي + سجل الأزرار) — يأخذ
ContactTarget جاهزًا من هنا ويستخدم selected_url كما هو.

الطرق الثلاث المحفوظة داخليًا (spec #4–#6):
    1. username        → https://t.me/{username}          (الأفضل — يعمل للجميع)
    2. user_id         → tg://user?id={user_id}            (fallback أساسي لمن لا username)
    3. openmessage     → tg://openmessage?user_id={user_id} (احتياطي داخلي تجريبي —
        لا يُختار أبدًا تلقائيًا ولا يُعرض؛ تيليجرام لا يضمنه في كل العملاء)

الاختيار (Resolver، spec #8):
    Sender → هل يوجد username صالح؟ → نعم: username URL
                                   → لا: user_id → tg://user?id=...
                                       (openmessage يُحتفظ به داخليًا فقط)

الهوية الأساسية هي user_id وليس username (spec #11/#القاعدة النهائية):
    username مجرد وسيلة وصول إضافية. غيابه لا يمنع الاسم القابل للنقر
    ولا زر المراسلة.

أنواع المرسلين (spec #18):
    User/Bot      → كل الطرق متاحة
    Channel       → لا يُبنى tg://user?id إطلاقًا (id قناة وليس مستخدم) —
                    username إن وجد وإلا بلا هدف (بلا اختلاق معلومات)
    Anonymous     → يظهر كـ Channel في تيليجرام → نفس معاملة القناة
    Deleted       → لا هدف (المستخدم محذوف فعلًا) — status=partial

النص الظاهر ≠ الرابط (spec #14):
    الاسم المعروض هو الاسم الكامل للشخص (أول + أخير ← أول ← username)،
    والرابط يُحمل ككيان MessageEntityTextUrl داخل النص — لا يظهر
    المستخدم أي https://t.me أو tg:// — كل الروابط داخلية بالكامل.
    بناء الكيانات هنا يحسب offset/length بوحدات UTF-16 (حساب تيليجرام
    الرسمي) — الحروف العربية والإيموجي خارج BMP تُحسب صحيحة.

الأداء (spec #24):
    كل شيء هنا pure-sync وبلا أي I/O أو استدعاء Telegram API —
    resolve_contact_target يعمل من البيانات الملتقطة مع الحدث نفسه.
    جلب الكيان (get_sender/get_input_sender) مسؤولية الطبقات الأعلى
    (sender_resolver مع كاشه) — لا حل مكرر ولا FloodWait من هذا الملف.

أمان الاختبار: استيراد telethon اختياري — بلا telethon تعيد الدوال
البيانات والروابط فقط (كيانات = قائمة فارغة) فتعمل الاختبارات واللوحة.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

try:  # pragma: no cover — بيئة الإنتاج لديها telethon دائمًا
    from telethon.tl.types import (
        MessageEntityBlockquote,
        MessageEntityBold,
        MessageEntityTextUrl,
    )

    _TL_OK = True
except Exception:  # pragma: no cover
    MessageEntityBold = None
    MessageEntityBlockquote = None
    MessageEntityTextUrl = None
    _TL_OK = False

# بطيء الاستيراد لكنه بلا دورة: nav_resolver لا يستورد contact_target.
from nav_resolver import clean_username as _clean_username  # noqa: E402
from nav_resolver import is_valid_username as _is_valid_username  # noqa: E402

__version__ = "1.0.0"

_TG_URL = "https://t.me/"

# ── نصوص القالب (المصدر الوحيد — alert_bot يستوردها) ─────────────────────
# شكل التنبيه النهائي الفعال (مواصفة المستخدم):
#     👤: [اسم المرسل قابل للنقر]
#     (سطر فارغ)
#     💬:
#     [نص الرسالة كاملاً]
#     [ مراسلة ]  [ عرض الرسالة ]
SENDER_LABEL = "👤: "
CARD_TITLE_PLAIN = "💬:"

# أنواع المرسل التي يُسمح لها بـ tg://user?id (لا القنوات ولا المحذوفون)
_USER_ID_CAPABLE_TYPES = frozenset({"user", "bot", "assumed_user"})

# مطابقة للسلوك التاريخي: sender_type غير مصنف (""/unknown/none) مع sender_id يُعامل
# كمستخدم مفترض (كما كان يبني alert_bot/monitors روابط tg://user?id قبل v11.2).
# الحالة الحرجة: sender=None في _event_to_dict (فشل تحميل الكيان) تترك
# sender_type="none" بينما sender_id موجودة — إسقاط tg://user?id هنا كان
# سيعيد الشكوى الأصلية (اسم المرسل غير قابل للنقر) لأكثر حالة فشل شيوعاً.
# لا هوية إطلاقاً (sender_id=0) فقط تعني none فعلياً.


# =============================================================================
# Metrics (spec #23) — عدادات محصورة بلا I/O
# =============================================================================
_CONTACT_METRICS: Dict[str, int] = defaultdict(int)

_METRIC_KEYS = (
    "sender_resolution_success",
    "sender_resolution_failure",
    "username_available",
    "user_id_fallback",
    "clickable_sender_success",
    "clickable_sender_failure",
    "contact_button_success",
    "contact_button_failure",
)


def metric_inc(name: str, by: int = 1) -> None:
    """زيادة عداد آمن — لا ترفع استثناءً أبدًا."""
    try:
        if name in _METRIC_KEYS:
            _CONTACT_METRICS[name] += int(by)
    except Exception:
        pass


def get_contact_metrics_snapshot() -> Dict[str, Any]:
    """لقطة محصورة للـ/health واللوحة — مفاتيح الصفر موجودة دائمًا."""
    snap: Dict[str, Any] = {"enabled": True, "version": __version__}
    for key in _METRIC_KEYS:
        snap[key] = int(_CONTACT_METRICS.get(key, 0))
    return snap


# =============================================================================
# UTF-16 helpers — تيليجرام يقيس offset/length بوحدات UTF-16
# =============================================================================
def utf16_len(value: str) -> int:
    """طول النص بوحدات UTF-16 (الحرف خارج BMP = وحدتان)."""
    try:
        return len(value.encode("utf-16-le")) // 2
    except Exception:
        return len(value or "")


# =============================================================================
# Display name (spec #17) — أول+أخير ← أول ← username ← fallback
# =============================================================================
def build_display_name(
    first_name: Any = None,
    last_name: Any = None,
    username: Any = None,
    user_id: Any = 0,
    display: Any = None,
) -> str:
    """اسم المرسل المعروض — بلا user_id كاسم ظاهر (يظهر في الـdebug فقط)."""
    if display and str(display).strip():
        return str(display).strip()
    full = f"{first_name or ''} {last_name or ''}".strip()
    if full:
        return full
    uname = _clean_username(username)
    if uname:
        return uname  # بلا @ — الاسم نفسه قابل للنقر
    try:
        sid = int(user_id or 0)
    except Exception:
        sid = 0
    return f"مستخدم ({sid})" if sid else "مستخدم"


# =============================================================================
# ContactTarget — التجريد الوحيد لوسيلة الوصول (spec #7)
# =============================================================================
@dataclass(slots=True)
class ContactTarget:
    """
    كل طرق الوصول الممكنة للمرسل + الهدف المختار.

        user_id            الهوية الأساسية (ليست username)
        username           الاسم العام إن وجد (بلا @)
        display_name       الاسم الظاهر في التنبيه (قابل للنقر)
        sender_type        user|bot|channel|assumed_user|deleted|none
        username_url       https://t.me/{username}
        user_id_url        tg://user?id={user_id}
        openmessage_url    tg://openmessage?user_id={user_id}  (داخلي تجريبي)
        selected_url       أفضل هدف متاح — يستخدمه الاسم والزر معًا (spec #16)
        target_type        username|user_id|openmessage|none
        resolution_status  resolved|partial|failed
        reasons            أسباب داخلية للوج فقط — لا تظهر في التنبيهات
    """

    user_id: int = 0
    username: Optional[str] = None
    usernames: Optional[List[str]] = None
    display_name: str = ""
    sender_type: str = "none"
    username_url: Optional[str] = None
    user_id_url: Optional[str] = None
    openmessage_url: Optional[str] = None
    selected_url: Optional[str] = None
    target_type: str = "none"
    resolution_status: str = "failed"
    reasons: List[str] = field(default_factory=list)

    @property
    def reachable(self) -> bool:
        """هدف واحد على الأقل متاح — الاسم والزر يستخدمان نفس الهدف."""
        return bool(self.selected_url)

    def to_log_dict(self) -> Dict[str, Any]:
        """لوج آمن — بلا أرقام هواتف ولا أي بيانات حساسة (spec #22)."""
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
                    ("user_id", self.user_id_url),
                    ("openmessage", self.openmessage_url),
                ) if v]
            ) or "none",
            "resolution_status": self.resolution_status,
            "reasons": ",".join(self.reasons) or "-",
        }


# =============================================================================
# Resolver — السياسة المركزية الوحيدة لاختيار الرابط (spec #8)
# =============================================================================
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
) -> ContactTarget:
    """
    يبني ContactTarget من بيانات المرسل الملتقطة — **بلا أي I/O**.

    الخريطة (تُحفظ كل الطرق ثم يُختار الأفضل):
        username صالح  → username_url   (الأفضل عالميًا)
        user_id > 0    → user_id_url    (fallback أساسي — spec #5)
        user_id > 0    → openmessage_url (يُحتفظ به داخليًا فقط — spec #6)

    لا يرفع استثناءً أبدًا؛ البيانات الفارغة تعني target_type="none".
    """
    started = time.perf_counter()
    reasons: List[str] = []
    try:
        sid = int(sender_id or 0)
    except Exception:
        sid = 0
    if sid < 0:
        # sender_id سالب = كيان محادثة وليس مستخدمًا — لا tg://user له
        reasons.append("negative_sender_id")
        sid = 0

    # ── username: الأساسي ثم كل usernames النشطة (حسابات premium) ──
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

    # ── نوع المرسل (spec #18) ──
    # ""/unknown/none مع sender_id → assumed_user: الكيان غائب لكن الهوية
    # الأساسية (user_id) موجودة — لا يجوز إسقاط وسيلة الوصول إليها (spec #5).
    # المرسل Channel/Deleted يُبنى من كيان فعلي (ليس غياب كيان) فيبقى كما هو.
    st = str(sender_type or "").strip().lower()
    if st in ("", "unknown", "none"):
        st = "assumed_user" if sid else "none"
    if is_deleted and str(is_deleted).strip().lower() in ("true", "1", "yes"):
        st = "deleted"
    elif st in ("user", "assumed_user") and (is_bot is True or str(is_bot or "").lower() == "true"):
        st = "bot"
    if st == "channel":
        reasons.append("channel_sender_no_user_id")

    # ── الطرق الثلاث ──
    username_url = f"{_TG_URL}{uname}" if uname else None
    user_id_url = (
        f"tg://user?id={sid}"
        if (sid and st in _USER_ID_CAPABLE_TYPES)
        else None
    )
    # openmessage: احتياطي داخلي تجريبي فقط — لا يُختار أبدًا (spec #6)
    openmessage_url = f"tg://openmessage?user_id={sid}" if sid else None

    # ── الاختيار: username ثم user_id ثم لا شيء ──
    if username_url:
        selected_url, target_type = username_url, "username"
    elif user_id_url:
        selected_url, target_type = user_id_url, "user_id"
    else:
        selected_url, target_type = None, "none"

    if uname:
        metric_inc("username_available")
    if target_type == "user_id":
        metric_inc("user_id_fallback")

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
        selected_url=selected_url,
        target_type=target_type,
        resolution_status=status,
        reasons=reasons,
    )
    try:  # لوج تشخيصي محصور — debug كي لا يزاحم لوج [SENDER] الحدثي
        logger.debug(
            f"contact_target resolved in {(time.perf_counter() - started) * 1000:.2f}ms | "
            + " ".join(f"{k}={v}" for k, v in target.to_log_dict().items())
        )
    except Exception:
        pass
    return target


# =============================================================================
# Validation chain (spec #20) — قبل الإرسال
# =============================================================================
def validate_contact_target(target: Optional[ContactTarget]) -> Tuple[bool, str]:
    """
    سلسلة التحقق قبل بناء الاسم/الزر:
        target موجود؟ → نوع مرسل صالح؟ → اسم معروض متاح؟ → هدف واحد على الأقل؟
        → الهدف من صيغة معروفة؟ → الكيان القابل للنقر قابل للبناء؟
    لا تفشل التنبيه — تفشل الهدف فقط (المستدعي يكمل بلا كيان).
    """
    if target is None:
        return False, "no_target"
    if target.sender_type in ("channel", "deleted", "none") and not target.username_url:
        return False, f"sender_type_{target.sender_type}_not_linkable"
    if not (target.display_name or "").strip():
        return False, "no_display_name"
    if not target.selected_url:
        return False, "no_reachable_target"
    if not (
        target.selected_url.startswith("https://t.me/")
        or target.selected_url.startswith("tg://user?id=")
    ):
        return False, "unknown_url_shape"
    if MessageEntityTextUrl is None:
        return False, "entity_class_unavailable"
    return True, ""


# =============================================================================
# Clickable-name entities (spec #14) — النص الظاهر ≠ الرابط
# =============================================================================
def build_alert_entity_pack(
    display_name: str,
    message_text: str,
    target: Optional[ContactTarget],
    *,
    rule_tag: Optional[str] = None,
    plain_budget: int = 3800,
) -> Dict[str, Any]:
    """
    يبني (plain_text, formatting_entities) للقالب v11.2 — نفس الشكل المرئي
    لحرفياً للنسخة HTML (parse_mode="html") لكن عبر كيانات جاهزة.

    لماذا كيانات جاهزة؟ Telethon 1.45 `client.send_message(parse_mode=...)`
    يمرر النص عبر _parse_message_text التي **تحذف** أي MessageEntityTextUrl
    ينتهي بـ tg://user?id= إذا لم يستطع عميل الإرسال حلّ المستخدم من كاش
    جلسته (_replace_with_mention → del entities[i]) — عميل البوت الحي
    (جلسة جديدة) لا يملك كاش المرسلين فما يقريبًا كل أسماء tg://user تُحذف
    صامتة ويصبح الاسم نصًا عاديًا (السبب الجذري للشكوى).
    تمرير formatting_entities= **يتفادى ذلك المسار كلياً** (مرجع:
    telethon/client/messages.py — parse ينفذ فقط عندما formatting_entities
    is None) فتصل الكيانات حرفياً كما بُنيت.

    الكيانات:
        MessageEntityTextUrl    → الاسم (الرابط الداخلي selected_url)
        MessageEntityBold       → «💬:»
        MessageEntityBlockquote → نص الرسالة (الضغط عليه → Copy Text)
    """
    display = str(display_name or "").strip() or "مستخدم"
    raw = str(message_text or "")
    if len(raw) > max(64, int(plain_budget)):
        raw = raw[: max(64, int(plain_budget)) - 1].rstrip() + "…"

    plain_text = f"{SENDER_LABEL}{display}\n\n{CARD_TITLE_PLAIN}\n{raw}"
    entities: List[Any] = []

    try:
        name_off = utf16_len(SENDER_LABEL)
        name_len = utf16_len(display)
        label_off = name_off + name_len + utf16_len("\n\n")
        if target is not None and target.selected_url and MessageEntityTextUrl is not None:
            entities.append(
                MessageEntityTextUrl(offset=name_off, length=name_len, url=target.selected_url)
            )
        if MessageEntityBold is not None:
            entities.append(
                MessageEntityBold(offset=label_off, length=utf16_len(CARD_TITLE_PLAIN))
            )
        if raw and MessageEntityBlockquote is not None:
            text_off = label_off + utf16_len(CARD_TITLE_PLAIN) + utf16_len("\n")
            entities.append(
                MessageEntityBlockquote(offset=text_off, length=utf16_len(raw))
            )
    except Exception as e:  # الكيانات ميزة — فشلها لا يكسر الإرسال
        logger.debug(f"contact_target entity pack failed: {type(e).__name__}: {e}")
        entities = []

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
            and target.selected_url
            and any(type(e).__name__ == "MessageEntityTextUrl" for e in entities)
        ),
    }


# =============================================================================
# [SENDER] logging (spec #22) — سطر واحد محصور بلا بيانات حساسة
# =============================================================================
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
    """سطر [SENDER] موحد — يظهر في اللوج فقط ولا يدخل نص التنبيه أبدًا."""
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
