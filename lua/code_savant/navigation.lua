--- @class CodeSavantNavigation
local Navigation = {}

--- Retrieves standard active editing window (non-chat, non-input window)
--- @return integer|nil win_id
local function get_editing_window()
  local current_tab = vim.api.nvim_get_current_tabpage()
  for _, win in ipairs(vim.api.nvim_tabpage_list_wins(current_tab)) do
    if vim.api.nvim_win_is_valid(win) then
      local buf = vim.api.nvim_win_get_buf(win)
      local ft = vim.bo[buf].filetype
      if ft ~= "code_savant_chat" and ft ~= "code_savant_input" then
        return win
      end
    end
  end
  return nil
end

--- Deconstructs a raw path string into file path, line number, and column number.
--- Supports formats like: "path/to/file:42:10" and "path/to/file:42".
--- @param raw_path string
--- @return string clean_path
--- @return integer|nil line_num
--- @return integer|nil col_num
function Navigation.parse_path_string(raw_path)
  local clean_path, line_num, col_num = raw_path, nil, nil
  local base, l, c = raw_path:match("^([^:]+):(%d+):(%d+)$")
  if base then
    clean_path, line_num, col_num = base, tonumber(l), tonumber(c)
  else
    local base2, l2 = raw_path:match("^([^:]+):(%d+)$")
    if base2 then clean_path, line_num = base2, tonumber(l2) end
  end
  return clean_path, line_num, col_num
end

--- Main entry point: intercepts cursor navigation keys and jumps to target.
--- @param open_mode "edit"|"split"|"vsplit"|"tab"
function Navigation.jump_to_file_at_cursor(open_mode)
  -- 1. Grab path under cursor instantly via Neovim's C-level expander
  local cfile = vim.fn.expand("<cfile>")
  if not cfile or cfile == "" then
    vim.notify("[CodeSavant] No file path found under cursor.", vim.log.levels.WARN)
    return
  end

  -- 2. Strip surrounding wrapping characters if any exist (extremely fast O(1) substitute)
  local raw_path = cfile:gsub("^[`'\"%[%(]+", ""):gsub("[`'\"%]%)]+$", "")

  -- 3. Extract path, line number, and column number
  local clean_path, line_num, col_num = Navigation.parse_path_string(raw_path)

  -- 4. Resolve absolute vs workspace-relative path
  local is_absolute = clean_path:sub(1, 1) == "/" or clean_path:sub(1, 1) == "~"
  local resolved = clean_path
  if not is_absolute then
    resolved = vim.fs.normalize(vim.fn.getcwd() .. "/" .. clean_path)
  else
    if clean_path:sub(1, 1) == "~" then
      resolved = vim.fs.normalize(vim.fn.expand(clean_path))
    else
      resolved = vim.fs.normalize(clean_path)
    end
  end

  -- 5. Strict Literal Validation: Try Telescope if file is missing, otherwise notify warning
  if vim.fn.filereadable(resolved) == 0 and vim.fn.isdirectory(resolved) == 0 then
    local cs = require("code_savant")
    local has_telescope = cs._has_telescope
    local telescope = cs._telescope_api

    if has_telescope and telescope then
      local search_text = vim.fs.basename(clean_path)
      if not search_text or search_text == "" then
        search_text = clean_path
      end

      -- Focus standard editor window first to prevent sidebar hijacking upon selection
      local edit_win = get_editing_window()
      if edit_win then
        vim.api.nvim_set_current_win(edit_win)
      else
        vim.cmd("vsplit") -- Fallback split if only the sidebar is visible
        edit_win = vim.api.nvim_get_current_win()
      end

      vim.notify("[CodeSavant] File not found. Opening Telescope for: " .. search_text, vim.log.levels.INFO)
      telescope.find_files({ default_text = search_text })
    else
      vim.notify("[CodeSavant] File not readable or does not exist: " .. clean_path, vim.log.levels.WARN)
    end
    return
  end

  -- 6. Redirect cursor focus out of the locked chat sidebar to a workspace editing window
  local edit_win = get_editing_window()
  if edit_win then
    vim.api.nvim_set_current_win(edit_win)
  else
    vim.cmd("vsplit") -- Fallback if only the sidebar is visible
    edit_win = vim.api.nvim_get_current_win()
  end

  -- Apply target open mode
  if open_mode == "split" then
    vim.cmd("split")
  elseif open_mode == "vsplit" then
    vim.cmd("vsplit")
  elseif open_mode == "tab" then
    vim.cmd("tabnew")
  end

  -- Open file and position cursor
  vim.cmd("edit " .. vim.fn.fnameescape(resolved))
  if line_num then
    local max_line = vim.api.nvim_buf_line_count(0)
    line_num = math.min(line_num, max_line)
    local col_idx = math.max(0, (col_num or 1) - 1)
    pcall(vim.api.nvim_win_set_cursor, 0, { line_num, col_idx })
    vim.cmd("normal! zz")
  end
end

--- Dynamic, Telescope-driven FIFO/Out-of-Order Approval Queue Browser
function Navigation.browse_approvals()
  local cs = require("code_savant")
  if not cs._has_telescope or not cs._telescope_api then
    vim.notify("[CodeSavant] Telescope.nvim is required to browse the approvals queue.", vim.log.levels.WARN)
    return
  end

  local UI = require("code_savant.ui").get_instance()
  local list = {}
  for idx, id in ipairs(UI.pending_approvals.queue) do
    local cached = UI.collapsed_blocks_cache[id]
    if cached and not cached.resolved then
      table.insert(list, {
        idx = idx,
        id = id,
        title = cached.title,
        content = cached.full_content,
        bufnr = cached.bufnr,
        row = cached.row,
        extmark_id = cached.extmark_id,
      })
    end
  end

  if #list == 0 then
    vim.notify("[CodeSavant] No pending approvals in queue.", vim.log.levels.INFO)
    return
  end

  local pickers = require("telescope.pickers")
  local finders = require("telescope.finders")
  local conf = require("telescope.config").values
  local actions = require("telescope.actions")
  local action_state = require("telescope.actions.state")
  local previewers = require("telescope.previewers")

  pickers.new({}, {
    prompt_title = "CodeSavant Pending Approvals Queue",
    finder = finders.new_table({
      results = list,
      entry_maker = function(entry)
        return {
          value = entry,
          display = string.format("[%d] ⚠️  %s", entry.idx, entry.title),
          ordinal = entry.title,
        }
      end,
    }),
    sorter = conf.generic_sorter({}),
    previewer = previewers.new_buffer_previewer({
      title = "Request Context & Arguments",
      define_preview = function(self, entry, status)
        local val = entry.value
        vim.api.nvim_buf_set_lines(self.state.bufnr, 0, -1, false, vim.split(val.content, "\n", { plain = true }))
        vim.api.nvim_buf_set_option(self.state.bufnr, "filetype", "json")
      end,
    }),
    attach_mappings = function(prompt_bufnr, map)
      -- <CR>: Jumps to correct history buffer and line
      actions.select_default:replace(function()
        actions.close(prompt_bufnr)
        local entry = action_state.get_selected_entry()
        if not entry then return end
        local val = entry.value

        local winids = vim.fn.win_findbuf(val.bufnr)
        if winids and #winids > 0 then
          vim.api.nvim_set_current_win(winids[1])
          pcall(vim.api.nvim_win_set_cursor, winids[1], { val.row + 1, 0 })
          vim.cmd("normal! zz")
        end
      end)

      -- <C-y>: Approve out-of-order directly from Telescope
      map({ "i", "n" }, "<C-y>", function()
        local entry = action_state.get_selected_entry()
        if not entry then return end
        local val = entry.value

        UI:execute_resolution(val.id, true)
        actions.close(prompt_bufnr)

        vim.schedule(function()
          Navigation.browse_approvals()
        end)
      end)

      -- <C-n>: Decline out-of-order directly from Telescope
      map({ "i", "n" }, "<C-n>", function()
        local entry = action_state.get_selected_entry()
        if not entry then return end
        local val = entry.value

        UI:execute_resolution(val.id, false)
        actions.close(prompt_bufnr)

        vim.schedule(function()
          Navigation.browse_approvals()
        end)
      end)

      return true
    end,
  }):find()
end

function Navigation.show_picker(opts)
  local cs = require("code_savant")
  if cs._has_telescope and cs._telescope_api then
    local pickers = require("telescope.pickers")
    local finders = require("telescope.finders")
    local conf = require("telescope.config").values
    local actions = require("telescope.actions")
    local action_state = require("telescope.actions.state")

    pickers.new({}, {
      prompt_title = opts.title,
      finder = finders.new_table({
        results = opts.results,
        entry_maker = opts.entry_maker,
      }),
      sorter = conf.generic_sorter({}),
      attach_mappings = function(prompt_bufnr)
        actions.select_default:replace(function()
          actions.close(prompt_bufnr)
          local selection = action_state.get_selected_entry()
          if selection then
            opts.on_select(selection.value)
          end
        end)
        return true
      end,
    }):find()
  else
    local labels = {}
    local lookup = {}
    for _, item in ipairs(opts.results) do
      local formatted = opts.entry_maker(item)
      table.insert(labels, formatted.display)
      lookup[formatted.display] = item
    end
    vim.ui.select(labels, { prompt = opts.title }, function(choice)
      if choice and lookup[choice] then
        opts.on_select(lookup[choice])
      end
    end)
  end
end

function Navigation.browse_sessions(filter_mode)
  filter_mode = filter_mode or "all"
  local cs = require("code_savant")
  local UI = require("code_savant.ui").get_instance()
  local Network = require("code_savant.network")
  local socket_path = cs.config.socket_path or "/tmp/code_savant.sock"
  local workspace_path = vim.fn.getcwd()

  local function display_merged_sessions(saved_sessions)
    saved_sessions = saved_sessions or {}
    local merged = {}
    local seen_session_ids = {}

    local active_connections = {}
    for bufnr, conn in pairs(Network._connections or {}) do
      if conn.session_id and conn.session_id ~= "" then
        active_connections[conn.session_id] = bufnr
      end
    end

    for _, s in ipairs(saved_sessions) do
      local s_id = s.session_id
      local parent_id = s.metadata and s.metadata.parent_session_id
      if type(parent_id) ~= "string" or parent_id == "" then
        parent_id = nil
      end
      local is_sub = (parent_id ~= nil)
      local active_buf = active_connections[s_id]

      seen_session_ids[s_id] = true

      table.insert(merged, {
        session_id = s_id,
        parent_id = parent_id,
        agent_name = s.metadata and s.metadata.name or "Untitled Session",
        status = active_buf and "active" or "saved",
        is_active = (active_buf ~= nil),
        is_subagent = is_sub,
        turn_count = s.turn_count or 0,
        last_updated = s.metadata and (s.metadata.last_updated or s.metadata.created_at) or "Unknown Date",
      })
    end

    for s_id, s in pairs(UI.sessions) do
      if not seen_session_ids[s_id] then
        local active_buf = active_connections[s_id]
        local parent_id = s.parent_id
        if type(parent_id) ~= "string" or parent_id == "" then
          parent_id = nil
        end
        local is_sub = (parent_id ~= nil)

        table.insert(merged, {
          session_id = s_id,
          parent_id = parent_id,
          agent_name = s.agent_name or "Untitled Session",
          status = s.status or (active_buf and "active" or "idle"),
          is_active = (active_buf ~= nil or s.status == "thinking"),
          is_subagent = is_sub,
          turn_count = s.turn_count or (s.history and #s.history) or 0,
          last_updated = "Live Session",
        })
      end
    end

    local filtered = {}
    for _, item in ipairs(merged) do
      local keep = false
      if filter_mode == "all" then
        keep = not item.is_subagent
      elseif filter_mode == "active" then
        keep = item.is_active
      elseif filter_mode == "subagents" then
        keep = item.is_subagent
      end

      if keep then
        table.insert(filtered, item)
      end
    end

    if #filtered == 0 then
      vim.notify("[CodeSavant] No sessions found matching filter '" .. filter_mode .. "'.", vim.log.levels.INFO)
      return
    end

    local picker_title = "CodeSavant Sessions (" .. filter_mode .. ")"
    Navigation.show_picker({
      title = picker_title,
      results = filtered,
      entry_maker = function(item)
        local status_sym = "󰄬 [SAVED]"
        if item.is_active then
          status_sym = (item.status == "thinking") and "󰒋 [RUNNING]" or "● [ACTIVE]"
        end

        local clean_date = item.last_updated:gsub("T", " "):gsub("%.%d+", "")
        local label = string.format("%s - %s (%s) - %d turns", status_sym, item.agent_name, clean_date, item.turn_count)
        return {
          value = item,
          display = label,
          ordinal = item.agent_name .. " " .. item.status .. " " .. clean_date,
        }
      end,
      on_select = function(selected)
        cs.open_session(selected.session_id, selected.parent_id)
      end,
    })
  end

  cs.ensure_daemon_running(function(success, _)
    if not success then
      display_merged_sessions({})
      return
    end

    Network.list_sessions(socket_path, workspace_path, function(sessions, list_err)
      if list_err then
        display_merged_sessions({})
        return
      end
      display_merged_sessions(sessions)
    end)
  end)
end

return Navigation
