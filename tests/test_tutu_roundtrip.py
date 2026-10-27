"""Тесты парсера Туту (tutu) для режима туда-обратно (roundtrip)."""
from datetime import date
import json
from pathlib import Path
import pytest

from avia.collector import Snapshot
from avia.models import Offer, Query
from avia.offers import compute_offer_id
from avia.parsers.tutu import parse_tutu

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "tutu"
API_URL = "https://offers-api.tutu.ru/avia/offers/v2"


def load_fixture_text(name: str) -> str:
    """Загружает содержимое тестовой фикстуры."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_roundtrip_snapshot(payloads: list[tuple[str, str]] | None = None) -> Snapshot:
    """Создаёт тестовый Snapshot Туту для маршрута туда-обратно Самара -> Ереван."""
    if payloads is None:
        payloads = [(API_URL, load_fixture_text("offers_rt_a_kuf_evn.json"))]
    return Snapshot(
        url="https://avia.tutu.ru/offers/?class=Y&travelers=1&route[0]=74-16122026-236&route[1]=236-20122026-74",
        text="Туту поиск авиабилетов туда-обратно",
        json_payloads=payloads,
    )


def test_roundtrip_fixture_a_count_prices_and_fields():
    """Проверка первой фикстуры (A): 5 офферов, точные цены, обратное плечо, рейсы, пересадки и багаж."""
    q = Query("KUF", "EVN", date(2026, 12, 16), return_date=date(2026, 12, 20))
    snap = make_roundtrip_snapshot([(API_URL, load_fixture_text("offers_rt_a_kuf_evn.json"))])
    offers = parse_tutu(snap, q)

    # Ровно 5 офферов в первой фикстуре
    assert len(offers) == 5

    # Цены за оба плеча проверены по amount // 100 из фикстуры и отсортированы по возрастанию:
    # 5164100 // 100 = 51641
    # 6078800 // 100 = 60788
    # 6614400 // 100 = 66144
    # 6661200 // 100 = 66612
    # 7223700 // 100 = 72237
    expected_prices = [51641, 60788, 66144, 66612, 72237]
    assert [o.price for o in offers] == expected_prices
    assert [o.price for o in offers] == sorted([o.price for o in offers])
    assert all(o.currency == "RUB" for o in offers)
    assert all(o.site == "tutu" for o in offers)

    # Все предложения в этой фикстуре имеют тариф «без багажа» (в самом дешёвом варианте)
    assert all(o.baggage == "без багажа" for o in offers)
    # Ручная кладь явно указана в условиях всех тарифов
    assert all(o.carry_on == "ручная кладь есть" for o in offers)

    # У каждого оффера должно быть заполнено обратное плечо back: Leg и номера рейсов
    for o in offers:
        assert o.back is not None
        assert o.depart_time is not None and len(o.depart_time) == 5
        assert o.arrive_time is not None and len(o.arrive_time) == 5
        assert o.depart_at is not None and "T" in o.depart_at
        assert o.arrive_at is not None and "T" in o.arrive_at
        assert o.flight_numbers is not None and len(o.flight_numbers) > 0

        assert o.back.depart_time is not None and len(o.back.depart_time) == 5
        assert o.back.arrive_time is not None and len(o.back.arrive_time) == 5
        assert o.back.depart_at is not None and "T" in o.back.depart_at
        assert o.back.arrive_at is not None and "T" in o.back.arrive_at
        assert o.back.flight_numbers is not None and len(o.back.flight_numbers) > 0

        # Прямая ссылка pinLink на avia.tutu.ru
        assert o.url is not None and o.url.startswith("https://avia.tutu.ru")

    # Первое предложение (самое дешёвое, Азимут + Аэрофлот)
    first = offers[0]
    assert first.price == 51641
    assert first.airline == "Азимут"
    assert first.depart_time == "14:40"
    assert first.arrive_time == "17:20"
    assert first.duration == "26ч 40м"
    assert first.transfers == 1
    assert first.flight_numbers == ["A4 6308", "A4 6027"]
    assert first.aircraft == "Sukhoi Superjet 100-95"
    assert first.layovers == ["Минеральные Воды, 22 ч 40 мин"]

    assert first.back is not None
    assert first.back.airline == "Аэрофлот"
    assert first.back.depart_time == "00:15"
    assert first.back.arrive_time == "09:25"
    assert first.back.duration == "9ч 10м"
    assert first.back.transfers == 1
    assert first.back.flight_numbers == ["SU 1869", "SU 1602"]
    assert first.back.aircraft is None
    assert first.back.layovers == ["Москва, 3 ч 55 мин"]

    # Второе предложение (прямой вылет туда авиакомпанией Россия, обратно Utair с пересадкой)
    second = offers[1]
    assert second.price == 60788
    assert second.airline == "Россия"
    assert second.transfers == 0
    assert second.flight_numbers == ["SU 6949"]
    assert second.layovers is None

    assert second.back is not None
    assert second.back.airline == "Utair"
    assert second.back.transfers == 1
    assert second.back.flight_numbers == ["UT 786", "UT 357"]
    assert second.back.aircraft == "Boeing 737-800"
    assert second.back.layovers == ["Москва, 13 ч 5 мин"]

    # Пятое предложение (обратно Utair + Победа с пересадкой в Москве)
    fifth = offers[4]
    assert fifth.price == 72237
    assert fifth.airline == "Россия"
    assert fifth.back is not None
    assert fifth.back.airline == "Utair"
    assert fifth.back.flight_numbers == ["UT 786", "DP 421"]
    assert fifth.back.aircraft == "Boeing 737-800, Boeing 737-800 Winglets"
    assert fifth.back.layovers == ["Москва, 4 ч 55 мин"]


def test_roundtrip_fixture_final_count_prices_and_fields():
    """Проверка финальной фикстуры: 5 офферов, точные цены, багаж (включая рейс с багажом) и самолёты."""
    q = Query("KUF", "EVN", date(2026, 12, 16), return_date=date(2026, 12, 20))
    snap = make_roundtrip_snapshot([(API_URL, load_fixture_text("offers_rt_final_kuf_evn.json"))])
    offers = parse_tutu(snap, q)

    assert len(offers) == 5

    # Цены за оба плеча проверены по amount // 100 из фикстуры:
    # 5164100 // 100 = 51641
    # 6411800 // 100 = 64118
    # 6562000 // 100 = 65620
    # 8429300 // 100 = 84293
    # 29701000 // 100 = 297010
    expected_prices = [51641, 64118, 65620, 84293, 297010]
    assert [o.price for o in offers] == expected_prices
    assert [o.price for o in offers] == sorted([o.price for o in offers])

    # Проверка багажа: 4 тарифа без багажа, один тариф (Аэрофлот за 297010) «с багажом»
    without_baggage = [o for o in offers if o.baggage == "без багажа"]
    with_baggage = [o for o in offers if o.baggage == "с багажом"]
    assert len(without_baggage) == 4
    assert len(with_baggage) == 1
    assert with_baggage[0].price == 297010

    # Предложение за 297010: все 4 сегмента включают 32 кг багажа
    expensive = with_baggage[0]
    assert expensive.airline == "Аэрофлот"
    assert expensive.flight_numbers == ["SU 1605", "SU 1868"]
    assert expensive.layovers == ["Москва, 3 ч 50 мин"]
    assert expensive.back is not None
    assert expensive.back.airline == "Аэрофлот"
    assert expensive.back.flight_numbers == ["SU 1867", "SU 1216"]
    assert expensive.back.layovers == ["Москва, 1 ч 35 мин"]

    # Предложение за 64118 (Россия туда, S7 обратно через Новосибирск с 2 самолётами)
    s7_offer = offers[1]
    assert s7_offer.price == 64118
    assert s7_offer.airline == "Россия"
    assert s7_offer.flight_numbers == ["SU 6390", "SU 1862"]
    assert s7_offer.layovers == ["Москва, 1 ч 40 мин"]
    assert s7_offer.back is not None
    assert s7_offer.back.airline == "S7 Airlines"
    assert s7_offer.back.flight_numbers == ["S7 5592", "S7 5033"]
    assert s7_offer.back.aircraft == "Airbus A320, Embraer 170"
    assert s7_offer.back.layovers == ["Новосибирск, 22 ч 50 мин"]


def test_roundtrip_dedup_and_merging():
    """Проверка дедупликации и объединения: 5 + 5 офферов дают 9 уникальных маршрутов при объединении."""
    q = Query("KUF", "EVN", date(2026, 12, 16), return_date=date(2026, 12, 20))
    text_a = load_fixture_text("offers_rt_a_kuf_evn.json")
    text_f = load_fixture_text("offers_rt_final_kuf_evn.json")

    offers_a = parse_tutu(make_roundtrip_snapshot([(API_URL, text_a)]), q)
    offers_f = parse_tutu(make_roundtrip_snapshot([(API_URL, text_f)]), q)

    assert len(offers_a) == 5
    assert len(offers_f) == 5

    # Объединение офферов из двух выборок с дедупликацией по ключу маршрута обоих плеч
    # (рейс, времена вылета/прилёта обоих плеч)
    merged_by_route: dict[tuple[str, str, str, str], Offer] = {}
    for o in offers_a + offers_f:
        assert o.back is not None
        key = (
            ",".join(o.flight_numbers or []),
            o.depart_at or "",
            ",".join(o.back.flight_numbers or []),
            o.back.depart_at or "",
        )
        if key not in merged_by_route or o.price < merged_by_route[key].price:
            merged_by_route[key] = o

    # Общий рейс между фикстурами ровно один (Азимут + Аэрофлот за 51641),
    # поэтому 5 + 5 - 1 = 9 уникальных предложений
    assert len(merged_by_route) == 9
    merged_offers = sorted(merged_by_route.values(), key=lambda o: o.price)
    expected_merged_prices = [51641, 60788, 64118, 65620, 66144, 66612, 72237, 84293, 297010]
    assert [o.price for o in merged_offers] == expected_merged_prices

    # Внутри снимка с двумя сетевыми ответами Туту побеждает финальный (offersCurrentAmount=64 > 44)
    snap_both = make_roundtrip_snapshot([
        (API_URL, text_a),
        (API_URL, text_f),
    ])
    offers_both = parse_tutu(snap_both, q)
    assert len(offers_both) == 5
    assert [o.price for o in offers_both] == [51641, 64118, 65620, 84293, 297010]


def test_roundtrip_query_without_return_date():
    """Если query.return_date не задан, парсер извлекает плечо туда, а back устанавливает в None."""
    q_oneway = Query("KUF", "EVN", date(2026, 12, 16))
    snap = make_roundtrip_snapshot([(API_URL, load_fixture_text("offers_rt_a_kuf_evn.json"))])
    offers = parse_tutu(snap, q_oneway)

    assert len(offers) == 5
    for o in offers:
        assert o.back is None
        assert o.depart_time is not None
        assert o.arrive_time is not None
        assert o.flight_numbers is not None
        assert len(o.flight_numbers) > 0
        assert o.depart_at is not None
        assert o.arrive_at is not None
        assert o.site == "tutu"


def test_roundtrip_skip_offers_without_two_routes():
    """В режиме туда-обратно офферы без ровно двух маршрутов должны отбрасываться."""
    q = Query("KUF", "EVN", date(2026, 12, 16), return_date=date(2026, 12, 20))
    data = json.loads(load_fixture_text("offers_rt_a_kuf_evn.json"))
    chunk = data[0]
    actual = chunk["offers"]["actual"]

    # Испортим первое предложение: оставим только 1 маршрут
    first_key = next(iter(actual.keys()))
    actual[first_key]["routeIds"] = [actual[first_key]["routeIds"][0]]

    # Испортим второе предложение: сделаем пустой список маршрутов
    keys = list(actual.keys())
    second_key = keys[1]
    actual[second_key]["routeIds"] = []

    # Испортим третье предложение: 3 маршрута
    third_key = keys[2]
    actual[third_key]["routeIds"] = actual[third_key]["routeIds"] + ["extra_route"]

    snap = make_roundtrip_snapshot([(API_URL, json.dumps(data))])
    offers = parse_tutu(snap, q)

    # 3 битых оффера отброшены, осталось ровно 2
    assert len(offers) == 2


def test_roundtrip_compute_offer_id_stability_and_uniqueness():
    """compute_offer_id стабилен и различается для офферов с разными обратными рейсами."""
    q = Query("KUF", "EVN", date(2026, 12, 16), return_date=date(2026, 12, 20))
    snap = make_roundtrip_snapshot([(API_URL, load_fixture_text("offers_rt_a_kuf_evn.json"))])
    offers = parse_tutu(snap, q)

    # Все 5 офферов имеют стабильные sha1-идентификаторы
    ids = [compute_offer_id(o) for o in offers]
    assert all(len(i) == 12 for i in ids)
    assert len(set(ids)) == 5  # Все уникальны

    # Повторный вызов возвращает те же самые идентификаторы (детерминизм)
    assert ids == [compute_offer_id(o) for o in offers]

    # Четыре оффера (с ценами 60788, 66144, 66612, 72237) имеют одинаковый прямой рейс (SU 6949, 14:40 -> 17:30),
    # но разные обратные рейсы -> их offer_id обязаны различаться
    same_outbound_offers = [o for o in offers if o.flight_numbers == ["SU 6949"]]
    assert len(same_outbound_offers) == 4
    outbound_ids = [compute_offer_id(o) for o in same_outbound_offers]
    assert len(set(outbound_ids)) == 4
