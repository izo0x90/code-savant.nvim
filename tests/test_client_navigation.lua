-- Headless Lua test suite for CodeSavant Navigation
vim.opt.rtp:append(".")
print("Running CodeSavant navigation tests...")

-- 1. Ensure we can load our module
local has_nav, nav = pcall(require, "code_savant.navigation")
if not has_nav then
  print("FAIL: Could not load code_savant.navigation module: " .. tostring(nav))
  os.exit(1)
end
print("Step 1 Passed: Loaded code_savant.navigation successfully.")

-- Helper function for assertions
local function assert_eq(actual, expected, msg)
  if actual ~= expected then
    print(string.format("FAIL: Expected %s, got %s. Context: %s", tostring(expected), tostring(actual), msg or ""))
    os.exit(1)
  end
end

-- 2. Test parse_path_string with different formats
print("Step 2: Testing parse_path_string parsing formats...")

-- Case A: File with line and column numbers
local pathA, lineA, colA = nav.parse_path_string("src/engine/executor.py:42:15")
assert_eq(pathA, "src/engine/executor.py", "Case A path")
assert_eq(lineA, 42, "Case A line")
assert_eq(colA, 15, "Case A column")

-- Case B: File with only line number
local pathB, lineB, colB = nav.parse_path_string("lua/code_savant/init.lua:105")
assert_eq(pathB, "lua/code_savant/init.lua", "Case B path")
assert_eq(lineB, 105, "Case B line")
assert_eq(colB, nil, "Case B column")

-- Case C: File path only without colons
local pathC, lineC, colC = nav.parse_path_string("pyproject.toml")
assert_eq(pathC, "pyproject.toml", "Case C path")
assert_eq(lineC, nil, "Case C line")
assert_eq(colC, nil, "Case C column")

print("Step 2 Passed: parse_path_string formats parsed perfectly.")

-- 3. Verify robust wrapping stripper logic in isolation
print("Step 3: Verification of expand wrapping strips...")
-- Simulate cfile extracts and verify fast wrapping trims
local test_cases = {
  { input = "`src/engine/config.py:12`", expected = "src/engine/config.py:12" },
  { input = "(src/engine/executor.py:45)", expected = "src/engine/executor.py:45" },
  { input = "\"lua/code_savant/init.lua:80\"", expected = "lua/code_savant/init.lua:80" },
  { input = "'pyproject.toml'", expected = "pyproject.toml" },
  { input = "lua/code_savant/navigation.lua", expected = "lua/code_savant/navigation.lua" }
}

for _, case in ipairs(test_cases) do
  local got = case.input:gsub("^[`'\"%[%(]+", ""):gsub("[`'\"%]%)]+$", "")
  assert_eq(got, case.expected, "wrapping trim for: " .. case.input)
end
print("Step 3 Passed: wrapping strips verified.")

-- 4. Verify Telescope caching configuration and fallback execution
print("Step 4: Verification of Telescope configuration & fallback branch...")
local cs = require("code_savant")
cs._has_telescope = true
local mock_searched_text = nil
cs._telescope_api = {
  find_files = function(opts)
    mock_searched_text = opts.default_text
  end
}

-- Setup a temporary workspace editing window and buffer
local temp_buf = vim.api.nvim_create_buf(false, true)
local temp_win = vim.api.nvim_open_win(temp_buf, true, { split = "right" })

-- Call jump_to_file_at_cursor with an un-readable dummy filename
-- To make sure the cursor expander finds a path, we populate a line of text in current buffer
vim.api.nvim_buf_set_lines(0, 0, -1, false, { "non_existent_mock_file.py" })
vim.api.nvim_win_set_cursor(0, { 1, 5 }) -- Position cursor on the filename

nav.jump_to_file_at_cursor("edit")

assert_eq(mock_searched_text, "non_existent_mock_file.py", "Telescope pre-populated default_text")

-- Clean up
pcall(vim.api.nvim_win_close, temp_win, true)
pcall(vim.api.nvim_buf_delete, temp_buf, { force = true })

print("Step 4 Passed: Telescope branch verified successfully.")

print("ALL NAVIGATION TESTS PASSED SUCCESSFULLY!")
os.exit(0)
