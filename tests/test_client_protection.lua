-- Headless Lua test suite for CodeSavant Native :bd and Redirection Protection
vim.opt.rtp:append(".")
print("Running CodeSavant protection and deletion tests...")

local has_cs, cs = pcall(require, "code_savant")
if not has_cs then
  print("FAIL: Could not load code_savant module: " .. tostring(cs))
  os.exit(1)
end

local function assert_eq(actual, expected, msg)
  if actual ~= expected then
    print(string.format("FAIL: Expected %s, got %s. Context: %s", vim.inspect(expected), vim.inspect(actual), msg or ""))
    os.exit(1)
  end
end

-- 1. Setup
cs.setup({ spawn_split = "vsplit" })

-- 2. Test Deletion & Cycling with multiple active sessions
print("Step 1: Testing native deletion and session cycling...")

-- Create Session 1
local s1 = cs.create_chat_buffer()
-- Create Session 2
local s2 = cs.create_chat_buffer()

assert(s1.bufnr ~= s2.bufnr, "Sessions should have unique buffer IDs")

-- Focus Session 2
vim.api.nvim_set_current_win(s2.history_win)

-- Wipe out Session 2 history buffer natively (like :bd)
vim.api.nvim_buf_delete(s2.bufnr, { force = true })

-- Wait for deferred schedule handlers to cycle sessions via non-blocking vim.wait
local success = vim.wait(200, function()
  local current_win = vim.api.nvim_get_current_win()
  local current_buf = vim.api.nvim_win_get_buf(current_win)
  return current_buf == s1.bufnr or current_buf == s1.input_bufnr
end, 10)

if not success then
  print("FAIL: Wiping out Session 2 did not automatically cycle focus to Session 1 splits!")
  os.exit(1)
end
print("Step 1 Passed: Deleting active buffer safely cycled to next session.")

-- 3. Test final session deletion (should wipe splits)
print("Step 2: Testing final session buffer wipeout...")

-- Deleting Session 1 history buffer natively
vim.api.nvim_buf_delete(s1.bufnr, { force = true })

success = vim.wait(200, function()
  local h_win, i_win = require("code_savant.layout").find_visible_layout()
  return h_win == nil and i_win == nil
end, 10)

if not success then
  print("FAIL: Deleting final session did not close/wipeout splits!")
  os.exit(1)
end
print("Step 2 Passed: Deleting final session closed splits cleanly.")

-- 4. Test Deletion & Cycling when deleting the INPUT buffer (Symmetric protection)
print("Step 3: Testing input buffer deletion and symmetric cycling...")

local sa = cs.create_chat_buffer()
local sb = cs.create_chat_buffer()

assert(sa.input_bufnr ~= sb.input_bufnr, "Sessions should have unique input buffer IDs")

-- Focus Session B's Input Window
vim.api.nvim_set_current_win(sb.input_win)

-- Wipe out Session B's INPUT buffer natively (simulating :bd inside focused input box)
vim.api.nvim_buf_delete(sb.input_bufnr, { force = true })

-- Wait for deferred schedule handlers to cycle sessions via non-blocking vim.wait
success = vim.wait(200, function()
  local current_win = vim.api.nvim_get_current_win()
  local current_buf = vim.api.nvim_win_get_buf(current_win)
  return current_buf == sa.bufnr or current_buf == sa.input_bufnr
end, 10)

if not success then
  print("FAIL: Wiping out Session B's INPUT buffer did not cycle focus to Session A!")
  os.exit(1)
end

-- Clean up remaining session
vim.api.nvim_buf_delete(sa.bufnr, { force = true })
print("Step 3 Passed: Deleting active input buffer safely cycled to next session.")

print("ALL PROTECTION AND DELETION TESTS PASSED SUCCESSFULLY!")
os.exit(0)
