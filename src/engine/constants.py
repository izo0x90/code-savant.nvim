"""
Centralized shared constants for the Agent Replica.
Eliminates magic values and string literals across engine files.
"""

DEFAULT_MAX_TURNS: int = 10
DEFAULT_MAX_TIME_SEC: int = 60
DEFAULT_RETENTION_DAYS: int = 30
DEFAULT_RECOVERY_TIME_SEC: float = 30.0

COMPLETE_TASK_TOOL_NAME: str = "complete_task"
SESSION_FILE_SUFFIX: str = ".json"
SESSION_META_SUFFIX: str = ".meta.json"
CHECKPOINT_SEPARATOR: str = "_checkpoint_"

# File logging and scratch constants
SCRATCH_DIR_NAME: str = "scratch"
TOOL_LOG_FILE_PREFIX: str = "tool_"
TOOL_LOG_FILE_SUFFIX: str = ".log"


# Orchestrator status states
STATUS_CONTINUE: str = "continue"
STATUS_STOP: str = "stop"

# Default fallback configurations
DEFAULT_PLATFORM: str = "mac"
DEFAULT_MODEL_NAME: str = "gemini-2.5-pro"
DEFAULT_SOCKET_PATH: str = "/tmp/code_savant.sock"

DEFAULT_SESSION_NAME: str = "Untitled Session"
MAX_AUTO_NAME_LENGTH: int = 64

# Protocol Event Types and Constants
EVENT_TOOL_CONFIRMATION_REQUEST: str = "tool-confirmation-request"

# Playback Delta Modifier Names
DELTA_TYPE_SET: str = "SetDelta"
DELTA_TYPE_REWIND: str = "RewindDelta"
KEY_SET_DELTA: str = "set_delta"
KEY_REWIND_DELTA: str = "rewind_delta"

# Playback Schema Keys
KEY_DELTA_INDEX: str = "index"
KEY_DELTA_MESSAGE: str = "message"
KEY_DELTA_METADATA: str = "metadata"
KEY_DELTA_COUNT: str = "count"
KEY_DELTA_TRUNCATE_TO: str = "truncate_to"

# Default request timeout
DEFAULT_REQUEST_TIMEOUT: float = 60.0
