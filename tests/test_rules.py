"""Tests for the v9.21 Rule Engine (P2) + Allowlist (P3).

Covers: AND matching, priority order, enable/disable, fail-safe paths,
batched hits flush, alert tagging, API auth + validation + audit trail.
The golden compatibility guarantee is implicit everywhere: empty tables
change nothing (original behavior, verbatim).
"""

import json
import time

import pytest

from database import EnhancedDatabase


# ---------------------------------------------------------------------------
# DB layer: conditions validation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestRuleValidation:
    async def test_valid_conditions_pass(self, db):
        clean, err = db.validate_rule_conditions({"keyword": "طلب", "min_confidence": 0.5})
        assert err is None and clean == {"keyword": "طلب", "min_confidence": 0.5}

    async def test_empty_conditions_rejected(self, db):
        clean, err = db.validate_rule_conditions({})
        assert clean is None and err is not None
        clean, err = db.validate_rule_conditions("not-a-dict")
        assert clean is None and err is not None

    async def test_unknown_key_rejected(self, db):
        clean, err = db.validate_rule_conditions({"hacker_key": 1})
        assert clean is None and "unknown condition key" in err

    async def test_numeric_ids_coerced(self, db):
        clean, err = db.validate_rule_conditions({"sender_id": "711", "source_chat_id": -100})
        assert err is None
        assert clean["sender_id"] == 711 and clean["source_chat_id"] == -100

    async def test_non_numeric_id_rejected(self, db):
        clean, err = db.validate_rule_conditions({"sender_id": "abc"})
        assert clean is None and err is not None

    async def test_confidence_bounds(self, db):
        clean, err = db.validate_rule_conditions({"min_confidence": 1.5})
        assert clean is None and err is not None
        clean, err = db.validate_rule_conditions({"min_confidence": 0.0})
        assert err is None and clean["min_confidence"] == 0.0


# ---------------------------------------------------------------------------
# DB layer: matching semantics
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestRuleEngine:
    async def _rule(self, db, conditions, action="block", priority=100, name="t"):
        return await db.add_rule(name, conditions, action, "v" if action == "tag" else "",
                                 priority)

    async def test_no_rules_no_effect(self, db):
        """Golden compatibility: empty table → apply_rules returns None."""
        assert await db.apply_rules("أي نص", source_chat_id=1, sender_id=2) is None

    async def test_keyword_and_match(self, db):
        await self._rule(db, {"keyword": "حل"}, action="block")
        hit = await db.apply_rules("أريد حل الواجب", source_chat_id=1, sender_id=2)
        assert hit is not None and hit["action"] == "block"
        miss = await db.apply_rules("نص لا يحوي الكلمة", source_chat_id=1, sender_id=2)
        assert miss is None  # AND semantics: keyword absent → no match

    async def test_case_insensitive_keyword(self, db):
        await self._rule(db, {"keyword": "SOLVED"}, action="block")
        hit = await db.apply_rules("please see this solved example", source_chat_id=1)
        assert hit is not None

    async def test_priority_order(self, db):
        """First matching rule by priority ASC then id ASC wins."""
        low = await self._rule(db, {"keyword": "x"}, action="block", priority=200, name="low")
        high = await self._rule(db, {"keyword": "x"}, action="allow", priority=1, name="high")
        hit = await db.apply_rules("xx", source_chat_id=0)
        assert hit["action"] == "allow"
        assert hit["rule"]["id"] == high["id"]
        assert hit["rule"]["id"] != low["id"]

    async def test_disabled_rules_ignored(self, db):
        rule = await self._rule(db, {"keyword": "y"}, action="block")
        await db.set_rule_enabled(rule["id"], False)
        assert await db.apply_rules("yy") is None
        await db.set_rule_enabled(rule["id"], True)
        assert await db.apply_rules("yy") is not None

    async def test_sender_and_source_match(self, db):
        await self._rule(db, {"sender_id": 711, "source_chat_id": -100}, action="block")
        assert await db.apply_rules("نص", source_chat_id=-100, sender_id=711) is not None
        assert await db.apply_rules("نص", source_chat_id=-100, sender_id=999) is None

    async def test_min_confidence(self, db):
        await self._rule(db, {"min_confidence": 0.9}, action="tag", name="strict")
        hit = await db.apply_rules("نص", confidence=0.95)
        assert hit is not None and hit["action"] == "tag"
        assert await db.apply_rules("نص", confidence=0.5) is None

    async def test_fail_safe_corrupt_conditions(self, db):
        """Corrupt stored conditions must never crash evaluation."""
        await db.add_rule("bad", {"keyword": "z"}, action="block")
        # Corrupt the stored JSON directly
        await db._execute("UPDATE rules SET conditions = ? ", ("{not-json",))
        await db._commit()
        db.invalidate_rules_cache()
        assert await db.apply_rules("zz") is None  # skipped safely

    async def test_hits_batched_flush(self, db):
        """Hits accumulate in memory and are pushed in batches."""
        rule = await self._rule(db, {"keyword": "hitme"}, action="block")
        for _ in range(5):
            await db.apply_rules("please HITME now")
        # not flushed yet — memory counter holds them
        row = await db.get_rule(rule["id"])
        assert row["hits"] == 0
        pushed = await db.flush_rule_hits()
        assert pushed > 0
        row = await db.get_rule(rule["id"])
        assert row["hits"] == 5

    async def test_hits_flush_fail_safe_restores(self, db, monkeypatch):
        """If the flush write fails, counters are restored (no loss)."""
        rule = await self._rule(db, {"keyword": "k"}, action="block")
        await db.apply_rules("kk")
        async def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(db, "_execute", boom)
        await db.flush_rule_hits()
        monkeypatch.undo()
        assert db._rule_hits_pending.get(rule["id"]) >= 1


# ---------------------------------------------------------------------------
# DB layer: allowlist (P3)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestAllowlist:
    async def test_empty_allowlist(self, db):
        assert await db.is_sender_allowed(1) is False
        assert await db.is_chat_allowed(1) is False

    async def test_sender_and_chat_allow(self, db):
        assert await db.add_allowed_entity("sender", 711) is True
        assert await db.add_allowed_entity("chat", -100) is True
        assert await db.is_sender_allowed(711) is True
        assert await db.is_chat_allowed(-100) is True
        assert await db.is_sender_allowed(999) is False
        assert await db.is_chat_allowed(999) is False

    async def test_upsert_note(self, db):
        await db.add_allowed_entity("sender", 42, note="a")
        await db.add_allowed_entity("sender", 42, note="b")
        rows = await db.list_allowed_entities()
        assert len(rows) == 1 and rows[0]["note"] == "b"

    async def test_remove(self, db):
        await db.add_allowed_entity("sender", 5)
        assert await db.remove_allowed_entity("sender", 5) is True
        assert await db.is_sender_allowed(5) is False
        assert await db.remove_allowed_entity("sender", 5) is False

    async def test_invalid_type_rejected(self, db):
        assert await db.add_allowed_entity("wormhole", 1) is False

    async def test_fail_safe_on_db_error(self, db, monkeypatch):
        await db.add_allowed_entity("sender", 3)
        async def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(db, "_allowed_sets", boom)
        assert await db.is_sender_allowed(3) is False  # fail-safe False


# ---------------------------------------------------------------------------
# API layer: auth, validation, audit
# ---------------------------------------------------------------------------
TOKEN = None


