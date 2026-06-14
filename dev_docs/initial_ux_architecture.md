# Initial UX & Buffer Architecture Specification (v0)

This document specifies the initial User Experience (UX) and buffer-management architecture for the Neovim-hosted frontend of Code Savant. This specification serves as the design baseline for the v0 implementation of the client-server interface.

---

## 1. System Topology Overview

The frontend follows a headless, decoupled client-server architecture. All core state, context optimization, and model orchestration are offloaded to a background Python daemon, while Neovim operates as a lightweight, non-blocking UI host.

```mermaid
graph LR
    subgraph Neovim (Client Host)
        UI[Unified Buffer] -- JSON-RPC 2.0 --> Libuv[Libuv Socket Client]
    end
    subgraph Python Daemon (Server Core)
        UDS[UDS Server] --> Exec[Executor Tasks]
        Exec --> Sess[Stateful Sessions]
    end
    Libuv -- /tmp/code_savant.sock --> UDS
```

---

## 2. Communication Protocol (JSON-RPC 2.0)

All communications are newline-delimited (`\n`) JSON-RPC 2.0 frames exchanged over the Unix Domain Socket `/tmp/code_savant.sock`.

### 2.1 Session Lifecycle Methods (Client to Server)

#### `session/start`
Initiates a stateful session on the background daemon for a specific workspace context.
*   **Request:**
    ```json
    {
      "jsonrpc": "2.0",
      "method": "session/start",
      "params": {
        "workspace_path": "/Users/izo/code/my_project",
        "agent_profile": "coder"
      },
      "id": 1
    }
    ```
*   **Response:**
    ```json
    {
      "jsonrpc": "2.0",
      "result": {
        "session_id": "coder_3f92b7",
        "status": "active"
      },
      "id": 1
    }
    ```

#### `session/send_prompt`
Transmits a new user input block to the executor.
*   **Request:**
    ```json
    {
      "jsonrpc": "2.0",
      "method": "session/send_prompt",
      "params": {
        "session_id": "coder_3f92b7",
        "text": "Explain how the registry copies tools."
      },
      "id": 2
    }
    ```
*   **Response:**
    ```json
    {
      "jsonrpc": "2.0",
      "result": {
        "status": "queued"
      },
      "id": 2
    }
    ```

#### `session/close`
Informs the daemon to save, close, and clean up memory resources associated with the session.
*   **Request:**
    ```json
    {
      "jsonrpc": "2.0",
      "method": "session/close",
      "params": {
        "session_id": "coder_3f92b7"
      },
      "id": 3
    }
    ```

---

### 2.2 Telemetry Streams (Server to Client Notifications)

The server streams execution telemetry to the active client as asynchronous, unrequested notifications.

#### `telemetry/status`
Broadcasts state changes of the active executor task.
*   **Payload:**
    ```json
    {
      "jsonrpc": "2.0",
      "method": "telemetry/status",
      "params": {
        "session_id": "coder_3f92b7",
        "status": "thinking" // Options: idle, thinking, streaming, completed, error
      }
    }
    ```

#### `telemetry/thought`
Streams chunked reasoning thoughts before or during generation. These thoughts should be rendered in a dimmed highlight group.
*   **Payload:**
    ```json
    {
      "jsonrpc": "2.0",
      "method": "telemetry/thought",
      "params": {
        "session_id": "coder_3f92b7",
        "text": "Locating registry.py inside the workspace... "
      }
    }
    ```

#### `telemetry/message`
Streams markdown content chunks of the agent's conversational response. **Note: Conversational responses are never truncated or collapsed; they always stream in full Markdown directly into the active buffer.**
*   **Payload:**
    ```json
    {
      "jsonrpc": "2.0",
      "method": "telemetry/message",
      "params": {
        "session_id": "coder_3f92b7",
        "text": "The `ToolRegistry` performs a shallow copy using..."
      }
    }
    ```

#### `telemetry/collapsed_block`
Delivers a complete, raw auxiliary event (thoughts, tool execution traces, or code changes) to be collapsed by default. The payload contains only pure, presentation-agnostic semantic properties. All icons, layout coloring, and user-facing key hints are strictly the responsibility of the client.
*   **Payload:**
    ```json
    {
      "jsonrpc": "2.0",
      "method": "telemetry/collapsed_block",
      "params": {
        "session_id": "coder_3f92b7",
        "id": "meta_thought_102",
        "type": "thought", // Options: "thought", "tool", "diff"
        "title": "Analyzed copy_tools context", // Raw text summary (no icons or hints)
        "full_content": "Entering registry.py...\nFound copy_tools method...\nEvaluating shallow copy side-effects on concurrent subagents...\nConfirmed copying is safe."
      }
    }
    ```

#### `telemetry/tool_call`
Signals that a tool execution has commenced. Can be used for inline feedback.
*   **Payload:**
    ```json
    {
      "jsonrpc": "2.0",
      "method": "telemetry/tool_call",
      "params": {
        "session_id": "coder_3f92b7",
        "tool_name": "read_file",
        "arguments": { "path": "engine/registry.py" }
      }
    }
    ```

---

## 3. Neovim Client Buffer Design

To balance simplicity, robustness, and powerful editing capabilities, the frontend utilizes a **Unified Buffer** design.

```
+---------------------------------------------------------+
| # Code Savant Chat (coder_3f92b7)                       |
|                                                         |
| 󰭻 USER                                                  |
| Explain how the registry copies tools.                  |
|                                                         |
| 󱚥 CODE SAVANT                                           |
| 󰭻 [Thought: Analyzed copy_tools context]                |
|                                                         |
| The `ToolRegistry` uses shallow copying to ensure...    |
|                                                         |
| ─────────────────────────────────────────────────────── |
| 󰭻 USER                                                  |
| [Active Prompt Input Area is here. Fully editable.]     |
+---------------------------------------------------------+
```

### 3.1 Buffer Configuration
*   **Filetype:** `code_savant_chat` (inherits from `markdown` for automated nested Treesitter syntax highlighting).
*   **Buffer Options:**
    *   `buftype = "nofile"` (Prevents physical file saving triggers).
    *   `bufhidden = "hide"` (Persists the session buffer in memory if its window is closed).
    *   `swapfile = false` (Disables swapfile creation).

### 3.2 Auxiliary Event Caching ("Dumb Client, Smart Server")
To eliminate complex string parsing, regular expression evaluations, or diff formatting inside Neovim Lua, the client uses a flat cache routing system:

1.  **State Isolation:**
    Every session buffer manages a flat key-value state cache inside the Lua runtime:
    ```lua
    session.cache = {} -- Maps metadata_id -> full_content string
    ```
2.  **O(1) Logging:**
    When Neovim receives a `telemetry/collapsed_block` notification:
    *   It caches the `full_content` under the given `id` key.
    *   The Lua client formats the line by appending theme-configured Nerd Font icons (e.g. `󰭻` for thought, `󰏪` for tool) to the raw `title` parameter, and writes it directly to the buffer.
    *   It binds a Neovim `extmark` to that newly written line, passing the block `id` in the extmark's metadata.

### 3.3 Configurable Expand Strategies & Keyboard Overrides
Because the raw content is isolated in the Lua cache, the user can configure a default behavior while retaining the power to trigger any alternative display strategy on-demand using modifier shortcuts:

*   **Default Action (`<Enter>` or `K`):**
    Executes the default strategy configured in the user's plugin setup (e.g., `expand_strategy = "inplace"`).
*   **Explicit Strategy Overrides (Global Hotkeys):**
    Regardless of the configured default, dedicated keys allow the user to choose the optimal rendering container on the fly:
    *   **`go` (or `<leader>i`):** Forces **`inplace`** expansion. Toggle the lines in-buffer directly beneath the summary line.
    *   **`gs` (or `<leader>s`):** Forces **`split`** expansion. Opens the detailed text in a vertical/horizontal buffer split.
    *   **`gp` (or `<leader>p`):** Forces **`popup`** expansion. Renders the detailed text in a temporary floating popup under the cursor.

#### Expand Strategy Implementations:
*   **`inplace`:**
    Inserts the lines of `full_content` directly beneath it in the active buffer. Pressing the toggle key again deletes the lines. Range tracking is managed using a pair of extmarks, ensuring perfect line-shift recalculations without coordinate math.
*   **`split`:**
    Opens a vertical or horizontal split buffer and loads the content. Allows the user to read or copy details while leaving the main chat view intact.
*   **`popup`:**
    Opens a styled, temporary overlay window under the cursor, which auto-closes on cursor movement.

### 3.4 Virtual Text Decorations (Extmarks)
To create a clean visual layout without polluting raw buffer text, we use Neovim's `extmarks` API in a dedicated namespace (`code_savant_decorations`):

*   **Headers:** Visual markers like `󰭻 User` and `󱚥 Code Savant` are drawn as `virt_lines` directly above their respective sections. Because they are virtual, the cursor skips over them and they cannot be deleted.
*   **Separators:** Visual borders (`───`) are drawn dynamically across the window width to clearly delineate individual chat turns.

### 3.5 Non-Editable History (Locked Regions)
While the user is free to search (`/`), copy (`y`), and move through the entire chat history, they must be prevented from editing or deleting past turns.

```
  Line 1    | [Read-Only History Region]
            | User can move, select, search, and copy text here.
            | Any keyboard insertion or deletion gets rejected.
  Line 42   | ---------------------------------- [prompt_start_line]
  Line 43   | [Fully Writable Active Input Region]
  Line 44   | Cursor is here. Direct writing, pasting, editing.
```

*   **Mechanism:**
    1.  The client tracks a buffer-local variable `prompt_start_line` indicating the line number where the current input starts.
    2.  An active buffer listener is registered via `vim.api.nvim_buf_attach` listening to the `on_bytes` event.
    3.  When a modification is attempted, the listener evaluates the target line range.
    4.  If the modification target falls on any line index strictly less than `prompt_start_line` (`line < prompt_start_line`), the transaction is aborted and a warning is printed to the command bar.
    5.  Once a prompt is sent, `prompt_start_line` is updated to point below the newly appended agent response, locking the old prompt and response regions permanently.

---

## 4. Client-Side Session & Connection Lifecycle

Managing multiple concurrent conversations is kept simple and leak-proof by mapping socket connections directly to the lifecycle of individual Neovim buffers.

### 4.1 Connection-per-Buffer Mapping
Instead of writing complex message-multiplexing and dispatch tables in Lua, Neovim establishes a **unique Unix Domain Socket (UDS) connection for each active chat buffer**:
*   Every chat buffer maintains its own dedicated Libuv pipe: `local client = vim.uv.new_pipe(false)`.
*   The socket stream handle is bound directly as a buffer-local state variable: `vim.b[bufnr].savant_socket = client`.
*   Data packets received on connection $A$ are routed and appended *exclusively* to buffer $A$. This ensures perfect concurrency without complex routing code.

### 4.2 Lifecycle Event Pipeline

```
  [Buffer Created] ──────> 1. Open Libuv Pipe & Connect to /tmp/code_savant.sock
                          2. Send JSON-RPC "session/start"
                          3. Cache "session_id" in vim.b[bufnr].savant_session_id
                               │
                               v
  [Prompt Submitted] ────> 4. Read text from prompt_start_line down
                          5. Send JSON-RPC "session/send_prompt" with cached ID
                               │
                               v
  [Buffer Wiped out] ────> 6. Send JSON-RPC "session/close"
                          7. Call socket:close() to release file descriptors
```

1.  **Buffer Allocation & Connect (`BufNew` / command trigger):**
    *   The user issues `:CodeSavantChat` $\rightarrow$ Spawns a new scratch buffer of filetype `code_savant_chat`.
    *   The client opens the Libuv pipe and connects asynchronously to `/tmp/code_savant.sock`.
    *   Once the connection is established, the client transmits `session/start` containing the current working directory.
    *   On receiving the response, the client binds the returned `session_id` directly to the buffer: `vim.b[bufnr].savant_session_id = result.session_id`.
2.  **Prompt Transmission (`session/send_prompt`):**
    *   When the user presses `<C-Enter>` inside the buffer's active input region:
    *   The client reads the lines from `prompt_start_line` to the end of the buffer.
    *   It clears the active input region and sends `session/send_prompt` containing the `session_id` and the raw prompt string.
3.  **Wipeout & Resource Teardown (`BufWipeout`):**
    *   A buffer-local autocommand binds to the `BufWipeout` event.
    *   When the user wipes or deletes the chat buffer (`:bd`), the autocommand:
        1. Transmits a quick `session/close` payload to the daemon (instructing it to flush and archive session state).
        2. Closes the Libuv pipe via `client:close()`, cleanly releasing the client's file descriptor and notifying the daemon's read stream of the EOF.

