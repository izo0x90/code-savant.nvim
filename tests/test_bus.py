import asyncio
import pytest
from typing import Any
from engine.bus import MessageBus
from engine.types import EventEnvelope, EventType

@pytest.mark.asyncio
async def test_message_bus_basic_pub_sub() -> None:
    bus = MessageBus()
    received_events = []

    async def listener(envelope: EventEnvelope[Any]) -> None:
        received_events.append(envelope.to_dict())

    # Subscribe
    sub_res = bus.subscribe(EventType.TELEMETRY_THOUGHT.value, listener)
    assert "subscription_id" in sub_res
    assert isinstance(sub_res["subscription_id"], str)

    # Publish dictionary
    await bus.publish({"type": EventType.TELEMETRY_THOUGHT.value, "text": "hello"})
    assert len(received_events) == 1
    assert received_events[0]["text"] == "hello"
    assert received_events[0]["sender"] == "main"

    # Unsubscribe by callback
    bus.unsubscribe(EventType.TELEMETRY_THOUGHT.value, listener)
    await bus.publish({"type": EventType.TELEMETRY_THOUGHT.value, "text": "world"})
    assert len(received_events) == 1  # Unsubscribed, count remains 1


@pytest.mark.asyncio
async def test_message_bus_unsubscribe_by_id() -> None:
    bus = MessageBus()
    received_events = []

    async def listener(envelope: EventEnvelope[Any]) -> None:
        received_events.append(envelope.to_dict())

    sub_res = bus.subscribe(EventType.TELEMETRY_THOUGHT.value, listener)
    sub_id = sub_res["subscription_id"]

    await bus.publish({"type": EventType.TELEMETRY_THOUGHT.value, "text": "msg1"})
    assert len(received_events) == 1

    # Unsubscribe by subscription_id
    bus.unsubscribe(EventType.TELEMETRY_THOUGHT.value, sub_id)
    await bus.publish({"type": EventType.TELEMETRY_THOUGHT.value, "text": "msg2"})
    assert len(received_events) == 1


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
        await bus.request("not-dict", EventType.TOOL_CONFIRMATION_RESPONSE.value, 1.0)  # type: ignore

    with pytest.raises(ValueError):
        await bus.request({"data": 1}, "", 1.0)


@pytest.mark.asyncio
async def test_message_bus_publish_formats() -> None:
    bus = MessageBus()
    received_events = []

    async def listener(envelope: EventEnvelope[Any]) -> None:
        received_events.append(envelope.to_dict())

    bus.subscribe(EventType.TELEMETRY_THOUGHT.value, listener)

    # Publish with message parameter
    await bus.publish(message={"type": EventType.TELEMETRY_THOUGHT.value, "text": "hello 1"})
    # Publish with event parameter (complying with some API contract structures)
    await bus.publish(event={"type": EventType.TELEMETRY_THOUGHT.value, "text": "hello 2"})

    assert len(received_events) == 2
    assert received_events[0]["text"] == "hello 1"
    assert received_events[1]["text"] == "hello 2"

    # Attempt publishing None
    with pytest.raises(ValueError):
        await bus.publish(None)


@pytest.mark.asyncio
async def test_message_bus_subscriber_exceptions(capsys: pytest.CaptureFixture[str]) -> None:
    bus = MessageBus()
    received = []

    async def throwing_listener(envelope: EventEnvelope[Any]) -> None:
        raise RuntimeError("Subscriber crashed intentionally")

    async def healthy_listener(envelope: EventEnvelope[Any]) -> None:
        received.append(envelope.to_dict())

    bus.subscribe(EventType.TELEMETRY_THOUGHT.value, throwing_listener)
    bus.subscribe(EventType.TELEMETRY_THOUGHT.value, healthy_listener)

    # Publish should not raise exception but print it to stderr
    await bus.publish({"type": EventType.TELEMETRY_THOUGHT.value, "text": "crash-test"})

    # Check that healthy subscriber still executed successfully
    assert len(received) == 1
    assert received[0]["text"] == "crash-test"


@pytest.mark.asyncio
async def test_message_bus_request_response() -> None:
    bus = MessageBus()

    async def replier(envelope: EventEnvelope[Any]) -> None:
        msg = envelope.to_dict()
        correlation_id = msg.get("correlationId")
        if correlation_id:
            # Send back the response with matching correlation ID
            await bus.publish({
                "type": EventType.TOOL_CONFIRMATION_RESPONSE.value,
                "correlationId": correlation_id,
                "confirmed": True
            })

    bus.subscribe(EventType.TOOL_CONFIRMATION_REQUEST.value, replier)

    # Execute request
    response = await bus.request(
        {"type": EventType.TOOL_CONFIRMATION_REQUEST.value, "toolCall": {"id": "1", "name": "foo", "args": {}}},
        EventType.TOOL_CONFIRMATION_RESPONSE.value,
        2.0
    )
    assert response["confirmed"] is True
    assert "correlationId" in response


@pytest.mark.asyncio
async def test_message_bus_request_timeout() -> None:
    bus = MessageBus()

    # Request with no reply, should trigger TimeoutError
    with pytest.raises(asyncio.TimeoutError):
        await bus.request(
            {"type": EventType.TOOL_CONFIRMATION_REQUEST.value, "toolCall": {"id": "2", "name": "bar", "args": {}}},
            EventType.TOOL_CONFIRMATION_RESPONSE.value,
            timeout_sec=0.1
        )


@pytest.mark.asyncio
async def test_message_bus_derive_hierarchy() -> None:
    parent_bus = MessageBus(name="parent")
    child_bus = parent_bus.derive("child")

    received_parent_events = []

    async def parent_listener(envelope: EventEnvelope[Any]) -> None:
        received_parent_events.append(envelope.to_dict())

    # Subscribing on the derived bus actually registers on the shared context
    child_bus.subscribe(EventType.TELEMETRY_THOUGHT.value, parent_listener)

    # Publishing on the child bus delegates up to parent
    await child_bus.publish({"type": EventType.TELEMETRY_THOUGHT.value, "text": "hello child"})

    assert len(received_parent_events) == 1
    assert received_parent_events[0]["text"] == "hello child"
    assert received_parent_events[0]["sender"] == "parent/child"


@pytest.mark.asyncio
async def test_message_bus_derive_tool_confirmation() -> None:
    parent_bus = MessageBus(name="parent")
    child_bus = parent_bus.derive("child")
    grandchild_bus = child_bus.derive("grandchild")

    received_events = []

    async def listener(envelope: EventEnvelope[Any]) -> None:
        received_events.append(envelope.to_dict())

    parent_bus.subscribe(EventType.TOOL_CONFIRMATION_REQUEST.value, listener)

    # Publish on grandchild
    await grandchild_bus.publish({
        "type": EventType.TOOL_CONFIRMATION_REQUEST.value,
        "toolCall": {"id": "3", "name": "run_command", "args": {}}
    })

    assert len(received_events) == 1
    # Check hierarchy nesting in 'subagent' attribute
    assert received_events[0]["subagent"] == "child/grandchild"
    assert received_events[0]["sender"] == "parent/child/grandchild"
