import copy
import json
import aiofiles
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from mcp.types import Tool as McpTool
from engine.tools import BaseTool
from engine.config import ModelDefinition


class ModelRegistryError(Exception):
    """Raised when model registration, configuration loading, or validation fails."""
    def __init__(self, message: str, model_name: Optional[str] = None, details: Optional[str] = None):
        super().__init__(f"{message} (Model: {model_name})" if model_name else message)
        self.model_name = model_name
        self.details = details


class ToolRegistry:
    """
    Replicates ToolRegistry (Lines 312054-312127).
    Deals with tool lookup, filtering, parent cloning, wildcard mapping ('*'),
    and provides a lightweight integration of MCP servers using the python mcp library.
    """

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}
        # Stores discovered MCP tools as schema dictionary maps
        self._mcp_tools: Dict[str, McpTool] = {}

    def register_tool(self, tool: BaseTool) -> None:
        """Saves tool to active registry."""
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> Optional[BaseTool]:
        """Looks up a tool by name, returning a shallow copy to prevent state pollution."""
        tool = self._tools.get(name)
        if tool is not None:
            return copy.copy(tool)
        return None

    def get_all_tool_names(self) -> List[str]:
        """Lists active tool keys."""
        return list(self._tools.keys())

    def get_function_declarations(self) -> List[Dict[str, Any]]:
        """
        Translates all registered tools to LLM function declaration specs.
        """
        declarations = []
        for tool in self._tools.values():
            declarations.append(tool.get_declaration())
        
        # Merge any discovered MCP tools directly as schema declarations
        for m_name, m_tool in self._mcp_tools.items():
            declarations.append({
                "name": m_name,
                "description": m_tool.description,
                "parameters": m_tool.inputSchema
            })
            
        return declarations

    # ==========================================================================
    # Dynamic Mapping / Cloners matching legacy orchestrator
    # ==========================================================================

    def map_from_parent(self, parent_registry: "ToolRegistry", tool_config_list: List[str]) -> None:
        """
        Clones and transfers tools from a parent registry based on config tags (Lines 312083-312126).
        Resolves '*' wildcards.
        """
        if "*" in tool_config_list:
            # Clone all tools
            for t_name in parent_registry.get_all_tool_names():
                tool = parent_registry.get_tool(t_name)
                if tool:
                    self.register_tool(tool)
            return

        for name in tool_config_list:
            tool = parent_registry.get_tool(name)
            if tool:
                self.register_tool(tool)

    # ==========================================================================
    # Mock/Virtual Model Context Protocol (MCP) Server Discovery
    # ==========================================================================

    def mock_discover_mcp_server(self, server_name: str, tools_discovered: List[McpTool]) -> None:
        """
        Simulates maybeDiscoverMcpServer (Lines 312060-312071)
        Registers remote schemas using python-mcp structures into local declarations.
        """
        for tool in tools_discovered:
            # MCP tools inside sub-registries receive prefix names matching server:tool
            prefixed_name = f"{server_name}__{tool.name}"
            self._mcp_tools[prefixed_name] = tool


class ModelRegistryService:
    """
    Loads, caches, and resolves rich capability profiles for registered LLMs.
    Resides cleanly alongside ToolRegistry in src/engine/registry.py.
    All operations are fully asynchronous and leverage native Pydantic schema validation.
    """
    def __init__(self, bundled_path: Path, cache_path: Path):
        self.bundled_path = Path(bundled_path)
        self.cache_path = Path(cache_path)
        self._models: Dict[str, ModelDefinition] = {}

    async def initialize(self) -> None:
        """Loads baseline model specifications and overrides asynchronously."""
        # Rule 4: Early guard check for bundled models database
        if not self.bundled_path.is_file():
            raise ModelRegistryError("Bundled model registry database is missing.", model_name=None, details=str(self.bundled_path))

        # Parse static bundled models using Pydantic's native fast validation
        try:
            async with aiofiles.open(self.bundled_path, mode="r", encoding="utf-8") as f:
                content = await f.read()
            raw_specs = json.loads(content)
        except Exception as e:
            raise ModelRegistryError("Failed to parse bundled model specifications.", details=str(e)) from e

        # Validate specifications and populate database (Rule 6)
        for key, raw_spec in raw_specs.items():
            try:
                self._models[key] = ModelDefinition.model_validate(raw_spec)
            except Exception as e:
                raise ModelRegistryError(f"Validation failed for registered model '{key}'.", model_name=key, details=str(e)) from e

        # Load dynamic cached overrides if present
        cache_resolved_path = self.cache_path.expanduser()
        if cache_resolved_path.is_file():
            try:
                async with aiofiles.open(cache_resolved_path, mode="r", encoding="utf-8") as f:
                    cache_content = await f.read()
                cache_specs = json.loads(cache_content)
                for key, raw_spec in cache_specs.items():
                    self._models[key] = ModelDefinition.model_validate(raw_spec)
            except Exception as e:
                # Rule 1: Fail loudly if the local cache file is corrupted
                raise ModelRegistryError("Local model registry cache is corrupted.", details=str(e)) from e

    def get_model(self, name: str) -> ModelDefinition:
        """
        Retrieves a copy of the model definition from the registry.
        Fails loudly if the model is not registered (Rule 1).
        """
        model = self._models.get(name)
        if model is None:
            raise ModelRegistryError(f"Model '{name}' is not registered in the Model Registry.", model_name=name)
        
        # Rule 8: Returns a deep copy to prevent runtime modification leaking into the registry
        return copy.deepcopy(model)

    async def register_model_override(self, model: ModelDefinition) -> None:
        """
        Dynamically registers or overrides a model definition and serializes it asynchronously (Rule 13).
        """
        # Add to local cache map safely
        self._models[model.name] = model

        # Ensure parent cache directory exists safely (Rule 13: Idempotent initialization)
        cache_resolved_path = self.cache_path.expanduser()
        parent_dir = cache_resolved_path.parent
        parent_dir.mkdir(parents=True, exist_ok=True)

        # Rule 13: Serialize database idempotently and atomically
        temp_path = cache_resolved_path.with_suffix(".tmp")
        try:
            # Map dictionary representing registry state
            serializable = {k: v.model_dump() for k, v in self._models.items()}
            raw_json = json.dumps(serializable, indent=2)
            
            # Atomic file update: write through to temp asynchronously and rename
            async with aiofiles.open(temp_path, mode="w", encoding="utf-8") as f:
                await f.write(raw_json)
            os.replace(temp_path, cache_resolved_path)
        except Exception as e:
            # Rule 4: Clean up temp file on failure
            if temp_path.is_file():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            raise ModelRegistryError(f"Failed to write model override cache for '{model.name}'.", model_name=model.name, details=str(e)) from e
