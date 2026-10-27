"""Тесты для avia.offers: хэширование, багаж, простановка id."""
from dataclasses import replace
from pathlib import Path
import sys

_src = str(Path(__file__).resolve().parent.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

import avia
_avia_path = str(Path(_src) / "avia")
if _avia_path not in avia.__path__:
    avia.__path__ = [_avia_path] + list(avia.__path__)

for _mod in ["avia.models", "avia.formatting", "avia.offers", "avia.store"]:
    sys.modules.pop(_mod, None)

import pytest
from avia.models import Leg, Offer, SiteResult
from avia.offers import assign_ids, baggage_state, compute_offer_id, filter_by_baggage


def test_offer_id_stability_and_determinism():
    """Тест стабильности offer_id: одинаков при другой цене и URL,

    разный при другом рейсе/времени, одинаков в разных запусках (сравнение с зашитым sha1).
    """
    o1 = Offer(
        site="aviasales",
        price=15000,
        airline="Aeroflot",
        flight_numbers=["SU 100", "SU 101"],
        depart_at="2026-12-12T10:00:00",
        arrive_at="2026-12-12T14:00:00",
        duration="4 ч",
        transfers=1,
        url="https://example.com/ticket1",
        back=Leg(
            airline="Aeroflot",
            flight_numbers=["SU 102"],
            depart_at="2026-12-20T12:00:00",
            arrive_at="2026-12-20T16:00:00",
            duration="4 ч",
            transfers=0,
        ),
    )

    # Зашитое значение sha1 hex[:12]
    expected_id = "d95575a8fd07"
    assert compute_offer_id(o1) == expected_id

    # Другая цена и URL не влияют на offer_id
    o_diff_price_url = replace(o1, price=99990, currency="USD", url="https://other.com/link")
    assert compute_offer_id(o_diff_price_url) == expected_id

    # Другой рейс дает другой offer_id
    o_diff_flight = replace(o1, flight_numbers=["SU 999"])
    assert compute_offer_id(o_diff_flight) != expected_id

    # Другое время вылета дает другой offer_id
    o_diff_time = replace(o1, depart_at="2026-12-12T11:00:00")
    assert compute_offer_id(o_diff_time) != expected_id

    # Другое обратное плечо дает другой offer_id
    o_diff_back = replace(o1, back=replace(o1.back, duration="5 ч"))
    assert compute_offer_id(o_diff_back) != expected_id

    # Без обратного плеча дает другой offer_id
    o_no_back = replace(o1, back=None)
    assert compute_offer_id(o_no_back) != expected_id


def test_baggage_state_parsing():
    """Тест определения состояния багажа по строке o.baggage."""
    def _make(b: str | None) -> Offer:
        return Offer(site="x", price=1000, baggage=b)

    assert baggage_state(_make("без багажа")) is False
    assert baggage_state(_make("Билет без багажа, только ручная кладь")) is False
    assert baggage_state(_make("с багажом")) is True
    assert baggage_state(_make("Тариф С Багажом 23 кг")) is True
    assert baggage_state(_make(None)) is None
    assert baggage_state(_make("")) is None
    assert baggage_state(_make("ручная кладь 10 кг")) is None


def test_filter_by_baggage_modes_and_counter():
    """Тест фильтрации по багажу: any, with, without, счётчик и сохранение исходных объектов."""
    o_with = Offer(site="x", price=1000, baggage="с багажом")
    o_without = Offer(site="x", price=2000, baggage="без багажа")
    o_none = Offer(site="x", price=3000, baggage=None)

    orig_offers = [o_with, o_without, o_none]
    orig_results = [SiteResult(site="site1", ok=True, offers=orig_offers)]

    # Режим any: не фильтрует ничего, 0 скрыто
    res_any, dropped_any = filter_by_baggage(orig_results, "any")
    assert dropped_any == 0
    assert len(res_any[0].offers) == 3
    assert res_any is not orig_results

    # Режим with: оставляет True и None, отсеивает False
    res_with, dropped_with = filter_by_baggage(orig_results, "with")
    assert dropped_with == 1
    assert res_with[0].offers == [o_with, o_none]

    # Режим without: оставляет False и None, отсеивает True
    res_without, dropped_without = filter_by_baggage(orig_results, "without")
    assert dropped_without == 1
    assert res_without[0].offers == [o_without, o_none]

    # Исходный список не мутирован
    assert len(orig_results[0].offers) == 3

    # Неизвестный режим поднимает ValueError
    with pytest.raises(ValueError):
        filter_by_baggage(orig_results, "invalid_mode")


def test_assign_ids():
    """Тест заполнения offer_id и fetched_at там, где они пусты."""
    o1 = Offer(site="s", price=5000, airline="S7", depart_time="10:00")
    o2 = Offer(site="s", price=6000, airline="S7", depart_time="12:00", offer_id="preset_id", fetched_at=12345.0)

    results = [SiteResult(site="s", ok=True, offers=[o1, o2])]
    assign_ids(results)

    assert o1.offer_id != ""
    assert o1.fetched_at is not None
    # o2 не перезаписывается
    assert o2.offer_id == "preset_id"
    assert o2.fetched_at == 12345.0
