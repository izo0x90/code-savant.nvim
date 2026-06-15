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
from engine.constants import (
    CHECKPOINT_SEPARATOR,
    SCRATCH_DIR_NAME,
    SESSION_FILE_SUFFIX,
    SESSION_META_SUFFIX,
    TOOL_LOG_FILE_PREFIX,
    TOOL_LOG_FILE_SUFFIX,
)
from engine.mock_executor import MockAgentExecutor
from engine.sessions import AgentSession, SessionManager
from engine.types import ActivityType, EventEnvelope, EventType, SessionMetadataPayload

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
            raise TypeError(f"result must be a dictionary, got {type(result).__name__}")
        if not isinstance(msg_id, (int, str)):
            raise TypeError(f"msg_id must be int or str, got {type(msg_id).__name__}")

        payload = {
            "jsonrpc": JsonRpcCodec.JSONRPC_VERSION,
            "result": result,
            "id": msg_id
        }
        return (json.dumps(payload) + JsonRpcCodec.DELIMITER).encode("utf-8")

    @staticmethod
    def encode_error(code: int, message: str, msg_id: int | str | None = None, data: Any = None) -> bytes:
        """Formats an error response into a newline-delimited JSON-RPC 2.0 payload."""
        if not isinstance(code, int):
            raise TypeError(f"code must be an integer, got {type(code).__name__}")
        if not isinstance(message, str):
            raise TypeError(f"message must be a string, got {type(message).__name__}")
        if msg_id is not None and not isinstance(msg_id, (int, str)):
            raise TypeError(f"msg_id must be int, str, or None, got {type(msg_id).__name__}")

        payload: Dict[str, Any] = {
            "jsonrpc": JsonRpcCodec.JSONRPC_VERSION,
            "error": {
                "code": code,
                "message": message
            },
            "id": msg_id
        }
        if data is not None:
            payload["error"]["data"] = data
        return (json.dumps(payload) + JsonRpcCodec.DELIMITER).encode("utf-8")

    @staticmethod
    def encode_notification(method: str, params: Dict[str, Any]) -> bytes:
        """Formats a notification into a newline-delimited JSON-RPC 2.0 payload."""
        if not isinstance(method, str):
            raise TypeError(f"method must be a string, got {type(method).__name__}")
        if not method.strip():
            raise ValueError("method cannot be empty or whitespace-only")
        if not isinstance(params, dict):
            raise TypeError(f"params must be a dictionary, got {type(params).__name__}")

        payload = {
            "jsonrpc": JsonRpcCodec.JSONRPC_VERSION,
            "method": method,
            "params": params
        }
        return (json.dumps(payload) + JsonRpcCodec.DELIMITER).encode("utf-8")


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
    executor_task: asyncio.Task[None] | None = None


class UdsServer:
    """
    Production-first Unix Domain Socket daemon.
    Manages client socket streams, session registration, and event telemetry routing.
    """
    socket_path: Path
    session_manager: Optional[SessionManager]
    active_sessions: Dict[str, ActiveSessionState]

    def __init__(self, socket_path: Path | str, session_manager: Optional[SessionManager] = None) -> None:
        self.socket_path = Path(socket_path) if isinstance(socket_path, str) else socket_path
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
                logger.error(f"Failed to clean up pre-existing socket {socket_str}: {e}")
                raise

        self._server = await asyncio.start_unix_server(
            self.handle_connection,
            path=socket_str
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

    async def _cleanup_session(self, session_id: str) -> None:
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
                return_exceptions=True
            )
        except Exception:
            pass

    async def handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Manages individual client stream life-cycles and newline-delimited frames."""
        bound_sessions: list[str] = []
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break

                try:
                    frame = json.loads(line.decode("utf-8").strip())
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to decode incoming JSON line: {line!r}", exc_info=True)
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

    async def dispatch_request(self, frame: Dict[str, Any], writer: asyncio.StreamWriter, bound_sessions: list[str]) -> None:
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
            elif method == "session/send_prompt":
                await self._handle_session_send_prompt(params, msg_id, writer)
            elif method == "session/close":
                await self._handle_session_close(params, msg_id, writer, bound_sessions)
            else:
                writer.write(JsonRpcCodec.encode_error(-32601, f"Method '{method}' not found", msg_id))
                await writer.drain()

        except JsonRpcError as e:
            writer.write(JsonRpcCodec.encode_error(e.code, e.message, msg_id, e.data))
            await writer.drain()
        except Exception as e:
            logger.error(f"Error handling method {method}: {e}", exc_info=True)
            writer.write(JsonRpcCodec.encode_error(-32603, f"Internal error: {e}", msg_id))
            await writer.drain()

    async def _handle_session_start(
        self,
        params: Dict[str, Any],
        msg_id: Any,
        writer: asyncio.StreamWriter,
        bound_sessions: list[str]
    ) -> None:
        """Handles initializing a new agent session and its corresponding telemetry stream."""
        workspace_path = params.get("workspace_path")
        agent_profile = params.get("agent_profile", "coder")

        if not workspace_path:
            raise JsonRpcError(-32602, "Missing required workspace_path parameter")

        # Establish session storage
        storage_dir = Path(workspace_path) / ".replica_sessions"
        session_manager = self.session_manager or SessionManager(
            storage_dir=storage_dir,
            session_suffix=SESSION_FILE_SUFFIX,
            meta_suffix=SESSION_META_SUFFIX,
            checkpoint_separator=CHECKPOINT_SEPARATOR,
            scratch_dir_name=SCRATCH_DIR_NAME,
            tool_log_prefix=TOOL_LOG_FILE_PREFIX,
            tool_log_suffix=TOOL_LOG_FILE_SUFFIX
        )
        await session_manager.ensure_storage_dir()

        # Instantiate new session
        session_id = f"{agent_profile}_{uuid.uuid7()}"
        session = AgentSession(
            session_id=session_id,
            chat_history=[],
            metadata=SessionMetadataPayload(
                name="uds_session",
                query="",
                created_at=datetime.datetime.now().isoformat(),
                last_updated=datetime.datetime.now().isoformat(),
                turn_count=0
            )
        )
        await session_manager.save_session(session)

        # Setup event bus and state
        bus = MessageBus()
        telemetry_task = asyncio.create_task(self.stream_session_telemetry(session_id, writer, bus))

        self.active_sessions[session_id] = ActiveSessionState(
            session=session,
            bus=bus,
            telemetry_task=telemetry_task
        )
        bound_sessions.append(session_id)

        result = {
            "session_id": session_id,
            "status": "active"
        }
        writer.write(JsonRpcCodec.encode_response(result, msg_id))
        await writer.drain()

    async def _handle_session_send_prompt(
        self,
        params: Dict[str, Any],
        msg_id: Any,
        writer: asyncio.StreamWriter
    ) -> None:
        """Queues a user prompt to be executed asynchronously by the agent executor."""
        session_id = params.get("session_id")
        text = params.get("text")

        if not session_id or not text:
            raise JsonRpcError(-32602, "Missing session_id or text parameter")

        state = self.active_sessions.get(session_id)
        if not state:
            raise JsonRpcError(-32602, f"Active session '{session_id}' not found")

        # Cancel any previous running executor task to prevent race conditions
        if state.executor_task and not state.executor_task.done():
            state.executor_task.cancel()

        # Spawn new mock agent executor task
        executor = MockAgentExecutor(state.bus)

        async def run_executor_safely() -> None:
            try:
                await executor.run(state.session, text)
            except Exception as e:
                logger.error(f"Executor exception in session {session_id}: {e}", exc_info=True)
                try:
                    # Stream the error details directly as a telemetry message
                    err_msg = f"\n[CodeSavant Error] Executor crashed: {e}\n"
                    notification = JsonRpcCodec.encode_notification(
                        method="telemetry/message",
                        params={
                            "session_id": session_id,
                            "text": err_msg
                        }
                    )
                    writer.write(notification)

                    # Send idle status to cleanly reset the UI's thinking indicator
                    status_notif = JsonRpcCodec.encode_notification(
                        method="telemetry/status",
                        params={
                            "session_id": session_id,
                            "status": "idle"
                        }
                    )
                    writer.write(status_notif)
                    await writer.drain()
                except Exception as write_err:
                    logger.error(f"Failed to transmit executor error notification: {write_err}", exc_info=True)

        state.executor_task = asyncio.create_task(run_executor_safely())

        result = {
            "status": "queued"
        }
        writer.write(JsonRpcCodec.encode_response(result, msg_id))
        await writer.drain()

    async def _handle_session_close(
        self,
        params: Dict[str, Any],
        msg_id: Any,
        writer: asyncio.StreamWriter,
        bound_sessions: list[str]
    ) -> None:
        """Gracefully closes and cleans up a single active session."""
        session_id = params.get("session_id")
        if not session_id:
            raise JsonRpcError(-32602, "Missing session_id parameter")

        if session_id in self.active_sessions:
            await self._cleanup_session(session_id)
            if session_id in bound_sessions:
                bound_sessions.remove(session_id)

        result = {
            "status": "closed"
        }
        writer.write(JsonRpcCodec.encode_response(result, msg_id))
        await writer.drain()

    async def stream_session_telemetry(self, session_id: str, writer: asyncio.StreamWriter, bus: MessageBus) -> None:
        """Long-running async task that translates internal bus EventEnvelopes to outgoing JSON-RPC notification frames."""
        queue: asyncio.Queue[EventEnvelope[Any]] = asyncio.Queue()

        async def listener(envelope: EventEnvelope[Any]) -> None:
            await queue.put(envelope)

        bus.subscribe(EventType.TELEMETRY_LOG, listener)
        bus.subscribe(EventType.ACTIVITY, listener)

        try:
            while True:
                envelope = await queue.get()
                event_type = envelope.event_type

                if event_type == EventType.TELEMETRY_LOG:
                    payload = envelope.payload if isinstance(envelope.payload, dict) else (envelope.payload.to_dict() if hasattr(envelope.payload, "to_dict") else envelope.payload)
                    if not isinstance(payload, dict):
                        payload = {}
                    p_type = payload.get("type", "thought")

                    if p_type in ("thought", "diff", "tool"):
                        notification = JsonRpcCodec.encode_notification(
                            method="telemetry/collapsed_block",
                            params={
                                "session_id": session_id,
                                "id": payload.get("id", ""),
                                "type": p_type,
                                "title": payload.get("title", ""),
                                "full_content": payload.get("full_content", "")
                            }
                        )
                    else:
                        notification = JsonRpcCodec.encode_notification(
                            method="telemetry/message",
                            params={
                                "session_id": session_id,
                                "text": payload.get("full_content", "")
                            }
                        )
                    writer.write(notification)
                    await writer.drain()

                elif event_type == EventType.ACTIVITY:
                    payload = envelope.payload if isinstance(envelope.payload, dict) else (envelope.payload.to_dict() if hasattr(envelope.payload, "to_dict") else envelope.payload)
                    if not isinstance(payload, dict):
                        payload = {}
                    act_type = payload.get("type")
                    status_str = "idle"

                    if act_type == ActivityType.MODEL_START:
                        status_str = "thinking"
                    elif act_type == ActivityType.MODEL_END:
                        status_str = "idle"

                    notification = JsonRpcCodec.encode_notification(
                        method="telemetry/status",
                        params={
                            "session_id": session_id,
                            "status": status_str
                        }
                    )
                    writer.write(notification)
                    await writer.drain()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in stream_session_telemetry: {e}", exc_info=True)
        finally:
            bus.unsubscribe(EventType.TELEMETRY_LOG, listener)
            bus.unsubscribe(EventType.ACTIVITY, listener)
