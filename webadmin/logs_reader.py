"""
webadmin/logs_reader.py – Efficient tail reader + parser for bot.log.

Loguru plain format: "YYYY-MM-DD HH:mm:ss | LEVEL    | name:line | message".
Multi-line tracebacks are attached to the preceding entry.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from config import CFG

_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|\s*([A-Z]+)\s*\|\s*([^|]*?)\s*\|\s?(.*)$"
)
_LEVELS = ("DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL")
_READ_WINDOW = 512 * 1024  # last 512 KB is plenty for 200 log lines


def _log_path() -> str:
    return getattr(CFG, "LOG_FILE", "bot.log") or "bot.log"


def tail(lines: int = 200, level: Optional[str] = None) -> Dict[str, Any]:
    path = _log_path()
    entries: List[Dict[str, Any]] = []
    if os.path.exists(path):
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > _READ_WINDOW:
                f.seek(-_READ_WINDOW, os.SEEK_END)
            raw = f.read().decode("utf-8", errors="replace")
        rows = raw.splitlines()
        if size > _READ_WINDOW and rows:
            rows = rows[1:]  # first line may be truncated
        for row in rows:
            m = _LINE_RE.match(row)
            if m and m.group(2) in _LEVELS:
                entries.append(
                    {
                        "time": m.group(1),
                        "level": m.group(2),
                        "source": m.group(3).strip(),
                        "message": m.group(4),
                    }
                )
            elif entries:
                entries[-1]["message"] += "\n" + row  # traceback continuation
    if level and level.upper() in _LEVELS:
        lvl = level.upper()
        entries = [e for e in entries if e["level"] == lvl]
    entries = entries[-lines:]
    return {
        "file": path,
        "file_exists": os.path.exists(path),
        "total_shown": len(entries),
        "entries": entries,
    }
