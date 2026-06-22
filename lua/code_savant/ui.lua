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
  SPINNER_STYLES = {
    braille   = { "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏" },
    clock     = { "◴", "◷", "◶", "◵" },
    circle    = { "◐", "◓", "◑", "◒" },
    equalizer = { " ", "▃", "▄", "▅", "▆", "▇", "█", "▇", "▆", "▅", "▄", "▃" },
    shade     = { "░", "▒", "▓", "█", "▓", "▒" },
    ellipsis  = { "   ", ".  ", ".. ", "..." },
    retro     = { "|", "/", "-", "\\" },
  },
}

UI.MESSAGE_REGISTRY = {
  -- Standard Continuous Flow Types
  user_prompt = {
    collapsible = false,
    header = "User:",
    indent = "  ",
    trailer = { "", "◀ CodeSavant is thinking...", "" },
    spinner_to_start = "thinking",
  },
  user_steering = {
    collapsible = false,
    header = "User (Steering Directive):",
    indent = "  ",
    trailer = {},
  },
  agent_stream = {
    collapsible = false,
    spinner_to_stop = "thinking",
    is_streaming = true,
  },
  system_error = {
    collapsible = false,
    header = "Error:",
    indent = "  ",
    hl_group = "DiagnosticError",
    spinner_to_stop = "thinking",
    clear_thinking_line = true,
  },

  -- Pure State Transition Types (Pure state changes, write no lines to the buffer)
  status_idle = {
    collapsible = false,
    spinner_to_stop = "thinking",
    clear_thinking_line = true,
    no_write = true,
  },
  cancel = {
    collapsible = false,
    spinner_to_stop = "thinking",
    clear_thinking_line = true,
    no_write = true,
  },

  -- Collapsible Block Types (Routed to collapsible handler)
  thought = {
    collapsible = true,
    block_type = "thought",
    prefix_collapsed = "▶ Thought: ",
    prefix_expanded = "▼ Thought: ",
    hl_group = "Comment",
  },
  tool = {
    collapsible = true,
    block_type = "tool",
    prefix_collapsed = "▶ Tool: ",
    prefix_expanded = "▼ Tool: ",
    hl_group = "Special",
  },
  confirmation = {
    collapsible = true,
    block_type = "confirmation",
    prefix_collapsed = "▶ Approve/Decline: ",
    prefix_expanded = "▼ Approve/Decline: ",
    hl_group = "DiagnosticWarn",
  },
  warning = {
    collapsible = true,
    block_type = "warning",
    prefix_collapsed = "▶ Warning: ",
    prefix_expanded = "▼ Warning: ",
    hl_group = "DiagnosticWarn",
  },
  steering = {
    collapsible = true,
    block_type = "steering",
    prefix_collapsed = "▶ Steering: ",
    prefix_expanded = "▼ Steering: ",
    hl_group = "Identifier",
  },
  error = {
    collapsible = true,
    block_type = "error",
    prefix_collapsed = "▶ Error: ",
    prefix_expanded = "▼ Error: ",
    hl_group = "DiagnosticError",
  }
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

  self.animation_wheel = {
    timer = nil,
    tick_rate = 100, -- Milliseconds default
    registry = {},   -- Map of "bufnr:key" -> SpinnerInstance
  }

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

  -- 3. Cache block content and metadata (idempotent, preserving previous state)
  local existing = self.collapsed_blocks_cache[id]
  local previous_extmark_id = existing and existing.extmark_id
  local previous_status = existing and existing.status or "collapsed"
  local previous_height = existing and existing.height

  self.collapsed_blocks_cache[id] = {
    full_content = full_content,
    type = block_type,
    title = title,
    bufnr = bufnr,
    status = previous_status,
    height = previous_height,
    extmark_id = previous_extmark_id,
    row = row or (existing and existing.row)
  }

  -- 4. If the block is currently expanded, perform an inline in-place update of expanded lines
  if previous_status == "expanded" then
    local target_row = row or (existing and existing.row)
    if previous_extmark_id then
      local pos = self.api.nvim_buf_get_extmark_by_id(bufnr, self.namespace, previous_extmark_id, {})
      if pos and #pos > 0 then
        target_row = pos[1]
      end
    end

    if target_row then
      local lines = vim.split(full_content, "\n", { plain = true })
      local display_lines = {}
      if self.CONSTANTS.BORDER_TOP and self.CONSTANTS.BORDER_TOP ~= "" then
        table.insert(display_lines, self.CONSTANTS.BORDER_TOP)
      end
      for _, line in ipairs(lines) do
        table.insert(display_lines, line)
      end
      if self.CONSTANTS.BORDER_BOTTOM and self.CONSTANTS.BORDER_BOTTOM ~= "" then
        table.insert(display_lines, self.CONSTANTS.BORDER_BOTTOM)
      end
      local height = #display_lines
      local prev_height = previous_height or 1

      self:run_programmatic_update(bufnr, function()
        -- Atomically replace previous expanded buffer lines
        self.api.nvim_buf_set_lines(bufnr, target_row, target_row + prev_height, false, display_lines)

        -- Clean up old expanded extmark
        if previous_extmark_id then
          pcall(self.api.nvim_buf_del_extmark, bufnr, self.namespace, previous_extmark_id)
        end

        -- Re-anchor the expanded indicator above the updated lines
        local block_config = self.MESSAGE_REGISTRY[block_type] or self.MESSAGE_REGISTRY["thought"]
        local indicator = block_config.prefix_expanded .. title
        local hl_group = block_config.hl_group
        local extmark_id = self.api.nvim_buf_set_extmark(bufnr, self.namespace, target_row, 0, {
          virt_lines = { { { indicator, hl_group } } },
          virt_lines_above = true,
        })

        -- Update the cache entry with new extmark ID and metrics
        self.collapsed_blocks_cache[id].extmark_id = extmark_id
        self.collapsed_blocks_cache[id].height = height
        self.collapsed_blocks_cache[id].row = target_row
      end)

      return previous_extmark_id
    end
  end

  -- 5. If collapsed, clear the old extmark before setting up the new one
  if previous_extmark_id then
    pcall(self.api.nvim_buf_del_extmark, bufnr, self.namespace, previous_extmark_id)
  end

  -- 6. Resolve row coordinate and ensure it exists
  local extmark_id = nil
  self:run_programmatic_update(bufnr, function()
    local target_row = row or (existing and existing.row)
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

    -- 7. Format collapsed line marker dynamically based on block_type
    local block_config = self.MESSAGE_REGISTRY[block_type] or self.MESSAGE_REGISTRY["thought"]
    local display_text = block_config.prefix_collapsed .. title
    local hl_group = block_config.hl_group

    -- 8. Anchor extmark with virtual text using overlay position
    extmark_id = self.api.nvim_buf_set_extmark(bufnr, self.namespace, target_row, 0, {
      virt_text = { { display_text, hl_group } },
      virt_text_pos = "overlay",
    })

    -- 9. Cache extmark association and row index
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
  -- Stop the animation timer wheel
  self:stop_wheel()

  -- Clean up all cached extmarks
  for id, cached in pairs(self.collapsed_blocks_cache) do
    if self.api.nvim_buf_is_valid(cached.bufnr) then
      pcall(self.api.nvim_buf_del_extmark, cached.bufnr, self.namespace, cached.extmark_id)
    end
  end
  self.collapsed_blocks_cache = {}
end

--- Starts a generic spinner instance
--- @param bufnr integer
--- @param key string Unique identifier (e.g., "thinking", "tool_exec_42", "bootstrap")
--- @param opts table Configuration table
function UI:start_spinner(bufnr, key, opts)
  if not bufnr or not key or not opts then
    error("[CodeSavantUI] start_spinner requires bufnr, key, and opts")
  end

  local registry_key = bufnr .. ":" .. key
  if self.animation_wheel.registry[registry_key] then return end

  -- Parse options
  local preset = opts.type or "braille"
  local frames = (preset == "custom" and opts.custom_frames) 
                 or UI.CONSTANTS.SPINNER_STYLES[preset] 
                 or UI.CONSTANTS.SPINNER_STYLES.braille

  if not frames or #frames == 0 then
    error("[CodeSavantUI] Invalid spinner style: " .. tostring(preset))
  end

  -- Create a tracker extmark to follow the line dynamically in O(1) time
  local initial_row = type(opts.row) == "function" and opts.row() or opts.row
  if not initial_row then
    error("[CodeSavantUI] Spinner requires a valid row or dynamic row function")
  end
  local col = opts.col or 0
  local tracker_extmark_id = self.api.nvim_buf_set_extmark(bufnr, self.namespace, initial_row, col, {})

  -- Register instance
  self.animation_wheel.registry[registry_key] = {
    bufnr = bufnr,
    key = key,
    frames = frames,
    frame_idx = 1,
    use_extmark = not not opts.use_extmark,
    col = col,
    extmark_id = tracker_extmark_id,
    format_fn = opts.format_fn,
  }

  -- Ensure timer tick rate matches interval if configured
  if opts.interval then
    self.animation_wheel.tick_rate = opts.interval
  end

  -- Wake up the global timer wheel
  self:ensure_wheel_running()
end

--- Stops a generic spinner instance and cleans up its resources
--- @param bufnr integer
--- @param key string
function UI:stop_spinner(bufnr, key)
  if not bufnr or not key then return end
  local registry_key = bufnr .. ":" .. key
  local inst = self.animation_wheel.registry[registry_key]
  
  if inst then
    -- Clean up tracker/virtual extmark
    if inst.extmark_id then
      pcall(self.api.nvim_buf_del_extmark, inst.bufnr, self.namespace, inst.extmark_id)
    end
    self.animation_wheel.registry[registry_key] = nil
  end
end

--- Ensures the global timer wheel is running
function UI:ensure_wheel_running()
  local wheel = self.animation_wheel
  if wheel.timer then return end

  local uv = vim.uv or vim.loop
  wheel.timer = uv.new_timer()

  wheel.timer:start(0, wheel.tick_rate, vim.schedule_wrap(function()
    local active_count = 0

    -- Iterate through the registry and apply updates
    for reg_key, inst in pairs(wheel.registry) do
      active_count = active_count + 1

      if vim.api.nvim_buf_is_valid(inst.bufnr) then
        -- 1. Resolve row in O(1) using Neovim's built-in extmark tracking
        local pos = self.api.nvim_buf_get_extmark_by_id(inst.bufnr, self.namespace, inst.extmark_id, {})
        
        if pos and #pos > 0 then
          local row = pos[1]
          local is_valid_line = true
          if not inst.use_extmark then
            local lines = self.api.nvim_buf_get_lines(inst.bufnr, row, row + 1, false)
            local line = lines[1] or ""
            if not line:find("CodeSavant is thinking...", 1, true) then
              is_valid_line = false
            end
          end

          if is_valid_line then
            local symbol = inst.frames[inst.frame_idx]
            inst.frame_idx = (inst.frame_idx % #inst.frames) + 1

            -- 2. Render efficiently
            self:run_programmatic_update(inst.bufnr, function()
              local representation = inst.format_fn(symbol)

              if inst.use_extmark then
                local virt_text = type(representation) == "table" and representation or { { representation, "Comment" } }
                self.api.nvim_buf_set_extmark(inst.bufnr, self.namespace, row, inst.col or 0, {
                  id = inst.extmark_id,
                  virt_text = virt_text,
                  virt_text_pos = "overlay",
                })
              else
                local text = type(representation) == "string" and representation or symbol
                self.api.nvim_buf_set_lines(inst.bufnr, row, row + 1, false, { text })
                -- Re-anchor/reset the tracker extmark on the updated row so it doesn't shift or get lost
                self.api.nvim_buf_set_extmark(inst.bufnr, self.namespace, row, inst.col or 0, {
                  id = inst.extmark_id,
                })
              end
            end)
          else
            -- Row content has changed or was deleted, auto-clean
            self:stop_spinner(inst.bufnr, inst.key)
          end
        else
          -- Row tracking was lost (line deleted), auto-clean
          self:stop_spinner(inst.bufnr, inst.key)
        end
      else
        -- Buffer closed, auto-clean
        self:stop_spinner(inst.bufnr, inst.key)
      end
    end

    -- If no spinners are active, sleep the timer to conserve 100% CPU resources
    if active_count == 0 then
      self:stop_wheel()
    end
  end))
end

--- Stops and cleans up the global timer wheel
function UI:stop_wheel()
  local wheel = self.animation_wheel
  if wheel.timer then
    wheel.timer:stop()
    wheel.timer:close()
    wheel.timer = nil
  end
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

-- Local helper to get current 0-indexed row of thinking extmark
local function get_thinking_row(self, bufnr)
  if not self.thinking_extmark_id then return nil end
  local ok, pos = pcall(self.api.nvim_buf_get_extmark_by_id, bufnr, self.namespace, self.thinking_extmark_id, {})
  if ok and pos and #pos > 0 then
    return pos[1]
  end
  return nil
end

--- Singly-Unified Entrypoint for all Chat Buffer Rendering
function UI:_render_message_impl(bufnr, msg_type, data)
  local config = self.MESSAGE_REGISTRY[msg_type]
  if not config then
    error("[CodeSavantUI] Invalid message type registered: " .. tostring(msg_type))
  end

  if config.collapsible then
    -- A. Collapsible Routing Path
    local payload = {
      id = data.id,
      title = data.title,
      content = data.content,
    }
    
    local cached = self.collapsed_blocks_cache[payload.id]
    if not cached then
      local thinking_row = get_thinking_row(self, bufnr)
      if thinking_row then
        self:run_programmatic_update(bufnr, function()
          self.api.nvim_buf_set_lines(bufnr, thinking_row, thinking_row, false, { "" })
        end)
        payload.row = thinking_row
      else
        local line_count = self.api.nvim_buf_line_count(bufnr)
        payload.row = math.max(0, line_count - 1)
        self:run_programmatic_update(bufnr, function()
          self.api.nvim_buf_set_lines(bufnr, payload.row, payload.row, false, { "" })
        end)
      end
    else
      payload.row = cached.row
    end

    return self:_render_collapsible_handler(bufnr, config.block_type, payload)
  else
    -- B. Standard Inline Routing Path
    return self:_render_standard_handler(bufnr, msg_type, data)
  end
end

--- Standard Continuous Text Handler
function UI:_render_standard_handler(bufnr, msg_type, data)
  local config = self.MESSAGE_REGISTRY[msg_type]

  -- 1. Automatically stop spinner if configured
  if config.spinner_to_stop then
    self:stop_spinner(bufnr, config.spinner_to_stop)
  end

  -- 2. Automatically clean up thinking placeholder row and extmark
  if config.clear_thinking_line then
    local thinking_row = get_thinking_row(self, bufnr)
    if thinking_row then
      self:run_programmatic_update(bufnr, function()
        self.api.nvim_buf_set_lines(bufnr, thinking_row, thinking_row + 1, false, {})
      end)
    end
    if self.thinking_extmark_id then
      pcall(self.api.nvim_buf_del_extmark, bufnr, self.namespace, self.thinking_extmark_id)
      self.thinking_extmark_id = nil
    end
  end

  -- 3. If pure state transition, we exit early
  if config.no_write then
    return
  end

  -- 4. Compile text block content
  local lines = type(data) == "string" and vim.split(data, "\n", { plain = true }) or data
  local text_to_write = {}
  if config.header then
    table.insert(text_to_write, config.header)
  end
  local indent = config.indent or ""
  for _, line in ipairs(lines) do
    table.insert(text_to_write, indent .. line)
  end
  if config.trailer then
    for _, t_line in ipairs(config.trailer) do
      table.insert(text_to_write, t_line)
    end
  end

  -- 5. Write text block atomically to buffer
  self:run_programmatic_update(bufnr, function()
    local line_count = self.api.nvim_buf_line_count(bufnr)

    if config.is_streaming then
      local thinking_row = get_thinking_row(self, bufnr)
      if thinking_row then
        self.api.nvim_buf_set_lines(bufnr, thinking_row, thinking_row + 1, false, text_to_write)
      else
        local target_row = math.max(0, line_count - 1)
        self.api.nvim_buf_set_lines(bufnr, target_row, target_row, false, text_to_write)
      end
    else
      local final_lines = {}
      if line_count > 1 or (line_count == 1 and self.api.nvim_buf_get_lines(bufnr, 0, 1, false)[1] ~= "") then
        table.insert(final_lines, "")
      end
      for _, l in ipairs(text_to_write) do
        table.insert(final_lines, l)
      end

      local insert_start = (line_count == 1 and self.api.nvim_buf_get_lines(bufnr, 0, 1, false)[1] == "") and 0 or line_count
      self.api.nvim_buf_set_lines(bufnr, insert_start, -1, false, final_lines)

      -- 6. Anchor thinking tracking extmark on placeholder row
      if config.trailer then
        local new_line_count = self.api.nvim_buf_line_count(bufnr)
        local trailer_row = new_line_count - 2
        self.thinking_extmark_id = self.api.nvim_buf_set_extmark(bufnr, self.namespace, trailer_row, 0, {})
      end
    end

    -- Auto scroll window view
    local winids = vim.fn.win_findbuf(bufnr)
    if winids and #winids > 0 then
      pcall(self.api.nvim_win_set_cursor, winids[1], { self.api.nvim_buf_line_count(bufnr), 0 })
    end
  end)

  -- 7. Automatically start spinner if configured
  if config.spinner_to_start then
    local spinner_opt = require("code_savant").config.spinner or {}
    self:start_spinner(bufnr, config.spinner_to_start, {
      type = spinner_opt.type,
      custom_frames = spinner_opt.custom_frames,
      interval = spinner_opt.interval,
      use_extmark = true,
      col = 4,
      row = function() return get_thinking_row(self, bufnr) end,
      format_fn = function(symbol) return { { symbol, "Special" } } end,
    })
  end
end

--- Collapsible Block Delegation Handler
function UI:_render_collapsible_handler(bufnr, block_type, payload)
  return self:_on_collapsed_block_impl(payload.id, block_type, payload.title, payload.content, bufnr, payload.row)
end

--- Module level static function forwarders to support both singleton and class usage patterns
function UI.render_message(self, bufnr, msg_type, data)
  if type(self) ~= "table" or self.collapsed_blocks_cache == nil then
    local actual_bufnr, actual_msg_type, actual_data = self, bufnr, msg_type
    return UI.get_instance():_render_message_impl(actual_bufnr, actual_msg_type, actual_data)
  else
    return self:_render_message_impl(bufnr, msg_type, data)
  end
end

return UI
