# -*- coding: utf-8 -*-
"""tests/test_dashboard_v15_7.py — اختبارات مركز الإدارة المتكاملة v15.7.0

يغطي:
  * قوائم الحظر: list_blocked / blocked_overview / unblock_bulk على DB حقيقي
  * الإثراء من sender_contacts (username/is_bot) للعرض فقط
  * مستعرض الأشخاص: list_people + أعلام محظور/مسموح/بوت + العدّادات
  * القواعد: CRUD كامل عبر DashboardAPI + التحقق من الشروط + تصدير/استيراد
  * الميزات: قائمة من FEATURE_REGISTRY + تبديل مع رفض المفاتيح المجهولة
  * إعادة تسمية حسابات اللوحة (panel_accounts)
  * سجل التدقيق: فلاتر q/action/since_hours
  * مسارات HTTP: مصادقة 401 + CSRF 403 + الحظر/الأشخاص/القواعد/الميزات
  * أمان: لا أسرار في الاستجابات + تنظيف المدخلات + حدود القوائم

الكل فوق EnhancedDatabase SQLite حقيقي — نفس مستهلكات monitors الفعلية
(blocked_senders/blocked_chats/features/rules/allowed_entities).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

# ═══════════════════════════════════════════════════════════════════════
# Fixtures محلية
# ═══════════════════════════════════════════════════════════════════════


class FakeResolveClient:
    """get_entity مزيف — يفصل username الرقمي عن اسم البوت."""

    def __init__(self, entities):
        self._entities = entities  # {username_or_handle: namespace}
        self.calls = []

    async def get_entity(self, uname):
        self.calls.append(uname)
        key = str(uname).lstrip("@")
        if key in self._entities:
            return self._entities[key]
        raise KeyError(f"Cannot find any entity corresponding to {uname}")


def make_api_bot(db, main_client=None):
    bot = SimpleNamespace(db=db, main_client=main_client)
    return bot


@pytest.fixture(autouse=True)
def _reset_dash():
    from dashboard.state import DASH
    DASH.overview_cache.invalidate()
    DASH.groups_cache.invalidate()
    yield
    DASH.overview_cache.invalidate()
    DASH.groups_cache.invalidate()


@pytest.fixture
async def api(db):
    """DashboardAPI فوق DB حقيقي — يستدعى مباشرة (بدون HTTP)."""
    from dashboard.api import DashboardAPI
    return DashboardAPI(make_api_bot(db))


# ═══════════════════════════════════════════════════════════════════════
# طبقة DB — قوائم الحظر
# ═══════════════════════════════════════════════════════════════════════


class TestBlockedDB:

    async def test_block_sender_then_list_and_overview(self, db):
        await db.block_sender(111, reason="سبام", by="test")
        rows = await db.list_blocked("sender")
        assert len(rows) == 1
        assert rows[0]["kind"] == "sender"
        assert rows[0]["entity_id"] == 111
        assert rows[0]["reason"] == "سبام"
        assert rows[0]["blocked_by"] == "test"
        ov = await db.blocked_overview()
        assert ov["senders"] == 1 and ov["chats"] == 0

    async def test_block_chat_then_list(self, db):
        await db.block_chat(-1001234567890, reason="قناة إعلانية", by="test")
        rows = await db.list_blocked("chat")
        assert len(rows) == 1
        assert rows[0]["entity_id"] == -1001234567890
        ov = await db.blocked_overview()
        assert ov["chats"] == 1

    async def test_list_blocked_invalid_kind(self, db):
        assert await db.list_blocked("bogus") == []

    async def test_list_blocked_search(self, db):
        await db.block_sender(222, reason="إعلانات مستمرة", by="t")
        await db.block_sender(333, reason="سبام", by="t")
        hits = await db.list_blocked("sender", q="إعلانات")
        assert len(hits) == 1 and hits[0]["entity_id"] == 222
        by_id = await db.list_blocked("sender", q="333")
        assert len(by_id) == 1 and by_id[0]["entity_id"] == 333
        none = await db.list_blocked("sender", q="لا_يوجد_ابدا")
        assert none == []

    async def test_list_blocked_enriched_from_contacts(self, db):
        await db._execute(
            "INSERT INTO sender_contacts (sender_id, username, first_name, is_bot)"
            " VALUES (?, ?, ?, ?)",
            (444, "spam_bot", "BotName", 1),
        )
        await db._commit()
        await db.block_sender(444, reason="بوت إعلاني", by="t")
        rows = await db.list_blocked("sender")
        assert rows[0]["username"] == "spam_bot"
        assert rows[0]["first_name"] == "BotName"
        assert bool(rows[0]["is_bot"]) is True
        ov = await db.blocked_overview()
        assert ov["bots"] == 1

    async def test_unblock_bulk_clears_cache(self, db):
        for sid in (1, 2, 3):
            await db.block_sender(sid, by="t")
        # املأ الكاش ثم تأكد من الفراغ بعد الإلغاء الجماعي
        assert await db.is_blocked_sender(1) is True
        removed = await db.unblock_bulk("sender", [1, 2, 3, 999])
        assert removed == 3
        assert 1 not in db._blocked_senders_cache
        assert await db.is_blocked_sender(1) is False
        rows = await db.list_blocked("sender")
        assert rows == []

    async def test_unblock_bulk_chat_and_limits(self, db):
        await db.block_chat(-1001, by="t")
        await db.block_chat(-1002, by="t")
        removed = await db.unblock_bulk("chat", [-1001, -1002, "not-a-number", -1001])
        assert removed == 2
        assert await db.unblock_bulk("bogus", [1]) == 0
        assert await db.unblock_bulk("sender", []) == 0


# ═══════════════════════════════════════════════════════════════════════
# طبقة DB — الأشخاص
# ═══════════════════════════════════════════════════════════════════════


class TestPeopleDB:

    async def _seed(self, db):
        people = [
            (10, "user_a", "أحمد", 0, 30, 3),
            (20, "helper_bot", "بوت مساعد", 1, 5, 0),
            (30, "user_c", "كريم", 0, 12, 1),
        ]
        for sid, uname, fname, is_bot, msgs, alerts in people:
            await db._execute(
                "INSERT INTO sender_contacts (sender_id, username, first_name, is_bot)"
                " VALUES (?, ?, ?, ?)",
                (sid, uname, fname, is_bot),
            )
            await db._execute(
                "INSERT INTO sender_stats (sender_id, total_messages, alerts_sent, first_seen)"
                " VALUES (?, ?, ?, ?)",
                (sid, msgs, alerts, time.time()),
            )
        await db._commit()

    async def test_list_people_flags_and_overview(self, db):
        await self._seed(db)
        await db.block_sender(30, by="t")
        await db.add_allowed_entity("sender", 10, note="موثوق")
        rows = await db.list_people()
        assert len(rows) == 3
        by_id = {r["sender_id"]: r for r in rows}
        assert by_id[30]["is_blocked"] == 1
        assert by_id[10]["is_allowed"] == 1
        assert by_id[20]["is_bot"] == 1
        assert by_id[10]["total_messages"] == 30
        ov = await db.people_overview()
        assert ov["total"] == 3 and ov["bots"] == 1 and ov["blocked"] == 1 and ov["allowed"] == 1

    async def test_list_people_filters(self, db):
        await self._seed(db)
        await db.block_sender(30, by="t")
        blocked_rows = await db.list_people(flag="blocked")
        assert [r["sender_id"] for r in blocked_rows] == [30]
        bot_rows = await db.list_people(flag="bots")
        assert [r["sender_id"] for r in bot_rows] == [20]
        allowed_rows = await db.list_people(flag="allowed")
        assert allowed_rows == []

    async def test_list_people_search(self, db):
        await self._seed(db)
        hits = await db.list_people(q="user_a")
        assert [r["sender_id"] for r in hits] == [10]
        hits = await db.list_people(q="20")
        assert [r["sender_id"] for r in hits] == [20]
        hits = await db.list_people(q="أحمد")
        assert [r["sender_id"] for r in hits] == [10]


# ═══════════════════════════════════════════════════════════════════════
# طبقة API — الحظر
# ═══════════════════════════════════════════════════════════════════════


class TestBlockedAPI:

    async def test_blocked_list_shape(self, api, db):
        await db.block_sender(111, reason="سبب", by="t")
        data = await api.blocked_list("sender", "", 1)
        assert data["items"][0]["entity_id"] == 111
        assert data["overview"]["senders"] == 1
        assert "note" in data and "capture_from_bots" in data
        # لا تسريب مفاتيح داخلية
        assert set(data["items"][0].keys()) <= {
            "kind", "entity_id", "reason", "blocked_by", "blocked_at",
            "username", "name", "is_bot",
        }

    async def test_blocked_add_numeric_sender(self, api, db):
        res = await api.blocked_add("123456789", "sender", "إزعاج")
        assert res["ok"] is True and res["kind"] == "sender"
        assert await db.is_blocked_sender(123456789) is True
        audits = await db.get_audit_logs(limit=5)
        assert any(a["action"] == "dashboard.blocked_add" for a in audits)

    async def test_blocked_add_numeric_bot(self, api, db):
        res = await api.blocked_add("987654321", "bot", "بوت سبام")
        assert res["ok"] is True and res["is_bot"] is True
        assert await db.is_blocked_sender(987654321) is True

    async def test_blocked_add_numeric_chat(self, api, db):
        res = await api.blocked_add("-1001234567890", "chat", "قناة")
        assert res["ok"] is True and res["kind"] == "chat"
        assert await db.is_blocked_chat(-1001234567890) is True

    async def test_blocked_add_rejects_bad_input(self, api):
        from dashboard.api import DashboardHTTPError
        with pytest.raises(DashboardHTTPError) as e:
            await api.blocked_add("not valid <<<", "sender", "")
        assert e.value.status == 400
        with pytest.raises(DashboardHTTPError):
            await api.blocked_add("123", "bogus_kind", "")
        # معرف سالب لمرسل → خطأ واضح
        with pytest.raises(DashboardHTTPError):
            await api.blocked_add("-100123", "sender", "")

    async def test_blocked_add_username_bot_resolution(self, db):
        client = FakeResolveClient({
            "spam_bot": SimpleNamespace(id=99988, bot=True, title="",
                                        first_name="بوت", broadcast=False, megagroup=False),
        })
        from dashboard.api import DashboardAPI
        api = DashboardAPI(make_api_bot(db, main_client=client))
        res = await api.blocked_add("t.me/spam_bot", "bot", "بوت")
        assert res["ok"] is True and res["is_bot"] is True
        assert res["entity_id"] == 99988
        assert await db.is_blocked_sender(99988) is True

    async def test_blocked_add_username_chat_normalizes_channel(self, db):
        client = FakeResolveClient({
            "spamch": SimpleNamespace(id=555666, bot=False, title="قناة",
                                      first_name="", broadcast=True, megagroup=False),
        })
        from dashboard.api import DashboardAPI
        api = DashboardAPI(make_api_bot(db, main_client=client))
        res = await api.blocked_add("@spamch", "chat", "")
        assert res["ok"] is True
        assert res["entity_id"] == -100555666  # صيغة أحداث Telethon
        assert await db.is_blocked_chat(-100555666) is True

    async def test_blocked_add_username_unresolvable(self, db):
        client = FakeResolveClient({})
        from dashboard.api import DashboardAPI
        api = DashboardAPI(make_api_bot(db, main_client=client))
        from dashboard.api import DashboardHTTPError
        with pytest.raises(DashboardHTTPError) as e:
            await api.blocked_add("@ghost_user", "sender", "")
        assert e.value.status == 400
        assert "resolve_failed" in e.value.payload["error"]

    async def test_blocked_add_no_client(self, db):
        from dashboard.api import DashboardAPI, DashboardHTTPError
        api = DashboardAPI(make_api_bot(db, main_client=None))
        with pytest.raises(DashboardHTTPError) as e:
            await api.blocked_add("@some_user", "sender", "")
        assert e.value.status == 503

    async def test_blocked_remove(self, api, db):
        await db.block_sender(777, by="t")
        res = await api.blocked_remove("sender", 777)
        assert res["ok"] is True
        assert await db.is_blocked_sender(777) is False

    async def test_blocked_bulk_remove(self, api, db):
        for sid in (5, 6, 7):
            await db.block_sender(sid, by="t")
        res = await api.blocked_bulk_remove("sender", [5, "6", "x", 7])
        assert res["removed"] == 3
        rows = await db.list_blocked("sender")
        assert rows == []

    async def test_blocked_remove_invalid(self, api):
        from dashboard.api import DashboardHTTPError
        with pytest.raises(DashboardHTTPError):
            await api.blocked_remove("bogus", 1)
        with pytest.raises(DashboardHTTPError):
            await api.blocked_remove("sender", "abc")


# ═══════════════════════════════════════════════════════════════════════
# طبقة API — الأشخاص
# ═══════════════════════════════════════════════════════════════════════


class TestPeopleAPI:

    async def _seed_one(self, db, sid=4242, uname="person_x"):
        await db._execute(
            "INSERT INTO sender_contacts (sender_id, username, first_name) VALUES (?, ?, ?)",
            (sid, uname, "شخص"),
        )
        await db._commit()

    async def test_person_block_unblock(self, api, db):
        res = await api.person_action("block", 4242, "سبب")
        assert res["ok"] is True
        assert await db.is_blocked_sender(4242) is True
        res = await api.person_action("unblock", 4242)
        assert res["ok"] is True
        assert await db.is_blocked_sender(4242) is False

    async def test_person_allow_disallow(self, api, db):
        res = await api.person_action("allow", 4242, "موثوق")
        assert res["ok"] is True
        assert await db.is_sender_allowed(4242) is True
        res = await api.person_action("disallow", 4242)
        assert res["ok"] is True
        assert await db.is_sender_allowed(4242) is False

    async def test_person_action_validation(self, api):
        from dashboard.api import DashboardHTTPError
        with pytest.raises(DashboardHTTPError):
            await api.person_action("fly", 1)
        with pytest.raises(DashboardHTTPError):
            await api.person_action("block", -5)
        with pytest.raises(DashboardHTTPError):
            await api.person_action("block", "abc")

    async def test_people_list(self, api, db):
        await self._seed_one(db)
        data = await api.people_list("", "all", 1)
        assert data["overview"]["total"] >= 1
        assert any(i["sender_id"] == 4242 for i in data["items"])
        data = await api.people_list("person_x", "all", 1)
        assert len(data["items"]) == 1


# ═══════════════════════════════════════════════════════════════════════
# طبقة API — القواعد
# ═══════════════════════════════════════════════════════════════════════


class TestRulesAPI:

    GOOD = {"name": "حظر سبام", "conditions": {"keyword": "بيع حسابات"},
            "action": "block", "action_value": "", "priority": 50}

    async def test_rule_add_and_list(self, api):
        res = await api.rule_add(self.GOOD)
        assert res["ok"] is True
        data = await api.rules_list()
        assert data["total"] == 1
        row = data["items"][0]
        assert row["name"] == "حظر سبام"
        assert row["action"] == "block"
        assert row["enabled"] is True
        assert data["condition_keys"] == ["keyword", "source_chat_id", "sender_id", "min_confidence"]
        assert set(data["actions"]) == {"allow", "block", "tag"}

    async def test_rule_add_validations(self, api):
        from dashboard.api import DashboardHTTPError
        bad = [
            {"name": "", "conditions": {"keyword": "x"}, "action": "block"},
            {"name": "بلا شروط", "conditions": {}, "action": "block"},
            {"name": "شرط مجهول", "conditions": {"hacker": 1}, "action": "block"},
            {"name": "إجراء مجهول", "conditions": {"keyword": "x"}, "action": "nuke"},
        ]
        for payload in bad:
            with pytest.raises(DashboardHTTPError):
                await api.rule_add(payload)

    async def test_rule_edit_toggle_delete(self, api):
        res = await api.rule_add(self.GOOD)
        rid = res["rule"]["id"]
        res = await api.rule_edit(rid, {"priority": 10,
                                        "conditions": {"keyword": "كلمة جديدة"}})
        assert res["ok"] is True
        data = await api.rules_list()
        assert data["items"][0]["priority"] == 10
        assert data["items"][0]["conditions"] == {"keyword": "كلمة جديدة"}
        res = await api.rule_toggle(rid, False)
        assert res["ok"] is True
        data = await api.rules_list()
        assert data["items"][0]["enabled"] is False
        res = await api.rule_delete(rid)
        assert res["ok"] is True
        assert (await api.rules_list())["total"] == 0

    async def test_rule_edit_invalid(self, api):
        from dashboard.api import DashboardHTTPError
        res = await api.rule_add(self.GOOD)
        rid = res["rule"]["id"]
        with pytest.raises(DashboardHTTPError):
            await api.rule_edit(rid, {"conditions": {"bogus": 1}})
        with pytest.raises(DashboardHTTPError) as e:
            await api.rule_edit(999999, {"priority": 1})
        assert e.value.status == 404

    async def test_rules_export_import_roundtrip(self, api):
        await api.rule_add(self.GOOD)
        await api.rule_add({"name": "وسم طلب", "conditions": {"keyword": "أبي"},
                            "action": "tag", "action_value": "طلب", "priority": 100})
        exported = await api.rules_export()
        assert len(exported["rules"]) == 2
        # DB جديد (نفس الـ db هنا — نحذف الجميع ثم نستورد)
        for r in (await api.rules_list())["items"]:
            await api.rule_delete(r["id"])
        res = await api.rules_import(
            __import__("json").dumps(exported, ensure_ascii=False)
        )
        assert res["ok"] is True and res["applied"] == 2 and res["skipped"] == 0
        assert (await api.rules_list())["total"] == 2

    async def test_rules_import_skips_invalid(self, api):
        import json as _json
        res = await api.rules_import(_json.dumps({
            "rules": [
                {"name": "سليمة", "conditions": {"keyword": "x"}, "action": "block"},
                {"name": "فاسدة", "conditions": {"hacker": 1}, "action": "block"},
                {"name": "فاسدة 2", "conditions": {"keyword": "x"}, "action": "nuke"},
                "not-a-dict",
            ]
        }))
        assert res["applied"] == 1 and res["skipped"] == 3

    async def test_rules_import_bad_payload(self, api):
        from dashboard.api import DashboardHTTPError
        for bad in ("{invalid json", '{"nope": 1}', '[1,2]'):
            with pytest.raises(DashboardHTTPError):
                await api.rules_import(bad)


# ═══════════════════════════════════════════════════════════════════════
# طبقة API — الميزات + إعادة التسمية + سجل التدقيق
# ═══════════════════════════════════════════════════════════════════════


class TestFeaturesAndOpsAPI:

    async def test_features_list_from_registry(self, api):
        from database import EnhancedDatabase
        keys = set(EnhancedDatabase.FEATURE_REGISTRY.keys())
        data = await api.features_list()
        got = {f["key"] for f in data["items"]}
        assert got == keys
        for f in data["items"]:
            assert f["label"] and f["impact"]

    async def test_feature_toggle_roundtrip(self, api, db):
        res = await api.feature_toggle("rule_engine", False)
        assert res["ok"] is True
        assert await db.is_feature_enabled("rule_engine") is False
        res = await api.feature_toggle("rule_engine", True)
        assert res["ok"] is True
        assert await db.is_feature_enabled("rule_engine") is True

    async def test_feature_toggle_unknown_key(self, api):
        from dashboard.api import DashboardHTTPError
        with pytest.raises(DashboardHTTPError) as e:
            await api.feature_toggle("not_a_feature", False)
        assert e.value.status == 400

    async def test_panel_rename(self, api, db):
        row_id = await db.insert_panel_account(
            phone="+966500000001", display_name="حساب قديم", username="old",
            tg_user_id=1, api_id="1", api_hash_encrypted="enc", session_encrypted="enc",
        )
        res = await api.panel_rename(row_id, "الحساب الرئيسي")
        assert res["ok"] is True
        row = await db.get_panel_account(row_id)
        assert row["display_name"] == "الحساب الرئيسي"
        from dashboard.api import DashboardHTTPError
        with pytest.raises(DashboardHTTPError) as e:
            await api.panel_rename(999999, "لا يوجد")
        assert e.value.status == 404
        with pytest.raises(DashboardHTTPError):
            await api.panel_rename(row_id, "   ")

    async def test_ops_audit_filters(self, api):
        await api.feature_toggle("rule_engine", False)
        await api.feature_toggle("rule_engine", True)
        await api.blocked_add("555000", "sender", "اختبار")
        # فلتر بالإجراء
        data = await api.ops_audit(1, action="dashboard.feature_toggle")
        assert data["items"] and all("feature_toggle" in i["action"] for i in data["items"])
        # فلتر بالبحث
        data = await api.ops_audit(1, q="555000")
        assert data["items"] and any("555000" in i["object_id"] for i in data["items"])
        # فلتر بالفترة (آخر ساعة تشمل كل شيء)
        data = await api.ops_audit(1, since_hours=1)
        assert len(data["items"]) >= 3


# ═══════════════════════════════════════════════════════════════════════
# مسارات HTTP — المصادقة + CSRF + التدفق الكامل
# ═══════════════════════════════════════════════════════════════════════

REAL_USERNAME = "bashar"
REAL_PASSWORD = "bashar123r"


def make_http_bot(db):
    bot = make_api_bot(db)
    bot._main_loop = asyncio.get_running_loop()
    return bot


@pytest.fixture
async def dash_env(db):
    from dashboard.server import setup_dashboard
    from aiohttp import web as aioweb
    from aiohttp.test_utils import TestClient, TestServer

    bot = make_http_bot(db)
    app = aioweb.Application()
    setup_dashboard(app, bot)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, bot
    finally:
        await client.close()


async def _login(client):
    r = await client.post("/admin/api/login",
                          json={"username": REAL_USERNAME, "password": REAL_PASSWORD})
    raw = r.headers.get("Set-Cookie", "")
    if "br7dash=" in raw:
        client._dash_token = raw.split("br7dash=", 1)[1].split(";", 1)[0]
    return r


async def _post(client, path, body=None):
    return await client.post(path, json=body or {},
                             headers={"X-CSRF-Token": getattr(client, "_dash_token", "")})


class TestDashboardHTTPv157:

    async def test_new_endpoints_require_auth(self, dash_env):
        client, _ = dash_env
        for path in ("/admin/api/blocked", "/admin/api/people", "/admin/api/rules",
                     "/admin/api/features", "/admin/api/ops/audit"):
            r = await client.get(path)
            assert r.status == 401, path
        for path in ("/admin/api/blocked/add", "/admin/api/people/action",
                     "/admin/api/rules/add", "/admin/api/features/toggle",
                     "/admin/api/accounts/row/rename"):
            r = await client.post(path, json={})
            assert r.status == 401, path

    async def test_blocked_add_requires_csrf(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await client.post("/admin/api/blocked/add",
                              json={"input": "123", "kind": "sender"})
        assert r.status == 403

    async def test_assets_smoke_v157(self, dash_env):
        client, _ = dash_env
        r = await client.get("/admin")
        assert r.status == 200
        body = await r.text()
        for marker in ("tab-blocked", "tab-people", "tab-rules", "tab-features",
                       "v15.7.0"):
            assert marker in body, marker

    async def test_blocked_full_flow_http(self, dash_env, db):
        client, _ = dash_env
        await _login(client)
        # GET فارغ
        r = await client.get("/admin/api/blocked?kind=sender")
        assert r.status == 200
        data = await r.json()
        assert data["items"] == [] and data["overview"]["senders"] == 0
        # إضافة حظر
        r = await _post(client, "/admin/api/blocked/add",
                        {"input": "123456", "kind": "sender", "reason": "إزعاج"})
        assert r.status == 200
        data = await r.json()
        assert data["ok"] is True and data["entity_id"] == 123456
        # القائمة تعرضه
        r = await client.get("/admin/api/blocked?kind=sender&q=123456")
        data = await r.json()
        assert data["total"] == 1
        # الحظر يُستهلك فعلاً في المسار الحرج
        assert await db.is_blocked_sender(123456) is True
        # إزالة
        r = await _post(client, "/admin/api/blocked/remove",
                        {"kind": "sender", "entity_id": 123456})
        assert r.status == 200
        assert await db.is_blocked_sender(123456) is False

    async def test_blocked_add_http_validation(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await _post(client, "/admin/api/blocked/add",
                        {"input": "bad input <<<", "kind": "sender"})
        assert r.status == 400
        r = await _post(client, "/admin/api/blocked/add",
                        {"input": "-100x", "kind": "bogus"})
        assert r.status == 400

    async def test_people_flow_http(self, dash_env, db):
        client, _ = dash_env
        await _login(client)
        await db._execute(
            "INSERT INTO sender_contacts (sender_id, username, first_name) VALUES (?, ?, ?)",
            (313, "http_user", "HTTP"),
        )
        await db._commit()
        r = await client.get("/admin/api/people")
        assert r.status == 200
        data = await r.json()
        assert any(i["sender_id"] == 313 for i in data["items"])
        r = await _post(client, "/admin/api/people/action",
                        {"action": "block", "sender_id": 313, "note": "اختبار"})
        assert r.status == 200 and (await r.json())["ok"] is True
        assert await db.is_blocked_sender(313) is True
        r = await client.get("/admin/api/people?flag=blocked")
        data = await r.json()
        assert any(i["sender_id"] == 313 for i in data["items"])
        r = await _post(client, "/admin/api/people/action", {"action": "unblock", "sender_id": 313})
        assert r.status == 200

    async def test_rules_flow_http(self, dash_env, db):
        client, _ = dash_env
        await _login(client)
        r = await _post(client, "/admin/api/rules/add",
                        {"name": "قاعدة HTTP", "conditions": {"keyword": "شراء"},
                         "action": "block", "priority": 10})
        assert r.status == 200 and (await r.json())["ok"] is True
        r = await client.get("/admin/api/rules")
        data = await r.json()
        assert data["total"] == 1
        rid = data["items"][0]["id"]
        r = await _post(client, "/admin/api/rules/toggle", {"rule_id": rid, "enabled": False})
        assert r.status == 200
        r = await _post(client, "/admin/api/rules/edit",
                        {"rule_id": rid, "payload": {"priority": 5}})
        assert r.status == 200
        r = await _post(client, "/admin/api/rules/delete", {"rule_id": rid})
        assert r.status == 200 and (await r.json())["ok"] is True
        # تحقق فاشل عبر HTTP
        r = await _post(client, "/admin/api/rules/add",
                        {"name": "بلا شروط", "conditions": {}, "action": "block"})
        assert r.status == 400

    async def test_features_flow_http(self, dash_env, db):
        client, _ = dash_env
        await _login(client)
        r = await client.get("/admin/api/features")
        assert r.status == 200
        data = await r.json()
        assert data["items"]
        key = data["items"][0]["key"]
        r = await _post(client, "/admin/api/features/toggle", {"key": key, "enabled": False})
        assert r.status == 200 and (await r.json())["ok"] is True
        assert await db.is_feature_enabled(key) is False
        r = await _post(client, "/admin/api/features/toggle", {"key": key, "enabled": True})
        assert r.status == 200
        r = await _post(client, "/admin/api/features/toggle", {"key": "bogus", "enabled": True})
        assert r.status == 400

    async def test_audit_filters_http(self, dash_env):
        client, _ = dash_env
        await _login(client)
        await _post(client, "/admin/api/blocked/add", {"input": "777", "kind": "sender"})
        r = await client.get("/admin/api/ops/audit?action=dashboard.blocked_add")
        assert r.status == 200
        data = await r.json()
        assert data["items"] and all("blocked_add" in i["action"] for i in data["items"])
        r = await client.get("/admin/api/ops/audit?q=777")
        data = await r.json()
        assert data["items"]

    async def test_rename_flow_http(self, dash_env, db):
        client, _ = dash_env
        await _login(client)
        row_id = await db.insert_panel_account(
            phone="+966500000777", display_name="قديم", username="u",
            tg_user_id=42, api_id="1", api_hash_encrypted="e", session_encrypted="s",
        )
        r = await _post(client, "/admin/api/accounts/row/rename",
                        {"row_id": row_id, "display_name": "جديد"})
        assert r.status == 200 and (await r.json())["ok"] is True
        row = await db.get_panel_account(row_id)
        assert row["display_name"] == "جديد"

    async def test_rules_export_http_attachment(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await client.get("/admin/api/rules/export")
        assert r.status == 200
        assert "attachment" in r.headers.get("Content-Disposition", "")
        data = await r.json()
        assert "rules" in data and "exported_at" in data
