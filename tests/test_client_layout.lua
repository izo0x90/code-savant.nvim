-- Headless Lua test suite for CodeSavant Layout Module
vim.opt.rtp:append(".")
print("Running CodeSavant layout module tests...")

-- 1. Load module
local has_layout, layout = pcall(require, "code_savant.layout")
if not has_layout then
  print("FAIL: Could not load code_savant.layout module: " .. tostring(layout))
  os.exit(1)
end
print("Step 1 Passed: Loaded layout module successfully.")

local function assert_eq(actual, expected, msg)
  if actual ~= expected then
    print(string.format("FAIL: Expected %s, got %s. Context: %s", vim.inspect(expected), vim.inspect(actual), msg or ""))
    os.exit(1)
  end
end

-- 2. Verify layout initialization & configuration merging
print("Step 2: Testing layout init...")
layout.init({ input_height = 5, sidebar_width_pct = 0.4 })
assert_eq(layout.config.input_height, 5, "injected input_height")
assert_eq(layout.config.sidebar_width_pct, 0.4, "injected sidebar_width_pct")
print("Step 2 Passed: Configuration injected cleanly.")

-- 3. Verify find_visible_layout returning nil on fresh state
print("Step 3: Testing find_visible_layout on clean slate...")
local hist, inp = layout.find_visible_layout()
assert_eq(hist, nil, "hist win on empty slate")
assert_eq(inp, nil, "inp win on empty slate")
print("Step 3 Passed: find_visible_layout handles clean state correctly.")

-- 4. Test mount_session in vertical split layout mode
print("Step 4: Testing mount_session layouts allocation...")
local cs = require("code_savant")
cs.config = { input_height = 3, sidebar_width_pct = 0.5, spawn_split = "vsplit" }
layout.init(cs.config)

local history_buf = vim.api.nvim_create_buf(false, true)
local input_buf = vim.api.nvim_create_buf(false, true)
vim.bo[history_buf].filetype = "code_savant_chat"
vim.bo[input_buf].filetype = "code_savant_input"

vim.b[history_buf].partner_buf = input_buf
vim.b[input_buf].partner_buf = history_buf

layout.mount_session(history_buf, nil, function(hb, ib)
  assert_eq(hb, history_buf, "callback history buf")
  assert_eq(ib, input_buf, "callback input buf")
end)

local h_win, i_win = layout.find_visible_layout()
assert(h_win ~= nil, "history window should be allocated")
assert(i_win ~= nil, "input window should be allocated")

-- Clean up allocations
pcall(vim.api.nvim_win_close, h_win, true)
pcall(vim.api.nvim_win_close, i_win, true)
pcall(vim.api.nvim_buf_delete, history_buf, { force = true })
pcall(vim.api.nvim_buf_delete, input_buf, { force = true })

print("Step 4 Passed: mount_session allocated splits and bound partners correctly.")

print("ALL LAYOUT TESTS PASSED SUCCESSFULLY!")
os.exit(0)
