"""v14.8.0 — اختبارات إغلاق دليل الترقية: P6.2 (سجل أسماء المستخدمين)
وP6.3 (أعمدة sources) والحقول الجديدة.

كل سلوك هنا جديد في v14.8.0: الجدول telegram_usernames_history +
record_username_change (لا يكرر نفس الاسم) + أعمدة sources الثلاثة.
تعتمد على بيئة tests/conftest.py الحتمية (fixture db = SQLite كامل المخطط).
"""

import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


# ═══════════════ P6.2: telegram_usernames_history ═══════════════


@pytest.mark.asyncio
class TestUsernameHistory:
    async def test_table_and_index_exist(self, db):
        row = await db._fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='telegram_usernames_history'"
        )
        assert row is not None, "جدول تاريخ الأسماء يجب أن يُنشأ مع المخطط"
        idx = await db._fetchone(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_tuh_username'"
        )
        assert idx is not None, "فهرس username يجب أن يُنشأ"

    async def test_first_username_is_recorded(self, db):
        await db.record_username_change(1001, "alpha_sender", "acc1")
        rows = await db._fetchall(
            "SELECT user_id, username, source_account FROM telegram_usernames_history "
            "WHERE user_id = 1001"
        )
        assert len(rows) == 1
        assert rows[0]["username"] == "alpha_sender"
        assert rows[0]["source_account"] == "acc1"

    async def test_same_username_is_not_duplicated(self, db):
        """الدليل 8.2: record_username_change لا يكرر نفس الاسم."""
        await db.record_username_change(1002, "stable_name", "acc1")
        await db.record_username_change(1002, "stable_name", "acc1")
        await db.record_username_change(1002, "stable_name", "acc2")
        rows = await db._fetchall(
            "SELECT username FROM telegram_usernames_history WHERE user_id = 1002"
        )
        assert len(rows) == 1, "نفس الاسم المتكرر يجب ألا يضيف صفوفاً"

    async def test_username_change_appends_new_row(self, db):
        await db.record_username_change(1003, "old_name", "acc1")
        await db.record_username_change(1003, "new_name", "acc1")
        rows = await db._fetchall(
            "SELECT username FROM telegram_usernames_history "
            "WHERE user_id = 1003 ORDER BY changed_at"
        )
        assert [r["username"] for r in rows] == ["old_name", "new_name"]

    async def test_returning_to_previous_name_is_still_new_row(self, db):
        """آخر قيمة هي المرجع — رجوع لاسم قديم يعني تغييراً جديداً فعلاً."""
        await db.record_username_change(1004, "a_name", "acc1")
        await db.record_username_change(1004, "b_name", "acc1")
        await db.record_username_change(1004, "a_name", "acc1")
        rows = await db._fetchall(
            "SELECT username FROM telegram_usernames_history "
            "WHERE user_id = 1004 ORDER BY changed_at"
        )
        assert [r["username"] for r in rows] == ["a_name", "b_name", "a_name"]

    async def test_invalid_input_is_ignored(self, db):
        await db.record_username_change(0, "x_name", "acc1")
        await db.record_username_change(-5, "y_name", "acc1")
        await db.record_username_change(1005, "", "acc1")
        await db.record_username_change(1005, "   ", "acc1")
        rows = await db._fetchall("SELECT COUNT(*) AS c FROM telegram_usernames_history")
        assert rows[0]["c"] == 0, "مدخلات غير صالحة يجب ألا تُدرج"

    async def test_upsert_hook_records_username(self, db):
        """الخطاف في upsert_sender_contact يسجل الاسم تلقائياً."""
        contact = {
            "sender_id": 2001,
            "username": "hooked_user",
            "first_name": "Hook",
            "owner_account": "acc_main",
        }
        await db.upsert_sender_contact(contact)
        rows = await db._fetchall(
            "SELECT username, source_account FROM telegram_usernames_history "
            "WHERE user_id = 2001"
        )
        assert len(rows) == 1 and rows[0]["username"] == "hooked_user"

    async def test_upsert_hook_without_username_writes_nothing(self, db):
        contact = {"sender_id": 2002, "first_name": "NoU", "username": None}
        await db.upsert_sender_contact(contact)
        rows = await db._fetchall(
            "SELECT COUNT(*) AS c FROM telegram_usernames_history WHERE user_id = 2002"
        )
        assert rows[0]["c"] == 0


# ═══════════════ P6.3: أعمدة sources الجديدة ═══════════════


@pytest.mark.asyncio
class TestSourcesExtraColumns:
    async def test_columns_exist_after_connect(self, db):
        rows = await db._fetchall("PRAGMA table_info(sources)")
        names = {r["name"] for r in rows}
        assert {"chat_type", "member_count", "last_seen_at"} <= names, (
            f"أعمدة sources الناقصة: {names}"
        )

    async def test_insert_with_new_columns_roundtrip(self, db):
        await db._execute(
            "INSERT INTO sources (chat_id, username, title, type, chat_type, member_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (987654321, "src_user", "Src Title", "group", "supergroup", 1520),
        )
        await db._commit()
        row = await db._fetchone(
            "SELECT chat_type, member_count, last_seen_at FROM sources WHERE chat_id = 987654321"
        )
        assert row is not None
        assert row["chat_type"] == "supergroup"
        assert row["member_count"] == 1520
        assert row["last_seen_at"] is None  # لم يُضبط بعد
