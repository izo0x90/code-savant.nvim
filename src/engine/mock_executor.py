from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Final, List

from engine.bus import MessageBus
from engine.sessions import AgentSession
from engine.types import ActivityType, EventEnvelope, EventType

logger = logging.getLogger("engine.mock_executor")


# ==============================================================================
# Centralized Strongly Typed Constants (Triggers, Latencies, Payloads)
# ==============================================================================

# Keywords trigger lists (lowercase for uniform matching)
TRIGGER_THINK_KEYWORDS: Final[List[str]] = ["think", "thought"]
TRIGGER_EDIT_KEYWORDS: Final[List[str]] = ["edit", "fix", "diff"]
TRIGGER_ERROR_KEYWORDS: Final[List[str]] = ["error", "fail"]

# Default simulated latencies in seconds
DEFAULT_MOCK_LATENCY: Final[float] = 0.1

# Payload identifiers and structures
MOCK_THOUGHT_PAYLOAD_ID: Final[str] = "meta_thought_102"
MOCK_THOUGHT_PAYLOAD: Final[Dict[str, Any]] = {
    "id": MOCK_THOUGHT_PAYLOAD_ID,
    "type": "thought",
    "title": "Reasoning about optimization...",
    "full_content": (
        "Analyzing active session database queries.\n"
        "Evaluating index constraints on users table..."
    ),
}

MOCK_DIFF_PAYLOAD_ID: Final[str] = "diff_block_102"
MOCK_DIFF_CONTENT: Final[str] = (
    "--- a/src/engine/main.py\n"
    "+++ b/src/engine/main.py\n"
    "@@ -10,1 +10,1 @@\n"
    '-print("Hello, world!")\n'
    '+logger.info("Hello, optimized world!")'
)
MOCK_DIFF_PAYLOAD: Final[Dict[str, Any]] = {
    "id": MOCK_DIFF_PAYLOAD_ID,
    "type": "diff",
    "title": "Applied logger optimizations",
    "full_content": MOCK_DIFF_CONTENT,
}

MOCK_ERROR_PAYLOAD_ID: Final[str] = "err_block_102"
MOCK_ERROR_PAYLOAD: Final[Dict[str, Any]] = {
    "id": MOCK_ERROR_PAYLOAD_ID,
    "type": "thought",
    "title": "Fatal operation failure",
    "full_content": "An unexpected error occurred during execution of the user's prompt.",
}

MOCK_DEFAULT_PAYLOAD_ID: Final[str] = "msg_block_102"


def make_default_response_payload(prompt: str) -> Dict[str, Any]:
    """
    Constructs a default message payload containing the user's prompt.

    Args:
        prompt: The user-provided prompt.

    Returns:
        A strongly typed payload dictionary.
    """
    if not isinstance(prompt, str):
        raise TypeError(f"prompt must be a string, got {type(prompt).__name__}")
    return {
        "id": MOCK_DEFAULT_PAYLOAD_ID,
        "type": "message",
        "title": "Response",
        "full_content": f"Hello! This is a simulated response to: '{prompt}'.",
    }


# ==============================================================================
# MockAgentExecutor Simulation Implementation
# ==============================================================================

class MockAgentExecutor:
    """
    Simulates the async execution loop of LocalAgentExecutor.
    Subscribes to a session's MessageBus and publishes a timed stream
    of mock telemetry events based on keyword-matching in user prompts.
    """

    def __init__(self, message_bus: MessageBus, latency: float = DEFAULT_MOCK_LATENCY) -> None:
        """
        Initializes the MockAgentExecutor with dependency injection for MessageBus and latency.

        Args:
            message_bus: MessageBus instance to publish events to.
            latency: Simulated processing delay in seconds.
        """
        if not isinstance(message_bus, MessageBus):
            raise TypeError(f"message_bus must be an instance of MessageBus, got {type(message_bus).__name__}")
        if not isinstance(latency, (int, float)):
            raise TypeError(f"latency must be an int or float, got {type(latency).__name__}")
        if latency < 0:
            raise ValueError(f"latency must be non-negative, got {latency}")

        self.bus = message_bus
        self.latency = latency

    async def run(self, session: AgentSession, prompt: str) -> None:
        """
        Simulates the async executor loop, publishing telemetry and saving session state.

        Args:
            session: Active AgentSession context.
            prompt: User-provided input prompt string.

        Raises:
            ValueError: If prompt matches any of the error/failure trigger keywords.
        """
        if not isinstance(session, AgentSession):
            raise TypeError(f"session must be an instance of AgentSession, got {type(session).__name__}")
        if not isinstance(prompt, str):
            raise TypeError(f"prompt must be a string, got {type(prompt).__name__}")

        prompt_lower = prompt.lower()

        # Emit turn start
        await self.bus.publish(
            EventEnvelope(
                event_type=EventType.ACTIVITY,
                payload={"type": ActivityType.MODEL_START, "prompt_length": len(prompt)},
                sender="mock_agent_executor",
            )
        )

        try:
            # Check for error / failure trigger keywords
            if any(kw in prompt_lower for kw in TRIGGER_ERROR_KEYWORDS):
                await self.bus.publish(
                    EventEnvelope(
                        event_type=EventType.TELEMETRY_LOG,
                        payload=dict(MOCK_ERROR_PAYLOAD),
                        sender="mock_agent_executor",
                    )
                )
                # Fail loudly with custom error and descriptive context
                matched_keywords = [kw for kw in TRIGGER_ERROR_KEYWORDS if kw in prompt_lower]
                raise ValueError(
                    f"Simulated executor exception requested by user prompt. "
                    f"Prompt snippet: '{prompt[:50]}'. Trigger keywords matched: {matched_keywords}."
                )

            # Check for thinking trigger keywords
            if any(kw in prompt_lower for kw in TRIGGER_THINK_KEYWORDS):
                await self.bus.publish(
                    EventEnvelope(
                        event_type=EventType.TELEMETRY_LOG,
                        payload=dict(MOCK_THOUGHT_PAYLOAD),
                        sender="mock_agent_executor",
                    )
                )
                await asyncio.sleep(self.latency)

            # Check for edit/fix/diff trigger keywords
            elif any(kw in prompt_lower for kw in TRIGGER_EDIT_KEYWORDS):
                await self.bus.publish(
                    EventEnvelope(
                        event_type=EventType.TELEMETRY_LOG,
                        payload=dict(MOCK_DIFF_PAYLOAD),
                        sender="mock_agent_executor",
                    )
                )
                await asyncio.sleep(self.latency)

            # Default fallback scenario
            else:
                await self.bus.publish(
                    EventEnvelope(
                        event_type=EventType.TELEMETRY_LOG,
                        payload=make_default_response_payload(prompt),
                        sender="mock_agent_executor",
                    )
                )
                await asyncio.sleep(self.latency)

        finally:
            # Emit turn end (always executed to ensure state and status consistency in the bus)
            await self.bus.publish(
                EventEnvelope(
                    event_type=EventType.ACTIVITY,
                    payload={"type": ActivityType.MODEL_END, "response_length": 100},
                    sender="mock_agent_executor",
                )
            )
