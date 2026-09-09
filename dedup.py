#!/usr/bin/env python3
"""
dedup.py — Cross-Account Alert Deduplication v1.0 (v9.10)

المشكلة التي يحلها هذا الملف:
  * نفس الرسالة تصل عبر الحسابات الستة (6 مراقبين) → يجب تنبيه واحد فقط.
  * نفس المرسل يعيد إرسال نفس النص (في نفس المجموعة أو مجموعة أخرى أو
    برسالة جديدة برقم مختلف) → يجب ألا يتكرر التنبيه.
  * التكرار قد يحدث عبر عمليات مراقبة مختلفة وعبر إعادة تشغيل العملية
    → الحماية يجب أن تكون في الذاكرة (سريعة) وفي قاعدة البيانات (دائمة).

التصميم — طبقتان:

  L1 (ذاكرة):  مجموعة أحادية الترابط في الـ event loop — العمليات
               المتزامنة (claim متزامن من workers متعددة) آمنة لأن
               check-then-act تتم بشكل متزامن صرف (بدون أي await بين
               الفحص والحجز)، وهو نفس أسلوب FastCaptureBuffer.
               LRU بسقف ثابت (DEDUP_MEM_MAX) مع كنس lazy للنافذة.

  L2 (قاعدة البيانات): جدول alert_dedup (fingerprint PRIMARY KEY) مع
               INSERT OR IGNORE — العملية آتومية عبر كل الـ workers
               وحتى عبر عمليات متزامنة (SQLite serialized writes،
               PostgreSQL ON CONFLICT DO NOTHING). تنجو من إعادة
               التشغيل والنشر (تحقق شرط "عند إعادة التشغيل تبقى
               الحماية قائمة" ضمن نافذة الـ TTL).

بصمة الرسالة (content fingerprint):
  fast_hash("v1:{sender_id}:{normalized_text}")
  * sender_id مُضمن: مرسلان مختلفان بنفس النص = طلبان مختلفان يجب أن
    يصلا (لا يُحذفان) — بينما نفس المرسل بنفس النص = مكرر.
  * normalized_text: تطبيع مضغوط (بدون تأثير على الفلتر):
    lower + إزالة التشكيل والتطويل + توحيد أ/إ/آ→ا وة→ه وى→ي وئ/ؤ
    + تقليص تكرار الحروف العربية (2+ → 1) + تقليص المسافات.
    "مرررررا" و"مررا" لنفس المرسل = نفس البصمة، بينما "ابي حل" و
    "ابي حلل" يبقيان مختلفتين (حماية متطلب "المتشابهة جزئياً لا تُحذف").

فشل-آمن:
  * أي خطأ في DB → نثق بـ L1 فقط ونعيد True (التنبيه الأول يمر، والذاكرة
    تمنع تكرار الحسابات خلال العملية) — لا يُفقد تنبيه بسبب مشكلة حفظ.
  * DEDUP_ENABLED=false (أو من اللوحة) → تعطيل كامل فوري.
  * النافذة الزمنية DEDUP_WINDOW_SECONDS قابلة للتعديل حياً من اللوحة
    (تُقرأ من CFG عند كل claim — تطبيق فوري بدون إعادة تشغيل).
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

from loguru import logger

from config import CFG, fast_hash

# ─────────────────────────────────────────────────────────────────────────────
# Fingerprint normalization (dedup-only — أقوى من تطبيع الفلتر ومقصوص عليه)
# ─────────────────────────────────────────────────────────────────────────────
_FP_DIACRITICS: "re.Pattern[str]" = re.compile(r"[\u064B-\u065F\u0670\u0640]")
# الحركات + السكون + التطويل (tatweel)
_FP_LETTER_MAP = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي", "ئ": "ي", "ؤ": "و",
})
# تقليص تكرار الحروف العربية فقط (3+ → 1) — "ههههه"→"ه"، "مرررررا"→"مرا".
# ⚠️ 3+ وليس 2+ عمداً: "حلل" و"حلاا" (كلمات مشروعة بتكرار مزدوج) يجب أن
# تبقى بصمتها مختلفة عن "حل" — متطلب "المتشابهة جزئياً لا تُحذف".
_FP_AR_REPEATS: "re.Pattern[str]" = re.compile(r"([\u0621-\u064A])\1{2,}")
_FP_WS: "re.Pattern[str]" = re.compile(r"\s+")

# أرقام عربية-هندية → لاتينية (توحيد "٥" و"5")
_FP_DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def normalize_for_fingerprint(text: str) -> str:
    """تطبيع خفيف لبناء بصمة المحتوى — لا يُستخدم في الفلترة إطلاقاً."""
    if not text:
        return ""
    t = text.strip().lower()
    t = _FP_DIACRITICS.sub("", t)
    t = t.translate(_FP_LETTER_MAP)
    t = t.translate(_FP_DIGIT_MAP)
    t = _FP_AR_REPEATS.sub(r"\1", t)
    t = _FP_WS.sub(" ", t).strip()
    return t


def content_fingerprint(sender_id: Any, text: str) -> str:
    """بصمة محتوى مستقرة: (sender_id, normalized_text)."""
    norm = normalize_for_fingerprint(text or "")
    return fast_hash(f"v1:{int(sender_id or 0)}:{norm}")


# ─────────────────────────────────────────────────────────────────────────────
# AlertDeduplicator
# ─────────────────────────────────────────────────────────────────────────────
class AlertDeduplicator:
    """
    حاجز منع تكرار التنبيهات — طبقة ذاكرة + طبقة قاعدة بيانات.

    الاستخدام في monitors.py::_send_alert:
        fp = content_fingerprint(sender_id, text)
        if not await dedup.check_and_claim(fp):      # مكرر → لا تنبيه
            return
        try:
            ... إرسال التنبيه ...
        except Exception:
            await dedup.release(fp)                  # فك الحجز لإعادة المحاولة
    """

    def __init__(self, db: Any = None, mem_max: int = 8192):
        self.db = db
        self._mem_max = max(256, int(mem_max))
        # fp -> first_seen (LRU مع كنس lazy حسب النافذة)
        self._mem: "OrderedDict[str, float]" = OrderedDict()
        self._stats: Dict[str, int] = {
            "claimed": 0,       # حجز ناجح (رسالة غير مكررة)
            "blocked": 0,       # رفض (مكرر)
            "released": 0,      # فك حجز بعد فشل إرسال
            "db_errors": 0,     # فشل DB (فشل-آمن: نثق بالذاكرة)
            "expired": 0,       # بصمات انتهت نافذتها (ذاكرة)
            "db_cleaned": 0,    # صفوف DB حذفها التنظيف الدوري
        }

    # ── إعدادات حية (تُقرأ من CFG عند كل استدعاء — لوحة التحكم تُغيّرها فوراً)
    @property
    def enabled(self) -> bool:
        return bool(getattr(CFG, "DEDUP_ENABLED", True))

    @property
    def window_seconds(self) -> int:
        try:
            return max(0, int(getattr(CFG, "DEDUP_WINDOW_SECONDS", 86400)))
        except (TypeError, ValueError):
            return 86400

    # ── L1: memory (SYNC — atomic in a single event loop, like FastCapture) ──
    def _mem_has(self, fp: str, now: float) -> bool:
        """فحص الذاكرة + انتهاء النافذة. لا يعدّل الحجز."""
        ts = self._mem.get(fp)
        if ts is None:
            return False
        if now - ts > self.window_seconds:
            del self._mem[fp]
            self._stats["expired"] += 1
            return False
        return True

    def _mem_claim(self, fp: str, now: float) -> None:
        self._mem[fp] = now
        self._mem.move_to_end(fp)
        while len(self._mem) > self._mem_max:
            self._mem.popitem(last=False)

    def _mem_release(self, fp: str) -> None:
        self._mem.pop(fp, None)

    # ── API ────────────────────────────────────────────────────────────────
    async def check_and_claim(self, fingerprint: str) -> bool:
        """
        True  → هذه هي "المرة الأولى" ضمن النافذة: احجز وتابع (أرسل التنبيه).
        False → مكرر (نفس المرسل بنفس النص، عبر أي حساب/عملية) → لا تنبيه.
        """
        if not self.enabled:
            return True
        now = time.time()
        window = self.window_seconds
        if window <= 0:
            # نافذة صفرية = الحماية معطلة عملياً
            return True

        # L1 أولاً — بدون أي await بين الفحص والحجز (atomic في الـ loop)
        if self._mem_has(fingerprint, now):
            self._stats["blocked"] += 1
            return False
        self._mem_claim(fingerprint, now)

        # L2: DB claim آتومي (INSERT OR IGNORE على PK). يمنع التكرار عبر
        # إعادة التشغيل ويضمن الاتفاق عبر الـ workers المتزامنة.
        if self.db is not None:
            try:
                claimed = await self.db.claim_alert_fingerprint(
                    fingerprint, first_seen=now, window_hint=window
                )
                if not claimed:
                    self._stats["blocked"] += 1
                    return False
            except Exception as e:
                # فشل-آمن: الحجز في الذاكرة كافٍ لمنع تكرار الحسابات؛
                # التنبيه الأول يمر ولا يُفقد بسبب مشكلة DB.
                self._stats["db_errors"] += 1
                logger.warning(f"dedup: DB claim failed (memory-only mode): {type(e).__name__}")

        self._stats["claimed"] += 1
        return True

    async def release(self, fingerprint: str) -> None:
        """فك الحجز عند فشل الإرسال — تسمح لإعادة محاولة DLQ بإرساله لاحقاً."""
        self._mem_release(fingerprint)
        self._stats["released"] += 1
        if self.db is not None:
            try:
                await self.db.release_alert_fingerprint(fingerprint)
            except Exception as e:
                logger.debug(f"dedup: DB release failed: {type(e).__name__}")

    async def cleanup_expired(self) -> int:
        """حذف البصمات المنتهية من DB — يستدعيه _cleanup_loop في main.py."""
        window = self.window_seconds
        if window <= 0 or self.db is None:
            return 0
        try:
            deleted = await self.db.cleanup_alert_fingerprints(max_age_seconds=window)
            self._stats["db_cleaned"] += int(deleted or 0)
            return int(deleted or 0)
        except Exception as e:
            logger.debug(f"dedup: cleanup failed: {type(e).__name__}")
            return 0

    def stats(self) -> Dict[str, int]:
        return dict(self._stats)

    def snapshot(self) -> Dict[str, Any]:
        """لقطة للقراءة فقط لـ /health واللوحة (مفاتيح ثابتة دائماً)."""
        try:
            base = {
                "claimed": 0, "blocked": 0, "released": 0,
                "db_errors": 0, "expired": 0, "db_cleaned": 0,
            }
            return {
                "enabled": self.enabled,
                "window_seconds": self.window_seconds,
                "mem_size": len(self._mem),
                "mem_max": self._mem_max,
                **{**base, **self._stats},
            }
        except Exception:
            return {"enabled": False, "window_seconds": 0, "mem_size": 0}


# ─────────────────────────────────────────────────────────────────────────────
# Singleton مشترك بين كل المراقبين (نفس نمط _capture في monitors.py)
# ─────────────────────────────────────────────────────────────────────────────
_dedup: Optional[AlertDeduplicator] = None


def init_deduplicator(db: Any) -> AlertDeduplicator:
    """تهيئة الـ singleton بقاعدة البيانات المشتركة — تُستدعى مرة من main.py."""
    global _dedup
    _dedup = AlertDeduplicator(db=db)
    logger.info(
        f"Alert Deduplicator initialized | enabled={_dedup.enabled} | "
        f"window={_dedup.window_seconds}s | mem_max={_dedup._mem_max}"
    )
    return _dedup


def get_deduplicator() -> AlertDeduplicator:
    """يعيد الـ singleton (أو نسخة بدون DB في dashboard-only mode / الاختبارات)."""
    global _dedup
    if _dedup is None:
        _dedup = AlertDeduplicator(db=None)
    return _dedup


def get_dedup_snapshot() -> Dict[str, Any]:
    """Read-only snapshot لـ /health و dashboard — لا يرمي استثناءات أبداً."""
    try:
        return get_deduplicator().snapshot()
    except Exception:
        return {"enabled": False, "window_seconds": 0, "mem_size": 0}
