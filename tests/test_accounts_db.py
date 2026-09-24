"""v9.33 — الحسابات مبنية على قاعدة البيانات + إصلاح إدارة الجلسات.

يغطي المسار الذي أبلغ عنه المستخدم (إضافة حساب → إدارة الجلسات):
- POST /api/accounts: يُخزَّن في dashboard_accounts (مصدر الحقيقة) +
  ملف env معزول للاختبار + استجابة صادقة (saved_to_render) — لا نجاح
  وهمياً إن فشل الحفظ.
- GET /api/login/accounts: الحساب المضاف يظهر فوراً في القائمة
  (pending_deploy=True) دون إعادة تشغيل — بجانب حسابات بيئة الإقلاع.
- GET /api/accounts: يعرض الحسابات بلا مراقب بصفوف صفرية صادقة.
- منع التكرار بالهاتف + سقف 20 محسوب على الاتحاد (بيئة + قاعدة بيانات).
- send-code يجد حساباً من قاعدة البيانات (LoginManager يحتاج
  api_id/api_hash/phone فقط لا مراقباً حياً).
- نجاح التسجيل يُحدّث الجلسة في قاعدة البيانات (DB مصدر الحقيقة).
- إصلاح Invalid token: صفحة الجلسات تقرأ dashboard_token — نفس مفتاح
  اللوحة الرئيسية (كانت تقرأ dash_token فتبدأ بلا توكن/بتوكن قديم).
"""

import json
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import dashboard as dash
from dashboard import app

TOKEN = "test-dashboard-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _ip(n: int) -> dict:
    return {"X-Forwarded-For": f"192.0.2.{n}"}


@pytest.fixture()
async def client(tmp_path, monkeypatch):
    """lifespan أولاً ثم تنظيف جدول الحسابات على الاتصال الحي نفسه
    (التنظيف قبل بدء lifespan يصطدم باتصال مغلق فيصمت) + ملف env معزول."""
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


def _acc(name: str = "حساب اختبار", api_id: int = 123456,
         phone: str = "+99900000001") -> dict:
    return {
        "name": name,
        "api_id": api_id,
        "api_hash": "abcdef0123456789abcdef0123456789",
        "phone": phone,
        "session_name": "acc_test_session",
        "priority": 5,
    }


# ─────────────────────────── الإضافة والحفظ الحقيقي ───────────────────────────

class TestAddAccountPersistence:
    @pytest.mark.asyncio
    async def test_add_saves_to_database(self, client):
        r = await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(1)})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["prefix"] == "ACCOUNT_1"
        db = app.state.db
        row = await db.get_dashboard_account("ACCOUNT_1")
        assert row is not None
        assert row["phone"] == "+99900000001"
        assert row["api_id"] == 123456
        assert row["origin"] == "panel"
        assert not row["session_string"]

    @pytest.mark.asyncio
    async def test_add_writes_isolated_env_file(self, client, tmp_path):
        r = await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(2)})
        assert r.status_code == 200
        env_file = tmp_path / "accounts.env"
        content = env_file.read_text(encoding="utf-8")
        assert "ACCOUNT_1_API_ID=123456" in content
        assert "ACCOUNT_1_PHONE=+99900000001" in content

    @pytest.mark.asyncio
    async def test_no_fake_success_when_db_fails(self, client, monkeypatch):
        """فشل قاعدة البيانات = فشل الطلب — لا رسالة نجاح وهمية."""
        async def _boom(*a, **k):
            return None
        monkeypatch.setattr(app.state.db, "upsert_dashboard_account", _boom)
        r = await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(3)})
        assert r.status_code == 500

    @pytest.mark.asyncio
    async def test_duplicate_phone_rejected(self, client):
        assert (await client.post("/api/accounts", json=_acc(),
                                  headers={**AUTH, **_ip(4)})).status_code == 200
        r = await client.post("/api/accounts", json=_acc(name="ثاني"),
                              headers={**AUTH, **_ip(4)})
        assert r.status_code == 400


# ─────────────────────────── القوائم من المصدر الحقيقي ───────────────────────────

class TestAccountsListing:
    @pytest.mark.asyncio
    async def test_login_accounts_shows_added_account_immediately(self, client):
        """إصلاح الشكوى الأساسية: الحساب المضاف يظهر في إدارة الجلسات
        فوراً دون إعادة تشغيل (pending_deploy)."""
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(5)})
        r = await client.get("/api/login/accounts", headers={**AUTH, **_ip(5)})
        assert r.status_code == 200
        accounts = r.json()["accounts"]
        row = next(a for a in accounts if a["prefix"] == "ACCOUNT_1")
        assert row["pending_deploy"] is True
        assert row["has_session_string"] is False
        assert row["phone_masked"].endswith("0001")
        assert "*" in row["phone_masked"]  # الهاتف مقنّع دائماً
        assert row["connected"] is False

    @pytest.mark.asyncio
    async def test_login_accounts_empty_ok(self, client):
        r = await client.get("/api/login/accounts", headers={**AUTH, **_ip(6)})
        assert r.status_code == 200
        assert r.json()["accounts"] == []

    @pytest.mark.asyncio
    async def test_get_accounts_includes_pending(self, client):
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(7)})
        r = await client.get("/api/accounts", headers={**AUTH, **_ip(7)})
        assert r.status_code == 200
        accounts = r.json()["accounts"]
        row = next(a for a in accounts if a["name"] == "حساب اختبار")
        assert row["connected"] is False
        assert row["pending_deploy"] is True
        assert row["messages_processed"] == 0


# ─────────────────────────── السقف والترقيم ───────────────────────────

class TestCapsAndNumbering:
    @pytest.mark.asyncio
    async def test_next_prefix_skips_used(self, client):
        db = app.state.db
        await db.upsert_dashboard_account("ACCOUNT_1", "a1", 111, "h1", "+88800000001")
        r = await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(8)})
        assert r.status_code == 200
        assert r.json()["prefix"] == "ACCOUNT_2"

    @pytest.mark.asyncio
    async def test_cap_20_counts_union(self, client):
        db = app.state.db
        for i in range(1, 21):
            await db.upsert_dashboard_account(
                f"ACCOUNT_{i}", f"a{i}", 100 + i, f"hash{i}", f"+8880000{i:04d}")
        r = await client.post("/api/accounts", json=_acc(),
                              headers={**AUTH, **_ip(9)})
        assert r.status_code == 409


# ─────────────────────────── إرسال الكود من قاعدة البيانات ───────────────────────────

class TestSendCodeFromDb:
    @pytest.mark.asyncio
    async def test_send_code_uses_db_account(self, client, monkeypatch):
        """حساب في قاعدة البيانات فقط (بلا مراقب) يستقبل الكود —
        LoginManager يُستدعى ببيانات الحساب من قاعدة البيانات."""
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(10)})
        calls = {}

        async def _fake_start(prefix, api_id, api_hash, phone):
            calls.update(prefix=prefix, api_id=api_id,
                         api_hash=api_hash, phone=phone)
            return {"sent": True, "code_type": "AppCode"}

        monkeypatch.setattr(dash.login_manager, "start", _fake_start)
        r = await client.post("/api/login/send-code",
                              json={"prefix": "ACCOUNT_1"}, headers={**AUTH, **_ip(10)})
        assert r.status_code == 200
        assert calls["prefix"] == "ACCOUNT_1"
        assert calls["api_id"] == 123456
        assert calls["phone"] == "+99900000001"
        assert r.json()["phone_masked"].endswith("0001")

    @pytest.mark.asyncio
    async def test_send_code_unknown_prefix_404(self, client):
        r = await client.post("/api/login/send-code",
                              json={"prefix": "GHOST_9"}, headers={**AUTH, **_ip(11)})
        assert r.status_code == 404


# ─────────────────────────── نجاح التسجيل → قاعدة البيانات ───────────────────────────

class TestLoginSuccessPersistence:
    @pytest.mark.asyncio
    async def test_success_saves_session_to_db(self, client, monkeypatch):
        await client.post("/api/accounts", json=_acc(), headers={**AUTH, **_ip(12)})

        async def _no_render(key, value):
            return {"saved": False, "reason": "غير مضبوطة في الاختبار"}

        monkeypatch.setattr(dash, "render_upsert_env", _no_render)
        r = await dash._login_success_response(
            "ACCOUNT_1",
            {"session_string": "1BQANOTEuMTA...secret", "user": "tester", "user_id": 77},
        )
        body = json.loads(r.body)
        assert body["saved_to_render"] is False
        db = app.state.db
        row = await db.get_dashboard_account("ACCOUNT_1")
        assert row["session_string"] == "1BQANOTEuMTA...secret"

    @pytest.mark.asyncio
    async def test_session_string_never_in_response(self, client, monkeypatch):
        """H-4: الجلسة لا تُعاد في الاستجابة أبداً حتى عند نجاح الحفظ."""
        async def _yes_render(key, value):
            return {"saved": True}

        monkeypatch.setattr(dash, "render_upsert_env", _yes_render)
        r = await dash._login_success_response(
            "ACCOUNT_1", {"session_string": "TOPSECRET", "user": "u", "user_id": 1})
        assert b"TOPSECRET" not in r.body


# ─────────────────────────── إصلاح مفتاح التوكن ───────────────────────────

class TestTokenKeyFix:
    def test_login_page_reads_shared_key(self):
        """صفحة الجلسات تقرأ/تكتب dashboard_token — نفس مفتاح اللوحة
        الرئيسية (السبب الجذري لـ Invalid token)."""
        assert "localStorage.getItem('dashboard_token')" in dash.LOGIN_PAGE_HTML
        assert "localStorage.setItem('dashboard_token'" in dash.LOGIN_PAGE_HTML
        # المفتاح القديم لم يعد يُكتب (قراءة احتياطية للهجرة فقط)
        assert "localStorage.setItem('dash_token'" not in dash.LOGIN_PAGE_HTML
