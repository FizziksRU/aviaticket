from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .criteria import SearchCriteria
from .models import Offer, Query, SiteResult


@dataclass
class SearchSnapshot:
    """Снимок последнего поиска для конкретного чата."""

    query: Query
    results: list[SiteResult]
    baggage_mode: str
    created_at: float
    flat: list[Offer]
    criteria: SearchCriteria = field(default_factory=SearchCriteria)
    pool: list[Offer] = field(default_factory=list)
    shown: int = 0
    view: str = "full"
    dropped_baggage: int = 0
    dropped_criteria: int = 0

    def offer_by_number(self, n: int) -> Offer | None:
        """Возвращает билет по сквозному номеру из отчёта (нумерация с 1)."""
        if 1 <= n <= len(self.flat):
            return self.flat[n - 1]
        return None


class ResultStore:
    """Последние выдачи по чатам в памяти: бот — один процесс, рестарт их стирает.

    SQLite уже используется для отслеживания (подписки), выдачи туда не пишутся.
    """

    def __init__(self, ttl: float = 1800.0, clock: Callable[[], float] = time.time) -> None:
        self._ttl = ttl
        self._clock = clock
        self._data: dict[int | str, SearchSnapshot] = {}

    def put(
        self,
        chat_id: int | str,
        query: Query,
        results: list[SiteResult],
        baggage_mode: str,
        flat: list[Offer],
        criteria: SearchCriteria | None = None,
        pool: list[Offer] | None = None,
        shown: int = 0,
        view: str = "full",
        dropped_baggage: int = 0,
        dropped_criteria: int = 0,
    ) -> SearchSnapshot:
        """Сохраняет снимок поиска чата."""
        snapshot = SearchSnapshot(
            query=query,
            results=results,
            baggage_mode=baggage_mode,
            created_at=self._clock(),
            flat=flat,
            criteria=SearchCriteria() if criteria is None else criteria,
            pool=[] if pool is None else pool,
            shown=shown,
            view=view,
            dropped_baggage=dropped_baggage,
            dropped_criteria=dropped_criteria,
        )
        self._data[chat_id] = snapshot
        return snapshot

    def get(self, chat_id: int | str) -> SearchSnapshot | None:
        """Возвращает снимок поиска или None, если он устарел (запись удаляется)."""
        snapshot = self._data.get(chat_id)
        if snapshot is None:
            return None
        if self._clock() - snapshot.created_at > self._ttl:
            del self._data[chat_id]
            return None
        return snapshot
