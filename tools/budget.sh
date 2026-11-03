#!/usr/bin/env bash
# Сколько реальных fetch_page к телефонам сделано за последний час (по docs/audit/phone_budget.log).
#   tools/budget.sh [limit]   → "used=N limit=L free=F"
set -euo pipefail
LOG="$(dirname "$0")/../docs/audit/phone_budget.log"
LIMIT="${1:-40}"
python3 -I - "$LOG" "$LIMIT" <<'PY'
import sys, datetime as dt
log, limit = sys.argv[1], int(sys.argv[2])
now = dt.datetime.now(dt.timezone.utc)
used = 0
for line in open(log, encoding="utf-8").read().splitlines()[1:]:
    parts = line.split("\t")
    if len(parts) < 2 or parts[1] not in ("mcp_fetch_page", "gateway_fetch_page"):
        continue
    ts = dt.datetime.fromisoformat(parts[0].replace("Z", "+00:00"))
    if (now - ts).total_seconds() < 3600:
        used += 1
print(f"used={used} limit={limit} free={max(0, limit - used)}")
PY
