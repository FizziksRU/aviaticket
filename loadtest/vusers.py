"""Виртуальные пользователи: проходят настоящий диалог бота кнопками и текстом, как человек.

Путь поиска: /start → «🔎 Найти билет» → «Москва» → «Санкт-Петербург» → дата →
«Только туда»/«Туда и обратно» (+ дата возврата) → «Не важно» (багаж) → ждать выдачу →
по желанию «🔔 Отслеживать цену».
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from .mock_telegram import MockTelegram, Sent

RESULT_MARKERS = ("track:",)
REFUSAL_TEXTS = ("Предыдущий поиск ещё идёт", "лимит поисков исчерпан", "Поиск ещё идёт")


@dataclass
class SearchRecord:
    chat_id: int
    route: tuple[str, str | None]
    t_click: float
    t_result: float | None = None
    outcome: str = "pending"   # ok | refused | error | timeout
    step_latencies: list[float] = field(default_factory=list)
    tracked: bool = False
    text_head: str = ""


def _buttons(msg: Sent) -> list[str]:
    rows = (msg.reply_markup or {}).get("inline_keyboard", [])
    return [b.get("callback_data", "") for row in rows for b in row]


class VirtualUser:
    def __init__(self, tg: MockTelegram, chat_id: int, rng: random.Random, today: date,
                 think: tuple[float, float] = (2.0, 6.0), patience: float = 3600.0) -> None:
        self.tg, self.chat_id, self.rng, self.today = tg, chat_id, rng, today
        self.think, self.patience = think, patience
        self.seen = 0

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    async def _pause(self) -> None:
        await asyncio.sleep(self.rng.uniform(*self.think))

    async def _wait(self, pred: Callable[[Sent], bool], timeout: float) -> Sent | None:
        cond = self.tg.chat_cond(self.chat_id)
        deadline = self._now() + timeout
        async with cond:
            while True:
                msgs = self.tg.sent.get(self.chat_id, [])
                while self.seen < len(msgs):
                    m = msgs[self.seen]
                    self.seen += 1
                    if m.method != "deleteMessage" and pred(m):
                        return m
                left = deadline - self._now()
                if left <= 0:
                    return None
                try:
                    await asyncio.wait_for(cond.wait(), left)
                except asyncio.TimeoutError:
                    return None

    async def _step(self, rec: SearchRecord, action: Callable[[], object], pred: Callable[[Sent], bool],
                    timeout: float = 120.0) -> Sent | None:
        t0 = self._now()
        await action()  # type: ignore[misc]
        msg = await self._wait(pred, timeout)
        if msg is not None:
            rec.step_latencies.append(msg.t - t0)
        return msg

    async def search(self, depart: date, ret: date | None, track: bool) -> SearchRecord:
        rec = SearchRecord(self.chat_id, (depart.isoformat(), ret.isoformat() if ret else None), t_click=0.0)
        cid = self.chat_id
        menu = await self._step(rec, lambda: self.tg.user_text(cid, "/start"), lambda m: "Главное меню" in m.text)
        if menu is None:
            rec.outcome = "error"
            rec.text_head = "нет ответа на /start"
            return rec
        await self._pause()
        q = await self._step(rec, lambda: self.tg.user_press(cid, menu.message_id, "menu:go"), lambda m: "Откуда" in m.text)
        await self._pause()
        await self._step(rec, lambda: self.tg.user_text(cid, "Москва"), lambda m: "Куда" in m.text)
        await self._pause()
        await self._step(rec, lambda: self.tg.user_text(cid, "Санкт-Петербург"), lambda m: "Дата вылета" in m.text)
        await self._pause()
        trip = await self._step(rec, lambda: self.tg.user_text(cid, depart.strftime("%d.%m.%Y")),
                                lambda m: "обратный билет" in m.text)
        if trip is None or q is None:
            rec.outcome = "error"
            rec.text_head = "диалог сломался до выбора маршрута"
            return rec
        await self._pause()
        trip_btn = next(b for b in _buttons(trip) if b.startswith("trip:") and b.endswith(":rt" if ret else ":one"))
        if ret:
            await self._step(rec, lambda: self.tg.user_press(cid, trip.message_id, trip_btn),
                             lambda m: "Дата возвращения" in m.text)
            await self._pause()
            bag = await self._step(rec, lambda: self.tg.user_text(cid, ret.strftime("%d.%m.%Y")), lambda m: m.text == "Багаж?")
        else:
            bag = await self._step(rec, lambda: self.tg.user_press(cid, trip.message_id, trip_btn), lambda m: m.text == "Багаж?")
        if bag is None:
            rec.outcome = "error"
            rec.text_head = "нет шага «Багаж?»"
            return rec
        await self._pause()
        bag_btn = next(b for b in _buttons(bag) if b.startswith("bag:") and b.endswith(":any"))
        rec.t_click = self._now()
        await self.tg.user_press(cid, bag.message_id, bag_btn)

        def is_final(m: Sent) -> bool:
            return (any(b.startswith(RESULT_MARKERS) for b in _buttons(m))
                    or m.text.startswith("⚠️") or any(t in m.text for t in REFUSAL_TEXTS))

        res = await self._wait(is_final, self.patience)
        if res is None:
            rec.outcome = "timeout"
            return rec
        rec.t_result = res.t
        rec.text_head = res.text[:80]
        if any(t in res.text for t in REFUSAL_TEXTS):
            rec.outcome = "refused"
        elif res.text.startswith("⚠️"):
            rec.outcome = "error"
        else:
            rec.outcome = "ok"
            if track:
                track_btn = next(b for b in _buttons(res) if b.startswith("track:"))
                await self._pause()
                ack = await self._step(rec, lambda: self.tg.user_press(cid, res.message_id, track_btn),
                                       lambda m: "отслеж" in m.text.lower() or "лимит" in m.text.lower())
                rec.tracked = ack is not None and "лимит" not in ack.text.lower()
        return rec


class RouteChooser:
    """Маршрут = (дата вылета, дата возврата|None) на одном направлении MOW→LED.

    Популярность дат по Зипфу: несколько «горячих» дат собирают большую часть поисков,
    что даёт реалистичный hit-rate кэша и схлопывания одинаковых запросов.
    """

    def __init__(self, rng: random.Random, today: date, n_dates: int = 45, zipf_s: float = 1.1,
                 rt_share: float = 0.2) -> None:
        self.rng, self.today, self.rt_share = rng, today, rt_share
        self.dates = [today + timedelta(days=3 + i) for i in range(n_dates)]
        rng.shuffle(self.dates)
        w = [1 / (k + 1) ** zipf_s for k in range(n_dates)]
        total = sum(w)
        self.weights = [x / total for x in w]

    def pick(self) -> tuple[date, date | None]:
        d = self.rng.choices(self.dates, self.weights)[0]
        if self.rng.random() < self.rt_share:
            return d, d + timedelta(days=self.rng.choice([3, 5, 7, 10]))
        return d, None
