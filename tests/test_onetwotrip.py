"""Тесты парсера OneTwoTrip (onetwotrip) для режима в одну сторону и туда-обратно."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import pytest

from avia.collector import Snapshot
from avia.models import Query
from avia.offers import compute_offer_id
from avia.parsers.onetwotrip import parse_onetwotrip, results_complete

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "onetwotrip"
SEARCH_URL = "https://www.onetwotrip.com/_avia-search-proxy/search/v3"


def load_fixture_text(name: str = "search_rt_kuf_evn_0212_1012.json") -> str:
    """Загружает содержимое фикстуры OneTwoTrip."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_snapshot(payloads: list[tuple[str, str]] | None = None, fixture_name: str = "search_rt_kuf_evn_0212_1012.json") -> Snapshot:
    """Создаёт тестовый Snapshot с ответом OneTwoTrip."""
    if payloads is None:
        payloads = [(SEARCH_URL, load_fixture_text(fixture_name))]
    return Snapshot(
        url="https://www.onetwotrip.com/ru/f/search/0212KUFEVN1012?sc=E&p=1_0_0",
        text="OneTwoTrip поиск",
        json_payloads=payloads,
    )


def test_results_complete():
    """Проверка полноты результатов: True при наличии непустого словаря prices."""
    snap_rt = make_snapshot(fixture_name="search_rt_kuf_evn_0212_1012.json")
    assert results_complete(snap_rt) is True

    snap_ow = make_snapshot(fixture_name="search_ow_kuf_evn_0212.json")
    assert results_complete(snap_ow) is True

    # Пустой снимок или снимок с другим URL / пустыми prices
    assert results_complete(None) is False
    assert results_complete(Snapshot(url="", text="", json_payloads=[])) is False
    snap_other = Snapshot(url="", text="", json_payloads=[("https://example.com/api", "{}")])
    assert results_complete(snap_other) is False
    snap_empty_prices = Snapshot(url="", text="", json_payloads=[(SEARCH_URL, '{"prices": {}}')])
    assert results_complete(snap_empty_prices) is False


def test_parse_onetwotrip_roundtrip_prices_and_halves():
    """Проверка туда-обратно: 30694 (готовая цена) — минимум, пара половинок 30804 присутствует."""
    q = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 10))
    snap = make_snapshot()
    offers = parse_onetwotrip(snap, q)

    assert len(offers) > 0
    # Офферы отсортированы по возрастанию цены
    prices = [o.price for o in offers]
    assert prices == sorted(prices)

    # Минимальная цена туда-обратно НЕ меньше самой дешёвой готовой цены (30693.62 -> 30694)
    # и не равна цене одной половинки (15130 — занижение вдвое)
    min_price = prices[0]
    assert min_price == 30694
    assert min_price != 15130

    # Проверяем наличие пары «половинка туда» + «половинка обратно» (15130.37 + 15673.59 -> 30804)
    assert 30804 in prices

    # Проверяем базовые поля первого оффера
    first = offers[0]
    assert first.site == "onetwotrip"
    assert first.currency == "RUB"
    assert first.url is None


def test_parse_onetwotrip_roundtrip_back_leg_and_flights():
    """У каждого оффера есть back: Leg с depart_time, arrive_time, flight_numbers."""
    q = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 10))
    snap = make_snapshot()
    offers = parse_onetwotrip(snap, q)

    for o in offers:
        assert o.back is not None
        assert o.airline
        assert o.depart_time
        assert o.arrive_time
        assert o.depart_at
        assert o.arrive_at
        assert o.flight_numbers
        assert len(o.flight_numbers) > 0

        assert o.back.airline
        assert o.back.depart_time
        assert o.back.arrive_time
        assert o.back.depart_at
        assert o.back.arrive_at
        assert o.back.flight_numbers
        assert len(o.back.flight_numbers) > 0

    first = offers[0]
    assert first.airline == "Utair"
    assert first.depart_time == "23:15"
    assert first.arrive_time == "02:30"
    assert first.depart_at == "2026-12-02T23:15"
    assert first.flight_numbers == ["UT 358", "UT 785"]
    assert first.transfers == 1
    assert first.aircraft == "Boeing 737-500, Boeing 737-800"

    assert first.back.airline == "Utair"
    assert first.back.depart_time == "03:55"
    assert first.back.arrive_time == "22:15"
    assert first.back.depart_at == "2026-12-10T03:55"
    assert first.back.arrive_at == "2026-12-10T22:15"
    assert first.back.duration == "18ч 20м"
    assert first.back.flight_numbers == ["UT 786", "UT 357"]
    assert first.back.aircraft == "Boeing 737-800"
    assert first.back.transfers == 1


def test_parse_onetwotrip_roundtrip_layovers():
    """Проверка формирования поля layovers у вариантов с пересадками."""
    q = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 10))
    snap = make_snapshot()
    offers = parse_onetwotrip(snap, q)

    # Первый оффер с пересадкой во Внуково (Москва): 00:10 -> 22:00 = 21 ч 50 мин
    first = offers[0]
    assert first.layovers == ["Москва, 21 ч 50 мин"]
    assert first.back.layovers == ["Москва, 13 ч 5 мин"]

    for o in offers:
        if o.transfers > 0:
            assert o.layovers is not None
            assert len(o.layovers) == o.transfers
            for l in o.layovers:
                assert ", " in l
        if o.back and o.back.transfers > 0:
            assert o.back.layovers is not None
            assert len(o.back.layovers) == o.back.transfers


def test_parse_onetwotrip_roundtrip_baggage_and_carry_on():
    """Проверка определения багажа по услугам: есть как 'с багажом', так и 'без багажа'."""
    q = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 10))
    snap = make_snapshot()
    offers = parse_onetwotrip(snap, q)

    with_bag = [o for o in offers if o.baggage == "с багажом"]
    without_bag = [o for o in offers if o.baggage == "без багажа"]

    assert len(with_bag) > 0
    assert len(without_bag) > 0

    # Самый дешёвый вариант — без багажа
    assert offers[0].baggage == "без багажа"
    assert offers[0].carry_on == "ручная кладь есть"

    # Вариант Ud2L1B (164677) имеет услугу 3CKps4 (1PC20KG) по обоим направлениям -> с багажом
    bag_offer = next(o for o in offers if o.price == 164677)
    assert bag_offer.baggage == "с багажом"
    assert bag_offer.carry_on == "ручная кладь есть"


def test_parse_onetwotrip_date_protection_and_mode_mismatch():
    """Защита от чужих дат: возврат [] при несовпадении дат или числа направлений."""
    snap_rt = make_snapshot(fixture_name="search_rt_kuf_evn_0212_1012.json")

    # Query без return_date на RT-фикстуре (направлений 2, режим 1) -> []
    q_ow = Query("KUF", "EVN", date(2026, 12, 2))
    assert parse_onetwotrip(snap_rt, q_ow) == []

    # Query на другие даты вылета (2026-12-03) -> []
    q_wrong_depart = Query("KUF", "EVN", date(2026, 12, 3), return_date=date(2026, 12, 10))
    assert parse_onetwotrip(snap_rt, q_wrong_depart) == []

    # Query на другие даты возврата (2026-12-11) -> []
    q_wrong_return = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 11))
    assert parse_onetwotrip(snap_rt, q_wrong_return) == []


def test_parse_onetwotrip_oneway_fixture():
    """Односторонняя фикстура: офферы без back, минимальная цена 8922."""
    snap_ow = make_snapshot(fixture_name="search_ow_kuf_evn_0212.json")
    q_ow = Query("KUF", "EVN", date(2026, 12, 2))
    offers = parse_onetwotrip(snap_ow, q_ow)

    assert len(offers) > 0
    assert min(o.price for o in offers) == 8922

    for o in offers:
        assert o.back is None
        assert o.depart_time
        assert o.arrive_time
        assert o.depart_at
        assert o.arrive_at
        assert o.flight_numbers

    # При попытке запросить return_date на односторонней фикстуре (направлений 1, режим 2) -> []
    q_rt = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 10))
    assert parse_onetwotrip(snap_ow, q_rt) == []


def test_parse_onetwotrip_stable_and_distinct_offer_id():
    """compute_offer_id стабилен при повторных вызовах и различается у разных обратных цепочек."""
    q = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 10))
    snap = make_snapshot()
    offers1 = parse_onetwotrip(snap, q)
    offers2 = parse_onetwotrip(snap, q)

    assert len(offers1) == len(offers2)
    for o1, o2 in zip(offers1, offers2):
        assert compute_offer_id(o1) == compute_offer_id(o2)

    # Находим оффер с другой обратной цепочкой
    o0 = offers1[0]
    o_diff = next(
        o for o in offers1
        if (o.back.flight_numbers, o.back.depart_at) != (o0.back.flight_numbers, o0.back.depart_at)
    )
    assert compute_offer_id(o0) != compute_offer_id(o_diff)
