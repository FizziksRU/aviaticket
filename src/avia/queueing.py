"""Очередь поисков с приоритетом: живые люди впереди фонового опроса подписок."""
from __future__ import annotations

import asyncio
import heapq
import itertools
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

INTERACTIVE = 0
BACKGROUND = 1
# вес нового замера в скользящем среднем длительности поиска
_EMA_ALPHA = 0.2


class PrioritySlots:
    """Не больше limit одновременных поисков; ждущие выходят по (приоритет, порядок прихода).

    С одинаковым приоритетом для всех ведёт себя как обычный asyncio.Semaphore (FIFO).
    """

    def __init__(self, limit: int, default_duration: float = 90.0) -> None:
        self.limit = max(1, int(limit))
        self.busy = 0
        self._heap: list[tuple[int, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()
        self._avg_duration = float(default_duration)

    @property
    def waiting(self) -> int:
        return sum(1 for *_, fut in self._heap if not fut.done())

    def position(self, priority: int, seq: int) -> int:
        """Номер в очереди (1 = следующий) для записи (priority, seq)."""
        return 1 + sum(1 for p, s, fut in self._heap if not fut.done() and (p, s) < (priority, seq))

    def observe(self, duration: float) -> None:
        self._avg_duration = (1 - _EMA_ALPHA) * self._avg_duration + _EMA_ALPHA * float(duration)

    def eta_seconds(self, position: int) -> float:
        """Оценка ожидания: каждые limit поисков освобождают место через среднюю длительность."""
        return position * self._avg_duration / self.limit

    @asynccontextmanager
    async def slot(
        self, priority: int = INTERACTIVE, on_wait: Callable[[int], Awaitable[None]] | None = None
    ) -> AsyncIterator[None]:
        if self.busy < self.limit and self.waiting == 0:
            self.busy += 1
        else:
            fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            entry = (priority, next(self._seq), fut)
            heapq.heappush(self._heap, entry)
            # сообщение о позиции шлём отдельной задачей: медленный Telegram не должен держать
            # уже выданный слот, а отмена во время отправки — терять его
            notice = None
            if on_wait is not None:
                notice = asyncio.ensure_future(_quietly(on_wait(self.position(entry[0], entry[1]))))
            try:
                await fut
            except asyncio.CancelledError:
                if fut.done() and not fut.cancelled():
                    self._release()  # слот уже передан нам, а мы уходим: отдаём дальше
                else:
                    fut.cancel()  # _release пропустит отменённую запись
                raise
            finally:
                if notice is not None and not notice.done():
                    notice.cancel()
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            yield
        finally:
            self.observe(loop.time() - started)
            self._release()

    def _release(self) -> None:
        while self._heap:
            _, _, fut = heapq.heappop(self._heap)
            if not fut.done():
                fut.set_result(None)  # слот переходит следующему, busy не меняется
                return
        self.busy -= 1


async def _quietly(coro: Awaitable[None]) -> None:
    """Сообщение о позиции в очереди не должно ломать поиск."""
    try:
        await coro
    except Exception:
        pass
