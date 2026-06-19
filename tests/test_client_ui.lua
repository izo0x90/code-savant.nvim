-- Redefine print to always flush stdout safely to avoid headless Neovim buffering
local original_print = print
print = function(...)
  original_print(...)
  pcall(function() io.stdout:flush() end)
end

-- Add local paths to package.path to ensure we can require code_savant.ui
package.path = "./lua/?.lua;./lua/?/init.lua;" .. package.path


local UI_Manager = require("code_savant.ui")

local function assert_eq(actual, expected, msg)
  if actual ~= expected then
    error(string.format("[ASSERT FAILURE] Expected: %s, Got: %s. Context: %s", vim.inspect(expected), vim.inspect(actual), msg or ""))
  end
end

local function run_tests()
  print("=== STARTING CODE SAVANT UI TESTS ===")

  -- 1. Test class initialization
  print("DEBUG: Step 1 - Class Initialization")
  local ui = UI_Manager.new()
  assert(ui ~= nil, "UI manager instance should not be nil")
  assert(ui.namespace ~= nil, "UI manager namespace should be created")
  assert_eq(type(ui.collapsed_blocks_cache), "table", "collapsed_blocks_cache should be initialized as a table")

  -- 2. Test chat buffer creation
  print("DEBUG: Step 2 - Chat Buffer Creation")
  local bufnr = ui:create_chat_buffer()
  assert(vim.api.nvim_buf_is_valid(bufnr), "Created buffer should be valid")
  assert_eq(vim.api.nvim_buf_get_option(bufnr, "buftype"), "nofile", "Buffer buftype should be nofile")

  -- 3. Test on_collapsed_block with invalid arguments (should return nil early)
  print("DEBUG: Step 3 - Invalid Collapsed Block Args")
  local bad_res = ui:on_collapsed_block("", "thought", "Title", "Content", bufnr, 0)
  assert_eq(bad_res, nil, "Should return nil for empty id")

  local bad_res2 = ui:on_collapsed_block("id1", "", "Title", "Content", bufnr, 0)
  assert_eq(bad_res2, nil, "Should return nil for empty block_type")

  -- 4. Test on_collapsed_block with valid arguments
  print("DEBUG: Step 4 - Valid Collapsed Block")
  local id = "test_block_1"
  local block_type = "thought"
  local title = "Deep reasoning about Neovim"
  local full_content = "This is line 1\nThis is line 2 of the secret thought."
  
  -- Set lines in buffer first so we have lines
  ui:run_programmatic_update(bufnr, function()
    vim.api.nvim_buf_set_lines(bufnr, 0, -1, false, { "Line 1", "Line 2", "Line 3", "Line 4", "Line 5" })
  end)

  local extmark_id = ui:on_collapsed_block(id, block_type, title, full_content, bufnr, 2)
  assert(extmark_id ~= nil, "Extmark ID should be returned")

  -- Check if cached
  local cached = ui.collapsed_blocks_cache[id]
  assert(cached ~= nil, "Block should be cached")
  assert_eq(cached.full_content, full_content, "Cached content should match")
  assert_eq(cached.extmark_id, extmark_id, "Cached extmark_id should match")
  assert_eq(cached.row, 2, "Cached row should match target row")

  -- Verify extmark is indeed in Neovim
  local extmark_pos = vim.api.nvim_buf_get_extmark_by_id(bufnr, ui.namespace, extmark_id, {})
  assert(extmark_pos ~= nil and #extmark_pos > 0, "Extmark position should be retrievable")
  assert_eq(extmark_pos[1], 2, "Extmark should be placed at row 2")

  -- 5. Test expand_inplace and collapse_inplace
  print("DEBUG: Step 5 - Expand and Collapse Inplace")
  ui:expand_inplace({ id = id })

  -- Verify line was replaced with full content + borders
  local buf_lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
  
  -- The original buffer had 5 lines:
  -- "Line 1", "Line 2", "Line 3", "Line 4", "Line 5"
  -- We replaced line at index 2 (which is "Line 3", 0-indexed) with the expanded block.
  -- The expanded block has borders:
  -- - Border Top
  -- - Line 1
  -- - Line 2 of the secret thought.
  -- - Border Bottom
  -- So "Line 3" should be replaced by those 4 lines.
  -- Total line count should be 5 - 1 + 4 = 8 lines.
  assert_eq(#buf_lines, 8, "Buffer line count after expansion should be 8")
  assert_eq(buf_lines[3], ui.CONSTANTS.BORDER_TOP, "Border Top should be present")
  assert_eq(buf_lines[4], "This is line 1", "Content line 1 should be present")
  assert_eq(buf_lines[5], "This is line 2 of the secret thought.", "Content line 2 should be present")
  assert_eq(buf_lines[6], ui.CONSTANTS.BORDER_BOTTOM, "Border Bottom should be present")

  -- Verify cache remains and is updated to "expanded"
  local expanded_cached = ui.collapsed_blocks_cache[id]
  assert(expanded_cached ~= nil, "Cache entry should NOT be nil after expansion")
  assert_eq(expanded_cached.status, "expanded", "Cache entry status should be 'expanded'")
  assert_eq(expanded_cached.height, 4, "Cache entry height should be 4")

  -- Verify old extmark is deleted
  local deleted_extmark_pos = vim.api.nvim_buf_get_extmark_by_id(bufnr, ui.namespace, extmark_id, {})
  assert_eq(#deleted_extmark_pos, 0, "Old collapsed extmark should be deleted after expansion")

  -- Verify new expanded extmark is active at the correct row
  local new_extmark_id = expanded_cached.extmark_id
  local new_extmark_pos = vim.api.nvim_buf_get_extmark_by_id(bufnr, ui.namespace, new_extmark_id, {})
  assert(new_extmark_pos ~= nil and #new_extmark_pos > 0, "New expanded extmark position should be retrievable")
  assert_eq(new_extmark_pos[1], 2, "New expanded extmark should be anchored at row 2")

  -- Now, test collapsing it back!
  print("DEBUG: Step 5.1 - Collapse Inplace")
  ui:collapse_inplace({ id = id })

  -- Verify line count and contents are restored to original (5 lines)
  local collapsed_lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
  assert_eq(#collapsed_lines, 5, "Buffer line count after collapsing should be restored to 5")
  assert_eq(collapsed_lines[3], "", "Line index 3 (0-indexed row 2) should be empty")
  assert_eq(collapsed_lines[4], "Line 4", "Line index 4 (0-indexed row 3) should be 'Line 4'")

  -- Verify cache is updated back to "collapsed"
  local collapsed_cached = ui.collapsed_blocks_cache[id]
  assert(collapsed_cached ~= nil, "Cache entry should NOT be nil after collapsing")
  assert_eq(collapsed_cached.status, "collapsed", "Cache entry status should be 'collapsed'")
  assert_eq(collapsed_cached.height, nil, "Cache entry height should be nil after collapsing")

  -- Verify expanded extmark is deleted
  local deleted_expanded_extmark_pos = vim.api.nvim_buf_get_extmark_by_id(bufnr, ui.namespace, new_extmark_id, {})
  assert_eq(#deleted_expanded_extmark_pos, 0, "Expanded extmark should be deleted after collapsing")

  -- Verify new collapsed extmark is active at row 2
  local final_extmark_id = collapsed_cached.extmark_id
  local final_extmark_pos = vim.api.nvim_buf_get_extmark_by_id(bufnr, ui.namespace, final_extmark_id, {})
  assert(final_extmark_pos ~= nil and #final_extmark_pos > 0, "Final collapsed extmark position should be retrievable")
  assert_eq(final_extmark_pos[1], 2, "Final collapsed extmark should be anchored at row 2")

  -- 5.2 Test open_in_new_buf
  print("DEBUG: Step 5.2 - Open in New Buffer (Vertical Split)")
  local target_buf, target_win = ui:open_in_new_buf({ id = id })
  assert(vim.api.nvim_buf_is_valid(target_buf), "New buffer should be valid")
  assert(vim.api.nvim_win_is_valid(target_win), "New window should be valid")
  assert_eq(vim.api.nvim_buf_get_option(target_buf, "buftype"), "nofile", "Buffer should be scratch type")
  assert_eq(vim.api.nvim_buf_get_option(target_buf, "filetype"), "code_savant_thought", "Buffer filetype should be code_savant_thought")
  local new_buf_lines = vim.api.nvim_buf_get_lines(target_buf, 0, -1, false)
  assert_eq(#new_buf_lines, 2, "New buffer should contain 2 lines of content")
  assert_eq(new_buf_lines[1], "This is line 1", "Line 1 contents should match")
  pcall(vim.api.nvim_win_close, target_win, true)

  -- 5.3 Test open_in_float
  print("DEBUG: Step 5.3 - Open in Floating Window")
  local float_buf, float_win = ui:open_in_float({ id = id })
  assert(vim.api.nvim_buf_is_valid(float_buf), "Float buffer should be valid")
  assert(vim.api.nvim_win_is_valid(float_win), "Float window should be valid")
  assert_eq(vim.api.nvim_buf_get_option(float_buf, "filetype"), "code_savant_thought", "Float buffer filetype should be code_savant_thought")
  local float_lines = vim.api.nvim_buf_get_lines(float_buf, 0, -1, false)
  assert_eq(#float_lines, 2, "Float buffer should contain 2 lines of content")
  pcall(vim.api.nvim_win_close, float_win, true)

  -- 6. Test Native Modifiable Protection and Programmatic Updates
  print("DEBUG: Step 6 - Native Modifiable Protection")
  local test_buf = ui:create_chat_buffer()

  -- Assert that editing the history buffer directly throws an error because modifiable is false
  local edit_ok, edit_err = pcall(vim.api.nvim_buf_set_lines, test_buf, 0, -1, false, { "User attempt to write" })
  assert(not edit_ok, "Direct user edits to history buffer should fail when modifiable is false")

  -- 6.1 Test Programmatic Update
  print("DEBUG: Step 6.1 - Programmatic Update Execution")
  local prog_ok = pcall(ui.run_programmatic_update, ui, test_buf, function()
    vim.api.nvim_buf_set_lines(test_buf, 0, -1, false, { "Programmatic line 1", "Programmatic line 2" })
  end)
  assert(prog_ok, "Programmatic update should succeed")

  -- Assert the contents were successfully written
  local final_lines = vim.api.nvim_buf_get_lines(test_buf, 0, -1, false)
  assert_eq(#final_lines, 2, "Programmatic update should have written 2 lines")
  assert_eq(final_lines[1], "Programmatic line 1", "Line 1 should match")

  -- Assert that the buffer is natively read-only again after programmatic update finishes
  assert_eq(vim.api.nvim_buf_get_option(test_buf, "modifiable"), false, "Buffer should be read-only after programmatic update")

  -- Assert that direct user edits still fail
  local post_edit_ok = pcall(vim.api.nvim_buf_set_lines, test_buf, 0, -1, false, { "Malicious user edit" })
  assert(not post_edit_ok, "User edits should still fail after programmatic update")

  -- 6.2 Test Native Animated Spinner Loader
  print("DEBUG: Step 6.2 - Native Animated Spinner Loader")
  local spin_buf = ui:create_chat_buffer()
  ui:run_programmatic_update(spin_buf, function()
    vim.api.nvim_buf_set_lines(spin_buf, 0, -1, false, { "◀   CodeSavant is thinking..." })
  end)

  -- Start thinking spinner
  ui:start_spinner(spin_buf, "thinking", {
    type = "braille",
    use_extmark = true,
    col = 4,
    row = 0,
    format_fn = function(s) return { { s, "Special" } } end
  })

  local registry_key = spin_buf .. ":thinking"
  local inst = ui.animation_wheel.registry[registry_key]
  assert(inst ~= nil, "Spinner instance should be registered")
  assert_eq(inst.key, "thinking", "Registered spinner key should match")
  assert_eq(inst.use_extmark, true, "Should be virtual text overlay spinner")
  assert_eq(inst.col, 4, "Should have column offset 4")
  assert(ui.animation_wheel.timer ~= nil, "Global timer wheel should be running")

  -- Stop thinking spinner
  ui:stop_spinner(spin_buf, "thinking")
  assert(ui.animation_wheel.registry[registry_key] == nil, "Spinner instance should be unregistered")

  -- Start a custom spinner
  ui:start_spinner(spin_buf, "custom_spin", {
    type = "custom",
    custom_frames = { "A", "B", "C" },
    use_extmark = true,
    col = 0,
    row = 0,
    format_fn = function(s) return { { "Custom: " .. s, "Comment" } } end
  })
  local custom_inst = ui.animation_wheel.registry[spin_buf .. ":custom_spin"]
  assert(custom_inst ~= nil, "Custom spinner should be registered")
  assert_eq(custom_inst.frames[1], "A", "Custom frame 1 should match")

  ui:stop_spinner(spin_buf, "custom_spin")

  -- 7. Test teardown / clean cleanup
  print("DEBUG: Step 7 - Teardown")
  ui:teardown()
  assert_eq(next(ui.collapsed_blocks_cache), nil, "Cache should be empty after teardown")

  print("=== ALL TESTS PASSED SUCCESSFULLY ===")
end

local ok, err = pcall(run_tests)
if not ok then
  print("TEST FAILED:")
  print(err)
  os.exit(1)
else
  os.exit(0)
end
