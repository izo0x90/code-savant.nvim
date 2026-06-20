from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from engine.bus import MessageBus
from engine.context import estimate_text_tokens
from engine.constants import (
    CHECKPOINT_SEPARATOR,
    FALLBACK_THOUGHT_TITLE,
    SCRATCH_DIR_NAME,
    SESSION_FILE_SUFFIX,
    SESSION_META_SUFFIX,
    TOOL_LOG_FILE_PREFIX,
    TOOL_LOG_FILE_SUFFIX,
)
from engine.client import LiveGenAIClient, MockGenAIClient
from engine.config import SettingsManager
from engine.registry import ModelRegistryService
from engine.sessions import AgentSession, SessionManager
from engine.types import (
    EventEnvelope,
    EventType,
    SessionMetadataPayload,
    TelemetryActivityType,
    JsonRpcNotification,
    JsonRpcResponse,
    JsonRpcErrorResponse,
    JsonRpcErrorPayload,
)

# Centralized Logger for UDS Server
logger = logging.getLogger("engine.uds_server")


class JsonRpcCodec:
    """
    Encoder/Decoder utility for formatting and parsing JSON-RPC 2.0 payloads.
    Guarantees strict formatting with newline delimiters and protocol-compliant structures.
    """

    JSONRPC_VERSION: str = "2.0"
    DELIMITER: str = "\n"

    @staticmethod
    def encode_response(result: Dict[str, Any], msg_id: int | str) -> bytes:
        """Formats a success response into a newline-delimited JSON-RPC 2.0 payload."""
        if not isinstance(result, dict):
            raise TypeError("result must be a dictionary, got " + type(result).__name__)
        if not isinstance(msg_id, (int, str)):
            raise TypeError("msg_id must be int or str, got " + type(msg_id).__name__)

        frame = JsonRpcResponse(result=result, id=msg_id)
        return (frame.model_dump_json() + JsonRpcCodec.DELIMITER).encode("utf-8")

    @staticmethod
    def encode_error(
        code: int, message: str, msg_id: int | str | None = None, data: Any = None
    ) -> bytes:
        """Formats an error response into a newline-delimited JSON-RPC 2.0 payload."""
        if not isinstance(code, int):
            raise TypeError("code must be an integer, got " + type(code).__name__)
        if not isinstance(message, str):
            raise TypeError("message must be a string, got " + type(message).__name__)
        if msg_id is not None and not isinstance(msg_id, (int, str)):
            raise TypeError(
                "msg_id must be int, str, or None, got " + type(msg_id).__name__
            )

        error_payload = JsonRpcErrorPayload(code=code, message=message, data=data)
        frame = JsonRpcErrorResponse(
            error=error_payload.model_dump(exclude_none=True), id=msg_id
        )
        return (frame.model_dump_json() + JsonRpcCodec.DELIMITER).encode("utf-8")

    @staticmethod
    def encode_notification(method: str, params: Dict[str, Any]) -> bytes:
        """Formats a notification into a newline-delimited JSON-RPC 2.0 payload."""
        if not isinstance(method, str):
            raise TypeError("method must be a string, got " + type(method).__name__)
        if not method.strip():
            raise ValueError("method cannot be empty or whitespace-only")
        if not isinstance(params, dict):
            raise TypeError("params must be a dictionary, got " + type(params).__name__)

        frame = JsonRpcNotification(method=method, params=params)
        return (frame.model_dump_json() + JsonRpcCodec.DELIMITER).encode("utf-8")


class JsonRpcError(Exception):
    """Custom exception representing a JSON-RPC 2.0 protocol or application-level error."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass
class ActiveSessionState:
    """Thread-safe session memory registry state."""

    session: AgentSession
    bus: MessageBus
    telemetry_task: asyncio.Task[None]
    workspace_path: Path
    session_manager: SessionManager
    executor_task: asyncio.Task[None] | None = None
    executor: Any | None = None
    context: Any | None = None


class UdsServer:
    """
    Production-first Unix Domain Socket daemon.
    Manages client socket streams, session registration, and event telemetry routing.
    """

    socket_path: Path
    settings_manager: SettingsManager
    model_registry: ModelRegistryService
    session_manager: Optional[SessionManager]
    active_sessions: Dict[uuid.UUID, ActiveSessionState]

    def __init__(
        self,
        socket_path: Path | str,
        settings_manager: SettingsManager,
        model_registry: ModelRegistryService,
        session_manager: Optional[SessionManager] = None,
    ) -> None:
        self.socket_path = (
            Path(socket_path) if isinstance(socket_path, str) else socket_path
        )
        self.settings_manager = settings_manager
        self.model_registry = model_registry
        self.session_manager = session_manager
        self.active_sessions = {}
        self._server: Optional[asyncio.Server] = None

    async def start(self) -> None:
        """Binds to the socket path and starts the asyncio Unix server."""
        if self._server is not None:
            logger.info("UDS Server is already running. Skipping startup.")
            return

        socket_str = str(self.socket_path)
        if os.path.exists(socket_str):
            try:
                os.unlink(socket_str)
            except OSError as e:
                logger.error(
                    f"Failed to clean up pre-existing socket {socket_str}: {e}"
                )
                raise

        self._server = await asyncio.start_unix_server(
            self.handle_connection, path=socket_str
        )
        logger.info(f"UDS Server started on {socket_str}")

    async def stop(self) -> None:
        """Gracefully shuts down the server and cleans up active sessions."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        socket_str = str(self.socket_path)
        if os.path.exists(socket_str):
            try:
                os.unlink(socket_str)
            except OSError:
                pass

        # Cancel all active session tasks
        for session_id in list(self.active_sessions.keys()):
            await self._cleanup_session(session_id)

    async def _cleanup_session(self, session_id: uuid.UUID) -> None:
        """Safely cancels and deletes all tasks associated with a session ID."""
        state = self.active_sessions.pop(session_id, None)
        if not state:
            return

        # Cancel tasks
        state.telemetry_task.cancel()
        if state.executor_task and not state.executor_task.done():
            state.executor_task.cancel()

        try:
            await asyncio.gather(
                state.telemetry_task,
                state.executor_task or asyncio.sleep(0),
                return_exceptions=True,
            )
        except Exception:
            pass

    async def handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Manages individual client stream life-cycles and newline-delimited frames."""
        bound_sessions: list[uuid.UUID] = []
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break

                try:
                    frame = json.loads(line.decode("utf-8").strip())
                except json.JSONDecodeError as e:
                    logger.error(
                        f"Failed to decode incoming JSON line: {line!r}", exc_info=True
                    )
                    writer.write(JsonRpcCodec.encode_error(-32700, f"Parse error: {e}"))
                    await writer.drain()
                    continue

                await self.dispatch_request(frame, writer, bound_sessions)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Unhandled exception in connection loop: {e}", exc_info=True)
        finally:
            # Clean up all sessions bound to this socket connection on disconnect
            for session_id in bound_sessions:
                await self._cleanup_session(session_id)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def dispatch_request(
        self,
        frame: Dict[str, Any],
        writer: asyncio.StreamWriter,
        bound_sessions: list[uuid.UUID],
    ) -> None:
        """Core router that parses JSON-RPC 2.0 frames and invokes matching session actions."""
        msg_id = frame.get("id")
        method = frame.get("method")
        params = frame.get("params", {})

        if frame.get("jsonrpc") != "2.0":
            writer.write(JsonRpcCodec.encode_error(-32600, "Invalid Request", msg_id))
            await writer.drain()
            return

        if not method:
            writer.write(JsonRpcCodec.encode_error(-32600, "Method not found", msg_id))
            await writer.drain()
            return

        try:
            if method == "session/start":
                await self._handle_session_start(params, msg_id, writer, bound_sessions)
            elif method == "session/list":
                await self._handle_session_list(params, msg_id, writer)
            elif method == "session/load":
                await self._handle_session_load(params, msg_id, writer, bound_sessions)
            elif method == "session/send_prompt":
                await self._handle_session_send_prompt(params, msg_id, writer)
            elif method == "session/respond_confirmation":
                await self._handle_session_respond_confirmation(params, msg_id, writer)
            elif method == "session/cancel":
                await self._handle_session_cancel(params, msg_id, writer)
            elif method == "session/inject_steering":
                await self._handle_session_inject_steering(params, msg_id, writer)
            elif method == "session/close":
                await self._handle_session_close(params, msg_id, writer, bound_sessions)
            else:
                writer.write(
                    JsonRpcCodec.encode_error(
                        -32601, f"Method '{method}' not found", msg_id
                    )
                )
                await writer.drain()

        except JsonRpcError as e:
            writer.write(JsonRpcCodec.encode_error(e.code, e.message, msg_id, e.data))
            await writer.drain()
        except Exception as e:
            logger.error(f"Error handling method {method}: {e}", exc_info=True)
            writer.write(
                JsonRpcCodec.encode_error(-32603, f"Internal error: {e}", msg_id)
            )
            await writer.drain()

    async def _handle_session_start(
        self,
        params: Dict[str, Any],
        msg_id: Any,
        writer: asyncio.StreamWriter,
        bound_sessions: list[uuid.UUID],
    ) -> None:
        """Handles initializing a new agent session and its corresponding telemetry stream."""
        workspace_path = params.get("workspace_path")
        agent_profile = params.get("agent_profile", "coder")

        if not workspace_path:
            raise JsonRpcError(-32602, "Missing required workspace_path parameter")

        # Retrieve session storage directory from centralized settings
        settings = self.settings_manager.settings
        configured_path = Path(settings.session_storage_dir)

        # Resolve relative configurations relative to active workspace_path
        if not configured_path.is_absolute():
            storage_dir = Path(workspace_path) / configured_path
        else:
            storage_dir = configured_path

        session_manager = self.session_manager or SessionManager(
            storage_dir=storage_dir,
            session_suffix=SESSION_FILE_SUFFIX,
            meta_suffix=SESSION_META_SUFFIX,
            checkpoint_separator=CHECKPOINT_SEPARATOR,
            scratch_dir_name=SCRATCH_DIR_NAME,
            tool_log_prefix=TOOL_LOG_FILE_PREFIX,
            tool_log_suffix=TOOL_LOG_FILE_SUFFIX,
        )
        await session_manager.ensure_storage_dir()

        # Instantiate new session
        mock_mode = params.get("mock_mode", False)
        session_id = uuid.uuid7()
        session = AgentSession(
            session_id=session_id,
            chat_history=[],
            metadata=SessionMetadataPayload(
                name="uds_session",
                query="",
                created_at=datetime.datetime.now().isoformat(),
                last_updated=datetime.datetime.now().isoformat(),
                turn_count=0,
                mock_mode=mock_mode,
                agent_name=agent_profile,
            ),
        )
        await session_manager.save_session(session)

        # Setup event bus and state
        bus = MessageBus()
        telemetry_task = asyncio.create_task(
            self.stream_session_telemetry(session_id, writer, bus)
        )

        self.active_sessions[session_id] = ActiveSessionState(
            session=session,
            bus=bus,
            telemetry_task=telemetry_task,
            workspace_path=Path(workspace_path),
            session_manager=session_manager,
        )
        bound_sessions.append(session_id)

        result = {"session_id": str(session_id), "status": "active"}
        writer.write(JsonRpcCodec.encode_response(result, msg_id))
        await writer.drain()

    async def _handle_session_send_prompt(
        self, params: Dict[str, Any], msg_id: Any, writer: asyncio.StreamWriter
    ) -> None:
        """Queues a user prompt to be executed asynchronously by the agent executor."""
        session_id_str = params.get("session_id")
        text = params.get("text")

        if not session_id_str or not text:
            raise JsonRpcError(-32602, "Missing session_id or text parameter")

        try:
            session_id = uuid.UUID(session_id_str)
        except ValueError as e:
            raise JsonRpcError(
                -32602, f"Malformed UUID format for session_id: {session_id_str}"
            ) from e

        state = self.active_sessions.get(session_id)
        if not state:
            raise JsonRpcError(-32602, f"Active session '{session_id}' not found")

        # Cancel any previous running executor task to prevent race conditions
        if state.executor_task and not state.executor_task.done():
            state.executor_task.cancel()

        # Select and inject client dynamically based on session mode
        if state.session.metadata.mock_mode:
            client = MockGenAIClient()
        else:
            try:
                settings = self.settings_manager.settings
                active_model_name = settings.model
                model_def = self.model_registry.get_model(active_model_name)
            except Exception as e:
                raise JsonRpcError(
                    -32603, f"Configuration or registry resolution failed: {e}"
                )

            provider_config = settings.providers.get(model_def.provider)
            if not provider_config:
                raise JsonRpcError(
                    -32603,
                    f"Provider '{model_def.provider}' is unconfigured in the engine settings.",
                )

            api_key = await provider_config.resolve_api_key()
            if not api_key:
                raise JsonRpcError(
                    -32602,
                    f"Credentials could not be resolved for provider '{model_def.provider}' of model '{active_model_name}'.",
                )

            client = LiveGenAIClient(
                api_key=api_key,
                model_name=model_def.name,
                base_url=provider_config.base_url,
                capabilities=model_def.capabilities,
                limits=model_def.limits,
                options=model_def.options,
                provider_options=provider_config.options,
            )

        from engine.executor import LocalAgentExecutor
        from engine.context import DefaultAgentContextStrategy
        from engine.prompts.loader import PromptTemplateLoader
        from engine.types import (
            ExecutorAgentConfig,
            ExecutionContext,
            ChatMessage,
            MessageRole,
            TextPart,
        )
        from engine.tools import (
            CompleteTaskTool,
            ReadFileTool,
            WriteFileTool,
            ListDirectoryTool,
            GlobTool,
            GrepSearchTool,
            ReplaceTool,
        )

        package_root = Path(__file__).parent.resolve()
        loader = PromptTemplateLoader(
            templates_dir=package_root / "prompts" / "templates"
        )

        settings = self.settings_manager.settings
        strategy_cfg = settings.strategy_config.get("default", {})
        strategy = DefaultAgentContextStrategy(loader=loader, config=strategy_cfg)

        config = ExecutorAgentConfig(
            name="coder",
            max_turns=10,
            max_time_seconds=60,
            plan_mode=False,
            requires_approval=settings.requires_approval,
            compression_threshold=strategy_cfg.get("compression_threshold", 0.60),
            query=text,
        )

        from engine.memory import HierarchicalContextManager

        memory_manager = HierarchicalContextManager(
            workspace_path=state.workspace_path,
            context_filenames=settings.context_filenames,
            global_context_dir=Path(settings.global_context_dir).expanduser(),
            max_depth=5,
        )

        from engine.agents import AgentRegistry
        from engine.subagents import AgentTool

        system_agents_dir = Path(settings.system_agents_dir)
        if not system_agents_dir.is_absolute():
            package_root = Path(__file__).parent.resolve()
            system_agents_dir = package_root / system_agents_dir

        agent_registry = AgentRegistry(
            search_paths=[state.workspace_path],
            agent_extensions=settings.agent_extensions,
            system_agents_dir=system_agents_dir,
        )
        await agent_registry.discover_agents()

        executor = LocalAgentExecutor(
            definition=config,
            context_strategy=strategy,
            memory_manager=memory_manager,
            agent_registry=agent_registry,
        )
        executor.registry.register_tool(CompleteTaskTool())
        executor.registry.register_tool(ReadFileTool())
        executor.registry.register_tool(WriteFileTool())
        executor.registry.register_tool(ListDirectoryTool())
        executor.registry.register_tool(GlobTool())
        executor.registry.register_tool(GrepSearchTool())
        executor.registry.register_tool(ReplaceTool())

        agent_tool = AgentTool(
            agent_registry=agent_registry,
            context_strategy=strategy,
            tool_registry=executor.registry,
            memory_manager=memory_manager,
        )
        executor.registry.register_tool(agent_tool)

        context = ExecutionContext(
            workspace_path=state.workspace_path,
            message_bus=state.bus,
            remaining_depth=3,
            session=state.session,
            session_manager=state.session_manager,
            client=client,
        )

        async def run_executor_safely() -> None:
            try:
                # Append user prompt to session history
                user_msg = ChatMessage(
                    role=MessageRole.USER.value, parts=[TextPart(text=text)]
                )
                await state.session.append_message(user_msg)

                inputs = {"target_dir": str(state.workspace_path)}
                await executor.run(context, inputs)
            except Exception as e:
                logger.error(
                    f"Executor exception in session {session_id}: {e}", exc_info=True
                )
                try:
                    # Stream the error details directly as a telemetry message
                    err_msg = f"\n[CodeSavant Error] Executor crashed: {e}\n"
                    notification = JsonRpcCodec.encode_notification(
                        method="telemetry/message",
                        params={"session_id": str(session_id), "text": err_msg},
                    )
                    writer.write(notification)

                    # Send idle status to cleanly reset the UI's thinking indicator
                    status_notif = JsonRpcCodec.encode_notification(
                        method="telemetry/status",
                        params={"session_id": str(session_id), "status": "idle"},
                    )
                    writer.write(status_notif)
                    await writer.drain()
                except Exception as write_err:
                    logger.error(
                        f"Failed to transmit executor error notification: {write_err}",
                        exc_info=True,
                    )

        state.executor = executor
        state.context = context
        state.executor_task = asyncio.create_task(run_executor_safely())

        result = {"status": "queued"}
        writer.write(JsonRpcCodec.encode_response(result, msg_id))
        await writer.drain()

    async def _handle_session_respond_confirmation(
        self, params: Dict[str, Any], msg_id: Any, writer: asyncio.StreamWriter
    ) -> None:
        """Processes and forwards a user's tool confirmation decision back to the execution bus."""
        session_id_str = params.get("session_id")
        call_id_str = params.get("id")
        confirmed = params.get("confirmed")

        if not session_id_str or not call_id_str or confirmed is None:
            raise JsonRpcError(
                -32602, "Missing required parameters: session_id, id, confirmed"
            )

        try:
            session_id = uuid.UUID(session_id_str)
        except ValueError as e:
            raise JsonRpcError(
                -32602, f"Malformed UUID format for session_id: {session_id_str}"
            ) from e

        try:
            call_id = uuid.UUID(call_id_str)
        except ValueError as e:
            raise JsonRpcError(
                -32602, f"Malformed UUID format for call_id: {call_id_str}"
            ) from e

        state = self.active_sessions.get(session_id)
        if not state:
            raise JsonRpcError(-32602, f"Active session '{session_id}' not found")

        from engine.types import (
            EventEnvelope,
            EventType,
            ToolConfirmationResponsePayload,
        )

        await state.bus.publish(
            EventEnvelope(
                event_type=EventType.TOOL_CONFIRMATION_RESPONSE,
                payload=ToolConfirmationResponsePayload(confirmed=confirmed),
                sender="user",
                correlation_id=str(call_id),
            )
        )

        result = {"status": "responded"}
        writer.write(JsonRpcCodec.encode_response(result, msg_id))
        await writer.drain()

    async def _handle_session_cancel(
        self, params: Dict[str, Any], msg_id: Any, writer: asyncio.StreamWriter
    ) -> None:
        """Gracefully aborts any actively running execution loop for the session."""
        session_id_str = params.get("session_id")
        if not session_id_str:
            raise JsonRpcError(-32602, "Missing required parameter: session_id")

        try:
            session_id = uuid.UUID(session_id_str)
        except ValueError as e:
            raise JsonRpcError(
                -32602, f"Malformed UUID format for session_id: {session_id_str}"
            ) from e

        state = self.active_sessions.get(session_id)
        if not state:
            raise JsonRpcError(-32602, f"Active session '{session_id}' not found")

        # Idempotent cancellation: if active task is running, request cancel and await it
        if state.executor_task and not state.executor_task.done():
            state.executor_task.cancel()
            try:
                # Block/await its complete termination so that we prevent parallel execution races
                await state.executor_task
            except asyncio.CancelledError:
                pass

            # Write a collapsible warning block to indicate the termination cleanly
            notification = JsonRpcCodec.encode_notification(
                method="telemetry/collapsed_block",
                params={
                    "session_id": session_id,
                    "id": uuid.uuid4(),
                    "type": "warning",
                    "title": "🛑 Execution Terminated by User",
                    "full_content": "The active execution loop was aborted and all outstanding operations cancelled.",
                },
            )
            writer.write(notification)

            # Set status to idle to cleanly reset UI
            status_notif = JsonRpcCodec.encode_notification(
                method="telemetry/status",
                params={"session_id": session_id, "status": "idle"},
            )
            writer.write(status_notif)
            await writer.drain()

        result = {"status": "cancelled"}
        writer.write(JsonRpcCodec.encode_response(result, msg_id))
        await writer.drain()

    async def _handle_session_inject_steering(
        self, params: Dict[str, Any], msg_id: Any, writer: asyncio.StreamWriter
    ) -> None:
        """Processes and forwards manual human guidance steering inline into the active agent loop."""
        session_id_str = params.get("session_id")
        text = params.get("text")

        if not session_id_str or not text:
            raise JsonRpcError(-32602, "Missing required parameters: session_id, text")

        try:
            session_id = uuid.UUID(session_id_str)
        except ValueError as e:
            raise JsonRpcError(
                -32602, f"Malformed UUID format for session_id: {session_id_str}"
            ) from e

        state = self.active_sessions.get(session_id)
        if not state:
            raise JsonRpcError(-32602, f"Active session '{session_id}' not found")

        if not state.executor or not state.context:
            raise JsonRpcError(
                -32602,
                f"Active session '{session_id}' has no running executor to steer.",
            )

        # Inject human steering directives inline
        await state.executor.inject_steering(text, state.context)

        result = {"status": "steered"}
        writer.write(JsonRpcCodec.encode_response(result, msg_id))
        await writer.drain()

    async def _handle_session_close(
        self,
        params: Dict[str, Any],
        msg_id: Any,
        writer: asyncio.StreamWriter,
        bound_sessions: list[uuid.UUID],
    ) -> None:
        """Gracefully closes and cleans up a single active session."""
        session_id_str = params.get("session_id")
        if not session_id_str:
            raise JsonRpcError(-32602, "Missing session_id parameter")

        try:
            session_id = uuid.UUID(session_id_str)
        except ValueError as e:
            raise JsonRpcError(
                -32602, f"Malformed UUID format for session_id: {session_id_str}"
            ) from e

        if session_id in self.active_sessions:
            await self._cleanup_session(session_id)
            if session_id in bound_sessions:
                bound_sessions.remove(session_id)

        result = {"status": "closed"}
        writer.write(JsonRpcCodec.encode_response(result, msg_id))
        await writer.drain()

    async def _handle_session_list(
        self,
        params: Dict[str, Any],
        msg_id: Any,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Asynchronously queries and lists saved sessions for a given workspace path."""
        workspace_path = params.get("workspace_path")
        if not workspace_path:
            raise JsonRpcError(-32602, "Missing workspace_path parameter")

        # Retrieve session storage directory from centralized settings
        settings = self.settings_manager.settings
        configured_path = Path(settings.session_storage_dir)

        # Resolve relative configurations relative to active workspace_path
        if not configured_path.is_absolute():
            storage_dir = Path(workspace_path) / configured_path
        else:
            storage_dir = configured_path

        session_manager = self.session_manager or SessionManager(
            storage_dir=storage_dir,
            session_suffix=SESSION_FILE_SUFFIX,
            meta_suffix=SESSION_META_SUFFIX,
            checkpoint_separator=CHECKPOINT_SEPARATOR,
            scratch_dir_name=SCRATCH_DIR_NAME,
            tool_log_prefix=TOOL_LOG_FILE_PREFIX,
            tool_log_suffix=TOOL_LOG_FILE_SUFFIX,
        )

        sessions = await session_manager.list_sessions()
        result = {"sessions": [s.model_dump(mode="json") for s in sessions]}
        writer.write(JsonRpcCodec.encode_response(result, msg_id))
        await writer.drain()

    async def _handle_session_load(
        self,
        params: Dict[str, Any],
        msg_id: Any,
        writer: asyncio.StreamWriter,
        bound_sessions: list[uuid.UUID],
    ) -> None:
        """Asynchronously loads an existing session and mounts it to active connections."""
        workspace_path = params.get("workspace_path")
        session_id_str = params.get("session_id")

        if not workspace_path:
            raise JsonRpcError(-32602, "Missing workspace_path parameter")
        if not session_id_str:
            raise JsonRpcError(-32602, "Missing session_id parameter")

        try:
            session_id = uuid.UUID(session_id_str)
        except ValueError as e:
            raise JsonRpcError(
                -32602, f"Malformed UUID format for session_id: {session_id_str}"
            ) from e

        # Retrieve session storage directory from centralized settings
        settings = self.settings_manager.settings
        configured_path = Path(settings.session_storage_dir)

        # Resolve relative configurations relative to active workspace_path
        if not configured_path.is_absolute():
            storage_dir = Path(workspace_path) / configured_path
        else:
            storage_dir = configured_path

        session_manager = self.session_manager or SessionManager(
            storage_dir=storage_dir,
            session_suffix=SESSION_FILE_SUFFIX,
            meta_suffix=SESSION_META_SUFFIX,
            checkpoint_separator=CHECKPOINT_SEPARATOR,
            scratch_dir_name=SCRATCH_DIR_NAME,
            tool_log_prefix=TOOL_LOG_FILE_PREFIX,
            tool_log_suffix=TOOL_LOG_FILE_SUFFIX,
        )

        try:
            session = await session_manager.load_session(session_id)
        except FileNotFoundError as e:
            raise JsonRpcError(
                -32602, f"Session not found on disk: {session_id_str}"
            ) from e
        except Exception as e:
            raise JsonRpcError(-32603, f"Internal error loading session: {e}") from e

        # Setup event bus and state
        bus = MessageBus()
        telemetry_task = asyncio.create_task(
            self.stream_session_telemetry(session_id, writer, bus)
        )

        self.active_sessions[session_id] = ActiveSessionState(
            session=session,
            bus=bus,
            telemetry_task=telemetry_task,
            workspace_path=Path(workspace_path),
            session_manager=session_manager,
        )
        bound_sessions.append(session_id)

        result = {
            "session_id": str(session_id),
            "status": "active",
            "metadata": session.metadata.model_dump(mode="json"),
            "chat_history": [
                msg.model_dump(mode="json") for msg in session.chat_history
            ],
        }
        writer.write(JsonRpcCodec.encode_response(result, msg_id))
        await writer.drain()

    async def stream_session_telemetry(
        self, session_id: uuid.UUID, writer: asyncio.StreamWriter, bus: MessageBus
    ) -> None:
        """Long-running async task that translates internal bus EventEnvelopes to outgoing JSON-RPC notification frames."""
        queue: asyncio.Queue[EventEnvelope[Any]] = asyncio.Queue()
        active_block_id: Optional[uuid.UUID] = None
        active_block_chunks: list[str] = []
        active_block_start_time: float = 0.0
        active_block_title: str = FALLBACK_THOUGHT_TITLE

        async def listener(envelope: EventEnvelope[Any]) -> None:
            await queue.put(envelope)

        bus.subscribe(EventType.TELEMETRY_THOUGHT.value, listener)
        bus.subscribe(EventType.TELEMETRY_CONTENT.value, listener)
        bus.subscribe(EventType.TELEMETRY_ACTIVITY.value, listener)
        bus.subscribe(EventType.TOOL_CONFIRMATION_REQUEST.value, listener)

        try:
            while True:
                envelope = await queue.get()
                event_type = envelope.event_type
                payload = envelope.payload

                if event_type == EventType.TELEMETRY_THOUGHT:
                    text = payload.text
                    block_id = payload.block_id
                    prompt_id_val = payload.prompt_id

                    if active_block_id != block_id:
                        active_block_id = block_id
                        active_block_chunks = []
                        active_block_start_time = envelope.timestamp
                        active_block_title = FALLBACK_THOUGHT_TITLE

                    active_block_chunks.append(text)
                    current_accumulation = "".join(active_block_chunks)

                    if payload.title:
                        active_block_title = payload.title

                    raw_subject = active_block_title

                    subject_title = (
                        (
                            (raw_subject[:50] + "...")
                            if len(raw_subject) > 50
                            else raw_subject
                        )
                        if raw_subject
                        else FALLBACK_THOUGHT_TITLE
                    )

                    elapsed_seconds = envelope.timestamp - active_block_start_time
                    estimated_tokens = int(estimate_text_tokens(current_accumulation))
                    dynamic_title = f"{subject_title} ({elapsed_seconds:.1f}s, {estimated_tokens} tokens)"

                    notification = JsonRpcCodec.encode_notification(
                        method="telemetry/collapsed_block",
                        params={
                            "session_id": session_id,
                            "id": block_id,
                            "prompt_id": prompt_id_val,
                            "type": "thought",
                            "title": dynamic_title,
                            "full_content": current_accumulation,
                        },
                    )
                    writer.write(notification)
                    await writer.drain()

                elif event_type == EventType.TELEMETRY_CONTENT:
                    text = payload.text
                    notification = JsonRpcCodec.encode_notification(
                        method="telemetry/message",
                        params={"session_id": str(session_id), "text": text},
                    )
                    writer.write(notification)
                    await writer.drain()

                elif event_type == EventType.TOOL_CONFIRMATION_REQUEST:
                    block_id = payload.block_id
                    prompt_id_val = payload.prompt_id
                    tc = payload.tool_call
                    tc_name = tc.name
                    tc_args = tc.args

                    args_str = json.dumps(tc_args, indent=2) if tc_args else ""
                    notification = JsonRpcCodec.encode_notification(
                        method="telemetry/collapsed_block",
                        params={
                            "session_id": session_id,
                            "id": block_id,
                            "prompt_id": prompt_id_val,
                            "type": "confirmation",
                            "title": f"⚠️ Authorization Required: Invoke tool {tc_name}",
                            "full_content": f"Arguments:\n{args_str}\n\nPress 'a' to Approve or 'd' to Decline.",
                        },
                    )
                    writer.write(notification)
                    await writer.drain()

                elif event_type == EventType.TELEMETRY_ACTIVITY:
                    activity_type = payload.activity_type
                    prompt_id_val = payload.prompt_id
                    act_block_id = payload.block_id

                    if activity_type == TelemetryActivityType.TURN_START:
                        notification = JsonRpcCodec.encode_notification(
                            method="telemetry/status",
                            params={
                                "session_id": session_id,
                                "status": "thinking",
                                "prompt_id": prompt_id_val,
                            },
                        )
                        writer.write(notification)
                        await writer.drain()

                    elif activity_type == TelemetryActivityType.RECOVERY:
                        notification = JsonRpcCodec.encode_notification(
                            method="telemetry/collapsed_block",
                            params={
                                "session_id": session_id,
                                "id": act_block_id,
                                "prompt_id": prompt_id_val,
                                "type": "warning",
                                "title": "⚠️ Execution Limit Exceeded (Recovery)",
                                "full_content": payload.msg or "",
                            },
                        )
                        writer.write(notification)
                        await writer.drain()

                    elif activity_type == TelemetryActivityType.STEERING_INJECTED:
                        notification = JsonRpcCodec.encode_notification(
                            method="telemetry/collapsed_block",
                            params={
                                "session_id": session_id,
                                "id": act_block_id,
                                "prompt_id": prompt_id_val,
                                "type": "steering",
                                "title": "👤 Steering Directive Injected",
                                "full_content": payload.msg or "",
                            },
                        )
                        writer.write(notification)
                        await writer.drain()

                    elif activity_type in (
                        TelemetryActivityType.STOP,
                        TelemetryActivityType.END,
                    ):
                        response_text = getattr(payload, "response", None)
                        if activity_type == TelemetryActivityType.END and response_text:
                            msg_notif = JsonRpcCodec.encode_notification(
                                method="telemetry/message",
                                params={
                                    "session_id": session_id,
                                    "text": str(response_text),
                                },
                            )
                            writer.write(msg_notif)
                            await writer.drain()

                        notification = JsonRpcCodec.encode_notification(
                            method="telemetry/status",
                            params={"session_id": session_id, "status": "idle"},
                        )
                        writer.write(notification)
                        await writer.drain()

                    elif activity_type == TelemetryActivityType.TOOL_CALL_START:
                        name = payload.name or ""
                        args = payload.args or {}
                        args_str = json.dumps(args, indent=2) if args else ""
                        notification = JsonRpcCodec.encode_notification(
                            method="telemetry/collapsed_block",
                            params={
                                "session_id": session_id,
                                "id": act_block_id,
                                "type": "tool",
                                "title": f"Invoking tool: {name}",
                                "full_content": f"Arguments:\n{args_str}",
                            },
                        )
                        writer.write(notification)
                        await writer.drain()

                    elif activity_type == TelemetryActivityType.TOOL_CALL_END:
                        name = payload.name or ""
                        resp = payload.response
                        resp_str = (
                            json.dumps(resp, indent=2) if resp is not None else ""
                        )
                        notification = JsonRpcCodec.encode_notification(
                            method="telemetry/collapsed_block",
                            params={
                                "session_id": session_id,
                                "id": act_block_id,
                                "type": "tool",
                                "title": f"Tool completed: {name}",
                                "full_content": f"Response:\n{resp_str}",
                            },
                        )
                        writer.write(notification)
                        await writer.drain()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in stream_session_telemetry: {e}", exc_info=True)
        finally:
            bus.unsubscribe(EventType.TELEMETRY_THOUGHT.value, listener)
            bus.unsubscribe(EventType.TELEMETRY_CONTENT.value, listener)
            bus.unsubscribe(EventType.TELEMETRY_ACTIVITY.value, listener)
            bus.unsubscribe(EventType.TOOL_CONFIRMATION_REQUEST.value, listener)
