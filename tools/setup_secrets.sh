#!/usr/bin/env bash
# Интерактивная настройка секретов для avia-scale. Токены не печатаются и не уходят в историю shell.
set -euo pipefail
cd "$(dirname "$0")/.."
ENV=.env
touch "$ENV"; chmod 600 "$ENV"

setkv() { # setkv KEY VALUE  — заменить или добавить строку в .env
  local k="$1" v="$2"
  grep -v "^${k}=" "$ENV" > "$ENV.tmp" || true
  printf '%s=%s\n' "$k" "$v" >> "$ENV.tmp"
  mv "$ENV.tmp" "$ENV"; chmod 600 "$ENV"
}
api() { curl -sS --max-time 70 "https://api.telegram.org/bot${TOKEN}/$1" "${@:2}"; }

echo "== Telegram =="
echo "Нужен ОТДЕЛЬНЫЙ тестовый бот (@BotFather → /newbot), не боевой."
while true; do
  read -rsp "Вставь токен тестового бота (ввод скрыт): " TOKEN; echo
  ME=$(api getMe || true)
  if echo "$ME" | grep -q '"ok":true'; then
    echo "OK: $(echo "$ME" | python3 -c 'import sys,json;r=json.load(sys.stdin)["result"];print("@"+r["username"])')"
    break
  fi
  echo "Токен не принят Telegram, попробуй ещё раз."
done

WH=$(api getWebhookInfo | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"].get("url",""))')
if [ -n "$WH" ]; then
  echo "ВНИМАНИЕ: у бота стоит webhook ($WH). Похоже, он где-то используется. Возьми другой бот или сними webhook сам."
  exit 1
fi
PROBE=$(api getUpdates -d timeout=0 || true)
if echo "$PROBE" | grep -q '"error_code":409'; then
  echo "ВНИМАНИЕ: этот бот уже кто-то опрашивает (409 Conflict). Возьми отдельный тестовый бот."
  exit 1
fi

echo "Теперь открой бота в Telegram и отправь ему /start (жду до 3 минут)..."
CHAT=""
for _ in $(seq 1 6); do
  CHAT=$(api getUpdates -d timeout=30 | python3 -c '
import sys,json
r=json.load(sys.stdin).get("result",[])
ids=[u["message"]["from"]["id"] for u in r if "message" in u and u["message"].get("text","").startswith("/start")]
print(ids[-1] if ids else "")')
  [ -n "$CHAT" ] && break
done
[ -n "$CHAT" ] || { echo "Не увидел /start. Запусти скрипт ещё раз."; exit 1; }
LAST=$(api getUpdates | python3 -c 'import sys,json;r=json.load(sys.stdin)["result"];print(r[-1]["update_id"] if r else 0)')
api getUpdates -d offset=$((LAST+1)) -d timeout=0 >/dev/null   # подтверждаем прочитанное
setkv TG_BOT_TOKEN "$TOKEN"
setkv TG_ALLOWED_IDS "$CHAT"
setkv TG_REPORT_CHAT_ID "$CHAT"
api sendMessage -d chat_id="$CHAT" -d text="avia-scale: бот подключён, Claude Code начинает работу" >/dev/null
echo "Telegram готов, твой id: $CHAT"

echo
echo "== DroidHub (пул телефонов) =="
read -rp "Есть адрес DroidHub API, чтобы бот ходил в телефоны? [y/N] " A
if [[ "${A:-n}" =~ ^[Yy]$ ]]; then
  read -rp  "DROIDHUB_URL (например http://host:port): " DURL
  read -rsp "DROIDHUB_TOKEN (ввод скрыт): " DTOK; echo
  read -rp  "DROIDHUB_DEVICES через запятую [realme,redmi]: " DDEV
  setkv DROIDHUB_URL "$DURL"; setkv DROIDHUB_TOKEN "$DTOK"; setkv DROIDHUB_DEVICES "${DDEV:-realme,redmi}"
  read -rp "Телефоны одновременно обслуживают боевой бот? [Y/n] " S
  setkv PHONES_SHARED_WITH_PROD "$([[ "${S:-y}" =~ ^[Nn]$ ]] && echo 0 || echo 1)"
fi

read -rp "Travelpayouts TP_TOKEN (Enter, чтобы пропустить; ввод виден): " TPT
[ -n "${TPT:-}" ] && setkv TP_TOKEN "$TPT"

grep -q '^\.env$' .gitignore || echo ".env" >> .gitignore
grep -q '^\.secrets_ok$' .gitignore || echo ".secrets_ok" >> .gitignore
date -u +%FT%TZ > .secrets_ok
echo "Готово. Возвращайся в Claude Code, дальше он работает сам."
