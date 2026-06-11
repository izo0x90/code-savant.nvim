# Legacy Gemini CLI (`@google/gemini-cli`) Architecture Map

This document serves as an exhaustive structural map of the core agent orchestration engine from the legacy Node.js/JavaScript version of the Gemini CLI. All source references are keyed against `/Users/izo/code/tmp/playground/gemini-cli-source/bundle/chunk-DN4XSYRG.js`.

---

## 1. Engine Core & Executor Lifecycle

The orchestration is powered by the `LocalAgentExecutor` class which instantiates, schedules, and handles active agent lifecycles.

| Component / Phase | Code Line Range | Rationale / Description |
| :--- | :--- | :--- |
| **`LocalAgentExecutor` Definition** | `Lines 312015–312039` | Defines properties, active `agentId` (random 6-character hex), context bridges, and the derived `executionContext` object. |
| **`LocalAgentExecutor.create()`** | `Lines 312051–312146` | Handles instantiation. Sets up localized `ToolRegistry`, `PromptRegistry`, and `ResourceRegistry`, translates wildcard configurations (e.g. `*` or `mcp:*`), discovers/activates MCP endpoints, registers the task-completion tool, and binds everything to a sandboxed subagent message bus. |
| **`run` / Workspace Scoping** | `Lines 313227–313247` | Enforces structural constraints like `workspaceDirectories` lockouts, JIT session memory extraction, and context-dependent file sandboxes before entering the running loop. |
| **`runInternal` Main Loop** | `Lines 313248–313467` | Prepares tools list, creates active chat instance, configures deadline timers, manages turn counters, checks termination checkpoints, processes mid-flight user-steering injection queues, and controls overall execution. |

---

## 2. Context Building & System Prompt Formulation

The system prompt is dynamically compiled at runtime rather than static. The orchestrator embeds external system-level environment realities, memory stores, and interaction boundaries into the system block.

| Feature / Logic Section | Code Line Range | Key Orchestration Details |
| :--- | :--- | :--- |
| **History Template Application** | `Lines 312978–312995` (`applyTemplateToInitialMessages`) | Formats double-brace substitution tags (e.g. `{{query}}`, `{{cliVersion}}`) on initial messages before the session starts. |
| **`buildSystemPrompt` Orchestrator** | `Lines 312916–312977` | Combines base system prompts with injected context sections. |
| **Skills Injector** | `Lines 312923–312937` | If `activate_skill` tool is enabled, dynamically renders current local active user skill schemas into the system block. |
| **Memory Injector** | `Lines 312938–312943` | Appends core persistent system memory instruction strings using `renderUserMemory`. |
| **Environment Context Injector** | `Lines 312944–312948` | Extracts the layout structure of local directory trees via `getDirectoryContextString` and appends it to keep paths absolute and real. |
| **Mode Lockdowns & Execution Constraints** | `Lines 312949–312957` | If operating in **Plan Mode**, hard-restricts edit/write operations exclusively to markdown plans under the `/plans/` path, appending strict security instructions. |
| **Non-Interactive Boundary Rules** | `Lines 312958–312976` | Injects runtime guidelines including: <br>• Strict non-interactive constraint (cannot ask user clarifying questions).<br>• Enforced usage of absolute paths based on Environment Context.<br>• Obligation to address and adjust strategies upon user-tool rejections.<br>• Non-negotiable mandate to finalize execution using `complete_task` instead of just halting. |

---

## 3. Turn Execution, Tool Call Processing & Graceful Recovery

The loop iterates by feeding the model history, receiving tool choices, safely dispatching those operations, and deciding when to finalize.

| Orchestration Stage | Code Line Range | Logic Description |
| :--- | :--- | :--- |
| **Turn Dispatcher** | `Lines 312171–312227` (`executeTurn`) | Compresses chat tokens if thresholds are breached, executes pre-turn callbacks, issues inference calls (`callModel`), intercepts cancel/timeout aborts, processes function calls, and parses success or exit outcomes. |
| **Tool Execution Handler** | `Lines 312724–312889` (`processFunctionCalls`) | Parses argument payloads, verifies that requested operations exist within the agent's authorized `allowedToolNames` set, delegates tasks through `scheduleAgentTools`, publishes execution status to the bus, and parses tool output. |
| **Protocol Violation Catch** | `Lines 312188–312199` | Catches cases where the LLM stopped generating tool calls but forgot to finalize. Halts execution and emits a `Protocol Violation` error. |
| **Graceful Recovery Turn** | `Lines 312254–312319` (`executeFinalWarningTurn`) | If limits (max turns or deadline time) are breached, the agent is granted a **60-second grace period** (`GRACE_PERIOD_MS = 60 * 1000`). It receives a target warning ("You have exceeded constraints... You have one final chance...") coercing it to issue a best-effort `complete_task` instead of terminating with empty-handed failure. |

---

## 4. Built-in Tools Registry

Each tool extends `BaseDeclarativeTool` and maps custom inputs to background actions.

| Tool Name | Class Declaration | Code Line Target | Purpose / Scope |
| :--- | :--- | :--- | :--- |
| `complete_task` | `CompleteTaskTool` | `Lines 311903–312008` | Submits structured findings or text output, ending the turn loop. |
| `view_file` / `read_file` | `ReadFileTool` | `Lines 276819–276886` | Extracts text from a local path. |
| `read_many_files` | `ReadManyFilesTool` | `Lines 352698–352750` | Performs bulk read of multiple local target paths in parallel. |
| `edit_file` / `replace_file` | `EditTool` | `Lines 272663–273310` | Edits specific segments or ranges of local files. |
| `write_file` | `WriteFileTool` | `Lines 273311–273400` | Creates a new file with specified content. |
| `list_dir` | `LSTool` | `Lines 276205–276818` | Returns directory listings and recursive file counts. |
| `grep_search` | `GrepTool` / `RipGrepTool` | `Lines 284565–285247` | Finds exact regex matches within directories or specific files. |
| `glob` | `GlobTool` | `Lines 285248–285603` | Searches files matching glob expressions. |
| `execute_command` | `ShellTool` | `Lines 288634–289000` | Spawns a shell task to run local developer terminal commands. |
| `web_fetch` / `web_search` | `WebFetchTool` / `WebSearchTool` | `Lines 294429–294645` | Queries search engines and downloads content parsed to markdown. |
