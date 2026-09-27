"""Integration tests for v10.0 Filter Lab — سجل القرارات + التغذية الراجعة.

Covers:
* EnhancedDatabase: save_filter_decision / get_filter_decisions /
  set_filter_decision_feedback / filter_decision_stats (fail-safe contract).
* Dashboard endpoints: /api/filter/recent, /api/filter/stats,
  /api/filter/feedback (auth + happy path + 404), /api/filter/test (503
  without bot, 400 on empty text).
"""

import time

import pytest
from httpx import ASGITransport, AsyncClient

import dashboard as dashboard_module
from dashboard import app
from database import EnhancedDatabase

TOKEN = "test-dashboard-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
async def client():
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.fixture()
async def db():
    d = EnhancedDatabase()
    await d.connect()
    try:
        yield d
    finally:
        try:
            await d.close()
        except Exception:
            pass


def _sample_rec(**over):
    rec = {
        "msg_hash": f"hash-{time.time_ns()}",
        "chat_id": -1001234567890,
        "sender_id": 111222333,
        "sender_name": "طالب",
        "account_name": "Account 1",
        "text": "ابغي احد يحل واجب رياضيات ضروري اليوم",
        "decision": "accept",
        "confidence": 0.82,
        "score": 75.0,
        "keyword": "ابغي",
        "intent_verb": "ابغي",
        "academic_object": "واجب",
        "subject": "رياضيات",
        "type_tag": "واجب",
        "urgent": True,
        "reasons": "intent_verb: ابغي; academic_object: واجب",
    }
    rec.update(over)
    return rec


class TestDatabaseFilterDecisions:
    @pytest.mark.asyncio
    async def test_save_and_get(self, db):
        rec = _sample_rec()
        assert await db.save_filter_decision(rec) is True
        rows = await db.get_filter_decisions(limit=10)
        assert rows, "expected at least one decision row"
        top = rows[0]
        assert top["keyword"] == "ابغي"
        assert top["subject"] == "رياضيات"
        assert top["urgent"] is True
        assert top["decision"] == "accept"

    @pytest.mark.asyncio
    async def test_get_filter_by_decision(self, db):
        await db.save_filter_decision(_sample_rec(decision="review", keyword="احتاج"))
        await db.save_filter_decision(_sample_rec(decision="ignore", keyword="مساعدة"))
        review_rows = await db.get_filter_decisions(limit=50, decision="review")
        assert all(r["decision"] == "review" for r in review_rows)
        assert review_rows, "expected the review row to come back"

    @pytest.mark.asyncio
    async def test_feedback_roundtrip(self, db):
        await db.save_filter_decision(_sample_rec())
        rows = await db.get_filter_decisions(limit=1)
        rid = rows[0]["id"]
        updated = await db.set_filter_decision_feedback(rid, True, "تنبيه صحيح")
        assert updated is not None
        assert updated["feedback"] == 1
        assert updated["feedback_note"] == "تنبيه صحيح"
        # تصحيح خاطئ
        updated2 = await db.set_filter_decision_feedback(rid, False, "")
        assert updated2["feedback"] == -1

    @pytest.mark.asyncio
    async def test_feedback_missing_id_returns_none(self, db):
        assert await db.set_filter_decision_feedback(999_999_999, True) is None

    @pytest.mark.asyncio
    async def test_stats_counts(self, db):
        await db.save_filter_decision(_sample_rec(decision="accept"))
        await db.save_filter_decision(_sample_rec(decision="review"))
        await db.save_filter_decision(_sample_rec(decision="ignore"))
        st = await db.filter_decision_stats(hours=24)
        assert st["total"] >= 3
        assert st["accepted"] >= 1
        assert st["review"] >= 1
        assert st["ignored"] >= 1
        assert st["avg_confidence"] > 0
        names = [s["name"] for s in st["subjects"]]
        assert "رياضيات" in names
        kws = [k["name"] for k in st["top_keywords"]]
        assert "ابغي" in kws

    @pytest.mark.asyncio
    async def test_fail_safety_bad_record(self, db):
        # صف فارغ لا يرفع استثناءً (فشل-آمن)
        ok = await db.save_filter_decision({})
        assert ok in (True, False)


class TestFilterLabEndpoints:
    @pytest.mark.asyncio
    async def test_recent_requires_auth(self, client):
        r = await client.get("/api/filter/recent")
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_recent_ok(self, client):
        r = await client.get("/api/filter/recent?limit=5", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert isinstance(body["decisions"], list)

    @pytest.mark.asyncio
    async def test_stats_ok(self, client):
        r = await client.get("/api/filter/stats?hours=24", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        for key in ("total", "accepted", "review", "ignored",
                    "avg_confidence", "subjects", "top_keywords"):
            assert key in body

    @pytest.mark.asyncio
    async def test_feedback_missing_decision_404(self, client):
        r = await client.post("/api/filter/feedback", headers=AUTH,
                              json={"id": 987_654_321, "correct": True})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_feedback_roundtrip_via_api(self, client, db):
        # أدخل صفاً عبر اتصال مستقل ثم قيّمه عبر اللوحة
        await db.save_filter_decision(_sample_rec(intent_verb="ابغي",
                                                  academic_object="واجب"))
        rows = await db.get_filter_decisions(limit=1)
        rid = rows[0]["id"]
        r = await client.post("/api/filter/feedback", headers=AUTH,
                              json={"id": rid, "correct": False,
                                    "note": "ليس طلباً حقيقياً"})
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["decision"]["feedback"] == -1
        # بلا bot_ref لا يوجد فلتر للتعلّم — learned فارغة ولا خطأ
        assert isinstance(body["learned"], list)

    @pytest.mark.asyncio
    async def test_test_endpoint_requires_auth(self, client):
        r = await client.post("/api/filter/test", json={"text": "ابغي واجب"})
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_test_endpoint_empty_text_400(self, client):
        r = await client.post("/api/filter/test", headers=AUTH, json={"text": "   "})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_test_endpoint_no_bot_503(self, client):
        # في بيئة الاختبار لا يوجد bot_ref → الفلتر غير متاح
        r = await client.post("/api/filter/test", headers=AUTH,
                              json={"text": "ابغي واجب رياضيات"})
        assert r.status_code == 503
