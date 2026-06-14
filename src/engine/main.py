import os
import argparse
import asyncio
from mcp.types import Tool as McpTool

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
from engine.sessions import SessionManager, AgentSession
from engine.client import LiveGenAIClient, MockGenAIClient
from engine.registry import ToolRegistry
from engine.types import SessionMetadataPayload, ExecutorAgentConfig, ExecutionContext
from engine.context import (
    ContextSourceRepository,
    DefaultPromptInputs,
    DefaultAgentContextStrategy,
    PromptTemplateLoader,
    SkillManager
)
from engine.bus import MessageBus
from pathlib import Path
from engine.constants import (
    SESSION_FILE_SUFFIX,
    SESSION_META_SUFFIX,
    CHECKPOINT_SEPARATOR,
    SCRATCH_DIR_NAME,
    TOOL_LOG_FILE_PREFIX,
    TOOL_LOG_FILE_SUFFIX,
    DEFAULT_SOCKET_PATH
)


async def bootstrap_agent_cli() -> None:
    parser = argparse.ArgumentParser(description="Legacy Replica CLI Agent Loop (Python)")
    parser.add_argument("--query", type=str, default="Find and resolve TODOs inside {{target_dir}}", help="The primary prompt query template.")
    parser.add_argument("--max-turns", type=int, default=5, help="Turn limit constraint.")
    parser.add_argument("--max-time-sec", type=int, default=30, help="Pausable deadline countdown seconds.")
    parser.add_argument("--plan-mode", action="store_true", help="Force structural markdown plan file lockdown restrictions.")
    parser.add_argument("--steering-test", action="store_true", help="Simulate a mid-flight user steering injection event.")
    parser.add_argument("--requires-approval", action="store_true", help="Demonstrate pausable timer tracking during tool approval delays.")
    
    # Server / daemon mode parameters
    parser.add_argument("--server", action="store_true", help="Start the UDS JSON-RPC server.")
    parser.add_argument("--socket-path", type=str, default=None, help="The Unix Domain Socket path to bind to.")
    
    # Session Persistence & GenAI Client parameters
    parser.add_argument("--resume", "-r", type=str, nargs="?", const="latest", default=None, help="Resume previous session (latest, by index, or UUID).")
    parser.add_argument("--list-sessions", action="store_true", help="List available sessions for this project and exit.")
    parser.add_argument("--delete-session", type=str, default=None, help="Delete session by index or UUID.")
    parser.add_argument("--mock", action="store_true", default=True, help="Use mock model simulation (defaults to True; turn off for live API calls).")
    parser.add_argument("--no-mock", dest="mock", action="store_false", help="Disable mock simulation and make live API calls.")

    args = parser.parse_args()
    
    if args.server:
        from engine.uds_server import UdsServer
        socket_path = args.socket_path or DEFAULT_SOCKET_PATH
        server = UdsServer(socket_path=socket_path)
        print(f"Starting UDS Server on {socket_path}...")
        await server.start()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await server.stop()
        return
    
    # Establish workspace directory context
    workspace = Path(os.getcwd()).resolve()
    package_root = Path(__file__).parent.resolve()
    templates_dir = package_root / "prompts" / "templates"
    system_agents_dir = package_root / "prompts" / "agents"
    
    # Injected Storage Directory - neutral naming, no hardcoded global paths
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

    # Handle --list-sessions command
    if args.list_sessions:
        sessions = await session_manager.list_sessions()
        if not sessions:
            print("No active sessions found for this project.")
        else:
            print(f"Available sessions for this project ({len(sessions)}):")
            for idx, s in enumerate(sessions, 1):
                last_updated = s["metadata"].get("last_updated", "unknown")
                name = s["metadata"].get("name", "unnamed")
                query = s["metadata"].get("query", "")
                short_query = (query[:40] + "...") if len(query) > 40 else query
                print(f"  {idx}. {name} - '{short_query}' ({last_updated}) [{s['session_id']}]")
        return

    # Handle --delete-session command
    if args.delete_session:
        sessions = await session_manager.list_sessions()
        target_id = args.delete_session
        
        # Check if index is passed
        try:
            idx = int(target_id) - 1
            if 0 <= idx < len(sessions):
                target_id = sessions[idx]["session_id"]
        except ValueError:
            pass

        await session_manager.delete_session(target_id)
        print(f"Deleted session: {target_id}")
        return

    # Prepare Client and Setup History Resumption
    if args.mock:
        client = MockGenAIClient()
    else:
        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError("GEMINI_API_KEY environment variable is required to run in live mode.")
        client = LiveGenAIClient(api_key=api_key)
    session_id = f"sess-{int(asyncio.get_event_loop().time() * 1000) % 10000}"
    
    if args.resume is not None:
        sessions = await session_manager.list_sessions()
        if not sessions:
            print("No sessions available to resume.")
            return
        
        resume_target = args.resume
        if resume_target == "latest":
            target_sess = sessions[0]
        else:
            try:
                idx = int(resume_target) - 1
                if 0 <= idx < len(sessions):
                    target_sess = sessions[idx]
                else:
                    print(f"Invalid session index: {resume_target}")
                    return
            except ValueError:
                # Treat as UUID
                matched = [s for s in sessions if s["session_id"] == resume_target]
                if matched:
                    target_sess = matched[0]
                else:
                    print(f"No session found matching ID: {resume_target}")
                    return
        
        # Load the stateful session info
        session = await session_manager.load_session(target_sess["session_id"])
        session_id = session.session_id
        print(f"Resuming session [{session_id}] with {len(session.chat_history)} turns of history restored.")
    else:
        # Construct a fresh stateful session
        session = AgentSession(
            session_id=session_id,
            chat_history=[],
            metadata=SessionMetadataPayload(name="cli_session", query=args.query)
        )

    # Instantiate Strategy
    prompt_strategy = DefaultAgentContextStrategy()

    # Instantiate discovery managers at the bootstrapper boundary
    loader = PromptTemplateLoader(templates_dir=templates_dir)
    skill_manager = SkillManager(search_paths=[workspace], skill_filenames=["SKILL.md"])
    agent_registry = AgentRegistry(
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
        requires_approval=args.requires_approval,
        query=args.query
    )
    
    # Instantiate the local execution container
    executor = LocalAgentExecutor(
        definition=config,
        context_strategy=prompt_strategy,
        skill_manager=skill_manager,
        agent_registry=agent_registry,
        context_filenames=context_filenames
    )

    # Initialize tool registrations directly in executor registry
    executor.registry.register_tool(CompleteTaskTool())
    executor.registry.register_tool(ReadFileTool())
    executor.registry.register_tool(WriteFileTool())
    executor.registry.register_tool(ListDirectoryTool())
    executor.registry.register_tool(GlobTool())
    executor.registry.register_tool(GrepSearchTool())
    executor.registry.register_tool(ReplaceTool())
    
    executor.registry.register_tool(AgentTool(agent_registry, prompt_strategy, executor.registry))

    # Discover MCP Server tools
    mock_mcp_tools = [
        McpTool(
            name="generate_diagram",
            description="Generates an architectural structural diagram matching system layouts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "diagram_type": {"type": "string", "description": "The target layout, e.g. flow, architecture, sequential."}
                },
                "required": ["diagram_type"]
            }
        )
    ]
    executor.registry.mock_discover_mcp_server(server_name="design_server", tools_discovered=mock_mcp_tools)

    # Print assembled tool function declarations sent to the model API
    declarations = executor.registry.get_function_declarations()
    print("\n[Bootstrapper] Initialized Tool Declarations list:")
    for d in declarations:
        print(f"  - {d['name']}: {d['description']}")
    print("-" * 60)

    # Construct the stateless ExecutionContext
    bus = MessageBus()
    context = ExecutionContext(
        workspace_path=workspace,
        message_bus=bus,
        remaining_depth=3,
        session=session,
        session_manager=session_manager,
        client=client
    )

    # Trigger mid-flight user steering injection test if flagged
    if args.steering_test:
        await executor.inject_steering("Focus heavily on checking for security vulnerabilities or hardcoded keys.", context)

    # Execute
    inputs = {
        "target_dir": workspace,
        "today": "2026-05-27"
    }
    
    result_output = await executor.run(context, inputs)
    print("\n" + "=" * 60)
    print("FINAL DELIVERED AGENT OUTCOME:")
    print(result_output)
    print("=" * 60)


def main():
    asyncio.run(bootstrap_agent_cli())


if __name__ == "__main__":
    main()
