--- @class CodeSavantUI
--- @field api table Neovim API client (usually vim.api or injected)
--- @field namespace integer Namespace ID for extmarks
--- @field collapsed_blocks_cache table<string, table> Cached collapsed block content and metadata
local UI = {}
UI.__index = UI

local function log_debug(fmt, ...)
  local file = io.open("/tmp/code_savant_client.log", "a")
  if file then
    file:write(string.format("[%s] " .. fmt .. "\n", os.date("%H:%M:%S"), ...))
    file:close()
  end
end

--- Centralized Configuration Constants
UI.CONSTANTS = {
  NAMESPACE_NAME = "code_savant_ui",
  BORDER_TOP = "╭────────────────────────Thought Space────────────────────────╮",
  BORDER_BOTTOM = "╰─────────────────────────────────────────────────────────────╯",
  
  -- Centralized Virtual Spinner Configuration
  SPINNER_PREFIX = "◀   ",
  SPINNER_SUFFIX = "   CodeSavant is thinking...",
  SPINNER_HIGHLIGHT = "Special",
  SPINNER_COLUMN_OFFSET = 0,

  SPINNER_STYLES = {
    braille   = { "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏" },
    clock     = { "◴", "◷", "◶", "◵" },
    circle    = { "◐", "◓", "◑", "◒" },
    equalizer = { " ", "▃", "▄", "▅", "▆", "▇", "█", "▇", "▆", "▅", "▄", "▃" },
    shade     = { "░", "▒", "▓", "█", "▓", "▒" },
    pulse     = { "·", "•", "●", "•" },
    scan      = { "⎺", "⎻", "⎼", "⎽", "⎼", "⎻" },
    compass   = { "▲", "►", "▼", "◄" },
    star      = { "✶", "✸", "✹", "✺", "✹", "✸" },
    retro     = { "|", "/", "-", "\\" },
  },
}

UI.MESSAGE_REGISTRY = {
  -- Standard Continuous Flow Types
  user = {
    collapsible = false,
    header = "User:",
    header_hl = "Identifier",
    indent = "  ",
    role = "user",
  },
  model = {
    collapsible = false,
    header = "CodeSavant:",
    header_hl = "Special",
    indent = "",
    role = "model",
  },
  system_error = {
    collapsible = false,
    header = "Error:",
    header_hl = "DiagnosticError",
    indent = "  ",
    hl_group = "DiagnosticError",
    role = "system",
  },

  -- Pure State Transition Types (Pure state changes, write no lines to the buffer)
  thinking = {
    collapsible = false,
    spinner_to_start = "thinking",
    no_write = true,
    status_value = "thinking",
  },
  idle = {
    collapsible = false,
    spinner_to_stop = "thinking",
    clear_thinking_line = true,
    no_write = true,
    status_value = "idle",
  },
  cancel = {
    collapsible = false,
    spinner_to_stop = "thinking",
    clear_thinking_line = true,
    no_write = true,
    status_value = "cancel",
  },

  -- Collapsible Block Types (Routed to collapsible handler)
  thought = {
    collapsible = true,
    prefix_collapsed = "▶ Thought: ",
    prefix_expanded = "▼ Thought: ",
    hl_group = "Comment",
    border_top = "╭────────────────────────Thought Space────────────────────────╮",
    border_bottom = "╰─────────────────────────────────────────────────────────────╯",
  },
  function_call = {
    collapsible = true,
    prefix_collapsed = "▶ Tool Call: ",
    prefix_expanded = "▼ Tool Call: ",
    hl_group = "Special",
    border_top = "╭─────────────────────────Tool Input─────────────────────────╮",
    border_bottom = "╰─────────────────────────────────────────────────────────────╯",
  },
  function_response = {
    collapsible = true,
    prefix_collapsed = "▶ Tool Response: ",
    prefix_expanded = "▼ Tool Response: ",
    hl_group = "Special",
    border_top = "╭────────────────────────Tool Response────────────────────────╮",
    border_bottom = "╰─────────────────────────────────────────────────────────────╯",
  },
  confirmation = {
    collapsible = true,
    prefix_collapsed = "▶ Approve/Decline: ",
    prefix_expanded = "▼ Approve/Decline: ",
    hl_group = "DiagnosticWarn",
    border_top = "╭─────────────────────Approval Requested──────────────────────╮",
    border_bottom = "╰─────────────────────────────────────────────────────────────╯",
  },
  warning = {
    collapsible = true,
    prefix_collapsed = "▶ Warning: ",
    prefix_expanded = "▼ Warning: ",
    hl_group = "DiagnosticWarn",
    border_top = "╭───────────────────────────Warning───────────────────────────╮",
    border_bottom = "╰─────────────────────────────────────────────────────────────╯",
  },
  steering = {
    collapsible = true,
    prefix_collapsed = "▶ Steering: ",
    prefix_expanded = "▼ Steering: ",
    hl_group = "Identifier",
    border_top = "╭──────────────────────Steering Directive─────────────────────╮",
    border_bottom = "╰─────────────────────────────────────────────────────────────╯",
  },
  steering_queued = {
    collapsible = true,
    prefix_collapsed = "▶ Steering Queued: ",
    prefix_expanded = "▼ Steering Queued: ",
    hl_group = "Comment",
    border_top = "╭──────────────────Steering Directive Queued──────────────────╮",
    border_bottom = "╰─────────────────────────────────────────────────────────────╯",
  },
  error = {
    collapsible = true,
    prefix_collapsed = "▶ Error: ",
    prefix_expanded = "▼ Error: ",
    hl_group = "DiagnosticError",
    border_top = "╭────────────────────────────Error────────────────────────────╮",
    border_bottom = "╰─────────────────────────────────────────────────────────────╯",
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
  self.active_stream_height = nil
  self.active_spinners = {}
  self.sessions = {}
  self.extmark_metadata = {}
  self.block_to_extmark = {}
  self.pending_approvals = {
    queue = {},
  }
  self.spinner_cycle_index = 1

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

function UI:_get_session(bufnr)
  if not self.sessions then
    self.sessions = {}
  end
  if not self.sessions[bufnr] then
    self.sessions[bufnr] = {
      status = "idle",
      active_spinner_id = nil,
      active_spinner_extmark_id = nil,
      active_stream_id = nil,
      active_stream_start_row = nil,
      active_stream_height = nil,
      active_stream_extmark_id = nil,
    }
  end
  return self.sessions[bufnr]
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
      local block_config = self.MESSAGE_REGISTRY[block_type] or self.MESSAGE_REGISTRY["thought"]
      local border_top = block_config.border_top or self.CONSTANTS.BORDER_TOP
      local border_bottom = block_config.border_bottom or self.CONSTANTS.BORDER_BOTTOM

      local lines = vim.split(full_content, "\n", { plain = true })
      local display_lines = {}
      if border_top and border_top ~= "" then
        table.insert(display_lines, border_top)
      end
      for _, line in ipairs(lines) do
        table.insert(display_lines, line)
      end
      if border_bottom and border_bottom ~= "" then
        table.insert(display_lines, border_bottom)
      end
      local height = #display_lines
      local prev_height = previous_height or 1

      self:run_programmatic_update(bufnr, function()
        -- Atomically replace previous expanded buffer lines
        self.api.nvim_buf_set_lines(bufnr, target_row, target_row + prev_height, false, display_lines)

        -- Clean up old expanded extmark
        if previous_extmark_id then
          self.extmark_metadata[previous_extmark_id] = nil
          pcall(self.api.nvim_buf_del_extmark, bufnr, self.namespace, previous_extmark_id)
        end

        -- Re-anchor the expanded indicator above the updated lines
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

        -- Track metadata natively
        self.extmark_metadata[extmark_id] = {
          id = id,
          type = block_type,
          status = "expanded",
          height = height,
        }
        self.block_to_extmark[id] = extmark_id
      end)

      return previous_extmark_id
    end
  end

  -- 5. If collapsed, clear the old extmark before setting up the new one
  if previous_extmark_id then
    self.extmark_metadata[previous_extmark_id] = nil
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

    -- Track metadata natively
    self.extmark_metadata[extmark_id] = {
      id = id,
      type = block_type,
      status = "collapsed",
    }
    self.block_to_extmark[id] = extmark_id
  end)

  if block_type == "confirmation" then
    self:push_pending_approval(id)
  end

  -- Cycle the running spinner style for the next progress step!
  self:cycle_spinner_style(bufnr)

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
  local block_config = self.MESSAGE_REGISTRY[cached.type] or self.MESSAGE_REGISTRY["thought"]
  local border_top = block_config.border_top or UI.CONSTANTS.BORDER_TOP
  local border_bottom = block_config.border_bottom or UI.CONSTANTS.BORDER_BOTTOM

  local lines = vim.split(cached.full_content, "\n", { plain = true })
  local display_lines = {}
  if border_top and border_top ~= "" then
    table.insert(display_lines, border_top)
  end
  for _, line in ipairs(lines) do
    table.insert(display_lines, line)
  end
  if border_bottom and border_bottom ~= "" then
    table.insert(display_lines, border_bottom)
  end

  local height = #display_lines

  -- 5. Replace buffer lines atomically
  self:run_programmatic_update(bufnr, function()
    self.api.nvim_buf_set_lines(bufnr, row, row + 1, false, display_lines)
    
    -- 6. Clean up old collapsed extmark
    self.extmark_metadata[cached.extmark_id] = nil
    pcall(self.api.nvim_buf_del_extmark, bufnr, self.namespace, cached.extmark_id)

    -- 7. Anchor new expanded tracking extmark at the top of the block with indicator above it as a virtual line
    local block_config = self.MESSAGE_REGISTRY[cached.type]
    if not block_config then
      error(string.format("[CodeSavantUI] Unsupported or unregistered block type: '%s'", tostring(cached.type)))
    end

    local prefix = block_config.prefix_expanded
    local hl_group = block_config.hl_group
    if not prefix or not hl_group then
      error(string.format("[CodeSavantUI] Missing required registry properties for block type: '%s'", tostring(cached.type)))
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

    -- Track metadata natively
    self.extmark_metadata[extmark_id] = {
      id = id,
      type = cached.type,
      status = "expanded",
      height = height,
    }
    self.block_to_extmark[id] = extmark_id
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
    self.extmark_metadata[cached.extmark_id] = nil
    pcall(self.api.nvim_buf_del_extmark, bufnr, self.namespace, cached.extmark_id)

    -- 6. Anchor new collapsed tracking extmark with overlay indicator
    local block_config = self.MESSAGE_REGISTRY[cached.type]
    if not block_config then
      error(string.format("[CodeSavantUI] Unsupported or unregistered block type: '%s'", tostring(cached.type)))
    end

    local prefix = block_config.prefix_collapsed
    local hl_group = block_config.hl_group
    if not prefix or not hl_group then
      error(string.format("[CodeSavantUI] Missing required registry properties for block type: '%s'", tostring(cached.type)))
    end

    local display_text = prefix .. cached.title
    local extmark_id = self.api.nvim_buf_set_extmark(bufnr, self.namespace, row, 0, {
      virt_text = { { display_text, hl_group } },
      virt_text_pos = "overlay",
    })

    -- 7. Update cache entry status and dimensions
    self.collapsed_blocks_cache[id].extmark_id = extmark_id
    self.collapsed_blocks_cache[id].status = "collapsed"
    self.collapsed_blocks_cache[id].height = nil

    -- Track metadata natively
    self.extmark_metadata[extmark_id] = {
      id = id,
      type = cached.type,
      status = "collapsed",
    }
    self.block_to_extmark[id] = extmark_id
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
  if preset == "custom" and opts.custom_frames then
    UI.CONSTANTS.SPINNER_STYLES["custom"] = opts.custom_frames
  end

  -- Create a tracker extmark to follow the line dynamically in O(1) time
  local initial_row = type(opts.row) == "function" and opts.row() or opts.row
  if not initial_row then
    error("[CodeSavantUI] Spinner requires a valid row or dynamic row function")
  end
  local col = opts.col or 0
  local tracker_extmark_id = self.api.nvim_buf_set_extmark(bufnr, self.namespace, initial_row, col, {})

  -- Register instance with a dynamic metatable proxy!
  self.animation_wheel.registry[registry_key] = setmetatable({
    bufnr = bufnr,
    key = key,
    type = preset,
    frame_idx = 1,
    use_extmark = not not opts.use_extmark,
    col = col,
    extmark_id = tracker_extmark_id,
    format_fn = opts.format_fn,
  }, {
    __index = function(tbl, k)
      if k == "frames" then
        return UI.CONSTANTS.SPINNER_STYLES[tbl.type] or UI.CONSTANTS.SPINNER_STYLES.braille
      end
      return nil
    end
  })

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

  local session = self:_get_session(bufnr)
  if key == "thinking" then
    session.active_spinner_id = nil
    session.active_spinner_extmark_id = nil
  end

  -- Clear from active_spinners table
  for id, extmark_id in pairs(self.active_spinners) do
    local ok, pos = pcall(self.api.nvim_buf_get_extmark_by_id, bufnr, self.namespace, extmark_id, {})
    if not ok or not pos or #pos == 0 then
      self.active_spinners[id] = nil
    end
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
            local frames = UI.CONSTANTS.SPINNER_STYLES[inst.type] or UI.CONSTANTS.SPINNER_STYLES.braille
            local symbol = frames[inst.frame_idx] or " "
            inst.frame_idx = (inst.frame_idx % #frames) + 1

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

-- Local helper to get current 0-indexed row of thinking extmark for a given ID
local function get_thinking_row(self, bufnr, id)
  local session = self:_get_session(bufnr)
  if id and session.active_spinner_id ~= id then
    return nil
  end
  local extmark_id = session.active_spinner_extmark_id
  if not extmark_id then return nil end
  local ok, pos = pcall(self.api.nvim_buf_get_extmark_by_id, bufnr, self.namespace, extmark_id, {})
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
      local thinking_row = get_thinking_row(self, bufnr, vim.b[bufnr].active_model_message_id)
      if thinking_row then
        self:run_programmatic_update(bufnr, function()
          self.api.nvim_buf_set_lines(bufnr, thinking_row, thinking_row, false, { "" })
        end)
        payload.row = thinking_row
      else
        local line_count = self.api.nvim_buf_line_count(bufnr)
        payload.row = line_count
        self:run_programmatic_update(bufnr, function()
          self.api.nvim_buf_set_lines(bufnr, payload.row, payload.row, false, { "" })
        end)
      end
    else
      payload.row = cached.row
    end

    return self:_render_collapsible_handler(bufnr, msg_type, payload)
  else
    -- B. Standard Inline Routing Path (Pass table directly to preserve metadata IDs)
    return self:_render_standard_handler(bufnr, msg_type, data)
  end
end

-- Helper to build consistent, high-contrast virtual lines containing optional subdued separator line and header text
local function build_virt_lines(self, bufnr, header, header_hl, needs_separator)
  -- Gracefully fallback to "Identifier" if highlight group is undefined
  if not header_hl then
    header_hl = "Identifier"
  end

  local virt_lines_spec = {}
  if needs_separator then
    local win_width = 80
    local winids = vim.fn.win_findbuf(bufnr)
    if winids and #winids > 0 then
      win_width = vim.api.nvim_win_get_width(winids[1])
    end
    -- Low-contrast horizontal divider line using WinSeparator highlight group
    local separator_line = string.rep("─", math.max(40, win_width - 4))
    table.insert(virt_lines_spec, { { separator_line, "WinSeparator" } })
    table.insert(virt_lines_spec, { { "", "Normal" } })
  end
  table.insert(virt_lines_spec, { { header, header_hl } })
  return virt_lines_spec
end

--- Standard Continuous Text Handler
function UI:_render_standard_handler(bufnr, msg_type, data)
  local config = self.MESSAGE_REGISTRY[msg_type]
  local session = self:_get_session(bufnr)

  -- 1. Automatically stop spinner if configured
  if config.spinner_to_stop then
    local msg_id = data.id
    local thinking_row = get_thinking_row(self, bufnr, msg_id)
    log_debug("SPINNER STOP REQUEST: msg_id=%s, thinking_row=%s, active_model_msg_id=%s", 
      tostring(msg_id), tostring(thinking_row), tostring(vim.b[bufnr].active_model_message_id))
    if thinking_row then
      self:run_programmatic_update(bufnr, function()
        self.api.nvim_buf_set_lines(bufnr, thinking_row, thinking_row + 1, false, {})
      end)
    end
    self:stop_spinner(bufnr, config.spinner_to_stop)
    vim.b[bufnr].active_model_message_id = nil
  end

  -- 7. Automatically start spinner if configured
  if config.spinner_to_start then
    -- Clean up any pre-existing active spinner of this same key in this buffer first!
    self:stop_spinner(bufnr, config.spinner_to_start)

    local new_line_count = self.api.nvim_buf_line_count(bufnr)
    self:run_programmatic_update(bufnr, function()
      self.api.nvim_buf_set_lines(bufnr, new_line_count, new_line_count, false, { "" })
    end)
    local trailer_row = new_line_count
    
    local spinner_opt = require("code_savant").config.spinner or {}
    local styles = {}
    for name, _ in pairs(UI.CONSTANTS.SPINNER_STYLES) do
      table.insert(styles, name)
    end
    table.sort(styles)
    local selected_type = styles[self.spinner_cycle_index] or "equalizer"
    self.spinner_cycle_index = (self.spinner_cycle_index % #styles) + 1

    local msg_id = data.id
    vim.b[bufnr].active_model_message_id = msg_id
    session.active_spinner_id = msg_id
    log_debug("SPINNER START REQUEST: msg_id=%s, trailer_row=%d, active_model_msg_id=%s", 
      tostring(msg_id), trailer_row, tostring(vim.b[bufnr].active_model_message_id))

    self:start_spinner(bufnr, config.spinner_to_start, {
      type = selected_type,
      custom_frames = spinner_opt.custom_frames,
      interval = spinner_opt.interval,
      use_extmark = true,
      col = UI.CONSTANTS.SPINNER_COLUMN_OFFSET,
      row = trailer_row,
      format_fn = function(symbol)
        local display_text = UI.CONSTANTS.SPINNER_PREFIX .. symbol .. UI.CONSTANTS.SPINNER_SUFFIX
        return { { display_text, UI.CONSTANTS.SPINNER_HIGHLIGHT } }
      end,
    })

    -- Store the newly created extmark in active_spinners mapping for clean cleanup!
    local registry_key = bufnr .. ":" .. config.spinner_to_start
    local inst = self.animation_wheel.registry[registry_key]
    if inst then
      session.active_spinner_extmark_id = inst.extmark_id
      self.active_spinners[msg_id] = inst.extmark_id
    end
  end

  -- 3. If pure state transition, we exit early
  if config.no_write then
    if msg_type == "idle" or msg_type == "cancel" then
      log_debug("STATE TRANSITION RESET: msg_type=%s", msg_type)
      session.active_stream_id = nil
      session.active_stream_extmark_id = nil
      session.active_stream_height = nil
      session.active_stream_start_row = nil
      session.active_stream_accumulator = nil
      -- Also clear global ones for safety
      self.active_stream_extmark_id = nil
      self.active_stream_height = nil
      self.active_stream_start_row = nil
    end
    return
  end

  -- 4. Compile text block content
  local text_to_write = {}
  local is_streaming = (type(data) == "table" and data.is_streaming == true)
  local chunk_text = (type(data) == "table" and data.content) and data.content or data

  if is_streaming then
    -- Accumulate raw delta content chunks in real-time
    if type(chunk_text) == "string" then
      if not session.active_stream_accumulator then
        session.active_stream_accumulator = {}
      end
      table.insert(session.active_stream_accumulator, chunk_text)
    end

    local full_accumulated = table.concat(session.active_stream_accumulator or {}, "")
    local lines = vim.split(full_accumulated, "\n", { plain = true })
    local indent = config.indent or ""
    for _, line in ipairs(lines) do
      table.insert(text_to_write, indent .. line)
    end
    if config.trailer then
      for _, t_line in ipairs(config.trailer) do
        table.insert(text_to_write, t_line)
      end
    end
  else
    local raw_content = chunk_text
    local lines = type(raw_content) == "string" and vim.split(raw_content, "\n", { plain = true }) or raw_content
    local indent = config.indent or ""
    for _, line in ipairs(lines) do
      table.insert(text_to_write, indent .. line)
    end
    if config.trailer then
      for _, t_line in ipairs(config.trailer) do
        table.insert(text_to_write, t_line)
      end
    end
  end

  -- 5. Write text block atomically to buffer
  self:run_programmatic_update(bufnr, function()
    local line_count = self.api.nvim_buf_line_count(bufnr)

    if is_streaming then
      local msg_id = type(data) == "table" and data.id or nil
      local thinking_row = get_thinking_row(self, bufnr, msg_id)
      self:stop_spinner(bufnr, "thinking")
      
      -- Anchor the stream starting row segment exactly once on the first chunk
      if not session.active_stream_start_row then
        if thinking_row then
          session.active_stream_start_row = thinking_row + 1
        else
          session.active_stream_start_row = line_count
        end
        session.active_stream_accumulator = { chunk_text } -- Reset accumulator with the first token
        log_debug("STREAM START ANCHOR SET: active_stream_start_row=%d, msg_id=%s", session.active_stream_start_row, tostring(msg_id))
      end
      
      local target_row = session.active_stream_start_row
      local replace_height = session.active_stream_height or 0
      
      log_debug("STREAM WRITE ATOM: msg_id=%s, thinking_row=%s, active_stream_start_row=%s, target_row=%d, replace_height=%d, active_stream_height=%s, lines=%d",
        tostring(msg_id), tostring(thinking_row), tostring(session.active_stream_start_row), target_row, replace_height, tostring(session.active_stream_height), #text_to_write)
      
      self.api.nvim_buf_set_lines(bufnr, target_row, target_row + replace_height, false, text_to_write)
      session.active_stream_height = #text_to_write

      -- Pin the stream virtual header atomically to target_row on every chunk write
      if config.header then
        local stream_needs_separator = (target_row > 1)
        local virt_lines_spec = build_virt_lines(self, bufnr, config.header, config.header_hl, stream_needs_separator)
        
        -- Dynamically bypass concealed markdown code fences by anchoring to the second line if present
        local header_row = target_row
        local has_code_fence = (text_to_write[1] and text_to_write[1]:sub(1, 3) == "```")
        if has_code_fence and #text_to_write > 1 then
          header_row = target_row + 1
        end

        local extmark_id = self.api.nvim_buf_set_extmark(bufnr, self.namespace, header_row, 0, {
          id = session.active_stream_extmark_id, -- 🌟 Atomically moves/pins the existing extmark!
          virt_lines = virt_lines_spec,
          virt_lines_above = true,
        })
        session.active_stream_extmark_id = extmark_id
        session.active_stream_id = msg_id

        -- Register metadata natively
        self.extmark_metadata[extmark_id] = {
          id = "active_stream",
          type = "message",
          role = "model",
          status = "inline",
        }
      end
    else
      local is_empty_buffer = (line_count == 1 and self.api.nvim_buf_get_lines(bufnr, 0, 1, false)[1] == "")
      local needs_separator = not is_empty_buffer

      local final_lines = {}
      if needs_separator then
        table.insert(final_lines, "")
      end
      for _, l in ipairs(text_to_write) do
        table.insert(final_lines, l)
      end

      -- If we have an active thinking spinner, write above it, otherwise append at the end
      local msg_id = type(data) == "table" and data.id or nil
      local thinking_row = get_thinking_row(self, bufnr, msg_id or vim.b[bufnr].active_model_message_id)

      local insert_start
      local header_row

      if thinking_row then
        insert_start = thinking_row
        header_row = thinking_row
      else
        insert_start = is_empty_buffer and 1 or line_count
        header_row = insert_start
      end

      log_debug("STATIC WRITE ATOM: msg_type=%s, is_empty_buffer=%s, thinking_row=%s, insert_start=%d, header_row=%d, lines=%d",
        tostring(msg_type), tostring(is_empty_buffer), tostring(thinking_row), insert_start, header_row, #final_lines)

      if thinking_row then
        -- This pushes the spinner down cleanly, maintaining the timeline!
        self.api.nvim_buf_set_lines(bufnr, insert_start, insert_start, false, final_lines)
      else
        self.api.nvim_buf_set_lines(bufnr, insert_start, -1, false, final_lines)
      end

      if config.header then
        local virt_lines_spec = build_virt_lines(self, bufnr, config.header, config.header_hl, needs_separator)

        -- Dynamically bypass concealed markdown code fences by anchoring to the second line of content if present
        local first_content_row = header_row
        if needs_separator then
          first_content_row = header_row + 1
        end

        local actual_header_row = header_row
        local has_code_fence = (text_to_write[1] and text_to_write[1]:sub(1, 3) == "```")
        if has_code_fence and #text_to_write > 1 then
          actual_header_row = first_content_row + 1
        end

        local extmark_id = self.api.nvim_buf_set_extmark(bufnr, self.namespace, actual_header_row, 0, {
          virt_lines = virt_lines_spec,
          virt_lines_above = true,
        })

        -- Track metadata natively (Fail loudly if configuration is incomplete)
        if not config.role then
          error(string.format("[CodeSavantUI] Missing required 'role' for message type '%s'", msg_type))
        end

        self.extmark_metadata[extmark_id] = {
          id = "msg_" .. tostring(extmark_id),
          type = "message",
          role = config.role,
          status = "inline",
        }
      end
    end

    -- Auto scroll window view
    local winids = vim.fn.win_findbuf(bufnr)
    if winids and #winids > 0 then
      pcall(self.api.nvim_win_set_cursor, winids[1], { self.api.nvim_buf_line_count(bufnr), 0 })
    end
  end)
end

--- Collapsible Block Delegation Handler
function UI:_render_collapsible_handler(bufnr, block_type, payload)
  return self:_on_collapsed_block_impl(payload.id, block_type, payload.title, payload.content, bufnr, payload.row)
end

function UI:push_pending_approval(id)
  for _, queued_id in ipairs(self.pending_approvals.queue) do
    if queued_id == id then return end
  end
  table.insert(self.pending_approvals.queue, id)
  
  local cached = self.collapsed_blocks_cache[id]
  if cached and cached.bufnr then
    self:update_input_overlay(cached.bufnr)
  end
end

function UI:remove_pending_approval(id)
  local found_idx = nil
  for idx, queued_id in ipairs(self.pending_approvals.queue) do
    if queued_id == id then
      found_idx = exit_code or idx
    end
  end
  if found_cycle or found_submit_normal or found_submit_insert or found_cycle then
    -- No-op, just fallback variable protection
  end
  if found_cycle == nil then found_cycle = false end
  if found_submit_normal == nil then found_submit_normal = false end
  if found_submit_insert == nil then found_submit_insert = false end
  if found_idx then
    table.remove(self.pending_approvals.queue, found_idx)
  end

  local cached = self.collapsed_blocks_cache[id]
  if cached then
    cached.resolved = true
    if cached.bufnr then
      self:update_input_overlay(cached.bufnr)
    end
  end
end

function UI:execute_resolution(id, confirmed)
  local cached = self.collapsed_blocks_cache[id]
  if not cached then return end
  local bufnr = cached.bufnr

  self:remove_pending_approval(id)

  local Network = require("code_savant.network")
  local conn = Network.get_connection(bufnr)
  if conn then
    pcall(Network.send_request, conn, "session/respond_confirmation", {
      session_id = conn.session_id,
      id = id,
      confirmed = confirmed
    })
  end

  local result_text = confirmed and " ✓ APPROVED" or " ✗ DECLINED"
  local hl = confirmed and "DiagnosticOk" or "DiagnosticError"
  local display_text = "▶ Approve/Decline: " .. cached.title .. result_text
  pcall(self.api.nvim_buf_set_extmark, bufnr, self.namespace, cached.row, 0, {
    id = cached.extmark_id,
    virt_text = { { display_text, hl } },
    virt_text_pos = "overlay",
  })
end

function UI:_resolve_fifo_confirmation_impl(confirmed)
  local oldest_id = self.pending_approvals.queue[1]
  if oldest_id then
    self:execute_resolution(oldest_id, confirmed)
    return true
  else
    vim.notify("[CodeSavant] No pending approvals in queue.", vim.log.levels.INFO)
    return false
  end
end

function UI:_resolve_cursor_confirmation_impl(confirmed)
  local bufnr = self.api.nvim_get_current_buf()
  local cursor_row = self.api.nvim_win_get_cursor(0)[1] - 1

  for id, block in pairs(self.collapsed_blocks_cache) do
    if block.bufnr == bufnr and block.type == "confirmation" and not block.resolved then
      local ok, pos = pcall(self.api.nvim_buf_get_extmark_by_id, bufnr, self.namespace, block.extmark_id, {})
      if ok and pos and #pos > 0 and pos[1] == cursor_row then
        self:execute_resolution(id, confirmed)
        return true
      end
    end
  end
  return false
end

function UI:update_input_overlay(history_bufnr)
  if not history_bufnr or not self.api.nvim_buf_is_valid(history_bufnr) then return end
  local input_bufnr = vim.b[history_bufnr].partner_buf
  if not input_bufnr or not self.api.nvim_buf_is_valid(input_bufnr) then return end

  if self.input_overlay_extmark_id then
    pcall(self.api.nvim_buf_del_extmark, input_bufnr, self.namespace, self.input_overlay_extmark_id)
    self.input_overlay_extmark_id = nil
  end

  local active_approvals = {}
  for _, id in ipairs(self.pending_approvals.queue) do
    local cached = self.collapsed_blocks_cache[id]
    if cached and cached.bufnr == history_bufnr and not cached.resolved then
      table.insert(active_approvals, { id = id, cached = cached })
    end
  end

  if #active_approvals == 0 then
    return
  end

  local oldest = active_approvals[1]
  local queue_text = string.format("⚠️  [Pending Approval 1/%d]: %s", #active_approvals, oldest.cached.title)
  local help_text = "👉 Press <leader>sa to Approve | <leader>sd to Decline | :CodeSavantApprovals to browse queue"

  self.input_overlay_extmark_id = self.api.nvim_buf_set_extmark(input_bufnr, self.namespace, 0, 0, {
    virt_lines = {
      { { queue_text, "DiagnosticWarn" } },
      { { help_text, "Comment" } },
      { { "", "Normal" } },
    },
    virt_lines_above = true,
  })
end

-- Module level forwarders for singleton/class patterns
function UI.resolve_fifo_confirmation(self, confirmed)
  if type(self) ~= "table" or self.collapsed_blocks_cache == nil then
    local actual_confirmed = self
    return UI.get_instance():_resolve_fifo_confirmation_impl(actual_confirmed)
  else
    return self:_resolve_fifo_confirmation_impl(confirmed)
  end
end

function UI.resolve_cursor_confirmation(self, confirmed)
  if type(self) ~= "table" or self.collapsed_blocks_cache == nil then
    local actual_confirmed = self
    return UI.get_instance():_resolve_cursor_confirmation_impl(actual_confirmed)
  else
    return self:_resolve_cursor_confirmation_impl(confirmed)
  end
end

function UI:cycle_spinner_style(bufnr)
  local instance = self.animation_wheel.registry[bufnr .. ":thinking"]
  if not instance then return end

  local styles = { "equalizer", "braille", "clock", "circle", "shade", "pulse", "scan", "compass", "star", "retro" }
  local next_style = styles[self.spinner_cycle_index] or "equalizer"
  self.spinner_cycle_index = (self.spinner_cycle_index % #styles) + 1

  -- Single-string assignment! Zero array operations needed
  instance.type = next_style
  instance.frame_idx = 1
end

--- Resolve the start and end row (0-indexed) for a text object of the given type and mode.
--- @param bufnr number The buffer number.
--- @param obj_type string "message", "thought", or "tool".
--- @param inner boolean Whether to return the inner range (true) or around range (false).
--- @param target_row number? Optional target row (0-indexed) to override active cursor row (highly useful for CI/CD deterministic tests)
--- @return number|nil, number? The start and end row, or nil if not found.
function UI:resolve_text_object_range(bufnr, obj_type, inner, target_row)
  local cursor_row = target_row or (self.api.nvim_win_get_cursor(0)[1] - 1)
  local extmarks = self.api.nvim_buf_get_extmarks(bufnr, self.namespace, 0, -1, {})
  local line_count = self.api.nvim_buf_line_count(bufnr)

  if #extmarks == 0 then return nil, nil end

  -- 1. Gather and sort all extmarks of interest
  local list = {}
  for _, mark in ipairs(extmarks) do
    local extmark_id = mark[1]
    local r = mark[2]
    local meta = self.extmark_metadata[extmark_id]
    if meta then
      local matched = false
      if obj_type == "message" then
        matched = (meta.type == "message")
      elseif obj_type == "thought" then
        matched = (meta.type == "thought")
      elseif obj_type == "tool" then
        matched = (meta.type == "tool" or meta.type == "confirmation" or meta.type == "function_call" or meta.type == "function_response")
      end

      if matched then
        table.insert(list, {
          id = extmark_id,
          row = r,
          meta = meta,
        })
      end
    end
  end

  if #list == 0 then return nil, nil end

  -- Sort by row index ascending
  table.sort(list, function(a, b) return a.row < b.row end)

  -- 2. Find the active block enclosing the cursor
  local active_idx = nil
  for i, item in ipairs(list) do
    local item_start = item.row
    local item_end = item_start
    if item.meta.status == "expanded" and item.meta.height then
      item_end = item_start + item.meta.height - 1
    end

    if cursor_row >= item_start then
      if obj_type == "message" then
        -- Messages are contiguous; the active message extends until the next message start
        local next_item = list[i + 1]
        local limit = next_item and next_item.row - 1 or (line_count - 1)
        if cursor_row <= limit then
          active_idx = i
        end
      else
        -- Nested blocks (thoughts/tools) are discrete and bounded
        if cursor_row <= item_end then
          active_idx = i
        end
      end
    end
  end

  if not active_idx then return nil, nil end

  local active = list[active_idx]
  local start_row = active.row
  local end_row = start_row

  if obj_type == "message" then
    local next_item = list[active_idx + 1]
    end_row = next_item and next_item.row - 1 or (line_count - 1)

    if inner then
      -- Trim any leading or trailing blank lines in the inner message
      local lines = self.api.nvim_buf_get_lines(bufnr, start_row, end_row + 1, false)
      local s_offset = 0
      local e_offset = 0
      for i = 1, #lines do
        if lines[i] == "" then
          s_offset = s_offset + 1
        else
          break
        end
      end
      for i = #lines, 1, -1 do
        if lines[i] == "" then
          e_offset = e_offset + 1
        else
          break
        end
      end
      
      start_row = math.min(end_row, start_row + s_offset)
      end_row = math.max(start_row, end_row - e_offset)
    end
  else
    if active.meta.status == "expanded" and active.meta.height then
      end_row = start_row + active.meta.height - 1
      if inner then
        -- Exclude the Border Top and Border Bottom physical lines
        start_row = math.min(end_row, start_row + 1)
        end_row = math.max(start_row, end_row - 1)
      end
    end
  end

  return start_row, end_row
end

--- Select the resolved line range in visual line mode.
--- @param obj_type string "message", "thought", or "tool".
--- @param inner boolean Whether to return the inner range (true) or around range (false).
function UI:select_text_object(obj_type, inner)
  local bufnr = self.api.nvim_get_current_buf()
  local s, e = self:resolve_text_object_range(bufnr, obj_type, inner)
  if s and e then
    -- Move cursor to start_row, col 0
    self.api.nvim_win_set_cursor(0, { s + 1, 0 })
    -- Start visual line mode "V"
    vim.cmd("normal! V")
    -- Move cursor to end_row, col 0 to extend selection
    self.api.nvim_win_set_cursor(0, { e + 1, 0 })
  end
end

--- Jump vertically to the previous or next extmark of the given type.
--- @param obj_type string "message", "thought", or "tool".
--- @param forward boolean True to jump forward (next), false to jump backward (previous).
function UI:jump_to_extmark(obj_type, forward)
  local bufnr = self.api.nvim_get_current_buf()
  local cursor_row = self.api.nvim_win_get_cursor(0)[1] - 1
  local extmarks = self.api.nvim_buf_get_extmarks(bufnr, self.namespace, 0, -1, {})

  if #extmarks == 0 then return end

  local list = {}
  for _, mark in ipairs(extmarks) do
    local extmark_id = mark[1]
    local r = mark[2]
    local meta = self.extmark_metadata[extmark_id]
    if meta then
      local matched = false
      if obj_type == "message" then
        matched = (meta.type == "message")
      elseif obj_type == "thought" then
        matched = (meta.type == "thought")
      elseif obj_type == "tool" then
        matched = (meta.type == "tool" or meta.type == "confirmation" or meta.type == "function_call" or meta.type == "function_response")
      end

      if matched then
        table.insert(list, r)
      end
    end
  end

  if #list == 0 then return end

  -- Sort unique row coordinates ascending
  table.sort(list)

  local target_row = nil
  if forward then
    for _, r in ipairs(list) do
      if r > cursor_row then
        target_row = r
        break
      end
    end
  else
    for i = #list, 1, -1 do
      local r = list[i]
      if r < cursor_row then
        target_row = r
        break
      end
    end
  end

  if target_row then
    self.api.nvim_win_set_cursor(0, { target_row + 1, 0 })
  end
end

function UI.select_text_object_static(obj_type, inner)
  return UI.get_instance():select_text_object(obj_type, inner)
end

function UI.jump_to_extmark_static(obj_type, forward)
  return UI.get_instance():jump_to_extmark(obj_type, forward)
end

-- Keeps track of the active floating HUD windows
UI.active_huds = {} -- Map of bufnr -> { win_id = integer, bufnr = integer }

function UI:update_sticky_hud(parent_bufnr)
  if not parent_bufnr or parent_bufnr == 0 or not self.api.nvim_buf_is_valid(parent_bufnr) then
    return
  end

  local parent_buf = parent_bufnr
  if vim.bo[parent_buf].filetype == "code_savant_input" then
    parent_buf = vim.b[parent_buf].partner_buf or parent_buf
  end

  local session = self:_get_session(parent_buf)
  local parent_session_id = vim.b[parent_buf].session_id or ""
  if parent_session_id == "" then return end

  -- 1. Get all active subagents for this parent session
  local active_subagents = {}
  for _, s in pairs(self.sessions) do
    if s.parent_id == parent_session_id and s.status == "thinking" then
      table.insert(active_subagents, s)
    end
  end

  -- If no active agents, and HUD is open, we can close it
  if #active_subagents == 0 then
    self:close_sticky_hud(parent_bufnr)
    return
  end

  -- 2. Ensure HUD buffer and window exist
  local hud = self.active_huds[parent_bufnr]
  if not hud or not self.api.nvim_win_is_valid(hud.win_id) then
    local hud_buf = self.api.nvim_create_buf(false, true)
    self.api.nvim_buf_set_option(hud_buf, "buftype", "nofile")
    self.api.nvim_buf_set_option(hud_buf, "bufhidden", "wipe")
    self.api.nvim_buf_set_option(hud_buf, "swapfile", false)

    -- Find parent window
    local parent_winids = vim.fn.win_findbuf(parent_bufnr)
    if not parent_winids or #parent_winids == 0 then return end
    local parent_win = parent_winids[1]
    local parent_width = self.api.nvim_win_get_width(parent_win)

    local win_id = self.api.nvim_open_win(hud_buf, false, {
      relative = "win",
      win = parent_win,
      row = 0,
      col = 0,
      width = parent_width,
      height = math.min(5, #active_subagents + 1),
      style = "minimal",
      border = "single",
    })

    hud = { win_id = win_id, bufnr = hud_buf }
    self.active_huds[parent_bufnr] = hud
  end

  -- 3. Update HUD buffer lines
  local lines = {}
  table.insert(lines, " 󰒋  Active Swarm Subagents:")
  for _, s in ipairs(active_subagents) do
    local last_up = s.last_update
    if type(last_up) ~= "string" or last_up == "" then
      last_up = "idle"
    end
    if #last_up > 50 then last_up = last_up:sub(1, 47) .. "..." end
    table.insert(lines, string.format("   ● [%s] - %s", tostring(s.agent_name), last_up))
  end

  -- Enable modifiable to set lines, then lock
  self.api.nvim_buf_set_option(hud.bufnr, "modifiable", true)
  self.api.nvim_buf_set_lines(hud.bufnr, 0, -1, false, lines)
  self.api.nvim_buf_set_option(hud.bufnr, "modifiable", false)

  -- Dynamically adjust height of float
  self.api.nvim_win_set_height(hud.win_id, math.min(6, #lines))
end

function UI:close_sticky_hud(parent_bufnr)
  local hud = self.active_huds[parent_bufnr]
  if hud then
    if self.api.nvim_win_is_valid(hud.win_id) then
      pcall(self.api.nvim_win_close, hud.win_id, true)
    end
    self.active_huds[parent_bufnr] = nil
  end
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
