#!/usr/bin/env python3
"""
similarity.py — v10.0 Precision Edition: حاجز منع التنبيهات المتشابهة.

المشكلة التي يحلها
------------------
dedup.py يمنع التكرار الحرفي فقط (نفس المرسل + نفس النص بعد تطبيع).
أما إعادة الصياغة القريبة من نفس المرسل خلال فترة قصيرة:

    «ابي احد يساعدني بالواجب»
    «ابي احد يساعدني في الواجب ضروري»

فتمر من الحاجز الحرفي وتُنبيه القناة مرتين لطلب واحد.

الحل: بابمة تشابه مزدوجة داخل الذاكرة:
  1) difflib.SequenceMatcher على النص المطبَّع (مقاوم لاستبدال حرف/كلمة).
  2) Jaccard على shingles حرفية (3-gram) كبديل مكمل.
نأخذ الأعلى من القياسين، ونمنع عند تجاوز عتبة محافظة (افتراضي 0.88) —
أي أن «المتشابهة جزئياً» (متطلب أبقيته دائماً) لا تُحذف؛ فقط
الشبه-المطابقة تُمنع.

التصميم:
  * ذاكرة فقط (بلا DB): الفاقد عند إعادة التشغيل مقبول — الحماية
    الحرفية الدائمة تبقى في dedup.py. السقف LRU لكل مرسل.
  * مطابقة المرسل هي المعيار: مرسلان مختلفان بنفس النص = طلبان يصلمان
    (نفس فلسفة dedup.py).
  * فشل-آمن: أي استثناء ⇒ لا منع (التنبيه يمر) — لا يُفقد طلب حقيقي.
  * قابلة للتعديل الحي: SIMILARITY_ENABLED / SIMILARITY_THRESHOLD /
    SIMILARITY_WINDOW_SECONDS تُقرأ من CFG عند كل فحص.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from config import CFG
from dedup import normalize_for_fingerprint

# ─────────────────────────────────────────────────────────────────────────────
# Shingles — أحرف n-gram على النص المطبَّع
# ─────────────────────────────────────────────────────────────────────────────
_SHINGLE_N = 3
# تطبيع بسيط للمسافات قبل بناء الشينغلز (بعد تطبيع dedup الأقوى)


def _shingles(norm_text: str, n: int = _SHINGLE_N) -> frozenset:
    """مجموعة shingles حرفية من نص مطبَّع — تُهمل المسافات المضاعفة."""
    try:
        import re as _re

        t = _re.sub(r"\s+", " ", norm_text).strip()
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
        import difflib

        r_seq = difflib.SequenceMatcher(None, norm_a or "", norm_b or "").ratio()
        r_jac = jaccard(
            sh_a if sh_a is not None else _shingles(norm_a),
            sh_b if sh_b is not None else _shingles(norm_b),
        )
        return max(r_seq, r_jac)
    except Exception:
        return 0.0


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
        # sender_id -> OrderedDict[fingerprint_text] = (shingles, ts)
        self._recent: "OrderedDict[int, OrderedDict[str, Tuple[frozenset, float]]]" = (
            OrderedDict()
        )
        self._stats: Dict[str, int] = {
            "checked": 0,
            "recorded": 0,
            "blocked": 0,
            "evicted": 0,
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
            self._recent.popitem(last=False)
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
        """
        try:
            if not self.enabled or not isinstance(text, str) or not text.strip():
                return None
            now = time.time()
            window = self.window_seconds
            if window <= 0:
                return None

            self._stats["checked"] += 1
            sid = int(sender_id or 0)
            norm = normalize_for_fingerprint(text)
            if len(norm) < 6:
                # نصوص قصيرة جداً: شينغلز غير مستقر — لا نمنع ولا نسجل
                return None
            sh = _shingles(norm)

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

            # تسجيل النص الجديد دائماً (حتى الممنوع — تحديث الطابع الزمني)
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
