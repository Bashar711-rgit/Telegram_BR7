#!/usr/bin/env python3
"""task_util.py — v14.5.1 (background task tracker)

v11.8.1 (F-17): مرجع محفوظ لمهام background عابرة.

Python لا يضمن بقاء Task بدون مرجع — GC قد يجمعها قبل اكتمالها
(حتى مع وجود coroutine قيد التشغيل). هذه الوحدة تحفظ مراجع قوية في
مجموعة وحدة-مستوى مع done_callback ينظّف تلقائياً — نفس نمط main.py
`_background_tasks` لكن للاستخدام من الوحدات التي لا تمتلك نسخة البوت.

الاستخدام:
    from task_util import spawn_bg
    spawn_bg(self._persist_account_hash(sid, ah), name="persist_hash")

v14.5.1 — correction pass:
  * _on_task_done callback now RETRIEVES the task's exception via
    task.exception() and logs unexpected failures through loguru with the
    task name attached. The previous callback was `_tasks.discard` only —
    any exception raised inside a spawned coroutine was never retrieved,
    so asyncio emitted "Task exception was never retrieved" at task-GC
    time with no context (no name, no traceback in bot.log). Callers had
    no way to know their background work failed. Now every unexpected
    exception is logged with `logger.opt(exception=exc)` and CancelledError
    stays silent (it is the expected outcome at shutdown).
  * New async `cancel_all(timeout=5.0)` — main.py can call it during
    shutdown to cancel + await every spawned task cleanly. Without it,
    pending background tasks continued running through process teardown
    (racing against closed DB connections / closed loguru sinks) and got
    force-cancelled only when `asyncio.run()` cleaned up the loop at a
    less controlled point.
  * Coroutine type annotation is now parameterized
    `Coroutine[Any, Any, Any]` to match `asyncio.create_task`'s contract
    (was the bare `Coroutine`). Zero runtime change; typing clarity only.
  * `pending_count()` now uses a generator (O(1) memory) instead of a
    list comprehension. Same result, no temporary list.
  * `__version__ = "14.5.1"` — matches the rest of the project.

from __future__ import annotations
"""

import asyncio
from typing import Any, Coroutine, Optional, Set

from loguru import logger

__version__ = "14.5.1"

_tasks: Set[asyncio.Task] = set()


def _on_task_done(task: asyncio.Task) -> None:
    """v14.5.1: إزالة من المجموعة + استرجاع الاستثناء + لوج عند الخطأ.

    بدون استرجاع استثناء (task.exception())، asyncio يطبع "Task exception
    was never retrieved" عند GC المهمة بلا أي سياق قابل للعمل عليه — لا
    اسم المهمة، لا موضع الخطأ، ولا شيء في bot.log. الآن كل استثناء غير
    متوقع يُسجَّل عبر loguru بكامل تتبع الأثر. CancelledError (إيقاف
    تشغيل متوقع) يبقى صامتاً.
    """
    _tasks.discard(task)
    if task.cancelled():
        return  # إلغاء متوقع (shutdown أو cancel_all) — لا لوج
    try:
        exc = task.exception()
    except (asyncio.CancelledError, asyncio.InvalidStateError):
        # سباق نادر: المهمة أُلغيت بين cancelled() وexception() — تجاهل
        return
    if exc is not None:
        try:
            name = task.get_name()
        except Exception:
            name = "unknown"
        logger.opt(exception=exc).error(f"spawn_bg task {name} raised")


def spawn_bg(
    coro: "Coroutine[Any, Any, Any]",
    name: Optional[str] = None,
) -> asyncio.Task:
    """create_task + مرجع محفوظ — بديل آمن من GC للإطلاق العابر.

    v14.5.1: done_callback يسترجع الاستثناء (لا يتركه يتسرب لتحذير asyncio).
    """
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task


def pending_count() -> int:
    """عدد المهام العابرة الجارية (تشخيص/اختبارات).

    v14.5.1: مولّد بدل قائمة مؤقتة — O(1) ذاكرة.
    """
    return sum(1 for t in _tasks if not t.done())


async def cancel_all(timeout: float = 5.0) -> int:
    """v14.5.1: إلغاء كل المهام العابرة وانتظارها — إيقاف نظيف.

    يُستدعى من main.py في نهاية shutdown (قبل إغلاق DB وloguru sinks).
    يعيد عدد المهام التي كانت جارية لحظة الاستدعاء.

    المهام التي تتجاوز timeout تبقى في مدار asyncio.run() — إلغاؤها
    يحدث عند إغلاق الـevent loop. تسجَّل تحذيراً بالعدد المتبقي.
    """
    # لقطة فورية للجاري فقط — done tasks يزيلها _on_task_done أصلاً
    pending = [t for t in _tasks if not t.done()]
    if not pending:
        return 0
    for t in pending:
        t.cancel()
    try:
        _, still_running = await asyncio.wait(pending, timeout=timeout)
        if still_running:
            logger.warning(
                f"task_util.cancel_all: {len(still_running)} task(s) did not "
                f"finish within {timeout}s — leaving to asyncio.run cleanup"
            )
    except Exception as e:
        # لا نُسقط shutdown بسبب خطأ في التنظيف نفسه
        logger.debug(f"task_util.cancel_all failed: {type(e).__name__}: {e}")
    return len(pending)
