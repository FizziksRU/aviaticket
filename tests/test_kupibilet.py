"""Тесты парсера Купибилет (Kupibilet)."""
from datetime import date
import json
from pathlib import Path
import pytest

from avia.collector import Snapshot
from avia.models import Query
from avia.parsers import PARSERS
from avia.parsers.kupibilet import parse_kupibilet, results_complete

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "kupibilet"
SEARCH_URL = "https://api-rs-lb.kupibilet.ru/frontend_search"


def load_fixture_text(name: str) -> str:
    """Загружает содержимое тестовой фикстуры."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_snapshot(payloads: list[tuple[str, str]] | None = None) -> Snapshot:
    """Создает тестовый Snapshot с заданными сетевыми ответами."""
    if payloads is None:
        payloads = [(SEARCH_URL, load_fixture_text("search_quick_mow_ala.json"))]
    return Snapshot(
        url="https://www.kupibilet.ru/search",
        text="Купибилет поиск",
        json_payloads=payloads,
    )


def test_parser_registered():
    """Парсер kupibilet должен быть зарегистрирован в реестре PARSERS."""
    assert "kupibilet" in PARSERS
    assert PARSERS["kupibilet"] is parse_kupibilet
    assert getattr(parse_kupibilet, "results_complete", None) is results_complete


def test_parse_kupibilet_quick_count_and_first_variant():
    """Проверка числа вариантов, минимальной цены, авиакомпании, времени и длительности."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    snap = make_snapshot([(SEARCH_URL, load_fixture_text("search_quick_mow_ala.json"))])
    offers = parse_kupibilet(snap, q)

    # В фикстуре ровно 8 вариантов
    assert len(offers) == 8
    # Отсортированы по цене
    assert [o.price for o in offers] == sorted([o.price for o in offers])

    first = offers[0]
    assert first.price == 18007
    assert first.currency == "RUB"
    assert first.airline == "SCAT"
    # Местное время вылета и прилета без пересчета часовых поясов
    assert first.depart_time == "22:10"
    assert first.arrive_time == "04:30"
    # Вылет 2026-12-12T22:10+03:00, прилёт 2026-12-13T04:30+05:00 = 4ч 20м (260 минут)
    assert first.duration == "4ч 20м"
    assert first.transfers == 0
    assert first.baggage == "без багажа"
    assert first.site == "kupibilet"
    assert first.url is None


def test_parse_kupibilet_baggage():
    """Проверка определения багажа: 'без багажа' при count == 0 и 'с багажом' при count > 0."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    snap = make_snapshot([(SEARCH_URL, load_fixture_text("search_quick_mow_ala.json"))])
    offers = parse_kupibilet(snap, q)

    # Вариант 18007: count = 0
    assert offers[0].baggage == "без багажа"

    # Вариант 21270: count = 1, weight = 23
    with_baggage = next(o for o in offers if o.price == 21270)
    assert with_baggage.baggage == "с багажом"


def test_parse_kupibilet_full_transfers():
    """Проверка пересадок на варианте с двумя перелётами в full-фикстуре."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    snap = make_snapshot([(SEARCH_URL, load_fixture_text("search_full_mow_ala.json"))])
    offers = parse_kupibilet(snap, q)

    # Второй вариант в full-фикстуре состоит из 2 перелётов (VKO -> NQZ, NQZ -> ALA)
    two_legs = offers[1]
    assert two_legs.price == 18125
    assert two_legs.transfers == 1
    assert two_legs.airline == "SCAT"  # первый перелёт выполняет SCAT (DV)
    assert two_legs.depart_time == "12:35"
    assert two_legs.arrive_time == "09:05"
    # 2026-12-12T12:35+03:00 до 2026-12-13T09:05+05:00 = 18ч 30м (1110 минут)
    assert two_legs.duration == "18ч 30м"
    assert two_legs.baggage == "без багажа"


def test_parse_kupibilet_two_responses_full_wins():
    """При наличии двух ответов побеждает полный (более поздний при равенстве, либо с большим числом вариантов)."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    quick_json = load_fixture_text("search_quick_mow_ala.json")
    full_json = load_fixture_text("search_full_mow_ala.json")

    # Сначала пришёл быстрый ответ, затем полный
    snap = make_snapshot([
        (SEARCH_URL, quick_json),
        (SEARCH_URL, full_json),
    ])
    offers = parse_kupibilet(snap, q)

    # В quick второй билет имеет цену 19126 (0 пересадок), а в full — 18125 (1 пересадка)
    assert len(offers) == 8
    assert offers[1].price == 18125
    assert offers[1].transfers == 1

    # Проверка победы ответа с БОЛЬШИМ числом вариантов, даже если он пришёл первым
    quick_data = json.loads(quick_json)
    full_data = json.loads(full_json)
    # Сделаем в full_data 9 вариантов
    full_data["variants"].append(full_data["variants"][0].copy())
    full_data["variants"][-1]["id"] = "synthetic_extra"
    snap_large_first = make_snapshot([
        (SEARCH_URL, json.dumps(full_data)),
        (SEARCH_URL, json.dumps(quick_data)),
    ])
    offers_large = parse_kupibilet(snap_large_first, q)
    assert len(offers_large) == 9


def test_results_complete():
    """Проверка results_complete: False на quick одной, True на quick+full, True на >= 20 вариантах."""
    quick_json = load_fixture_text("search_quick_mow_ala.json")
    full_json = load_fixture_text("search_full_mow_ala.json")

    # Только быстрый ответ (1 ответ < 20 вариантов) -> False
    snap_quick = make_snapshot([(SEARCH_URL, quick_json)])
    assert results_complete(snap_quick) is False
    assert parse_kupibilet.results_complete(snap_quick) is False

    # Быстрый + полный ответы (2 ответа) -> True
    snap_both = make_snapshot([(SEARCH_URL, quick_json), (SEARCH_URL, full_json)])
    assert results_complete(snap_both) is True
    assert parse_kupibilet.results_complete(snap_both) is True

    # Один ответ с >= 20 вариантами -> True
    single_large = json.loads(quick_json)
    single_large["variants"] = single_large["variants"] * 3  # 24 варианта
    snap_large = make_snapshot([(SEARCH_URL, json.dumps(single_large))])
    assert results_complete(snap_large) is True

    # Пустые / нерелевантные снимки -> False
    assert results_complete(None) is False
    assert results_complete(Snapshot("https://www.kupibilet.ru", "", [])) is False
    assert results_complete(make_snapshot([("https://kupibilet.ru/other_api", quick_json)])) is False


def test_empty_and_broken_json():
    """Пустой или битый JSON не должен вызывать исключений."""
    q = Query("MOW", "ALA", date(2026, 12, 12))

    # Битый JSON
    snap_broken = make_snapshot([(SEARCH_URL, "broken json {[{")])
    assert parse_kupibilet(snap_broken, q) == []
    assert results_complete(snap_broken) is False

    # Пустой JSON объект
    snap_empty = make_snapshot([(SEARCH_URL, "{}")])
    assert parse_kupibilet(snap_empty, q) == []

    # Битый ответ + валидный ответ: валидный разбирается корректно
    quick_json = load_fixture_text("search_quick_mow_ala.json")
    snap_mixed = make_snapshot([
        (SEARCH_URL, "broken json"),
        (SEARCH_URL, quick_json),
    ])
    offers = parse_kupibilet(snap_mixed, q)
    assert len(offers) == 8


def test_broken_variants_skipped():
    """Битые варианты пропускаются, валидные остаются."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    data = json.loads(load_fixture_text("search_quick_mow_ala.json"))

    # Добавим варианты с невалидной ценой, отсутствующими перелётами и битыми сегментами
    data["variants"].append({"price": {"amount": "не_число"}})
    data["variants"].append({"price": {"amount": "-500"}})
    data["variants"].append({"price": {"amount": "25000"}, "segments": []})
    data["variants"].append({"price": {"amount": "25000"}, "segments": [{"flights": ["unknown_flight_id"]}]})

    snap = make_snapshot([(SEARCH_URL, json.dumps(data))])
    offers = parse_kupibilet(snap, q)
    # Все исходные 8 валидных вариантов должны остаться
    assert len(offers) == 8


def test_skip_trains_synthetic():
    """Варианты с transport_kind отличным от 'airplane' (например, поезда) пропускаются."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    data = json.loads(load_fixture_text("search_quick_mow_ala.json"))

    # Пометим перелёт первого варианта как поезд (этот перелёт используют варианты 0 и 4)
    flight_id = data["variants"][0]["segments"][0]["flights"][0]
    data["flights"][flight_id]["transport_kind"] = "train"

    snap = make_snapshot([(SEARCH_URL, json.dumps(data))])
    offers = parse_kupibilet(snap, q)

    # Оба варианта с этим перелётом (18007 и 21270) должны быть отфильтрованы, осталось 6
    assert len(offers) == 6
    assert all(o.price not in (18007, 21270) for o in offers)


def test_transport_kind_none_allowed():
    """Перелёты без поля transport_kind или с transport_kind == None разрешены."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    data = json.loads(load_fixture_text("search_quick_mow_ala.json"))

    flight_id = data["variants"][0]["segments"][0]["flights"][0]
    data["flights"][flight_id]["transport_kind"] = None

    snap = make_snapshot([(SEARCH_URL, json.dumps(data))])
    offers = parse_kupibilet(snap, q)
    assert len(offers) == 8
    assert offers[0].price == 18007


def test_duration_without_timezone():
    """Если время дано без часовой зоны, duration не вычисляется (None), но время извлекается."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    data = json.loads(load_fixture_text("search_quick_mow_ala.json"))

    flight_id = data["variants"][0]["segments"][0]["flights"][0]
    data["flights"][flight_id]["departure_datetime"] = "2026-12-12T22:10:00"  # без зоны
    data["flights"][flight_id]["arrival_datetime"] = "2026-12-13T04:30:00"    # без зоны

    snap = make_snapshot([(SEARCH_URL, json.dumps(data))])
    offers = parse_kupibilet(snap, q)

    assert offers[0].depart_time == "22:10"
    assert offers[0].arrive_time == "04:30"
    assert offers[0].duration is None


def test_airline_fallback():
    """Если код авиакомпании отсутствует в словаре airlines, используется сам код."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    data = json.loads(load_fixture_text("search_quick_mow_ala.json"))

    flight_id = data["variants"][0]["segments"][0]["flights"][0]
    data["flights"][flight_id]["operating_carrier"] = "UNKNOWN_CODE"

    snap = make_snapshot([(SEARCH_URL, json.dumps(data))])
    offers = parse_kupibilet(snap, q)

    assert offers[0].airline == "UNKNOWN_CODE"
