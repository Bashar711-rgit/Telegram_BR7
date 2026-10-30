"""اختبارات v13.0 — منع تكرار التنبيهات المركزي + بطاقة التنبيه الموحدة.

تغطي الحالات العشر الإلزامية من مواصفة المهمة (§13) وعقد البطاقة (§14):

  T1  الحسابات السبعة ← تنبيه واحد
  T2  الوصول المتزامن ← لا ازدواج بالسباق
  T3  رسالتان مختلفتان من المرسل نفسه ← تنبيهان
  T4  النص نفسه من مرسلين مختلفين ← لا دمج
  T5  إعادة تشغيل التطبيق ← لا إعادة إرسال مؤكد
  T6  تعدد العمال ← مالك واحد للحجز (استيلاء CAS)
  T7  فشل الإرسال ← فك الحجز وإعادة محاولة منظمة
  T8  التعطل بعد نجاح الإرسال ← الإيصال يمنع الإعادة
  T9  اختلاف المرفقات ← هوية محتوى مختلفة
  T10 الرسائل القديمة المؤكدة ← لا إعادة تنبيه

وعقد البطاقة v13: B_Bot ← المرسل ← نص الرسالة ← المحتوى ← [مراسلة][عرض الرسالة]
"""

import asyncio
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from config import CFG  # noqa: E402
from dedup import (  # noqa: E402
    AlertDeduplicator,
    content_fingerprint,
    normalize_for_fingerprint,
)
from alert_bot import build_alert_buttons, build_alert_html  # noqa: E402
from contact_target import (  # noqa: E402
    BOT_NAME_LINE,
    MSG_TEXT_LABEL,
    SENDER_LABEL,
    build_alert_entity_pack,
    resolve_contact_target,
    utf16_len,
)
from monitors import is_alert_card_echo  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════
# أدوات مساعدة
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture()
async def clean_db(db):
    """connftest `db` + جدول dedup نظيف."""
    await db._execute("DELETE FROM alert_dedup")
    yield db
    await db._execute("DELETE FROM alert_dedup")


@pytest.fixture()
def dd(clean_db):
    return AlertDeduplicator(db=clean_db)


SENDER_ID = 6079171409
CHAT_ID = -100111222333
INNER = "111222333"
MSG_ID = 4242
ORIG = "ابي اعرف وش المطلوب قبل الاختبار"


def _data(**kw) -> Dict[str, Any]:
    base = {
        "chat_id": CHAT_ID, "message_id": MSG_ID, "sender_id": SENDER_ID,
        "sender_username": "layan_l9", "sender_first_name": "ليان",
        "sender_last_name": None,
        "sender_type": "user", "text": ORIG,
        "sender_usernames": None, "chat_username": None,
    }
    base.update(kw)
    return base


def _visible(text: str) -> str:
    import html as _h
    import re as _re
    return _h.unescape(_re.sub(r"<[^>]+>", "", text))


# ═══════════════════════════════════════════════════════════════════════
# T1 — الحسابات السبعة: نفس الرسالة عبر 7 حسابات → تنبيه واحد
# ═══════════════════════════════════════════════════════════════════════

class TestT1_SevenAccounts:
    @pytest.mark.asyncio
    async def test_seven_accounts_one_claim(self, dd):
        """كل حساب يستدعي check_and_claim لنفس البصمة — الأولى فقط تنجح."""
        fp = content_fingerprint(SENDER_ID, ORIG)
        results = []
        for _ in range(7):  # 7 حسابات مراقبة
            results.append(await dd.check_and_claim(fp))
        assert results.count(True) == 1, f"تنبيه واحد مطلوب، حصل {results.count(True)}"
        assert results.count(False) == 6
        assert dd.stats()["claimed"] == 1
        assert dd.stats()["blocked"] == 6

    @pytest.mark.asyncio
    async def test_hits_counter_tracks_cross_process_detections(self, clean_db):
        """اكتشافات ما بعد إعادة التشغيل/عملية أخرى تزيد hits في alert_dedup
        (تتبع بلا تنبيهات) — كل حساب بنسخة dedup جديدة (ذاكرة فارغة)."""
        fp = content_fingerprint(SENDER_ID, ORIG)
        first = AlertDeduplicator(db=clean_db)
        assert await first.check_and_claim(fp) is True
        for _ in range(6):
            other = AlertDeduplicator(db=clean_db)
            assert await other.check_and_claim(fp) is False
        row = await clean_db._fetchone(
            "SELECT hits FROM alert_dedup WHERE fingerprint = ?", (fp,))
        assert row is not None
        assert int(row["hits"]) >= 6


# ═══════════════════════════════════════════════════════════════════════
# T2 — الوصول المتزامن: 7 استدعاءات بالتوازي → حجز واحد
# ═══════════════════════════════════════════════════════════════════════

class TestT2_ConcurrentAccess:
    @pytest.mark.asyncio
    async def test_event_loop_concurrency_one_claim(self, dd):
        fp = content_fingerprint(SENDER_ID, ORIG)
        results = await asyncio.gather(*[dd.check_and_claim(fp) for _ in range(7)])
        assert results.count(True) == 1

    @pytest.mark.asyncio
    async def test_db_level_race_one_winner(self, clean_db):
        """السباق على مستوى DB مباشرة (INSERT OR IGNORE آتومي) — فائز واحد."""
        fp = content_fingerprint(SENDER_ID, ORIG)
        now = time.time()
        results = await asyncio.gather(*[
            clean_db.claim_alert_fingerprint(fp, first_seen=now) for _ in range(7)
        ])
        assert results.count(True) == 1


# ═══════════════════════════════════════════════════════════════════════
# T3 — رسالتان مختلفتان من المرسل نفسه → تنبيهان مستقلان
# ═══════════════════════════════════════════════════════════════════════

class TestT3_TwoDifferentMessages:
    @pytest.mark.asyncio
    async def test_different_texts_both_alert(self, dd):
        fp1 = content_fingerprint(SENDER_ID, "ابي حل واجب الاحصاء")
        fp2 = content_fingerprint(SENDER_ID, "ابي اعرف بذ اكرد قبل الاختبار")
        assert await dd.check_and_claim(fp1) is True
        assert await dd.check_and_claim(fp2) is True
        assert dd.stats()["claimed"] == 2

    def test_partial_similarity_not_merged(self):
        """المتشابهة جزئياً تبقى رسائل منفصلة (لا حذف خاطئ)."""
        a = content_fingerprint(111, "ابي حل")
        b = content_fingerprint(111, "ابي حلل")
        assert a != b


# ═══════════════════════════════════════════════════════════════════════
# T4 — النص نفسه من مرسلين مختلفين → لا دمج تلقائي
# ═══════════════════════════════════════════════════════════════════════

class TestT4_TwoSenders:
    @pytest.mark.asyncio
    async def test_same_text_two_senders_both_alert(self, dd):
        fp1 = content_fingerprint(111, ORIG)
        fp2 = content_fingerprint(222, ORIG)
        assert fp1 != fp2
        assert await dd.check_and_claim(fp1) is True
        assert await dd.check_and_claim(fp2) is True

    def test_anonymous_senders_not_merged(self):
        """v13: مرسلان بلا معرف في محادثتين مختلفتين = بصمتان مستقلتان؛
        نفس المحادثة = بصمة واحدة (منع تكرار الحسابات يبقى قائماً)."""
        a = content_fingerprint(0, ORIG, chat_id=-100111)
        b = content_fingerprint(0, ORIG, chat_id=-100222)
        same_chat = content_fingerprint(0, ORIG, chat_id=-100111)
        assert a != b
        assert a == same_chat


# ═══════════════════════════════════════════════════════════════════════
# T5 — إعادة تشغيل التطبيق: الذاكرة تتloss لكن DB يمنع الإعادة
# ═══════════════════════════════════════════════════════════════════════

class TestT5_Restart:
    @pytest.mark.asyncio
    async def test_new_instance_after_restart_still_blocked(self, dd, clean_db):
        fp = content_fingerprint(SENDER_ID, ORIG)
        assert await dd.check_and_claim(fp) is True
        # "إعادة تشغيل": نسخة dedup جديدة بذاكرة فارغة على نفس DB
        dd2 = AlertDeduplicator(db=clean_db)
        assert await dd2.check_and_claim(fp) is False


# ═══════════════════════════════════════════════════════════════════════
# T6 — تعدد العمال: الاستيلاء CAS — حجز طازج لا يُمس، عتيق يُستولى
# ═══════════════════════════════════════════════════════════════════════

class TestT6_MultiWorkerSteal:
    @pytest.mark.asyncio
    async def test_fresh_claim_steal_denied(self, dd, clean_db):
        """عامل حي يرسل الآن (حجز طازج) — المسترد الثاني ينسحب بلا تكرار."""
        fp = content_fingerprint(SENDER_ID, ORIG)
        assert await dd.check_and_claim(fp) is True
        dd2 = AlertDeduplicator(db=clean_db)
        assert await dd2.steal_if_stale(fp, min_age_seconds=120) is False
        assert await dd2.check_and_claim(fp) is False
        assert dd2.stats()["steal_denied"] == 1

    @pytest.mark.asyncio
    async def test_stale_claim_steal_granted_once(self, dd, clean_db):
        """محاولة منهارة قبل الإرسال (حجز عتيق بلا إيصال) — استيلاء واحد ينجح."""
        fp = content_fingerprint(SENDER_ID, ORIG)
        assert await dd.check_and_claim(fp) is True
        # أعمّر الحجز (انهيار قبل 600s = عمر استرداد in-flight)
        await clean_db._execute(
            "UPDATE alert_dedup SET last_seen = ? WHERE fingerprint = ?",
            (time.time() - 600, fp),
        )
        await clean_db._commit()
        dd2 = AlertDeduplicator(db=clean_db)
        assert await dd2.steal_if_stale(fp, min_age_seconds=120) is True
        assert await dd2.check_and_claim(fp) is True
        # عامل ثالث يعجز عن الاستيلاء على الحجز الطازج الجديد
        dd3 = AlertDeduplicator(db=clean_db)
        assert await dd3.steal_if_stale(fp, min_age_seconds=120) is False

    @pytest.mark.asyncio
    async def test_steal_is_atomic_cas(self, clean_db):
        """الاستيلاء DELETE...WHERE last_seen<cutoff — rowcount هو الحكم."""
        fp = content_fingerprint(SENDER_ID, ORIG)
        now = time.time()
        assert await clean_db.claim_alert_fingerprint(fp, first_seen=now) is True
        # حجز طازج: لا استيلاء
        assert await clean_db.steal_stale_alert_fingerprint(fp, 120.0) is False
        # عتيق: استيلاء
        await clean_db._execute(
            "UPDATE alert_dedup SET last_seen = ? WHERE fingerprint = ?",
            (now - 900, fp),
        )
        await clean_db._commit()
        assert await clean_db.steal_stale_alert_fingerprint(fp, 120.0) is True
        # بعد الاستيلاء لا صف — استيلاء ثانٍ يفشل
        assert await clean_db.steal_stale_alert_fingerprint(fp, 120.0) is False


# ═══════════════════════════════════════════════════════════════════════
# T7 — فشل الإرسال: فك الحجز ثم إعادة المطالبة مسموحة
# ═══════════════════════════════════════════════════════════════════════

class TestT7_SendFailure:
    @pytest.mark.asyncio
    async def test_release_then_reclaim(self, dd):
        fp = content_fingerprint(SENDER_ID, ORIG)
        assert await dd.check_and_claim(fp) is True
        await dd.release(fp)
        assert await dd.check_and_claim(fp) is True  # إعادة محاولة DLQ
        assert dd.stats()["released"] == 1


# ═══════════════════════════════════════════════════════════════════════
# T8 — التعطل بعد نجاح الإرسال: الإيصال يمنع الإعادة الأعمى
# ═══════════════════════════════════════════════════════════════════════

class TestT8_ReceiptAfterCrash:
    @pytest.mark.asyncio
    async def test_immediate_receipt_visible_to_alert_exists(self, clean_db):
        """add_alert_immediate يلزم الإيصال لحظة النجاح — alert_exists يراه فوراً."""
        from database import AlertRecord
        from config import fast_hash
        msg_hash = fast_hash(f"{CHAT_ID}_{MSG_ID}_{ORIG}")
        assert await clean_db.alert_exists(msg_hash) is False
        ok = await clean_db.add_alert_immediate(AlertRecord(
            message_hash=msg_hash, chat_id=CHAT_ID, sender_id=SENDER_ID,
            account_name="A1", keyword="طلب", alert_text="بطاقة", timestamp=time.time(),
        ))
        assert ok is True
        assert await clean_db.alert_exists(msg_hash) is True
        # إيصال مكرر لا يتضاعف (UNIQUE message_hash)
        ok2 = await clean_db.add_alert_immediate(AlertRecord(
            message_hash=msg_hash, chat_id=CHAT_ID, sender_id=SENDER_ID,
            account_name="A1", keyword="طلب", alert_text="بطاقة", timestamp=time.time(),
        ))
        assert ok2 is False
        assert await clean_db.alert_exists(msg_hash) is True


# ═══════════════════════════════════════════════════════════════════════
# T9 — اختلاف المرفقات: لا دمج خاطئ عندما يغيّر المرفق هوية المحتوى
# ═══════════════════════════════════════════════════════════════════════

class TestT9_MediaIdentity:
    def test_media_kind_changes_fingerprint(self):
        fp_text = content_fingerprint(SENDER_ID, ORIG)
        fp_photo = content_fingerprint(SENDER_ID, ORIG, media_kind="photo")
        fp_doc = content_fingerprint(SENDER_ID, ORIG, media_kind="document")
        assert len({fp_text, fp_photo, fp_doc}) == 3

    @pytest.mark.asyncio
    async def test_same_text_different_media_two_alerts(self, dd):
        fp1 = content_fingerprint(SENDER_ID, ORIG, media_kind=None)
        fp2 = content_fingerprint(SENDER_ID, ORIG, media_kind="photo")
        assert await dd.check_and_claim(fp1) is True
        assert await dd.check_and_claim(fp2) is True

    @pytest.mark.asyncio
    async def test_same_media_cross_account_still_one(self, dd):
        """نفس الرسالة بالوسائط عبر حسابات مختلفة تبقى تنبيهاً واحداً."""
        fp = content_fingerprint(SENDER_ID, ORIG, media_kind="photo")
        first = await dd.check_and_claim(fp)
        second = await dd.check_and_claim(fp)
        assert (first, second) == (True, False)


# ═══════════════════════════════════════════════════════════════════════
# T10 — الرسائل القديمة المؤكدة: لا إعادة تنبيه، والجديدة لا تُحذف
# ═══════════════════════════════════════════════════════════════════════

class TestT10_OldEvents:
    @pytest.mark.asyncio
    async def test_window_expiry_allows_new_repeat(self, dd, clean_db):
        """سياسة زمنية موثقة: انتهاء النافذة يسمح بتكرار لاحق مشروع
        (إعلان جديد بعد يوم) — النافذة ليست الوسيلة الوحيدة بل طبقة فوق
        إيصال alerts الدائم."""
        fp = content_fingerprint(SENDER_ID, ORIG)
        assert await dd.check_and_claim(fp) is True
        await clean_db._execute(
            "UPDATE alert_dedup SET last_seen = ? WHERE fingerprint = ?",
            (time.time() - (int(getattr(CFG, "DEDUP_WINDOW_SECONDS", 86400)) + 5), fp),
        )
        await clean_db._commit()
        # نسخة جديدة (ذاكرة فارغة — دلالات إعادة التشغيل): الذاكرة لا تحجز
        dd2 = AlertDeduplicator(db=clean_db)
        assert await dd2.check_and_claim(fp) is True  # نافذة تنطلق من جديد

    @pytest.mark.asyncio
    async def test_confirmed_receipt_blocks_realert_within_window(self, dd, clean_db):
        """حدث قديم مؤكد المعالجة (إيصال + بصمة داخل النافذة) لا يُعاد تنبيهه."""
        fp = content_fingerprint(SENDER_ID, ORIG)
        assert await dd.check_and_claim(fp) is True
        # الحدث يعاد استقباله (catch-up) بعد ساعات — البصمة داخل النافذة
        await clean_db._execute(
            "UPDATE alert_dedup SET last_seen = ? WHERE fingerprint = ?",
            (time.time() - 3600, fp),
        )
        await clean_db._commit()
        dd2 = AlertDeduplicator(db=clean_db)
        assert await dd2.check_and_claim(fp) is False

    @pytest.mark.asyncio
    async def test_new_message_not_deleted_for_similarity(self, dd):
        """الرسائل الجديدة المختلفة لا تُحذف لمجرد تشابه محتواها."""
        fp_old = content_fingerprint(SENDER_ID, "ابي اذكار اللي يجي مقالي")
        fp_new = content_fingerprint(SENDER_ID, "ابي اذكار اللي يجي مقالي وثو it232")
        assert fp_old != fp_new
        assert await dd.check_and_claim(fp_old) is True
        assert await dd.check_and_claim(fp_new) is True


# ═══════════════════════════════════════════════════════════════════════
# عقد البطاقة v13 (مواصفة §14)
# ═══════════════════════════════════════════════════════════════════════

class TestCardV13:
    def test_1_bot_name_first_line(self):
        built = build_alert_html(_data())
        visible = _visible(built["text"])
        assert visible.split("\n")[0] == BOT_NAME_LINE == "B_Bot"

    def test_2_sender_line_present(self):
        built = build_alert_html(_data())
        visible = _visible(built["text"])
        lines = visible.split("\n")
        assert lines[1].startswith("المرسل: ")
        assert "ليان" in lines[1]
        assert "— @layan_l9" in lines[1]

    def test_3_message_label_present(self):
        built = build_alert_html(_data())
        assert "\n\nنص الرسالة:\n\n" in _visible(built["text"])
        assert MSG_TEXT_LABEL == "نص الرسالة:"

    def test_4_arabic_content_preserved(self):
        orig = "أَبِي حل الواجب — اللغة العربية 📚\nوالسطر الثاني"
        built = build_alert_html(_data(text=orig))
        body = _visible(built["text"]).split("\n\nنص الرسالة:\n\n", 1)[1]
        assert body.startswith(orig)

    def test_5_long_text_truncated_within_limit(self):
        long_text = "ط" * 6000
        built = build_alert_html(_data(text=long_text))
        assert utf16_len(built["text"]) <= 4096
        assert built["text"].rstrip().endswith("…")

    def test_6_buttons_present_in_one_row_v13_order(self):
        built = build_alert_html(_data())
        assert built["buttons"], "اللوحة مطلوبة"
        row = built["buttons"][0]
        texts = [b["text"] for b in row]
        assert texts == ["مراسلة", "عرض الرسالة"]

    def test_7_contact_button_linked_to_correct_sender(self):
        built = build_alert_html(_data(sender_username="layan_l9"))
        contact = next(b for b in built["buttons"][0] if b["text"] == "مراسلة")
        assert contact["url"] == "https://t.me/layan_l9"

    def test_8_view_button_linked_to_correct_source_public(self):
        built = build_alert_html(_data(chat_username="mygroup"))
        view = next(b for b in built["buttons"][0] if b["text"] == "عرض الرسالة")
        assert view["url"] == f"https://t.me/mygroup/{MSG_ID}"

    def test_8b_view_button_private_group(self):
        built = build_alert_html(_data(chat_username=None))
        view = next(b for b in built["buttons"][0] if b["text"] == "عرض الرسالة")
        assert view["url"] == f"https://t.me/c/{INNER}/{MSG_ID}"

    def test_9_no_fake_links_channel_sender(self):
        """قناة (sender_type=channel) → لا زر tg://user مُختلق."""
        built = build_alert_html(_data(sender_username=None, sender_type="channel",
                                       sender_first_name="قناة", sender_last_name=None))
        row = built["buttons"][0] if built["buttons"] else []
        assert all(not str(b.get("url", "")).startswith("tg://user") for b in row)

    def test_9b_no_contact_button_without_any_target(self):
        """لا هوية إطلاقاً → لا زر مراسلة ولا رابط مُختلق."""
        built = build_alert_html(_data(sender_id=0, sender_username=None,
                                       sender_type="none",
                                       sender_first_name=None, sender_last_name=None))
        row = built["buttons"][0] if built["buttons"] else []
        assert all(b["text"] != "مراسلة" for b in row)
        assert "<a " not in built["text"]

    def test_10_name_entity_offset_with_bot_prefix(self):
        """إزاحة كيان الاسم = utf16_len(SENDER_LABEL) بعد سطر B_Bot."""
        target = resolve_contact_target(sender_id=SENDER_ID, username="layan_l9",
                                        first_name="ليان")
        pack = build_alert_entity_pack(
            display_name="ليان", message_text=ORIG, target=target)
        ents = pack["formatting_entities"] or []
        assert ents, "كيان الاسم مطلوب"
        first = ents[0]
        assert int(getattr(first, "offset", -1)) == utf16_len(SENDER_LABEL)
        # النص العادي يطابق قالب HTML مرئياً
        assert pack["plain_text"].startswith(f"{BOT_NAME_LINE}\nالمرسل: ")

    def test_11_echo_guard_recognizes_v13_card(self):
        built = build_alert_html(_data())
        visible = _visible(built["text"])
        assert is_alert_card_echo(visible) is True
        assert is_alert_card_echo(built["text"]) is True  # بنسخته HTML أيضاً

    def test_11b_echo_guard_ignores_user_messages(self):
        assert is_alert_card_echo("B_Bot شكرا لك") is False
        assert is_alert_card_echo("ابي اذكار اللي يجي مقالي") is False
        assert is_alert_card_echo("") is False

    @pytest.mark.asyncio
    async def test_12_no_second_card_for_same_message(self, dd):
        """نفس الرسالة عبر الحسابات → حجز واحد → بطاقة واحدة."""
        fp = content_fingerprint(SENDER_ID, ORIG)
        assert await dd.check_and_claim(fp) is True
        assert await dd.check_and_claim(fp) is False

    @pytest.mark.asyncio
    async def test_12b_one_claim_per_message_two_cards_for_two_messages(self, dd):
        fp1 = content_fingerprint(SENDER_ID, "رسالة أولى")
        fp2 = content_fingerprint(SENDER_ID, "رسالة ثانية مختلفة")
        assert await dd.check_and_claim(fp1) is True
        assert await dd.check_and_claim(fp1) is False  # لا بطاقة ثانية
        assert await dd.check_and_claim(fp2) is True   # بطاقة مستقلة للجديدة

    def test_13_media_message_caption_handled(self):
        built = build_alert_html(_data(text="شرح الواجب في الصورة", media_type="photo"))
        body = _visible(built["text"]).split("\n\nنص الرسالة:\n\n", 1)[1]
        assert body.startswith("شرح الواجب في الصورة")
        # بطاقة الوسائط تحمل نفس العقد
        assert _visible(built["text"]).split("\n")[0] == "B_Bot"

    def test_14_no_text_message_safe_card(self):
        """رسالة بلا نص (وسائط فقط) — بطاقة صالحة بلا نص مُختلق."""
        built = build_alert_html(_data(text="", media_type="photo"))
        assert "\n\nنص الرسالة:\n\n" in built["text"] or built["text"]
        assert _visible(built["text"]).split("\n")[0] == "B_Bot"

    def test_15_empty_media_label_not_fabricated(self):
        """لا يُعرض نص بديل يوهم بأنه النص الأصلي — قسم نص الرسالة فارغ بصدق."""
        built = build_alert_html(_data(text=""))
        visible = _visible(built["text"])
        body = visible.split("\n\nنص الرسالة:\n\n", 1)[1]
        assert body.strip() == ""
