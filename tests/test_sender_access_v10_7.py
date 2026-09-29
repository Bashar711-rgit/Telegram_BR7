"""v10.7 Sender Access Registry — اختبارات القبول السبعة + الوحدات.

يغطي مواصفة «كل اسم مرسل قابل للنقر والوصول» حرفياً:
  §1 جمع بيانات المرسل: get_sender/get_input_sender → sender_access_hash
     (من InputPeer) + owner_account + sender_type + sender_phone +
     sender_usernames.
  §2 قاعدة البيانات: جدول sender_access (PK sender_id+account_name) —
     upsert لكل رسالة، ترحيل آمن من sender_account_hashes، أعمدة
     owner_account في sender_contacts، contact_method في alerts.
  §3 سلسلة المستويات:
     1) username → t.me/USERNAME (html ذهبي، بلا mention).
     2) Text Mention من الحساب المالك (InputMessageEntityMentionName).
     3) حسابات sender_access الأخرى واحداً واحداً بـhash الخاص بها
        (محاكاة: الحساب المالك خارج الهدف → انتقال تلقائي).
     4) مرسل قناة/أدمن مجهول أو فشل الكل → رابط الرسالة + forward.
  §4 التحقق بعد الإرسال: MessageEntityMentionName(user_id) في الرد؛
     غيابه → forward supplement + تسجيل السبب. contact_method يُخزَّن.
  §5 فحص عضوية TARGET_GROUP_ID عند الإقلاع (تحذير لكل حساب غير عضو).

كل تفاعلات تيليجرام مزيّفة (FakeClient) — لا شبكة إطلاقاً.
"""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from telethon.tl.types import (
    Channel as TgChannel,
    InputMessageEntityMentionName,
    InputPeerUser,
    InputUser,
    MessageEntityMentionName,
    MessageEntityTextUrl,
    User as TgUser,
)

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import monitors as mon
from config import CFG
from monitors import EnhancedAccountMonitor, _verify_mention_entity
from monitors import _target_membership_warned
from database import AlertRecord

SENDER_ID = 555000777
GOOD_HASH = 7234567890123
ACCOUNT2_HASH = 8812345678901
CHAT_ID = -100111222333
MSG_ID = 4242
TARGET = int(CFG.TARGET_GROUP_ID)


@pytest.fixture(autouse=True)
def _clean_shared_module_state():
    """هذه الاختبارات تشارك الحالة أحادية المشروع (dedup/similarity/hashes)
    بينها — تُنظّف بعد كل اختبار لمنع تلوث متبادل."""
    yield
    mon.account_hash_store._data.clear()
    mon.mention_negative._data.clear()
    mon._recent_input_senders.clear()
    mon._post_capable_accounts.clear()
    mon._target_membership_warned.clear()
    try:
        from dedup import get_deduplicator as _gd
        _gd()._mem.clear()
    except Exception:
        pass
    try:
        from similarity import get_similarity_gate as _gs
        _gs()._recent.clear()
    except Exception:
        pass


def _alert_data(**kw) -> Dict[str, Any]:
    base = {
        "chat_id": CHAT_ID, "message_id": MSG_ID, "sender_id": SENDER_ID,
        "sender_username": None, "sender_first_name": "أحمد", "sender_last_name": None,
        "sender_access_hash": GOOD_HASH, "chat_access_hash": 4242, "chat_username": None,
        "chat_title": "مجموعة الطلاب",
        "text": "محتاج حل واجب رياضيات", "account_name": "Account 1",
        "owner_account": "Account 1", "sender_type": "user",
        "sender_phone": None, "sender_usernames": None,
        "msg_date_ts": time.time(), "receive_lag_ms": 120.0,
    }
    base.update(kw)
    return base


class FakeClient:
    """عميل تيليجرام مزيّف يكفي لمسار الإرسال كله (v10.7).

    * الرد يحمل كيانات MessageEntityMentionName كما يفعل السيرفر الحقيقي
      (التحقق الإلزامي بعد الإرسال يفحصها).
    * fail_all_mentions=True → كل محاولة mention ترفع UserIDInvalidError
      (hash غير صالح لهذا الحساب) بينما ينجح الإرسال العادي.
    * fail_all_sends=True → الحساب «خارج الهدف» (ChatWriteForbidden على
      كل إرسال) — يحاكي المستوى 3 (انتقال تلقائي لحساب آخر).
    * drop_entities=True → الرد بلا كيان mention (فحص التصعيد إلى forward).
    """

    def __init__(
        self,
        name: str,
        bad_hash: Optional[int] = None,
        fail_all_mentions: bool = False,
        fail_all_sends: bool = False,
        drop_entities: bool = False,
    ):
        self.name = name
        self.is_connected = True
        self.bad_hash = bad_hash
        self.fail_all_mentions = fail_all_mentions
        self.fail_all_sends = fail_all_sends
        self.drop_entities = drop_entities
        self.calls: List[Dict[str, Any]] = []
        self.forward_calls: List[Dict[str, Any]] = []
        self._chat_entities: Dict[int, Any] = {}
        self._user_entities: Dict[str, Any] = {}

    # entity resolution fakes -------------------------------------------------
    def set_chat(self, chat_id: int, peer: Any) -> None:
        self._chat_entities[int(chat_id)] = peer

    def set_user(self, username: str, peer: Any) -> None:
        self._user_entities[username.lstrip("@")] = peer

    async def get_input_entity(self, key: Any):
        if isinstance(key, int):
            if key in self._chat_entities:
                return self._chat_entities[key]
            raise ValueError(f"no cached entity for {key}")
        if isinstance(key, str) and key.startswith("@"):
            uname = key[1:]
            if uname in self._user_entities:
                return self._user_entities[uname]
            raise ValueError(f"no cached entity for {key}")
        raise ValueError("unresolvable")

    # send fakes --------------------------------------------------------------
    def _response_entities(self, kwargs: Dict[str, Any]) -> List[Any]:
        """كيان الطلب InputMessageEntityMentionName (user_id=InputUser)
        يعود في الرد MessageEntityMentionName (user_id=int)."""
        if self.drop_entities:
            return []
        out: List[Any] = []
        for e in kwargs.get("formatting_entities") or []:
            if isinstance(e, InputMessageEntityMentionName):
                iu = e.user_id
                out.append(
                    MessageEntityMentionName(offset=0, length=1, user_id=int(getattr(iu, "user_id", 0) or 0))
                )
        return out

    def _maybe_raise(self, kwargs: Dict[str, Any]) -> None:
        if self.fail_all_sends:
            # «الحساب خارج الهدف» — فشل إرسال كامل (غير mention-invalid)
            raise RuntimeError("ChatWriteForbidden (simulated)")
        ents = kwargs.get("formatting_entities")
        if ents is None:
            return
        if self.fail_all_mentions:
            raise mon.UserIDInvalidError()
        for e in ents:
            if isinstance(e, InputMessageEntityMentionName):
                iu = e.user_id
                if self.bad_hash is not None and getattr(iu, "access_hash", None) == self.bad_hash:
                    raise mon.UserIDInvalidError()

    async def send_message(self, entity, message, **kwargs):
        self._maybe_raise(kwargs)
        self.calls.append({"kind": "message", "text": message, **kwargs})
        return SimpleNamespace(id=999, chat_id=entity, entities=self._response_entities(kwargs))

    async def send_file(self, entity, file=None, caption=None, **kwargs):
        self._maybe_raise(kwargs)
        self.calls.append({"kind": "file", "text": caption, **kwargs})
        return SimpleNamespace(id=998, chat_id=entity, entities=self._response_entities(kwargs))

    async def forward_messages(self, entity, message_id, *, from_peer=None, **kwargs):
        self.forward_calls.append({"to": entity, "msg_id": message_id, "from_peer": from_peer})
        return SimpleNamespace(id=1000 + len(self.forward_calls), chat_id=entity)


def _monitor(db, name: str, client: Optional[FakeClient]) -> EnhancedAccountMonitor:
    account = {
        "id": 1, "prefix": "ACCOUNT_1", "name": name, "session": f"s_{name}",
        "api_id": 12345, "api_hash": "h" * 32, "phone": "+966500000000", "priority": 20,
    }
    m = EnhancedAccountMonitor(account, db, None)
    m.client = client
    m.is_connected = True
    return m


def _wire(db, m: EnhancedAccountMonitor, *monitors_with_clients) -> None:
    """bot_ref كامل كي تعبر حراس _send_alert + مرشحو الإرسال."""
    clients = [c for _, c in monitors_with_clients]

    async def _can_proceed(account_name):
        return True

    m._bot_ref = SimpleNamespace(
        alert_sender_client=None,
        main_client=clients[0] if clients else None,
        monitors=[],
        rate_limiter=SimpleNamespace(can_proceed=_can_proceed),
        _note_alert_message=lambda *a, **k: None,
    )
    for mon_obj, client in monitors_with_clients:
        m._bot_ref.monitors.append(SimpleNamespace(account={"name": mon_obj.account["name"]}, client=client, is_connected=True))
    m.client = clients[0] if clients else None

    async def _fake_chat_info(cl, chat_id, message_id, chat_access_hash=None, chat_username=None):
        return {"entity": None, "title": "مجموعة الطلاب", "group_link": "#", "msg_link": "#"}

    m._chat_info = _fake_chat_info


def _mention_call(client: FakeClient) -> Optional[Dict[str, Any]]:
    for c in client.calls:
        if c.get("formatting_entities"):
            return c
    return None


def _mention_user_ids(call: Dict[str, Any]) -> List[int]:
    ius = []
    for e in call["formatting_entities"]:
        if isinstance(e, InputMessageEntityMentionName):
            ius.append(int(getattr(e.user_id, "user_id", 0) or 0))
    return ius


# ═════════════════ §4 وحدة: التحقق الإلزامي بعد الإرسال ═════════════════

class TestVerifyMentionEntity:
    def test_verified_when_entity_present(self):
        msg = SimpleNamespace(entities=[MessageEntityMentionName(offset=0, length=1, user_id=SENDER_ID)])
        ok, why = _verify_mention_entity(msg, SENDER_ID)
        assert ok is True and why == ""

    def test_fails_when_user_id_differs(self):
        msg = SimpleNamespace(entities=[MessageEntityMentionName(offset=0, length=1, user_id=111222333)])
        ok, why = _verify_mention_entity(msg, SENDER_ID)
        assert ok is False and why == "mention_entity_missing"

    def test_fails_when_no_entities(self):
        ok, why = _verify_mention_entity(SimpleNamespace(entities=None), SENDER_ID)
        assert ok is False and why == "no_entities_in_response"

    def test_never_raises(self):
        ok, why = _verify_mention_entity(None, SENDER_ID)
        assert ok is False and why


# ═════════════════ §1 وحدة: جمع بيانات المرسل (_event_to_dict) ═════════════════

def _fake_event(sender: Any, input_sender: Any = None) -> SimpleNamespace:
    msg = SimpleNamespace(id=MSG_ID, text="محتاج حل واجب رياضيات", media=None, date=None)
    ev = SimpleNamespace(
        sender=sender,
        sender_id=SENDER_ID,
        chat_id=CHAT_ID,
        message=msg,
        chat=SimpleNamespace(username=None, access_hash=4242, title="مجموعة الطلاب"),
    )

    async def _get_sender():
        return sender

    async def _get_input_sender():
        if isinstance(input_sender, Exception):
            raise input_sender
        return input_sender

    ev.get_sender = _get_sender
    ev.get_input_sender = _get_input_sender
    return ev


class TestEventToDictSenderData:
    @pytest.mark.asyncio
    async def test_user_sender_full_fields(self, db):
        m = _monitor(db, "Account 1", FakeClient("Account 1"))
        sender = TgUser(id=SENDER_ID, access_hash=GOOD_HASH, first_name="أحمد", username="ahmed_99", phone="+966500000000")
        sender.usernames = None
        ev = _fake_event(sender, InputPeerUser(user_id=SENDER_ID, access_hash=GOOD_HASH))
        data = await m._event_to_dict(ev)
        # access_hash من الـInputPeer (رؤية هذا الحساب المثبتة)
        assert data["sender_access_hash"] == GOOD_HASH
        assert data["owner_account"] == "Account 1"
        assert data["sender_type"] == "user"
        assert data["sender_phone"] == "+966500000000"
        assert data["sender_usernames"] == ["ahmed_99"]
        # المفاتيح القديمة كما هي (توافق خلفي)
        assert data["sender_id"] == SENDER_ID and data["account_name"] == "Account 1"

    @pytest.mark.asyncio
    async def test_channel_sender_detected(self, db):
        """أدمن مجهول/قناة → sender_type=channel → لا Text Mention (مستوى 4)."""
        m = _monitor(db, "Account 1", FakeClient("Account 1"))
        ch = TgChannel(id=-100999, title="قناة", photo=None, date=None, megagroup=True, access_hash=777)
        ev = _fake_event(ch, None)
        data = await m._event_to_dict(ev)
        assert data["sender_type"] == "channel"
        assert data["sender_phone"] is None

    @pytest.mark.asyncio
    async def test_active_usernames_collected(self, db):
        m = _monitor(db, "Account 1", FakeClient("Account 1"))
        sender = TgUser(id=SENDER_ID, access_hash=GOOD_HASH, first_name="أحمد", username="main_u")
        sender.usernames = [
            SimpleNamespace(username="main_u", active=True),
            SimpleNamespace(username="brand_u", active=True),
            SimpleNamespace(username="old_u", active=False),
        ]
        ev = _fake_event(sender, None)
        data = await m._event_to_dict(ev)
        assert data["sender_usernames"] == ["main_u", "brand_u"]

    @pytest.mark.asyncio
    async def test_sender_none_stays_failure_safe(self, db):
        """sender مفقود والـget_sender يفشل → القيم بلا انهيار (worker يكمل)."""
        m = _monitor(db, "Account 1", FakeClient("Account 1"))
        ev = _fake_event(None, RuntimeError("no input sender"))

        async def _boom():
            raise RuntimeError("no sender")

        ev.get_sender = _boom
        data = await m._event_to_dict(ev)
        assert data["sender_type"] == "none"
        assert data["_sender_needs_enrichment"] is True


# ═════════════════ §2 قاعدة البيانات: sender_access + الأعمدة ═════════════════

class TestSenderAccessDB:
    @pytest.mark.asyncio
    async def test_upsert_and_get_roundtrip(self, db):
        assert await db.upsert_sender_access(SENDER_ID, "Account 1", GOOD_HASH) is True
        assert await db.get_sender_access_hash("Account 1", SENDER_ID) == GOOD_HASH
        # عزل لكل حساب — hash الحساب A لا يظهر للحساب B
        assert await db.get_sender_access_hash("Account 2", SENDER_ID) is None
        # تحديث نفس المفتاح
        await db.upsert_sender_access(SENDER_ID, "Account 1", 999888777)
        assert await db.get_sender_access_hash("Account 1", SENDER_ID) == 999888777
        # مدخلات غير صالحة مرفوضة
        assert await db.upsert_sender_access(SENDER_ID, "Account 1", 0) is False
        assert await db.upsert_sender_access(0, "Account 1", GOOD_HASH) is False

    @pytest.mark.asyncio
    async def test_get_sender_access_accounts_freshest_first(self, db):
        await db.upsert_sender_access(SENDER_ID, "Account 1", GOOD_HASH)
        await asyncio.sleep(0.01)
        await db.upsert_sender_access(SENDER_ID, "Account 2", ACCOUNT2_HASH)
        rows = await db.get_sender_access_accounts(SENDER_ID)
        names = [r["account_name"] for r in rows]
        assert names[0] == "Account 2"  # الأحدث أولاً
        hashes = {r["account_name"]: r["access_hash"] for r in rows}
        assert hashes["Account 1"] == GOOD_HASH and hashes["Account 2"] == ACCOUNT2_HASH

    @pytest.mark.asyncio
    async def test_legacy_methods_redirect_to_sender_access(self, db):
        """v10.4 APIs تعمل كما كانت — لكن التخزين الآن في sender_access."""
        assert await db.upsert_sender_account_hash("Account 3", SENDER_ID, 4242) is True
        assert await db.get_sender_account_hash("Account 3", SENDER_ID) == 4242
        assert await db.get_sender_access_hash("Account 3", SENDER_ID) == 4242

    @pytest.mark.asyncio
    async def test_migration_copies_legacy_rows_and_is_idempotent(self, db):
        """ترحيل آمن: صف في sender_account_hashes → يُنسخ إلى sender_access؛
        إعادة الترحيل لا تكرر ولا تفقد (idempotent)."""
        await db._execute(
            "INSERT INTO sender_account_hashes (account_name, sender_id, access_hash, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("LegacyAcc", 424242, 31337, time.time()),
        )
        await db._commit()
        await db._migrate_sender_access()  # مرة ثانية (الأولى جرت في connect)
        assert await db.get_sender_access_hash("LegacyAcc", 424242) == 31337

    @pytest.mark.asyncio
    async def test_alerts_contact_method_column(self, db):
        """contact_method يُخزَّن مع كل تنبيه (بعد flush الدفعي)."""
        await db.add_alert(AlertRecord(
            message_hash="cmhash1", chat_id=CHAT_ID, sender_id=SENDER_ID,
            account_name="Account 1", keyword="واجب", alert_text="نص", timestamp=time.time(),
            contact_method="mention",
        ))
        await db._flush()
        row = await db._fetchone("SELECT contact_method FROM alerts WHERE message_hash = ?", ("cmhash1",))
        assert row and row["contact_method"] == "mention"

    @pytest.mark.asyncio
    async def test_sender_contacts_owner_account_and_links(self, db):
        """أعمدة v10.7 في sender_contacts: owner_account + msg_link/group_link
        (الإصلاح: كانا يُحفظان NULL دائماً)."""
        await db.upsert_sender_contact({
            "sender_id": SENDER_ID, "access_hash": GOOD_HASH, "username": "ahmed_99",
            "first_name": "أحمد", "last_name": None, "chat_id": CHAT_ID, "message_id": MSG_ID,
            "msg_link": f"https://t.me/c/111222333/{MSG_ID}", "group_link": "https://t.me/c/111222333",
            "owner_account": "Account 1",
        })
        row = await db.get_sender_contact(SENDER_ID)
        assert row["owner_account"] == "Account 1"
        assert row["last_message_link"] == f"https://t.me/c/111222333/{MSG_ID}"
        assert row["last_group_link"] == "https://t.me/c/111222333"
        # تحديث لاحق بلا روابط لا يمحو القديمة (COALESCE)
        await db.upsert_sender_contact({
            "sender_id": SENDER_ID, "owner_account": "Account 2",
        })
        row = await db.get_sender_contact(SENDER_ID)
        assert row["owner_account"] == "Account 2"
        assert row["last_message_link"] == f"https://t.me/c/111222333/{MSG_ID}"


# ═════════════════ §3 سلسلة المستويات — القبول السبعة ═════════════════

@pytest.mark.asyncio
async def test_acceptance_1_username_opens_tme_link(db):
    """1) مستخدم بـusername → الاسم يفتح t.me/username (مستوى 1 — html
    ذهبي، بلا أي محاولة mention وبلا forward)."""
    client = FakeClient("Account 1")
    m = _monitor(db, "Account 1", client)
    _wire(db, m, (m, client))
    data = _alert_data(sender_username="ahmed_99")
    await m._send_alert(data, "واجب", data["text"], "acc1-hash", {"rule_tag": None})
    sends = [c for c in client.calls if c["kind"] == "message"]
    assert len(sends) == 1
    assert sends[0].get("parse_mode") == "html"
    assert "https://t.me/ahmed_99" in sends[0]["text"]
    assert not _mention_call(client)  # لا mention — الرابط أضمن
    assert client.forward_calls == []


@pytest.mark.asyncio
async def test_acceptance_2_no_username_mention_opens_profile(db):
    """2) مستخدم بلا username → الاسم Mention حقيقي يفتح ملفه (مستوى 2
    من الحساب المالك — InputMessageEntityMentionName بـhash رؤيته)."""
    client = FakeClient("Account 1")
    m = _monitor(db, "Account 1", client)
    _wire(db, m, (m, client))
    data = _alert_data()
    await m._send_alert(data, "واجب", data["text"], "acc2-hash", {"rule_tag": None})
    call = _mention_call(client)
    assert call, "يجب إرسال تنبيه بكيان mention حقيقي"
    assert SENDER_ID in _mention_user_ids(call)
    # الكيان المُرسل عليه hash رؤية الحساب المالك نفسه
    ent = [e for e in call["formatting_entities"] if isinstance(e, InputMessageEntityMentionName)][0]
    assert int(getattr(ent.user_id, "access_hash", 0)) == GOOD_HASH
    # الرد تحقق من كيان الmention → لا forward
    assert client.forward_calls == []


@pytest.mark.asyncio
async def test_acceptance_3_account2_only_sends_the_mention(db):
    """3) مستخدم رآه Account_2 فقط وأرسل التنبيه عبر Account_2 → يعمل
    (sender_access يحمل hash رؤية Account_2 — درجة account_hash)."""
    client1 = FakeClient("Account 1")  # لم يرَ المرسل: hash فارغ، لا كاش محادثة
    client2 = FakeClient("Account 2")
    m1 = _monitor(db, "Account 1", client1)
    m2 = _monitor(db, "Account 2", client2)
    _wire(db, m1, (m1, client1), (m2, client2))
    # «إعادة تشغيل»: ذاكرة الـhash فارغة — كل شيء من sender_access فقط
    mon.account_hash_store._data.clear()
    await db.upsert_sender_access(SENDER_ID, "Account 2", ACCOUNT2_HASH)
    data = _alert_data(sender_access_hash=None, account_name="Account 1")
    await m1._send_alert(data, "واجب", data["text"], "acc3-hash", {"rule_tag": None})
    m2_calls = _mention_call(client2)
    assert m2_calls, "الحساب الثاني يجب أن يرسل الmention بhash الخاص به"
    ent = [e for e in m2_calls["formatting_entities"] if isinstance(e, InputMessageEntityMentionName)][0]
    assert int(getattr(ent.user_id, "access_hash", 0)) == ACCOUNT2_HASH
    assert not _mention_call(client1)


@pytest.mark.asyncio
async def test_acceptance_4_owner_outside_target_moves_to_next_account(db):
    """4) حساب المالك خارج الهدف (فشل إرسال كامل) → ينتقل تلقائياً
    لحساب آخر ويصل التنبيه بmention مُتحقَّق."""
    dead = FakeClient("Account 1", fail_all_sends=True)   # خارج الهدف
    alive = FakeClient("Account 2")
    m1 = _monitor(db, "Account 1", dead)
    m2 = _monitor(db, "Account 2", alive)
    _wire(db, m1, (m1, dead), (m2, alive))
    mon.account_hash_store._data.clear()
    await db.upsert_sender_access(SENDER_ID, "Account 2", ACCOUNT2_HASH)
    data = _alert_data(sender_access_hash=None, account_name="Account 1")
    await m1._send_alert(data, "واجب", data["text"], "acc4-hash", {"rule_tag": None})
    assert dead.calls == []  # الحساب الخارج فشل ولم يُرسل
    call = _mention_call(alive)
    assert call, "التنبيه وصل من الحساب البديل بmention"
    assert SENDER_ID in _mention_user_ids(call)


@pytest.mark.asyncio
async def test_acceptance_5_channel_sender_uses_forward_and_link(db):
    """5) مرسل قناة/أدمن مجهول → لا Text Mention إطلاقاً؛ التنبيه يخرج
    برابط الرسالة + forward يُظهر اسم المرسل أعلى النسخة."""
    client = FakeClient("Account 1")
    m = _monitor(db, "Account 1", client)
    _wire(db, m, (m, client))
    data = _alert_data(sender_id=999000111, sender_type="channel", sender_access_hash=None)
    await m._send_alert(data, "واجب", data["text"], "acc5-hash", {"rule_tag": None})
    sends = [c for c in client.calls if c["kind"] == "message"]
    assert sends and all(not c.get("formatting_entities") for c in sends)
    assert len(client.forward_calls) == 1
    fc = client.forward_calls[0]
    assert fc["to"] == TARGET and fc["msg_id"] == MSG_ID and fc["from_peer"] == CHAT_ID


@pytest.mark.asyncio
async def test_acceptance_6_restart_queue_survives_via_sender_access(db):
    """6) إعادة تشغيل البوت ورسائل عالقة بالطابور → الـmention لا يفشل:
    الذاكرة تضيع عند الإقلاع لكن sender_access في DB يبقى (upsert لكل
    رسالة)."""
    client = FakeClient("Account 1")
    m = _monitor(db, "Account 1", client)
    _wire(db, m, (m, client))
    # محاكاة الرسائل السابقة: كل رسالة تحدّث السجل (upsert عند كل رسالة)
    await db.upsert_sender_access(SENDER_ID, "Account 1", GOOD_HASH)
    mon.account_hash_store._data.clear()  # «إعادة تشغيل» — الذاكرة فارغة
    data = _alert_data(sender_access_hash=None)  # حدث مُسترجَع من الطابور
    await m._send_alert(data, "واجب", data["text"], "acc6-hash", {"rule_tag": None})
    call = _mention_call(client)
    assert call, "mention يعمل بعد الإقلاع من سجل sender_access"
    ent = [e for e in call["formatting_entities"] if isinstance(e, InputMessageEntityMentionName)][0]
    assert int(getattr(ent.user_id, "access_hash", 0)) == GOOD_HASH


@pytest.mark.asyncio
async def test_acceptance_7_contact_method_recorded_for_every_alert(db):
    """7) contact_method يُسجَّل في alerts لكل تنبيه (username/mention/
    forward/link)."""
    # (أ) username — نصوص مختلفة كي لا يلتقطها حاجز dedup داخل نفس الاختبار
    c1 = FakeClient("Account 1")
    m1 = _monitor(db, "Account 1", c1)
    _wire(db, m1, (m1, c1))
    await m1._send_alert(_alert_data(sender_username="ahmed_99"), "واجب", "نص الأول", "cm-username", {"rule_tag": None})
    # (ب) mention
    mon.account_hash_store._data.clear()
    c2 = FakeClient("Account 1")
    m2 = _monitor(db, "Account 1", c2)
    _wire(db, m2, (m2, c2))
    await m2._send_alert(_alert_data(), "واجب", "نص الثاني", "cm-mention", {"rule_tag": None})
    # (ج) forward (قناة) — المرسل مختلف أيضاً (sender_id يدخل البصمة)
    c3 = FakeClient("Account 1")
    m3 = _monitor(db, "Account 1", c3)
    _wire(db, m3, (m3, c3))
    await m3._send_alert(_alert_data(sender_id=999000112, sender_type="channel", sender_access_hash=None), "واجب", "نص الثالث", "cm-forward", {"rule_tag": None})

    async def _method(h):
        await db._flush()  # add_alert دفعي — الصف يظهر بعد flush
        row = await db._fetchone("SELECT contact_method FROM alerts WHERE message_hash = ?", (h,))
        return row["contact_method"] if row else None

    assert await _method("cm-username") == "username"
    assert await _method("cm-mention") == "mention"
    assert await _method("cm-forward") == "forward"


# ═════════════════ §4 تصعيد: كيان مفقود بعد الإرسال → forward ═════════════════

@pytest.mark.asyncio
async def test_unverified_mention_escalates_to_forward(db):
    """أُرسل التنبيه بmention لكن الرد بلا كيان (حالة نادرة) → تسجيل السبب
    + forward supplement (بلا إعادة إرسال التنبيه — لا تكرار)."""
    client = FakeClient("Account 1", drop_entities=True)
    m = _monitor(db, "Account 1", client)
    _wire(db, m, (m, client))
    data = _alert_data()
    await m._send_alert(data, "واجب", data["text"], "uv-hash", {"rule_tag": None})
    sends = [c for c in client.calls if c["kind"] == "message"]
    assert len(sends) == 1, "التنبيه لا يُعاد إرساله (لا تكرار)"
    assert len(client.forward_calls) == 1, "التصعيد تم عبر forward"
    await db._flush()
    row = await db._fetchone("SELECT contact_method FROM alerts WHERE message_hash = ?", ("uv-hash",))
    assert row["contact_method"] == "forward"


@pytest.mark.asyncio
async def test_forward_kill_switch(db):
    """ALERT_FORWARD_FALLBACK=false يطفئ شبكة الأمان (سلوك قديم نظيف)."""
    old = CFG.ALERT_FORWARD_FALLBACK
    try:
        object.__setattr__(CFG, "ALERT_FORWARD_FALLBACK", False)
        client = FakeClient("Account 1")
        m = _monitor(db, "Account 1", client)
        _wire(db, m, (m, client))
        await m._send_alert(_alert_data(sender_type="channel", sender_id=999000113, sender_access_hash=None), "واجب", "نص", "fk-hash", {"rule_tag": None})
        assert client.forward_calls == []
        await db._flush()
        row = await db._fetchone("SELECT contact_method FROM alerts WHERE message_hash = ?", ("fk-hash",))
        assert row["contact_method"] == "link"
    finally:
        object.__setattr__(CFG, "ALERT_FORWARD_FALLBACK", old)


# ═════════════════ §5 فحص العضوية عند الإقلاع ═════════════════

class _MembershipClient(FakeClient):
    def __init__(self, is_member: bool, **kw):
        super().__init__("membership", **kw)
        self._is_member = is_member

    async def get_permissions(self, entity, user):
        return SimpleNamespace(is_member=self._is_member)


class TestTargetMembershipCheck:
    @pytest.mark.asyncio
    async def test_member_ok_no_warning(self, db):
        _target_membership_warned.discard("MemberAcc")
        m = _monitor(db, "MemberAcc", _MembershipClient(is_member=True))
        result = await m.check_target_membership()
        assert result == "ok"
        assert "MemberAcc" in _target_membership_warned

    @pytest.mark.asyncio
    async def test_non_member_warns_once(self, db):
        _target_membership_warned.discard("OutsideAcc")
        m = _monitor(db, "OutsideAcc", _MembershipClient(is_member=False))
        result = await m.check_target_membership()
        assert result == "missing"
        # التحذير مرة واحدة لكل إقلاع — الاستدعاء الثاني يتخطى (None)
        m2 = _monitor(db, "OutsideAcc", _MembershipClient(is_member=False))
        assert await m2.check_target_membership() is None

    @pytest.mark.asyncio
    async def test_check_failure_never_raises(self, db):
        """فشل الفحص نفسه (استثناء تيليجرام) لا يوقف التشغيل ويسجل تحذيراً."""
        _target_membership_warned.discard("BrokenAcc")

        class _Broken(FakeClient):
            async def get_permissions(self, entity, user):
                raise RuntimeError("channel private")

        m = _monitor(db, "BrokenAcc", _Broken("Broken"))
        result = await m.check_target_membership()  # لا استثناء
        assert result == "error"
        assert "BrokenAcc" in _target_membership_warned
