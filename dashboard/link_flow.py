"""
panel/link_flow.py — آلة حالة ربط حساب تيليجرام جديد (v14.10.0).

تسلسل الإضافة (مواصفة المالك):
    إدخال رقم الهاتف ← رمز التأكيد ← كلمة المرور (2FA عند الحاجة فقط)
    ← التحقق من الحساب (get_me + صلاحية الجلسة + منع التكرار)
    ← حفظ الجلسة مشفّرة في DB ← تفعيل المراقبة على الحلقة الرئيسية.

الحالات: linking / waiting_code / waiting_password / verifying / ready
         / active / error (+ cancelled عابرة)

ضوابط أمنية وتشغيلية صارمة:
* رمز التأكيد وكلمة المرور وphone_code_hash تعيش في الذاكرة داخل العملية
  فقط — تُمحى فور الاستخدام أو الإلغاء، ولا تُسجَّل ولا تُخزَّن أبداً.
* عميل Telethon مؤقت واحد لكل عملية ربط (وليس لكل طلب HTTP) — يُدار
  مركزياً هنا ويُغلق ويُحرَّر عند الإلغاء/الانتهاء/انتهاء TTL.
* معالجة FloodWait مع إرجاع عدد الثواني للواجهة، وحدود صارمة للمحاولات:
  3 محاولات رمز + 3 محاولات كلمة مرور لكل عملية، وتهدئة لكل رقم بعد
  البدء الفاشل، وحد أقصى للعمليات المتزامنة.
* لا يُكتب أي صف في قاعدة البيانات إلا بعد نجاح المصادقة الكاملة
  (get_me + is_user_authorized + استخراج Session String) — فشل الإضافة
  لا يترك حساباً غير مكتمل.
* Session String لا يعود للواجهة أبداً — لا في الاستجابة ولا في السجل.

ملاحظة معمارية: كل دوال هذه الوحدة تعمل على حلقة خادم الصحة (thread
اللوحة). عمليات DB والتفعيل على الحلقة الرئيسية تصل عبر callbacks
غير متزامنة تمرّرها app.py (run_coroutine_threadsafe) — هذه الوحدة
لا تعرف شيئاً عن الخيوط ولا عن قاعدة البيانات مباشرة.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PasswordHashInvalidError,
    SessionPasswordNeededError,
)

logger = logging.getLogger(__name__)

# ═══ قيم الحالات (تُترجم عربياً في الواجهة) ═══
STATE_LINKING = "linking"
STATE_WAITING_CODE = "waiting_code"
STATE_WAITING_PASSWORD = "waiting_password"
STATE_VERIFYING = "verifying"
STATE_READY = "ready"
STATE_ACTIVE = "active"
STATE_ERROR = "error"

MAX_CODE_ATTEMPTS = 3
MAX_PASSWORD_ATTEMPTS = 3
MAX_CONCURRENT_OPS = 2          # عمليتا ربط كحد أقصى معاً (خفة الموارد)
OP_TTL_SECONDS = 900            # 15 دقيقة ثم تنظيف تلقائي
START_COOLDOWN_PER_PHONE = 45   # ثوانٍ بين محاولتي بدء لنفس الرقم
FAILED_START_WINDOW = 600       # نافذة عدّ البدوات الفاشلة (10 دقائق)
FAILED_START_LIMIT = 3          # 3 بدوات فاشلة في النافذة → حظر 10 دقائق

_PHONE_RE = re.compile(r"^\d{8,15}$")

# ═══ رسائل أخطاء عربية واضحة (لا تكشف تفاصيل داخلية ولا أسراراً) ═══
ERRORS_AR: Dict[str, str] = {
    "phone_invalid": "رقم الهاتف غير صالح — اختر مفتاح الدولة وأدخل الرقم دون رمز الدولة مكرراً",
    "phone_duplicate": "هذا الحساب مضاف مسبقاً في النظام",
    "no_api_creds": "لا توجد بيانات API — أضف MAIN_API_ID و MAIN_API_HASH في متغيرات البيئة أولاً",
    "too_many_ops": "هناك عملية ربط قائمة بالفعل — أكملها أو ألغِها أولاً",
    "rate_limited": "محاولات كثيرة على هذا الرقم — انتظر قبل إعادة المحاولة",
    "phone_invalid_tg": "الرقم غير صالح لدى تيليجرام — تأكد من صيغة الرقم الدولية",
    "phone_banned": "الرقم محظور لدى تيليجرام",
    "phone_unoccupied": "الرقم غير مسجل في تيليجرام — أنشئ حساباً به أولاً ثم أعد الربط",
    "api_id_invalid": "بيانات API_ID/API_HASH غير صالحة — راجع متغيرات البيئة",
    "code_invalid": "الرمز غير صحيح — تحقق من الرمز المرسل في تيليجرام وأعد المحاولة",
    "code_expired": "انتهت صلاحية رمز التأكيد — أبدأ عملية الربط من جديد لتوليد رمز آخر",
    "code_empty": "أدخل رمز التأكيد",
    "password_invalid": "كلمة المرور غير صحيحة — أعد المحاولة",
    "flood_wait": "تجاوزت حد المحاولات لدى تيليجرام — انتظر المدة المحددة ثم أعد المحاولة",
    "auth_failed": "فشل تسجيل الدخول — أعد المحاولة",
    "session_failed": "تعذّر حفظ الجلسة — أعد المحاولة",
    "duplicate_after_auth": "الحساب أُضيف أثناء العملية من مصدر آخر — أُلغيت العملية",
    "network": "تعذّر الاتصال بخوادم تيليجرام — تحقق من الشبكة وأعد المحاولة",
    "timeout": "انتهت مهلة العملية — أعد المحاولة",
    "cancelled": "أُلغيت العملية",
    "unknown": "حدث خطأ غير متوقع — أعد المحاولة",
}


def error_message(code: str) -> str:
    return ERRORS_AR.get(code, ERRORS_AR["unknown"])


def _err(code: str, flood_wait: int = 0, **extra: Any) -> Dict[str, Any]:
    """استجابة فشل موحدة: error_code + رسالة عربية جاهزة للواجهة."""
    out: Dict[str, Any] = {
        "ok": False,
        "error_code": code,
        "error_message": error_message(code),
    }
    if flood_wait:
        out["flood_wait"] = int(flood_wait)
    out.update(extra)
    return out


def normalize_phone(raw: Any) -> Optional[str]:
    """توحيد الرقم الدولي: أرقام فقط بلا رموز/فراغات، 8-15 رقماً، مع '+'."""
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not _PHONE_RE.match(digits):
        return None
    if digits.startswith("00"):  # صيغة 00XXX الدولية → أزل البادئة
        digits = digits[2:]
        if not (8 <= len(digits) <= 15):
            return None
    return "+" + digits


def _norm_key(phone: str) -> str:
    """مفتاح مقارنة الرقم (منع التكرار): أرقام فقط."""
    return re.sub(r"\D", "", phone or "")


@dataclass
class LinkOperation:
    """عملية ربط واحدة — تعيش في الذاكرة فقط (لا DB إلا عند النجاح)."""
    op_id: str
    phone: str
    state: str = STATE_LINKING
    error_code: Optional[str] = None
    flood_wait: int = 0
    attempts_code: int = 0
    attempts_password: int = 0
    phone_code_hash: Optional[str] = field(default=None, repr=False)  # سر عابر
    client: Any = field(default=None, repr=False)                     # عميل مؤقت
    api_creds: Optional[Dict[str, Any]] = field(default=None, repr=False)  # api_id/hash المستخدمان
    me: Optional[Dict[str, Any]] = None
    account_row_id: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()

    def wipe_secrets(self) -> None:
        """محو أسرار المصادقة العابرة (رمز/كلمة مرور/hash) من العملية."""
        self.phone_code_hash = None

    def view(self) -> Dict[str, Any]:
        """لقطة آمنة للواجهة — بلا عملاء ولا أسرار ولا hash."""
        return {
            "op_id": self.op_id,
            "state": self.state,
            "error_code": self.error_code,
            "error_message": error_message(self.error_code) if self.error_code else None,
            "flood_wait": self.flood_wait,
            "attempts_left_code": max(0, MAX_CODE_ATTEMPTS - self.attempts_code),
            "attempts_left_password": max(0, MAX_PASSWORD_ATTEMPTS - self.attempts_password),
            "phone": self.phone,
            "me": dict(self.me) if self.me else None,
            "account_row_id": self.account_row_id,
            "age_seconds": int(time.time() - self.created_at),
        }


class AccountLinkManager:
    """
    مدير مركزي لعمليات ربط الحسابات — يعيش على حلقة خادم الصحة.

    callbacks (من app.py — كلها تعمل فعلياً على الحلقة الرئيسية):
      * find_existing_cb(phone, tg_user_id) -> Optional[dict]  فحص التكرار في DB
      * save_account_cb(payload) -> Optional[int]              حفظ الحساب المشفّر → row_id
      * activate_cb(row_id, payload) -> None                   تفعيل المراقبة (spawn monitor)
    """

    def __init__(
        self,
        find_existing_cb: Callable[[str, Optional[int]], Awaitable[Optional[Dict[str, Any]]]],
        save_account_cb: Callable[[Dict[str, Any]], Awaitable[Optional[int]]],
        activate_cb: Callable[[int, Dict[str, Any]], Awaitable[None]],
        excluded_env_phones_cb: Optional[Callable[[], List[str]]] = None,
    ) -> None:
        self._ops: Dict[str, LinkOperation] = {}
        self._failed_starts: Dict[str, List[float]] = {}
        self._blocked_until: Dict[str, float] = {}
        self._sweeper_task: Optional[asyncio.Task] = None
        self._find_existing_cb = find_existing_cb
        self._save_account_cb = save_account_cb
        self._activate_cb = activate_cb
        # v16.2.0: أرقام env المستبعدة نهائياً — يُسمح بإعادة تسجيلها
        self._excluded_env_phones_cb = excluded_env_phones_cb

    # ─── دورة حياة المدير ────────────────────────────────────────────────

    def start_sweeper(self) -> None:
        """مهمة تنظيف دورية (كل 60s): إغلاق العمليات المنتهية وتحرير العملاء."""
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return

        async def _loop() -> None:
            while True:
                try:
                    await asyncio.sleep(60)
                    await self._cleanup_expired()
                except asyncio.CancelledError:
                    return
                except Exception as e:  # التنظيف لا يسقط أبداً
                    logger.debug(f"link sweeper error: {type(e).__name__}: {e}")

        self._sweeper_task = asyncio.ensure_future(_loop())

    async def stop(self) -> None:
        """إيقاف المدير (إخراج نظيف): إلغاء كل العمليات + إيقاف التنظيف."""
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            try:
                await self._sweeper_task
            except asyncio.CancelledError:
                pass
            self._sweeper_task = None
        for op_id in list(self._ops.keys()):
            await self.cancel(op_id)

    async def _cleanup_expired(self) -> None:
        now = time.time()
        expired = [oid for oid, op in self._ops.items() if now - op.updated_at > OP_TTL_SECONDS]
        for oid in expired:
            logger.info(f"Link op {oid[:8]} expired (TTL {OP_TTL_SECONDS}s) — cleaning up")
            await self.cancel(oid, reason="timeout")
        # تنظيف عدّادات البدوات الفاشلة القديمة
        for phone in list(self._failed_starts.keys()):
            self._failed_starts[phone] = [t for t in self._failed_starts[phone] if now - t < FAILED_START_WINDOW]
            if not self._failed_starts[phone]:
                self._failed_starts.pop(phone, None)
        for phone, until in list(self._blocked_until.items()):
            if now >= until:
                self._blocked_until.pop(phone, None)

    # ─── فحوصات مشتركة ──────────────────────────────────────────────────

    def _api_creds(self) -> Optional[Dict[str, Any]]:
        """api_id/api_hash من حسابات البيئة القائمة (أو PANEL_API_ID env)."""
        import os

        try:
            api_id = int(os.getenv("PANEL_API_ID", "") or 0)
        except ValueError:
            api_id = 0
        api_hash = (os.getenv("PANEL_API_HASH", "") or "").strip()
        if api_id and api_hash:
            return {"api_id": api_id, "api_hash": api_hash}
        try:
            from config import ACCOUNTS

            for acc in ACCOUNTS:
                if acc.get("api_id") and acc.get("api_hash"):
                    return {"api_id": int(acc["api_id"]), "api_hash": str(acc["api_hash"])}
        except Exception as e:
            logger.debug(f"api creds from ACCOUNTS failed: {type(e).__name__}")
        return None

    def _env_account_phones(self) -> List[str]:
        """أرقام حسابات البيئة (لمنع التكرار مع مصدر env).
        v16.2.0: المستبعد نهائياً من اللوحة لا يُحسب مكرراً — حتى يمكن
        حذف الحساب الميت ثم إعادة تسجيل رقمه نفسه."""
        try:
            from config import ACCOUNTS

            excluded: set = set()
            try:
                excluded = {re.sub(r"\D", "", str(x))
                            for x in (self._excluded_env_phones_cb() or [])}
            except Exception:
                excluded = set()
            return [
                _norm_key(a.get("phone", "")) for a in ACCOUNTS
                if a.get("phone") and _norm_key(a.get("phone", "")) not in excluded
            ]
        except Exception:
            return []

    def _live_monitor_phones(self, bot: Any) -> List[str]:
        """أرقام المراقبين الأحياء (env + panel) — منع تكرار كامل."""
        out: List[str] = []
        try:
            for m in list(getattr(bot, "monitors", []) or []):
                phone = (getattr(m, "account", {}) or {}).get("phone")
                if phone:
                    out.append(_norm_key(phone))
        except Exception:
            pass
        return out

    def _check_rate(self, phone_key: str) -> Optional[str]:
        """حدود المعدل لبدء عملية: حظر الترقيم المتكرر الفاشل."""
        now = time.time()
        until = self._blocked_until.get(phone_key)
        if until and now < until:
            return "rate_limited"
        fails = [t for t in self._failed_starts.get(phone_key, []) if now - t < FAILED_START_WINDOW]
        if len(fails) >= FAILED_START_LIMIT:
            self._blocked_until[phone_key] = now + FAILED_START_WINDOW
            return "rate_limited"
        return None

    def _record_failed_start(self, phone_key: str) -> None:
        self._failed_starts.setdefault(phone_key, []).append(time.time())

    # ─── الخطوة 1: إدخال رقم الهاتف وإرسال الرمز ─────────────────────────

    async def start(self, bot: Any, phone_raw: Any) -> Dict[str, Any]:
        phone = normalize_phone(phone_raw)
        if phone is None:
            return _err("phone_invalid")
        phone_key = _norm_key(phone)

        rate = self._check_rate(phone_key)
        if rate:
            return _err(rate, flood_wait=int(self._blocked_until.get(phone_key, 0) - time.time()))

        # حد العمليات المتزامنة
        active_ops = [op for op in self._ops.values() if op.state not in (STATE_ERROR,)]
        if len(active_ops) >= MAX_CONCURRENT_OPS:
            return _err("too_many_ops")

        # منع التكرار: env + مراقبين أحياء + DB
        if phone_key in self._env_account_phones() or phone_key in self._live_monitor_phones(bot):
            return _err("phone_duplicate")
        try:
            existing = await self._find_existing_cb(phone, None)
            if existing:
                return _err("phone_duplicate")
        except Exception as e:
            logger.warning(f"find_existing_cb failed (fail-open to Telegram check): {type(e).__name__}")

        creds = self._api_creds()
        if creds is None:
            return _err("no_api_creds")

        op = LinkOperation(op_id=secrets.token_hex(12), phone=phone, api_creds=creds)
        self._ops[op.op_id] = op

        client = None
        try:
            from telethon.sessions import StringSession

            client = TelegramClient(StringSession(), **_client_kwargs(creds))
            await asyncio.wait_for(client.connect(), timeout=30.0)
            sent = await asyncio.wait_for(client.send_code_request(phone), timeout=30.0)
            op.phone_code_hash = getattr(sent, "phone_code_hash", None)
            if not op.phone_code_hash:
                raise RuntimeError("empty phone_code_hash")
            op.client = client
            op.state = STATE_WAITING_CODE
            op.touch()
            logger.info(f"Link op {op.op_id[:8]}: code sent to {phone[:4]}***{phone[-2:]} (state=waiting_code)")
            return {"ok": True, "op": op.view()}
        except Exception as e:
            # إغلاق العميل المؤقت فوراً — العملية الفاشلة لا تبقي موارد
            try:
                if client is not None:
                    await asyncio.wait_for(client.disconnect(), timeout=5.0)
            except Exception:
                pass
            self._ops.pop(op.op_id, None)
            self._record_failed_start(phone_key)
            code, flood = _map_exception(e)
            logger.warning(f"Link start failed for {phone[:4]}***: {type(e).__name__} → {code} (flood={flood})")
            return _err(code, flood_wait=flood)

    # ─── الخطوة 2: رمز التأكيد ───────────────────────────────────────────

    async def submit_code(self, op_id: str, code: str) -> Dict[str, Any]:
        op = self._ops.get(op_id)
        if op is None:
            return _err("cancelled")
        if op.state != STATE_WAITING_CODE:
            return _err(_stale_state_code(op), op=op.view())
        code = re.sub(r"\D", "", str(code or ""))
        if not code:
            return _err("code_empty", op=op.view())

        op.attempts_code += 1
        op.touch()
        try:
            assert op.client is not None and op.phone_code_hash
            await asyncio.wait_for(
                op.client.sign_in(phone=op.phone, code=code, phone_code_hash=op.phone_code_hash),
                timeout=30.0,
            )
            # نجاح دون 2FA → التحقق النهائي
            return await self._finalize(op)
        except SessionPasswordNeededError:
            # الخطوة 3: الحساب محمي بخطوتين — طلب كلمة المرور
            op.state = STATE_WAITING_PASSWORD
            op.touch()
            logger.info(f"Link op {op.op_id[:8]}: 2FA required (state=waiting_password)")
            return {"ok": True, "op": op.view()}
        except (PhoneCodeInvalidError, PhoneCodeEmptyError):
            if op.attempts_code >= MAX_CODE_ATTEMPTS:
                await self.cancel(op_id, reason="code attempts exhausted")
                return _err("code_invalid", exhausted=True)
            op.touch()
            return _err("code_invalid", op=op.view())
        except PhoneCodeExpiredError:
            await self.cancel(op_id, reason="code expired")
            return _err("code_expired")
        except Exception as e:
            code_, flood = _map_exception(e)
            if code_ in ("flood_wait",) and flood and flood <= 120:
                # FloodWait قصير على sign_in → نبقي العملية صالحة ونخبر
                # المستخدم بالانتظار ثم يعيد إدخال الرمز (بلا إعادة إرسال رمز).
                op.touch()
                op.flood_wait = flood
                return _err(code_, flood_wait=flood, op=op.view())
            await self.cancel(op_id, reason=f"sign_in failed: {type(e).__name__}")
            return _err(code_, flood_wait=flood)

    # ─── الخطوة 3: كلمة المرور (2FA) ─────────────────────────────────────

    async def submit_password(self, op_id: str, password: str) -> Dict[str, Any]:
        op = self._ops.get(op_id)
        if op is None:
            return _err("cancelled")
        if op.state != STATE_WAITING_PASSWORD:
            return _err(_stale_state_code(op), op=op.view())
        if not password or len(str(password)) > 512:
            return _err("password_invalid", op=op.view())

        op.attempts_password += 1
        op.touch()
        try:
            assert op.client is not None
            await asyncio.wait_for(op.client.sign_in(password=str(password)), timeout=30.0)
            # كلمة المرور صحيحة → التحقق النهائي فوراً. المرجع المحلي
            # لكلمة المرور يهدم مع خروج الدالة — لا تُخزَّن في أي حقل.
            return await self._finalize(op)
        except PasswordHashInvalidError:
            if op.attempts_password >= MAX_PASSWORD_ATTEMPTS:
                await self.cancel(op_id, reason="password attempts exhausted")
                return _err("password_invalid", exhausted=True)
            op.touch()
            return _err("password_invalid", op=op.view())
        except Exception as e:
            code_, flood = _map_exception(e)
            await self.cancel(op_id, reason=f"password sign_in failed: {type(e).__name__}")
            return _err(code_, flood_wait=flood)

    # ─── الخطوة 4: التحقق من الحساب وحفظ الجلسة ──────────────────────────

    async def _finalize(self, op: LinkOperation) -> Dict[str, Any]:
        """التحقق النهائي: get_me + صلاحية الجلسة + منع التكرار + حفظ مشفّر."""
        op.state = STATE_VERIFYING
        op.wipe_secrets()  # الرمز/الـhash لم تعد مطلوبة — تُمحى فوراً
        op.touch()
        try:
            assert op.client is not None
            me = await asyncio.wait_for(op.client.get_me(), timeout=20.0)
            if not await asyncio.wait_for(op.client.is_user_authorized(), timeout=15.0):
                raise RuntimeError("not authorized after sign_in")

            tg_user_id = int(getattr(me, "id", 0) or 0)
            username = (getattr(me, "username", None) or None)
            first = (getattr(me, "first_name", None) or "").strip()
            last = (getattr(me, "last_name", None) or "").strip()
            display_name = (first + (" " + last if last else "")).strip() or f"حساب {tg_user_id}"

            # منع إضافة الحساب نفسه مرتين (بالرقم ومعرّف تيليجرام)
            try:
                dup = await self._find_existing_cb(op.phone, tg_user_id if tg_user_id else None)
                if dup:
                    await self.cancel(op.op_id, reason="duplicate after auth")
                    return _err("duplicate_after_auth")
            except Exception as e:
                logger.warning(f"finalize duplicate check failed: {type(e).__name__} — continuing (saved by phone UNIQUE)")

            # استخراج Session String ثم إغلاق العميل المؤقت فوراً
            session_string = op.client.session.save()
            if not session_string or not isinstance(session_string, str):
                raise RuntimeError("empty session string")
            try:
                await asyncio.wait_for(op.client.disconnect(), timeout=10.0)
            except Exception:
                pass
            op.client = None  # آخر مرجع — يُحرَّر بعد الخروج من هنا

            payload = {
                "phone": op.phone,
                "display_name": display_name,
                "username": username,
                "tg_user_id": tg_user_id,
                "session_string": session_string,
                "api_id": int((op.api_creds or {}).get("api_id") or 0),
                "api_hash": str((op.api_creds or {}).get("api_hash") or ""),
            }
            del session_string  # المرجع المحلي يهدم — النسخة الحية داخل payload فقط

            row_id = await self._save_account_cb(payload)
            if not row_id:
                op.state = STATE_ERROR
                op.error_code = "session_failed"
                op.touch()
                return _err("session_failed")

            op.me = {
                "id": tg_user_id,
                "name": display_name,
                "username": username,
                "phone": op.phone,
            }
            op.account_row_id = int(row_id)
            op.state = STATE_READY
            op.touch()
            logger.info(f"Link op {op.op_id[:8]}: account saved row={row_id} (state=ready) — activating")

            # تفعيل المراقبة على الحلقة الرئيسية (تفعيل خيار المراقبة)
            try:
                await self._activate_cb(int(row_id), dict(payload))
            except Exception as act_err:
                logger.warning(f"activate_cb failed for row {row_id}: {type(act_err).__name__}: {act_err}")

            return {"ok": True, "op": op.view()}
        except Exception as e:
            code_, flood = _map_exception(e)
            await self.cancel(op.op_id, reason=f"finalize failed: {type(e).__name__}")
            return _err(code_, flood_wait=flood)

    # ─── الإلغاء والاستعلام ──────────────────────────────────────────────

    async def cancel(self, op_id: str, reason: str = "user cancel") -> Dict[str, Any]:
        """إلغاء العملية المؤقتة دون ترك أي أثر: إغلاق العميل + محو الأسرار."""
        op = self._ops.pop(op_id, None)
        if op is None:
            return {"ok": True, "cancelled": False}
        client = op.client
        op.client = None
        op.wipe_secrets()
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=10.0)
            except Exception:
                pass
        if op.state not in (STATE_READY, STATE_ACTIVE, STATE_ERROR):
            op.state = STATE_ERROR
            op.error_code = "cancelled"
        if reason not in ("user cancel", "timeout"):
            self._record_failed_start(_norm_key(op.phone))
        logger.info(f"Link op {op_id[:8]} cancelled ({reason}) — temp client closed, secrets wiped")
        return {"ok": True, "cancelled": True}

    def status(self, op_id: str) -> Optional[Dict[str, Any]]:
        op = self._ops.get(op_id)
        return op.view() if op else None

    def mark_active(self, row_id: int) -> None:
        """تفعيل المراقبة نجح (spawn monitor) — العملية تصير نشطة."""
        for op in self._ops.values():
            if op.account_row_id == row_id:
                op.state = STATE_ACTIVE
                op.touch()
                break

    def mark_error(self, row_id: int, error_text: str) -> None:
        """فشل التفعيل بعد الحفظ — الحساب محفوظ (جلسة صالحة) ويحتاج إعادة اتصال."""
        for op in self._ops.values():
            if op.account_row_id == row_id:
                op.state = STATE_ERROR
                op.error_code = "activation_failed"
                op.touch()
                break

    def active_count(self) -> int:
        return len([op for op in self._ops.values() if op.state not in (STATE_ERROR,)])


def _stale_state_code(op: LinkOperation) -> str:
    return {
        STATE_WAITING_PASSWORD: "awaiting_password",
        STATE_VERIFYING: "verifying",
        STATE_READY: "already_ready",
        STATE_ACTIVE: "already_ready",
        STATE_ERROR: "already_failed",
    }.get(op.state, "invalid_state")


def _client_kwargs(creds: Dict[str, Any]) -> Dict[str, Any]:
    """مواصفات عميل المؤقت — أخف من عميل المراقبة (بلا catch_up)."""
    return dict(
        api_id=int(creds["api_id"]),
        api_hash=str(creds["api_hash"]),
        device_model="BR7 Panel",
        system_version="Linux",
        app_version="14.10.0",
        timeout=15,
        connection_retries=2,
        retry_delay=2,
        auto_reconnect=False,
        catch_up=False,
        sequential_updates=False,
    )


def _map_exception(e: Exception) -> tuple:
    """ترجمة استثناءات تيليجرام → (error_code, flood_wait_seconds).

    isinstance على مستوى الفئات (أدق من مقارنة الأسماء — يلتقط الفئات
    الفرعية أيضاً)، والاستيراد داخل الدالة حتى لا يثقل الاستيراد المبكر.
    """
    flood = int(getattr(e, "seconds", 0) or 0)
    try:
        from telethon.errors import (
            ApiIdInvalidError,
            FloodWaitError,
            PasswordHashInvalidError,
            PhoneCodeEmptyError,
            PhoneCodeExpiredError,
            PhoneCodeInvalidError,
            PhoneNumberBannedError,
            PhoneNumberInvalidError,
            PhoneNumberUnoccupiedError,
        )

        if isinstance(e, FloodWaitError):
            return "flood_wait", flood
        if isinstance(e, PhoneNumberInvalidError):
            return "phone_invalid_tg", 0
        if isinstance(e, PhoneNumberBannedError):
            return "phone_banned", 0
        if isinstance(e, PhoneNumberUnoccupiedError):
            return "phone_unoccupied", 0
        if isinstance(e, ApiIdInvalidError):
            return "api_id_invalid", 0
        if isinstance(e, PhoneCodeInvalidError):
            return "code_invalid", 0
        if isinstance(e, PhoneCodeExpiredError):
            return "code_expired", 0
        if isinstance(e, PhoneCodeEmptyError):
            return "code_empty", 0
        if isinstance(e, PasswordHashInvalidError):
            return "password_invalid", 0
        if isinstance(e, SessionPasswordNeededError):
            return "password_required", 0
    except Exception:
        pass
    if isinstance(e, asyncio.TimeoutError):
        return "timeout", 0
    if isinstance(e, (ConnectionError, OSError)):
        return "network", 0
    return "unknown", flood
