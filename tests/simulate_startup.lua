-- Simulate startup command sequence of CodeSavant
vim.opt.rtp:append(".")
local code_savant = require("code_savant")
code_savant.setup()

print("--- Simulating CodeSavantChat startup command ---")
vim.cmd("CodeSavantChat")

local wins = vim.api.nvim_list_wins()
print("Number of windows created: " .. tostring(#wins))
for i, win in ipairs(wins) do
  local buf = vim.api.nvim_win_get_buf(win)
  local ft = vim.bo[buf].filetype
  local bt = vim.bo[buf].buftype
  local width = vim.api.nvim_win_get_width(win)
  local height = vim.api.nvim_win_get_height(win)
  print(string.format("Window %d (ID %d): ft='%s', bt='%s', size=%dx%d", i, win, ft, bt, width, height))
end

os.exit(0)
