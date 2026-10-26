from __future__ import annotations

import asyncio
from datetime import date
import time

import httpx
import pytest

from avia.guard import SearchGuard, friendly_error
from avia.models import Query


class FakeTime:
    """Фейковые часы и асинхронный sleep без реального ожидания времени."""

    def __init__(self, start: float = 1000.0) -> None:
        self.current = start
        self.sleep_calls: list[float] = []

    def clock(self) -> float:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.current += seconds


@pytest.fixture
def fake():
    return FakeTime()


@pytest.fixture
def guard(fake: FakeTime):
    return SearchGuard(
        min_interval=30.0,
        cache_ttl=300.0,
        clock=fake.clock,
        sleep=fake.sleep,
        retry_delay=2.0,
    )


def test_default_parameters():
    g = SearchGuard()
    assert g.min_interval == 30.0
    assert g.cache_ttl == 300.0
    assert g.retry_delay == 2.0
    assert g.clock is time.monotonic
    assert g.sleep is asyncio.sleep


@pytest.mark.asyncio
async def test_cache_hit_and_expiration(guard: SearchGuard, fake: FakeTime):
    count = 0

    async def factory():
        nonlocal count
        count += 1
        return f"result_{count}"

    # Первый поиск — промах кэша
    res1, from_cache1 = await guard.run("site1", "query1", factory)
    assert res1 == "result_1"
    assert from_cache1 is False
    assert count == 1

    # Прошло 100 секунд (меньше TTL 300) — попадание в кэш
    fake.current = 1100.0
    res2, from_cache2 = await guard.run("site1", "query1", factory)
    assert res2 == "result_1"
    assert from_cache2 is True
    assert count == 1  # factory не вызывалась

    # Прошло 301 секунда от момента кэширования — TTL истёк
    fake.current = 1301.0
    res3, from_cache3 = await guard.run("site1", "query1", factory)
    assert res3 == "result_2"
    assert from_cache3 is False
    assert count == 2  # новый реальный поиск


@pytest.mark.asyncio
async def test_errors_are_not_cached(guard: SearchGuard):
    count = 0

    async def failing_factory():
        nonlocal count
        count += 1
        raise RuntimeError("search failed")

    with pytest.raises(RuntimeError, match="search failed"):
        await guard.run("site1", "query1", failing_factory)

    assert count == 1

    # Следующий запрос не берётся из кэша, а выполняет factory
    async def ok_factory():
        return "success"

    res, from_cache = await guard.run("site1", "query1", ok_factory)
    assert res == "success"
    assert from_cache is False


@pytest.mark.asyncio
async def test_min_interval_same_site(guard: SearchGuard, fake: FakeTime):
    calls: list[float] = []

    async def factory():
        calls.append(fake.clock())
        return "ok"

    # Первый поиск стартует сразу в t=1000
    await guard.run("site1", "q1", factory)
    assert calls == [1000.0]
    assert fake.sleep_calls == []

    # Следующий поиск того же сайта через 10 секунд
    fake.current = 1010.0
    await guard.run("site1", "q2", factory)

    # Должен был подождать остаток 20 секунд (до 30 сек от первого старта)
    assert fake.sleep_calls == [20.0]
    assert calls == [1000.0, 1030.0]
    assert calls[1] - calls[0] == 30.0


@pytest.mark.asyncio
async def test_different_sites_are_independent(guard: SearchGuard, fake: FakeTime):
    order: list[str] = []

    async def factory_a():
        order.append("A")
        return "resA"

    async def factory_b():
        order.append("B")
        return "resB"

    # Сайт А запустился в t=1000
    await guard.run("siteA", "q1", factory_a)
    assert order == ["A"]

    # Сайт Б запускается в t=1005 — он не должен ждать min_interval сайта А
    fake.current = 1005.0
    await guard.run("siteB", "q1", factory_b)
    assert order == ["A", "B"]
    assert fake.sleep_calls == []  # sleep не вызывался


@pytest.mark.asyncio
async def test_concurrent_identical_queries_single_factory_call(guard: SearchGuard):
    count = 0

    async def slow_factory():
        nonlocal count
        count += 1
        await asyncio.sleep(0)  # отдаём управление, чтобы второй запрос успел войти
        return "shared_result"

    t1 = asyncio.create_task(guard.run("site1", "query", slow_factory))
    t2 = asyncio.create_task(guard.run("site1", "query", slow_factory))

    r1, r2 = await asyncio.gather(t1, t2)

    assert count == 1
    assert r1[0] == "shared_result"
    assert r2[0] == "shared_result"
    # Один получил из_кэша=False (тот, кто выполнял), второй из_кэша=True
    assert {r1[1], r2[1]} == {False, True}


@pytest.mark.asyncio
async def test_cancelled_flight_leader_allows_follower_to_proceed(guard: SearchGuard):
    """Если запрос-лидер отменён, ожидающий запрос не падает с CancelledError, а выполняет поиск сам."""
    started_first = asyncio.Event()

    async def leader_factory():
        started_first.set()
        await asyncio.sleep(10)  # Будет отменён во время ожидания
        return "leader_result"

    async def follower_factory():
        return "follower_result"

    t1 = asyncio.create_task(guard.run("site1", "query1", leader_factory))
    await started_first.wait()

    t2 = asyncio.create_task(guard.run("site1", "query1", follower_factory))
    await asyncio.sleep(0)  # Даём t2 начать и встать в ожидание t1

    t1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t1

    res, from_cache = await t2
    assert res == "follower_result"
    assert from_cache is False


@pytest.mark.asyncio
async def test_retry_on_transport_error_success(guard: SearchGuard, fake: FakeTime):
    attempts = 0

    async def flaky_factory():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("connection dropped")
        return "recovered"

    res, from_cache = await guard.run("site1", "q1", flaky_factory)
    assert res == "recovered"
    assert from_cache is False
    assert attempts == 2
    assert fake.sleep_calls == [2.0]  # retry_delay


@pytest.mark.asyncio
async def test_retry_on_transport_error_failure(guard: SearchGuard, fake: FakeTime):
    attempts = 0

    async def always_failing():
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout(f"timeout on attempt {attempts}")

    with pytest.raises(httpx.ReadTimeout, match="attempt 2"):
        await guard.run("site1", "q1", always_failing)

    assert attempts == 2
    assert fake.sleep_calls == [2.0]


@pytest.mark.asyncio
async def test_no_retry_on_value_error(guard: SearchGuard, fake: FakeTime):
    attempts = 0

    async def bad_factory():
        nonlocal attempts
        attempts += 1
        raise ValueError("non-network error")

    with pytest.raises(ValueError, match="non-network error"):
        await guard.run("site1", "q1", bad_factory)

    assert attempts == 1
    assert fake.sleep_calls == []


@pytest.mark.parametrize(
    "exc",
    [
        httpx.TransportError("transport error"),
        httpx.TimeoutException("timeout exception"),
        asyncio.TimeoutError(),
        ConnectionError("connection reset"),
        OSError("os network error"),
    ],
)
@pytest.mark.asyncio
async def test_retry_all_network_exceptions(guard: SearchGuard, exc: Exception):
    attempts = 0

    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise exc
        return "ok"

    res, from_cache = await guard.run("site1", "q", flaky)
    assert res == "ok"
    assert attempts == 2


@pytest.mark.asyncio
async def test_retry_does_not_count_as_new_user_request_for_min_interval(guard: SearchGuard, fake: FakeTime):
    """Повторная попытка подчиняется очереди, но не считается новым запросом для min_interval."""
    attempts = 0

    async def flaky_search():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.TransportError("fail 1")
        return "ok1"

    # t=1000: старт первого поиска, попытка 1 падает, пауза retry_delay 2 сек, попытка 2 успешна в t=1002
    await guard.run("site1", "q1", flaky_search)
    assert fake.current == 1002.0
    assert fake.sleep_calls == [2.0]

    # Сразу запускаем второй запрос того же сайта (t=1002)
    # От начала первого поиска (1000) прошло 2 сек, осталось ждать 28 сек до min_interval=30
    async def second_search():
        return "ok2"

    await guard.run("site1", "q2", second_search)
    assert fake.current == 1030.0
    assert fake.sleep_calls == [2.0, 28.0]


@pytest.mark.asyncio
async def test_hashable_query_types(guard: SearchGuard):
    q_str = "MOW-ALA"
    q_tuple = ("MOW", "ALA", "2026-12-10")
    q_model = Query("MOW", "ALA", date(2026, 12, 10), 1)

    r1, _ = await guard.run("s", q_str, lambda: asyncio.sleep(0, result=1))
    r2, _ = await guard.run("s", q_tuple, lambda: asyncio.sleep(0, result=2))
    r3, _ = await guard.run("s", q_model, lambda: asyncio.sleep(0, result=3))

    assert (r1, r2, r3) == (1, 2, 3)


def test_friendly_error_cases():
    captcha_msg = "сайт показал капчу, владельцу нужно пройти её руками: python -m avia.recon <сайт> ... --show"

    # Капча и блокировка
    assert friendly_error(RuntimeError("сайт показал капчу или блокировку")) == captcha_msg
    assert friendly_error(Exception("Обнаружена капча")) == captcha_msg
    assert friendly_error(RuntimeError("Сработала блокировка IP")) == captcha_msg

    # Таймауты
    assert friendly_error(httpx.ReadTimeout("read timed out")) == "таймаут"
    assert friendly_error(httpx.ConnectTimeout("connect timed out")) == "таймаут"
    assert friendly_error(asyncio.TimeoutError()) == "таймаут"
    assert friendly_error(TimeoutError("operation timed out")) == "таймаут"
    assert friendly_error(RuntimeError("таймаут соединения")) == "таймаут"

    # Сетевые ошибки
    assert friendly_error(httpx.ConnectError("connection refused")) == "нет сети"
    assert friendly_error(ConnectionResetError("peer reset")) == "нет сети"
    assert friendly_error(ConnectionError("network down")) == "нет сети"
    assert friendly_error(OSError("Host unreachable")) == "нет сети"

    # Неизвестные ошибки (только тип, без секретов и технических деталей)
    secret_exc = ValueError("secret_token_123456")
    msg = friendly_error(secret_exc)
    assert msg == "неизвестная ошибка (ValueError)"
    assert "secret_token_123456" not in msg

    key_exc = KeyError("private_key")
    assert friendly_error(key_exc) == "неизвестная ошибка (KeyError)"
    assert "private_key" not in friendly_error(key_exc)
