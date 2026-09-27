#!/usr/bin/env python3
"""
classifier.py — v10.0: التصنيف الدقيق لطلبات الطلاب.

الهدف
-----
طبقة تصنيف خفيفة وسريعة (Regex مطَّبَّع على النص) تعمل أعلى محرك
الفلترة الحالي دون تعديل قراره إطلاقاً:

  1) *مادة الطلب* — رياضيات/فيزياء/كيمياء/برمجة/لغة إنجليزية/… — تُعرض
     في التنبيه كوسم واضح فيصل المشرف للطلب المناسب أسرع.
  2) *نوع المطلوب* — واجب/مشروع/تقرير/بحث/عرض تقديمي/اختبار/تلخيص/حل —
     مستخرج من مفردات أكاديمية شائعة بلهجتين (سعودي/يمني) ومصطلحات إنجليزية.

المبادئ
-------
* فشل-آمن: أي استثناء يعيد قاموساً فارغاً — لا يؤثر على مسار التنبيه.
* مستقل تماماً عن filter_engine (لا استيراد دائري) — تطبيع عربي خفيف خاص.
* يحافظ على النص الأصلي — التطبيع لغرض المطابقة فقط.
* الحسم بعدد التطابقات ثم ترتيب الأولوية (المواد المتخصصة أولاً).

"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# تطبيع عربي خفيف (مطابقة فقط — لا يمس النص الأصلي)
# ---------------------------------------------------------------------------
_TASHKEEL = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_TATWEEL = re.compile(r"\u0640+")
_DIACRITICS_REPEAT = re.compile(r"(.)\1{2,}")  # ضرورررري → ضروري
_NON_WORD = re.compile(r"[^\w\u0600-\u06FF]+")  # غير الحرف/الرقم/العربي
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u2190-\u21FF\u2B00-\u2BFF]+"
)


def normalize_light(text: str) -> str:
    """تطبيع خفيف للمطابقة: همزات/تاء مربوطة/ألف مقصورة/تطويل/تشكيل/تكرار."""
    if not isinstance(text, str):
        return ""
    t = _EMOJI.sub(" ", text)
    t = _TASHKEEL.sub("", t)
    t = _TATWEEL.sub("", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ة", "ه").replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي")
    t = _DIACRITICS_REPEAT.sub(r"\1", t)
    return t


def _norm_word(term: str) -> str:
    """تطبيع نفسه لمصطلحات الأنماط حتى تتطابق الجهتان."""
    return normalize_light(term)


def _mk_pattern(terms: List[str], word_boundary: bool = True) -> re.Pattern:
    """يبني regex واحداً من قائمة مصطلحات مطبَّعة — الأطول أولاً."""
    normalized = sorted({_norm_word(t).strip() for t in terms if _norm_word(t).strip()},
                        key=len, reverse=True)
    body = "|".join(re.escape(t) for t in normalized if t)
    if word_boundary and body:
        return re.compile(rf"(?:^|[\s_\-/\.،؟!])({body})(?=$|[\s_\-/\.،؟!])")
    return re.compile(body) if body else re.compile(r"(?!x)x")


# ---------------------------------------------------------------------------
# المواد — الأكثر تخصصاً أولاً (منع ابتلاع شائع: «عربي» داخل «لغة عربية» إلخ)
# ترتيب القائمة هو ترتيب الفحص عند تعادل عدد التطابقات.
# ---------------------------------------------------------------------------
SUBJECTS: List[Tuple[str, str, List[str]]] = [
    # (المفتاح، التسمية العربية، أنماط)
    ("excel_office", "إكسل وأوفيس", [
        "اكسل", "إكسل", "excel", "اكسس", "access", "وورد", "word", "بوربوينت",
        "powerpoint", "أوفيس", "افيس", "office", " Pivot".strip(), "vlookup",
    ]),
    ("programming", "برمجة", [
        "برمجه", "برمجة", "كود", "بايثون", "python", "جافا", "java", "جافاسكربت",
        "javascript", "js", "html", "css", "سي شارب", "c#", "c++", " ++c",
        "خوارزميه", "خوارزمية", "الخوارزميات", "سحقوان", "sql", "mysql",
        "flutter", "فلاتر", "تطبيق جوال", "موقع الكتروني", "لعبه الكترونيه",
        "csharp", "اندروید".replace("ی", "ي"), "android", "اندرويد", "react", "ريأكت",
    ]),
    ("databases", "قواعد بيانات", [
        "قاعده بيانات", "قواعد بيانات", "oracle", "اوراكل", "sql server",
        "نظم قواعد", "database",
    ]),
    ("networks", "شبكات", [
        "شبكات", "شبكه", "الشبكات", "network", "ccna", "tcp", "cisco", "سيسكو",
    ]),
    ("stats", "إحصاء ورياضيات تطبيقية", [
        "احصاء", "إحصاء", "احصائيه", "إحصائية", "احتمالات", "spss", "minitab",
        "رگرسيون".replace("گ", "ج"), "انحدار", "استقراء", "مينيتاب",
    ]),
    ("physics", "فيزياء", [
        "فيزياء", "فزياء", "physics", "ميكانيكا كلاسيكيه", "كهرومغناطيسيه",
        "كموميه", "الكم", "ترمو",
    ]),
    ("chemistry", "كيمياء", [
        "كيمياء", "كيماء", "chemistry", "عضويه", "غير عضويه", "تحليليه",
    ]),
    ("biology", "أحياء", [
        "احياء", "أحياء", "بيولوجي", "biology", "وراثه", "خليه", "خلايا",
    ]),
    ("math", "رياضيات", [
        "رياضيات", "رياضي", "حساب", "تفاضل", "تكامل", "جبر", "هندسه تحليليه",
        "معادلات تفاضليه", "تحليل حقيقي", "متحولات", "math", "calculus",
        "algebra", "geometry", "trigonometry", "مثلثات", "خطوط واتجاهات",
    ]),
    ("accounting", "محاسبة ومالية", [
        "محاسبه", "محاسبة", "محاسبه ماليه", "قيود", "ميزانيه", "تدقيق",
        "تكاليف", "ضريبه", "زكاه", "accounting",
    ]),
    ("economy", "اقتصاد", [
        "اقتصاد", "اقتصاديه", "اقتصاد كلي", "اقتصاد جزئي", "economy",
    ]),
    ("management", "إدارة وأعمال", [
        "اداره اعمال", "إدارة أعمال", "تسويق", "موارد بشريه", "سلوك تنظيمي",
        "استراتيجيه", "سلسله امداد", "اداره مشاريع", "pmp", "management",
    ]),
    ("law", "قانون وأنظمة", [
        "حقوق", "قانون", "نظم", "شريعه اسلاميه".split()[0], "أنظمه",
        "عقود", "مرافعات",
    ]),
    ("english", "لغة إنجليزية", [
        "انجليزي", "إنجليزي", "انجليزيه", "إنجليزيه", "الانجليزيه",
        "english", "لغه انجليزيه", "ترجمه انجليزي", "essay", "IELTS", "TOEFL",
        "ايلتس", "توفل",
    ]),
    ("arabic", "لغة عربية", [
        "نحو", "بلاغه", "اعراب", "إعراب", "لغه عربيه", "عربي", "اللغه العربيه",
        "نصوص", "صرف",
    ]),
    ("islamic", "علوم شرعية", [
        "قران", "قرآن", "حديث", "فقه", "عقيده", "سيره", "تجويد", "تفسير",
        "اسلاميه", "إسلامية",
    ]),
    ("special_ed", "تربية خاصة وطفولة", [
        "تربيه خاصه", "طفوله", "روضه", "اعاقه", "توحد", "صعوبات تعلم",
    ]),
    ("education", "تربية وتعليم", [
        "تربيه", "مناهج", "طرق تدريس", "قياس وتقويم", "اجراءات تعليميه".replace("اجراءات", "إجراءات"),
    ]),
    ("design", "تصميم", [
        "تصميم", "فوتوشوب", "photoshop", "illustrator", "كانفا", "canva",
        "موشن", "مونتاج", "premier", "بريمير",
    ]),
    ("translation", "ترجمة", [
        "ترجمه", "ترجمة", "مترجم", "ترجم نص", "ترجمه فوريه",
    ]),
]

_COMPILED_SUBJECTS: List[Tuple[str, str, re.Pattern]] = [
    (key, label, _mk_pattern(terms)) for key, label, terms in SUBJECTS
]

# ---------------------------------------------------------------------------
# أنواع المطلوب — مستخرجة من صنف academic_objects في keywords.json + شائعات
# ---------------------------------------------------------------------------
TYPES: List[Tuple[str, str, List[str]]] = [
    ("assignment", "واجب", ["واجب", "واجبات", "اسايمنت", "assignment", "homework", "درجات واجب"]),
    ("exam", "اختبار", ["اختبار", "امتحان", "كويز", "كويزات", "quiz", "exam", "منتصف", "نهائي", "مراجعه نهائي"]),
    ("project", "مشروع", ["مشروع", "مشاريع", "project", "graduation project", "مشروع تخرج"]),
    ("research", "بحث", ["بحث", "بحوث", "بحث تخرج", "خطة بحث", "research", "بحث علمي"]),
    ("report", "تقرير", ["تقرير", "تقارير", "report", "رابورت"]),
    ("summary", "تلخيص", ["تلخيص", "ملخص", "ملزمة", "تلخيصات", "summary", "نوتس", "notes"]),
    ("presentation", "عرض تقديمي", ["عرض تقديمي", "برزنتيشن", "برزنتيشن", "presentation", "سلايدز", "slides"]),
    ("solution", "حل", ["حل", "يحل", "اقدر احل", "حل اسئله", "حل نماذج", "solve", "solution"]),
    ("translation_type", "ترجمة نص", ["ترجمه نص", "ترجمه فوريه", "ترجمه ملف"]),
]

_COMPILED_TYPES: List[Tuple[str, str, re.Pattern]] = [
    (key, label, _mk_pattern(terms)) for key, label, terms in TYPES
]

# كلمات عامة لا تصلح وحدها لتعيين مادة (تُهمش عند الحسم)
_GENERIC_SUBJECT_HINTS = {"عربي", "حساب"}


# ---------------------------------------------------------------------------
# v10.0 Precision: استخراج المواعيد النهائية ودرجات الأولوية
# المطلوب: ترتيب تنبيهات القناة — الأقرب موعداً يظهر بأعلى شارة.
# فشل-آمن: أي استثناء يعيد مستوى "none".
# ---------------------------------------------------------------------------
DEADLINE_LEVELS: List[Tuple[str, str, List[str]]] = [
    # (المستوى، التسمية، أنماط) — الأكثر إلحاحاً أولاً
    ("critical", "🔥 عاجل جداً", [
        "الان", "الان نفسها", "حالا", "هسة", "دحين",
        "اللحين", "بعد ساعه", "بعد ساعتين", "بعد نصف ساعه", "خلال ساعه",
        "الوقت ضيق", "الوقت دايم", "بسرعه جدا", "مستعجل جدا",
        "remaining hours", "باقي ساعه", "باكي ساعه", "وينقص الوقت",
    ]),
    ("today", "⚡ اليوم", [
        "اليوم", "اليومه", "الليله", "هالليله", "قبل نهايه اليوم", "باجر",
        "بكره", "بكرا", "باكر", "صباح باكر", "فجر باكر", "هالاسبوع ينتهي",
        "before midnight", "today",
    ]),
    ("soon", "📌 قريب", [
        "هالاسبوع", "هذا الاسبوع", "الاسبوع الجاي", "بعد باجر", "بعد بكرة",
        "قريب", "before weekend", "هالشهر",
    ]),
]

_DEADLINE_COMPILED: List[Tuple[str, str, re.Pattern]] = [
    # word_boundary=True: «هلا» لا تطابق داخل «اهلا وسهلا» و«الوقت» لا تطابق
    # داخل كلمات أخرى — المطابقة على كلمات/عبارات كاملة فقط.
    (key, label, _mk_pattern(terms, word_boundary=True))
    for key, label, terms in DEADLINE_LEVELS
]


def extract_deadline(text: str) -> Dict[str, Any]:
    """يستخرج الموعد النهائي من نص الطلب.

    يعيد: {"level": critical|today|soon|none, "label": تسمية عربية أو "",
           "marker": الكلمة المطابقة أو ""}
    فشل-آمن: أي استثناء ⇒ {"level": "none", "label": "", "marker": ""}.
    """
    empty = {"level": "none", "label": "", "marker": ""}
    try:
        if not isinstance(text, str) or not text.strip():
            return empty
        n = normalize_light(text)
        for key, label, pattern in _DEADLINE_COMPILED:
            try:
                matches = pattern.findall(n)
            except Exception:
                continue
            if matches:
                first = matches[0] if isinstance(matches[0], str) else ""
                return {"level": key, "label": label, "marker": first.strip()}
        return empty
    except Exception:
        return empty


def priority_tier(analysis: Dict[str, Any]) -> str:
    """يحدد شارة الأولوية المعروضة في التنبيه (أعلى شارة تحكم).

    الترتيب: موعد حرج > موعد اليوم > عاجل من المحرك > ثقة مرتفعة > عادي.
    """
    try:
        a = analysis or {}
        dl = a.get("deadline") if isinstance(a.get("deadline"), dict) else {}
        level = dl.get("level", "none")
        if level == "critical":
            return "🔥 عاجل جداً"
        if level == "today":
            return "⚡ اليوم"
        if a.get("urgent"):
            return "⚡ عاجل"
        if level == "soon":
            return "📌 قريب"
        conf = a.get("confidence")
        if isinstance(conf, (int, float)) and conf >= 0.85:
            return "⭐ طلب واضح"
        return ""
    except Exception:
        return ""


def _count_hits(pattern: re.Pattern, text: str) -> List[str]:
    try:
        return [m.group(1) for m in pattern.finditer(text) if m.group(1)]
    except Exception:
        return []


def classify_text(text: str) -> Dict[str, Any]:
    """يصنف نص الطلب: المادة + نوع المطلوب.

    يعيد:
        {"subject": تسمية عربية أو "", "subject_key": مفتاح أو "",
         "type": تسمية نوع أو "", "type_key": مفتاح أو "",
         "subject_matches": [...], "type_matches": [...],
         "classified": bool}
    فشل-آمن: أي استثناء ⇒ {"classified": False} مع الحقول الفارغة.
    """
    empty = {"subject": "", "subject_key": "", "type": "", "type_key": "",
             "subject_matches": [], "type_matches": [], "classified": False}
    try:
        if not isinstance(text, str) or not text.strip():
            return empty
        n = normalize_light(text)
        n = _NON_WORD.sub(" ", n)
        if not n.strip():
            return empty

        best_sub, best_sub_hits, best_sub_score = "", "", 0
        for key, label, pattern in _COMPILED_SUBJECTS:
            hits = _count_hits(pattern, n)
            # الكلمات العامة تحتاج تكراراً أو اقتراناً لتُحتسب
            weight = sum(1 for h in hits)
            if not hits:
                continue
            strong = any(h not in _GENERIC_SUBJECT_HINTS for h in hits)
            score = weight * (2 if strong else 1)
            if score > best_sub_score:
                best_sub, best_sub_hits, best_sub_score = label, hits, score

        best_typ, best_typ_hits, best_typ_score = "", "", 0
        for key, label, pattern in _COMPILED_TYPES:
            hits = _count_hits(pattern, n)
            if hits and len(hits) > best_typ_score:
                best_typ, best_typ_hits, best_typ_score = label, hits, len(hits)

        return {
            "subject": best_sub,
            "subject_key": next((k for k, lb, _ in SUBJECTS if lb == best_sub), ""),
            "type": best_typ,
            "type_key": next((k for k, lb, _ in TYPES if lb == best_typ), ""),
            "subject_matches": best_sub_hits[:5],
            "type_matches": best_typ_hits[:5],
            "classified": bool(best_sub or best_typ),
        }
    except Exception:
        return empty


def classification_line(analysis: Dict[str, Any]) -> str:
    """سطر التصنيف الجاهز للعرض (نص عادي) من قاموس التحليل."""
    try:
        parts: List[str] = []
        subject = (analysis or {}).get("subject")
        type_tag = (analysis or {}).get("type_tag")
        urgent = (analysis or {}).get("urgent")
        conf = (analysis or {}).get("confidence")
        if subject:
            parts.append(f"#{subject}")
        if type_tag:
            parts.append(str(type_tag))
        # v10.0 Precision: شارة الأولوية (موعد نهائي/عاجل/ثقة) — تحل محل
        # شارة العاجل القديمة عند وجود أولوية أعلى.
        tier = priority_tier(analysis)
        if tier:
            parts.append(tier)
        elif urgent:
            parts.append("⚡ عاجل")
        if isinstance(conf, (int, float)) and conf > 0:
            parts.append(f"الثقة {int(round(float(conf) * 100))}%")
        return " • ".join(parts)
    except Exception:
        return ""
