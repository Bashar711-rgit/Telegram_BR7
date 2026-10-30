#!/usr/bin/env python3
"""
unified_alerts/filter_policy.py — سياسة الفلترة الموحدة (v12.1.0)

«مصدر حقيقة واحد» لقياس طول رسالة الطلب — يُستخدم من كل الطبقات التي كانت
تقيس بنفسها بطريقة مختلفة (السبب الجذري لفقدان رسائل صالحة):

  ✧ الثغرة التي كانت تفقد رسائل فعلياً (مصلحة حقيقية قبل v12.1.0):
      - monitors._validate_event تقيس النص بعد strip (مقصوص)
      - monitors._store_message كانت تقيس النص الخام قبل strip
      - filter_engine.analyze كانت ترفض قبل strip على النص الخام
    النتيجة: رسالة نصها الخام 80 محرفاً لكن مقصوصها 40 (فراغات/أسطر
    زائدة) كانت تعبر _validate_event ثم تُقتل في _store_message
    (validated_text=None → analysis "no_content") أو في الفلتر
    ("too_long") — تنبيه صالح يُفقد بصمت. التوحيد هنا يغلق الفئة كلها.

القاعدة الموحدة (مواصفة المستخدم):
  * القياس على النص بعد strip وإزالة محارف صفر-العرض.
  * MIN_MESSAGE_LENGTH <= الطول <= MAX_MESSAGE_LENGTH — **شاملاً الحدين**
    (6..65 افتراضياً): 6 يمر، 65 يمر، 5 يُرفض، 66 يُرفض.
  * القراءة حية من CFG عند كل استدعاء — تعديل اللوحة يسري فوراً.
  * هامش الحد: قرارات على الحد بالضبط مستقرة (لا انقلاب بين استدعاءين
    بسبب microtic اختلاف قياس — نفس روح _BOUNDARY_GRACE في gate.py).

فشل-آمن: أي خلل داخلي يعيد verdict مقبول (ok=True) مع عداد
policy_errors — خلل السياسة لا يُسقط طلباً صالحاً أبداً.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from unified_alerts.stats import get_unified_stats

# محارف صفر-العرض/اتجاه — لا تُعد محتوى في القياس
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u061c]")

# هامش شمولية الحد: قيمة مساوية للحد تنقلب قرارها بين طبقتين لو قاست
# كل طبقة نسخة مختلفة بقليل (strip متداخل مثلاً). التوحيد يجعل القياس
# واحداً؛ الهامش حماية إضافية لقرارات الحد بالضبط.
_BOUNDARY_GRACE_CHARS = 0  # القياس الموحد نفسه يجعل الحد شاملاً بالضبط


@dataclass(slots=True)
class FilterVerdict:
    """نتيجة فحص سياسة الطول الموحدة."""

    ok: bool
    reason: str = ""
    stripped_text: str = ""
    length: int = 0
    meta: dict = field(default_factory=dict)


def normalize_for_measure(text: Any) -> str:
    """تطبيع القياس: strip + إزالة صفر-العرض. لا يغيّر المعنى أبداً."""
    try:
        if not isinstance(text, str):
            return ""
        return _ZERO_WIDTH.sub("", text).strip()
    except Exception:
        return str(text or "").strip()


class FilterPolicy:
    """السياسة الموحدة لحدود طول رسالة الطلب — متزامنة خالصة (بلا I/O).

    الاستخدام:
        v = FilterPolicy.check(text)
        if not v.ok: ...  # reason موحد للعدادات والسجلات
        # v.stripped_text هو النسخة المعتمدة للتحليل والتنبيه والتخزين
    """

    @staticmethod
    def limits() -> tuple:
        """(min, max) حية من CFG — فشل-آمن إلى 6..65."""
        try:
            from config import CFG

            mn = int(getattr(CFG, "MIN_MESSAGE_LENGTH", 6) or 6)
            mx = int(getattr(CFG, "MAX_MESSAGE_LENGTH", 65) or 65)
        except Exception:
            mn, mx = 6, 65
        if mn < 0:
            mn = 0
        if mx < mn:
            mx = mn
        return mn, mx

    @staticmethod
    def measure(text: Any) -> int:
        """طول النص بالقياس الموحد (بعد strip وإزالة صفر-العرض)."""
        return len(normalize_for_measure(text))

    @classmethod
    def check(cls, text: Any) -> FilterVerdict:
        """القرار الموحد — نفس القرار من أي طبقة استدعته."""
        stats = get_unified_stats()
        try:
            if text is None:
                stats.inc("filter_invalid")
                return FilterVerdict(ok=False, reason="invalid_input",
                                     stripped_text="", length=0)
            if not isinstance(text, str):
                # أرقام/كائنات أخرى: تُحوَّل نصاً وتُقاس بنفس السياسة
                text = str(text)
            stripped = normalize_for_measure(text)
            n = len(stripped)
            mn, mx = cls.limits()
            if n == 0:
                stats.inc("filter_invalid")
                return FilterVerdict(ok=False, reason="empty_text",
                                     stripped_text="", length=0)
            if n < mn:
                stats.inc("filter_too_short")
                return FilterVerdict(ok=False, reason="too_short",
                                     stripped_text=stripped, length=n,
                                     meta={"min": mn, "max": mx})
            if n > mx:
                stats.inc("filter_too_long")
                return FilterVerdict(ok=False, reason="too_long",
                                     stripped_text=stripped, length=n,
                                     meta={"min": mn, "max": mx})
            stats.inc("filter_ok")
            return FilterVerdict(ok=True, reason="in_range",
                                 stripped_text=stripped, length=n,
                                 meta={"min": mn, "max": mx})
        except Exception:
            # فشل-آمن مطلق: خلل السياسة لا يُسقط طلباً صالحاً
            stats.inc("policy_errors")
            try:
                s = str(text or "").strip()
            except Exception:
                s = ""
            return FilterVerdict(ok=bool(s), reason="policy_error",
                                 stripped_text=s, length=len(s))

    @classmethod
    def quick_ok(cls, text: Any) -> bool:
        """اختصار بولياني لنفس القرار (بلا كائن نتيجة)."""
        return cls.check(text).ok

    @staticmethod
    def snapshot() -> dict:
        """لقطة الإعدادات لـ /health/full واللوحة."""
        mn, mx = FilterPolicy.limits()
        return {
            "min_message_length": mn,
            "max_message_length": mx,
            "inclusive_bounds": True,
            "measure": "stripped+zero-width-removed",
        }


__all__ = [
    "FilterPolicy",
    "FilterVerdict",
    "normalize_for_measure",
]
