# Production UDS Daemon & Protocol Implementation Plan (v0)

This document specifies the implementation strategy and architecture for the production Unix Domain Socket (UDS) Daemon (`engine/uds_server.py`). 

To facilitate rapid, safe development of the Neovim UI host, the system is designed around a **Production-First Protocol Loop** that integrates authentic engine components with a clearly bounded, temporary **`MockAgentExecutor`** driver. As the frontend matures, this mock driver will be swapped out for the real `LocalAgentExecutor` with zero structural changes to the server, socket, or Neovim client code.

---

## 1. Architectural Philosophy

We do not build a "mock server." We build the **actual production daemon structure** from day one. All connection handlers, session lifecycles, and event buses represent authentic production code. 

The only component that carries a "mock" prefix is the executor driver itself, which generates pre-baked semantic content to simulate agent reasoning and code updates.

```
+-------------------------------------------------------------------------+
|                          engine/uds_server.py                           |
|                                                                         |
|  +--------------------+      +---------------------------------------+  |
|  | Socket Connection  |      |       Session Registry & Memory       |  |
|  |      Listener      | <--> |  (Uses production SessionManager)     |  |
|  +--------------------+      +---------------------------------------+  |
|            ^                                     ^                      |
|            | Subscribes to                       | Spawns               |
|            v                                     v                      |
|  +--------------------+      +---------------------------------------+  |
|  | MessageBus Event   |      |          MockAgentExecutor            |  |
|  |    Translator      | <=== |      (Temporary simulated driver)     |  |
|  +--------------------+      +---------------------------------------+  |
+-------------------------------------------------------------------------+
```

---

## 2. Shared Core Module Integrations

The daemon directly imports and instantiates the production engine modules to enforce thread-safety, typing, and state persistence:

*   **`SessionManager` & `AgentSession` (`engine/sessions.py`):** Holds and drives the active chat contexts. Using the real session manager means v0 will generate real JSON files and `.meta.json` sidecar files in the user's workspace directory, making session listing and resuming fully operational out-of-the-box.
*   **`MessageBus` (`engine/bus.py`):** Acts as the asynchronous pub/sub router for session telemetry.
*   **`types.py` (`engine/types.py`):** Enforces data boundary schemas using frozen slotted Python dataclasses.

---

## 3. Component Map Breakdown

To establish clean boundaries, we decompose the server's architectural elements into a hierarchy of parent classes and their bound sub-components (methods):

### 3.1 Existing Components (To Be Integrated)
These are active Python modules inside `engine/` that we will leverage directly to drive state, persistence, and event dispatching:
*   **`SessionManager` (`engine/sessions.py`)** [Parent]
    *   *`create_session`* [Sub-Component]: Instantiates a fresh session context on disk.
    *   *`load_session`* [Sub-Component]: Resumes a past chat history from disk.
    *   *`list_sessions`* [Sub-Component]: Scans lightweight `.meta.json` sidecars to list available chats.
    *   *`save_session`* [Sub-Component]: Asynchronously serializes and flushes model states to disk.
*   **`AgentSession` (`engine/sessions.py`)** [Parent]
    *   *`append_turn`* [Sub-Component]: Saves user prompts directly into session history.
    *   *`bus`* [Sub-Component]: The native `MessageBus` instance bound to this specific session.
*   **`MessageBus` (`engine/bus.py`)** [Parent]
    *   *`subscribe`* [Sub-Component]: Attaches a listener coroutine to observe telemetry events.
    *   *`publish`* [Sub-Component]: Broadcasts an event to active subscribers.

### 3.2 New Components (To Be Built)
These are the network transport, protocol translation, and simulation logic boundaries:
*   **`UdsServer` (`engine/uds_server.py`)** [Parent]
    *   *`start`* [Sub-Component]: Binds to `/tmp/code_savant.sock` and establishes the `asyncio.start_unix_server` network loop.
    *   *`handle_connection`* [Sub-Component]: Coroutine that manages individual client read/write streams and newline-delimited frames.
    *   *`dispatch_request`* [Sub-Component]: Core router that parses incoming JSON-RPC 2.0 frames and invokes respective session manager actions.
    *   *`stream_session_telemetry`* [Sub-Component]: Long-running async task that listens to `session.bus` and translates internal engine `EventEnvelope` streams into socket JSON-RPC frames.
*   **`MockAgentExecutor` (`engine/mock_executor.py` or inline helper)** [Parent]
    *   *`run`* [Sub-Component]: Simulates the executor loop, publishing timed dummy events to `session.bus` and calling `SessionManager.save_session` at the end of each run to persist state to disk.
*   **`JsonRpcCodec` (`engine/uds_server.py` or new protocol utility)** [Parent]
    *   *`encode_response` / `encode_error`* [Sub-Component]: Formats success structures or protocol-compliant error codes (`-32700`, `-32600`, etc.) into newline-delimited payloads.
    *   *`encode_notification`* [Sub-Component]: Converts internal telemetry events into outgoing JSON-RPC notifications.

---

## 4. Multi-Session Concurrency & Task Management

Supporting multiple parallel active sessions requires robust resource isolation and leak-proof task management.

### 4.1 Client-Side Topology: Connection-per-Buffer
Rather than multiplexing all chats over a single socket connection—which requires complex message-routing loops inside Neovim Lua—we use a **Connection-per-Buffer** model:
*   Every time Neovim opens a new chat buffer, it spawns a **dedicated** socket connection to `/tmp/code_savant.sock` via Libuv (`vim.uv.new_pipe(false)`).
*   Any telemetry received on socket $A$ automatically writes directly to buffer $A$. Neovim does not have to parse `session_id` tags or maintain a buffer-routing registry in Lua.

### 4.2 Daemon-Side Concurrency: Asyncio Task Registry
On the Python daemon side, we leverage `asyncio`'s native concurrent task scheduling. The server maintains a global, thread-safe memory mapping of active sessions:

```python
@dataclass
class ActiveSessionState:
    session: AgentSession          # The real engine database/history container
    telemetry_task: asyncio.Task   # Task bridging session.bus -> socket StreamWriter
    executor_task: asyncio.Task | None = None  # Task running the active mock/agent executor
```

```python
class UdsServer:
    def __init__(self):
        # Global map of active session states
        self.active_sessions: dict[str, ActiveSessionState] = {}
```

### 4.3 Task Concurrency and Cancellation Lifecycle
When client socket connections shift states, the daemon manages their background tasks as follows:

```
[Client Connects (session/start)]
               |
               v
  1. Instantiate AgentSession (via SessionManager)
  2. Create Session MessageBus Subscription
  3. Spawn telemetry_task (Bridge Bus -> StreamWriter)
               |
[User Sends Prompt (session/send_prompt)]
               |
               v
  4. Is state.executor_task running? 
     - YES -> Cancel previous task to prevent race conditions.
     - NO  -> Proceed.
  5. Spawn state.executor_task = asyncio.create_task(MockAgentExecutor.run())
               |
[Client Disconnects (Socket Closed / BufWipeout)]
               |
               v
  6. Detect EOF on Connection Stream
  7. Cancel state.telemetry_task
  8. Cancel state.executor_task (if running)
  9. Garbage collect session ID from active_sessions memory map
```

---

## 5. Daemon Specification (`engine/uds_server.py`)

The server establishes a multi-session asynchronous server over a Unix Domain Socket.

### 5.1 Network Transport
*   **Socket Path:** `/tmp/code_savant.sock`
*   **API Framework:** Native Python `asyncio.start_unix_server` loop.
*   **Framing:** Newline character (`\n`) delimitation. The reading loop uses `StreamReader.readline()` to prevent buffer fragmentation.

### 5.2 Thread-Safe Session Memory Registry
The daemon maintains the `active_sessions` map as specified above to ensure that multiple clients connected simultaneously can run calculations in parallel without state pollution.

### 5.3 Connection-to-Bus Routing
When a client starts or resumes a session:
1.  The daemon invokes `SessionManager.load_session()` or `SessionManager.create_session()`.
2.  It spawns an asynchronous subscriber task (`telemetry_task`) that listens directly to the session's native `MessageBus`.
3.  As the bus publishes generic, strongly typed `EventEnvelope[T]` instances (such as `EventEnvelope[TelemetryThought]`), the subscriber translates them into their respective JSON-RPC 2.0 semantic frames and writes them directly down the client's `StreamWriter`.

---

## 6. Temporary Driver: `MockAgentExecutor`

To isolate the AI execution layer during frontend testing, we introduce a simulated executor engine that conforms to the production executor execution interface:

```python
class MockAgentExecutor:
    """
    Simulates the async execution loop of LocalAgentExecutor.
    Subscribes to a session's MessageBus and publishes a timed stream 
    of mock telemetry events based on keyword-matching in user prompts.
    Calls SessionManager.save_session(session) at the end of each run to flush state.
    """
    async def run(self, session: AgentSession, prompt: str) -> None:
        ...
```

### 6.1 Trigger Scenarios & Simulated Responses
The mock driver will inspect the incoming prompt and stream authentic, rich Markdown responses mimicking real-world AI behaviors:

| Scenario / Keyword | Internals Simulated | UX Components Tested in Neovim |
| :--- | :--- | :--- |
| **"think"** or **"thought"** | Large multi-line `TelemetryThought` blocks | Stress-tests client-side rendering of collapsed blocks and floating popup expansions. |
| **"edit"**, **"fix"**, or **"diff"** | Generates a unified diff using Python's native `difflib` and emits `collapsed_block` of type `"diff"`. | Exercises Neovim's **Side-by-Side Diff Review split view** natively. |
| **"error"** or **"fail"** | Emits a protocol failure event or abruptly disconnects. | Verifies Neovim UI's error recovery, disconnect indicators, and notification bars. |
| **Default (Any input)** | Standard conversational streaming via `TelemetryMessage` | Tests smooth incremental rendering of text streams and code-block syntax highlights. |

---

## 7. JSON-RPC 2.0 API Spec (Production Baseline)

All messages exchanged over `/tmp/code_savant.sock` use the standard JSON-RPC 2.0 format.

### 7.1 Requests (Neovim $\rightarrow$ Daemon)

#### `session/start`
Creates or resumes a session.
*   **Payload:**
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
Submits a user prompt to the executor.
*   **Payload:**
    ```json
    {
      "jsonrpc": "2.0",
      "method": "session/send_prompt",
      "params": {
        "session_id": "coder_3f92b7",
        "text": "Optimize the database query."
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
Closes the session and saves final history states to disk.
*   **Payload:**
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

### 5.2 Notifications (Daemon $\rightarrow$ Neovim Stream)

#### `telemetry/status`
*   **Payload:**
    ```json
    {
      "jsonrpc": "2.0",
      "method": "telemetry/status",
      "params": {
        "session_id": "coder_3f92b7",
        "status": "thinking" // Options: "idle", "thinking", "streaming", "completed", "error"
      }
    }
    ```

#### `telemetry/collapsed_block`
Delivers raw auxiliary content that should be collapsed by default. The payload contains only pure semantic properties, ensuring no presentation code exists in the daemon.
*   **Payload:**
    ```json
    {
      "jsonrpc": "2.0",
      "method": "telemetry/collapsed_block",
      "params": {
        "session_id": "coder_3f92b7",
        "id": "meta_thought_102",
        "type": "thought", // Options: "thought", "tool", "diff"
        "title": "Analyzed copy_tools context",
        "full_content": "Entering registry.py...\nFound copy_tools method...\nEvaluating shallow copy side-effects on concurrent subagents..."
      }
    }
    ```

#### `telemetry/message`
Streams active markdown output.
*   **Payload:**
    ```json
    {
      "jsonrpc": "2.0",
      "method": "telemetry/message",
      "params": {
        "session_id": "coder_3f92b7",
        "text": "The optimized query is as follows:\n\n```sql\n..."
      }
    }
    ```
