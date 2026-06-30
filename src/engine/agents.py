import asyncio
import aiofiles
import sys
import yaml
from pathlib import Path
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field, root_validator


class SubagentDefinition(BaseModel):
    """
    Pydantic model representing strict validation boundary for a subagent profile card.
    Adheres strictly to PEP 8 snake_case conventions while providing dual-compatibility
    for camelCase fields (internal runtime) and custom user options (YAML/Markdown).
    """
    name: str = Field(..., description="Unique slug name identifier of the subagent.")
    description: str = Field(..., description="High-level description of what this subagent does.")
    system_prompt: str = Field(..., description="The base system prompt used for instructing this subagent.")
    systemPrompt: str = Field(..., description="Compatible/deprecated camelCase system prompt representation.")
    kind: str = Field(default="local", description="Whether the subagent is local or remote.")
    tools: Optional[List[str]] = Field(default_factory=list, description="List of tools this subagent is allowed to use. Must explicitly include '*' to inherit all parent tools. Defaults to empty.")
    mcp_servers: Optional[Dict[str, Any]] = Field(default=None, description="Inline custom subagent MCP configurations.")
    mcpServers: Optional[Dict[str, Any]] = Field(default=None, description="Compatible camelCase MCP configurations.")
    model: str = Field(default="inherit", description="Specific model configuration to use (e.g., 'inherit' or a model name).")
    temperature: float = Field(default=1.0, description="Model temperature.")
    max_turns: int = Field(default=30, description="Maximum execution turns allowed.")
    maxTurns: int = Field(default=30, description="Compatible camelCase max turns.")
    max_time_seconds: int = Field(default=600, description="Maximum elapsed execution time in seconds.")
    maxTimeSeconds: int = Field(default=600, description="Compatible camelCase max time in seconds.")
    requires_approval: bool = Field(default=False, description="Whether execution actions require human-in-the-loop validation.")
    options: Dict[str, Any] = Field(
        default_factory=dict,
        description="Custom capability toggles, thinking budgets, or parameters overridden for this agent."
    )

    @root_validator(pre=True)
    def normalize_keys(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        """
        Pre-validator normalizing snake_case and camelCase fields from config cards
        to ensure both fields are fully synchronized, preventing boundary desync.
        """
        # 1. system_prompt / systemPrompt normalization
        if "systemPrompt" in values and "system_prompt" not in values:
            values["system_prompt"] = values["systemPrompt"]
        elif "system_prompt" in values and "systemPrompt" not in values:
            values["systemPrompt"] = values["system_prompt"]

        # 2. max_turns / maxTurns normalization
        if "maxTurns" in values and "max_turns" not in values:
            values["max_turns"] = values["maxTurns"]
        elif "max_turns" in values and "maxTurns" not in values:
            values["maxTurns"] = values["max_turns"]

        # 3. mcp_servers / mcpServers normalization
        if "mcpServers" in values and "mcp_servers" not in values:
            values["mcp_servers"] = values["mcpServers"]
        elif "mcp_servers" in values and "mcpServers" not in values:
            values["mcpServers"] = values["mcp_servers"]

        # 4. max_time_seconds / maxTimeSeconds / timeout_mins normalization
        if "timeout_mins" in values:
            values["max_time_seconds"] = int(values["timeout_mins"] * 60)
        elif "maxTimeSeconds" in values and "max_time_seconds" not in values:
            values["max_time_seconds"] = values["maxTimeSeconds"]
        elif "max_time_seconds" in values and "maxTimeSeconds" not in values:
            values["maxTimeSeconds"] = values["max_time_seconds"]

        # Set default values if fields remain unpopulated
        if "max_time_seconds" in values and "maxTimeSeconds" not in values:
            values["maxTimeSeconds"] = values["max_time_seconds"]
        elif "maxTimeSeconds" in values and "max_time_seconds" not in values:
            values["max_time_seconds"] = values["maxTimeSeconds"]

        return values


def parse_agent_config_content(content: str, filename: str) -> Dict[str, Any]:
    """
    Parses agent configuration content. Supports:
    1. Pure YAML files (e.g. *.agent.yaml)
    2. Markdown files with YAML frontmatter (e.g. *.md) starting with '---'
    """
    stripped_content = content.lstrip()
    if stripped_content.startswith("---"):
        # Split on '---' but limit to 2 splits (part 0 is before first '---', i.e. empty;
        # part 1 is frontmatter; part 2 is the markdown body / system prompt)
        parts = stripped_content.split("---", 2)
        if len(parts) < 3:
            raise ValueError(
                f"Malformed Markdown subagent card '{filename}': "
                f"Missing the closing '---' for the YAML frontmatter block."
            )
        frontmatter_raw = parts[1]
        system_prompt = parts[2].strip()

        try:
            profile = yaml.safe_load(frontmatter_raw)
        except Exception as e:
            raise ValueError(
                f"Failed to parse YAML frontmatter in '{filename}': {e}"
            ) from e

        if not isinstance(profile, dict):
            raise ValueError(
                f"YAML frontmatter in '{filename}' must parse to a dictionary, got {type(profile).__name__}."
            )

        profile["system_prompt"] = system_prompt
        return profile
    else:
        try:
            profile = yaml.safe_load(content)
        except Exception as e:
            raise ValueError(
                f"Failed to parse pure YAML agent card '{filename}': {e}"
            ) from e

        if not isinstance(profile, dict):
            raise ValueError(
                f"Pure YAML agent card '{filename}' must parse to a dictionary, got {type(profile).__name__}."
            )

        return profile


def _sync_find_agent_files(search_paths: List[Path], extensions: List[str]) -> List[Path]:
    discovered = []
    for path in search_paths:
        if not path.exists():
            continue
        try:
            for p in path.iterdir():
                if p.is_file():
                    if any(p.name.endswith(ext) for ext in extensions):
                        discovered.append(p)
        except Exception:
            continue
    return discovered


class AgentRegistry:
    """
    Registry holding available subagent definitions/profiles, supporting native async discovery.
    """
    def __init__(
        self,
        search_paths: List[Path],
        agent_extensions: List[str],
        system_agents_dir: Path
    ):
        """Strictly requires all search paths, config extensions, and system directories to be injected."""
        self._profiles: Dict[str, Dict[str, Any]] = {}
        self._sources: Dict[str, Optional[str]] = {}
        self.search_paths = [Path(p) for p in search_paths]
        self.system_agents_dir = Path(system_agents_dir)
        self.agent_extensions = agent_extensions

        self.load_system_profiles()

    def register_profile(self, name: str, definition: Dict[str, Any], source: Optional[str]) -> None:
        """
        Validates the raw definition against the SubagentDefinition Pydantic schema,
        checks for namespace collisions adhering to Rule 1 (fail loudly), and registers the profile.
        """
        # Parse and structurally validate definition using Pydantic
        validated = SubagentDefinition(**definition)
        
        if name in self._profiles:
            existing_source = self._sources.get(name)
            if existing_source is None:
                # Intended override of built-in system agent
                import sys
                print(f"[AgentRegistry] System default agent '{name}' is overridden by custom profile at '{source}'", file=sys.stderr)
            elif existing_source != source:
                # Custom vs. Custom conflict! Fail loudly with rich context!
                raise RuntimeError(
                    f"Agent registration collision! Subagent name '{name}' defined in '{source}' "
                    f"conflicts with already registered custom subagent from '{existing_source}'."
                )

        dumped = validated.model_dump()
        self._profiles[name] = dumped
        self._sources[name] = source

    def get_profile(self, name: str) -> Optional[Dict[str, Any]]:
        return self._profiles.get(name)

    def get_all_profiles(self) -> List[Dict[str, Any]]:
        return list(self._profiles.values())

    def load_system_profiles(self) -> None:
        """
        Synchronously loads default system subagents from the prompts/agents directory on startup.
        Loudly propagates exceptions upon system default card parsing or validation failure.
        """
        if not self.system_agents_dir.exists():
            return

        try:
            files = list(self.system_agents_dir.iterdir())
        except Exception as e:
            raise RuntimeError(
                f"CRITICAL: Failed to list system agents directory '{self.system_agents_dir}': {e}"
            ) from e

        for p in files:
            if any(p.name.endswith(ext) for ext in self.agent_extensions):
                try:
                    with open(p, mode="r", encoding="utf-8") as f:
                        content = f.read()

                    profile = parse_agent_config_content(content, p.name)
                    if "name" not in profile:
                        base = p.name
                        for ext in self.agent_extensions:
                            if base.endswith(ext):
                                base = base[:-len(ext)]
                                break
                        profile["name"] = base

                    self.register_profile(profile["name"], profile, source=None)
                except Exception as e:
                    # System default loader fails loudly on validation errors (Rule 1)
                    raise RuntimeError(
                        f"CRITICAL: Failed to load system default subagent profile from '{p}': {e}"
                    ) from e

    async def discover_agents(self) -> List[Dict[str, Any]]:
        """
        Asynchronously scans search paths for custom subagent card files (<name>.agent.yaml, .yml or .md).
        """
        agent_files = await asyncio.to_thread(
            _sync_find_agent_files,
            self.search_paths,
            self.agent_extensions
        )

        discovered = []
        for p in agent_files:
            try:
                profile = await self._load_agent_card_async(p)
                name = profile.get("name")
                if name:
                    self.register_profile(name, profile, source=str(p.resolve()))
                    discovered.append(self.get_profile(name))
            except Exception as e:
                # Fail loudly by logging detailed diagnostic message to stderr (Rule 1)
                print(f"[AgentRegistry] Error loading custom subagent card '{p}': {e}", file=sys.stderr)

        return discovered

    async def _load_agent_card_async(self, filepath: Path) -> Dict[str, Any]:
        """
        Asynchronously loads a single agent configuration card and parses its content.
        """
        async with aiofiles.open(filepath, mode="r", encoding="utf-8") as f:
            content = await f.read()

        profile = parse_agent_config_content(content, filepath.name)

        if "name" not in profile:
            base = filepath.name
            for ext in self.agent_extensions:
                if base.endswith(ext):
                    base = base[:-len(ext)]
                    break
            profile["name"] = base

        return profile

