"""Telegram-бот на long polling (только httpx, без лишних зависимостей)."""
from __future__ import annotations

import asyncio
from html import escape
import logging

import httpx

from .config import Settings, load_settings
from .dialog import Dialog, HELP, Reply, SearchRequest, Step, to_reply_markup
from .formatting import build_report, format_details
from .guard import friendly_error
from .offers import assign_ids, filter_by_baggage
from .pipeline import search
from .store import ResultStore

log = logging.getLogger("avia.bot")
API = "https://api.telegram.org/bot{token}/{method}"


class Bot:
    def __init__(
        self,
        settings: Settings,
        store: ResultStore | None = None,
        dialog: Dialog | None = None,
    ):
        if not settings.tg_token:
            raise SystemExit("Задайте TG_BOT_TOKEN в .env")
        if not settings.tg_allowed_ids:
            raise SystemExit("Задайте TG_ALLOWED_IDS в .env (ваш Telegram id), иначе бот открыт всем")
        self.s = settings
        self.http = httpx.AsyncClient(timeout=70)
        self.busy: set[int] = set()
        if dialog is not None:
            self.dialog = dialog
            self.store = store if store is not None else dialog.store
        else:
            self.store = store if store is not None else ResultStore()
            self.dialog = Dialog(store=self.store)

    async def call(self, method: str, **params):
        payload = {k: v for k, v in params.items() if v is not None}
        r = await self.http.post(API.format(token=self.s.tg_token, method=method), json=payload)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data}")
        return data["result"]

    async def send(self, chat_id: int, text: str, reply_markup: dict | None = None):
        if len(text) <= 4096:
            params: dict = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                params["reply_markup"] = reply_markup
            return await self.call("sendMessage", **params)

        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in text.splitlines(keepends=True):
            if current_len + len(line) > 4096:
                if current:
                    chunks.append("".join(current))
                    current = []
                    current_len = 0
                while len(line) > 4096:
                    chunks.append(line[:4096])
                    line = line[4096:]
            current.append(line)
            current_len += len(line)
        if current:
            chunks.append("".join(current))

        last_res = None
        for i, chunk in enumerate(chunks):
            params = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if i == len(chunks) - 1 and reply_markup is not None:
                params["reply_markup"] = reply_markup
            last_res = await self.call("sendMessage", **params)
        return last_res

    async def _process_actions(
        self,
        chat_id: int,
        actions: list[Reply | SearchRequest],
        message_id: int | None = None,
    ):
        for item in actions:
            if isinstance(item, Reply):
                reply_markup = to_reply_markup(item.keyboard)
                if item.edit and message_id is not None:
                    try:
                        await self.call(
                            "editMessageText",
                            chat_id=chat_id,
                            message_id=message_id,
                            text=item.text,
                            parse_mode="HTML",
                            reply_markup=reply_markup,
                        )
                    except Exception:
                        await self.send(chat_id, item.text, reply_markup=reply_markup)
                else:
                    if item.text:
                        await self.send(chat_id, item.text, reply_markup=reply_markup)
            elif isinstance(item, SearchRequest):
                await self._run_search(chat_id, item)

    async def _run_search(self, chat_id: int, req: SearchRequest):
        if chat_id in self.busy:
            await self.send(chat_id, "Предыдущий поиск ещё идёт, подождите.")
            return
        self.busy.add(chat_id)
        q = req.query
        baggage_mode = req.baggage_mode
        wait_msg_id = None
        try:
            wait_msg = await self.send(
                chat_id,
                f"Ищу {q.origin} → {q.destination} на {q.depart:%d.%m}, это займёт около минуты…",
            )
            if isinstance(wait_msg, dict):
                wait_msg_id = wait_msg.get("message_id")

            results = await search(q, self.s)
            results = assign_ids(results)
            filtered, dropped = filter_by_baggage(results, baggage_mode)
            chunks, flat = build_report(
                q,
                filtered,
                self.s.top_n,
                baggage_mode=baggage_mode,
                dropped=dropped,
            )
            for chunk in chunks:
                await self.send(chat_id, chunk)
            self.store.put(chat_id, q, filtered, baggage_mode, flat)
            session = self.dialog.sessions.get(chat_id)
            if session:
                session.step = Step.DONE
        except Exception as e:
            log.exception("search failed")
            await self.send(chat_id, f"⚠️ {escape(friendly_error(e))}")
        finally:
            self.busy.discard(chat_id)
            if wait_msg_id is not None:
                try:
                    await self.call("deleteMessage", chat_id=chat_id, message_id=wait_msg_id)
                except Exception:
                    try:
                        await self.call("editMessageText", chat_id=chat_id, message_id=wait_msg_id, text="Готово")
                    except Exception:
                        pass

    async def handle(self, msg: dict):
        chat_id = msg["chat"]["id"]
        user_id = msg.get("from", {}).get("id")
        if user_id not in self.s.tg_allowed_ids:
            return
        text = (msg.get("text") or "").strip()
        actions = self.dialog.on_text(chat_id, text)
        await self._process_actions(chat_id, actions)

    async def handle_callback(self, cq: dict):
        user_id = cq.get("from", {}).get("id")
        if user_id not in self.s.tg_allowed_ids:
            return
        cq_id = cq["id"]
        msg = cq.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        if chat_id is None:
            chat_id = user_id
        message_id = msg.get("message_id")
        data = cq.get("data", "")

        answered = False
        try:
            actions = self.dialog.on_callback(chat_id, data)
            answer_text = None
            for item in actions:
                if isinstance(item, Reply) and item.answer:
                    answer_text = item.answer
                    break

            if answer_text:
                await self.call("answerCallbackQuery", callback_query_id=cq_id, text=answer_text)
            else:
                await self.call("answerCallbackQuery", callback_query_id=cq_id)
            answered = True

            await self._process_actions(chat_id, actions, message_id=message_id)
        finally:
            if not answered:
                try:
                    await self.call("answerCallbackQuery", callback_query_id=cq_id)
                except Exception:
                    pass

    async def run(self):
        offset = 0
        log.info("bot started")
        while True:
            try:
                updates = await self.call(
                    "getUpdates",
                    offset=offset,
                    timeout=50,
                    allowed_updates=["message", "callback_query"],
                )
            except Exception:
                log.exception("getUpdates")
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                if "message" in u:
                    asyncio.create_task(self.handle(u["message"]))
                elif "callback_query" in u:
                    asyncio.create_task(self.handle_callback(u["callback_query"]))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(Bot(load_settings()).run())


if __name__ == "__main__":
    main()
