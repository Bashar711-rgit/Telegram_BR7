# -*- coding: utf-8 -*-
"""tests/test_dashboard_v15_8.py — اختبارات v15.7.1 (الطابور والذاكرة من اللوحة)

يغطي:
  * purge_processing_queue على DB حقيقي (pending + in_flight + المرآة)
  * purge_all_queues على نواة البوت (طابور التسليم الداخلي + DB) دون
    إنشاء EnhancedTelegramBot كاملاً — استدعاء غير مربوط بـ self مزيف
  * memory_guard.manual_cleanup: مسح كاشات db/المراقبين/الفلترة + gc
    + إحصاءات صادقة (قبل/بعد) + تحديث _stats
  * DashboardAPI: ops_purge_queue / ops_memory_cleanup — أرقام حقيقية
    وتدقيق ورصد 503 عند غياب دوال النواة
  * مسارات HTTP: 401 بلا مصادقة + 403 بلا CSRF + تدفق كامل موفق
  * واجهة /admin تعرض زرَّي v15.7.1 وإصدار اللوحة الجديد
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

REAL_USERNAME = "bashar"
REAL_PASSWORD = "bashar123r"


def make_api_bot(db):
    return SimpleNamespace(db=db, main_client=None)


# ═══════════════════════════════════════════════════════════════════════
# طبقة DB — حذف الطابور
# ═══════════════════════════════════════════════════════════════════════


class TestPurgeQueueDB:

    async def test_purge_removes_pending_and_resets_mirror(self, db):
        for i in range(5):
            await db.add_to_queue({"text": f"e{i}"}, priority=5)
        assert await db.queue_size() == 5
        res = await db.purge_processing_queue()
        assert res["purged"] == 5
        assert await db.queue_size() == 0
        # المرآة صفر بعد الحذف الكامل — لا عدّ وهمي
        assert db._queue_size_mirror == 0
        assert db._queue_mirror_dirty is False

    async def test_purge_removes_in_flight_too(self, db):
        for i in range(4):
            await db.add_to_queue({"text": f"e{i}"}, priority=5)
        batch = await db.pop_batch_from_queue(2)
        assert len(batch) == 2  # مسحوبة ومعلّمة in_flight
        res = await db.purge_processing_queue()
        assert res["purged"] == 4
        assert await db.queue_size() == 0
        # ack لحدث in_flight محذوف يفشل بهدوء (بلا آثار جانبية)
        acked = await db.ack_queue_event(batch[0]["_queue_id"])
        assert acked is False
        assert await db.queue_size() == 0

    async def test_purge_empty_queue(self, db):
        res = await db.purge_processing_queue()
        assert res["purged"] == 0
        assert await db.queue_size() == 0

    async def test_purge_then_add_still_works(self, db):
        await db.add_to_queue({"text": "old"}, priority=5)
        await db.purge_processing_queue()
        row_id = await db.add_to_queue({"text": "fresh"}, priority=8)
        assert row_id > 0
        assert await db.queue_size() == 1
        batch = await db.pop_batch_from_queue(4)
        assert len(batch) == 1 and batch[0]["text"] == "fresh"


# ═══════════════════════════════════════════════════════════════════════
# نواة البوت — purge_all_queues (استدعاء غير مربوط)
# ═══════════════════════════════════════════════════════════════════════


class TestBotPurgeAllQueues:

    async def test_drains_memory_and_db(self, db):
        from main import EnhancedTelegramBot as BotApp

        for i in range(6):
            await db.add_to_queue({"text": f"e{i}"}, priority=5)
        fake_self = SimpleNamespace(db=db, _internal_queue=asyncio.Queue())
        # عناصر مسحوبة من DB ولم تُسلَّم بعد — تُفرَّغ من الذاكرة
        for i in range(3):
            await fake_self._internal_queue.put({"_qid": 9000 + i, "event": {"text": f"m{i}"}})
        res = await BotApp.purge_all_queues(fake_self)
        assert res["db_purged"] == 6
        assert res["memory_drained"] == 3
        assert res["total"] == 9
        assert await db.queue_size() == 0
        assert fake_self._internal_queue.empty()

    async def test_without_internal_queue_attr(self, db):
        from main import EnhancedTelegramBot as BotApp

        await db.add_to_queue({"text": "x"}, priority=5)
        fake_self = SimpleNamespace(db=db)  # بلا _internal_queue
        res = await BotApp.purge_all_queues(fake_self)
        assert res["db_purged"] == 1
        assert res["memory_drained"] == 0

    async def test_memory_queue_bound_guard(self, db):
        from main import EnhancedTelegramBot as BotApp

        # حد الأمان (10000) يمنع سباقاً لا نهائياً مع منتج حي
        fake_self = SimpleNamespace(db=db, _internal_queue=asyncio.Queue())
        for i in range(10005):
            fake_self._internal_queue.put_nowait({"i": i})
        res = await BotApp.purge_all_queues(fake_self)
        assert res["memory_drained"] == 10000
        assert fake_self._internal_queue.qsize() == 5


# ═══════════════════════════════════════════════════════════════════════
# memory_guard — التنظيف اليدوي
# ═══════════════════════════════════════════════════════════════════════


class TestManualMemoryCleanup:

    def _fake_db(self):
        return SimpleNamespace(
            message_cache={"a": 1, "b": 2},
            _blocked_senders_cache={1, 2},
            _blocked_chats_cache={-100},
            _can_send_alert_cache={3: True},
            _features_cache={"x": True},
            _sources_cache=[{"id": 1}],
            _rules_cache=[{"id": 2}],
            _allowed_cache={"y"},
        )

    async def test_clears_all_cache_layers(self):
        from memory_guard import MemoryGuard

        fdb = self._fake_db()
        mon_locked = SimpleNamespace(
            _entity_cache={1: "x"}, _cache_lock=asyncio.Lock()
        )
        mon_filter = SimpleNamespace(
            _entity_cache={2: "y"},
            _cache_lock=None,
            filter=SimpleNamespace(_text_cache={"t": 1}, _hash_cache={"h": 1}),
        )
        async with mon_locked._cache_lock:
            # مراقب يحمل قفله — التنظيف يتخطاه بأمان (نفس عقد soft_reset)
            guard = MemoryGuard(fdb, [mon_locked, mon_filter])
            res = await guard.manual_cleanup()
        assert fdb.message_cache == {} and fdb._sources_cache is None
        assert fdb._rules_cache is None and fdb._allowed_cache is None
        assert mon_filter._entity_cache == {}
        assert mon_filter.filter._text_cache == {}
        assert mon_filter.filter._hash_cache == {}
        # المراقب المقفل لم يُمس — القفل كان محمولاً
        assert mon_locked._entity_cache == {1: "x"}
        assert res["rss_before_mb"] >= 0 and res["rss_after_mb"] >= 0
        assert res["gc_objects"] >= 0
        assert res["manual_cleanups"] >= 1
        assert res["freed_mb"] == round(max(0.0, res["rss_before_mb"] - res["rss_after_mb"]), 1)

    async def test_stats_snapshot_updated(self):
        from memory_guard import MemoryGuard, snapshot

        guard = MemoryGuard(self._fake_db(), [])
        res = await guard.manual_cleanup()
        snap = snapshot()
        assert snap["manual_cleanups"] == res["manual_cleanups"]
        assert snap["last_action"] == "manual_cleanup"
        assert snap["last_action_ts"] > 0

    async def test_module_level_uses_temp_instance_when_not_started(self):
        import memory_guard as mg

        assert mg._guard_instance is None  # الحارس غير مشغّل في الاختبار
        res = await mg.manual_cleanup(self._fake_db(), [])
        assert res["manual_cleanups"] >= 1
        assert mg._guard_instance is None  # بلا تسريب نسخة مؤقتة

    async def test_start_stop_roundtrip_stores_instance(self):
        import memory_guard as mg

        fdb = self._fake_db()
        started = await mg.start_memory_guard(fdb, [])
        if started:
            assert mg._guard_instance is not None
            res = await mg.manual_cleanup(fdb, [])
            assert res["manual_cleanups"] >= 1
            await mg.stop_memory_guard()
            assert mg._guard_instance is None
        else:
            pytest.skip("RSS measurement unavailable — guard not started")


# ═══════════════════════════════════════════════════════════════════════
# DashboardAPI — العمليات الجديدة
# ═══════════════════════════════════════════════════════════════════════


class TestDashboardAPITwoOps:

    @pytest.fixture
    async def api(self, db):
        from dashboard.api import DashboardAPI

        async def _purge():
            return {"db_purged": 7, "memory_drained": 2, "total": 9}

        async def _mem():
            return {"rss_before_mb": 100.0, "rss_after_mb": 90.0,
                    "freed_mb": 10.0, "gc_objects": 1234, "manual_cleanups": 1}

        bot = make_api_bot(db)
        bot.purge_all_queues = _purge
        bot.manual_memory_cleanup = _mem
        return DashboardAPI(bot)

    async def test_purge_queue_returns_real_numbers(self, api, db):
        for i in range(3):
            await db.add_to_queue({"text": f"e{i}"}, priority=5)
        res = await api.ops_purge_queue()
        assert res["ok"] is True
        assert res["before"] == 3
        assert res["db_purged"] == 7
        assert res["memory_drained"] == 2
        assert res["total"] == 9
        assert res["after"] == await db.queue_size()
        # تدقيق حقيقي
        logs = await db.get_audit_logs(limit=5, action="dashboard.purge_queue")
        assert any("before=3" in (l.get("new_value") or "") for l in logs)

    async def test_memory_cleanup_returns_stats(self, api, db):
        res = await api.ops_memory_cleanup()
        assert res["ok"] is True
        assert res["rss_before_mb"] == 100.0
        assert res["rss_after_mb"] == 90.0
        assert res["gc_objects"] == 1234
        logs = await db.get_audit_logs(limit=5, action="dashboard.memory_cleanup")
        assert any("rss=100.0" in (l.get("new_value") or "") for l in logs)

    async def test_purge_503_without_bot_method(self, db):
        from dashboard.api import DashboardAPI, DashboardHTTPError

        api = DashboardAPI(make_api_bot(db))  # بلا purge_all_queues
        with pytest.raises(DashboardHTTPError) as ei:
            await api.ops_purge_queue()
        assert ei.value.status == 503

    async def test_cleanup_503_without_bot_method(self, db):
        from dashboard.api import DashboardAPI, DashboardHTTPError

        api = DashboardAPI(make_api_bot(db))  # بلا manual_memory_cleanup
        with pytest.raises(DashboardHTTPError) as ei:
            await api.ops_memory_cleanup()
        assert ei.value.status == 503


# ═══════════════════════════════════════════════════════════════════════
# مسارات HTTP — المصادقة والـCSRF والتدفق الكامل
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
async def dash_env(db):
    from dashboard.server import setup_dashboard
    from aiohttp import web as aioweb
    from aiohttp.test_utils import TestClient, TestServer

    async def _purge():
        # stub واقعي: نفس منطق النواة — تفريغ الذاكرة + حذف DB حقيقي
        drained = 0
        q = getattr(bot, "_internal_queue", None)
        if q is not None:
            try:
                while True:
                    q.get_nowait()
                    drained += 1
                    if drained >= 10000:
                        break
            except asyncio.QueueEmpty:
                pass
        res = await db.purge_processing_queue()
        return {"db_purged": int(res.get("purged", 0)),
                "memory_drained": drained,
                "total": drained + int(res.get("purged", 0))}

    async def _mem():
        return {"rss_before_mb": 50.0, "rss_after_mb": 48.0,
                "freed_mb": 2.0, "gc_objects": 10, "manual_cleanups": 1}

    bot = make_api_bot(db)
    bot.purge_all_queues = _purge
    bot.manual_memory_cleanup = _mem
    bot._main_loop = asyncio.get_running_loop()
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


class TestDashboardHTTPv1571:

    async def test_new_endpoints_require_auth(self, dash_env):
        client, _ = dash_env
        for path in ("/admin/api/ops/purge_queue", "/admin/api/ops/memory_cleanup"):
            r = await client.post(path, json={})
            assert r.status == 401, path

    async def test_purge_requires_csrf(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await client.post("/admin/api/ops/purge_queue", json={})
        assert r.status == 403

    async def test_full_purge_flow(self, dash_env, db):
        client, _ = dash_env
        await _login(client)
        for i in range(4):
            await db.add_to_queue({"text": f"e{i}"}, priority=5)
        r = await client.post("/admin/api/ops/purge_queue", json={},
                              headers={"X-CSRF-Token": client._dash_token})
        assert r.status == 200
        d = await r.json()
        assert d["ok"] is True
        assert d["before"] == 4
        assert d["db_purged"] == 4
        assert d["memory_drained"] == 0
        assert d["total"] == 4
        assert d["after"] == 0
        # تدقيق حقيقي في DB
        logs = await db.get_audit_logs(limit=5, action="dashboard.purge_queue")
        assert len(logs) == 1

    async def test_full_memory_cleanup_flow(self, dash_env):
        client, _ = dash_env
        await _login(client)
        r = await client.post("/admin/api/ops/memory_cleanup", json={},
                              headers={"X-CSRF-Token": client._dash_token})
        assert r.status == 200
        d = await r.json()
        assert d["ok"] is True
        assert d["rss_before_mb"] == 50.0
        assert d["freed_mb"] == 2.0

    async def test_assets_show_v1571(self, dash_env):
        client, _ = dash_env
        r = await client.get("/admin")
        assert r.status == 200
        body = await r.text()
        for marker in ("purgeQueueBtn", "memCleanBtn", "v15.7.1"):
            assert marker in body, marker
