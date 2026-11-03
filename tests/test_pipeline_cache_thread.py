"""Кэш guard: неполная выдача по флагу не кэшируется, протухшее вычищается; парсер работает в потоке."""
from __future__ import annotations

import asyncio
import threading
from datetime import date

import pytest

import avia.guard as guard_mod
import avia.pipeline as pipeline
from avia.collector import Snapshot
from avia.config import Settings, SiteConfig, load_settings
from avia.guard import SearchGuard
from avia.models import Offer, Query
from avia.parsers import PARSERS

Q = Query("MOW", "LED", date(2026, 12, 1))


@pytest.mark.asyncio
async def test_uncacheable_result_is_shared_with_waiters_but_not_cached():
    g = SearchGuard(min_interval=0.0, cache_ttl=300.0)
    calls = 0
    gate = asyncio.Event()

    async def factory():
        nonlocal calls
        calls += 1
        await gate.wait()
        return "partial"

    first = asyncio.create_task(g.run("s", 1, factory, cacheable=lambda r: r != "partial"))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(g.run("s", 1, factory, cacheable=lambda r: r != "partial"))
    await asyncio.sleep(0)
    gate.set()
    assert await first == ("partial", False)
    assert await waiter == ("partial", True)
    assert calls == 1
    await g.run("s", 1, factory, cacheable=lambda r: r != "partial")
    assert calls == 2


@pytest.mark.asyncio
async def test_cache_sweeps_expired_entries_and_caps_size(monkeypatch):
    now = [0.0]
    g = SearchGuard(min_interval=0.0, cache_ttl=10.0, clock=lambda: now[0])

    async def factory():
        return "ok"

    await g.run("s", "old", factory)
    now[0] = 20.0
    await g.run("s", "new", factory)
    assert list(g._cache) == [("s", "new")]

    monkeypatch.setattr(guard_mod, "MAX_CACHE_ENTRIES", 2)
    for k in ("a", "b", "c"):
        await g.run("s", k, factory)
    assert list(g._cache) == [("s", "b"), ("s", "c")]


def make_site_settings(monkeypatch, cache_partial: bool, threads: list[str]) -> Settings:
    def parser(snap, q):
        threads.append(threading.current_thread().name)
        return [Offer(site="x", price=5000)]

    parser.results_complete = lambda snap: False  # type: ignore[attr-defined]  # всегда «неполная»
    monkeypatch.setitem(PARSERS, "cache_test_parser", parser)

    async def fake_any(settings, cands, url, **kwargs):
        return Snapshot(url=url, text="выдача " * 50), True

    monkeypatch.setattr(pipeline, "fetch_page_snapshot_any", fake_any)
    site = SiteConfig("yandex_travel", "https://y.test/{origin}{destination}", phone="realme", parser="cache_test_parser")
    return Settings(sites=[site], droidhub_url="https://hub.test", droidhub_token="t", cache_partial=cache_partial)


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_partial, fetches", [(True, 1), (False, 2)])
async def test_partial_results_cached_only_when_allowed(monkeypatch, cache_partial, fetches):
    threads: list[str] = []
    settings = make_site_settings(monkeypatch, cache_partial, threads)
    g = SearchGuard(min_interval=0.0, retry_delay=0.0)
    first = await pipeline.search(Q, settings, guard=g)
    second = await pipeline.search(Q, settings, guard=g)
    assert first[0].partial and second[0].ok
    assert len(threads) == fetches


@pytest.mark.asyncio
async def test_parser_runs_off_the_event_loop_thread(monkeypatch):
    threads: list[str] = []
    settings = make_site_settings(monkeypatch, True, threads)
    await pipeline.search(Q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert threads and threads[0] != threading.current_thread().name


def test_cache_partial_flag(monkeypatch):
    monkeypatch.delenv("CACHE_PARTIAL", raising=False)
    assert load_settings().cache_partial is True
    monkeypatch.setenv("CACHE_PARTIAL", "0")
    assert load_settings().cache_partial is False
