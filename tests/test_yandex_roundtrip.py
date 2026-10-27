"""Тесты парсера Яндекс Путешествий для режима туда-обратно (roundtrip)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from avia.collector import Snapshot
from avia.models import Query
from avia.offers import compute_offer_id
from avia.parsers.yandex_travel import parse_yandex_travel

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "yandex_travel"

URL_PORTION_A = "https://travel.yandex.ru/api/avia/search/results?qid=test_rt_qid&cont=1"
URL_PORTION_B = "https://travel.yandex.ru/api/avia/search/results?qid=test_rt_qid&cont=2"
URL_PORTION_FINAL = "https://travel.yandex.ru/api/avia/search/results?qid=test_rt_qid"


def load_fixture_text(name: str) -> str:
    """Загружает содержимое тестовой фикстуры Яндекс Путешествий."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_roundtrip_snapshot(payloads: list[tuple[str, str]] | None = None) -> Snapshot:
    """Создаёт тестовый Snapshot туда-обратно Самара -> Ереван."""
    if payloads is None:
        payloads = [
            (URL_PORTION_A, load_fixture_text("results_rt_a_kuf_evn.json")),
            (URL_PORTION_B, load_fixture_text("results_rt_b_kuf_evn.json")),
            (URL_PORTION_FINAL, load_fixture_text("results_rt_final_kuf_evn.json")),
        ]
    return Snapshot(
        url="https://travel.yandex.ru/avia/search/result/?adult_seats=1&fromId=c51&toId=c10262&when=2026-12-16&return_when=2026-12-22",
        text="Яндекс Путешествия авиабилеты туда-обратно",
        json_payloads=payloads,
    )


def test_roundtrip_combined_offers_count_and_min_price():
    """Проверка объединения трёх порций: 20 офферов, мин. цена 38022, сортировка по возрастанию."""
    q = Query("KUF", "EVN", date(2026, 12, 16), return_date=date(2026, 12, 22))
    snap = make_roundtrip_snapshot()
    offers = parse_yandex_travel(snap, q)

    # 8 тарифов из порции A + 6 тарифов из порции B + 6 тарифов из финальной порции = 20 офферов
    assert len(offers) == 20

    # Минимальная цена среди всех тарифов трёх фикстур — 38022
    assert offers[0].price == 38022

    # Проверка упорядоченности по цене
    assert [o.price for o in offers] == sorted([o.price for o in offers])
    assert all(o.currency == "RUB" for o in offers)
    assert all(o.site == "yandex_travel" for o in offers)


def test_roundtrip_back_leg_fields():
    """У каждого оффера заполнено обратное плечо back: depart_time, arrive_time, flight_numbers, depart_at, arrive_at."""
    q = Query("KUF", "EVN", date(2026, 12, 16), return_date=date(2026, 12, 22))
    snap = make_roundtrip_snapshot()
    offers = parse_yandex_travel(snap, q)

    assert len(offers) == 20
    for o in offers:
        assert o.back is not None
        assert o.back.depart_time is not None and len(o.back.depart_time) == 5
        assert o.back.arrive_time is not None and len(o.back.arrive_time) == 5
        assert o.back.flight_numbers is not None and len(o.back.flight_numbers) > 0
        assert o.back.depart_at is not None and "T" in o.back.depart_at
        assert o.back.arrive_at is not None and "T" in o.back.arrive_at

        # Плечо туда также должно содержать номера рейсов и ISO-даты
        assert o.flight_numbers is not None and len(o.flight_numbers) > 0
        assert o.depart_at is not None and "T" in o.depart_at
        assert o.arrive_at is not None and "T" in o.arrive_at


def test_roundtrip_layovers():
    """Проверка формирования layovers: город пересадки и время ожидания."""
    q = Query("KUF", "EVN", date(2026, 12, 16), return_date=date(2026, 12, 22))
    snap = make_roundtrip_snapshot()
    offers = parse_yandex_travel(snap, q)

    # В обратном плече всех вариантов ровно 2 рейса (1 пересадка в Минеральных Водах)
    for o in offers:
        assert o.back is not None
        assert o.back.transfers == 1
        assert o.back.layovers is not None
        assert len(o.back.layovers) == 1
        assert o.back.layovers[0] in ("Минеральные Воды, 7 ч 15 мин", "Минеральные Воды, 14 ч 55 мин")

    # Для вариантов с прямым вылетом туда (рейс FV 6949) layovers туда должен быть None
    direct_out = [o for o in offers if o.transfers == 0]
    assert len(direct_out) > 0
    for o in direct_out:
        assert o.flight_numbers in (["FV 6949"], ["3F 3384"])
        assert o.layovers is None

    # Для вариантов с пересадкой туда (рейсы Азимута через Минеральные Воды) layovers заполнен
    transfer_out = [o for o in offers if o.transfers == 1]
    assert len(transfer_out) > 0
    for o in transfer_out:
        assert o.layovers is not None
        assert len(o.layovers) == 1
        assert o.layovers[0] in ("Минеральные Воды, 14 ч 40 мин", "Минеральные Воды, 22 ч 40 мин")


def test_roundtrip_baggage_all_zero_codes():
    """Багаж 'без багажа', когда все коды '0p0p0p'."""
    q = Query("KUF", "EVN", date(2026, 12, 16), return_date=date(2026, 12, 22))
    snap_a = make_roundtrip_snapshot([(URL_PORTION_A, load_fixture_text("results_rt_a_kuf_evn.json"))])
    offers_a = parse_yandex_travel(snap_a, q)

    # В порции A все минимальные тарифы имеют коды '0p0p0p' на всех рейсах обоих плеч
    assert len(offers_a) == 8
    assert all(o.baggage == "без багажа" for o in offers_a)
    assert all(o.carry_on == "ручная кладь нет" for o in offers_a)

    # В финальной порции у тарифов 0-3 норма багажа '0d1d23d' (багаж 0 мест, ручная кладь 1 место 23 кг)
    snap_final = make_roundtrip_snapshot([(URL_PORTION_FINAL, load_fixture_text("results_rt_final_kuf_evn.json"))])
    offers_final = parse_yandex_travel(snap_final, q)
    assert len(offers_final) == 6
    assert offers_final[0].baggage == "без багажа"
    assert offers_final[0].carry_on == "ручная кладь есть"


def test_oneway_query_no_back():
    """Оффер без back при Query без return_date (оба плеча одной фикстуры — туда-плечо)."""
    q_oneway = Query("KUF", "EVN", date(2026, 12, 16))
    snap_final = make_roundtrip_snapshot([(URL_PORTION_FINAL, load_fixture_text("results_rt_final_kuf_evn.json"))])
    offers = parse_yandex_travel(snap_final, q_oneway)

    # Все 6 тарифов фикстуры возвращаются как офферы без обратного плеча
    assert len(offers) == 6
    assert all(o.back is None for o in offers)
    for o in offers:
        assert o.depart_time is not None
        assert o.arrive_time is not None
        assert o.flight_numbers is not None and len(o.flight_numbers) > 0
        assert o.depart_at is not None and "T" in o.depart_at
        assert o.arrive_at is not None and "T" in o.arrive_at


def test_compute_offer_id_stability_and_distinct_back():
    """compute_offer_id стабилен при повторном разборе и различается у офферов с разными обратными рейсами."""
    q = Query("KUF", "EVN", date(2026, 12, 16), return_date=date(2026, 12, 22))
    snap = make_roundtrip_snapshot()

    offers_run1 = parse_yandex_travel(snap, q)
    offers_run2 = parse_yandex_travel(snap, q)

    ids1 = [compute_offer_id(o) for o in offers_run1]
    ids2 = [compute_offer_id(o) for o in offers_run2]

    # Стабильность при повторном разборе
    assert ids1 == ids2
    assert len(ids1) == 20

    # Проверка различия хэшей для офферов с одинаковым вылетом туда (FV 6949), но разными обратными рейсами
    fv_offers = [o for o in offers_run1 if o.flight_numbers == ["FV 6949"]]
    assert len(fv_offers) >= 2
    # Находим два оффера с разными обратными рейсами
    o1, o2 = None, None
    for i in range(len(fv_offers)):
        for j in range(i + 1, len(fv_offers)):
            if fv_offers[i].back and fv_offers[j].back and fv_offers[i].back.flight_numbers != fv_offers[j].back.flight_numbers:
                o1, o2 = fv_offers[i], fv_offers[j]
                break
        if o1 is not None:
            break

    assert o1 is not None and o2 is not None
    assert o1.back is not None and o2.back is not None
    assert o1.back.flight_numbers != o2.back.flight_numbers
    assert compute_offer_id(o1) != compute_offer_id(o2)
