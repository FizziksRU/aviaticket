from __future__ import annotations

import asyncio
from datetime import date
import logging
import pytest

from avia.config import Settings, SiteConfig
from avia.guard import SearchGuard
from avia.models import Offer, Query
from avia.pipeline import EMPTY_RETRY_DELAY, ParserEmptyError, search


@pytest.fixture(autouse=True)
def zero_empty_retry_delay(monkeypatch):
    """Сбрасываем задержку повтора при ParserEmptyError на 0 для быстрых тестов."""
    monkeypatch.setattr("avia.pipeline.EMPTY_RETRY_DELAY", 0.0)


@pytest.fixture
def query() -> Query:
    return Query("MOW", "LED", date(2026, 12, 16))


@pytest.fixture
def browser_site() -> SiteConfig:
    return SiteConfig(
        name="kupibilet_mock",
        url_template="https://site.test/{origin}{destination}",
        source="browser",
        parser="dummy_parser",
    )


@pytest.fixture
def settings(browser_site: SiteConfig) -> Settings:
    return Settings(sites=[browser_site])


@pytest.fixture
def test_guard() -> SearchGuard:
    return SearchGuard(min_interval=0.0, retry_delay=0.0)


@pytest.mark.asyncio
async def test_retry_on_first_empty_then_success(monkeypatch, browser_site, settings, query, test_guard, caplog):
    """Первый вызов ParserEmptyError, второй успешный → SiteResult ok с офферами и ровно 2 вызова."""
    calls = 0

    async def fake_from_browser(site, q, s):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ParserEmptyError(
                "формат выдачи мог измениться, 0 вариантов при непустой странице",
                page_url="https://site.test/search",
            )
        return (
            [Offer(site=site.name, price=4500, airline="TestAir")],
            "https://site.test/result",
            False,
        )

    monkeypatch.setattr("avia.pipeline._from_browser", fake_from_browser)

    with caplog.at_level(logging.WARNING):
        results = await search(query, settings, guard=test_guard)

    assert len(results) == 1
    res = results[0]

    assert res.ok is True
    assert res.error is None
    assert len(res.offers) == 1
    assert res.offers[0].price == 4500
    assert calls == 2

    # Проверяем, что в лог записалось предупреждение с именем сайта
    assert "kupibilet_mock" in caplog.text
    assert "0 вариантов" in caplog.text or "повторная попытка" in caplog.text


@pytest.mark.asyncio
async def test_retry_on_both_empty_fails_with_original_error(monkeypatch, browser_site, settings, query, test_guard):
    """Оба вызова ParserEmptyError → ok=False, error с исходным текстом, ровно 2 вызова."""
    calls = 0
    error_text = "формат выдачи мог измениться, 0 вариантов при непустой странице"

    async def fake_from_browser(site, q, s):
        nonlocal calls
        calls += 1
        raise ParserEmptyError(error_text, page_url="https://site.test/search")

    monkeypatch.setattr("avia.pipeline._from_browser", fake_from_browser)

    results = await search(query, settings, guard=test_guard)

    assert len(results) == 1
    res = results[0]

    assert res.ok is False
    assert res.error == error_text
    assert res.offers == []
    assert calls == 2


@pytest.mark.asyncio
async def test_successful_first_call_does_not_retry(monkeypatch, browser_site, settings, query, test_guard):
    """Успешный первый вызов → 1 вызов, без повтора."""
    calls = 0

    async def fake_from_browser(site, q, s):
        nonlocal calls
        calls += 1
        return (
            [Offer(site=site.name, price=3200, airline="AirlineOK")],
            "https://site.test/result",
            False,
        )

    monkeypatch.setattr("avia.pipeline._from_browser", fake_from_browser)

    results = await search(query, settings, guard=test_guard)

    assert len(results) == 1
    res = results[0]

    assert res.ok is True
    assert res.error is None
    assert len(res.offers) == 1
    assert res.offers[0].price == 3200
    assert calls == 1


@pytest.mark.asyncio
async def test_runtime_error_captcha_does_not_retry(monkeypatch, browser_site, settings, query, test_guard):
    """RuntimeError (капча или блокировка) → 1 вызов, без повтора в _fetch."""
    calls = 0

    async def fake_from_browser(site, q, s):
        nonlocal calls
        calls += 1
        raise RuntimeError("сайт показал капчу или блокировку")

    monkeypatch.setattr("avia.pipeline._from_browser", fake_from_browser)

    results = await search(query, settings, guard=test_guard)

    assert len(results) == 1
    res = results[0]

    assert res.ok is False
    assert "капчу" in res.error
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_on_wrong_dates_then_success(monkeypatch, browser_site, settings, query, test_guard):
    """Повтор при ParserEmptyError из-за неверных дат ('сайт вернул билеты на другие даты')."""
    calls = 0

    async def fake_from_browser(site, q, s):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ParserEmptyError(
                "сайт вернул билеты на другие даты, а не на запрошенные",
                page_url="https://site.test/search",
            )
        return (
            [Offer(site=site.name, price=5100, airline="DateAir")],
            "https://site.test/result",
            False,
        )

    monkeypatch.setattr("avia.pipeline._from_browser", fake_from_browser)

    results = await search(query, settings, guard=test_guard)

    assert len(results) == 1
    res = results[0]

    assert res.ok is True
    assert res.error is None
    assert len(res.offers) == 1
    assert res.offers[0].price == 5100
    assert calls == 2


@pytest.mark.asyncio
async def test_search_guard_collapse_and_cache_with_retry(monkeypatch, browser_site, settings, query):
    """Повтор внутри _fetch сохраняет корректное схлопывание одновременных запросов в SearchGuard."""
    calls = 0
    guard = SearchGuard(min_interval=0.0, retry_delay=0.0)

    async def fake_from_browser(site, q, s):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ParserEmptyError("0 вариантов", page_url="https://site.test/search")
        return (
            [Offer(site=site.name, price=4000, airline="Air")],
            "https://site.test/result",
            False,
        )

    monkeypatch.setattr("avia.pipeline._from_browser", fake_from_browser)

    # Запускаем два одинаковых одновременных запроса
    res1, res2 = await asyncio.gather(
        search(query, settings, guard=guard),
        search(query, settings, guard=guard),
    )

    # Оба запроса завершились успешно
    assert res1[0].ok is True
    assert res2[0].ok is True
    assert len(res1[0].offers) == 1
    assert len(res2[0].offers) == 1

    # Ровно один запрос выполнял реальный поиск (2 вызова из-за повтора), второй взял из кэша
    assert calls == 2
    cached_statuses = [res1[0].cached, res2[0].cached]
    assert False in cached_statuses
    assert True in cached_statuses
