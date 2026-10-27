"""Тесты парсера Aviasales для режима туда-обратно (roundtrip)."""
from datetime import date
from pathlib import Path
import json
import pytest

from avia.collector import Snapshot
from avia.models import Offer, Query
from avia.offers import compute_offer_id
from avia.parsers.aviasales import parse_aviasales

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "aviasales"
RESULTS_URL = "https://www.aviasales.ru/search/v3.2/results?search_id=01a17a0e-c6fd-8031-68ac-a7017f49fd7b"


def load_fixture_text(name: str = "results_rt_kuf_evn.json") -> str:
    """Загружает текст фикстуры Aviasales."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_snapshot(payloads: list[tuple[str, str]] | None = None) -> Snapshot:
    """Создаёт Snapshot с ответами Aviasales."""
    if payloads is None:
        payloads = [(RESULTS_URL, load_fixture_text())]
    return Snapshot(
        url="https://www.aviasales.ru/search/KUF1612EVN20121",
        text="Выдача билетов",
        json_payloads=payloads,
    )


def test_parse_aviasales_roundtrip_count_and_min_price():
    """Проверка числа офферов и минимальной цены по реальной фикстуре туда-обратно."""
    q = Query(
        origin="KUF",
        destination="EVN",
        depart=date(2026, 12, 16),
        return_date=date(2026, 12, 20),
    )
    snap = make_snapshot()
    offers = parse_aviasales(snap, q)

    # 5 уникальных билетов в фикстуре
    assert len(offers) == 5
    # Минимальная цена 42898 за оба плеча (билет FLYONE Armenia)
    assert offers[0].price == 42898
    assert offers[0].currency == "RUB"


def test_parse_aviasales_roundtrip_back_leg_and_flight_numbers():
    """У каждого оффера есть back с непустыми depart_time/arrive_time и flight_numbers в обоих плечах."""
    q = Query(
        origin="KUF",
        destination="EVN",
        depart=date(2026, 12, 16),
        return_date=date(2026, 12, 20),
    )
    snap = make_snapshot()
    offers = parse_aviasales(snap, q)

    for o in offers:
        assert o.back is not None
        assert o.back.depart_time
        assert o.back.arrive_time
        assert o.flight_numbers
        assert len(o.flight_numbers) > 0
        assert o.back.flight_numbers
        assert len(o.back.flight_numbers) > 0

    # Проверка деталей первого (прямого) билета
    first = offers[0]
    assert first.airline == "FLYONE Armenia"
    assert first.depart_time == "10:15"
    assert first.arrive_time == "13:30"
    assert first.flight_numbers == ["3F 3384"]
    assert first.aircraft == "A320A"
    assert first.depart_at == "2026-12-16T10:15"
    assert first.arrive_at == "2026-12-16T13:30"
    assert first.carry_on == "ручная кладь нет"
    assert first.layovers is None

    assert first.back.airline == "FLYONE Armenia"
    assert first.back.depart_time == "06:05"
    assert first.back.arrive_time == "08:50"
    assert first.back.flight_numbers == ["3F 3383"]
    assert first.back.aircraft == "A320A"
    assert first.back.depart_at == "2026-12-20T06:05"
    assert first.back.arrive_at == "2026-12-20T08:50"
    assert first.back.layovers is None


def test_parse_aviasales_roundtrip_layovers():
    """Проверка layovers для билетов с пересадками в обоих плечах."""
    q = Query(
        origin="KUF",
        destination="EVN",
        depart=date(2026, 12, 16),
        return_date=date(2026, 12, 20),
    )
    snap = make_snapshot()
    offers = parse_aviasales(snap, q)

    # Билет с 2 пересадками туда и 2 обратно (цена 42987)
    o_transfer = next(o for o in offers if o.price == 42987)
    assert o_transfer.transfers == 2
    assert o_transfer.layovers == ["Москва, 6 ч 20 мин", "Стамбул, 18 ч 55 мин"]
    assert o_transfer.carry_on == "ручная кладь есть"
    assert o_transfer.back is not None
    assert o_transfer.back.transfers == 2
    assert o_transfer.back.layovers == ["Стамбул, 6 ч 35 мин", "Москва, 3 ч 10 мин"]
    # Проверка уникальности aircraft через запятую
    assert o_transfer.back.aircraft == "Airbus A321, Boeing 737-800/BBJ2 (winglets)"

    # Билет с 3 пересадками на обратном плече (цена 45388)
    o_t4 = next(o for o in offers if o.price == 45388)
    assert o_t4.back is not None
    assert o_t4.back.layovers == ["Тбилиси, 16 ч", "Сочи, 4 ч 20 мин", "Москва, 1 ч 30 мин"]


def test_parse_aviasales_roundtrip_baggage():
    """Проверка определения багажа: только если у ВСЕХ перелётов обоих плеч total_baggage_count > 0."""
    q = Query(
        origin="KUF",
        destination="EVN",
        depart=date(2026, 12, 16),
        return_date=date(2026, 12, 20),
    )
    snap = make_snapshot()
    offers = parse_aviasales(snap, q)

    # В реальной фикстуре ни у одного билета нет полного включения багажа на всех перелётах
    for o in offers:
        assert o.baggage == "без багажа"

    # Синтетический чанк: багаж есть на всех плечах vs багаж есть только на одном плече
    chunk = {
        "chunk_id": "results",
        "last_update_timestamp": 0,
        "flight_legs": [
            {
                "local_departure_date_time": "2026-12-16 10:00",
                "local_arrival_date_time": "2026-12-16 14:00",
                "departure_unix_timestamp": 1700000000,
                "arrival_unix_timestamp": 1700014400,
                "operating_carrier_designator": {"carrier": "SU", "number": "100"},
            },
            {
                "local_departure_date_time": "2026-12-20 10:00",
                "local_arrival_date_time": "2026-12-20 14:00",
                "departure_unix_timestamp": 1700345600,
                "arrival_unix_timestamp": 1700360000,
                "operating_carrier_designator": {"carrier": "SU", "number": "101"},
            },
        ],
        "airlines": {"SU": {"name": {"ru": {"default": "Аэрофлот"}}}},
        "tickets": [
            {
                "id": "t_both_baggage",
                "segments": [{"flights": [0]}, {"flights": [1]}],
                "proposals": [
                    {
                        "price": {"value": 20000, "currency_code": "RUB"},
                        "flight_terms": {
                            "0": {"total_baggage_count": 1},
                            "1": {"total_baggage_count": 1},
                        },
                    }
                ],
            },
            {
                "id": "t_one_baggage",
                "segments": [{"flights": [0]}, {"flights": [1]}],
                "proposals": [
                    {
                        "price": {"value": 15000, "currency_code": "RUB"},
                        "flight_terms": {
                            "0": {"total_baggage_count": 1},
                            "1": {"total_baggage_count": 0},
                        },
                    }
                ],
            },
        ],
    }

    snap_synth = make_snapshot([(RESULTS_URL, json.dumps([chunk]))])
    offers_synth = parse_aviasales(snap_synth, q)
    assert len(offers_synth) == 2
    one_bag = next(o for o in offers_synth if o.price == 15000)
    both_bag = next(o for o in offers_synth if o.price == 20000)
    assert one_bag.baggage == "без багажа"
    assert both_bag.baggage == "с багажом"


def test_parse_aviasales_oneway_query_without_return_date():
    """Тот же файл фикстуры, Query без return_date -> туда-плечо, back=None."""
    q_oneway = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 16))
    snap = make_snapshot()
    offers = parse_aviasales(snap, q_oneway)

    # 5 офферов, у всех back=None
    assert len(offers) == 5
    for o in offers:
        assert o.back is None
        assert o.depart_time
        assert o.arrive_time
        assert o.flight_numbers
        assert o.depart_at
        assert o.arrive_at

    # Минимальная цена сохраняется
    assert offers[0].price == 42898
    assert offers[0].flight_numbers == ["3F 3384"]


def test_parse_aviasales_roundtrip_stable_offer_id():
    """offer_id стабилен при повторном разборе и различается у офферов с разными обратными рейсами."""
    q = Query(
        origin="KUF",
        destination="EVN",
        depart=date(2026, 12, 16),
        return_date=date(2026, 12, 20),
    )
    snap = make_snapshot()
    offers1 = parse_aviasales(snap, q)
    offers2 = parse_aviasales(snap, q)

    assert len(offers1) == len(offers2)
    for o1, o2 in zip(offers1, offers2):
        assert compute_offer_id(o1) == compute_offer_id(o2)

    # Офферы offers[1] (42987) и offers[4] (45388) имеют одинаковое плечо туда, но разные обратные рейсы
    o1 = next(o for o in offers1 if o.price == 42987)
    o4 = next(o for o in offers1 if o.price == 45388)
    assert o1.flight_numbers == o4.flight_numbers
    assert o1.depart_at == o4.depart_at
    assert o1.back.flight_numbers != o4.back.flight_numbers
    assert compute_offer_id(o1) != compute_offer_id(o4)


def test_parse_aviasales_roundtrip_skips_tickets_without_two_segments():
    """Билет, у которого нет ровно двух сегментов, при return_date пропускается."""
    q = Query(
        origin="KUF",
        destination="EVN",
        depart=date(2026, 12, 16),
        return_date=date(2026, 12, 20),
    )

    chunk = {
        "chunk_id": "results",
        "last_update_timestamp": 0,
        "flight_legs": [
            {
                "local_departure_date_time": "2026-12-16 10:00",
                "local_arrival_date_time": "2026-12-16 14:00",
                "departure_unix_timestamp": 1700000000,
                "arrival_unix_timestamp": 1700014400,
                "operating_carrier_designator": {"carrier": "SU", "number": "100"},
            },
            {
                "local_departure_date_time": "2026-12-20 10:00",
                "local_arrival_date_time": "2026-12-20 14:00",
                "departure_unix_timestamp": 1700345600,
                "arrival_unix_timestamp": 1700360000,
                "operating_carrier_designator": {"carrier": "SU", "number": "101"},
            },
        ],
        "airlines": {"SU": {"name": {"ru": {"default": "Аэрофлот"}}}},
        "tickets": [
            {
                "id": "t_1_seg",
                "segments": [{"flights": [0]}],
                "proposals": [{"price": {"value": 10000, "currency_code": "RUB"}}],
            },
            {
                "id": "t_3_segs",
                "segments": [{"flights": [0]}, {"flights": [1]}, {"flights": [0]}],
                "proposals": [{"price": {"value": 30000, "currency_code": "RUB"}}],
            },
            {
                "id": "t_2_segs",
                "segments": [{"flights": [0]}, {"flights": [1]}],
                "proposals": [{"price": {"value": 20000, "currency_code": "RUB"}}],
            },
        ],
    }

    snap = make_snapshot([(RESULTS_URL, json.dumps([chunk]))])
    offers = parse_aviasales(snap, q)
    # Только билет с ровно двумя сегментами попал в выдачу
    assert len(offers) == 1
    assert offers[0].price == 20000
    assert offers[0].back is not None


def test_parse_aviasales_roundtrip_deduplication_different_back_legs():
    """В режиме туда-обратно билеты с одинаковым id/вылетом туда, но разными обратными рейсами, не склеиваются."""
    q = Query(
        origin="KUF",
        destination="EVN",
        depart=date(2026, 12, 16),
        return_date=date(2026, 12, 20),
    )

    chunk = {
        "chunk_id": "results",
        "last_update_timestamp": 0,
        "flight_legs": [
            {
                "local_departure_date_time": "2026-12-16 10:00",
                "local_arrival_date_time": "2026-12-16 14:00",
                "departure_unix_timestamp": 1700000000,
                "arrival_unix_timestamp": 1700014400,
                "operating_carrier_designator": {"carrier": "SU", "number": "100"},
            },
            {
                "local_departure_date_time": "2026-12-20 10:00",
                "local_arrival_date_time": "2026-12-20 14:00",
                "departure_unix_timestamp": 1700345600,
                "arrival_unix_timestamp": 1700360000,
                "operating_carrier_designator": {"carrier": "SU", "number": "101"},
            },
            {
                "local_departure_date_time": "2026-12-20 18:00",
                "local_arrival_date_time": "2026-12-20 22:00",
                "departure_unix_timestamp": 1700374400,
                "arrival_unix_timestamp": 1700388800,
                "operating_carrier_designator": {"carrier": "SU", "number": "102"},
            },
        ],
        "airlines": {"SU": {"name": {"ru": {"default": "Аэрофлот"}}}},
        "tickets": [
            {
                "id": "same_ticket_id",
                "signature": "same_sig",
                "segments": [{"flights": [0]}, {"flights": [1]}],
                "proposals": [{"price": {"value": 20000, "currency_code": "RUB"}}],
            },
            {
                "id": "same_ticket_id",
                "signature": "same_sig",
                "segments": [{"flights": [0]}, {"flights": [2]}],
                "proposals": [{"price": {"value": 25000, "currency_code": "RUB"}}],
            },
        ],
    }

    snap = make_snapshot([(RESULTS_URL, json.dumps([chunk]))])
    offers = parse_aviasales(snap, q)
    # Оба оффера сохранились и не склеились
    assert len(offers) == 2
    assert offers[0].price == 20000
    assert offers[0].back.flight_numbers == ["SU 101"]
    assert offers[1].price == 25000
    assert offers[1].back.flight_numbers == ["SU 102"]
