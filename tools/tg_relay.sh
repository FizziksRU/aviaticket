#!/usr/bin/env bash
# Telegram-relay для автономного аудита. Запускает ВЛАДЕЛЕЦ (процесс владельца читает .env):
#   tools/tg_relay.sh start|stop|status|run
# Агент кладёт тексты в $AVIA_OUTBOX/pending/*.txt (tools/report.sh), relay отправляет их
# через tools/notify.sh. Сообщения владельца (TG_REPORT_CHAT_ID) пишутся в inbox.log;
# «стоп» создаёт файл STOP рядом с outbox. Токен нигде не печатается.
set -euo pipefail
cd "$(dirname "$0")/.."
BOX="${AVIA_OUTBOX:-$(cd .. && pwd)/outbox}"
mkdir -p "$BOX/pending" "$BOX/sent" "$BOX/failed"
POLL_TIMEOUT=20

cmd="${1:-status}"
case "$cmd" in
  start)
    if [ -f "$BOX/relay.pid" ] && kill -0 "$(cat "$BOX/relay.pid")" 2>/dev/null; then
      echo "relay уже работает (pid $(cat "$BOX/relay.pid"))"; exit 0
    fi
    nohup "$0" run >> "$BOX/relay.log" 2>&1 &
    echo $! > "$BOX/relay.pid"
    echo "relay запущен (pid $!), outbox: $BOX"; exit 0 ;;
  stop)
    [ -f "$BOX/relay.pid" ] && kill "$(cat "$BOX/relay.pid")" 2>/dev/null && echo "relay остановлен" || echo "relay не запущен"
    rm -f "$BOX/relay.pid"; exit 0 ;;
  status)
    if [ -f "$BOX/relay.pid" ] && kill -0 "$(cat "$BOX/relay.pid")" 2>/dev/null; then
      echo "running pid $(cat "$BOX/relay.pid"), alive $(cat "$BOX/relay.alive" 2>/dev/null)"
    else echo "stopped"; fi
    exit 0 ;;
  run) ;;
  *) echo "usage: $0 start|stop|status|run" >&2; exit 2 ;;
esac

CHAT=$(grep -E '^TG_REPORT_CHAT_ID=' .env | tail -1 | cut -d= -f2- | tr -d '\r"'"'")
ME=$(tools/tg.sh getMe || true)
printf '%s' "$ME" | python3 -I -c 'import sys,json
try:
    r = json.load(sys.stdin); print(json.dumps({"ok": r.get("ok", False), "username": r.get("result", {}).get("username", "")}))
except Exception: print(json.dumps({"ok": False}))' > "$BOX/relay_status.json"

OFF=$(cat "$BOX/.offset" 2>/dev/null || echo 0)
while true; do
  date -u +%FT%TZ > "$BOX/relay.alive"
  for f in $(ls "$BOX/pending/"*.txt 2>/dev/null | sort); do
    if tools/notify.sh "$(cat "$f")" > "$BOX/last_send.json" 2>&1; then
      mv "$f" "$BOX/sent/"
    else
      mv "$f" "$BOX/failed/"; cp "$BOX/last_send.json" "$BOX/failed/$(basename "$f").err"
    fi
  done
  RESP=$(tools/tg.sh getUpdates -d offset="$OFF" -d timeout="$POLL_TIMEOUT" || true)
  OFF=$(printf '%s' "$RESP" | CHAT="$CHAT" BOX="$BOX" OFF="$OFF" python3 -I -c '
import json, os, sys, time
box, chat, off = os.environ["BOX"], os.environ["CHAT"], int(os.environ["OFF"])
try:
    ups = json.load(sys.stdin).get("result", [])
except Exception:
    ups = []
for u in ups:
    off = max(off, u["update_id"] + 1)
    m = u.get("message") or {}
    if str(m.get("from", {}).get("id")) != chat:
        continue
    text = (m.get("text") or "").strip()
    with open(os.path.join(box, "inbox.log"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "text": text}, ensure_ascii=False) + "\n")
    if text.lower().lstrip("/") in ("стоп", "stop"):
        open(os.path.join(os.path.dirname(box), "STOP"), "w").write(time.strftime("%FT%TZ") + " from telegram\n")
print(off)')
  echo "$OFF" > "$BOX/.offset"
  [ -f "$(dirname "$BOX")/STOP" ] && [ ! -f "$BOX/.stop_acked" ] && {
    touch "$BOX/.stop_acked"; tools/notify.sh "avia-scale: получил «стоп», агент сохранит состояние и завершится." >/dev/null 2>&1 || true; }
  sleep 2
done
