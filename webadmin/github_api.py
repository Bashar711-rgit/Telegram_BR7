"""
webadmin/github_api.py – Minimal async GitHub API client (v9.31).

Master Prompt 6.13 «GitHub panel»: Repository · Branch · Current/Latest
Commit · Commit History · Deployment Relation (push → Render auto-deploy).

Credentials come ONLY from environment variables (Master Prompt 2.1):
  GITHUB_TOKEN  – fine-grained PAT (Contents: Read is enough)
  GITHUB_REPO   – "owner/repo" (optional; auto-detected from origin URL)

Degrades gracefully when not configured: every entry point returns a
{"configured": False, ...} dict instead of raising, so the dashboard
card can show setup instructions without error noise. No secrets are
ever logged or echoed back — only booleans and masked hints.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

import aiohttp

_API_BASE = "https://api.github.com"
_TIMEOUT = aiohttp.ClientTimeout(total=20)
_ORIGIN_RE = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def _credentials() -> tuple[str, str]:
    return (
        (os.getenv("GITHUB_TOKEN") or "").strip(),
        (os.getenv("GITHUB_REPO") or "").strip(),
    )


def is_configured() -> bool:
    token, repo = _credentials()
    return bool(token and repo)


def local_git_state(cwd: str = ".") -> Dict[str, str]:
    """v9.31: حالة git المحلية للعملية (فشل-آمن) — sha + فرع + owner/repo
    من origin. لا أسرار هنا: التوكن لا يظهر في روابط القراءة أبداً."""
    import subprocess

    def _git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", "-C", cwd, *args],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except Exception:
            return ""

    head = _git("rev-parse", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    url = _git("remote", "get-url", "origin")
    m = _ORIGIN_RE.search(url) if url else None
    return {"head": head, "branch": branch, "repo": m.group(1) if m else ""}


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _not_configured() -> Dict[str, Any]:
    token, repo = _credentials()
    return {
        "configured": False,
        "has_token": bool(token),
        "has_repo": bool(repo),
        "hint": (
            "عرّف GITHUB_TOKEN (fine-grained PAT بصلاحية Contents: Read) "
            "وGITHUB_REPO (owner/repo) في متغيرات البيئة — لا توكن في الكود."
        ),
    }


async def get_overview(local_head: Optional[str] = None) -> Dict[str, Any]:
    """نظرة عامة: المستودع + الفرع + آخر commit + حالة التزامن مع النشر.

    local_head: sha النسخة العاملة الحالية (من Render/Locally) — عند
    تطابقه مع آخر commit في GitHub يعني أن الكود المنشور محدّث.
    """
    token, repo = _credentials()
    if not (token and repo):
        return _not_configured()
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.get(f"{_API_BASE}/repos/{repo}", headers=_headers(token)) as r:
                if r.status == 401:
                    return {"configured": False, "has_token": True, "has_repo": bool(repo),
                            "error": "توكن GitHub مرفوض (401) — حدّث الـPAT"}
                if r.status == 404:
                    return {"configured": False, "has_token": True, "has_repo": True,
                            "error": f"المستودع {repo} غير موجود أو التوكن بلا وصول"}
                if r.status != 200:
                    return {"configured": False, "error": f"GitHub HTTP {r.status}"}
                info = await r.json()

            branch = info.get("default_branch") or "main"
            async with s.get(f"{_API_BASE}/repos/{repo}/commits",
                             params={"sha": branch, "per_page": 5},
                             headers=_headers(token)) as r:
                commits: List[Dict[str, Any]] = []
                if r.status == 200:
                    for c in (await r.json())[:5]:
                        commits.append({
                            "sha": (c.get("sha") or "")[:7],
                            "message": (c.get("commit", {}).get("message") or "").splitlines()[0][:120],
                            "author": (c.get("commit", {}).get("author") or {}).get("name", ""),
                            "date": (c.get("commit", {}).get("author") or {}).get("date", ""),
                        })

        head = commits[0] if commits else None
        synced = bool(head and local_head and head["sha"].startswith(local_head[:7]))
        return {
            "configured": True,
            "repo": repo,
            "branch": branch,
            "private": bool(info.get("private")),
            "pushed_at": info.get("pushed_at", ""),
            "latest_commit": head,
            "commits": commits,
            "local_head": (local_head or "")[:7],
            "up_to_date": synced,
            # Master Prompt 6.13: علاقة النشر — الدفع إلى GitHub يشغّل النشر التلقائي
            "deploy_relation": "الدفع إلى الفرع الرئيسي يشغّل النشر التلقائي على Render (Auto-Deploy).",
        }
    except Exception as e:
        return {"configured": False, "error": f"GitHub check failed: {e}"}


async def list_commits(limit: int = 10) -> Dict[str, Any]:
    """سجل commit المختصر (أول سطر من الرسالة فقط — بلا أسرار)."""
    token, repo = _credentials()
    if not (token and repo):
        return _not_configured()
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as s:
            async with s.get(f"{_API_BASE}/repos/{repo}/commits",
                             params={"per_page": max(1, min(int(limit), 30))},
                             headers=_headers(token)) as r:
                if r.status != 200:
                    return {"configured": True, "error": f"GitHub HTTP {r.status}"}
                return {"configured": True, "commits": await r.json()}
    except Exception as e:
        return {"configured": False, "error": f"GitHub check failed: {e}"}
