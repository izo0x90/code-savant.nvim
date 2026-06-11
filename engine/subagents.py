import asyncio
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from engine.tools import (
    BaseTool, CompleteTaskTool, ReadFileTool, WriteFileTool,
    ListDirectoryTool, GlobTool, GrepSearchTool, ReplaceTool
)
from engine.types import ExecutionContext, ExecutorAgentConfig
from engine.agents import AgentRegistry
from engine.context import ContextStrategy
from engine.registry import ToolRegistry


class AgentArgs(BaseModel):
    agent_name: str = Field(..., description="The name of the target subagent profile (e.g., 'coder', 'researcher').")
    prompt: str = Field(..., description="The specific, detailed task instructions for the subagent to perform.")


class AgentTool(BaseTool):
    """
    Asynchronous Subagent delegation tool ('agent') (chunk-DN4XSYRG.js Lines 315760-315825).
    Allows launching nested async subagent executables.
    """

    def __init__(self, agent_registry: AgentRegistry, context_strategy: ContextStrategy, tool_registry: ToolRegistry):
        super().__init__(
            name="agent",
            description="Delegate sub-tasks to specialized subagents. Spawns an isolated nested execution loop.",
            args_schema=AgentArgs,
        )
        self.agent_registry = agent_registry
        self.context_strategy = context_strategy
        self.tool_registry = tool_registry

    async def run(self, args: AgentArgs, context: ExecutionContext) -> Dict[str, Any]:
        # Enforce Nesting Guard (Rule 1: Fail Loudly)
        if context.remaining_depth <= 0:
            raise RuntimeError(
                f"Nesting Depth Violation: Attempted to spawn subagent '{args.agent_name}' "
                f"but remaining depth is {context.remaining_depth}."
            )

        profile = self.agent_registry.get_profile(args.agent_name)
        if not profile:
            return {"error": f"Subagent profile '{args.agent_name}' not found."}

        # Spawn isolated child session through SessionManager
        child_session = await context.session_manager.create_sub_session(
            parent_session_id=context.session.session_id,
            agent_name=args.agent_name,
            query=args.prompt
        )

        from engine.executor import LocalAgentExecutor
        
        # Derive Child Message Bus (Hierarchical prefixing)
        child_bus = context.message_bus.derive(args.agent_name)
        
        # Formulate and validate child executor config using strict Pydantic model
        config = ExecutorAgentConfig(
            name=profile.get("name", args.agent_name),
            max_turns=profile.get("max_turns", profile.get("maxTurns", 10)),
            max_time_seconds=profile.get("max_time_seconds", profile.get("maxTimeSeconds", 60)),
            plan_mode=profile.get("plan_mode", profile.get("planMode", False)),
            requires_approval=profile.get("requires_approval", profile.get("requiresApproval", False)),
            query=args.prompt
        )
        
        # Initialize child executor as completely stateless, reusing parent's tool registry
        child_executor = LocalAgentExecutor(
            definition=config,
            context_strategy=self.context_strategy,
            agent_registry=self.agent_registry,
            tool_registry=self.tool_registry
        )
        
        # Formulate dynamic child ExecutionContext
        child_context = ExecutionContext(
            workspace_path=context.workspace_path,
            message_bus=child_bus,
            remaining_depth=context.remaining_depth - 1,
            session=child_session,
            session_manager=context.session_manager,
            client=context.client
        )

        # Trigger child execution asynchronously passing the context dynamically
        sub_result = await child_executor.run(child_context, inputs={"query": args.prompt})

        return {
            "success": True,
            "agent_name": args.agent_name,
            "outcome": sub_result or "Subagent execution completed with no output."
        }
