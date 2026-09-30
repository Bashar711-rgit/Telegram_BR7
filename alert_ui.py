#!/usr/bin/env python3
"""
alert_ui.py – v11.0 Alert UI Client — عميل بوت تنبيهات حي (Telethon Bot Client).

الهدف (طلب المستخدم: «أزرار حقيقية وليست وهمية — أزرار عملية»):
  * AlertBot (alert_bot.py) يرسل الأزرار عبر Bot API HTTP — أزرار حقيقية
    فعلاً (URL تفتح روابط حقيقية + copy_text ينسخ للحافظة فعلياً)، لكنه
    استدعاء HTTP عابر **لا يستقبل أي Callbacks**.
  * هذا الملف يشغّل عميل Telethon بنفس توكن البوت (ALERT_BOT_TOKEN أو
    BOT_TOKEN) يبقى متصلاً 24/7 فيستقبل CallbackQuery فعلياً (معالج
    «copy_» يجيب بنص التنبيه من DB في نافذة منبثقة)، ويعمل **طبقة إرسال
    احتياطية بالأزرار** إن فشل Bot API — فيصل التنبيه بأزراره العملية
    دائماً: Bot API ← عميل البوت الحي ← حسابات المستخدمين (نص فقط).

الاستخدام (ربط تلقائي في main.initialize — بلا أي تدخل يدوي):
    self.alert_ui_client = AlertBotClient(api_id, api_hash, token, db, target)
    if await self.alert_ui_client.start():          # False بلا توكن (بلا أعطال)
        self.alert_bot.set_client(self.alert_ui_client)   # سلّم الإرسال
  والإيقاف في stop(): await self.alert_ui_client.stop()

حدود تيليجرام المعلنة بصدق (لا تحايل عليها ولا هذا الملف):
  * tg://user?id= زر يُرفض من السيرفر لمرسل تخفيه خصوصيته (BUTTON_USER_*)
    ولا يفتح إلا لدى عميل يعرف المستخدم — وهذا قيد من تيليجرام نفسه.
  * روابط t.me/c/... تفتح لأعضاء المجموعة المصدر فقط.

توافق Telethon: طبقة الأزرار أُعيدت هيكلتها في الإصدارات الحديثة
(KeyboardInlineButton + InlineButtonType*) — الدعم هنا للجيلين معاً،
والاختيار وقت الاستيراد.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from loguru import logger
from telethon import TelegramClient, events
from telethon.errors import RPCError
from telethon.sessions import StringSession

try:  # Telethon ≥ 1.45 — طبقة الأزرار المُعاد هيكلتها
    from telethon.tl.types import (
        InlineButtonTypeCallback,
        InlineButtonTypeCopy,
        InlineButtonTypeUrl,
        KeyboardInlineButton,
        KeyboardInlineButtonRow,
        ReplyInlineMarkup,
    )

    _MODERN_TL = True
except ImportError:  # أجيال Telethon الأقدم (KeyboardButton*)
    from telethon.tl.types import (  # type: ignore  # noqa: F401
        KeyboardButtonCallback,
        KeyboardButtonCopy,
        KeyboardButtonRow,
        KeyboardButtonUrl,
        ReplyInlineMarkup,
    )

    _MODERN_TL = False

# أخطاء الأزرار التي يعاد الإرسال بعدها بدون أزرار (النص يبقى بروابطه
# القابلة للنقر في الترويسة) — التنبيه لا يضيع بسبب زر أبداً.
_BUTTON_RETRY_ERRORS = (
    "BUTTON_USER_PRIVACY_RESTRICTED",
    "BUTTON_USER_INVALID",
    "BUTTON_URL_INVALID",
    "BUTTON_DATA_INVALID",
    "BUTTON_TYPE_INVALID",
    "BUTTON_TEXT_INVALID",
    "REPLY_MARKUP_INVALID",
)


def _mk_url_button(text: str, url: str) -> Any:
    if _MODERN_TL:
        return KeyboardInlineButton(text, type=InlineButtonTypeUrl(url=url))
    return KeyboardButtonUrl(text, url)  # type: ignore[name-defined]  # noqa: F821


def _mk_copy_button(text: str, value: str) -> Any:
    if _MODERN_TL:
        return KeyboardInlineButton(text, type=InlineButtonTypeCopy(copy_text=value))
    return KeyboardButtonCopy(text, value)  # type: ignore[name-defined]  # noqa: F821


def _mk_callback_button(text: str, payload: bytes) -> Any:
    if _MODERN_TL:
        return KeyboardInlineButton(text, type=InlineButtonTypeCallback(data=payload))
    return KeyboardButtonCallback(text, payload)  # type: ignore[name-defined]  # noqa: F821


def _mk_row(buttons: List[Any]) -> Any:
    if _MODERN_TL:
        return KeyboardInlineButtonRow(buttons=buttons)
    return KeyboardButtonRow(buttons)  # type: ignore[name-defined]  # noqa: F821


class AlertBotClient:
    """عميل بوت التنبيهات الحي — يستقبل Callbacks ويرسل أزراراً حقيقية.

    * بلا توكن → start() تعيد False بلا أي استثناء (السلوك القديم محفوظ).
    * المعالج الحي: CallbackQuery بنمط ^copy_ → نافذة منبثقة بنص التنبيه
      الأصلي من DB (get_alert_text_by_hash ← get_message_text_by_hash).
    * send_buttons(): إرسال HTML + صفوف أزرار (نفس بنية build_alert_buttons:
      {"text","url"} | {"text","copy_text"}) عبر MTProto — طبقة الإرسال
      الاحتياطية لسلّم alert_bot. زر بلا نوع معروف يُحذف بصدق (لا زر ميت).
    """

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        token: Optional[str],
        db: Any,
        target_chat_id: Any = 0,
    ) -> None:
        self.api_id = int(api_id or 0)
        self.api_hash = str(api_hash or "")
        self.token = str(token or "").strip()
        self.db = db
        try:
            self.target_chat_id = int(target_chat_id) if target_chat_id else 0
        except Exception:
            self.target_chat_id = 0
        self.client: Optional[TelegramClient] = None
        self.bot_username: Optional[str] = None
        self.bot_id: Optional[int] = None
        self._started = False

    # ── الحالة ──
    @property
    def is_alive(self) -> bool:
        return bool(self._started and self.client is not None and self.client.is_connected())

    # ── الإقلاع/الإيقاف ──
    async def start(self) -> bool:
        """يشغّل عميل البوت ويسجّل معالجات الأزرار. False بلا توكن/فشل (بلا أعطال)."""
        if not self.token:
            logger.warning(
                "⚠️ BOT_TOKEN/ALERT_BOT_TOKEN غير مضبوط — عميل أزرار التنبيهات "
                "لا يعمل، والإرسال عبر Bot API كما هو (الأزرار URL/copy_text "
                "تعمل دون هذا العميل)"
            )
            return False
        if not self.api_id or not self.api_hash:
            logger.warning("⚠️ api_id/api_hash مفقود — عميل أزرار التنبيهات معطّل")
            return False
        try:
            self.client = TelegramClient(StringSession(), self.api_id, self.api_hash)
            await self.client.start(bot_token=self.token)
            self._register_handlers(self.client)
            me = await self.client.get_me()
            self.bot_username = getattr(me, "username", None)
            self.bot_id = getattr(me, "id", None)
            self._started = True
            logger.info(
                f"✅ AlertBotClient جاهز: @{self.bot_username} — يستقبل Callbacks "
                "ويعمل طبقة إرسال احتياطية بالأزرار (v11.0)"
            )
            return True
        except Exception as e:
            logger.warning(
                f"⚠️ فشل تشغيل AlertBotClient ({type(e).__name__}: {e}) — "
                "الإرسال يستمر عبر Bot API وبوت حسابات المستخدمين كما هو"
            )
            self.client = None
            self._started = False
            return False

    async def stop(self) -> None:
        """فصل نظيف — لا يرفع استثناءً أبداً."""
        try:
            if self.client is not None:
                await self.client.disconnect()
        except Exception:
            pass
        self._started = False
        self.client = None

    # ── المعالجات الحية (أزرار عملية — استقبال فعلي للأحداث) ──
    def _register_handlers(self, client: TelegramClient) -> None:
        @client.on(events.CallbackQuery(pattern=rb"^copy_"))
        async def _copy_cb(event: Any) -> None:  # pragma: no cover - wiring only
            await self._handle_copy_callback(event)

    async def _handle_copy_callback(self, event: Any) -> None:
        """نافذة منبثقة بنص التنبيه الأصلي — استجابة حقيقية لضغطة الزر."""
        try:
            h = bytes(event.data).decode("utf-8", "ignore").split("_", 1)[1]
            text = None
            if self.db is not None:
                text = await self.db.get_alert_text_by_hash(h)
                if not text:
                    text = await self.db.get_message_text_by_hash(h)
            await event.answer((text or "النص غير متوفر")[:200], alert=True)
        except Exception as e:
            logger.debug(f"copy callback error: {type(e).__name__}")
            try:
                await event.answer("حدث خطأ", alert=True)
            except Exception:
                pass

    # ── تحويل الأزرار (بنية build_alert_buttons ← كائنات Telethon) ──
    @staticmethod
    def _convert_row(row: List[Dict[str, Any]]) -> List[Any]:
        """زر بلا بيانات صالحة يُحذف بصدق — لا أزرار ميتة أبداً."""
        out: List[Any] = []
        for b in row or []:
            text = str(b.get("text") or "").strip()
            if not text:
                continue
            url = str(b.get("url") or "").strip()
            if url:
                out.append(_mk_url_button(text, url))
                continue
            copy_spec = b.get("copy_text")
            if isinstance(copy_spec, dict) and str(copy_spec.get("text") or "").strip():
                out.append(_mk_copy_button(text, str(copy_spec["text"])))
                continue
            data = b.get("callback_data")
            if data:
                payload = data if isinstance(data, bytes) else str(data).encode()
                out.append(_mk_callback_button(text, payload[:64]))
            # بلا url/copy/callback → الزر يُسقط (لا وهمي)
        return out

    @staticmethod
    def _convert_markup(buttons: List[List[Dict[str, Any]]]) -> Optional[ReplyInlineMarkup]:
        rows = [_mk_row(r) for r in (AlertBotClient._convert_row(row) for row in buttons or []) if r]
        return ReplyInlineMarkup(rows=rows) if rows else None

    # ── الإرسال (طبقة احتياطية بالأزرار — v11.0) ──
    async def send_buttons(
        self,
        text_html: str,
        buttons: List[List[Dict[str, Any]]],
        target: Any = None,
    ) -> bool:
        """يرسل HTML + أزراراً حقيقية عبر عميل البوت. False = سلّم المسار
        التالي (حسابات المستخدمين). خطأ زر → إعادة بدون أزرار (النص بروابطه
        يبقى) قبل الاستسلام."""
        if not self.is_alive:
            return False
        chat = int(target) if target else self.target_chat_id
        if not chat:
            return False
        markup = self._convert_markup(buttons)
        try:
            await self.client.send_message(
                chat, text_html, parse_mode="html", buttons=markup, link_preview=False
            )
            return True
        except RPCError as e:
            msg = (getattr(e, "message", "") or type(e).__name__).upper()
            if any(code in msg for code in _BUTTON_RETRY_ERRORS) and markup is not None:
                logger.warning(f"⚠️ AlertBotClient button rejected ({msg[:80]}) — إعادة بدون أزرار")
                try:
                    await self.client.send_message(
                        chat, text_html, parse_mode="html", buttons=None, link_preview=False
                    )
                    return True
                except Exception:
                    return False
            logger.debug(f"AlertBotClient send failed: {type(e).__name__}: {str(e)[:120]}")
            return False
        except Exception as e:
            logger.debug(f"AlertBotClient send error: {type(e).__name__}: {str(e)[:120]}")
            return False
