"""v14.2.0-dedup (rebased on v14.5.2) — اختبارات حارس ازدواج الطابور.

يغطي:
  1. إدراج نفس الرسالة (chat_id, message_id) مرتين → نفس الصف (بلا ازدواج).
  2. رسائل مختلفة → صفوف مختلفة.
  3. بعد ack (حذف الصف النشط) → إعادة إدراج نفس الرسالة مسموحة (صف جديد).
  4. حدث بلا معرفين معاً → بلا dedup (يُدرج دائماً كصف جديد).
  5. الفهرس الفريد الجزئي موجود فعلاً بعد connect.
  6. عداد queue_dedup_hits يُحتسب عند اصطدام الحارس.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def test_duplicate_message_returns_same_row(db):
    """نفس الرسالة مرتين → نفس id (لا صفوف مكررة)."""
    first = await db.add_to_queue({"chat_id": 100, "message_id": 7}, priority=5)
    assert first > 0
    second = await db.add_to_queue({"chat_id": 100, "message_id": 7}, priority=5)
    assert second == first
    assert await db.queue_size() == 1
    assert db.stats.get("queue_dedup_hits", 0) == 1


async def test_different_messages_get_different_rows(db):
    """رسائل مختلفة → صفوف مختلفة."""
    a = await db.add_to_queue({"chat_id": 100, "message_id": 7}, priority=5)
    b = await db.add_to_queue({"chat_id": 100, "message_id": 8}, priority=5)
    c = await db.add_to_queue({"chat_id": 200, "message_id": 7}, priority=5)
    assert len({a, b, c}) == 3
    assert await db.queue_size() == 3


async def test_reinsert_allowed_after_ack(db):
    """بعد ack (حذف الصف النشط) يُسمح بإعادة إدراج نفس الرسالة."""
    first = await db.add_to_queue({"chat_id": 300, "message_id": 42}, priority=5)
    assert first > 0
    assert await db.ack_queue_event(first) is True
    assert await db.queue_size() == 0
    again = await db.add_to_queue({"chat_id": 300, "message_id": 42}, priority=5)
    assert again > 0
    assert await db.queue_size() == 1


async def test_events_without_full_identity_not_deduped(db):
    """حدث بلا chat_id أو message_id → بلا حماية ازدواج (كما سابقاً)."""
    a = await db.add_to_queue({"foo": "bar"}, priority=5)
    b = await db.add_to_queue({"foo": "bar"}, priority=5)
    assert a > 0 and b > 0 and a != b
    assert await db.queue_size() == 2


async def test_unique_partial_index_exists(db):
    """الفهرس الفريد الجزئي uq_queue_active_dedup موجود بعد connect."""
    rows = await db._fetchall(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_queue_active_dedup'",
        (),
    )
    assert len(rows) == 1


async def test_inflight_row_still_blocks_duplicate(db):
    """صف محجوز in_flight (قيد المعالجة) يمنع ازدواج نفس الرسالة أيضاً."""
    first = await db.add_to_queue({"chat_id": 400, "message_id": 99}, priority=5)
    claimed = await db.pop_from_queue()
    assert claimed is not None
    dup = await db.add_to_queue({"chat_id": 400, "message_id": 99}, priority=5)
    assert dup == first


async def test_legacy_duplicate_rows_cleaned_on_connect(db):
    """جدول قديم ملوّث بالتكرار: _ensure_queue_dedup_index ينظّف التكرار
    النشط (يُبقي الأحدث) وينشئ الفهرس الفريد بنجاح."""
    # حذف الفهرس (أنشأه connect) ومحاكاة جدول قديم بثلاثة صفوف نشطة مكررة.
    await db._execute("DROP INDEX IF EXISTS uq_queue_active_dedup")
    for _ in range(3):
        await db._execute(
            "INSERT INTO processing_queue (event_data, priority, dedup_key) "
            "VALUES (?, ?, ?)",
            ('{"chat_id": 1, "message_id": 1}', 5, "chat:1:msg:1"),
        )
    await db._commit()
    rows = await db._fetchall("SELECT COUNT(*) AS cnt FROM processing_queue", ())
    assert rows[0]["cnt"] == 3

    await db._ensure_queue_dedup_index()
    rows = await db._fetchall("SELECT COUNT(*) AS cnt FROM processing_queue", ())
    assert rows[0]["cnt"] == 1
    rows = await db._fetchall(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name='uq_queue_active_dedup'",
        (),
    )
    assert len(rows) == 1
