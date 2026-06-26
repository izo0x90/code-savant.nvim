-- Headless Lua test suite for CodeSavant Custom Text-Objects and Motions
vim.opt.rtp:append(".")
print("=== STARTING CODE SAVANT TEXT OBJECTS & MOTIONS TESTS ===")

local has_code_savant, code_savant = pcall(require, "code_savant")
if not has_code_savant then
  print("FAIL: Could not load code_savant module: " .. tostring(code_savant))
  os.exit(1)
end

local UI = require("code_savant.ui").get_instance()

local function assert_eq(actual, expected, msg)
  if actual ~= expected then
    error(string.format("[ASSERT FAILURE] Expected: %s, Got: %s. Context: %s", vim.inspect(expected), vim.inspect(actual), msg or ""))
  end
end

local bufnr = UI:create_chat_buffer()
assert(vim.api.nvim_buf_is_valid(bufnr), "Buffer must be valid")

-- Clear metadata cache
UI.extmark_metadata = {}
UI.block_to_extmark = {}

-- 1. Setup a structured conversation in the buffer
-- Line 0 (Row 0): Blank margin
-- Line 1 (Row 1): User Message starts
UI:render_message(bufnr, "user", "Hello CodeSavant")

-- Now, wait. A user prompt was written. Let's verify line count and where the extmark is.
local lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
-- Buffer has the prompt text lines.
-- Let's render a thought block
UI:render_message(bufnr, "thought", {
  id = "test_thought_1",
  title = "Deep Thinking Phase",
  content = "Line 1 of thought\nLine 2 of thought"
})

-- Let's render a tool block
UI:render_message(bufnr, "function_call", {
  id = "test_tool_1",
  title = "Invoke bash tool",
  content = "ls -la"
})

-- Let's render a model response
UI:render_message(bufnr, "model", "Here are the files in your directory.")

-- Let's verify our metadata structures are populated
local extmark_count = 0
for _, _ in pairs(UI.extmark_metadata) do
  extmark_count = extmark_count + 1
end
assert(extmark_count >= 4, "Metadata should contain entries for all rendered turns and blocks")

print("DEBUG: Active buffer lines and heights verified.")

-- 2. Test Range Resolution for message (User)
-- Move cursor to row 1 (User message line)
vim.api.nvim_win_set_buf(0, bufnr)
vim.api.nvim_win_set_cursor(0, { 2, 0 })

-- Pass 1 explicitly as the target_row to resolve user message boundaries deterministically (aligned with first turn row 1)
local msg_s, msg_e = UI:resolve_text_object_range(bufnr, "message", false, 1)
assert(msg_s ~= nil, "Should resolve message start")
assert(msg_e ~= nil, "Should resolve message end")
assert_eq(msg_s, 1, "User message start row")

-- 3. Test Range Resolution for nested collapsible thought block (when expanded)
-- Let's expand the thought block
UI:expand_inplace({ id = "test_thought_1" })

local thought_cached = UI.collapsed_blocks_cache["test_thought_1"]
assert_eq(thought_cached.status, "expanded", "Thought block should be expanded")

-- Locate expanded thought extmark row
local thought_pos = vim.api.nvim_buf_get_extmark_by_id(bufnr, UI.namespace, thought_cached.extmark_id, {})
local thought_start_row = thought_pos[1]

-- Move cursor inside the expanded thought block (e.g. start_row + 2)
vim.api.nvim_win_set_cursor(0, { thought_start_row + 3, 0 }) -- 1-indexed cursor

-- Verify Around Thought (includes borders) passing the target_row explicitly for absolute test stability
local th_s_ao, th_e_ao = UI:resolve_text_object_range(bufnr, "thought", false, thought_start_row + 2)
assert_eq(th_s_ao, thought_start_row, "Around thought start row")
assert_eq(th_e_ao, thought_start_row + thought_cached.height - 1, "Around thought end row")

-- Verify Inner Thought (excludes borders) passing the target_row explicitly for absolute test stability
local th_s_io, th_e_io = UI:resolve_text_object_range(bufnr, "thought", true, thought_start_row + 2)
assert_eq(th_s_io, thought_start_row + 1, "Inner thought start row")
assert_eq(th_e_io, thought_start_row + thought_cached.height - 2, "Inner thought end row")

print("DEBUG: Text-object boundaries verified flawlessly.")

-- 4. Test Jumps (Motions)
-- Move cursor back to row 1
vim.api.nvim_win_set_cursor(0, { 2, 0 })

-- Jump to next thought
UI:jump_to_extmark("thought", true)
local cur_pos = vim.api.nvim_win_get_cursor(0)
assert_eq(cur_pos[1] - 1, thought_start_row, "Cursor should jump directly to the expanded thought extmark row")

-- Jump to previous message
UI:jump_to_extmark("message", false)
cur_pos = vim.api.nvim_win_get_cursor(0)
assert_eq(cur_pos[1], 2, "Cursor should jump back to User message start line (row 1)")

print("DEBUG: Navigation motions jumping verified flawlessly.")

print("=== ALL TEXT OBJECTS & MOTIONS TESTS PASSED SUCCESSFULLY ===")
os.exit(0)
