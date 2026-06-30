import datetime
import asyncio
import uuid
import json
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional, Union
from pydantic import BaseModel, Field, ConfigDict, TypeAdapter

from engine.constants import (
    DEFAULT_SESSION_NAME,
    MAX_AUTO_NAME_LENGTH,
    DELTA_TYPE_SET,
    DELTA_TYPE_REWIND,
    KEY_SET_DELTA,
    KEY_REWIND_DELTA,
    KEY_DELTA_INDEX,
    KEY_DELTA_MESSAGE,
    KEY_DELTA_METADATA,
    KEY_DELTA_COUNT,
    KEY_DELTA_TRUNCATE_TO,
)
from engine.types import ChatMessage, AgentSessionProtocol, SessionMetadataPayload, SessionMetaSidecar





class SetDelta(BaseModel):
    model_config = ConfigDict(frozen=True, slots=True)
    index: int
    message: ChatMessage


class RewindDelta(BaseModel):
    model_config = ConfigDict(frozen=True, slots=True)
    count: Optional[int] = None
    truncate_to: Optional[int] = None


class SessionIdRecord(BaseModel):
    model_config = ConfigDict(frozen=True, slots=True)
    session_id: uuid.UUID


SessionRecord = Union[ChatMessage, SessionMetadataPayload, SetDelta, RewindDelta, SessionIdRecord]
session_record_adapter = TypeAdapter(SessionRecord)


class AgentSession(AgentSessionProtocol):
    """
    Pure Domain Entity.
    Contains absolutely zero knowledge of filesystem locations, paths, or persistence.
    """
    __slots__ = ("_session_id", "_history", "_metadata")

    def __init__(
        self,
        session_id: uuid.UUID,
        chat_history: List[ChatMessage],
        metadata: SessionMetadataPayload
    ):
        self._session_id = session_id
        self._metadata: SessionMetadataPayload = metadata
        self._history: List[ChatMessage] = list(chat_history)

    @property
    def session_id(self) -> uuid.UUID:
        return self._session_id

    @property
    def chat_history(self) -> List[ChatMessage]:
        return self._history

    @property
    def metadata(self) -> SessionMetadataPayload:
        return self._metadata

    def __repr__(self) -> str:
        return f"AgentSession(id={self.session_id}, turns={len(self._history)}, agent_name={self._metadata.agent_name})"

    def __str__(self) -> str:
        return f"AgentSession {self.session_id} [{self._metadata.name or 'Untitled'}] (Turns: {len(self._history)})"

    async def append_message(self, message: ChatMessage) -> None:
        """Appends a pre-validated strict ChatMessage turn element."""
        self._history.append(message)

    async def set_history(self, history: List[ChatMessage]) -> None:
        """Replaces the entire history sequence with strict ChatMessage turn elements."""
        self._history = list(history)

    # ==========================================================================
    # Backwards-Compatibility Boundary Layer for Transitional Compilation
    # ==========================================================================

    def __getitem__(self, key: str) -> Any:
        if key == "session_id":
            return self.session_id
        elif key == "chat_history":
            return [m.model_dump() for m in self._history]
        elif key == "metadata":
            return self.metadata.model_dump()
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def to_dict(self) -> Dict[str, Any]:
        """Converts stateful session parameters to standard dict."""
        return {
            "session_id": self.session_id,
            "metadata": self.metadata.model_dump(),
            "chat_history": [msg.model_dump() for msg in self._history]
        }


def _sync_save_session_files(filepath: Path, payload_str: str, meta_filepath: Path, meta_str: str) -> None:
    """Cohesively creates directories and writes both session files in a single thread-offload invocation."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f1:
        f1.write(payload_str)
    with open(meta_filepath, "w", encoding="utf-8") as f2:
        f2.write(meta_str)


def _sync_write_truncation_log(log_filepath: Path, content: str) -> None:
    """Cohesively creates scratch directory and writes log content synchronously in a thread-offload invocation."""
    log_filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(log_filepath, "w", encoding="utf-8") as f:
        f.write(content)


def _sync_write_checkpoint(filepath: Path, payload_str: str) -> None:
    """Cohesively creates parent directories and writes checkpoint content synchronously."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(payload_str)


class SessionManager:
    """
    Decoupled File-System Persistence Repository.
    Exclusively manages IO context scopes and filesystem structure using Path objects.
    All suffixes, prefixes, and separating characters are strictly injected.
    """
    def __init__(
        self,
        storage_dir: Path,
        session_suffix: str,
        meta_suffix: str,
        checkpoint_separator: str,
        scratch_dir_name: str,
        tool_log_prefix: str,
        tool_log_suffix: str
    ):
        self.storage_dir = Path(storage_dir)
        self.session_suffix = session_suffix
        self.meta_suffix = meta_suffix
        self.checkpoint_separator = checkpoint_separator
        self.scratch_dir_name = scratch_dir_name
        self.tool_log_prefix = tool_log_prefix
        self.tool_log_suffix = tool_log_suffix

    async def ensure_storage_dir(self) -> None:
        await asyncio.to_thread(self.storage_dir.mkdir, parents=True, exist_ok=True)

    def _resolve_paths(self, session_id: uuid.UUID, parent_session_id: Optional[uuid.UUID] = None) -> Tuple[Path, Path]:
        """
        Pure deterministic path resolver.
        - Metadata Sidecar: ALWAYS flat in the root storage directory (.code_savant/sessions/).
        - Session History File: ALWAYS inside the master session folder (parent_session_id/ or session_id/).
        """
        session_str = str(session_id)
        meta_filepath = self.storage_dir / f"{session_str}{self.meta_suffix}"

        parent_dir = parent_session_id or session_id
        filepath = self.storage_dir / str(parent_dir) / f"{session_str}{self.session_suffix}"

        return filepath, meta_filepath

    async def save_session(self, session: AgentSession) -> None:
        """Asynchronously serializes and persists a session to disk using batched IO."""
        filepath, meta_filepath = self._resolve_paths(
            session.session_id, session.metadata.parent_session_id
        )

        now_iso = datetime.datetime.now().isoformat()
        updated_metadata = session.metadata.model_copy(
            update={
                "last_updated": now_iso,
                "created_at": session.metadata.created_at or now_iso,
                "turn_count": len(session.chat_history)
            }
        )
        session._metadata = updated_metadata

        payload_str = "\n".join([msg.model_dump_json() for msg in session.chat_history])
        meta_payload = SessionMetaSidecar(
            session_id=session.session_id,
            metadata=updated_metadata,
            turn_count=len(session.chat_history)
        )

        def _batch_write():
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(payload_str, encoding="utf-8")
            meta_filepath.write_text(meta_payload.model_dump_json(), encoding="utf-8")

        await asyncio.to_thread(_batch_write)

    async def create_sub_session(self, parent_session_id: uuid.UUID, agent_name: str, query: str) -> AgentSession:
        """Creates a new isolated child session stored flat inside the parent's directory."""
        child_session_id = uuid.uuid7()
        sub_session = AgentSession(
            session_id=child_session_id,
            chat_history=[],
            metadata=SessionMetadataPayload(
                name=f"Subagent: {agent_name}",
                query=query,
                created_at=datetime.datetime.now().isoformat(),
                last_updated=datetime.datetime.now().isoformat(),
                turn_count=0,
                agent_name=agent_name,
                parent_session_id=parent_session_id
            )
        )
        await self.save_session(sub_session)
        return sub_session

    async def load_session(self, session_id: uuid.UUID, parent_session_id: Optional[uuid.UUID] = None, checkpoint_name: Optional[str] = None) -> AgentSession:
        """Asynchronously reads and parses session files using a single batched IO block."""
        filepath, meta_filepath = self._resolve_paths(session_id, parent_session_id)

        def _batch_read():
            if not filepath.exists():
                raise FileNotFoundError(f"Session file not found: {filepath}")
            content = filepath.read_text(encoding="utf-8")
            meta_content = meta_filepath.read_text(encoding="utf-8") if meta_filepath.exists() else None
            return content, meta_content

        content, meta_content = await asyncio.to_thread(_batch_read)

        chat_history: List[ChatMessage] = []
        metadata_from_payload: Optional[SessionMetadataPayload] = None
        session_id_from_payload: Optional[uuid.UUID] = None

        for idx, line in enumerate(content.splitlines()):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                record = session_record_adapter.validate_json(line_str)
            except Exception as e:
                raise ValueError(f"Line {idx + 1} in {filepath} is invalid: {e}")

            if isinstance(record, ChatMessage):
                chat_history.append(record)
            elif isinstance(record, SessionMetadataPayload):
                metadata_from_payload = record
            elif isinstance(record, SessionIdRecord):
                session_id_from_payload = record.session_id
            elif isinstance(record, SetDelta):
                idx_val = record.index
                msg_val = record.message
                if 0 <= idx_val < len(chat_history):
                    chat_history[idx_val] = msg_val
                else:
                    chat_history.append(msg_val)
            elif isinstance(record, RewindDelta):
                if record.count is not None:
                    chat_history[:] = chat_history[:-record.count]
                elif record.truncate_to is not None:
                    chat_history[:] = chat_history[:record.truncate_to]

        if meta_content:
            metadata = SessionMetaSidecar.model_validate_json(meta_content).metadata
        elif metadata_from_payload:
            metadata = metadata_from_payload
        else:
            raise ValueError(f"No valid metadata sidecar or payload metadata found for session {session_id}")

        if metadata.name and len(metadata.name) > MAX_AUTO_NAME_LENGTH:
            metadata = metadata.model_copy(update={"name": metadata.name[:MAX_AUTO_NAME_LENGTH]})

        return AgentSession(
            session_id=session_id_from_payload or session_id,
            chat_history=chat_history,
            metadata=metadata
        )

    async def delete_session(self, session_id: uuid.UUID, parent_session_id: Optional[uuid.UUID] = None) -> None:
        """Asynchronously deletes a session sidecar and its isolated directory using a single batched IO block."""
        filepath, meta_filepath = self._resolve_paths(session_id, parent_session_id)

        def _batch_delete():
            if meta_filepath.exists():
                meta_filepath.unlink()
            if not parent_session_id:
                parent_dir = filepath.parent
                if parent_dir.exists() and parent_dir.is_dir():
                    shutil.rmtree(parent_dir)
            else:
                if filepath.exists():
                    filepath.unlink()

        await asyncio.to_thread(_batch_delete)

    async def list_sessions(self, exclude_subagents: bool = True) -> List[SessionMetaSidecar]:
        """Asynchronously lists and parses root parent sessions in a single, high-performance batched IO block."""
        if not self.storage_dir.exists():
            return []

        def _batch_list():
            sessions: List[SessionMetaSidecar] = []
            for p in self.storage_dir.iterdir():
                if p.name.endswith(self.meta_suffix):
                    try:
                        content = p.read_text(encoding="utf-8")
                        sidecar = SessionMetaSidecar.model_validate_json(content)
                        if exclude_subagents and sidecar.metadata.parent_session_id is not None:
                            continue
                        sessions.append(sidecar)
                    except Exception:
                        continue
            sessions.sort(key=lambda s: s.metadata.last_updated or "", reverse=True)
            return sessions

        return await asyncio.to_thread(_batch_list)

    async def write_truncation_log(self, session: AgentSession, tool_name: str, content: str) -> Path:
        """Asynchronously writes truncated tool outputs to a structured, session-specific directory."""
        log_filepath = self.storage_dir / str(session.session_id) / self.scratch_dir_name / f"{self.tool_log_prefix}{tool_name}_{uuid.uuid7()}{self.tool_log_suffix}"
        
        def _write():
            log_filepath.parent.mkdir(parents=True, exist_ok=True)
            log_filepath.write_text(content, encoding="utf-8")
            
        await asyncio.to_thread(_write)
        return log_filepath

    async def save_checkpoint(self, session_id: uuid.UUID, checkpoint_name: str) -> None:
        """Asynchronously creates a named checkpoint snapshot of the current session in JSONL format."""
        session = await self.load_session(session_id)
        filepath = self.storage_dir / str(session_id) / f"{str(session_id)}{self.checkpoint_separator}{checkpoint_name}{self.session_suffix}"

        lines = [msg.model_dump_json() for msg in session.chat_history]
        payload_str = "\n".join(lines)
        
        def _write_chk():
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(payload_str, encoding="utf-8")
            
        await asyncio.to_thread(_write_chk)

    async def enforce_retention_policy(self, max_age_days: int, max_count: Optional[int] = None) -> None:
        """Asynchronously enforces age and count constraints across active sessions."""
        sessions = await self.list_sessions(exclude_subagents=False)
        now = datetime.datetime.now()

        for session in list(sessions):
            last_updated_str = session.metadata.last_updated
            if last_updated_str:
                try:
                    last_updated = datetime.datetime.fromisoformat(last_updated_str)
                    age_delta = now - last_updated
                    if age_delta.days > max_age_days:
                        await self.delete_session(session.session_id, session.metadata.parent_session_id)
                        sessions.remove(session)
                except ValueError:
                    continue

        if max_count is not None and len(sessions) > max_count:
            old_sessions = sessions[max_count:]
            for session in old_sessions:
                await self.delete_session(session.session_id, session.metadata.parent_session_id)

