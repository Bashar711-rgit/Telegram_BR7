#!/usr/bin/env python3
"""negative_memory.py — v11.8.2 (F-18): استمرارية الذاكرة السلبية عبر إعادة التشغيل.

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
import logging
import time
from typing import Any, Callable, Dict, Iterable, Optional, Set

logger = logging.getLogger(__name__)

try:  # GC-safe background task refs (F-17 pattern)
    from task_util import spawn_bg
except Exception:  # pragma: no cover — fallback آمن
    def spawn_bg(coro):  # type: ignore[misc]
        return asyncio.get_event_loop().create_task(coro)

_DEBOUNCE_S = 5.0
_FLUSH_MIN_INTERVAL_S = 30.0
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
    """ضع علامة وسَخة + جدولة تفريغ مؤجل (بلا استثناءات — فشل-آمن)."""
    try:
        if str(name) not in _stores:
            return
        _dirty.add(str(name))
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
    await asyncio.sleep(_DEBOUNCE_S)
    db = _db_ref
    if db is None or not _dirty:
        return
    now = time.monotonic()
    if now - _last_flush_ts < _FLUSH_MIN_INTERVAL_S:
        return  # الوسَخة تبقى — التفريغ التالي (أو flush_now عند الإيقاف) يلتقطها
    names = set(_dirty)
    _dirty.clear()
    try:
        await flush_now(db, names)
    except Exception as e:  # noqa: BLE001 — لا يُسمح باعتراض من خلفية
        logger.debug(f"negative_memory flush failed: {type(e).__name__}: {e}")


def _setting_key(name: str) -> str:
    return _KEY_PREFIX + str(name)


async def hydrate_all(db: Any, names: Optional[Iterable[str]] = None) -> int:
    """عند الإقلاع: اقرأ snapshots من app_settings وادمجها في L1.

    يعيد عدد المخازن المرطَّبة فعلاً. أي فشل = تجاهل (يبقى L1 فارغاً
    كما في السلوك القديم).
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
    return count


async def flush_now(db: Any, names: Optional[Iterable[str]] = None) -> int:
    """اكتب snapshots الحالية إلى app_settings (أحدث يفوز عند الاندماج)."""
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
