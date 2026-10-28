"""v11.8.1 (F-17): مرجع محفوظ لمهام background عابرة.

Python لا يضمن بقاء Task بدون مرجع — GC قد يجمعها قبل اكتمالها
(حتى مع وجود coroutine قيد التشغيل). هذه الوحدة تحفظ مراجع قوية في
مجموعة وحدة-مستوى مع done_callback ينظّف تلقائياً — نفس نمط main.py
`_background_tasks` لكن للاستخدام من الوحدات التي لا تمتلك نسخة البوت.

الاستخدام:
    from task_util import spawn_bg
    spawn_bg(self._persist_account_hash(sid, ah), name="persist_hash")
"""
from __future__ import annotations

import asyncio
from typing import Coroutine, Optional, Set

_tasks: Set[asyncio.Task] = set()


def spawn_bg(coro: Coroutine, name: Optional[str] = None) -> asyncio.Task:
    """create_task + مرجع محفوظ — بديل آمن من GC للإطلاق العابر."""
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def pending_count() -> int:
    """عدد المهام العابرة الجارية (تشخيص/اختبارات)."""
    return len([t for t in _tasks if not t.done()])
