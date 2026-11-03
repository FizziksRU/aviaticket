#!/usr/bin/env bash
# Прогон матрицы сценариев на текущем коде: loadtest/matrix.sh [доп. флаги run.py]
set -uo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src:.
unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy
SCEN="${SCEN:-A}"; USERS="${USERS:-10 50 100}"; PHONES="${PHONES:-2 4 8 16}"
for u in $USERS; do for p in $PHONES; do
  .venv/bin/python -m loadtest.run --scenario "$SCEN" --users "$u" --phones "$p" "$@" 2>&1 | tail -1 | cut -c1-400
done; done
