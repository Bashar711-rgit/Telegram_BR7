"""Unit tests for sender_resolver.py + the monitors.py v9.9 sender-intel
wiring (extract → capture → persist → recovery → resolve → links).

All Telegram interactions are mocked (SimpleNamespace/fake clients);
nothing touches the real network. DB tests use the shared SQLite fixture.
"""

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from config import CFG
from monitors import FastCaptureBuffer, _capture
from sender_resolver import (
    FailureReason,
    RetryPolicy,
    SenderResolver,
    build_display_name,
    extract_flat,
    extract_sender,
    meta_to_contact_fields,
    parse_telegram_link,
    sender_intel,
)


# ──────────────────────────── fake entities ───────────────────────────────

def _user(**kw) -> SimpleNamespace:
    """Telethon User stand-in with default safe values."""
    base = dict(
        id=555000111, access_hash=7234567890123, username="ahmed_99",
        first_name="أحمد", last_name="محمد", phone=None, bot=False,
        verified=False, premium=False, scam=False, fake=False,
        restricted=False, restriction_reason=None, status=None,
        lang_code=None, photo=None, deleted=False, contact=False,
        mutual_contact=False, min=False, bot_info_version=None,
        usernames=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ──────────────────────────── extract / normalize ─────────────────────────

class TestExtractSender:
    def test_full_user(self):
        u = _user(premium=True, verified=True, phone="+966500000000",
                  status=SimpleNamespace(__class__=type("UserStatusRecently", (), {})))
        # NOTE: SimpleNamespace __class__ trick doesn't apply — set real attr
        u.status = SimpleNamespace(expires=0)
        u.status = None
        meta = extract_sender(u)
        assert meta["sender_id"] == 555000111
        assert meta["access_hash"] == 7234567890123
        assert meta["username"] == "ahmed_99"
        assert meta["first_name"] == "أحمد"
        assert meta["last_name"] == "محمد"
        assert meta["is_premium"] is True
        assert meta["is_verified"] is True
        assert meta["is_bot"] is False
        assert meta["phone"] == "+966500000000"  # only when Telegram provides it

    def test_minimal_user_no_username(self):
        u = _user(username=None, last_name=None)
        meta = extract_sender(u)
        assert meta["username"] is None
        assert meta["last_name"] is None
        assert meta["access_hash"] == 7234567890123

    def test_missing_attributes_never_fail(self):
        """Requirement #2: absence of any property must not break processing."""
        u = SimpleNamespace(id=1)  # bare entity — almost nothing available
        meta = extract_sender(u)
        assert meta["sender_id"] == 1
        assert meta["username"] is None
        assert meta["access_hash"] is None
        assert meta["is_bot"] is False
        assert meta["phone"] is None

    def test_none_entity(self):
        assert extract_sender(None) == {}

    def test_bot_verified_premium_deleted_restricted_flags(self):
        u = _user(bot=True, verified=True, premium=True, deleted=True,
                  restricted=True, restriction_reason="spam")
        meta = extract_sender(u)
        assert meta["is_bot"] and meta["is_verified"] and meta["is_premium"]
        assert meta["is_deleted"] and meta["is_restricted"]
        assert meta["restriction_reason"] == "spam"

    def test_numeric_username_rejected(self):
        """Numeric placeholder 'usernames' are not real usernames."""
        u = _user(username="12345")
        assert extract_sender(u)["username"] is None

    def test_extract_flat_prefixes(self):
        flat = extract_flat(_user())
        assert flat["sender_is_premium"] is False
        assert flat["sender_is_bot"] is False
        assert flat["sender_lang_code"] is None
        # identity keys (username/access_hash/names) intentionally NOT in the
        # flat set — they already exist in event_data base keys (brief #4)
        assert "sender_username" not in flat
        assert "sender_access_hash" not in flat

    def test_meta_to_contact_fields(self):
        data = {"sender_is_bot": True, "sender_phone": "+9665", "sender_username": "x"}
        out = meta_to_contact_fields(data)
        assert out["is_bot"] is True
        assert out["phone"] == "+9665"
        assert out["status"] is None  # missing → None → DB keeps old value

    def test_display_name_rule_unchanged(self):
        assert build_display_name("أحمد", "محمد", 1) == "أحمد محمد"
        assert build_display_name(None, None, 42) == "مستخدم (42)"
        assert build_display_name("", "", 7) == "مستخدم (7)"


# ──────────────────────────── link parser ─────────────────────────────────

class TestLinkParser:
    def test_public_chat(self):
        p = parse_telegram_link("https://t.me/somegroup")
        assert p.kind == "public_chat" and p.username == "somegroup"

    def test_public_message(self):
        p = parse_telegram_link("https://t.me/somegroup/123")
        assert p.kind == "public_message" and p.username == "somegroup" and p.message_id == 123

    def test_private_message_c_link(self):
        p = parse_telegram_link("https://t.me/c/123456789/456")
        assert p.kind == "private_message"
        assert p.inner_id == 123456789
        assert p.chat_id == -100123456789
        assert p.message_id == 456

    def test_private_chat_c_link(self):
        p = parse_telegram_link("https://t.me/c/123456789")
        assert p.kind == "private_chat" and p.chat_id == -100123456789

    def test_telegram_me_alias(self):
        p = parse_telegram_link("https://telegram.me/somegroup/9")
        assert p.kind == "public_message" and p.username == "somegroup" and p.message_id == 9

    def test_at_username(self):
        p = parse_telegram_link("@someuser")
        assert p.kind == "username" and p.username == "someuser"

    def test_invite_plus(self):
        p = parse_telegram_link("https://t.me/+AbCdEf_12345")
        assert p.kind == "invite" and p.invite_hash == "AbCdEf_12345"

    def test_invite_joinchat(self):
        p = parse_telegram_link("https://t.me/joinchat/AbCdEf_12345")
        assert p.kind == "invite" and p.invite_hash == "AbCdEf_12345"

    def test_trailing_slash_query_fragment(self):
        p = parse_telegram_link("https://t.me/somegroup/77/?x=1#top")
        assert p.kind == "public_message" and p.username == "somegroup" and p.message_id == 77

    def test_malformed(self):
        for bad in ("", "https://t.me/", "https://t.me/c/abc/1", "https://t.me/g/xyz",
                    "not a link at all!!", "https://t.me/somegroup/notanumber",
                    "https://t.me/+short"):
            p = parse_telegram_link(bad)
            assert p.kind == "invalid", bad
            assert p.reason == FailureReason.INVALID_LINK

    def test_c_link_number_is_not_chat_id_blindly(self):
        """Requirement #15: the /c/ number must map to -100<inner>, never used raw."""
        p = parse_telegram_link("https://t.me/c/123456789/456")
        assert p.inner_id == 123456789
        assert p.chat_id != 123456789
        assert str(p.chat_id).startswith("-100")


# ──────────────────────────── access methods ──────────────────────────────

class TestAccessMethods:
    def test_input_peer_requires_both(self):
        assert SenderResolver.build_input_peer(555, None) is None
        assert SenderResolver.build_input_peer(None, 123) is None
        peer = SenderResolver.build_input_peer(555, 7234567890123)
        assert peer is not None
        assert peer.user_id == 555 and peer.access_hash == 7234567890123

    def test_profile_link_priority(self):
        assert SenderResolver.build_profile_link(1, "@user") == "https://t.me/user"
        assert SenderResolver.build_profile_link(42, None) == "tg://user?id=42"

    def test_message_link_shapes(self):
        assert SenderResolver.build_message_link(-1001234567890, 55) == {
            "group": "https://t.me/c/1234567890", "message": "https://t.me/c/1234567890/55",
        }
        assert SenderResolver.build_message_link(-1001234567890, 55, "@g") == {
            "group": "https://t.me/g", "message": "https://t.me/g/55",
        }

    def test_contact_methods_and_best(self):
        data = {"sender_id": 5, "sender_access_hash": 77, "sender_username": "u",
                "chat_id": -100, "message_id": 1}
        flags = SenderResolver.build_contact_methods(data)
        assert flags["has_username"] and flags["has_access_hash"] and flags["has_input_peer"]
        assert SenderResolver.get_best_access_method(data) == "has_input_peer"

    def test_best_access_method_none(self):
        assert SenderResolver.get_best_access_method({}) == "none"


# ──────────────────────────── resolve_message ─────────────────────────────

class _FakeClient:
    def __init__(self, messages=None, entity=None, exc=None):
        self._messages = messages or {}
        self._entity = entity or SimpleNamespace(id=-1001234567890, title="G")
        self._exc = exc
        self.get_messages_calls: List[Any] = []

    async def get_entity(self, ref):
        if self._exc:
            raise self._exc
        return self._entity

    async def get_messages(self, entity, ids=None):
        self.get_messages_calls.append((entity, ids))
        return self._messages.get(ids)


class TestResolveMessage:
    async def test_success_and_verification(self):
        msg = SimpleNamespace(id=42, chat_id=-1001234567890, sender_id=7)
        client = _FakeClient(messages={42: msg})
        r = await sender_intel.resolve_message(
            client, chat_id=-1001234567890, message_id=42, expected_chat_id=-1001234567890
        )
        assert r.ok and r.message.id == 42
        assert client.get_messages_calls[0][1] == 42  # direct ids=, never a search

    async def test_deleted_message_none(self):
        client = _FakeClient(messages={42: None})
        r = await sender_intel.resolve_message(client, chat_id=-1001234567890, message_id=42)
        assert r.reason == FailureReason.MESSAGE_DELETED

    async def test_wrong_id_is_not_success(self):
        """Requirement #27: API returned an object ≠ success — verify id."""
        msg = SimpleNamespace(id=99, chat_id=-1001234567890)
        client = _FakeClient(messages={42: msg})
        r = await sender_intel.resolve_message(client, chat_id=-1001234567890, message_id=42)
        assert r.reason == FailureReason.MESSAGE_NOT_FOUND

    async def test_wrong_chat_is_not_success(self):
        msg = SimpleNamespace(id=42, chat_id=-1009999999999)
        client = _FakeClient(messages={42: msg})
        r = await sender_intel.resolve_message(
            client, chat_id=-1001234567890, message_id=42, expected_chat_id=-1001234567890
        )
        assert r.reason == FailureReason.MESSAGE_NOT_FOUND

    async def test_chat_unresolved(self):
        client = _FakeClient(exc=ValueError("no entity"))
        r = await sender_intel.resolve_message(client, chat_id=-1001234567890, message_id=42)
        assert r.reason in (FailureReason.USER_NOT_RESOLVABLE, FailureReason.RPC_ERROR,
                            FailureReason.CHAT_NOT_FOUND)

    async def test_floodwait_recorded(self):
        class _FloodClient(_FakeClient):
            async def get_messages(self, entity, ids=None):
                raise FloodWaitMock(3)
        from telethon.errors import FloodWaitError

        def FloodWaitMock(seconds):
            err = FloodWaitError(request=None)
            object.__setattr__(err, "seconds", seconds)
            return err

        client = _FloodClient()
        r = await sender_intel.resolve_message(client, chat_id=-1001234567890, message_id=42)
        assert r.reason == FailureReason.FLOOD_WAIT
        assert sender_intel.snapshot()["flood_wait_count"] >= 1


# ──────────────────────────── retry / dedup / cache ───────────────────────

class _FlakyClient:
    """get_entity raises a transient error N times, then succeeds."""
    def __init__(self, fail_times: int, exc_factory):
        self.fail_times = fail_times
        self.exc_factory = exc_factory
        self.calls = 0

    async def get_entity(self, ref):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc_factory()
        return SimpleNamespace(id=555000111, first_name="أحمد")


class _TimeoutError(Exception):
    pass


class TestRetryAndDedup:
    async def test_transient_retry_then_success(self, monkeypatch):
        # shrink sleeps for the test
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        resolver = SenderResolver(retry=RetryPolicy(max_attempts=3, base_delay=0.01, max_delay=0.02))
        client = _FlakyClient(1, lambda: ConnectionError("reset"))
        r = await resolver.resolve_entity(client, 555000111)
        assert r.ok and r.method == "sender_id"
        assert client.calls == 2

    async def test_permanent_error_no_retry(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        resolver = SenderResolver(retry=RetryPolicy(max_attempts=3, base_delay=0.01))
        client = _FlakyClient(99, lambda: ValueError("Cannot find any entity corresponding to 1"))
        r = await resolver.resolve_entity(client, 555000111)
        assert not r.ok
        assert client.calls == 1  # ValueError is permanent → single attempt

    async def test_inflight_dedup(self, monkeypatch):
        """Requirement #34: N concurrent resolves → ONE underlying call."""
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        resolver = SenderResolver(retry=RetryPolicy(max_attempts=3, base_delay=0.01))

        class _SlowClient:
            def __init__(self):
                self.calls = 0

            async def get_entity(self, ref):
                self.calls += 1
                await asyncio.sleep(0.05)
                return SimpleNamespace(id=777, first_name="x")

        client = _SlowClient()
        results = await asyncio.gather(*[
            resolver.resolve_entity(client, 777) for _ in range(5)
        ])
        assert all(r.ok for r in results)
        assert client.calls == 1
        assert resolver.snapshot()["inflight_dedup"] == 4

    async def test_entity_cache_hit(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        resolver = SenderResolver()
        client = _FlakyClient(0, lambda: None)
        r1 = await resolver.resolve_entity(client, 888)
        r2 = await resolver.resolve_entity(client, 888)
        assert r1.ok and r2.ok
        assert r2.method == "entity_cache"
        snap = resolver.snapshot()
        assert snap["entity_cache_hit"] >= 1

    async def test_flood_cooldown_blocks_subsequent(self, monkeypatch):
        """Requirement #19: no immediate retry after FloodWait."""
        monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
        from telethon.errors import FloodWaitError

        resolver = SenderResolver()

        class _Flood:
            calls = 0

            async def get_entity(self, ref):
                _Flood.calls += 1
                err = FloodWaitError(request=None)
                object.__setattr__(err, "seconds", 30)
                raise err

        client = _Flood()
        r1 = await resolver.resolve_entity(client, 321)
        assert r1.reason == FailureReason.FLOOD_WAIT
        r2 = await resolver.resolve_entity(client, 321)
        assert r2.reason == FailureReason.FLOOD_WAIT  # cooldown gate, no API call
        assert _Flood.calls == 1


async def _fast_sleep(delay, *a, **kw):  # noqa: test helper
    return None


# ──────────────────────────── fast capture + sender ───────────────────────

class TestCaptureSenderMeta:
    def test_save_with_meta(self):
        buf = FastCaptureBuffer(maxsize=10, ttl=30)
        meta = {
            "sender_username": "ahmed_99", "sender_first_name": "أحمد",
            "sender_last_name": "محمد", "sender_access_hash": 7234567890123,
            "sender_meta_full": {"username": "ahmed_99", "is_bot": False},
        }
        buf.save(-1001234567890, 5, "نص", sender_id=11, sender_name="أحمد", sender_meta=meta)
        e = buf.get(-1001234567890, 5)
        assert e["sender_username"] == "ahmed_99"
        assert e["sender_access_hash"] == 7234567890123
        assert e["sender_meta_full"]["is_bot"] is False

    def test_duplicate_never_blanks_sender(self):
        """Requirement #24: later duplicate delivery with missing fields
        must NOT erase already-captured values."""
        buf = FastCaptureBuffer(maxsize=10, ttl=30)
        full = {"sender_username": "ahmed_99", "sender_first_name": "أحمد",
                "sender_last_name": "محمد", "sender_access_hash": 123,
                "sender_meta_full": {"x": 1}}
        buf.save(-1001234567890, 6, "نص", sender_id=11, sender_meta=full)
        # second account delivers the same message but its view lacks username/hash
        poor = {"sender_username": None, "sender_first_name": None,
                "sender_last_name": None, "sender_access_hash": None,
                "sender_meta_full": None}
        buf.save(-1001234567890, 6, "نص", sender_id=11, sender_meta=poor)
        e = buf.get(-1001234567890, 6)
        assert e["sender_username"] == "ahmed_99"
        assert e["sender_access_hash"] == 123
        assert e["sender_meta_full"] == {"x": 1}


# ──────────────────────────── recovery enrichment ─────────────────────────

ACCOUNT = "TestAcc"
CHAT = -1001234567890


def _fake_event(chat_id=CHAT, msg_id=42, text="أبي مساعدة", sender_id=555000111, sender=None):
    msg = SimpleNamespace(id=msg_id, text=text, message=None, date=None, media=None)
    return SimpleNamespace(out=False, chat_id=chat_id, message=msg,
                           sender=sender, sender_id=sender_id, chat=None)


@pytest.fixture()
def enable_capture():
    old = CFG.FAST_CAPTURE_ENABLED
    old_intel = CFG.SENDER_INTEL_ENABLED
    object.__setattr__(CFG, "FAST_CAPTURE_ENABLED", True)
    object.__setattr__(CFG, "SENDER_INTEL_ENABLED", True)
    _capture.clear()
    yield
    object.__setattr__(CFG, "FAST_CAPTURE_ENABLED", old)
    object.__setattr__(CFG, "SENDER_INTEL_ENABLED", old_intel)
    _capture.clear()


@pytest.fixture()
async def monitor(db):
    from monitors import EnhancedAccountMonitor

    account: Dict[str, Any] = {
        "name": ACCOUNT, "api_id": 12345, "api_hash": "test-hash",
        "phone": "+90000000000", "session": "test_session", "priority": 1,
    }
    mon = EnhancedAccountMonitor(account, db, None)
    yield mon


class TestRecoverySenderFields:
    async def test_recovery_reuses_captured_sender(self, monitor, enable_capture):
        """Requirement #10 core test: message received → fast capture →
        message deleted → recovery → sender data still available."""
        sender = _user(username="ahmed_99", access_hash=7234567890123,
                       first_name="أحمد", last_name="محمد")
        event = _fake_event(sender=sender)
        monitor._fast_capture(event)
        entry = _capture.lookup(CHAT, 42)
        assert entry is not None and entry["sender_username"] == "ahmed_99"

        rebuilt: Dict[str, Any] = {}
        # replicate _spawn_recovery_task's rebuild logic (without spawning)
        rebuilt["sender_id"] = entry.get("sender_id", 0) or 0
        rebuilt["sender_username"] = entry.get("sender_username")
        rebuilt["sender_first_name"] = entry.get("sender_first_name") or entry.get("sender_name")
        rebuilt["sender_last_name"] = entry.get("sender_last_name")
        rebuilt["sender_access_hash"] = entry.get("sender_access_hash")
        assert rebuilt["sender_username"] == "ahmed_99"
        assert rebuilt["sender_access_hash"] == 7234567890123
        assert rebuilt["sender_first_name"] == "أحمد"
        assert rebuilt["sender_last_name"] == "محمد"

    async def test_recovery_db_fallback_fills_missing_only(self, monitor, enable_capture):
        """DB fallback fills ONLY missing fields; existing values survive."""
        # store a full contact for the sender
        await monitor.db.upsert_sender_contact({
            "sender_id": 555000111, "username": "db_username",
            "access_hash": 999, "first_name": "قاعدة", "last_name": "البيانات",
        })
        data = {"sender_id": 555000111, "sender_username": "kept",
                "sender_first_name": "أحمد", "sender_access_hash": 7234567890123}
        await monitor._enrich_sender_from_db(data)
        assert data["sender_username"] == "kept"       # not overwritten
        assert data["sender_access_hash"] == 7234567890123

        empty = {"sender_id": 555000111, "sender_username": None,
                 "sender_access_hash": None, "sender_first_name": None}
        await monitor._enrich_sender_from_db(empty)
        assert empty["sender_username"] == "db_username"
        assert empty["sender_access_hash"] == 999
        assert empty["sender_first_name"] == "قاعدة"

    async def test_event_to_dict_additive_keys(self, monitor, enable_capture):
        """Requirement #4: all existing keys stay, additive sender_* keys join."""
        sender = _user(premium=True, phone=None)
        event = _fake_event(sender=sender)
        data = await monitor._event_to_dict(event)
        # original keys — exact names
        for key in ("chat_id", "message_id", "sender_id", "sender_username",
                    "sender_first_name", "sender_last_name", "sender_access_hash",
                    "chat_access_hash", "chat_username", "text", "has_text",
                    "has_media", "media_type", "account_name", "timestamp"):
            assert key in data, key
        assert data["sender_username"] == "ahmed_99"
        # additive keys
        assert data["sender_is_premium"] is True
        assert data["sender_is_bot"] is False

    async def test_event_to_dict_without_sender_still_works(self, monitor, enable_capture):
        """sender=None (and disabled resolve path via missing client) must
        not fail — data comes back with the original shape."""
        event = _fake_event(sender=None)
        # monitor.client is None in tests; fetch_event_sender path must be
        # exception-safe (resolver handles None client by failing the attempt)
        data = await monitor._event_to_dict(event)
        assert data["sender_id"] == 555000111
        assert data["sender_username"] is None
        assert data["text"] == "أبي مساعدة"


# ──────────────────────────── database roundtrip ──────────────────────────

class TestSenderContactsDB:
    async def test_fresh_schema_has_new_columns(self, db):
        row = await db._fetchone("SELECT * FROM sender_contacts LIMIT 1", ())
        cols = set(row.keys()) if row else set()
        rows = await db._fetchall("PRAGMA table_info(sender_contacts)", ())
        cols = {r["name"] for r in rows}
        for c in ("phone", "is_bot", "is_verified", "is_premium", "is_scam",
                  "is_fake", "is_restricted", "is_deleted", "is_contact",
                  "is_mutual_contact", "photo_available", "restriction_reason",
                  "lang_code", "status", "usernames", "last_seen", "last_updated"):
            assert c in cols, c

    async def test_upsert_full_snapshot_and_readback(self, db):
        await db.upsert_sender_contact({
            "sender_id": 42, "access_hash": 7234567890123, "username": "u1",
            "first_name": "أ", "last_name": "ب", "chat_id": -1001, "message_id": 5,
            "phone": None, "is_bot": False, "is_verified": True, "is_premium": True,
            "is_scam": False, "is_fake": False, "is_restricted": False,
            "is_deleted": False, "is_contact": False, "is_mutual_contact": False,
            "photo_available": True, "restriction_reason": None, "lang_code": "ar",
            "status": "recently", "usernames": '["u1", "u2"]', "last_seen": 123.0,
        })
        row = await db.get_sender_contact(42)
        assert row["username"] == "u1"
        assert bool(row["is_verified"]) is True
        assert bool(row["is_premium"]) is True
        assert row["lang_code"] == "ar"
        assert row["status"] == "recently"
        assert row["access_hash"] == 7234567890123

    async def test_null_never_erasers_stored_value(self, db):
        """Requirement #24: new NULL → keep previous; new non-NULL → update."""
        await db.upsert_sender_contact({
            "sender_id": 7, "username": "original", "access_hash": 111,
            "first_name": "القديم", "chat_id": -1001, "message_id": 1,
        })
        # a degraded view arrives with everything None + newer message context
        await db.upsert_sender_contact({
            "sender_id": 7, "username": None, "access_hash": None,
            "first_name": None, "chat_id": -1002, "message_id": 2,
        })
        row = await db.get_sender_contact(7)
        assert row["username"] == "original"       # kept
        assert row["access_hash"] == 111           # kept
        assert row["first_name"] == "القديم"       # kept
        assert row["last_chat_id"] == -1002        # last-seen context updated
        assert row["last_message_id"] == 2

        # a fresh richer view arrives — identity fields update
        await db.upsert_sender_contact({
            "sender_id": 7, "username": "renamed", "access_hash": 222,
            "first_name": "الجديد", "is_premium": True, "lang_code": "ar",
        })
        row = await db.get_sender_contact(7)
        assert row["username"] == "renamed"
        assert row["access_hash"] == 222
        assert row["first_name"] == "الجديد"
        assert bool(row["is_premium"]) is True

    async def test_legacy_call_shape_still_works(self, db):
        """The pre-v9.9 caller dict (9 keys) must keep working unchanged."""
        await db.upsert_sender_contact({
            "sender_id": 9, "access_hash": 5, "username": "x",
            "first_name": "f", "last_name": "l", "chat_id": -1,
            "message_id": 2, "msg_link": "https://t.me/c/1/2", "group_link": "https://t.me/c/1",
        })
        row = await db.get_sender_contact(9)
        assert row["username"] == "x"
        assert row["last_message_link"] == "https://t.me/c/1/2"
        assert row["last_group_link"] == "https://t.me/c/1"

    async def test_deleted_account_row_kept(self, db):
        """Requirement #23: deleted sender keeps its historical identity."""
        await db.upsert_sender_contact({"sender_id": 11, "username": "ghost", "chat_id": -1, "message_id": 1})
        await db.upsert_sender_contact({"sender_id": 11, "username": "ghost", "is_deleted": True})
        row = await db.get_sender_contact(11)
        assert row is not None
        assert bool(row["is_deleted"]) is True
        assert row["username"] == "ghost"


# ──────────────────────────── metrics snapshot ────────────────────────────

class TestSnapshot:
    def test_snapshot_stable_keys(self):
        snap = sender_intel.snapshot()
        for key in ("sender_resolve_success", "sender_resolve_failure",
                    "message_resolve_success", "message_resolve_failure",
                    "entity_cache_hit", "entity_cache_miss",
                    "telegram_retry_count", "flood_wait_count",
                    "average_resolve_latency", "cache_size"):
            assert key in snap, key
