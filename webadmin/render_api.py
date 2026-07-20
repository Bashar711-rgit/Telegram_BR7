"""
webadmin/render_api.py – Minimal async Render API client.

Used to persist account/env configuration on Render and to trigger
service restarts. Degrades gracefully when RENDER_API_KEY or
RENDER_SERVICE_ID are not configured (local/dev environments).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

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
    """Bulk upsert env vars (single redeploy on Render's side)."""
    if not is_configured():
        return {"saved": False, "reason": "RENDER_API_KEY / RENDER_SERVICE_ID غير مضبوطة"}
    _, service_id = _credentials()
    payload: List[Dict[str, str]] = [{"key": k, "value": v} for k, v in pairs.items()]
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=_headers()) as s:
            async with s.put(
                f"{_API_BASE}/services/{service_id}/env-vars", json=payload
            ) as r:
                if r.status in (200, 201):
                    return {"saved": True}
                body = (await r.text())[:200]
                return {"saved": False, "reason": f"Render API HTTP {r.status}: {body}"}
    except Exception as e:
        return {"saved": False, "reason": f"{type(e).__name__}: {e}"}


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
