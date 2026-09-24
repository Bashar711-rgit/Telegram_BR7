"""v9.32 — توسيع RBAC على كل نقاط BotPanel.

قبل v9.32: فقط /api/users* كانت محمية بـ require_permission، والبقية على
verify_token (الرئيسي فقط — أي توكن مستخدم كان يُرفض 401 حتى لو كان دوره
مخوّلاً). بعد التوسيع: كل نقطة مربوطة بصلاحية دقيقة من مصفوفة
ROLE_PERMISSIONS المعتمدة في v9.29.

المصفوفة المختبرة (نماذج تمييزية من كل دور):
- viewer: قراءات auth/accounts/sources/keywords/rules/messages/
  notifications/audit تعمل 200 — لكن settings وbackup وfeatures-toggle
  وكل الكتابات 403.
- operator: يقرأ الرسائل والمصادر — لكن rules.read غير موجودة لديه 403
  وrestart 403 (لا deployment.execute).
- supervisor: يقرأ settings وbackup — لكن restart 403 (لا
  deployment.execute) ولا accounts.write.
- admin: restart يصل المعالج (503 لأن bot_ref غير مهيأ في الاختبار) —
  أي الصلاحية مرّت والقرار ليس 403.
- master: مطلق كما كان (كل اختبارات v9.18-v9.31 تنجو بدون تعديل).
- /health/full احتفظ بـverify_token: توكن مستخدم → 401 (موثق عمداً).
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
    """XFF فريد لكل طلب — عزل عدّاد حارس القفل."""
    return {"X-Forwarded-For": f"203.0.113.{n}"}


def _user_headers(role: str, username: str, n: int) -> dict:
    tok, _ = dash.make_user_token(username, role)
    return {"Authorization": f"Bearer {tok}", **_ip(n)}


@pytest.fixture()
async def client():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# ─────────────────────────── قراءات (viewer مسموح) ───────────────────────────

@pytest.mark.asyncio
async def test_viewer_reads_allowed(client):
    """viewer: المصفوفة تمنحه قراءات النظرة العامة والمصادر والكلمات
    والقواعد والإشعارات والتدقيق (auth.read ضمنياً عبر stats)."""
    h = _user_headers("viewer", "v.read", 1)
    for path in ("/api/stats", "/api/keywords", "/api/sources",
                 "/api/rules", "/api/notifications", "/api/audit",
                 "/api/accounts", "/api/messages", "/api/analytics",
                 "/api/allowed", "/api/blocked/senders",
                 "/api/login/accounts"):
        r = await client.get(path, headers=h)
        assert r.status_code == 200, f"{path} → {r.status_code}"


@pytest.mark.asyncio
async def test_viewer_denied_settings_and_backup(client):
    """viewer: settings.read/backup.read ليست في دوره → 403."""
    h = _user_headers("viewer", "v.deny", 2)
    assert (await client.get("/api/settings", headers=h)).status_code == 403
    assert (await client.get("/api/features", headers=h)).status_code == 403
    assert (await client.get("/api/backup/export", headers=h)).status_code == 403


@pytest.mark.asyncio
async def test_operator_lacks_rules_read(client):
    """operator: مصفوفة v9.29 لا تمنحه rules.read → 403 رغم أنه يقرأ
    المصادر والكلمات."""
    h = _user_headers("operator", "op.rules", 3)
    assert (await client.get("/api/rules", headers=h)).status_code == 403
    assert (await client.get("/api/sources", headers=h)).status_code == 200
    assert (await client.get("/api/keywords", headers=h)).status_code == 200


@pytest.mark.asyncio
async def test_supervisor_reads_settings_and_backup(client):
    """supervisor: settings.read + backup.read موجودة — لكن ليس أكثر."""
    h = _user_headers("supervisor", "sup.rb", 4)
    assert (await client.get("/api/settings", headers=h)).status_code == 200
    assert (await client.get("/api/backup/export", headers=h)).status_code == 200
    assert (await client.get("/api/audit/export", headers=h)).status_code == 200


# ─────────────────────────── كتابات (viewer مرفوض) ───────────────────────────

@pytest.mark.asyncio
async def test_viewer_denied_all_writes(client):
    """كل الكتابات تحت أدوار أعلى — viewer 403 قبل الوصول للمعالج."""
    h = _user_headers("viewer", "v.write", 5)
    assert (await client.post("/api/keywords", json={}, headers=h)).status_code == 403
    assert (await client.request("DELETE", "/api/keywords", json={"keyword": "x", "category": "y"}, headers=h)).status_code == 403
    assert (await client.post("/api/settings", json={}, headers=h)).status_code == 403
    assert (await client.post("/api/sources", json={}, headers=h)).status_code == 403
    assert (await client.post("/api/rules", json={}, headers=h)).status_code == 403
    assert (await client.post("/api/accounts", json={}, headers=h)).status_code == 403
    assert (await client.post("/api/purge", headers=h)).status_code == 403
    assert (await client.post("/api/restart", headers=h)).status_code == 403
    assert (await client.post("/api/backup/import", content=b"{}", headers=h)).status_code == 403
    assert (await client.post("/api/features/monitors/toggle", headers=h)).status_code == 403
    assert (await client.post("/api/login/send-code", json={}, headers=h)).status_code == 403


@pytest.mark.asyncio
async def test_supervisor_cannot_restart(client):
    """supervisor لا يملك deployment.execute → 403 على restart."""
    h = _user_headers("supervisor", "sup.restart", 6)
    r = await client.post("/api/restart", headers=h)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_reaches_restart_handler(client):
    """admin يملك deployment.execute → الطلب يتجاوز الحارس ويصل المعالج
    (503: bot_ref غير مهيأ في بيئة الاختبار — ليس 403)."""
    h = _user_headers("admin", "adm.restart", 7)
    r = await client.post("/api/restart", headers=h)
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_supervisor_cannot_delete_keywords(client):
    """supervisor: keywords.write موجودة لكن keywords.delete ليست → 403
    على الحذف (الكتابة تصل المعالج كما تقتضي المصفوفة)."""
    h = _user_headers("supervisor", "sup.kw", 8)
    assert (await client.request("DELETE", "/api/keywords",
                                 json={"keyword": "x", "category": "y"},
                                 headers=h)).status_code == 403


# ─────────────────────────── حدود التوسيع المقصودة ───────────────────────────

@pytest.mark.asyncio
async def test_master_still_absolute(client):
    """التوكن الرئيسي مطلق على النقاط الموسعة (توافق كامل مع القديم)."""
    assert (await client.get("/api/settings", headers={**AUTH, **_ip(9)})).status_code == 200
    assert (await client.get("/api/backup/export", headers={**AUTH, **_ip(9)})).status_code == 200


@pytest.mark.asyncio
async def test_health_full_keeps_master_only(client):
    """/health/full احتفظ عمداً بـverify_token (عمق تشخيصي) — توكن
    مستخدم حتى admin → 401، والرئيسي → 200."""
    h = _user_headers("admin", "adm.health", 10)
    assert (await client.get("/health/full", headers=h)).status_code == 401
    assert (await client.get("/health/full", headers={**AUTH, **_ip(10)})).status_code == 200


@pytest.mark.asyncio
async def test_bad_user_token_still_401_and_counts(client):
    """توكن مستخدم فاسد → 401 ويغذي حارس القفل (لا باب جانبي جديد)."""
    tok, _ = dash.make_user_token("ghost", "viewer")
    r = await client.get("/api/stats",
                         headers={"Authorization": f"Bearer {tok[:-4]}beef", **_ip(11)})
    assert r.status_code == 401
