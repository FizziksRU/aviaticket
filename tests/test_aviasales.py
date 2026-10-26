from datetime import date
from pathlib import Path
import json
import pytest

from avia.collector import Snapshot
from avia.formatting import format_report
from avia.models import Offer, Query, SiteResult
from avia.parsers import PARSERS
from avia.parsers.aviasales import parse_aviasales, results_complete

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "aviasales"
RESULTS_URL = "https://www.aviasales.ru/search/v3.2/results?search_id=01a17a0e-c6fd-8031-68ac-a7017f49fd7b"


def load_fixture_text(name: str = "results_mow_ala.json") -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_snapshot(payloads: list[tuple[str, str]] | None = None) -> Snapshot:
    if payloads is None:
        payloads = [(RESULTS_URL, load_fixture_text())]
    return Snapshot(
        url="https://www.aviasales.ru/search/MOW1012ALA1",
        text="Выдача билетов",
        json_payloads=payloads,
    )


def test_parser_registered():
    """Парсер aviasales должен быть зарегистрирован в PARSERS."""
    assert "aviasales" in PARSERS
    assert PARSERS["aviasales"] is parse_aviasales


def test_parse_aviasales_count_and_min_price():
    """Проверка числа билетов и минимальной цены по реальной фикстуре."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    snap = make_snapshot()
    offers = parse_aviasales(snap, q)

    # 4 билета в фикстуре
    assert len(offers) == 4
    # Отсортированы по возрастанию цены: минимальная цена 20147
    assert offers[0].price == 20147
    assert offers[0].currency == "RUB"


def test_parse_aviasales_ticket_details():
    """Проверка времени, авиакомпании, пересадок и длительности конкретных билетов."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    snap = make_snapshot()
    offers = parse_aviasales(snap, q)

    # Самый дешевый билет (S7 Airlines)
    s7 = offers[0]
    assert s7.price == 20147
    assert s7.airline == "S7 Airlines"
    assert s7.depart_time == "23:00"
    assert s7.arrive_time == "12:15"
    assert s7.duration == "11ч 15м"
    assert s7.transfers == 1
    assert s7.baggage == "без багажа"

    # Билет авиакомпании Azal (цена 23842)
    azal = next(o for o in offers if o.price == 23842)
    assert azal.airline == "Azal"
    assert azal.depart_time == "00:55"
    assert azal.arrive_time == "01:35"
    assert azal.duration == "22ч 40м"
    assert azal.transfers == 1
    assert azal.baggage == "без багажа"

    # Билет Uzbekistan Airways (цена 35306)
    uzb = next(o for o in offers if o.price == 35306)
    assert uzb.airline == "Uzbekistan Airways"
    assert uzb.depart_time == "22:50"
    assert uzb.arrive_time == "09:35"
    assert uzb.duration == "8ч 45м"
    assert uzb.transfers == 1
    assert uzb.baggage == "без багажа"


def test_parse_aviasales_baggage_inclusion():
    """Проверка определения багажа: с багажом vs без багажа."""
    q = Query("MOW", "ALA", date(2026, 12, 10))

    # Формируем чанк с билетом, в котором предложение включает багаж
    chunk = {
        "chunk_id": "results",
        "last_update_timestamp": 1000,
        "flight_legs": [
            {
                "local_departure_date_time": "2026-12-10 10:00",
                "local_arrival_date_time": "2026-12-10 14:00",
                "departure_unix_timestamp": 1700000000,
                "arrival_unix_timestamp": 1700014400,
                "operating_carrier_designator": {"carrier": "SU"},
            }
        ],
        "airlines": {
            "SU": {"name": {"ru": {"default": "Аэрофлот"}}}
        },
        "tickets": [
            {
                "id": "ticket_with_baggage",
                "segments": [{"flights": [0]}],
                "proposals": [
                    {
                        "price": {"value": 15000, "currency_code": "RUB"},
                        "flight_terms": {"0": {"total_baggage_count": 1}},
                    }
                ],
            },
            {
                "id": "ticket_without_baggage",
                "segments": [{"flights": [0]}],
                "proposals": [
                    {
                        "price": {"value": 12000, "currency_code": "RUB"},
                        "flight_terms": {"0": {"total_baggage_count": 0}},
                    }
                ],
            },
        ],
    }

    payload = [(RESULTS_URL, json.dumps([chunk]))]
    snap = make_snapshot(payload)
    offers = parse_aviasales(snap, q)

    assert len(offers) == 2
    no_bag = next(o for o in offers if o.price == 12000)
    with_bag = next(o for o in offers if o.price == 15000)
    assert no_bag.baggage == "без багажа"
    assert with_bag.baggage == "с багажом"


def test_parse_aviasales_deduplication():
    """Проверка дедупликации чанков по id/signature: берётся более свежий по last_update_timestamp."""
    q = Query("MOW", "ALA", date(2026, 12, 10))

    chunk_old = {
        "chunk_id": "results",
        "last_update_timestamp": 100,
        "flight_legs": [
            {
                "local_departure_date_time": "2026-12-10 10:00",
                "local_arrival_date_time": "2026-12-10 14:00",
                "departure_unix_timestamp": 1700000000,
                "arrival_unix_timestamp": 1700014400,
                "operating_carrier_designator": {"carrier": "SU"},
            }
        ],
        "airlines": {"SU": {"name": {"ru": {"default": "Аэрофлот"}}}},
        "tickets": [
            {
                "id": "same_ticket_id",
                "signature": "sig1",
                "segments": [{"flights": [0]}],
                "proposals": [{"price": {"value": 25000, "currency_code": "RUB"}}],
            }
        ],
    }

    chunk_new = {
        "chunk_id": "results",
        "last_update_timestamp": 200,
        "flight_legs": [
            {
                "local_departure_date_time": "2026-12-10 10:00",
                "local_arrival_date_time": "2026-12-10 14:00",
                "departure_unix_timestamp": 1700000000,
                "arrival_unix_timestamp": 1700014400,
                "operating_carrier_designator": {"carrier": "SU"},
            }
        ],
        "airlines": {"SU": {"name": {"ru": {"default": "Аэрофлот"}}}},
        "tickets": [
            {
                "id": "same_ticket_id",
                "signature": "sig1",
                "segments": [{"flights": [0]}],
                "proposals": [{"price": {"value": 21000, "currency_code": "RUB"}}],
            }
        ],
    }

    # Подаем сначала старый, потом новый чанк
    payload = [(RESULTS_URL, json.dumps([chunk_old, chunk_new]))]
    snap = make_snapshot(payload)
    offers = parse_aviasales(snap, q)
    assert len(offers) == 1
    assert offers[0].price == 21000

    # Подаем сначала новый, потом старый — более свежий не должен перезаписаться
    payload_reversed = [(RESULTS_URL, json.dumps([chunk_new, chunk_old]))]
    snap_rev = make_snapshot(payload_reversed)
    offers_rev = parse_aviasales(snap_rev, q)
    assert len(offers_rev) == 1
    assert offers_rev[0].price == 21000


def test_parse_aviasales_broken_json_and_tickets():
    """Битый JSON не роняет парсер; битые билеты пропускаются."""
    q = Query("MOW", "ALA", date(2026, 12, 10))

    # Битый JSON
    snap_broken_json = make_snapshot([(RESULTS_URL, "Not a valid json {{{{")])
    assert parse_aviasales(snap_broken_json, q) == []

    # Чанк с битым билетом и валидным билетом
    chunk_with_errors = {
        "chunk_id": "results",
        "last_update_timestamp": 100,
        "flight_legs": [
            {
                "local_departure_date_time": "2026-12-10 10:00",
                "local_arrival_date_time": "2026-12-10 14:00",
                "departure_unix_timestamp": 1700000000,
                "arrival_unix_timestamp": 1700014400,
                "operating_carrier_designator": {"carrier": "SU"},
            }
        ],
        "airlines": {"SU": {"name": {"ru": {"default": "Аэрофлот"}}}},
        "tickets": [
            {"id": "bad_ticket_1", "segments": []},  # нет сегментов
            {"id": "bad_ticket_2", "segments": [{"flights": [999]}]},  # индекс вне границ
            {"id": "bad_ticket_3", "segments": [{"flights": [0]}], "proposals": []},  # нет proposals
            {
                "id": "good_ticket",
                "segments": [{"flights": [0]}],
                "proposals": [{"price": {"value": 18000, "currency_code": "RUB"}}],
            },
        ],
    }

    snap_mixed = make_snapshot([(RESULTS_URL, json.dumps([chunk_with_errors]))])
    offers = parse_aviasales(snap_mixed, q)
    assert len(offers) == 1
    assert offers[0].price == 18000


def test_parse_aviasales_empty_results():
    """Пустой список payloads или чанков даёт []."""
    q = Query("MOW", "ALA", date(2026, 12, 10))

    # Пустые payloads
    assert parse_aviasales(Snapshot("url", "", []), q) == []

    # Неподходящий URL
    assert parse_aviasales(Snapshot("url", "", [("https://other.com/api", "{}")]), q) == []

    # Пустой массив чанков
    assert parse_aviasales(make_snapshot([(RESULTS_URL, "[]")]), q) == []

    # Чанк с пустыми билетами
    assert parse_aviasales(make_snapshot([(RESULTS_URL, json.dumps([{"chunk_id": "results", "tickets": []}]))]), q) == []


def test_results_complete():
    """Проверка results_complete на синтетических вариантах и реальной фикстуре."""
    # Реальная фикстура не имеет признака завершения
    snap_real = make_snapshot()
    assert results_complete(snap_real) is False

    # Синтетический чанк с is_over=True
    snap_over = make_snapshot([(RESULTS_URL, json.dumps([{"chunk_id": "results", "is_over": True}]))])
    assert results_complete(snap_over) is True

    # Синтетический чанк с meta.is_over=True
    snap_meta_over = make_snapshot([(RESULTS_URL, json.dumps([{"chunk_id": "results", "meta": {"is_over": True}}]))])
    assert results_complete(snap_meta_over) is True

    # Синтетический чанк с completed=True
    snap_completed = make_snapshot([(RESULTS_URL, json.dumps([{"chunk_id": "results", "completed": True}]))])
    assert results_complete(snap_completed) is True

    # Синтетический чанк с is_complete=True
    snap_is_complete = make_snapshot([(RESULTS_URL, json.dumps([{"chunk_id": "results", "is_complete": True}]))])
    assert results_complete(snap_is_complete) is True

    # Синтетический чанк с is_over=False
    snap_not_over = make_snapshot([(RESULTS_URL, json.dumps([{"chunk_id": "results", "is_over": False}]))])
    assert results_complete(snap_not_over) is False

    # Пустой список
    assert results_complete(Snapshot("url", "", [])) is False


def test_partial_in_formatting():
    """Пометка о неполной выдаче должна попадать в форматированный отчет."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    offer = Offer(
        site="aviasales",
        price=20000,
        currency="RUB",
        airline="S7 Airlines",
        depart_time="10:00",
        arrive_time="14:00",
        duration="4ч 00м",
        transfers=0,
    )

    # При partial=True строка должна присутствовать
    res_partial = SiteResult("aviasales", True, [offer], page_url="https://aviasales.ru", partial=True)
    report_partial = format_report(q, [res_partial])
    assert "неполная выдача: сайт ещё догружал варианты" in report_partial

    # При partial=False строки быть не должно
    res_full = SiteResult("aviasales", True, [offer], page_url="https://aviasales.ru", partial=False)
    report_full = format_report(q, [res_full])
    assert "неполная выдача: сайт ещё догружал варианты" not in report_full

    # При partial=True и отсутствии билетов строка также должна присутствовать
    res_empty_partial = SiteResult("aviasales", True, [], partial=True)
    report_empty = format_report(q, [res_empty_partial])
    assert "неполная выдача: сайт ещё догружал варианты" in report_empty


@pytest.mark.asyncio
async def test_pipeline_sets_partial(monkeypatch):
    """Pipeline выставляет partial=True, если results_complete возвращает False."""
    from avia.config import SiteConfig, Settings
    from avia.pipeline import search

    q = Query("MOW", "ALA", date(2026, 12, 10))

    # Создаем фиктивный парсер с results_complete=False
    def dummy_parser_incomplete(snap, query):
        return [Offer("test_site", 10000)]

    dummy_parser_incomplete.results_complete = lambda snap: False

    # Создаем фиктивный парсер с results_complete=True
    def dummy_parser_complete(snap, query):
        return [Offer("test_site", 10000)]

    dummy_parser_complete.results_complete = lambda snap: True

    # Регистрируем временно в PARSERS
    monkeypatch.setitem(PARSERS, "test_incomplete", dummy_parser_incomplete)
    monkeypatch.setitem(PARSERS, "test_complete", dummy_parser_complete)

    # Мокаем fetch_snapshot, чтобы не запускать реальный браузер
    async def mock_fetch_snapshot(url, site, **kwargs):
        return Snapshot(url=url, text="", json_payloads=[])

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", mock_fetch_snapshot)

    site_inc = SiteConfig(name="site_inc", url_template="https://x.com", parser="test_incomplete")
    site_comp = SiteConfig(name="site_comp", url_template="https://x.com", parser="test_complete")

    settings = Settings(sites=[site_inc, site_comp])
    results = await search(q, settings)

    assert len(results) == 2
    res_inc = next(r for r in results if r.site == "site_inc")
    res_comp = next(r for r in results if r.site == "site_comp")

    assert res_inc.partial is True
    assert res_comp.partial is False


@pytest.mark.asyncio
async def test_collector_response_filter_and_polling(monkeypatch):
    """Collector сохраняет короткие ответы для search/v3.2/results и останавливает опрос по is_over."""
    from avia.collector import fetch_snapshot
    from avia.config import SiteConfig

    captured_callbacks = []
    timeouts_waited = []

    class MockResp:
        def __init__(self, url, body_bytes, ctype="application/json"):
            self.url = url
            self._body = body_bytes
            self.headers = {"content-type": ctype}

        async def body(self):
            return self._body

    class MockPage:
        def __init__(self):
            self.url = "https://aviasales.ru"

        def on(self, event, callback):
            captured_callbacks.append(callback)

        async def goto(self, url, **kwargs):
            cb = captured_callbacks[0]
            # 1. Короткий ответ aviasales (< 200 байт) с is_over
            await cb(MockResp("https://aviasales.ru/search/v3.2/results?id=1", b'{"is_over": true}'))
            # 2. Короткий ответ не-aviasales (< 200 байт) - должен быть отброшен
            await cb(MockResp("https://site.ru/search/fares", b'{"short": 1}'))
            # 3. Длинный ответ не-aviasales (> 200 байт) - должен быть сохранен
            await cb(MockResp("https://site.ru/search/fares", b'{"big": "' + b"x" * 250 + b'"}'))

        async def wait_for_timeout(self, ms):
            timeouts_waited.append(ms)

        async def inner_text(self, selector):
            return "page text"

        class mouse:
            @staticmethod
            async def wheel(x, y):
                pass

    class MockCtx:
        async def new_page(self):
            return MockPage()

    class MockBrowser:
        async def new_context(self, **kwargs):
            return MockCtx()

        async def close(self):
            pass

    class MockPW:
        class chromium:
            @staticmethod
            async def launch(**kwargs):
                return MockBrowser()

    class MockPWCM:
        async def __aenter__(self):
            return MockPW()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("avia.collector.async_playwright", lambda: MockPWCM())

    site = SiteConfig("aviasales", "https://aviasales.ru/search", parser="aviasales", settle_ms=10000, scrolls=0)
    snap = await fetch_snapshot("https://aviasales.ru/search", site)

    urls = [u for u, _ in snap.json_payloads]
    # Короткий ответ aviasales сохранен
    assert any("/search/v3.2/results" in u for u in urls)
    # Короткий ответ не-aviasales отброшен
    assert not any(u == "https://site.ru/search/fares" and "short" in b for u, b in snap.json_payloads)
    # Обычный ответ не-aviasales сохранен
    assert any("big" in b for u, b in snap.json_payloads)
    # Опрос прервался сразу по is_over, не дожидаясь settle_ms
    assert len(timeouts_waited) == 0
