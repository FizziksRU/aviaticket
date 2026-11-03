"""Мок Telegram Bot API для нагрузочного прогона: getUpdates, sendMessage, editMessageText,
answerCallbackQuery, deleteMessage, с лимитами Telegram и ответом 429 retry_after.

Лимиты (документация Bot API, FAQ «My bot is hitting limits»): ~30 сообщений в секунду на бота,
не больше ~1 сообщения в секунду в один чат (кратковременные всплески допускаются).
Здесь это два token bucket: глобальный 30/с (ёмкость 30) и на чат 1/с (ёмкость 3).
Также проверяется длина ≤4096 и парность HTML-тегов (400 «can't parse entities»).
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs

ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre",
                "blockquote", "tg-spoiler", "span", "tg-emoji"}


class _Bucket:
    def __init__(self, rate: float, capacity: float, now: float) -> None:
        self.rate, self.capacity, self.tokens, self.t = rate, capacity, capacity, now

    def take(self, now: float) -> float:
        """0 если можно отправить, иначе сколько ждать."""
        self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.rate)
        self.t = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / self.rate


class _TagChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.error = ""

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag not in ALLOWED_TAGS:
            self.error = self.error or f"unsupported start tag \"{tag}\""
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.error = self.error or f"unexpected end tag \"{tag}\""
        else:
            self.stack.pop()


def html_error(text: str) -> str:
    p = _TagChecker()
    p.feed(text)
    p.close()
    if p.error:
        return p.error
    if p.stack:
        return f"can't find end tag corresponding to start tag \"{p.stack[-1]}\""
    if re.search(r"&(?![a-zA-Z]+;|#\d+;|#x[0-9a-fA-F]+;)", text):
        return "unescaped &"
    if re.search(r"<(?![a-zA-Z/])", text):
        return "unescaped <"
    return ""


@dataclass
class Sent:
    t: float
    method: str
    chat_id: int
    message_id: int
    text: str
    reply_markup: dict | None


@dataclass
class MockTelegram:
    global_rate: float = 30.0
    chat_rate: float = 1.0
    chat_burst: float = 3.0
    updates: list[dict] = field(default_factory=list)
    next_update_id: int = 1
    next_message_id: int = 1000
    sent: dict[int, list[Sent]] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._cond: asyncio.Condition | None = None
        self._chat_cond: dict[int, asyncio.Condition] = {}
        self._global: _Bucket | None = None
        self._chats: dict[int, _Bucket] = {}

    # --- для виртуальных пользователей ---
    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    def cond(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    def chat_cond(self, chat_id: int) -> asyncio.Condition:
        if chat_id not in self._chat_cond:
            self._chat_cond[chat_id] = asyncio.Condition()
        return self._chat_cond[chat_id]

    async def push(self, update: dict) -> None:
        async with self.cond():
            update["update_id"] = self.next_update_id
            self.next_update_id += 1
            self.updates.append(update)
            self.cond().notify_all()

    async def user_text(self, chat_id: int, text: str) -> None:
        self.next_message_id += 1
        await self.push({"message": {
            "message_id": self.next_message_id, "date": int(self._now()),
            "from": {"id": chat_id, "is_bot": False, "first_name": f"u{chat_id}"},
            "chat": {"id": chat_id, "type": "private"}, "text": text}})

    async def user_press(self, chat_id: int, message_id: int, data: str) -> None:
        await self.push({"callback_query": {
            "id": f"cq{self.next_update_id}", "data": data,
            "from": {"id": chat_id, "is_bot": False, "first_name": f"u{chat_id}"},
            "message": {"message_id": message_id, "chat": {"id": chat_id, "type": "private"}}}})

    # --- Bot API ---
    def _limit(self, chat_id: int) -> float:
        now = self._now()
        if self._global is None:
            self._global = _Bucket(self.global_rate, self.global_rate, now)
        bucket = self._chats.setdefault(chat_id, _Bucket(self.chat_rate, self.chat_burst, now))
        wait = max(self._global.take(now), 0.0)
        if wait:
            return wait
        return bucket.take(now)

    async def _record(self, method: str, chat_id: int, message_id: int, text: str, markup: dict | None) -> None:
        self.sent.setdefault(chat_id, []).append(Sent(self._now(), method, chat_id, message_id, text, markup))
        c = self.chat_cond(chat_id)
        async with c:
            c.notify_all()

    def _too_many(self, wait: float) -> tuple[int, dict]:
        self.stats["429"] = self.stats.get("429", 0) + 1
        ra = max(1, math.ceil(wait))
        return 429, {"ok": False, "error_code": 429, "description": f"Too Many Requests: retry after {ra}",
                     "parameters": {"retry_after": ra}}

    def _check_text(self, params: dict) -> tuple[int, dict] | None:
        text = str(params.get("text", ""))
        if not text.strip():
            return 400, {"ok": False, "error_code": 400, "description": "Bad Request: message text is empty"}
        if len(text) > 4096:
            return 400, {"ok": False, "error_code": 400, "description": "Bad Request: message is too long"}
        if params.get("parse_mode") == "HTML":
            err = html_error(text)
            if err:
                return 400, {"ok": False, "error_code": 400,
                             "description": f"Bad Request: can't parse entities: {err}"}
        return None

    async def api(self, method: str, params: dict) -> tuple[int, dict]:
        self.stats[method] = self.stats.get(method, 0) + 1
        if method == "getUpdates":
            return 200, {"ok": True, "result": await self._get_updates(params)}
        if method in ("sendMessage", "editMessageText"):
            chat_id = int(params["chat_id"])
            bad = self._check_text(params)
            if bad:
                self.errors.append({"method": method, "chat_id": chat_id, "error": bad[1]["description"],
                                    "len": len(str(params.get("text", "")))})
                return bad
            wait = self._limit(chat_id)
            if wait:
                return self._too_many(wait)
            if method == "sendMessage":
                self.next_message_id += 1
                mid = self.next_message_id
            else:
                mid = int(params["message_id"])
            markup = params.get("reply_markup")
            if isinstance(markup, str):
                markup = json.loads(markup)
            await self._record(method, chat_id, mid, str(params["text"]), markup)
            return 200, {"ok": True, "result": {"message_id": mid, "chat": {"id": chat_id}, "text": params["text"]}}
        if method == "answerCallbackQuery":
            return 200, {"ok": True, "result": True}
        if method == "deleteMessage":
            await self._record(method, int(params["chat_id"]), int(params["message_id"]), "", None)
            return 200, {"ok": True, "result": True}
        return 404, {"ok": False, "error_code": 404, "description": "Not Found"}

    async def _get_updates(self, params: dict) -> list[dict]:
        offset = int(params.get("offset", 0) or 0)
        timeout = float(params.get("timeout", 0) or 0)
        cond = self.cond()
        async with cond:
            self.updates = [u for u in self.updates if u["update_id"] >= offset]
            if not self.updates and timeout > 0:
                try:
                    await asyncio.wait_for(cond.wait_for(lambda: bool(self.updates)), timeout)
                except asyncio.TimeoutError:
                    pass
            batch = self.updates[:100]
        return batch

    # --- ASGI ---
    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        method = scope["path"].rsplit("/", 1)[-1]
        ctype = dict(scope.get("headers") or []).get(b"content-type", b"")
        if body and b"json" in ctype:
            params = json.loads(body)
        else:
            params = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        status, data = await self.api(method, params)
        raw = json.dumps(data, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": raw})
