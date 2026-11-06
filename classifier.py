#!/usr/bin/env python3
"""
classifier.py — v14.5.1: التصنيف الدقيق لطلبات الطلاب.

v14.5.1 — correction pass:
  * Arabic definite article ("ال") is now handled correctly. The
    previous _mk_pattern matched each term as a bare literal ("واجب",
    "رياضيات", ...). Any Arabic input with a definite article
    ("الواجب", "الرياضيات", "البحث") failed the word-boundary check —
    "ال" sits directly before the keyword, so neither `^`, whitespace,
    nor punctuation could satisfy the boundary. In Arabic prose those
    definite forms are the natural ones ("الواجب صعب"), so the
    classifier missed the subject/type of nearly every real student
    message. The fix: _mk_pattern() takes al_optional=True for
    SUBJECTS and TYPES and emits `(?:ال)?<word>` per word in the term
    (matching what filter_engine's _expand_al_variants already does for
    keyword trie matching). DEADLINE terms stay literal because several
    of their entries ("اليوم", "الليله", "الان") already start with
    the article and would be mangled by a naive strip.
  * Dead terms removed from SUBJECTS["programming"]: "c++", "c#", "++c".
    _NON_WORD strips "+" and "#" from the INPUT before matching, so any
    pattern that re.escape()s them (which produces "c\\+\\+", "c\\#")
    can NEVER match. They were pure noise. "csharp" and the Arabic
    "سي شارب" remain and cover the same concept. Added the Arabic
    "سي بلس بلس" as the practical replacement for "c++".
  * _DIACRITICS_REPEAT is now scoped to Arabic letters only
    ([\u0621-\u064A]\1{2,}) — matching dedup._FP_AR_REPEATS v14.5.1.
    The previous (.)\1{2,} collapsed 3+ repeats of ANY character, so
    "1000" became "10" and Latin elongated words lost content. No
    pattern in this file depends on the collapse for Latin/digits, so
    the previous behavior didn't produce false positives — but it made
    normalize_light() silently diverge from the dedup module's
    normalization, which is exactly the kind of drift that makes
    future debugging impossible.
  * _GENERIC_SUBJECT_HINTS check is now "ال"-aware: a matched hit like
    "العربي" is compared as "عربي" so the weak-signal logic keeps
    working after the "ال" expansion above. Without this, "العربي"
    would have been treated as a strong signal despite being exactly
    the generic word the set was meant to de-weight.
  * SUBJECTS/TYPES cleanup: "شريعه اسلاميه".split()[0] replaced with the
    literal "شريعه" (the split was a no-op masquerading as code);
    no-op ".replace('اجراءات', 'إجراءات')" removed (normalize_light
    already unifies إ → ا on both sides, making the replace pointless);
    the inline Persian-گ/ی → ج/ي replaces kept but noted — they are
    kept for documentation even though normalize_light does not
    perform them on the input side (a Persian-typed گ will NOT be
    matched by the "رجسيون" pattern; documented limitation).
  * __version__ = "14.5.1".

⚠ Integration note (not a bug in this file): the current alert card
format (v11.6+) does not display subject/type_tag/deadline, and
monitors._analyze_and_alert stores them in `analysis` without passing
them to the alert builder. classification_line() and priority_tier()
are therefore unused today. The classifier's cost (regex scans over
normalized text) is small but non-zero; if the card format is not
planned to grow a classification line, consider skipping the classify
call in monitors._analyze_and_alert to save a few microseconds per
message. Deliberately left unchanged here — the display decision is
owned by alert_bot/monitors, not by classifier.

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

__version__ = "14.5.1"

# ---------------------------------------------------------------------------
# تطبيع عربي خفيف (مطابقة فقط — لا يمس النص الأصلي)
# ---------------------------------------------------------------------------
_TASHKEEL = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_TATWEEL = re.compile(r"\u0640+")
# v14.5.1: عربي فقط — نفس dedup._FP_AR_REPEATS (3+ → 1).
# كان (.)\1{2,} فيُقلّص أي حرف (Latin/digit) — انحراف عن dedup يُربك
# المراجعة المستقبلية دون فائدة (لا نمط في الملف يعتمد عليه).
_DIACRITICS_REPEAT = re.compile(r"([\u0621-\u064A])\1{2,}")  # ضرورررري → ضروري
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


def _mk_pattern(
    terms: List[str],
    word_boundary: bool = True,
    al_optional: bool = False,
) -> re.Pattern:
    """يبني regex واحداً من قائمة مصطلحات مطبَّعة — الأطول أولاً.

    v14.5.1: al_optional=True يجعل كل كلمة داخل المصطلح تقبل "ال"
    التعريف اختيارياً: "واجب" → `(?:ال)?واجب` (يطابق "واجب" و"الواجب")،
    "لغه عربيه" → `(?:ال)?لغه\\s+(?:ال)?عربيه` (يطابق كل التنويعات الأربع).
    هذا يُطابق سلوك filter_engine._expand_al_variants الذي يبنيه المحرك
    لـ keywords؛ بدونه كان classifier لا يتعرف على "الواجب"/"الرياضيات"
    رغم أنها الشكل الطبيعي في العربية.
    """
    parts: List[str] = []
    for t in terms:
        nt = _norm_word(t).strip()
        if not nt:
            continue
        if al_optional:
            words = nt.split()
            pattern_words = []
            for w in words:
                base = w[2:] if w.startswith("ال") else w
                pattern_words.append(rf"(?:ال)?{re.escape(base)}")
            parts.append(r"\s+".join(pattern_words))
        else:
            parts.append(re.escape(nt))
    # v14.5.1: dedupe مع الحفاظ على الترتيب (قد تنتج تكرارات بعد التوسيع)
    parts = list(dict.fromkeys(parts))
    body = "|".join(parts)
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
        "powerpoint", "أوفيس", "افيس", "office", "Pivot", "vlookup",
    ]),
    ("programming", "برمجة", [
        "برمجه", "برمجة", "كود", "بايثون", "python", "جافا", "java", "جافاسكربت",
        "javascript", "js", "html", "css", "سي شارب", "csharp",
        "سي بلس بلس",  # v14.5.1: بديل "c++" الذي لا يُطابق بعد _NON_WORD
        "خوارزميه", "خوارزمية", "الخوارزميات", "سحقوان", "sql", "mysql",
        "flutter", "فلاتر", "تطبيق جوال", "موقع الكتروني", "لعبه الكترونيه",
        "اندرويد", "android", "react", "ريأكت",
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
        "رجسيون", "انحدار", "استقراء", "مينيتاب",
        # ملاحظة: "رگرسيون" (بالفارسية) لن يُطابق "رجسيون" لأن normalize_light
        # لا يستبدل گ → ج في المدخل. مذكور كقيد موثق، ليس خطأ قاتلاً.
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
        "حقوق", "قانون", "نظم",
        "شريعه",  # v14.5.1: بدل "شريعه اسلاميه".split()[0] الذي كان no-op
        "أنظمه", "عقود", "مرافعات",
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
        "تربيه", "مناهج", "طرق تدريس", "قياس وتقويم",
        "اجراءات تعليميه",  # v14.5.1: حُذف .replace("اجراءات","إجراءات") — no-op
    ]),
    ("design", "تصميم", [
        "تصميم", "فوتوشوب", "photoshop", "illustrator", "كانفا", "canva",
        "موشن", "مونتاج", "premier", "بريمير",
    ]),
    ("translation", "ترجمة", [
        "ترجمه", "ترجمة", "مترجم", "ترجم نص", "ترجمه فوريه",
    ]),
]

# v14.5.1: al_optional=True — يطابق "واجب" و"الواجب"، "رياضيات" و"الرياضيات".
_COMPILED_SUBJECTS: List[Tuple[str, str, re.Pattern]] = [
    (key, label, _mk_pattern(terms, al_optional=True))
    for key, label, terms in SUBJECTS
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
    ("presentation", "عرض تقديمي", ["عرض تقديمي", "برزنتيشن", "presentation", "سلايدز", "slides"]),
    ("solution", "حل", ["حل", "يحل", "اقدر احل", "حل اسئله", "حل نماذج", "solve", "solution"]),
    ("translation_type", "ترجمة نص", ["ترجمه نص", "ترجمه فوريه", "ترجمه ملف"]),
]

_COMPILED_TYPES: List[Tuple[str, str, re.Pattern]] = [
    (key, label, _mk_pattern(terms, al_optional=True))
    for key, label, terms in TYPES
]

# كلمات عامة لا تصلح وحدها لتعيين مادة (تُهمش عند الحسم)
# v14.5.1: مع al_optional، المطابقة قد تكون "العربي"/"الحساب" — المقارنة
# تتم على النص بعد إزالة "ال" (انظر _is_generic_hit).
_GENERIC_SUBJECT_HINTS = {"عربي", "حساب"}


def _is_generic_hit(hit: str) -> bool:
    """v14.5.1: هل هذا تطابق "عام" (لا يُرجّح مادة) بعد إزالة ال التعريف؟"""
    try:
        base = hit[2:] if hit.startswith("ال") else hit
        return base in _GENERIC_SUBJECT_HINTS
    except Exception:
        return False


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

# v14.5.1: DEADLINE يبقى al_optional=False — عدة مصطلحات تبدأ بـ"ال"
# أصلاً ("اليوم" و"الليله" و"الان")، فيجب مطابقتها حرفياً لا بعد التجريد.
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
            if not hits:
                continue
            weight = sum(1 for h in hits)
            # v14.5.1: استخدام _is_generic_hit ليعمل مع "العربي"/"الحساب"
            strong = any(not _is_generic_hit(h) for h in hits)
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
