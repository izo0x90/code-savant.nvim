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
        context_repo = ContextSourceRepository(
            workspace_path=Path(os.getcwd()),
            tool_registry=ToolRegistry(),
            skill_manager=skill_manager,
            agent_registry=agent_registry,
            context_filenames=["GEMINI.md"]
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
