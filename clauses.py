#!/usr/bin/env python3
"""
clauses.py — v10.0 Precision Edition: تقسيم الرسالة إلى بنود مستقلة.

المشكلة التي يحلها
------------------
المحرك v14.5 يقيّم الرسالة ككتلة واحدة. الرسالة متعددة الأفكار مثل:

    «لقيت حل للواجب ما قصرتوا، بس عندي تقرير ثاني محتاج مساعدة فيه»

تحصل على سقف review (القيد الموثق resolution_with_new_request) لأن بند
الاسترسال (لقيت الحل) يلوّث تقييم بند الطلب الجديد. الحل الاحترافي:
تقسيم الرسالة لبنود ثم إعادة تقييم أفضل بند قسماً مستقلاً داخل المحرك.

التصميم —保守ي ومحكم:
  * التقسيم على: علامات الترقيم + الأسطر الجديدة + روابط الاستدراك
    (لكن/لكنه/غير أن/إلا أن) — وليس على «لا» (نفي) ولا «و» (ربط شائع جداً).
  * البند القصير جداً (< min_len) يُهمل.
  * الحد الأقصى max_clauses بنود (الأطول أولاً بعد الترتيب الأصلي).
  * دوال نقية (pure) — بلا أي حالة أو I/O — فشل-آمن (قائمة فارغة عند أي خطأ).

المبادئ:
  * لا يغيّر هذا الملف أي قرار وحده — هو طبقة تقطيع يستدعيها المحرك.
  * التطبيع تم مسبقاً في المحرك (_clean) — لا تطبيع هنا تجنباً للازدواج.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# فواصل البنود
# ─────────────────────────────────────────────────────────────────────────────
# 1) فواصل ترقيم قوية (فاصلة عربية/لاتينية، فاصلة منقوطة، نقطة، علامات استفهام
#    وتعجب، نهايات الأسطر، الشرطة الطويلة).
_PUNCT_SPLIT: "re.Pattern[str]" = re.compile(r"[،؛,;.!؟?\n\r]+")

# 2) روابط الاستدراك — تفصل فكرة جديدة عن سابقة. تُطابَق بحدود كلمات صريحة
#    حتى لا نقسم داخل كلمة («بلكن»، «بستنى») ولا داخل «بس» المضمنة في تعبير آخر.
#    ملاحظة: «لا» نفي وليست استدراك — عمداً غير موجودة هنا. «بس» الاستدراكية
#    الشائعة بلهجة خليجية مقصودة: حراس البند العاري في المحرك يمنعون أي
#    ترقية زائفة تنشأ عن تقسيم «بس» الفاصلة الوظيفية («ابي بس اعرف»).
_CONTRAST_PATTERN: "re.Pattern[str]" = re.compile(
    r"(?:(?<=\s)|^)(?:لكن|لكنه|لكنها|لكنّ|بس|غير ان|الا ان|الا انه|however|but)(?=\s|$)",
    re.IGNORECASE,
)

# كلمات تتابع شائعة بلهجة خليجية تشير لبند جديد — محافظة: نقسم بعدها فقط
# عندما يسبقها نهاية فكرة واضحة (طول البند السابق >= 2 كلمات) يقررها المحرك.
_SEQUENCE_PATTERN: "re.Pattern[str]" = re.compile(
    r"(?:(?<=\s)|^)(?:بعدين|وبعدين|و بعدين|ثم|كمان عندي|عندي كمان)(?=\s|$)"
)

# تخصيصات يمنع خطؤها التقسيم: صيغ الاستغاثة/التحية المركبة الشائعة التي قد
# تحمل فاصلة داخلية لكنها فكرة واحدة («السلام عليكم ورحمة الله وبركاته»).
_KEEP_TOGETHER = (
    "ان شاء الله",
    "سلام عليكم",
    "السلام عليكم",
    "صباح الخير",
    "مساء الخير",
    "شكرا جزيلا",
    "شكرا لكم",
    "ما قصرتوا",
    "ما قصرتو",
    "ربي يعطيكم",
)


def _protect_keep_together(text: str) -> Tuple[str, Dict[int, str]]:
    """يحمي التعبيرات المركبة من التقسيم باستبدال مؤقت بمحرف نائب.

    يعيد (النص المحمي، خريطة فهرس_نائب → التعبير الأصلي) — الفهرس
    هو موضعه في _KEEP_TOGETHER (وليس ترتيب الاكتشاف) ليصح الاسترجاع.
    """
    placeholders: Dict[int, str] = {}
    out = text
    for i, phrase in enumerate(_KEEP_TOGETHER):
        if phrase in out:
            out = out.replace(phrase, f"\x00{i}\x00")
            placeholders[i] = phrase
    return out, placeholders


def _restore_keep_together(text: str, placeholders: Dict[int, str]) -> str:
    for i, phrase in placeholders.items():
        text = text.replace(f"\x00{i}\x00", phrase)
    return text


def split_clauses(text: str, max_clauses: int = 6, min_len: int = 5) -> List[str]:
    """يقسم النص المطبَّع مسبقاً إلى بنود مرتبة بترتيب ظهورها.

    Args:
        text: نص المرسلة (يفضَّل مطبَّع من المحرك — لا يشترط).
        max_clauses: الحد الأقصى للبنود المعادة (يقتطع الزائد عن الحاجة).
        min_len: أقل طول حرفي لبند ليُعتبر ذا معنى.

    Returns:
        قائمة بنود نصية (قد تكون فارغة عند الفشل — فشل-آمن).
    """
    try:
        if not isinstance(text, str):
            return []
        raw = text.strip()
        if len(raw) < min_len:
            return [raw] if raw else []

        protected, placeholders = _protect_keep_together(raw)

        # استدراك/تتابع أولاً ثم الترقيم — كي لا يُقصّ التعبير المحمي بواسطة
        # النائب (المحرف \x00 ليس ضمن فئات الترقيم).
        s1 = _CONTRAST_PATTERN.sub("\x01", protected)
        s2 = _SEQUENCE_PATTERN.sub("\x01", s1)
        s3 = _PUNCT_SPLIT.sub("\x01", s2)

        parts = [p.strip() for p in s3.split("\x01")]
        clauses: List[str] = []
        for p in parts:
            restored = _restore_keep_together(p, placeholders).strip()
            if restored and len(restored) >= min_len and restored not in clauses:
                clauses.append(restored)
            if len(clauses) >= max_clauses:
                break
        return clauses
    except Exception:
        # فشل-آمن: بند واحد = النص الكامل (سلوك المحرك الأصلي).
        return [text.strip()] if isinstance(text, str) and text.strip() else []


def count_clauses(text: str) -> int:
    """عدد البنود ذات المعنى — مساعد خفيف للسجلات والاختبارات."""
    return len(split_clauses(text))
