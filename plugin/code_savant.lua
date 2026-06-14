if vim.fn.exists("g:loaded_code_savant") == 1 then
  return
end
vim.g.loaded_code_savant = 1

-- Initialize plugin with default settings if not already loaded
local has_code_savant, code_savant = pcall(require, "code_savant")
if has_code_savant then
  code_savant.setup()
end
