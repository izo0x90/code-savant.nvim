import asyncio
import pytest
from typing import Any
from engine.bus import MessageBus
from engine.types import (
    EventEnvelope,
    EventType,
    ToolConfirmationRequestPayload,
    ToolConfirmationResponsePayload,
    ToolCallSpec
)

@pytest.mark.asyncio
async def test_message_bus_publish_subscribe() -> None:
    bus = MessageBus()
    received_events = []

    async def listener(envelope: EventEnvelope[Any]) -> None:
        received_events.append(envelope)

    bus.subscribe(EventType.TELEMETRY_THOUGHT.value, listener)

    # Publish on the bus using EventEnvelope (pure design)
    envelope = EventEnvelope(
        event_type=EventType.TELEMETRY_THOUGHT,
        payload={"text": "Hello world!"},
        sender="test_sender"
    )
    await bus.publish(envelope)

    assert len(received_events) == 1
    assert received_events[0].payload["text"] == "Hello world!"
    assert received_events[0].sender == "test_sender"


@pytest.mark.asyncio
async def test_message_bus_subscriber_error_handling() -> None:
    bus = MessageBus()
    received = []

    async def throwing_listener(envelope: EventEnvelope[Any]) -> None:
        raise RuntimeError("Boom!")

    async def healthy_listener(envelope: EventEnvelope[Any]) -> None:
        received.append(envelope)

    bus.subscribe(EventType.TELEMETRY_THOUGHT.value, throwing_listener)
    bus.subscribe(EventType.TELEMETRY_THOUGHT.value, healthy_listener)

    # Publish should not raise exception but print it to stderr
    await bus.publish(EventEnvelope(
        event_type=EventType.TELEMETRY_THOUGHT,
        payload={"text": "crash-test"},
        sender="test_sender"
    ))

    # Check that healthy subscriber still executed successfully
    assert len(received) == 1
    assert received[0].payload["text"] == "crash-test"


@pytest.mark.asyncio
async def test_message_bus_request_response() -> None:
    bus = MessageBus()

    async def replier(envelope: EventEnvelope[Any]) -> None:
        correlation_id = envelope.correlation_id
        if correlation_id:
            # Send back the response with matching correlation ID
            await bus.publish(EventEnvelope(
                event_type=EventType.TOOL_CONFIRMATION_RESPONSE,
                payload=ToolConfirmationResponsePayload(confirmed=True),
                sender="replier",
                correlation_id=correlation_id
            ))

    bus.subscribe(EventType.TOOL_CONFIRMATION_REQUEST.value, replier)

    # Instantiate a pure, explicit request envelope
    request_env = EventEnvelope(
        event_type=EventType.TOOL_CONFIRMATION_REQUEST,
        payload=ToolConfirmationRequestPayload(
            tool_call=ToolCallSpec(id="1", name="foo", args={})
        ),
        sender="test_caller",
        correlation_id="test-correlation-id"
    )

    response_env = await bus.request(
        request_env,
        EventType.TOOL_CONFIRMATION_RESPONSE.value,
        2.0
    )

    # Pure, strongly-typed assertion (No dictionary peeking)
    assert isinstance(response_env.payload, ToolConfirmationResponsePayload)
    assert response_env.payload.confirmed is True
    assert response_env.correlation_id == "test-correlation-id"


@pytest.mark.asyncio
async def test_message_bus_request_timeout() -> None:
    bus = MessageBus()

    # Request with no reply, should trigger TimeoutError
    request_env = EventEnvelope(
        event_type=EventType.TOOL_CONFIRMATION_REQUEST,
        payload=ToolConfirmationRequestPayload(
            tool_call=ToolCallSpec(id="2", name="bar", args={})
        ),
        sender="test_caller",
        correlation_id="test-timeout-id"
    )

    with pytest.raises(asyncio.TimeoutError):
        await bus.request(
            request_env,
            EventType.TOOL_CONFIRMATION_RESPONSE.value,
            timeout_sec=0.1
        )


@pytest.mark.asyncio
async def test_message_bus_derive_hierarchy() -> None:
    parent_bus = MessageBus(name="parent")
    child_bus = parent_bus.derive("child")

    received_parent_events = []

    async def parent_listener(envelope: EventEnvelope[Any]) -> None:
        received_parent_events.append(envelope)

    # Subscribing on the derived bus actually registers on the shared context
    child_bus.subscribe(EventType.TELEMETRY_THOUGHT.value, parent_listener)

    # Publishing on the child bus delegates up to parent
    await child_bus.publish(EventEnvelope(
        event_type=EventType.TELEMETRY_THOUGHT,
        payload={"text": "hello child"},
        sender="child"
    ))

    assert len(received_parent_events) == 1
    assert received_parent_events[0].payload["text"] == "hello child"
    assert received_parent_events[0].sender == "parent/child"


@pytest.mark.asyncio
async def test_message_bus_input_validation() -> None:
    bus = MessageBus()

    # Invalid event_type type
    with pytest.raises(TypeError):
        bus.subscribe(123, lambda x: None)  # type: ignore

    # Empty event_type
    with pytest.raises(ValueError):
        bus.subscribe("", lambda x: None)  # type: ignore

    # Non-callable listener
    with pytest.raises(TypeError):
        bus.subscribe(EventType.TELEMETRY_ACTIVITY.value, "not-callable")  # type: ignore

    # Invalid request arguments
    with pytest.raises(TypeError):
        await bus.request("not-envelope", EventType.TOOL_CONFIRMATION_RESPONSE.value, 1.0)  # type: ignore

    request_env_no_id = EventEnvelope(
        event_type=EventType.TOOL_CONFIRMATION_REQUEST,
        payload=ToolConfirmationRequestPayload(
            tool_call=ToolCallSpec(id="4", name="baz", args={})
        ),
        sender="test_caller"
    )
    with pytest.raises(ValueError):
        await bus.request(request_env_no_id, "", 1.0)
