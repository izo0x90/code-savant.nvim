from __future__ import annotations

import pytest
from engine.bus import MessageBus
from engine.mock_executor import (
    MockAgentExecutor,
    MOCK_DIFF_PAYLOAD,
    MOCK_DIFF_PAYLOAD_ID,
    MOCK_ERROR_PAYLOAD,
    MOCK_ERROR_PAYLOAD_ID,
    MOCK_THOUGHT_PAYLOAD,
    MOCK_THOUGHT_PAYLOAD_ID,
    make_default_response_payload,
)
from engine.sessions import AgentSession
from engine.types import ActivityType, EventType, SessionMetadataPayload


@pytest.fixture
def message_bus() -> MessageBus:
    """Fixture for creating a clean MessageBus instance."""
    return MessageBus()


@pytest.fixture
def agent_session() -> AgentSession:
    """Fixture for creating a basic AgentSession instance."""
    return AgentSession(
        session_id="test_executor_session",
        chat_history=[],
        metadata=SessionMetadataPayload(
            name="test_session",
            query="test query",
            turn_count=0
        )
    )


@pytest.mark.asyncio
async def test_mock_executor_thinking_scenario(message_bus: MessageBus, agent_session: AgentSession) -> None:
    """Verify that a think trigger keyword emits model start, thought log, and model end."""
    events: list[dict] = []

    async def listener(event: dict) -> None:
        events.append(event)

    message_bus.subscribe(EventType.ACTIVITY.value, listener)
    message_bus.subscribe(EventType.TELEMETRY_LOG.value, listener)

    executor = MockAgentExecutor(message_bus, latency=0.0)
    await executor.run(agent_session, "Please think and optimize the db queries.")

    # 1. Verify we received exactly 3 events
    assert len(events) == 3

    # 2. Verify turn start
    assert events[0]["type"] == EventType.ACTIVITY.value
    assert events[0]["payload"]["type"] == ActivityType.MODEL_START.value
    assert events[0]["payload"]["prompt_length"] == len("Please think and optimize the db queries.")

    # 3. Verify thinking telemetry log
    assert events[1]["type"] == EventType.TELEMETRY_LOG.value
    assert events[1]["payload"]["id"] == MOCK_THOUGHT_PAYLOAD_ID
    assert events[1]["payload"]["type"] == "thought"
    assert events[1]["payload"]["full_content"] == MOCK_THOUGHT_PAYLOAD["full_content"]

    # 4. Verify turn end
    assert events[2]["type"] == EventType.ACTIVITY.value
    assert events[2]["payload"]["type"] == ActivityType.MODEL_END.value


@pytest.mark.asyncio
async def test_mock_executor_editing_scenario(message_bus: MessageBus, agent_session: AgentSession) -> None:
    """Verify that an edit trigger keyword emits model start, diff log, and model end."""
    events: list[dict] = []

    async def listener(event: dict) -> None:
        events.append(event)

    message_bus.subscribe(EventType.ACTIVITY.value, listener)
    message_bus.subscribe(EventType.TELEMETRY_LOG.value, listener)

    executor = MockAgentExecutor(message_bus, latency=0.0)
    await executor.run(agent_session, "Please fix the print statements using diffs.")

    # 1. Verify we received exactly 3 events
    assert len(events) == 3

    # 2. Verify turn start
    assert events[0]["type"] == EventType.ACTIVITY.value
    assert events[0]["payload"]["type"] == ActivityType.MODEL_START.value

    # 3. Verify editing diff telemetry log
    assert events[1]["type"] == EventType.TELEMETRY_LOG.value
    assert events[1]["payload"]["id"] == MOCK_DIFF_PAYLOAD_ID
    assert events[1]["payload"]["type"] == "diff"
    assert events[1]["payload"]["full_content"] == MOCK_DIFF_PAYLOAD["full_content"]

    # 4. Verify turn end
    assert events[2]["type"] == EventType.ACTIVITY.value
    assert events[2]["payload"]["type"] == ActivityType.MODEL_END.value


@pytest.mark.asyncio
async def test_mock_executor_error_scenario(message_bus: MessageBus, agent_session: AgentSession) -> None:
    """Verify that an error trigger keyword emits error telemetry log, raises ValueError, and still emits model end."""
    events: list[dict] = []

    async def listener(event: dict) -> None:
        events.append(event)

    message_bus.subscribe(EventType.ACTIVITY.value, listener)
    message_bus.subscribe(EventType.TELEMETRY_LOG.value, listener)

    executor = MockAgentExecutor(message_bus, latency=0.0)

    with pytest.raises(ValueError) as exc_info:
        await executor.run(agent_session, "Trigger a simulated failure now.")

    # Verify descriptive diagnostic context in the exception
    assert "Simulated executor exception requested" in str(exc_info.value)
    assert "failure" in str(exc_info.value)

    # Verify we received model start, error log, and still got model end due to try/finally
    assert len(events) == 3
    assert events[0]["type"] == EventType.ACTIVITY.value
    assert events[0]["payload"]["type"] == ActivityType.MODEL_START.value

    assert events[1]["type"] == EventType.TELEMETRY_LOG.value
    assert events[1]["payload"]["id"] == MOCK_ERROR_PAYLOAD_ID
    assert events[1]["payload"]["type"] == "thought"
    assert events[1]["payload"]["full_content"] == MOCK_ERROR_PAYLOAD["full_content"]

    assert events[2]["type"] == EventType.ACTIVITY.value
    assert events[2]["payload"]["type"] == ActivityType.MODEL_END.value


@pytest.mark.asyncio
async def test_mock_executor_default_scenario(message_bus: MessageBus, agent_session: AgentSession) -> None:
    """Verify that default prompt emits model start, custom message log, and model end."""
    events: list[dict] = []

    async def listener(event: dict) -> None:
        events.append(event)

    message_bus.subscribe(EventType.ACTIVITY.value, listener)
    message_bus.subscribe(EventType.TELEMETRY_LOG.value, listener)

    executor = MockAgentExecutor(message_bus, latency=0.0)
    prompt = "Hello Savant!"
    await executor.run(agent_session, prompt)

    # 1. Verify we received exactly 3 events
    assert len(events) == 3

    # 2. Verify turn start
    assert events[0]["type"] == EventType.ACTIVITY.value
    assert events[0]["payload"]["type"] == ActivityType.MODEL_START.value

    # 3. Verify message response telemetry log
    assert events[1]["type"] == EventType.TELEMETRY_LOG.value
    expected_payload = make_default_response_payload(prompt)
    assert events[1]["payload"]["type"] == "message"
    assert events[1]["payload"]["full_content"] == expected_payload["full_content"]

    # 4. Verify turn end
    assert events[2]["type"] == EventType.ACTIVITY.value
    assert events[2]["payload"]["type"] == ActivityType.MODEL_END.value


def test_mock_executor_invalid_initialization() -> None:
    """Verify that MockAgentExecutor validates its arguments during __init__ and run."""
    with pytest.raises(TypeError):
        # Invalid MessageBus type
        MockAgentExecutor(message_bus="not_a_bus")  # type: ignore

    with pytest.raises(TypeError):
        # Invalid latency type
        MockAgentExecutor(MessageBus(), latency="slow")  # type: ignore

    with pytest.raises(ValueError):
        # Negative latency
        MockAgentExecutor(MessageBus(), latency=-1.0)
