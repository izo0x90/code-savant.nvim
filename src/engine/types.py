"""
Lightweight data transport classes utilizing slots=True and frozen=True for speed, memory efficiency, and runtime immutability.
Used strictly for internal communication; Pydantic or dicts are used at serialization boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional, Dict, Generic, TypeVar, Union, Protocol, AsyncIterator
import time
import uuid
from pydantic import BaseModel, ConfigDict, Field, JsonValue, AliasChoices

T = TypeVar("T")


# ==============================================================================
# Unified Slotted & Frozen Pydantic v2 Models (Metadata & Sidecars)
# ==============================================================================

class SessionMetadataPayload(BaseModel):
    """Strict representation of session metadata."""
    model_config = ConfigDict(frozen=True, slots=True)
    name: Optional[str] = Field(default=None, description="Name or title of the session.")
    query: Optional[str] = Field(default=None, description="The initial user query for the session.")
    created_at: Optional[str] = Field(default=None, description="ISO timestamp when session was created.")
    last_updated: Optional[str] = Field(default=None, description="ISO timestamp when session was last updated.")
    turn_count: Optional[int] = Field(default=None, description="The number of turns completed in the session.")
    mock_mode: bool = Field(default=False, description="Whether the session is running in mock mode.")


class SessionMetaSidecar(BaseModel):
    """Strict representation of companion sidecar metadata files on disk."""
    model_config = ConfigDict(frozen=True, slots=True)
    session_id: str
    metadata: SessionMetadataPayload
    turn_count: int


class FunctionDeclarationSpec(BaseModel):
    """Strict representation of LLM function call declaration parameters."""
    model_config = ConfigDict(frozen=True, slots=True)
    name: str
    description: str
    parameters: Dict[str, JsonValue]


class MessageRole(str, Enum):
    USER = "user"
    MODEL = "model"
    SYSTEM = "system"


class LoopStatus(str, Enum):
    STATUS_CONTINUE = "continue"
    STATUS_STOP = "stop"


class TerminationReason(str, Enum):
    REASON_GOAL = "GOAL"
    REASON_TIMEOUT = "TIMEOUT"
    REASON_MAX_TURNS = "MAX_TURNS"
    REASON_ABORTED = "ABORTED"
    REASON_ERROR = "ERROR"
    REASON_ERROR_NO_COMPLETE_TASK_CALL = "ERROR_NO_COMPLETE_TASK_CALL"


class ExecutorAgentConfig(BaseModel):
    """Strict validated configuration model for LocalAgentExecutor."""
    model_config = ConfigDict(frozen=True, slots=True, populate_by_name=True)
    name: str = "agent"
    max_turns: int = Field(default=10, validation_alias=AliasChoices("max_turns", "maxTurns"))
    max_time_seconds: int = Field(default=60, validation_alias=AliasChoices("max_time_seconds", "maxTimeSeconds"))
    recovery_time_seconds: int = Field(default=30, validation_alias=AliasChoices("recovery_time_seconds", "recoveryTimeSeconds"))
    plan_mode: bool = Field(default=False, validation_alias=AliasChoices("plan_mode", "planMode", "plan_mode"))
    requires_approval: bool = Field(default=False, validation_alias=AliasChoices("requires_approval", "requiresApproval"))
    query: str = "Investigate target file inside {{target_dir}}"


# ==============================================================================
# Decoupled Structural Protocols (Unidirectional, Decoupled Typings)
# ==============================================================================

class MessageBusProtocol(Protocol):
    async def publish(self, event: Dict[str, Any]) -> None: ...
    async def request(self, payload: Dict[str, Any], response_type: str) -> Dict[str, Any]: ...
    def derive(self, namespace: str) -> MessageBusProtocol: ...

class AgentSessionProtocol(Protocol):
    @property
    def session_id(self) -> str: ...
    @property
    def chat_history(self) -> List[ChatMessage]: ...
    @property
    def metadata(self) -> SessionMetadataPayload: ...
    async def append_message(self, message: ChatMessage) -> None: ...
    async def set_history(self, history: List[ChatMessage]) -> None: ...

class SessionManagerProtocol(Protocol):
    async def create_sub_session(self, parent_session_id: str, agent_name: str, query: str) -> AgentSessionProtocol: ...
    async def load_session(self, session_id: str) -> AgentSessionProtocol: ...
    async def save_session(self, session: AgentSessionProtocol) -> None: ...
    async def list_sessions(self) -> List[SessionMetaSidecar]: ...

class GenAIClientProtocol(Protocol):
    def generate_response_stream(
        self,
        system_prompt: str,
        chat_history: List[ChatMessage],
        tools_declarations: List[FunctionDeclarationSpec],
        agent_name: str = "",
        turn_counter: int = 0,
        agent_id: str = ""
    ) -> AsyncIterator[Any]: ...


# ==============================================================================
# Unified Slotted & Frozen Pydantic v2 Message Part Models
# ==============================================================================

class TextPart(BaseModel):
    """Immutable representation of a text-based turn segment."""
    model_config = ConfigDict(frozen=True, slots=True)
    text: str

class FunctionCallPart(BaseModel):
    """
    Immutable representation of an LLM tool invocation request.
    Strictly type-restricted to enforce purely serializable JSON values.
    """
    model_config = ConfigDict(frozen=True, slots=True)
    name: str
    args: Dict[str, JsonValue]
    id: str
    thought_signature: Optional[str] = None

class FunctionResponsePart(BaseModel):
    """
    Immutable representation of a tool execution outcome returned to the LLM.
    Guarantees zero leakage of non-serializable runtime handles.
    """
    model_config = ConfigDict(frozen=True, slots=True)
    name: str
    response: JsonValue

class ToolCallPart(BaseModel):
    """
    Immutable representation of an LLM tool invocation request.
    Strictly type-restricted to enforce purely serializable JSON values.
    """
    model_config = ConfigDict(frozen=True, slots=True)
    name: str
    args: Dict[str, JsonValue]
    id: str
    thought_signature: Optional[str] = None

class ToolResultPart(BaseModel):
    """
    Immutable representation of a tool execution outcome returned to the LLM.
    Guarantees zero leakage of non-serializable runtime handles.
    """
    model_config = ConfigDict(frozen=True, slots=True)
    name: str
    response: JsonValue
    id: Optional[str] = None

MessagePart = Union[TextPart, FunctionCallPart, FunctionResponsePart, ToolCallPart, ToolResultPart]

class SessionLogRecord(BaseModel):
    """
    Immutable representation of a structured log record for a session event.
    """
    model_config = ConfigDict(frozen=True, slots=True)
    session_id: str
    timestamp: float = Field(default_factory=time.time)
    event: str
    message: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ==============================================================================
# Strict Chat Message & Execution Context
# ==============================================================================

class ChatMessage(BaseModel):
    """
    Immutable representation of an individual conversation turn.
    Eliminates broad, loose dictionaries inside core orchestrator logic.
    """
    model_config = ConfigDict(frozen=True, slots=True)
    id: str = Field(default_factory=lambda: str(uuid.uuid7()))
    timestamp: float = Field(default_factory=time.time)
    role: str
    parts: List[MessagePart]
    metadata: Optional[Dict[str, Any]] = Field(default=None)

@dataclass(slots=True, frozen=True)
class ModelRequestContext:
    """Encapsulates all compiled model generation parameters with absolute type-safety."""
    system_instruction: str
    tools: List[FunctionDeclarationSpec]  # Zero Dict[str, Any] schemas!
    contents: List[ChatMessage]          # Zero Dict[str, Any] message lists!

@dataclass(slots=True, frozen=True)
class ExecutionContext:
    """
    Immutable snapshot of the active executor's runtime dependencies and constraints.
    Safely propagated across executing tool chains, interceptor guards, and stateless tools.
    100% strictly typed to guarantee compile-time verification without circular imports.
    """
    workspace_path: Path
    message_bus: MessageBusProtocol
    remaining_depth: int
    session: AgentSessionProtocol
    session_manager: SessionManagerProtocol
    client: GenAIClientProtocol


# ==============================================================================
# Core Orchestrator Transport Dataclasses
# ==============================================================================

@dataclass(slots=True, frozen=True)
class ToolCall:
    name: str
    args: Dict[str, Any]
    id: str
    thought_signature: Optional[str] = None


@dataclass(slots=True, frozen=True)
class ThoughtChunk:
    text: str


@dataclass(slots=True, frozen=True)
class CompletionChunk:
    function_calls: List[ToolCall]
    thought_signature: Optional[str] = None


@dataclass(slots=True, frozen=True)
class ToolExecutionOutcome:
    tool_responses: List[Dict[str, Any]]
    task_completed: bool
    submitted_output: Optional[str] = None
    aborted: bool = False


@dataclass(slots=True, frozen=True)
class TurnOutcome:
    status: str  # "continue" or "stop"
    terminate_reason: Optional[str] = None
    final_result: Optional[str] = None
    next_message: Optional[Dict[str, Any]] = None


# ==============================================================================
# Telemetry event enums and wrappers
# ==============================================================================

class EventType(str, Enum):
    TOOL_CONFIRMATION_REQUEST = "tool-confirmation-request"
    TOOL_CONFIRMATION_RESPONSE = "tool-confirmation-response"
    TELEMETRY_THOUGHT = "telemetry:thought"
    TELEMETRY_ACTIVITY = "telemetry:activity"


class TelemetryActivityType(str, Enum):
    START = "START"
    TURN_START = "TURN_START"
    STEERING_QUEUED = "STEERING_QUEUED"
    STEERING_INJECTED = "STEERING_INJECTED"
    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_END = "TOOL_CALL_END"
    RECOVERY = "RECOVERY"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    STOP = "STOP"
    END = "END"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVAL_DENIED = "APPROVAL_DENIED"


class TelemetryThoughtPayload(BaseModel):
    model_config = ConfigDict(frozen=True, slots=True)
    text: str


class TelemetryActivityPayload(BaseModel):
    model_config = ConfigDict(frozen=True, slots=True)
    activity_type: TelemetryActivityType
    msg: str = ""
    name: Optional[str] = None
    callId: Optional[str] = None
    args: Optional[Dict[str, Any]] = None
    id: Optional[str] = None
    response: Optional[Any] = None
    system_prompt: Optional[str] = None
    query: Optional[str] = None
    prompt_id: Optional[str] = None
    tool: Optional[str] = None


class ToolCallSpec(BaseModel):
    model_config = ConfigDict(frozen=True, slots=True)
    id: str
    name: str
    args: Dict[str, Any]


class ToolConfirmationRequestPayload(BaseModel):
    model_config = ConfigDict(frozen=True, slots=True)
    tool_call: ToolCallSpec


class ToolConfirmationResponsePayload(BaseModel):
    model_config = ConfigDict(frozen=True, slots=True)
    confirmed: bool


class EventEnvelope(BaseModel, Generic[T]):
    """
    Programmatic, typed generic container wrapping event payloads with metadata.
    """
    model_config = ConfigDict(frozen=True, slots=True)
    event_type: EventType
    payload: T
    sender: str
    correlation_id: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)

