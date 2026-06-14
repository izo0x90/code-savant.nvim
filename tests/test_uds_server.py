from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
import pytest

from engine.uds_server import JsonRpcCodec, UdsServer


@pytest.mark.asyncio
async def test_json_rpc_codec():
    """Verify that JsonRpcCodec formats and encodes JSON-RPC messages cleanly with newline delimiters."""
    # Test response
    resp = JsonRpcCodec.encode_response({"status": "active"}, msg_id=42)
    decoded_resp = json.loads(resp.decode("utf-8").strip())
    assert decoded_resp["jsonrpc"] == "2.0"
    assert decoded_resp["result"] == {"status": "active"}
    assert decoded_resp["id"] == 42
    assert resp.endswith(b"\n")

    # Test error
    err = JsonRpcCodec.encode_error(-32600, "Invalid Request", msg_id="abc", data="extra info")
    decoded_err = json.loads(err.decode("utf-8").strip())
    assert decoded_err["jsonrpc"] == "2.0"
    assert decoded_err["error"]["code"] == -32600
    assert decoded_err["error"]["message"] == "Invalid Request"
    assert decoded_err["error"]["data"] == "extra info"
    assert decoded_err["id"] == "abc"
    assert err.endswith(b"\n")

    # Test notification
    notif = JsonRpcCodec.encode_notification("telemetry/status", {"status": "thinking"})
    decoded_notif = json.loads(notif.decode("utf-8").strip())
    assert decoded_notif["jsonrpc"] == "2.0"
    assert decoded_notif["method"] == "telemetry/status"
    assert decoded_notif["params"] == {"status": "thinking"}
    assert "id" not in decoded_notif
    assert notif.endswith(b"\n")


@pytest.mark.asyncio
async def test_uds_server_lifecycle():
    """Start the UDS Server, connect a client, run a session/start and session/send_prompt sequence."""
    test_dir = tempfile.mkdtemp()
    socket_path = os.path.join(test_dir, "test_code_savant.sock")
    workspace_path = os.path.join(test_dir, "workspace")
    os.makedirs(workspace_path, exist_ok=True)

    server = UdsServer(socket_path=socket_path)
    await server.start()

    try:
        # Connect client socket
        reader, writer = await asyncio.open_unix_connection(socket_path)

        # 1. Send session/start
        start_req = {
            "jsonrpc": "2.0",
            "method": "session/start",
            "params": {
                "workspace_path": workspace_path,
                "agent_profile": "coder"
            },
            "id": 1
        }
        writer.write((json.dumps(start_req) + "\n").encode("utf-8"))
        await writer.drain()

        # Read response
        line = await reader.readline()
        response = json.loads(line.decode("utf-8").strip())
        assert response["id"] == 1
        assert "result" in response
        session_id = response["result"]["session_id"]
        assert response["result"]["status"] == "active"

        # 2. Send session/send_prompt with "think" prompt to trigger model_start and thought notifications
        prompt_req = {
            "jsonrpc": "2.0",
            "method": "session/send_prompt",
            "params": {
                "session_id": session_id,
                "text": "please think and optimize"
            },
            "id": 2
        }
        writer.write((json.dumps(prompt_req) + "\n").encode("utf-8"))
        await writer.drain()

        # Read response for queue confirmation
        line = await reader.readline()
        response = json.loads(line.decode("utf-8").strip())
        assert response["id"] == 2
        assert response["result"]["status"] == "queued"

        # Accumulate streamed telemetry notifications
        notifications = []
        # Expecting telemetry/status (thinking), telemetry/collapsed_block (thought), telemetry/status (idle)
        for _ in range(3):
            line = await reader.readline()
            if not line:
                break
            notif = json.loads(line.decode("utf-8").strip())
            notifications.append(notif)

        assert len(notifications) == 3
        methods = [n["method"] for n in notifications]
        assert "telemetry/status" in methods
        assert "telemetry/collapsed_block" in methods

        # 3. Send session/close
        close_req = {
            "jsonrpc": "2.0",
            "method": "session/close",
            "params": {
                "session_id": session_id
            },
            "id": 3
        }
        writer.write((json.dumps(close_req) + "\n").encode("utf-8"))
        await writer.drain()

        line = await reader.readline()
        response = json.loads(line.decode("utf-8").strip())
        assert response["id"] == 3
        assert response["result"]["status"] == "closed"

        writer.close()
        await writer.wait_closed()

    finally:
        await server.stop()
        if os.path.exists(test_dir):
            shutil.rmtree(test_dir)


@pytest.mark.asyncio
async def test_uds_server_idempotency_and_errors():
    """Verify that UdsServer is robust against double start, invalid requests, and invalid params."""
    test_dir = tempfile.mkdtemp()
    socket_path = os.path.join(test_dir, "test_code_savant_err.sock")

    server = UdsServer(socket_path=socket_path)
    
    # 1. Test idempotency of start
    await server.start()
    # Double start should be a safe no-op
    await server.start()

    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)

        # 2. Test invalid JSON-RPC version
        req_invalid_ver = {
            "jsonrpc": "1.0",
            "method": "session/start",
            "params": {"workspace_path": "foo"},
            "id": 100
        }
        writer.write((json.dumps(req_invalid_ver) + "\n").encode("utf-8"))
        await writer.drain()
        
        line = await reader.readline()
        resp = json.loads(line.decode("utf-8").strip())
        assert resp["id"] == 100
        assert resp["error"]["code"] == -32600
        assert "Invalid Request" in resp["error"]["message"]

        # 3. Test missing method
        req_no_method = {
            "jsonrpc": "2.0",
            "id": 101
        }
        writer.write((json.dumps(req_no_method) + "\n").encode("utf-8"))
        await writer.drain()
        
        line = await reader.readline()
        resp = json.loads(line.decode("utf-8").strip())
        assert resp["id"] == 101
        assert resp["error"]["code"] == -32600
        assert "Method not found" in resp["error"]["message"]

        # 4. Test nonexistent method
        req_unknown_method = {
            "jsonrpc": "2.0",
            "method": "session/nonexistent",
            "id": 102
        }
        writer.write((json.dumps(req_unknown_method) + "\n").encode("utf-8"))
        await writer.drain()
        
        line = await reader.readline()
        resp = json.loads(line.decode("utf-8").strip())
        assert resp["id"] == 102
        assert resp["error"]["code"] == -32601
        assert "Method 'session/nonexistent' not found" in resp["error"]["message"]

        # 5. Test missing parameter
        req_missing_param = {
            "jsonrpc": "2.0",
            "method": "session/start",
            "params": {},
            "id": 103
        }
        writer.write((json.dumps(req_missing_param) + "\n").encode("utf-8"))
        await writer.drain()
        
        line = await reader.readline()
        resp = json.loads(line.decode("utf-8").strip())
        assert resp["id"] == 103
        assert resp["error"]["code"] == -32602
        assert "Missing required workspace_path parameter" in resp["error"]["message"]

        writer.close()
        await writer.wait_closed()

    finally:
        await server.stop()
        # 6. Test idempotency of stop
        await server.stop()
        if os.path.exists(test_dir):
            shutil.rmtree(test_dir)

