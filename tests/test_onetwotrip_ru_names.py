"""Тесты русских названий городов в layovers парсера OneTwoTrip."""
from __future__ import annotations

import copy
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


def test_onetwotrip_ru_names_moscow():
    """У вариантов с пересадкой в Москве layovers начинаются с «Москва», ни один не содержит «Moscow»."""
    q = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 10))
    snap = make_snapshot()
    offers = parse_onetwotrip(snap, q)

    assert len(offers) > 0

    all_layovers: list[str] = []
    for o in offers:
        if o.layovers:
            all_layovers.extend(o.layovers)
        if o.back and o.back.layovers:
            all_layovers.extend(o.back.layovers)

    # Ни одна строка layovers не содержит английское «Moscow»
    for layover in all_layovers:
        assert isinstance(layover, str)
        assert "Moscow" not in layover

    # Первый оффер с пересадкой во Внуково (Москва): layovers начинаются с «Москва»
    first = offers[0]
    assert first.layovers == ["Москва, 21 ч 50 мин"]
    assert first.back is not None
    assert first.back.layovers == ["Москва, 13 ч 5 мин"]

    # Проверяем, что есть варианты с пересадкой в Москве и все они начинаются с «Москва»
    moscow_layovers = [l for l in all_layovers if l.startswith("Москва")]
    assert len(moscow_layovers) > 0
    for l in moscow_layovers:
        assert l.startswith("Москва, ")


def test_onetwotrip_unknown_city_fallback_to_english_name():
    """Для кода города, которого нет в CITIES, берётся английское имя из справочника."""
    q = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 10))
    raw_text = load_fixture_text("search_rt_kuf_evn_0212_1012.json")
    data = json.loads(raw_text)

    # Делаем копию данных и подменяем references
    data_copy = copy.deepcopy(data)

    unknown_city_code = "XYZ"
    assert unknown_city_code.lower() not in CITIES
    assert unknown_city_code not in CITIES.values()

    # Внуково (VKO) привязываем к неизвестному коду города с английским именем
    data_copy["references"]["airports"]["VKO"]["city"] = unknown_city_code
    data_copy["references"]["cities"][unknown_city_code] = {
        "name": "Custom English City",
        "code": unknown_city_code,
        "country": "XX",
    }

    snap = make_snapshot(payloads=[(SEARCH_URL, json.dumps(data_copy))])
    offers = parse_onetwotrip(snap, q)

    assert len(offers) > 0
    first = offers[0]
    # Должно использоваться английское имя из справочника cities
    assert first.layovers == ["Custom English City, 21 ч 50 мин"]
    assert first.back is not None
    assert first.back.layovers == ["Custom English City, 13 ч 5 мин"]


def test_onetwotrip_unknown_city_fallback_to_airport_name_and_code():
    """Если города нет ни в CITIES, ни в references.cities, берётся имя аэропорта, иначе код."""
    q = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 10))
    raw_text = load_fixture_text("search_rt_kuf_evn_0212_1012.json")
    data = json.loads(raw_text)

    # 1. Города нет в CITIES и нет в references.cities -> берётся airport name ("Vnukovo")
    data_copy1 = copy.deepcopy(data)
    data_copy1["references"]["airports"]["VKO"]["city"] = "XYZ"
    data_copy1["references"]["cities"].pop("XYZ", None)

    snap1 = make_snapshot(payloads=[(SEARCH_URL, json.dumps(data_copy1))])
    offers1 = parse_onetwotrip(snap1, q)
    assert offers1[0].layovers == ["Vnukovo, 21 ч 50 мин"]

    # 2. Имени аэропорта тоже нет -> берётся код аэропорта в верхнем регистре
    data_copy2 = copy.deepcopy(data)
    data_copy2["references"]["airports"]["VKO"]["city"] = "XYZ"
    data_copy2["references"]["airports"]["VKO"]["name"] = ""
    data_copy2["references"]["cities"].pop("XYZ", None)

    snap2 = make_snapshot(payloads=[(SEARCH_URL, json.dumps(data_copy2))])
    offers2 = parse_onetwotrip(snap2, q)
    assert offers2[0].layovers == ["VKO, 21 ч 50 мин"]
