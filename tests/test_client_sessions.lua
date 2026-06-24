-- Headless Lua test suite for CodeSavant Session Management and Navigation
vim.opt.rtp:append(".")
print("=== STARTING CODE SAVANT SESSION MANAGEMENT TESTS ===")

local has_code_savant, code_savant = pcall(require, "code_savant")
if not has_code_savant then
  print("FAIL: Could not load code_savant module: " .. tostring(code_savant))
  os.exit(1)
end

-- Reset and configure
code_savant.setup({
  spawn_split = "vsplit",
  keymaps = {
    next_session = { key = "<Tab>", desc = "Cycle to next session" },
    prev_session = { key = "<S-Tab>", desc = "Cycle to previous session" },
  }
})

-- Test 1: Verify configurable spawning options
print("Test 1: Verification of configurable spawn split...")
local initial_win_count = #vim.api.nvim_list_wins()

-- Create first session
local session1 = code_savant.create_chat_buffer()
if not session1 or not session1.bufnr or not session1.input_bufnr then
  print("FAIL: Failed to create session 1")
  os.exit(1)
end

local final_win_count = #vim.api.nvim_list_wins()
if final_win_count <= initial_win_count then
  print("FAIL: Spawning new session did not open expected window splits. Init: " .. tostring(initial_win_count) .. ", Final: " .. tostring(final_win_count))
  os.exit(1)
end
print("Test 1 Passed!")

-- Test 1.5: Verify sidebar buffer redirection protection (Option 3)
print("Test 1.5: Verification of sidebar buffer redirection protection...")
local temp_buf = vim.api.nvim_create_buf(false, true)
vim.bo[temp_buf].filetype = "python"

-- Focus history window (stored in session1.input_win due to partner_win linkage) and switch its buffer
vim.api.nvim_set_current_win(session1.input_win)
vim.api.nvim_win_set_buf(session1.input_win, temp_buf)

-- Trigger the BufEnter redirection autocommand
vim.api.nvim_exec_autocmds("BufEnter", { buffer = temp_buf })

-- Check if history window was restored to the history buffer
local hist_buf = vim.api.nvim_win_get_buf(session1.input_win)
if hist_buf ~= session1.bufnr then
  print("FAIL: Sidebar history window was not restored to history buffer! Got: " .. tostring(hist_buf) .. ", Expected: " .. tostring(session1.bufnr))
  os.exit(1)
end

-- Clean up
print("Test 1.5 Passed!")

-- Test 2: Create multiple sessions and verify isolation
print("Test 2: Verification of multiple active sessions...")
local session2 = code_savant.create_chat_buffer()
if not session2 or not session2.bufnr or not session2.input_bufnr then
  print("FAIL: Failed to create session 2")
  os.exit(1)
end

if session1.bufnr == session2.bufnr or session1.input_bufnr == session2.input_bufnr then
  print("FAIL: Sessions did not receive unique isolated buffers. S1 hist: " .. tostring(session1.bufnr) .. ", S2 hist: " .. tostring(session2.bufnr))
  os.exit(1)
end

-- Check local partner linkages
if vim.b[session1.bufnr].partner_buf ~= session1.input_bufnr then
  print("FAIL: Session 1 history buffer not linked correctly to input buffer.")
  os.exit(1)
end
if vim.b[session2.bufnr].partner_buf ~= session2.input_bufnr then
  print("FAIL: Session 2 history buffer not linked correctly to input buffer.")
  os.exit(1)
end
print("Test 2 Passed!")

-- Test 3: Session Cycling via user commands
print("Test 3: Verification of session cycling commands...")

-- Focus session 2 history window, then switch to session 1
vim.api.nvim_set_current_win(session2.history_win)

-- Trigger cycle previous
vim.cmd("CodeSavantPrevSession")

local current_win = vim.api.nvim_get_current_win()
local current_buf = vim.api.nvim_win_get_buf(current_win)

-- Since next/prev session cycles to the partner input window, check both history or input of session 1
if current_buf ~= session1.bufnr and current_buf ~= session1.input_bufnr then
  print("FAIL: CodeSavantPrevSession did not cycle to Session 1 buffer. Current buffer: " .. tostring(current_buf) .. ", Expected S1 hist: " .. tostring(session1.bufnr) .. " or input: " .. tostring(session1.input_bufnr))
  os.exit(1)
end

-- Trigger cycle next
vim.cmd("CodeSavantNextSession")
current_win = vim.api.nvim_get_current_win()
current_buf = vim.api.nvim_win_get_buf(current_win)

if current_buf ~= session2.bufnr and current_buf ~= session2.input_bufnr then
  print("FAIL: CodeSavantNextSession did not cycle back to Session 2. Current buffer: " .. tostring(current_buf))
  os.exit(1)
end
print("Test 3 Passed!")

-- Test 4: Verify contextual commands trapping config and submit_alt bindings
print("Test 4: Verification of command abbreviations / keymaps...")
-- Abbreviations are evaluated inside command line, let's verify keymaps are locally bound
local input_keymaps = vim.api.nvim_buf_get_keymap(session1.input_bufnr, "n")
local found_cycle = false
local found_submit_normal = false
for _, map in ipairs(input_keymaps) do
  if map.desc and map.desc:match("CodeSavant Cycle to next session") then
    found_cycle = true
  elseif map.desc and map.desc:match("CodeSavant Submit prompt") and map.lhs == "<S-CR>" then
    found_submit_normal = true
  end
end

local input_keymaps_i = vim.api.nvim_buf_get_keymap(session1.input_bufnr, "i")
local found_submit_insert = false
for _, map in ipairs(input_keymaps_i) do
  if map.desc and map.desc:match("CodeSavant Submit prompt") and map.lhs == "<S-CR>" then
    found_submit_insert = true
    break
  end
end

if not found_cycle then
  print("FAIL: Session cycling keymaps not locally bound to buffers.")
  os.exit(1)
end
if not found_submit_normal then
  print("FAIL: Alternative submit keymap not locally bound in Normal mode.")
  os.exit(1)
end
if not found_submit_insert then
  print("FAIL: Alternative submit keymap not locally bound in Insert mode.")
  os.exit(1)
end
print("Test 4 Passed!")

-- Test 4.5: Verify WinClosed synchronization closes both windows but keeps buffers alive
print("Test 4.5: Verification of WinClosed split-closure synchronization...")
local h_buf = session2.bufnr
local i_buf = session2.input_bufnr
local h_win = session2.history_win
local i_win = session2.input_win

-- Manually close the input window
pcall(vim.api.nvim_win_close, i_win, true)

-- Wait for the WinClosed schedule tick to propagate
local sync_completed = false
local win_timer = vim.uv.new_timer()
win_timer:start(100, 0, function()
  win_timer:stop()
  win_timer:close()

  vim.schedule(function()
    -- Check that both windows are closed
    local h_win_valid = vim.api.nvim_win_is_valid(h_win)
    local i_win_valid = vim.api.nvim_win_is_valid(i_win)

    if h_win_valid or i_win_valid then
      print("FAIL: WinClosed synchronization did not close both windows. Hist win valid: " .. tostring(h_win_valid) .. ", Input win valid: " .. tostring(i_win_valid))
      os.exit(1)
    end

    -- Check that both buffers remain valid and loaded in memory
    local h_buf_valid = vim.api.nvim_buf_is_valid(h_buf)
    local i_buf_valid = vim.api.nvim_buf_is_valid(i_buf)

    if not h_buf_valid or not i_buf_valid then
      print("FAIL: WinClosed synchronization wiped out the buffers from memory! Hist buf: " .. tostring(h_buf_valid) .. ", Input buf: " .. tostring(i_buf_valid))
      os.exit(1)
    end

    print("Test 4.5 Passed!")
    sync_completed = true
  end)
end)

vim.wait(1000, function() return sync_completed end, 50)
if not sync_completed then
  print("FAIL: Timeout waiting for WinClosed sync test")
  os.exit(1)
end

-- Test 5: Dynamic window cleanup during wiping out of last session
print("Test 5: Verification of session wiping out and dynamic window destruction...")
-- Mount session 1 to make it active and visible in the layout splits
code_savant.mount_session(session1.bufnr)

-- First, delete session 2's history buffer (which should cycle splits to session 1)
vim.api.nvim_buf_delete(session2.bufnr, { force = true })

local pre_wipe_wins = #vim.api.nvim_list_wins()

-- Now, delete history buffer of session 1 (the final session) which should close the splits
vim.api.nvim_buf_delete(session1.bufnr, { force = true })

-- Give scheduling loop a brief tick to process BufWipeout callbacks
local test_completed = false
local timer = vim.uv.new_timer()
timer:start(200, 0, function()
  timer:stop()
  timer:close()
  
  vim.schedule(function()
    local post_wipe_wins = #vim.api.nvim_list_wins()
    if post_wipe_wins >= pre_wipe_wins then
      print("FAIL: Deleting final history buffer did not tear down corresponding session windows dynamically. Pre: " .. tostring(pre_wipe_wins) .. ", Post: " .. tostring(post_wipe_wins))
      os.exit(1)
    end
    
    print("Test 5 Passed!")
    print("=== ALL SESSION TESTS PASSED SUCCESSFULLY ===")
    test_completed = true
  end)
end)

vim.wait(1000, function() return test_completed end, 50)
if not test_completed then
  print("FAIL: Timeout waiting for async cleanup")
  os.exit(1)
end

os.exit(0)
