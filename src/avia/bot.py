"""Telegram-бот на long polling (только httpx, без лишних зависимостей)."""
from __future__ import annotations

import asyncio
from html import escape
import logging

import httpx

from .config import Settings, load_settings
from .formatting import format_report
from .guard import friendly_error
from .parsing import ParseError, parse_query
from .pipeline import search

log = logging.getLogger("avia.bot")
API = "https://api.telegram.org/bot{token}/{method}"

HELP = (
    "Пришлите команду:\n"
    "<code>/price Москва Алматы 10.12</code>\n"
    "<code>/price MOW TAS 2026-12-10 2</code> (2 пассажира)\n\n"
    "Города по-русски или кодом IATA."
)


class Bot:
    def __init__(self, settings: Settings):
        if not settings.tg_token:
            raise SystemExit("Задайте TG_BOT_TOKEN в .env")
        if not settings.tg_allowed_ids:
            raise SystemExit("Задайте TG_ALLOWED_IDS в .env (ваш Telegram id), иначе бот открыт всем")
        self.s = settings
        self.http = httpx.AsyncClient(timeout=70)
        self.busy: set[int] = set()

    async def call(self, method: str, **params):
        r = await self.http.post(API.format(token=self.s.tg_token, method=method), json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data}")
        return data["result"]

    async def send(self, chat_id: int, text: str):
        # лимит Telegram 4096 символов
        return await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text[:4000],
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def handle(self, msg: dict):
        chat_id = msg["chat"]["id"]
        user_id = msg.get("from", {}).get("id")
        if user_id not in self.s.tg_allowed_ids:
            return
        text = (msg.get("text") or "").strip()
        if text.startswith(("/start", "/help")):
            await self.send(chat_id, HELP)
            return
        if not text.startswith("/price"):
            await self.send(chat_id, HELP)
            return
        try:
            q = parse_query(text.removeprefix("/price"))
        except ParseError as e:
            await self.send(chat_id, f"⚠️ {e}")
            return
        if chat_id in self.busy:
            await self.send(chat_id, "Предыдущий поиск ещё идёт, подождите.")
            return
        self.busy.add(chat_id)
        try:
            await self.send(chat_id, f"Ищу {q.origin} → {q.destination} на {q.depart:%d.%m}, это займёт около минуты…")
            results = await search(q, self.s)
            await self.send(chat_id, format_report(q, results, self.s.top_n))
        except Exception as e:
            log.exception("search failed")
            await self.send(chat_id, f"⚠️ {escape(friendly_error(e))}")
        finally:
            self.busy.discard(chat_id)

    async def run(self):
        offset = 0
        log.info("bot started")
        while True:
            try:
                updates = await self.call("getUpdates", offset=offset, timeout=50)
            except Exception:
                log.exception("getUpdates")
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                if "message" in u:
                    asyncio.create_task(self.handle(u["message"]))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(Bot(load_settings()).run())


if __name__ == "__main__":
    main()
