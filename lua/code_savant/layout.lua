--- @class CodeSavantLayout
local Layout = {}

-- Flag to prevent infinite recursive interception loops
Layout._spawning_layout = false

-- Config state injected from the setup block
Layout.config = {}

-- Load core client dependencies safely
local Network = require("code_savant.network")
local UI = require("code_savant.ui").get_instance()

--- Sets the configuration for the layout module
--- @param config table
function Layout.init(config)
  Layout.config = config or {}
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

--- Scans the current tabpage for any active CodeSavant layout split windows.
--- @return integer|nil hist_win, integer|nil inp_win
function Layout.find_visible_layout()
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

--- Restores Code Savant split windows to their default configured dimensions
function Layout.restore_layout_balance()
  local hist_win, inp_win = Layout.find_visible_layout()

  if hist_win and inp_win then
    local input_height = Layout.config.input_height or 3
    local sidebar_width_pct = Layout.config.sidebar_width_pct or 0.5
    local dynamic_width = math.floor(vim.o.columns * sidebar_width_pct)
    
    -- Restore input box height split
    vim.api.nvim_win_set_height(inp_win, input_height)
    
    -- Restore dynamic vertical split width
    vim.api.nvim_win_set_width(hist_win, dynamic_width)
    vim.api.nvim_win_set_width(inp_win, dynamic_width)
  end
end

--- Mounts a history buffer and its linked partner input buffer into the layout.
--- @param history_bufnr integer
--- @param target_winid? integer
--- @param apply_config_cb fun(hist_buf: integer, inp_buf: integer) Callback to configure keymaps
function Layout.mount_session(history_bufnr, target_winid, apply_config_cb)
  if not vim.api.nvim_buf_is_valid(history_bufnr) then
    error("[CodeSavant Error] Invalid history buffer provided for mounting")
  end

  local input_bufnr = vim.b[history_bufnr].partner_buf
  if not input_bufnr or not vim.api.nvim_buf_is_valid(input_bufnr) then
    error("[CodeSavant Error] Partner input buffer not found for history buffer " .. tostring(history_bufnr))
  end

  if apply_config_cb then
    apply_config_cb(history_bufnr, input_bufnr)
  end

  local hist_win, inp_win = Layout.find_visible_layout()

  if hist_win and inp_win then
    safe_set_buf(hist_win, history_bufnr)
    safe_set_buf(inp_win, input_bufnr)

    vim.b[history_bufnr].partner_win = inp_win
    vim.b[input_bufnr].partner_win = hist_win

    vim.api.nvim_set_current_win(inp_win)
    return
  end

  local spawn_split = Layout.config.spawn_split or "vsplit"
  local new_hist_win, new_inp_win

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

  local target_win = target_winid or vim.api.nvim_get_current_win()
  if not vim.api.nvim_win_is_valid(target_win) then
    target_win = vim.api.nvim_get_current_win()
  end

  Layout._spawning_layout = true

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
    new_hist_win = vim.api.nvim_open_win(history_bufnr, false, {
      win = target_win,
      split = direction,
    })
  end

  local input_height = Layout.config.input_height or 3

  new_inp_win = vim.api.nvim_open_win(input_bufnr, false, {
    win = new_hist_win,
    split = "below",
    height = input_height,
  })

  vim.api.nvim_win_set_height(new_inp_win, input_height)
  vim.wo[new_inp_win].number = false
  vim.wo[new_inp_win].relativenumber = false

  if spawn_split == "vsplit" then
    local dynamic_width = math.floor(vim.o.columns * (Layout.config.sidebar_width_pct or 0.5))
    vim.api.nvim_win_set_width(new_hist_win, dynamic_width)
    vim.api.nvim_win_set_width(new_inp_win, dynamic_width)
  end

  Layout._spawning_layout = false

  vim.b[history_bufnr].partner_win = new_inp_win
  vim.b[input_bufnr].partner_win = new_hist_win

  safe_set_buf(new_hist_win, history_bufnr)
  safe_set_buf(new_inp_win, input_bufnr)

  local win_group_name = "CodeSavantWinSync_" .. tostring(new_hist_win) .. "_" .. tostring(new_inp_win)
  local win_group = vim.api.nvim_create_augroup(win_group_name, { clear = true })

  local closed_in_progress = false

  local function handle_win_closed(is_hist)
    if closed_in_progress then return end
    closed_in_progress = true
    vim.schedule(function()
      local tab = vim.api.nvim_get_current_tabpage()
      local wins = vim.api.nvim_tabpage_list_wins(tab)
      local valid_wins = {}
      for _, w in ipairs(wins) do
        if vim.api.nvim_win_is_valid(w) then
          table.insert(valid_wins, w)
        end
      end

      -- If no standard editing splits remain (only Code Savant is left on screen), trigger cleanup
      if #valid_wins <= 2 then
        local only_savant = true
        for _, w in ipairs(valid_wins) do
          local b = vim.api.nvim_win_get_buf(w)
          local ft = vim.bo[b].filetype
          if ft ~= "code_savant_chat" and ft ~= "code_savant_input" then
            only_savant = false
            break
          end
        end
        if only_savant then
          pcall(Network.disconnect, history_bufnr)
          pcall(UI.teardown, UI)
          pcall(vim.api.nvim_buf_delete, input_bufnr, { force = true })
          pcall(vim.api.nvim_buf_delete, history_bufnr, { force = true })
          closed_in_progress = false
          return
        end
      end

      local partner = is_hist and new_inp_win or new_hist_win
      if vim.api.nvim_win_is_valid(partner) then
        pcall(vim.api.nvim_win_close, partner, true)
      end
      pcall(vim.api.nvim_del_augroup_by_id, win_group)
      closed_in_progress = false
    end)
  end

  vim.api.nvim_create_autocmd("WinClosed", {
    pattern = tostring(new_hist_win),
    group = win_group,
    callback = function() handle_win_closed(true) end,
  })

  vim.api.nvim_create_autocmd("WinClosed", {
    pattern = tostring(new_inp_win),
    group = win_group,
    callback = function() handle_win_closed(false) end,
  })

  vim.api.nvim_set_current_win(new_inp_win)
end

--- Registers the split interception engine with dynamic split-type detection and self-healing.
function Layout.setup_split_interception()
  local split_group = vim.api.nvim_create_augroup("CodeSavantSplitInterception", { clear = true })
  vim.api.nvim_create_autocmd("WinNew", {
    group = split_group,
    callback = function()
      if Layout._spawning_layout then return end

      vim.schedule(function()
        if Layout._spawning_layout then return end
        local new_win = vim.api.nvim_get_current_win()
        if not vim.api.nvim_win_is_valid(new_win) then return end
        
        local cur_buf = vim.api.nvim_win_get_buf(new_win)
        local ft = vim.bo[cur_buf].filetype
        
        if ft == "code_savant_chat" or ft == "code_savant_input" then
          -- Identify which sidebar window was the parent
          local hist_win, inp_win = Layout.find_visible_layout()
          local parent_win = (ft == "code_savant_chat") and hist_win or inp_win
          if not parent_win or not vim.api.nvim_win_is_valid(parent_win) then return end

          -- Detect the exact split kind (vsplit vs split) geometrically
          local pos_new = vim.api.nvim_win_get_position(new_win)
          local pos_parent = vim.api.nvim_win_get_position(parent_win)
          
          local split_cmd = "vsplit"
          if pos_new[1] ~= pos_parent[1] then
            split_cmd = "split"
          end

          -- Close the invalid division inside the sidebar instantly
          pcall(vim.api.nvim_win_close, new_win, true)

          -- Locate last active standard editing split
          local code_win = nil
          for _, win in ipairs(vim.api.nvim_list_wins()) do
            if vim.api.nvim_win_is_valid(win) then
              local b = vim.api.nvim_win_get_buf(win)
              local bft = vim.bo[b].filetype
              if bft ~= "code_savant_chat" and bft ~= "code_savant_input" then
                code_win = win
                break
              end
            end
          end

          Layout._spawning_layout = true

          if code_win then
            -- Case A: Standard edit window exists -> Re-emit identical split kind there
            vim.api.nvim_set_current_win(code_win)
            vim.cmd(split_cmd)
          else
            -- Case B: Self-Healing (No edit window left) -> Spawn identical split next to sidebar
            if hist_win and vim.api.nvim_win_is_valid(hist_win) then
              vim.api.nvim_set_current_win(hist_win)
              vim.cmd(split_cmd)
              vim.cmd("enew") -- Open blank scratch buffer
            end
          end

          Layout._spawning_layout = false
          Layout.restore_layout_balance()
        end
      end)
    end
  })
end

return Layout
