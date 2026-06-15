--- @class CodeSavant
--- @field config table
--- @field _initialized boolean
--- @field bootstrap_in_progress boolean
--- @field daemon_job_id integer|nil
local M = {}

local Network = require("code_savant.network")
local UI = require("code_savant.ui").get_instance()

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

--- Default Preferences
--- Adheres to Guideline 3: Top-level default configurations.
local DEFAULT_CONFIG = {
  socket_path = CONSTANTS.DEFAULT_SOCKET_PATH,
  spawn_split = "vsplit", -- Options: "vsplit" (vertical), "split" (horizontal), "tabnew" (new tab), "edit" (current window)
  keymaps = {
    expand = "<CR>",  -- Key to expand collapsed virtual thought blocks
    submit = "<CR>",  -- Key to submit prompt lines at the bottom of the buffer
    next_session = "<S-L>", -- Local normal-mode cycle forward
    prev_session = "<S-H>", -- Local normal-mode cycle backward
    open_buffer = "gO",     -- Key to open thought space in a new buffer split
    open_float = "K",       -- Key to open thought space in a floating window
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
  -- Run as package module to avoid shadow types.py collision with Python standard library
  -- Note: Do NOT use "--server" as it is an unrecognized argument and causes the daemon to crash (argparse error).
  local cmd = { python_bin, "-m", "engine.main", "--socket-path", socket_path }

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
    if line:find("◀ CodeSavant is thinking...", 1, true) then
      return i - 1 -- 0-indexed row
    end
  end
  return nil
end

--- Asynchronously starts/ensures daemon is running, connects to the UDS pipe,
--- and registers network handlers.
--- @param bufnr integer
--- @param mock_mode? boolean Optional mock mode flag
function M.start_chat_session(bufnr, mock_mode)
  M.ensure_daemon_running(function(success, err_msg)
    if not success then
      vim.schedule(function()
        vim.notify(err_msg or "[CodeSavant Error] Unable to connect to background mock daemon.", vim.log.levels.ERROR)
      end)
      return
    end

    -- Connect over socket pipe and initiate session
    local socket_path = M.config.socket_path or CONSTANTS.DEFAULT_SOCKET_PATH
    Network.connect(socket_path, bufnr, vim.fn.getcwd(), mock_mode)
    
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

          -- Match incoming server notifications
          if parsed.method == "telemetry/collapsed_block" then
            local params = parsed.params or {}
            local thinking_row = find_thinking_line(bufnr)
            if thinking_row then
              -- Shifting thinking indicator down by inserting an empty line at its row,
              -- and anchoring the collapsed block extmark on the newly inserted row.
              vim.api.nvim_buf_set_lines(bufnr, thinking_row, thinking_row, false, { "" })
              UI:on_collapsed_block(params.id, params.type, params.title, params.full_content, bufnr, thinking_row)
            else
              local line_count = vim.api.nvim_buf_line_count(bufnr)
              local target_row = math.max(0, line_count - 1)
              vim.api.nvim_buf_set_lines(bufnr, target_row, target_row, false, { "" })
              UI:on_collapsed_block(params.id, params.type, params.title, params.full_content, bufnr, target_row)
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
            if params.status == "idle" then
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
    table.insert(to_append, "◀ CodeSavant is thinking...")
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

  -- Clear input buffer
  vim.api.nvim_buf_set_lines(input_bufnr, 0, -1, false, {})

  -- Send prompt
  Network.send_prompt(conn, prompt_text)
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

    -- Defer buffer deletions and window closures to run asynchronously outside the synchronous wipeout phase
    vim.schedule(function()
      -- Wipe out input buffer if valid
      if vim.api.nvim_buf_is_valid(input_bufnr) then
        pcall(vim.api.nvim_buf_delete, input_bufnr, { force = true })
      end
      -- Wipe out history buffer if valid
      if vim.api.nvim_buf_is_valid(history_bufnr) then
        pcall(vim.api.nvim_buf_delete, history_bufnr, { force = true })
      end

      -- Close any active windows that are currently showing either of these buffers
      for _, win in ipairs(vim.api.nvim_list_wins()) do
        if vim.api.nvim_win_is_valid(win) then
          local b = vim.api.nvim_win_get_buf(win)
          if b == history_bufnr or b == input_bufnr then
            pcall(vim.api.nvim_win_close, win, true)
          end
        end
      end
    end)

    pcall(vim.api.nvim_del_augroup_by_id, group)
  end

  vim.api.nvim_create_autocmd("BufWipeout", {
    buffer = history_bufnr,
    group = group,
    callback = cleanup_all,
    once = true,
  })

  vim.api.nvim_create_autocmd("BufWipeout", {
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

--- Scans the current tabpage for any active CodeSavant layout split windows.
--- @return integer|nil hist_win, integer|nil inp_win
local function find_visible_layout()
  local current_tab = vim.api.nvim_get_current_tabpage()
  local hist_win, inp_win = nil, nil
  for _, win in ipairs(vim.api.nvim_tabpage_list_wins(current_tab)) do
    if vim.api.nvim_win_is_valid(win) then
      local buf = vim.api.nvim_win_get_buf(win)
      local ft = vim.bo[buf].filetype
      if ft == "code_savant_chat" then
        hist_win = win
      elseif ft == "code_savant_input" then
        inp_win = win
      end
    end
  end
  return hist_win, inp_win
end


--- Mounts a history buffer and its linked partner input buffer into the layout.
--- If active CodeSavant split windows are already visible, they are safely reused in-place.
--- Otherwise, it creates the initial split windows and applies the locked winfixbuf protection.
--- @param history_bufnr integer
--- @param target_winid? integer
function M.mount_session(history_bufnr, target_winid)
  if not vim.api.nvim_buf_is_valid(history_bufnr) then
    error("[CodeSavant Error] Invalid history buffer provided for mounting")
  end

  local input_bufnr = vim.b[history_bufnr].partner_buf
  if not input_bufnr or not vim.api.nvim_buf_is_valid(input_bufnr) then
    error("[CodeSavant Error] Partner input buffer not found for history buffer " .. tostring(history_bufnr))
  end

  -- Dynamically restore options, variables, abbreviations and keymaps (survives unloading on ZQ)
  apply_buffer_config(history_bufnr, input_bufnr)

  -- 1. Check if a CodeSavant window split layout is already visible on the current tabpage.
  local hist_win, inp_win = find_visible_layout()

  if hist_win and inp_win then
    -- Reuse existing visible windows in-place!
    safe_set_buf(hist_win, history_bufnr)
    safe_set_buf(inp_win, input_bufnr)

    vim.b[history_bufnr].partner_win = inp_win
    vim.b[input_bufnr].partner_win = hist_win

    vim.api.nvim_set_current_win(inp_win)
    return
  end

  -- If only a partial or no layout is reusable, cleanly close any existing CodeSavant windows
  -- on the current tabpage first to prevent layout duplication and leaks.
  local current_tab = vim.api.nvim_get_current_tabpage()
  for _, win in ipairs(vim.api.nvim_tabpage_list_wins(current_tab)) do
    if vim.api.nvim_win_is_valid(win) then
      local buf = vim.api.nvim_win_get_buf(win)
      local ft = vim.bo[buf].filetype
      if ft == "code_savant_chat" or ft == "code_savant_input" then
        pcall(vim.api.nvim_win_close, win, true)
      end
    end
  end

  -- 2. No layout is visible: construct the splits programmatically.
  local target_win = target_winid or vim.api.nvim_get_current_win()
  if not vim.api.nvim_win_is_valid(target_win) then
    target_win = vim.api.nvim_get_current_win()
  end

  local spawn_split = M.config.spawn_split or "vsplit"
  local new_hist_win

  if spawn_split == "tabnew" then
    vim.api.nvim_cmd({ cmd = "tabnew" }, {})
    target_win = vim.api.nvim_get_current_win()
    new_hist_win = target_win
  elseif spawn_split == "edit" then
    new_hist_win = target_win
  else
    local direction = "right"
    if spawn_split == "split" or spawn_split == "hsplit" then
      direction = "below"
    end
    -- Create history window programmatically relative to target_win with buffer pre-set!
    new_hist_win = vim.api.nvim_open_win(history_bufnr, false, {
      win = target_win,
      split = direction,
    })
  end

  -- Create input window programmatically below new_hist_win with input buffer pre-set!
  local new_inp_win = vim.api.nvim_open_win(input_bufnr, false, {
    win = new_hist_win,
    split = "below",
    height = 3,
  })

  -- Style the newly created input split window
  vim.api.nvim_win_set_height(new_inp_win, 3)
  vim.wo[new_inp_win].winfixheight = true
  vim.wo[new_inp_win].number = false
  vim.wo[new_inp_win].relativenumber = false

  -- Bind partner metadata
  vim.b[history_bufnr].partner_win = new_inp_win
  vim.b[input_bufnr].partner_win = new_hist_win

  -- Set buffers and activate winfixbuf AFTER all splits are fully done!
  safe_set_buf(new_hist_win, history_bufnr)
  safe_set_buf(new_inp_win, input_bufnr)

  -- Create clean WinClosed window-linked closing autocommands.
  -- This ensures closing either window (e.g. via <C-w>q, :q, :close) automatically closes the partner window,
  -- but leaves the buffers alive and loaded in memory for toggle/reuse.
  local win_group_name = "CodeSavantWinSync_" .. tostring(new_hist_win) .. "_" .. tostring(new_inp_win)
  local win_group = vim.api.nvim_create_augroup(win_group_name, { clear = true })

  local closed_in_progress = false

  vim.api.nvim_create_autocmd("WinClosed", {
    pattern = tostring(new_hist_win),
    group = win_group,
    callback = function()
      if closed_in_progress then return end
      closed_in_progress = true
      vim.schedule(function()
        if vim.api.nvim_win_is_valid(new_inp_win) then
          pcall(vim.api.nvim_win_close, new_inp_win, true)
        end
        pcall(vim.api.nvim_del_augroup_by_id, win_group)
      end)
    end,
  })

  vim.api.nvim_create_autocmd("WinClosed", {
    pattern = tostring(new_inp_win),
    group = win_group,
    callback = function()
      if closed_in_progress then return end
      closed_in_progress = true
      vim.schedule(function()
        if vim.api.nvim_win_is_valid(new_hist_win) then
          pcall(vim.api.nvim_win_close, new_hist_win, true)
        end
        pcall(vim.api.nvim_del_augroup_by_id, win_group)
      end)
    end,
  })

  -- Focus the input window for typing
  vim.api.nvim_set_current_win(new_inp_win)
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

  vim.api.nvim_create_user_command("CodeSavantNextSession", function()
    M.cycle_session("next")
  end, { force = true })

  vim.api.nvim_create_user_command("CodeSavantPrevSession", function()
    M.cycle_session("prev")
  end, { force = true })

  M._initialized = true
end

return M
