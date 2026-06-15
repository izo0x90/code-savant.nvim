from abc import ABC, abstractmethod
from typing import Any, Dict, List
from pathlib import Path
from engine.constants import DEFAULT_REQUEST_TIMEOUT

from engine.tools import BaseTool
from engine.types import ExecutionContext


class ToolExecutionGuard(ABC):
    """Abstract middleware interceptor for runtime tool execution safety and auditing."""

    @abstractmethod
    async def before_execute(self, tool_name: str, args: Dict[str, Any], context: ExecutionContext) -> bool:
        """
        Invoked before tool execution.
        Returns True to proceed, False to skip, or raises exceptions to abort.
        """
        pass

    @abstractmethod
    async def after_execute(self, tool_name: str, args: Dict[str, Any], result: Any, context: ExecutionContext) -> Any:
        """
        Invoked after successful execution.
        Allows logging, modifying, or formatting the tool's return payload.
        """
        pass


class PathValidationGuard(ToolExecutionGuard):
    """Enforces absolute/relative path boundaries, preventing escape from workspace."""

    async def before_execute(self, tool_name: str, args: Dict[str, Any], context: ExecutionContext) -> bool:
        # Fully synchronous path calculations resolved against context.workspace_path
        workspace_root = context.workspace_path.resolve()

        # Inspect any argument that represents a path
        path_keys = ["file_path", "dir_path", "target_dir", "path", "dest", "src"]
        for key, val in args.items():
            if key in path_keys and isinstance(val, str):
                p = Path(val)
                if not p.is_absolute():
                    resolved = (workspace_root / p).resolve()
                else:
                    resolved = p.resolve()

                if workspace_root != resolved and workspace_root not in resolved.parents:
                    raise PermissionError(
                        f"Access Denied: Path '{val}' resolves to '{resolved}' which lies outside "
                        f"the authorized workspace boundary: '{workspace_root}'."
                    )
        return True

    async def after_execute(self, tool_name: str, args: Dict[str, Any], result: Any, context: ExecutionContext) -> Any:
        return result


class TelemetryLoggerGuard(ToolExecutionGuard):
    """No-op logger guard demonstrating telemetry injection hooks."""

    async def before_execute(self, tool_name: str, args: Dict[str, Any], context: ExecutionContext) -> bool:
        return True

    async def after_execute(self, tool_name: str, args: Dict[str, Any], result: Any, context: ExecutionContext) -> Any:
        return result


class UserConfirmationGuard(ToolExecutionGuard):
    """Interactive approval gate that requests user verification over the MessageBus."""

    def __init__(self, timer: Any, is_interactive: bool, call_id: str):
        self.timer = timer
        self.is_interactive = is_interactive
        self.call_id = call_id

    async def before_execute(self, tool_name: str, args: Dict[str, Any], context: ExecutionContext) -> bool:
        # Pause execution and check user consent for destructive/structural tools
        if self.is_interactive and tool_name in ["write_file", "replace"]:
            await context.message_bus.publish({
                "type": "telemetry:activity",
                "activity_type": "AWAITING_APPROVAL",
                "msg": "Suspending budget countdown for user verification...",
                "tool": tool_name
            })
            self.timer.pause()
            
            confirm_payload = {
                "type": "tool-confirmation-request",
                "toolCall": {
                    "id": self.call_id,
                    "name": tool_name,
                    "args": args
                },
                "correlationId": self.call_id
            }
            response = await context.message_bus.request(confirm_payload, "tool-confirmation-response", DEFAULT_REQUEST_TIMEOUT)
            self.timer.resume()

            if not response.get("confirmed", False):
                await context.message_bus.publish({
                    "type": "telemetry:activity",
                    "activity_type": "APPROVAL_DENIED",
                    "msg": "User declined tool execution request.",
                    "tool": tool_name
                })
                raise PermissionError("Tool execution rejected by user confirmation safeguard.")
        return True

    async def after_execute(self, tool_name: str, args: Dict[str, Any], result: Any, context: ExecutionContext) -> Any:
        return result


class ToolExecutionChain:
    """Orchestrates sequential execution of stacked policy guards around a target tool."""

    def __init__(self, guards: List[ToolExecutionGuard], tool: BaseTool):
        self.guards = guards
        self.tool = tool

    async def execute(self, args: Dict[str, Any], context: ExecutionContext) -> Any:
        # 1. Run Pre-Execution Hooks
        for guard in self.guards:
            await guard.before_execute(self.tool.name, args, context)

        # 2. Run the actual tool
        result = await self.tool.execute(args, context)

        # 3. Run Post-Execution Hooks (in reverse order)
        for guard in reversed(self.guards):
            result = await guard.after_execute(self.tool.name, args, result, context)

        return result
