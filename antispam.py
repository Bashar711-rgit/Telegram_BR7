#!/usr/bin/env python3
"""
antispam.py — نظام مكافحة المعلنين والسبام (المرحلة الثانية)

الهدف (كما طلب المستخدم):
  * تقليل استهلاك الموارد وتقليل التنبيهات المزعجة.
  * منع المعلنين المتكررين (Cross-Group Spam / Mass Poster).
  * تحسين جودة الرسائل المرسلة للمشرفين.

التصميم — ثلاث مراحل:

  1) Watch List (قائمة المراقبة) — لا حظر ولا تجاهل، مراقبة 10 دقائق فقط:
       الشرط 1: نشر رسالتين أو أكثر خلال 60 ثانية (مجموعة واحدة أو أكثر).
       الشرط 2: رسائل متشابهة نصياً/دلالياً ≥ 80% خلال 5 دقائق.
       الشرط 3: أكثر من طلب من فئات الطلبات التعليمية خلال 5 دقائق
                (واجبات/بحوث/مشاريع/برمجة/تصميم/عروض/تقارير/ترجمة/خدمات أكاديمية).

  2) Spam Confirmation (تأكيد السبام) — المستخدم موجود مسبقاً في قائمة
     المراقبة ثم تحقق أي شرط من:
       أ) طلب جديد أو معاد الصياغة أثناء فترة المراقبة.
       ب) رسالة إضافية أو أكثر خلال 5 دقائق.
       ج) الانتقال إلى مجموعات أخرى بنفس الغرض.
       د) عدة طلبات تعليمية مختلفة خلال فترة قصيرة.
       هـ) 5 رسائل أو أكثر خلال 10 دقائق، أو 3 مجموعات أو أكثر خلال 10 دقائق.

  3) التصنيف المباشر كمزعج (دون المرور بالمراقبة):
       نفس الرسالة أو نفس المعنى (تشابه ≥ العتبة) في 4 مجموعات أو أكثر
       خلال 5 دقائق → Cross-Group Spam / Mass Poster فوراً.

الإجراء النهائي بعد التأكيد:
  * إيقاف جميع التنبيهات الخاصة بالمستخدم.
  * تجاهل جميع رسائله المستقبلية (عدم معالجتها مرة أخرى — نقطة الفحص
    المبكرة في _validate_event).
  * إضافته إلى Permanent Ignore List (جدول spam_ignore — ينجو من إعادة
    التشغيل) مع تسجيل سبب الحظر بالتفصيل.

مبادئ التنفيذ:
  * محرك مستقل تماماً — لا يعدّل منطق الفلترة الحالي إطلاقاً.
  * كل العتبات قابلة للتعديل الحي من لوحة التحكم (CFG.ANTISPAM_*).
  * فشل-آمن: أي استثناء داخلي = "allow" (لا يُوقف النظام ولا يُحظر أحد
    بالخطأ بسبب خلل في مكافحة السبام نفسها).
  * ذاكرة محدودة: سجل نشاط محدود لكل مستخدم + سقف للمستخدمين المتتبعين
    (TTLCache) + كنس كسول للمراقبة المنتهية.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from loguru import logger

try:
    from rapidfuzz import fuzz
    _FUZZ_AVAILABLE = True
except Exception:  # pragma: no cover — rapidfuzz is a hard dep, fallback anyway
    _FUZZ_AVAILABLE = False

from cachetools import TTLCache

from config import CFG
from dedup import normalize_for_fingerprint

# ─────────────────────────────────────────────────────────────────────────────
# فئات الطلبات التعليمية (كشف خاص بمكافحة السبام — مستقل عن filter_engine)
# مطابقة بادئة/جزء على النص المطبع الصغير؛ كل فئة تضم صيغها الشائعة.
# ─────────────────────────────────────────────────────────────────────────────
ANTISPAM_CATEGORY_TERMS: Dict[str, List[str]] = {
    "واجبات": ["واجب", "homework", "assignment", "اسايمنت", "اساينمنت"],
    "بحوث": ["بحث", "بحوث", "research", "ريسيرش"],
    "مشاريع": ["مشروع", "مشاريع", "project", "بروجكت"],
    "برمجة": ["برمج", "كود", "coding", "programming", "تطبيق", "موقع", "بايثون", "بيثون", "جافا", "ماتلاب", "ماتلاب"],
    "تصميم": ["تصميم", "design", "رسم هندسي", "اتوكاد", "اوتوكاد", "أوتوكاد"],
    "عروض تقديمية": ["عرض تقديمي", "عروض تقديمية", "بوربوينت", "powerpoint", "presentation", "سلاید", "سلايد", "slides"],
    "تقارير": ["تقرير", "تقارير", "report"],
    "ترجمة": ["ترجم", "translate", "translation"],
    "خدمات أكاديمية": ["خدمة اكاديمية", "خدمات اكاديمية", "خدمة أكاديمية", "خدمات أكاديمية", "مساعدة اكاديمية", "مساعدة أكاديمية"],
}


def detect_categories(text: str) -> Set[str]:
    """الفئات التي يظهر فيها مصطلح مطابق داخل النص (تطبيع خفيف بدون تشكيل)."""
    if not text:
        return set()
    t = normalize_for_fingerprint(text)
    if not t:
        return set()
    matched: Set[str] = set()
    for category, terms in ANTISPAM_CATEGORY_TERMS.items():
        for term in terms:
            if term in t:
                matched.add(category)
                break
    return matched


# ─────────────────────────────────────────────────────────────────────────────
# بنية سجل النشاط
# ─────────────────────────────────────────────────────────────────────────────
class _Activity:
    """سجل نشاط مستخدم واحد: (وقت، مجموعة، نص مطبع، فئات)."""

    __slots__ = ("records",)

    def __init__(self) -> None:
        self.records: Deque[Tuple[float, Any, str, Set[str]]] = deque()

    def trim(self, max_len: int, now: float, max_age: float = 1800.0) -> None:
        """حدّ السجل وأزل ما تجاوز 30 دقيقة (أطول نافذة في النظام = 10 دقائق)."""
        while len(self.records) > max_len:
            self.records.popleft()
        while self.records and (now - self.records[0][0]) > max_age:
            self.records.popleft()


class _WatchEntry:
    """إدخال قائمة مراقبة: أسباب + حتى متى."""

    __slots__ = ("reasons", "until", "added_at")

    def __init__(self, reasons: List[str], until: float) -> None:
        self.reasons: List[str] = reasons
        self.until: float = until
        self.added_at: float = time.time()


# ─────────────────────────────────────────────────────────────────────────────
# المحرك
# ─────────────────────────────────────────────────────────────────────────────
class AntiSpamEngine:
    """
    observe() هي نقطة الدخول الوحيدة — تُستدعى مرة واحدة لكل رسالة جديدة
    (من _analyze_and_alert بعد التحليل وقبل قرار التنبيه) وتعيد:
        ("allow", info)  — سلوك عادي (المستخدم ربما تحت المراقبة).
        ("watch", info)  — أُدرج/جُدد في قائمة المراقبة (لا تجاهل ولا حظر).
        ("spam",  info)  — تأكد السبام → تجاهل دائم + إيقاف التنبيهات.
    """

    def __init__(self) -> None:
        self._db: Any = None
        # sender_id → _Activity (TTL 30 دقيقة بلا نشاط، سقف ANTISPAM_MAX_TRACKED_USERS)
        self._activities: TTLCache = TTLCache(maxsize=CFG.ANTISPAM_MAX_TRACKED_USERS, ttl=1800)
        self._watch: Dict[int, _WatchEntry] = {}
        self._ignored: Set[int] = set()
        self._sweeps: int = 0
        # عدادات للوحة/الصحة
        self.stats: Dict[str, int] = {
            "observed": 0,
            "watch_added": 0,
            "spam_confirmed": 0,
            "direct_spam": 0,
            "ignored_skipped": 0,
            "errors": 0,
        }

    # ── التهيئة ─────────────────────────────────────────────────────────────
    async def setup(self, db: Any) -> None:
        """اربط قاعدة البيانات وحمّل قائمة التجاهل الدائم (تنجو من إعادة التشغيل)."""
        self._db = db
        try:
            rows = await db.get_spam_ignored(limit=100000) if db else []
            if rows:
                for r in rows:
                    try:
                        self._ignored.add(int(r.get("sender_id") or r["sender_id"]))
                    except Exception:
                        continue
            logger.info(
                f"AntiSpam engine ready: {len(self._ignored)} permanently-ignored "
                f"sender(s) loaded from DB | enabled={CFG.ANTISPAM_ENABLED}"
            )
        except Exception as e:
            # جدول spam_ignore قد لا يكون موجوداً بعد (أول تشغيل قبل إنشائه)
            logger.debug(f"AntiSpam setup: ignore-list load deferred ({e})")

    # ── التجاهل الدائم (فحص متزامن سريع — يُستدعى لكل رسالة واردة) ─────────
    def is_ignored(self, sender_id: Any) -> bool:
        try:
            return int(sender_id or 0) in self._ignored
        except Exception:
            return False

    async def confirm_spam(self, sender_id: Any, reasons: List[str], evidence: Optional[Dict] = None) -> None:
        """نفّذ الإجراء النهائي: تصنيف + تجاهل دائم + تسجيل السبب بالتفصيل."""
        sid = int(sender_id or 0)
        self._ignored.add(sid)
        self._watch.pop(sid, None)
        self._activities.pop(sid, None)
        reason_text = "؛ ".join(reasons) if reasons else "cross_group_spam"
        if self._db is not None:
            try:
                await self._db.add_spam_ignore(
                    sid,
                    reason=reason_text,
                    evidence=evidence or {},
                    classified="cross_group_spam",
                )
            except Exception as e:
                self.stats["errors"] += 1
                logger.error(f"AntiSpam: failed to persist ignore for {sid}: {e}")
        # تسجيل السبب بالتفصيل في السجلات (متطلب «تسجيل سبب الحظر بالتفصيل»)
        logger.warning(
            f"🚫 SPAM CONFIRMED | sender={sid} | classified=Cross-Group Spam / Mass Poster | "
            f"reasons={reason_text} | evidence={evidence or {}} — "
            f"alerts stopped, future messages will be ignored permanently"
        )

    async def unignore(self, sender_id: Any) -> bool:
        """إزالة من قائمة التجاهل الدائم (أمر المشرف /unspam)."""
        sid = int(sender_id or 0)
        self._ignored.discard(sid)
        if self._db is not None:
            try:
                await self._db.remove_spam_ignore(sid)
                return True
            except Exception as e:
                self.stats["errors"] += 1
                logger.error(f"AntiSpam: failed to remove ignore for {sid}: {e}")
                return False
        return True

    # ── نقطة الدخول: مراقبة رسالة جديدة ────────────────────────────────────
    async def observe(self, sender_id: Any, chat_id: Any, text: str, ts: Optional[float] = None) -> Tuple[str, Dict]:
        now = float(ts if ts is not None else time.time())
        info: Dict[str, Any] = {"sender_id": sender_id, "chat_id": chat_id}
        try:
            if not CFG.ANTISPAM_ENABLED:
                return "allow", info
            sid = int(sender_id or 0)
            if sid and sid in self._ignored:
                self.stats["ignored_skipped"] += 1
                # مصنّف مسبقاً — لا معالجة ولا تنبيه (تجاهل دائم قائم)
                info["reasons"] = ["مستخدم مصنّف مسبقاً كمزعج — تجاهل دائم قائم"]
                return "spam", info

            self.stats["observed"] += 1
            norm = normalize_for_fingerprint(text or "")
            cats = detect_categories(text or "")
            chat_key = chat_id  # قد يكون سالباً (-100…) — يُستخدم كمفتاح مجموعة

            act = self._activities.get(sid)
            if act is None:
                act = _Activity()
                self._activities[sid] = act
            act.records.append((now, chat_key, norm, cats))
            act.trim(CFG.ANTISPAM_MEMORY_PER_USER, now)

            self._lazy_sweep(now)

            # ══ 1) التصنيف المباشر: نفس الرسالة/المعنى في ≥ 4 مجموعات خلال 5 دقائق ══
            direct_groups, direct_hits = self._similar_group_spread(
                act, now, norm,
                window=CFG.ANTISPAM_DIRECT_SPAM_WINDOW_SECONDS,
                threshold=CFG.ANTISPAM_SIMILARITY_THRESHOLD,
            )
            if len(direct_groups) >= max(2, CFG.ANTISPAM_DIRECT_SPAM_GROUPS):
                self.stats["direct_spam"] += 1
                info["reasons"] = [
                    f"نفس الرسالة/المعنى في {len(direct_groups)} مجموعات خلال "
                    f"{CFG.ANTISPAM_DIRECT_SPAM_WINDOW_SECONDS // 60} دقائق (تصنيف مباشر)"
                ]
                info["evidence"] = {
                    "groups": len(direct_groups), "hits": direct_hits,
                    "window_seconds": CFG.ANTISPAM_DIRECT_SPAM_WINDOW_SECONDS,
                    "similarity_threshold": CFG.ANTISPAM_SIMILARITY_THRESHOLD,
                    "chats": [str(c) for c in list(direct_groups)[:10]],
                }
                # الإجراء النهائي فوراً (دون المرور بالمراقبة) — ذاتياً داخل
                # المحرك حتى لا يعتمد على تذكر المستدعي استدعاء confirm_spam.
                await self.confirm_spam(sid, info["reasons"], info["evidence"])
                return "spam", info

            # ══ 2) تأكيد السبام: المستخدم تحت مراقبة نشطة + أي شرط من الشروط ══
            entry = self._watch.get(sid)
            if entry is not None and now <= entry.until:
                reasons, evidence = self._confirmation_reasons(act, now, norm, cats, chat_key, entry)
                if reasons:
                    self.stats["spam_confirmed"] += 1
                    info["reasons"] = reasons
                    info["evidence"] = evidence
                    # نفس المنطق: التأكيد ذاتي داخل المحرك (تجاهل دائم + تسجيل السبب)
                    await self.confirm_spam(sid, reasons, evidence)
                    return "spam", info
                # تحت المراقبة لكن لم يتحقق شرط تأكيد بعد — استمرار المراقبة
                info["watched"] = True
                info["watch_reasons"] = list(entry.reasons)
                return "allow", info

            # ══ 3) قائمة المراقبة: أي شرط من شروط الإدراج الثلاثة ══
            watch_reasons: List[str] = []
            # (1) رسالتان أو أكثر خلال 60 ثانية
            burst = [r for r in act.records if now - r[0] <= CFG.ANTISPAM_BURST_WINDOW_SECONDS]
            if len(burst) >= max(2, CFG.ANTISPAM_BURST_MESSAGES):
                chats_in_burst = {r[1] for r in burst}
                watch_reasons.append(
                    f"{len(burst)} رسائل خلال {CFG.ANTISPAM_BURST_WINDOW_SECONDS} ثانية "
                    f"في {len(chats_in_burst)} مجموعة"
                )
            # (2) تشابه ≥ 80% خلال 5 دقائق (مقارنة بالسوابق فقط — بدون الحالي)
            sim_ratio, _ = self._max_similarity(
                act, now, norm, CFG.ANTISPAM_SIMILARITY_WINDOW_SECONDS, skip_current=True
            )
            if norm and sim_ratio >= CFG.ANTISPAM_SIMILARITY_THRESHOLD:
                watch_reasons.append(
                    f"رسائل متشابهة بنسبة {round(sim_ratio * 100)}% خلال "
                    f"{CFG.ANTISPAM_SIMILARITY_WINDOW_SECONDS // 60} دقائق"
                )
            # (3) أكثر من طلب من فئات مختلفة خلال 5 دقائق — عبر عدة رسائل:
            # رسالة واحدة تذكر عدة فئات (إعلان خدمات مثلاً) = طلب واحد،
            # وليست طلبات متعددة — يشترط رسالتان تحملان فئات في النافذة.
            cats_window = set()
            msgs_with_cats = 0
            for r in act.records:
                if now - r[0] <= CFG.ANTISPAM_CATEGORY_WINDOW_SECONDS and r[3]:
                    msgs_with_cats += 1
                    cats_window.update(r[3])
            if len(cats_window) >= 2 and msgs_with_cats >= 2:
                watch_reasons.append(
                    f"طلبات من {len(cats_window)} فئات خلال "
                    f"{CFG.ANTISPAM_CATEGORY_WINDOW_SECONDS // 60} دقائق: {'، '.join(sorted(cats_window))}"
                )

            if watch_reasons:
                self._watch[sid] = _WatchEntry(
                    reasons=watch_reasons, until=now + max(60, CFG.ANTISPAM_WATCH_DURATION_SECONDS)
                )
                self.stats["watch_added"] += 1
                # تسجيل أسباب الإدراج (متطلب صريح) — ذاكرة + قاعدة بيانات
                logger.info(
                    f"👀 WATCH LIST | sender={sid} | reasons={'؛ '.join(watch_reasons)} | "
                    f"duration={CFG.ANTISPAM_WATCH_DURATION_SECONDS}s"
                )
                if self._db is not None:
                    try:
                        await self._db.record_watch(
                            sid,
                            reason="؛ ".join(watch_reasons),
                            evidence={"chats": len({r[1] for r in act.records if now - r[0] <= 600})},
                            watch_until=now + max(60, CFG.ANTISPAM_WATCH_DURATION_SECONDS),
                        )
                    except Exception as e:
                        self.stats["errors"] += 1
                        logger.debug(f"AntiSpam: record_watch failed: {e}")
                info["watch_reasons"] = watch_reasons
                return "watch", info

            return "allow", info
        except Exception as e:
            # فشل-آمن: خلل في مكافحة السبام لا يمنع التنبيهات أبداً
            self.stats["errors"] += 1
            logger.error(f"AntiSpam observe error (fail-open): {e}")
            return "allow", info

    # ── أدوات داخلية ────────────────────────────────────────────────────────
    def _similar_group_spread(
        self, act: _Activity, now: float, norm: str, window: float, threshold: float
    ) -> Tuple[Set[Any], int]:
        """مجموعات الرسائل المتشابهة (≥ threshold) للنص الحالي خلال النافذة.

        يتضمن السجل الحالي (مجموعته تُحتسب — نفس الرسالة في 4 مجموعات تعني
        المجموعة الرابعة أيضاً) — هذا مقصود لتصنيف Cross-Group Spam.
        """
        groups: Set[Any] = set()
        hits = 0
        if not norm:
            # نص فارغ (ميديا) — تجميع حسب المجموعات الخام خلال النافذة
            for r in act.records:
                if now - r[0] <= window:
                    groups.add(r[1])
                    hits += 1
            return groups, hits
        for r in act.records:
            if now - r[0] > window:
                continue
            if r[2] == norm or self._ratio(norm, r[2]) >= threshold:
                groups.add(r[1])
                hits += 1
        return groups, hits

    def _max_similarity(
        self, act: _Activity, now: float, norm: str, window: float, skip_current: bool = False
    ) -> Tuple[float, float]:
        """أعلى نسبة تشابه بين النص الحالي والرسائل السابقة داخل النافذة.

        skip_current=True يستثني السجل الأخير (الرسالة الحالية نفسها) —
        إلزامي عند الاستدعاء بعد إلحاق الرسالة الحالية بسجل النشاط، وإلا
        لكانت كل رسالة متشابهة مع نفسها بنسبة 100%.
        """
        best = 0.0
        best_at = 0.0
        if not norm:
            return 0.0, 0.0
        records = act.records
        if skip_current and records:
            records = records.__class__(list(records)[:-1])
        for r in records:
            if now - r[0] > window or not r[2]:
                continue
            ratio = self._ratio(norm, r[2])
            if ratio > best:
                best, best_at = ratio, r[0]
        return best, best_at

    @staticmethod
    def _ratio(a: str, b: str) -> float:
        if a == b:
            return 1.0
        if not _FUZZ_AVAILABLE:
            return 1.0 if a == b else 0.0
        try:
            return float(fuzz.ratio(a, b)) / 100.0
        except Exception:
            return 0.0

    def _confirmation_reasons(
        self, act: _Activity, now: float, norm: str, cats: Set[str], chat_key: Any, entry: _WatchEntry
    ) -> Tuple[List[str], Dict]:
        """شروط تأكيد السبام الخمسة (أي شرط يكفي) — تُعاد الأسباب + الأدلة."""
        reasons: List[str] = []
        evidence: Dict[str, Any] = {"watch_reasons": list(entry.reasons)}
        recent_5m = [r for r in act.records if now - r[0] <= CFG.ANTISPAM_CONFIRM_WINDOW_SECONDS]
        recent_10m = [r for r in act.records if now - r[0] <= CFG.ANTISPAM_CONFIRM_ACTIVITY_WINDOW_SECONDS]
        chats_10m = {r[1] for r in recent_10m}

        # (أ) طلب جديد أو معاد الصياغة أثناء المراقبة
        if cats:
            reasons.append(f"طلب جديد أثناء المراقبة من فئة: {'، '.join(sorted(cats))}")
        elif norm:
            sim, _ = self._max_similarity(
                act, now, norm, CFG.ANTISPAM_SIMILARITY_WINDOW_SECONDS, skip_current=True
            )
            if sim >= CFG.ANTISPAM_SIMILARITY_THRESHOLD:
                reasons.append(f"طلب معاد الصياغة أثناء المراقبة (تشابه {round(sim * 100)}%)")

        # (ب) رسالة إضافية أو أكثر خلال 5 دقائق
        if len(recent_5m) >= 2:
            reasons.append(f"{len(recent_5m)} رسائل خلال {CFG.ANTISPAM_CONFIRM_WINDOW_SECONDS // 60} دقائق أثناء المراقبة")

        # (ج) الانتقال إلى مجموعات أخرى بنفس الغرض (مجموعة جديدة + تشابه/فئة)
        prior_chats = {r[1] for r in act.records if r[0] < now and now - r[0] <= CFG.ANTISPAM_SIMILARITY_WINDOW_SECONDS}
        if chat_key not in prior_chats and len(prior_chats) >= 1:
            same_purpose = False
            if cats:
                for r in act.records:
                    if now - r[0] <= CFG.ANTISPAM_SIMILARITY_WINDOW_SECONDS and (r[3] & cats):
                        same_purpose = True
                        break
            if not same_purpose and norm:
                sim, _ = self._max_similarity(
                    act, now, norm, CFG.ANTISPAM_SIMILARITY_WINDOW_SECONDS, skip_current=True
                )
                same_purpose = sim >= CFG.ANTISPAM_SIMILARITY_THRESHOLD
            if same_purpose:
                reasons.append(
                    f"الانتقال إلى مجموعة جديدة بنفس الغرض (المجموعات النشطة: {len(prior_chats) + 1})"
                )

        # (د) عدة طلبات تعليمية مختلفة خلال فترة قصيرة (عبر عدة رسائل)
        cats_window: Set[str] = set()
        msgs_with_cats = 0
        for r in recent_5m:
            if r[3]:
                msgs_with_cats += 1
                cats_window.update(r[3])
        if len(cats_window) >= 2 and msgs_with_cats >= 2:
            reasons.append(f"طلبات من فئات مختلفة خلال فترة قصيرة: {'، '.join(sorted(cats_window))}")

        # (هـ) 5 رسائل خلال 10 دقائق أو 3 مجموعات خلال 10 دقائق
        if len(recent_10m) >= max(2, CFG.ANTISPAM_CONFIRM_MESSAGES):
            reasons.append(
                f"{len(recent_10m)} رسائل خلال {CFG.ANTISPAM_CONFIRM_ACTIVITY_WINDOW_SECONDS // 60} دقائق"
            )
        if len(chats_10m) >= max(2, CFG.ANTISPAM_CONFIRM_GROUPS):
            reasons.append(
                f"{len(chats_10m)} مجموعات خلال {CFG.ANTISPAM_CONFIRM_ACTIVITY_WINDOW_SECONDS // 60} دقائق"
            )

        evidence.update({
            "messages_10m": len(recent_10m),
            "chats_10m": len(chats_10m),
            "messages_5m": len(recent_5m),
        })
        return reasons, evidence

    def _lazy_sweep(self, now: float, every: int = 200) -> None:
        """إزالة إدخالات المراقبة المنتهية كل 200 عملية مراقبة."""
        self._sweeps += 1
        if self._sweeps % every != 0:
            return
        expired = [sid for sid, e in self._watch.items() if now > e.until]
        for sid in expired:
            self._watch.pop(sid, None)

    # ── تقارير ──────────────────────────────────────────────────────────────
    def active_watch_count(self) -> int:
        now = time.time()
        return sum(1 for e in self._watch.values() if now <= e.until)

    def snapshot(self) -> Dict[str, Any]:
        """لقطة للوحة /health و /spam."""
        return {
            "enabled": bool(CFG.ANTISPAM_ENABLED),
            "tracked_users": len(self._activities),
            "active_watch": self.active_watch_count(),
            "permanently_ignored": len(self._ignored),
            **dict(self.stats),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Singleton (نفس نمط dedup.py — مشترك بين كل المراقبين)
# ─────────────────────────────────────────────────────────────────────────────
_engine: Optional[AntiSpamEngine] = None


def init_antispam(db: Any) -> AntiSpamEngine:
    """اربط المحرك بقاعدة البيانات (متزامن — يُستدعى مبكراً إن لزم)."""
    global _engine
    if _engine is None:
        _engine = AntiSpamEngine()
    _engine._db = db
    return _engine


async def setup_antispam(db: Any) -> AntiSpamEngine:
    """اربط + حمّل قائمة التجاهل الدائم — يُستدعى من main.initialize بعد db.connect."""
    engine = init_antispam(db)
    await engine.setup(db)
    return engine


def get_antispam() -> AntiSpamEngine:
    global _engine
    if _engine is None:
        _engine = AntiSpamEngine()
    return _engine


def get_antispam_snapshot() -> Dict[str, Any]:
    try:
        return get_antispam().snapshot()
    except Exception:
        return {}
