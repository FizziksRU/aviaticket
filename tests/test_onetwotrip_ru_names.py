"""Тесты русских названий городов в пересадках OneTwoTrip."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from avia.collector import Snapshot
from avia.models import Query
from avia.parsers.onetwotrip import parse_onetwotrip
from avia.parsing import CITIES

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "onetwotrip"
SEARCH_URL = "https://www.onetwotrip.com/_avia-search-proxy/search/v3"


def load_fixture_text(name: str = "search_rt_kuf_evn_0212_1012.json") -> str:
    """Загружает содержимое фикстуры OneTwoTrip."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_snapshot(
    payloads: list[tuple[str, str]] | None = None,
    fixture_name: str = "search_rt_kuf_evn_0212_1012.json",
) -> Snapshot:
    """Создаёт тестовый Snapshot с ответом OneTwoTrip."""
    if payloads is None:
        payloads = [(SEARCH_URL, load_fixture_text(fixture_name))]
    return Snapshot(
        url="https://www.onetwotrip.com/ru/f/search/0212KUFEVN1012?sc=E&p=1_0_0",
        text="OneTwoTrip поиск",
        json_payloads=payloads,
    )


def test_onetwotrip_moscow_layovers_russian_name_and_no_english_moscow():
    """У вариантов с пересадкой в Москве layovers начинаются с 'Москва', ни одна строка не содержит 'Moscow'."""
    q = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 10))
    snap = make_snapshot()
    offers = parse_onetwotrip(snap, q)

    assert len(offers) > 0

    # Первый оффер с пересадкой в Москве (Внуково VKO -> MOW)
    first = offers[0]
    assert first.layovers is not None
    assert first.layovers[0].startswith("Москва")
    assert first.layovers == ["Москва, 21 ч 50 мин"]

    assert first.back is not None
    assert first.back.layovers is not None
    assert first.back.layovers[0].startswith("Москва")
    assert first.back.layovers == ["Москва, 13 ч 5 мин"]

    # Проверяем все офферы: ни одна строка layovers не содержит "Moscow"
    # и варианты с пересадкой в Москве начинаются с "Москва"
    all_layovers: list[str] = []
    moscow_layover_count = 0

    for o in offers:
        if o.layovers:
            all_layovers.extend(o.layovers)
            for l in o.layovers:
                assert "Moscow" not in l
                if l.startswith("Москва"):
                    moscow_layover_count += 1
        if o.back and o.back.layovers:
            all_layovers.extend(o.back.layovers)
            for l in o.back.layovers:
                assert "Moscow" not in l
                if l.startswith("Москва"):
                    moscow_layover_count += 1

    assert len(all_layovers) > 0
    assert moscow_layover_count > 0
    # Гарантируем, что ни в одной строке всех пересадок нет слова Moscow
    for l in all_layovers:
        assert "Moscow" not in l


def test_onetwotrip_unknown_city_fallback_to_english_name():
    """Для кода города, которого нет в CITIES, берётся английское имя из справочника references."""
    q = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 10))
    payload = json.loads(load_fixture_text())

    # Проверяем код, которого гарантированно нет в CITIES
    unknown_city_code = "XYZ"
    assert unknown_city_code.lower() not in CITIES
    assert unknown_city_code not in CITIES.values()

    # Подменяем город аэропорта VKO на XYZ и добавляем английское имя в references.cities
    payload["references"]["airports"]["VKO"]["city"] = unknown_city_code
    payload["references"]["cities"][unknown_city_code] = {
        "name": "Custom English City",
        "code": unknown_city_code,
    }

    snap = make_snapshot(payloads=[(SEARCH_URL, json.dumps(payload))])
    offers = parse_onetwotrip(snap, q)

    assert len(offers) > 0
    first = offers[0]
    assert first.layovers is not None
    assert first.layovers[0].startswith("Custom English City, 21 ч 50 мин")
    assert first.back is not None
    assert first.back.layovers is not None
    assert first.back.layovers[0].startswith("Custom English City, 13 ч 5 мин")


def test_onetwotrip_unknown_city_fallback_to_airport_name():
    """Если кода города нет в CITIES и нет в cities, берётся английское имя из references.airports."""
    q = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 10))
    payload = json.loads(load_fixture_text())

    unknown_city_code = "ABC"
    assert unknown_city_code.lower() not in CITIES

    payload["references"]["airports"]["VKO"]["city"] = unknown_city_code
    payload["references"]["airports"]["VKO"]["name"] = "Vnukovo Intl"
    payload["references"]["cities"].pop(unknown_city_code, None)

    snap = make_snapshot(payloads=[(SEARCH_URL, json.dumps(payload))])
    offers = parse_onetwotrip(snap, q)

    assert len(offers) > 0
    first = offers[0]
    assert first.layovers is not None
    assert first.layovers[0].startswith("Vnukovo Intl, 21 ч 50 мин")


def test_onetwotrip_unknown_city_fallback_to_airport_code():
    """Если нет имени ни в parsing, ни в cities, ни в airports, берётся код аэропорта."""
    q = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 10))
    payload = json.loads(load_fixture_text())

    payload["references"]["airports"]["VKO"]["city"] = "DEF"
    payload["references"]["airports"]["VKO"]["name"] = ""
    payload["references"]["cities"].pop("DEF", None)

    snap = make_snapshot(payloads=[(SEARCH_URL, json.dumps(payload))])
    offers = parse_onetwotrip(snap, q)

    assert len(offers) > 0
    first = offers[0]
    assert first.layovers is not None
    assert first.layovers[0].startswith("VKO, 21 ч 50 мин")
