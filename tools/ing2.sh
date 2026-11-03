#!/usr/bin/env bash
# tools/ing2.sh TS_A SITE_A PHONE_A TS_B SITE_B PHONE_B [ROUTE] — разбор пары сохранённых ответов MCP fetch_page
set -euo pipefail
TR=/Users/a1/.claude/projects/-Users-a1/de8f9cd5-5a18-4c32-a241-c5b37d6592ed/tool-results/mcp-8141da4a-5e69-4cdf-bb4b-50b9d5c8ba53-fetch_page
R="${7:-near}"
"$(dirname "$0")/ingest.sh" "$TR-$1.txt" "$2" "$3" "$R" | cut -c1-230
"$(dirname "$0")/ingest.sh" "$TR-$4.txt" "$5" "$6" "$R" | cut -c1-230
