"""Виртуальное время для нагрузочного прогона бота без изменения его кода.

Цикл событий живёт по часам «реальное время + смещение»: когда циклу нечего делать и он
собрался бы спать до ближайшего таймера, смещение увеличивается на этот интервал, а сна нет.
CPU-работа (парсеры, SQLite, форматирование) идёт в реальном времени и поэтому честно
отражается в замерах, а ожидания (сеть телефона, sleep в guard, тик планировщика) проходят мгновенно.

Все модули бота используют time.time/time.monotonic, поэтому они подменяются глобально
на виртуальные; asyncio.to_thread выполняется в потоке цикла (иначе прыжок времени
мог бы случиться, пока поток ещё работает).
"""
from __future__ import annotations

import asyncio
import selectors
import time
from collections.abc import Callable
from typing import Any

_real_monotonic = time.monotonic
_real_time = time.time
_real_perf = time.perf_counter


class VirtualClock:
    def __init__(self) -> None:
        self.offset = 0.0

    def monotonic(self) -> float:
        return _real_monotonic() + self.offset

    def time(self) -> float:
        return _real_time() + self.offset

    def jump(self, seconds: float) -> None:
        if seconds > 0:
            self.offset += seconds


class _JumpSelector(selectors.BaseSelector):
    """Селектор, который вместо сна сдвигает виртуальные часы."""

    def __init__(self, clock: VirtualClock) -> None:
        self._inner = selectors.DefaultSelector()
        self._clock = clock

    def register(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._inner.register(fileobj, events, data)

    def unregister(self, fileobj: Any) -> selectors.SelectorKey:
        return self._inner.unregister(fileobj)

    def modify(self, fileobj: Any, events: int, data: Any = None) -> selectors.SelectorKey:
        return self._inner.modify(fileobj, events, data)

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        ready = self._inner.select(0)
        if ready or timeout == 0:
            return ready
        if timeout is None:
            # нет таймеров и нет готовых задач: в замкнутой симуляции это дедлок, не висим вечно
            return self._inner.select(0.05)
        self._clock.jump(timeout)
        return []

    def close(self) -> None:
        self._inner.close()

    def get_key(self, fileobj: Any) -> selectors.SelectorKey:
        return self._inner.get_key(fileobj)

    def get_map(self) -> Any:
        return self._inner.get_map()


class VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(self, clock: VirtualClock) -> None:
        super().__init__(selector=_JumpSelector(clock))
        self._vclock = clock

    def time(self) -> float:
        return self._vclock.monotonic()


async def _inline_to_thread(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


def install(clock: VirtualClock) -> Callable[[], None]:
    """Подменяет time.* и asyncio.to_thread; возвращает функцию отката."""
    saved = (time.monotonic, time.time, asyncio.to_thread)
    time.monotonic = clock.monotonic  # type: ignore[assignment]
    time.time = clock.time  # type: ignore[assignment]
    asyncio.to_thread = _inline_to_thread  # type: ignore[assignment]

    def restore() -> None:
        time.monotonic, time.time, asyncio.to_thread = saved  # type: ignore[assignment]

    return restore


def run(coro_factory: Callable[[VirtualClock], Any]) -> tuple[Any, VirtualClock]:
    """Запускает coro_factory(clock) в виртуальном времени. Возвращает (результат, часы)."""
    clock = VirtualClock()
    restore = install(clock)
    loop = VirtualLoop(clock)
    try:
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(coro_factory(clock))
        return result, clock
    finally:
        restore()
        asyncio.set_event_loop(None)
        loop.close()


def real_perf_counter() -> float:
    return _real_perf()
