--- @class CodeSavantConnection
--- @field pipe any Libuv pipe handle
--- @field bufnr integer The buffer number associated with this connection
--- @field socket_path string The Unix Domain Socket path
--- @field accumulator string The raw bytes accumulated so far from the socket stream
--- @field listeners function[] Callback functions registered to receive parsed messages
--- @field session_id? string The optional active session identifier

--- @class CodeSavantNetwork
local Network = {}

-- Centralized constants (Guideline 2)
Network.CONSTANTS = {
  JSONRPC_VERSION = "2.0",
  METHODS = {
    SEND_PROMPT = "session/send_prompt",
  },
  DELIMITER = "\n",
}

-- Registry to maintain active socket connections per buffer (Guideline 6, 13)
--- @type table<integer, CodeSavantConnection>
Network._connections = {}

-- Stateful request ID sequence
local last_request_id = 0

--- Generate an incremental unique request ID
--- @return integer
function Network._next_id()
  last_request_id = last_request_id + 1
  return last_request_id
end

--- Log error message safely from any (including fast event) context
--- @param msg string
local function log_error(msg)
  if vim.in_fast_event() then
    vim.schedule(function()
      vim.api.nvim_err_writeln(msg)
    end)
  else
    vim.api.nvim_err_writeln(msg)
  end
end

--- Find an active connection by its Libuv pipe handle
--- @param pipe any
--- @return CodeSavantConnection|nil
local function find_connection_by_pipe(pipe)
  for _, conn in pairs(Network._connections) do
    if conn.pipe == pipe then
      return conn
    end
  end
  return nil
end

--- Establish a non-blocking pipe connection asynchronously mapped to a buffer
--- @param socket_path string
--- @param bufnr integer
--- @param workspace_path? string Optional workspace path to start a session with
--- @param mock_mode? boolean Optional mock mode flag
--- @param session_id? string Optional session ID to load instead of starting a new one
--- @return CodeSavantConnection
function Network.connect(socket_path, bufnr, workspace_path, mock_mode, session_id)
  -- 1. Parameter Validation with early exits & loud errors (Guideline 1, 4)
  if type(socket_path) ~= "string" or socket_path == "" then
    error("Invalid argument 'socket_path': must be a non-empty string. Got: " .. vim.inspect(socket_path))
  end
  if type(bufnr) ~= "number" or bufnr < 0 then
    error("Invalid argument 'bufnr': must be a non-negative integer. Got: " .. vim.inspect(bufnr))
  end

  -- 2. Idempotency Check (Safeguard against double-initialization to prevent fd leaks)
  local existing_conn = Network._connections[bufnr]
  if existing_conn and existing_conn.pipe and not existing_conn.pipe:is_closing() then
    return existing_conn
  end

  -- Retain listeners if connection is being re-established, or initialize new
  local listeners = existing_conn and existing_conn.listeners or {}

  -- 3. Initialize Libuv Pipe
  local uv = vim.uv or vim.loop
  local pipe, new_err = uv.new_pipe(false)
  if not pipe then
    local err_msg = "Failed to create Libuv pipe: " .. tostring(new_err)
    log_error(err_msg)
    error(err_msg)
  end

  -- 4. Establish Connection
  pipe:connect(socket_path, function(connect_err)
    if connect_err then
      local err_msg = string.format(
        "LOUD failure connecting socket '%s' for buffer %d: %s",
        socket_path,
        bufnr,
        tostring(connect_err)
      )
      log_error(err_msg)
      
      -- Propagate connection error to registered listeners to unlock the buffer
      local active_conn = Network._connections[bufnr]
      if active_conn then
        for _, listener in ipairs(active_conn.listeners) do
          pcall(listener, {
            error = {
              code = -32002,
              message = err_msg,
            }
          })
        end
      end
      
      Network.disconnect(bufnr)
    else
      -- Connection established successfully!
      -- Send session/start or session/load request to server to register and begin streaming telemetry
      local active_conn = Network._connections[bufnr]
      if active_conn then
        vim.schedule(function()
          local path = active_conn.workspace_path or vim.fn.getcwd()
          if active_conn.session_id_to_load then
            local params = {
              workspace_path = path,
              session_id = active_conn.session_id_to_load,
            }
            Network.send_request(active_conn, "session/load", params)
          else
            local params = {
              workspace_path = path,
              agent_profile = "coder"
            }
            if active_conn.mock_mode ~= nil then
              params.mock_mode = active_conn.mock_mode
            end
            Network.send_request(active_conn, "session/start", params)
          end
        end)
      end
    end
  end)

  -- Save connection state
  local conn = {
    pipe = pipe,
    bufnr = bufnr,
    socket_path = socket_path,
    workspace_path = workspace_path,
    mock_mode = mock_mode,
    session_id_to_load = session_id,
    accumulator = "",
    listeners = listeners,
  }
  Network._connections[bufnr] = conn

  -- 5. Register Read Callback immediately to prevent stream frames from being missed
  pipe:read_start(function(read_err, data)
    Network.on_read(pipe, read_err, data)
  end)

  return conn
end

--- Query the backend asynchronously for a list of saved sessions in a workspace
--- @param socket_path string
--- @param workspace_path string
--- @param callback fun(sessions: table|nil, err: string|nil)
function Network.list_sessions(socket_path, workspace_path, callback)
  if type(socket_path) ~= "string" or socket_path == "" then
    error("Invalid argument 'socket_path': must be a non-empty string. Got: " .. vim.inspect(socket_path))
  end
  if type(workspace_path) ~= "string" or workspace_path == "" then
    error("Invalid argument 'workspace_path': must be a non-empty string. Got: " .. vim.inspect(workspace_path))
  end
  if type(callback) ~= "function" then
    error("Invalid argument 'callback': must be a function. Got: " .. vim.inspect(callback))
  end

  local uv = vim.uv or vim.loop
  local pipe, err = uv.new_pipe(false)
  if not pipe then
    callback(nil, "Failed to create pipe: " .. tostring(err))
    return
  end

  local accumulator = ""
  pipe:connect(socket_path, function(connect_err)
    if connect_err then
      pcall(function() pipe:close() end)
      vim.schedule(function()
        callback(nil, "Failed to connect to daemon socket: " .. tostring(connect_err))
      end)
      return
    end

    -- Send session/list request
    local payload = {
      jsonrpc = "2.0",
      method = "session/list",
      params = { workspace_path = workspace_path },
      id = 9999,
    }
    local status, serialized = pcall(vim.json.encode, payload)
    if not status then
      pcall(function() pipe:close() end)
      vim.schedule(function()
        callback(nil, "Failed to serialize JSON-RPC payload")
      end)
      return
    end

    pipe:write(serialized .. "\n", function(write_err)
      if write_err then
        pcall(function() pipe:close() end)
        vim.schedule(function()
          callback(nil, "Failed to write payload to pipe: " .. tostring(write_err))
        end)
      end
    end)
  end)

  pipe:read_start(function(read_err, data)
    if read_err or not data then
      pcall(function() pipe:close() end)
      return
    end

    accumulator = accumulator .. data
    local newline_pos = string.find(accumulator, "\n", 1, true)
    if newline_pos then
      local frame = string.sub(accumulator, 1, newline_pos - 1)
      pcall(function() pipe:close() end)

      vim.schedule(function()
        local decode_ok, parsed = pcall(vim.json.decode, frame)
        if decode_ok then
          if parsed.error then
            callback(nil, parsed.error.message or "Unknown backend error")
          elseif parsed.result and parsed.result.sessions then
            callback(parsed.result.sessions, nil)
          else
            callback(nil, "Invalid response payload")
          end
        else
          callback(nil, "Failed to decode JSON response")
        end
      end)
    end
  end)
end

--- Send a generic JSON-RPC 2.0 request asynchronously over a connection
--- @param conn CodeSavantConnection The active connection structure
--- @param method string JSON-RPC method name
--- @param params table JSON-RPC parameters
function Network.send_request(conn, method, params)
  if not conn or type(conn) ~= "table" then
    error("Invalid argument 'conn': must be a connection table. Got: " .. vim.inspect(conn))
  end
  if not conn.pipe or type(conn.pipe) ~= "userdata" then
    error("Invalid connection state: 'conn.pipe' is missing or invalid.")
  end
  if conn.pipe:is_closing() then
    error("Cannot write to pipe: pipe handle is closing or closed.")
  end
  if type(method) ~= "string" or method == "" then
    error("Invalid argument 'method': must be a non-empty string. Got: " .. vim.inspect(method))
  end

  local payload = {
    jsonrpc = Network.CONSTANTS.JSONRPC_VERSION,
    method = method,
    params = params or {},
    id = Network._next_id(),
  }

  local status, serialized = pcall(vim.json.encode, payload)
  if not status then
    local err_msg = "Failed to serialize JSON-RPC payload: " .. tostring(serialized) .. "\n" .. debug.traceback()
    log_error(err_msg)
    error(err_msg)
  end

  local framed = serialized .. Network.CONSTANTS.DELIMITER

  conn.pipe:write(framed, function(write_err)
    if write_err then
      local err_msg = string.format(
        "LOUD write error on pipe write: %s\n%s",
        tostring(write_err),
        debug.traceback()
      )
      log_error(err_msg)
    end
  end)
end

--- Asynchronously writes structured JSON-RPC prompt message over Libuv stream
--- @param pipe any Raw Libuv pipe or CodeSavantConnection object
--- @param prompt string Raw non-empty text prompt
function Network.send_prompt(pipe, prompt)
  -- Normalize to get raw pipe handle and associated connection for session ID check
  local conn = nil
  if type(pipe) == "table" then
    if pipe.pipe then
      conn = pipe
    else
      -- Search if connection table was passed
      conn = find_connection_by_pipe(pipe)
    end
  else
    conn = find_connection_by_pipe(pipe)
  end

  if not conn then
    error("Could not find active connection matching the provided pipe.")
  end

  if type(prompt) ~= "string" or prompt == "" then
    error("Invalid argument 'prompt': must be a non-empty string. Got: " .. vim.inspect(prompt))
  end

  -- 2. Construct JSON-RPC Payload (Guideline 6)
  local params = {
    text = prompt,
  }
  if conn.session_id then
    params.session_id = conn.session_id
  else
    -- Queue the prompt until the asynchronous session/start handshake completes and session_id is bound
    conn.pending_prompts = conn.pending_prompts or {}
    table.insert(conn.pending_prompts, prompt)
    return
  end

  Network.send_request(conn, Network.CONSTANTS.METHODS.SEND_PROMPT, params)
end

--- Accumulate stream chunks, frame by newline boundary, decode safely, and dispatch
--- @param pipe any
--- @param err string|nil
--- @param data string|nil
function Network.on_read(pipe, err, data)
  local conn = find_connection_by_pipe(pipe)
  if not conn then
    -- Pipe is orphaned, clean up safely
    if pipe and not pipe:is_closing() then
      pipe:read_stop()
      pipe:close()
    end
    return
  end

  -- 1. Loud Error Check (Guideline 1)
  if err then
    local err_msg = string.format(
      "LOUD socket read error on buffer %d: %s",
      conn.bufnr,
      tostring(err)
    )
    log_error(err_msg)
    
    -- Propagate error to registered listeners to unlock the buffer
    for _, listener in ipairs(conn.listeners) do
      pcall(listener, {
        error = {
          code = -32001,
          message = err_msg,
        }
      })
    end
    
    Network.disconnect(conn.bufnr)
    return
  end

  -- 2. Null/EOF Check (Clean teardown to prevent FD leaks)
  if not data or data == "" then
    -- Propagate disconnect event to registered listeners to unlock the buffer
    for _, listener in ipairs(conn.listeners) do
      pcall(listener, {
        error = {
          code = -32000,
          message = "Connection closed by daemon.",
        }
      })
    end
    
    Network.disconnect(conn.bufnr)
    return
  end

  -- 3. Chunk Accumulation
  conn.accumulator = conn.accumulator .. data

  -- 4. Newline Segment Parsing
  while true do
    local newline_pos = string.find(conn.accumulator, Network.CONSTANTS.DELIMITER, 1, true)
    if not newline_pos then
      break
    end
    local frame = string.sub(conn.accumulator, 1, newline_pos - 1)
    conn.accumulator = string.sub(conn.accumulator, newline_pos + 1)

    -- Handle potential message parsing and dispatch
    if frame ~= "" then
      -- 5. Protected JSON-RPC Decoding
      local decode_ok, parsed = pcall(vim.json.decode, frame)
      if decode_ok then
        -- Update connection session_id if this is a response to session/start
        if parsed.result and parsed.result.session_id then
          conn.session_id = parsed.result.session_id
          -- Flush any pending prompts queued during the connection/handshake window
          if conn.pending_prompts then
            local prompts = conn.pending_prompts
            conn.pending_prompts = nil
            for _, pending_prompt in ipairs(prompts) do
              Network.send_prompt(conn, pending_prompt)
            end
          end
        end

        -- 6. UI Event Dispatch (Guideline 5)
        for _, listener in ipairs(conn.listeners) do
          local listener_ok, listener_err = pcall(listener, parsed)
          if not listener_ok then
            log_error(string.format(
              "LOUD listener error on buffer %d: %s\n%s",
              conn.bufnr,
              tostring(listener_err),
              debug.traceback()
            ))
          end
        end
      else
        log_error(string.format(
          "LOUD decoding failure on frame: %s. Error: %s\n%s",
          frame,
          tostring(parsed),
          debug.traceback()
        ))
      end
    end
  end
end

--- Safely tear down active client socket connection for a buffer
--- @param bufnr integer
function Network.disconnect(bufnr)
  if type(bufnr) ~= "number" or bufnr < 0 then
    error("Invalid argument 'bufnr' in disconnect: must be a non-negative integer.")
  end
  local conn = Network._connections[bufnr]
  if not conn then
    return
  end
  local pipe = conn.pipe
  if pipe and not pipe:is_closing() then
    pipe:read_stop()
    pipe:close()
  end
  Network._connections[bufnr] = nil
end

--- Add a message listener callback to a specific buffer connection
--- @param bufnr integer
--- @param listener function
function Network.add_listener(bufnr, listener)
  if type(bufnr) ~= "number" or bufnr < 0 then
    error("Invalid argument 'bufnr' in add_listener: must be a non-negative integer.")
  end
  if type(listener) ~= "function" then
    error("Invalid argument 'listener' in add_listener: must be a function.")
  end

  local conn = Network._connections[bufnr]
  if not conn then
    -- Create entry placeholder for listeners if connection hasn't started yet
    Network._connections[bufnr] = {
      listeners = { listener },
      accumulator = "",
    }
    return
  end

  table.insert(conn.listeners, listener)
end

--- Retrieve the connection object for a buffer
--- @param bufnr integer
--- @return CodeSavantConnection|nil
function Network.get_connection(bufnr)
  return Network._connections[bufnr]
end

return Network
