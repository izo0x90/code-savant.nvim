import asyncio
import os
import shutil
import pytest
from pathlib import Path
from engine.prompts.loader import PromptTemplateLoader
from engine.skills import SkillManager
from engine.agents import AgentRegistry
from engine.sessions import AgentSession
from engine.context import (
    ContextSourceRepository,
    DefaultPromptInputs,
    DefaultAgentContextStrategy
)
from engine.memory import HierarchicalContextManager
from engine.types import SessionMetadataPayload


async def main():
    print("--- STARTING SYSTEM PROMPT & DISCOVERY VERIFICATION ---")

    # 1. Setup temporary directories for dynamic discovery tests
    temp_skills_dir = "temp_skills_search_path"
    temp_agents_dir = "temp_agents_search_path"
    os.makedirs(temp_skills_dir, exist_ok=True)
    os.makedirs(temp_agents_dir, exist_ok=True)

    # Write a mock SKILL.md
    sample_skill_dir = os.path.join(temp_skills_dir, "test_skill")
    os.makedirs(sample_skill_dir, exist_ok=True)
    skill_content = """---
name: mock-sql-coder
description: Synthesizes high-performance PostgreSQL queries.
---
# Mock SQL Coder instructions
Ensure all table names are quoted.
"""
    with open(os.path.join(sample_skill_dir, "SKILL.md"), "w") as f:
        f.write(skill_content)

    # Write a mock custom subagent card
    agent_content = """
name: doc-helper
description: Expert in scanning and writing sphinx documentation.
systemPrompt: "You are a sphinx-doc expert. Write complete sphinx docstrings. Task: {{query}}"
maxTurns: 5
maxTimeSeconds: 30
"""
    with open(os.path.join(temp_agents_dir, "doc-helper.agent.yaml"), "w") as f:
        f.write(agent_content)

    try:
        # Initialize loaders and discovery managers
        templates_dir = Path("src/engine/prompts/templates")
        system_agents_dir = Path("src/engine/prompts/agents")
        loader = PromptTemplateLoader(templates_dir=templates_dir)
        skill_manager = SkillManager(search_paths=[Path(temp_skills_dir)], skill_filenames=["SKILL.md"])
        agent_registry = AgentRegistry(
            search_paths=[Path(temp_agents_dir)],
            agent_extensions=[".agent.yaml", ".agent.yml", ".md"],
            system_agents_dir=system_agents_dir
        )

        # Discover dynamically
        discovered_skills = await skill_manager.discover_skills()
        discovered_agents = await agent_registry.discover_agents()

        print(f"Discovered Skills: {[s['name'] for s in discovered_skills]}")
        print(f"Discovered Custom Subagents: {[a['name'] for a in discovered_agents]}")

        # Initialize the Context Source Repository
        from engine.registry import ToolRegistry
        memory_manager = HierarchicalContextManager(
            workspace_path=Path(os.getcwd()),
            context_filenames=["GEMINI.md"],
            global_context_dir=Path("~/.gemini").expanduser(),
            max_depth=5
        )
        context_repo = ContextSourceRepository(
            workspace_path=Path(os.getcwd()),
            tool_registry=ToolRegistry(),
            skill_manager=skill_manager,
            agent_registry=agent_registry,
            memory_manager=memory_manager
        )

        strategy = DefaultAgentContextStrategy(loader=loader)

        # Build prompt in 'plan' mode
        plan_inputs = DefaultPromptInputs(
            is_interactive=True,
            approval_mode="plan"
        )
        plan_session = AgentSession(
            session_id="test-plan-sess",
            chat_history=[],
            metadata=SessionMetadataPayload()
        )
        plan_context = await strategy.compile_context(
            inputs=plan_inputs,
            context_repo=context_repo,
            history=plan_session.chat_history
        )
        plan_prompt = plan_context.system_instruction

        print("\n--- PLAN PROMPT PREVIEW (First 500 chars) ---")
        print(plan_prompt[:500])
        print("------------------------------------------")

        # Build prompt in 'yolo' mode
        yolo_inputs = DefaultPromptInputs(
            is_interactive=False,
            approval_mode="yolo"
        )
        yolo_session = AgentSession(
            session_id="test-yolo-sess",
            chat_history=[],
            metadata=SessionMetadataPayload()
        )
        yolo_context = await strategy.compile_context(
            inputs=yolo_inputs,
            context_repo=context_repo,
            history=yolo_session.chat_history
        )
        yolo_prompt = yolo_context.system_instruction

        print("\n--- YOLO PROMPT PREVIEW (First 500 chars) ---")
        print(yolo_prompt[:500])
        print("------------------------------------------")

        # Verify that subagent and skill are injected into XML blocks
        assert "doc-helper" in plan_prompt, "doc-helper subagent should be in prompt"
        assert "mock-sql-coder" in plan_prompt, "mock-sql-coder skill should be in prompt"
        assert "Plan Mode" in plan_prompt or "Active Approval Mode: Plan" in plan_prompt, "Plan mode constraints should be rendered"
        assert "YOLO" in yolo_prompt, "YOLO mode elements should be rendered"

        print("\n✅ Verification Successful: All checks passed with 100% async integrity!")

    finally:
        # Clean up temporary test directories
        shutil.rmtree(temp_skills_dir, ignore_errors=True)
        shutil.rmtree(temp_agents_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())


@pytest.mark.asyncio
async def test_prompt_system_all():
    await main()


@pytest.mark.asyncio
async def test_hierarchical_context_manager(tmp_path: Path):
    # Setup temporary directory structures
    global_dir = tmp_path / "global_gemini"
    global_dir.mkdir()
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    # Create dummy .git to resolve project root correctly
    (global_dir / ".git").mkdir()
    (workspace_dir / ".git").mkdir()

    # Create recursive sub-files inside workspace
    sub_file_1 = workspace_dir / "file1.txt"
    sub_file_1.write_text("Hello from file1! Inline import: @file2.txt")
    
    sub_file_2 = workspace_dir / "file2.txt"
    sub_file_2.write_text("Hello from file2! Inline code mention: `@file1.txt` and circular ref: @file1.txt")

    # Create memory context files
    global_mem = global_dir / "GEMINI.md"
    global_mem.write_text("Global config guidelines. Import: @file1.txt")

    project_mem = workspace_dir / "GEMINI.md"
    project_mem.write_text("Project configuration. Import: @file1.txt")

    # Instantiate our pure context manager
    manager = HierarchicalContextManager(
        workspace_path=workspace_dir,
        context_filenames=["GEMINI.md"],
        global_context_dir=global_dir,
        max_depth=5
    )

    result = await manager.load_hierarchical_context()

    # Asserts
    assert "<global_context>" in result
    assert "<project_context>" in result
    assert "Global config guidelines" in result
    assert "Project configuration" in result
    assert "Hello from file1!" in result
    assert "Hello from file2!" in result
    # Mentions inside code blocks (`@file1.txt`) must NOT be resolved/inlined
    assert "`@file1.txt`" in result
    # Circular imports are detected and prevented (file1 already processed)
    assert "<!-- File already processed: file1.txt -->" in result or "<!-- File already processed" in result

