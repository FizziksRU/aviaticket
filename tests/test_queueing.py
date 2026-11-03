from __future__ import annotations

import asyncio

import pytest

from avia.queueing import INTERACTIVE, BACKGROUND, PrioritySlots


@pytest.mark.asyncio
async def test_free_slot_is_taken_without_waiting():
    slots = PrioritySlots(2)
    async with slots.slot(INTERACTIVE):
        assert slots.busy == 1
        assert slots.waiting == 0
    assert slots.busy == 0


@pytest.mark.asyncio
async def test_interactive_overtakes_background_in_queue():
    slots = PrioritySlots(1)
    order: list[str] = []
    gate = asyncio.Event()

    async def holder():
        async with slots.slot(BACKGROUND):
            await gate.wait()

    async def job(name: str, prio: int):
        async with slots.slot(prio):
            order.append(name)

    h = asyncio.create_task(holder())
    await asyncio.sleep(0)
    bg = asyncio.create_task(job("bg", BACKGROUND))
    await asyncio.sleep(0)
    user = asyncio.create_task(job("user", INTERACTIVE))
    await asyncio.sleep(0)
    assert slots.waiting == 2
    gate.set()
    await asyncio.gather(h, bg, user)
    assert order == ["user", "bg"]


@pytest.mark.asyncio
async def test_fifo_within_same_priority_and_position_callback():
    slots = PrioritySlots(1)
    order: list[int] = []
    positions: dict[int, int] = {}
    gate = asyncio.Event()

    async def holder():
        async with slots.slot(INTERACTIVE):
            await gate.wait()

    async def job(i: int):
        async def on_wait(pos: int) -> None:
            positions[i] = pos

        async with slots.slot(INTERACTIVE, on_wait=on_wait):
            order.append(i)

    h = asyncio.create_task(holder())
    await asyncio.sleep(0)
    jobs = []
    for i in range(3):
        jobs.append(asyncio.create_task(job(i)))
        await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(h, *jobs)
    assert order == [0, 1, 2]
    assert positions == {0: 1, 1: 2, 2: 3}


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_slot():
    slots = PrioritySlots(1)
    gate = asyncio.Event()

    async def holder():
        async with slots.slot(INTERACTIVE):
            await gate.wait()

    async def waiter():
        async with slots.slot(INTERACTIVE):
            pass

    h = asyncio.create_task(holder())
    await asyncio.sleep(0)
    w = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    w.cancel()
    await asyncio.gather(w, return_exceptions=True)
    gate.set()
    await h
    assert slots.busy == 0 and slots.waiting == 0
    async with slots.slot(INTERACTIVE):
        assert slots.busy == 1


@pytest.mark.asyncio
async def test_eta_uses_moving_average_of_durations():
    slots = PrioritySlots(2, default_duration=100.0)
    assert slots.eta_seconds(position=4) == pytest.approx(200.0)
    slots.observe(50.0)
    assert slots.eta_seconds(position=2) < 100.0


@pytest.mark.asyncio
async def test_guard_site_concurrency_allows_parallel_fetches_but_spaces_starts():
    from avia.guard import SearchGuard

    now = [0.0]
    slept: list[float] = []

    async def fake_sleep(sec: float) -> None:
        slept.append(sec)
        now[0] += sec

    g = SearchGuard(min_interval=10.0, cache_ttl=0.0, clock=lambda: now[0], sleep=fake_sleep, site_concurrency=2)
    running = 0
    peak = 0
    release = asyncio.Event()

    async def factory():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await release.wait()
        running -= 1
        return "ok"

    tasks = [asyncio.create_task(g.run("site", i, factory)) for i in range(2)]
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2
    assert slept == [10.0]


@pytest.mark.asyncio
async def test_guard_default_keeps_one_fetch_per_site():
    from avia.guard import SearchGuard

    g = SearchGuard(min_interval=0.0, cache_ttl=0.0)
    running = 0
    peak = 0

    async def factory():
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        running -= 1
        return 1

    await asyncio.gather(*(g.run("site", i, factory) for i in range(3)))
    assert peak == 1


def test_settings_read_queue_and_guard_env(monkeypatch, tmp_path):
    from avia.config import load_settings

    for k, v in {"GUARD_MIN_INTERVAL": "12", "GUARD_CACHE_TTL": "900", "GUARD_SITE_CONCURRENCY": "3",
                 "SITES_PER_SEARCH": "4", "SEARCH_PRIORITY": "1", "QUEUE_NOTICE": "1"}.items():
        monkeypatch.setenv(k, v)
    s = load_settings()
    assert (s.guard_min_interval, s.guard_cache_ttl, s.guard_site_concurrency) == (12.0, 900.0, 3)
    assert (s.sites_per_search, s.search_priority, s.queue_notice) == (4, True, True)


def test_settings_defaults_keep_old_behaviour(monkeypatch):
    from avia.config import load_settings

    for k in ("GUARD_MIN_INTERVAL", "GUARD_CACHE_TTL", "GUARD_SITE_CONCURRENCY", "SITES_PER_SEARCH",
              "SEARCH_PRIORITY", "QUEUE_NOTICE"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings()
    assert (s.guard_min_interval, s.guard_cache_ttl, s.guard_site_concurrency, s.sites_per_search) == (30.0, 300.0, 1, 2)
    assert not s.search_priority and not s.queue_notice


@pytest.mark.asyncio
async def test_bot_queue_notice_edits_wait_message(tmp_path):
    from datetime import date

    from avia.bot import Bot
    from avia.config import Settings
    from avia.models import Query

    calls: list[tuple[str, dict]] = []

    class B(Bot):
        async def call(self, method, **params):
            calls.append((method, params))
            return {"message_id": 77}

    bot = B(Settings(sites=[], tg_token="t", tg_allowed_ids={1}, storage_dir=tmp_path, queue_notice=True))
    notice = bot._queue_notice(1, 77, Query("MOW", "LED", date(2026, 12, 1)))
    await notice(3)
    method, params = calls[-1]
    assert method == "editMessageText" and params["message_id"] == 77
    assert "3-й в очереди" in params["text"] and "MOW → LED" in params["text"]


@pytest.mark.asyncio
async def test_scheduler_search_yields_to_people_when_priority_enabled(monkeypatch, tmp_path):
    import avia.bot as bot_mod
    from avia.config import Settings

    order: list[str] = []

    async def fake_search(q, settings):
        order.append("subscription")
        return []

    monkeypatch.setattr(bot_mod, "search", fake_search)
    bot = bot_mod.Bot(Settings(sites=[], tg_token="t", tg_allowed_ids={1}, storage_dir=tmp_path,
                               max_parallel_searches=1, search_priority=True))
    gate = asyncio.Event()

    async def holder():
        async with bot._search_sem.slot(INTERACTIVE):
            await gate.wait()

    async def person():
        async with bot._search_sem.slot(INTERACTIVE):
            order.append("person")

    h = asyncio.create_task(holder())
    await asyncio.sleep(0)
    from datetime import date

    from avia.models import Query

    sub = asyncio.create_task(bot._scheduler_search(Query("MOW", "LED", date(2026, 12, 1))))
    await asyncio.sleep(0)
    user = asyncio.create_task(person())
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(h, sub, user)
    assert order == ["person", "subscription"]
