"""Здоровье источников: последние исходы сбора по сайтам и сигнал «сайт сломался».

Парсеры глотают ошибки по элементам, а пустая выдача выглядит для человека как «нет билетов».
Здесь копится то, что видно снаружи: сколько сборов подряд не дали результата, сколько было
частичных, сколько длился сбор. Бот показывает это админу в /status и, если включено,
пишет админам, когда сайт N раз подряд не ответил.
"""
from __future__ import annotations

import time
from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .models import SiteResult

HISTORY_PER_SITE = 20
ALERT_AFTER_FAILURES = 3
_SEARCH_HISTORY = 200
_ERROR_PREVIEW = 80


@dataclass(frozen=True)
class Outcome:
    at: float
    ok: bool
    offers: int
    elapsed: float
    partial: bool
    error: str


@dataclass(frozen=True)
class SiteStat:
    site: str
    total: int
    ok: int
    partial: int
    avg_elapsed: float
    failures_in_row: int
    last_error: str
    last_at: float


class Health:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        history: int = HISTORY_PER_SITE,
        alert_after: int = ALERT_AFTER_FAILURES,
        skip_errors: Iterable[str] = (),
    ) -> None:
        self.clock = clock
        self.history = history
        self.alert_after = alert_after
        self.skip_errors = tuple(skip_errors)
        self.started_at = clock()
        self.counters: Counter[str] = Counter()
        self._sites: dict[str, deque[Outcome]] = {}
        self._streak: dict[str, int] = {}
        self._alerted: set[str] = set()
        self._search_seconds: deque[float] = deque(maxlen=_SEARCH_HISTORY)

    def record(self, results: Iterable[SiteResult], *, kind: str = "user", seconds: float | None = None) -> list[str]:
        """Учитывает результаты одного поиска; возвращает сайты, которые только что признаны сломанными."""
        self.counters[f"searches_{kind}"] += 1
        if seconds is not None:
            self._search_seconds.append(float(seconds))
        broken: list[str] = []
        now = self.clock()
        for r in results:
            if r.cached:
                self.counters["site_cache_hits"] += 1
                continue  # ответ из кэша ничего не говорит о текущем состоянии сайта
            error = r.error or ""
            if not r.ok and any(e in error for e in self.skip_errors):
                continue  # сайт не умеет такой запрос (например, обратные билеты): это не сбой
            self._sites.setdefault(r.site, deque(maxlen=self.history)).append(
                Outcome(now, r.ok, len(r.offers), float(r.elapsed), r.partial, error[:_ERROR_PREVIEW])
            )
            if r.ok:
                self._streak[r.site] = 0
                self._alerted.discard(r.site)
                continue
            self._streak[r.site] = self._streak.get(r.site, 0) + 1
            if self._streak[r.site] >= self.alert_after and r.site not in self._alerted:
                self._alerted.add(r.site)
                broken.append(r.site)
        return broken

    def stats(self) -> list[SiteStat]:
        out: list[SiteStat] = []
        for site, items in sorted(self._sites.items()):
            last_error = next((o.error for o in reversed(items) if not o.ok), "")
            out.append(SiteStat(
                site=site,
                total=len(items),
                ok=sum(o.ok for o in items),
                partial=sum(o.partial for o in items),
                avg_elapsed=sum(o.elapsed for o in items) / len(items),
                failures_in_row=self._streak.get(site, 0),
                last_error=last_error,
                last_at=items[-1].at,
            ))
        return out

    def search_percentiles(self) -> tuple[float, float] | None:
        if not self._search_seconds:
            return None
        xs = sorted(self._search_seconds)
        return xs[len(xs) // 2], xs[min(len(xs) - 1, int(len(xs) * 0.95))]

    def failures_in_row(self, site: str) -> int:
        return self._streak.get(site, 0)
