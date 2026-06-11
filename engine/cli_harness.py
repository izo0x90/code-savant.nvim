import os
import sys
import asyncio
import argparse
from typing import Any, Dict
from engine.executor import LocalAgentExecutor
from engine.tools import (
    CompleteTaskTool,
    ReadFileTool,
    WriteFileTool,
    ListDirectoryTool,
    GlobTool,
    GrepSearchTool,
    ReplaceTool,
)
from engine.agents import AgentRegistry
from engine.subagents import AgentTool
from engine.types import ExecutorAgentConfig, ExecutionContext, SessionMetadataPayload
from engine.sessions import AgentSession, SessionManager
from engine.client import MockGenAIClient
from engine.bus import MessageBus
from pathlib import Path
from engine.constants import (
    SESSION_FILE_SUFFIX,
    SESSION_META_SUFFIX,
    CHECKPOINT_SEPARATOR,
    SCRATCH_DIR_NAME,
    TOOL_LOG_FILE_PREFIX,
    TOOL_LOG_FILE_SUFFIX
)
from engine.prompts.loader import PromptTemplateLoader
from engine.skills import SkillManager
from engine.context import DefaultAgentContextStrategy

# ANSI styling codes for high-fidelity premium console aesthetics
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_THOUGHT = "\033[38;5;39m"       # Sleek Neon Blue
C_ACTIVITY = "\033[38;5;220m"    # Gold/Yellow
C_TOOL_CALL = "\033[38;5;49m"    # Mint Cyan
C_SUCCESS = "\033[38;5;82m"      # Emerald Green
C_ERROR = "\033[38;5;196m"       # Vibrant Red
C_STEERING = "\033[38;5;135m"    # Soft Purple
C_CONFIRM = "\033[38;5;208m"     # Warning Orange
C_SUBAGENT = "\033[38;5;141m"    # Lavender

def format_sender(sender: str) -> str:
    """Format the sender's path with distinct colors for subagents."""
    parts = sender.split("/")
    if len(parts) == 1:
        return f"{C_BOLD}[{parts[0]}]{C_RESET}"
    else:
        parent = parts[0]
        children = "/".join(parts[1:])
        return f"{C_BOLD}[{parent}/{C_SUBAGENT}{children}{C_RESET}{C_BOLD}]{C_RESET}"

async def handle_telemetry_activity(msg: Dict[str, Any]) -> None:
    """Callback for rendering telemetry activities in a high-fidelity visual format."""
    sender = msg.get("sender", "unknown")
    sender_prefix = format_sender(sender)
    activity_type = msg.get("activity_type")
    
    if activity_type == "START":
        print(f"{sender_prefix} {C_ACTIVITY}▶ Starting execution:{C_RESET} {msg.get('query')}")
        if msg.get("system_prompt"):
            # Indent system prompt for cleaner look
            indented_sp = "\n".join(f"    {line}" for line in msg.get("system_prompt").splitlines())
            print(f"  {C_BOLD}System Prompt:{C_RESET}\n{indented_sp}\n")
            
    elif activity_type == "TURN_START":
        print(f"{sender_prefix} {C_ACTIVITY}🔄 Turn Cycle {msg.get('prompt_id')}{C_RESET}")
        
    elif activity_type == "TOOL_CALL_START":
        print(f"{sender_prefix} {C_TOOL_CALL}🛠️  Tool Call: {msg.get('name')}{C_RESET}")
        print(f"    {C_BOLD}Arguments:{C_RESET} {msg.get('args')}")
        
    elif activity_type == "TOOL_CALL_END":
        resp = msg.get("response", {})
        if "error" in resp:
            print(f"{sender_prefix} {C_ERROR}❌ Tool Response Error:{C_RESET} {resp.get('error')}")
        else:
            print(f"{sender_prefix} {C_SUCCESS}✅ Tool Response Success:{C_RESET}")
            # Format and show a preview of response
            resp_str = str(resp)
            if len(resp_str) > 300:
                resp_str = resp_str[:300] + " ... [TRUNCATED]"
            print(f"    {resp_str}")
            
    elif activity_type == "AWAITING_APPROVAL":
        print(f"{sender_prefix} {C_CONFIRM}⏳ {msg.get('msg')}{C_RESET} (Approval requested for {msg.get('tool')})")
        
    elif activity_type == "APPROVAL_DENIED":
        print(f"{sender_prefix} {C_ERROR}🚫 {msg.get('msg')}{C_RESET}")
        
    elif activity_type == "STEERING_QUEUED":
        print(f"{sender_prefix} {C_STEERING}📥 {msg.get('msg')}{C_RESET}")
        
    elif activity_type == "STEERING_INJECTED":
        print(f"{sender_prefix} {C_STEERING}⚙️  {msg.get('msg')}{C_RESET}")
        
    elif activity_type == "COMPRESSION":
        print(f"{sender_prefix} {C_CONFIRM}📦 {msg.get('msg')}{C_RESET}")
        
    elif activity_type == "RECOVERY":
        print(f"{sender_prefix} {C_CONFIRM}⚠️ {msg.get('msg')}{C_RESET}")
        
    elif activity_type == "RECOVERY_SUCCESS":
        print(f"{sender_prefix} {C_SUCCESS}🌟 {msg.get('msg')}{C_RESET}")
        
    elif activity_type == "END":
        print(f"{sender_prefix} {C_ACTIVITY}■ Finish execution: {msg.get('msg')}{C_RESET}")
        print(f"  {C_SUCCESS}{C_BOLD}Result:{C_RESET} {msg.get('final_result')}\n")
        
    elif activity_type == "ERROR":
        print(f"{sender_prefix} {C_ERROR}💥 Error:{C_RESET} {msg.get('msg')}")

async def handle_telemetry_thought(msg: Dict[str, Any]) -> None:
    """Callback for rendering real-time reasoning thought-stream chunks."""
    sender = msg.get("sender", "unknown")
    sender_prefix = format_sender(sender)
    text = msg.get("text", "")
    print(f"{sender_prefix} {C_THOUGHT}🧠 {text}{C_RESET}")

def make_confirmation_handler(bus):
    """Factory to create confirmation request handlers mapped back to the active message bus."""
    async def handler(msg: Dict[str, Any]) -> None:
        sender = msg.get("sender", "unknown")
        subagent = msg.get("subagent")
        display_sender = f"{sender}/{subagent}" if subagent else sender
        sender_prefix = format_sender(display_sender)
        
        tool_call = msg.get("toolCall", {})
        tool_name = tool_call.get("name")
        tool_args = tool_call.get("args")
        correlation_id = msg.get("correlationId")
        
        # Build prompt
        print(f"\n{C_BOLD}{'=' * 60}{C_RESET}")
        print(f"{sender_prefix} {C_CONFIRM}{C_BOLD}🔒 TOOL APPROVAL REQUESTED{C_RESET}")
        print(f"  {C_BOLD}Tool Name:{C_RESET} {tool_name}")
        print(f"  {C_BOLD}Arguments:{C_RESET} {tool_args}")
        print(f"{C_BOLD}{'=' * 60}{C_RESET}")
        
        # Use asyncio.to_thread to run standard blocking terminal input safely
        def prompt_user():
            try:
                sys.stdout.write(f"  {C_CONFIRM}Approve this action? (y/N): {C_RESET}")
                sys.stdout.flush()
                choice = sys.stdin.readline().strip().lower()
                return choice in ["y", "yes"]
            except Exception:
                return False

        confirmed = await asyncio.to_thread(prompt_user)
        print() # Add empty line for spacing
        
        # Publish response with correlationId back to the bus
        response_payload = {
            "type": "tool-confirmation-response",
            "correlationId": correlation_id,
            "confirmed": confirmed
        }
        await bus.publish(response_payload)
        
    return handler

async def main() -> None:
    parser = argparse.ArgumentParser(description="Async Decoupled Agent UI Harness")
    parser.add_argument("--query", type=str, default="Find and resolve TODOs inside {{target_dir}}", help="The primary query template.")
    parser.add_argument("--max-turns", type=int, default=5, help="Turn limit constraint.")
    parser.add_argument("--max-time-sec", type=int, default=30, help="Pausable deadline countdown seconds.")
    parser.add_argument("--requires-approval", action="store_true", default=True, help="Enable interactive tool confirmation prompt (default: True).")
    parser.add_argument("--no-approval", action="store_true", help="Disable interactive tool confirmation prompt.")
    parser.add_argument("--plan-mode", action="store_true", help="Enable plan mode restrictions.")
    parser.add_argument("--steering-test", action="store_true", help="Simulate a mid-flight user steering injection event.")
    
    args = parser.parse_args()
    
    # Resolve requires_approval
    requires_approval = False if args.no_approval else args.requires_approval
    
    # Establish workspace
    workspace = Path(os.getcwd()).resolve()
    package_root = Path(__file__).parent.resolve()
    templates_dir = package_root / "prompts" / "templates"
    system_agents_dir = package_root / "prompts" / "agents"

    # Instantiate discovery managers at the bootstrapper boundary
    loader = PromptTemplateLoader(templates_dir=templates_dir)
    skill_manager = SkillManager(search_paths=[workspace], skill_filenames=["SKILL.md"])
    agent_reg = AgentRegistry(
        search_paths=[workspace],
        agent_extensions=[".agent.yaml", ".agent.yml", ".md"],
        system_agents_dir=system_agents_dir
    )

    gemini_path = workspace / "GEMINI.md"
    context_filenames = ["GEMINI.md"] if gemini_path.exists() else ["GEMINI.md"]
    
    # Declare Agent Definition Config
    config = ExecutorAgentConfig(
        name="refactor_helper",
        max_turns=args.max_turns,
        max_time_seconds=args.max_time_sec,
        plan_mode=args.plan_mode,
        requires_approval=requires_approval,
        query=args.query
    )
    
    # Instantiate Strategy
    prompt_strategy = DefaultAgentContextStrategy(loader=loader)

    # 1. Instantiate the stateless parent Agent Executor with injected managers
    executor = LocalAgentExecutor(
        definition=config,
        context_strategy=prompt_strategy,
        skill_manager=skill_manager,
        agent_registry=agent_reg,
        context_filenames=context_filenames
    )
    
    # 2. Wire up the decoupled terminal subscribers on the parent MessageBus
    parent_bus = MessageBus()
    
    parent_bus.subscribe("telemetry:activity", handle_telemetry_activity)
    parent_bus.subscribe("telemetry:thought", handle_telemetry_thought)
    parent_bus.subscribe("tool-confirmation-request", make_confirmation_handler(parent_bus))
    
    # 3. Register native toolset
    executor.registry.register_tool(CompleteTaskTool())
    executor.registry.register_tool(ReadFileTool())
    executor.registry.register_tool(WriteFileTool())
    executor.registry.register_tool(ListDirectoryTool())
    executor.registry.register_tool(GlobTool())
    executor.registry.register_tool(GrepSearchTool())
    executor.registry.register_tool(ReplaceTool())
    
    # Enable and register Subagents delegation!
    executor.registry.register_tool(AgentTool(agent_reg, prompt_strategy, executor.registry))

    # Initialize Client and Sessions
    client = MockGenAIClient()
    storage_dir = workspace / ".replica_sessions"
    session_manager = SessionManager(
        storage_dir=storage_dir,
        session_suffix=SESSION_FILE_SUFFIX,
        meta_suffix=SESSION_META_SUFFIX,
        checkpoint_separator=CHECKPOINT_SEPARATOR,
        scratch_dir_name=SCRATCH_DIR_NAME,
        tool_log_prefix=TOOL_LOG_FILE_PREFIX,
        tool_log_suffix=TOOL_LOG_FILE_SUFFIX
    )
    await session_manager.ensure_storage_dir()
    
    session = AgentSession(
        session_id=f"sess-cli-{int(asyncio.get_event_loop().time() * 1000) % 10000}",
        chat_history=[],
        metadata=SessionMetadataPayload(name="cli_harness_session", query=args.query)
    )

    # 4. Construct dynamic ExecutionContext
    context = ExecutionContext(
        workspace_path=workspace,
        message_bus=parent_bus,
        remaining_depth=3,
        session=session,
        session_manager=session_manager,
        client=client
    )
    
    # 5. Trigger mid-flight user steering injection test asynchronously if flagged
    if args.steering_test:
        async def inject_delayed_steering():
            await asyncio.sleep(1.5) # Wait for some turns to start
            await executor.inject_steering("Check if there are duplicate or unused variables in target compute files.", context)
        asyncio.create_task(inject_delayed_steering())
        
    # Execute the engine asynchronously
    inputs = {
        "target_dir": workspace,
        "today": "2026-05-27"
    }
    
    print(f"\n{C_BOLD}============================================================{C_RESET}")
    print(f"{C_BOLD}🚀 ASYNC REPLICA ENGINE RUNNING (100% DECOUPLED EVENT BUS){C_RESET}")
    print(f"{C_BOLD}============================================================{C_RESET}\n")
    
    await executor.run(context, inputs)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{C_ERROR}Execution interrupted by user.{C_RESET}")
        sys.exit(1)
