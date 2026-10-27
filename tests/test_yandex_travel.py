"""Тесты парсера Яндекс Путешествий (yandex_travel)."""
from datetime import date
import json
from pathlib import Path

from avia.collector import Snapshot
from avia.models import Query
from avia.parsers import PARSERS
from avia.parsers.yandex_travel import (
    _calculate_duration,
    _extract_time,
    parse_yandex_travel,
    results_complete,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "yandex_travel"
API_URL = "https://travel.yandex.ru/api/avia/search/results?qid=test_qid&cont=0"


def load_fixture_text(name: str) -> str:
    """Загружает содержимое тестовой фикстуры Яндекс Путешествий."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_snapshot(payloads: list[tuple[str, str]] | None = None) -> Snapshot:
    """Создает тестовый Snapshot с заданными сетевыми ответами."""
    if payloads is None:
        payloads = [(API_URL, load_fixture_text("results_cont0_mow_ala.json"))]
    return Snapshot(
        url="https://travel.yandex.ru/avia/search/result/?adult_seats=1&fromId=c213&toId=c22177&when=2026-12-12",
        text="Яндекс Путешествия поиск авиабилетов",
        json_payloads=payloads,
    )


def test_parser_registered():
    """Парсер yandex_travel должен быть зарегистрирован в реестре PARSERS."""
    assert "yandex_travel" in PARSERS
    assert PARSERS["yandex_travel"] is parse_yandex_travel
    assert getattr(parse_yandex_travel, "results_complete", None) is results_complete


def test_parse_first_fixture_cont0():
    """Проверка первой порции: число тарифов 6, мин. цена 17672, сортировка, поля первого тарифа."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    snap = make_snapshot([(API_URL, load_fixture_text("results_cont0_mow_ala.json"))])
    offers = parse_yandex_travel(snap, q)

    # В первой фикстуре ровно 6 тарифов
    assert len(offers) == 6

    # Цены вручную проверены по фикстуре:
    # Fare 0: N4 77 + RT 715 -> 17672
    # Fare 1: DV 1008 -> 18056 (наименьшая из предложений 18490, 18368, ..., 18056)
    # Fare 2: DV 1010 + DV 724 -> 18951
    # Fare 3: S7 2513 + S7 5501 -> 19101
    # Fare 4: S7 2503 + S7 5501 -> 19101
    # Fare 5: S7 2509 + S7 5501 -> 19101
    expected_prices = [17672, 18056, 18951, 19101, 19101, 19101]
    assert [o.price for o in offers] == expected_prices
    assert [o.price for o in offers] == sorted([o.price for o in offers])
    assert offers[0].price == 17672

    # Проверка первого (наиболее дешёвого) тарифа:
    # Рейсы: N4 77 (Nordwind) вылет 2026-12-12T22:50:00+03:00,
    # затем RT 715 прилёт 2026-12-13T18:25:00+05:00
    first = offers[0]
    assert first.price == 17672
    assert first.currency == "RUB"
    assert first.airline == "Nordwind"
    assert first.depart_time == "22:50"
    assert first.arrive_time == "18:25"
    # Длительность: 19:50 UTC (12.12) -> 13:25 UTC (13.12) = 17 часов 35 минут
    assert first.duration == "17ч 35м"
    assert first.transfers == 1
    assert first.baggage == "без багажа"
    assert first.site == "yandex_travel"
    assert first.url is None

    # Проверка второго тарифа (прямой рейс SCAT)
    second = offers[1]
    assert second.price == 18056
    assert second.airline == "SCAT"
    assert second.depart_time == "22:10"
    assert second.arrive_time == "04:30"
    # Длительность: 19:10 UTC -> 23:30 UTC = 4 часа 20 минут
    assert second.duration == "4ч 20м"
    assert second.transfers == 0
    assert second.baggage == "без багажа"


def test_parse_last_fixture_cont_last():
    """Проверка последней порции: число тарифов 6, мин. цена 16403, сортировка, пересадки."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    snap = make_snapshot([(API_URL, load_fixture_text("results_cont_last_mow_ala.json"))])
    offers = parse_yandex_travel(snap, q)

    assert len(offers) == 6

    # Цены вручную по второй фикстуре:
    # Fare 0: 16403 (Nordwind + ЮВТ Аэро)
    # Fare 1: 17241 (Победа, 3 пересадки, 4 сегмента)
    # Fare 2: 17241 (Победа, 3 пересадки)
    # Fare 3: 17241 (Победа, 3 пересадки)
    # Fare 4: 17865 (SCAT, прямой)
    # Fare 5: 17981 (ЮТэйр, 3 пересадки)
    expected_prices = [16403, 17241, 17241, 17241, 17865, 17981]
    assert [o.price for o in offers] == expected_prices
    assert [o.price for o in offers] == sorted([o.price for o in offers])
    assert offers[0].price == 16403

    # Первый тариф
    first = offers[0]
    assert first.price == 16403
    assert first.currency == "RUB"
    assert first.airline == "Nordwind"
    assert first.depart_time == "22:50"
    assert first.arrive_time == "18:25"
    assert first.duration == "17ч 35м"
    assert first.transfers == 1
    assert first.baggage == "без багажа"
    assert first.site == "yandex_travel"
    assert first.url is None

    # Тариф с 3 пересадками (Победа: DP6583, WZ1034, WZ1097, IQ351)
    pobeda = offers[1]
    assert pobeda.price == 17241
    assert pobeda.airline == "Победа"
    assert pobeda.depart_time == "19:30"
    assert pobeda.arrive_time == "23:55"
    # Длительность: 16:30 UTC -> 18:55 UTC следующего дня = 26ч 25м
    assert pobeda.duration == "26ч 25м"
    assert pobeda.transfers == 3
    assert pobeda.baggage == "без багажа"


def test_parse_both_fixtures_combined():
    """Проверка объединения двух фикстур: число тарифов 12, сортировка по возрастанию цены."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    snap = make_snapshot([
        ("https://travel.yandex.ru/api/avia/search/results?qid=test&cont=0", load_fixture_text("results_cont0_mow_ala.json")),
        ("https://travel.yandex.ru/api/avia/search/results?qid=test&cont=3", load_fixture_text("results_cont_last_mow_ala.json")),
    ])
    offers = parse_yandex_travel(snap, q)

    # 6 тарифов из cont0 + 6 тарифов из cont_last = 12 тарифов
    assert len(offers) == 12

    # Минимальная цена во всей выборке — 16403
    assert offers[0].price == 16403
    assert [o.price for o in offers] == sorted([o.price for o in offers])
    assert all(o.currency == "RUB" for o in offers)
    assert all(o.site == "yandex_travel" for o in offers)


def test_deduplication_repeated_portion():
    """Проверка дедупликации при повторной подаче одной и той же порции."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    first_text = load_fixture_text("results_cont0_mow_ala.json")

    # Подаем первую порцию дважды (например, опрос или дубликат ответа)
    snap_dup = make_snapshot([
        (API_URL, first_text),
        (API_URL, first_text),
    ])
    offers = parse_yandex_travel(snap_dup, q)

    # Должно остаться ровно 6 тарифов, а не 12
    assert len(offers) == 6
    assert [o.price for o in offers] == [17672, 18056, 18951, 19101, 19101, 19101]

    # Аналогично при повторной подаче последней порции
    last_text = load_fixture_text("results_cont_last_mow_ala.json")
    snap_last_dup = make_snapshot([
        (API_URL, last_text),
        (API_URL, last_text),
    ])
    offers_last = parse_yandex_travel(snap_last_dup, q)
    assert len(offers_last) == 6


def test_route_deduplication_within_portion():
    """Проверка дедупликации внутри одной порции по ключу route с сохранением минимальной цены."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    data = json.loads(load_fixture_text("results_cont0_mow_ala.json"))

    # Добавляем дублирующий тариф с тем же маршрутом, что у Fare 0 (N4 77 + RT 715),
    # но с большей ценой (25000 вместо 17672). Итоговое число тарифов и цена не должны измениться.
    dup_expensive = {
        "route": data["variants"]["fares"][0]["route"],
        "prices": [{"tariff": {"value": 25000, "currency": "RUR"}}],
    }
    data["variants"]["fares"].append(dup_expensive)

    snap = make_snapshot([(API_URL, json.dumps(data))])
    offers = parse_yandex_travel(snap, q)
    assert len(offers) == 6
    assert offers[0].price == 17672

    # Теперь добавляем тот же маршрут, но с меньшей ценой (15000 вместо 17672).
    # Должно обновиться ценовое предложение для данного маршрута.
    dup_cheaper = {
        "route": data["variants"]["fares"][0]["route"],
        "prices": [{"tariff": {"value": 15000, "currency": "RUR"}}],
    }
    data["variants"]["fares"].append(dup_cheaper)

    snap2 = make_snapshot([(API_URL, json.dumps(data))])
    offers2 = parse_yandex_travel(snap2, q)
    assert len(offers2) == 6
    assert offers2[0].price == 15000


def test_results_complete():
    """Проверка эмпирического признака полноты выдачи (results_complete)."""
    text0 = load_fixture_text("results_cont0_mow_ala.json")
    text_last = load_fixture_text("results_cont_last_mow_ala.json")

    # None / пустой снимок -> False
    assert results_complete(None) is False
    assert results_complete(Snapshot("https://travel.yandex.ru", "", [])) is False

    # Неподходящий URL -> False
    snap_other = make_snapshot([("https://travel.yandex.ru/api/other/endpoint", text_last)])
    assert results_complete(snap_other) is False

    # Пустой словарь JSON или ответ с ошибкой не должны считаться завершённой выдачей
    snap_empty = make_snapshot([(API_URL, "{}")])
    assert results_complete(snap_empty) is False

    snap_err = make_snapshot([(API_URL, json.dumps({"status": "error", "error": "rate_limit"}))])
    assert results_complete(snap_err) is False

    snap_err_wrapped = make_snapshot([(API_URL, json.dumps({"data": {"status": "error"}}))])
    assert results_complete(snap_err_wrapped) is False

    # Первая порция (cont=1, progress 4/20) -> False
    snap_first = make_snapshot([(API_URL, text0)])
    assert results_complete(snap_first) is False
    assert parse_yandex_travel.results_complete(snap_first) is False

    # Последняя порция (cont=null, progress 20/20) -> True
    snap_last = make_snapshot([(API_URL, text_last)])
    assert results_complete(snap_last) is True
    assert parse_yandex_travel.results_complete(snap_last) is True

    # Обе порции вместе (наличие последней делает выдачу полной) -> True
    snap_both = make_snapshot([
        (API_URL, text0),
        (API_URL, text_last),
    ])
    assert results_complete(snap_both) is True
    assert parse_yandex_travel.results_complete(snap_both) is True

    # cont=null, но опрос не закончен (progress 10/20) -> False
    data_incomplete = json.loads(text_last)
    data_incomplete["progress"] = {"current": 10, "all": 20}
    snap_inc = make_snapshot([(API_URL, json.dumps(data_incomplete))])
    assert results_complete(snap_inc) is False

    # cont=null без блока progress (но с реальной выдачей) -> True
    data_no_prog = json.loads(text_last)
    del data_no_prog["progress"]
    snap_no_prog = make_snapshot([(API_URL, json.dumps(data_no_prog))])
    assert results_complete(snap_no_prog) is True

    # cont=null с boolean в progress -> False
    data_bool_prog = json.loads(text_last)
    data_bool_prog["progress"] = {"current": True, "all": 1}
    snap_bool_prog = make_snapshot([(API_URL, json.dumps(data_bool_prog))])
    assert results_complete(snap_bool_prog) is False


def test_malformed_and_empty_payloads():
    """Парсер не падает на пустых, битых данных, невалидных типах и битых тарифах."""
    q = Query("MOW", "ALA", date(2026, 12, 12))

    # Пустой снимок
    assert parse_yandex_travel(Snapshot("", "", []), q) == []

    # Невалидный JSON
    snap_bad = make_snapshot([(API_URL, "{broken json")])
    assert parse_yandex_travel(snap_bad, q) == []

    # JSON не-словарь
    snap_list = make_snapshot([(API_URL, json.dumps([1, 2, 3]))])
    assert parse_yandex_travel(snap_list, q) == []

    # Пустой JSON-объект
    snap_empty = make_snapshot([(API_URL, "{}")])
    assert parse_yandex_travel(snap_empty, q) == []

    # Частично поврежденный тариф и невалидные типы полей не роняют парсер
    data = json.loads(load_fixture_text("results_cont0_mow_ala.json"))

    # Добавляем различные битые тарифы (включая route: None, route: [None], route: [[None]])
    data["variants"]["fares"].append({"broken": True})
    data["variants"]["fares"].append({"route": None, "prices": None})
    data["variants"]["fares"].append({"route": [None], "prices": []})
    data["variants"]["fares"].append({"route": [[None]], "prices": []})
    data["variants"]["fares"].append({"route": 123, "prices": "not_a_list"})
    data["variants"]["fares"].append({"route": [], "prices": []})
    data["variants"]["fares"].append(None)
    data["variants"]["fares"].append("broken_string_fare")
    data["variants"]["fares"].append({
        "route": [["unknown_flight_key"], []],
        "prices": [{"tariff": {"value": 10000, "currency": "RUR"}}],
    })
    data["variants"]["fares"].append({
        "route": [["2612122210DV10082612130430"]],
        "prices": [{"tariff": {"value": None}}],
    })
    data["variants"]["fares"].append({
        "route": [["2612122210DV10082612130430"]],
        "prices": [{"tariff": {"value": True}}],
    })
    data["variants"]["fares"].append({
        "route": [["2612122210DV10082612130430"]],
        "prices": [{"tariff": {"value": "invalid_number"}}],
    })

    # Повреждаем типы справочников reference
    data["reference"]["flights"].append(None)
    data["reference"]["flights"].append(123)
    data["reference"]["companies"] = 123  # не список
    data["reference"]["aviaCompanies"] = True  # не список
    data["reference"]["companyTariffs"] = "not_a_list"

    # Нехэшируемые cont и progress
    data["cont"] = [1, 2, 3]
    data["progress"] = {"current": {}, "all": []}

    snap_corrupt = make_snapshot([(API_URL, json.dumps(data))])
    offers = parse_yandex_travel(snap_corrupt, q)
    assert len(offers) == 6


def test_wrapped_data_payload():
    """Поддержка обернутого формата ответа {'data': {...}}."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    raw_dict = json.loads(load_fixture_text("results_cont0_mow_ala.json"))
    wrapped = {"data": raw_dict}

    snap = make_snapshot([(API_URL, json.dumps(wrapped))])
    offers = parse_yandex_travel(snap, q)
    assert len(offers) == 6
    assert offers[0].price == 17672


def test_baggage_detection():
    """Проверка детекции багажа по кодам '0d1d15d', '0p0p0p', '1p1p23p' и fallback companyTariffs."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    data = json.loads(load_fixture_text("results_cont0_mow_ala.json"))

    # По умолчанию у всех cheapest предложений багаж отсутствует
    snap_orig = make_snapshot([(API_URL, json.dumps(data))])
    offers_orig = parse_yandex_travel(snap_orig, q)
    assert all(o.baggage == "без багажа" for o in offers_orig)

    # 1. Изменяем код багажа первого предложения на 1p1p23p (1 место багажа)
    data["variants"]["fares"][0]["prices"][0]["baggage"] = [["1p1p23p", "0d1d10d"], []]
    snap_with_luggage = make_snapshot([(API_URL, json.dumps(data))])
    offers_luggage = parse_yandex_travel(snap_with_luggage, q)
    first_with_luggage = next(o for o in offers_luggage if o.price == 17672)
    assert first_with_luggage.baggage == "с багажом"

    # 2. Проверяем запасной вариант по companyTariffs.baggageAllowed первого рейса
    data2 = json.loads(load_fixture_text("results_cont0_mow_ala.json"))
    del data2["variants"]["fares"][0]["prices"][0]["baggage"]
    # Включаем baggageAllowed в соответствующем тарифе (id: 184)
    for ct in data2["reference"]["companyTariffs"]:
        if ct["id"] == 184:
            ct["baggageAllowed"] = True

    snap_ct_luggage = make_snapshot([(API_URL, json.dumps(data2))])
    offers_ct = parse_yandex_travel(snap_ct_luggage, q)
    assert offers_ct[0].baggage == "с багажом"


def test_helper_functions_edge_cases():
    """Проверка вспомогательных функций _extract_time и _calculate_duration на граничных случаях."""
    # _extract_time
    assert _extract_time("2026-12-12T22:50:00") == "22:50"
    assert _extract_time("2026-12-12 18:25:00") == "18:25"
    assert _extract_time("22:50") == "22:50"
    assert _extract_time("2026-12-12") is None  # дата без времени не должна возвращать "2026-"
    assert _extract_time("invalid") is None
    assert _extract_time(None) is None
    assert _extract_time(12345) is None

    # _calculate_duration
    dep = {"local": "2026-12-12T22:50:00", "offset": 180}
    arr = {"local": "2026-12-13T18:25:00", "offset": 300}
    assert _calculate_duration(dep, arr) == "17ч 35м"

    # Boolean в offset не должен интерпретироваться как 1 или 0 минут
    dep_bool = {"local": "2026-12-12T22:50:00", "offset": True}
    assert _calculate_duration(dep_bool, arr) is None

    arr_bool = {"local": "2026-12-13T18:25:00", "offset": False}
    assert _calculate_duration(dep, arr_bool) is None

    # Невалидные типы словарей
    assert _calculate_duration("bad", arr) is None
    assert _calculate_duration(dep, "bad") is None

    # Прилёт раньше вылета
    arr_early = {"local": "2026-12-11T18:25:00", "offset": 300}
    assert _calculate_duration(dep, arr_early) is None
