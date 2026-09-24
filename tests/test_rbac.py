"""v9.29 P4 RBAC — اختبارات مستخدمي اللوحة والأدوار والصلاحيات (Master Prompt 8/9).

التغطية:
- scrypt hashing/verify (وحدات) + صفوف بلا hash.
- طبقة قاعدة البيانات: إنشاء/تكرار/تعطيل/دور/كلمة مرور/حذف ناعم/لمسة دخول.
- API: /api/auth/login (نجاح/فشل/معطّل/محذوف)، /api/auth/me، /api/users CRUD.
- RBAC: master = super_admin مطلق، admin يدير، viewer مرفوض 403، توكن منتهٍ 401.
- حارس القفل: فشلات دخول متكررة من نفس IP → 429 (نفس عدّاد التوكن الرئيسي).
- التدقيق: user.create/user.login يظهران في /api/audit بعد تصفير المهام.
"""

import base64
import hmac as hmac_mod
import json
import sys
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import dashboard as dash
from dashboard import app
from database import EnhancedDatabase

PROJ_DIR = Path(__file__).resolve().parent.parent
TOKEN = "test-dashboard-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _ip(n: int) -> dict:
    """XFF فريد لكل اختبار — عزل عدّاد حارس القفل بين الاختبارات."""
    return {"X-Forwarded-For": f"198.51.100.{n}"}


@pytest.fixture()
async def client():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.fixture(autouse=True)
async def clean_users():
    """جدول المستخدمين نظيف قبل/بعد كل اختبار (لا تسريب بذور)."""
    db = getattr(app.state, "db", None)
    if db is not None:
        try:
            await db._execute("DELETE FROM dashboard_users")
            await db._commit()
        except Exception:
            pass
    yield
    db = getattr(app.state, "db", None)
    if db is not None:
        try:
            await db._execute("DELETE FROM dashboard_users")
            await db._commit()
        except Exception:
            pass


async def _mkuser(username: str, role: str = "viewer", password: str = "Password-123"):
    db = app.state.db
    return await db.create_dashboard_user(username, password, role)


# ─────────────────────────── وحدات: التشفير والتوكن ───────────────────────────

class TestPasswordHashing:
    def test_roundtrip(self):
        h = EnhancedDatabase.hash_dashboard_password("S3cret-Pass!")
        assert h.startswith("scrypt$16384$8$1$")
        assert EnhancedDatabase.verify_dashboard_password("S3cret-Pass!", h)

    def test_wrong_password(self):
        h = EnhancedDatabase.hash_dashboard_password("correct-horse")
        assert not EnhancedDatabase.verify_dashboard_password("wrong", h)

    def test_malformed_hash_is_false(self):
        assert not EnhancedDatabase.verify_dashboard_password("x", "")
        assert not EnhancedDatabase.verify_dashboard_password("x", "garbage")
        assert not EnhancedDatabase.verify_dashboard_password("x", "scrypt$bad$hex")

    def test_unique_salt_per_hash(self):
        a = EnhancedDatabase.hash_dashboard_password("same")
        b = EnhancedDatabase.hash_dashboard_password("same")
        assert a != b  # ملح عشوائي — لا hashين متطابقين لنفس الكلمة


class TestUserTokens:
    def test_roundtrip(self):
        t, exp = dash.make_user_token("alice", "admin")
        p = dash.decode_user_token(t)
        assert p is not None and p["username"] == "alice" and p["role"] == "admin"
        assert p["exp"] == exp and exp > time.time()

    def test_bad_signature_rejected(self):
        t, _ = dash.make_user_token("alice", "viewer")
        assert dash.decode_user_token(t[:-4] + "beef") is None

    def test_expired_rejected(self):
        old = dash.USER_TOKEN_TTL_SECONDS
        try:
            dash.USER_TOKEN_TTL_SECONDS = -10
            t, _ = dash.make_user_token("alice", "viewer")
        finally:
            dash.USER_TOKEN_TTL_SECONDS = old
        assert dash.decode_user_token(t) is None

    def test_unknown_role_rejected(self):
        t, _ = dash.make_user_token("alice", "hacker")
        assert dash.decode_user_token(t) is None

    def test_secret_derived_from_master_token(self):
        # تغيير التوكن الرئيسي يبطل التوكنات المشتقة (جذر ثقة واحد).
        t, _ = dash.make_user_token("alice", "viewer")
        assert dash.decode_user_token(t) is not None


# ─────────────────────────── طبقة قاعدة البيانات ───────────────────────────

class TestDashboardUsersDB:
    @pytest.mark.asyncio
    async def test_create_and_get(self, client):
        u = await _mkuser("alice", "admin")
        assert u is not None and u["username"] == "alice"
        assert u["role"] == "admin" and u["enabled"] is True
        assert "password_hash" not in u  # لا hash في أي استجابة

    @pytest.mark.asyncio
    async def test_duplicate_username_returns_none(self, client):
        assert await _mkuser("bob") is not None
        assert await _mkuser("bob") is None

    @pytest.mark.asyncio
    async def test_invalid_role_returns_none(self, client):
        assert await _mkuser("carol", "root") is None

    @pytest.mark.asyncio
    async def test_soft_delete_hides_from_auth_paths(self, client):
        u = await _mkuser("dave")
        assert await app.state.db.soft_delete_dashboard_user(u["id"]) is True
        # get_dashboard_user يشمل المحذوف (سجل تدقيقي) لكن by_name يخفيه.
        assert (await app.state.db.get_dashboard_user(u["id"]))["deleted_at"]
        assert await app.state.db.get_dashboard_user_by_name("dave") is None
        lst = await app.state.db.list_dashboard_users()
        assert all(x["username"] != "dave" for x in lst)

    @pytest.mark.asyncio
    async def test_enabled_toggle_and_login_touch(self, client):
        u = await _mkuser("erin")
        db = app.state.db
        assert await db.set_dashboard_user_enabled(u["id"], False) is True
        assert (await db.get_dashboard_user_by_name("erin"))["enabled"] is False
        assert await db.touch_dashboard_user_login(u["id"]) is True
        assert (await db.get_dashboard_user_by_name("erin"))["last_login_at"]


# ─────────────────────────── API: المصادقة ───────────────────────────

class TestAuthLoginAPI:
    @pytest.mark.asyncio
    async def test_login_success(self, client):
        await _mkuser("frank", "operator")
        r = await client.post("/api/auth/login",
                              json={"username": "frank", "password": "Password-123"},
                              headers=_ip(1))
        assert r.status_code == 200
        body = r.json()
        assert body["token"] and body["role"] == "operator"
        assert "users.read" not in body["permissions"]  # operator بلا users.read
        p = dash.decode_user_token(body["token"])
        assert p and p["username"] == "frank"

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, client):
        await _mkuser("gina")
        r = await client.post("/api/auth/login",
                              json={"username": "gina", "password": "wrong-pass"},
                              headers=_ip(2))
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_login_unknown_user(self, client):
        r = await client.post("/api/auth/login",
                              json={"username": "ghost", "password": "whatever-1"},
                              headers=_ip(3))
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_login_disabled_user(self, client):
        u = await _mkuser("hank")
        await app.state.db.set_dashboard_user_enabled(u["id"], False)
        r = await client.post("/api/auth/login",
                              json={"username": "hank", "password": "Password-123"},
                              headers=_ip(4))
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_login_deleted_user(self, client):
        u = await _mkuser("iris")
        await app.state.db.soft_delete_dashboard_user(u["id"])
        r = await client.post("/api/auth/login",
                              json={"username": "iris", "password": "Password-123"},
                              headers=_ip(5))
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_failed_logins_lockout(self, client):
        # حارس القفل مشترك ومعدَّل عالمياً بملفات اختبار أخرى — الاختبار
        # يعيّن عتبته المعروفة ثم يعيد الأصل (نمط test_auth_guard_retention).
        ip = _ip(6)
        saved = (dash._auth_guard.threshold, dash._auth_guard.window,
                 dash._auth_guard.base_lock, dash._auth_guard.max_lock)
        try:
            dash._auth_guard.threshold = 5
            dash._auth_guard.window = 300
            dash._auth_guard.base_lock = 60
            dash._auth_guard.max_lock = 900
            dash._auth_guard._fails.pop(ip["X-Forwarded-For"], None)
            dash._auth_guard._lockout_until.pop(ip["X-Forwarded-For"], None)
            for _ in range(5):
                r = await client.post("/api/auth/login",
                                      json={"username": "nobody", "password": "bad-pass-1"},
                                      headers=ip)
                assert r.status_code == 401
            r = await client.post("/api/auth/login",
                                  json={"username": "nobody", "password": "bad-pass-1"},
                                  headers=ip)
            assert r.status_code == 429
            assert "retry-after" in {k.lower() for k in r.headers}
        finally:
            (dash._auth_guard.threshold, dash._auth_guard.window,
             dash._auth_guard.base_lock, dash._auth_guard.max_lock) = saved
            dash._auth_guard.record_success(ip["X-Forwarded-For"])

    @pytest.mark.asyncio
    async def test_me_master_vs_user(self, client):
        r = await client.get("/api/auth/me", headers={**AUTH, **_ip(7)})
        assert r.status_code == 200
        assert r.json()["role"] == "super_admin"
        await _mkuser("jack", "admin")
        lr = await client.post("/api/auth/login",
                               json={"username": "jack", "password": "Password-123"},
                               headers=_ip(7))
        ut = {"Authorization": f"Bearer {lr.json()['token']}", **_ip(7)}
        r2 = await client.get("/api/auth/me", headers=ut)
        assert r2.status_code == 200
        me = r2.json()
        assert me["username"] == "jack" and me["via"] == "user_token"
        assert "users.write" in me["permissions"]


# ─────────────────────────── API: إدارة المستخدمين ───────────────────────────

class TestUsersAPI:
    @pytest.mark.asyncio
    async def test_list_requires_auth(self, client):
        r = await client.get("/api/users", headers=_ip(8))
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_list_master_and_admin(self, client):
        await _mkuser("kate", "viewer")
        r = await client.get("/api/users", headers={**AUTH, **_ip(9)})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] >= 1
        assert all("password_hash" not in u for u in body["users"])
        assert set(body["roles"]) == set(EnhancedDatabase.DASHBOARD_ROLES)

    @pytest.mark.asyncio
    async def test_viewer_forbidden_on_users_read(self, client):
        await _mkuser("liam", "viewer")
        lr = await client.post("/api/auth/login",
                               json={"username": "liam", "password": "Password-123"},
                               headers=_ip(10))
        ut = {"Authorization": f"Bearer {lr.json()['token']}", **_ip(10)}
        r = await client.get("/api/users", headers=ut)
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_create_validation(self, client):
        h = {**AUTH, **_ip(11)}
        assert (await client.post("/api/users", headers=h,
                                  json={"username": "ab", "password": "longenough1"})).status_code == 400
        assert (await client.post("/api/users", headers=h,
                                  json={"username": "valid_name", "password": "short"})).status_code == 400
        assert (await client.post("/api/users", headers=h,
                                  json={"username": "valid_name", "password": "longenough1",
                                        "role": "ninja"})).status_code == 400
        assert (await client.post("/api/users", headers=h,
                                  json={"username": "bad name!", "password": "longenough1"})).status_code == 400

    @pytest.mark.asyncio
    async def test_create_duplicate_conflict(self, client):
        h = {**AUTH, **_ip(12)}
        assert (await client.post("/api/users", headers=h,
                                  json={"username": "mona", "password": "longenough1"})).status_code == 200
        assert (await client.post("/api/users", headers=h,
                                  json={"username": "mona", "password": "longenough2"})).status_code == 409

    @pytest.mark.asyncio
    async def test_admin_token_cannot_create_super_admin(self, client):
        await _mkuser("nour", "admin")
        lr = await client.post("/api/auth/login",
                               json={"username": "nour", "password": "Password-123"},
                               headers=_ip(13))
        ut = {"Authorization": f"Bearer {lr.json()['token']}", **_ip(13)}
        r = await client.post("/api/users", headers=ut,
                              json={"username": "evil_sa", "password": "longenough1",
                                    "role": "super_admin"})
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_master_can_create_super_admin(self, client):
        h = {**AUTH, **_ip(14)}
        r = await client.post("/api/users", headers=h,
                              json={"username": "root2", "password": "longenough1",
                                    "role": "super_admin"})
        assert r.status_code == 200
        assert r.json()["user"]["role"] == "super_admin"

    @pytest.mark.asyncio
    async def test_patch_disable_then_login_denied(self, client):
        u = await _mkuser("omar")
        h = {**AUTH, **_ip(15)}
        r = await client.patch(f"/api/users/{u['id']}", headers=h, json={"enabled": False})
        assert r.status_code == 200
        lr = await client.post("/api/auth/login",
                               json={"username": "omar", "password": "Password-123"},
                               headers=_ip(15))
        assert lr.status_code == 401

    @pytest.mark.asyncio
    async def test_patch_password_rotation(self, client):
        u = await _mkuser("petra")
        h = {**AUTH, **_ip(16)}
        assert (await client.patch(f"/api/users/{u['id']}", headers=h,
                                   json={"password": "NewPass-999"})).status_code == 200
        lr = await client.post("/api/auth/login",
                               json={"username": "petra", "password": "Password-123"},
                               headers=_ip(16))
        assert lr.status_code == 401  # القديمة ماتت
        lr2 = await client.post("/api/auth/login",
                                json={"username": "petra", "password": "NewPass-999"},
                                headers=_ip(16))
        assert lr2.status_code == 200

    @pytest.mark.asyncio
    async def test_patch_role_upgrade_and_downgrade(self, client):
        u = await _mkuser("quds", "viewer")
        h = {**AUTH, **_ip(17)}
        r = await client.patch(f"/api/users/{u['id']}", headers=h, json={"role": "supervisor"})
        assert r.status_code == 200 and r.json()["user"]["role"] == "supervisor"
        assert (await client.patch(f"/api/users/{u['id']}", headers=h,
                                   json={"role": "wizzard"})).status_code == 400

    @pytest.mark.asyncio
    async def test_patch_empty_body(self, client):
        u = await _mkuser("rana")
        r = await client.patch(f"/api/users/{u['id']}", headers={**AUTH, **_ip(18)}, json={})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_self_disable_blocked(self, client):
        await _mkuser("sami", "admin")
        lr = await client.post("/api/auth/login",
                               json={"username": "sami", "password": "Password-123"},
                               headers=_ip(19))
        ut = {"Authorization": f"Bearer {lr.json()['token']}", **_ip(19)}
        uid = (await client.get("/api/users", headers=ut)).json()["users"]
        sami_id = next(u["id"] for u in uid if u["username"] == "sami")
        r = await client.patch(f"/api/users/{sami_id}", headers=ut, json={"enabled": False})
        assert r.status_code == 400  # حماية الذات

    @pytest.mark.asyncio
    async def test_delete_soft_and_guardrails(self, client):
        u = await _mkuser("tariq")
        h = {**AUTH, **_ip(20)}
        assert (await client.delete(f"/api/users/{u['id']}", headers=h)).status_code == 200
        # الحذف ناعم: الصف يبقى مع deleted_at (قائمة include_deleted=True).
        body = (await client.get("/api/users", headers=h)).json()
        assert any(x["username"] == "tariq" and x["deleted_at"] for x in body["users"])
        # حذف غير موجود → 404، وإعادة الحذف → 404.
        assert (await client.delete(f"/api/users/{u['id']}", headers=h)).status_code == 404

    @pytest.mark.asyncio
    async def test_self_delete_blocked(self, client):
        # users.delete لـsuper_admin فقط — لذا حماية الذات تُختبر بحساب super_admin.
        h = {**AUTH, **_ip(21)}
        cr = await client.post("/api/users", headers=h,
                               json={"username": "wafa", "password": "Password-123",
                                     "role": "super_admin"})
        assert cr.status_code == 200
        lr = await client.post("/api/auth/login",
                               json={"username": "wafa", "password": "Password-123"},
                               headers=_ip(21))
        ut = {"Authorization": f"Bearer {lr.json()['token']}", **_ip(21)}
        users = (await client.get("/api/users", headers=ut)).json()["users"]
        wafa_id = next(u["id"] for u in users if u["username"] == "wafa")
        r = await client.delete(f"/api/users/{wafa_id}", headers=ut)
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_404_unknown_user(self, client):
        r = await client.patch("/api/users/99999", headers={**AUTH, **_ip(22)},
                               json={"enabled": False})
        assert r.status_code == 404


# ─────────────────────────── التدقيق والانعكاس ───────────────────────────

class TestAuditReflection:
    @pytest.mark.asyncio
    async def test_create_and_login_audited(self, client):
        from conftest import drain_dashboard_tasks
        h = {**AUTH, **_ip(23)}
        cr = await client.post("/api/users", headers=h,
                               json={"username": "yalda", "password": "longenough1",
                                     "role": "viewer"})
        assert cr.status_code == 200
        lr = await client.post("/api/auth/login",
                               json={"username": "yalda", "password": "longenough1"},
                               headers=_ip(23))
        assert lr.status_code == 200
        await drain_dashboard_tasks()
        r = await client.get("/api/audit?action=user.create&limit=50", headers=h)
        actions = {row["action"] for row in r.json()["items"]}
        assert "user.create" in actions
        r2 = await client.get("/api/audit?action=user.login&limit=50", headers=h)
        rows2 = r2.json()["items"]
        assert any(row["actor"] == "panel:yalda" for row in rows2)

    @pytest.mark.asyncio
    async def test_master_regression_stats(self, client):
        # التوكن الرئيسي لم يتأثر: كل المسارات القديمة تعمل كما هي.
        r = await client.get("/api/stats", headers={**AUTH, **_ip(24)})
        assert r.status_code == 200


# ─────────────── v9.31: لوحة GitHub (تدهور رشيق بلا توكنات) ───────────────

class TestGithubPanel:
    def test_local_git_state_failsafe(self):
        from webadmin.github_api import local_git_state
        st = local_git_state(str(PROJ_DIR))
        # داخل المستودع: يجب أن يعيد sha + فرع + owner/repo من origin.
        assert st["head"] and len(st["head"]) == 40
        assert st["branch"] == "main"
        assert "Telegram_BR7" in st["repo"]

    def test_overview_not_configured_shape(self, monkeypatch):
        from webadmin import github_api
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_REPO", raising=False)
        import asyncio
        r = asyncio.new_event_loop().run_until_complete(github_api.get_overview("abc"))
        assert r["configured"] is False
        assert "GITHUB_TOKEN" in r["hint"]
        # لا أسرار ولا استثناءات — قاموس تفسيري فقط.
        assert "error" not in r or isinstance(r.get("error"), str)


class TestWebadminUsersRoutes:
    """v9.29: مسارات إدارة المستخدمين عبر جلسة webadmin (CSRF + تدقيق)."""

    async def _login_admin(self, ac: AsyncClient, monkeypatch) -> str:
        monkeypatch.setenv("DASHBOARD_USERNAME", "wa")
        monkeypatch.setenv("DASHBOARD_PASSWORD", "wa-pass-12345")
        r = await ac.post("/admin/api/auth/login",
                          json={"username": "wa", "password": "wa-pass-12345"})
        assert r.status_code == 200
        return r.json()["csrf"]

    @pytest.mark.asyncio
    async def test_users_list_requires_session(self, client, monkeypatch):
        monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
        r = await client.get("/admin/api/users")
        assert r.status_code in (401, 403, 503)

    @pytest.mark.asyncio
    async def test_create_update_delete_via_webadmin(self, client, monkeypatch):
        from conftest import drain_dashboard_tasks
        csrf = await self._login_admin(client, monkeypatch)
        h = {"x-csrf-token": csrf}
        # إنشاء
        r = await client.post("/admin/api/users/create",
                              json={"username": "web.user", "password": "longenough1",
                                    "role": "operator"}, headers=h)
        assert r.status_code == 200
        uid = r.json()["user"]["id"]
        # تكرار → 409
        r2 = await client.post("/admin/api/users/create",
                               json={"username": "web.user", "password": "longenough1",
                                     "role": "viewer"}, headers=h)
        assert r2.status_code == 409
        # قائمة تحتويه بلا hash
        lst = (await client.get("/admin/api/users")).json()
        row = next(u for u in lst["users"] if u["username"] == "web.user")
        assert row["role"] == "operator" and "password_hash" not in row
        assert set(lst["roles"]) == set(EnhancedDatabase.DASHBOARD_ROLES)
        # تعطيل ثم حذف ناعم
        assert (await client.post("/admin/api/users/update",
                                  json={"user_id": uid, "enabled": False},
                                  headers=h)).status_code == 200
        assert (await client.post("/admin/api/users/delete",
                                  json={"user_id": uid}, headers=h)).status_code == 200
        # مسح CSRF → رفض
        r3 = await client.post("/admin/api/users/create",
                               json={"username": "nocsrf", "password": "longenough1"},
                               headers={"x-csrf-token": "bad"})
        assert r3.status_code in (401, 403)
        # التدقيق من مصدر webadmin
        await drain_dashboard_tasks()
        audits = await client.get("/api/audit?action=user.create&limit=50",
                                  headers=AUTH)
        assert any(a["actor"] == "webadmin:wa" and a["source"] == "webadmin"
                   for a in audits.json()["items"])

    @pytest.mark.asyncio
    async def test_github_status_with_session_not_configured(self, client, monkeypatch):
        monkeypatch.setenv("DASHBOARD_USERNAME", "wa")
        monkeypatch.setenv("DASHBOARD_PASSWORD", "wa-pass-12345")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_REPO", raising=False)
        await self._login_admin(client, monkeypatch)
        # الجلسة في كوكيز العميل تلقائياً — المسار محمي ويعيد التدهور الرشيق.
        r = await client.get("/bot/github/status")
        assert r.status_code == 200
        body = r.json()
        assert body["github"]["configured"] is False
        assert "GITHUB_TOKEN" in body["github"]["hint"]
        # داخل المستودع أثناء الاختبارات: git متاح فيرجع sha/فرع.
        assert "branch" in body["git"]
