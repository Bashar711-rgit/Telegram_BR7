#!/usr/bin/env python3
"""
reply_fix.py — الرد التعويضي F (v11.5 — مواصفة المستخدم §11)

الغرض:
  عند إرسال تنبيه عبر AlertBot لمرسل **بلا username**، يرد حساب المراقبة
  (owner_account — الحساب الذي استقبل الرسالة الأصلية) على التنبيه بكيان
  Text Mention فعلي (InputMessageEntityMentionName) مبني بـInputUser
  (user_id, access_hash) الخاص **بحساب المراقبة نفسه** — hash معزول لكل
  حساب، لا يُستخدم hash حساب في سياق آخر.

الضوابط المنفذة حرفياً (§11):
  * لا يُسقط التنبيه الأساسي أبداً — فشل F يُسجل فقط (فشل-آمن).
  * مرة واحدة لكل user_id في الساعة (REPLY_FIX_HOURLY_PER_USER).
  * حد يومي كلي قابل للضبط (REPLY_FIX_DAILY_CAP).
  * فاصل عشوائي 2–5 ثوانٍ (JITTER_MIN/JITTER_MAX) قبل الإرسال — في مهمة
    خلفية مستقلة، لا يحجب خط المعالجة.
  * الوجهة قناة بث (broadcast) → تعطيل تلقائي مع تسجيل السبب.
  * ChatWriteForbidden / SlowModeWait / FloodWait → تجاوز آمن بلا إعادة
    محاولة فورية (FloodWait يُسجل مدته).
  * التحقق الإلزامي: كيانات الرسالة المرسلة يجب أن تحوي
    MessageEntityMentionName — غيابه = FAILED (لا ادعاء نجاح).
  * لا يُنفَّذ F إلا لمرسل من نوع user/bot ولديه access_hash لحساب
    المراقبة المعني (المحذوف/القناة/المجهول → SKIP مسجَّل).

القيود الموثقة (حدود النظام لا ادعاءات):
  * خريطة التقييد بالساعة **بذاكرة العملية** — تعود صفرية بعد إعادة
    تشغيل العملية. المقاومة للتكرار عبر إعادة التشغيل محسّنة قدر الإمكان
    (لا يُعاد F إلا إذا مرت ساعة كاملة، وفشل الإرسال لا يحجز الحصة).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

from loguru import logger

try:
    from config import CFG
except Exception:  # pragma: no cover — الاستيراد المبكر في الاختبارات فقط
    CFG = None  # type: ignore[assignment]

from contact_target import metric_inc, utf16_len

# ── استيراد تيليجرام المحمي (نفس أسلوب alert_bot — فشل قديم جدول يتجاوز) ──
try:
    from telethon.errors import (
        ChatWriteForbiddenError,
        FloodWaitError,
        SlowModeWaitError,
    )
    _TELETHON_ERRORS = True
except Exception:  # pragma: no cover
    ChatWriteForbiddenError = FloodWaitError = SlowModeWaitError = None  # type: ignore[assignment]
    _TELETHON_ERRORS = False

try:
    from telethon.tl.functions.messages import SendMessageRequest
    from telethon.tl.types import (
        InputPeerUser,
        InputMessageEntityMentionName,
        InputUser,
        MessageEntityMentionName,
    )
    _TELETHON_TYPES = True
except Exception:  # pragma: no cover
    SendMessageRequest = None  # type: ignore[assignment]
    InputPeerUser = InputMessageEntityMentionName = None  # type: ignore[assignment]
    InputUser = MessageEntityMentionName = None  # type: ignore[assignment]
    _TELETHON_TYPES = False

__version__ = "11.5.0"

# نص الرد: إشارة شخص واحدة (المواصفة §11 — بديل الاسم القابل للنقر)
_REPLY_TEXT = "👤"
_REPLY_LEN_UTF16 = utf16_len(_REPLY_TEXT)


class ReplyFixer:
    """منفّذ الرد التعويضي F — بلا حالة عالمية، مقيد بالحدود، فشل-آمن."""

    def __init__(
        self,
        *,
        hourly_per_user: Optional[int] = None,
        daily_cap: Optional[int] = None,
        jitter_min: Optional[float] = None,
        jitter_max: Optional[float] = None,
        enabled: Optional[bool] = None,
        max_tracked_users: int = 4096,
    ) -> None:
        _cfg = CFG if CFG is not None else None

        def _val(name: str, default):
            if getattr(_cfg, name, None) is not None:
                return getattr(_cfg, name)
            return default

        self.enabled = bool(_val("REPLY_FIX_ENABLED", True) if enabled is None else enabled)
        self.hourly_per_user = max(1, int(
            hourly_per_user if hourly_per_user is not None
            else _val("REPLY_FIX_HOURLY_PER_USER", 1)
        ))
        self.daily_cap = max(0, int(
            daily_cap if daily_cap is not None else _val("REPLY_FIX_DAILY_CAP", 100)
        ))
        self.jitter_min = max(0.0, float(
            jitter_min if jitter_min is not None else _val("REPLY_FIX_JITTER_MIN", 2.0)
        ))
        self.jitter_max = max(self.jitter_min, float(
            jitter_max if jitter_max is not None else _val("REPLY_FIX_JITTER_MAX", 5.0)
        ))
        self._max_tracked_users = max(16, int(max_tracked_users))
        # السجل الزمني لكل user_id (ساعة دوارة) — bounded LRU بسيط
        self._hour_history: Dict[int, Deque[float]] = {}
        self._day_key: int = time.gmtime().tm_yday
        self._day_count: int = 0
        self._broadcast_disabled: bool = False

    # ─────────────────────────── الحدود ───────────────────────────
    def _rollover_day(self) -> None:
        today = time.gmtime().tm_yday
        if today != self._day_key:
            self._day_key = today
            self._day_count = 0

    def allowed(self, user_id: int) -> tuple[bool, str]:
        """هل يُسمح بF الآن؟ يعيد (القرار، السبب) — بلا آثار جانبية."""
        if not self.enabled:
            return False, "disabled"
        if self._broadcast_disabled:
            return False, "broadcast_target"
        self._rollover_day()
        if self._day_count >= self.daily_cap:
            return False, "daily_cap"
        now = time.monotonic()
        history = self._hour_history.get(int(user_id))
        if history is not None:
            hour_ago = now - 3600.0
            recent = sum(1 for ts in history if ts > hour_ago)
            if recent >= self.hourly_per_user:
                return False, "hourly_cap"
        return True, ""

    def _record_attempt(self, user_id: int) -> None:
        """يحجز الحصة — يُستدعى فقط قبل الإرسال الفعلي (فشل الإرسال
        المسبق عبر allowed() لا يحجز شيئاً)."""
        uid = int(user_id)
        history = self._hour_history.get(uid)
        if history is None:
            if len(self._hour_history) >= self._max_tracked_users:
                # إخلاء أقدم مفتاح (dict مرتب بالإدراج — أرخص إخلاء ممكن)
                try:
                    self._hour_history.pop(next(iter(self._hour_history)))
                except Exception:
                    pass
            history = deque(maxlen=max(8, self.hourly_per_user * 4))
            self._hour_history[uid] = history
        history.append(time.monotonic())
        self._rollover_day()
        self._day_count += 1

    def disable_for_broadcast(self, reason: str = "") -> None:
        """تعطيل تلقائي: وجهة التنبيه قناة بث لا تقبل الردود المناسبة."""
        self._broadcast_disabled = True
        logger.warning(f"[ALERT] reply_fix=SKIP reason=broadcast_target ({reason[:80]})")
        metric_inc("reply_fix_skipped")

    # ─────────────────────── التنفيذ الفعلي ───────────────────────
    async def run(
        self,
        client: Any,
        *,
        chat_id: int,
        reply_to_msg_id: Optional[int],
        user_id: int,
        access_hash: Optional[int],
        owner_account: str = "",
    ) -> str:
        """ينفذ F كاملاً. يعيد OK | FAIL:سبب | SKIP:سبب — لا يرفع استثناءً.

        client = عميل تيليجرام لحساب المراقبة المالك للـaccess_hash."""
        uid = int(user_id or 0)
        if not _TELETHON_TYPES:
            metric_inc("reply_fix_skipped")
            return "SKIP:telethon_unavailable"

        # 1) حالة المرسل — لا mention لمحذوف/قناة/بلا hash (مواصفة §11)
        if uid <= 0:
            metric_inc("reply_fix_skipped")
            return "SKIP:no_user_id"
        if access_hash is None:
            metric_inc("reply_fix_skipped")
            return "SKIP:no_access_hash"

        # 2) الحدود (ساعة/يوم/تعطيل)
        ok, why = self.allowed(uid)
        if not ok:
            metric_inc("reply_fix_skipped")
            return f"SKIP:{why}"

        if client is None:
            metric_inc("reply_fix_skipped")
            return "SKIP:no_client"

        # 3) فاصل عشوائي 2–5 ث (مواصفة §11) — خارج المسار الحرج أصلاً
        try:
            await asyncio.sleep(random.uniform(self.jitter_min, self.jitter_max))
        except asyncio.CancelledError:
            raise

        # 4) بناء الكيان — InputUser بحساب المراقبة نفسه حصراً
        try:
            entity = InputMessageEntityMentionName(
                offset=0, length=_REPLY_LEN_UTF16,
                user_id=InputUser(user_id=uid, access_hash=int(access_hash)),
            )
        except Exception as e:
            metric_inc("reply_fix_failure")
            return f"FAIL:entity_build:{type(e).__name__}"

        # 5) الإرسال كرد على التنبيه
        try:
            self._record_attempt(uid)
            if reply_to_msg_id:
                sent = await client.send_message(
                    chat_id, _REPLY_TEXT,
                    reply_to=reply_to_msg_id,
                    formatting_entities=[entity],
                )
            else:
                sent = await client.send_message(
                    chat_id, _REPLY_TEXT,
                    formatting_entities=[entity],
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            ename = type(e).__name__
            if _TELETHON_ERRORS and isinstance(e, (ChatWriteForbiddenError, SlowModeWaitError)):
                # تجاوز آمن — بلا إعادة محاولة فورية (مواصفة §11)
                metric_inc("reply_fix_skipped")
                return f"SKIP:write_forbidden_or_slowmode:{ename}"
            if _TELETHON_ERRORS and isinstance(e, FloodWaitError):
                metric_inc("reply_fix_skipped")
                return f"SKIP:floodwait_{int(getattr(e, 'seconds', 0) or 0)}"
            # الخطأ غير المتوقع: الحجز تم — يُحسب من الحصة (منع التكرار)
            metric_inc("reply_fix_failure")
            return f"FAIL:{ename}:{str(e)[:80]}"

        # 6) التحقق الإلزامي — كيان MessageEntityMentionName في المرسل فعلاً
        sent_entities = list(getattr(sent, "entities", None) or [])
        has_mention = any(
            type(e).__name__ == "MessageEntityMentionName" for e in sent_entities
        )
        if not has_mention:
            logger.warning(
                f"[ALERT] reply_fix=FAIL reason=entity_missing "
                f"(mention لم يصل) owner={owner_account}"
            )
            metric_inc("reply_fix_failure")
            return "FAIL:entity_missing"

        logger.info(
            f"[ALERT] reply_fix=OK owner={owner_account} (mention delivered)"
        )
        metric_inc("reply_fix_success")
        return "OK"


# ── Singleton مودولي (نفس نمط get_antispam) ──────────────────────────
_instance: Optional[ReplyFixer] = None


def get_reply_fixer() -> ReplyFixer:
    global _instance
    if _instance is None:
        _instance = ReplyFixer()
    return _instance


def reset_reply_fixer() -> None:
    """للاختبارات — يعيد بناء الـsingleton بالإعدادات الحالية."""
    global _instance
    _instance = None
