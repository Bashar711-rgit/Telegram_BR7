#!/usr/bin/env python3
"""negative_memory.py — v14.5.1 (F-18): استمرارية الذاكرة السلبية عبر إعادة التشغيل.

v14.5.1 — correction pass:
  * _dirty set is cleared ONLY for stores that actually flushed
    successfully. The previous _debounced_flush() called `_dirty.clear()`
    BEFORE flush_now(), so a transient DB failure silently dropped the
    dirty tracking — the failed stores were not retried until the app
    happened to mark_dirty() again (which for tg_user_blocked / reply_fix
    is rare). Now failed stores stay in _dirty and are retried on the
    next debounce window.
  * The _FLUSH_MIN_INTERVAL_S gate now WAITS for the remaining interval
    instead of dropping the flush silently. Previously the debounce task
    could fire during the gate, return immediately, and no new task was
    scheduled — if nothing re-marked dirty, the state sat forever. Now
    the task sleeps the remaining interval (bounded at 5min) and retries.
  * _debounced_flush now re-schedules itself when dirty state arrives
    DURING its run (mark_dirty during the await cannot spawn a new task
    because _flush_task still points at the running debounce).
  * spawn_bg fallback (used only when task_util import fails) now keeps
    a strong reference + done-callback, matching what task_util exists
    for. The previous fallback called loop.create_task() with no
    reference — exactly the GC hazard.
  * mark_dirty() no-ops (except the _dirty.add) when no DB has been
    hydrated yet. Pre-hydrate marks are still tracked and are flushed
    at the end of hydrate_all() or on the next explicit flush_now().
  * hydrate_all() now kicks a flush at the end if _dirty is non-empty —
    pre-hydrate marks get persisted immediately instead of waiting for
    the next mark_dirty().
  * Switched from stdlib `logging` to `loguru.logger`. config.setup_logging()
    does not install a stdlib→loguru bridge, so every INFO/DEBUG line in
    this module was silently dropped (only WARNING+ reached stderr). The
    "hydrated N store(s)" line — the whole point of F-18 observability —
    never appeared in bot.log. All other v14.5.1 files use loguru.
  * __version__ = "14.5.1".

المشكلة (التدقيق F-18): الذكريات السلبية والحدود (tg_user_blocked في
message_context، حدود reply_fix بالساعة/باليوم) كانت بذاكرة العملية فقط —
تصفّر بعد restart/redeploy، فيُعاد إرسال tg://user المرفوض ويُستهلك حد
reply_fix من جديد.

التصميم (بلا أي تغيير على المسار الساخن):
  * L1 = نفس خرائط الذاكرة القائمة (قراءة متزامنة بلا DB — صفر تكلفة إضافية).
  * الترطيب: hydrate_all(db) عند الإقلاع — يقرأ rows الخاصة به من
    app_settings (key = 'negmem:<name>'، قيمة JSON) ويدمجها في L1
    (الأحدث يفوز + إسقاط ما تجاوز TTL).
  * التخزين: كل mark يضع علامة وسَخة (mark_dirty) → تفريغ مؤجل واحد
    (debounce) يكتب snapshot الكل-أو-لاشيء لكل مخزن. بلا لوب دائم.
  * فشل-آمن: أي خطأ DB = تجاهل مسجّل (الذاكرة القائمة تبقى الحقيقة
    التشغيلية) — لا يرفع استثناءً أبداً في مسارات الرسائل.
  * متوافق مع المحركين: SQL بعلامات ؟ يترجمها EnhancedDatabase لـPG،
    وON CONFLICT مدعوم في SQLite 3.24+ وPostgreSQL.

الخصوصية: لا يُخزَّن هنا أي نص رسالة أو session — فقط معرّفات رقمية
وطوابع زمنية/عدّادات.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Dict, Iterable, Optional, Set

from loguru import logger

__version__ = "14.5.1"

try:  # GC-safe background task refs (F-17 pattern)
    from task_util import spawn_bg
except Exception:  # pragma: no cover — fallback يحفظ مرجعاً قوياً (v14.5.1)
    _fallback_tasks: Set["asyncio.Task"] = set()

    def spawn_bg(coro: Any) -> "asyncio.Task":  # type: ignore[misc]
        """v14.5.1: fallback يحفظ مرجعاً قوياً + done_callback.

        النسخة السابقة استدعت loop.create_task(coro) بلا مرجع — نفس
        خطر GC الذي وُجد task_util لمنعه. هذا المسار آخر خط دفاع إن
        تعطّل task_util نفسه، فيجب أن يكون آمناً بنفس القدر."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        task = loop.create_task(coro)
        _fallback_tasks.add(task)
        task.add_done_callback(_fallback_tasks.discard)
        return task


_DEBOUNCE_S = 5.0
_FLUSH_MIN_INTERVAL_S = 30.0
_MAX_GATE_WAIT_S = 300.0         # سقف انتظار نافذة الـgate داخل debounce
_KEY_PREFIX = "negmem:"

# name -> {"snapshot": Callable[-> dict], "hydrate": Callable[[dict], None]}
_stores: Dict[str, Dict[str, Callable]] = {}
_dirty: Set[str] = set()
_db_ref: Any = None
_flush_task: Optional["asyncio.Task"] = None
_last_flush_ts: float = 0.0


def register_store(name: str,
                   snapshot_fn: Callable[[], Dict[str, Any]],
                   hydrate_fn: Callable[[Any], None]) -> None:
    """سجّل مخزناً باسم ثابت (يستبدل السابق — آمن للاستدعاء المتكرر)."""
    _stores[str(name)] = {"snapshot": snapshot_fn, "hydrate": hydrate_fn}


def mark_dirty(name: str) -> None:
    """ضع علامة وسَخة + جدولة تفريغ مؤجل (بلا استثناءات — فشل-آمن).

    v14.5.1: إذا لم يُرطَّب أي DB بعد (hydrate_all لم يُستدعَ) — نكتفي
    بتسجيل الوسَخة بلا جدولة مهمة debounce مهدورة (كانت تُنشأ ثم تخرج
    فوراً بعد 5s). أول flush_now() يدوي أو نهاية hydrate_all يأخذها.
    """
    try:
        if str(name) not in _stores:
            return
        _dirty.add(str(name))
        if _db_ref is None:
            return  # لا DB بعد — hydrate_all/flush_now اليدوي سيأخذها
        _kick()
    except Exception:
        pass


def _kick() -> None:
    global _flush_task
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # لا لوب حي (استيراد/اختبار متزامن) — التفريغ عبر flush_now لاحقاً
    if _flush_task is None or _flush_task.done():
        _flush_task = spawn_bg(_debounced_flush())


async def _debounced_flush() -> None:
    """v14.5.1: ثلاث إصلاحات مترابطة:

    1) انتظار نافذة الـgate بدل الإسقاط الصامت (كانت return بلا إعادة
       جدولة → dirty state راقد حتى mark_dirty() جديد).
    2) إزالة فقط المخازن التي نجح تفريغها (كانت _dirty.clear() قبل
       flush_now → فشل عابر يفقد الوسَخة).
    3) إعادة جدولة ذاتية إن وُضعت وسَخات أثناء انتظارنا (mark_dirty لا
       يستطيع جدولة مهمة جديدة لأن _flush_task لا يزال يشير إلينا).
    """
    global _flush_task
    await asyncio.sleep(_DEBOUNCE_S)
    db = _db_ref
    if db is None or not _dirty:
        return
    # v14.5.1: انتظر المتبقي من نافذة الـgate بدل الإسقاط.
    elapsed = time.monotonic() - _last_flush_ts
    if elapsed < _FLUSH_MIN_INTERVAL_S:
        remaining = _FLUSH_MIN_INTERVAL_S - elapsed
        if 0 < remaining < _MAX_GATE_WAIT_S:
            await asyncio.sleep(remaining)
            db = _db_ref
            if db is None or not _dirty:
                return
        else:
            # نافذة غير طبيعية (ساعة سيئة/ساعة حائط مقفزة) — دع mark
            # التالي أو flush_now عند الإيقاف يحلّها.
            return
    names = set(_dirty)
    failed: Set[str] = set()
    try:
        await flush_now(db, names, failed_out=failed)
    except Exception as e:  # flush_now لا يرفع — دفاع ضد تعديل مستقبلي
        logger.debug(f"negative_memory flush failed: {type(e).__name__}: {e}")
        failed = set(names)
    # v14.5.1: أزل الناجحين فقط؛ الفاشلون يبقون لإعادة المحاولة.
    succeeded = names - failed
    _dirty.difference_update(succeeded)
    if failed:
        _dirty.update(failed)  # idempotent — يبقون مسجلين
    # v14.5.1: أعِد الجدولة إن وُضعت وسَخات أثناء انتظارنا.
    if _dirty:
        _flush_task = None
        _kick()


def _setting_key(name: str) -> str:
    return _KEY_PREFIX + str(name)


async def hydrate_all(db: Any, names: Optional[Iterable[str]] = None) -> int:
    """عند الإقلاع: اقرأ snapshots من app_settings وادمجها في L1.

    يعيد عدد المخازن المرطَّبة فعلاً. أي فشل = تجاهل (يبقى L1 فارغاً
    كما في السلوك القديم).

    v14.5.1: في النهاية، إن كانت هناك وسَخات سابقة للترطيب (mark_dirty
    قبل hydrate_all) — يُطلق تفريغاً فورياً لها بدل انتظار mark جديد.
    """
    global _db_ref
    _db_ref = db
    count = 0
    for name in list(names if names is not None else _stores.keys()):
        store = _stores.get(str(name))
        if store is None:
            continue
        try:
            row = await db._fetchone(
                "SELECT value FROM app_settings WHERE key = ?",
                (_setting_key(name),),
            )
            if not row or not row.get("value"):
                continue
            raw = row["value"]
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            if isinstance(data, dict):
                store["hydrate"](data)
                count += 1
        except Exception as e:  # noqa: BLE001
            logger.debug(f"negative_memory hydrate[{name}] skipped: {type(e).__name__}")
    if count:
        logger.info(f"negative_memory: hydrated {count} store(s) from DB")
    # v14.5.1: ادفع وسَخات ما-قبل-الترطيب الآن (كانت راقدة بلا أثر).
    if _dirty:
        _kick()
    return count


async def flush_now(
    db: Any,
    names: Optional[Iterable[str]] = None,
    failed_out: Optional[Set[str]] = None,
) -> int:
    """اكتب snapshots الحالية إلى app_settings (أحدث يفوز عند الاندماج).

    v14.5.1: يقبل `failed_out` اختيارياً — عند تمريره، تُضاف إليه أسماء
    المتاجر التي فشل تفريغها (ليُعيد _debounced_flush إضافتها إلى
    _dirty). عند عدم تمريره، السلوك القديم محفوظ (int فقط، الأسماء
    الفاشلة غير معروفة للمستدعي).
    """
    global _last_flush_ts
    count = 0
    for name in list(names if names is not None else _stores.keys()):
        store = _stores.get(str(name))
        if store is None:
            continue
        try:
            snap = store["snapshot"]()
            if snap is None:
                continue
            payload = json.dumps(snap, separators=(",", ":"), ensure_ascii=False)
            await db._execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_setting_key(name), payload),
            )
            count += 1
        except Exception as e:  # noqa: BLE001
            logger.debug(f"negative_memory flush[{name}] skipped: {type(e).__name__}")
            if failed_out is not None:
                failed_out.add(str(name))
    _last_flush_ts = time.monotonic()
    return count


def reset_for_tests() -> None:
    """للاختبارات — تصفير السجل (لا يلمس مخازن الوحدات نفسها)."""
    global _db_ref, _flush_task, _last_flush_ts
    _stores.clear()
    _dirty.clear()
    _db_ref = None
    _flush_task = None
    _last_flush_ts = 0.0
