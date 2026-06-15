-- Headless Lua test suite for CodeSavant Init & Bootstrapping
vim.opt.rtp:append(".")
print("Running CodeSavant client initialization tests...")

-- Ensure we can load our module
local has_code_savant, code_savant = pcall(require, "code_savant")
if not has_code_savant then
  print("FAIL: Could not load code_savant module: " .. tostring(code_savant))
  os.exit(1)
end

local test_completed = false
local test_failed = false

-- Test 1: Setup and configuration merging
print("Test 1: Verification of setup and configuration merging...")
local test_socket = "/tmp/test_code_savant_custom.sock"
code_savant.setup({ socket_path = test_socket })

if code_savant.config.socket_path ~= test_socket then
  print("FAIL: Custom socket_path not merged correctly. Expected: " .. test_socket .. ", got: " .. tostring(code_savant.config.socket_path))
  os.exit(1)
end
print("Test 1 Passed!")

-- Test 2: User commands registration
print("Test 2: Verification of command registration...")
local commands = vim.api.nvim_get_commands({})
if not commands.CodeSavantChat then
  print("FAIL: CodeSavantChat user command was not registered.")
  os.exit(1)
end
print("Test 2 Passed!")

-- Test 2.5: Command arguments parsing and start_chat_session invocation
print("Test 2.5: Verification of CodeSavantChat command arguments parsing...")
local captured_mock_mode = nil
local original_start_chat_session = code_savant.start_chat_session
code_savant.start_chat_session = function(bufnr, mock_mode)
  captured_mock_mode = mock_mode
end

-- Run command with "mock"
vim.api.nvim_cmd({ cmd = "CodeSavantChat", args = { "mock" } }, {})
if captured_mock_mode ~= true then
  print("FAIL: Expected mock_mode to be true when CodeSavantChat is run with 'mock'. Got: " .. tostring(captured_mock_mode))
  os.exit(1)
end

-- Run command with "live"
vim.api.nvim_cmd({ cmd = "CodeSavantChat", args = { "live" } }, {})
if captured_mock_mode ~= false then
  print("FAIL: Expected mock_mode to be false when CodeSavantChat is run with 'live'. Got: " .. tostring(captured_mock_mode))
  os.exit(1)
end

-- Run command with no arguments
captured_mock_mode = nil
vim.api.nvim_cmd({ cmd = "CodeSavantChat", args = {} }, {})
if captured_mock_mode ~= false then
  print("FAIL: Expected mock_mode to default to false with no arguments. Got: " .. tostring(captured_mock_mode))
  os.exit(1)
end

-- Restore original function
code_savant.start_chat_session = original_start_chat_session
print("Test 2.5 Passed!")

-- Test 3: Scratch-buffer generation
print("Test 3: Verification of scratch-buffer generation and settings...")
local result = code_savant.create_chat_buffer()
if not result or not result.bufnr or result.bufnr == 0 then
  print("FAIL: create_chat_buffer did not return a valid buffer number.")
  os.exit(1)
end

local bufnr = result.bufnr
local ft = vim.bo[bufnr].filetype
local bt = vim.bo[bufnr].buftype
local sf = vim.bo[bufnr].swapfile
local bh = vim.bo[bufnr].bufhidden

if ft ~= "code_savant_chat" then
  print("FAIL: Buffer filetype expected 'code_savant_chat', got: " .. tostring(ft))
  os.exit(1)
end
if bt ~= "nofile" then
  print("FAIL: Buffer buftype expected 'nofile', got: " .. tostring(bt))
  os.exit(1)
end
if sf ~= false then
  print("FAIL: Buffer swapfile should be false, got: " .. tostring(sf))
  os.exit(1)
end
if bh ~= "hide" then
  print("FAIL: Buffer bufhidden expected 'hide', got: " .. tostring(bh))
  os.exit(1)
end
print("Test 3 Passed!")

-- Test 4: Bootstrapping check (since .venv exists, it should return true immediately)
print("Test 4: Verification of self-bootstrapping check...")
local ok = code_savant.bootstrap_if_needed()
if ok ~= true then
  print("FAIL: bootstrap_if_needed should return true since .venv exists in this workspace.")
  os.exit(1)
end
print("Test 4 Passed!")

-- Test 5: Daemon start and stop sequence
print("Test 5: Verification of daemon start/stop lifecycle...")
local temp_socket = "/tmp/test_savant_lifecycle.sock"
code_savant.config.socket_path = temp_socket

code_savant.ensure_daemon_running(function(success)
  if not success then
    print("FAIL: Failed to start and verify running daemon on socket: " .. temp_socket)
    test_failed = true
    test_completed = true
    return
  end

  print("Daemon is verified as running!")
  
  -- Now verify that is_daemon_running reports true
  code_savant.is_daemon_running(function(running)
    if not running then
      print("FAIL: is_daemon_running returned false, but daemon was just started and verified.")
      test_failed = true
      test_completed = true
      return
    end
    
    -- Stop daemon
    print("Stopping daemon...")
    code_savant.stop_daemon()
    
    -- Check that it is indeed stopped (after a brief delay)
    local uv = vim.uv or vim.loop
    local check_timer = uv.new_timer()
    uv.timer_start(check_timer, 300, 0, function()
      uv.close(check_timer)
      code_savant.is_daemon_running(function(still_running)
        if still_running then
          print("FAIL: Daemon is still running after stop_daemon was invoked.")
          test_failed = true
        else
          print("Test 5 Passed!")
          print("ALL TESTS PASSED SUCCESSFULLY!")
        end
        test_completed = true
      end)
    end)
  end)
end)

-- Wait for the asynchronous tests to complete with a 15-second timeout
vim.wait(15000, function() return test_completed end, 100)

if not test_completed then
  print("FAIL: Tests timed out after 15 seconds.")
  os.exit(1)
elseif test_failed then
  os.exit(1)
else
  os.exit(0)
end
