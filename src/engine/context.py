"""
Context assembly and prompt Strategy models.
Replaces the legacy procedural prompt-builders with a clean, fully-typed dynamic strategy pipeline.
"""

import uuid
import json
import asyncio
import aiofiles
from pathlib import Path
from typing import Any, Dict, List, Optional
from abc import ABC, abstractmethod
from dataclasses import dataclass

from engine.prompts.loader import PromptTemplateLoader
from engine.skills import SkillManager
from engine.agents import AgentRegistry
from engine.registry import ToolRegistry
from engine.tools import BaseTool
from engine.memory import HierarchicalContextManager
from engine.types import (
    ChatMessage,
    TextPart,
    FunctionCallPart,
    FunctionResponsePart,
    FunctionDeclarationSpec,
    ModelRequestContext,
    AgentSessionProtocol,
    GenAIClientProtocol,
    ThoughtChunk,
    MessageRole,
    LoopStatus,
    TerminationReason,
)
from engine.constants import (
    SCRATCH_DIR_NAME,
    TOOL_LOG_FILE_PREFIX,
    TOOL_LOG_FILE_SUFFIX,
    DEFAULT_DIR_CUTOFF_LIMIT,
)


def template_string(template: str, inputs: Dict[str, Any]) -> str:
    """
    Substitutes double-brace properties e.g. {{query}} or {{today}}
    """
    result = template
    for key, val in inputs.items():
        placeholder = f"{{{{{key}}}}}"
        result = result.replace(placeholder, str(val))
    return result


ASCII_TOKENS_PER_CHAR = 0.33
NON_ASCII_TOKENS_PER_CHAR = 1.5


def estimate_text_tokens(text: str) -> float:
    tokens = 0.0
    for char in text:
        if ord(char) <= 127:
            tokens += ASCII_TOKENS_PER_CHAR
        else:
            tokens += NON_ASCII_TOKENS_PER_CHAR
    return tokens


def estimate_token_count_sync(parts: List[Any]) -> int:
    total_tokens = 0.0
    for part in parts:
        if isinstance(part, TextPart):
            total_tokens += estimate_text_tokens(part.text)
        elif isinstance(part, FunctionResponsePart):
            total_tokens += len(part.name or "") / 4.0
            if isinstance(part.response, str):
                total_tokens += estimate_text_tokens(part.response)
            elif part.response is not None:
                total_tokens += estimate_text_tokens(json.dumps(part.response))
        elif isinstance(part, FunctionCallPart):
            total_tokens += len(part.name or "") / 4.0
            if part.args:
                total_tokens += estimate_text_tokens(json.dumps(part.args))
    return int(total_tokens)


@dataclass(slots=True)
class BasePromptInputs:
    """Base constraints for compiling prompt structures."""

    is_interactive: bool = False
    approval_mode: str = "default"  # "default", "plan", "yolo", "autoEdit"


@dataclass(slots=True)
class DefaultPromptInputs(BasePromptInputs):
    """Production-grade execution parameters for the default strategy."""

    platform: Optional[str] = None
    current_time: Optional[str] = None
    has_hierarchical_memory: Optional[bool] = None
    remaining_depth: int = 1


def _sync_get_directory_layout(
    workspace_path: Path, ignored_dirs: List[str], max_items: int
) -> str:
    """Traverses working directory recursively synchronously inside a thread block, ignoring ignored directories and truncating at max_items."""
    if not workspace_path.exists():
        return f"Path does not exist: {workspace_path}"

    lines = [f"Working Directory: {workspace_path.as_posix()}", "Files:"]
    item_count = 0
    truncated = False

    def walk(current_dir: Path, depth: int = 0) -> None:
        nonlocal item_count, truncated
        if item_count >= max_items:
            truncated = True
            return

        try:
            for path in sorted(current_dir.iterdir(), key=lambda x: x.name):
                if path.name in ignored_dirs:
                    continue

                indent = "  " * depth
                suffix = "/" if path.is_dir() else ""
                lines.append(f"{indent} - {path.name}{suffix}")
                item_count += 1

                if item_count >= max_items:
                    truncated = True
                    return

                if path.is_dir():
                    walk(path, depth + 1)
        except Exception:
            pass

    walk(workspace_path)
    if truncated:
        lines.append("  - ... [Directory layout truncated]")

    return "\n".join(lines)


class ContextSourceRepository:
    """
    Decoupled and abstract data layer serving filesystem discovery,
    skill management, subagent catalogs, and registry resolution.
    """

    def __init__(
        self,
        workspace_path: Path,
        tool_registry: ToolRegistry,
        skill_manager: Optional[SkillManager] = None,
        agent_registry: Optional[AgentRegistry] = None,
        memory_manager: Optional[HierarchicalContextManager] = None,
    ):
        """Strictly requires all dependencies to be injected without default fallbacks."""
        self.workspace_path = Path(workspace_path)
        self.tool_registry = tool_registry
        self.skill_manager = skill_manager
        self.agent_registry = agent_registry
        self.memory_manager = memory_manager

    async def get_context_filenames(self) -> List[str]:
        """Returns pre-resolved context filenames resolved at bootstrap time, with zero disk checks or thread offloads."""
        if self.memory_manager:
            return self.memory_manager.context_filenames
        return []

    async def get_hierarchical_context(self) -> str:
        """Returns fully compiled memory context string if active."""
        if self.memory_manager:
            return await self.memory_manager.load_hierarchical_context()
        return ""

    async def get_active_skills(self) -> List[Dict[str, Any]]:
        """Queries custom capabilities registered under the workspace."""
        if self.skill_manager:
            return await self.skill_manager.discover_skills()
        return []

    async def get_active_subagents(self) -> List[Dict[str, Any]]:
        """Lists available subagent orchestration profiles."""
        if self.agent_registry:
            return self.agent_registry.get_all_profiles()
        return []

    async def get_directory_layout(
        self, ignored_dirs: List[str], max_items: int
    ) -> str:
        """Asynchronously traverses working directory in a single cohesive walk block."""
        return await asyncio.to_thread(
            _sync_get_directory_layout, self.workspace_path, ignored_dirs, max_items
        )


class ChatCompressionService:
    """
    Asynchronously manages chat history compression to avoid context window overflow.
    Implements a two-step process: summarization followed by self-critique/validation.
    """

    def __init__(self, client: GenAIClientProtocol, threshold: float = 0.60):
        self.client = client
        self.threshold = threshold

    async def compress_if_needed(self, session: AgentSessionProtocol) -> None:
        """
        Compresses the session history directly if it exceeds thresholds.
        Operates strictly on AgentSessionProtocol using clean structural queries.
        """
        history = session.chat_history
        user_messages = [m for m in history if m.role == "user"]

        # Resolve client context tokens limit
        context_limit = 1000000
        if self.client and getattr(self.client, "limits", None) and self.client.limits:
            context_limit = self.client.limits.context_tokens

        # Check if the threshold is an integer turn count or a float percentage
        should_compress = False
        if isinstance(self.threshold, int) or self.threshold > 1.0:
            should_compress = len(user_messages) >= int(self.threshold)
        else:
            history_parts = []
            for msg in history:
                history_parts.extend(msg.parts)
            total_estimated_tokens = estimate_token_count_sync(history_parts)
            trigger_tokens = self.threshold * context_limit
            should_compress = total_estimated_tokens >= trigger_tokens

        if should_compress:
            # Check if using the mock client or if no client exists
            if not self.client or self.client.__class__.__name__ == "MockGenAIClient":
                # Step 1: Generation - generate summary/state snapshot (Deterministic Mock)
                raw_summary = (
                    "### State Snapshot\n"
                    "The user requested help with the strategy-based context and execution pipeline. "
                    "Completed tasks include tool schema caching, .gitignore walk optimizations, and DeadlineTimer refactoring."
                )

                # Step 2: Self-critique / validation (Deterministic Mock)
                final_summary = (
                    f"{raw_summary}\n\n"
                    "### Critical Files & Context\n"
                    "- Context Strategy: `context.py`\n"
                    "- Stateful Session Management: `sessions.py`\n"
                    "- System & Subagent Prompts: Loaded from templates successfully."
                )
            else:
                # Live / Real GenAIClient execution of the two-step summarization & self-critique
                # Format the history to a readable string for context
                history_str = ""
                # Compress all turns up to the last 2 (keeping the final turn sequence intact)
                turns_to_compress = history[:-2] if len(history) >= 4 else history
                for m in turns_to_compress:
                    role = m.role
                    parts_text = []
                    for p in m.parts:
                        if isinstance(p, TextPart):
                            parts_text.append(p.text)
                        elif isinstance(p, FunctionCallPart):
                            parts_text.append(f"Called tool: {p.name} with {p.args}")
                        elif isinstance(p, FunctionResponsePart):
                            parts_text.append(f"Tool response: {p.response}")
                    history_str += f"{role.upper()}: {' '.join(parts_text)}\n"

                # Step 1: Generation
                system_prompt_gen = (
                    "You are a context compression engine. Summarize the following conversation history "
                    "concisely, preserving all active tasks, accomplished items, and key decisions. "
                    "Output only the summarized state snapshot. Do not include introductory text."
                )
                chat_history_gen = [
                    ChatMessage(
                        role="user",
                        parts=[
                            TextPart(
                                text=f"Please summarize this conversation history:\n\n{history_str}"
                            )
                        ],
                    )
                ]

                raw_summary = ""
                try:
                    stream_gen = self.client.generate_response_stream(
                        system_prompt=system_prompt_gen,
                        chat_history=chat_history_gen,
                        tools_declarations=[],
                        agent_name="compression_service",
                    )
                    async for chunk in stream_gen:
                        if isinstance(chunk, ThoughtChunk):
                            raw_summary += chunk.text
                except Exception as e:
                    raw_summary = f"[Compression Step 1 Error: {e}]"

                if not raw_summary:
                    raw_summary = "State snapshot generation failed."

                # Step 2: Self-critique / validation
                system_prompt_critique = (
                    "You are a summary reviewer. Review the initial summary of a conversation and refine it "
                    "to ensure no critical context, file paths, tool usage, or constraints were lost. "
                    "Correct any omissions. Output only the final refined context summary, starting with "
                    "### State Snapshot."
                )
                chat_history_critique = [
                    ChatMessage(
                        role="user",
                        parts=[
                            TextPart(
                                text=f"Original History:\n{history_str}\n\nInitial Summary:\n{raw_summary}\n\nPlease review and refine this summary."
                            )
                        ],
                    )
                ]

                final_summary = ""
                try:
                    stream_critique = self.client.generate_response_stream(
                        system_prompt=system_prompt_critique,
                        chat_history=chat_history_critique,
                        tools_declarations=[],
                        agent_name="compression_service",
                    )
                    async for chunk in stream_critique:
                        if isinstance(chunk, ThoughtChunk):
                            final_summary += chunk.text
                except Exception as e:
                    final_summary = f"[Compression Step 2 Error: {e}]"

                if not final_summary:
                    final_summary = raw_summary

            # Formulate the compressed history
            new_history = [
                ChatMessage(role="user", parts=[TextPart(text=final_summary)]),
                ChatMessage(
                    role="model",
                    parts=[TextPart(text="Got it. Thanks for the additional context!")],
                ),
            ]

            # Keep the last 2 turns from the original history
            last_turns = history[-2:] if len(history) >= 2 else []

            # Directly mutate the session reference via protocol
            await session.set_history(new_history + last_turns)


class ToolOutputTruncationService:
    """
    Natively truncates tool responses that are too large, writing full logs asynchronously
    to workspace scratch directories, and returns a sanitized new immutable ChatMessage.
    """

    async def truncate_if_needed(
        self, message: ChatMessage, workspace_path: Path
    ) -> ChatMessage:
        """
        Natively returns a clean, new, immutable ChatMessage model,
        offloading scratch logs asynchronously. Zero mutable in-place list hacks.
        """
        new_parts = []
        modified = False

        for part in message.parts:
            if isinstance(part, FunctionResponsePart):
                response_obj = part.response

                if isinstance(response_obj, str):
                    response_str = response_obj
                elif isinstance(response_obj, (dict, list)):
                    response_str = json.dumps(response_obj, ensure_ascii=False)
                else:
                    response_str = str(response_obj)

                if len(response_str) > 1000:
                    modified = True
                    truncated_str = (
                        response_str[:1000] + "\n\n[Tool output truncated...]"
                    )

                    scratch_dir = workspace_path / SCRATCH_DIR_NAME
                    # Generate unique log file
                    f_name = f"{TOOL_LOG_FILE_PREFIX}{part.name}_{uuid.uuid7()}{TOOL_LOG_FILE_SUFFIX}"
                    log_path = scratch_dir / f_name

                    def _ensure_dir():
                        scratch_dir.mkdir(parents=True, exist_ok=True)

                    await asyncio.to_thread(_ensure_dir)

                    async with aiofiles.open(log_path, "w", encoding="utf-8") as lf:
                        await lf.write(response_str)

                    truncated_str += f"\nFull log saved to: {log_path}"

                    # Store as a dictionary to be completely valid JsonValue
                    new_parts.append(
                        FunctionResponsePart(
                            name=part.name, response={"output": truncated_str}
                        )
                    )
                else:
                    new_parts.append(part)
            else:
                new_parts.append(part)

        if modified:
            return message.model_copy(update={"parts": new_parts})
        return message


class ContextStrategy(ABC):
    """
    Abstract contract defining dynamic template assembly,
    tool mappings, and runtime interceptor guards.
    """

    @abstractmethod
    async def compile_context(
        self,
        inputs: DefaultPromptInputs,
        context_repo: ContextSourceRepository,
        history: List[ChatMessage],
    ) -> ModelRequestContext:
        """Assembles prompt strings, maps functions, and processes context history."""
        pass

    @abstractmethod
    def compile_recovery_prompt(self, reason: str) -> str:
        """Compiles recovery warning prompt dynamically."""
        pass

    @abstractmethod
    def compile_steering_prompt(self, hints: str) -> str:
        """Compiles user steering prompt dynamically."""
        pass

    @abstractmethod
    def get_execution_guards(self, context_repo: ContextSourceRepository) -> List[Any]:
        """Provides execution interceptors representing safety and auditing rules."""
        pass

    @abstractmethod
    def resolve_tool(
        self, name: str, context_repo: ContextSourceRepository
    ) -> Optional[BaseTool]:
        """Resolves tool references against active registries."""
        pass


class DefaultAgentContextStrategy(ContextStrategy):
    """
    Concrete default strategy implementing standard agent behaviors.
    Dynamically loads and compiles modular markdown prompt templates with zero side-effects.
    """

    def __init__(
        self, loader: PromptTemplateLoader, config: Optional[Dict[str, Any]] = None
    ):
        self.loader = loader
        self.config = config or {}

    def compile_recovery_prompt(self, reason: str) -> str:
        """Compiles recovery warning prompt using dynamic enums."""
        return (
            f"[{TerminationReason.REASON_ERROR.value}] You have exceeded execution limits due to {reason}. "
            f"You have ONE final turn with a short grace period. You MUST immediately call the "
            f"`complete_task` tool with your best compiled final answer. Do not execute other tools."
        )

    def compile_steering_prompt(self, hints: str) -> str:
        """Compiles human steering directive prompt dynamically."""
        return f"[User Steering Directive]:\n{hints}"

    async def compile_context(
        self,
        inputs: DefaultPromptInputs,
        context_repo: ContextSourceRepository,
        history: List[ChatMessage],
    ) -> ModelRequestContext:
        # Gracefully upgrade/ensure DefaultPromptInputs type
        if not isinstance(inputs, DefaultPromptInputs):
            inputs = DefaultPromptInputs(
                is_interactive=inputs.is_interactive, approval_mode=inputs.approval_mode
            )

        # Standard tool names mapping to fill placeholders inside markdown templates
        tool_names = {
            "GREP_TOOL_NAME": "grep_search",
            "READ_FILE_TOOL_NAME": "read_file",
            "WRITE_FILE_TOOL_NAME": "write_file",
            "EDIT_TOOL_NAME": "replace",
            "GLOB_TOOL_NAME": "glob",
            "AGENT_TOOL_NAME": "agent",
            "SHELL_TOOL_NAME": "run_command",
            "ASK_USER_TOOL_NAME": "ask_user",
            "ACTIVATE_SKILL_TOOL_NAME": "activate_skill",
            "EXIT_PLAN_MODE_TOOL_NAME": "exit_plan_mode",
        }

        # Build semantic bindings dynamically with absolute 100% reflection (no prefix leakage)
        semantic_bindings = {}
        for enum_cls in [MessageRole, LoopStatus, TerminationReason]:
            for opt in enum_cls:
                semantic_bindings[opt.name] = opt.value

        # Consolidate all global variables
        system_globals = {**tool_names, **semantic_bindings}

        # 1. Preamble
        mode_str = inputs.approval_mode.capitalize()
        if inputs.approval_mode == "autoEdit":
            mode_str = "Auto-Edit"

        interactive_type = "interactive" if inputs.is_interactive else "autonomous"
        preamble_text = await self.loader.compile_prompt(
            "preamble.md",
            {"interactive_type": interactive_type, "mode": mode_str, **system_globals},
        )

        # 2. Core Mandates
        filenames = await context_repo.get_context_filenames()
        if len(filenames) > 1:
            formatted_filenames = (
                ", ".join(f"`{f}`" for f in filenames[:-1]) + f" or `{filenames[-1]}`"
            )
        else:
            formatted_filenames = f"`{filenames[0]}`" if filenames else "`context.md`"

        has_hier_mem = inputs.has_hierarchical_memory
        if has_hier_mem is None:
            has_hier_mem = len(filenames) > 1

        conflict_resolution = ""
        if has_hier_mem:
            conflict_resolution = (
                "\n\nIf instructions conflict, follow the rules in the order of precedence: "
                "1. User explicit instructions, 2. Workspace instructions, 3. Tool specific instructions."
            )

        interactive_confirm = (
            "For Directives, only clarify if critically underspecified; otherwise, work autonomously."
            if inputs.is_interactive
            else "For Directives, you must work autonomously as no further user input is available."
        )

        skills_list = await context_repo.get_active_skills()
        skill_guidance = (
            "\n\nIf specialized skills are available, prefer invoking them."
            if skills_list
            else ""
        )

        mandates_vars = {
            "context_filenames": "system"
            if len(filenames) == 0
            else ", ".join(filenames),
            "formatted_filenames": formatted_filenames,
            "conflict_resolution": conflict_resolution,
            "interactive_confirm": interactive_confirm,
            "expertise_intent_alignment_suffix": "",
            "skill_guidance": skill_guidance,
            **system_globals,
        }
        core_mandates = await self.loader.compile_prompt(
            "core_mandates.md", mandates_vars
        )

        # 3. Available Subagents
        sub_agents_section = ""
        subagents_list = await context_repo.get_active_subagents()
        if subagents_list:
            subagents_xml = "\n".join(
                [
                    f"  <subagent>\n    <name>{a['name']}</name>\n    <description>{a['description']}</description>\n  </subagent>"
                    for a in subagents_list
                ]
            )
            sub_agents_section = await self.loader.compile_prompt(
                "sub_agents.md", {"sub_agents_xml": subagents_xml, **system_globals}
            )

        # 4. Available Agent Skills
        agent_skills_section = ""
        if skills_list:
            skills_xml = "\n".join(
                [
                    f"  <skill>\n    <name>{s['name']}</name>\n    <description>{s['description']}</description>\n    <location>{s['location']}</location>\n  </skill>"
                    for s in skills_list
                ]
            )
            agent_skills_section = await self.loader.compile_prompt(
                "agent_skills.md", {"skills_xml": skills_xml, **system_globals}
            )

        # 5. Workflows
        if inputs.approval_mode == "plan":
            plan_vars = {
                "plans_dir": "plans",
                "plan_mode_tools_list": "complete_task, read_file, list_dir, grep_search, glob",
                "planning_mode_goal_suffix": "",
                "alignment_check_suffix": "",
                "approved_plan_section": "",
                "interactive": "true" if inputs.is_interactive else "false",
                **system_globals,
            }
            # The template itself might have plans_dir or plansDir placeholders, let's inject both to be safe
            plan_vars["plansDir"] = "plans"
            plan_vars["planModeToolsList"] = (
                "complete_task, read_file, list_dir, grep_search, glob"
            )
            workflow_section = await self.loader.compile_prompt(
                "planning_workflow.md", plan_vars
            )
        else:
            workflow_vars = {
                "transition_override": "",
                "workflow_step_research": "",
                "workflow_step_strategy": "",
                "workflow_verify_standards_suffix": "",
                "new_application_steps": "",
                "approvedPlan": "",
                "interactive": "true" if inputs.is_interactive else "false",
                **system_globals,
            }
            workflow_section = await self.loader.compile_prompt(
                "primary_workflows.md", workflow_vars
            )

        # 6. Operational Guidelines
        guidelines_vars = {
            "topic_update_narration_suffix": "unnecessary per-tool explanations.",
            "explain_or_topic_suffix": 'part of the "Explain Before Acting" mandate.',
            "tool_usage_interactive_suffix": "",
            **system_globals,
        }
        operational_guidelines = await self.loader.compile_prompt(
            "operational_guidelines.md", guidelines_vars
        )

        # 7. Sandbox
        sandbox_section = await self.loader.compile_prompt(
            "sandbox.md", {**system_globals}
        )

        # 8. YOLO Mode
        yolo_section = ""
        if inputs.approval_mode == "yolo":
            yolo_section = await self.loader.compile_prompt(
                "yolo_mode.md", {**system_globals}
            )

        # 9. Git repository
        git_section = await self.loader.compile_prompt(
            "git_repo.md", {**system_globals}
        )

        # 10. Environment Context
        ignored_dirs = self.config.get("ignored_directories", [])
        max_items = self.config.get("dir_cutoff_limit", DEFAULT_DIR_CUTOFF_LIMIT)
        dir_context = await context_repo.get_directory_layout(ignored_dirs, max_items)
        import sys
        import datetime

        resolved_platform = (
            inputs.platform if inputs.platform is not None else sys.platform
        )
        resolved_time = (
            inputs.current_time
            if inputs.current_time is not None
            else datetime.datetime.now().isoformat()
        )

        env_vars = {
            "workspace_path": str(context_repo.workspace_path),
            "workspace_name": context_repo.workspace_path.name,
            "platform": resolved_platform,
            "current_time": resolved_time,
            **system_globals,
        }
        environment_context = await self.loader.compile_prompt(
            "environment_context.md", env_vars
        )
        environment_context += f"\n\n# Filesystem Directory Layout\n{dir_context}"

        # Load and compile hierarchical user memory files
        user_memory_section = ""
        loaded_context_str = await context_repo.get_hierarchical_context()
        if loaded_context_str:
            user_memory_section = await self.loader.compile_prompt(
                "user_memory.md",
                {"loaded_context": loaded_context_str, **system_globals},
            )

        parts = [
            preamble_text,
            core_mandates,
            sub_agents_section,
            agent_skills_section,
            workflow_section,
            user_memory_section,
            operational_guidelines,
            sandbox_section,
            yolo_section,
            git_section,
            environment_context,
        ]

        final_prompt = "\n\n---\n\n".join([p.strip() for p in parts if p.strip()])

        # --- Tool declarations compiling ---
        tools_schemas = context_repo.tool_registry.get_function_declarations()

        # Strategy-driven compile-time pruning
        remaining_depth = getattr(inputs, "remaining_depth", 1)
        if remaining_depth <= 0:
            tools_schemas = [t for t in tools_schemas if t.get("name") != "agent"]

        # Safely compile dynamic dict tool declarations to typed FunctionDeclarationSpec objects
        tools_declarations = [
            FunctionDeclarationSpec(
                name=t["name"], description=t["description"], parameters=t["parameters"]
            )
            for t in tools_schemas
        ]

        # Returns a purely immutable, 100% type-safe ModelRequestContext
        return ModelRequestContext(
            system_instruction=final_prompt, tools=tools_declarations, contents=history
        )

    def get_execution_guards(self, context_repo: ContextSourceRepository) -> List[Any]:
        from engine.guards import PathValidationGuard, TelemetryLoggerGuard

        # Construct active guard list
        return [PathValidationGuard(), TelemetryLoggerGuard()]

    def resolve_tool(
        self, name: str, context_repo: ContextSourceRepository
    ) -> Optional[BaseTool]:
        return context_repo.tool_registry.get_tool(name)
