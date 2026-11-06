#!/usr/bin/env python3
"""
similarity.py — v14.5.1 Precision Edition: حاجز منع التنبيهات المتشابهة.

المشكلة التي يحلها
------------------
dedup.py يمنع التكرار الحرفي فقط (نفس المرسل + نفس النص بعد تطبيع).
أما إعادة الصياغة القريبة من نفس المرسل خلال فترة قصيرة:

    «ابي احد يساعدني بالواجب»
    «ابي احد يساعدني في الواجب ضروري»

فتمر من الحاجز الحرفي وتُنبيه القناة مرتين لطلب واحد.

v14.5.1 — correction pass:
  * check_and_record() now SKIPS anonymous senders (sender_id falsy).
    The gate is keyed by sender_id; when it was 0 (unknown sender —
    anonymous channel admin, deleted account, or an entity resolution
    failure), every such message landed in bucket[0] and got compared
    against every other anonymous message across all chats. Two real
    but UNRELATED requesters in different groups could consequently
    have their (similar) messages blocked as if they came from the
    same person. dedup.py already isolates anonymous senders via
    `anon:{chat_id}` domains — the similarity gate has no chat_id
    parameter and cannot do the same, so the safest fix is to skip
    similarity for sender_id 0 while keeping literal dedup intact
    (which still catches exact repeats across all senders).
  * All `import re` statements hoisted to module top (the file used to
    do `import re as _re` inside _shingles and `import re as
    _digit_re_mod` mid-file, both needlessly).
  * _mask_digits(norm) hoisted out of the per-key loop (was recomputed
    on every bucket entry — same result, wasted work on the hot path).
  * __version__ = "14.5.1" added (was absent; the module had no
    version marker while every other module in the stack did).
  * _recent type annotation simplified to Dict[str, Tuple[...]]
    (runtime remains OrderedDict for LRU — same pattern used in
    dedup.py v14.5.1).

الحل: بصمة تشابه مزدوجة داخل الذاكرة:
  1) difflib.SequenceMatcher على النص المطبَّع (مقاوم لاستبدال حرف/كلمة).
  2) Jaccard على shingles حرفية (3-gram) كبديل مكمل.
نأخذ الأعلى من القياسين، ونمنع عند تجاوز عتبة محافظة (افتراضي 0.88) —
أي أن «المتشابهة جزئياً» (متطلب أبقيته دائماً) لا تُحذف؛ فقط
الشبه-مطابقة تُمنع.

التصميم:
  * ذاكرة فقط (بلا DB): الفاقد عند إعادة التشغيل مقبول — الحماية
    الحرفية الدائمة تبقى في dedup.py. السقف LRU لكل مرسل.
  * مطابقة المرسل هي المعيار: مرسلان مختلفان بنفس النص = طلبان يصلمان
    (نفس فلسفة dedup.py). المرسل المجهول (sender_id=0) يُستثنى كلياً
    (v14.5.1) لأن الحاجز لا يستطيع التمييز بين مجهولين مختلفين بلا
    chat_id — الحماية الحرفية عبر dedup.py تبقى سارية.
  * فشل-آمن: أي استثناء ⇒ لا منع (التنبيه يمر) — لا يُفقد طلب حقيقي.
  * قابلة للتعديل الحي: SIMILARITY_ENABLED / SIMILARITY_THRESHOLD /
    SIMILARITY_WINDOW_SECONDS تُقرأ من CFG عند كل فحص.
"""

from __future__ import annotations

import difflib
import re
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from loguru import logger

from config import CFG
from dedup import normalize_for_fingerprint

__version__ = "14.5.1"

# ─────────────────────────────────────────────────────────────────────────────
# Shingles — أحرف n-gram على النص المطبَّع
# ─────────────────────────────────────────────────────────────────────────────
_SHINGLE_N = 3
# v14.5.1: التطبيع في أعلى الملف — كان داخل _shingles لكل استدعاء.
_SHINGLE_WS_RE = re.compile(r"\s+")
# v14.5.1: mask digits once per comparison, not per bucket entry.
_DIGIT_MASK_RE = re.compile(r"\d")


def _shingles(norm_text: str, n: int = _SHINGLE_N) -> frozenset:
    """مجموعة shingles حرفية من نص مطبَّع — تُهمل المسافات المضاعفة."""
    try:
        t = _SHINGLE_WS_RE.sub(" ", norm_text).strip()
        t = t.replace(" ", "_")  # حرف المسافة جزء من السياق لكن موحّد
        if len(t) < n:
            return frozenset({t}) if t else frozenset()
        return frozenset(t[i : i + n] for i in range(len(t) - n + 1))
    except Exception:
        return frozenset()


def jaccard(a: frozenset, b: frozenset) -> float:
    """معامل جاكارد بين مجموعتي shingles — 0..1 (1 = تطابق كامل)."""
    try:
        if not a or not b:
            return 0.0
        inter = len(a & b)
        union = len(a | b)
        return inter / union if union else 0.0
    except Exception:
        return 0.0


def similarity_ratio(norm_a: str, norm_b: str, sh_a=None, sh_b=None) -> float:
    """قياس تشابه مزدوج — أعلى difflib ratio و Jaccard (3-gram).

    Args:
        norm_a/norm_b: النصان بعد normalize_for_fingerprint.
        sh_a/sh_b: shingles محسوبة مسبقاً (اختياري — توفير حساب).
    """
    try:
        r_seq = difflib.SequenceMatcher(None, norm_a or "", norm_b or "").ratio()
        r_jac = jaccard(
            sh_a if sh_a is not None else _shingles(norm_a),
            sh_b if sh_b is not None else _shingles(norm_b),
        )
        return max(r_seq, r_jac)
    except Exception:
        return 0.0


# v11.7 (audit F-1): أداة المقارنة بعد حجب الأرقام — زوج نصوص متطابق
# بعد استبدال الأرقام بعلامة = طلبان مختلفان فعلاً (درجات/أرقام/مواعيد):
# «النهائية هي 95 من 100» و«النهائية هي 94 من 100» مطلبان مستقلان،
# بينما difflib يعطيهما ~0.97 ويمنعهما كتكرار (عيب مُثبت بإعادة إنتاج حية).


def _mask_digits(norm_text: str) -> str:
    """يستبدل كل رقم بعلامة موحدة للمقارنة فقط (لا يغيّر النص المخزّن)."""
    try:
        return _DIGIT_MASK_RE.sub("#", norm_text or "")
    except Exception:
        return norm_text


class SimilarityGate:
    """حاجز منع التنبيهات المتشابهة (نفس المرسل، إعادة صياغة، نافذة زمنية).

    الاستخدام في monitors.py::_send_alert (بعد حاجز dedup الحرفي):
        gate = get_similarity_gate()
        hit = gate.check_and_record(sender_id, text)
        if hit is not None:
            # تنبيه مشابه مُنع — hit يحمل تفاصيل المطابقة للسجل
            return
    """

    def __init__(self, per_sender_keep: int = 8, senders_max: int = 4096):
        self._keep = max(1, int(per_sender_keep))
        self._senders_max = max(64, int(senders_max))
        # sender_id -> OrderedDict[norm_text] = (shingles, ts).
        # v14.5.1: annotation مبسّط Dict — runtime يبقى OrderedDict (LRU).
        self._recent: Dict[int, "OrderedDict[str, Tuple[frozenset, float]]"] = (
            OrderedDict()
        )  # type: ignore[assignment]
        self._stats: Dict[str, int] = {
            "checked": 0,
            "recorded": 0,
            "blocked": 0,
            "evicted": 0,
            "skipped_anon": 0,  # v14.5.1: مرسل مجهول — تخطي (انظر check_and_record)
        }

    # ── إعدادات حية من CFG ──
    @property
    def enabled(self) -> bool:
        return bool(getattr(CFG, "SIMILARITY_ENABLED", True))

    @property
    def threshold(self) -> float:
        try:
            t = float(getattr(CFG, "SIMILARITY_THRESHOLD", 0.88))
            return min(0.999, max(0.5, t))
        except (TypeError, ValueError):
            return 0.88

    @property
    def window_seconds(self) -> int:
        try:
            return max(0, int(getattr(CFG, "SIMILARITY_WINDOW_SECONDS", 3600)))
        except (TypeError, ValueError):
            return 3600

    # ── إدارة الذاكرة ──
    def _evict_sender(self) -> None:
        while len(self._recent) > self._senders_max:
            self._recent.popitem(last=False)  # type: ignore[attr-defined]
            self._stats["evicted"] += 1

    def _gc_sender(self, sender_id: int, now: float) -> None:
        bucket = self._recent.get(sender_id)
        if not bucket:
            return
        window = self.window_seconds
        stale = [k for k, (_sh, ts) in bucket.items() if now - ts > window]
        for k in stale:
            bucket.pop(k, None)
        if not bucket:
            self._recent.pop(sender_id, None)

    # ── API ──
    def check_and_record(self, sender_id: int, text: str) -> Optional[Dict[str, Any]]:
        """يفحص التشابه ثم يسجّل النص الجديد دائماً (إن مرّ).

        Returns:
            None  → لا تشابه (أو الحاجز معطل) — التنبيه يمر، والنص مُسجّل.
            dict  → وُجد تشابه: {score, matched_excerpt, age_seconds} —
                    على المستدعي منع التنبيه.

        v14.5.1: المرسل المجهول (sender_id=0/None) يُستثنى كلياً. الحاجز
        لا يستقبل chat_id ولا يستطيع التمييز بين مجهولين مختلفين، فكان
        يضعهم كلهم في دلو[0] فيمنع طلبًا حقيقيًا لمجرد تشابهه مع طلب
        شخص آخر (في مجموعة أخرى تمامًا). الحماية الحرفية (dedup.py)
        تغطي المجهولين عبر domain لكل محادثة — تبقى سارية بلا تغيير.
        """
        try:
            if not self.enabled or not isinstance(text, str) or not text.strip():
                return None
            # v14.5.1: تجاوز المجهولين — dedup يغطيهم عبر domain/chat_id.
            try:
                sid = int(sender_id or 0)
            except (TypeError, ValueError):
                sid = 0
            if sid <= 0:
                self._stats["skipped_anon"] += 1
                return None
            now = time.time()
            window = self.window_seconds
            if window <= 0:
                return None

            self._stats["checked"] += 1
            norm = normalize_for_fingerprint(text)
            if len(norm) < 6:
                # نصوص قصيرة جداً: شينغلز غير مستقر — لا نمنع ولا نسجل
                return None
            sh = _shingles(norm)
            # v14.5.1: تُحسَب مرة واحدة، لا لكل عنصر في الدلو.
            masked_norm = _mask_digits(norm)

            self._gc_sender(sid, now)
            bucket = self._recent.get(sid)

            best_score = 0.0
            best_excerpt: str = ""
            best_age = 0.0
            if bucket:
                for key, (old_sh, ts) in bucket.items():
                    age = now - ts
                    if age > window:
                        continue
                    # v11.7 (audit F-1): اختلاف بالأرقام فقط = رسالتان مختلفتان
                    # (درجة/رقم/موعد) — لا يُعد تكراراً مهما بلغ تشابه الحروف.
                    if masked_norm == _mask_digits(key):
                        continue
                    score = similarity_ratio(norm, key, sh, old_sh)
                    if score > best_score:
                        best_score = score
                        best_excerpt = key
                        best_age = age

            hit = None
            if best_score >= self.threshold:
                self._stats["blocked"] += 1
                hit = {
                    "score": round(best_score, 3),
                    "matched_excerpt": best_excerpt[:120],
                    "age_seconds": round(best_age, 1),
                }

            # v11.7 (audit F-1): تسجيل النص الجديد فقط عند المروّغ — النص
            # الممنوع لا يُسجّل ولا يُجدَّد طابعه: النسخة السابقة كانت تُعيد
            # تسجيل كل صيغة ممنوعة بطابع جديد فتنزلق نافذة الكبت إلى
            # ما لا نهاية (مرسل يعيد المحاولة = كبت أبدي). النص الأصلي
            # المسجّل مسبقاً ينتهي بانتهاء النافذة الطبيعية.
            if hit is None:
                if bucket is None:
                    bucket = OrderedDict()
                    self._recent[sid] = bucket
                    self._evict_sender()
                bucket.pop(norm, None)
                bucket[norm] = (sh, now)
                while len(bucket) > self._keep:
                    bucket.popitem(last=False)
                self._stats["recorded"] += 1
            return hit
        except Exception as e:
            logger.debug(f"similarity gate skipped: {type(e).__name__}: {e}")
            return None

    def reset(self) -> None:
        """تفريغ الذاكرة (للاختبارات ولوحة التحكم)."""
        self._recent.clear()

    def snapshot(self) -> Dict[str, Any]:
        """لقطة للقراءة لـ /health واللوحة — فشل-آمن."""
        try:
            total_tracked = sum(len(b) for b in self._recent.values())
            return {
                "enabled": self.enabled,
                "threshold": self.threshold,
                "window_seconds": self.window_seconds,
                "senders_tracked": len(self._recent),
                "texts_tracked": total_tracked,
                **dict(self._stats),
            }
        except Exception:
            return {"enabled": False}


# ─────────────────────────────────────────────────────────────────────────────
# Singleton مشترك (نفس نمط dedup.py)
# ─────────────────────────────────────────────────────────────────────────────
_gate: Optional[SimilarityGate] = None


def init_similarity_gate() -> SimilarityGate:
    global _gate
    _gate = SimilarityGate()
    logger.info(
        f"Similarity Gate initialized | enabled={_gate.enabled} | "
        f"threshold={_gate.threshold} | window={_gate.window_seconds}s"
    )
    return _gate


def get_similarity_gate() -> SimilarityGate:
    global _gate
    if _gate is None:
        _gate = SimilarityGate()
    return _gate


def get_similarity_snapshot() -> Dict[str, Any]:
    """Read-only snapshot للوحة — لا يرمي استثناءات أبداً."""
    try:
        return get_similarity_gate().snapshot()
    except Exception:
        return {"enabled": False}
