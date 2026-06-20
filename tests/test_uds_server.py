from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
import pytest
import pytest_asyncio

from engine.uds_server import JsonRpcCodec, UdsServer
from engine.config import SettingsManager
from engine.registry import ModelRegistryService


async def setup_test_dependencies(tmp_path: Path):
    default_file = tmp_path / "default.json"
    user_file = tmp_path / "user.json"
    proj_file = tmp_path / "proj.json"
    bundled_file = tmp_path / "bundled_models.json"
    cache_file = tmp_path / "cache_models.json"

    defaults_dict = {
        "socket_path": str(tmp_path / "test_code_savant.sock"),
        "model": "google/gemini-3.5-flash",
        "small_model": None,
        "context_filenames": ["GEMINI.md"],
        "global_context_dir": str(tmp_path / "global_gemini"),
        "session_storage_dir": ".code_savant/sessions",
        "system_agents_dir": "prompts/agents",
        "agent_extensions": [".agent.yaml", ".agent.yml", ".md"],
        "requires_approval": False,
        "providers": {"gemini": {"api_key_env_var": "GEMINI_API_KEY"}},
    }
    default_file.write_text(json.dumps(defaults_dict))

    bundled_data = {
        "google/gemini-3.5-flash": {
            "name": "google/gemini-3.5-flash",
            "provider": "gemini",
            "limits": {"context_tokens": 1000000, "max_output_tokens": 8192},
        }
    }
    bundled_file.write_text(json.dumps(bundled_data))

    settings_manager = SettingsManager(
        default_path=default_file, user_path=user_file, project_path=proj_file
    )
    await settings_manager.load_settings()

    model_registry = ModelRegistryService(
        bundled_path=bundled_file, cache_path=cache_file
    )
    await model_registry.initialize()

    return settings_manager, model_registry


@pytest_asyncio.fixture
async def server_env():
    """A pytest-asyncio fixture that sets up and tears down a cleanly decoupled running UdsServer."""
    test_dir = tempfile.mkdtemp()
    socket_path = os.path.join(test_dir, "test_code_savant_fixture.sock")
    workspace_path = os.path.join(test_dir, "workspace")
    os.makedirs(workspace_path, exist_ok=True)

    settings_manager, model_registry = await setup_test_dependencies(Path(test_dir))
    server = UdsServer(
        socket_path=socket_path,
        settings_manager=settings_manager,
        model_registry=model_registry,
    )
    await server.start()

    yield {
        "test_dir": test_dir,
        "socket_path": socket_path,
        "workspace_path": workspace_path,
        "server": server,
        "settings_manager": settings_manager,
        "model_registry": model_registry,
    }

    # Absolute guarantees of server stopping and directory teardown
    await server.stop()
    if os.path.exists(test_dir):
        try:
            shutil.rmtree(test_dir)
        except Exception:
            pass


@pytest_asyncio.fixture
async def client_conn(server_env):
    """A pytest-asyncio fixture that connects a client stream and guarantees closed socket teardown."""
    reader, writer = await asyncio.open_unix_connection(server_env["socket_path"])
    yield reader, writer

    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


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
    err = JsonRpcCodec.encode_error(
        -32600, "Invalid Request", msg_id="abc", data="extra info"
    )
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
async def test_uds_server_lifecycle(server_env, client_conn):
    """Start the UDS Server, connect a client, run a session/start and session/send_prompt sequence."""
    workspace_path = server_env["workspace_path"]
    reader, writer = client_conn

    async def read_response(expected_id: int) -> dict:
        while True:
            line = await reader.readline()
            if not line:
                raise EOFError("Connection closed before response received")
            msg = json.loads(line.decode("utf-8").strip())
            if msg.get("id") == expected_id:
                return msg

    # 1. Send session/start
    start_req = {
        "jsonrpc": "2.0",
        "method": "session/start",
        "params": {
            "workspace_path": workspace_path,
            "agent_profile": "coder",
            "mock_mode": True,
        },
        "id": 1,
    }
    writer.write((json.dumps(start_req) + "\n").encode("utf-8"))
    await writer.drain()

    # Read response
    response = await read_response(1)
    assert "result" in response
    session_id = response["result"]["session_id"]
    assert response["result"]["status"] == "active"

    # 2. Send session/send_prompt with "think" prompt to trigger model_start and thought notifications
    prompt_req = {
        "jsonrpc": "2.0",
        "method": "session/send_prompt",
        "params": {"session_id": session_id, "text": "please think and optimize"},
        "id": 2,
    }
    writer.write((json.dumps(prompt_req) + "\n").encode("utf-8"))
    await writer.drain()

    # Read response for queue confirmation
    response = await read_response(2)
    assert response["result"]["status"] == "queued"

    # Accumulate streamed telemetry notifications
    notifications = []
    # Expecting telemetry/status (thinking), telemetry/collapsed_block (thought)
    while len(notifications) < 10:
        line = await reader.readline()
        if not line:
            break
        msg = json.loads(line.decode("utf-8").strip())
        if "id" not in msg:
            notifications.append(msg)
            methods = [n["method"] for n in notifications]
            if "telemetry/status" in methods and "telemetry/collapsed_block" in methods:
                break

    assert len(notifications) >= 1
    methods = [n["method"] for n in notifications]
    assert "telemetry/status" in methods
    assert "telemetry/collapsed_block" in methods

    # 3. Send session/close
    close_req = {
        "jsonrpc": "2.0",
        "method": "session/close",
        "params": {"session_id": session_id},
        "id": 3,
    }
    writer.write((json.dumps(close_req) + "\n").encode("utf-8"))
    await writer.drain()

    # Read response
    response = await read_response(3)
    assert response["result"]["status"] == "closed"


@pytest.mark.asyncio
async def test_uds_server_idempotency_and_errors(server_env, client_conn):
    """Verify that UdsServer is robust against double start, invalid requests, and invalid params."""
    server = server_env["server"]
    reader, writer = client_conn

    # 1. Test idempotency of start
    await server.start()

    # 2. Test invalid JSON-RPC version
    req_invalid_ver = {
        "jsonrpc": "1.0",
        "method": "session/start",
        "params": {"workspace_path": "foo"},
        "id": 100,
    }
    writer.write((json.dumps(req_invalid_ver) + "\n").encode("utf-8"))
    await writer.drain()

    line = await reader.readline()
    resp = json.loads(line.decode("utf-8").strip())
    assert resp["id"] == 100
    assert resp["error"]["code"] == -32600
    assert "Invalid Request" in resp["error"]["message"]

    # 3. Test missing method
    req_no_method = {"jsonrpc": "2.0", "id": 101}
    writer.write((json.dumps(req_no_method) + "\n").encode("utf-8"))
    await writer.drain()

    line = await reader.readline()
    resp = json.loads(line.decode("utf-8").strip())
    assert resp["id"] == 101
    assert resp["error"]["code"] == -32600
    assert "Method not found" in resp["error"]["message"]

    # 4. Test nonexistent method
    req_unknown_method = {"jsonrpc": "2.0", "method": "session/nonexistent", "id": 102}
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
        "id": 103,
    }
    writer.write((json.dumps(req_missing_param) + "\n").encode("utf-8"))
    await writer.drain()

    line = await reader.readline()
    resp = json.loads(line.decode("utf-8").strip())
    assert resp["id"] == 103
    assert resp["error"]["code"] == -32602
    assert "Missing required workspace_path parameter" in resp["error"]["message"]

    # 6. Test idempotency of stop
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    await server.stop()
    await server.stop()


@pytest.mark.asyncio
async def test_uds_server_provider_resolution_failures(
    server_env, client_conn, monkeypatch
):
    """Verify that UdsServer fails cleanly and returns JSON-RPC error structures when API keys are unresolved."""
    workspace_path = server_env["workspace_path"]
    reader, writer = client_conn

    # Use monkeypatch to safely delete GEMINI_API_KEY from the environment for this test block only
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    async def read_response(expected_id: int) -> dict:
        while True:
            line = await reader.readline()
            if not line:
                raise EOFError("Connection closed before response received")
            msg = json.loads(line.decode("utf-8").strip())
            if msg.get("id") == expected_id:
                return msg

    # 1. Start an active, non-mock session
    start_req = {
        "jsonrpc": "2.0",
        "method": "session/start",
        "params": {
            "workspace_path": workspace_path,
            "agent_profile": "coder",
            "mock_mode": False,
        },
        "id": 1,
    }
    writer.write((json.dumps(start_req) + "\n").encode("utf-8"))
    await writer.drain()

    response = await read_response(1)
    session_id = response["result"]["session_id"]

    # 2. Send prompt (which triggers client resolution)
    prompt_req = {
        "jsonrpc": "2.0",
        "method": "session/send_prompt",
        "params": {"session_id": session_id, "text": "optimize loop"},
        "id": 2,
    }
    writer.write((json.dumps(prompt_req) + "\n").encode("utf-8"))
    await writer.drain()

    response = await read_response(2)
    # It should return a direct JSON-RPC error response because credentials fail to resolve
    assert "error" in response
    assert response["error"]["code"] == -32602
    assert "Credentials could not be resolved" in response["error"]["message"]


@pytest.mark.asyncio
async def test_uds_server_session_list_and_load(server_env, client_conn):
    """Start the UDS Server, start a session to persist a file, then list and load it back."""
    workspace_path = server_env["workspace_path"]
    reader, writer = client_conn

    async def read_response(expected_id: int) -> dict:
        while True:
            line = await reader.readline()
            if not line:
                raise EOFError("Connection closed before response received")
            msg = json.loads(line.decode("utf-8").strip())
            if msg.get("id") == expected_id:
                return msg

    # 1. Start a session
    start_req = {
        "jsonrpc": "2.0",
        "method": "session/start",
        "params": {
            "workspace_path": workspace_path,
            "agent_profile": "coder",
            "mock_mode": True,
        },
        "id": 1,
    }
    writer.write((json.dumps(start_req) + "\n").encode("utf-8"))
    await writer.drain()

    response = await read_response(1)
    session_id = response["result"]["session_id"]

    # 2. Query session/list
    list_req = {
        "jsonrpc": "2.0",
        "method": "session/list",
        "params": {
            "workspace_path": workspace_path,
        },
        "id": 2,
    }
    writer.write((json.dumps(list_req) + "\n").encode("utf-8"))
    await writer.drain()

    list_response = await read_response(2)
    assert "result" in list_response
    sessions_list = list_response["result"]["sessions"]
    assert len(sessions_list) > 0
    assert any(s["session_id"] == session_id for s in sessions_list)

    # 3. Query session/load on the saved session
    load_req = {
        "jsonrpc": "2.0",
        "method": "session/load",
        "params": {
            "workspace_path": workspace_path,
            "session_id": session_id,
        },
        "id": 3,
    }
    writer.write((json.dumps(load_req) + "\n").encode("utf-8"))
    await writer.drain()

    load_response = await read_response(3)
    assert "result" in load_response
    assert load_response["result"]["session_id"] == session_id
    assert load_response["result"]["status"] == "active"
    assert "chat_history" in load_response["result"]
