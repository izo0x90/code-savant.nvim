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

return Navigation
