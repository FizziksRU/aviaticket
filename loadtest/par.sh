#!/usr/bin/env bash
# Параллельный прогон: EXTRA="--tag _x" JOBS=5 loadtest/par.sh "B C" "10 50 100" "2 4"
set -uo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src:. EXTRA="${EXTRA:-}"
unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy
for s in $1; do for u in $2; do for p in $3; do echo "$s $u $p"; done; done; done |
  xargs -P "${JOBS:-5}" -L 1 sh -c '.venv/bin/python -m loadtest.run --scenario "$0" --users "$1" --phones "$2" $EXTRA 2>/dev/null | grep -E "^[ABC]_u" | cut -c1-200'
