"""
webadmin/render_api.py – Minimal async Render API client.

Used to persist account/env configuration on Render and to trigger
service restarts. Degrades gracefully when RENDER_API_KEY or
RENDER_SERVICE_ID are not configured (local/dev environments).
"""

from __future__ import annotations

import os
from typing import Any, Dict

import aiohttp

_API_BASE = "https://api.render.com/v1"
_TIMEOUT = aiohttp.ClientTimeout(total=30)


def _credentials() -> tuple[str, str]:
    return (
        (os.getenv("RENDER_API_KEY") or "").strip(),
        (os.getenv("RENDER_SERVICE_ID") or "").strip(),
    )


def is_configured() -> bool:
    api_key, service_id = _credentials()
    return bool(api_key and service_id)


def _headers() -> Dict[str, str]:
    api_key, _ = _credentials()
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


async def upsert_env(key: str, value: str) -> Dict[str, Any]:
    """Create or update one env var on the Render service."""
    if not is_configured():
        return {"saved": False, "reason": "RENDER_API_KEY / RENDER_SERVICE_ID غير مضبوطة"}
    _, service_id = _credentials()
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=_headers()) as s:
            async with s.put(
                f"{_API_BASE}/services/{service_id}/env-vars/{key}",
                json={"value": value},
            ) as r:
                if r.status in (200, 201):
                    return {"saved": True}
                body = (await r.text())[:200]
                return {"saved": False, "reason": f"Render API HTTP {r.status}: {body}"}
    except Exception as e:  # network errors must never crash the dashboard
        return {"saved": False, "reason": f"{type(e).__name__}: {e}"}


async def upsert_env_many(pairs: Dict[str, str]) -> Dict[str, Any]:
    """Upsert several env vars WITHOUT touching unrelated ones.

    P0 fix (v9.13.1): the previous implementation used the bulk endpoint
    PUT /services/{id}/env-vars, which on Render has REPLACE semantics —
    every variable not included in the payload was WIPED from the service.
    Saving a single account from the dashboard therefore deleted the whole
    environment (session strings, TARGET_GROUP_ID, dashboard tokens, ...),
    after which every new deploy boots with a broken env and permanently
    fails with update_failed.

    The per-variable endpoint PUT /services/{id}/env-vars/{key} is a true
    upsert: it creates or updates exactly one key and leaves all others
    untouched. We now iterate over the requested keys with that endpoint.
    Render applies env-var changes on the next deploy, so the number of
    HTTP calls does not translate into multiple redeploys.
    """
    if not is_configured():
        return {"saved": False, "reason": "RENDER_API_KEY / RENDER_SERVICE_ID غير مضبوطة"}
    _, service_id = _credentials()
    results: Dict[str, str] = {}
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=_headers()) as s:
            for key, value in pairs.items():
                try:
                    async with s.put(
                        f"{_API_BASE}/services/{service_id}/env-vars/{key}",
                        json={"value": value},
                    ) as r:
                        if r.status in (200, 201):
                            results[key] = "ok"
                        else:
                            body = (await r.text())[:150]
                            results[key] = f"HTTP {r.status}: {body}"
                except Exception as e:  # one bad key must not abort the batch
                    results[key] = f"{type(e).__name__}: {e}"
    except Exception as e:  # network errors must never crash the dashboard
        return {"saved": False, "reason": f"{type(e).__name__}: {e}"}
    failed = {k: v for k, v in results.items() if v != "ok"}
    if failed:
        return {
            "saved": False,
            "reason": "; ".join(f"{k}: {v}" for k, v in failed.items()),
            "results": results,
        }
    return {"saved": True, "results": results}


async def delete_env(key: str) -> Dict[str, Any]:
    """Delete one env var from the Render service."""
    if not is_configured():
        return {"saved": False, "reason": "RENDER_API_KEY / RENDER_SERVICE_ID غير مضبوطة"}
    _, service_id = _credentials()
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=_headers()) as s:
            async with s.delete(
                f"{_API_BASE}/services/{service_id}/env-vars/{key}"
            ) as r:
                if r.status in (200, 204, 404):
                    return {"saved": True}
                body = (await r.text())[:200]
                return {"saved": False, "reason": f"Render API HTTP {r.status}: {body}"}
    except Exception as e:
        return {"saved": False, "reason": f"{type(e).__name__}: {e}"}


async def get_latest_deploy() -> Dict[str, Any]:
    """Fetch the most recent deploy of the service (for the dashboard card).

    Returns a normalized dict:
      {"available": bool, "status": str|None, "commit_id": str,
       "commit_message": str, "created_at": str, "finished_at": str|None,
       "reason": str (only when available=False)}
    Never raises — the dashboard must keep working without Render access.
    """
    if not is_configured():
        return {"available": False, "reason": "RENDER_API_KEY / RENDER_SERVICE_ID غير مضبوطة"}
    _, service_id = _credentials()
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=_headers()) as s:
            async with s.get(
                f"{_API_BASE}/services/{service_id}/deploys",
                params={"limit": "1"},
            ) as r:
                if r.status != 200:
                    body = (await r.text())[:150]
                    return {"available": False, "reason": f"Render API HTTP {r.status}: {body}"}
                data = await r.json()
                items = data if isinstance(data, list) else data.get("deploys", [])
                if not items:
                    return {"available": True, "status": None}
                dep = items[0].get("deploy", {}) if isinstance(items[0], dict) else {}
                commit = dep.get("commit") or {}
                message = (commit.get("message") or "").splitlines()
                return {
                    "available": True,
                    "status": dep.get("status"),
                    "commit_id": (commit.get("id") or "")[:7],
                    "commit_message": (message[0] if message else "")[:140],
                    "created_at": dep.get("createdAt"),
                    "finished_at": dep.get("finishedAt"),
                }
    except Exception as e:  # network errors must never crash the dashboard
        return {"available": False, "reason": f"{type(e).__name__}: {e}"}


async def restart_service() -> Dict[str, Any]:
    """Ask Render to restart the service."""
    if not is_configured():
        return {"restarted": False, "reason": "RENDER_API_KEY / RENDER_SERVICE_ID غير مضبوطة"}
    _, service_id = _credentials()
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=_headers()) as s:
            async with s.post(f"{_API_BASE}/services/{service_id}/restart") as r:
                if r.status in (200, 201, 202):
                    return {"restarted": True}
                body = (await r.text())[:200]
                return {"restarted": False, "reason": f"Render API HTTP {r.status}: {body}"}
    except Exception as e:
        return {"restarted": False, "reason": f"{type(e).__name__}: {e}"}
