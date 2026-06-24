#!/usr/bin/env bash
# dev_tools/launch_test.sh
# Runs physical Neovim preloaded with our pure local client configuration.

set -euo pipefail

# Resolve absolute directory of repository root
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Parse optional arguments
MOCK_ARG=""
PERF_DEBUG="false"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mock|-m)
      MOCK_ARG="mock"
      shift
      ;;
    --live|-l)
      MOCK_ARG="live"
      shift
      ;;
    --perf|-p)
      PERF_DEBUG="true"
      shift
      ;;
    *)
      echo "Unknown option: $1"
      echo "Usage: $0 [--mock|-m] [--live|-l] [--perf|-p]"
      exit 1
      ;;
  esac
done

# Launch Neovim
exec nvim -c "set rtp+=${PROJECT_ROOT}" \
          -c "lua require('code_savant').setup({
                socket_path = '/tmp/code_savant.sock',
                perf_debug = ${PERF_DEBUG}
              })" \
          -c "CodeSavantChat ${MOCK_ARG}"
