import copy
from typing import Any, Dict, List, Optional
from mcp.types import Tool as McpTool
from engine.tools import BaseTool


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
