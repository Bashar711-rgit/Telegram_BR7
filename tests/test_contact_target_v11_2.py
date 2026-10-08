"""v11.2 — نظام مراسلة المرسل (طلب المستخدم — المهمة الكاملة).

المواصفة: «كل تنبيه يعرض اسم المرسل قابلًا للنقر + زر 📩 مراسلة المرسل،
بلا أي رابط خام يظهر للمستخدم النهائي».

المبادئ المُختبرة هنا (أرقام المواصفة):
  #4  username → https://t.me/{u} (بلا @)
  #5  user_id → tg://user?id={id} — fallback أساسي (username ليس شرطاً)
  #6  openmessage يُحتفظ به داخليًا فقط — لا يُختار أبدًا
  #7  ContactTarget — التجريد الوحيد
  #8  Resolver مركزي واحد (resolve_contact_target) — بلا منطق روابط في ملفات أخرى
  #10/#11 الكيان كاش — CACHE HIT بلا API request / MISS → resolve → كاش
  #12 multi-account: الدمج حسب user_id
  #14 الاسم الظاهر ≠ الرابط (كيانات داخلية)
  #15 زر مراسلة يعمل بلا username
  #16 الاسم والزر من نفس selected_url
  #17 أولوية الاسم: أول+أخير ← أول ← username ← fallback (بلا user_id ظاهر)
  #18 قناة/مجهول/محذوف → لا اختلاق tg://user
  #20 سلسلة التحقق قبل الإرسال
  #21 فشل الصورة لا يكسر شيئًا (avatar = optional)
  #22 لوج [SENDER] محصور بلا بيانات حساسة
  #23 metrics حصورة
  #24 بلا I/O في الحل المركزي (pure-sync)

الحالات المطلوبة (اختبارات المواصفة العشرة) في TestSpecTenCases.
كل تفاعلات الشبكة مُزيفة — لا نداءات حقيقية إطلاقًا.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from alert_bot import AlertBot, build_alert_buttons, build_alert_html
from contact_target import (
    build_alert_entity_pack,
    build_display_name,
    get_contact_metrics_snapshot,
    log_sender_resolution,
    metric_inc,
    resolve_contact_target,
    utf16_len,
    validate_contact_target,
)

SENDER_ID = 5601276336
SENDER_ID_2 = 1630216165
CHAT_ID = -100111222333
MSG_ID = 4242


def _data(**kw) -> Dict[str, Any]:
    base = {
        "chat_id": CHAT_ID, "message_id": MSG_ID, "sender_id": SENDER_ID,
        "sender_username": None, "sender_first_name": "نواف", "sender_last_name": "محمد",
        "sender_type": "user", "sender_usernames": None, "text": "نص الرسالة الأصلي",
        "account_name": "Account 1",
    }
    base.update(kw)
    return base


# ═══════════════════════════════════════════════════════════════════════
# ContactTarget — التجريد والمحلل المركزي (spec #4–#8, #17, #18)
# ═══════════════════════════════════════════════════════════════════════

class TestContactTargetCore:
    def test_username_url_strips_at(self):
        """spec #4: @iesk1 → https://t.me/iesk1 (الـ@ تُزال)."""
        t = resolve_contact_target(sender_id=123456789, username="@iesk1")
        assert t.username == "iesk1"
        assert t.username_url == "https://t.me/iesk1"
        assert t.user_id_url == "tg://user?id=123456789"
        assert t.openmessage_url == "tg://openmessage?user_id=123456789"
        # الاختيار: username يفوز
        assert t.selected_url == "https://t.me/iesk1"
        assert t.target_type == "username"
        assert t.resolution_status == "resolved"

    def test_user_id_fallback_primary(self):
        """spec #5: بلا username → tg://user?id هو الـfallback الأساسي."""
        t = resolve_contact_target(sender_id=SENDER_ID)
        assert t.username is None
        assert t.user_id_url == f"tg://user?id={SENDER_ID}"
        assert t.selected_url == f"tg://user?id={SENDER_ID}"
        assert t.target_type == "user_id"
        assert t.reachable is True

    def test_openmessage_never_selected(self):
        """spec #6: openmessage يُحتفظ به داخليًا فقط — لا يُختار أبدًا."""
        t = resolve_contact_target(sender_id=SENDER_ID)
        assert t.openmessage_url  # محفوظ داخليًا
        assert t.target_type != "openmessage"
        assert t.selected_url != t.openmessage_url

    def test_sender_type_none_with_sid_is_assumed_user(self):
        """الحالة الحرجة: كيان غائب (sender_type=none) مع sender_id موجودة
        → assumed_user — إسقاط tg://user هنا يعيد الشكوى الأصلية."""
        t = resolve_contact_target(sender_id=SENDER_ID, sender_type="none")
        assert t.sender_type == "assumed_user"
        assert t.user_id_url == f"tg://user?id={SENDER_ID}"
        assert t.reachable is True

    def test_channel_never_fabricates_user_id(self):
        """spec #18: قناة → لا tg://user إطلاقًا (بلا اختلاق معلومات)."""
        t = resolve_contact_target(sender_id=555, sender_type="channel")
        assert t.user_id_url is None
        assert t.selected_url is None
        assert t.resolution_status == "partial"
        assert "channel_sender_no_user_id" in t.reasons

    def test_deleted_sender_no_target(self):
        """spec #18: حساب محذوف → لا هدف وهمي."""
        t = resolve_contact_target(sender_id=SENDER_ID, is_deleted="true")
        assert t.sender_type == "deleted"
        assert t.selected_url is None

    def test_bot_type_resolves_via_user_id(self):
        """البوتات مستخدمون — tg://user?id يعمل معها."""
        t = resolve_contact_target(sender_id=SENDER_ID, is_bot=True)
        assert t.sender_type == "bot"
        assert t.user_id_url == f"tg://user?id={SENDER_ID}"

    def test_negative_sender_id_rejected(self):
        """sender_id سالب = كيان محادثة وليس مستخدمًا."""
        t = resolve_contact_target(sender_id=-1001234)
        assert t.user_id == 0
        assert t.selected_url is None

    def test_multiple_active_usernames_picked(self):
        """usernames النشطة تُستخدم عندما username الأساسي مفقود."""
        ru = [SimpleNamespace(username="alt_name1", active=True)]
        t = resolve_contact_target(sender_id=SENDER_ID, usernames=ru)
        assert t.username == "alt_name1"
        assert t.username_url == "https://t.me/alt_name1"

    def test_display_name_priority(self):
        """spec #17: أول+أخير ← أول ← username ← fallback؛ بلا user_id ظاهر."""
        assert build_display_name("نواف", "محمد", "iesk1", SENDER_ID) == "نواف محمد"
        assert build_display_name("نواف", None, "iesk1", SENDER_ID) == "نواف"
        assert build_display_name(None, None, "iesk1", SENDER_ID) == "iesk1"
        # user_id لا يظهر مع أي اسم معروف
        assert "5601276336" not in build_display_name("نواف", None, "iesk1", SENDER_ID)

    def test_resolution_is_pure_sync_no_io(self):
        """spec #24: الحل المركزي بلا أي I/O — دالة متزامنة تنتهي فورًا."""
        import time
        started = time.perf_counter()
        for _ in range(200):
            resolve_contact_target(sender_id=SENDER_ID, username="iesk1",
                                   first_name="نواف", last_name="محمد")
        # 200 حل متزامن أقل من ثانية (بلا شبكة — لو توجد I/O لتوقف كثيراً)
        assert (time.perf_counter() - started) < 1.0


# ═══════════════════════════════════════════════════════════════════════
# الاسم القابل للنقر + الكيانات (spec #14) — النص الظاهر ≠ الرابط
# ═══════════════════════════════════════════════════════════════════════

class TestClickableName:
    def test_html_name_clickable_no_raw_url_visible(self):
        """spec #3/#14: المستخدم يرى «👤: نواف» فقط — الرابط داخل href."""
        built = build_alert_html(_data())
        # السطر الظاهر: الاسم فقط بين وسوم الرابط
        assert built["text"].startswith("👤: <a href=\"tg://user?id=5601276336\">نواف محمد</a>\n")
        # لا رابط خام خارج الوسوم: بعد إزالة قيم href لا يبقى أي tg:// أو t.me
        import re as _re
        visible = _re.sub(r'href="[^"]*"', "", built["text"])
        assert "tg://" not in visible
        assert "https://t.me" not in visible

    def test_both_targets_share_selected_url(self):
        """spec #16: الاسم والزر من نفس ContactTarget.selected_url حصراً."""
        built = build_alert_html(_data())
        t = built["contact_target"]
        name_url = built["text"].split('href="')[1].split('"')[0]
        button_url = built["buttons"][0][0]["url"]
        assert name_url == button_url == t.selected_url

    def test_entity_pack_utf16_offsets(self):
        """الكيانات تحسب offset/length بوحدات UTF-16 (عربي + إيموجي)."""
        t = resolve_contact_target(sender_id=SENDER_ID)
        pack = build_alert_entity_pack("نواف محمد", "نص الرسالة", t)
        assert pack["clickable"] is True
        ents = pack["formatting_entities"]
        kinds = {type(e).__name__: e for e in ents}
        name_ent = kinds["MessageEntityTextUrl"]
        assert name_ent.offset == utf16_len("👤: ") == 4
        assert name_ent.length == utf16_len("نواف محمد")
        assert name_ent.url == f"tg://user?id={SENDER_ID}"
        # القالب نفسه
        assert pack["plain_text"].startswith("👤: نواف محمد\n\n💬:\nنص الرسالة")

    def test_entity_pack_without_target_plain(self):
        """بلا هدف → نص عادي بلا كيان رابط (فشل-آمن)."""
        pack = build_alert_entity_pack("مستخدم", "نص", None)
        assert pack["clickable"] is False
        assert pack["formatting_entities"] is None or all(
            type(e).__name__ != "MessageEntityTextUrl"
            for e in (pack["formatting_entities"] or [])
        )

    def test_entity_pack_rule_tag_appended(self):
        pack = build_alert_entity_pack("نواف", "نص",
                                       resolve_contact_target(sender_id=SENDER_ID),
                                       rule_tag="واجب فيزياء")
        assert pack["plain_text"].endswith("\n\n🏷 قاعدة: واجب فيزياء")

    def test_openmessage_kept_internally_in_pack(self):
        """spec #6: openmessage يبقى داخل الهدف — لا يظهر في النص ولا الكيانات."""
        t = resolve_contact_target(sender_id=SENDER_ID)
        pack = build_alert_entity_pack("نواف", "نص", t)
        assert t.openmessage_url == f"tg://openmessage?user_id={SENDER_ID}"
        assert "openmessage" not in pack["plain_text"]


# ═══════════════════════════════════════════════════════════════════════
# زر مراسلة المرسل (spec #15/#16) — username ليس شرطًا
# ═══════════════════════════════════════════════════════════════════════

class TestContactButton:
    def test_button_without_username_via_user_id(self):
        """spec #15: username=None لكن user_id → زر tg://user?id يعمل."""
        built = build_alert_html(_data())
        row = built["buttons"][0]
        assert {"text": "مراسلة", "url": f"tg://user?id={SENDER_ID}"} in row

    def test_button_with_username(self):
        built = build_alert_html(_data(sender_username="@iesk1"))
        row = built["buttons"][0]
        assert {"text": "مراسلة", "url": "https://t.me/iesk1"} in row

    def test_channel_gets_no_contact_button(self):
        """spec #18: قناة بلا username → لا زر مراسلة (لا روابط وهمية)."""
        built = build_alert_html(_data(sender_id=555, sender_type="channel"))
        texts = [b["text"] for row in built["buttons"] for b in row]
        assert "مراسلة" not in texts
        assert built["contact_method"] == "text_only"

    def test_contact_url_override_wins(self):
        """contact_url من ContactTarget يتفوق على بناء الرابط من id."""
        row = build_alert_buttons(sender_id=SENDER_ID,
                                  contact_url="https://t.me/merged_name")
        assert row[0][0]["url"] == "https://t.me/merged_name"

    def test_include_user_button_false_drops_only_contact(self):
        """بعد رفض tg://user (خصوصية): زر المراسلة فقط يُسقط."""
        rows = build_alert_buttons(sender_id=SENDER_ID,
                                   msg_link="https://t.me/g/1",
                                   include_user_button=False)
        assert [b["text"] for b in rows[0]] == ["عرض الرسالة"]

    def test_sender_mention_v2_contract_preserved(self):
        """قيم contact_method الثلاثة القديمة محفوظة (عقد v10.7)."""
        assert build_alert_html(_data(sender_username="iesk1"))["contact_method"] == "username"
        assert build_alert_html(_data())["contact_method"] == "mention_button"
        assert build_alert_html(_data(sender_id=0, sender_type="none"))["contact_method"] == "text_only"


# ═══════════════════════════════════════════════════════════════════════
# AlertBot.send — المسار الإنتاجي (spec #15/#26)
# ═══════════════════════════════════════════════════════════════════════

class _FakeAlertBot(AlertBot):
    """Bot API مزيف — لا شبكة."""

    def __init__(self, responses):
        super().__init__(token="123:fake", chat_id=-100999888777)
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    async def _post(self, method: str, payload: Dict[str, Any]):
        self.calls.append({"method": method, "payload": payload})
        if self._responses:
            return self._responses.pop(0)
        return 200, {"ok": True}


class TestAlertBotSend:
    @pytest.mark.asyncio
    async def test_send_username_method(self):
        bot = _FakeAlertBot([])
        ok, method, reason = await bot.send(_data(sender_username="iesk1"))
        assert ok is True and method == "username" and reason == ""
        sent = bot.calls[0]["payload"]
        assert sent["parse_mode"] == "HTML"
        # لا رابط خام في النص الظاهر (داخل href فقط)
        assert sent["text"].startswith("👤: <a href=\"https://t.me/iesk1\">")

    @pytest.mark.asyncio
    async def test_send_user_id_button_without_username(self):
        """spec #15: بلا username → زر مراسلة tg://user مُقبل + method=mention_button."""
        bot = _FakeAlertBot([])
        ok, method, _ = await bot.send(_data())
        assert ok is True and method == "mention_button"
        row = bot.calls[0]["payload"]["reply_markup"]["inline_keyboard"][0]
        assert {"text": "مراسلة", "url": f"tg://user?id={SENDER_ID}"} in row

    @pytest.mark.asyncio
    async def test_privacy_rejection_retries_without_contact_only(self):
        """BUTTON_USER_PRIVACY_RESTRICTED → إعادة بلا زر مراسلة، النص يبقى."""
        bot = _FakeAlertBot([
            (400, {"ok": False, "description": "Bad Request: BUTTON_USER_PRIVACY_RESTRICTED"}),
            (200, {"ok": True}),
        ])
        ok, method, _ = await bot.send(_data())
        assert ok is True and method == "text_only"
        assert len(bot.calls) == 2
        assert "reply_markup" in bot.calls[0]["payload"]
        kb = bot.calls[1]["payload"].get("reply_markup", {}).get("inline_keyboard", [[]])[0]
        assert all(b["text"] != "مراسلة" for b in kb)

    @pytest.mark.asyncio
    async def test_client_fallback_carries_entity_pack(self):
        """السبب الجذري: الطبقة الاحتياطية (MTProto) تمرر كيانات جاهزة عبر
        formatting_entities — tg://user?id لا يُحذف صامتًا بعد الآن."""
        sent: List[Any] = []

        class _Live:
            async def send_buttons(self, text, buttons, target=None, **kw):
                sent.append({"text": text, "buttons": buttons, "kwargs": kw})
                return True

        bot = _FakeAlertBot([(500, {"ok": False, "description": "Bad Gateway"})])
        bot.set_client(_Live())
        ok, method, _ = await bot.send(_data())
        assert ok is True and method == "mention_button"
        call = sent[0]
        ents = call["kwargs"].get("formatting_entities")
        assert ents, "الكيانات مطلوبة في المسار الاحتياطي"
        assert any(
            type(e).__name__ == "MessageEntityTextUrl"
            and getattr(e, "url", "") == f"tg://user?id={SENDER_ID}"
            for e in ents
        )
        # النص الظاهر في الطبقة الاحتياطية يطابق القالب (بلا وسوم HTML)
        assert call["text"].startswith("👤: نواف محمد\n\n💬:\n")

    @pytest.mark.asyncio
    async def test_no_token_safe_fallback(self):
        """spec #26: بلا توكن → ok=False — المسار البديل يعمل بلا أعطال."""
        bot = AlertBot(token=None, chat_id=0)
        ok, method, reason = await bot.send(_data())
        assert ok is False and reason == "no_alert_bot_token"

    @pytest.mark.asyncio
    async def test_alert_never_lost_on_avatar_failure(self):
        """spec #21: الصورة optional — فشلها لا يمنع الاسم ولا الزر."""
        built = build_alert_html(_data(sender_photo_available=False))
        assert built["text"].startswith("👤: <a href=\"tg://user?id=5601276336\">نواف محمد</a>")
        assert built["buttons"][0][0]["text"] == "مراسلة"


# ═══════════════════════════════════════════════════════════════════════
# الكيان كاش — SenderResolver (spec #10/#11/#24)
# ═══════════════════════════════════════════════════════════════════════

class _FakeTelegramClient:
    """عميل تيليجرام مزيف — يعدّ استدعاءات get_entity."""

    def __init__(self, entity):
        self._entity = entity
        self.get_entity_calls = 0

    async def get_entity(self, ref, **kw):
        self.get_entity_calls += 1
        await asyncio.sleep(0)
        return self._entity


def _user_entity(uid: int, username: Optional[str] = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uid, username=username, access_hash=987654321,
        first_name="نواف", last_name="محمد", phone=None,
        bot=False, deleted=False, min=False, photo=None,
    )


class TestEntityCache:
    @pytest.mark.asyncio
    async def test_cache_hit_no_api_request(self):
        """اختبار المواصفة 4: مستخدم في الكاش → صفر API request."""
        from sender_resolver import SenderResolver
        resolver = SenderResolver(entity_cache_size=100, entity_cache_ttl=900)
        client = _FakeTelegramClient(_user_entity(SENDER_ID))

        r1 = await resolver.resolve_entity(client, SENDER_ID)
        assert r1.ok and r1.method != "entity_cache"
        n_calls_after_first = client.get_entity_calls

        for _ in range(5):
            r = await resolver.resolve_entity(client, SENDER_ID)
            assert r.ok and r.method == "entity_cache"
        assert client.get_entity_calls == n_calls_after_first  # صفر نداءات إضافية

    @pytest.mark.asyncio
    async def test_cache_miss_resolves_then_caches(self):
        """اختبار المواصفة 5: MISS → resolve → كاش → الطلب التالي HIT."""
        from sender_resolver import SenderResolver
        resolver = SenderResolver(entity_cache_size=100, entity_cache_ttl=900)
        client = _FakeTelegramClient(_user_entity(SENDER_ID_2, "iesk1"))

        r1 = await resolver.resolve_entity(client, SENDER_ID_2)
        assert r1.ok
        assert client.get_entity_calls == 1
        r2 = await resolver.resolve_entity(client, SENDER_ID_2)
        assert r2.ok and r2.method == "entity_cache"
        assert client.get_entity_calls == 1

    @pytest.mark.asyncio
    async def test_inflight_dedup_burst(self):
        """spec #24: 4 رسائل متتالية من نفس المستخدم → حل واحد."""
        from sender_resolver import SenderResolver
        resolver = SenderResolver(entity_cache_size=100, entity_cache_ttl=900)
        client = _FakeTelegramClient(_user_entity(SENDER_ID))
        results = await asyncio.gather(*[
            resolver.resolve_entity(client, SENDER_ID) for _ in range(4)
        ])
        assert all(r.ok for r in results)
        assert client.get_entity_calls <= 2  # واحد فعلي + هامش العرقلة

    @pytest.mark.asyncio
    async def test_cached_username_cross_account_safe(self):
        """username حقيقة عالمية — تُقرأ من الكاش المشترك للروابط فقط."""
        from sender_resolver import sender_intel
        sender_intel._entity_cache[SENDER_ID] = _user_entity(SENDER_ID, "iesk1")
        try:
            assert sender_intel.cached_username(SENDER_ID) == "iesk1"
        finally:
            sender_intel._entity_cache.pop(SENDER_ID, None)


# ═══════════════════════════════════════════════════════════════════════
# Multi-account merge حسب user_id (spec #12) — عبر DB (COALESCE)
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture()
async def db():
    from database import EnhancedDatabase
    d = EnhancedDatabase()
    await d.connect()
    try:
        yield d
    finally:
        await d.close()


class TestMultiAccountMerge:
    @pytest.mark.asyncio
    async def test_two_accounts_merge_by_user_id(self, db):
        """اختبار المواصفة 6: حسابان يعرّفان نفس user_id → صف واحد مدموج."""
        await db.upsert_sender_contact({
            "sender_id": SENDER_ID, "username": None,
            "first_name": "نواف", "last_name": None,
            "owner_account": "Account 1",
        })
        await db.upsert_sender_contact({
            "sender_id": SENDER_ID, "username": "iesk1",
            "first_name": "نواف", "last_name": "محمد",
            "owner_account": "Account 2",
        })
        await db._flush()
        row = await db.get_sender_contact(SENDER_ID)
        assert row is not None and row["username"] == "iesk1"
        # الهدف المركزي يستخدم المدموج
        t = resolve_contact_target(sender_id=SENDER_ID, username=row["username"],
                                   first_name=row["first_name"], last_name=row["last_name"])
        assert t.username_url == "https://t.me/iesk1"
        assert t.display_name == "نواف محمد"

    @pytest.mark.asyncio
    async def test_enrich_missing_sender_from_db(self, db):
        """spec #13: معلومات المرسل لا تضيع — الـworker يكمل من DB."""
        from monitors import EnhancedAccountMonitor
        await db.upsert_sender_contact({
            "sender_id": SENDER_ID, "username": "iesk1",
            "first_name": "نواف", "last_name": "محمد",
            "owner_account": "Account 1",
        })
        await db._flush()
        account = {"id": 1, "prefix": "ACCOUNT_1", "name": "Account 1",
                   "session": "s1", "api_id": 1, "api_hash": "h",
                   "phone": "+966500000000", "priority": 1}
        m = EnhancedAccountMonitor(account, db, None)
        data = {"sender_id": SENDER_ID, "sender_username": None,
                "sender_first_name": None, "sender_last_name": None,
                "sender_access_hash": None}
        await m._enrich_sender_from_db(data)
        assert data["sender_username"] == "iesk1"
        assert data["sender_first_name"] == "نواف"
        t = resolve_contact_target(sender_id=SENDER_ID, username=data["sender_username"],
                                   first_name=data["sender_first_name"],
                                   last_name=data["sender_last_name"])
        assert t.reachable and t.display_name == "نواف محمد"


# ═══════════════════════════════════════════════════════════════════════
# الحالات العشر المطلوبة في المواصفة (قسم #27) — مصفوفة كاملة
# ═══════════════════════════════════════════════════════════════════════

class TestSpecTenCases:
    def test_case1_username_plus_uid(self):
        """Test 1: username=@iesk1 + user_id → اسم clickable + زر موجود."""
        built = build_alert_html(_data(sender_id=123456789, sender_username="@iesk1"))
        assert 'href="https://t.me/iesk1"' in built["text"]
        assert built["buttons"][0][0] == {"text": "مراسلة", "url": "https://t.me/iesk1"}

    def test_case2_no_username_uid_5601276336(self):
        """Test 2: username=None + user_id=5601276336 → clickable عبر user_id."""
        built = build_alert_html(_data(sender_id=5601276336, sender_first_name="نواف",
                                       sender_last_name=None))
        assert 'href="tg://user?id=5601276336"' in built["text"]
        assert built["buttons"][0][0]["url"] == "tg://user?id=5601276336"

    def test_case3_no_username_uid_1630216165(self):
        """Test 3: username=None + user_id=1630216165 → نفس المطلوب."""
        built = build_alert_html(_data(sender_id=1630216165, sender_first_name="أحمد",
                                       sender_last_name=None))
        assert 'href="tg://user?id=1630216165"' in built["text"]
        assert built["buttons"][0][0]["url"] == "tg://user?id=1630216165"

    def test_case4_cached_user_no_api(self):
        """Test 4: cached → لا API request — مغطى في TestEntityCache.test_cache_hit_no_api_request."""
        # الحل المركزي نفسه بلا I/O إطلاقاً (pure-sync)
        t = resolve_contact_target(sender_id=SENDER_ID)
        assert t.reachable

    def test_case7_anonymous_admin(self):
        """Test 7: Anonymous Admin (يظهر Channel) → لا اختلاق user_id/رابط."""
        t = resolve_contact_target(sender_id=555, sender_type="channel")
        assert t.user_id_url is None and t.selected_url is None
        built = build_alert_html(_data(sender_id=555, sender_type="channel"))
        assert "tg://user" not in built["text"].replace('href="', "").replace('"', "")
        assert all(b["text"] != "مراسلة" for row in built["buttons"] for b in row)

    def test_case8_channel_sender(self):
        """Test 8: مرسل قناة → لا يُعامل كمستخدم."""
        t = resolve_contact_target(sender_id=777, sender_type="channel",
                                   first_name=None, last_name=None)
        assert t.sender_type == "channel"
        built = build_alert_html(_data(sender_id=777, sender_type="channel",
                                       sender_username=None))
        assert built["contact_method"] == "text_only"

    def test_case9_forwarded_source_not_mixed_with_sender(self):
        """Test 9: المرسل الفعلي ≠ مصدر التوجيه — الحل من sender_id حصراً؛
        حقول fwd لا تدخل في الهدف إطلاقًا."""
        t = resolve_contact_target(sender_id=SENDER_ID, first_name="نواف")
        # حتى لو حملت data حقول مصدر توجيه، لا تؤثر على الهدف
        assert t.user_id == SENDER_ID
        assert t.selected_url == f"tg://user?id={SENDER_ID}"

    def test_case10_avatar_failure_alert_continues(self):
        """Test 10: فشل الصورة → التنبيه + الاسم + الزر كلهم يستمرون."""
        built = build_alert_html(_data(sender_photo_available=False,
                                       sender_is_deleted=False))
        assert built["contact_method"] == "mention_button"
        assert built["buttons"][0][0]["text"] == "مراسلة"


# ═══════════════════════════════════════════════════════════════════════
# سلسلة التحقق (spec #20) + اللوج (spec #22) + المقاييس (spec #23)
# ═══════════════════════════════════════════════════════════════════════

class TestValidation:
    def test_valid_target_passes(self):
        ok, reason = validate_contact_target(
            resolve_contact_target(sender_id=SENDER_ID, username="iesk1"))
        assert ok is True and reason == ""

    def test_none_fails(self):
        ok, reason = validate_contact_target(None)
        assert ok is False and reason == "no_target"

    def test_channel_without_username_fails(self):
        ok, reason = validate_contact_target(
            resolve_contact_target(sender_id=555, sender_type="channel"))
        assert ok is False and "not_linkable" in reason

    def test_unknown_url_shape_fails(self):
        t = resolve_contact_target(sender_id=SENDER_ID)
        t.selected_url = "javascript:alert(1)"
        ok, reason = validate_contact_target(t)
        assert ok is False and reason == "unknown_url_shape"


class _LogCapture:
    def __init__(self):
        self.lines: List[str] = []

    def __call__(self, message):
        self.lines.append(str(message))


@pytest.fixture()
def sender_logs():
    """يلتقط مخرجات loguru (caplog لا يرى loguru) — نظيف بعد كل اختبار."""
    from loguru import logger
    cap = _LogCapture()
    handler_id = logger.add(cap, level="INFO", filter=lambda r: "[SENDER]" in r["message"])
    try:
        yield cap
    finally:
        logger.remove(handler_id)


class TestLogging:
    def test_log_line_contains_spec_fields(self, sender_logs):
        """spec #22: الحقول المطلوبة تظهر في سطر [SENDER] واحد."""
        t = resolve_contact_target(sender_id=SENDER_ID, username="iesk1")
        log_sender_resolution(t, message_id=4242, chat_id=CHAT_ID,
                              sender_id=SENDER_ID, resolution="nav",
                              clickable=True, contact_button=True)
        assert sender_logs.lines, "سطر [SENDER] مطلوب"
        line = sender_logs.lines[-1]
        for field in ("[SENDER] message_id=4242", "chat_id=-100111222333",
                      "sender_id=5601276336", "username=iesk1",
                      "user_id_target=AVAILABLE", "openmessage_target=AVAILABLE",
                      "selected_target=username", "clickable_name=SUCCESS",
                      "contact_button=SUCCESS"):
            assert field in line, field

    def test_log_failure_reason(self, sender_logs):
        t = resolve_contact_target(sender_id=0)
        log_sender_resolution(t, message_id=1, clickable=False, contact_button=False)
        assert any("clickable_name=FAILED" in m and "reason=" in m
                   for m in sender_logs.lines)

    def test_log_no_phone_leak(self, sender_logs):
        """لا بيانات حساسة (هاتف) في اللوج — spec #22."""
        t = resolve_contact_target(sender_id=SENDER_ID, username="iesk1")
        log_sender_resolution(t, message_id=1)
        all_logs = "\n".join(sender_logs.lines)
        assert "+9665" not in all_logs and "phone" not in all_logs


class TestMetrics:
    def test_snapshot_has_all_spec_keys(self):
        """spec #23: كل مفاتيح المواصفة موجودة (صفرية افتراضيًا)."""
        snap = get_contact_metrics_snapshot()
        for key in ("sender_resolution_success", "sender_resolution_failure",
                    "username_available", "user_id_fallback",
                    "clickable_sender_success", "clickable_sender_failure",
                    "contact_button_success", "contact_button_failure"):
            assert key in snap, key

    def test_counters_increment(self):
        before = get_contact_metrics_snapshot()["user_id_fallback"]
        resolve_contact_target(sender_id=SENDER_ID)  # بلا username → fallback
        after = get_contact_metrics_snapshot()["user_id_fallback"]
        assert after == before + 1

    def test_metric_inc_never_raises(self):
        metric_inc("not_a_key")  # مفتاح غير معروف → تجاهل صامت
        metric_inc("clickable_sender_success", by=0)


# ═══════════════════════════════════════════════════════════════════════
# القالب النهائي الفعّال (مواصفة المستخدم — الشكل الحرفي)
# ═══════════════════════════════════════════════════════════════════════

class TestFinalTemplate:
    def test_exact_shape(self):
        """الشكل النهائي الفعّال: 👤: الاسم ← (فارغ) ← 💬: ← النص ← [مراسلة][عرض]."""
        built = build_alert_html(_data())
        expected_head = "👤: <a href=\"tg://user?id=5601276336\">نواف محمد</a>\n\n<b>💬:</b>\n<blockquote>نص الرسالة الأصلي</blockquote>"
        assert built["text"] == expected_head
        assert [[b["text"] for b in row] for row in built["buttons"]] == [
            ["مراسلة", "عرض الرسالة"]
        ]

    def test_monitor_build_alert_uses_same_target(self):
        """مسار الحسابات (monitors._build_alert) يبني نفس النص بلا أزرار."""
        from monitors import EnhancedAccountMonitor
        account = {"id": 1, "prefix": "ACCOUNT_1", "name": "A1", "session": "s",
                   "api_id": 1, "api_hash": "h", "phone": "+966500000000", "priority": 1}
        m = EnhancedAccountMonitor(account, None, None)
        sender = {"id": SENDER_ID, "display": "نواف محمد", "username": None,
                  "access_hash": None}
        chat = {"entity": None, "title": None, "id": CHAT_ID, "message_id": MSG_ID,
                "username": None, "group_link": "#", "msg_link": "#"}
        target = resolve_contact_target(sender_id=SENDER_ID, first_name="نواف",
                                        last_name="محمد")
        text, buttons = m._build_alert(sender, chat, "واجب", "النص", {},
                                       contact_target=target)
        assert text == ("👤: <a href=\"tg://user?id=5601276336\">نواف محمد</a>\n\n"
                        "<b>💬:</b>\n<blockquote>النص</blockquote>")
        assert buttons is None  # الأزرار من بوت التنبيهات فقط (عقد v10.8)
