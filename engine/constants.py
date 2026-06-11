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

# Message roles matching the model API
ROLE_USER: str = "user"
ROLE_MODEL: str = "model"

# Orchestrator status states
STATUS_CONTINUE: str = "continue"
STATUS_STOP: str = "stop"

# Default fallback configurations
DEFAULT_PLATFORM: str = "mac"
DEFAULT_MODEL_NAME: str = "gemini-2.5-pro"
