#!/bin/bash
# Run the demo under the coz-mcp libcoz in manual mode (for manual testing;
# the MCP server starts the demo itself when COZ_MCP_WORKLOAD=demo).
#
# usage: scripts/run-demo.sh [build_dir] [demo args...]
set -euo pipefail

BUILD=${1:-$HOME/builds/coz-mcp-release}
shift || true
SOCK=${COZ_MCP_SOCKET:-/tmp/coz-mcp.sock}

exec env \
  LD_PRELOAD="$BUILD/coz/libcoz/libcoz.so" \
  COZ_BINARY_SCOPE=MAIN \
  COZ_SOURCE_SCOPE=% \
  COZ_OUTPUT=/tmp/coz-mcp-demo.coz \
  COZ_CONTROL_SOCKET="$SOCK" \
  "$BUILD/demo/demo" --threads=4 --time=3600 "$@"
