#!/usr/bin/env bash
# tools/ingest.sh FILE SITE PHONE ROUTE — разбор ответа MCP fetch_page кодом бота (см. tools/phase2.py)
set -euo pipefail
cd "$(dirname "$0")/.."
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy \
  PYTHONPATH=src .venv/bin/python tools/phase2.py ingest "$@" 2>&1 | tail -1
