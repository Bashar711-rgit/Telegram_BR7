"""v9.34 — إدارة حسابات كاملة من اللوحة: CRUD + Runtime + مزامنة DB.

يغطي متطلبات Master Prompt 5 (زر إضافة حساب يعمل + إعدادات حسابات وجلسات
فعلية + قاعدة البيانات مصدر الحقيقة + مزامنة Runtime):
- GET /api/accounts/{prefix}: تفاصيل كاملة + 404 للمجهول.
- PUT /api/accounts/{prefix}: حفظ فعلي في DB (قراءة عكسية تثبت) + منع
  تكرار الهاتف + رفض جسم فارغ.
- DELETE /api/accounts/{prefix}: حذف فعلي من DB (404 بعده) + حماية MAIN.
- POST toggle: enabled يُحفظ في DB وينعكس في القائمة.
- POST connect/reconnect: أرقام أخطاء صادقة بلا bot (503) وبلا جلسة (400).
- POST disconnect: idempotent (نجاح حتى بلا مراقب).
- POST test: نتيجة صادقة (بلا جلسة = success=False tested=no_session).
- GET /api/accounts: الحقول الجديدة (prefix/enabled/status/has_session/
  session_masked/last_connected_at/last_activity_at) والجلسة لا تُسرب أبداً.
- الصلاحيات: 401 بلا توكن · 403 لعارض على الكتابة.
- DB: هجرة الأعمدة الجديدة + دوال الحالة (mark_connected/update partial).
"""

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
    """lifespan أولاً ثم تنظيف جدول الحسابات على الاتصال الحي نفسه +
    ملف env معزول (نفس نمط test_accounts_db.py)."""
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


async def _add_account(client, name="حساب إدارة", phone="+99911111111") -> str:
    r = await client.post("/api/accounts", json={
        "name": name,
        "api_id": 123456,
        "api_hash": "abcdef0123456789abcdef0123456789",
        "phone": phone,
        "session_name": "acc_mgmt_session",
        "priority": 5,
    }, headers={**AUTH, **_ip(1)})
    assert r.status_code == 200, r.text
    return r.json()["prefix"]


def _viewer_headers() -> dict:
    tok, _ = dash.make_user_token("viewer-mgmt", "viewer")
    return {"Authorization": f"Bearer {tok}", "X-Forwarded-For": "192.0.2.90"}


# ─────────────────────────── القائمة والتفاصيل ───────────────────────────

class TestAccountsListFields:
    @pytest.mark.asyncio
    async def test_list_has_management_fields(self, client):
        pfx = await _add_account(client)
        r = await client.get("/api/accounts", headers={**AUTH, **_ip(2)})
        assert r.status_code == 200
        rows = r.json()["accounts"]
        row = next((a for a in rows if a["prefix"] == pfx), None)
        assert row is not None
        for key in ("prefix", "id", "enabled", "status", "has_session",
                    "session_masked", "last_connected_at", "last_activity_at",
                    "pending_deploy", "is_main", "name", "phone"):
            assert key in row, f"missing field: {key}"
        assert row["has_session"] is False
        assert row["enabled"] is True
        assert row["pending_deploy"] is True
        assert row["status"] == "pending"

    @pytest.mark.asyncio
    async def test_session_string_never_leaks(self, client):
        """بعد تسجيل جلسة (محاكاة مباشرة في DB) لا تُعاد القيمة الكاملة أبداً."""
        pfx = await _add_account(client)
        db = app.state.db
        secret = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789SECRETVALUE"
        await db.set_dashboard_account_session(pfx, secret)
        r = await client.get("/api/accounts", headers={**AUTH, **_ip(2)})
        body = r.text
        assert secret not in body
        rows = r.json()["accounts"]
        row = next(a for a in rows if a["prefix"] == pfx)
        assert row["has_session"] is True
        assert row["session_masked"].startswith("1AbC")
        assert "•" in row["session_masked"]
        assert "SECRETVALUE" not in row["session_masked"]

    @pytest.mark.asyncio
    async def test_detail_200_and_404(self, client):
        pfx = await _add_account(client)
        r = await client.get(f"/api/accounts/{pfx}", headers={**AUTH, **_ip(3)})
        assert r.status_code == 200
        body = r.json()
        assert body["prefix"] == pfx
        assert body["api_id"] == 123456
        assert body["status"] in ("pending", "disconnected", "unknown")
        assert body["monitor_stats"] is None  # لا مراقب حي في الاختبار
        r404 = await client.get("/api/accounts/ACCOUNT_99", headers={**AUTH, **_ip(3)})
        assert r404.status_code == 404
        assert "غير موجود" in r404.json()["detail"]


# ─────────────────────────── التعديل (PUT) ───────────────────────────

class TestUpdateAccount:
    @pytest.mark.asyncio
    async def test_update_persists_to_db(self, client):
        pfx = await _add_account(client)
        r = await client.put(f"/api/accounts/{pfx}", json={
            "name": "اسم معدّل", "priority": 8, "phone": "+99922222222",
        }, headers={**AUTH, **_ip(4)})
        assert r.status_code == 200, r.text
        assert r.json()["success"] is True
        db = app.state.db
        row = await db.get_dashboard_account(pfx)
        assert row is not None
        assert row["name"] == "اسم معدّل"
        assert row["priority"] == 8
        assert row["phone"] == "+99922222222"
        # api_hash لم يُرسل — لم يُمسّ
        assert row["api_hash"] == "abcdef0123456789abcdef0123456789"

    @pytest.mark.asyncio
    async def test_update_rejects_duplicate_phone(self, client):
        await _add_account(client, phone="+99933333333")
        pfx2 = await _add_account(client, name="ثانٍ", phone="+99944444444")
        r = await client.put(f"/api/accounts/{pfx2}", json={"phone": "+99933333333"},
                             headers={**AUTH, **_ip(5)})
        assert r.status_code == 400
        assert "مستخدم بالفعل" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_update_empty_body_400(self, client):
        pfx = await _add_account(client)
        r = await client.put(f"/api/accounts/{pfx}", json={},
                             headers={**AUTH, **_ip(6)})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_update_unknown_404(self, client):
        r = await client.put("/api/accounts/ACCOUNT_77", json={"name": "x"},
                             headers={**AUTH, **_ip(7)})
        assert r.status_code == 404


# ─────────────────────────── الحذف (DELETE) ───────────────────────────

class TestDeleteAccount:
    @pytest.mark.asyncio
    async def test_delete_removes_from_db(self, client):
        pfx = await _add_account(client)
        r = await client.delete(f"/api/accounts/{pfx}", headers={**AUTH, **_ip(8)})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["db_deleted"] is True
        db = app.state.db
        assert await db.get_dashboard_account(pfx) is None
        # بعد الحذف: التفاصيل 404
        r2 = await client.get(f"/api/accounts/{pfx}", headers={**AUTH, **_ip(8)})
        assert r2.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_main_protected(self, client):
        r = await client.delete("/api/accounts/MAIN", headers={**AUTH, **_ip(9)})
        assert r.status_code == 400
        assert "الرئيسي" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_delete_unknown_404(self, client):
        r = await client.delete("/api/accounts/ACCOUNT_55", headers={**AUTH, **_ip(10)})
        assert r.status_code == 404


# ─────────────────────────── التفعيل/التعطيل (toggle) ───────────────────────────

class TestToggleAccount:
    @pytest.mark.asyncio
    async def test_disable_then_enable_persists(self, client):
        pfx = await _add_account(client)
        db = app.state.db
        # تعطيل
        r = await client.post(f"/api/accounts/{pfx}/toggle", json={"enabled": False},
                              headers={**AUTH, **_ip(11)})
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        row = await db.get_dashboard_account(pfx)
        assert row["enabled"] is False
        # القائمة تعكس الحالة
        r2 = await client.get("/api/accounts", headers={**AUTH, **_ip(11)})
        row2 = next(a for a in r2.json()["accounts"] if a["prefix"] == pfx)
        assert row2["enabled"] is False
        assert row2["status"] == "disabled"
        # تفعيل
        r3 = await client.post(f"/api/accounts/{pfx}/toggle", json={"enabled": True},
                               headers={**AUTH, **_ip(11)})
        assert r3.status_code == 200
        row3 = await db.get_dashboard_account(pfx)
        assert row3["enabled"] is True

    @pytest.mark.asyncio
    async def test_toggle_unknown_404(self, client):
        r = await client.post("/api/accounts/ACCOUNT_66/toggle", json={"enabled": True},
                              headers={**AUTH, **_ip(12)})
        assert r.status_code == 404


# ─────────────────────── الاتصال/القطع/الإعادة/الاختبار ───────────────────────

class TestRuntimeEndpoints:
    @pytest.mark.asyncio
    async def test_connect_no_session_400(self, client):
        """حساب بلا جلسة → رسالة صادقة تطلب التسجيل أولاً (وليس فشلاً غامضاً)."""
        pfx = await _add_account(client)
        r = await client.post(f"/api/accounts/{pfx}/connect", headers={**AUTH, **_ip(13)})
        assert r.status_code == 400
        assert "سجّل الدخول" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_connect_without_bot_503(self, client):
        """بلا bot_ref: 503 برسالة واضحة (وضع اللوحة فقط) — لا انهيار."""
        pfx = await _add_account(client)
        db = app.state.db
        await db.set_dashboard_account_session(pfx, "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789")
        r = await client.post(f"/api/accounts/{pfx}/connect", headers={**AUTH, **_ip(14)})
        assert r.status_code == 503
        assert "وضع اللوحة فقط" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self, client):
        pfx = await _add_account(client)
        r = await client.post(f"/api/accounts/{pfx}/disconnect", headers={**AUTH, **_ip(15)})
        assert r.status_code == 200
        assert r.json()["was_connected"] is False
        db = app.state.db
        row = await db.get_dashboard_account(pfx)
        assert row["status"] == "disconnected"

    @pytest.mark.asyncio
    async def test_reconnect_without_bot_503(self, client):
        pfx = await _add_account(client)
        r = await client.post(f"/api/accounts/{pfx}/reconnect", headers={**AUTH, **_ip(16)})
        assert r.status_code == 503

    @pytest.mark.asyncio
    async def test_test_endpoint_no_session_honest(self, client):
        pfx = await _add_account(client)
        r = await client.post(f"/api/accounts/{pfx}/test", headers={**AUTH, **_ip(17)})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False
        assert body["tested"] == "no_session"

    @pytest.mark.asyncio
    async def test_test_unknown_404(self, client):
        r = await client.post("/api/accounts/ACCOUNT_88/test", headers={**AUTH, **_ip(18)})
        assert r.status_code == 404


# ─────────────────────────── الصلاحيات ───────────────────────────

class TestPermissions:
    @pytest.mark.asyncio
    async def test_no_token_401(self, client):
        r = await client.get("/api/accounts")
        assert r.status_code == 401
        r2 = await client.post("/api/accounts/ACCOUNT_1/toggle", json={"enabled": True})
        assert r2.status_code == 401

    @pytest.mark.asyncio
    async def test_viewer_cannot_write(self, client):
        pfx = await _add_account(client)
        vh = _viewer_headers()
        r = await client.put(f"/api/accounts/{pfx}", json={"name": "x"}, headers=vh)
        assert r.status_code == 403
        r2 = await client.delete(f"/api/accounts/{pfx}", headers=vh)
        assert r2.status_code == 403
        r3 = await client.post(f"/api/accounts/{pfx}/toggle", json={"enabled": False}, headers=vh)
        assert r3.status_code == 403

    @pytest.mark.asyncio
    async def test_viewer_can_read(self, client):
        await _add_account(client)
        r = await client.get("/api/accounts", headers=_viewer_headers())
        assert r.status_code == 200


# ─────────────────────────── قاعدة البيانات (طبقة التحت) ───────────────────────────

class TestDatabaseLayer:
    @pytest.mark.asyncio
    async def test_status_columns_exist(self, client):
        db = app.state.db
        rows = await db._fetchall("PRAGMA table_info(dashboard_accounts)", ())
        cols = {r["name"] for r in rows}
        assert {"status", "last_error", "last_connected_at", "last_activity_at"} <= cols

    @pytest.mark.asyncio
    async def test_mark_connected_sets_fields(self, client):
        pfx = await _add_account(client)
        db = app.state.db
        await db.set_dashboard_account_status(pfx, "error", "Some transient error")
        row = await db.get_dashboard_account(pfx)
        assert row["status"] == "error"
        assert row["last_error"] == "Some transient error"
        ok = await db.mark_dashboard_account_connected(pfx)
        assert ok is True
        row2 = await db.get_dashboard_account(pfx)
        assert row2["status"] == "connected"
        assert row2["last_error"] is None
        assert row2["last_connected_at"] is not None

    @pytest.mark.asyncio
    async def test_update_partial_fields(self, client):
        pfx = await _add_account(client)
        db = app.state.db
        ok = await db.update_dashboard_account(pfx, name="عبر DB", priority=3)
        assert ok is True
        row = await db.get_dashboard_account(pfx)
        assert row["name"] == "عبر DB"
        assert row["priority"] == 3
        assert row["phone"] == "+99911111111"  # لم يُمسّ

    @pytest.mark.asyncio
    async def test_delete_missing_returns_false(self, client):
        db = app.state.db
        assert await db.delete_dashboard_account("ACCOUNT_99") is False

    @pytest.mark.asyncio
    async def test_invalid_status_rejected(self, client):
        pfx = await _add_account(client)
        db = app.state.db
        assert await db.set_dashboard_account_status(pfx, "hacked", None) is False
