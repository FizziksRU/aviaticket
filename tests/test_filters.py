from __future__ import annotations

from avia.criteria import SearchCriteria
from avia.filters import (
    apply_criteria,
    balance_score,
    matches,
    pareto_front,
    parse_duration_minutes,
    pick_cards,
    total_duration_minutes,
)
from avia.models import Leg, Offer, SiteResult


def test_parse_duration_minutes():
    # Реальные форматы парсеров и фикстур
    assert parse_duration_minutes("5 ч 30 м") == 330
    assert parse_duration_minutes("5ч") == 300
    assert parse_duration_minutes("5 ч") == 300
    assert parse_duration_minutes("4ч 20м") == 260
    assert parse_duration_minutes("4ч 00м") == 240
    assert parse_duration_minutes("4 ч 00 мин") == 240
    assert parse_duration_minutes("2 д 3 ч") == 3060
    assert parse_duration_minutes("2 д 3 ч 15 м") == 3075
    assert parse_duration_minutes("45 мин") == 45
    assert parse_duration_minutes("45м") == 45
    assert parse_duration_minutes("1 д") == 1440
    assert parse_duration_minutes("26ч 40м") == 1600

    # Непонятные / некорректные форматы возвращают None
    assert parse_duration_minutes(None) is None
    assert parse_duration_minutes("") is None
    assert parse_duration_minutes("   ") is None
    assert parse_duration_minutes("непонятно") is None
    assert parse_duration_minutes("123") is None
    assert parse_duration_minutes(123) is None  # type: ignore[arg-type]


def test_default_criteria_does_not_change_results():
    c = SearchCriteria()
    assert c.is_default()

    o1 = Offer(site="s1", price=10000, duration="4ч", transfers=1, depart_time="10:00")
    o2 = Offer(site="s1", price=20000, duration="12ч", transfers=2, depart_time="23:00")
    res = [SiteResult(site="s1", ok=True, offers=[o1, o2])]

    assert matches(o1, c) is True
    assert matches(o2, c) is True

    filtered, dropped = apply_criteria(res, c)
    assert dropped == 0
    assert filtered == res
    assert filtered[0].offers == [o1, o2]


def test_direct_flights_filter():
    crit_direct = SearchCriteria(max_transfers=0)
    crit_one_transfer = SearchCriteria(max_transfers=1)

    direct = Offer(site="s", price=10000, transfers=0)
    one_stop = Offer(site="s", price=8000, transfers=1)
    two_stops = Offer(site="s", price=6000, transfers=2)
    no_transfers_info = Offer(site="s", price=5000, transfers=None)

    assert matches(direct, crit_direct) is True
    assert matches(one_stop, crit_direct) is False
    assert matches(two_stops, crit_direct) is False
    # Билет без данных не отбрасывается
    assert matches(no_transfers_info, crit_direct) is True

    assert matches(direct, crit_one_transfer) is True
    assert matches(one_stop, crit_one_transfer) is True
    assert matches(two_stops, crit_one_transfer) is False
    assert matches(no_transfers_info, crit_one_transfer) is True


def test_time_limit_filter():
    crit = SearchCriteria(max_duration_hours=8)  # 480 минут

    fast = Offer(site="s", price=10000, duration="4ч 00м")
    exact = Offer(site="s", price=10000, duration="8ч 00м")
    slow = Offer(site="s", price=10000, duration="8ч 01м")
    unknown = Offer(site="s", price=10000, duration=None)

    assert matches(fast, crit) is True
    assert matches(exact, crit) is True
    assert matches(slow, crit) is False
    assert matches(unknown, crit) is True


def test_parts_of_day_and_exact_boundaries():
    # Границы:
    # "night" 00:00–05:59
    # "morning" 06:00–11:59
    # "day" 12:00–17:59
    # "evening" 18:00–23:59

    crit_night = SearchCriteria(depart_parts={"night"})
    crit_morning = SearchCriteria(depart_parts={"morning"})
    crit_day = SearchCriteria(depart_parts={"day"})
    crit_evening = SearchCriteria(depart_parts={"evening"})

    # 05:59 (night) vs 06:00 (morning)
    o_0559 = Offer(site="s", price=1000, depart_time="05:59")
    o_0600 = Offer(site="s", price=1000, depart_time="06:00")
    assert matches(o_0559, crit_night) is True
    assert matches(o_0559, crit_morning) is False
    assert matches(o_0600, crit_night) is False
    assert matches(o_0600, crit_morning) is True

    # 11:59 (morning) vs 12:00 (day)
    o_1159 = Offer(site="s", price=1000, depart_time="11:59")
    o_1200 = Offer(site="s", price=1000, depart_time="12:00")
    assert matches(o_1159, crit_morning) is True
    assert matches(o_1159, crit_day) is False
    assert matches(o_1200, crit_morning) is False
    assert matches(o_1200, crit_day) is True

    # 17:59 (day) vs 18:00 (evening)
    o_1759 = Offer(site="s", price=1000, depart_time="17:59")
    o_1800 = Offer(site="s", price=1000, depart_time="18:00")
    assert matches(o_1759, crit_day) is True
    assert matches(o_1759, crit_evening) is False
    assert matches(o_1800, crit_day) is False
    assert matches(o_1800, crit_evening) is True

    # 23:59 (evening) vs 00:00 (night)
    o_2359 = Offer(site="s", price=1000, depart_time="23:59")
    o_0000 = Offer(site="s", price=1000, depart_time="00:00")
    assert matches(o_2359, crit_evening) is True
    assert matches(o_2359, crit_night) is False
    assert matches(o_0000, crit_evening) is False
    assert matches(o_0000, crit_night) is True

    # Fallback на depart_at (ISO) при отсутствии depart_time
    o_iso_morning = Offer(site="s", price=1000, depart_at="2026-12-10T09:30:00")
    assert matches(o_iso_morning, crit_morning) is True
    assert matches(o_iso_morning, crit_evening) is False

    # arrive_parts
    crit_arr_day = SearchCriteria(arrive_parts={"day"})
    o_arr_day = Offer(site="s", price=1000, arrive_time="14:00")
    o_arr_night = Offer(site="s", price=1000, arrive_time="03:00")
    assert matches(o_arr_day, crit_arr_day) is True
    assert matches(o_arr_night, crit_arr_day) is False


def test_ticket_without_data_passes():
    # Если у билета нет данных для проверки критерия, он НЕ отбрасывается
    empty_offer = Offer(site="s", price=10000)
    strict_crit = SearchCriteria(
        max_transfers=0,
        max_duration_hours=2,
        depart_parts={"morning"},
        arrive_parts={"night"},
    )
    assert matches(empty_offer, strict_crit) is True


def test_roundtrip_checks_both_legs():
    # Для туда-обратно max_transfers и max_duration применяются к обоим плечам,
    # части суток — только к плечу «туда».
    crit_direct = SearchCriteria(max_transfers=0)
    crit_dur = SearchCriteria(max_duration_hours=5)
    crit_depart = SearchCriteria(depart_parts={"morning"})

    # Transfers check
    rt_both_direct = Offer(site="s", price=10000, transfers=0, back=Leg(transfers=0))
    rt_back_stop = Offer(site="s", price=10000, transfers=0, back=Leg(transfers=1))
    rt_out_stop = Offer(site="s", price=10000, transfers=1, back=Leg(transfers=0))

    assert matches(rt_both_direct, crit_direct) is True
    assert matches(rt_back_stop, crit_direct) is False
    assert matches(rt_out_stop, crit_direct) is False

    # Duration check (каждое плечо проверяется отдельно)
    rt_dur_ok = Offer(site="s", price=10000, duration="4ч", back=Leg(duration="4ч"))
    rt_dur_back_long = Offer(site="s", price=10000, duration="4ч", back=Leg(duration="6ч"))
    assert matches(rt_dur_ok, crit_dur) is True
    assert matches(rt_dur_back_long, crit_dur) is False

    # Части суток только к плечу «туда»
    # Вылет туда утром (09:00), обратно вечером (20:00) -> подходит под morning
    rt_depart_ok = Offer(
        site="s", price=10000, depart_time="09:00", back=Leg(depart_time="20:00")
    )
    assert matches(rt_depart_ok, crit_depart) is True

    # Вылет туда вечером (20:00), обратно утром (09:00) -> отбрасывается
    rt_depart_fail = Offer(
        site="s", price=10000, depart_time="20:00", back=Leg(depart_time="09:00")
    )
    assert matches(rt_depart_fail, crit_depart) is False


def test_apply_criteria():
    crit = SearchCriteria(max_transfers=0)
    o1 = Offer(site="s1", price=10000, transfers=0)
    o2 = Offer(site="s1", price=12000, transfers=1)
    res = [SiteResult(site="s1", ok=True, offers=[o1, o2])]

    filtered, dropped = apply_criteria(res, crit)
    assert dropped == 1
    assert len(filtered[0].offers) == 1
    assert filtered[0].offers[0] == o1
    # Исходный объект не изменился
    assert len(res[0].offers) == 2


def test_pareto_front():
    # Доминирование по (цена, общее время в пути):
    # o1: 10000 руб, 5ч (300 мин)
    # o2: 12000 руб, 6ч (360 мин) -> доминируется o1 (дороже и дольше)
    # o3: 15000 руб, 3ч (180 мин) -> не доминируется (дороже, но быстрее)
    # o4: 20000 руб, время неизвестно -> не отбрасывается и не доминирует других
    o1 = Offer(site="s", price=10000, duration="5ч 00м")
    o2 = Offer(site="s", price=12000, duration="6ч 00м")
    o3 = Offer(site="s", price=15000, duration="3ч 00м")
    o4 = Offer(site="s", price=20000, duration=None)

    front = pareto_front([o2, o4, o1, o3])
    # o2 отброшен, o1, o3, o4 остались; порядок по цене
    assert front == [o1, o3, o4]


def test_total_duration_minutes():
    # Для туда-обратно сумма плеч, если время известно у обоих, иначе None
    o_oneway = Offer(site="s", price=1000, duration="4ч 30м")
    assert total_duration_minutes(o_oneway) == 270

    o_rt_both = Offer(site="s", price=1000, duration="4ч", back=Leg(duration="3ч 30м"))
    assert total_duration_minutes(o_rt_both) == 450

    o_rt_one_missing = Offer(site="s", price=1000, duration="4ч", back=Leg(duration=None))
    assert total_duration_minutes(o_rt_one_missing) is None


def test_balance_score():
    # balance_score = цена + часы в пути * hour_cost + 500 * число пересадок
    # 10000 + (2.5 * 1000) + (500 * 1) = 10000 + 2500 + 500 = 13000.0
    o = Offer(site="s", price=10000, duration="2ч 30м", transfers=1)
    assert balance_score(o, 1000) == 13000.0

    # Без данных по длительности и пересадкам слагаемые = 0
    o_empty = Offer(site="s", price=10000)
    assert balance_score(o_empty, 1000) == 10000.0


def test_pick_cards_cheapest_and_fastest_same():
    # Карточки: дешёвый и быстрый совпали -> ровно одна карточка
    o1 = Offer(site="s", price=10000, duration="3ч")
    o2 = Offer(site="s", price=15000, duration="5ч")

    cards = pick_cards([o1, o2], SearchCriteria())
    assert len(cards) == 1
    assert cards[0] == ("Самый дешёвый", o1)


def test_pick_cards_different_cheapest_and_fastest():
    o_cheap = Offer(site="s", price=10000, duration="6ч")
    o_fast = Offer(site="s", price=15000, duration="2ч")

    cards = pick_cards([o_cheap, o_fast], SearchCriteria())
    assert len(cards) == 2
    assert cards[0] == ("Самый дешёвый", o_cheap)
    assert cards[1] == ("Самый быстрый", o_fast)


def test_pick_cards_balance_only_when_hour_cost_set():
    o_cheap = Offer(site="s", price=10000, duration="10ч", transfers=2)
    o_fast = Offer(site="s", price=30000, duration="2ч", transfers=0)
    o_balance = Offer(site="s", price=14000, duration="3ч", transfers=0)

    # Без hour_cost карточка баланса не добавляется
    cards_no_balance = pick_cards([o_cheap, o_fast, o_balance], SearchCriteria())
    assert len(cards_no_balance) == 2
    assert cards_no_balance[0] == ("Самый дешёвый", o_cheap)
    assert cards_no_balance[1] == ("Самый быстрый", o_fast)

    # При hour_cost=1000:
    # o_cheap: 10000 + 10*1000 + 500*2 = 21000
    # o_fast: 30000 + 2*1000 + 0 = 32000
    # o_balance: 14000 + 3*1000 + 0 = 17000 (лучший баланс)
    crit = SearchCriteria(hour_cost=1000)
    cards_with_balance = pick_cards([o_cheap, o_fast, o_balance], crit)
    assert len(cards_with_balance) == 3
    assert cards_with_balance[0] == ("Самый дешёвый", o_cheap)
    assert cards_with_balance[1] == ("Самый быстрый", o_fast)
    assert cards_with_balance[2] == ("Лучший баланс", o_balance)
