#!/usr/bin/env bash
# Запись в журнал бюджета телефонов: tools/blog.sh DEVICE SITE ROUTE [NOTE]
set -euo pipefail
printf '%s\tmcp_fetch_page\t%s\t%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$1" "$2" "$3" "${4:-phase2}" >> "$(dirname "$0")/../docs/audit/phone_budget.log"
