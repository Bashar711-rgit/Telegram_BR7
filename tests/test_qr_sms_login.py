"""v9.37 — الطريقتان المباشرتان لتسجيل الدخول: SMS للشريحة + دخول QR بلا رمز.

الخلفية من الإنتاج (2026-09-27): الرموز أُرسلت «عبر التطبيق» لكنها لم تصل
للمستخدم إطلاقاً (الجلسات النشطة على أجهزة قديمة/شبح تستلمها بدلاً منه).
طريقتان حاسمتان:
1) force_sms — توجيه تيليجرام لإرسال الرمز SMS إلى الشريحة مباشرة.
2) دخول QR — رابط tg://login يفتح من تطبيق الحساب المؤكد دخوله (أو QR
   يُمسح من الإعدادات ← الأجهزة) فيُصرَّح للجلسة فوراً بلا أي رمز، مع
   تجديد تلقائي لرمز QR قصير العمر ومسار 2FA كامل.

التغطية:
- send-code يمرر force_sms إلى LoginManager.start.
- qr-start يعيد url + svg ويعمل لصالح الحسابات المدموجة.
- دورة حياة QR كاملة (وحدة مستقلة بلا شبكة): انتظار ← نجاح (نتيجة +
  تنظيف) / 2FA / انتهاء صلاحية ← تجديد تلقائي.
- qr-wait الناجح يسلك مسار verify-code بالضبط (حفظ Render + DB) دون
  إرجاع Session String في الاستجابة (أمان H-4).
- qr-wait بلا عملية معلقة → 400 برسالة عربية واضحة.
- مساعدات SVG/انتهاء الصلاحية فشل-آمنة.
- صفحة الدخول تعرض زر SMS وزر QR.
"""

import asyncio
import datetime
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


# ─────────────────────── force_sms: الرمز للشريحة مباشرة ───────────────────────

class TestForceSms:
    @pytest.mark.asyncio
    async def test_send_code_passes_force_sms_true(self, client, monkeypatch):
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(60)})
        captured = {}

        async def _fake_start(prefix, api_id, api_hash, phone, force_sms=False):
            captured["force_sms"] = force_sms
            return {"sent": True, "code_type": "SentCodeTypeSms",
                    "delivery": "sms", "delivery_hint": "رسالة نصية SMS"}

        monkeypatch.setattr(dash.login_manager, "start", _fake_start)
        r = await client.post("/api/login/send-code",
                              json={"prefix": "ACCOUNT_1", "force_sms": True},
                              headers={**AUTH, **_ip(60)})
        assert r.status_code == 200
        assert captured["force_sms"] is True
        assert r.json()["delivery"] == "sms"

    @pytest.mark.asyncio
    async def test_send_code_default_is_app_not_sms(self, client, monkeypatch):
        """السلوك الافتراضي بلا force_sms يبقى كما هو (توافق كامل)."""
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(61)})
        captured = {}

        async def _fake_start(prefix, api_id, api_hash, phone, force_sms=False):
            captured["force_sms"] = force_sms
            return {"sent": True, "code_type": "AppCode",
                    "delivery": "app", "delivery_hint": "رسالة داخل تطبيق تيليجرام"}

        monkeypatch.setattr(dash.login_manager, "start", _fake_start)
        r = await client.post("/api/login/send-code",
                              json={"prefix": "ACCOUNT_1"},
                              headers={**AUTH, **_ip(61)})
        assert r.status_code == 200
        assert captured["force_sms"] is False


# ─────────────────────── دورة حياة QR كاملة (وحدة مستقلة) ───────────────────────

class _FakeUser:
    username = None
    first_name = "مستخدم"
    id = 424242


class _FakeQR:
    url = "tg://login?token=TOKEN-ONE"
    fail = None  # None | "password" | "expired"

    def __init__(self):
        self.expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30)
        self.recreated = False

    async def wait(self):
        if _FakeQR.fail == "password":
            raise dash.SessionPasswordNeededError(request=object())
        if _FakeQR.fail == "expired":
            raise asyncio.TimeoutError()
        return _FakeUser()

    async def recreate(self):
        self.recreated = True
        _FakeQR.url = "tg://login?token=TOKEN-TWO"
        _FakeQR.url = self.url = "tg://login?token=TOKEN-TWO"
        self.expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30)


class _FakeSession:
    def save(self):
        return "FAKE-QR-SESSION-STRING"


class _FakeClient:
    def __init__(self, *a, **k):
        self.session = _FakeSession()

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def qr_login(self):
        return _FakeQR()

    async def get_me(self):
        return _FakeUser()

    async def sign_in(self, password=None, phone=None, code=None, phone_code_hash=None):
        if password == "WRONG":
            raise dash.PasswordHashInvalidError(request=object())
        return _FakeUser()


@pytest.fixture()
def fake_telethon(monkeypatch):
    _FakeQR.fail = None
    _FakeQR.url = "tg://login?token=TOKEN-ONE"
    monkeypatch.setattr(dash, "TelegramClient", _FakeClient)
    yield


async def _drain(loop_turns: int = 3):
    for _ in range(loop_turns):
        await asyncio.sleep(0.01)


class TestQRLifecycle:
    @pytest.mark.asyncio
    async def test_start_qr_issues_token_and_waits(self, fake_telethon):
        res = await dash.login_manager.start_qr("ACCOUNT_1", 123, "hash")
        assert res["url"] == "tg://login?token=TOKEN-ONE"
        assert res["expires_in"] >= 0
        entry = dash.login_manager._pending["ACCOUNT_1"]
        assert entry["type"] == "qr"
        assert entry["status"] == "waiting"
        assert entry["waiter"] is not None
        await _drain()
        # التنظيف
        await dash.login_manager._drop_entry(entry)
        dash.login_manager._pending.pop("ACCOUNT_1", None)

    @pytest.mark.asyncio
    async def test_qr_success_authorizes_and_cleans(self, fake_telethon):
        await dash.login_manager.start_qr("ACCOUNT_2", 123, "hash")
        await _drain()
        st = await dash.login_manager.qr_status("ACCOUNT_2")
        assert st["authorized"] is True
        assert st["result"]["session_string"] == "FAKE-QR-SESSION-STRING"
        assert st["result"]["user_id"] == 424242
        # العملية نُظّفت — استطلاع لاحق يفشل برسالة عربية واضحة
        with pytest.raises(Exception) as ei:
            await dash.login_manager.qr_status("ACCOUNT_2")
        assert "لا توجد عملية" in str(ei.value)

    @pytest.mark.asyncio
    async def test_qr_2fa_needs_password_then_verify_password(self, fake_telethon):
        _FakeQR.fail = "password"
        await dash.login_manager.start_qr("ACCOUNT_3", 123, "hash")
        await _drain()
        st = await dash.login_manager.qr_status("ACCOUNT_3")
        assert st == {"need_password": True}
        # مسار verify-password الحالي يعمل على نفس العملية المعلقة
        result = await dash.login_manager.verify_password("ACCOUNT_3", "correct")
        assert result["done"] is True
        assert result["session_string"] == "FAKE-QR-SESSION-STRING"

    @pytest.mark.asyncio
    async def test_qr_expiry_auto_recreates_token(self, fake_telethon):
        _FakeQR.fail = "expired"
        await dash.login_manager.start_qr("ACCOUNT_4", 123, "hash")
        await _drain()
        st = await dash.login_manager.qr_status("ACCOUNT_4")
        assert st["waiting"] is True
        assert st["recreated"] is True
        assert st["url"] == "tg://login?token=TOKEN-TWO"
        entry = dash.login_manager._pending["ACCOUNT_4"]
        assert entry["status"] == "waiting"
        # التجديد بدأ منتظراً جديداً — نجاح لاحق ممكن
        _FakeQR.fail = None
        await _drain()
        st2 = await dash.login_manager.qr_status("ACCOUNT_4")
        assert st2["authorized"] is True
        await dash.login_manager._drop_entry(entry)
        dash.login_manager._pending.pop("ACCOUNT_4", None)

    @pytest.mark.asyncio
    async def test_send_code_drops_pending_qr_operation(self, fake_telethon, monkeypatch):
        """طلب رمز OTP عادي يلغي أي عملية QR معلقة لنفس الحساب (لا تسريب)."""
        await dash.login_manager.start_qr("ACCOUNT_5", 123, "hash")
        assert "ACCOUNT_5" in dash.login_manager._pending
        entry = dash.login_manager._pending["ACCOUNT_5"]

        class _Sent:
            type = types.SimpleNamespace().__class__
            phone_code_hash = "h"

        class _FakeOTPClient(_FakeClient):
            async def send_code_request(self, phone, force_sms=False):
                return types.SimpleNamespace(
                    type=type("SentCodeTypeApp", (), {})(),
                    phone_code_hash="hash-xyz")

        monkeypatch.setattr(dash, "TelegramClient", _FakeOTPClient)
        await dash.login_manager.start("ACCOUNT_5", 123, "hash", "+99900000005")
        assert "ACCOUNT_5" not in dash.login_manager._pending or \
            dash.login_manager._pending["ACCOUNT_5"].get("type") != "qr"
        await _drain(5)  # مهلة لمعالجة إلغاء المنتظر (cancel مجدولة لا فورية)
        assert entry["waiter"].cancelled() or entry["waiter"].done()


# ─────────────────────── نقاط QR عبر API ───────────────────────

class TestQREndpoints:
    @pytest.mark.asyncio
    async def test_qr_start_returns_url_and_svg(self, client, monkeypatch):
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(70)})

        async def _fake_start_qr(prefix, api_id, api_hash):
            return {"url": "tg://login?token=ENDPOINT-TEST", "expires_in": 30}

        monkeypatch.setattr(dash.login_manager, "start_qr", _fake_start_qr)
        r = await client.post("/api/login/qr-start",
                              json={"prefix": "ACCOUNT_1"}, headers={**AUTH, **_ip(70)})
        assert r.status_code == 200
        body = r.json()
        assert body["url"] == "tg://login?token=ENDPOINT-TEST"
        assert body["svg"].startswith("<svg")
        assert "phone_masked" in body

    @pytest.mark.asyncio
    async def test_qr_wait_success_saves_without_echoing_session(self, client, monkeypatch):
        """نجاح QR يسلك مسار verify-code بالضبط — والجلسة لا تظهر في الرد."""
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(71)})
        app.state.bot_ref = None
        monkeypatch.setattr(dash, "render_upsert_env",
                            lambda k, v: asyncio.sleep(0, result={"saved": True}))

        async def _fake_qr_status(prefix):
            return {"authorized": True,
                    "result": {"done": True, "user": "@test", "user_id": 1,
                               "session_string": "TOPSECRET-QR-SESSION"}}

        monkeypatch.setattr(dash.login_manager, "qr_status", _fake_qr_status)
        r = await client.post("/api/login/qr-wait",
                              json={"prefix": "ACCOUNT_1"}, headers={**AUTH, **_ip(71)})
        assert r.status_code == 200
        body = r.json()
        assert body["done"] is True
        assert body["saved_to_render"] is True
        assert "TOPSECRET-QR-SESSION" not in r.text  # أمان H-4
        db = app.state.db
        row = await db.get_dashboard_account("ACCOUNT_1")
        assert row["session_string"] == "TOPSECRET-QR-SESSION"

    @pytest.mark.asyncio
    async def test_qr_wait_need_password(self, client, monkeypatch):
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(72)})

        async def _fake_qr_status(prefix):
            return {"need_password": True}

        monkeypatch.setattr(dash.login_manager, "qr_status", _fake_qr_status)
        r = await client.post("/api/login/qr-wait",
                              json={"prefix": "ACCOUNT_1"}, headers={**AUTH, **_ip(72)})
        assert r.status_code == 200
        assert r.json()["need_password"] is True

    @pytest.mark.asyncio
    async def test_qr_wait_without_pending_is_clear_400(self, client):
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(73)})
        r = await client.post("/api/login/qr-wait",
                              json={"prefix": "ACCOUNT_1"}, headers={**AUTH, **_ip(73)})
        assert r.status_code == 400
        assert "لا توجد عملية" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_qr_start_unknown_account_404(self, client):
        r = await client.post("/api/login/qr-start",
                              json={"prefix": "ACCOUNT_9"}, headers={**AUTH, **_ip(74)})
        assert r.status_code == 404


# ─────────────────────── مساعدات فشل-آمنة ───────────────────────

class TestQrHelpers:
    def test_qr_svg_valid_output(self):
        svg = dash._qr_svg("tg://login?token=svg-test")
        assert svg.startswith("<svg")

    def test_qr_svg_fail_safe_empty(self, monkeypatch):
        monkeypatch.setitem(__import__("sys").modules, "qrcode", None)
        assert dash._qr_svg("tg://login?token=x") == ""

    def test_qr_expires_in_positive(self):
        qr = types.SimpleNamespace(
            expires=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=45))
        val = dash._qr_expires_in(qr)
        assert 0 <= val <= 45

    def test_qr_expires_in_garbage_defaults_30(self):
        assert dash._qr_expires_in(object()) == 30
        assert dash._qr_expires_in(None) == 30


class TestLoginPageUI:
    def test_login_page_has_sms_button(self):
        assert "btnSms" in dash.LOGIN_PAGE_HTML
        assert "force_sms: !!forceSms" in dash.LOGIN_PAGE_HTML
        assert "SMS" in dash.LOGIN_PAGE_HTML

    def test_login_page_has_qr_flow(self):
        assert "btnQr" in dash.LOGIN_PAGE_HTML
        assert "qr-start" in dash.LOGIN_PAGE_HTML
        assert "qr-wait" in dash.LOGIN_PAGE_HTML
        assert "qrLink" in dash.LOGIN_PAGE_HTML
        assert "pollQr" in dash.LOGIN_PAGE_HTML
