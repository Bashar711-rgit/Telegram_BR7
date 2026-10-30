#!/usr/bin/env python3
"""اختبارات v12.1.0 — التزامن الصلب + محرك الفلترة الموحد + البطاقة المرجعية.

التغطية:
  A. FilterPolicy — الحدود الشاملة 6..65 على النص المقصوص، صفر-العرض،
     فشل-آمن، إرجاع النص المعتمد (stripped).
  B. التزامن الصلب في DB (SQLite) — claim بلا حذف، ack، استرداد العالق
     (recovered/dead-letter)، حقن _queue_id و_recovered.
  C. إيصال التنبيه alert_exists — أساس قرار مسار المسترد.
  D. صيغة الصورة المرجعية — الاسم و@username كلاهما رابط (أزرق) في قالب
     Bot API + كيان MTProto الثاني.
  E. توحيد القياس — رسالة خامها > 65 ومقصوصها ضمن الحدود لا تُرفض
     "too_long" من الفلتر بعد الآن (الثغرة التي كانت تفقد رسائل).
"""

import time

import pytest

from config import CFG
from database import EnhancedDatabase
from filter_engine import EnhancedFilter
from unified_alerts.filter_policy import FilterPolicy, normalize_for_measure
from unified_alerts.stats import get_unified_stats


# ═══════════════════════════════════════════════════════════════════════
# A. FilterPolicy — السياسة الموحدة
# ═══════════════════════════════════════════════════════════════════════

class TestFilterPolicy:
    def test_lower_boundary_inclusive_6(self):
        v = FilterPolicy.check("ابي حل")  # 6 أحرف بالضبط
        assert v.ok is True
        assert v.length == 6

    def test_below_lower_boundary_5(self):
        v = FilterPolicy.check("ابي ح")  # 5
        assert v.ok is False and v.reason == "too_short"

    def test_upper_boundary_inclusive_65(self):
        t = "ا" * 65
        v = FilterPolicy.check(t)
        assert v.ok is True and v.length == 65

    def test_above_upper_boundary_66(self):
        v = FilterPolicy.check("ا" * 66)
        assert v.ok is False and v.reason == "too_long"

    def test_measurement_on_stripped_text(self):
        raw = "    " + "ا" * 40 + "    "
        v = FilterPolicy.check(raw)
        assert v.ok is True and v.length == 40
        assert v.stripped_text == "ا" * 40

    def test_zero_width_not_counted(self):
        # 6 أحرف حقيقية + محارف صفر-العرض لا تُحصى
        t = "ا\u200bب\u200cي\u200d ح\u200eل\u200f"
        v = FilterPolicy.check(t)
        assert v.ok is True and v.length == 6

    def test_none_and_empty_invalid(self):
        assert FilterPolicy.check(None).ok is False
        assert FilterPolicy.check(None).reason == "invalid_input"
        assert FilterPolicy.check("   ").ok is False
        assert FilterPolicy.check("").ok is False
        # فارغ بعد strip = empty_text (قرار صادق مختلف عن غير-النص)
        assert FilterPolicy.check("").reason == "empty_text"

    def test_stripped_text_is_the_canonical_copy(self):
        raw = "\u200b  ابغى حل واجب  \u200b"
        v = FilterPolicy.check(raw)
        assert v.ok is True
        assert v.stripped_text == "ابغى حل واجب"

    def test_quick_ok_matches_check(self):
        assert FilterPolicy.quick_ok("ابي حل") is True
        assert FilterPolicy.quick_ok("ا" * 66) is False

    def test_snapshot_has_inclusive_flag(self):
        snap = FilterPolicy.snapshot()
        assert snap["inclusive_bounds"] is True
        assert snap["min_message_length"] >= 0
        assert snap["max_message_length"] >= snap["min_message_length"]

    def test_measure_helper(self):
        assert FilterPolicy.measure("  ابي حل  ") == 6
        assert normalize_for_measure("\u200bابي حل\u200b") == "ابي حل"


# ═══════════════════════════════════════════════════════════════════════
# B. التزامن الصلب — claim/ack/recover (SQLite)
# ═══════════════════════════════════════════════════════════════════════

def _mk_event(chat_id=111, message_id=222, text="ابغى احد يحل لي واجب الرياضيات"):
    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "sender_id": 333,
        "text": text,
        "account_name": "ACCOUNT_1",
        "timestamp": time.time(),
        "msg_date_ts": time.time(),
    }


class TestInflightSynchronization:
    @pytest.mark.asyncio
    async def test_claim_does_not_delete_row(self, db: EnhancedDatabase):
        await db.add_to_queue(_mk_event(), priority=7)
        claimed = await db.pop_from_queue()
        assert claimed is not None
        assert "_queue_id" in claimed  # حقن هوية الصف
        # الصف ما زال موجوداً (in_flight) — لم يُحذف عند السحب
        row = await db._fetchone(
            "SELECT status, attempts, claimed_at FROM processing_queue WHERE id = ?",
            (claimed["_queue_id"],),
        )
        assert row is not None
        assert row["status"] == "in_flight"
        assert int(row["attempts"]) == 1
        assert row["claimed_at"] is not None

    @pytest.mark.asyncio
    async def test_claim_skips_inflight_rows(self, db: EnhancedDatabase):
        await db.add_to_queue(_mk_event(message_id=1), priority=7)
        first = await db.pop_from_queue()
        assert first is not None
        # لا يوجد غير صف واحد وهو in_flight → السحب لا يعيده
        assert await db.pop_from_queue() is None

    @pytest.mark.asyncio
    async def test_ack_deletes_row(self, db: EnhancedDatabase):
        await db.add_to_queue(_mk_event(message_id=2), priority=7)
        claimed = await db.pop_from_queue()
        assert await db.ack_queue_event(claimed["_queue_id"]) is True
        row = await db._fetchone(
            "SELECT id FROM processing_queue WHERE id = ?", (claimed["_queue_id"],)
        )
        assert row is None
        # ack مزدوج آمن
        assert await db.ack_queue_event(claimed["_queue_id"]) is False

    @pytest.mark.asyncio
    async def test_stale_inflight_recovered_with_flag(self, db: EnhancedDatabase):
        await db.add_to_queue(_mk_event(message_id=3), priority=7)
        claimed = await db.pop_from_queue()
        # تقديم claimed_at لما قبل الحد — محاكاة انهيار قديم
        await db._execute(
            "UPDATE processing_queue SET claimed_at = ? WHERE id = ?",
            (time.time() - 3600, claimed["_queue_id"]),
        )
        await db._commit()
        result = await db.recover_stale_inflight(max_age_seconds=600)
        assert result["recovered"] == 1
        assert result["dead"] == 0
        row = await db._fetchone(
            "SELECT status, event_data, claimed_at FROM processing_queue WHERE id = ?",
            (claimed["_queue_id"],),
        )
        assert row["status"] == "pending"
        assert row["claimed_at"] is None
        import json
        payload = json.loads(row["event_data"])
        assert payload.get("_recovered") == 1  # الوسم المطلوب لمسار الإعادة

    @pytest.mark.asyncio
    async def test_exhausted_attempts_dead_letter(self, db: EnhancedDatabase):
        await db.add_to_queue(_mk_event(message_id=4), priority=7)
        claimed = await db.pop_from_queue()
        # attempts عند السقف (3) + claimed_at قديم → dead letter بصدق
        await db._execute(
            "UPDATE processing_queue SET claimed_at = ?, attempts = 3 WHERE id = ?",
            (time.time() - 3600, claimed["_queue_id"]),
        )
        await db._commit()
        result = await db.recover_stale_inflight(max_age_seconds=600)
        assert result["dead"] == 1
        assert result["recovered"] == 0
        row = await db._fetchone(
            "SELECT id FROM processing_queue WHERE id = ?", (claimed["_queue_id"],)
        )
        assert row is None  # خرج من الطابور
        dl = await db._fetchone(
            "SELECT error_text, retry_count FROM dead_letters "
            "ORDER BY id DESC LIMIT 1"
        )
        assert dl is not None
        assert "inflight_expired" in dl["error_text"]
        assert int(dl["retry_count"]) == 3

    @pytest.mark.asyncio
    async def test_fresh_inflight_not_recovered(self, db: EnhancedDatabase):
        await db.add_to_queue(_mk_event(message_id=5), priority=7)
        claimed = await db.pop_from_queue()
        result = await db.recover_stale_inflight(max_age_seconds=600)
        assert result["recovered"] == 0 and result["dead"] == 0
        row = await db._fetchone(
            "SELECT status FROM processing_queue WHERE id = ?",
            (claimed["_queue_id"],),
        )
        assert row["status"] == "in_flight"  # عامل حي يعالج — لا لمس

    @pytest.mark.asyncio
    async def test_migration_idempotent_columns(self, db: EnhancedDatabase):
        # connect() شغّل الترحيل؛ إعادة تشغيله لا تفشل ولا تضاعف
        await db._migrate_queue_inflight()
        cols = await db._fetchall("PRAGMA table_info(processing_queue)", ())
        names = {c["name"] for c in cols}
        assert {"status", "claimed_at", "attempts"} <= names


# ═══════════════════════════════════════════════════════════════════════
# C. إيصال التنبيه — alert_exists
# ═══════════════════════════════════════════════════════════════════════

class TestAlertReceipt:
    @pytest.mark.asyncio
    async def test_receipt_after_add_alert(self, db: EnhancedDatabase):
        from database import AlertRecord
        h = "hash-receipt-1"
        assert await db.alert_exists(h) is False
        await db.add_alert(AlertRecord(
            message_hash=h, chat_id=1, sender_id=2,
            account_name="ACCOUNT_1", keyword="طلب",
            alert_text="x", timestamp=time.time(),
        ))
        # add_alert دفعي (writer) — الكاتب متوقف في الاختبار: دفّع يدوياً
        await db._flush()
        assert await db.alert_exists(h) is True
        assert await db.alert_exists("unknown-hash") is False


# ═══════════════════════════════════════════════════════════════════════
# D. صيغة الصورة المرجعية — كلا الاسمين رابطان
# ═══════════════════════════════════════════════════════════════════════

class TestCardBothAnchorsLinked:
    def _build(self, sender_username="l8i18l", display="N+"):
        from alert_bot import build_alert_html
        return build_alert_html(
            {
                "sender_id": 555000111,
                "sender_username": sender_username,
                "sender_display": display,
                "text": "ابي اذاكار اللي يجبي مقالي وشو it232",
                "chat_id": -1001234567890,
                "message_id": 42,
            },
            None,
        )

    def test_username_suffix_is_linked_too(self):
        built = self._build()
        header = built["text"].split("\n")[0]
        # رابطان في سطر الترويسة: الاسم + @username
        assert header.count('<a href="https://t.me/l8i18l">') == 2
        assert ">@l8i18l</a>" in header
        assert "نص الرسالة:" in built["text"]

    def test_plain_text_layout_unchanged(self):
        # البنية الحرفية نفسها: المرسل: {display} — @user (فارغ) نص الرسالة:
        built = self._build()
        import re
        plain = re.sub(r"<[^>]+>", "", built["text"])
        lines = plain.split("\n")
        assert lines[0].startswith("المرسل: ")
        assert "— @l8i18l" in lines[0]
        assert lines[1] == "" and lines[2] == "نص الرسالة:" and lines[3] == ""

    def test_no_username_keeps_plain_suffix(self):
        built = self._build(sender_username=None)
        header = built["text"].split("\n")[0]
        # بلا username: لا ذيل @ — لا اختلاق (عقد v11.6 §3.6)
        assert "— @" not in header

    def test_linked_suffix_helper_rejects_bad_urls(self):
        from contact_target import build_username_suffix_linked
        assert build_username_suffix_linked("user_ok", "https://t.me/user_ok") != ""
        assert build_username_suffix_linked("user_ok", "javascript:alert(1)") == ""
        assert build_username_suffix_linked(None, "https://t.me/x") == ""
        assert build_username_suffix_linked("", "https://t.me/x") == ""

    def test_mtpacto_entity_pack_has_second_entity(self):
        from contact_target import build_alert_entity_pack, resolve_contact_target
        target = resolve_contact_target(
            sender_id=555000111, username="l8i18l", display="N+",
        )
        pack = build_alert_entity_pack(
            "N+",
            "ابي اذاكار اللي يجبي مقالي وشو it232",
            target,
        )
        ents = pack.get("formatting_entities") or []
        text_urls = [e for e in ents if type(e).__name__ == "MessageEntityTextUrl"]
        assert len(text_urls) >= 2  # الاسم + @username
        # كيان @username ملتصق بموضعه الصحيح في النص الخام
        assert any(
            e.offset > 0 and e.length == len("@l8i18l") for e in text_urls
        )


# ═══════════════════════════════════════════════════════════════════════
# E. توحيد القياس — الثغرة التي كانت تفقد رسائل
# ═══════════════════════════════════════════════════════════════════════

class TestUnifiedMeasurement:
    @pytest.mark.asyncio
    async def test_raw_over_65_but_stripped_valid_not_too_long(self):
        """رسالة خامها > 65 (فراغات زائدة) ومقصوصها ضمن الحدود — كانت تُقتل
        "too_long" في الفلتر بعد اجتياز التحقق؛ الآن تُحلل طبيعياً."""
        flt = EnhancedFilter()
        core = "ابغى احد يحل لي واجب الرياضيات ضروري جدا اليوم بسرعة"  # 54 مقصوص
        assert 6 <= len(core) <= CFG.MAX_MESSAGE_LENGTH
        raw = "            " + core + "            "  # 78 خام
        assert len(raw) > CFG.MAX_MESSAGE_LENGTH  # الشرط المسبب للفقد سابقاً
        analysis = await flt.analyze(raw)
        assert "too_long" not in (analysis.get("reasons") or [])
        assert analysis.get("decision") in ("accept", "review", "ignore")
        # والأهم: ليس رفض طول
        assert analysis.get("reason") != "too_long"

    @pytest.mark.asyncio
    async def test_filter_rejects_true_overlength(self):
        flt = EnhancedFilter()
        analysis = await flt.analyze("ا" * 300)
        assert analysis.get("decision") == "ignore"
        assert "too_long" in (analysis.get("reasons") or [])

    def test_stats_keys_present(self):
        snap = get_unified_stats().snapshot()
        for key in (
            "filter_ok", "filter_too_short", "filter_too_long",
            "queue_recovered", "queue_recovered_dead",
            "recovered_resend", "recovered_dupe_skip",
            "inflight_errors", "policy_errors",
        ):
            assert key in snap, f"unified snapshot missing {key}"
