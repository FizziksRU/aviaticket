#!/usr/bin/env bash
# Отправка отчёта в TG_REPORT_CHAT_ID: tools/notify.sh "текст"
# HTML-экранирование, нарезка по 4000 символов (по строкам, где можно),
# повтор при 429 с учётом retry_after. Печатает ответ API (без токена).
set -euo pipefail
cd "$(dirname "$0")/.."
TEXT="${1:?usage: tools/notify.sh \"text\"}"
CHAT=$(grep -E '^TG_REPORT_CHAT_ID=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r"'"'" || true)
[ -n "$CHAT" ] || { echo "notify.sh: TG_REPORT_CHAT_ID missing in .env" >&2; exit 2; }
MAX_TRIES=5

split_chunks() { # stdin: raw text → stdout: NUL-separated HTML-escaped chunks ≤4000
  python3 -I -c '
import html, sys
LIMIT = 4000
text = sys.stdin.read()
chunks, cur = [], ""
for line in text.splitlines(keepends=True):
    esc = html.escape(line, quote=False)
    while len(esc) > LIMIT:  # одна длинная строка: режем по символам, не ломая сущности
        part, n = "", 0
        for ch in line:
            e = html.escape(ch, quote=False)
            if len(part) + len(e) > LIMIT:
                break
            part += e; n += 1
        if cur:
            chunks.append(cur); cur = ""
        chunks.append(part); line = line[n:]; esc = html.escape(line, quote=False)
    if len(cur) + len(esc) > LIMIT:
        chunks.append(cur); cur = ""
    cur += esc
if cur.strip():
    chunks.append(cur)
sys.stdout.write("\0".join(chunks) + ("\0" if chunks else ""))
'
}

rc_all=0
while IFS= read -r -d '' CHUNK; do
  for try in $(seq 1 "$MAX_TRIES"); do
    RESP=$(tools/tg.sh sendMessage -d chat_id="$CHAT" -d parse_mode=HTML \
      -d disable_web_page_preview=true --data-urlencode text="$CHUNK" || true)
    if printf '%s' "$RESP" | grep -q '"ok":true'; then
      echo '{"ok":true}'; break
    fi
    RETRY=$(printf '%s' "$RESP" | python3 -I -c 'import sys,json
try: print(int(json.load(sys.stdin).get("parameters",{}).get("retry_after",0)))
except Exception: print(0)')
    if [ "$RETRY" -gt 0 ] && [ "$try" -lt "$MAX_TRIES" ]; then
      sleep $((RETRY + 1)); continue
    fi
    echo "$RESP" >&2; rc_all=1; break
  done
done < <(printf '%s' "$TEXT" | split_chunks)
exit $rc_all
