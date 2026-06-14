# Tool Comparison Matrix: Python Engine vs. Original JS/TS CLI

This document compares our Python agent engine (`engine/tools.py`) with the original JS/TS CLI bundle definitions. It highlights tool naming constants, schema declaration locations in the JS/TS source, and implementation status.

---

## 1. Tool Mapping & Source Code Audit

All standard tools inside the original JS CLI bundle are registered with specialized tool name string constants inside `gemini-cli-source/bundle/chunk-ECNYAST2.js`.

Below is the comparison matrix detailing where each tool is defined in the original source bundle and its current implementation status in our Python engine:

### 1.1 Complete Tool Matrix

| Tool Constant | JS Constant Definition (Line #) | JS Schema Declaration (Line #) | Tool Name | Python Status | Python Implementation Details |
| :--- | :---: | :---: | :--- | :---: | :--- |
| `GLOB_TOOL_NAME` | Line 43743 | Line 50074 | `"glob"` | **Implemented** | `GlobTool` inside `engine/tools.py` |
| `GREP_TOOL_NAME` | Line 43744 | Line 49340 | `"grep_search"` | **Implemented** | `GrepSearchTool` inside `engine/tools.py` |
| `LS_TOOL_NAME` | Line 43755 | Line 49476 | `"list_directory"` | **Implemented** | `ListDirectoryTool` inside `engine/tools.py` |
| `READ_FILE_TOOL_NAME` | Line 43757 | Line 49298 | `"read_file"` | **Implemented** | `ReadFileTool` inside `engine/tools.py` |
| `WRITE_FILE_TOOL_NAME` | Line 43763 | Line 49320 | `"write_file"` | **Implemented** | `WriteFileTool` inside `engine/tools.py` |
| `EDIT_TOOL_NAME` | Line 43765 | Line 49516 | `"replace"` | **Implemented** | `ReplaceTool` inside `engine/tools.py` (exact search/replace) |
| `COMPLETE_TASK_TOOL_NAME` | Line 43810 | *N/A* | `"complete_task"` | **Implemented** | `CompleteTaskTool` inside `engine/tools.py` |
| `SHELL_TOOL_NAME` | Line 43760 | Line 49176 | `"run_shell_command"` | *Planned* | Not yet implemented. Will run processes via `asyncio` subprocesses. |
| `WEB_SEARCH_TOOL_NAME` | Line 43770 | Line 49576 | `"google_web_search"` | *Planned* | Under development (refer to [search_tool_impl.md](file:///Users/izo/code/code_savant/dev_docs/search_tool_impl.md)). |
| `READ_MANY_FILES_TOOL_NAME`| Line 43778 | Line 49604 | `"read_many_files"` | *Planned* | To be added as a high-performance batch file reader. |
| `WEB_FETCH_TOOL_NAME` | Line 43776 | Line 49590 | `"web_fetch"` | *Planned* | To be added for fetching raw webpage HTML/content. |
| `MEMORY_TOOL_NAME` | Line 43783 | Line 49665 | `"save_memory"` | *Planned* | To write persistent facts to user profiles. |
| `WRITE_TODOS_TOOL_NAME` | Line 43772 | Line 49692 | `"write_todos"` | *Planned* | To track and persist intermediate agent execution goals. |
| `ASK_USER_TOOL_NAME` | Line 43790 | Line 49806 | `"ask_user"` | *Planned* | Prompts the user for blocking clarification. |
| `ACTIVATE_SKILL_TOOL_NAME` | Line 43788 | Line 49265 | `"activate_skill"` | *Planned* | Dynamically loads instruction prompts or subagent groups. |
| `ENTER_PLAN_MODE_TOOL_NAME`| Line 43802 | Line 49874 | `"enter_plan_mode"` | *Planned* | Gating/phase tool for planning modes. |
| `EXIT_PLAN_MODE_TOOL_NAME` | Line 43800 | Line 49238 | `"exit_plan_mode"` | *Planned* | Gating/phase tool to exit planning. |
| `UPDATE_TOPIC_TOOL_NAME` | Line 43805 | Line 49272 | `"update_topic"` | *Planned* | UI side-channel notification helper to set topic info. |
| `GET_INTERNAL_DOCS_TOOL_NAME`| Line 43786 | Line 49793 | `"get_internal_docs"`| *Planned* | Retreive local documentation index schemas. |
| `READ_MCP_RESOURCE_TOOL_NAME`| Line 43812 | Line 49889 | `"read_mcp_resource"`| *Planned* | Model Context Protocol resource retriever. |
| `LIST_MCP_RESOURCES_TOOL_NAME`| Line 43813 | Line 49903 | `"list_mcp_resources"`| *Planned* | Model Context Protocol resource lister. |

---

## 2. Highlighted Core Gaps & Implementation Blueprints

### 2.1 Terminal Execution (`run_shell_command`)
*   **JS Location:** Constant defined on `Line 43760`; Schema on `Line 49176`; Backend logic around `Line 286840` using pseudo-terminals via `node-pty`.
*   **Python Blueprint:**
    We will implement this in `engine/tools.py` using `asyncio.create_subprocess_exec` or `asyncio.create_subprocess_shell`. This will run the command under a strict timeout and stream stderr/stdout into our logging system.

### 2.2 Batch File Scanning (`read_many_files`)
*   **JS Location:** Constant on `Line 43778`; Schema on `Line 49604`.
*   **Python Blueprint:**
    Avoids expensive sequential roundtrips. We can expand `engine/tools.py` with `ReadManyFilesTool` accepting a list of file paths and reading them concurrently:
    ```python
    async def run(self, args: ReadManyFilesArgs, context: ExecutionContext):
        tasks = [self.read_one(path, context) for path in args.file_paths]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {"results": results}
    ```

### 2.3 Web Querying (`web_fetch`)
*   **JS Location:** Constant on `Line 43776`; Schema on `Line 49590`.
*   **Python Blueprint:**
    Fetches raw HTTP web content using `httpx` or `aiohttp` and extracts the text body, converting HTML tags to clean, readable Markdown layout.
