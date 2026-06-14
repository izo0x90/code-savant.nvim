#!/usr/bin/env bash
# dev_tools/launch_test.sh
# Runs physical Neovim preloaded with our pure local client configuration.

set -euo pipefail

# Resolve absolute directory of repository root
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Launch Neovim
exec nvim -c "set rtp+=${PROJECT_ROOT}" \
          -c "lua require('code_savant').setup({
                socket_path = '/tmp/code_savant.sock',
                keymaps = {
                  expand = '<CR>',
                  submit = '<CR>'
                }
              })" \
          -c "CodeSavantChat"
