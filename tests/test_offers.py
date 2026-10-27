import pytest

from avia.models import Leg, Offer, SiteResult
from avia.offers import assign_ids, baggage_state, compute_offer_id, filter_by_baggage


def test_compute_offer_id_stability_and_hardcoded_value():
    """offer_id стабилен, не зависит от цены и ссылки, меняется при других рейсах и совпадает с зашитым значением."""
    o1 = Offer(
        site="aviasales",
        price=15000,
        airline="Aeroflot",
        depart_time="10:00",
        arrive_time="14:00",
        duration="4 ч",
        transfers=0,
        flight_numbers=["SU 100"],
        url="https://example.com/ticket1",
    )
    # Зашитое значение 12-символьного hex
    assert compute_offer_id(o1) == "64190d33f719"

    # Изменение цены и ссылки не влияет на offer_id
    o2 = Offer(
        site="aviasales",
        price=99999,
        airline="Aeroflot",
        depart_time="10:00",
        arrive_time="14:00",
        duration="4 ч",
        transfers=0,
        flight_numbers=["SU 100"],
        url="https://other.com/ticket2",
    )
    assert compute_offer_id(o2) == compute_offer_id(o1)

    # Изменение времени или рейса меняет offer_id
    o3 = Offer(
        site="aviasales",
        price=15000,
        airline="Aeroflot",
        depart_time="10:05",
        arrive_time="14:00",
        duration="4 ч",
        transfers=0,
        flight_numbers=["SU 100"],
    )
    assert compute_offer_id(o3) != compute_offer_id(o1)

    o4 = Offer(
        site="aviasales",
        price=15000,
        airline="Aeroflot",
        depart_time="10:00",
        arrive_time="14:00",
        duration="4 ч",
        transfers=0,
        flight_numbers=["SU 102"],
    )
    assert compute_offer_id(o4) != compute_offer_id(o1)

    # Добавление обратного плеча также изменяет offer_id и совпадает с зашитым значением
    o_round = Offer(
        site="aviasales",
        price=15000,
        airline="Aeroflot",
        depart_time="10:00",
        arrive_time="14:00",
        duration="4 ч",
        transfers=0,
        flight_numbers=["SU 100"],
        url="https://example.com/1",
        back=Leg(
            airline="Aeroflot",
            depart_time="18:00",
            arrive_time="22:00",
            duration="4 ч",
            transfers=0,
            flight_numbers=["SU 101"],
        ),
    )
    assert compute_offer_id(o_round) == "daee28010c28"
    assert compute_offer_id(o_round) != compute_offer_id(o1)


def test_baggage_state_parsing():
    """Проверка определения состояния багажа: без багажа, с багажом, неизвестно."""
    assert baggage_state(Offer("site", 1000, baggage="Только без багажа")) is False
    assert baggage_state(Offer("site", 1000, baggage="Тариф Без Багажа (ручная кладь 5 кг)")) is False
    assert baggage_state(Offer("site", 1000, baggage="Билет с багажом 23 кг")) is True
    assert baggage_state(Offer("site", 1000, baggage="С Багажом")) is True
    assert baggage_state(Offer("site", 1000, baggage="Ручная кладь 10 кг")) is None
    assert baggage_state(Offer("site", 1000, baggage=None)) is None
    assert baggage_state(Offer("site", 1000, baggage="")) is None


def test_filter_by_baggage_all_modes_and_counter():
    """Проверка фильтрации во всех режимах (any, with, without) с сохранением None и счётчиком отсеянных."""
    o_with = Offer("s", 1000, baggage="с багажом")
    o_without = Offer("s", 2000, baggage="без багажа")
    o_none = Offer("s", 3000, baggage=None)
    o_other = Offer("s", 4000, baggage="ручная кладь 5кг")

    orig_offers = [o_with, o_without, o_none, o_other]
    results = [SiteResult("site1", True, orig_offers)]

    # 1. Режим any: не фильтрует ничего, dropped=0, исходные объекты не мутируются
    res_any, dropped_any = filter_by_baggage(results, "any")
    assert dropped_any == 0
    assert len(res_any[0].offers) == 4
    assert res_any[0] is not results[0]
    assert results[0].offers == orig_offers

    # 2. Режим with: оставляет с багажом (True) и неизвестно (None), отсеивает без багажа (False)
    res_with, dropped_with = filter_by_baggage(results, "with")
    assert dropped_with == 1
    assert len(res_with[0].offers) == 3
    assert o_with in res_with[0].offers
    assert o_none in res_with[0].offers
    assert o_other in res_with[0].offers
    assert o_without not in res_with[0].offers
    # Исходный список не мутирован
    assert len(results[0].offers) == 4

    # 3. Режим without: оставляет без багажа (False) и неизвестно (None), отсеивает с багажом (True)
    res_without, dropped_without = filter_by_baggage(results, "without")
    assert dropped_without == 1
    assert len(res_without[0].offers) == 3
    assert o_without in res_without[0].offers
    assert o_none in res_without[0].offers
    assert o_other in res_without[0].offers
    assert o_with not in res_without[0].offers
    assert len(results[0].offers) == 4

    # Некорректный режим
    with pytest.raises(ValueError):
        filter_by_baggage(results, "invalid_mode")


def test_assign_ids():
    """assign_ids заполняет offer_id и fetched_at там, где они не заданы."""
    o1 = Offer("site1", 1000, airline="SU", depart_time="10:00")
    o2 = Offer("site1", 2000, airline="S7", depart_time="12:00", offer_id="preset_id", fetched_at=12345.0)

    res = [SiteResult("site1", True, [o1, o2])]
    fixed_time = 1700000000.0
    assign_ids(res, now=fixed_time)

    assert o1.offer_id != ""
    assert o1.fetched_at == fixed_time

    # Заданные ранее значения не перезаписаны
    assert o2.offer_id == "preset_id"
    assert o2.fetched_at == 12345.0
