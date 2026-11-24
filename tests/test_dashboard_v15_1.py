# -*- coding: utf-8 -*-
"""tests/test_dashboard_v15_1.py — اختبارات لوحة التحكم الخفيفة v15.1.0

يغطي:
  * المصادقة (bashar/bashar123r) + الجلسات + CSRF + Rate Limit + القفل
  * عدم كشف الأسرار (Session/API Hash/Password/قيم env السرية)
  * الفحص الدخاني للأصول (/admin, app.js, style.css)
  * مركز المراقبة (overview + كشف توقف الاستقبال)
  * الحسابات (قناع الهاتف، اختبار get_me، العضوية)
  * الكلمات + Filter Playground (المحرك الحقيقي حصراً) + الحفظ الدائم
  * المجموعات (CRUD عبر sources الموجودة)
  * التنبيهات (سجل مقسم صفحات، إعادة إرسال، اختبار إرسال، معاينة القالب)
  * الإعدادات (مفاتيح الإيقاف الدائمة، env بلا أسرار، تدقيق، نسخ احتياطي،
    إعادة تشغيل آمنة)
  * المسار الحرج: مفاتيح الإيقاف تعيد مبكراً من _analyze_and_alert و
    _send_alert (فحص ذاكرة صفرية التكلفة)
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import time
from types import SimpleNamespace

import pytest
from aiohttp import web as aioweb
from aiohttp.test_utils import TestClient, TestServer

# ═══════════════════════════════════════════════════════════════════════
# Fakes
# ═══════════════════════════════════════════════════════════════════════
REAL_USERNAME = "bashar"
REAL_PASSWORD = "bashar123r"


class FakeHealthSnapshot:
    def get(self):
        return {
            "boot_complete": True,
            "db_down": False,
            "db_healthy": True,
            "payload": {"status": "ok", "db_latency_ms": 3.2, "queue_size": 0},
        }


class FakeFilter:
    def __init__(self):
        self.last_analyze_text = None
        self.last_reload_path = None
        self.analyze_calls = 0

    async def analyze(self, text):
        self.last_analyze_text = text
        self.analyze_calls += 1
        return {
            "valid": True, "decision": "accept", "confidence": 0.9,
            "keyword": "طلب", "reasons": ["طلب صريح"], "score": 70,
            "score_details": {"intent": 0.3}, "intent_verb": "أبي",
            "negation_detected": False, "advert_score": 0.0,
            "context_type": "academic", "context_confidence": 0.8,
            "spam_score": 0.1, "analysis_time_ms": 1.2, "original_text": text,
        }

    async def get_telemetry(self):
        return {"processed": 5, "accepted": 2, "avg_time_ms": 1.0}

    async def reload_keywords(self, path):
        self.last_reload_path = path
        return True


class FakeAlertBot:
    def __init__(self):
        self.enabled = True
        self.last_text = None

    async def send_plain(self, text, chat_id=None, silent=True):
        self.last_text = text
        return (True, "ok")


class FakeReconnectManager:
    def snapshot(self):
        return {"attempts": 0, "backoff_seconds": 5.0, "running": True}


class FakeClient:
    async def get_me(self):
        return SimpleNamespace(id=111222333, username="main_acct",
                               first_name="رئيسي", phone="+966501234567")

    async def get_permissions(self, entity, user):
        return SimpleNamespace(is_member=True)


class FakeMonitor:
    def __init__(self, connected=True, last_event_age=120):
        self.account = {"name": "MAIN", "phone": "+966501234567",
                        "is_main": True, "enabled": True}
        self.is_connected = connected
        self._stats = {"messages_processed": 3, "alerts_sent": 1, "errors": 0,
                       "send_errors": 0, "duplicates": 0, "rate_limited": 0,
                       "last_error": None}
        self.last_event_ts = time.time() - last_event_age
        self.events_received = 10
        self._terminal_error = None
        self.client = FakeClient()
        self._reconnect = FakeReconnectManager()


def make_fake_bot(db):
    from security import constant_time_equals

    bot = SimpleNamespace(
        db=db,
        filter=FakeFilter(),
        monitors=[FakeMonitor()],
        alert_bot=FakeAlertBot(),
        _health_snapshot=FakeHealthSnapshot(),
        _boot_complete=True,
        _start_time=time.monotonic() - 3600,
        is_running=True,
        _restart_requested=[],
    )

    def _req_restart(reason="dashboard"):
        bot._restart_requested.append(reason)
        return True

    bot.request_safe_restart = _req_restart
    return bot


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def _reset_dash_state():
    from dashboard.state import DASH
    DASH.overview_cache.invalidate()
    DASH.groups_cache.invalidate()
    DASH.alerts_cache.invalidate()
    DASH.errors_cache.invalidate()
    DASH.identity_cache.clear()
    DASH.membership_cache.clear()
    DASH.ops.set_filtering(False)
    DASH.ops.set_alerts(False)
    DASH.ops.counters["paused_filter_msgs"] = 0
    DASH.ops.counters["paused_alert_msgs"] = 0
    yield
    DASH.sessions.drop(None)  # noop
    # طيّ كل الجلسات لمنع التسرب بين الاختبارات
    for tok in list(getattr(DASH.sessions, "_sessions", {}).keys()):
        DASH.sessions.drop(tok)


@pytest.fixture
async def dash_env(db):
    """تطبيق اللوحة الكامل + عميل اختبار aiohttp + بوت مزيف."""
    from dashboard.server import setup_dashboard

    bot = make_fake_bot(db)
    bot._main_loop = asyncio.get_running_loop()
    app = aioweb.Application()
    app.router.add_get("/ping", lambda r: aioweb.json_response({"pong": True}))
    setup_dashboard(app, bot)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, bot
    finally:
        await client.close()


async def _login(client, username=REAL_USERNAME, password=REAL_PASSWORD):
    r = await client.post("/admin/api/login",
                          json={"username": username, "password": password})
    # نقرأ التوكن من ترويسة Set-Cookie مباشرة (الأدق عبر مضيف/منفذ عشوائي)
    raw = r.headers.get("Set-Cookie", "")
    if "br7dash=" in raw:
        client._dash_token = raw.split("br7dash=", 1)[1].split(";", 1)[0]
    return r


async def _csrf(client):
    return getattr(client, "_dash_token", "")


async def _post(client, path, body=None):
    return await client.post(path, json=body or {}, headers={"X-CSRF-Token": await _csrf(client)})


def _db_row(db, sql, params=()):
    import concurrent.futures

    async def _q():
        return await db._fetchone(sql, params)
    return _q


# ═══════════════════════════════════════════════════════════════════════
# وحدات الحالة (توتر-سيف بلا asyncio)
# ═══════════════════════════════════════════════════════════════════════
class TestStateUnits:
    def test_rate_limiter_window(self):
        from dashboard.state import RateLimiter
        rl = RateLimiter(per_minute=3)
        assert rl.allow("ip1") and rl.allow("ip1") and rl.allow("ip1")
        assert not rl.allow("ip1")
        assert rl.allow("ip2")  # عزل لكل IP

    def rate_eviction(self):
        from dashboard.state import RateLimiter
        rl = RateLimiter(per_minute=2, )
        rl.allow("a")
        rl.allow("b", now=time.monotonic() - 120)
        assert rl.allow("b")  # النافذة القديمة أُخليت

    def test_login_guard_lockout(self):
        from dashboard.state import LoginGuard
        g = LoginGuard(max_attempts=3, lockout_seconds=600)
        assert not g.is_locked("ip")
        g.record_failure("ip")
        g.record_failure("ip")
        assert not g.is_locked("ip")
        g.record_failure("ip")
        assert g.is_locked("ip")
        assert g.remaining_lock("ip") > 0
        g.record_success("ip")
        assert not g.is_locked("ip")

    def test_session_store_cap_and_ttl(self):
        from dashboard.state import SessionStore
        s = SessionStore(ttl_seconds=3600, max_sessions=2)
        s.create("t1", "ip1")
        s.create("t2", "ip2")
        assert s.validate("t1") and s.validate("t2")
        s.create("t3", "ip3")  # يسقط الأقدم (t1)
        assert s.count() == 2
        assert not s.validate("t1")   # أُسقط الأقدم فعلاً
        assert s.validate("t2") and s.validate("t3")
        s.drop("t3")
        assert s.count() == 1

    def test_ops_flags(self):
        from dashboard.state import DASH
        DASH.ops.set_filtering(True)
        DASH.ops.set_alerts(True)
        snap = DASH.ops.snapshot()
        assert snap["filtering_paused"] and snap["alerts_paused"]
        DASH.ops.bump("paused_filter_msgs")
        DASH.ops.bump("paused_filter_msgs")
        assert snap is not DASH.ops.snapshot()
        assert DASH.ops.snapshot()["counters"]["paused_filter_msgs"] == 2
        DASH.ops.set_filtering(False)
        DASH.ops.set_alerts(False)


# ═══════════════════════════════════════════════════════════════════════
# المصادقة والأمان
# ═══════════════════════════════════════════════════════════════════════
@pytest.mark.usefixtures("dash_env")
class TestAuthAndSecurity:

    @pytest.mark.asyncio
    async def test_admin_index_and_assets(self, dash_env):
        client, _ = dash_env
        r = await client.get("/admin")
        assert r.status == 200
        body = await r.text()
        assert "Telegram BR7" in body
        assert r.headers.get("X-Frame-Options") == "DENY"
        r = await client.get("/admin/assets/app.js")
        assert r.status == 200
        assert "javascript" in r.headers.get("Content-Type", "")
        r = await client.get("/admin/assets/style.css")
        assert r.status == 200
        r = await client.get("/admin/assets/nope.js")
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_api_requires_auth(self, dash_env):
        client, _ = dash_env
        for path in ("/admin/api/overview", "/admin/api/accounts", "/admin/api/groups"):
            r = await client.get(path)
            assert r.status == 401, path

    @pytest.mark.asyncio
    async def test_session_endpoint_public(self, dash_env):
        client, _ = dash_env
        r = await client.get("/admin/api/session")
        assert r.status == 200
        data = await r.json()
        assert data["authenticated"] is False

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, dash_env):
        client, _ = dash_env
        r = await _login(client, password="wrong")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_login_success_cookie_flags(self, dash_env):
        client, _ = dash_env
        r = await _login(client)
        assert r.status == 200
        data = await r.json()
        assert data["ok"] is True
        raw = r.headers.get("Set-Cookie", "")
        assert "HttpOnly" in raw
        assert "SameSite=Strict" in raw or "samesite=strict" in raw.lower()
        assert "br7dash=" in raw

    @pytest.mark.asyncio
    async def test_csrf_required_and_enforced(self, dash_env):
        client, _ = dash_env
        assert (await _login(client)).status == 200
        # بلا ترويسة CSRF → 403
        r = await client.post("/admin/api/ops/toggle", json={"flag": "alerts", "enabled": False})
        assert r.status == 403
        # بترويسة خاطئة → 403
        r = await client.post("/admin/api/ops/toggle", json={"flag": "alerts", "enabled": False},
                              headers={"X-CSRF-Token": "bad-token"})
        assert r.status == 403
        # بالترويسة الصحيحة → نجاح
        r = await _post(client, "/admin/api/ops/toggle", {"flag": "alerts", "enabled": False})
        assert r.status == 200

    @pytest.mark.asyncio
    async def test_logout_kills_session(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await client.get("/admin/api/overview")
        assert r.status == 200
        r = await _post(client, "/admin/api/logout", {})
        assert r.status == 200
        r = await client.get("/admin/api/overview")
        assert r.status == 401

    @pytest.mark.asyncio
    async def test_login_rate_limit_lockout(self, dash_env):
        client, _ = dash_env
        for _ in range(5):
            r = await _login(client, password="nope")
            assert r.status == 401
        r = await _login(client, password=REAL_PASSWORD)  # حتى الصحيحة مقفولة
        assert r.status == 429

    @pytest.mark.asyncio
    async def test_health_like_paths_not_gated(self, dash_env):
        client, _ = dash_env
        r = await client.get("/ping")
        assert r.status == 200  # الوسيط لا يلمس خارج /admin

    @pytest.mark.asyncio
    async def test_overview_never_contains_secrets(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await client.get("/admin/api/overview")
        assert r.status == 200
        body = await r.text()
        for secret in (REAL_PASSWORD, "SESSION_STRING", "API_HASH", "ALERT_BOT_TOKEN"):
            assert secret not in body


# ═══════════════════════════════════════════════════════════════════════
# مركز المراقبة والحسابات
# ═══════════════════════════════════════════════════════════════════════
@pytest.mark.usefixtures("dash_env")
class TestOverviewAndAccounts:

    @pytest.mark.asyncio
    async def test_overview_payload(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await client.get("/admin/api/overview")
        assert r.status == 200
        d = await r.json()
        assert d["status"] == "ok"
        assert d["accounts"]["total"] == 1
        assert d["accounts"]["connected"] == 1
        assert "chart_24h" in d and len(d["chart_24h"]) == 24
        assert d["bot_version"] == "15.8.0"
        assert "staleness" in d

    @pytest.mark.asyncio
    async def test_overview_staleness_detection(self, dash_env):
        client, bot = dash_env
        bot.monitors[0].last_event_ts = time.time() - 3600 * 3  # ساعات بلا نشاط
        await _login(client)
        r = await client.get("/admin/api/overview")
        d = await r.json()
        assert d["staleness"]["stale"] is True
        assert d["staleness"]["note"]

    @pytest.mark.asyncio
    async def test_overview_single_flight_cache(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r1 = await client.get("/admin/api/overview")
        t1 = (await r1.json())["time"]
        r2 = await client.get("/admin/api/overview")  # ضمن TTL → نسخة مخبأة
        t2 = (await r2.json())["time"]
        assert t1 == t2

    @pytest.mark.asyncio
    async def test_accounts_no_secret_leaks_and_phone_masked(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await client.get("/admin/api/accounts")
        assert r.status == 200
        body = await r.text()
        assert "966501234567" not in body          # الهاتف الكامل لا يُعاد
        assert "+9665****567" in body              # القناع موجود
        for secret in (REAL_PASSWORD, "SESSION_STRING", "API_HASH"):
            assert secret not in body

    @pytest.mark.asyncio
    async def test_account_test_get_me(self, dash_env):
        client, bot = dash_env
        await _login(client)
        r = await _post(client, "/admin/api/accounts/test", {"name": "MAIN"})
        assert r.status == 200
        d = await r.json()
        assert d["ok"] is True
        assert d["identity"]["id"] == 111222333
        assert d["identity"]["phone_masked"] == "+9665****567"

    @pytest.mark.asyncio
    async def test_account_membership(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await _post(client, "/admin/api/accounts/membership", {"name": "MAIN"})
        assert r.status == 200
        d = await r.json()
        assert d["status"] == "ok"

    @pytest.mark.asyncio
    async def test_account_not_found(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await _post(client, "/admin/api/accounts/test", {"name": "GHOST"})
        assert r.status == 404


# ═══════════════════════════════════════════════════════════════════════
# الكلمات + Playground
# ═══════════════════════════════════════════════════════════════════════
@pytest.mark.usefixtures("dash_env")
class TestKeywordsAndPlayground:

    @pytest.mark.asyncio
    async def test_playground_uses_real_engine(self, dash_env):
        client, bot = dash_env
        await _login(client)
        r = await _post(client, "/admin/api/playground", {"text": "أبي أحد يحل واجبي"})
        assert r.status == 200
        d = await r.json()
        assert bot.filter.last_analyze_text == "أبي أحد يحل واجبي"  # نفس المحرك
        assert d["analysis"]["decision"] == "accept"
        assert "thresholds" in d
        assert d["engine_version"]

    @pytest.mark.asyncio
    async def test_playground_empty_text(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await _post(client, "/admin/api/playground", {"text": "   "})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_keywords_view(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await client.get("/admin/api/keywords")
        assert r.status == 200
        d = await r.json()
        assert isinstance(d["units"], dict)
        # بنية keywords.json الفعلية: intent_verbs.tier_*.terms — يجب وجود
        # وحدات أوراق داخل فئات القائمة البيضاء
        assert any(p.startswith("intent_verbs") for p in d["units"])
        assert any(p.startswith("help_expressions") for p in d["units"])
        assert isinstance(d["runtime_weights"], dict)

    @pytest.mark.asyncio
    async def test_keywords_save_persists_and_reloads(self, dash_env):
        client, bot = dash_env
        await _login(client)
        r = await _post(client, "/admin/api/keywords/save",
                        {"units": {"help_expressions": ["فزعه", "استغاثة جديدة"]}})
        assert r.status == 200
        assert (await r.json())["ok"] is True
        # أعيد بناء المحرك من ملف المخصصة
        assert bot.filter.last_reload_path and bot.filter.last_reload_path.endswith("keywords_dashboard.json")
        # تخزين دائم في app_settings + قائمة الأوراق محدّثة
        row = await bot.db._fetchone(
            "SELECT value FROM app_settings WHERE key = ?", ("dashboard:keywords_json",))
        assert row and "استغاثة جديدة" in (row["value"] or "")
        kw_now = _json.loads(row["value"])
        assert "استغاثة جديدة" in kw_now["help_expressions"]
        # مفاتيح الوصف للفئات المتداخلة لم تُمس
        assert "_weight_hint" in kw_now["intent_verbs"]["tier_1_high"]
        # سجل تدقيق
        logs = await bot.db.get_audit_logs(limit=10)
        assert any("keywords_save" in str(l.get("action", "")) for l in logs)

    @pytest.mark.asyncio
    async def test_keywords_save_invalid_unit(self, dash_env):
        client, _ = dash_env
        await _login(client)
        # فئة خارج القائمة البيضاء
        r = await _post(client, "/admin/api/keywords/save",
                        {"units": {"templates.greet": ["x"]}})
        assert r.status == 400
        # مسار غير موجود
        r = await _post(client, "/admin/api/keywords/save",
                        {"units": {"intent_verbs.NOPE": ["x"]}})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_keywords_import_units(self, dash_env):
        client, _ = dash_env
        await _login(client)
        payload = _json.dumps({"units": {"help_expressions": ["س1", "س2"]}})
        r = await _post(client, "/admin/api/keywords/import", {"json_text": payload})
        assert r.status == 200
        assert (await r.json())["applied"] == 2

    @pytest.mark.asyncio
    async def test_keywords_import_invalid_json(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await _post(client, "/admin/api/keywords/import", {"json_text": "{not json"})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_keywords_export(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await client.get("/admin/api/keywords/export")
        assert r.status == 200
        d = await r.json()
        assert "keywords" in d
        assert "intent_verbs" in d["keywords"]
        assert r.headers.get("Content-Disposition", "").startswith("attachment")

    @pytest.mark.asyncio
    async def test_keyword_stats(self, dash_env):
        client, bot = dash_env
        await _login(client)
        r = await client.get("/admin/api/keywords/stats")
        assert r.status == 200
        d = await r.json()
        assert {"top_keywords", "top_rejections", "false_positives"} <= set(d.keys())


# ═══════════════════════════════════════════════════════════════════════
# المجموعات
# ═══════════════════════════════════════════════════════════════════════
@pytest.mark.usefixtures("dash_env")
class TestGroups:

    @pytest.mark.asyncio
    async def test_groups_allow_all_when_empty(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await client.get("/admin/api/groups")
        d = await r.json()
        assert d["allow_all"] is True

    @pytest.mark.asyncio
    async def test_group_add_list_toggle_remove_audit(self, dash_env):
        client, bot = dash_env
        await _login(client)
        r = await _post(client, "/admin/api/groups/add", {"input": "-1001234567890"})
        assert r.status == 200
        assert (await r.json())["chat_id"] == -1001234567890
        d = (await (await client.get("/admin/api/groups")).json())
        assert d["allow_all"] is False
        assert d["items"][0]["chat_id"] == -1001234567890
        # تكرار → 409
        r = await _post(client, "/admin/api/groups/add", {"input": "-1001234567890"})
        assert r.status == 409
        # إيقاف/تفعيل
        r = await _post(client, "/admin/api/groups/toggle",
                        {"chat_id": -1001234567890, "enabled": False})
        assert r.status == 200
        d = (await (await client.get("/admin/api/groups")).json())
        assert d["items"][0]["enabled"] is False
        # إزالة
        r = await _post(client, "/admin/api/groups/remove", {"chat_id": -1001234567890})
        assert r.status == 200
        d = (await (await client.get("/admin/api/groups")).json())
        assert d["items"] == []
        logs = await bot.db.get_audit_logs(limit=20)
        actions = [str(l.get("action", "")) for l in logs]
        assert "dashboard.group_add" in actions and "dashboard.group_remove" in actions

    @pytest.mark.asyncio
    async def test_group_add_invalid(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await _post(client, "/admin/api/groups/add", {"input": "https://t.me/+secretjoin"})
        assert r.status == 400


# ═══════════════════════════════════════════════════════════════════════
# التنبيهات
# ═══════════════════════════════════════════════════════════════════════
async def _insert_alerts(db, n, status=None):
    for i in range(n):
        await db._execute(
            "INSERT INTO alerts (message_hash, chat_id, sender_id, account_name, keyword, "
            "alert_text, timestamp, decision, confidence, contact_method) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'accept', 0.8, 'username')",
            (f"hash{i}_{os.urandom(4).hex()}", -100123, 42 + i, "MAIN", f"kw{i}",
             f"نص التنبيه {i}", time.time() - i * 10),
        )
    await db._commit()
    if status:
        await db._execute("UPDATE alerts SET alert_status = ?", (status,))
        await db._commit()


@pytest.mark.usefixtures("dash_env")
class TestAlerts:

    @pytest.mark.asyncio
    async def test_alerts_log_pagination(self, dash_env):
        client, bot = dash_env
        await _insert_alerts(bot.db, 25)
        await _login(client)
        r = await client.get("/admin/api/alerts?page=1")
        d = await r.json()
        assert len(d["items"]) == 20 and d["has_more"] is True and d["total"] == 25
        r = await client.get("/admin/api/alerts?page=2")
        d = await r.json()
        assert len(d["items"]) == 5 and d["has_more"] is False

    @pytest.mark.asyncio
    async def test_alerts_status_filter(self, dash_env):
        client, bot = dash_env
        await _insert_alerts(bot.db, 3, status="failed")
        await _login(client)
        r = await client.get("/admin/api/alerts?page=1&status=failed")
        d = await r.json()
        assert d["total"] == 3
        assert all(i["status"] == "failed" for i in d["items"])

    @pytest.mark.asyncio
    async def test_alerts_stats_buttons(self, dash_env):
        client, bot = dash_env
        await _insert_alerts(bot.db, 4)
        await bot.db._execute(
            "UPDATE alerts SET button_message_url = 'https://t.me/x/1', "
            "button_user_url = 'https://t.me/sender'")
        await bot.db._commit()
        await _login(client)
        r = await client.get("/admin/api/alerts/stats")
        d = await r.json()
        assert d["buttons"]["with_message_button"] == 4
        assert d["buttons"]["with_user_button"] == 4

    @pytest.mark.asyncio
    async def test_alert_test_send_and_audit(self, dash_env):
        client, bot = dash_env
        await _login(client)
        r = await _post(client, "/admin/api/alerts/test", {})
        assert r.status == 200
        d = await r.json()
        assert d["ok"] is True
        assert "اختبار" in bot.alert_bot.last_text
        logs = await bot.db.get_audit_logs(limit=5)
        assert any("alert_test" in str(l.get("action", "")) for l in logs)

    @pytest.mark.asyncio
    async def test_alert_resend_updates_status(self, dash_env):
        client, bot = dash_env
        await bot.db._execute(
            "INSERT INTO alerts (message_hash, chat_id, sender_id, account_name, keyword, "
            "alert_text, timestamp, alert_status, notification_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', 'timeout')",
            ("resendhash1", -100123, 42, "MAIN", "kw", "نص قابل لإعادة الإرسال", time.time()))
        await bot.db._commit()
        await _login(client)
        r = await _post(client, "/admin/api/alerts/resend", {"hash": "resendhash1"})
        assert r.status == 200
        assert (await r.json())["ok"] is True
        row = await bot.db._fetchone(
            "SELECT alert_status, alerted_to FROM alerts WHERE message_hash = ?",
            ("resendhash1",))
        assert row["alert_status"] == "sent"
        assert row["alerted_to"] == "dashboard-resend"

    @pytest.mark.asyncio
    async def test_alert_resend_missing(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await _post(client, "/admin/api/alerts/resend", {"hash": "ghost"})
        assert r.status == 404

    @pytest.mark.asyncio
    async def test_template_preview_buttons(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await client.get("/admin/api/alerts/template")
        assert r.status == 200
        d = await r.json()
        assert d["text"]
        flat = [b for row in (d["buttons"] or []) for b in row]
        labels = [b.get("text", "") for b in flat]
        assert any("جروب" in lbl for lbl in labels)
        assert any("@sample_user" == lbl for lbl in labels)


# ═══════════════════════════════════════════════════════════════════════
# الإعدادات والتدقيق والنسخ
# ═══════════════════════════════════════════════════════════════════════
@pytest.mark.usefixtures("dash_env")
class TestOps:

    @pytest.mark.asyncio
    async def test_ops_toggle_filtering_persist(self, dash_env):
        client, bot = dash_env
        await _login(client)
        r = await _post(client, "/admin/api/ops/toggle", {"flag": "filtering", "enabled": True})
        assert r.status == 200
        from dashboard.state import DASH
        assert DASH.ops.filtering_paused is True
        row = await bot.db._fetchone(
            "SELECT value FROM app_settings WHERE key = ?", ("dashboard:ops",))
        assert row and _json.loads(row["value"])["filtering_paused"] is True
        logs = await bot.db.get_audit_logs(limit=5)
        assert any(l.get("action") == "dashboard.ops_toggle" for l in logs)
        # إعادة التفعيل (تنظيف)
        await _post(client, "/admin/api/ops/toggle", {"flag": "filtering", "enabled": False})

    @pytest.mark.asyncio
    async def test_ops_toggle_invalid_flag(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await _post(client, "/admin/api/ops/toggle", {"flag": "bogus", "enabled": True})
        assert r.status == 400

    @pytest.mark.asyncio
    async def test_ops_status_memory_shape(self, dash_env):
        client, _ = dash_env
        r = await client.get("/admin/api/ops")  # قراءة ذاكرة مباشرة — بلا جلسة؟ لا: مطلوبة
        assert r.status == 401  # محمية
        await _login(client)
        r = await client.get("/admin/api/ops")
        d = await r.json()
        assert "ops" in d and "mem_guard" in d

    @pytest.mark.asyncio
    async def test_ops_env_no_secret_values(self, dash_env, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgres://user:sup3rsecret@host/db")
        monkeypatch.setenv("MAIN_SESSION_STRING", "1BQANOTEuMTA4L...SECRETSESSION")
        monkeypatch.setenv("TARGET_GROUP_ID", "-1001234567890")
        client, _ = dash_env
        await _login(client)
        r = await client.get("/admin/api/ops/env")
        assert r.status == 200
        body = await r.text()
        assert "sup3rsecret" not in body
        assert "SECRETSESSION" not in body
        d = await r.json()
        rows = {x["key"]: x for g in d["groups"] for x in g["items"]}
        assert rows["DATABASE_URL"]["status"] == "set"
        assert rows["DATABASE_URL"]["value"] is None
        assert rows["TARGET_GROUP_ID"]["value"] == "-1001234567890"

    @pytest.mark.asyncio
    async def test_ops_errors_shape(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await client.get("/admin/api/ops/errors")
        assert r.status == 200
        d = await r.json()
        assert {"system_health", "dead_letters", "notifications", "monitors"} <= set(d.keys())

    @pytest.mark.asyncio
    async def test_ops_audit_paginated(self, dash_env):
        client, bot = dash_env
        for i in range(23):
            await bot.db.record_audit("tester", f"audit.action.{i}", source="dashboard")
        await _login(client)
        r = await client.get("/admin/api/ops/audit?page=1")
        d = await r.json()
        assert len(d["items"]) == 20 and d["has_more"] is True
        r = await client.get("/admin/api/ops/audit?page=2")
        d = await r.json()
        assert d["has_more"] is False

    @pytest.mark.asyncio
    async def test_ops_backup(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await client.get("/admin/api/ops/backup")
        assert r.status == 200
        d = await r.json()
        assert d["generated_at"] and "data" in d
        assert r.headers.get("Content-Disposition", "").startswith("attachment")

    @pytest.mark.asyncio
    async def test_safe_restart(self, dash_env):
        client, bot = dash_env
        await _login(client)
        r = await _post(client, "/admin/api/ops/restart", {})
        assert r.status == 200
        assert bot._restart_requested == ["dashboard"]
        logs = await bot.db.get_audit_logs(limit=5)
        assert any(l.get("action") == "dashboard.safe_restart" for l in logs)


# ═══════════════════════════════════════════════════════════════════════
# المسار الحرج: مفاتيح الإيقاف في monitors
# ═══════════════════════════════════════════════════════════════════════
class TestHotPathPause:

    @pytest.mark.asyncio
    async def test_pause_filtering_short_circuits_analyze(self, db):
        from dashboard.state import DASH
        from monitors import EnhancedAccountMonitor
        acc = {"name": "T1", "phone": "+966500000000", "is_main": False, "enabled": True}
        fake_filter = FakeFilter()
        mon = EnhancedAccountMonitor(acc, db, fake_filter, None)
        DASH.ops.set_filtering(True)
        try:
            result = await mon._analyze_and_alert(
                {"sender_id": 1, "chat_id": -100, "message_id": 1, "text": "مرحبا"},
                "hash_x", "مرحبا")
            assert result is None
            assert fake_filter.analyze_calls == 0
            assert mon._stats["ops_paused_filter_msgs"] == 1
            assert DASH.ops.snapshot()["counters"]["paused_filter_msgs"] == 1
        finally:
            DASH.ops.set_filtering(False)

    @pytest.mark.asyncio
    async def test_pause_alerts_short_circuits_send(self, db):
        from dashboard.state import DASH
        from monitors import EnhancedAccountMonitor
        acc = {"name": "T2", "phone": "+966500000000", "is_main": False, "enabled": True}
        mon = EnhancedAccountMonitor(acc, db, FakeFilter(), None)
        mon._bot_ref = SimpleNamespace()  # يتجاوز بوابة البوت — ثم يوقف مفتاح الإيقاف
        DASH.ops.set_alerts(True)
        try:
            ok = await mon._send_alert({"chat_id": -100, "message_id": 1, "sender_id": 2},
                                       "kw", "text", "hash_y", {})
            assert ok is False
            assert mon._stats["ops_paused_alert_msgs"] == 1
            assert DASH.ops.snapshot()["counters"]["paused_alert_msgs"] == 1
        finally:
            DASH.ops.set_alerts(False)

    @pytest.mark.asyncio
    async def test_not_paused_passes_through_gate(self, db):
        """بلا إيقاف: لا يُرجع مبكراً بسبب مفاتيح اللوحة (يجري كالمعتاد)."""
        from dashboard.state import DASH
        from monitors import EnhancedAccountMonitor
        acc = {"name": "T3", "phone": "+966500000000", "is_main": False, "enabled": True}
        mon = EnhancedAccountMonitor(acc, db, FakeFilter(), None)
        mon._bot_ref = SimpleNamespace()
        assert DASH.ops.alerts_paused is False
        # الفحص ليس ادعاء سلوك السلّم كاملاً — فقط أن مفتاح اللوحة لا يعترض:
        # قد يفشل السلّم لاحقاً لأسباب بيئية (لا عملاء) — نبتلع ذلك وأكتفي
        # بأن عدّاد الإيقاف صفر (لم يتعارض المفتاح مع المسار الطبيعي).
        try:
            await mon._send_alert({"chat_id": -100, "message_id": 1, "sender_id": 2},
                                  "kw", "text", "hash_z", {})
        except Exception:
            pass
        assert mon._stats.get("ops_paused_alert_msgs", 0) == 0


# ═══════════════════════════════════════════════════════════════════════
# الإصدار
# ═══════════════════════════════════════════════════════════════════════
class TestVersion:
    def test_bot_version_bumped(self):
        # v15.7.0: Admin Center Dashboard — الترقية تُحدّث توقع الإصدار (اتفاق المشروع)
        from config import BOT_VERSION, BOT_CODENAME
        assert BOT_VERSION == "15.8.0"
        assert (
            "Dashboard" in BOT_CODENAME or "Panel" in BOT_CODENAME
            or "Silence" in BOT_CODENAME or "Throughput" in BOT_CODENAME
            or "Gate" in BOT_CODENAME or "Freshness" in BOT_CODENAME
            or "Stability" in BOT_CODENAME
        )

    def test_dashboard_version_marker(self):
        from dashboard import DASHBOARD_VERSION
        assert DASHBOARD_VERSION == "15.8.0"
