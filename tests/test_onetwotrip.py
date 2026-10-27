"""Тесты парсера OneTwoTrip (onetwotrip) для режима в одну сторону и туда-обратно."""
from __future__ import annotations

from datetime import date
import json
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
    """Проверка туда-обратно: только готовые цены (8 офферов), минимум 30694, пары 30804 нет."""
    q = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 10))
    snap = make_snapshot()
    offers = parse_onetwotrip(snap, q)

    # В фикстуре 9 цен с 2 variantIds, 2 с одинаковыми цепочками дедуплицируются -> 8 офферов
    assert len(offers) == 8
    # Офферы отсортированы по возрастанию цены
    prices = [o.price for o in offers]
    assert prices == sorted(prices)

    # Минимальная цена туда-обратно равна 30694 (30693.62 -> 30694)
    min_price = prices[0]
    assert min_price == 30694
    assert min_price != 15131
    # Ни один оффер не должен иметь цену ниже минимальной готовой цены с двумя variantIds
    assert all(p >= 30694 for p in prices)

    # Цена пары «половинка туда» + «половинка обратно» (15130.37 + 15673.59 -> 30804) отсутствует
    assert 30804 not in prices

    # Готовая цена с копейками округляется вверх (38016.2 -> 38017, ранее int(round(...)) давал 38016)
    assert 38017 in prices

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


def test_parse_onetwotrip_price_ceil_rounding_and_float_noise():
    """Проверка округления цен вверх (ceil) и защиты от float-шума: math.ceil(round(x, 2))."""
    # 1. Готовая цена округляется вверх (38016.2 -> 38017)
    q_rt = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 10))
    snap_rt = make_snapshot()
    offers_rt = parse_onetwotrip(snap_rt, q_rt)
    prices_rt = [o.price for o in offers_rt]
    assert 38017 in prices_rt
    assert 38016 not in prices_rt

    # 2. Цена пары половинок (15130.37 + 15673.59 -> 30804) не должна присутствовать
    assert 30804 not in prices_rt

    # 3. Одиночная цена в одну сторону также округляется вверх (15130.37 -> 15131, 8921.72 -> 8922)
    snap_ow = make_snapshot(fixture_name="search_ow_kuf_evn_0212.json")
    q_ow = Query("KUF", "EVN", date(2026, 12, 2))
    offers_ow = parse_onetwotrip(snap_ow, q_ow)
    prices_ow = [o.price for o in offers_ow]
    assert 15131 in prices_ow
    assert 15130 not in prices_ow
    assert min(prices_ow) == 8922

    # 4. Защита от float-шума: 38016.00000000001 не должна округляться в 38017
    raw = load_fixture_text("search_ow_kuf_evn_0212.json")
    data = json.loads(raw)
    first_pid = next(iter(data["prices"]))
    data["prices"][first_pid]["totalAmount"] = 38016.00000000001
    snap_noise = make_snapshot(payloads=[(SEARCH_URL, json.dumps(data))])
    offers_noise = parse_onetwotrip(snap_noise, q_ow)
    noise_prices = [o.price for o in offers_noise]
    assert 38016 in noise_prices
    assert 38017 not in noise_prices


def test_parse_onetwotrip_roundtrip_only_halves_returns_empty():
    """Если в данных есть только «половинки» (без цен с 2 variantIds), парсер при return_date возвращает []."""
    raw = load_fixture_text("search_rt_kuf_evn_0212_1012.json")
    data = json.loads(raw)
    data["prices"] = {
        k: v for k, v in data["prices"].items()
        if len(v.get("transportationVariantIds", [])) != 2
    }
    snap = make_snapshot(payloads=[(SEARCH_URL, json.dumps(data))])
    q = Query("KUF", "EVN", date(2026, 12, 2), return_date=date(2026, 12, 10))
    assert parse_onetwotrip(snap, q) == []

