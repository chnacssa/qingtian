"""
GBZ 协议网关 — 单元测试
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from huanyu.gbz_protocol import GBZEnvelope


# ── Encode ───────────────────────────────────────────────

class TestGBZEncode:
    def test_basic_message(self):
        msg = {
            "from_agent_id": "biz:buyer-01",
            "to_agent_id": "biz:seller-05",
            "message_type": "inquiry",
            "payload": {"product": "螺纹钢"},
            "priority": "normal",
            "signature": "sig123",
            "idempotency_key": "idem_abc",
            "negotiation_id": "neg_xyz",
        }
        env = GBZEnvelope.encode(msg)
        assert env["senderRole"] == "requester"
        assert env["taskId"] == ""
        assert env["artifact"] == "work_communication"
        assert env["final"] is False
        assert env["from"]["ain"] == "biz:buyer-01"
        assert env["to"]["ain"] == "biz:seller-05"
        assert env["payload"] == {"product": "螺纹钢"}
        assert env["messageType"] == "inquiry"
        assert env["priority"] == "normal"
        assert env["signature"] == "sig123"
        assert env["idempotencyKey"] == "idem_abc"
        assert env["replyTo"] is None
        assert env["extensions"]["namespace"] == "huanyu:v1"
        assert env["extensions"]["negotiationId"] == "neg_xyz"

    def test_defaults_for_missing_fields(self):
        env = GBZEnvelope.encode({"from_agent_id": "a", "to_agent_id": "b"})
        assert env["senderRole"] == "requester"
        assert env["taskId"] == ""
        assert env["artifact"] == "work_communication"
        assert env["final"] is False
        assert env["payload"] == {}
        assert env["extensions"]["namespace"] == "huanyu:v1"

    def test_sender_role_preserved(self):
        env = GBZEnvelope.encode({"from_agent_id": "a", "to_agent_id": "b", "sender_role": "service"})
        assert env["senderRole"] == "service"

    def test_task_id_stringified(self):
        env = GBZEnvelope.encode({"from_agent_id": "a", "to_agent_id": "b", "task_id": 42})
        assert env["taskId"] == "42"

    def test_gbz185_ids_passthrough(self):
        msg = {
            "from_agent_id": "a", "to_agent_id": "b",
            "from_gbz185_id": "1.2.156.3088.1.acssa.a",
            "to_gbz185_id": "1.2.156.3088.1.acssa.b",
        }
        env = GBZEnvelope.encode(msg)
        assert env["from"]["gbz185_id"] == "1.2.156.3088.1.acssa.a"
        assert env["to"]["gbz185_id"] == "1.2.156.3088.1.acssa.b"

    def test_gbz185_ids_empty_when_missing(self):
        env = GBZEnvelope.encode({"from_agent_id": "a", "to_agent_id": "b"})
        assert env["from"]["gbz185_id"] == ""
        assert env["to"]["gbz185_id"] == ""

    def test_reply_to_uuid_serialization(self):
        import uuid
        uid = uuid.uuid4()
        msg = {"from_agent_id": "a", "to_agent_id": "b", "reply_to": uid}
        env = GBZEnvelope.encode(msg)
        assert isinstance(env["replyTo"], str)
        assert env["replyTo"] == str(uid)

    def test_reply_to_none(self):
        env = GBZEnvelope.encode({"from_agent_id": "a", "to_agent_id": "b"})
        assert env["replyTo"] is None

    def test_reply_to_empty_string(self):
        env = GBZEnvelope.encode({"from_agent_id": "a", "to_agent_id": "b", "reply_to": ""})
        assert env["replyTo"] is None

    def test_chunk_fields(self):
        env = GBZEnvelope.encode({
            "from_agent_id": "a", "to_agent_id": "b",
            "chunk_index": 3, "last_chunk": True,
        })
        assert env["chunkIndex"] == 3
        assert env["lastChunk"] is True

    def test_timestamp_injected(self):
        env = GBZEnvelope.encode({"from_agent_id": "a", "to_agent_id": "b"})
        assert "timestamp" in env
        assert isinstance(env["timestamp"], str)

    def test_preserve_existing_timestamp(self):
        env = GBZEnvelope.encode({"from_agent_id": "a", "to_agent_id": "b", "timestamp": "2026-01-01T00:00:00Z"})
        assert env["timestamp"] == "2026-01-01T00:00:00Z"


# ── Decode ───────────────────────────────────────────────

class TestGBZDecode:
    def test_basic_envelope(self):
        envelope = {
            "senderRole": "requester",
            "taskId": "task-123",
            "artifact": "work_communication",
            "final": False,
            "from": {"ain": "biz:buyer-01", "gbz185_id": "1.2.156.3088.1.a"},
            "to": {"ain": "biz:seller-05", "gbz185_id": ""},
            "payload": {"product": "螺纹钢"},
            "messageType": "inquiry",
            "priority": "normal",
            "extensions": {"namespace": "huanyu:v1", "negotiationId": "neg_xyz"},
        }
        msg = GBZEnvelope.decode(envelope)
        assert msg["from_agent_id"] == "biz:buyer-01"
        assert msg["from_gbz185_id"] == "1.2.156.3088.1.a"
        assert msg["to_agent_id"] == "biz:seller-05"
        assert msg["to_gbz185_id"] == ""
        assert msg["sender_role"] == "requester"
        assert msg["task_id"] == "task-123"
        assert msg["artifact"] == "work_communication"
        assert msg["final_flag"] is False
        assert msg["message_type"] == "inquiry"
        assert msg["payload"] == {"product": "螺纹钢"}
        assert msg["negotiation_id"] == "neg_xyz"

    def test_missing_from_to_handled(self):
        envelope = {"senderRole": "requester"}
        msg = GBZEnvelope.decode(envelope)
        assert msg["from_agent_id"] == ""
        assert msg["to_agent_id"] == ""

    def test_priority_fallback_to_extensions(self):
        """priority 优先取顶层字段，为 None 时回退到 extensions"""
        envelope = {
            "senderRole": "requester",
            "from": {"ain": "a"}, "to": {"ain": "b"},
            "extensions": {"priority": "high"},
        }
        msg = GBZEnvelope.decode(envelope)
        assert msg["priority"] == "high"

    def test_priority_top_level_wins(self):
        envelope = {
            "senderRole": "requester",
            "from": {"ain": "a"}, "to": {"ain": "b"},
            "priority": "urgent",
            "extensions": {"priority": "normal"},
        }
        msg = GBZEnvelope.decode(envelope)
        assert msg["priority"] == "urgent"

    def test_defaults(self):
        envelope = {"senderRole": "service", "from": {"ain": "a"}, "to": {"ain": "b"}}
        msg = GBZEnvelope.decode(envelope)
        assert msg["sender_role"] == "service"
        assert msg["artifact"] == "work_communication"
        assert msg["final_flag"] is False
        assert msg["payload"] == {}

    def test_extensions_safe_when_missing(self):
        envelope = {"senderRole": "requester", "from": {"ain": "a"}, "to": {"ain": "b"}}
        msg = GBZEnvelope.decode(envelope)
        assert msg["negotiation_id"] is None
        assert msg["priority"] == "normal"


# ── Format Detection ─────────────────────────────────────

class TestIsGBZFormat:
    def test_valid_gbz(self):
        env = {"senderRole": "requester", "from": {"ain": "a"}, "to": {"ain": "b"}}
        assert GBZEnvelope.is_gbz_format(env) is True

    def test_invalid_missing_sender_role(self):
        assert GBZEnvelope.is_gbz_format({"from": {"ain": "a"}, "to": {"ain": "b"}}) is False

    def test_invalid_from_not_dict(self):
        assert GBZEnvelope.is_gbz_format({"senderRole": "r", "from": "a", "to": {"ain": "b"}}) is False

    def test_invalid_to_not_dict(self):
        assert GBZEnvelope.is_gbz_format({"senderRole": "r", "from": {"ain": "a"}, "to": "b"}) is False

    def test_invalid_from_missing_ain(self):
        assert GBZEnvelope.is_gbz_format({"senderRole": "r", "from": {}, "to": {"ain": "b"}}) is False

    def test_not_dict(self):
        assert GBZEnvelope.is_gbz_format("not a dict") is False
        assert GBZEnvelope.is_gbz_format(None) is False
        assert GBZEnvelope.is_gbz_format([]) is False

    def test_legacy_format_rejected(self):
        legacy = {"from": "biz:buyer-01", "to": "biz:seller-05", "payload": {}}
        assert GBZEnvelope.is_gbz_format(legacy) is False

    def test_empty_dict(self):
        assert GBZEnvelope.is_gbz_format({}) is False


# ── Round-Trip ───────────────────────────────────────────

class TestRoundTrip:
    def test_encode_decode_symmetry(self):
        original = {
            "from_agent_id": "biz:buyer-01",
            "to_agent_id": "biz:seller-05",
            "message_type": "inquiry",
            "payload": {"product": "螺纹钢", "quantity": 200},
            "priority": "high",
            "task_id": "task-42",
            "sender_role": "requester",
            "negotiation_id": "neg-xyz",
        }
        encoded = GBZEnvelope.encode(original)
        decoded = GBZEnvelope.decode(encoded)
        assert decoded["from_agent_id"] == original["from_agent_id"]
        assert decoded["to_agent_id"] == original["to_agent_id"]
        assert decoded["message_type"] == original["message_type"]
        assert decoded["payload"] == original["payload"]
        assert decoded["priority"] == original["priority"]
        assert decoded["task_id"] == original["task_id"]
        assert decoded["sender_role"] == original["sender_role"]
        assert decoded["negotiation_id"] == original["negotiation_id"]

    def test_roundtrip_preserves_from_to(self):
        original = {"from_agent_id": "a1", "to_agent_id": "b2"}
        encoded = GBZEnvelope.encode(original)
        decoded = GBZEnvelope.decode(encoded)
        assert decoded["from_agent_id"] == "a1"
        assert decoded["to_agent_id"] == "b2"
