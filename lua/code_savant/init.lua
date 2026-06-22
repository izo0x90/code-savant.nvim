--- @class CodeSavant
--- @field config table
--- @field _initialized boolean
--- @field bootstrap_in_progress boolean
--- @field daemon_job_id integer|nil
local M = {}
M._has_render_markdown = false
M._render_markdown_api = nil
M._has_telescope = false
M._telescope_api = nil

local Network = require("code_savant.network")
local UI = require("code_savant.ui").get_instance()
local Layout = require("code_savant.layout")

--- Centralized Constants for Neovim plugin options and commands
--- Adheres to Guideline 2: No magic values or inline literals.
local CONSTANTS = {
  FILETYPE = "code_savant_chat",
  BUFTYPE = "nofile",
  BUF_HIDDEN = "hide",
  SWAPFILE = false,
  COMMAND_CHAT = "CodeSavantChat",
  DEFAULT_SOCKET_PATH = "/tmp/code_savant.sock",
}

local function get_render_markdown()
  if M._has_render_markdown then
    return M._render_markdown_api
  end
  local ok, rm = pcall(require, "render-markdown")
  if ok then
    M._has_render_markdown = true
    M._render_markdown_api = rm
    return rm
  end
  return nil
end

--- Default Preferences
--- Adheres to Guideline 3: Top-level default configurations.
local DEFAULT_CONFIG = {
  socket_path = CONSTANTS.DEFAULT_SOCKET_PATH,
  spawn_split = "vsplit", -- Options: "vsplit" (vertical), "split" (horizontal), "tabnew" (new tab), "edit" (current window)
  perf_debug = false, -- Options: true to run under viztracer, false for standard execution
  perf_trace_file = ".code_savant/perf_trace.json", -- Target location for the visualization report
  input_height = 3,
  sidebar_width_pct = 0.5,
  integrations = {
    render_markdown = "auto", -- Options: "auto" (detects & auto-manages), "on" (forces), "off" (disabled)
  },
  spinner = {
    type = "equalizer", -- Options: "braille", "clock", "circle", "equalizer", "shade", "ellipsis", "retro", "custom"
    custom_frames = nil,
    interval = 100,
  },
  keymaps = {
    expand = "<CR>",  -- Key to expand collapsed virtual thought blocks
    submit = "<CR>",  -- Key to submit prompt lines at the bottom of the buffer
    submit_alt = "<S-CR>", -- Alternative submission (e.g., Shift+Enter) in any mode
    next_session = "<S-L>", -- Local normal-mode cycle forward
    prev_session = "<S-H>", -- Local normal-mode cycle backward
    open_buffer = "gO",     -- Key to open thought space in a new buffer split
    open_float = "K",       -- Key to open thought space in a floating window
    cancel = "<C-c>",       -- Key to cancel/abort outstanding agent runs
    approve = "a",          -- Key to approve tool confirmation requests
    decline = "d",          -- Key to decline tool confirmation requests
    toggle_render = "<leader>mr", -- Key to manually toggle render-markdown inside chat windows
    balance = "<leader>cb", -- Key to restore standard layout splits instantly
  }
}

M.config = {}
M._initialized = false
M.bootstrap_in_progress = false
M.daemon_job_id = nil
M._daemon_stderr_chunks = {}
M._daemon_intentional_stop = false

--- Helper to retrieve the absolute plugin root path dynamically.
--- Computes the root from this script's path (lua/code_savant/init.lua)
--- @return string
local function get_plugin_root()
  local script_path = debug.getinfo(1).source:sub(2)
  -- Go up three levels: init.lua -> code_savant -> lua -> plugin_root
  local plugin_root = vim.fs.dirname(vim.fs.dirname(vim.fs.dirname(script_path)))
  return vim.fs.normalize(plugin_root)
end

--- Self-Bootstrapping Approach A
--- Checks if local '.venv' directory exists. If missing, launches non-blocking
--- 'uv sync' using vim.fn.jobstart. Fails loudly if 'uv' is missing from PATH.
--- @return boolean true if .venv exists, false if bootstrap started
function M.bootstrap_if_needed()
  local plugin_root = get_plugin_root()
  local venv_path = plugin_root .. "/.venv"

  -- If .venv already exists, bootstrapping is done.
  if vim.fn.isdirectory(venv_path) == 1 then
    return true
  end

  -- Guard clause: Avoid duplicate background job spams
  if M.bootstrap_in_progress then
    return false
  end

  -- Guard clause: Fail loudly if 'uv' is missing from PATH
  if vim.fn.executable("uv") == 0 then
    local err_msg = "[CodeSavant Error] '.venv' is missing and 'uv' executable was not found on PATH.\n" ..
                    "Please install 'uv' or create '.venv' manually at " .. venv_path
    vim.notify(err_msg, vim.log.levels.ERROR)
    error(err_msg)
  end

  M.bootstrap_in_progress = true
  vim.notify("[CodeSavant] '.venv' is missing. Launching non-blocking self-bootstrap with 'uv sync'...", vim.log.levels.INFO)

  local job_id = vim.fn.jobstart({ "uv", "sync" }, {
    cwd = plugin_root,
    on_exit = function(_, exit_code)
      M.bootstrap_in_progress = false
      if exit_code == 0 then
        vim.notify("[CodeSavant] Self-bootstrapping completed successfully!", vim.log.levels.INFO)
      else
        vim.notify("[CodeSavant Error] Self-bootstrapping 'uv sync' failed with code: " .. tostring(exit_code), vim.log.levels.ERROR)
      end
    end
  })

  if job_id <= 0 then
    M.bootstrap_in_progress = false
    local err_msg = "[CodeSavant Error] Failed to spawn 'uv sync' job. jobstart returned: " .. tostring(job_id)
    vim.notify(err_msg, vim.log.levels.ERROR)
    error(err_msg)
  end

  return false
end

--- Check if the Python UDS daemon is currently listening on the socket path.
--- @param callback fun(running: boolean)
function M.is_daemon_running(callback)
  if not callback then
    error("[CodeSavant Error] is_daemon_running requires a callback function")
  end

  local uv = vim.uv or vim.loop
  local client = uv.new_pipe(false)
  local socket_path = M.config.socket_path or CONSTANTS.DEFAULT_SOCKET_PATH

  uv.pipe_connect(client, socket_path, function(err)
    uv.close(client)
    vim.schedule(function()
      if err then
        callback(false)
      else
        callback(true)
      end
    end)
  end)
end

--- Starts the background python UDS daemon dynamically.
--- @return boolean
function M.start_daemon()
  local plugin_root = get_plugin_root()
  
  -- Resolve python path dynamically with Windows support
  local python_bin = plugin_root .. "/.venv/bin/python"
  if vim.fn.has("win32") == 1 then
    python_bin = plugin_root .. "/.venv/Scripts/python.exe"
  end

  local main_py = plugin_root .. "/src/engine/main.py"

  -- Ensure executable and script exist before spawning
  if vim.fn.executable(python_bin) == 0 then
    error("[CodeSavant Error] Python virtual environment binary not found or executable at: " .. python_bin)
  end
  if vim.fn.filereadable(main_py) == 0 then
    error("[CodeSavant Error] Main Python daemon script not found at: " .. main_py)
  end

  local socket_path = M.config.socket_path or CONSTANTS.DEFAULT_SOCKET_PATH
  local is_perf = M.config.perf_debug or false
  if is_perf == nil then is_perf = false end

  if is_perf then
    local test_cmd = { python_bin, "-c", "import viztracer" }
    vim.fn.system(test_cmd)
    if vim.v.shell_error ~= 0 then
      error("[CodeSavant Error] Performance debugging is enabled ('perf_debug = true'), but 'viztracer' is not installed in the virtual environment.\n" ..
            "Please run 'uv sync' in your terminal to install development dependencies.")
    end
  end

  -- Run as package module to avoid shadow types.py collision with Python standard library
  -- Note: Do NOT use "--server" as it is an unrecognized argument and causes the daemon to crash (argparse error).
  local cmd
  if is_perf then
    local trace_file = M.config.perf_trace_file or ".code_savant/perf_trace.json"
    if not (trace_file:sub(1, 1) == "/" or trace_file:sub(2, 2) == ":") then
      trace_file = plugin_root .. "/" .. trace_file
    end
    local trace_dir = vim.fs.dirname(trace_file)
    if trace_dir and trace_dir ~= "." and vim.fn.isdirectory(trace_dir) == 0 then
      vim.fn.mkdir(trace_dir, "p")
    end
    cmd = { python_bin, "-m", "viztracer", "--log_async", "--exclude_files", ".venv/", "-o", trace_file, "-m", "engine.main", "--socket-path", socket_path }
  else
    cmd = { python_bin, "-m", "engine.main", "--socket-path", socket_path }
  end

  M._daemon_stderr_chunks = {}

  local job_id = vim.fn.jobstart(cmd, {
    cwd = plugin_root,
    env = {
      PYTHONPATH = plugin_root .. "/src",
    },
    on_stderr = function(_, data)
      if data then
        for _, line in ipairs(data) do
          if line ~= "" then
            table.insert(M._daemon_stderr_chunks, line)
          end
        end
      end
    end,
    on_exit = function(_, exit_code)
      M.daemon_job_id = nil
      local was_intentional = M._daemon_intentional_stop
      M._daemon_intentional_stop = false

      if exit_code ~= 0 and not was_intentional then
        local stderr_str = table.concat(M._daemon_stderr_chunks, "\n")
        local err_msg = string.format(
          "[CodeSavant Daemon Crash] The background daemon exited unexpectedly with code %d.\n",
          exit_code
        )
        if stderr_str ~= "" then
          err_msg = err_msg .. "Stderr Output:\n" .. stderr_str
        end
        vim.schedule(function()
          vim.notify(err_msg, vim.log.levels.ERROR)
        end)
      end
    end
  })

  if job_id <= 0 then
    error("[CodeSavant Error] Failed to start Python daemon job via jobstart. Code: " .. tostring(job_id))
  end

  M.daemon_job_id = job_id
  return true
end

--- Ensures daemon is running, spawning it if missing, and notifies via callback.
--- @param callback fun(success: boolean, err_msg?: string)
function M.ensure_daemon_running(callback)
  if not callback then
    error("[CodeSavant Error] ensure_daemon_running requires a callback function")
  end

  M.is_daemon_running(function(running)
    if running then
      callback(true)
      return
    end

    -- Spawn the daemon since it's not running
    local ok, err = pcall(M.start_daemon)
    if not ok then
      local err_msg = "[CodeSavant Error] Daemon startup failed: " .. tostring(err)
      vim.notify(err_msg, vim.log.levels.ERROR)
      callback(false, err_msg)
      return
    end

    -- Poll until the socket is active or timeout is reached
    local uv = vim.uv or vim.loop
    local timer = uv.new_timer()
    local attempts = 0
    local max_attempts = 15

    uv.timer_start(timer, 100, 100, function()
      attempts = attempts + 1
      M.is_daemon_running(function(active)
        if active then
          uv.timer_stop(timer)
          uv.close(timer)
          callback(true)
        elseif attempts >= max_attempts then
          uv.timer_stop(timer)
          uv.close(timer)
          local stderr_str = table.concat(M._daemon_stderr_chunks, "\n")
          local err_msg = "[CodeSavant Error] Unable to connect to background daemon (connection timeout)."
          if stderr_str ~= "" then
            err_msg = err_msg .. "\nDaemon Stderr Output:\n" .. stderr_str
          end
          callback(false, err_msg)
        end
      end)
    end)
  end)
end

--- Stops the running daemon job if it was spawned by this session.
function M.stop_daemon()
  if M.daemon_job_id then
    M._daemon_intentional_stop = true
    vim.fn.jobstop(M.daemon_job_id)
    M.daemon_job_id = nil
  end
end

local function find_thinking_line(bufnr)
  local lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
  for i, line in ipairs(lines) do
    if line:find("CodeSavant is thinking...", 1, true) then
      return i - 1 -- 0-indexed row
    end
  end
  return nil
end

--- Asynchronously starts/ensures daemon is running, connects to the UDS pipe,
--- and registers network handlers.
--- @param bufnr integer
--- @param mock_mode? boolean Optional mock mode flag
function M.start_chat_session(bufnr, mock_mode, session_id)
  -- Programmatically register our custom filetype with render-markdown active state if present
  if get_render_markdown() then
    local state_ok, state = pcall(require, "render-markdown.state")
    if state_ok and state and state.file_types then
      if not vim.tbl_contains(state.file_types, CONSTANTS.FILETYPE) then
        table.insert(state.file_types, CONSTANTS.FILETYPE)
        -- Re-trigger FileType autocommands so render-markdown hooks our buffer
        pcall(vim.api.nvim_exec_autocmds, "FileType", { buffer = bufnr })
      end
    end
  end

  M.ensure_daemon_running(function(success, err_msg)
    if not success then
      vim.schedule(function()
        vim.notify(err_msg or "[CodeSavant Error] Unable to connect to background mock daemon.", vim.log.levels.ERROR)
      end)
      return
    end

    -- Connect over socket pipe and initiate session
    local socket_path = M.config.socket_path or CONSTANTS.DEFAULT_SOCKET_PATH
    Network.connect(socket_path, bufnr, vim.fn.getcwd(), mock_mode, session_id)
    
    -- Register incoming message stream handler (JSON-RPC listener)
    Network.add_listener(bufnr, function(parsed)
      if type(parsed) ~= "table" then
        return
      end

      vim.schedule(function()
        if not vim.api.nvim_buf_is_valid(bufnr) then
          return
        end

        UI:run_programmatic_update(bufnr, function()
          -- Handle daemon/executor error frames cleanly
          if parsed.error then
            UI:stop_spinner(bufnr, "thinking")
            local daemon_err_msg = string.format("[CodeSavant Daemon Error] %s (Code: %s)",
              tostring(parsed.error.message or "Unknown Error"), tostring(parsed.error.code or "nil"))
            if parsed.error.data then
              daemon_err_msg = daemon_err_msg .. " - " .. vim.inspect(parsed.error.data)
            end
            vim.notify(daemon_err_msg, vim.log.levels.ERROR)

            -- Clean up the thinking line indicator to keep buffer responsive
            local thinking_row = find_thinking_line(bufnr)
            if thinking_row then
              vim.api.nvim_buf_set_lines(bufnr, thinking_row, thinking_row + 1, false, {})
            end
            return
          end

          -- Handle historical chat history loading
          if parsed.result and parsed.result.chat_history then
            local chat_history = parsed.result.chat_history
            local formatted_lines = {}
            local blocks_to_render = {}

            for msg_idx, msg in ipairs(chat_history) do
              local role = msg.role
              if msg.parts and #msg.parts > 0 then
                if #formatted_lines > 0 then
                  table.insert(formatted_lines, "") -- separator
                end

                if role == "user" then
                  table.insert(formatted_lines, "User:")
                  for _, part in ipairs(msg.parts) do
                    if part.text then
                      local msg_lines = vim.split(part.text, "\n", { plain = true })
                      for _, line in ipairs(msg_lines) do
                        table.insert(formatted_lines, "  " .. line)
                      end
                    end
                  end
                elseif role == "model" then
                  for part_idx, part in ipairs(msg.parts) do
                    if part.type == "thought" then
                      -- Insert an empty placeholder line to anchor the collapsible extmark
                      table.insert(formatted_lines, "")
                      local row_idx = #formatted_lines -- 1-based index of the placeholder line

                      local block_id = string.format("loaded_thought_%s_%d_%d", tostring(parsed.result.session_id), msg_idx, part_idx)
                      local title = part.title or "Thinking..."
                      local content = part.text or ""

                      table.insert(blocks_to_render, {
                        id = block_id,
                        title = title,
                        content = content,
                        row = row_idx - 1 -- convert to 0-based row index for Neovim API
                      })
                    elseif part.type == "text" and part.text then
                      local content_lines = vim.split(part.text, "\n", { plain = true })
                      for _, line in ipairs(content_lines) do
                        table.insert(formatted_lines, line)
                      end
                    end
                  end
                end
              end
            end

            if #formatted_lines > 0 then
              vim.api.nvim_buf_set_lines(bufnr, 0, -1, false, formatted_lines)
            end

            -- Now render all our collapsed blocks on their respective anchors!
            for _, block in ipairs(blocks_to_render) do
              UI:on_collapsed_block(block.id, "thought", block.title, block.content, bufnr, block.row)
            end
          end

          -- Match incoming server notifications
          if parsed.method == "telemetry/collapsed_block" then
            local params = parsed.params or {}
            local cached_block = UI.collapsed_blocks_cache[params.id]
            local target_row

            if not cached_block then
              -- This is a brand NEW block! We must find a row for it and insert exactly one empty line.
              local thinking_row = find_thinking_line(bufnr)
              if thinking_row then
                -- Shifting thinking indicator down by inserting an empty line at its row,
                -- and anchoring the collapsed block extmark on the newly inserted row.
                vim.api.nvim_buf_set_lines(bufnr, thinking_row, thinking_row, false, { "" })
                target_row = thinking_row
              else
                local line_count = vim.api.nvim_buf_line_count(bufnr)
                target_row = math.max(0, line_count - 1)
                vim.api.nvim_buf_set_lines(bufnr, target_row, target_row, false, { "" })
              end
            else
              -- This is an update to an existing block! Retain its previous row.
              target_row = cached_block.row
            end

            UI:on_collapsed_block(params.id, params.type, params.title, params.full_content, bufnr, target_row)

            -- Bind buffer-local shortcuts if this is an interactive tool confirmation request
            if params.type == "confirmation" then
              local Network = require("code_savant.network")
              local keymaps = M.config.keymaps or DEFAULT_CONFIG.keymaps
              local function respond(confirmed)
                -- Send the decision back to the daemon
                local conn = Network.get_connection(bufnr)
                if conn then
                  local ok, err = pcall(Network.send_request, conn, "session/respond_confirmation", {
                    session_id = params.session_id,
                    id = params.id,
                    confirmed = confirmed
                  })
                  if not ok then
                    vim.notify("[CodeSavant Error] Failed to send tool confirmation response: " .. tostring(err), vim.log.levels.ERROR)
                  end
                else
                  vim.notify("[CodeSavant Error] Failed to send tool confirmation: No active connection found for buffer " .. tostring(bufnr), vim.log.levels.ERROR)
                end
                -- Delete the mapping to make it single-use
                pcall(vim.keymap.del, "n", keymaps.approve, { buffer = bufnr })
                pcall(vim.keymap.del, "n", keymaps.decline, { buffer = bufnr })

                -- Inline status update
                local result_text = confirmed and " ✓ APPROVED" or " ✗ DECLINED"
                local hl = confirmed and "DiagnosticOk" or "DiagnosticError"
                pcall(vim.api.nvim_buf_set_extmark, bufnr, UI.namespace, target_row, 0, {
                  id = UI.collapsed_blocks_cache[params.id].extmark_id,
                  virt_text = { { "▶ Approve/Decline: " .. params.title .. result_text, hl } },
                  virt_text_pos = "overlay",
                })
              end

              -- Set keymaps for approval and decline
              vim.keymap.set("n", keymaps.approve, function()
                local cursor = vim.api.nvim_win_get_cursor(0)
                if cursor[1] - 1 == target_row then
                  respond(true)
                else
                  -- Standard action fallback
                  vim.api.nvim_feedkeys(keymaps.approve, "n", false)
                end
              end, { buffer = bufnr, silent = true, desc = "Approve tool execution" })

              vim.keymap.set("n", keymaps.decline, function()
                local cursor = vim.api.nvim_win_get_cursor(0)
                if cursor[1] - 1 == target_row then
                  respond(false)
                else
                  -- Standard action fallback
                  vim.api.nvim_feedkeys(keymaps.decline, "n", false)
                end
              end, { buffer = bufnr, silent = true, desc = "Decline tool execution" })
            end

          elseif parsed.method == "telemetry/message" then
            local params = parsed.params or {}
            local text = params.text or ""
            if text ~= "" then
              -- LOUD error surfacing for stream crashes
              if text:find("[CodeSavant Error]", 1, true) then
                vim.notify(text, vim.log.levels.ERROR)
              end
              local lines = vim.split(text, "\n", { plain = true })
              local thinking_row = find_thinking_line(bufnr)
              if thinking_row then
                -- Stop the thinking spinner immediately so it doesn't overwrite our streamed lines!
                UI:stop_spinner(bufnr, "thinking")
                -- Replace the thinking indicator line with final text chunk
                vim.api.nvim_buf_set_lines(bufnr, thinking_row, thinking_row + 1, false, lines)
              else
                local line_count = vim.api.nvim_buf_line_count(bufnr)
                local target_row = math.max(0, line_count - 1)
                vim.api.nvim_buf_set_lines(bufnr, target_row, target_row, false, lines)
              end
            end

          elseif parsed.method == "telemetry/status" then
            local params = parsed.params or {}
            vim.b[bufnr].status = params.status

            if params.status == "idle" then
              UI:stop_spinner(bufnr, "thinking")
              local thinking_row = find_thinking_line(bufnr)
              if thinking_row then
                -- Safely wipe any remaining thinking text placeholder on completion
                vim.api.nvim_buf_set_lines(bufnr, thinking_row, thinking_row + 1, false, {})
              end
            end
          end

          -- Scroll history window to the bottom to follow streaming
          local winids = vim.fn.win_findbuf(bufnr)
          if winids and #winids > 0 then
            local history_winid = winids[1]
            local line_count = vim.api.nvim_buf_line_count(bufnr)
            pcall(vim.api.nvim_win_set_cursor, history_winid, { line_count, 0 })
          end
        end)
      end)
    end)
  end)
end

--- Sets buffer on a window while temporarily disabling winfixbuf.
--- @param winid integer
--- @param bufnr integer
local function safe_set_buf(winid, bufnr)
  if vim.api.nvim_win_is_valid(winid) then
    vim.wo[winid].winfixbuf = false
    vim.api.nvim_win_set_buf(winid, bufnr)
    vim.wo[winid].winfixbuf = true
  end
end

local function handle_enter(input_bufnr)
  local history_bufnr = vim.b[input_bufnr].partner_buf
  if not history_bufnr then return end
  local lines = vim.api.nvim_buf_get_lines(input_bufnr, 0, -1, false)
  local prompt_text = table.concat(lines, "\n")
  if prompt_text:match("^%s*$") then
    return
  end

  local conn = Network.get_connection(history_bufnr)
  if not conn or not conn.pipe or conn.pipe:is_closing() then
    vim.notify("[CodeSavant Error] Connection to daemon is not active. Please wait or verify daemon is running.", vim.log.levels.ERROR)
    return
  end

  local status = vim.b[history_bufnr].status or "idle"
  if status == "thinking" then
    UI:run_programmatic_update(history_bufnr, function()
      local line_count = vim.api.nvim_buf_line_count(history_bufnr)
      local to_append = {
        "",
        "User (Steering Directive):",
        "  " .. prompt_text,
        ""
      }
      local insert_start = (line_count == 1 and vim.api.nvim_buf_get_lines(history_bufnr, 0, 1, false)[1] == "") and 0 or line_count
      vim.api.nvim_buf_set_lines(history_bufnr, insert_start, -1, false, to_append)

      -- Scroll history window
      local winids = vim.fn.win_findbuf(history_bufnr)
      if winids and #winids > 0 then
        local history_winid_found = winids[1]
        local new_line_count = vim.api.nvim_buf_line_count(history_bufnr)
        pcall(vim.api.nvim_win_set_cursor, history_winid_found, { new_line_count, 0 })
      end
    end)

    -- Clear input buffer
    vim.api.nvim_buf_set_lines(input_bufnr, 0, -1, false, {})

    -- Send steering request
    local ok, err = pcall(Network.send_request, conn, "session/inject_steering", {
      session_id = conn.session_id,
      text = prompt_text
    })
    if not ok then
      vim.notify("[CodeSavant Error] Failed to inject steering directive: " .. tostring(err), vim.log.levels.ERROR)
    end
    return
  end

  UI:run_programmatic_update(history_bufnr, function()
    local line_count = vim.api.nvim_buf_line_count(history_bufnr)
    local to_append = {}
    if line_count > 1 or (line_count == 1 and vim.api.nvim_buf_get_lines(history_bufnr, 0, 1, false)[1] ~= "") then
      table.insert(to_append, "") -- Separator
    end
    table.insert(to_append, "User:")
    for _, line in ipairs(lines) do
      table.insert(to_append, "  " .. line)
    end
    table.insert(to_append, "")
    table.insert(to_append, "◀   CodeSavant is thinking...")
    table.insert(to_append, "")

    local insert_start = (line_count == 1 and vim.api.nvim_buf_get_lines(history_bufnr, 0, 1, false)[1] == "") and 0 or line_count
    vim.api.nvim_buf_set_lines(history_bufnr, insert_start, -1, false, to_append)

    -- Scroll history window
    local winids = vim.fn.win_findbuf(history_bufnr)
    if winids and #winids > 0 then
      local history_winid_found = winids[1]
      local new_line_count = vim.api.nvim_buf_line_count(history_bufnr)
      pcall(vim.api.nvim_win_set_cursor, history_winid_found, { new_line_count, 0 })
    end
  end)

  -- Kick off the animated spinner loader
  local spinner_opt = M.config.spinner or {}
  UI:start_spinner(history_bufnr, "thinking", {
    type = spinner_opt.type,
    custom_frames = spinner_opt.custom_frames,
    interval = spinner_opt.interval,
    use_extmark = true, -- Massively efficient virtual text overlay!
    col = 4,            -- Position the spinner directly over the Space 2 gap
    row = function() return find_thinking_line(history_bufnr) end,
    format_fn = function(symbol)
      return { { symbol, "Special" } } -- Color the rotating spinner beautifully!
    end,
  })

  -- Clear input buffer
  vim.api.nvim_buf_set_lines(input_bufnr, 0, -1, false, {})

  -- Send prompt
  Network.send_prompt(conn, prompt_text)
end

local function handle_cancel(bufnr)
  local history_bufnr = vim.b[bufnr].partner_buf or bufnr
  UI:stop_spinner(history_bufnr, "thinking")
  local conn = Network.get_connection(history_bufnr)
  if conn and conn.pipe and not conn.pipe:is_closing() then
    local ok, err = pcall(Network.send_request, conn, "session/cancel", { session_id = conn.session_id })
    if not ok then
      vim.notify("[CodeSavant Error] Failed to cancel execution loop: " .. tostring(err), vim.log.levels.ERROR)
    end
  end
end

local function handle_action_at_cursor(history_bufnr, action_callback)
  local cursor_row = vim.api.nvim_win_get_cursor(0)[1] - 1
  for id, cached in pairs(UI.collapsed_blocks_cache) do
    if cached.bufnr == history_bufnr then
      local pos = vim.api.nvim_buf_get_extmark_by_id(history_bufnr, UI.namespace, cached.extmark_id, {})
      if pos and #pos > 0 then
        local start_row = pos[1]
        local matched = false
        if cached.status == "expanded" then
          if cursor_row >= start_row and cursor_row < start_row + (cached.height or 1) then
            matched = true
          end
        else
          if start_row == cursor_row then
            matched = true
          end
        end

        if matched then
          action_callback(id, cached)
          return true
        end
      end
    end
  end
  return false
end

local function handle_expand(history_bufnr)
  local handled = handle_action_at_cursor(history_bufnr, function(id, cached)
    UI:run_programmatic_update(history_bufnr, function()
      if cached.status == "expanded" then
        UI:collapse_inplace({ id = id })
      else
        UI:expand_inplace({ id = id })
      end
    end)
  end)

  if not handled then
    -- Fallback: Use standard Enter behavior
    local fallback_code = vim.api.nvim_replace_termcodes("<CR>", true, true, true)
    vim.api.nvim_feedkeys(fallback_code, "n", false)
  end
end

local function handle_open_buffer(history_bufnr)
  handle_action_at_cursor(history_bufnr, function(id)
    UI:open_in_new_buf({ id = id })
  end)
end

local function handle_open_float(history_bufnr)
  handle_action_at_cursor(history_bufnr, function(id)
    UI:open_in_float({ id = id })
  end)
end


local function apply_buffer_config(history_bufnr, input_bufnr)
  -- 1. History Buffer Options
  vim.bo[history_bufnr].buftype = CONSTANTS.BUFTYPE
  vim.bo[history_bufnr].swapfile = CONSTANTS.SWAPFILE
  vim.bo[history_bufnr].bufhidden = CONSTANTS.BUF_HIDDEN
  vim.bo[history_bufnr].filetype = CONSTANTS.FILETYPE
  vim.bo[history_bufnr].modifiable = false -- Locked History!

  -- 2. Input Buffer Options
  vim.bo[input_bufnr].buftype = CONSTANTS.BUFTYPE
  vim.bo[input_bufnr].swapfile = CONSTANTS.SWAPFILE
  vim.bo[input_bufnr].bufhidden = CONSTANTS.BUF_HIDDEN
  vim.bo[input_bufnr].filetype = "code_savant_input"
  vim.bo[input_bufnr].modifiable = true -- Modifiable Input!

  -- Bind partner metadata bindings
  vim.b[history_bufnr].partner_buf = input_bufnr
  vim.b[input_bufnr].partner_buf = history_bufnr

  local keymaps = M.config.keymaps or DEFAULT_CONFIG.keymaps

  -- Bind submit key inside Input Buffer
  vim.keymap.set("n", keymaps.submit, function() handle_enter(input_bufnr) end, { buffer = input_bufnr, silent = true, desc = "CodeSavant Submit Prompt" })

  -- Bind submit_alt key inside Input Buffer (both Normal and Insert modes)
  if keymaps.submit_alt then
    vim.keymap.set("n", keymaps.submit_alt, function() handle_enter(input_bufnr) end, { buffer = input_bufnr, silent = true, desc = "CodeSavant Submit Prompt" })
    vim.keymap.set("i", keymaps.submit_alt, function() handle_enter(input_bufnr) end, { buffer = input_bufnr, silent = true, desc = "CodeSavant Submit Prompt" })
  end

  -- Bind expand key inside History Buffer
  vim.keymap.set("n", keymaps.expand, function() handle_expand(history_bufnr) end, { buffer = history_bufnr, silent = true, desc = "CodeSavant Expand Block" })

  -- Bind open_buffer key inside History Buffer
  if keymaps.open_buffer then
    vim.keymap.set("n", keymaps.open_buffer, function() handle_open_buffer(history_bufnr) end, { buffer = history_bufnr, silent = true, desc = "CodeSavant Open Block in New Buffer" })
  end

  -- Bind open_float key inside History Buffer
  if keymaps.open_float then
    vim.keymap.set("n", keymaps.open_float, function() handle_open_float(history_bufnr) end, { buffer = history_bufnr, silent = true, desc = "CodeSavant Open Block in Floating Window" })
  end

  -- Bind local next/prev session cycling keymaps cleanly
  if keymaps.next_session then
    vim.keymap.set("n", keymaps.next_session, function() M.cycle_session("next") end, { buffer = input_bufnr, silent = true, desc = "CodeSavant Next Session Override" })
    vim.keymap.set("n", keymaps.next_session, function() M.cycle_session("next") end, { buffer = history_bufnr, silent = true, desc = "CodeSavant Next Session Override" })
  end

  if keymaps.prev_session then
    vim.keymap.set("n", keymaps.prev_session, function() M.cycle_session("prev") end, { buffer = input_bufnr, silent = true, desc = "CodeSavant Prev Session Override" })
    vim.keymap.set("n", keymaps.prev_session, function() M.cycle_session("prev") end, { buffer = history_bufnr, silent = true, desc = "CodeSavant Prev Session Override" })
  end

  -- Bind cancellation keymaps elegantly based on modal design
  if keymaps.cancel then
    -- Universal interrupts inside Input Buffer (Insert and Normal)
    vim.keymap.set("i", keymaps.cancel, function() handle_cancel(input_bufnr) end, { buffer = input_bufnr, silent = true, desc = "Abort agent execution" })
    vim.keymap.set("n", keymaps.cancel, function() handle_cancel(input_bufnr) end, { buffer = input_bufnr, silent = true, desc = "Abort agent execution" })
    -- Interrupt inside History Buffer (Normal)
    vim.keymap.set("n", keymaps.cancel, function() handle_cancel(history_bufnr) end, { buffer = history_bufnr, silent = true, desc = "Abort agent execution" })
  end

  -- Normal mode Esc inside Input Buffer also cancels/aborts execution
  vim.keymap.set("n", "<Esc>", function() handle_cancel(input_bufnr) end, { buffer = input_bufnr, silent = true, desc = "Abort agent execution" })

  -- Bind manual render-markdown toggle keymaps if configured
  if keymaps.toggle_render then
    vim.keymap.set("n", keymaps.toggle_render, function() M.toggle_render_markdown(history_bufnr) end, { buffer = history_bufnr, silent = true, desc = "CodeSavant Toggle Render-Markdown" })
    vim.keymap.set("n", keymaps.toggle_render, function() M.toggle_render_markdown(history_bufnr) end, { buffer = input_bufnr, silent = true, desc = "CodeSavant Toggle Render-Markdown" })
  end

  -- Bind manual layout balancing keymaps if configured
  if keymaps.balance then
    vim.keymap.set("n", keymaps.balance, function() M.restore_layout_balance() end, { buffer = history_bufnr, silent = true, desc = "CodeSavant Balance Layout Splits" })
    vim.keymap.set("n", keymaps.balance, function() M.restore_layout_balance() end, { buffer = input_bufnr, silent = true, desc = "CodeSavant Balance Layout Splits" })
  end

  -- Bind C-level expand("<cfile>") optimized navigation keymaps on History Buffer
  vim.keymap.set("n", "gf", function()
    require("code_savant.navigation").jump_to_file_at_cursor("edit")
  end, { buffer = history_bufnr, silent = true, desc = "CodeSavant Go to File" })

  vim.keymap.set("n", "<C-w>f", function()
    require("code_savant.navigation").jump_to_file_at_cursor("split")
  end, { buffer = history_bufnr, silent = true, desc = "CodeSavant Go to File (Split)" })

  vim.keymap.set("n", "<C-w>gf", function()
    require("code_savant.navigation").jump_to_file_at_cursor("tab")
  end, { buffer = history_bufnr, silent = true, desc = "CodeSavant Go to File (Tab)" })

  vim.keymap.set("n", "gV", function()
    require("code_savant.navigation").jump_to_file_at_cursor("vsplit")
  end, { buffer = history_bufnr, silent = true, desc = "CodeSavant Go to File (VSplit)" })

  -- Add local cnoreabbrevs to trap buffer commands
  local function setup_abbrevs(buf)
    vim.api.nvim_buf_call(buf, function()
      vim.cmd("cnoreabbrev <buffer> bn CodeSavantNextSession")
      vim.cmd("cnoreabbrev <buffer> bp CodeSavantPrevSession")
      vim.cmd("cnoreabbrev <buffer> bnext CodeSavantNextSession")
      vim.cmd("cnoreabbrev <buffer> bprev CodeSavantPrevSession")
      vim.cmd("cnoreabbrev <buffer> bprevious CodeSavantPrevSession")
    end)
  end
  setup_abbrevs(history_bufnr)
  setup_abbrevs(input_bufnr)
end

--- Retrieves all active CodeSavant chat history buffers dynamically.
--- @return integer[]
local function get_active_sessions()
  local sessions = {}
  for _, bufnr in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_valid(bufnr) and vim.bo[bufnr].filetype == CONSTANTS.FILETYPE then
      table.insert(sessions, bufnr)
    end
  end
  table.sort(sessions) -- Stable ordering by buffer ID
  return sessions
end

--- Allocates a new history and input buffer pair, configures them, sets up local maps,
--- and mounts them cleanly.
--- @return table { bufnr: integer, input_bufnr: integer, history_win: integer, input_win: integer }
function M.create_chat_buffer()
  -- Self-Bootstrapping check on demand
  M.bootstrap_if_needed()

  -- 1. History Buffer Allocation
  local history_bufnr = vim.api.nvim_create_buf(false, true)
  if not history_bufnr or history_bufnr == 0 then
    error("[CodeSavant Error] Failed to allocate history buffer via vim.api.nvim_create_buf")
  end

  -- 2. Input Buffer Allocation
  local input_bufnr = vim.api.nvim_create_buf(false, true)
  if not input_bufnr or input_bufnr == 0 then
    error("[CodeSavant Error] Failed to allocate input buffer via vim.api.nvim_create_buf")
  end

  -- Apply base configuration and keymaps
  apply_buffer_config(history_bufnr, input_bufnr)

  -- 4. Atomic Lifecycle management (BufWipeout Autocommands)
  local group_name = "CodeSavantLifecycle_" .. tostring(history_bufnr) .. "_" .. tostring(input_bufnr)
  local group = vim.api.nvim_create_augroup(group_name, { clear = true })

  local wiping_in_progress = false

  local function cleanup_all()
    if wiping_in_progress then return end
    wiping_in_progress = true

    -- Perform network disconnection and UI teardown immediately
    pcall(Network.disconnect, history_bufnr)
    pcall(UI.teardown, UI)

    -- Defer everything asynchronously to run safely outside Neovim's buffer-freeing phase
    vim.schedule(function()
      local sessions = get_active_sessions()

      -- Filter out this history_bufnr from active sessions list
      local other_sessions = {}
      for _, s_buf in ipairs(sessions) do
        if s_buf ~= history_bufnr then
          table.insert(other_sessions, s_buf)
        end
      end

      if #other_sessions > 0 then
        -- Case A: Switch to next session asynchronously (reliably re-opens splits if closed)
        local next_session_buf = other_sessions[1]
        
        -- Activate the session cycling lock so WinClosed events don't close partner splits
        Layout._cycling_session = true
        
        local m_ok, m_err = pcall(M.mount_session, next_session_buf)
        if not m_ok then
          print("LOUD ERROR: mount_session failed: " .. tostring(m_err))
        end
        
        -- Release the session cycling lock
        Layout._cycling_session = false

        -- Safely delete BOTH buffers of the closed session
        if vim.api.nvim_buf_is_valid(input_bufnr) then
          pcall(vim.api.nvim_buf_delete, input_bufnr, { force = true })
        end
        if vim.api.nvim_buf_is_valid(history_bufnr) then
          pcall(vim.api.nvim_buf_delete, history_bufnr, { force = true })
        end
      else
        -- Case B: Last session -> Standard teardown and window closure
        if vim.api.nvim_buf_is_valid(input_bufnr) then
          pcall(vim.api.nvim_buf_delete, input_bufnr, { force = true })
        end
        if vim.api.nvim_buf_is_valid(history_bufnr) then
          pcall(vim.api.nvim_buf_delete, history_bufnr, { force = true })
        end
        for _, win in ipairs(vim.api.nvim_list_wins()) do
          if vim.api.nvim_win_is_valid(win) then
            local b = vim.api.nvim_win_get_buf(win)
            if b == history_bufnr or b == input_bufnr then
              pcall(vim.api.nvim_win_close, win, true)
            end
          end
        end
      end
      wiping_in_progress = false
    end)

    pcall(vim.api.nvim_del_augroup_by_id, group)
  end

  vim.api.nvim_create_autocmd("BufUnload", {
    buffer = history_bufnr,
    group = group,
    callback = cleanup_all,
    once = true,
  })

  vim.api.nvim_create_autocmd("BufUnload", {
    buffer = input_bufnr,
    group = group,
    callback = cleanup_all,
    once = true,
  })

  -- Mount layout splits and assign history/input buffers
  M.mount_session(history_bufnr)

  local history_win = vim.b[history_bufnr].partner_win
  local input_win = vim.b[input_bufnr].partner_win

  return {
    bufnr = history_bufnr,
    input_bufnr = input_bufnr,
    history_win = history_win,
    input_win = input_win,
  }
end

--- Mounts a history buffer and its linked partner input buffer into the layout.
--- If active CodeSavant split windows are already visible, they are safely reused in-place.
--- Otherwise, it creates the initial split windows and applies the locked winfixbuf protection.
--- @param history_bufnr integer
--- @param target_winid? integer
function M.mount_session(history_bufnr, target_winid)
  Layout.mount_session(history_bufnr, target_winid, apply_buffer_config)
end

--- Cycles between active CodeSavant chat sessions.
--- @param direction "next"|"prev"
function M.cycle_session(direction)
  local cur_buf = vim.api.nvim_get_current_buf()
  local cur_history_bufnr = nil

  if vim.bo[cur_buf].filetype == CONSTANTS.FILETYPE then
    cur_history_bufnr = cur_buf
  elseif vim.bo[cur_buf].filetype == "code_savant_input" then
    cur_history_bufnr = vim.b[cur_buf].partner_buf
  end

  if not cur_history_bufnr then
    vim.notify("[CodeSavant] Not in a CodeSavant chat buffer.", vim.log.levels.WARN)
    return
  end

  local sessions = get_active_sessions()
  if #sessions <= 1 then
    vim.notify("[CodeSavant] Only one active chat session exists.", vim.log.levels.INFO)
    return
  end

  local current_idx = nil
  for i, bufnr in ipairs(sessions) do
    if bufnr == cur_history_bufnr then
      current_idx = i
      break
    end
  end

  if not current_idx then
    return
  end

  local target_idx
  if direction == "next" then
    target_idx = (current_idx % #sessions) + 1
  else
    target_idx = ((current_idx - 2 + #sessions) % #sessions) + 1
  end

  local target_buf = sessions[target_idx]
  M.mount_session(target_buf)
end

--- Restores Code Savant split windows to their default configured dimensions
function M.restore_layout_balance()
  Layout.restore_layout_balance()
end

--- Renders a fuzzy session switching picker (using Telescope if available, or vim.ui.select).
function M.select_session()
  local sessions = get_active_sessions()
  if #sessions == 0 then
    vim.notify("[CodeSavant] No active chat sessions.", vim.log.levels.INFO)
    return
  end

  local items = {}
  local lookup = {}

  for _, bufnr in ipairs(sessions) do
    local lines = vim.api.nvim_buf_get_lines(bufnr, 0, 10, false)
    local first_prompt = "Empty Session"
    for _, line in ipairs(lines) do
      if line ~= "" and not line:match("^%s*$") and not line:match("User:") and not line:match("CodeSavant") then
        local cleaned = line:gsub("^%s+", ""):gsub("%s+$", "")
        if cleaned ~= "" then
          first_prompt = cleaned
          if #first_prompt > 50 then
            first_prompt = first_prompt:sub(1, 47) .. "..."
          end
          break
        end
      end
    end

    local label = string.format("Session %d: %s", bufnr, first_prompt)
    table.insert(items, label)
    lookup[label] = bufnr
  end

  local has_telescope, telescope = pcall(require, "telescope")
  if has_telescope then
    local pickers = require("telescope.pickers")
    local finders = require("telescope.finders")
    local conf = require("telescope.config").values
    local actions = require("telescope.actions")
    local action_state = require("telescope.actions.state")

    pickers.new({}, {
      prompt_title = "CodeSavant Chat Sessions",
      finder = finders.new_table({
        results = items,
      }),
      sorter = conf.generic_sorter({}),
      attach_mappings = function(prompt_bufnr)
        actions.select_default:replace(function()
          actions.close(prompt_bufnr)
          local selection = action_state.get_selected_entry()
          if selection then
            local bufnr = lookup[selection[1]]
            if bufnr then
              M.mount_session(bufnr)
            end
          end
        end)
        return true
      end,
    }):find()
  else
    vim.ui.select(items, {
      prompt = "Select CodeSavant Chat Session:",
    }, function(choice)
      if choice then
        local bufnr = lookup[choice]
        if bufnr then
          M.mount_session(bufnr)
        end
      end
    end)
  end
end

--- Asynchronously queries the daemon for workspace sessions and presents an interactive selector.
function M.load_session_picker()
  -- Self-Bootstrapping check on demand
  M.bootstrap_if_needed()

  local socket_path = M.config.socket_path or CONSTANTS.DEFAULT_SOCKET_PATH
  local workspace_path = vim.fn.getcwd()

  M.ensure_daemon_running(function(success, err_msg)
    if not success then
      vim.schedule(function()
        vim.notify(err_msg or "[CodeSavant Error] Background daemon is not running.", vim.log.levels.ERROR)
      end)
      return
    end

    local Network = require("code_savant.network")
    Network.list_sessions(socket_path, workspace_path, function(sessions, list_err)
      if list_err then
        vim.notify("[CodeSavant Error] Failed to list sessions: " .. tostring(list_err), vim.log.levels.ERROR)
        return
      end

      if not sessions or #sessions == 0 then
        vim.notify("[CodeSavant] No saved chat sessions found in this workspace.", vim.log.levels.INFO)
        return
      end

      local items = {}
      local session_map = {}

      for _, s in ipairs(sessions) do
        local meta = s.metadata or {}
        local name = meta.name or "Untitled Session"
        local date = meta.last_updated or meta.created_at or "Unknown Date"
        -- Format date nicely (usually in ISO 8601 e.g. "2026-06-19T10:20:30")
        local clean_date = date:gsub("T", " "):gsub("%.%d+", "")
        local label = string.format("%s (%s) - %d turns", name, clean_date, s.turn_count or 0)
        table.insert(items, label)
        session_map[label] = s
      end

      local function on_choice(choice)
        if not choice then return end
        local selected = session_map[choice]
        if not selected then return end

        -- 🛡️ Idempotent check: Prevent duplicate buffers for the same session!
        local existing_bufnr = nil
        local Network = require("code_savant.network")
        for bufnr, conn in pairs(Network._connections) do
          if conn.session_id == selected.session_id then
            existing_bufnr = bufnr
            break
          end
        end

        if existing_bufnr then
          M.mount_session(existing_bufnr)
          return
        end

        local result = M.create_chat_buffer()
        -- Load connection and stream session history in the active split window
        M.start_chat_session(result.bufnr, selected.metadata.mock_mode or false, selected.session_id)
      end

      local has_telescope, telescope = pcall(require, "telescope")
      if has_telescope then
        local pickers = require("telescope.pickers")
        local finders = require("telescope.finders")
        local conf = require("telescope.config").values
        local actions = require("telescope.actions")
        local action_state = require("telescope.actions.state")

        pickers.new({}, {
          prompt_title = "Load CodeSavant Session",
          finder = finders.new_table({
            results = items,
          }),
          sorter = conf.generic_sorter({}),
          attach_mappings = function(prompt_bufnr)
            actions.select_default:replace(function()
              actions.close(prompt_bufnr)
              local selection = action_state.get_selected_entry()
              if selection then
                on_choice(selection[1])
              end
            end)
            return true
          end,
        }):find()
      else
        vim.ui.select(items, {
          prompt = "Select CodeSavant Session to Load:",
        }, function(choice)
          if choice then
            on_choice(choice)
          end
        end)
      end
    end)
  end)
end

--- Manually toggles the visual rendering of markdown overlays on a per-buffer basis
--- @param bufnr integer
function M.toggle_render_markdown(bufnr)
  local rm = get_render_markdown()
  if not rm then
    vim.notify("[CodeSavant] render-markdown.nvim is not installed in your Neovim environment.", vim.log.levels.WARN)
    return
  end

  local is_disabled = vim.b[bufnr].render_markdown_disabled or false
  
  if is_disabled then
    pcall(rm.enable, bufnr)
    vim.b[bufnr].render_markdown_disabled = false
    vim.notify("[CodeSavant] Render-Markdown Enabled", vim.log.levels.INFO)
  else
    pcall(rm.disable, bufnr)
    vim.b[bufnr].render_markdown_disabled = true
    vim.notify("[CodeSavant] Render-Markdown Disabled (Unrendered Raw View)", vim.log.levels.INFO)
  end
end

--- Setup CodeSavant plugin
--- Merges configuration, registers commands, and handles boot configurations.
--- @param opts? table
function M.setup(opts)
  if M._initialized then
    -- Merge options even if called again (idempotent setup updates)
    M.config = vim.tbl_deep_extend("force", M.config, opts or {})
    return
  end

  M.config = vim.tbl_deep_extend("force", DEFAULT_CONFIG, opts or {})

  -- One-time boot check for render-markdown (Zero repeating lookup costs)
  local ok, rm = pcall(require, "render-markdown")
  M._has_render_markdown = ok
  if ok then
    M._render_markdown_api = rm
  end

  -- One-time boot check for Telescope (Zero repeating lookup costs)
  local t_ok, telescope = pcall(require, "telescope.builtin")
  M._has_telescope = t_ok
  if t_ok then
    M._telescope_api = telescope
  end

  -- Native Tree-sitter markdown injection (instant syntax highlights)
  pcall(vim.treesitter.language.register, "markdown", CONSTANTS.FILETYPE)

  -- Register public commands
  vim.api.nvim_create_user_command(CONSTANTS.COMMAND_CHAT, function(cmd_opts)
    local mock_mode = false
    if cmd_opts.args ~= "" then
      local arg = cmd_opts.args:lower():gsub("^%s+", ""):gsub("%s+$", "")
      if arg == "mock" or arg == "--mock" or arg == "-m" then
        mock_mode = true
      elseif arg == "live" or arg == "--live" or arg == "-l" then
        mock_mode = false
      else
        vim.schedule(function()
          vim.notify("[CodeSavant Error] Invalid argument: " .. cmd_opts.args .. ". Use 'mock' or 'live'.", vim.log.levels.ERROR)
        end)
        return
      end
    end

    local result = M.create_chat_buffer()
    -- Start connection and stream sessions in the active split window
    M.start_chat_session(result.bufnr, mock_mode)
  end, {
    nargs = "?",
    complete = function()
      return { "mock", "live" }
    end,
    force = true,
  })

  vim.api.nvim_create_user_command("CodeSavantSessions", function()
    M.select_session()
  end, { force = true })

  vim.api.nvim_create_user_command("CodeSavantHistory", function()
    M.load_session_picker()
  end, { force = true })

  vim.api.nvim_create_user_command("CodeSavantNextSession", function()
    M.cycle_session("next")
  end, { force = true })

  vim.api.nvim_create_user_command("CodeSavantPrevSession", function()
    M.cycle_session("prev")
  end, { force = true })

  vim.api.nvim_create_user_command("CodeSavantBalance", function()
    M.restore_layout_balance()
  end, { force = true })

  -- Initialize layout engine and register split interception
  Layout.init(M.config)
  Layout.setup_split_interception()
  Layout.setup_sidebar_protection()

  -- Register graceful cleanup autocommand to prevent hanging on exit
  local group = vim.api.nvim_create_augroup("CodeSavantCleanup", { clear = true })
  vim.api.nvim_create_autocmd("VimLeavePre", {
    group = group,
    callback = function()
      -- Close all active Libuv sockets to prevent handle hangs
      local Network = require("code_savant.network")
      for bufnr, _ in pairs(Network._connections or {}) do
        pcall(Network.disconnect, bufnr)
      end
      
      -- Terminate the background daemon job
      pcall(M.stop_daemon)
    end
  })

  M._initialized = true
end

return M
