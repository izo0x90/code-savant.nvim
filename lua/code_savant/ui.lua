--- @class CodeSavantUI
--- @field api table Neovim API client (usually vim.api or injected)
--- @field namespace integer Namespace ID for extmarks
--- @field collapsed_blocks_cache table<string, table> Cached collapsed block content and metadata
local UI = {}
UI.__index = UI

--- Centralized Configuration Constants
UI.CONSTANTS = {
  NAMESPACE_NAME = "code_savant_ui",
  GLYPH_COLLAPSED = "▶ Thought: ",
  HIGHLIGHT_GROUP_COLLAPSED = "Comment",
  BORDER_TOP = "╭────────────────────────Thought Space────────────────────────╮",
  BORDER_BOTTOM = "╰─────────────────────────────────────────────────────────────╯",
}

-- Default module-level instance for simple access
local default_instance = nil

--- Create a new UI manager instance (Dependency Injection)
--- @param api? table Optional injected API client, defaults to vim.api
--- @return CodeSavantUI
function UI.new(api)
  local self = setmetatable({}, UI)
  self.api = api or vim.api
  self.collapsed_blocks_cache = {}

  -- Create namespace using injected API or vim.api
  self.namespace = self.api.nvim_create_namespace(UI.CONSTANTS.NAMESPACE_NAME)

  return self
end

--- Initialize the UI layout (satisfying skeletal init function)
--- @param api? table
--- @return CodeSavantUI
function UI.init(api)
  default_instance = UI.new(api)
  return default_instance
end

--- Get the default initialized UI instance
--- @return CodeSavantUI
function UI.get_instance()
  if not default_instance then
    default_instance = UI.new()
  end
  return default_instance
end

--- Create and configure a dedicated chat history buffer (natively read-only)
--- @return integer bufnr The created buffer number
function UI:create_chat_buffer()
  local bufnr = self.api.nvim_create_buf(false, true) -- listed=false, scratch=true
  if not bufnr or bufnr == 0 then
    error("[CodeSavantUI] Failed to create chat buffer.")
  end

  -- Set buffer options for a chat log
  self.api.nvim_buf_set_option(bufnr, "buftype", "nofile")
  self.api.nvim_buf_set_option(bufnr, "bufhidden", "hide")
  self.api.nvim_buf_set_option(bufnr, "swapfile", false)
  self.api.nvim_buf_set_option(bufnr, "filetype", "code_savant_chat")
  self.api.nvim_buf_set_option(bufnr, "modifiable", false) -- Natively Read-Only!

  return bufnr
end

--- Asynchronously handles collapsed events, caches full content, and anchors an extmark.
--- @param id string Unique block identifier
--- @param block_type string Type of block (e.g. "thought", "code")
--- @param title string Human-readable title
--- @param full_content string Full block content to be cached
--- @param bufnr? integer Buffer number, defaults to current active buffer
--- @param row? integer Target row index (0-indexed). If not specified, appends a new line.
--- @return integer|nil extmark_id The created extmark ID, or nil if validation fails
function UI:_on_collapsed_block_impl(id, block_type, title, full_content, bufnr, row)
  -- 1. Validate parameters
  if not id or type(id) ~= "string" or id == "" then
    return nil
  end
  if not block_type or type(block_type) ~= "string" or block_type == "" then
    return nil
  end
  if not title or type(title) ~= "string" or title == "" then
    return nil
  end
  if not full_content or type(full_content) ~= "string" or full_content == "" then
    return nil
  end

  -- 2. Verify target chat buffer
  bufnr = bufnr or self.api.nvim_get_current_buf()
  if not bufnr or bufnr == 0 or not self.api.nvim_buf_is_valid(bufnr) then
    error("[CodeSavantUI] Target buffer is invalid or closed: " .. tostring(bufnr))
  end

  -- 3. Cache block content and metadata (preliminary step before extmark ID)
  self.collapsed_blocks_cache[id] = {
    full_content = full_content,
    type = block_type,
    title = title,
    bufnr = bufnr,
    status = "collapsed", -- Initial state is collapsed
    height = nil,
  }

  -- 4. Idempotently clear existing extmark
  local cached = self.collapsed_blocks_cache[id]
  if cached and cached.extmark_id then
    pcall(self.api.nvim_buf_del_extmark, bufnr, self.namespace, cached.extmark_id)
  end

  -- 5. Resolve row coordinate and ensure it exists
  local extmark_id = nil
  self:run_programmatic_update(bufnr, function()
    local target_row = row
    if not target_row then
      local line_count = self.api.nvim_buf_line_count(bufnr)
      self.api.nvim_buf_set_lines(bufnr, line_count, line_count, false, { "" })
      target_row = line_count
    else
      local line_count = self.api.nvim_buf_line_count(bufnr)
      if target_row >= line_count then
        local dummy_lines = {}
        for _ = line_count, target_row do
          table.insert(dummy_lines, "")
        end
        self.api.nvim_buf_set_lines(bufnr, line_count, line_count, false, dummy_lines)
      end
    end

    -- 6. Format collapsed line marker dynamically based on block_type
    local prefix = "▶ Thought: "
    local hl_group = "Comment"

    if block_type == "warning" then
      prefix = "▶ Warning: "
      hl_group = "DiagnosticWarn"
    elseif block_type == "steering" then
      prefix = "▶ Steering: "
      hl_group = "Identifier"
    elseif block_type == "error" then
      prefix = "▶ Error: "
      hl_group = "DiagnosticError"
    elseif block_type == "confirmation" then
      prefix = "▶ Approve/Decline: "
      hl_group = "DiagnosticWarn"
    elseif block_type == "tool" then
      prefix = "▶ Tool: "
      hl_group = "Special"
    end

    local display_text = prefix .. title

    -- 7. Anchor extmark with virtual text using overlay position
    extmark_id = self.api.nvim_buf_set_extmark(bufnr, self.namespace, target_row, 0, {
      virt_text = { { display_text, hl_group } },
      virt_text_pos = "overlay",
    })

    -- 8. Cache extmark association
    self.collapsed_blocks_cache[id].extmark_id = extmark_id
    self.collapsed_blocks_cache[id].row = target_row
  end)

  return extmark_id
end

--- Asynchronously expands cached contents inline under the corresponding target extmark, recalculating range dimensions cleanly.
--- @param opts table Arguments table containing 'id'
function UI:_expand_inplace_impl(opts)
  -- 1. Validate parameters
  if not opts or type(opts) ~= "table" or not opts.id or type(opts.id) ~= "string" or opts.id == "" then
    error("[CodeSavantUI] Invalid parameter: 'id' is required and must be a non-empty string.")
  end
  local id = opts.id

  -- 2. Lookup cached block
  local cached = self.collapsed_blocks_cache[id]
  if not cached then
    error("[CodeSavantUI] Cache miss: No collapsed block found with ID: " .. tostring(id))
  end

  local bufnr = cached.bufnr
  if not self.api.nvim_buf_is_valid(bufnr) then
    error("[CodeSavantUI] Cannot expand: Target buffer " .. tostring(bufnr) .. " is invalid or closed.")
  end

  -- 3. Locate extmark position
  local pos = self.api.nvim_buf_get_extmark_by_id(bufnr, self.namespace, cached.extmark_id, {})
  if not pos or #pos == 0 then
    error("[CodeSavantUI] Extmark not found or unresolvable for block ID: " .. tostring(id) .. ", extmark ID: " .. tostring(cached.extmark_id))
  end
  local row = pos[1]

  -- 4. Format expanded content
  local lines = vim.split(cached.full_content, "\n", { plain = true })
  local display_lines = {}
  if UI.CONSTANTS.BORDER_TOP and UI.CONSTANTS.BORDER_TOP ~= "" then
    table.insert(display_lines, UI.CONSTANTS.BORDER_TOP)
  end
  for _, line in ipairs(lines) do
    table.insert(display_lines, line)
  end
  if UI.CONSTANTS.BORDER_BOTTOM and UI.CONSTANTS.BORDER_BOTTOM ~= "" then
    table.insert(display_lines, UI.CONSTANTS.BORDER_BOTTOM)
  end

  local height = #display_lines

  -- 5. Replace buffer lines atomically
  self:run_programmatic_update(bufnr, function()
    self.api.nvim_buf_set_lines(bufnr, row, row + 1, false, display_lines)
    
    -- 6. Clean up old collapsed extmark
    pcall(self.api.nvim_buf_del_extmark, bufnr, self.namespace, cached.extmark_id)

    -- 7. Anchor new expanded tracking extmark at the top of the block with indicator above it as a virtual line
    local prefix = "▼ Thought: "
    local hl_group = "Comment"

    if cached.type == "warning" then
      prefix = "▼ Warning: "
      hl_group = "DiagnosticWarn"
    elseif cached.type == "steering" then
      prefix = "▼ Steering: "
      hl_group = "Identifier"
    elseif cached.type == "error" then
      prefix = "▼ Error: "
      hl_group = "DiagnosticError"
    elseif cached.type == "confirmation" then
      prefix = "▼ Approve/Decline: "
      hl_group = "DiagnosticWarn"
    elseif cached.type == "tool" then
      prefix = "▼ Tool: "
      hl_group = "Special"
    end

    local indicator = prefix .. cached.title
    local extmark_id = self.api.nvim_buf_set_extmark(bufnr, self.namespace, row, 0, {
      virt_lines = { { { indicator, hl_group } } },
      virt_lines_above = true,
    })

    -- 8. Update cache entry status and dimensions
    self.collapsed_blocks_cache[id].extmark_id = extmark_id
    self.collapsed_blocks_cache[id].status = "expanded"
    self.collapsed_blocks_cache[id].height = height
  end)
end

--- Asynchronously collapses expanded contents back to an inline extmark, restoring layout vertical height cleanly.
--- @param opts table Arguments table containing 'id'
function UI:_collapse_inplace_impl(opts)
  -- 1. Validate parameters
  if not opts or type(opts) ~= "table" or not opts.id or type(opts.id) ~= "string" or opts.id == "" then
    error("[CodeSavantUI] Invalid parameter: 'id' is required and must be a non-empty string.")
  end
  local id = opts.id

  -- 2. Lookup cached block
  local cached = self.collapsed_blocks_cache[id]
  if not cached then
    error("[CodeSavantUI] Cache miss: No collapsed block found with ID: " .. tostring(id))
  end

  local bufnr = cached.bufnr
  if not self.api.nvim_buf_is_valid(bufnr) then
    error("[CodeSavantUI] Cannot collapse: Target buffer " .. tostring(bufnr) .. " is invalid or closed.")
  end

  -- 3. Locate extmark position
  local pos = self.api.nvim_buf_get_extmark_by_id(bufnr, self.namespace, cached.extmark_id, {})
  if not pos or #pos == 0 then
    error("[CodeSavantUI] Extmark not found or unresolvable for block ID: " .. tostring(id) .. ", extmark ID: " .. tostring(cached.extmark_id))
  end
  local row = pos[1]

  local height = cached.height
  if not height then
    error("[CodeSavantUI] Cannot collapse: Height is missing for expanded block ID: " .. tostring(id))
  end

  -- 4. Replace buffer lines atomically back to single empty line
  self:run_programmatic_update(bufnr, function()
    self.api.nvim_buf_set_lines(bufnr, row, row + height, false, { "" })

    -- 5. Clean up old expanded extmark
    pcall(self.api.nvim_buf_del_extmark, bufnr, self.namespace, cached.extmark_id)

    -- 6. Anchor new collapsed tracking extmark with overlay indicator
    local display_text = UI.CONSTANTS.GLYPH_COLLAPSED .. cached.title
    local extmark_id = self.api.nvim_buf_set_extmark(bufnr, self.namespace, row, 0, {
      virt_text = { { display_text, UI.CONSTANTS.HIGHLIGHT_GROUP_COLLAPSED } },
      virt_text_pos = "overlay",
    })

    -- 7. Update cache entry status and dimensions
    self.collapsed_blocks_cache[id].extmark_id = extmark_id
    self.collapsed_blocks_cache[id].status = "collapsed"
    self.collapsed_blocks_cache[id].height = nil
  end)
end

--- Open cached block contents in a new vertical split buffer.
--- @param opts table Arguments table containing 'id'
function UI:_open_in_new_buf_impl(opts)
  -- 1. Validate parameters
  if not opts or type(opts) ~= "table" or not opts.id or type(opts.id) ~= "string" or opts.id == "" then
    error("[CodeSavantUI] Invalid parameter: 'id' is required and must be a non-empty string.")
  end
  local id = opts.id

  -- 2. Lookup cached block
  local cached = self.collapsed_blocks_cache[id]
  if not cached then
    error("[CodeSavantUI] Cache miss: No collapsed block found with ID: " .. tostring(id))
  end

  -- 3. Create a clean, scratch buffer
  local buf = self.api.nvim_create_buf(false, true)
  self.api.nvim_buf_set_option(buf, "buftype", "nofile")
  self.api.nvim_buf_set_option(buf, "swapfile", false)
  self.api.nvim_buf_set_option(buf, "bufhidden", "wipe")
  self.api.nvim_buf_set_option(buf, "filetype", "code_savant_thought")

  -- 4. Fill contents
  local lines = vim.split(cached.full_content, "\n", { plain = true })
  self.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  self.api.nvim_buf_set_option(buf, "modifiable", false)

  -- 5. Open in vertical split
  vim.cmd("vsplit")
  local win = self.api.nvim_get_current_win()
  self.api.nvim_win_set_buf(win, buf)

  return buf, win
end

--- Open cached block contents in a beautiful, centered floating window.
--- @param opts table Arguments table containing 'id'
function UI:_open_in_float_impl(opts)
  -- 1. Validate parameters
  if not opts or type(opts) ~= "table" or not opts.id or type(opts.id) ~= "string" or opts.id == "" then
    error("[CodeSavantUI] Invalid parameter: 'id' is required and must be a non-empty string.")
  end
  local id = opts.id

  -- 2. Lookup cached block
  local cached = self.collapsed_blocks_cache[id]
  if not cached then
    error("[CodeSavantUI] Cache miss: No collapsed block found with ID: " .. tostring(id))
  end

  -- 3. Create a clean buffer
  local buf = self.api.nvim_create_buf(false, true)
  self.api.nvim_buf_set_option(buf, "buftype", "nofile")
  self.api.nvim_buf_set_option(buf, "swapfile", false)
  self.api.nvim_buf_set_option(buf, "bufhidden", "wipe")
  self.api.nvim_buf_set_option(buf, "filetype", "code_savant_thought")

  -- 4. Fill contents
  local lines = vim.split(cached.full_content, "\n", { plain = true })
  self.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  self.api.nvim_buf_set_option(buf, "modifiable", false)

  -- 5. Calculate dimensions (width, height, column, row)
  local screen_width = self.api.nvim_get_option("columns")
  local screen_height = self.api.nvim_get_option("lines")

  -- Use 80% width and 60% height or adjust to fit content
  local width = math.min(80, math.floor(screen_width * 0.8))
  local height = math.min(#lines, math.floor(screen_height * 0.6))
  if height < 3 then height = 3 end
  if width < 20 then width = 20 end

  local col = math.floor((screen_width - width) / 2)
  local row = math.floor((screen_height - height) / 2)

  -- 6. Open floating window
  local win_config = {
    relative = "editor",
    width = width,
    height = height,
    col = col,
    row = row,
    style = "minimal",
    border = "rounded",
  }
  if vim.fn.has("nvim-0.9") == 1 then
    win_config.title = " " .. cached.title .. " "
    win_config.title_pos = "center"
  end

  local win = self.api.nvim_open_win(buf, true, win_config)

  -- 7. Add buffer-local mapping to easily close the floating window
  vim.keymap.set("n", "q", function()
    pcall(self.api.nvim_win_close, win, true)
  end, { buffer = buf, silent = true, desc = "Close Thought Float" })
  vim.keymap.set("n", "<Esc>", function()
    pcall(self.api.nvim_win_close, win, true)
  end, { buffer = buf, silent = true, desc = "Close Thought Float" })

  return buf, win
end


--- Execute a callback function with temporary modifiable permission on a buffer, bypassing read-only limits.
--- @param bufnr_or_callback any Buffer number or update callback function
--- @param callback? fun() The programmatic callback if buffer is supplied
function UI:run_programmatic_update(bufnr_or_callback, callback)
  local bufnr, cb
  if type(bufnr_or_callback) == "function" then
    cb = bufnr_or_callback
    bufnr = self.api.nvim_get_current_buf()
  else
    bufnr = bufnr_or_callback
    cb = callback
  end

  if not bufnr or not self.api.nvim_buf_is_valid(bufnr) then
    error("[CodeSavantUI] run_programmatic_update requires a valid buffer")
  end
  if type(cb) ~= "function" then
    error("[CodeSavantUI] run_programmatic_update requires a function callback")
  end

  local original_modifiable = self.api.nvim_buf_get_option(bufnr, "modifiable")
  
  -- Temporarily enable edits
  self.api.nvim_buf_set_option(bufnr, "modifiable", true)
  self.is_programmatic_update = true

  local ok, err = pcall(cb)

  self.is_programmatic_update = false
  -- Lock it back down
  self.api.nvim_buf_set_option(bufnr, "modifiable", original_modifiable)

  if not ok then
    error(err)
  end
end

--- Teardown the UI manager, cleaning up all registered extmarks.
function UI:teardown()
  -- Clean up all cached extmarks
  for id, cached in pairs(self.collapsed_blocks_cache) do
    if self.api.nvim_buf_is_valid(cached.bufnr) then
      pcall(self.api.nvim_buf_del_extmark, cached.bufnr, self.namespace, cached.extmark_id)
    end
  end
  self.collapsed_blocks_cache = {}
end

--- Module level static function forwarders to support both singleton and class usage patterns
function UI.on_collapsed_block(self, id, block_type, title, full_content, bufnr, row)
  if type(self) ~= "table" or self.collapsed_blocks_cache == nil then
    local actual_id, actual_block_type, actual_title, actual_full_content, actual_bufnr, actual_row = self, id, block_type, title, full_content, bufnr
    local inst = UI.get_instance()
    return inst:_on_collapsed_block_impl(actual_id, actual_block_type, actual_title, actual_full_content, actual_bufnr, actual_row)
  else
    return self:_on_collapsed_block_impl(id, block_type, title, full_content, bufnr, row)
  end
end

function UI.expand_inplace(self, opts)
  if type(self) ~= "table" or self.collapsed_blocks_cache == nil then
    local actual_opts = self
    local inst = UI.get_instance()
    return inst:_expand_inplace_impl(actual_opts)
  else
    return self:_expand_inplace_impl(opts)
  end
end

function UI.collapse_inplace(self, opts)
  if type(self) ~= "table" or self.collapsed_blocks_cache == nil then
    local actual_opts = self
    local inst = UI.get_instance()
    return inst:_collapse_inplace_impl(actual_opts)
  else
    return self:_collapse_inplace_impl(opts)
  end
end

function UI.open_in_new_buf(self, opts)
  if type(self) ~= "table" or self.collapsed_blocks_cache == nil then
    local actual_opts = self
    local inst = UI.get_instance()
    return inst:_open_in_new_buf_impl(actual_opts)
  else
    return self:_open_in_new_buf_impl(opts)
  end
end

function UI.open_in_float(self, opts)
  if type(self) ~= "table" or self.collapsed_blocks_cache == nil then
    local actual_opts = self
    local inst = UI.get_instance()
    return inst:_open_in_float_impl(actual_opts)
  else
    return self:_open_in_float_impl(opts)
  end
end

return UI
