"""IPC 层单元测试 — JSON-RPC 2.0 编解码"""

import pytest
from common.ipc.protocol import Request, Response, encode, decode, make_id
from common.ipc.errors import (
    ParseError,
    InvalidRequestError,
    MethodNotFoundError,
    InternalError,
    TimeoutError,
)


class TestEncodeDecode:
    def test_encode_request(self):
        req = Request(method="llm.chat", params={"messages": []}, id="abc")
        encoded = encode(req)
        assert '"jsonrpc":"2.0"' in encoded
        assert '"method":"llm.chat"' in encoded
        assert '"id":"abc"' in encoded

    def test_encode_notification(self):
        notif = Request(method="ping")
        encoded = encode(notif)
        assert '"jsonrpc":"2.0"' in encoded
        assert '"method":"ping"' in encoded
        assert '"id"' not in encoded
        assert notif.is_notification()

    def test_encode_response(self):
        resp = Response(id="1", result={"ok": True})
        encoded = encode(resp)
        assert '"result"' in encoded
        assert '"id":"1"' in encoded

    def test_encode_error_response(self):
        resp = Response(id="1", error={"code": -32601, "message": "Not found"})
        encoded = encode(resp)
        assert '"error"' in encoded
        assert resp.is_success() is False

    def test_decode_request(self):
        decoded = decode('{"jsonrpc":"2.0","method":"test","id":"1"}')
        assert isinstance(decoded, Request)
        assert decoded.method == "test"
        assert decoded.id == "1"

    def test_decode_response(self):
        decoded = decode('{"jsonrpc":"2.0","id":"1","result":42}')
        assert isinstance(decoded, Response)
        assert decoded.id == "1"
        assert decoded.result == 42

    def test_decode_error(self):
        decoded = decode(
            '{"jsonrpc":"2.0","id":"1","error":{"code":-32601,"message":"Not found"}}'
        )
        assert isinstance(decoded, Response)
        assert decoded.error["code"] == -32601

    def test_decode_notification(self):
        decoded = decode('{"jsonrpc":"2.0","method":"update"}')
        assert isinstance(decoded, Request)
        assert decoded.is_notification()

    def test_make_id(self):
        id1 = make_id()
        id2 = make_id()
        assert len(id1) == 16
        assert id1 != id2

    def test_response_raise_for_error(self):
        resp = Response(id="1", error={"code": -32601, "message": "no"})
        with pytest.raises(Exception):
            resp.raise_for_error()


class TestDecodeErrors:
    def test_invalid_json(self):
        with pytest.raises(InvalidRequestError):
            decode("not json")

    def test_missing_jsonrpc(self):
        with pytest.raises(InvalidRequestError):
            decode('{"method":"test","id":"1"}')

    def test_wrong_jsonrpc(self):
        with pytest.raises(InvalidRequestError):
            decode('{"jsonrpc":"1.0","method":"test","id":"1"}')

    def test_empty_method(self):
        with pytest.raises(InvalidRequestError):
            decode('{"jsonrpc":"2.0","method":"","id":"1"}')

    def test_response_no_id(self):
        with pytest.raises(InvalidRequestError):
            decode('{"jsonrpc":"2.0","result":42}')


class TestErrorClasses:
    def test_parse_error(self):
        e = ParseError("bad")
        assert e.code == -32700
        assert "bad" in str(e)

    def test_method_not_found(self):
        e = MethodNotFoundError("foo")
        assert e.code == -32601
        assert "foo" in str(e)

    def test_internal_error(self):
        e = InternalError("crash")
        assert e.code == -32603

    def test_timeout_error(self):
        e = TimeoutError("timed out")
        assert e.code == -32000
