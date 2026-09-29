#!/usr/bin/env python3
"""
alert_bot.py – v10.8 Alert Bot — إرسال التنبيهات إلى TARGET_GROUP_ID عبر
Telegram Bot API (aiohttp) بالشكل الجديد المطلوب حرفياً.

المهمة: تغيير شكل وطريقة إرسال التنبيهات إلى TARGET_GROUP_ID لتطابق
النمط التالي بالضبط (parse_mode=HTML):

    سطر 1:  👤 {SENDER}
    سطر 2:  <b>المرسل :</b> ID {sender_id}
    (سطر فارغ)
    <b>نص الرسالة :</b>
    {text}        <- escape_html + truncate(400)
    <b>رابط الرسالة :</b> {msg_link}   <- أو النص "غير متاح" إن لم يوجد رابط

SENDER:
  - إن وُجد username:  <a href="https://t.me/USERNAME">@USERNAME</a>
  - بدونه:             <a href="tg://user?id=ID">الاسم الكامل</a>

msg_link:
  - قروب عام (له username):  https://t.me/{username}/{message_id}
  - قروب خاص (id يبدأ -100): https://t.me/c/{id بدون -100}/{message_id}
  - غير ذلك: «غير متاح» (بدون رابط)

الأزرار (Inline URL buttons في صف واحد — تُرسل عبر البوت فقط):
  - زر «جروب» → رابط القروب (t.me/username أو t.me/c/inner)؛ يُحذف الزر
    إن لم يوجد رابط.
  - زر المرسل:
      * username موجود: نص الزر «@USERNAME» و url = https://t.me/USERNAME
      * بدون username: نص الزر = اسمه و url = tg://user?id=ID

طريقة الإرسال:
  POST https://api.telegram.org/bot{TOKEN}/sendMessage
  json: chat_id=TARGET_GROUP_ID, text, parse_mode="HTML",
        link_preview_options={"is_disabled": true},
        reply_markup={"inline_keyboard": [[...]]}

معالجة الأخطاء:
  * 429 → انتظار retry_after ثم إعادة المحاولة (بحد أقصى 3).
  * BUTTON_USER_INVALID أو BUTTON_USER_PRIVACY_RESTRICTED → إعادة الإرسال
    بدون زر المرسل (يبقى رابط الاسم في النص ويبقى زر «جروب»).
  * أي فشل آخر أو عدم وجود ALERT_BOT_TOKEN → العودة لمسار الإرسال من
    حساب المستخدم (fallback في monitors.py) بنفس نص التنبيه بدون أزرار
    مع تسجيل السبب في اللوج.

المراقبون (حسابات المستخدمين) يستمرون في الاستماع وبناء البيانات فقط —
الإرسال كله يمر عبر AlertBot.send(data, analysis) من _send_alert، مع
الحفاظ على مرور الرسالة عبر rate_limiter والـ circuit breaker.

شروط بيئية: البوت يجب أن يكون عضواً (يفضل أدمن) في TARGET_GROUP_ID —
check_membership() (getChatMember) تسجّل تحذيراً واضحاً عند الإقلاع إن
لم يكن عضواً.

تسجيل النتيجة: alerts.contact_method = username | mention_button |
text_only (عمود موجودة منذ v10.7 — قيم جديدة بنفس العقد ≤20 حرفاً).

حفاظاً على الميزات: سطر «🏷 قاعدة: …» (محرك القواعد) يُلاحق في نهاية
التنبيه عند وجوده فقط — لا يظهر في الشكل الافتراضي إطلاقاً.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from loguru import logger

from config import InputSanitizer

BOT_API_BASE = "https://api.telegram.org"
# المواصفة حرفياً: نص الرسالة escape_html + truncate(400)
ALERT_TEXT_TRUNCATE = 400
MSG_LINK_UNAVAILABLE = "غير متاح"
GROUP_BUTTON_TEXT = "جروب"

# أخطاء زر tg://user — يُعاد الإرسال بدون زر المرسل (زر «جروب» يبقى،
# ورابط الاسم يبقى في النص حرفياً كما في المواصفة).
BUTTON_USER_ERRORS = ("BUTTON_USER_INVALID", "BUTTON_USER_PRIVACY_RESTRICTED")


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


def build_alert_html(
    data: Dict[str, Any],
    analysis: Optional[Dict[str, Any]] = None,
    *,
    msg_link: Optional[str] = None,
    group_link: Optional[str] = None,
    chat_username: Optional[str] = None,
    sender_username: Optional[str] = None,
) -> Dict[str, Any]:
    """يبني نص التنبيه بالصيغة الجديدة (HTML) + زري المرسل/الجروب.

    يعيد dict:
      text           — نص التنبيه الكامل (parse_mode="HTML")
      sender_button  — {"text", "url"} | None (زر المرسل)
      group_button   — {"text", "url"} | None (زر «جروب»)
      contact_method — username | mention_button | text_only
      msg_link       — رابط الرسالة المستخدم | None
      display        — الاسم المعروض للمرسل

    القيم الواردة من msg_link/group_link/chat_username/sender_username
    تتفوق على قيم data (هي نتيجة محلل v10.5 المُحسَّن). لا يرفع استثناء
    أبداً — فشل-آمن: بيانات ناقصة تعني حقولاً بصدق («غير متاح»/بلا زر).
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

    # ── الاسم المعروض ──
    display = (
        str(data.get("sender_display") or "").strip()
        or f"{data.get('sender_first_name') or ''} {data.get('sender_last_name') or ''}".strip()
        or (f"مستخدم ({sender_id})" if sender_id else "مستخدم")
    )

    # ── SENDER: سطر 1 ──
    if username:
        sender_html = f'<a href="https://t.me/{username}">@{username}</a>'
        sender_button: Optional[Dict[str, str]] = {
            "text": f"@{username}",
            "url": f"https://t.me/{username}",
        }
        contact_method = "username"
    elif sender_id:
        sender_html = f'<a href="tg://user?id={sender_id}">{InputSanitizer.escape_html(display)}</a>'
        sender_button = {"text": display, "url": f"tg://user?id={sender_id}"}
        contact_method = "mention_button"
    else:
        sender_html = InputSanitizer.escape_html(display)
        sender_button = None
        contact_method = "text_only"

    # ── msg_link: قروب عام → t.me/{u}/{id} | خاص -100 → t.me/c/{inner}/{id} ──
    link = str(msg_link or "").strip()
    if link in ("", "#"):
        link = ""
        chat_uname = _clean_username(chat_username) or _clean_username(data.get("chat_username"))
        message_id = data.get("message_id")
        if chat_uname and message_id:
            link = f"https://t.me/{chat_uname}/{message_id}"
        else:
            inner = _tme_inner_id(data.get("chat_id"))
            if inner and message_id:
                link = f"https://t.me/c/{inner}/{message_id}"
    msg_html = f'<a href="{link}">{link}</a>' if link else MSG_LINK_UNAVAILABLE

    # ── زر «جروب»: رابط القروب فقط — يُحذف الزر إن لم يوجد رابط ──
    glink = str(group_link or "").strip()
    if glink in ("", "#"):
        chat_uname = _clean_username(chat_username) or _clean_username(data.get("chat_username"))
        inner = _tme_inner_id(data.get("chat_id"))
        if chat_uname:
            glink = f"https://t.me/{chat_uname}"
        elif inner:
            glink = f"https://t.me/c/{inner}"
        else:
            glink = ""
    group_button = {"text": GROUP_BUTTON_TEXT, "url": glink} if glink else None

    # ── نص الرسالة: escape_html + truncate(400) — المواصفة حرفياً ──
    safe_text = InputSanitizer.escape_html(
        InputSanitizer.truncate(str(data.get("text") or ""), ALERT_TEXT_TRUNCATE)
    )

    lines: List[str] = [
        f"👤 {sender_html}",
        f"<b>المرسل :</b> ID {sender_id}",
        "",
        "<b>نص الرسالة :</b>",
        safe_text,
        f"<b>رابط الرسالة :</b> {msg_html}",
    ]
    # حفاظاً على ميزة محرك القواعد: سطر القاعدة يُلاحق عند وجوده فقط.
    rule_tag = (analysis or {}).get("rule_tag") if isinstance(analysis, dict) else None
    if rule_tag:
        lines.append("")
        lines.append(f"🏷 قاعدة: {InputSanitizer.escape_html(str(rule_tag))}")

    return {
        "text": "\n".join(lines),
        "sender_button": sender_button,
        "group_button": group_button,
        "contact_method": contact_method,
        "msg_link": link or None,
        "display": display,
    }


class AlertBot:
    """مُرسِل التنبيهات عبر Telegram Bot API (aiohttp).

    * بلا توكن → enabled=False → مسار حسابات المستخدمين (fallback) يعمل
      كما هو بلا أعطال (شرط القبول رقم 6).
    * كل استدعاء send() يبني الحمولة من بيانات الرسالة نفسها ويُرسل
      sendMessage بـ parse_mode="HTML" + link_preview_options معطّل +
      صف أزرار inline URL واحد (المرسل ثم «جروب»).
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

    # ── الحالة ──
    @property
    def enabled(self) -> bool:
        """توكن + قناة هدف = البوت جاهز. غير ذلك → fallback مباشرة."""
        return bool(self.token) and bool(self.chat_id)

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
        self, built: Dict[str, Any], include_sender_button: bool = True
    ) -> Dict[str, Any]:
        """حمولة sendMessage — صف أزرار واحد: [زر المرسل، جروب] (بالصورة
        المطلوبة). زر بلا بيانات لا يُعرض إطلاقاً."""
        kb: List[Dict[str, str]] = []
        if built.get("sender_button") and include_sender_button:
            kb.append(built["sender_button"])
        if built.get("group_button"):
            kb.append(built["group_button"])
        payload: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": built["text"],
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if kb:
            payload["reply_markup"] = {"inline_keyboard": [kb]}
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
    ) -> Tuple[bool, str, str]:
        """يرسل تنبيهاً واحداً عبر Bot API.

        يعيد (ok, contact_method, reason):
          ok=True  → contact_method = username | mention_button | text_only
                     (username: له معرف؛ mention_button: زر tg://user مُقبل؛
                      text_only: أُعيد الإرسال بدون زر المرسل بسبب الخصوصية
                      أو لا يوجد مرسول قابل للزر)
          ok=False → reason = سبب الفشل (المستدعي يعود لمسار حسابات
                     المستخدمين بنفس النص بدون أزرار ويُسجل السبب).
        """
        if not self.enabled:
            return False, "", "no_alert_bot_token"
        try:
            built = build_alert_html(
                data, analysis,
                msg_link=msg_link, group_link=group_link,
                chat_username=chat_username, sender_username=sender_username,
            )
        except Exception as e:
            return False, "", f"build_error:{type(e).__name__}"

        base_method = built["contact_method"]
        sender_dropped = False
        payload = self._build_payload(built, include_sender_button=True)

        for attempt in range(1, self.max_retries + 1):
            try:
                status, body = await self._post("sendMessage", payload)
            except asyncio.TimeoutError:
                return False, "", f"timeout_attempt_{attempt}"
            except Exception as e:
                return False, "", f"network_error:{type(e).__name__}"

            if status == 200 and body.get("ok"):
                method = "text_only" if sender_dropped else base_method
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

            # ── زر المرسل المرفوض (خصوصية/صلاحية) → إعادة بدون زر المرسل ──
            # الخطآن هذان يخصان زر tg://user حصراً — زر t.me/username لا
            # يسببها أبداً فيُسلَّم فشله لمسار الـfallback مباشرة.
            if (
                not sender_dropped
                and (built.get("sender_button") or {}).get("url", "").startswith("tg://user?id=")
                and any(b in desc.upper() for b in BUTTON_USER_ERRORS)
            ):
                sender_dropped = True
                payload = self._build_payload(built, include_sender_button=False)
                logger.warning(
                    f"⚠️ AlertBot sender button rejected ({desc[:80]}) — إعادة "
                    "الإرسال بدون زر المرسل (رابط الاسم يبقى في النص وزر «جروب» يبقى)"
                )
                continue

            self.last_error = f"bot_api_{status}:{desc[:120]}"
            return False, "", self.last_error

        return False, "", "exhausted_retries"
