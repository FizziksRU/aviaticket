"""Тесты парсера Купибилет (Kupibilet) для режима туда-обратно."""
from datetime import date
import json
from pathlib import Path
import pytest

from avia.collector import Snapshot
from avia.models import Query
from avia.offers import compute_offer_id
from avia.parsers.kupibilet import parse_kupibilet

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "kupibilet"
SEARCH_URL = "https://api-rs-lb.kupibilet.ru/frontend_search"


def load_fixture_text(name: str = "search_rt_kuf_evn.json") -> str:
    """Загружает содержимое тестовой фикстуры."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_snapshot(payloads: list[tuple[str, str]] | None = None) -> Snapshot:
    """Создает тестовый Snapshot с заданными сетевыми ответами."""
    if payloads is None:
        payloads = [(SEARCH_URL, load_fixture_text("search_rt_kuf_evn.json"))]
    return Snapshot(
        url="https://www.kupibilet.ru/search",
        text="Купибилет поиск туда-обратно",
        json_payloads=payloads,
    )


def test_parse_kupibilet_roundtrip_count_and_min_price():
    """Проверка числа офферов (12), минимальной цены (23829 за оба плеча) и сортировки по цене."""
    q = Query("KUF", "EVN", date(2026, 12, 12), return_date=date(2026, 12, 16))
    snap = make_snapshot()
    offers = parse_kupibilet(snap, q)

    # В фикстуре ровно 12 вариантов туда-обратно
    assert len(offers) == 12
    # Офферы отсортированы по возрастанию цены
    assert [o.price for o in offers] == sorted([o.price for o in offers])

    first = offers[0]
    assert first.price == 23829
    assert first.currency == "RUB"
    assert first.site == "kupibilet"
    assert first.url is None


def test_parse_kupibilet_roundtrip_back_leg_and_flight_numbers():
    """Проверка наличия back: Leg у всех офферов, времени вылета/прилёта и номеров рейсов."""
    q = Query("KUF", "EVN", date(2026, 12, 12), return_date=date(2026, 12, 16))
    snap = make_snapshot()
    offers = parse_kupibilet(snap, q)

    # У каждого оффера есть плечо back с временем и рейсами
    for o in offers:
        assert o.back is not None
        assert o.depart_time
        assert o.arrive_time
        assert o.depart_at
        assert o.arrive_at
        assert o.flight_numbers
        assert len(o.flight_numbers) > 0
        assert o.back.depart_time
        assert o.back.arrive_time
        assert o.back.depart_at
        assert o.back.arrive_at
        assert o.back.flight_numbers
        assert len(o.back.flight_numbers) > 0

    # Проверка деталей первого оффера
    first = offers[0]
    # Плечо туда
    assert first.airline == "Победа"
    assert first.depart_time == "10:25"
    assert first.arrive_time == "20:40"
    assert first.depart_at == "2026-12-12T10:25"
    assert first.arrive_at == "2026-12-12T20:40"
    assert first.duration == "10ч 15м"
    assert first.transfers == 1
    assert first.flight_numbers == ["DP 332", "3F 3350"]
    assert first.aircraft == "Boeing 737-800 (winglets), Airbus A320-100/200"

    # Плечо обратно
    assert first.back.airline == "Россия"
    assert first.back.depart_time == "18:20"
    assert first.back.arrive_time == "21:05"
    assert first.back.depart_at == "2026-12-16T18:20"
    assert first.back.arrive_at == "2026-12-16T21:05"
    assert first.back.duration == "2ч 45м"
    assert first.back.transfers == 0
    assert first.back.flight_numbers == ["SU 6950"]
    assert first.back.aircraft == "Airbus A319"
    assert first.back.layovers is None


def test_parse_kupibilet_roundtrip_layovers():
    """Проверка описания пересадок layovers (город пересадки и время ожидания по datetime)."""
    q = Query("KUF", "EVN", date(2026, 12, 12), return_date=date(2026, 12, 16))
    snap = make_snapshot()
    offers = parse_kupibilet(snap, q)

    # Вариант 0 с пересадкой в Сочи (AER): 12:45 -> 18:30 = 5 ч 45 мин
    first = offers[0]
    assert first.layovers == ["Сочи, 5 ч 45 мин"]

    # Вариант 7 с пересадкой в Москве (SVO): 18:30 -> 21:00 следующего дня (1590 мин = 26 ч 30 мин)
    v7 = next(
        o for o in offers
        if o.flight_numbers == ["DP 6582", "3F 3316"] and o.back.flight_numbers == ["SU 6950"]
    )
    assert v7.layovers == ["Москва, 26 ч 30 мин"]


def test_parse_kupibilet_roundtrip_baggage_and_carry_on():
    """Проверка определения багажа и ручной клади: 'без багажа' при count == 0, carry_on."""
    q = Query("KUF", "EVN", date(2026, 12, 12), return_date=date(2026, 12, 16))
    snap = make_snapshot()
    offers = parse_kupibilet(snap, q)

    # В исходной фикстуре у всех вариантов count == 0 и hand_luggage weight == 0, count == 0
    assert all(o.baggage == "без багажа" for o in offers)
    assert all(o.carry_on == "ручной клади нет" for o in offers)

    # Проверка на синтетически модифицированных данных
    data = json.loads(load_fixture_text())
    # Первый вариант делаем «с багажом» и с ручной кладью по весу
    data["variants"][0]["baggage"] = {"count": 1, "weight": 23}
    data["variants"][0]["hand_luggage"] = {"count": 0, "weight": 10}
    # Второй вариант делаем «без багажа», но с ручной кладью по count
    data["variants"][1]["baggage"] = {"count": 0, "weight": 0}
    data["variants"][1]["hand_luggage"] = {"count": 1, "weight": 0}

    snap_mod = make_snapshot([(SEARCH_URL, json.dumps(data))])
    offers_mod = parse_kupibilet(snap_mod, q)

    o0 = next(o for o in offers_mod if o.price == 23829)
    assert o0.baggage == "с багажом"
    assert o0.carry_on == "ручная кладь есть"

    o1 = next(o for o in offers_mod if o.price == 24212)
    assert o1.baggage == "без багажа"
    assert o1.carry_on == "ручная кладь есть"


def test_parse_kupibilet_oneway_query_without_return_date():
    """Тот же файл фикстуры при Query без return_date возвращает офферы без back."""
    q_oneway = Query("KUF", "EVN", date(2026, 12, 12))
    snap = make_snapshot()
    offers = parse_kupibilet(snap, q_oneway)

    assert len(offers) == 12
    for o in offers:
        assert o.back is None
        assert o.depart_time
        assert o.arrive_time
        assert o.flight_numbers
        assert o.depart_at
        assert o.arrive_at
        assert o.carry_on == "ручной клади нет"

    # Минимальная цена и данные первого плеча сохраняются
    first = offers[0]
    assert first.price == 23829
    assert first.flight_numbers == ["DP 332", "3F 3350"]
    assert first.depart_at == "2026-12-12T10:25"
    assert first.arrive_at == "2026-12-12T20:40"


def test_parse_kupibilet_roundtrip_stable_offer_id():
    """offer_id стабилен при повторном разборе и различается у офферов с разными обратными рейсами."""
    q = Query("KUF", "EVN", date(2026, 12, 12), return_date=date(2026, 12, 16))
    snap = make_snapshot()
    offers1 = parse_kupibilet(snap, q)
    offers2 = parse_kupibilet(snap, q)

    assert len(offers1) == len(offers2)
    for o1, o2 in zip(offers1, offers2):
        assert compute_offer_id(o1) == compute_offer_id(o2)

    # Офферы offers[0] (23829) и offers[1] (24212) имеют одинаковое плечо туда, но разные обратные рейсы
    o0 = next(o for o in offers1 if o.price == 23829)
    o1 = next(o for o in offers1 if o.price == 24212)
    assert o0.flight_numbers == o1.flight_numbers
    assert o0.depart_at == o1.depart_at
    assert o0.back.flight_numbers != o1.back.flight_numbers
    assert compute_offer_id(o0) != compute_offer_id(o1)


def test_parse_kupibilet_roundtrip_skips_variants_without_two_segments():
    """Варианты без ровно двух сегментов или с поездами при return_date пропускаются."""
    q = Query("KUF", "EVN", date(2026, 12, 12), return_date=date(2026, 12, 16))
    data = json.loads(load_fixture_text())

    # Вариант с 1 сегментом вместо 2
    data["variants"].append(data["variants"][0].copy())
    data["variants"][-1]["id"] = "synthetic_one_segment"
    data["variants"][-1]["segments"] = [data["variants"][0]["segments"][0]]

    # Вариант с 3 сегментами
    data["variants"].append(data["variants"][0].copy())
    data["variants"][-1]["id"] = "synthetic_three_segments"
    data["variants"][-1]["segments"] = [
        data["variants"][0]["segments"][0],
        data["variants"][0]["segments"][1],
        data["variants"][0]["segments"][0],
    ]

    # Вариант с поездом в обратном сегменте
    flight_id = data["variants"][0]["segments"][1]["flights"][0]
    data["flights"][flight_id]["transport_kind"] = "train"

    snap = make_snapshot([(SEARCH_URL, json.dumps(data))])
    offers = parse_kupibilet(snap, q)

    # Вариант с поездом в seg1 отфильтрован (этот обратный перелёт используют 10 из 12 вариантов)
    # И синтетические с 1 и 3 сегментами тоже пропущены
    assert all(o.site == "kupibilet" for o in offers)
    for o in offers:
        assert o.back is not None
        assert o.back.flight_numbers != ["SU 6950"]


def test_parse_kupibilet_roundtrip_deduplication():
    """Билеты с одинаковым id/вылетом туда, но разными обратными рейсами, не склеиваются."""
    q = Query("KUF", "EVN", date(2026, 12, 12), return_date=date(2026, 12, 16))
    data = json.loads(load_fixture_text())

    # Берём варианты 0 и 1 (одинаковое туда, разное обратно) и даём им одинаковый id
    data["variants"] = [data["variants"][0].copy(), data["variants"][1].copy()]
    data["variants"][0]["id"] = "same_variant_id"
    data["variants"][1]["id"] = "same_variant_id"

    snap = make_snapshot([(SEARCH_URL, json.dumps(data))])
    offers = parse_kupibilet(snap, q)

    # Оба оффера сохранились, так как обратные рейсы различаются
    assert len(offers) == 2
    assert offers[0].price == 23829
    assert offers[0].back.flight_numbers == ["SU 6950"]
    assert offers[1].price == 24212
    assert offers[1].back.flight_numbers == ["3F 3383"]

    # Теперь добавим полный дубликат первого варианта с более высокой ценой
    dup_expensive = data["variants"][0].copy()
    dup_expensive["price"] = {"amount": "99999", "currency": "RUB"}
    data["variants"].append(dup_expensive)

    snap2 = make_snapshot([(SEARCH_URL, json.dumps(data))])
    offers2 = parse_kupibilet(snap2, q)

    # Дубликат склеился, остался более дешёвый (цена 23829, а не 99999)
    assert len(offers2) == 2
    assert offers2[0].price == 23829
