-- Standalone integration and unit test suite for Neovim Libuv network implementation
-- Run using: nvim --headless -l tests/test_client_network.lua

package.path = package.path .. ";./lua/?.lua;./lua/?/init.lua"
local uv = vim.uv or vim.loop
local Network = require("code_savant.network")


print("=========================================")
print("STARTING NEVIM CLIENT NETWORK TEST SUITE")
print("=========================================")

local socket_path = "./tests/test_client_network.sock"
os.remove(socket_path) -- Clean any previous test run leftovers

-- 1. Initialize Mock Unix Domain Socket Server using Libuv
local server = uv.new_pipe(false)
local bind_ok, bind_err = uv.pipe_bind(server, socket_path)
if not bind_ok then
  print("Failed to bind mock server: " .. tostring(bind_err))
  os.exit(1)
end

local server_received_data = ""
local server_client_pipe = nil

local listen_ok, listen_err = uv.listen(server, 128, function(err)
  if err then
    print("Mock server listen error: " .. tostring(err))
    return
  end
  local client = uv.new_pipe(false)
  uv.accept(server, client)
  server_client_pipe = client
  
  -- Read data sent from client to server
  uv.read_start(client, function(read_err, chunk)
    if read_err then
      print("Mock server read error: " .. tostring(read_err))
      return
    end
    if chunk then
      server_received_data = server_received_data .. chunk
    end
  end)
end)

if not listen_ok then
  print("Failed to listen on mock server: " .. tostring(listen_err))
  os.exit(1)
end

print("[INFO] Mock server successfully listening on " .. socket_path)

-- Helper function to tick / wait for async condition with a timeout
local function wait_for_condition(timeout, condition_func, desc)
  local passed, status = vim.wait(timeout, condition_func, 10)
  if not passed then
    print("[ERROR] Timeout waiting for: " .. desc)
    os.exit(1)
  end
end

---------------------------------------------------------
-- TEST 1: Parameter Validation (Guard Clauses)
---------------------------------------------------------
print("\n[TEST 1] Verifying parameter validation & guard clauses...")
local ok, err = pcall(Network.connect, "", 1)
assert(not ok, "connect should reject empty socket path")
assert(string.find(err, "socket_path"), "connect error should mention socket_path")

ok, err = pcall(Network.connect, socket_path, -1)
assert(not ok, "connect should reject negative bufnr")
assert(string.find(err, "bufnr"), "connect error should mention bufnr")

ok, err = pcall(Network.send_prompt, nil, "valid prompt")
assert(not ok, "send_prompt should reject nil pipe")

ok, err = pcall(Network.send_prompt, {}, "")
assert(not ok, "send_prompt should reject empty prompt")
print("[SUCCESS] Test 1 passed.")

---------------------------------------------------------
-- TEST 2: Active Connection & Asynchronous Establishment
---------------------------------------------------------
print("\n[TEST 2] Connecting client socket asynchronously...")
local bufnr = 42
local conn = Network.connect(socket_path, bufnr)
assert(conn, "connect should return a connection state table")
assert(conn.pipe, "connection table must hold the pipe handle")
assert(conn.bufnr == bufnr, "connection bufnr must match")
assert(conn.accumulator == "", "connection accumulator must start empty")

-- Wait until the mock server accepts the connection
wait_for_condition(2000, function() return server_client_pipe ~= nil end, "mock server accept connection")
print("[SUCCESS] Test 2 passed.")

---------------------------------------------------------
-- TEST 3: Connection Idempotency Check
---------------------------------------------------------
print("\n[TEST 3] Verifying connection idempotency...")
local conn_redundant = Network.connect(socket_path, bufnr)
assert(conn == conn_redundant, "connect must return the same active connection without creating a new socket descriptor")
print("[SUCCESS] Test 3 passed.")

---------------------------------------------------------
-- TEST 4: Asynchronous send_prompt and JSON-RPC Framing
---------------------------------------------------------
print("\n[TEST 4] Verifying prompt serialization & transmission...")
conn.session_id = "sess-test" -- Simulate completed start handshake
local prompt_msg = "Please optimize my binary search tree implementation."
Network.send_prompt(conn, prompt_msg)

-- Wait for mock server to receive full newline delimited frame
wait_for_condition(2000, function()
  return string.find(server_received_data, "\n") ~= nil
end, "server to receive trailing newline delimited frame")

local received_lines = vim.split(server_received_data, "\n", { plain = true })
local decoded_req = nil
for _, line in ipairs(received_lines) do
  if line ~= "" then
    local ok, parsed = pcall(vim.json.decode, line)
    if ok and parsed.method == "session/send_prompt" then
      decoded_req = parsed
      break
    end
  end
end

assert(decoded_req, "Could not find session/send_prompt request in received data")
assert(decoded_req.jsonrpc == "2.0", "JSON-RPC request must specify version '2.0'")
assert(decoded_req.method == "session/send_prompt", "JSON-RPC method must be session/send_prompt")
assert(decoded_req.params.text == prompt_msg, "JSON-RPC param text must match prompt_msg")
assert(type(decoded_req.id) == "number", "JSON-RPC request must contain a numeric id")
print("[SUCCESS] Test 4 passed.")

---------------------------------------------------------
-- TEST 5: TCP Packet Fragmentation & Accumulator Reassembly
---------------------------------------------------------
print("\n[TEST 5] Verifying resilience to TCP packet fragmentation...")
local received_frames = {}
Network.add_listener(bufnr, function(msg)
  table.insert(received_frames, msg)
end)

-- Send a fragmented JSON-RPC frame across multiple server writes
local frag1 = '{"jsonrpc":"2.0",'
local frag2 = '"result":{"session_id":"sess-999","status":"active"},'
local frag3 = '"id":1}\n'

-- Write first fragment
uv.write(server_client_pipe, frag1)
vim.wait(50, function() return false end) -- Tick loop
assert(#received_frames == 0, "No frames should be dispatched on partial segment 1")

-- Write second fragment
uv.write(server_client_pipe, frag2)
vim.wait(50, function() return false end) -- Tick loop
assert(#received_frames == 0, "No frames should be dispatched on partial segment 2")

-- Write third fragment with newline delimiter
uv.write(server_client_pipe, frag3)

-- Wait for client dispatcher to process complete frame
wait_for_condition(2000, function() return #received_frames == 1 end, "reassembling fragmented frame")
local msg = received_frames[1]
assert(msg.jsonrpc == "2.0", "decrypted JSON-RPC version must match")
assert(msg.result.session_id == "sess-999", "session_id should match payload")
assert(msg.result.status == "active", "status should match payload")

-- Confirm that connection state updated with the received session_id
assert(conn.session_id == "sess-999", "connection state should save session_id")
print("[SUCCESS] Test 5 passed.")

---------------------------------------------------------
-- TEST 6: Coalesced Multiple Packets in Single Read
---------------------------------------------------------
print("\n[TEST 6] Verifying coalesced packets in a single TCP chunk...")
local coalesced_payload = '{"jsonrpc":"2.0","id":200}\n{"jsonrpc":"2.0","id":201}\n'
uv.write(server_client_pipe, coalesced_payload)

wait_for_condition(2000, function() return #received_frames == 3 end, "receiving coalesced frames")
assert(received_frames[2].id == 200, "second parsed frame ID must match")
assert(received_frames[3].id == 201, "third parsed frame ID must match")
print("[SUCCESS] Test 6 passed.")

---------------------------------------------------------
-- TEST 7: Safe Error / Malformed JSON Resilience
---------------------------------------------------------
print("\n[TEST 7] Verifying resilience to syntax error / malformed frames...")
local malformed_payload = '{"invalid_json": }\n'
uv.write(server_client_pipe, malformed_payload)

-- Wait a short period to let reader process it. Loop should not crash.
vim.wait(100, function() return false end)
print("[SUCCESS] Test 7 passed (did not crash).")

---------------------------------------------------------
-- TEST 8: Connection EOF and Resource Teardown
---------------------------------------------------------
print("\n[TEST 8] Verifying resource cleanup on EOF...")
uv.close(server_client_pipe)

wait_for_condition(2000, function()
  return Network.get_connection(bufnr) == nil
end, "connection state cleanup on server disconnect")

assert(Network.get_connection(bufnr) == nil, "connection must be deregistered from registry")
print("[SUCCESS] Test 8 passed.")

---------------------------------------------------------
-- TEST 9: Async Network.list_sessions
---------------------------------------------------------
print("\n[TEST 9] Verifying Network.list_sessions asynchronous lookup...")

-- Rebind server to accept a new connection for list_sessions
local list_server_pipe = nil
local server_received_list_data = ""

uv.close(server) -- Close previous server to bind again on a clean state
server = uv.new_pipe(false)
os.remove(socket_path)
local list_bind_ok, list_bind_err = uv.pipe_bind(server, socket_path)
if not list_bind_ok then
  print("Failed to re-bind mock server for Test 9: " .. tostring(list_bind_err))
  os.exit(1)
end

local list_listen_ok, list_listen_err = uv.listen(server, 128, function(err)
  if err then return end
  local client = uv.new_pipe(false)
  uv.accept(server, client)
  list_server_pipe = client
  
  uv.read_start(client, function(read_err, chunk)
    if chunk then
      server_received_list_data = server_received_list_data .. chunk
      -- Respond with session list
      local response_payload = {
        jsonrpc = "2.0",
        result = {
          sessions = {
            {
              session_id = "sess-1234",
              metadata = { name = "Mock Saved Session", created_at = "2026-06-19T12:00:00" },
              turn_count = 5
            }
          }
        },
        id = 9999
      }
      uv.write(client, vim.json.encode(response_payload) .. "\n")
    end
  end)
end)

if not list_listen_ok then
  print("Failed to re-listen on mock server for Test 9: " .. tostring(list_listen_err))
  os.exit(1)
end

local received_sessions = nil
local list_error = nil

Network.list_sessions(socket_path, "/mock/workspace", function(sessions, err)
  received_sessions = sessions
  list_error = err
end)

wait_for_condition(2000, function()
  return received_sessions ~= nil or list_error ~= nil
end, "Network.list_sessions response callback")

assert(list_error == nil, "list_sessions should not raise an error: " .. tostring(list_error))
assert(received_sessions, "list_sessions should return sessions")
assert(#received_sessions == 1, "should contain exactly one session")
assert(received_sessions[1].session_id == "sess-1234", "session_id should match")
assert(received_sessions[1].metadata.name == "Mock Saved Session", "name should match")
assert(received_sessions[1].turn_count == 5, "turn_count should match")

pcall(function() uv.close(list_server_pipe) end)
print("[SUCCESS] Test 9 passed.")

---------------------------------------------------------
-- TEARDOWN & EXIT
---------------------------------------------------------
print("\n[TEST TEARDOWN] Cleaning up resources...")
uv.close(server)
os.remove(socket_path)

print("\n=========================================")
print("ALL TESTS COMPLETED SUCCESSFULLY!")
print("=========================================")
os.exit(0)
