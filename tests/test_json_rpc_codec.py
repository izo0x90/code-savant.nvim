from __future__ import annotations

import json
import pytest
from engine.uds_server import JsonRpcCodec


def test_encode_response_success():
    """Verify success response encoding with valid types."""
    # Integer msg_id
    res_bytes = JsonRpcCodec.encode_response({"ok": True}, msg_id=123)
    assert res_bytes.endswith(JsonRpcCodec.DELIMITER.encode("utf-8"))
    
    payload = json.loads(res_bytes.decode("utf-8").strip())
    assert payload["jsonrpc"] == JsonRpcCodec.JSONRPC_VERSION
    assert payload["result"] == {"ok": True}
    assert payload["id"] == 123

    # String msg_id
    res_bytes_str = JsonRpcCodec.encode_response({"active": False}, msg_id="req-99")
    payload_str = json.loads(res_bytes_str.decode("utf-8").strip())
    assert payload_str["id"] == "req-99"


def test_encode_response_invalid_types():
    """Verify that encode_response raises TypeError on invalid types."""
    # Invalid result type (not dict)
    with pytest.raises(TypeError) as exc_info:
        JsonRpcCodec.encode_response("not a dict", msg_id=123)  # type: ignore
    assert "result must be a dictionary" in str(exc_info.value)

    # Invalid msg_id type (not int or str)
    with pytest.raises(TypeError) as exc_info:
        JsonRpcCodec.encode_response({"ok": True}, msg_id=45.6)  # type: ignore
    assert "msg_id must be int or str" in str(exc_info.value)


def test_encode_error_success():
    """Verify error encoding with valid types and varying optional arguments."""
    # Minimum params
    err_bytes = JsonRpcCodec.encode_error(code=-32700, message="Parse error")
    assert err_bytes.endswith(JsonRpcCodec.DELIMITER.encode("utf-8"))
    
    payload = json.loads(err_bytes.decode("utf-8").strip())
    assert payload["jsonrpc"] == JsonRpcCodec.JSONRPC_VERSION
    assert payload["error"]["code"] == -32700
    assert payload["error"]["message"] == "Parse error"
    assert "data" not in payload["error"]
    assert payload["id"] is None

    # Full params with data and string msg_id
    err_bytes_full = JsonRpcCodec.encode_error(
        code=-32602,
        message="Invalid params",
        msg_id="err-456",
        data={"reason": "Missing workspace_path"}
    )
    payload_full = json.loads(err_bytes_full.decode("utf-8").strip())
    assert payload_full["error"]["code"] == -32602
    assert payload_full["error"]["message"] == "Invalid params"
    assert payload_full["error"]["data"] == {"reason": "Missing workspace_path"}
    assert payload_full["id"] == "err-456"


def test_encode_error_invalid_types():
    """Verify that encode_error raises TypeError on invalid types."""
    with pytest.raises(TypeError) as exc_info:
        JsonRpcCodec.encode_error(code="not-int", message="Error")  # type: ignore
    assert "code must be an integer" in str(exc_info.value)

    with pytest.raises(TypeError) as exc_info:
        JsonRpcCodec.encode_error(code=-32000, message=12345)  # type: ignore
    assert "message must be a string" in str(exc_info.value)

    with pytest.raises(TypeError) as exc_info:
        JsonRpcCodec.encode_error(code=-32000, message="Error", msg_id=[1, 2, 3])  # type: ignore
    assert "msg_id must be int, str, or None" in str(exc_info.value)


def test_codec_constants():
    """Verify that the class-level constants are configured correctly."""
    assert JsonRpcCodec.JSONRPC_VERSION == "2.0"
    assert JsonRpcCodec.DELIMITER == "\n"
