"""Тесты парсера Туту (tutu)."""
from datetime import date
import json
from pathlib import Path

from avia.collector import Snapshot
from avia.models import Query
from avia.parsers import PARSERS
from avia.parsers.tutu import parse_tutu, results_complete

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "tutu"
API_URL = "https://offers-api.tutu.ru/avia/offers/v2"


def load_fixture_text(name: str) -> str:
    """Загружает содержимое тестовой фикстуры."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_snapshot(payloads: list[tuple[str, str]] | None = None) -> Snapshot:
    """Создает тестовый Snapshot с заданными сетевыми ответами."""
    if payloads is None:
        payloads = [(API_URL, load_fixture_text("offers_first_mow_ala.json"))]
    return Snapshot(
        url="https://avia.tutu.ru/offers/?class=Y&travelers=1&route[0]=491-12122026-117",
        text="Туту поиск",
        json_payloads=payloads,
    )


def test_parser_registered():
    """Парсер tutu должен быть зарегистрирован в реестре PARSERS."""
    assert "tutu" in PARSERS
    assert PARSERS["tutu"] is parse_tutu
    assert getattr(parse_tutu, "results_complete", None) is results_complete


def test_parse_tutu_first_fixture_count_and_fields():
    """Проверка первой фикстуры: число предложений, ручные цены, сортировка, времена, авиакомпания, пересадки."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    snap = make_snapshot([(API_URL, load_fixture_text("offers_first_mow_ala.json"))])
    offers = parse_tutu(snap, q)

    # В фикстуре ровно 4 предложения
    assert len(offers) == 4

    # Цены в рублях проверены вручную по amount/100 из фикстуры:
    # 1875400 / 100 = 18754
    # 1899100 / 100 = 18991
    # 1942300 / 100 = 19423
    # 2324000 / 100 = 23240
    assert [o.price for o in offers] == [18754, 18991, 19423, 23240]
    assert [o.price for o in offers] == sorted([o.price for o in offers])

    # Первое предложение (самое дешёвое, S7 Airlines)
    first = offers[0]
    assert first.price == 18754
    assert first.currency == "RUB"
    assert first.airline == "S7 Airlines"
    # Местное время вылета и прилёта HH:MM
    assert first.depart_time == "23:00"
    assert first.arrive_time == "12:15"
    # Длительность с учётом часовых поясов: 2026-12-12T23:00+03:00 -> 2026-12-13T12:15+05:00 = 11ч 15м
    assert first.duration == "11ч 15м"
    assert first.transfers == 1
    assert first.baggage == "без багажа"
    assert first.site == "tutu"
    assert first.url == (
        "https://avia.tutu.ru/offers/?class=Y&travelers=1&route[0]=491-12122026-117"
        "&pin=d5c3d59391e66411dcb3871130e753c4-a0a544af90f6b74c090b11913241cc15"
    )

    # Второе предложение — прямой рейс Аэрофлота
    second = offers[1]
    assert second.price == 18991
    assert second.airline == "Аэрофлот"
    assert second.depart_time == "01:05"
    assert second.arrive_time == "07:45"
    assert second.duration == "4ч 40м"
    assert second.transfers == 0
    assert second.baggage == "без багажа"


def test_parse_tutu_last_fixture_count_and_fields():
    """Проверка второй фикстуры: число предложений, цены, пересадки (включая рейс с 2 пересадками)."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    snap = make_snapshot([(API_URL, load_fixture_text("offers_last_mow_ala.json"))])
    offers = parse_tutu(snap, q)

    assert len(offers) == 4
    # Цены вручную: 19423, 26178, 29110, 31687
    assert [o.price for o in offers] == [19423, 26178, 29110, 31687]
    assert [o.price for o in offers] == sorted([o.price for o in offers])

    # Первое предложение (SCAT Air)
    first = offers[0]
    assert first.price == 19423
    assert first.currency == "RUB"
    assert first.airline == "SCAT Air"
    assert first.depart_time == "12:35"
    assert first.arrive_time == "12:00"
    assert first.duration == "21ч 25м"
    assert first.transfers == 1
    assert first.baggage == "без багажа"
    assert first.url == (
        "https://avia.tutu.ru/offers/?class=Y&travelers=1&route[0]=491-12122026-117"
        "&pin=11b87c7c4a18b6da0521677fb6950fd4-b9cdc7c035a35fabee421917ba2dcf33"
    )

    # Четвёртое предложение (Победа с 2 пересадками, 3 сегмента)
    fourth = offers[3]
    assert fourth.price == 31687
    assert fourth.airline == "Победа"
    assert fourth.depart_time == "23:25"
    assert fourth.arrive_time == "01:00"
    assert fourth.duration == "47ч 35м"
    assert fourth.transfers == 2
    assert fourth.baggage == "без багажа"


def test_parse_tutu_baggage_detection():
    """Проверка определения багажа: 'без багажа' по умолчанию и 'с багажом' при baggageKg/baggagePc > 0."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    data = json.loads(load_fixture_text("offers_first_mow_ala.json"))

    # В исходной фикстуре все самые дешёвые варианты без багажа
    snap_orig = make_snapshot([(API_URL, json.dumps(data))])
    offers_orig = parse_tutu(snap_orig, q)
    assert all(o.baggage == "без багажа" for o in offers_orig)

    # Модифицируем условия тарифа для первого сегмента первого предложения, чтобы включить багаж
    chunk = data[0]
    # Находим condition_id для первого тарифа
    actual_offers = chunk["offers"]["actual"]
    first_offer = next(iter(actual_offers.values()))
    first_variant = first_offer["offerVariants"][0]
    first_fa_hash = next(iter(first_variant["fareApplications"].values()))[0]
    cond_id = chunk["dictionary"]["common"]["fareApplications"][first_fa_hash]["segmentConditions"]

    # Устанавливаем 23 кг багажа
    chunk["dictionary"]["avia"]["conditions"][cond_id]["baggageKg"] = {"defined": True, "value": 23}
    chunk["dictionary"]["avia"]["conditions"][cond_id]["baggagePc"] = {"defined": True, "value": 1}

    snap_with_luggage = make_snapshot([(API_URL, json.dumps(data))])
    offers_luggage = parse_tutu(snap_with_luggage, q)

    matching = [o for o in offers_luggage if o.price == 19423]
    assert len(matching) == 1
    assert matching[0].baggage == "с багажом"


def test_parse_tutu_url_pinlink():
    """URL заполняется из pinLink предложения, если это валидная ссылка на avia.tutu.ru."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    data = json.loads(load_fixture_text("offers_first_mow_ala.json"))
    chunk = data[0]

    # Сделаем у одного предложения некорректный pinLink
    first_offer_key = next(iter(chunk["offers"]["actual"].keys()))
    chunk["offers"]["actual"][first_offer_key]["pinLink"] = "https://other-site.ru/invalid"

    snap = make_snapshot([(API_URL, json.dumps(data))])
    offers = parse_tutu(snap, q)

    # Предложение с некорректным URL должно получить url = None
    target_offer = next(o for o in offers if o.price == 19423)
    assert target_offer.url is None

    # Остальные предложения сохраняют валидные URL
    valid_offers = [o for o in offers if o.price != 19423]
    assert all(o.url is not None and o.url.startswith("https://avia.tutu.ru") for o in valid_offers)


def test_two_responses_selection():
    """При нескольких ответах побеждает ответ с большим summary.offersCurrentAmount (при равенстве более поздний)."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    data1 = json.loads(load_fixture_text("offers_first_mow_ala.json"))
    data2 = json.loads(load_fixture_text("offers_last_mow_ala.json"))

    # Настроим разные amounts: data1 имеет 50, data2 имеет 87
    data1[0]["summary"]["offersCurrentAmount"] = 50
    data2[0]["summary"]["offersCurrentAmount"] = 87

    # Порядок 1: data1 (50) затем data2 (87) -> побеждает data2
    snap1 = make_snapshot([
        (API_URL, json.dumps(data1)),
        (API_URL, json.dumps(data2)),
    ])
    offers1 = parse_tutu(snap1, q)
    assert [o.price for o in offers1] == [19423, 26178, 29110, 31687]

    # Порядок 2: data2 (87) затем data1 (50) -> побеждает data2 (с большим offersCurrentAmount)
    snap2 = make_snapshot([
        (API_URL, json.dumps(data2)),
        (API_URL, json.dumps(data1)),
    ])
    offers2 = parse_tutu(snap2, q)
    assert [o.price for o in offers2] == [19423, 26178, 29110, 31687]

    # Порядок 3: оба имеют одинаковый offersCurrentAmount (87) -> побеждает более поздний
    data1[0]["summary"]["offersCurrentAmount"] = 87
    data2[0]["summary"]["offersCurrentAmount"] = 87
    # Если сначала data2, затем data1 -> побеждает data1 (более поздний)
    snap3 = make_snapshot([
        (API_URL, json.dumps(data2)),
        (API_URL, json.dumps(data1)),
    ])
    offers3 = parse_tutu(snap3, q)
    assert [o.price for o in offers3] == [18754, 18991, 19423, 23240]


def test_results_complete():
    """Проверка results_complete: False на пустом/одном малом и True на двух или одном с offersCurrentAmount >= 20."""
    first_json = load_fixture_text("offers_first_mow_ala.json")
    last_json = load_fixture_text("offers_last_mow_ala.json")

    # Пустые / нерелевантные снимки -> False
    assert results_complete(None) is False
    assert results_complete(Snapshot("https://avia.tutu.ru", "", [])) is False
    assert results_complete(make_snapshot([("https://other-api.tutu.ru/avia", first_json)])) is False

    # Один ответ с малым числом предложений (< 20) -> False
    small_data = json.loads(first_json)
    small_data[0]["summary"]["offersCurrentAmount"] = 10
    snap_small = make_snapshot([(API_URL, json.dumps(small_data))])
    assert results_complete(snap_small) is False
    assert parse_tutu.results_complete(snap_small) is False

    # Один ответ с >= 20 предложений -> True
    snap_single_large = make_snapshot([(API_URL, first_json)])  # offersCurrentAmount = 87
    assert results_complete(snap_single_large) is True
    assert parse_tutu.results_complete(snap_single_large) is True

    # Два ответа с малым числом предложений -> True (есть хотя бы два ответа)
    snap_two_small = make_snapshot([
        (API_URL, json.dumps(small_data)),
        (API_URL, json.dumps(small_data)),
    ])
    assert results_complete(snap_two_small) is True
    assert parse_tutu.results_complete(snap_two_small) is True

    # Два ответа из фикстур -> True
    snap_both = make_snapshot([
        (API_URL, first_json),
        (API_URL, last_json),
    ])
    assert results_complete(snap_both) is True
    assert parse_tutu.results_complete(snap_both) is True


def test_empty_and_broken_json():
    """Битый или пустой JSON не должен ронять приложение."""
    q = Query("MOW", "ALA", date(2026, 12, 12))

    # Битый JSON
    snap_broken = make_snapshot([(API_URL, "broken json {[[")])
    assert parse_tutu(snap_broken, q) == []
    assert results_complete(snap_broken) is False

    # Пустой JSON-объект / список
    snap_empty = make_snapshot([(API_URL, "{}")])
    assert parse_tutu(snap_empty, q) == []
    assert results_complete(snap_empty) is False

    snap_empty_list = make_snapshot([(API_URL, "[]")])
    assert parse_tutu(snap_empty_list, q) == []
    assert results_complete(snap_empty_list) is False

    # Битый ответ + валидный ответ: валидный разбирается корректно
    first_json = load_fixture_text("offers_first_mow_ala.json")
    snap_mixed = make_snapshot([
        (API_URL, "broken json {[["),
        (API_URL, first_json),
    ])
    offers = parse_tutu(snap_mixed, q)
    assert len(offers) == 4


def test_broken_offers_and_variants_skipped():
    """Битые предложения и варианты пропускаются без исключений."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    data = json.loads(load_fixture_text("offers_first_mow_ala.json"))
    chunk = data[0]

    # Добавляем предложения с невалидными ценами, битыми маршрутами и сегментами
    actual = chunk["offers"]["actual"]
    actual["broken_price"] = {
        "id": "broken_price",
        "routeIds": ["unknown_route"],
        "offerVariants": [{"price": {"value": {"amount": "не_число"}}}],
    }
    actual["negative_price"] = {
        "id": "negative_price",
        "routeIds": ["unknown_route"],
        "offerVariants": [{"price": {"value": {"amount": -1000, "fraction": 100}}}],
    }
    actual["zero_fraction"] = {
        "id": "zero_fraction",
        "routeIds": ["unknown_route"],
        "offerVariants": [{"price": {"value": {"amount": 1000, "fraction": 0}}}],
    }
    actual["missing_route"] = {
        "id": "missing_route",
        "routeIds": ["route_not_in_dict"],
        "offerVariants": [{"price": {"value": {"amount": 100000, "fraction": 100}}}],
    }
    actual["empty_variants"] = {
        "id": "empty_variants",
        "routeIds": list(chunk["dictionary"]["common"]["routes"].keys())[:1],
        "offerVariants": [],
    }

    snap = make_snapshot([(API_URL, json.dumps(data))])
    offers = parse_tutu(snap, q)
    # Все 4 исходных валидных предложения остались, битые отброшены
    assert len(offers) == 4
    assert [o.price for o in offers] == [18754, 18991, 19423, 23240]
