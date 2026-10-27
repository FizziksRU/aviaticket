"""Хранилище результатов поиска для диалогового бота в памяти."""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable

from .models import Offer, Query, SiteResult


@dataclass
class SearchSnapshot:
    query: Query
    results: list[SiteResult]
    baggage_mode: str
    created_at: float
    flat: list[Offer]

    def offer_by_number(self, n: int) -> Offer | None:
        """Возвращает предложение по сквозному номеру с 1, либо None если номер вне диапазона."""
        if 1 <= n <= len(self.flat):
            return self.flat[n - 1]
        return None


class ResultStore:
    """Хранение последнего поиска в оперативной памяти (бот работает как один процесс; SQLite понадобится при реализации отслеживания цен)."""

    def __init__(self, ttl: float = 1800.0, clock: Callable[[], float] = time.time) -> None:
        self.ttl = ttl
        self._clock = clock
        self._store: dict[int | str, SearchSnapshot] = {}

    def put(
        self,
        chat_id: int | str,
        query: Query,
        results: list[SiteResult],
        baggage_mode: str = "any",
        *,
        flat: list[Offer] | None = None,
        top_n: int = 5,
        dropped: int = 0,
    ) -> SearchSnapshot:
        """Сохраняет снимок поиска для chat_id и возвращает его."""
        if flat is None:
            from .formatting import build_report

            _, flat = build_report(
                query, results, top_n=top_n, baggage_mode=baggage_mode, dropped=dropped
            )
        snapshot = SearchSnapshot(
            query=query,
            results=results,
            baggage_mode=baggage_mode,
            created_at=self._clock(),
            flat=flat,
        )
        self._store[chat_id] = snapshot
        return snapshot

    def get(self, chat_id: int | str) -> SearchSnapshot | None:
        """Возвращает снимок поиска для chat_id или None, если запись устарела или отсутствует."""
        snapshot = self._store.get(chat_id)
        if snapshot is None:
            return None
        now = self._clock()
        if now - snapshot.created_at > self.ttl:
            self._store.pop(chat_id, None)
            return None
        return snapshot
