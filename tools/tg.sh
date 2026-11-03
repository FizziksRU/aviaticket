#!/usr/bin/env bash
# Вызов Telegram Bot API: tools/tg.sh METHOD [-d key=value ...]
# Токен берётся из .env (TG_BOT_TOKEN), передаётся curl через stdin-конфиг
# (не виден в ps) и вырезается из любого вывода, включая ошибки.
set -euo pipefail
cd "$(dirname "$0")/.."
METHOD="${1:?usage: tools/tg.sh METHOD [curl args...]}"; shift
[[ "$METHOD" =~ ^[A-Za-z]+$ ]] || { echo "tg.sh: bad method name" >&2; exit 2; }
[ -f .env ] || { echo "tg.sh: .env not found (run tools/setup_secrets.sh)" >&2; exit 2; }
TOKEN=$(grep -E '^TG_BOT_TOKEN=' .env | tail -1 | cut -d= -f2- | tr -d '\r"'"'" || true)
[ -n "$TOKEN" ] || { echo "tg.sh: TG_BOT_TOKEN missing in .env" >&2; exit 2; }
mask() { local t="$TOKEN"; sed "s#${t//#/\\#}#<TOKEN>#g"; }
set +e
OUT=$(printf 'url = "https://api.telegram.org/bot%s/%s"\n' "$TOKEN" "$METHOD" \
  | curl -sS --max-time 70 -K - "$@" 2>&1)
RC=$?
set -e
printf '%s\n' "$OUT" | mask
exit $RC
