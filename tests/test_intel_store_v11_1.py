"""v11.1 — الكيانات المعيارية المنفصلة (طلب المستخدم):

    users → group_memberships → group_messages → message_analysis → alerts
    (+ user_activity)

عقود مختبَرة:
  1. الترحيل ينشئ الجداول الخمسة + أعمدة الإرسال على alerts (idempotent).
  2. users: upsert بسياسة COALESCE (القيمة الجديدة غير-ال-None تفوز،
     None لا يمسح القيمة المحفوظة) + full_name يُشتق تلقائياً.
  3. group_memberships: هوية المجموعة تُحدَّث + الأعلام تتراكم بـOR
     (العضوية مثبتة بإرسال رسالة — is_member=1).
  4. group_messages: إدراج + تحديث تعديل لاحق (is_edited/edit_date).
  5. message_analysis: صف واحد لكل تحليل بكل الحقول الموصولة.
  6. user_activity: عداد تراكمي + هِستوغرام الساعات/الأيام يُدمج.
  7. get_user_activity: نوافذ متدرجة (اليوم/الأسبوع/الشهر) محسوبة من
     group_messages + متوسط يومي مشتق.
  8. mark_alert_dispatched: حالة الإرسال + روابط الأزرار الفعلية تُسجل
     على صف alerts (فصل بيانات التنبيه عن بيانات المستخدم).

كل الدوال فشل-آمنة (لا ترفع أبداً) — الاختبارات تتحقق من العائد True
والبيانات الفعلية في القاعدة.
"""
import time

import pytest

from database import AlertRecord


pytestmark = pytest.mark.asyncio


class TestMigration:
    async def test_tables_created(self, db):
        for table in ("users", "group_memberships", "group_messages",
                      "message_analysis", "user_activity"):
            rows = await db._fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
            assert rows, f"table {table} must exist after migration"

    async def test_alerts_dispatch_columns_added(self, db):
        rows = await db._fetchall("PRAGMA table_info(alerts)", ())
        cols = {r["name"] for r in rows}
        for name in ("alert_status", "alert_sent_at", "alerted_to",
                     "notification_attempts", "notification_error",
                     "button_message_url", "button_user_url"):
            assert name in cols

    async def test_migration_idempotent(self, db):
        assert await db._migrate_intel_entities() is None  # no raise


class TestUsersEntity:
    async def test_insert_and_coalesce_update(self, db):
        assert await db.upsert_tg_user({
            "user_id": 6079171409, "access_hash": 111, "first_name": "هناء",
            "last_name": "العنزي", "username": "hana_a", "phone": "+966500000000",
            "is_bot": False, "is_premium": True, "status": "offline",
        }) is True
        # تحديث جزئي: username فقط — لا يمسح الباقي
        assert await db.upsert_tg_user({
            "user_id": 6079171409, "username": "hana_new",
        }) is True
        row = await db._fetchone("SELECT * FROM users WHERE user_id=?", (6079171409,))
        assert row["username"] == "hana_new"           # الجديد فاز
        assert row["first_name"] == "هناء"             # القديم بقي
        assert row["phone"] == "+966500000000"         # القديم بقي
        assert row["access_hash"] == 111               # القديم بقي
        assert row["is_premium"] == 1                  # القديم بقي
        assert row["full_name"] == "هناء العنزي"       # مُشتق عند الإدراج

    async def test_full_name_derived_from_parts(self, db):
        await db.upsert_tg_user({"user_id": 42, "first_name": "أ", "last_name": "ب"})
        row = await db._fetchone("SELECT full_name FROM users WHERE user_id=42")
        assert row["full_name"] == "أ ب"

    async def test_never_raises_on_garbage(self, db):
        assert await db.upsert_tg_user({}) is True  # user_id 0 — يخزن ولا يرفع


class TestGroupMemberships:
    async def test_insert_and_flag_accumulation(self, db):
        base = {"chat_id": -1001234567890, "user_id": 777, "chat_title": "مجموعة الطلاب",
                "chat_username": "mygroup", "is_member": True}
        assert await db.upsert_group_membership(base) is True
        # تحديث لاحق بلا بيانات مجموعة — لا يمسحها، والعلم يتراكم
        assert await db.upsert_group_membership({
            "chat_id": -1001234567890, "user_id": 777, "is_admin": True,
        }) is True
        row = await db._fetchone(
            "SELECT * FROM group_memberships WHERE chat_id=-1001234567890 AND user_id=777")
        assert row["chat_title"] == "مجموعة الطلاب"   # بقي
        assert row["is_member"] == 1
        assert row["is_admin"] == 1                    # تراكم OR
        assert row["is_banned"] == 0

    async def test_membership_created_by_message(self, db):
        await db.upsert_group_membership({"chat_id": -1001, "user_id": 5})
        row = await db._fetchone("SELECT is_member FROM group_memberships WHERE user_id=5")
        # الإدراج الصريح بلا is_member → 0؛ مسار المعالجة يمرر is_member=True
        assert row["is_member"] == 0


class TestGroupMessages:
    async def test_insert_and_edit_update(self, db):
        msg = {
            "chat_id": -1001234567890, "message_id": 4242, "user_id": 777,
            "message_text": "أبي مساعدة في واجب الاحصاء", "message_date": time.time() - 60,
            "message_link": "https://t.me/c/1234567890/4242",
            "is_reply": True, "reply_to_message_id": 4200,
            "urls": '["https://example.com"]', "mentions": '["@علي"]',
            "hashtags": '["#رياضيات"]', "phone_numbers": '["+966500000000"]',
            "media_type": "photo", "msg_hash": "h_msg_1",
        }
        assert await db.record_group_message(msg) is True
        # تعديل لاحق
        assert await db.record_group_message({
            "chat_id": -1001234567890, "message_id": 4242,
            "message_text": "أبي مساعدة في واجب الاحصاء ضروري",
            "edit_date": time.time(), "is_edited": True,
        }) is True
        row = await db._fetchone(
            "SELECT * FROM group_messages WHERE chat_id=-1001234567890 AND message_id=4242")
        assert row["message_text"].endswith("ضروري")  # عدّل
        assert row["is_edited"] == 1
        assert row["edit_date"] is not None
        assert row["is_reply"] == 1                    # بقي
        assert row["media_type"] == "photo"            # بقي (COALESCE)
        assert row["msg_hash"] == "h_msg_1"

    async def test_extraction_fields_roundtrip(self, db):
        await db.record_group_message({
            "chat_id": -1001, "message_id": 9, "urls": '["https://t.me/x"]',
            "mentions": '["@u"]', "hashtags": '["#h"]', "phone_numbers": '["+966500000001"]',
        })
        row = await db._fetchone("SELECT * FROM group_messages WHERE message_id=9")
        assert row["urls"] == '["https://t.me/x"]'
        assert row["mentions"] == '["@u"]'
        assert row["hashtags"] == '["#h"]'
        assert row["phone_numbers"] == '["+966500000001"]'


class TestMessageAnalysis:
    async def test_full_analysis_row(self, db):
        rec = {
            "chat_id": -1001234567890, "message_id": 4242, "user_id": 777,
            "msg_hash": "h_ana_1", "intent": "طلب مساعدة", "intent_confidence": 0.87,
            "matched_keywords": '["واجب"]', "matched_patterns": '["أبي"]',
            "academic_context": "الاحصاء", "action_score": 0.9, "urgency_score": 1.0,
            "negation_score": 0.0, "spam_score": 0.02, "advertisement_score": 0.0,
            "final_score": 87, "classification": "accept",
            "accepted": True, "rejection_reason": None, "detection_reason": "كلمة: واجب",
            "processing_time_ms": 3.4, "engine_version": "11.1.0", "filter_version": "v13",
        }
        assert await db.record_message_analysis(rec) is True
        row = await db._fetchone("SELECT * FROM message_analysis WHERE msg_hash='h_ana_1'")
        assert row["intent"] == "طلب مساعدة"
        assert row["intent_confidence"] == 0.87
        assert row["urgency_score"] == 1.0
        assert row["classification"] == "accept"
        assert row["accepted"] == 1
        assert row["duplicate"] == 0
        assert row["processing_time_ms"] == 3.4
        assert row["engine_version"] == "11.1.0"

    async def test_rejected_row(self, db):
        await db.record_message_analysis({
            "chat_id": -1001, "message_id": 10, "classification": "ignore",
            "rejected": True, "rejection_reason": "لا كلمة مطابقة",
        })
        row = await db._fetchone("SELECT * FROM message_analysis WHERE message_id=10")
        assert row["rejected"] == 1 and row["accepted"] == 0


class TestUserActivity:
    async def test_counter_and_histograms(self, db):
        assert await db.record_user_activity(777, -1001, 4242, time.time() - 30) is True
        assert await db.record_user_activity(777, -1001, 4243, time.time()) is True
        row = await db._fetchone(
            "SELECT * FROM user_activity WHERE user_id=777 AND chat_id=-1001")
        assert row["total_messages"] == 2
        assert row["first_message_id"] == 4242
        assert row["last_message_id"] == 4243
        hours = __import__("json").loads(row["activity_hours"])
        days = __import__("json").loads(row["activity_days"])
        assert sum(hours.values()) == 2
        assert sum(days.values()) == 2

    async def test_get_user_activity_windows(self, db):
        now = time.time()
        await db.record_group_message({"chat_id": -1001, "user_id": 88, "message_id": 1, "message_date": now - 3600})
        await db.record_group_message({"chat_id": -1001, "user_id": 88, "message_id": 2, "message_date": now - 3 * 86400})
        await db.record_group_message({"chat_id": -1001, "user_id": 88, "message_id": 3, "message_date": now - 20 * 86400})
        await db.record_user_activity(88, -1001, 3, now)
        snap = await db.get_user_activity(88, -1001)
        assert snap["total_messages"] == 1          # من user_activity
        assert snap["messages_today"] == 1          # نافذة اليوم من group_messages
        assert snap["messages_week"] == 2
        assert snap["messages_month"] == 3
        assert snap["average_messages_per_day"] is not None
        assert snap["first_message_id"] == 3 or snap["last_message_id"] == 3

    async def test_get_activity_empty_is_honest(self, db):
        snap = await db.get_user_activity(999, -1001)
        assert snap["total_messages"] == 0
        assert snap["messages_today"] == 0
        assert snap["average_messages_per_day"] is None


class TestAlertDispatchLog:
    async def test_mark_dispatched_on_alert_row(self, db):
        await db.add_alert(AlertRecord(
            message_hash="h_alert_1", chat_id=-1001234567890, sender_id=777,
            account_name="MAIN", keyword="واجب", alert_text="نص", timestamp=time.time(),
        ))
        await db._flush()  # add_alert is batched — flush for the test
        assert await db.mark_alert_dispatched(
            "h_alert_1", status="sent", alerted_to="-1001234567890",
            button_message_url="https://t.me/c/1234567890/4242",
            button_user_url="tg://user?id=777",
        ) is True
        row = await db._fetchone("SELECT * FROM alerts WHERE message_hash='h_alert_1'")
        assert row["alert_status"] == "sent"
        assert row["alert_sent_at"] is not None
        assert row["alerted_to"] == "-1001234567890"
        assert row["notification_attempts"] == 1
        assert row["button_message_url"] == "https://t.me/c/1234567890/4242"
        assert row["button_user_url"] == "tg://user?id=777"

    async def test_failed_dispatch_records_error(self, db):
        await db.add_alert(AlertRecord(
            message_hash="h_alert_2", chat_id=-1001, sender_id=5,
            account_name="MAIN", keyword="ك", alert_text="نص", timestamp=time.time(),
        ))
        await db._flush()  # add_alert is batched — flush for the test
        await db.mark_alert_dispatched("h_alert_2", status="failed",
                                       error="bot_api_400:BUTTON_URL_INVALID")
        row = await db._fetchone("SELECT * FROM alerts WHERE message_hash='h_alert_2'")
        assert row["alert_status"] == "failed"
        assert "BUTTON_URL_INVALID" in row["notification_error"]
        assert row["alert_sent_at"] is None  # لم يُرسل → لا زمن إرسال

    async def test_missing_alert_row_is_noop(self, db):
        assert await db.mark_alert_dispatched("nonexistent_hash") is True  # لا يرفع
