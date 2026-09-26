"""v9.35 — «رمز التحقق لا يصل» + التفعيل المباشر بعد الربط.

يغطي السبب الجذري الموثق من سجلات الإنتاج (22:37:59):
- SendCodeUnavailableError يظهر 500 إنجليزي خام عند verify-code — الآن
  400 برسالة عربية واضحة مع البديل الموثوق.
- send-code يكشف وسيلة التوصيل (app/sms/call/flash-call) بدل «تم
  الإرسال» الأعمى الذي أخفى فشل التوصيل الفعلي.
- _connect_after_login كان يتخطى بصمت أي حساب مضاف من اللوحة (ليس في
  بيئة الإقلاع) فلا يتصل ولا يراقب بعد التسجيل — الآن يُبنى من قاعدة
  البيانات ويتصل حياً فوراً.
- لصق Session String جاهز من اللوحة (POST/PUT) — البديل الموثوق حين
  يرفض تيليجرام التوصيل من سيرفرات السحابة.
- GET /api/accounts يكشف وضع «اللوحة فقط» (monitoring_ready=false).
"""

import asyncio
import types

import pytest
from httpx import ASGITransport, AsyncClient

import dashboard as dash
from dashboard import app

TOKEN = "test-dashboard-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _ip(n: int) -> dict:
    return {"X-Forwarded-For": f"203.0.113.{n}"}


@pytest.fixture()
async def client(tmp_path, monkeypatch):
    async with app.router.lifespan_context(app):
        db = app.state.db
        if db is not None:
            await db._execute("DELETE FROM dashboard_accounts")
            await db._commit()
        monkeypatch.setattr(dash, "ACCOUNTS_ENV_PATH", str(tmp_path / "accounts.env"))
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
        if db is not None:
            await db._execute("DELETE FROM dashboard_accounts")
            await db._commit()
        app.state.bot_ref = None


def _acc(name: str = "حساب اختبار", phone: str = "+99900000001") -> dict:
    return {
        "name": name,
        "api_id": 123456,
        "api_hash": "abcdef0123456789abcdef0123456789",
        "phone": phone,
        "session_name": "acc_test_session",
        "priority": 5,
    }


class _FakeBot:
    """بوت مصغّر لتتبع الربط الحي دون Telethon حقيقي."""

    def __init__(self, connected: bool = True):
        self.added: list = []
        self.removed: list = []
        self._connected = connected

    async def add_runtime_monitor(self, acc: dict) -> dict:
        self.added.append(dict(acc))
        return {"ok": self._connected}

    def get_monitor_by_prefix(self, prefix: str):
        return None

    def remove_runtime_monitor(self, prefix: str, disconnect_client: bool = True) -> bool:
        self.removed.append(prefix)
        return True


# ─────────────────────── وسيلة توصيل الرمز ───────────────────────

class TestOtpDeliveryInfo:
    def _sent(self, type_cls_name: str):
        return types.SimpleNamespace(type=type(type_cls_name, (), {})())

    def test_app_delivery(self):
        delivery, hint = dash._otp_delivery_info(self._sent("SentCodeTypeApp"))
        assert delivery == "app"
        assert "تيليجرام" in hint

    def test_sms_delivery(self):
        delivery, hint = dash._otp_delivery_info(self._sent("SentCodeTypeSms"))
        assert delivery == "sms"
        assert "SMS" in hint

    def test_flash_call_delivery_warns(self):
        delivery, hint = dash._otp_delivery_info(self._sent("SentCodeTypeFlashCall"))
        assert delivery == "flash_call"
        assert "مكالمة" in hint

    def test_missed_call_delivery(self):
        delivery, _ = dash._otp_delivery_info(self._sent("SentCodeTypeMissedCall"))
        assert delivery == "missed_call"

    def test_unknown_delivery_fail_safe(self):
        delivery, hint = dash._otp_delivery_info(object())
        assert delivery == "unknown"
        assert hint  # وصف عام بلا استثناء


# ─────────────────────── send-code / verify-code ───────────────────────

class TestSendCodeErrors:
    @pytest.mark.asyncio
    async def test_send_code_returns_delivery_info(self, client, monkeypatch):
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(20)})

        async def _fake_start(prefix, api_id, api_hash, phone):
            return {"sent": True, "code_type": "AppCode",
                    "delivery": "app", "delivery_hint": "رسالة داخل تطبيق تيليجرام"}

        monkeypatch.setattr(dash.login_manager, "start", _fake_start)
        r = await client.post("/api/login/send-code",
                              json={"prefix": "ACCOUNT_1"}, headers={**AUTH, **_ip(20)})
        assert r.status_code == 200
        body = r.json()
        assert body["delivery"] == "app"
        assert "تطبيق" in body["delivery_hint"]

    @pytest.mark.asyncio
    async def test_send_code_unavailable_is_clear_arabic(self, client, monkeypatch):
        """الحالة الموثقة في الإنتاج: تيليجرام يرفض إرسال الرمز —
        رسالة عربية + بديل، لا 500 إنجليزي خام."""
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(21)})

        async def _boom(prefix, api_id, api_hash, phone):
            raise dash.SendCodeUnavailableError(request=object())

        monkeypatch.setattr(dash.login_manager, "start", _boom)
        r = await client.post("/api/login/send-code",
                              json={"prefix": "ACCOUNT_1"}, headers={**AUTH, **_ip(21)})
        assert r.status_code == 400
        assert "SEND_CODE_UNAVAILABLE" in r.json()["detail"]
        assert "Session String" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_verify_code_unavailable_is_clear_arabic(self, client, monkeypatch):
        """السطر الحرفي من سجلات الإنتاج 22:37:59 — كان 500 مرة."""
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(22)})

        async def _boom(prefix, code):
            raise dash.SendCodeUnavailableError(request=object())

        monkeypatch.setattr(dash.login_manager, "verify_code", _boom)
        r = await client.post("/api/login/verify-code",
                              json={"prefix": "ACCOUNT_1", "code": "12345"},
                              headers={**AUTH, **_ip(22)})
        assert r.status_code == 400
        assert "Session String" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_send_code_phone_unoccupied(self, client, monkeypatch):
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(23)})

        async def _boom(prefix, api_id, api_hash, phone):
            raise dash.PhoneNumberUnoccupiedError(request=object())

        monkeypatch.setattr(dash.login_manager, "start", _boom)
        r = await client.post("/api/login/send-code",
                              json={"prefix": "ACCOUNT_1"}, headers={**AUTH, **_ip(23)})
        assert r.status_code == 400
        assert "غير مسجل" in r.json()["detail"]


# ─────────────────────── الربط الحي بعد التسجيل ───────────────────────

class TestPostLoginLiveConnect:
    @pytest.mark.asyncio
    async def test_panel_only_account_connects_from_db(self, client, monkeypatch):
        """الإصلاح الجوهري: حساب في قاعدة البيانات فقط (مضاف من اللوحة،
        غير موجود في بيئة الإقلاع) يتصل حياً بعد التسجيل — سابقاً كان
        يُتخطى بصمت."""
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(30)})
        bot = _FakeBot()
        app.state.bot_ref = bot
        monkeypatch.setattr(dash, "render_upsert_env",
                            lambda k, v: asyncio.sleep(0, result={"saved": True}))
        await dash._connect_after_login("ACCOUNT_1", "1BQANOTEuMTA...secret")
        assert len(bot.added) == 1
        acc = bot.added[0]
        assert acc["prefix"] == "ACCOUNT_1"
        assert acc["session_string"] == "1BQANOTEuMTA...secret"
        assert acc["api_id"] == 123456
        assert acc["phone"] == "+99900000001"
        assert acc["session"]  # اسم جلسة صالح للمراقب
        db = app.state.db
        row = await db.get_dashboard_account("ACCOUNT_1")
        assert row["status"] == "connected"

    @pytest.mark.asyncio
    async def test_disabled_account_not_connected(self, client, monkeypatch):
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(31)})
        db = app.state.db
        await db.set_dashboard_account_enabled("ACCOUNT_1", False)
        bot = _FakeBot()
        app.state.bot_ref = bot
        await dash._connect_after_login("ACCOUNT_1", "1BQANOTEuMTA...secret")
        assert bot.added == []  # احترام قرار المشرف


# ─────────────────────── لصق Session String من اللوحة ───────────────────────

class TestPasteSessionString:
    @pytest.mark.asyncio
    async def test_add_account_with_session_saves_it(self, client, monkeypatch):
        data = _acc()
        data["session_string"] = "1BQANOTEuMTA...pasted-secret"
        r = await client.post("/api/accounts", json=data, headers={**AUTH, **_ip(40)})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert "الجلسة" in body["message"]
        db = app.state.db
        row = await db.get_dashboard_account("ACCOUNT_1")
        assert row["session_string"] == "1BQANOTEuMTA...pasted-secret"

    @pytest.mark.asyncio
    async def test_add_account_session_not_echoed(self, client):
        """الأمان: Session String لا يظهر في استجابة API أبداً."""
        data = _acc()
        data["session_string"] = "TOPSECRET-SESSION-VALUE-123"
        r = await client.post("/api/accounts", json=data, headers={**AUTH, **_ip(41)})
        assert b"TOPSECRET-SESSION-VALUE-123" not in r.content

    @pytest.mark.asyncio
    async def test_put_session_persists_to_db(self, client):
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(42)})
        r = await client.put("/api/accounts/ACCOUNT_1",
                             json={"session_string": "1BQANOTEuMTA...new-session"},
                             headers={**AUTH, **_ip(42)})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert "الجلسة" in body["message"]
        db = app.state.db
        row = await db.get_dashboard_account("ACCOUNT_1")
        assert row["session_string"] == "1BQANOTEuMTA...new-session"

    @pytest.mark.asyncio
    async def test_put_empty_fields_rejected(self, client):
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(43)})
        r = await client.put("/api/accounts/ACCOUNT_1", json={},
                             headers={**AUTH, **_ip(43)})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_put_session_triggers_reconnect(self, client, monkeypatch):
        """حساب حي بجلسة جديدة = إزالة + توصيل بالجلسة الجديدة."""
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(44)})
        bot = _FakeBot()
        app.state.bot_ref = bot
        r = await client.put("/api/accounts/ACCOUNT_1",
                             json={"session_string": "1BQANOTEuMTA...fresh"},
                             headers={**AUTH, **_ip(44)})
        assert r.status_code == 200
        # أعطِ المهمة الخلفية فرصة تشغيل (حلقة حدث الاختبار نشطة)
        for _ in range(10):
            if bot.added:
                break
            await asyncio.sleep(0.01)
        assert bot.removed == ["ACCOUNT_1"]
        assert len(bot.added) == 1
        assert bot.added[0]["session_string"] == "1BQANOTEuMTA...fresh"


# ─────────────────────── مزامنة Render بدمج آمن (لا مسح) ───────────────────────

class _FakeResp:
    def __init__(self, status=200, json_data=None, text=""):
        self.status = status
        self._json = json_data
        self._text = text

    async def json(self):
        return self._json

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeRenderSession:
    calls: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, **k):
        _FakeRenderSession.calls.append(("GET", url))
        return _FakeResp(200, {"env_vars": [
            {"envVar": {"key": "TARGET_GROUP_ID", "value": "-100111"}},
            {"envVar": {"key": "ADMIN_CHAT_ID", "value": "555"}},
            {"envVar": {"key": "ACCOUNT_1_API_ID", "value": "29508560"}},
        ], "cursor": None})

    def put(self, url, json=None, **k):
        _FakeRenderSession.calls.append(("PUT", url, json))
        return _FakeResp(200, json)


class TestRenderEnvMergeSafety:
    @pytest.mark.asyncio
    async def test_bulk_upsert_merges_never_replaces(self, client, monkeypatch):
        """السبب الجذري لاختفاء TARGET_GROUP_ID/ADMIN_CHAT_ID من الإنتاج:
        PUT المجمّع يستبدل كل متغيرات البيئة. الإصلاح: GET ← دمج ← PUT كامل."""
        monkeypatch.setattr(dash.aiohttp, "ClientSession", _FakeRenderSession)
        monkeypatch.setenv("RENDER_API_KEY", "test-key")
        monkeypatch.setenv("RENDER_SERVICE_ID", "srv-test")
        _FakeRenderSession.calls = []
        result = await dash.render_upsert_env_many([("ACCOUNT_1_SESSION_STRING", "NEWSESSION")])
        assert result.get("saved") is True
        puts = [c for c in _FakeRenderSession.calls if c[0] == "PUT"]
        assert puts, "لا يوجد PUT إطلاقاً"
        payload = {p["key"]: p["value"] for p in puts[0][2]}
        # الجديد موجود…
        assert payload["ACCOUNT_1_SESSION_STRING"] == "NEWSESSION"
        # …والقديم لم يُمس (هذا ما كان يُمسح قبل الإصلاح)
        assert payload["TARGET_GROUP_ID"] == "-100111"
        assert payload["ADMIN_CHAT_ID"] == "555"
        assert payload["ACCOUNT_1_API_ID"] == "29508560"

    @pytest.mark.asyncio
    async def test_single_upsert_failure_never_wipes_env(self, client, monkeypatch):
        """إزالة السقوط الخاطئ: فشل لاحقة المفتاح لا يرسل PUT مجمّعاً
        بمفتاح واحد (كان يمحو كل البيئة)."""
        monkeypatch.setattr(dash.aiohttp, "ClientSession", _FakeRenderSession)
        monkeypatch.setenv("RENDER_API_KEY", "test-key")
        monkeypatch.setenv("RENDER_SERVICE_ID", "srv-test")
        _FakeRenderSession.calls = []

        def _deny_per_key(url, json=None, **k):
            if url.rstrip("/").endswith("/env-vars/TOP_KEY"):
                return _FakeResp(405, text="method not allowed")
            return _FakeResp(200, json)

        monkeypatch.setattr(_FakeRenderSession, "put", _deny_per_key)
        result = await dash.render_upsert_env("TOP_KEY", "V")
        assert result.get("saved") is False
        bulk_puts = [c for c in _FakeRenderSession.calls if c[0] == "PUT"
                     and c[1].rstrip("/").endswith("/env-vars")]
        assert bulk_puts == []  # لا استبدال كامل أبداً


class TestMonitoringReady:
    @pytest.mark.asyncio
    async def test_accounts_endpoint_exposes_monitoring_ready(self, client):
        r = await client.get("/api/accounts", headers={**AUTH, **_ip(50)})
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["monitoring_ready"], bool)

    @pytest.mark.asyncio
    async def test_login_accounts_endpoint_exposes_monitoring_ready(self, client):
        r = await client.get("/api/login/accounts", headers={**AUTH, **_ip(51)})
        assert r.status_code == 200
        assert isinstance(r.json()["monitoring_ready"], bool)

    def test_login_page_has_session_paste_card(self):
        """البديل الموثوق متاح مباشرة من صفحة الجلسات."""
        assert "pasteSession" in dash.LOGIN_PAGE_HTML
        assert "sess-string" in dash.LOGIN_PAGE_HTML
        assert "generate_session.py" in dash.LOGIN_PAGE_HTML
        # لافتة وضع اللوحة فقط
        assert "monWarn" in dash.LOGIN_PAGE_HTML
