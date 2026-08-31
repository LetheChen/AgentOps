#!/usr/bin/env bash
# M0 orchestrator benchmark one-click runner (POSIX)
# Usage: ./bench/run_bench.sh [extra args to bench.runner]
set -euo pipefail
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$( cd "$SCRIPT_DIR/.." && pwd )"
cd "$PROJECT_ROOT"
exec python -m bench.runner "$@"

