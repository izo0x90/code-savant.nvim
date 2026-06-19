import os
import uuid
import shutil
import asyncio
import pytest
from pathlib import Path

from engine.sessions import SessionManager, AgentSession, SessionMetadataPayload
from engine.client import MockGenAIClient
from engine.types import ThoughtChunk, CompletionChunk, ChatMessage, TextPart, ExecutorAgentConfig, ExecutionContext
from engine.executor import LocalAgentExecutor
from engine.tools import CompleteTaskTool
from engine.prompts.loader import PromptTemplateLoader
from engine.context import DefaultAgentContextStrategy
from engine.bus import MessageBus
from engine.constants import (
    SESSION_FILE_SUFFIX,
    SESSION_META_SUFFIX,
    CHECKPOINT_SEPARATOR,
    SCRATCH_DIR_NAME,
    TOOL_LOG_FILE_PREFIX,
    TOOL_LOG_FILE_SUFFIX
)


async def run_session_tests(tmp_dir: str):
    print("\n--- [1/4] Running Session Persistence Tests ---")
    manager = SessionManager(
        storage_dir=Path(tmp_dir),
        session_suffix=SESSION_FILE_SUFFIX,
        meta_suffix=SESSION_META_SUFFIX,
        checkpoint_separator=CHECKPOINT_SEPARATOR,
        scratch_dir_name=SCRATCH_DIR_NAME,
        tool_log_prefix=TOOL_LOG_FILE_PREFIX,
        tool_log_suffix=TOOL_LOG_FILE_SUFFIX
    )
    await manager.ensure_storage_dir()

    session_id_1 = uuid.uuid7()
    session_id_2 = uuid.uuid7()
    
    session_1 = AgentSession(
        session_id=session_id_1,
        chat_history=[
            ChatMessage(role="user", parts=[TextPart(text="Hello world")]),
            ChatMessage(role="model", parts=[TextPart(text="I can assist with software development.")])
        ],
        metadata=SessionMetadataPayload(name="Test Session 1", query="Hello world")
    )

    session_2 = AgentSession(
        session_id=session_id_2,
        chat_history=[
            ChatMessage(role="user", parts=[TextPart(text="Querying local code")])
        ],
        metadata=SessionMetadataPayload(name="Test Session 2", query="Querying local code")
    )

    # 1. Save Sessions
    await manager.save_session(session_1)
    await manager.save_session(session_2)
    print("✅ Successfully saved sessions.")

    # 2. List Sessions
    sessions = await manager.list_sessions()
    assert len(sessions) == 2, f"Expected 2 sessions, got {len(sessions)}"
    assert sessions[0].session_id == session_id_2, "Expected newest session first"
    print("✅ Successfully listed and sorted sessions.")

    # 3. Load Session
    loaded = await manager.load_session(session_id_1)
    assert loaded.session_id == session_id_1
    assert len(loaded.chat_history) == 2
    assert loaded.metadata.name == "Test Session 1"
    print("✅ Successfully loaded session.")

    # 4. Save Checkpoint
    checkpoint_name = "savepoint-beta"
    await manager.save_checkpoint(session_id_1, checkpoint_name)
    loaded_cp = await manager.load_session(session_id_1, checkpoint_name=checkpoint_name)
    assert loaded_cp.session_id == session_id_1
    assert loaded_cp.metadata.name == "Test Session 1"
    print("✅ Successfully created and loaded session checkpoint.")

    # 5. Delete Session
    await manager.delete_session(session_id_1)
    sessions_post_delete = await manager.list_sessions()
    assert len(sessions_post_delete) == 1
    assert sessions_post_delete[0].session_id == session_id_2
    print("✅ Successfully deleted session.")

    # 6. Test playback, loud parse failures, and sidecar metadata merging
    print("\n--- [1b] Testing Sequential Playback and Loud Failures ---")
    
    # 6a. Playback with State Modifiers
    playback_sess_id = uuid.uuid7()
    playback_file = Path(tmp_dir) / f"{str(playback_sess_id)}{SESSION_FILE_SUFFIX}"
    playback_meta_file = Path(tmp_dir) / f"{str(playback_sess_id)}{SESSION_META_SUFFIX}"
    
    import json
    lines = [
        json.dumps({"role": "user", "parts": [{"text": "First message"}]}),
        json.dumps({"role": "model", "parts": [{"text": "Second message"}]}),
        json.dumps({"type": "SetDelta", "index": 1, "message": {"role": "model", "parts": [{"text": "Updated second message"}]}}),
        json.dumps({"type": "RewindDelta", "count": 1})
    ]
    with open(playback_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        
    meta_data = {
        "session_id": str(playback_sess_id),
        "metadata": {
            "name": "Sidecar Session Name That Is Way Too Long To Be Auto Saved In Under Sixty Four Characters Constraint",
            "query": "Sidecar Query",
            "created_at": "2026-06-14T12:00:00Z",
            "last_updated": "2026-06-14T13:00:00Z",
            "turn_count": 5
        },
        "turn_count": 5
    }
    with open(playback_meta_file, "w", encoding="utf-8") as f:
        f.write(json.dumps(meta_data))
        
    loaded_playback = await manager.load_session(playback_sess_id)
    assert len(loaded_playback.chat_history) == 1
    assert loaded_playback.chat_history[0].parts[0].text == "First message"
    
    # Verify sidecar merging and name trimming
    assert len(loaded_playback.metadata.name) == 64
    assert loaded_playback.metadata.name.startswith("Sidecar Session Name")
    assert loaded_playback.metadata.query == "Sidecar Query"
    print("✅ Successfully verified sequential playback modifiers and sidecar merging / trimming.")
    
    # 6b. Loud Parse Failure
    corrupt_sess_id = uuid.uuid7()
    corrupt_file = Path(tmp_dir) / f"{str(corrupt_sess_id)}{SESSION_FILE_SUFFIX}"
    corrupt_lines = [
        json.dumps({"role": "user", "parts": [{"text": "Valid message"}]}),
        "{invalid-json-here}"
    ]
    with open(corrupt_file, "w", encoding="utf-8") as f:
        f.write("\n".join(corrupt_lines))
        
    with pytest.raises(ValueError) as exc_info:
        await manager.load_session(corrupt_sess_id)
        
    err_msg = str(exc_info.value)
    assert "Line 2" in err_msg
    assert str(corrupt_sess_id) in err_msg
    print("✅ Successfully verified loud parse failures with line numbers and file path.")


async def run_retention_tests(tmp_dir: str):
    print("\n--- [2/4] Running Retention Cleanup Tests ---")
    manager = SessionManager(
        storage_dir=Path(tmp_dir),
        session_suffix=SESSION_FILE_SUFFIX,
        meta_suffix=SESSION_META_SUFFIX,
        checkpoint_separator=CHECKPOINT_SEPARATOR,
        scratch_dir_name=SCRATCH_DIR_NAME,
        tool_log_prefix=TOOL_LOG_FILE_PREFIX,
        tool_log_suffix=TOOL_LOG_FILE_SUFFIX
    )
    await manager.ensure_storage_dir()

    # Clear out any residual session files in the directory
    for f in await asyncio.to_thread(os.listdir, tmp_dir):
        await asyncio.to_thread(os.remove, os.path.join(tmp_dir, f))

    ret_ids = [uuid.uuid7() for _ in range(5)]
    # Create 5 mock sessions
    for i in range(5):
        sess_id = ret_ids[i]
        session = AgentSession(
            session_id=sess_id,
            chat_history=[],
            metadata=SessionMetadataPayload(name=f"Session {i}")
        )
        await manager.save_session(session)
        # Tiny yield to ensure separate timestamps
        await asyncio.sleep(0.01)

    # Verify count
    sessions = await manager.list_sessions()
    assert len(sessions) == 5, f"Expected 5 sessions, got {len(sessions)}"

    # Enforce count limit of 3
    await manager.enforce_retention_policy(max_age_days=30, max_count=3)
    sessions_after = await manager.list_sessions()
    assert len(sessions_after) == 3, f"Expected retention limit to prune to 3, got {len(sessions_after)}"
    # The remaining sessions should be the newest ones
    session_ids = [s.session_id for s in sessions_after]
    assert ret_ids[4] in session_ids
    assert ret_ids[0] not in session_ids
    print("✅ Successfully enforced count-based retention pruning.")


async def run_client_mock_tests():
    print("\n--- [3/4] Running GenAIClient Mock Mode Stream Tests ---")
    client = MockGenAIClient()

    # 1. Test "coder_subagent" Mock Stream
    chunks_coder = []
    async for chunk in client.generate_response_stream(
        system_prompt="System Prompt",
        chat_history=[],
        tools_declarations=[],
        agent_name="coder_subagent",
        turn_counter=0
    ):
        chunks_coder.append(chunk)

    assert len(chunks_coder) == 3
    assert isinstance(chunks_coder[0], ThoughtChunk)
    assert isinstance(chunks_coder[1], ThoughtChunk)
    assert isinstance(chunks_coder[2], CompletionChunk)
    assert chunks_coder[2].function_calls[0].name == "read_file"
    print("✅ Successfully streamed mock 'coder_subagent' turns.")

    # 2. Test "refactor_helper" Mock Stream
    chunks_helper = []
    async for chunk in client.generate_response_stream(
        system_prompt="System Prompt",
        chat_history=[],
        tools_declarations=[],
        agent_name="refactor_helper",
        turn_counter=0
    ):
        chunks_helper.append(chunk)

    assert len(chunks_helper) == 2
    assert isinstance(chunks_helper[0], ThoughtChunk)
    assert isinstance(chunks_helper[1], CompletionChunk)
    assert chunks_helper[1].function_calls[0].name == "grep_search"
    print("✅ Successfully streamed mock 'refactor_helper' turns.")


async def run_integration_tests(tmp_dir: str):
    print("\n--- [4/4] Running Integrated Loop Persistence Tests ---")
    manager = SessionManager(
        storage_dir=Path(tmp_dir),
        session_suffix=SESSION_FILE_SUFFIX,
        meta_suffix=SESSION_META_SUFFIX,
        checkpoint_separator=CHECKPOINT_SEPARATOR,
        scratch_dir_name=SCRATCH_DIR_NAME,
        tool_log_prefix=TOOL_LOG_FILE_PREFIX,
        tool_log_suffix=TOOL_LOG_FILE_SUFFIX
    )
    client = MockGenAIClient()

    agent_def = {
        "name": "refactor_helper",
        "maxTurns": 1,
        "maxTimeSeconds": 10,
        "session_id": "integrated-test-sess"
    }

    sess_id = uuid.uuid7()
    session = AgentSession(
        session_id=sess_id,
        chat_history=[],
        metadata=SessionMetadataPayload()
    )

    # Setup isolated executor
    config = ExecutorAgentConfig.model_validate(agent_def)
    bus = MessageBus()
    context = ExecutionContext(
        workspace_path=Path(os.getcwd()),
        message_bus=bus,
        remaining_depth=3,
        session=session,
        session_manager=manager,
        client=client
    )

    # For integrated tests, create a PromptTemplateLoader pointing to the system templates directory
    templates_dir = Path("src/engine/prompts/templates")
    loader = PromptTemplateLoader(templates_dir=templates_dir)
    strategy = DefaultAgentContextStrategy(loader=loader)

    executor = LocalAgentExecutor(
        definition=config,
        context_strategy=strategy
    )
    
    # Register CompleteTaskTool to satisfy requirements
    executor.registry.register_tool(CompleteTaskTool())

    # Run execution turn
    inputs = {"target_dir": os.getcwd()}
    await executor.run(context, inputs)

    # Verify that a session file was automatically created and written to
    sessions = await manager.list_sessions()
    assert len(sessions) >= 1
    assert any(s.session_id == sess_id for s in sessions)

    loaded = await manager.load_session(sess_id)
    assert len(loaded.chat_history) > 0, "Expected session history to be saved dynamically"
    print("✅ Successfully verified auto-save triggers inside the live orchestration loop.")


async def main():
    # Setup fresh temporary test directory inside workspace
    workspace = os.getcwd()
    tmp_test_dir = os.path.join(workspace, ".replica_test_sessions_tmp")
    
    if os.path.exists(tmp_test_dir):
        shutil.rmtree(tmp_test_dir)
    os.makedirs(tmp_test_dir, exist_ok=True)

    try:
        await run_session_tests(tmp_test_dir)
        await run_retention_tests(tmp_test_dir)
        await run_client_mock_tests()
        await run_integration_tests(tmp_test_dir)
        print("\n🎉 ALL TESTS PASSED WITH 100% CORRECTNESS AND ASYNC INTEGRITY!")
    finally:
        # Clean up temporary test directories
        if os.path.exists(tmp_test_dir):
            shutil.rmtree(tmp_test_dir)


if __name__ == "__main__":
    asyncio.run(main())


@pytest.mark.asyncio
async def test_session_and_client_all():
    await main()


@pytest.mark.asyncio
async def test_token_and_timer_performance():
    from engine.context import estimate_token_count_sync
    from engine.types import TextPart
    from engine.timer import DeadlineTimer
    import asyncio
    
    # 1. Verify token calculation
    parts = [TextPart(text="Hello, how are you?")]
    tokens = estimate_token_count_sync(parts)
    assert tokens > 0
    
    # 2. Verify Timer behavior
    timer = DeadlineTimer(limit_seconds=0.1)
    timer.start()
    assert not timer.is_triggered
    await asyncio.sleep(0.01)
    timer.pause()
    assert timer.paused
    
    elapsed = timer.elapsed_seconds
    assert elapsed > 0.0
    await asyncio.sleep(0.02)
    # Since paused, elapsed seconds should not increase
    assert timer.elapsed_seconds == elapsed
    
    timer.resume()
    assert not timer.paused
    await asyncio.sleep(0.12)
    assert timer.is_triggered
    timer.stop()
