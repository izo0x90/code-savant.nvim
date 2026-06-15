import datetime
import asyncio
import uuid
from pathlib import Path
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from engine.constants import (
    DEFAULT_RETENTION_DAYS,
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


class SessionPayload(BaseModel):
    session_id: str
    metadata: SessionMetadataPayload = Field(default_factory=SessionMetadataPayload)
    chat_history: List[ChatMessage] = Field(default_factory=list)


class AgentSession(AgentSessionProtocol):
    """
    Pure Domain Entity.
    Contains absolutely zero knowledge of filesystem locations, paths, or persistence.
    """
    __slots__ = ("_session_id", "_history", "_metadata")

    def __init__(
        self,
        session_id: str,
        chat_history: List[ChatMessage],
        metadata: SessionMetadataPayload
    ):
        self._session_id = session_id
        self._metadata: SessionMetadataPayload = metadata
        self._history: List[ChatMessage] = list(chat_history)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def chat_history(self) -> List[ChatMessage]:
        return self._history

    @property
    def metadata(self) -> SessionMetadataPayload:
        return self._metadata

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
        def _mkdir():
            self.storage_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_mkdir)

    def _get_filepath(self, session_id: str, checkpoint_name: Optional[str] = None, storage_dir: Optional[Path] = None) -> Path:
        target_dir = storage_dir or self.storage_dir
        if checkpoint_name:
            filename = f"{session_id}{self.checkpoint_separator}{checkpoint_name}{self.session_suffix}"
        else:
            filename = f"{session_id}{self.session_suffix}"
        return target_dir / filename

    async def save_session(self, session: AgentSession) -> None:
        """
        Asynchronously serializes and persists a session to disk.
        Leverages Pydantic v2's native model_dump_json for zero dictionary copy costs.
        """
        # Resolve output files against repository base storage_dir
        filepath = self._get_filepath(session.session_id)
        meta_filepath = self.storage_dir / f"{session.session_id}{self.meta_suffix}"

        # Update metadata state cleanly using Pydantic's native model_copy (zero manual dict rebuilds)
        now_iso = datetime.datetime.now().isoformat()
        updated_metadata = session.metadata.model_copy(
            update={
                "last_updated": now_iso,
                "created_at": session.metadata.created_at or now_iso,
                "turn_count": len(session.chat_history)
            }
        )
        session._metadata = updated_metadata

        # Serialize ChatMessage objects line-by-line in JSONL format
        lines = [msg.model_dump_json() for msg in session.chat_history]
        payload_str = "\n".join(lines)

        meta_payload = SessionMetaSidecar(
            session_id=session.session_id,
            metadata=session.metadata,
            turn_count=len(session.chat_history)
        )
        meta_str = meta_payload.model_dump_json(indent=2)

        # Execute full directory creation and writes inside a single thread call
        await asyncio.to_thread(
            _sync_save_session_files,
            filepath,
            payload_str,
            meta_filepath,
            meta_str
        )

    async def create_sub_session(self, parent_session_id: str, agent_name: str, query: str) -> AgentSession:
        """
        Creates a new isolated child session under the parent session's subdirectory.
        Prevents parent listing pollution while keeping sessions structurally associated.
        """
        child_session_id = f"{agent_name}_{uuid.uuid7()}"
        
        # Return a pure domain AgentSession
        sub_session = AgentSession(
            session_id=child_session_id,
            chat_history=[],
            metadata=SessionMetadataPayload(
                name=f"Subagent: {agent_name}",
                query=query,
                created_at=datetime.datetime.now().isoformat(),
                last_updated=datetime.datetime.now().isoformat(),
                turn_count=0
            )
        )
        
        # Resolve path under parent session id
        child_storage_dir = self.storage_dir / parent_session_id
        
        sub_manager = SessionManager(
            storage_dir=child_storage_dir,
            session_suffix=self.session_suffix,
            meta_suffix=self.meta_suffix,
            checkpoint_separator=self.checkpoint_separator,
            scratch_dir_name=self.scratch_dir_name,
            tool_log_prefix=self.tool_log_prefix,
            tool_log_suffix=self.tool_log_suffix
        )
        await sub_manager.save_session(sub_session)
        return sub_session

    def _apply_playback_line(
        self,
        data: Dict[str, Any],
        chat_history: List[ChatMessage],
        line_num: int,
        filepath: Path
    ) -> Optional[SessionMetadataPayload]:
        """
        Surgically applies a single log modifier or appends a raw ChatMessage.
        Returns updated session metadata if found, otherwise None.
        """
        modifier_type = data.get("type")

        if modifier_type == DELTA_TYPE_SET or KEY_SET_DELTA in data:
            try:
                if KEY_DELTA_INDEX in data and KEY_DELTA_MESSAGE in data:
                    index_val = data[KEY_DELTA_INDEX]
                    msg_data = data[KEY_DELTA_MESSAGE]
                    msg = ChatMessage.model_validate(msg_data)
                    if 0 <= index_val < len(chat_history):
                        chat_history[index_val] = msg
                    else:
                        chat_history.append(msg)
                elif KEY_DELTA_METADATA in data:
                    meta_data = data[KEY_DELTA_METADATA]
                    return SessionMetadataPayload.model_validate(meta_data)
            except Exception as e:
                raise ValueError(f"Line {line_num} in {filepath} has invalid SetDelta structure: {e}")
        elif modifier_type == DELTA_TYPE_REWIND or KEY_REWIND_DELTA in data:
            try:
                if KEY_DELTA_COUNT in data:
                    count_val = data[KEY_DELTA_COUNT]
                    if isinstance(count_val, int) and count_val > 0:
                        chat_history[:] = chat_history[:-count_val]
                elif KEY_DELTA_TRUNCATE_TO in data:
                    trunc_val = data[KEY_DELTA_TRUNCATE_TO]
                    if isinstance(trunc_val, int) and 0 <= trunc_val <= len(chat_history):
                        chat_history[:] = chat_history[:trunc_val]
            except Exception as e:
                raise ValueError(f"Line {line_num} in {filepath} has invalid RewindDelta structure: {e}")
        else:
            try:
                # Clean up "type" key if it was injected or present
                msg_data = dict(data)
                msg_data.pop("type", None)
                msg = ChatMessage.model_validate(msg_data)
                chat_history.append(msg)
            except Exception as e:
                raise ValueError(f"Line {line_num} in {filepath} has invalid ChatMessage structure: {e}")
        return None

    async def load_session(self, session_id: str, checkpoint_name: Optional[str] = None) -> AgentSession:
        """
        Asynchronously loads and parses session files line-by-line using a sequential playback parser.
        Supports both standard JSON (SessionPayload) and JSONL formats containing state modifiers.
        Loads companion metadata sidecar to merge dates and reconstruct the fully validated session.
        """
        filepath = self._get_filepath(session_id, checkpoint_name)
        meta_filepath = self.storage_dir / f"{session_id}{self.meta_suffix}"

        def _read_files():
            if not filepath.exists():
                raise FileNotFoundError(f"Session file not found: {filepath}")
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            
            meta_content = None
            if meta_filepath.exists():
                with open(meta_filepath, "r", encoding="utf-8") as f:
                    meta_content = f.read()
            return content, meta_content

        content, meta_content = await asyncio.to_thread(_read_files)

        chat_history: List[ChatMessage] = []
        metadata_from_payload: Optional[SessionMetadataPayload] = None
        session_id_from_payload: Optional[str] = None

        # Clean the input content to check if it represents a single standard JSON object
        stripped_content = content.strip()
        is_standard_json = False

        if stripped_content.startswith("{") and stripped_content.endswith("}"):
            try:
                payload = SessionPayload.model_validate_json(stripped_content)
                chat_history = list(payload.chat_history)
                session_id_from_payload = payload.session_id
                metadata_from_payload = payload.metadata
                is_standard_json = True
            except Exception:
                # If it looks like standard JSON but fails to validate, we will fall back to JSONL
                # parsing line-by-line to find the exact line causing the corrupt structure/validation error.
                is_standard_json = False

        if not is_standard_json:
            import json
            lines = content.splitlines()
            for idx, line in enumerate(lines):
                line_num = idx + 1
                stripped_line = line.strip()
                if not stripped_line:
                    continue
                
                try:
                    data = json.loads(stripped_line)
                except Exception as e:
                    raise ValueError(f"Line {line_num} in {filepath} is corrupt or invalid JSON: {e}")

                if not isinstance(data, dict):
                    raise ValueError(f"Line {line_num} in {filepath} is not a valid JSON object")

                meta = self._apply_playback_line(data, chat_history, line_num, filepath)
                if meta:
                    metadata_from_payload = meta

        # Reconstruct session metadata by merging dates and companion sidecar cleanly
        name = None
        query = None
        created_at = None
        last_updated = None
        turn_count = len(chat_history)

        if metadata_from_payload:
            name = metadata_from_payload.name
            query = metadata_from_payload.query
            created_at = metadata_from_payload.created_at
            last_updated = metadata_from_payload.last_updated
            if metadata_from_payload.turn_count is not None:
                turn_count = metadata_from_payload.turn_count

        if meta_content:
            try:
                sidecar = SessionMetaSidecar.model_validate_json(meta_content)
                sidecar_meta = sidecar.metadata
                if sidecar_meta:
                    name = sidecar_meta.name or name
                    query = sidecar_meta.query or query
                    created_at = sidecar_meta.created_at or created_at
                    last_updated = sidecar_meta.last_updated or last_updated
                    if sidecar_meta.turn_count is not None:
                        turn_count = sidecar_meta.turn_count
            except Exception as e:
                raise ValueError(f"Companion sidecar metadata file {meta_filepath} is corrupt: {e}")

        # Enforce name fallback and maximum auto name length limit using centralized constants
        if not name:
            name = DEFAULT_SESSION_NAME
        elif len(name) > MAX_AUTO_NAME_LENGTH:
            name = name[:MAX_AUTO_NAME_LENGTH]

        merged_metadata = SessionMetadataPayload(
            name=name,
            query=query,
            created_at=created_at,
            last_updated=last_updated,
            turn_count=turn_count
        )

        return AgentSession(
            session_id=session_id_from_payload or session_id,
            chat_history=chat_history,
            metadata=merged_metadata
        )

    async def list_sessions(self) -> List[SessionMetaSidecar]:
        """
        Asynchronously lists all active sessions in the storage directory.
        Reads lightweight sidecars directly into strict SessionMetaSidecar Pydantic models.
        """
        if not self.storage_dir.exists():
            return []

        def _list_dir():
            try:
                return list(self.storage_dir.iterdir())
            except Exception:
                return []

        paths = await asyncio.to_thread(_list_dir)
        sessions: List[SessionMetaSidecar] = []

        for p in paths:
            if p.name.endswith(self.meta_suffix):
                try:
                    def _read_meta():
                        with open(p, "r", encoding="utf-8") as f:
                            return f.read()
                    
                    content = await asyncio.to_thread(_read_meta)
                    sidecar = SessionMetaSidecar.model_validate_json(content)
                    sessions.append(sidecar)
                except Exception:
                    continue

        sessions.sort(key=lambda s: s.metadata.last_updated or "", reverse=True)
        return sessions

    async def delete_session(self, session_id: str) -> None:
        """Asynchronously deletes a session JSON file and its companion sidecar, along with nested scratch logs."""
        filepath = self._get_filepath(session_id)
        meta_filepath = self.storage_dir / f"{session_id}{self.meta_suffix}"
        scratch_dir = self.storage_dir / session_id / self.scratch_dir_name

        def _remove():
            if filepath.exists():
                filepath.unlink()
            if meta_filepath.exists():
                meta_filepath.unlink()
            
            # Clean up nested scratch directory
            if scratch_dir.exists() and scratch_dir.is_dir():
                for item in scratch_dir.iterdir():
                    if item.is_file():
                        item.unlink()
                scratch_dir.rmdir()
                
            # If the session folder itself exists, delete it if empty
            session_dir = self.storage_dir / session_id
            if session_dir.exists() and session_dir.is_dir():
                try:
                    if not list(session_dir.iterdir()):
                        session_dir.rmdir()
                except Exception:
                    pass

        await asyncio.to_thread(_remove)

    async def write_truncation_log(self, session: AgentSession, tool_name: str, content: str) -> Path:
        """
        Asynchronously writes truncated tool outputs to a structured, session-specific directory.
        """
        log_filepath = self.storage_dir / session.session_id / self.scratch_dir_name / f"{self.tool_log_prefix}{tool_name}_{uuid.uuid7()}{self.tool_log_suffix}"
        await asyncio.to_thread(_sync_write_truncation_log, log_filepath, content)
        return log_filepath

    async def save_checkpoint(self, session_id: str, checkpoint_name: str) -> None:
        """Asynchronously creates a named checkpoint snapshot of the current session in JSONL format."""
        session = await self.load_session(session_id)
        filepath = self._get_filepath(session_id, checkpoint_name)

        # Write sequential JSONL lines for checkpoints
        lines = [msg.model_dump_json() for msg in session.chat_history]
        payload_str = "\n".join(lines)
        await asyncio.to_thread(_sync_write_checkpoint, filepath, payload_str)

    async def enforce_retention_policy(self, max_age_days: int = DEFAULT_RETENTION_DAYS, max_count: Optional[int] = None) -> None:
        """Asynchronously enforces age and count constraints across active sessions."""
        sessions = await self.list_sessions()
        now = datetime.datetime.now()

        for session in list(sessions):
            last_updated_str = session.metadata.last_updated
            if last_updated_str:
                try:
                    last_updated = datetime.datetime.fromisoformat(last_updated_str)
                    age_delta = now - last_updated
                    if age_delta.days > max_age_days:
                        await self.delete_session(session.session_id)
                        sessions.remove(session)
                except ValueError:
                    continue

        if max_count is not None and len(sessions) > max_count:
            old_sessions = sessions[max_count:]
            for session in old_sessions:
                await self.delete_session(session.session_id)

