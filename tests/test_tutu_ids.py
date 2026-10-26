from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import httpx
import pytest

from avia.collector import Snapshot
from avia.config import Settings, SiteConfig
from avia.models import Query
from avia.pipeline import _from_browser
from avia.sites import build_url
from avia.tutu_ids import _CACHE, STATIC_CITIES, parse_suggestions, resolve_city

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "tutu" / "suggest_moskva.json"


@pytest.fixture(autouse=True)
def reset_cache():
    """Сбрасывает кэш идентификаторов городов перед и после каждого теста."""
    _CACHE.clear()
    yield
    _CACHE.clear()


@pytest.mark.asyncio
async def test_resolve_city_suggest_moskva():
    """Разбор suggest_moskva.json через MockTransport возвращает (491, Moskva)."""
    raw_json = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        assert "name=MOW" in str(request.url)
        assert request.headers.get("User-Agent", "").startswith("Mozilla/5.0")
        return httpx.Response(200, json=raw_json)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        city_id, seo_id = await resolve_city("MOW", client=client)

    assert city_id == "491"
    assert seo_id == "Moskva"
    assert len(requested_urls) == 1


@pytest.mark.asyncio
async def test_resolve_city_chooses_locality_not_airport():
    """Выбор первой подсказки именно с geo_type == LOCALITY, а не AIRPORT."""
    # Создаём полезную нагрузку, где первым идёт AIRPORT, а вторым LOCALITY
    payload = {
        "suggestions": [
            {
                "geo_type": "AIRPORT",
                "city": {"id": "491", "name": "Москва", "code": "MOW", "seo_id": "Moskva"},
                "airport": {"id": "848", "name": "Жуковский", "code": "ZIA"},
            },
            {
                "geo_type": "LOCALITY",
                "city": {"id": "491", "name": "Москва", "code": "MOW", "seo_id": "Moskva"},
                "airport": None,
            },
        ]
    }

    # Проверка прямой функции разбора
    city_id, seo_id = parse_suggestions(payload, "MOW")
    assert (city_id, seo_id) == ("491", "Moskva")

    # И через resolve_city с MockTransport
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        res = await resolve_city("mow", client=client)
    assert res == ("491", "Moskva")


@pytest.mark.asyncio
async def test_resolve_city_cache():
    """Кэш в памяти: второй вызов с тем же IATA не делает повторный сетевой запрос."""
    raw_json = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=raw_json)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        # Первый вызов: идёт обращение к клиенту
        res1 = await resolve_city("MOW", client=client)
        assert res1 == ("491", "Moskva")
        assert call_count == 1

        # Второй вызов: значение возвращается из кэша, mock не вызывается
        res2 = await resolve_city("MOW", client=client)
        assert res2 == ("491", "Moskva")
        assert call_count == 1


@pytest.mark.asyncio
async def test_resolve_city_network_error_raises_runtime_error():
    """Ошибка сети при запросе к подсказчику оборачивается в RuntimeError."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused by peer")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="Ошибка сети"):
            await resolve_city("MOW", client=client)


@pytest.mark.asyncio
async def test_resolve_city_empty_response_raises_runtime_error():
    """Пустой ответ подсказчика вызывает RuntimeError с понятным русским сообщением."""
    # Пустой список подсказок
    transport_empty = httpx.MockTransport(lambda req: httpx.Response(200, json={"suggestions": []}))
    async with httpx.AsyncClient(transport=transport_empty) as client:
        with pytest.raises(RuntimeError, match="Пустой ответ"):
            await resolve_city("MOW", client=client)

    # Нет совпадения по запрошенному коду
    payload_other = {
        "suggestions": [
            {
                "geo_type": "LOCALITY",
                "city": {"id": "117", "name": "Алматы", "code": "ALA", "seo_id": "Almati"},
            }
        ]
    }
    transport_other = httpx.MockTransport(lambda req: httpx.Response(200, json=payload_other))
    async with httpx.AsyncClient(transport=transport_other) as client:
        with pytest.raises(RuntimeError, match="не найден"):
            await resolve_city("MOW", client=client)


@pytest.mark.asyncio
async def test_resolve_city_static_reserve():
    """Встроенный словарь-запас для частых городов работает без сети при client=None."""
    res_mow = await resolve_city("MOW")
    res_ala = await resolve_city("ALA")
    assert res_mow == ("491", "Moskva")
    assert res_ala == ("117", "Almati")
    assert STATIC_CITIES["MOW"] == ("491", "Moskva")
    assert STATIC_CITIES["ALA"] == ("117", "Almati")


def test_build_url_with_extra():
    """build_url с extra возвращает ровно ожидаемый URL для Туту."""
    site = SiteConfig(
        name="tutu",
        url_template="https://avia.tutu.ru/f/{origin_seo}/{destination_seo}/?class=Y&travelers={adults}&route[0]={origin_id}-{ddmmyyyy}-{destination_id}",
        id_resolver="tutu",
    )
    q = Query("MOW", "ALA", date(2026, 12, 12))
    extra = {
        "origin_id": "491",
        "destination_id": "117",
        "origin_seo": "Moskva",
        "destination_seo": "Almati",
    }
    url = build_url(site, q, extra=extra)
    assert url == "https://avia.tutu.ru/f/Moskva/Almati/?class=Y&travelers=1&route[0]=491-12122026-117"


def test_build_url_without_extra_unchanged():
    """Старый build_url без extra работает по-прежнему (шаблоны Aviasales и Купибилет)."""
    q = Query("MOW", "ALA", date(2026, 12, 12))

    site_as = SiteConfig(
        name="aviasales",
        url_template="https://www.aviasales.ru/search/{origin}{ddmm}{destination}{adults}",
    )
    assert build_url(site_as, q) == "https://www.aviasales.ru/search/MOW1212ALA1"

    site_kb = SiteConfig(
        name="kupibilet",
        url_template="https://www.kupibilet.ru/search?from={origin}&to={destination}&date={date_iso}&passengers={adults}",
    )
    assert build_url(site_kb, q) == "https://www.kupibilet.ru/search?from=MOW&to=ALA&date=2026-12-12&passengers=1"


@pytest.mark.asyncio
async def test_pipeline_tutu_id_resolution(monkeypatch):
    """В _from_browser при id_resolver == 'tutu' формируется URL с разрешёнными extra-параметрами."""
    from avia.models import Offer
    from avia.parsers import PARSERS

    captured_urls: list[str] = []

    async def fake_fetch_snapshot(url, site, **kwargs):
        captured_urls.append(url)
        return Snapshot(url=url, text="OK page", json_payloads=[])

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_fetch_snapshot)
    monkeypatch.setitem(PARSERS, "dummy_tutu_parser", lambda snap, q: [Offer(site=snap.url, price=12345)])

    site = SiteConfig(
        name="tutu",
        url_template="https://avia.tutu.ru/f/{origin_seo}/{destination_seo}/?class=Y&travelers={adults}&route[0]={origin_id}-{ddmmyyyy}-{destination_id}",
        id_resolver="tutu",
        parser="dummy_tutu_parser",
    )
    settings = Settings(sites=[site])
    q = Query("MOW", "ALA", date(2026, 12, 12))

    # _from_browser разрешит MOW и ALA из встроенного словаря/кэша и передаст extra в build_url
    offers, page_url, partial = await _from_browser(site, q, settings)
    assert len(captured_urls) == 1
    assert captured_urls[0] == "https://avia.tutu.ru/f/Moskva/Almati/?class=Y&travelers=1&route[0]=491-12122026-117"
    assert len(offers) == 1
    assert offers[0].price == 12345
