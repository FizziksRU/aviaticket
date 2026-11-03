"""Тесты парсера Озон Тревел (Ozon Travel)."""
from datetime import date
import json
from pathlib import Path
import pytest

from avia.collector import Snapshot
from avia.models import Query
from avia.offers import compute_offer_id
from avia.parsers import PARSERS
from avia.parsers.ozon_travel import parse_ozon_travel, results_complete

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "ozon_travel"
WIDGET_URL = (FIXTURES_DIR / "widget_url.txt").read_text(encoding="utf-8").strip()
WIDGET_JSON = (FIXTURES_DIR / "search_oneway_kuf_evn_0112_widget.json").read_text(encoding="utf-8")
PAGE_TEXT = (FIXTURES_DIR / "search_oneway_kuf_evn_0112_page.txt").read_text(encoding="utf-8")


def _q(return_date: date | None = None) -> Query:
    """Создаёт тестовый запрос Самара (KUF) -> Ереван (EVN) на 01.12.2026."""
    return Query(
        origin="KUF",
        destination="EVN",
        depart=date(2026, 12, 1),
        adults=1,
        return_date=return_date,
    )


def make_snapshot(payloads: list[tuple[str, str]] | None = None, blocked: bool = False) -> Snapshot:
    """Создаёт тестовый Snapshot с сетевыми ответами виджета Озон Тревел."""
    if payloads is None:
        payloads = [(WIDGET_URL, WIDGET_JSON)]
    return Snapshot(
        url="https://www.ozon.ru/travel/flight/search",
        text=PAGE_TEXT,
        json_payloads=payloads,
        blocked=blocked,
    )


def test_parser_registered():
    """Парсер ozon_travel должен быть зарегистрирован в реестре PARSERS."""
    assert "ozon_travel" in PARSERS
    assert PARSERS["ozon_travel"] is parse_ozon_travel
    assert getattr(parse_ozon_travel, "results_complete", None) is results_complete


def test_parse_ozon_travel_count_and_cheapest():
    """Число офферов равно числу карточек (30), самый дешёвый билет стоит 11586 ₽."""
    snap = make_snapshot()
    offers = parse_ozon_travel(snap, _q())

    assert len(offers) == 30
    assert offers[0].price == 11586
    assert min(o.price for o in offers) == 11586
    # Отсортированы по возрастанию цены
    assert [o.price for o in offers] == sorted(o.price for o in offers)


def test_parse_ozon_travel_first_offer_fields():
    """Проверка всех полей первой карточки (17:35 – 14:00, Победа + Аэрофлот, 1 пересадка, Москва 14.5ч, без багажа)."""
    snap = make_snapshot()
    offers = parse_ozon_travel(snap, _q())
    first = offers[0]

    assert first.site == "ozon_travel"
    assert first.price == 11586
    assert first.currency == "RUB"
    assert first.airline == "Победа, Аэрофлот"
    assert first.depart_time == "17:35"
    assert first.arrive_time == "14:00"
    # Вылет 2026-12-01, прилёт на следующий день (+1 день из пометки ⁺¹)
    assert first.depart_at == "2026-12-01T17:35"
    assert first.arrive_at == "2026-12-02T14:00"
    assert first.duration == "20ч 25м"
    assert first.transfers == 1
    assert first.layovers == ["Москва, 14.5ч"]
    # В карточке luggageText='Багаж 10 кг <b>+6 181 ₽</b>', доплата за багаж => 'без багажа'
    assert first.baggage == "без багажа"
    assert first.url is not None
    assert first.url.startswith("https://www.ozon.ru/travel/modal/aviaChooseTariffWithFlightDetails?")
    assert first.flight_numbers == ["DP6582", "SU1866"]
    assert len(first.offer_id) == 12
    assert first.offer_id == compute_offer_id(first)


def test_parse_direct_flight_if_present_or_synthetic():
    """Проверка прямого рейса: в фикстуре все рейсы с пересадками, но 'Прямой' корректно разбирается."""
    snap = make_snapshot()
    offers = parse_ozon_travel(snap, _q())
    # В реальной фикстуре Самара-Ереван на 01.12.2026 прямых рейсов нет
    direct_in_fixture = [o for o in offers if o.transfers == 0]
    assert len(direct_in_fixture) == 0

    # Проверяем синтетическую карточку с "Прямой"
    base_data = json.loads(WIDGET_JSON)
    card_copy = dict(base_data["state"]["flights"][0])
    way_copy = dict(card_copy["wayThereDetails"])
    way_copy["transfers"] = [{"city": {"text": "Прямой"}}]
    card_copy["wayThereDetails"] = way_copy

    synthetic_payload = json.dumps({"state": {"flights": [card_copy]}})
    snap_direct = make_snapshot([(WIDGET_URL, synthetic_payload)])
    direct_offers = parse_ozon_travel(snap_direct, _q())

    assert len(direct_offers) == 1
    assert direct_offers[0].transfers == 0
    assert direct_offers[0].layovers is None


def test_baggage_included_when_no_surcharge():
    """Если багаж включён в стоимость без доплаты, проставляется 'с багажом'."""
    base_data = json.loads(WIDGET_JSON)
    card_copy = dict(base_data["state"]["flights"][0])
    card_copy["luggageText"] = {"text": "Багаж 23 кг"}

    synthetic_payload = json.dumps({"state": {"flights": [card_copy]}})
    snap_baggage = make_snapshot([(WIDGET_URL, synthetic_payload)])
    offers = parse_ozon_travel(snap_baggage, _q())

    assert len(offers) == 1
    assert offers[0].baggage == "с багажом"

    # Проверка текста вида "Багаж + ручная кладь": плюс без суммы не считается доплатой
    card_combo = dict(base_data["state"]["flights"][0])
    card_combo["luggageText"] = {"text": "Багаж + ручная кладь"}
    snap_combo = make_snapshot([(WIDGET_URL, json.dumps({"state": {"flights": [card_combo]}}))])
    offers_combo = parse_ozon_travel(snap_combo, _q())
    assert len(offers_combo) == 1
    assert offers_combo[0].baggage == "с багажом"


def test_time_difference_date_offsets():
    """Сдвиг даты прилёта корректно обрабатывает надстрочные индексы и знаки (+ и -)."""
    base_data = json.loads(WIDGET_JSON)

    # Рейс со сдвигом на -1 день (например, пересечение линии перемены дат)
    card_minus = dict(base_data["state"]["flights"][0])
    way_minus = dict(card_minus["wayThereDetails"])
    way_minus["timeDifference"] = {"text": "⁻¹"}
    card_minus["wayThereDetails"] = way_minus
    snap_minus = make_snapshot([(WIDGET_URL, json.dumps({"state": {"flights": [card_minus]}}))])
    offers_minus = parse_ozon_travel(snap_minus, _q())
    assert len(offers_minus) == 1
    assert offers_minus[0].depart_at == "2026-12-01T17:35"
    assert offers_minus[0].arrive_at == "2026-11-30T14:00"

    # Рейс со сдвигом на +2 дня
    card_plus2 = dict(base_data["state"]["flights"][0])
    way_plus2 = dict(card_plus2["wayThereDetails"])
    way_plus2["timeDifference"] = {"text": "⁺²"}
    card_plus2["wayThereDetails"] = way_plus2
    snap_plus2 = make_snapshot([(WIDGET_URL, json.dumps({"state": {"flights": [card_plus2]}}))])
    offers_plus2 = parse_ozon_travel(snap_plus2, _q())
    assert len(offers_plus2) == 1
    assert offers_plus2[0].depart_at == "2026-12-01T17:35"
    assert offers_plus2[0].arrive_at == "2026-12-03T14:00"


def test_empty_snapshot_returns_empty_list():
    """Пустой snapshot или snapshot без ответов возвращает пустой список."""
    assert parse_ozon_travel(None, _q()) == []
    assert parse_ozon_travel(Snapshot(url="", text=""), _q()) == []
    assert parse_ozon_travel(Snapshot(url="", text="", json_payloads=[]), _q()) == []
    assert parse_ozon_travel(make_snapshot(blocked=True), _q()) == []


def test_async_error_without_flights_returns_empty_list():
    """Ответ с asyncErrorMessage без карточек flights игнорируется и даёт []."""
    error_payload = json.dumps({
        "state": {
            "asyncErrorMessage": {
                "title": {"text": "Что-то пошло не так"},
                "message": {"text": "Попробуйте возобновить поиск"},
            },
            "flights": [],
        }
    })
    snap_err = make_snapshot([(WIDGET_URL, error_payload)])
    assert parse_ozon_travel(snap_err, _q()) == []

    # Также проверяем случай, когда flights вовсе отсутствует
    error_payload_no_flights = json.dumps({
        "state": {
            "asyncErrorMessage": {
                "title": {"text": "Ошибка поиска"},
            }
        }
    })
    snap_no_flights = make_snapshot([(WIDGET_URL, error_payload_no_flights)])
    assert parse_ozon_travel(snap_no_flights, _q()) == []


def test_return_date_returns_empty_list():
    """Для запроса туда-обратно (query.return_date) парсер пока возвращает []."""
    snap = make_snapshot()
    q_round = _q(return_date=date(2026, 12, 10))
    assert parse_ozon_travel(snap, q_round) == []


def test_select_best_payload_among_multiple():
    """При наличии нескольких ответов виджета выбирается ответ с наибольшим числом карточек (при равенстве более поздний)."""
    base_data = json.loads(WIDGET_JSON)
    all_flights = base_data["state"]["flights"]

    payload_5 = json.dumps({"state": {"flights": all_flights[:5]}})
    payload_30 = json.dumps({"state": {"flights": all_flights}})

    # Ответ с 30 карточками побеждает ответ с 5
    snap = make_snapshot([
        (WIDGET_URL, payload_5),
        (WIDGET_URL, payload_30),
    ])
    offers = parse_ozon_travel(snap, _q())
    assert len(offers) == 30

    # Если порядок обратный: сначала 30, потом 5 — побеждает 30
    snap_rev = make_snapshot([
        (WIDGET_URL, payload_30),
        (WIDGET_URL, payload_5),
    ])
    offers_rev = parse_ozon_travel(snap_rev, _q())
    assert len(offers_rev) == 30

    # При равенстве побеждает более поздний ответ
    card1 = dict(all_flights[0])
    card1["price"] = {"price": [{"text": "10 000 ₽", "textStyle": "PRICE"}]}
    payload_a = json.dumps({"state": {"flights": [card1]}})

    card2 = dict(all_flights[0])
    card2["price"] = {"price": [{"text": "9 000 ₽", "textStyle": "PRICE"}]}
    payload_b = json.dumps({"state": {"flights": [card2]}})

    snap_equal = make_snapshot([
        (WIDGET_URL, payload_a),
        (WIDGET_URL, payload_b),
    ])
    offers_equal = parse_ozon_travel(snap_equal, _q())
    assert len(offers_equal) == 1
    assert offers_equal[0].price == 9000

    # Если после хорошего ответа пришёл ответ с asyncErrorMessage без flights — он игнорируется
    err_payload = json.dumps({
        "state": {
            "asyncErrorMessage": {"title": {"text": "Ошибка"}},
            "flights": [],
        }
    })
    snap_with_trailing_err = make_snapshot([
        (WIDGET_URL, payload_30),
        (WIDGET_URL, err_payload),
    ])
    offers_after_err = parse_ozon_travel(snap_with_trailing_err, _q())
    assert len(offers_after_err) == 30


def test_results_complete():
    """Проверка results_complete: True при наличии карточек flights, False при незавершённом поиске или блокировке."""
    snap = make_snapshot()
    assert results_complete(snap) is True

    # Пустой snapshot или None
    assert results_complete(None) is False
    assert results_complete(Snapshot(url="", text="")) is False

    # Блокировка капчей
    assert results_complete(make_snapshot(blocked=True)) is False

    # Неподходящий URL виджета
    snap_other_url = make_snapshot([("https://www.ozon.ru/api/other", WIDGET_JSON)])
    assert results_complete(snap_other_url) is False

    # Ответ с пустым списком flights
    empty_flights_payload = json.dumps({"state": {"flights": []}})
    assert results_complete(make_snapshot([(WIDGET_URL, empty_flights_payload)])) is False

    # Битый JSON
    assert results_complete(make_snapshot([(WIDGET_URL, "{broken json")])) is False


def test_offer_id_stability_and_uniqueness():
    """offer_id стабилен при повторном разборе и уникален для всех карточек фикстуры."""
    snap = make_snapshot()
    run1 = parse_ozon_travel(snap, _q())
    run2 = parse_ozon_travel(snap, _q())

    ids1 = [o.offer_id for o in run1]
    ids2 = [o.offer_id for o in run2]

    assert ids1 == ids2
    assert len(set(ids1)) == 30
