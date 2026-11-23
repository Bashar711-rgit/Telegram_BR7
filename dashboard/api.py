# -*- coding: utf-8 -*-
"""════════════════════════════════════════════════════════════════════════
dashboard.api — منطق الواجهة الخلفية للوحة (v15.1.0)
════════════════════════════════════════════════════════════════════════

كل الدوال هنا async وتعمل **حصراً على الحلقة الرئيسية للبوت** (main loop)
— تُستدعى عبر جسر run_coroutine_threadsafe من معالجات aiohttp التي تعيش
على حلقة خيط الصحة. لهذا:

  * نستخدم bot.db الموجود (pool مشترك واحد) — لا pool جديد.
  * نستخدم bot.monitors / bot.alert_bot / bot.filter الموجودة — لا
    Telethon client جديد ولا نسخة موازية من المحرك.
  * Filter Playground ينادي bot.filter.analyze() — نفس المحرك الحقيقي
    حرفياً (لا نسخة مبسطة، شرط صريح من المواصفات).

قاعدة الاستهلاك: كل القراءات المتكررة تمر عبر كاش مشترك single-flight
في dashboard.state — لا استعلام GROUP BY أكثر من مرة كل 30-60 ثانية
مهما كان عدد المستخدمين المتصلين باللوحة.
"""

from __future__ import annotations

import json as _json
import os
import re as _re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dashboard.state import DASH

PROJECT_DIR = Path(__file__).resolve().parent.parent

# ─────────────────────────────────────────────────────────────────────────
# ثوابت المحرر — حدود صارمة تمنع أي تضخم للذاكرة أو ملف keywords
# بنية keywords.json متداخلة فعلياً (مثل intent_verbs.tier_1_high.terms)،
# لذا يعمل المحرر على «وحدات أوراق»: كل قائمة نصية يُشار إليها بمسار
# منقّط (help_expressions أو ignore_signals.greetings أو
# intent_verbs.tier_1_high.terms). الفئات العليا المسموح بها فقط.
# ─────────────────────────────────────────────────────────────────────────
EDITABLE_TOP_CATEGORIES = frozenset({
    "intent_verbs",
    "implicit_request_patterns",
    "action_verbs",
    "solve_actions",
    "academic_objects",
    "request_phrases",
    "help_expressions",
    "urgency_markers",
    "subject_markers",
    "ignore_signals",
    "advertisement_signals",
    "ad_blockers",
})
MAX_UNITS = 120
MAX_ENTRIES_PER_UNIT = 2000
MAX_ENTRY_LENGTH = 200
MAX_UNIT_PATH_LENGTH = 120
MAX_PLAYGROUND_LENGTH = 500
MAX_IMPORT_BYTES = 1_000_000
IDENTITY_TTL_SECONDS = 300
MEMBERSHIP_TTL_SECONDS = 300
KW_FILE = PROJECT_DIR / "keywords_dashboard.json"
OPS_KEY = "dashboard:ops"
KEYWORDS_KEY = "dashboard:keywords_json"

_ALERT_LOG_LIMIT = 20
_AUDIT_LOG_LIMIT = 20
_BLOCKED_LOG_LIMIT = 50
_PEOPLE_LOG_LIMIT = 50


class DashboardHTTPError(Exception):
    """خطأ واضح يُحوَّل إلى استجابة JSON بحالة HTTP مناسبة."""

    def __init__(self, status: int, payload: Dict[str, Any]) -> None:
        super().__init__(str(payload.get("error", "error")))
        self.status = int(status)
        self.payload = payload


def _mask_phone(phone: Any) -> str:
    s = str(phone or "")
    if len(s) <= 8:
        return s[:2] + "****"
    return s[:5] + "****" + s[-3:]


def _sanitize_detail(value: Any, max_len: int = 200) -> str:
    """تنظيف أي نص عائد للواجهة (لا أسرار ولا أحرف تحكم)."""
    try:
        from security import sanitize_for_log
        return sanitize_for_log(value, max_len=max_len)
    except Exception:
        s = _re.sub(r"[\x00-\x1f\x7f\u2028\u2029]+", " ", str(value if value is not None else ""))
        return s[:max_len]


def _is_postgres(db: Any) -> bool:
    if os.getenv("DATABASE_URL"):
        return True
    try:
        from config import CFG
        return str(getattr(CFG, "DB_TYPE", "sqlite")).lower() == "postgresql"
    except Exception:
        return False


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fmt_uptime(seconds: int) -> str:
    if seconds <= 0:
        return "0د"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    if h >= 24:
        d, h2 = divmod(h, 24)
        return f"{d}ي {h2}س {m}د"
    if h:
        return f"{h}س {m}د"
    return f"{m}د"


def _iter_units(kw: Dict[str, Any]) -> Dict[str, List[str]]:
    """يستخرج وحدات أوراق (قوائم نصية) من الفئات العليا المسموح بها فقط."""
    units: Dict[str, List[str]] = {}

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k.startswith("_"):
                    continue
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            if not path:
                return
            top = path.split(".", 1)[0]
            if top in EDITABLE_TOP_CATEGORIES and all(isinstance(x, str) for x in node):
                units[path] = list(node)

    for top_key, sub in (kw or {}).items():
        if top_key in EDITABLE_TOP_CATEGORIES:
            walk(sub, top_key)
            if isinstance(sub, list) and all(isinstance(x, str) for x in sub):
                units[top_key] = list(sub)
    return units


def _set_unit(kw: Dict[str, Any], path: str, values: List[str]) -> bool:
    """يضبط ورقة قائمة بمسار منقّط — يُبقي المفاتيح الوصفية (_*) كما هي."""
    parts = path.split(".")
    if parts[0] not in EDITABLE_TOP_CATEGORIES:
        return False
    node: Any = kw
    for p in parts[:-1]:
        if not isinstance(node, dict) or p not in node:
            return False
        node = node[p]
    if not isinstance(node, dict) or parts[-1] not in node:
        return False
    if not isinstance(node[parts[-1]], list):
        return False
    node[parts[-1]] = values
    return True


def _runtime_weights() -> Dict[str, float]:
    """الأوزان الجارية الفعلية (المصدر الرسمي: env عبر config) — للعرض فقط."""
    out: Dict[str, float] = {}
    try:
        from config import CFG
        for name in ("SCORE_WEIGHT_INTENT", "SCORE_WEIGHT_ACADEMIC",
                     "SCORE_WEIGHT_GRAMMAR", "SCORE_WEIGHT_DISTANCE",
                     "SCORE_WEIGHT_URGENCY", "SCORE_WEIGHT_CONTEXT"):
            try:
                out[name.replace("SCORE_WEIGHT_", "").lower()] = float(getattr(CFG, name))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _parse_group_input(raw: str) -> Tuple[Optional[int], Optional[str]]:
    """يحوّل إدخال المستخدم إلى (chat_id رقمي) أو (username) — لا اختلاق."""
    s = (raw or "").strip()
    if not s:
        return None, None
    try:
        return int(s), ""
    except ValueError:
        pass
    m = _re.search(r"(?:t\.me|telegram\.me)/(@?[A-Za-z0-9_]{4,64})", s)
    if m:
        return None, m.group(1).lstrip("@")
    if s.startswith("@"):
        return None, s[1:].strip()
    if _re.fullmatch(r"[A-Za-z0-9_]{4,64}", s):
        return None, s
    return None, None


async def _audit(db: Any, action: str, object_type: str = "",
                 object_id: str = "", old_value: str = "",
                 new_value: str = "") -> None:
    """تدقيق العمليات الإدارية — جدول audit_logs الموجود (فشل-آمن)."""
    try:
        await db.record_audit(
            actor=f"dashboard:{DASH.username}",
            action=action,
            object_type=object_type,
            object_id=object_id,
            old_value=str(old_value)[:200] if old_value else "",
            new_value=str(new_value)[:200] if new_value else "",
            source="dashboard",
        )
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════
# الواجهة — كل الدوال تعمل على الحلقة الرئيسية
# ═════════════════════════════════════════════════════════════════════════
class DashboardAPI:

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    # ── helpers ────────────────────────────────────────────────────────
    @property
    def db(self) -> Any:
        return self.bot.db

    def _monitor_by_name(self, name: str) -> Any:
        for m in (getattr(self.bot, "monitors", None) or []):
            if (getattr(m, "account", {}) or {}).get("name") == name:
                return m
        return None

    async def _hourly_buckets(self, table: str, since: float) -> List[Dict[str, Any]]:
        col = "timestamp"
        if _is_postgres(self.db):
            sql = (
                f"SELECT (FLOOR({col} / 3600) * 3600)::bigint AS hb, COUNT(*) AS c "
                f"FROM {table} WHERE {col} >= ? GROUP BY hb ORDER BY hb"
            )
        else:
            sql = (
                f"SELECT CAST({col} / 3600 AS INTEGER) * 3600 AS hb, COUNT(*) AS c "
                f"FROM {table} WHERE {col} >= ? GROUP BY hb ORDER BY hb"
            )
        rows = await self.db._fetchall(sql, (since,))
        return [{"h": int(r["hb"]), "c": int(r["c"])} for r in rows if r and r.get("hb")]

    # ═══════════════════════════════════════════════════════════════════
    # الوحدة 1: مركز المراقبة
    # ═══════════════════════════════════════════════════════════════════
    async def overview(self) -> Dict[str, Any]:
        bot = self.bot
        db = self.db
        now = time.time()
        day_start = now - (now % 86400.0)
        since_24h = now - 86400.0

        snap: Dict[str, Any] = {}
        try:
            snap = (bot._health_snapshot.get() or {}).get("payload") or {}
        except Exception:
            snap = {}

        stats = await db.get_stats()

        row = await db._fetchone("SELECT COUNT(*) AS c FROM messages WHERE timestamp >= ?", (day_start,))
        msgs_today = int(row["c"]) if row and row.get("c") else 0

        row = await db._fetchone("SELECT COUNT(*) AS c FROM alerts WHERE timestamp >= ?", (day_start,))
        alerts_today = int(row["c"]) if row and row.get("c") else 0

        alert_status_today: Dict[str, int] = {}
        try:
            rows = await db._fetchall(
                "SELECT COALESCE(alert_status, 'unknown') AS s, COUNT(*) AS c "
                "FROM alerts WHERE timestamp >= ? GROUP BY s",
                (day_start,),
            )
            for r in rows:
                alert_status_today[str(r["s"])] = int(r["c"])
        except Exception:
            pass

        filter_today = {"accepted": 0, "rejected": 0, "review": 0, "total": 0}
        try:
            r = await db._fetchone(
                "SELECT COUNT(*) AS c, COALESCE(SUM(accepted), 0) AS a, "
                "COALESCE(SUM(rejected), 0) AS rj FROM message_analysis WHERE created_at >= ?",
                (day_start,),
            )
            if r:
                filter_today["total"] = int(r["c"] or 0)
                filter_today["accepted"] = int(r["a"] or 0)
                filter_today["rejected"] = int(r["rj"] or 0)
        except Exception:
            pass

        row = await db._fetchone("SELECT COUNT(*) AS c FROM sources")
        sources_total = int(row["c"]) if row and row.get("c") else 0
        row = await db._fetchone("SELECT COUNT(*) AS c FROM sources WHERE enabled = 0")
        sources_paused = int(row["c"]) if row and row.get("c") else 0

        monitors_out: List[Dict[str, Any]] = []
        connected = 0
        last_event_ts = 0.0
        monitors_with_errors: List[str] = []
        for m in (getattr(bot, "monitors", None) or []):
            acc = getattr(m, "account", {}) or {}
            is_conn = bool(getattr(m, "is_connected", False))
            if is_conn:
                connected += 1
            ev_ts = float(getattr(m, "last_event_ts", 0.0) or 0.0)
            if ev_ts > last_event_ts:
                last_event_ts = ev_ts
            last_err = (getattr(m, "_stats", {}) or {}).get("last_error")
            if last_err:
                monitors_with_errors.append(str(acc.get("name", "?")))
            monitors_out.append({
                "name": str(acc.get("name", "?")),
                "connected": is_conn,
                "events_received": int(getattr(m, "events_received", 0) or 0),
                "last_event_age_s": int(now - ev_ts) if ev_ts else None,
            })

        # كشف توقف الاستقبال رغم ظهور الحسابات متصلة (ميزة الوحدة 1)
        staleness_min = 30
        try:
            from config import CFG
            staleness_min = max(5, int(getattr(CFG, "DASHBOARD_STALENESS_MINUTES", 30)))
        except Exception:
            pass
        boot_complete = bool(getattr(bot, "_boot_complete", False))
        age_min = (now - last_event_ts) / 60.0 if last_event_ts else None
        stale = False
        stale_note = ""
        if boot_complete and connected > 0:
            if last_event_ts <= 0.0:
                uptime_s = max(0, int(time.monotonic() - getattr(bot, "_start_time", time.monotonic())))
                if uptime_s > staleness_min * 60:
                    stale = True
                    stale_note = "لم يُستقبل أي نشاط استقبال حقيقي منذ الإقلاع رغم اتصال الحسابات"
            elif age_min is not None and age_min > staleness_min:
                stale = True
                stale_note = "لا نشاط استقبال حقيقي منذ آخر رسالة — الحسابات تظهر متصلة"

        # نظام
        rss_mb = 0.0
        limit_mb = 0
        try:
            from memory_guard import snapshot as _mg_snap
            mg = _mg_snap() or {}
            rss_mb = float(mg.get("last_rss_mb", 0.0) or 0.0)
            limit_mb = int(mg.get("limit_mb", 0) or 0)
        except Exception:
            pass
        cpu = 0.0
        try:
            import psutil
            proc_cpu = psutil.Process().cpu_percent(None)
            DASH.note_cpu(proc_cpu)
            cpu = DASH.read_cpu()
        except Exception:
            pass

        telemetry: Dict[str, Any] = {}
        try:
            tel = await bot.filter.get_telemetry()
            if isinstance(tel, dict):
                telemetry = {
                    "processed": tel.get("processed"),
                    "accepted": tel.get("accepted"),
                    "review": tel.get("review"),
                    "spam": tel.get("spam"),
                    "prefilter_rejected": tel.get("prefilter_rejected"),
                    "cache_hits": tel.get("cache_hits"),
                    "avg_time_ms": tel.get("avg_time_ms"),
                }
        except Exception:
            pass

        # أخطاء مختصرة
        sys_health_rows: List[Dict[str, Any]] = []
        try:
            rows = await self.db._fetchall(
                "SELECT component, status, details, timestamp FROM system_health "
                "ORDER BY timestamp DESC LIMIT 5"
            )
            for r in rows:
                sys_health_rows.append({
                    "component": str(r.get("component", "")),
                    "status": str(r.get("status", "")),
                    "details": _sanitize_detail(r.get("details"), 160),
                    "ts": float(r.get("timestamp") or 0.0),
                })
        except Exception:
            pass
        dead_count = 0
        try:
            r = await db._fetchone("SELECT COUNT(*) AS c FROM dead_letters WHERE resolved = 0")
            dead_count = int(r["c"]) if r and r.get("c") else 0
        except Exception:
            pass

        msgs_chart = await self._hourly_buckets("messages", since_24h)
        alerts_chart = await self._hourly_buckets("alerts", since_24h)
        chart: List[Dict[str, Any]] = []
        bucket = {d["h"]: d["c"] for d in msgs_chart}
        bucket_a = {d["h"]: d["c"] for d in alerts_chart}
        hb_now = int(now // 3600) * 3600
        for i in range(23, -1, -1):
            h = hb_now - i * 3600
            chart.append({"h": h, "msgs": bucket.get(h, 0), "alerts": bucket_a.get(h, 0)})

        sent_ok = alert_status_today.get("sent", 0)
        sent_failed = alert_status_today.get("failed", 0)
        conversion = round((alerts_today / msgs_today) * 100.0, 1) if msgs_today else 0.0

        uptime_s = max(0, int(time.monotonic() - getattr(bot, "_start_time", time.monotonic())))
        try:
            from config import BOT_VERSION
            version = BOT_VERSION
        except Exception:
            version = "?"

        return {
            "time": _now_iso(),
            "status": str(snap.get("status", "unknown")),
            "boot_complete": boot_complete,
            "uptime_seconds": uptime_s,
            "uptime_text": _fmt_uptime(uptime_s),
            "accounts": {"total": len(monitors_out), "connected": connected, "monitors": monitors_out},
            "groups": {"sources": sources_total, "paused": sources_paused,
                       "allow_all": sources_total == 0},
            "messages": {"today": msgs_today,
                         "last_hour": int(stats.get("messages_last_hour", 0) or 0),
                         "total_db": int(stats.get("total_messages", 0) or 0)},
            "filter": {**filter_today,
                       "telemetry": telemetry,
                       "lang_filter": bool(getattr(_cfg(), "LANGUAGE_FILTER", True))},
            "alerts": {"today": alerts_today, "sent_ok": sent_ok,
                       "failed": sent_failed,
                       "last_hour": int(stats.get("alerts_last_hour", 0) or 0),
                       "conversion_pct": conversion},
            "system": {"rss_mb": round(rss_mb, 1), "limit_mb": limit_mb,
                       "cpu_percent": round(cpu, 1),
                       "db_healthy": bool(stats.get("db_healthy", True)),
                       "db_latency_ms": snap.get("db_latency_ms"),
                       "queue_size": snap.get("queue_size")},
            "staleness": {"stale": stale, "note": stale_note,
                          "last_activity_ts": last_event_ts or None,
                          "age_minutes": round(age_min, 1) if age_min is not None else None,
                          "threshold_minutes": staleness_min},
            "errors": {"system_health": sys_health_rows,
                       "dead_letters_unresolved": dead_count,
                       "monitors_with_errors": monitors_with_errors[:10]},
            "chart_24h": chart,
            "bot_version": version,
            "dashboard_version": DASH.version,
        }

    # ═══════════════════════════════════════════════════════════════════
    # الوحدة 2: الحسابات
    # ═══════════════════════════════════════════════════════════════════
    async def accounts_list(self) -> Dict[str, Any]:
        bot = self.bot
        now = time.time()
        items: List[Dict[str, Any]] = []
        for m in (getattr(bot, "monitors", None) or []):
            acc = getattr(m, "account", {}) or {}
            name = str(acc.get("name", "?"))
            st = getattr(m, "_stats", {}) or {}
            recon: Dict[str, Any] = {}
            try:
                recon = dict(getattr(m, "_reconnect").snapshot() or {})
            except Exception:
                recon = {}
            ev_ts = float(getattr(m, "last_event_ts", 0.0) or 0.0)
            ident = DASH.identity_cache.get(name)
            if ident and (now - ident.get("ts", 0.0)) > IDENTITY_TTL_SECONDS:
                ident = None
            mem = DASH.membership_cache.get(name)
            if mem and (now - mem.get("ts", 0.0)) > MEMBERSHIP_TTL_SECONDS:
                mem = None
            items.append({
                "name": name,
                "phone_masked": _mask_phone(acc.get("phone")),
                "is_main": bool(acc.get("is_main")),
                "enabled": bool(acc.get("enabled", True)),
                "connected": bool(getattr(m, "is_connected", False)),
                "terminal_error": getattr(m, "_terminal_error", None),
                "events_received": int(getattr(m, "events_received", 0) or 0),
                "last_event_age_s": int(now - ev_ts) if ev_ts else None,
                "messages_processed": int(st.get("messages_processed", 0) or 0),
                "alerts_sent": int(st.get("alerts_sent", 0) or 0),
                "errors": int(st.get("errors", 0) or 0),
                "send_errors": int(st.get("send_errors", 0) or 0),
                "duplicates": int(st.get("duplicates", 0) or 0),
                "rate_limited": int(st.get("rate_limited", 0) or 0),
                "last_error": _sanitize_detail(st.get("last_error")) if st.get("last_error") else None,
                "reconnect": {
                    "attempts": recon.get("attempts") if isinstance(recon, dict) else None,
                    "backoff_s": recon.get("backoff_seconds") if isinstance(recon, dict) else None,
                    "running": recon.get("running") if isinstance(recon, dict) else None,
                },
                "identity": (ident or {}).get("data"),
                "membership": (mem or {}).get("data"),
            })
        # ═══ v15.2.0: حسابات اللوحة (panel_accounts — جلسات مشفّرة في DB) ═══
        # تُدمج مع حسابات env: المراقب الحي يحمل الحالة الفعلية، والصفوف
        # بلا مراقب تظهر بحالة DB (جاهز/متوقف/يحتاج إعادة دخول/خطأ).
        try:
            panel_rows = await self.db.get_panel_accounts()
        except Exception:
            panel_rows = []
        mon_by_phone = {}
        for m in (getattr(bot, "monitors", None) or []):
            acc = getattr(m, "account", {}) or {}
            pk = _re.sub(r"\D", "", str(acc.get("phone") or ""))
            if pk:
                mon_by_phone[pk] = m
        env_phones = set(mon_by_phone.keys())
        panel_phones = set()
        for row in panel_rows:
            pk = _re.sub(r"\D", "", str(row.get("phone") or ""))
            panel_phones.add(pk)
            mon = mon_by_phone.get(pk)
            enabled = bool(int(row.get("monitoring_enabled") or 0))
            st = getattr(mon, "_stats", {}) or {}
            ev_ts = float(getattr(mon, "last_event_ts", 0.0) or 0.0)
            if mon is not None and getattr(mon, "is_connected", False):
                status = "active"
            elif mon is not None and getattr(mon, "_terminal_error", None):
                status = "needs_relogin"
            elif mon is not None and st.get("last_error"):
                status = "error"
            elif not enabled:
                status = "stopped"
            else:
                status = str(row.get("status") or "ready")
            items.append({
                "kind": "panel",
                "row_id": int(row.get("id") or 0),
                "name": str(row.get("display_name") or "").strip() or "حساب لوحة",
                "username": str(row.get("username") or "") or None,
                "tg_user_id": int(row.get("tg_user_id") or 0) or None,
                "phone_masked": _mask_phone(row.get("phone")),
                "is_main": False,
                "enabled": enabled,
                "monitoring_enabled": enabled,
                "status": status,
                "connected": bool(getattr(mon, "is_connected", False)) if mon else False,
                "terminal_error": getattr(mon, "_terminal_error", None) if mon else None,
                "events_received": int(getattr(mon, "events_received", 0) or 0) if mon else 0,
                "last_event_age_s": int(now - ev_ts) if (mon and ev_ts) else None,
                "messages_processed": int(st.get("messages_processed", 0) or 0) if mon else 0,
                "alerts_sent": int(st.get("alerts_sent", 0) or 0) if mon else 0,
                "errors": int(st.get("errors", 0) or 0) if mon else 0,
                "send_errors": int(st.get("send_errors", 0) or 0) if mon else 0,
                "duplicates": int(st.get("duplicates", 0) or 0) if mon else 0,
                "rate_limited": int(st.get("rate_limited", 0) or 0) if mon else 0,
                "last_error": _sanitize_detail(row.get("last_error") or (st.get("last_error") if mon else None)) or None,
                "reconnect": {},
                "identity": None,
                "membership": (DASH.membership_cache.get(f"panel:{row.get('id')}") or {}).get("data"),
            })
        env_only_phones = env_phones - panel_phones
        return {
            "items": items,
            "stats": {
                "total": len(items),
                "active": sum(1 for a in items if a.get("status") == "active"),
                "stopped": sum(1 for a in items if a.get("status") == "stopped"),
                "needs_relogin": sum(1 for a in items if a.get("status") == "needs_relogin"),
                "error": sum(1 for a in items if a.get("status") == "error"),
            },
            "note": ("حسابات env تُدار من متغيرات Render — وحسابات اللوحة تُضاف/تُحذف "
                     "من هذا التبويب بجلسات مشفّرة في قاعدة البيانات دون إعادة نشر. "
                     f"env مستقلة: {len(env_only_phones)} · لوحة: {len(panel_rows)}."),
        }

    async def account_test(self, name: str) -> Dict[str, Any]:
        """اختبار الاتصال الفعلي عبر get_me() — تزامن محدود (semaphore 3)."""
        m = self._monitor_by_name(name)
        if m is None:
            raise DashboardHTTPError(404, {"error": "account_not_found"})
        client = getattr(m, "client", None)
        if client is None or not bool(getattr(m, "is_connected", False)):
            return {"ok": False, "reason": "not_connected"}
        if not hasattr(self.bot, "_dash_test_sem"):
            import asyncio
            self.bot._dash_test_sem = asyncio.Semaphore(3)
        async with self.bot._dash_test_sem:
            try:
                me = await asyncio_wait_for(client.get_me(), timeout=15)
            except Exception as e:
                DASH.identity_cache.pop(name, None)
                return {"ok": False, "reason": f"{type(e).__name__}: {_sanitize_detail(e, 120)}"}
        data = {
            "id": getattr(me, "id", None),
            "username": getattr(me, "username", None),
            "first_name": str(getattr(me, "first_name", "") or "")[:64],
            "phone_masked": _mask_phone(getattr(me, "phone", None)),
        }
        DASH.identity_cache[name] = {"ts": time.time(), "data": data}
        await _audit(self.db, "dashboard.account_test", "account", name,
                     new_value=f"ok user_id={data.get('id')}")
        return {"ok": True, "identity": data}

    async def account_reconnect(self, name: str) -> Dict[str, Any]:
        """إعادة اتصال دون إعادة نشر — connect() آمن تحت _connect_lock."""
        m = self._monitor_by_name(name)
        if m is None:
            raise DashboardHTTPError(404, {"error": "account_not_found"})
        if getattr(m, "_terminal_error", None):
            return {"ok": False,
                    "reason": f"terminal_error: {m._terminal_error}",
                    "note": "الحساب بحالة نهائية (AuthKey/2FA) — يحتاج Session String جديد من Render"}
        try:
            ok = await asyncio_wait_for(m.connect(), timeout=90)
        except Exception as e:
            ok = False
            reason = f"{type(e).__name__}: {_sanitize_detail(e, 160)}"
            await _audit(self.db, "dashboard.account_reconnect", "account", name,
                         new_value=f"failed {reason}")
            return {"ok": False, "reason": reason}
        await _audit(self.db, "dashboard.account_reconnect", "account", name,
                     new_value="ok" if ok else "failed")
        return {"ok": bool(ok), "connected": bool(getattr(m, "is_connected", False))}

    async def account_membership(self, name: str) -> Dict[str, Any]:
        """فحص عضوية الحساب في مجموعة الهدف — فحص مباشر جديد (لا بوابة
        التحذير-مرة-واحدة الخاصة بالمسار التلقائي)."""
        m = self._monitor_by_name(name)
        if m is None:
            raise DashboardHTTPError(404, {"error": "account_not_found"})
        client = getattr(m, "client", None)
        if client is None or not bool(getattr(m, "is_connected", False)):
            return {"ok": False, "status": "not_connected"}
        target = getattr(_cfg(), "TARGET_GROUP_ID", 0)
        if not target:
            return {"ok": False, "status": "no_target_group"}
        try:
            perms = await asyncio_wait_for(client.get_permissions(target, "me"), timeout=20)
            status = "ok" if bool(getattr(perms, "is_member", True)) else "missing"
        except Exception as e:
            status = f"error:{type(e).__name__}"
        data = {"status": status, "checked_at": _now_iso()}
        DASH.membership_cache[name] = {"ts": time.time(), "data": data}
        return {"ok": status == "ok", **data}

    # ═══════════════════════════════════════════════════════════════════
    # الوحدة 3: الكلمات + Filter Playground (المحرك الحقيقي)
    # ═══════════════════════════════════════════════════════════════════
    async def keywords_view(self) -> Dict[str, Any]:
        from config import KEYWORDS
        units = _iter_units(KEYWORDS or {})
        custom = bool(await self._custom_keywords_row())
        return {"units": units, "runtime_weights": _runtime_weights(),
                "meta": {"version": str((KEYWORDS or {}).get("_meta", {}).get("version", ""))
                         if isinstance((KEYWORDS or {}).get("_meta"), dict) else ""},
                "custom_active": custom,
                "note": ("التعديل يعمل على قوائم الكلمات (وحدات أوراق) ويُطبَّق فوراً على المحرك الحقيقي "
                         "وتُحفظ لتبقى بعد إعادة التشغيل. أوزان التقييم الجارية تُدار من env "
                         "(SCORE_WEIGHT_*) وتُعرض أعلاه للمرجعية فقط.")}

    async def _custom_keywords_row(self) -> Optional[str]:
        try:
            row = await self.db._fetchone(
                "SELECT value FROM app_settings WHERE key = ?", (KEYWORDS_KEY,)
            )
            return (row or {}).get("value")
        except Exception:
            return None

    async def keywords_save(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """تطبيق تعديلات وحدات الكلمات على المحرك الحقيقي + تخزين دائم.

        التعديل يجري على نسخة كاملة من config.KEYWORDS (يُستبدل محتوى ورقة
        القائمة فقط، وتُبقى مفاتيح الوصف والأوزان كما هي) ثم يُكتب ملف
        keywords_dashboard.json ويُعاد بناء المحرك عبر reload_keywords —
        نفس مسار التحميل الأصلي تماماً (لا منطق موازٍ).
        """
        import copy as _copy
        from config import KEYWORDS

        if not isinstance(patch, dict):
            raise DashboardHTTPError(400, {"error": "invalid_payload"})
        units_in = patch.get("units") or {}
        if not isinstance(units_in, dict) or not units_in:
            raise DashboardHTTPError(400, {"error": "nothing_to_save"})
        if len(units_in) > MAX_UNITS:
            raise DashboardHTTPError(400, {"error": "too_many_units"})

        new_kw = _copy.deepcopy(dict(KEYWORDS or {}))
        changed = 0
        for path, entries in units_in.items():
            p = str(path or "")
            if len(p) > MAX_UNIT_PATH_LENGTH:
                raise DashboardHTTPError(400, {"error": f"path_too_long:{p[:40]}"})
            if not isinstance(entries, list):
                raise DashboardHTTPError(400, {"error": f"invalid_entries:{p[:40]}"})
            if len(entries) > MAX_ENTRIES_PER_UNIT:
                raise DashboardHTTPError(400, {"error": f"too_many_entries:{p[:40]}"})
            clean: List[str] = []
            for e in entries:
                s = str(e or "").strip()
                if not s:
                    continue
                if len(s) > MAX_ENTRY_LENGTH:
                    raise DashboardHTTPError(400, {"error": f"entry_too_long:{p[:40]}"})
                clean.append(s)
            if not _set_unit(new_kw, p, clean):
                raise DashboardHTTPError(400, {"error": f"unit_not_editable:{p[:40]}"})
            changed += len(clean)

        payload = _json.dumps(new_kw, ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise DashboardHTTPError(400, {"error": "payload_too_large"})

        await self._apply_keywords(new_kw)
        await _audit(self.db, "dashboard.keywords_save", "keywords", "",
                     new_value=f"entries={changed}")
        DASH.overview_cache.invalidate()
        return {"ok": True, "changed": changed}

    async def keywords_import(self, json_text: str) -> Dict[str, Any]:
        """استيراد JSON — يقبل صيغتين:
        {"units": {مسار: [كلمات]}} أو ملف تصدير كامل {"keywords": {...}}
        — يُطبّق فقط قوائم الأوراق داخل الفئات المسموح بها."""
        raw = (json_text or "").strip()
        if not raw or len(raw.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise DashboardHTTPError(400, {"error": "invalid_or_too_large"})
        try:
            data = _json.loads(raw)
        except Exception as e:
            raise DashboardHTTPError(400, {"error": f"invalid_json:{type(e).__name__}"})
        if not isinstance(data, dict):
            raise DashboardHTTPError(400, {"error": "root_must_be_object"})
        if "units" in data and isinstance(data["units"], dict):
            units_in = data["units"]
        elif "keywords" in data and isinstance(data["keywords"], dict):
            units_in = _iter_units(data["keywords"])
        else:
            raise DashboardHTTPError(400, {"error": "no_units_or_keywords"})
        if not units_in or len(units_in) > MAX_UNITS:
            raise DashboardHTTPError(400, {"error": "no_editable_units_or_too_many"})
        import copy as _copy
        from config import KEYWORDS
        new_kw = _copy.deepcopy(dict(KEYWORDS or {}))
        applied = 0
        for path, entries in units_in.items():
            p = str(path or "")
            if len(p) > MAX_UNIT_PATH_LENGTH or not isinstance(entries, list):
                continue
            clean = [str(e).strip()[:MAX_ENTRY_LENGTH]
                     for e in entries if str(e or "").strip()][:MAX_ENTRIES_PER_UNIT]
            if _set_unit(new_kw, p, clean):
                applied += len(clean)
        if applied == 0:
            raise DashboardHTTPError(400, {"error": "no_editable_units_found"})
        await self._apply_keywords(new_kw)
        await _audit(self.db, "dashboard.keywords_import", "keywords", "",
                     new_value=f"applied={applied}")
        return {"ok": True, "applied": applied}

    async def _apply_keywords(self, new_kw: Dict[str, Any]) -> None:
        """الكتابة الدائمة (app_settings) + الملف + إعادة بناء المحرك الحقيقي."""
        # 1) تخزين دائم في app_settings (يبقى بعد إعادة النشر)
        await self.db._execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = CURRENT_TIMESTAMP",
            (KEYWORDS_KEY, _json.dumps(new_kw, ensure_ascii=False)),
        )
        await self.db._commit()
        # 2) تحديث config.KEYWORDS في مكانها (يظل export متسقاً مع الحي)
        try:
            from config import KEYWORDS as _KW
            _KW.clear()
            _KW.update(new_kw)
        except Exception:
            pass
        # 3) كتابة الملف + إعادة بناء المحرك من نفس مسار التحميل الأصلي
        KW_FILE.write_text(_json.dumps(new_kw, ensure_ascii=False), encoding="utf-8")
        reload = getattr(self.bot.filter, "reload_keywords", None)
        if reload is not None:
            await reload(str(KW_FILE))
            # ══ v15.6.0: إعادة بناء البوابة السرعة من القوائم الجديدة ══
            try:
                from fast_gate import get_fast_gate as _fgg
                _fg = _fgg()
                if _fg is not None:
                    _fg.schedule_rebuild()
            except Exception:
                pass

    async def keywords_export(self) -> Dict[str, Any]:
        from config import KEYWORDS
        return {"keywords": KEYWORDS or {}, "exported_at": _now_iso()}

    async def playground(self, text: str) -> Dict[str, Any]:
        """Filter Playground — نفس المحرك الحقيقي bot.filter.analyze() حرفياً.

        يعيد كل الدرجات التي يحسبها المحرك: الكلمات المتطابقة، Intent /
        Context / Negative / Confidence / Decision + أسباب القبول أو الرفض.
        """
        t = (text or "").strip()[:MAX_PLAYGROUND_LENGTH]
        if not t:
            raise DashboardHTTPError(400, {"error": "empty_text"})
        try:
            from filter_engine import FILTER_ENGINE_VERSION
        except Exception:
            FILTER_ENGINE_VERSION = "?"
        analysis = await self.bot.filter.analyze(t)
        try:
            from config import CFG
            thresholds = {
                "accept": float(getattr(CFG, "CONFIDENCE_ACCEPT_THRESHOLD", 0.65)),
                "review": float(getattr(CFG, "CONFIDENCE_REVIEW_THRESHOLD", 0.40)),
            }
        except Exception:
            thresholds = {"accept": 0.65, "review": 0.40}
        return {
            "analysis": analysis,
            "thresholds": thresholds,
            "engine_version": FILTER_ENGINE_VERSION,
        }

    async def keyword_stats(self) -> Dict[str, Any]:
        """إحصائيات أداء الكلمات + كاشفات التطابقات الزائفة (7 أيام)."""
        now = time.time()
        week = now - 7 * 86400.0
        top_keywords: List[Dict[str, Any]] = []
        top_rejections: List[Dict[str, Any]] = []
        false_positives: List[Dict[str, Any]] = []
        try:
            rows = await self.db._fetchall(
                "SELECT keyword, COUNT(*) AS c FROM alerts WHERE timestamp >= ? "
                "GROUP BY keyword ORDER BY c DESC LIMIT 12",
                (week,),
            )
            top_keywords = [{"keyword": str(r["keyword"] or "؟"), "count": int(r["c"])}
                            for r in rows]
        except Exception:
            pass
        try:
            rows = await self.db._fetchall(
                "SELECT COALESCE(rejection_reason, 'unknown') AS rr, COUNT(*) AS c "
                "FROM message_analysis WHERE created_at >= ? AND rejected = 1 "
                "GROUP BY rr ORDER BY c DESC LIMIT 12",
                (week,),
            )
            top_rejections = [{"reason": _sanitize_detail(r["rr"], 120), "count": int(r["c"])}
                              for r in rows]
        except Exception:
            pass
        try:
            rows = await self.db._fetchall(
                "SELECT matched_keywords AS mk, COUNT(*) AS c FROM message_analysis "
                "WHERE created_at >= ? AND rejected = 1 AND matched_keywords IS NOT NULL "
                "AND matched_keywords != '' GROUP BY mk ORDER BY c DESC LIMIT 12",
                (week,),
            )
            false_positives = [{"matched": _sanitize_detail(r["mk"], 160), "count": int(r["c"])}
                               for r in rows]
        except Exception:
            pass
        return {"top_keywords": top_keywords, "top_rejections": top_rejections,
                "false_positives": false_positives, "window_days": 7}

    # ═══════════════════════════════════════════════════════════════════
    # الوحدة 4: المجموعات
    # ═══════════════════════════════════════════════════════════════════
    async def groups_list(self) -> Dict[str, Any]:
        cached = DASH.groups_cache.get_if_fresh()
        if cached is not None:
            return cached
        if not DASH.groups_cache.try_begin_refresh():
            stale = DASH.groups_cache.get_stale()
            if stale is not None:
                return stale
        try:
            data = await self._groups_list_uncached()
            DASH.groups_cache.end_refresh(data)
            return data
        except DashboardHTTPError:
            DASH.groups_cache.abort_refresh()
            raise
        except Exception as e:
            DASH.groups_cache.abort_refresh()
            raise DashboardHTTPError(500, {"error": f"groups_failed:{type(e).__name__}"})

    async def _groups_list_uncached(self) -> Dict[str, Any]:
        db = self.db
        now = time.time()
        since_24h = now - 86400.0
        sources = await db.list_sources(limit=500)
        agg_msgs: Dict[int, Dict[str, Any]] = {}
        try:
            rows = await db._fetchall(
                "SELECT chat_id, COUNT(*) AS c, MAX(timestamp) AS last_ts "
                "FROM messages GROUP BY chat_id"
            )
            for r in rows:
                cid = int(r["chat_id"])
                agg_msgs[cid] = {"msgs": int(r["c"]), "last_ts": float(r["last_ts"] or 0.0)}
        except Exception:
            pass
        agg_alerts: Dict[int, int] = {}
        try:
            rows = await db._fetchall(
                "SELECT chat_id, COUNT(*) AS c FROM alerts WHERE timestamp >= ? GROUP BY chat_id",
                (since_24h,),
            )
            for r in rows:
                agg_alerts[int(r["chat_id"])] = int(r["c"])
        except Exception:
            pass

        staleness_min = 30
        try:
            staleness_min = max(5, int(getattr(_cfg(), "DASHBOARD_STALENESS_MINUTES", 30)))
        except Exception:
            pass

        items: List[Dict[str, Any]] = []
        for s in sources:
            cid = int(s.get("chat_id") or 0)
            a = agg_msgs.get(cid, {})
            last_ts = float(a.get("last_ts") or 0.0)
            age_min = (now - last_ts) / 60.0 if last_ts else None
            enabled = bool(s.get("enabled", True))
            items.append({
                "chat_id": cid,
                "title": _sanitize_detail(s.get("title") or s.get("username") or str(cid), 120),
                "username": s.get("username") or None,
                "type": s.get("type") or "",
                "enabled": enabled,
                "msgs_total": int(a.get("msgs") or 0),
                "alerts_24h": int(agg_alerts.get(cid, 0)),
                "last_msg_age_minutes": round(age_min, 1) if age_min is not None else None,
                "stale": bool(enabled and age_min is not None and age_min > staleness_min),
                "added_by": s.get("added_by") or "",
            })
        return {"items": items, "allow_all": len(items) == 0,
                "staleness_threshold_minutes": staleness_min}

    async def group_add(self, raw_input: str) -> Dict[str, Any]:
        chat_id, username = _parse_group_input(raw_input)
        if chat_id is None and not username:
            raise DashboardHTTPError(400, {"error": "invalid_input",
                                           "note": "استخدم رابط t.me/username أو @username أو المعرف الرقمي (-100...)"})
        title = ""
        gtype = ""
        if chat_id is None:
            client = getattr(self.bot, "main_client", None)
            if client is None:
                raise DashboardHTTPError(503, {"error": "no_main_client"})
            try:
                entity = await asyncio_wait_for(client.get_entity(username), timeout=25)
                chat_id = int(getattr(entity, "id", 0) or 0)
                title = str(getattr(entity, "title", "") or username)
                gtype = "channel" if getattr(entity, "broadcast", False) else "group"
            except Exception as e:
                raise DashboardHTTPError(400, {"error": f"resolve_failed:{type(e).__name__}"})
            if not chat_id:
                raise DashboardHTTPError(400, {"error": "resolve_failed"})
        if chat_id < 0:
            chat_id = chat_id  # نحفظ بصيغة Telethon (-100...) كما تصل الأحداث
        existing = await self.db.get_source(chat_id)
        if existing:
            raise DashboardHTTPError(409, {"error": "already_exists"})
        ok = await self.db.add_source(chat_id, username=username or "",
                                      title=title or username or str(chat_id),
                                      type_=gtype, added_by=f"dashboard:{DASH.username}")
        try:
            self.db.invalidate_sources_cache()
        except Exception:
            pass
        DASH.groups_cache.invalidate()
        await _audit(self.db, "dashboard.group_add", "source", str(chat_id),
                     new_value=title or username or str(chat_id))
        return {"ok": bool(ok), "chat_id": chat_id, "title": title or username}

    async def group_toggle(self, chat_id: int, enabled: bool) -> Dict[str, Any]:
        ok = await self.db.set_source_enabled(int(chat_id), bool(enabled))
        try:
            self.db.invalidate_sources_cache()
        except Exception:
            pass
        DASH.groups_cache.invalidate()
        await _audit(self.db, "dashboard.group_toggle", "source", str(chat_id),
                     new_value="enabled" if enabled else "paused")
        return {"ok": bool(ok)}

    async def group_remove(self, chat_id: int) -> Dict[str, Any]:
        ok = await self.db.remove_source(int(chat_id))
        try:
            self.db.invalidate_sources_cache()
        except Exception:
            pass
        DASH.groups_cache.invalidate()
        await _audit(self.db, "dashboard.group_remove", "source", str(chat_id))
        return {"ok": bool(ok)}

    # ═══════════════════════════════════════════════════════════════════
    # الوحدة 5: التنبيهات
    # ═══════════════════════════════════════════════════════════════════
    async def alerts_log(self, page: int, status: str) -> Dict[str, Any]:
        try:
            page = max(1, min(int(page), 500))
        except (TypeError, ValueError):
            page = 1
        offset = (page - 1) * _ALERT_LOG_LIMIT
        where = ""
        params: List[Any] = []
        if status in ("sent", "failed", "unknown"):
            where = "WHERE COALESCE(alert_status, 'unknown') = ?"
            params.append(status)
        total = 0
        try:
            r = await self.db._fetchone(
                f"SELECT COUNT(*) AS c FROM alerts {where}", tuple(params)
            )
            total = int(r["c"]) if r and r.get("c") else 0
        except Exception:
            pass
        sql = (
            "SELECT id, timestamp, chat_id, sender_id, account_name, keyword, "
            "alert_text, decision, confidence, contact_method, alert_status, "
            "notification_error, button_message_url, button_user_url "
            f"FROM alerts {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        )
        rows = await self.db._fetchall(sql, tuple(params + [_ALERT_LOG_LIMIT + 1, offset]))
        has_more = len(rows) > _ALERT_LOG_LIMIT
        rows = rows[:_ALERT_LOG_LIMIT]
        items = []
        for r in rows:
            items.append({
                "id": r.get("id"),
                "ts": float(r.get("timestamp") or 0.0),
                "chat_id": r.get("chat_id"),
                "sender_id": r.get("sender_id"),
                "account": r.get("account_name"),
                "keyword": _sanitize_detail(r.get("keyword"), 60),
                "text": _sanitize_detail(r.get("alert_text"), 300),
                "decision": r.get("decision"),
                "confidence": r.get("confidence"),
                "contact_method": r.get("contact_method"),
                "status": r.get("alert_status") or "unknown",
                "error": _sanitize_detail(r.get("notification_error"), 160)
                         if r.get("notification_error") else None,
                "has_msg_button": bool(r.get("button_message_url")),
                "has_user_button": bool(r.get("button_user_url")),
            })
        return {"items": items, "page": page, "has_more": has_more,
                "total": total, "limit": _ALERT_LOG_LIMIT}

    async def alerts_stats(self) -> Dict[str, Any]:
        cached = DASH.alerts_cache.get_if_fresh()
        if cached is not None:
            return cached
        if not DASH.alerts_cache.try_begin_refresh():
            stale = DASH.alerts_cache.get_stale()
            if stale is not None:
                return stale
        try:
            data = await self._alerts_stats_uncached()
            DASH.alerts_cache.end_refresh(data)
            return data
        except DashboardHTTPError:
            DASH.alerts_cache.abort_refresh()
            raise
        except Exception as e:
            DASH.alerts_cache.abort_refresh()
            raise DashboardHTTPError(500, {"error": f"alerts_stats_failed:{type(e).__name__}"})

    async def _alerts_stats_uncached(self) -> Dict[str, Any]:
        now = time.time()
        day_start = now - (now % 86400.0)
        out: Dict[str, Any] = {"by_status": {}, "buttons": {}, "contact_methods": {},
                               "total": 0, "today": 0, "target_group": None}
        try:
            r = await self.db._fetchone("SELECT COUNT(*) AS c FROM alerts")
            out["total"] = int(r["c"]) if r and r.get("c") else 0
            r = await self.db._fetchone(
                "SELECT COUNT(*) AS c FROM alerts WHERE timestamp >= ?", (day_start,)
            )
            out["today"] = int(r["c"]) if r and r.get("c") else 0
        except Exception:
            pass
        try:
            rows = await self.db._fetchall(
                "SELECT COALESCE(alert_status,'unknown') AS s, COUNT(*) AS c FROM alerts GROUP BY s"
            )
            out["by_status"] = {str(r["s"]): int(r["c"]) for r in rows}
        except Exception:
            pass
        try:
            r = await self.db._fetchone(
                "SELECT COUNT(*) AS c, "
                "SUM(CASE WHEN button_message_url IS NOT NULL AND button_message_url != '' "
                "THEN 1 ELSE 0 END) AS mb, "
                "SUM(CASE WHEN button_user_url IS NOT NULL AND button_user_url != '' "
                "THEN 1 ELSE 0 END) AS ub FROM alerts"
            )
            if r:
                out["buttons"] = {"with_message_button": int(r["mb"] or 0),
                                  "with_user_button": int(r["ub"] or 0),
                                  "total": int(r["c"] or 0)}
        except Exception:
            pass
        try:
            rows = await self.db._fetchall(
                "SELECT COALESCE(contact_method,'') AS cm, COUNT(*) AS c FROM alerts "
                "GROUP BY cm ORDER BY c DESC LIMIT 6"
            )
            out["contact_methods"] = {str(r["cm"] or "غير مسجل"): int(r["c"]) for r in rows}
        except Exception:
            pass
        try:
            out["target_group"] = getattr(_cfg(), "TARGET_GROUP_ID", None)
        except Exception:
            pass
        return out

    async def alert_test(self) -> Dict[str, Any]:
        ab = getattr(self.bot, "alert_bot", None)
        if ab is None or not bool(getattr(ab, "enabled", False)):
            raise DashboardHTTPError(503, {"error": "alert_bot_disabled",
                                           "note": "ALERT_BOT_TOKEN غير مضبوط — التنبيهات تعمل حالياً عبر حسابات المستخدمين بدون أزرار"})
        try:
            ok, reason = await ab.send_plain(
                "🔔 رسالة اختبار من لوحة التحكم Telegram BR7 — الأزرار والقوالب تعمل"
            )
        except Exception as e:
            ok, reason = False, f"{type(e).__name__}: {_sanitize_detail(e, 120)}"
        await _audit(self.db, "dashboard.alert_test", "alert_bot", "",
                     new_value=("ok" if ok else f"failed:{reason}"))
        return {"ok": bool(ok), "reason": str(reason or "")}

    async def alert_resend(self, msg_hash: str) -> Dict[str, Any]:
        h = str(msg_hash or "").strip()
        if not h or len(h) > 80:
            raise DashboardHTTPError(400, {"error": "invalid_hash"})
        row = await self.db._fetchone(
            "SELECT alert_text, alert_status FROM alerts WHERE message_hash = ?", (h,)
        )
        if not row:
            raise DashboardHTTPError(404, {"error": "alert_not_found"})
        text = str(row.get("alert_text") or "").strip()
        if not text:
            raise DashboardHTTPError(400, {"error": "no_text_to_resend"})
        ab = getattr(self.bot, "alert_bot", None)
        if ab is None or not bool(getattr(ab, "enabled", False)):
            raise DashboardHTTPError(503, {"error": "alert_bot_disabled"})
        try:
            ok, reason = await ab.send_plain(text)
        except Exception as e:
            ok, reason = False, f"{type(e).__name__}: {_sanitize_detail(e, 120)}"
        if ok:
            try:
                await self.db.mark_alert_dispatched(h, status="sent",
                                                    alerted_to="dashboard-resend")
            except Exception:
                pass
        await _audit(self.db, "dashboard.alert_resend", "alert", h,
                     new_value=("ok" if ok else f"failed:{reason}"))
        return {"ok": bool(ok), "reason": str(reason or "")}

    async def template_preview(self) -> Dict[str, Any]:
        """معاينة قالب التنبيه الموحد — بنفس دوال الإنتاج حرفياً (بلا إرسال)."""
        try:
            from alert_bot import build_alert_buttons, build_alert_html
            from contact_target import resolve_contact_target
        except Exception as e:
            raise DashboardHTTPError(500, {"error": f"builders_missing:{type(e).__name__}"})
        ct = resolve_contact_target(
            sender_id=987654321,
            username="sample_user",
            first_name="مستخدم",
            display="مستخدم تجريبي",
            sender_type="user",
        )
        data = {
            "sender_id": 987654321,
            "sender_username": "sample_user",
            "sender_first_name": "مستخدم",
            "sender_last_name": "تجريبي",
            "chat_id": -1001234567890,
            "message_id": 123,
            "text": "هذه رسالة تجريبية لمعاينة قالب التنبيه الموحد (نص الرسالة يظهر داخل البطاقة)",
        }
        analysis = {"decision": "accept", "confidence": 0.87, "keyword": "طلب"}
        try:
            built = build_alert_html(
                data, analysis,
                msg_link="https://t.me/samplegroup/123",
                group_link="https://t.me/samplegroup",
                chat_username="samplegroup",
                sender_username="sample_user",
                contact_target=ct,
                context=None,
            )
        except Exception as e:
            raise DashboardHTTPError(500, {"error": f"build_failed:{type(e).__name__}"})
        buttons = build_alert_buttons(
            sender_id=987654321,
            sender_username="sample_user",
            sender_name="مستخدم تجريبي",
            msg_link="https://t.me/samplegroup/123",
            group_link="https://t.me/samplegroup",
        )
        return {
            "text": built.get("text", ""),
            "buttons": buttons,
            "contact_method": built.get("contact_method", ""),
        }

    # ═══════════════════════════════════════════════════════════════════
    # الوحدة 6: إدارة النظام
    # ═══════════════════════════════════════════════════════════════════
    async def ops_toggle(self, flag: str, enabled: bool) -> Dict[str, Any]:
        if flag == "filtering":
            DASH.ops.set_filtering(bool(enabled))
            label = "إيقاف الفلترة"
        elif flag == "alerts":
            DASH.ops.set_alerts(bool(enabled))
            label = "إيقاف التنبيهات"
        else:
            raise DashboardHTTPError(400, {"error": "unknown_flag"})
        await self._persist_ops()
        await _audit(self.db, "dashboard.ops_toggle", "ops", flag,
                     new_value="paused" if enabled else "active")
        return {"ok": True, "flag": flag, "paused": bool(enabled), "label": label}

    async def _persist_ops(self) -> None:
        payload = _json.dumps({
            "filtering_paused": DASH.ops.filtering_paused,
            "alerts_paused": DASH.ops.alerts_paused,
        })
        await self.db._execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = CURRENT_TIMESTAMP",
            (OPS_KEY, payload),
        )
        await self.db._commit()

    async def ops_env(self) -> Dict[str, Any]:
        """فحص متغيرات البيئة — أسماء وحالة حصراً، القيم السرية لا تُعاد أبداً."""
        groups: List[Dict[str, Any]] = []
        non_secret_show = {
            "TARGET_GROUP_ID", "ADMIN_CHAT_ID", "PORT", "DB_TYPE", "DB_POOL_MAX",
            "MEMORY_LIMIT_MB", "DASHBOARD_ENABLED", "ALERT_BOT_ENABLED",
            "ACTIVITY_MESSAGE_ENABLED", "CAPTURE_FROM_BOTS", "FAST_CAPTURE_ENABLED",
            "MAX_ALERTS_PER_MINUTE", "MAX_ALERTS_PER_HOUR",
        }

        def row(name: str, category: str, secret: bool = False) -> Dict[str, Any]:
            val = os.getenv(name)
            show = None
            if val is not None and not secret and name in non_secret_show:
                show = str(val)[:64]
            return {"key": name, "category": category, "secret": secret,
                    "status": "set" if val else "unset", "value": show}

        groups.append({"name": "قاعدة البيانات", "items": [
            row("DATABASE_URL", "db", secret=True), row("DB_TYPE", "db"),
            row("DB_POOL_MAX", "db"),
        ]})
        acc_items = [row("MAIN_API_ID", "accounts"), row("MAIN_API_HASH", "accounts", secret=True),
                     row("MAIN_PHONE", "accounts"), row("MAIN_SESSION_STRING", "accounts", secret=True)]
        for n in range(1, 21):
            acc_items.append(row(f"ACCOUNT_{n}_SESSION_STRING", "accounts", secret=True))
        groups.append({"name": "حسابات تيليجرام", "items": acc_items})
        groups.append({"name": "التنبيهات والهدف", "items": [
            row("ALERT_BOT_TOKEN", "alerts", secret=True), row("ALERT_BOT_ENABLED", "alerts"),
            row("TARGET_GROUP_ID", "alerts"), row("ADMIN_CHAT_ID", "alerts"),
            row("MAX_ALERTS_PER_MINUTE", "alerts"),
        ]})
        groups.append({"name": "النشاط والالتقاط", "items": [
            row("ACTIVITY_MESSAGE_ENABLED", "activity"),
            row("CAPTURE_FROM_BOTS", "activity"),
            row("FAST_CAPTURE_ENABLED", "activity"),
            row("SENDER_INTEL_ENABLED", "activity"),
        ]})
        groups.append({"name": "اللوحة", "items": [
            row("DASHBOARD_ENABLED", "dashboard"),
            row("DASHBOARD_USERNAME", "dashboard"),
            row("DASHBOARD_PASSWORD", "dashboard", secret=True),
        ]})
        groups.append({"name": "النظام", "items": [
            row("PORT", "system"), row("MEMORY_LIMIT_MB", "system"),
            row("HEALTH_FULL_TOKEN", "system", secret=True),
            row("RENDER_EXTERNAL_URL", "system"),
        ]})
        set_count = sum(1 for g in groups for it in g["items"] if it["status"] == "set")
        return {"groups": groups, "set_total": set_count,
                "note": "القيم السرية لا تُعرض أبداً — حالة set/unset فقط"}

    async def ops_errors(self) -> Dict[str, Any]:
        cached = DASH.errors_cache.get_if_fresh()
        if cached is not None:
            return cached
        if not DASH.errors_cache.try_begin_refresh():
            stale = DASH.errors_cache.get_stale()
            if stale is not None:
                return stale
        try:
            data = await self._ops_errors_uncached()
            DASH.errors_cache.end_refresh(data)
            return data
        except DashboardHTTPError:
            DASH.errors_cache.abort_refresh()
            raise
        except Exception as e:
            DASH.errors_cache.abort_refresh()
            raise DashboardHTTPError(500, {"error": f"errors_failed:{type(e).__name__}"})

    async def _ops_errors_uncached(self) -> Dict[str, Any]:
        health_rows: List[Dict[str, Any]] = []
        try:
            rows = await self.db._fetchall(
                "SELECT component, status, details, timestamp FROM system_health "
                "ORDER BY timestamp DESC LIMIT 15"
            )
            for r in rows:
                health_rows.append({
                    "component": str(r.get("component", "")),
                    "status": str(r.get("status", "")),
                    "details": _sanitize_detail(r.get("details"), 200),
                    "ts": float(r.get("timestamp") or 0.0),
                })
        except Exception:
            pass
        dead: List[Dict[str, Any]] = []
        try:
            recs = await self.db.get_dead_letters(limit=20, only_unresolved=True)
            for d in recs or []:
                dead.append({
                    "id": getattr(d, "id", None),
                    "error": _sanitize_detail(getattr(d, "error_text", ""), 160),
                    "retries": getattr(d, "retry_count", None),
                })
        except Exception:
            pass
        notifs: List[Dict[str, Any]] = []
        try:
            rows = await self.db.get_notifications(limit=15, offset=0)
            for r in rows or []:
                notifs.append({
                    "title": _sanitize_detail(r.get("title"), 120),
                    "body": _sanitize_detail(r.get("body"), 200),
                    "severity": r.get("severity"),
                    "ts": str(r.get("created_at") or ""),
                })
        except Exception:
            pass
        monitors: List[Dict[str, Any]] = []
        for m in (getattr(self.bot, "monitors", None) or []):
            st = getattr(m, "_stats", {}) or {}
            last_err = st.get("last_error")
            if last_err:
                monitors.append({
                    "name": str((getattr(m, "account", {}) or {}).get("name", "?")),
                    "error": _sanitize_detail(last_err),
                    "errors_count": int(st.get("errors", 0) or 0),
                })
        return {"system_health": health_rows, "dead_letters": dead,
                "notifications": notifs, "monitors": monitors}

    async def ops_audit(self, page: int, q: str = "", action: str = "",
                        since_hours: int = 0) -> Dict[str, Any]:
        try:
            page = max(1, min(int(page), 500))
        except (TypeError, ValueError):
            page = 1
        q = str(q or "").strip()[:64]
        action = str(action or "").strip()[:60]
        rows = await self.db.get_audit_logs(
            limit=_AUDIT_LOG_LIMIT + 1, offset=(page - 1) * _AUDIT_LOG_LIMIT,
            action=action or None, q=q or None,
            since_hours=int(since_hours or 0) or None,
        )
        has_more = len(rows) > _AUDIT_LOG_LIMIT
        rows = rows[:_AUDIT_LOG_LIMIT]
        items = []
        for r in rows:
            items.append({
                "actor": _sanitize_detail(r.get("actor"), 60),
                "action": _sanitize_detail(r.get("action"), 60),
                "object_type": r.get("object_type") or "",
                "object_id": _sanitize_detail(r.get("object_id"), 60),
                "new_value": _sanitize_detail(r.get("new_value"), 120),
                "source": r.get("source") or "",
                "ts": str(r.get("created_at") or ""),
            })
        return {"items": items, "page": page, "has_more": has_more,
                "limit": _AUDIT_LOG_LIMIT}

    async def ops_backup(self) -> Dict[str, Any]:
        """نسخ احتياطي للجداول الحرجة عبر export_critical_data الموجودة والمختبَرة."""
        data = await self.db.export_critical_data()
        await _audit(self.db, "dashboard.backup_download", "backup", "",
                     new_value="ok")
        return {"generated_at": _now_iso(), "data": data}

    async def request_restart(self) -> Dict[str, Any]:
        """إعادة تشغيل آمنة — إيقاف رشيق كامل ثم خروج نظيف (Render يعيد الإقلاع)."""
        fn = getattr(self.bot, "request_safe_restart", None)
        if fn is None or not fn("dashboard"):
            raise DashboardHTTPError(503, {"error": "restart_unavailable"})
        await _audit(self.db, "dashboard.safe_restart", "process", "",
                     new_value="requested")
        return {"ok": True,
                "note": "تم بدء الإيقاف الرشيق — ستعود اللوحة خلال دقيقة تقريباً"}

    # ═══════════════════════════════════════════════════════════════════
    # v15.7.1: عمليات الطابور والذاكرة — تنفيذ حقيقي على نواة البوت عبر
    # الجسر (المصادقة + CSRF + Rate-Limit يغطيان المسارين تلقائياً)
    # ═══════════════════════════════════════════════════════════════════
    async def ops_purge_queue(self) -> Dict[str, Any]:
        """حذف الطابور يدوياً — إسقاط صريح للتراكم المعلق (عملية حساسة)."""
        before = 0
        try:
            before = int(await self.db.queue_size())
        except Exception:
            before = 0
        fn = getattr(self.bot, "purge_all_queues", None)
        if fn is None:
            raise DashboardHTTPError(503, {"error": "purge_unavailable"})
        res = await fn()
        after = 0
        try:
            after = int(await self.db.queue_size())
        except Exception:
            after = 0
        detail = (f"before={before},db={res.get('db_purged', 0)},"
                  f"mem={res.get('memory_drained', 0)},after={after}")
        await _audit(self.db, "dashboard.purge_queue", "queue", "",
                     new_value=detail)
        return {
            "ok": True,
            "before": before,
            "db_purged": int(res.get("db_purged", 0)),
            "memory_drained": int(res.get("memory_drained", 0)),
            "total": int(res.get("total", 0)),
            "after": after,
            "note": ("تم حذف جميع الأحداث المعلقة من الطابور نهائياً — "
                     "الأحداث الجديدة تُعالج طبيعياً"),
        }

    async def ops_memory_cleanup(self) -> Dict[str, Any]:
        """تنظيف الذاكرة يدوياً — مسح الكاشات + gc بلا إغلاق DB (آمن أثناء التشغيل)."""
        fn = getattr(self.bot, "manual_memory_cleanup", None)
        if fn is None:
            raise DashboardHTTPError(503, {"error": "cleanup_unavailable"})
        res = await fn()
        try:
            detail = (f"rss={res.get('rss_before_mb')}->{res.get('rss_after_mb')}MB,"
                      f"gc={res.get('gc_objects', 0)}")
        except Exception:
            detail = "ok"
        await _audit(self.db, "dashboard.memory_cleanup", "process", "",
                     new_value=detail)
        return {
            "ok": True,
            "rss_before_mb": res.get("rss_before_mb"),
            "rss_after_mb": res.get("rss_after_mb"),
            "freed_mb": res.get("freed_mb"),
            "gc_objects": res.get("gc_objects"),
            "manual_cleanups": res.get("manual_cleanups"),
            "note": ("تم مسح الكاشات (الكيانات/الفلترة/قاعدة البيانات) وتشغيل "
                     "جامع النفايات — لم يُقاطع أي تشغيل"),
        }

    async def ops_status(self) -> Dict[str, Any]:
        """حالة مفاتيح التشغيل + حارس الذاكرة (يُستدعى من خيط الصحة مباشرة)."""
        out: Dict[str, Any] = {
            "ops": DASH.ops.snapshot(),
        }
        try:
            from memory_guard import snapshot as _mg_snap
            out["mem_guard"] = _mg_snap() or {}
        except Exception:
            out["mem_guard"] = {}
        return out

    # ═══════════════════════════════════════════════════════════════════
    # الوحدة 7: الحظر (blocked_senders / blocked_chats — المستهلكة فعلاً
    # في monitors._validate_event مع كاش TTL، فالتعديل يُطبَّق خلال ≤30ث
    # دون إعادة تشغيل)
    # ═══════════════════════════════════════════════════════════════════
    async def blocked_list(self, kind: str, q: str, page: int) -> Dict[str, Any]:
        try:
            page = max(1, min(int(page), 200))
        except (TypeError, ValueError):
            page = 1
        kind = str(kind or "sender").lower()
        if kind not in ("sender", "chat"):
            kind = "sender"
        q = str(q or "").strip()[:64]
        rows = await self.db.list_blocked(kind, q=q, limit=500)
        offset = (page - 1) * _BLOCKED_LOG_LIMIT
        total = len(rows)
        page_rows = rows[offset:offset + _BLOCKED_LOG_LIMIT]
        items: List[Dict[str, Any]] = []
        for r in page_rows:
            items.append({
                "kind": kind,
                "entity_id": int(r.get("entity_id") or 0),
                "reason": _sanitize_detail(r.get("reason"), 160) or None,
                "blocked_by": _sanitize_detail(r.get("blocked_by"), 60) or "system",
                "blocked_at": str(r.get("blocked_at") or "") or None,
                "username": _sanitize_detail(r.get("username"), 64) or None,
                "name": _sanitize_detail(
                    f"{r.get('first_name') or ''} {r.get('last_name') or ''}".strip(), 80
                ) or None,
                "is_bot": bool(r.get("is_bot")),
            })
        overview = await self.db.blocked_overview()
        capture_bots = None
        try:
            from config import CFG as _c
            capture_bots = bool(getattr(_c, "CAPTURE_FROM_BOTS", True))
        except Exception:
            capture_bots = None
        return {
            "items": items, "kind": kind, "q": q, "page": page,
            "has_more": total > offset + _BLOCKED_LOG_LIMIT,
            "total": total, "limit": _BLOCKED_LOG_LIMIT,
            "overview": overview, "capture_from_bots": capture_bots,
            "note": ("حظر المرسل/البوت يمنع معالجة رسائله فوراً (كاش ≤30ث). "
                     "التقاط البوتات عموماً يُدار من env CAPTURE_FROM_BOTS على Render — "
                     "ولحظر بوت بعينه أضفه هنا."),
        }

    async def blocked_add(self, raw_input: str, kind: str, reason: str) -> Dict[str, Any]:
        raw = str(raw_input or "").strip()[:200]
        kind = str(kind or "").strip().lower()
        if kind not in ("sender", "chat", "bot"):
            raise DashboardHTTPError(400, {"error": "invalid_kind",
                                           "note": "النوع يجب أن يكون sender أو chat أو bot"})
        reason = _sanitize_detail(reason, 160)
        by = f"dashboard:{DASH.username}"
        entity_id, username = _parse_group_input(raw)
        is_bot = False
        resolved_title = ""
        if entity_id is None and not username:
            raise DashboardHTTPError(400, {"error": "invalid_input",
                                           "note": "استخدم رابط t.me أو @username أو معرفاً رقمياً"})
        if entity_id is None:
            # مسار username — يتطلب حل الكيان عبر main_client (نفس مسار group_add)
            client = getattr(self.bot, "main_client", None)
            if client is None:
                raise DashboardHTTPError(503, {"error": "no_main_client"})
            try:
                entity = await asyncio_wait_for(client.get_entity(username), timeout=25)
            except Exception as e:
                raise DashboardHTTPError(400, {"error": f"resolve_failed:{type(e).__name__}",
                                               "note": "تعذر حل الكيان — تأكد من الرمز أو استخدم المعرف الرقمي"})
            entity_id = int(getattr(entity, "id", 0) or 0)
            if not entity_id:
                raise DashboardHTTPError(400, {"error": "resolve_failed"})
            is_bot = bool(getattr(entity, "bot", False))
            resolved_title = str(getattr(entity, "title", "") or getattr(entity, "first_name", "") or username)
            if kind in ("sender", "bot"):
                pass  # معرف المستخدم موجب كما هو
            else:
                # توحيد صيغة المجموعات/القنوات بصيغة Telethon كما تصل الأحداث
                if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
                    if entity_id > 0:
                        entity_id = int(f"-100{entity_id}")
                elif entity_id > 0:
                    entity_id = -entity_id
        if kind == "bot":
            is_bot = True
        if kind in ("sender", "bot") and entity_id <= 0:
            raise DashboardHTTPError(400, {"error": "expected_positive_user_id",
                                           "note": "معرف المرسل/البوت يجب أن يكون موجباً"})
        if kind == "chat":
            if entity_id > 0:
                # معرف رقمي موجب لمجموعة — اقبله كما أدخله المالك (بصيغة أحداثه)
                pass
        final_kind = "sender" if kind in ("sender", "bot") else "chat"
        if final_kind == "sender":
            await self.db.block_sender(int(entity_id), reason=reason, by=by)
        else:
            await self.db.block_chat(int(entity_id), reason=reason, by=by)
        await _audit(self.db, "dashboard.blocked_add", "blocked_" + final_kind,
                     str(entity_id), new_value=reason or f"kind={kind}")
        DASH.overview_cache.invalidate()
        return {"ok": True, "kind": final_kind, "entity_id": int(entity_id),
                "is_bot": is_bot, "title": _sanitize_detail(resolved_title, 80) or None,
                "username": username or None}

    async def blocked_remove(self, kind: str, entity_id: int) -> Dict[str, Any]:
        kind = str(kind or "").strip().lower()
        if kind not in ("sender", "chat"):
            raise DashboardHTTPError(400, {"error": "invalid_kind"})
        try:
            eid = int(entity_id)
        except (TypeError, ValueError):
            raise DashboardHTTPError(400, {"error": "invalid_entity_id"})
        if kind == "sender":
            await self.db.unblock_sender(eid)
        else:
            await self.db.unblock_chat(eid)
        await _audit(self.db, "dashboard.blocked_remove", "blocked_" + kind, str(eid))
        DASH.overview_cache.invalidate()
        return {"ok": True}

    async def blocked_bulk_remove(self, kind: str, ids: List[int]) -> Dict[str, Any]:
        kind = str(kind or "").strip().lower()
        if kind not in ("sender", "chat"):
            raise DashboardHTTPError(400, {"error": "invalid_kind"})
        clean: List[int] = []
        for x in (ids or [])[:200]:
            try:
                clean.append(int(x))
            except (TypeError, ValueError):
                continue
        if not clean:
            raise DashboardHTTPError(400, {"error": "nothing_to_remove"})
        removed = await self.db.unblock_bulk(kind, clean)
        await _audit(self.db, "dashboard.blocked_bulk", "blocked_" + kind,
                     new_value=f"removed={removed}")
        DASH.overview_cache.invalidate()
        return {"ok": True, "removed": int(removed)}

    # ═══════════════════════════════════════════════════════════════════
    # الوحدة 8: الأشخاص (sender_contacts + sender_stats) — قراءة عرضية
    # وإجراءات حظر/سماح تمرّ بالدوال الموجودة نفسها
    # ═══════════════════════════════════════════════════════════════════
    async def people_list(self, q: str, flag: str, page: int) -> Dict[str, Any]:
        try:
            page = max(1, min(int(page), 200))
        except (TypeError, ValueError):
            page = 1
        q = str(q or "").strip()[:64]
        flag = str(flag or "all").lower()
        if flag not in ("all", "blocked", "bots", "allowed"):
            flag = "all"
        offset = (page - 1) * _PEOPLE_LOG_LIMIT
        rows = await self.db.list_people(q=q, flag=flag,
                                         limit=_PEOPLE_LOG_LIMIT + 1, offset=offset)
        has_more = len(rows) > _PEOPLE_LOG_LIMIT
        rows = rows[:_PEOPLE_LOG_LIMIT]
        items: List[Dict[str, Any]] = []
        for r in rows:
            items.append({
                "sender_id": int(r.get("sender_id") or 0),
                "username": _sanitize_detail(r.get("username"), 64) or None,
                "name": _sanitize_detail(
                    f"{r.get('first_name') or ''} {r.get('last_name') or ''}".strip(), 80
                ) or None,
                "is_bot": bool(r.get("is_bot")),
                "is_scam": bool(r.get("is_scam")),
                "total_messages": int(r.get("total_messages") or 0),
                "alerts_sent": int(r.get("alerts_sent") or 0),
                "reputation": float(r.get("reputation_score") or 50.0),
                "is_blocked": bool(r.get("is_blocked")),
                "is_allowed": bool(r.get("is_allowed")),
            })
        overview = await self.db.people_overview()
        return {"items": items, "q": q, "flag": flag, "page": page,
                "has_more": has_more, "limit": _PEOPLE_LOG_LIMIT,
                "overview": overview}

    async def person_action(self, action: str, sender_id: int, note: str = "") -> Dict[str, Any]:
        action = str(action or "").strip().lower()
        if action not in ("block", "unblock", "allow", "disallow"):
            raise DashboardHTTPError(400, {"error": "invalid_action"})
        try:
            sid = int(sender_id)
        except (TypeError, ValueError):
            raise DashboardHTTPError(400, {"error": "invalid_sender_id"})
        if sid <= 0:
            raise DashboardHTTPError(400, {"error": "expected_positive_user_id"})
        note = _sanitize_detail(note, 160)
        by = f"dashboard:{DASH.username}"
        if action == "block":
            await self.db.block_sender(sid, reason=note, by=by)
        elif action == "unblock":
            await self.db.unblock_sender(sid)
        elif action == "allow":
            await self.db.add_allowed_entity("sender", sid, note=note)
        else:
            await self.db.remove_allowed_entity("sender", sid)
        await _audit(self.db, f"dashboard.person_{action}", "person", str(sid),
                     new_value=note)
        DASH.overview_cache.invalidate()
        return {"ok": True, "action": action, "sender_id": sid}

    # ═══════════════════════════════════════════════════════════════════
    # الوحدة 9: القواعد (rules — المستهلكة فعلاً عبر db.apply_rules في
    # monitors._analyze_and_alert مع كاش TTL ≤30ث، مفاتيح rule_engine/allowlist)
    # ═══════════════════════════════════════════════════════════════════
    async def rules_list(self) -> Dict[str, Any]:
        rows = await self.db.list_rules(limit=200)
        items: List[Dict[str, Any]] = []
        for r in rows:
            conds = r.get("conditions") or {}
            items.append({
                "id": int(r.get("id") or 0),
                "name": _sanitize_detail(r.get("name"), 120) or "—",
                "conditions": conds if isinstance(conds, dict) else {},
                "action": str(r.get("action") or "tag"),
                "action_value": _sanitize_detail(r.get("action_value"), 120) or "",
                "priority": int(r.get("priority") or 100),
                "hits": int(r.get("hits") or 0),
                "enabled": bool(r.get("enabled")),
                "created_by": _sanitize_detail(r.get("created_by"), 60) or "",
                "created_at": str(r.get("created_at") or "") or None,
            })
        rule_engine_on = await self.db.is_feature_enabled("rule_engine")
        allowlist_on = await self.db.is_feature_enabled("allowlist")
        return {"items": items, "total": len(items),
                "rule_engine_enabled": bool(rule_engine_on),
                "allowlist_enabled": bool(allowlist_on),
                "condition_keys": list(self.db.RULE_CONDITION_KEYS),
                "actions": list(self.db.RULE_ACTIONS)}

    async def rule_add(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise DashboardHTTPError(400, {"error": "invalid_payload"})
        name = str(payload.get("name") or "").strip()[:120]
        if not name:
            raise DashboardHTTPError(400, {"error": "name_required"})
        conds, err = self.db.validate_rule_conditions(payload.get("conditions"))
        if err:
            raise DashboardHTTPError(400, {"error": f"invalid_conditions:{err}"})
        action = str(payload.get("action") or "tag").lower()
        if action not in self.db.RULE_ACTIONS:
            raise DashboardHTTPError(400, {"error": "invalid_action",
                                           "note": "الإجراء: allow | block | tag"})
        try:
            priority = int(payload.get("priority") or 100)
        except (TypeError, ValueError):
            priority = 100
        created = await self.db.add_rule(
            name, conds, action,
            action_value=str(payload.get("action_value") or "")[:200],
            priority=priority,
            created_by=f"dashboard:{DASH.username}",
        )
        if not created:
            raise DashboardHTTPError(500, {"error": "rule_create_failed"})
        await _audit(self.db, "dashboard.rule_add", "rule", str(created.get("id")),
                     new_value=f"{name} → {action}")
        DASH.overview_cache.invalidate()
        return {"ok": True, "rule": {"id": int(created.get("id") or 0), "name": name}}

    async def rule_edit(self, rule_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            rid = int(rule_id)
        except (TypeError, ValueError):
            raise DashboardHTTPError(400, {"error": "invalid_rule_id"})
        if not isinstance(payload, dict):
            raise DashboardHTTPError(400, {"error": "invalid_payload"})
        updates: Dict[str, Any] = {}
        if "name" in payload:
            name = str(payload.get("name") or "").strip()[:120]
            if not name:
                raise DashboardHTTPError(400, {"error": "name_required"})
            updates["name"] = name
        if "conditions" in payload:
            conds, err = self.db.validate_rule_conditions(payload.get("conditions"))
            if err:
                raise DashboardHTTPError(400, {"error": f"invalid_conditions:{err}"})
            updates["conditions"] = conds
        if "action" in payload:
            action = str(payload.get("action") or "").lower()
            if action not in self.db.RULE_ACTIONS:
                raise DashboardHTTPError(400, {"error": "invalid_action"})
            updates["action"] = action
        if "action_value" in payload:
            updates["action_value"] = str(payload.get("action_value") or "")[:200]
        if "priority" in payload:
            try:
                updates["priority"] = int(payload.get("priority") or 100)
            except (TypeError, ValueError):
                raise DashboardHTTPError(400, {"error": "invalid_priority"})
        if not updates:
            raise DashboardHTTPError(400, {"error": "nothing_to_update"})
        try:
            updated = await self.db.update_rule(rid, updates)
        except ValueError as e:
            raise DashboardHTTPError(400, {"error": f"invalid_update:{e}"})
        if not updated:
            raise DashboardHTTPError(404, {"error": "rule_not_found"})
        await _audit(self.db, "dashboard.rule_edit", "rule", str(rid),
                     new_value=",".join(sorted(updates.keys())))
        DASH.overview_cache.invalidate()
        return {"ok": True, "id": rid}

    async def rule_toggle(self, rule_id: int, enabled: bool) -> Dict[str, Any]:
        try:
            rid = int(rule_id)
        except (TypeError, ValueError):
            raise DashboardHTTPError(400, {"error": "invalid_rule_id"})
        ok = await self.db.set_rule_enabled(rid, bool(enabled))
        if not ok:
            raise DashboardHTTPError(404, {"error": "rule_not_found"})
        await _audit(self.db, "dashboard.rule_toggle", "rule", str(rid),
                     new_value="enabled" if enabled else "disabled")
        DASH.overview_cache.invalidate()
        return {"ok": True, "enabled": bool(enabled)}

    async def rule_delete(self, rule_id: int) -> Dict[str, Any]:
        try:
            rid = int(rule_id)
        except (TypeError, ValueError):
            raise DashboardHTTPError(400, {"error": "invalid_rule_id"})
        ok = await self.db.delete_rule(rid)
        if not ok:
            raise DashboardHTTPError(404, {"error": "rule_not_found"})
        await _audit(self.db, "dashboard.rule_delete", "rule", str(rid))
        DASH.overview_cache.invalidate()
        return {"ok": True}

    async def rules_export(self) -> Dict[str, Any]:
        rows = await self.db.list_rules(limit=200)
        clean: List[Dict[str, Any]] = []
        for r in rows:
            clean.append({
                "name": r.get("name") or "",
                "conditions": r.get("conditions") or {},
                "action": r.get("action") or "tag",
                "action_value": r.get("action_value") or "",
                "priority": int(r.get("priority") or 100),
                "enabled": bool(r.get("enabled")),
            })
        return {"rules": clean, "exported_at": _now_iso(),
                "version": DASH.version}

    async def rules_import(self, json_text: str) -> Dict[str, Any]:
        raw = (json_text or "").strip()
        if not raw or len(raw.encode("utf-8")) > MAX_IMPORT_BYTES:
            raise DashboardHTTPError(400, {"error": "invalid_or_too_large"})
        try:
            data = _json.loads(raw)
        except Exception as e:
            raise DashboardHTTPError(400, {"error": f"invalid_json:{type(e).__name__}"})
        rules_in = data.get("rules") if isinstance(data, dict) else None
        if not isinstance(rules_in, list) or not rules_in:
            raise DashboardHTTPError(400, {"error": "no_rules"})
        if len(rules_in) > 200:
            raise DashboardHTTPError(400, {"error": "too_many_rules"})
        applied, skipped = 0, 0
        for r in rules_in[:200]:
            if not isinstance(r, dict):
                skipped += 1
                continue
            conds, err = self.db.validate_rule_conditions(r.get("conditions"))
            if err:
                skipped += 1
                continue
            action = str(r.get("action") or "tag").lower()
            if action not in self.db.RULE_ACTIONS:
                skipped += 1
                continue
            try:
                ok = await self.db.add_rule(
                    str(r.get("name") or "قاعدة")[:120], conds, action,
                    action_value=str(r.get("action_value") or "")[:200],
                    priority=int(r.get("priority") or 100),
                    created_by=f"dashboard:{DASH.username}",
                )
            except Exception:
                ok = None
            if ok:
                applied += 1
            else:
                skipped += 1
        await _audit(self.db, "dashboard.rules_import", "rules", "",
                     new_value=f"applied={applied} skipped={skipped}")
        DASH.overview_cache.invalidate()
        return {"ok": True, "applied": applied, "skipped": skipped}

    # ═══════════════════════════════════════════════════════════════════
    # الوحدة 10: الميزات (features — نفس الجدول الذي تقرؤه monitors عبر
    # is_feature_enabled بكاش ≤30ث — تفعيل/تعطيل دون إعادة تشغيل)
    # ═══════════════════════════════════════════════════════════════════
    async def features_list(self) -> Dict[str, Any]:
        rows = await self.db.list_features()
        return {"items": rows,
                "note": ("التعطيل/التفعيل يُطبَّق خلال ≤30ث (كاش الميزات) دون إعادة تشغيل — "
                         "كل بند مرتبط فعلياً بمسار المعالجة في monitors.")}

    async def feature_toggle(self, key: str, enabled: bool) -> Dict[str, Any]:
        key = str(key or "").strip()[:60]
        if not key:
            raise DashboardHTTPError(400, {"error": "invalid_key"})
        ok = await self.db.set_feature_enabled(key, bool(enabled),
                                              by=f"dashboard:{DASH.username}")
        if not ok:
            raise DashboardHTTPError(400, {"error": "unknown_feature_key"})
        await _audit(self.db, "dashboard.feature_toggle", "feature", key,
                     new_value="enabled" if enabled else "disabled")
        DASH.overview_cache.invalidate()
        return {"ok": True, "key": key, "enabled": bool(enabled)}

    async def panel_rename(self, row_id: int, display_name: str) -> Dict[str, Any]:
        try:
            rid = int(row_id)
        except (TypeError, ValueError):
            raise DashboardHTTPError(400, {"error": "invalid_row_id"})
        name = str(display_name or "").strip()[:80]
        if not name:
            raise DashboardHTTPError(400, {"error": "name_required"})
        row = await self.db.get_panel_account(rid)
        if not row:
            raise DashboardHTTPError(404, {"error": "panel_account_not_found"})
        ok = await self.db.update_panel_account(rid, display_name=name)
        if not ok:
            raise DashboardHTTPError(500, {"error": "rename_failed"})
        await _audit(self.db, "dashboard.panel_rename", "panel_account", str(rid),
                     new_value=name)
        return {"ok": True}


def _cfg() -> Any:
    try:
        from config import CFG
        return CFG
    except Exception:
        return type("NoCfg", (), {"TARGET_GROUP_ID": 0, "DASHBOARD_STALENESS_MINUTES": 30,
                                  "LANGUAGE_FILTER": True})()


async def asyncio_wait_for(fut: Any, timeout: float) -> Any:
    import asyncio
    return await asyncio.wait_for(fut, timeout=timeout)


# ═════════════════════════════════════════════════════════════════════════
# الترطيب عند الإقلاع — مفاتيح الإيقاف + الكلمات المخصصة
# ═════════════════════════════════════════════════════════════════════════
async def hydrate_dashboard(bot: Any) -> None:
    """يُستدعى مرة واحدة من initialize() بعد db.connect — فشل-آمن دائماً.

    * يقرأ dashboard:ops من app_settings (سطر واحد) — مفاتيح الإيقاف
      تبقى بعد إعادة التشغيل (إيقاف صريح من اللوحة لا يُنسى).
    * يقرأ dashboard:keywords_json إن وُجد — يكتب ملف المخصصة ويعيد
      بناء المحرك من نفس مسار التحميل الأصلي (reload_keywords).
    """
    db = getattr(bot, "db", None)
    if db is None:
        return
    try:
        row = await db._fetchone("SELECT value FROM app_settings WHERE key = ?", (OPS_KEY,))
        val = (row or {}).get("value")
        if val:
            d = _json.loads(val)
            DASH.ops.set_filtering(bool(d.get("filtering_paused", False)))
            DASH.ops.set_alerts(bool(d.get("alerts_paused", False)))
            if DASH.ops.filtering_paused or DASH.ops.alerts_paused:
                from loguru import logger
                logger.warning(
                    f"dashboard ops restored: filtering_paused={DASH.ops.filtering_paused} "
                    f"alerts_paused={DASH.ops.alerts_paused} (من الجلسة السابقة)"
                )
    except Exception:
        pass
    try:
        val = await DashboardAPI(bot)._custom_keywords_row()
        if val:
            data = _json.loads(val)
            if isinstance(data, dict):
                KW_FILE.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
                reload = getattr(getattr(bot, "filter", None), "reload_keywords", None)
                if reload is not None:
                    await reload(str(KW_FILE))
                    # ══ v15.6.0: إعادة بناء البوابة السرعة بعد الاسترداد ══
                    try:
                        from fast_gate import get_fast_gate as _fgg
                        _fg = _fgg()
                        if _fg is not None:
                            _fg.schedule_rebuild()
                    except Exception:
                        pass
                from loguru import logger
                logger.info("dashboard: custom keywords restored from app_settings")
    except Exception as e:
        try:
            from loguru import logger
            logger.warning(f"dashboard keywords hydrate skipped: {type(e).__name__}")
        except Exception:
            pass
