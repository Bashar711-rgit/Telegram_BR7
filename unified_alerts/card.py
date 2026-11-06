#!/usr/bin/env python3
"""
unified_alerts/card.py — عقد سلامة البطاقة (v14.5.1)

«لا يصل مستخدم أبداً تنبيه لم يُبنَ صحيحاً» — كل بطاقة (نص HTML + أزرار)
تُفحص قبل الإرسال عبر Bot API أو مسار الحسابات الاحتياطي:

1. النص غير فارغ بعد التطبيع (فراغات/صفر-عرض لا تُعد محتوى).
2. حد تيليجرام الصلب: 4096 وحدة UTF-16 للنص كله (الإيموجي وحدة×2).
   الحد التشغيلي ALERT_CARD_MAX_CHARS (افتراضي 4000) أضيق من الصلب.
3. وسوم HTML متوازنة — لا فقط <a>: <b>/<i>/<u>/<s>/<code>/<pre>/...
   وسم غير مغلق بعد القص يُصلَّح أو يُحذف (Bot API يرفض HTML مكسوراً
   بخطأ 400 can't parse entities → تنبيه يفشل كلياً).
4. الأزرار: لكل زر نص + رابط (t.me/https/tg://) — الزر المعطوب يُحذف
   بصدق بدل رفض الحمولة كلها.

v14.5.1 — إصلاح جذري للقص:
  * النسخة السابقة كانت توازن <a> فقط. عند قص بطاقة تحتوي <b>/<i>،
    يبقى الوسم مفتوحاً فيرفض Bot API الرسالة كلياً — نفس العطب الذي
    وُجد card.py لمنعه. الآن:
      - _find_unclosed_tags يحلل كل وسوم تيليجرام المعروفة
        (b/strong/i/em/u/ins/s/strike/del/code/pre/a/blockquote/
        tg-spoiler/span) ويستخدم stack لتحديد المفتوح في النهاية.
      - _truncate_safe_html يغلقها كلها بترتيب معاكس بعد القص.
      - _ANCHOR_OPEN (الذي كان يفوت <a href='...'> و<a href="..." target=...>)
        لم يعد مستخدماً في القص — استُبدل بـ_TAG_OPEN_RE العام.
  * لا تغيير في السلوك للمسارات السليمة: بطاقة تحت 4000 وحدة تمر كما هي.
  * فشل-آمن كما هو: أي خلل في الفحص يُعيد البطاقة الأصلية.

فشل-آمن: أي خلل في الفحص نفسه يعيد البطاقة كما هي (ok=True بلا تعديل) —
الفحص ميزة، وأخطاؤه لا تُسقط تنبيهات صالحة.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from unified_alerts.stats import get_unified_stats

# حد تيليجرام الصلب (وحدات UTF-16)
_TG_HARD_LIMIT = 4096

# ─────────────────────────────────────────────────────────────────────────────
# v14.5.1: مجموعة وسوم HTML التي يفهمها Bot API ويجب موازنتها عند القص.
# <strong>/<em>/<ins>/<strike>/<del> مرادفات لـ<b>/<i>/<u>/<s> — تُطبَّع
# عند التتبع كي لا يُنشئ <b>...</strong> عدم توازن كاذب.
# ─────────────────────────────────────────────────────────────────────────────
_TG_TAGS = (
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "code", "pre", "a", "blockquote", "tg-spoiler", "span",
)
_TG_TAGS_ALT = "|".join(_TG_TAGS)
_TAG_OPEN_RE = re.compile(r"<(" + _TG_TAGS_ALT + r")(?:\s[^>]*)?>", re.IGNORECASE)
_TAG_CLOSE_RE = re.compile(r"</(" + _TG_TAGS_ALT + r")\s*>", re.IGNORECASE)
_TAG_CANON = {"strong": "b", "em": "i", "ins": "u", "strike": "s", "del": "s"}

# Legacy: يبقى مستخدماً في _balance_anchors فقط — لم يعد داخل القص.
_ANCHOR_OPEN = re.compile(r'<a\s+href="[^"]*">', re.IGNORECASE)
_ANY_TAG = re.compile(r"<[^>]+>")
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff]")


def utf16_len(text: str) -> int:
    """طول النص بوحدات UTF-16 — نفس مقياس حد Bot API (4096)."""
    try:
        return len(text.encode("utf-16-le")) // 2
    except Exception:
        return len(text)


def _canonical_tag(name: str) -> str:
    low = name.lower()
    return _TAG_CANON.get(low, low)


def _find_unclosed_tags(text: str) -> List[str]:
    """أسماء الوسوم المفتوحة في نهاية النص، بترتيب الفتح.

    المسح خطي (regex finditer) + stack، فيتعامل مع:
      * تعدد الوسوم المتداخلة <b><i>...</i></b>
      * وسوم مغلقة بمرادفها <b>...</strong>
      * فوضى خفيفة <b><i>...</b></i> (يُغلق من الداخل للأعلى عند رؤية </b>)
    """
    stack: List[str] = []
    events: List[Tuple[int, str, str]] = []
    for m in _TAG_OPEN_RE.finditer(text):
        events.append((m.start(), "open", _canonical_tag(m.group(1))))
    for m in _TAG_CLOSE_RE.finditer(text):
        events.append((m.start(), "close", _canonical_tag(m.group(1))))
    events.sort(key=lambda e: e[0])
    for _, kind, name in events:
        if kind == "open":
            stack.append(name)
        else:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == name:
                    del stack[i:]
                    break
    return stack


@dataclass(slots=True)
class CardReport:
    """نتيجة فحص البطاقة — ok=False مع repaired_text=None يعني إسقاطاً صادقاً."""

    ok: bool
    reasons: List[str] = field(default_factory=list)
    repaired_text: Optional[str] = None
    repaired_buttons: Optional[List[List[Dict[str, Any]]]] = None
    dropped_buttons: int = 0


def _is_valid_url(url: Any) -> bool:
    try:
        u = str(url or "").strip()
        if not u:
            return False
        return (
            u.startswith("https://")
            or u.startswith("http://")
            or u.startswith("tg://")
        )
    except Exception:
        return False


def _clean_buttons(buttons: Any) -> Tuple[Optional[List[List[Dict[str, Any]]]], int]:
    """إزالة الأزرار المعطوبة — يعيد (اللوحة النظيفة أو None، عدد المحذوف)."""
    if buttons is None:
        return None, 0
    dropped = 0
    try:
        clean_kb: List[List[Dict[str, Any]]] = []
        for row in buttons or []:
            clean_row: List[Dict[str, Any]] = []
            for btn in row or []:
                try:
                    txt = str(btn.get("text") or "").strip()
                    url = btn.get("url")
                    if txt and _is_valid_url(url):
                        clean_row.append({"text": txt[:64], "url": str(url)})
                    else:
                        dropped += 1
                except Exception:
                    dropped += 1
            if clean_row:
                clean_kb.append(clean_row)
        return clean_kb, dropped
    except Exception:
        return buttons, dropped


def _truncate_safe_html(text: str, max_units: int) -> str:
    """قص النص عند حد UTF-16 مع إغلاق كل وسوم HTML المفتوحة.

    v14.5.1: كانت النسخة السابقة توازن <a> فقط. القص داخل <b>/<i>/...
    كان يُنتج HTML غير متوازن يرفضه Bot API (400 can't parse entities).
    الآن نكشف كل الوسوم المفتوحة (بأي اسم تيليجرام) ونغلقها بترتيب معاكس.
    """
    if utf16_len(text) <= max_units:
        return text
    # 1) حساب نقطة القص بوحدات UTF-16 (لا نقسم زوجاً بديلاً)
    cut_py = 0
    units = 0
    for i, ch in enumerate(text):
        w = 2 if ord(ch) > 0xFFFF else 1
        if units + w > max_units - 1:  # -1 هامش «…»
            break
        units += w
        cut_py = i + 1
    head = text[:cut_py]
    # 2) لا نقص في منتصف وسم — نقص قبله
    last_lt = head.rfind("<")
    last_gt = head.rfind(">")
    if last_lt > last_gt:
        head = head[:last_lt]
    # 3) v14.5.1: أغلق كل وسوم HTML المفتوحة، لا <a> فقط
    unclosed = _find_unclosed_tags(head)
    for tag in reversed(unclosed):
        head += f"</{tag}>"
    head = head.rstrip() + "…"
    return head


def _balance_anchors(text: str) -> Tuple[str, bool]:
    """موازنة <a> — يعيد (النص، هل عدّل).

    تُستخدم كطبقة أولية (input cleanup) قبل الطول. إصلاح الوسوم الأخرى
    يتم في _truncate_safe_html فقط.
    """
    try:
        opens = len(_ANCHOR_OPEN.findall(text))
        closes = text.lower().count("</a>")
        if opens == closes:
            return text, False
        if opens > closes:
            return text + "</a>" * (opens - closes), True
        fixed = text
        extra = closes - opens
        for _ in range(extra):
            idx = fixed.lower().rfind("</a>")
            if idx < 0:
                break
            fixed = fixed[:idx] + fixed[idx + 4:]
        return fixed, True
    except Exception:
        return text, False


def validate_and_repair_card(
    text: Any,
    buttons: Any = None,
    *,
    max_chars: Optional[int] = None,
) -> CardReport:
    """فحص + إصلاح ذاتي للبطاقة الموحدة.

    يعيد CardReport:
      ok=True  → النص (أو repaired_text) جاهز للإرسال، الأزرار نظيفة.
      ok=False → بطاقة لا تصلح حتى بعد الإصلاح (نص فارغ مثلاً) — إسقاط صادق.
    """
    stats = get_unified_stats()
    try:
        if not isinstance(text, str):
            text = "" if text is None else str(text)
        reasons: List[str] = []
        repaired = False

        # 1) نص فارغ (بعد إزالة صفر-العرض والفراغ) → إسقاط صادق
        if not _ZERO_WIDTH.sub("", text).strip():
            stats.inc("card_dropped")
            return CardReport(ok=False, reasons=["empty_text"])

        # 2) موازنة وسوم <a> (input cleanup — التصفية النهائية في القص)
        text, changed = _balance_anchors(text)
        if changed:
            repaired = True
            reasons.append("anchors_balanced")

        # 3) حد الطول — ALERT_CARD_MAX_CHARS ثم الحد الصلب 4096
        try:
            from config import CFG

            cfg_max = int(getattr(CFG, "ALERT_CARD_MAX_CHARS", 4000) or 4000)
        except Exception:
            cfg_max = 4000
        limit = min(max(256, cfg_max), _TG_HARD_LIMIT - 96)
        if max_chars:
            limit = min(limit, max(256, int(max_chars)))
        if utf16_len(text) > limit:
            text = _truncate_safe_html(text, limit)
            repaired = True
            reasons.append("truncated_to_limit")

        # 4) الأزرار — نظافة صادقة
        clean_kb, dropped = _clean_buttons(buttons)
        if dropped:
            repaired = True
            reasons.append(f"buttons_dropped={dropped}")
        final_buttons: Optional[List[List[Dict[str, Any]]]] = None
        if buttons is not None:
            final_buttons = clean_kb

        # 5) بعد الإصلاح ما زال فارغاً؟ (قصٌّ هوّن نص وسومٍ فقط)
        if not _ZERO_WIDTH.sub("", text).strip():
            stats.inc("card_dropped")
            return CardReport(
                ok=False,
                reasons=reasons + ["empty_after_repair"],
                repaired_buttons=final_buttons,
                dropped_buttons=dropped,
            )

        if repaired:
            stats.inc("card_repaired")
            return CardReport(
                ok=True,
                reasons=reasons,
                repaired_text=text,
                repaired_buttons=final_buttons,
                dropped_buttons=dropped,
            )
        stats.inc("card_valid")
        return CardReport(
            ok=True,
            reasons=[],
            repaired_text=None,
            repaired_buttons=final_buttons,
            dropped_buttons=0,
        )
    except Exception:
        # فشل-آمن: خلل الفحص لا يمنع البطاقة الأصلية
        stats.inc("card_check_errors")
        return CardReport(ok=True, reasons=["check_error_fail_open"])


def validate_card(text: Any, buttons: Any = None, *, max_chars: Optional[int] = None) -> bool:
    """اختصار بولياني — صحيح فقط إن كانت البطاقة صالحة بلا إصلاح."""
    rep = validate_and_repair_card(text, buttons, max_chars=max_chars)
    return rep.ok and not rep.repaired_text
