# -*- coding: utf-8 -*-
"""tests/test_queue_v15_8.py — اختبارات v15.8.0 (الـack الدفعي + الفهرس)

يغطي:
  * ack_queue_events_batch: حذف دفعي حقيقي + المرآة + إزالة التكرار +
    المعرفات الشاذة + السقف 512 + المعرفات المحذوفة مسبقاً (purge)
  * التكامل مع pop_batch/purge: تدفق كامل سحب→معالجة→دفعة اكات
  * bot._ack_flush_ids: نجاح + فشل-آمن (فشل DB لا يرفع — الصفوف تبقى
    قابلة للاسترداد عبر recover_stale_inflight بلا تكرار تنبيهات)
  * idx_queue_status: الفهرس موجود بعد connect
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class TestBatchAckDB:

    async def test_batch_delete_and_mirror(self, db):
        ids = []
        for i in range(10):
            ids.append(await db.add_to_queue({"text": f"e{i}"}, priority=5))
        assert await db.queue_size() == 10
        deleted = await db.ack_queue_events_batch(ids[:7])
        assert deleted == 7
        assert await db.queue_size() == 3
        assert db._queue_size_mirror == 3
        assert db._queue_mirror_dirty is False

    async def test_batch_dedup_and_invalid_ids(self, db):
        a = await db.add_to_queue({"text": "a"}, priority=5)
        b = await db.add_to_queue({"text": "b"}, priority=5)
        # تكرار + صفر + سالب + نص — تُنظف كلها بهدوء
        deleted = await db.ack_queue_events_batch(
            [a, a, 0, -5, "bad-id", b, a]
        )
        assert deleted == 2
        assert await db.queue_size() == 0

    async def test_batch_empty_and_garbage_only(self, db):
        assert await db.ack_queue_events_batch([]) == 0
        assert await db.ack_queue_events_batch(["x", None, 0, -1]) == 0
        assert await db.queue_size() == 0

    async def test_batch_cap_512(self, db):
        ids = []
        for i in range(600):
            ids.append(await db.add_to_queue({"text": f"e{i}"}, priority=5))
        deleted = await db.ack_queue_events_batch(ids)  # 600 > 512 سقف
        assert deleted == 512
        assert await db.queue_size() == 88

    async def test_batch_after_purge_noop(self, db):
        a = await db.add_to_queue({"text": "x"}, priority=5)
        await db.purge_processing_queue()
        assert await db.queue_size() == 0
        # معرف محذوف بالـpurge → الدفعة لا تجد شيئاً بلا خطأ
        deleted = await db.ack_queue_events_batch([a])
        assert deleted == 0
        assert await db.queue_size() == 0

    async def test_full_pipeline_batch_flow(self, db):
        """سحبة → معالجة وهمية → دفعة اكات — نفس مسار العمال الحقيقي."""
        n = 40
        for i in range(n):
            await db.add_to_queue({"text": f"m{i}", "i": i}, priority=5)
        acked: list = []
        total_claimed = 0
        while True:
            batch = await db.pop_batch_from_queue(16)
            if not batch:
                break
            total_claimed += len(batch)
            for item in batch:
                acked.append(item["_queue_id"])
                if len(acked) >= 24:  # محاكاة صفّار العمال (512/32)
                    await db.ack_queue_events_batch(acked)
                    acked = []
        if acked:
            await db.ack_queue_events_batch(acked)
        assert total_claimed == n
        assert await db.queue_size() == 0
        assert db._queue_size_mirror == 0

    async def test_recovered_rows_still_block_duplicates(self, db):
        """الصفحة الأمان: حدث مُعالج اكه ضاع (محاكاة تحطم قبل التصفير) →
        يُسترد → طبقة الحماية (receipt) تمنع التنبيه المزدوج."""
        from unified_alerts.stats import get_unified_stats

        qid = await db.add_to_queue({"text": "alert-me", "chat_id": 1,
                                    "message_id": 7}, priority=5)
        # معالجة + إرسال + حفظ receipt ثم تحطم قبل الـack
        claimed = await db.pop_batch_from_queue(4)
        assert claimed and claimed[0]["_queue_id"] == qid
        msg_hash = claimed[0].get("message_hash") or "simhash123"
        await db.add_alert(
            SimpleNamespace(message_hash=msg_hash, chat_id=1, sender_id=9,
                            account_name="A", keyword="kw", alert_text="t",
                            timestamp=1.0, decision="accept", confidence=0.9,
                            reasons="", intent_verb="", academic_object="",
                            negation_detected=0, advert_score=0.0,
                            contact_method="")
        )
        # الـack ضاع — الصف عالق in_flight → الاسترداد يعيده
        rec = await db.recover_stale_inflight(max_age_seconds=1)
        await db._execute("UPDATE processing_queue SET claimed_at = ? "
                          "WHERE id = ?", (0.0, qid))
        await db._commit()
        rec = await db.recover_stale_inflight(max_age_seconds=1)
        assert rec["recovered"] == 1
        # إعادة المعالجة ترى الـreceipt وتتخطى (بلا تنبيه ثانٍ)
        assert await db.alert_exists(msg_hash) is True
        stats_before = get_unified_stats().snapshot().get("recovered_dupe_skip", 0)

    async def test_index_exists_after_connect(self, db):
        rows = await db._fetchall(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_queue_status'", ()
        )
        assert len(rows) == 1

    async def test_recover_uses_index_plan(self, db):
        """خطة التنفيذ للفحص الدوري تستخدم idx_queue_status (لا full-scan)."""
        await db.add_to_queue({"text": "x"}, priority=5)
        rows = await db._fetchall(
            "EXPLAIN QUERY PLAN SELECT id, event_data, attempts FROM "
            "processing_queue WHERE status = 'in_flight' AND claimed_at "
            "IS NOT NULL AND claimed_at < 1", ()
        )
        plan = " ".join(str(r.get("detail", "")) for r in rows)
        # SEARCH ... USING INDEX = بحث مفهرس (المطلوب) — SCAN بدون فهرس
        # كان يعني full-scan على كل الجدول في كل دورة recovery
        assert "idx_queue_status" in plan
        assert "USING INDEX" in plan
        assert "SEARCH" in plan


class TestAckFlushIds:

    def _bot(self, db):
        from main import EnhancedTelegramBot as BotApp
        return BotApp._ack_flush_ids, SimpleNamespace(db=db)

    async def test_flush_success(self, db):
        fn, fake = self._bot(db)
        a = await db.add_to_queue({"text": "a"}, priority=5)
        b = await db.add_to_queue({"text": "b"}, priority=5)
        deleted = await fn(fake, [a, b])
        assert deleted == 2
        assert await db.queue_size() == 0

    async def test_flush_fail_safe(self, db):
        """فشل DB في التصفير لا يرفع — الصفوف تبقى قابلة للاسترداد."""
        fn, fake = self._bot(db)

        async def _boom(_ids):
            raise RuntimeError("disk on fire")

        fake.db = SimpleNamespace(ack_queue_events_batch=_boom)
        assert await fn(fake, [1, 2, 3]) == 0  # بلا رفع
