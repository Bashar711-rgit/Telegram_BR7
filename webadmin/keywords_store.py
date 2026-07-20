"""
webadmin/keywords_store.py – Keywords persistence + hot-reload engine.

keywords.json has a mixed schema (top-level lists, nested dicts, "terms"
leaves, and "_"-prefixed metadata). This store exposes every list-of-strings
leaf as an editable keyword list addressed by its dotted path, keeps all
metadata untouched, writes atomically, and hot-reloads the running bot's
filter without a restart.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any, Dict, List, Optional

from loguru import logger

from config import CFG, KEYWORDS

KEYWORDS_FILE = "keywords.json"
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# File IO (atomic)
# ---------------------------------------------------------------------------
def _read_raw() -> Dict[str, Any]:
    import json
    try:
        with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def _write_raw_atomic(data: Dict[str, Any]) -> None:
    import json
    fd, tmp = tempfile.mkstemp(dir=".", prefix=".keywords_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, KEYWORDS_FILE)  # atomic on POSIX
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def _split_path(path: str) -> List[str]:
    parts = [p.strip() for p in (path or "").split(".") if p.strip()]
    return parts


def _resolve(data: Dict[str, Any], path: str) -> Optional[List[str]]:
    node: Any = data
    for part in _split_path(path):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if isinstance(node, list) and all(isinstance(x, str) for x in node):
        return node
    return None


def _walk_lists(node: Any, prefix: str, out: List[Dict[str, Any]]) -> None:
    """Collect every editable list-of-strings leaf with its dotted path."""
    if isinstance(node, list):
        if all(isinstance(x, str) for x in node):
            out.append({"path": prefix, "count": len(node), "items": node})
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if key.startswith("_"):
                continue  # metadata is preserved but never editable
            child = f"{prefix}.{key}" if prefix else key
            _walk_lists(value, child, out)


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------
def get_index() -> List[Dict[str, Any]]:
    """Full index of editable keyword lists, grouped by top-level category."""
    raw = _read_raw()
    lists: List[Dict[str, Any]] = []
    _walk_lists(raw, "", lists)
    categories: Dict[str, Dict[str, Any]] = {}
    for entry in lists:
        top = entry["path"].split(".", 1)[0]
        cat = categories.setdefault(top, {"category": top, "total": 0, "lists": []})
        label = entry["path"][len(top):].lstrip(".") or "(القائمة الرئيسية)"
        cat["lists"].append(
            {"path": entry["path"], "label": label, "count": entry["count"], "items": entry["items"]}
        )
        cat["total"] += entry["count"]
    return [categories[k] for k in sorted(categories)]


def search(query: str) -> List[Dict[str, str]]:
    """Substring search across every editable list; returns flat matches."""
    query = (query or "").strip().lower()
    if not query:
        return []
    raw = _read_raw()
    lists: List[Dict[str, Any]] = []
    _walk_lists(raw, "", lists)
    matches: List[Dict[str, str]] = []
    for entry in lists:
        for item in entry["items"]:
            if query in item.lower():
                matches.append({"path": entry["path"], "value": item})
    return matches[:500]


def category_terms(category: str) -> List[str]:
    """All keyword strings under one top-level category (for alert filtering)."""
    raw = _read_raw()
    node = raw.get(category)
    collector: List[Dict[str, Any]] = []
    _walk_lists(node, category, collector)
    out: List[str] = []
    for entry in collector:
        out.extend(entry["items"])
    return out


# ---------------------------------------------------------------------------
# Mutations (persist + hot reload)
# ---------------------------------------------------------------------------
async def add_keyword(path: str, value: str) -> Dict[str, Any]:
    value = (value or "").strip()
    if not value:
        return {"success": False, "error": "القيمة فارغة"}
    async with _lock:
        raw = _read_raw()
        node = _resolve(raw, path)
        if node is None:
            return {"success": False, "error": f"مسار غير صالح: {path}"}
        if value in node:
            return {"success": False, "error": "الكلمة موجودة مسبقاً"}
        node.append(value)
        _write_raw_atomic(raw)
    await hot_reload()
    return {"success": True, "path": path, "value": value, "count": len(node)}


async def update_keyword(path: str, old: str, new: str) -> Dict[str, Any]:
    new = (new or "").strip()
    if not new:
        return {"success": False, "error": "القيمة الجديدة فارغة"}
    async with _lock:
        raw = _read_raw()
        node = _resolve(raw, path)
        if node is None:
            return {"success": False, "error": f"مسار غير صالح: {path}"}
        try:
            idx = node.index(old)
        except ValueError:
            return {"success": False, "error": "الكلمة الأصلية غير موجودة"}
        if new != old and new in node:
            return {"success": False, "error": "الكلمة الجديدة موجودة مسبقاً"}
        node[idx] = new
        _write_raw_atomic(raw)
    await hot_reload()
    return {"success": True, "path": path, "old": old, "new": new}


async def delete_keyword(path: str, value: str) -> Dict[str, Any]:
    async with _lock:
        raw = _read_raw()
        node = _resolve(raw, path)
        if node is None:
            return {"success": False, "error": f"مسار غير صالح: {path}"}
        if value not in node:
            return {"success": False, "error": "الكلمة غير موجودة"}
        node.remove(value)
        _write_raw_atomic(raw)
    await hot_reload()
    return {"success": True, "path": path, "value": value, "count": len(node)}


async def import_keywords(data: Dict[str, Any]) -> Dict[str, Any]:
    """Replace the whole keywords.json (validated) and hot-reload."""
    if not isinstance(data, dict) or not data:
        return {"success": False, "error": "البنية يجب أن تكون JSON object غير فارغ"}
    async with _lock:
        _write_raw_atomic(data)
    await hot_reload()
    return {"success": True}


def export_keywords() -> Dict[str, Any]:
    return _read_raw()


# ---------------------------------------------------------------------------
# Hot reload
# ---------------------------------------------------------------------------
_bot_ref: Any = None
_app_ref: Any = None


def set_bot_reference(bot: Any) -> None:
    global _bot_ref
    _bot_ref = bot


def set_app_reference(app: Any) -> None:
    """Store the FastAPI app so the bot can be resolved lazily at reload time
    (bot_ref is attached to app.state only after the bot initializes)."""
    global _app_ref
    _app_ref = app


def _resolve_bot() -> Any:
    if _bot_ref is not None:
        return _bot_ref
    if _app_ref is not None:
        return getattr(_app_ref.state, "bot_ref", None)
    return None


async def hot_reload() -> Dict[str, Any]:
    """Reload KEYWORDS in memory + rebuild the running filter (no restart)."""
    reloaded = {"config": False, "filter": False}
    try:
        fresh = _read_raw()
        KEYWORDS.clear()
        KEYWORDS.update(fresh)
        reloaded["config"] = True
    except Exception as e:
        logger.error(f"Keywords config reload failed: {e}")
        return {"reloaded": reloaded, "error": str(e)}

    bot = _resolve_bot()
    if bot is not None and getattr(bot, "filter", None) is not None:
        try:
            import filter_engine

            flt = bot.filter
            flt._load_keyword_sets()
            flt._build_tries()
            # Rebuild caches so stale decisions are forgotten immediately
            flt._bloom = filter_engine.OptimizedBloomFilter(
                CFG.BLOOM_FILTER_SIZE, CFG.BLOOM_FILTER_FP
            )
            flt._cache = filter_engine.ShardedLRUCache(CFG.MAX_CACHE_SIZE, CFG.CACHE_TTL)
            try:
                flt._text_cache.clear()
            except Exception:
                pass
            reloaded["filter"] = True
        except Exception as e:
            logger.error(f"Filter hot reload failed: {e}")
            return {"reloaded": reloaded, "error": str(e)}
    logger.info("Keywords hot reload completed")
    return {"reloaded": reloaded}
