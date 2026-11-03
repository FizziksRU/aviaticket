#!/usr/bin/env bash
# Эксперименты с отключением ограничителей на текущем коде (профиль v2): loadtest/isolate.sh USERS PHONES
set -uo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src:.
unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy
U="$1"; P="$2"
printf '%s\n' "--max-parallel 100 --tag _v2nosem" "--sites-auto --tag _v2auto" "--ideal --tag _v2ideal" "--ideal --max-parallel 100 --tag _v2idealnosem" |
  xargs -P "${JOBS:-2}" -I{} sh -c ".venv/bin/python -m loadtest.run --scenario A --users $U --phones $P {} 2>/dev/null | grep -E '^A_u' | cut -c1-120"
