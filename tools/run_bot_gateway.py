"""Запуск бота из рабочего дерева через локальный шлюз секретов (tools/tg_gateway.py владельца).

    PYTHONPATH=src .venv/bin/python tools/run_bot_gateway.py [--phones]

Токенов здесь нет: Telegram и DroidHub идут на 127.0.0.1:8787, шлюз подставляет секреты сам.
Без --phones DroidHub не настраивается, и сайты собираются desktop-Playwright (как в CLI).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

GATEWAY = os.environ.get("AVIA_GATEWAY", "http://127.0.0.1:8787")


def main() -> None:
    with urllib.request.urlopen(f"{GATEWAY}/meta", timeout=5) as resp:
        meta = json.load(resp)
    if not meta.get("owner_id"):
        raise SystemExit("шлюз не знает id владельца (TG_REPORT_CHAT_ID)")
    for var in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        os.environ.pop(var, None)
    os.environ.update({"TG_BOT_TOKEN": "via-gateway", "TG_ALLOWED_IDS": str(meta["owner_id"]), "TG_OPEN": "0"})
    if "--phones" in sys.argv:
        if not meta.get("droidhub"):
            raise SystemExit("в .env владельца нет DROIDHUB_URL: перезапустите setup_secrets.sh и ответьте y про DroidHub")
        os.environ.update({"DROIDHUB_URL": f"{GATEWAY}/droidhub", "DROIDHUB_TOKEN": "via-gateway",
                           "DROIDHUB_DEVICES": os.environ.get("DROIDHUB_DEVICES", "realme,redmi")})
    import avia.bot as bot

    bot.API = GATEWAY + "/bot{token}/{method}"
    bot.main()


if __name__ == "__main__":
    main()
