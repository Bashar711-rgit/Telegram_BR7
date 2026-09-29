"""v10.4 Sender Mention Intelligence — unit tests.

Covers the engineering-brief requirements WITHOUT touching the alert
format contract:
  * build_mention_entities: tg://user?id & tg://openmessage sender anchors
    → real InputMessageEntityMentionName; t.me links & group card untouched;
    text byte-identical to Telethon's own html.parse output.
  * AccountAccessHashStore: (account, sender_id) keying + isolation.
  * MentionNegativeCache: tier blocking after USER_ID_INVALID.
  * _resolve_mention_input_users: the 5-tier ladder ordering.
  * do_send: tier advance on mention-invalid errors, plain-path fallback
    (parse_mode="html" — identical wire output to v10.3), capability notes.
  * _resolve_send_clients(data): capability-aware ordering (sticky first).
  * AlertLatencyTracker percentiles; DB sender_account_hashes; queue wakeup.

All Telegram interactions are faked; nothing touches the real network.
"""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    InputMessageEntityMentionName,
    InputPeerUser,
    InputUser,
    InputUserFromMessage,
    MessageEntityTextUrl,
)

import monitors as mon
from config import CFG
from monitors import (
    AlertLatencyTracker,
    EnhancedAccountMonitor,
    _post_capable_accounts,
    _recent_input_senders,
)
from sender_resolver import (
    AccountAccessHashStore,
    MentionNegativeCache,
    account_hash_store,
    build_mention_entities,
    input_user_from_entity,
    input_user_from_input_peer,
    input_user_from_message_context,
    mention_negative,
    sender_url_forms,
)
from telethon.extensions import html as tg_html
from telethon.tl.types import MessageEntityMentionName


# ───────────────────────────── helpers ─────────────────────────────

SENDER_ID = 555000111
GOOD_HASH = 7234567890123
BAD_HASH = 1111  # simulates a hash the sending account cannot use
CHAT_ID = -100111222333
MSG_ID = 4242


def _alert_html(sender_id: int = SENDER_ID, username: Optional[str] = None) -> str:
    """The frozen v9.37 alert shape (same anchors _build_alert emits)."""
    from config import InputSanitizer

    safe_text = InputSanitizer.escape_html(InputSanitizer.truncate("محتاج حل واجب رياضيات", 400))
    if username:
        sender_link = f'<a href="https://t.me/{username}">أحمد</a>'
    elif sender_id:
        sender_link = f'<a href="tg://openmessage?user_id={sender_id}">أحمد</a>'
    else:
        sender_link = "أحمد"
    group_link = "https://t.me/c/111222333"
    msg_link = f"https://t.me/c/111222333/{MSG_ID}"
    group_card = (
        f'<blockquote dir="rtl"><a href="{group_link}">مجموعة الطلاب</a>\n\n'
        f'<a href="{msg_link}"><b>عرض الرسالة الأصلية</b></a></blockquote>'
    )
    return f"<b>الرسالة:</b>\n{safe_text}\n\n👤: {sender_link}\n\n{group_card}"


def _entities_by_type(entities: List[Any], cls) -> List[Any]:
    return [e for e in entities if isinstance(e, cls)]


def _iu(hash_: Optional[int] = GOOD_HASH, user_id: int = SENDER_ID):
    if hash_ is None:
        return None
    return InputUser(user_id=user_id, access_hash=hash_)


class FakeClient:
    """Minimal TelegramClient stand-in for the send path.

    v10.7: الرد يحمل كيانات MessageEntityMentionName (كما يفعل السيرفر
    الحقيقي) ليُطلب التحقق الإلزامي بعد الإرسال. drop_entities=True يحاكي
    الحالة النادرة التي يُرسل فيها التنبيه بلا كيان الmention (فحص
    التصعيد إلى forward). forward_messages تُسجّل استدعاءاتها للمستوى 4.
    """

    def __init__(self, name: str, bad_hash: Optional[int] = None, fail_all_mentions: bool = False,
                 drop_entities: bool = False):
        self.name = name
        self.is_connected = True
        self.bad_hash = bad_hash
        self.fail_all_mentions = fail_all_mentions
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
        """خدعة السيرفر الحقيقي: كيان الطلب InputMessageEntityMentionName
        (user_id = InputUser) يعود في الرد MessageEntityMentionName (user_id
        = int). drop_entities يحاكي حذف الكيان (التحقق يفشل)."""
        if self.drop_entities:
            return []
        out: List[Any] = []
        for e in kwargs.get("formatting_entities") or []:
            if isinstance(e, InputMessageEntityMentionName):
                iu = e.user_id
                out.append(MessageEntityMentionName(offset=0, length=1, user_id=int(getattr(iu, "user_id", 0) or 0)))
        return out

    def _maybe_raise(self, kwargs: Dict[str, Any]) -> None:
        ents = kwargs.get("formatting_entities")
        if ents is None:
            return
        if self.fail_all_mentions:
            raise mon.UserIDInvalidError()
        for e in ents:
            if isinstance(e, InputMessageEntityMentionName):
                iu = e.user_id  # InputMessageEntityMentionName exposes user_id
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


def _monitor(db, name: str = "Account 1", client: Optional[FakeClient] = None) -> EnhancedAccountMonitor:
    account = {
        "id": 1, "prefix": "ACCOUNT_1", "name": name, "session": f"s_{name}",
        "api_id": 12345, "api_hash": "h" * 32, "phone": "+966500000000", "priority": 20,
    }
    m = EnhancedAccountMonitor(account, db, None)
    m.client = client
    m.is_connected = True
    return m


def _alert_data(**kw) -> Dict[str, Any]:
    base = {
        "chat_id": CHAT_ID, "message_id": MSG_ID, "sender_id": SENDER_ID,
        "sender_username": None, "sender_first_name": "أحمد", "sender_last_name": None,
        "sender_access_hash": GOOD_HASH, "chat_access_hash": 4242, "chat_username": None,
        "text": "محتاج حل واجب رياضيات", "account_name": "Account 1",
        "msg_date_ts": time.time(), "receive_lag_ms": 120.0,
    }
    base.update(kw)
    return base


# ───────────────── build_mention_entities (the core fix) ─────────────────

class TestBuildMentionEntities:
    def test_tg_user_anchor_replaced_text_identical(self):
        html_alert = _alert_html()
        iu = _iu(GOOD_HASH)
        out = build_mention_entities(html_alert, SENDER_ID, iu)
        assert out is not None
        text, ents = out
        # text must be byte-identical to Telethon's own parse (frozen format)
        expected_text, expected_ents = tg_html.parse(html_alert)
        assert text == expected_text
        # the sender anchor is now a REAL mention
        mentions = _entities_by_type(ents, InputMessageEntityMentionName)
        assert len(mentions) == 1
        src = [e for e in expected_ents if isinstance(e, MessageEntityTextUrl) and e.url == f"tg://openmessage?user_id={SENDER_ID}"][0]
        assert mentions[0].offset == src.offset and mentions[0].length == src.length
        assert mentions[0].user_id is iu
        # the group card links are untouched TextUrl entities
        urls = [getattr(e, "url", "") for e in ents if isinstance(e, MessageEntityTextUrl)]
        assert f"https://t.me/c/111222333/{MSG_ID}" in urls

    def test_tg_user_id_form_replaced(self):
        html_alert = _alert_html().replace(
            f"tg://openmessage?user_id={SENDER_ID}", f"tg://user?id={SENDER_ID}"
        )
        out = build_mention_entities(html_alert, SENDER_ID, _iu(GOOD_HASH))
        assert out is not None
        mentions = _entities_by_type(out[1], InputMessageEntityMentionName)
        assert len(mentions) == 1

    def test_username_anchor_never_replaced(self):
        """t.me/username works for everyone — the frozen contract keeps it."""
        html_alert = _alert_html(username="ahmed_99")
        out = build_mention_entities(html_alert, SENDER_ID, _iu(GOOD_HASH))
        assert out is None  # nothing replaced → caller uses original path

    def test_mention_of_other_user_untouched(self):
        """An anchor for a DIFFERENT user must never be converted (and the
        alert keeps its original wire path)."""
        html_alert = 'مرحبا <a href="tg://user?id=42">صديق</a> وزر'
        _, ents = tg_html.parse(html_alert)
        other = [e for e in ents
                 if isinstance(e, MessageEntityTextUrl) and e.url == "tg://user?id=42"]
        assert other, "premise: Telethon keeps tg:// anchors as TextUrl"
        out = build_mention_entities(html_alert, SENDER_ID, _iu(GOOD_HASH))
        assert out is None

    def test_no_input_user_returns_none(self):
        assert build_mention_entities(_alert_html(), SENDER_ID, None) is None

    def test_empty_html_returns_none(self):
        assert build_mention_entities("", SENDER_ID, _iu()) is None
        assert build_mention_entities(_alert_html(), 0, _iu()) is None

    def test_zero_length_entities_stripped_like_telethon(self):
        from telethon.tl.types import MessageEntityBold

        # Telethon strips 0-length entities post-parse (#3884) — mirror it.
        text, ents = tg_html.parse("<b>الرسالة</b> بعد")
        zero = MessageEntityBold(offset=len(text), length=0)
        html_alert = "<b>الرسالة</b> بعد"
        out = build_mention_entities(html_alert + f'<a href="tg://user?id={SENDER_ID}">أحمد</a>', SENDER_ID, _iu())
        assert out is not None
        assert all(getattr(e, "length", 1) > 0 for e in out[1])

    def test_url_forms_match_builder_shapes(self):
        assert sender_url_forms(SENDER_ID) == (
            f"tg://user?id={SENDER_ID}",
            f"tg://openmessage?user_id={SENDER_ID}",
        )


class TestInputUserHelpers:
    def test_from_entity_rejects_zero_hash(self):
        ent = SimpleNamespace(id=SENDER_ID, access_hash=0)
        assert input_user_from_entity(ent) is None

    def test_from_entity_ok(self):
        iu = input_user_from_entity(SimpleNamespace(id=SENDER_ID, access_hash=GOOD_HASH))
        assert isinstance(iu, InputUser) and iu.access_hash == GOOD_HASH

    def test_from_input_peer(self):
        iu = input_user_from_input_peer(InputPeerUser(user_id=SENDER_ID, access_hash=GOOD_HASH))
        assert isinstance(iu, InputUser)
        assert input_user_from_input_peer(SimpleNamespace(user_id=1)) is None

    def test_from_message_context(self):
        peer = InputPeerUser(user_id=1, access_hash=2)
        iu = input_user_from_message_context(peer, MSG_ID, SENDER_ID)
        assert isinstance(iu, InputUserFromMessage) and iu.msg_id == MSG_ID
        assert input_user_from_message_context(None, MSG_ID, SENDER_ID) is None
        assert input_user_from_message_context(peer, 0, SENDER_ID) is None


# ───────────────────────── per-account hash store ─────────────────────────

class TestAccountAccessHashStore:
    def test_record_and_get(self):
        s = AccountAccessHashStore()
        assert s.record("Account 1", SENDER_ID, GOOD_HASH) is True
        assert s.get("Account 1", SENDER_ID) == GOOD_HASH

    def test_per_account_isolation(self):
        """access_hash is per-account knowledge — never keyed by sender alone."""
        s = AccountAccessHashStore()
        s.record("Account 1", SENDER_ID, GOOD_HASH)
        assert s.get("Account 2", SENDER_ID) is None
        s.record("Account 2", SENDER_ID, 987654321)
        assert s.get("Account 1", SENDER_ID) == GOOD_HASH
        assert s.get("Account 2", SENDER_ID) == 987654321

    def test_same_value_returns_false(self):
        s = AccountAccessHashStore()
        assert s.record("A", 1, 100) is True
        assert s.record("A", 1, 100) is False  # unchanged → no write-behind
        assert s.record("A", 1, 200) is True  # changed → persist again

    def test_invalid_inputs_rejected(self):
        s = AccountAccessHashStore()
        assert s.record(None, 1, 100) is False
        assert s.record("A", None, 100) is False
        assert s.record("A", 1, 0) is False  # zero hash = min view, never stored
        assert s.record("A", 1, "garbage") is False

    def test_bounded(self):
        s = AccountAccessHashStore(maxsize=100)
        for i in range(500):
            s.record("A", i, i)
        assert s.size() <= 100


class TestMentionNegativeCache:
    def test_mark_and_block(self):
        c = MentionNegativeCache(ttl_seconds=60)
        assert c.blocked("Account 1", SENDER_ID, "account_hash") is False
        c.mark("Account 1", SENDER_ID, "account_hash")
        assert c.blocked("Account 1", SENDER_ID, "account_hash") is True
        # isolation by account / tier
        assert c.blocked("Account 2", SENDER_ID, "account_hash") is False
        assert c.blocked("Account 1", SENDER_ID, "username") is False

    def test_expiry(self):
        c = MentionNegativeCache(ttl_seconds=10)
        c.mark("A", 1, "t")
        c._data[("A", 1, "t")] = time.time() - 11
        assert c.blocked("A", 1, "t") is False


# ─────────────────────── the 5-tier resolution ladder ───────────────────────

@pytest.mark.asyncio
async def test_ladder_tier1_event_sender_hash_first(db):
    """Sending account == capturing account → event sender hash wins."""
    client = FakeClient("Account 1")
    m = _monitor(db, "Account 1", client)
    data = _alert_data()
    tiers = await m._resolve_mention_input_users(client, data)
    assert tiers and tiers[0][0] == "event_sender_hash"
    assert tiers[0][1].access_hash == GOOD_HASH


@pytest.mark.asyncio
async def test_ladder_tier2_captured_input_sender(db):
    client = FakeClient("Account 1")
    m = _monitor(db, "Account 1", client)
    # tier 1 must be skipped: capture had min=True (access_hash None)
    data = _alert_data(sender_access_hash=None)
    _recent_input_senders[(CHAT_ID, MSG_ID)] = {
        "account": "Account 1",
        "input_sender": InputPeerUser(user_id=SENDER_ID, access_hash=GOOD_HASH),
        "ts": time.time(),
    }
    try:
        tiers = await m._resolve_mention_input_users(client, data)
        names = [n for n, _ in tiers]
        assert "captured_input_sender" in names
    finally:
        _recent_input_senders.pop((CHAT_ID, MSG_ID), None)


@pytest.mark.asyncio
async def test_ladder_tier3_own_view_from_message(db):
    """Cross-account: InputUserFromMessage built from the SENDING client's
    own view of the source chat (no hash needed)."""
    client = FakeClient("Account 2")
    client.set_chat(CHAT_ID, InputPeerUser(user_id=1, access_hash=2))
    m = _monitor(db, "Account 2", client)
    data = _alert_data(account_name="Account 1", sender_access_hash=None)
    tiers = await m._resolve_mention_input_users(client, data)
    names = [n for n, _ in tiers]
    assert "own_view_from_message" in names
    tier3 = [iu for n, iu in tiers if n == "own_view_from_message"][0]
    assert isinstance(tier3, InputUserFromMessage) and tier3.user_id == SENDER_ID


@pytest.mark.asyncio
async def test_ladder_tier4_db_fallback_per_account(db):
    client = FakeClient("Account 2")
    client.set_chat(CHAT_ID, InputPeerUser(user_id=1, access_hash=2))
    m = _monitor(db, "Account 2", client)
    await db.upsert_sender_account_hash("Account 2", SENDER_ID, 777000)
    data = _alert_data(account_name="Account 1", sender_access_hash=None)
    tiers = await m._resolve_mention_input_users(client, data)
    by_name = dict(tiers)
    assert "account_hash" in by_name
    assert by_name["account_hash"].access_hash == 777000


@pytest.mark.asyncio
async def test_ladder_tier5_username(db):
    client = FakeClient("Account 1")
    client.set_user("ahmed_99", InputPeerUser(user_id=SENDER_ID, access_hash=GOOD_HASH))
    m = _monitor(db, "Account 1", client)
    data = _alert_data(sender_access_hash=None, sender_username="ahmed_99")
    tiers = await m._resolve_mention_input_users(client, data)
    names = [n for n, _ in tiers]
    assert "username" in names


@pytest.mark.asyncio
async def test_ladder_respects_negative_cache_and_cap(db):
    client = FakeClient("Account 1")
    client.set_chat(CHAT_ID, InputPeerUser(user_id=1, access_hash=2))
    client.set_user("ahmed_99", InputPeerUser(user_id=SENDER_ID, access_hash=GOOD_HASH))
    m = _monitor(db, "Account 1", client)
    data = _alert_data(sender_access_hash=None, sender_username="ahmed_99")
    mention_negative.mark("Account 1", SENDER_ID, "own_view_from_message")
    mention_negative.mark("Account 1", SENDER_ID, "username")
    try:
        tiers = await m._resolve_mention_input_users(client, data)
        names = [n for n, _ in tiers]
        assert "own_view_from_message" not in names
        assert "username" not in names
    finally:
        # cleanup for other tests
        for t in ("own_view_from_message", "username"):
            c = MentionNegativeCache(ttl_seconds=10)
            assert not c.blocked("A", 1, t)
        mon.mention_negative._data.pop(("Account 1", SENDER_ID, "own_view_from_message"), None)
        mon.mention_negative._data.pop(("Account 1", SENDER_ID, "username"), None)


@pytest.mark.asyncio
async def test_ladder_disabled_by_flag(db):
    client = FakeClient("Account 1")
    m = _monitor(db, "Account 1", client)
    data = _alert_data()
    old = CFG.SENDER_MENTION_FIX_ENABLED
    try:
        object.__setattr__(CFG, "SENDER_MENTION_FIX_ENABLED", False)
        assert await m._resolve_mention_input_users(client, data) == []
    finally:
        object.__setattr__(CFG, "SENDER_MENTION_FIX_ENABLED", old)


# ───────────────────── do_send tier advance + fallback ─────────────────────

def _wire_monitor(db, m: EnhancedAccountMonitor, client: FakeClient) -> None:
    """Minimal bot_ref so _send_alert's guards pass."""
    async def _can_proceed(account_name):
        return True

    m._bot_ref = SimpleNamespace(
        alert_sender_client=None,
        main_client=client,
        monitors=[m],
        rate_limiter=SimpleNamespace(can_proceed=_can_proceed),
        _note_alert_message=lambda *a, **k: None,
    )
    m.client = client

    async def _fake_chat_info(cl, chat_id, message_id, chat_access_hash=None, chat_username=None):
        return {"entity": None, "title": "مجموعة", "group_link": "#", "msg_link": "#"}

    m._chat_info = _fake_chat_info


@pytest.mark.asyncio
async def test_do_send_advances_to_next_tier_on_invalid(db):
    """Tier 1 hash invalid for the sending account → USER_ID_INVALID →
    next tier succeeds. The alert is delivered WITH a real mention and the
    text is identical to the frozen format."""
    client = FakeClient("Account 1", bad_hash=GOOD_HASH)
    client.set_chat(CHAT_ID, InputPeerUser(user_id=1, access_hash=2))
    m = _monitor(db, "Account 1", client)
    _wire_monitor(db, m, client)
    data = _alert_data(sender_id=555000201, text="ابغي حل اسيمنت فيزياءجة")
    await m._send_alert(data, "واجب", data["text"], "hash-1", {"rule_tag": None})
    mention_calls = [c for c in client.calls if c.get("formatting_entities")]
    assert mention_calls, "a real-mention send must have been attempted"
    # the successful call used a tier whose hash was NOT the bad one
    final = mention_calls[-1]
    ius = [e.user_id for e in final["formatting_entities"]
           if isinstance(e, InputMessageEntityMentionName)]
    assert ius and ius[0].user_id == data["sender_id"]
    # the winning tier must NOT carry the hash the account rejected
    assert getattr(ius[0], "access_hash", None) != GOOD_HASH
    # wire text is the parsed v10.8 message: same content & field order
    assert "أحمد" in final["text"] and "المرسل : ID 555000201" in final["text"]
    assert "👤" in final["text"]


@pytest.mark.asyncio
async def test_do_send_falls_back_to_plain_html_when_all_tiers_invalid(db):
    """All mention tiers invalid → the ORIGINAL parse_mode="html" path runs
    (byte-identical wire output to v10.3 — the format never regresses)."""
    client = FakeClient("Account 1", fail_all_mentions=True)
    m = _monitor(db, "Account 1", client)
    _wire_monitor(db, m, client)
    data = _alert_data(sender_id=555000202, text="عايز مشروع تخرج جاهز بالكود")
    await m._send_alert(data, "مشروع", data["text"], "hash-2", {"rule_tag": None})
    assert any(c.get("parse_mode") == "html" for c in client.calls)
    assert not any(c.get("formatting_entities") and not isinstance(c, SimpleNamespace) for c in client.calls if "parse_mode" not in c)


@pytest.mark.asyncio
async def test_do_send_without_any_tier_uses_plain_path(db):
    """No mention candidates at all → exactly one parse_mode='html' send
    (v10.3 wire behavior, zero entity substitution)."""
    client = FakeClient("Account 1", fail_all_mentions=True)
    m = _monitor(db, "Account 1", client)
    _wire_monitor(db, m, client)
    data = _alert_data(sender_id=555000203, text="احتاج تلخيص مادة احصاء اليوم", sender_access_hash=None, sender_username=None)
    old_flag = CFG.SENDER_MENTION_FIX_ENABLED
    try:
        object.__setattr__(CFG, "SENDER_MENTION_FIX_ENABLED", False)
        await m._send_alert(data, "تلخيص", data["text"], "hash-3", {"rule_tag": None})
    finally:
        object.__setattr__(CFG, "SENDER_MENTION_FIX_ENABLED", old_flag)
    kinds = [(c.get("parse_mode"), c.get("formatting_entities")) for c in client.calls]
    assert ("html", None) in kinds


# ─────────────────────── capability-aware send order ───────────────────────

@pytest.mark.asyncio
async def test_resolve_send_clients_prefers_capable_capturing_account(db):
    sticky_client = FakeClient("Account 9")
    main_client = FakeClient("Account 2")
    capturing = FakeClient("Account 1")
    m = _monitor(db, "Account 1", capturing)
    other = SimpleNamespace(account={"name": "Account 2"}, client=main_client, is_connected=True)
    m._bot_ref = SimpleNamespace(
        alert_sender_client=None, main_client=main_client,
        monitors=[m, other], monitors_at_boot=None,
    )
    _post_capable_accounts.add("Account 1")
    try:
        clients = await m._resolve_send_clients({"account_name": "Account 1"})
        assert clients[0] is capturing  # capable capturing account jumps ahead
        assert main_client in clients
    finally:
        _post_capable_accounts.discard("Account 1")


@pytest.mark.asyncio
async def test_resolve_send_clients_no_capability_no_change(db):
    main_client = FakeClient("Account 2")
    capturing = FakeClient("Account 1")
    m = _monitor(db, "Account 1", capturing)
    other = SimpleNamespace(account={"name": "Account 2"}, client=main_client, is_connected=True)
    m._bot_ref = SimpleNamespace(
        alert_sender_client=None, main_client=main_client, monitors=[m, other],
    )
    clients = await m._resolve_send_clients({"account_name": "Account 1"})
    assert clients[0] is main_client  # unproven account never jumps ahead


@pytest.mark.asyncio
async def test_sticky_sender_stays_first(db):
    sticky = FakeClient("Account 9")
    capturing = FakeClient("Account 1")
    m = _monitor(db, "Account 1", capturing)
    m._bot_ref = SimpleNamespace(
        alert_sender_client=sticky, main_client=None, monitors=[m],
    )
    _post_capable_accounts.add("Account 1")
    try:
        clients = await m._resolve_send_clients({"account_name": "Account 1"})
        assert clients[0] is sticky
    finally:
        _post_capable_accounts.discard("Account 1")


# ─────────────────────────── latency + db plumbing ───────────────────────────

class TestAlertLatencyTracker:
    def test_record_and_percentiles(self):
        t = AlertLatencyTracker(maxlen=100)
        for i in range(10):
            t.record(receive_lag_ms=100.0 + i, msg_date_ts=time.time() - (1.0 + i / 1000))
        snap = t.snapshot()
        assert snap["samples"] == 10
        assert snap["receive_lag_ms"]["p50"] >= 100
        assert snap["total_lag_ms"]["p90"] >= 1000
        assert snap["receive_lag_ms"]["p50"] <= snap["receive_lag_ms"]["p99"]

    def test_missing_values_do_not_crash(self):
        t = AlertLatencyTracker()
        t.record(None, None)
        assert t.snapshot()["samples"] == 1
        assert "receive_lag_ms" not in t.snapshot()


@pytest.mark.asyncio
async def test_db_sender_account_hash_roundtrip(db):
    assert await db.get_sender_account_hash("Account 1", SENDER_ID) is None
    assert await db.upsert_sender_account_hash("Account 1", SENDER_ID, GOOD_HASH) is True
    assert await db.get_sender_account_hash("Account 1", SENDER_ID) == GOOD_HASH
    # per-account isolation at DB level
    assert await db.get_sender_account_hash("Account 2", SENDER_ID) is None
    # update wins
    await db.upsert_sender_account_hash("Account 1", SENDER_ID, 555000)
    assert await db.get_sender_account_hash("Account 1", SENDER_ID) == 555000
    # invalid input rejected
    assert await db.upsert_sender_account_hash("Account 1", SENDER_ID, 0) is False


@pytest.mark.asyncio
async def test_db_recent_chat_ids(db):
    ids = await db.recent_chat_ids(limit=10)
    assert isinstance(ids, list)  # empty DB → [] (never raises)


@pytest.mark.asyncio
async def test_add_to_queue_signals_wakeup_event(db):
    assert not db.queue_notify.is_set()
    res = await db.add_to_queue({"chat_id": 1, "message_id": 2}, priority=5)
    assert res > 0
    assert db.queue_notify.is_set()
    db.queue_notify.clear()


# ─────────────────────── format-frozen regression guard ───────────────────────

@pytest.mark.asyncio
async def test_alert_builder_output_is_mention_fixable(db):
    """The REAL _build_alert output must be fixable by the mention builder
    without any text change — the frozen contract, verified end-to-end.
    v10.8: النمط الجديد — مرساة tg://user?id= تبقى في سطر 👤 فيُعيد
    build_mention_entities استبدالها بـmention حقيقي."""
    m = _monitor(db, "Account 1", FakeClient("Account 1"))
    sender = {"id": SENDER_ID, "display": "أحمد", "username": None, "access_hash": GOOD_HASH}
    chat = {"entity": None, "title": "مجموعة", "group_link": "#", "msg_link": "#",
            "id": CHAT_ID, "message_id": MSG_ID, "username": None}
    alert_text, _buttons = m._build_alert(sender, chat, "واجب", "محتاج حل واجب", {"rule_tag": None})
    out = build_mention_entities(alert_text, SENDER_ID, _iu(GOOD_HASH))
    expected_text, _ = tg_html.parse(alert_text)
    assert out is not None
    assert out[0] == expected_text
    mentions = _entities_by_type(out[1], InputMessageEntityMentionName)
    assert len(mentions) == 1
    assert mentions[0].user_id.access_hash == GOOD_HASH
    # v10.8: النمط الجديد بلا blockquote — الاسم نفسه صار الـmention
    assert "أحمد" in out[0]


@pytest.mark.asyncio
async def test_username_sender_output_not_converted(db):
    """When a username exists, the frozen builder uses t.me — the mention
    fixer must leave it exactly as-is (out=None → original wire path)."""
    m = _monitor(db, "Account 1", FakeClient("Account 1"))
    sender = {"id": SENDER_ID, "display": "أحمد", "username": "ahmed_99", "access_hash": GOOD_HASH}
    chat = {"entity": None, "title": "مجموعة", "group_link": "#", "msg_link": "#",
            "id": CHAT_ID, "message_id": MSG_ID, "username": None}
    alert_text, _ = m._build_alert(sender, chat, "واجب", "محتاج حل واجب", {"rule_tag": None})
    assert build_mention_entities(alert_text, SENDER_ID, _iu(GOOD_HASH)) is None
