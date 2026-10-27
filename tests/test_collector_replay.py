"""Тесты для механизма повтора запроса страницы (replay_post) и парсера Туту.

Все тесты работают без сети через подмену Playwright фейковыми объектами.
"""
from __future__ import annotations

from datetime import date
import inspect
import json
from pathlib import Path
from typing import Any
import pytest

from avia.collector import (
    CAPTCHA_DOM_JS,
    MAX_JSON_BYTES,
    MAX_RESULTS_BYTES,
    Snapshot,
    fetch_snapshot,
)
from avia.config import ROOT, SiteConfig
from avia.models import Query
from avia.parsers import PARSERS


# --- Фейковые объекты для эмуляции Playwright ---


class FakeRequest:
    """Фейковый объект запроса Playwright с методом, URL, заголовками и телом."""

    def __init__(
        self,
        url: str,
        method: str = "POST",
        post_data: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.method = method
        self.post_data = post_data
        self.headers = headers if headers is not None else {"content-type": "application/json"}


class FakeResponse:
    """Фейковый объект ответа Playwright."""

    def __init__(
        self,
        url: str,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self._body = body
        self.headers = headers if headers is not None else {"content-type": "application/json"}

    async def body(self) -> bytes:
        return self._body


class FakeReplayPage:
    """Фейковая страница Playwright с поддержкой on('request'), on('response') и evaluate."""

    def __init__(
        self,
        requests_on_goto: list[FakeRequest] | None = None,
        responses_on_goto: list[FakeResponse] | None = None,
        evaluate_result: Any = None,
        exc_on_evaluate: Exception | None = None,
        evaluate_captcha_dom: bool = False,
        inner_text: str = "Результаты поиска авиабилетов",
        url: str = "https://example.com/search",
    ) -> None:
        self.url = url
        self._requests_on_goto = requests_on_goto or []
        self._responses_on_goto = responses_on_goto or []
        self._evaluate_result = evaluate_result
        self._exc_on_evaluate = exc_on_evaluate
        self._evaluate_captcha_dom = evaluate_captcha_dom
        self._inner_text = inner_text
        self.request_callbacks: list = []
        self.response_callbacks: list = []
        self.evaluate_calls: list[tuple[str, Any]] = []
        self.replay_evaluated: bool = False

    def on(self, event: str, callback: Any) -> None:
        if event == "request":
            self.request_callbacks.append(callback)
        elif event == "response":
            self.response_callbacks.append(callback)

    async def goto(self, url: str, **kwargs: Any) -> None:
        # Эмулируем события запросов и ответов страницы при переходе
        for req in self._requests_on_goto:
            for cb in self.request_callbacks:
                res = cb(req)
                if inspect.isawaitable(res):
                    await res
        for resp in self._responses_on_goto:
            for cb in self.response_callbacks:
                res = cb(resp)
                if inspect.isawaitable(res):
                    await res

    async def wait_for_selector(self, selector: str, **kwargs: Any) -> None:
        pass

    async def wait_for_timeout(self, ms: int) -> None:
        pass

    class mouse:
        @staticmethod
        async def wheel(x: int, y: int) -> None:
            pass

    async def inner_text(self, selector: str) -> str:
        return self._inner_text

    async def evaluate(self, expression: str, *args: Any) -> Any:
        self.evaluate_calls.append((expression, args))
        # Проверка капчи через CAPTCHA_DOM_JS возвращает заданный флаг
        if expression == CAPTCHA_DOM_JS:
            return self._evaluate_captcha_dom

        # Вызов fetch для повтора запроса
        self.replay_evaluated = True
        if self._exc_on_evaluate:
            raise self._exc_on_evaluate
        if self._evaluate_result is not None:
            return self._evaluate_result
        return {"status": 200, "text": "{}"}

    async def query_selector_all(self, selector: str) -> list:
        return [object()] if self._evaluate_captcha_dom else []

    async def screenshot(self, path: str, **kwargs: Any) -> None:
        pass


class FakeContext:
    def __init__(self, page: FakeReplayPage) -> None:
        self.page = page
        self.pages = [page]
        self.browser = None

    async def new_page(self) -> FakeReplayPage:
        return self.page

    async def close(self) -> None:
        pass


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.context.browser = self

    async def new_context(self, **kwargs: Any) -> FakeContext:
        return self.context

    async def close(self) -> None:
        pass


class FakePlaywrightManager:
    def __init__(self, page: FakeReplayPage) -> None:
        self.ctx = FakeContext(page)
        self.browser = FakeBrowser(self.ctx)

        class FakeChromium:
            pass

        self.chromium = FakeChromium()

        async def launch(**kwargs: Any) -> FakeBrowser:
            return self.browser

        self.chromium.launch = launch

    async def __aenter__(self) -> FakePlaywrightManager:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


# --- Тесты повтора запроса (replay_post) ---


@pytest.mark.asyncio
async def test_replay_performed_once_with_modified_body(monkeypatch):
    """Повтор выполняется ровно один раз с изменённым телом (sorting/limit изменены, searchId сохранён, content-type тот же)."""
    orig_post_data = {
        "searchId": "sid_kuf_evn_12345",
        "sessionId": "sess_abcde",
        "pageId": "page_1",
        "sorting": "recommended",
        "limit": 10,
        "offset": 0,
        "isNewSearch": True,
        "routes": [{"from": "KUF", "to": "EVN"}],
    }

    matching_request = FakeRequest(
        url="https://offers-api.tutu.ru/avia/offers/v2",
        method="POST",
        post_data=json.dumps(orig_post_data),
        headers={"content-type": "application/json; charset=utf-8"},
    )
    # Второй запрос, который не должен перезаписать первый запомненный
    second_request = FakeRequest(
        url="https://offers-api.tutu.ru/avia/offers/v2",
        method="POST",
        post_data=json.dumps({"searchId": "sid_second"}),
        headers={"content-type": "application/json"},
    )
    other_request = FakeRequest(
        url="https://offers-api.tutu.ru/avia/analytics",
        method="POST",
        post_data="{}",
    )

    replay_response_payload = json.dumps({"summary": {"offersCurrentAmount": 70, "sorting": "cheapest"}})
    page = FakeReplayPage(
        requests_on_goto=[matching_request, second_request, other_request],
        evaluate_result={"status": 200, "text": replay_response_payload},
    )
    pw = FakePlaywrightManager(page)
    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig(
        name="tutu",
        url_template="https://avia.tutu.ru/search",
        settle_ms=100,
        scrolls=0,
        replay_post={
            "url_contains": "/avia/offers/v2",
            "set": {
                "sorting": "cheapest",
                "limit": 100,
                "offset": 0,
                "isNewSearch": False,
            },
        },
    )

    snap = await fetch_snapshot("https://avia.tutu.ru/search", site)

    # 1. Повтор через evaluate вызван ровно один раз
    fetch_calls = [(expr, args) for expr, args in page.evaluate_calls if expr != CAPTCHA_DOM_JS]
    assert len(fetch_calls) == 1
    _, args = fetch_calls[0]
    payload_arg = args[0]

    # 2. Проверка URL, метода и Content-Type
    assert payload_arg["url"] == "https://offers-api.tutu.ru/avia/offers/v2"
    assert payload_arg["method"] == "POST"
    headers = payload_arg["headers"]
    ctype = headers.get("Content-Type") or headers.get("content-type")
    assert ctype == "application/json; charset=utf-8"

    # 3. Проверка подстановки ключей из set и сохранения исходных полей
    replayed_body = json.loads(payload_arg["body"])
    assert replayed_body["sorting"] == "cheapest"
    assert replayed_body["limit"] == 100
    assert replayed_body["offset"] == 0
    assert replayed_body["isNewSearch"] is False
    assert replayed_body["searchId"] == "sid_kuf_evn_12345"
    assert replayed_body["sessionId"] == "sess_abcde"
    assert replayed_body["pageId"] == "page_1"
    assert replayed_body["routes"] == [{"from": "KUF", "to": "EVN"}]

    # 4. Ответ повтора сохранён в payloads
    assert ("https://offers-api.tutu.ru/avia/offers/v2", replay_response_payload) in snap.json_payloads
    assert snap.blocked is False


@pytest.mark.asyncio
async def test_replay_response_large_payload_added(monkeypatch):
    """Ответ повтора попадает в payloads, даже если его размер превышает MAX_JSON_BYTES (400 КБ)."""
    matching_request = FakeRequest(
        url="https://offers-api.tutu.ru/avia/offers/v2",
        method="POST",
        post_data=json.dumps({"searchId": "test_sid"}),
    )

    # Формируем тело ответа размером 600 КБ (> MAX_JSON_BYTES=400 КБ, но <= MAX_RESULTS_BYTES=8 МБ)
    large_response = json.dumps({"data": "x" * 600_000})
    assert len(large_response.encode("utf-8")) > MAX_JSON_BYTES
    assert len(large_response.encode("utf-8")) <= MAX_RESULTS_BYTES

    page = FakeReplayPage(
        requests_on_goto=[matching_request],
        evaluate_result={"status": 200, "text": large_response},
    )
    pw = FakePlaywrightManager(page)
    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig(
        name="tutu",
        url_template="https://avia.tutu.ru/search",
        settle_ms=100,
        scrolls=0,
        replay_post={
            "url_contains": "/avia/offers/v2",
            "set": {"sorting": "cheapest", "limit": 100},
        },
    )

    snap = await fetch_snapshot("https://avia.tutu.ru/search", site)

    # Крупный ответ повтора успешно добавлен в payloads
    saved_urls = [u for u, _ in snap.json_payloads]
    assert "https://offers-api.tutu.ru/avia/offers/v2" in saved_urls
    assert any(len(b) > MAX_JSON_BYTES for _, b in snap.json_payloads)


@pytest.mark.asyncio
async def test_replay_response_exceeding_max_results_bytes_not_added(monkeypatch):
    """Ответ повтора, превышающий MAX_RESULTS_BYTES (8 МБ), не добавляется в payloads."""
    matching_request = FakeRequest(
        url="https://offers-api.tutu.ru/avia/offers/v2",
        method="POST",
        post_data=json.dumps({"searchId": "test_sid"}),
    )

    # Тело ответа > 8 МБ
    huge_response = "a" * (MAX_RESULTS_BYTES + 10)
    page = FakeReplayPage(
        requests_on_goto=[matching_request],
        evaluate_result={"status": 200, "text": huge_response},
    )
    pw = FakePlaywrightManager(page)
    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig(
        name="tutu",
        url_template="https://avia.tutu.ru/search",
        settle_ms=100,
        scrolls=0,
        replay_post={
            "url_contains": "/avia/offers/v2",
            "set": {"sorting": "cheapest"},
        },
    )

    snap = await fetch_snapshot("https://avia.tutu.ru/search", site)

    # Ответ не должен попасть в payloads из-за превышения 8 МБ
    assert len(snap.json_payloads) == 0


@pytest.mark.asyncio
async def test_site_without_replay_post_does_not_evaluate_replay(monkeypatch):
    """Для сайта без replay_post evaluate для повтора запроса не вызывается, слушатель request не ставится."""
    post_req = FakeRequest(
        url="https://example.com/avia/offers/v2",
        method="POST",
        post_data=json.dumps({"searchId": "sid"}),
    )
    page = FakeReplayPage(requests_on_goto=[post_req])
    pw = FakePlaywrightManager(page)
    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig(
        name="regular_site",
        url_template="https://example.com",
        settle_ms=100,
        scrolls=0,
        replay_post=None,
    )

    snap = await fetch_snapshot("https://example.com", site)

    # Повтор запроса не выполнялся
    assert page.replay_evaluated is False
    fetch_calls = [(expr, args) for expr, args in page.evaluate_calls if expr != CAPTCHA_DOM_JS]
    assert len(fetch_calls) == 0
    assert len(page.request_callbacks) == 0
    assert snap.blocked is False


@pytest.mark.asyncio
async def test_replay_skipped_when_no_matching_request(monkeypatch):
    """Если подходящий POST-запрос не был перехвачен, снимок возвращается без повтора."""
    get_req = FakeRequest(
        url="https://offers-api.tutu.ru/avia/offers/v2",
        method="GET",
    )
    page = FakeReplayPage(requests_on_goto=[get_req])
    pw = FakePlaywrightManager(page)
    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig(
        name="tutu",
        url_template="https://avia.tutu.ru",
        settle_ms=100,
        scrolls=0,
        replay_post={"url_contains": "/avia/offers/v2", "set": {"sorting": "cheapest"}},
    )

    snap = await fetch_snapshot("https://avia.tutu.ru", site)

    assert page.replay_evaluated is False
    assert len(snap.json_payloads) == 0
    assert snap.blocked is False


@pytest.mark.asyncio
async def test_replay_skipped_when_post_data_invalid_json(monkeypatch):
    """Если тело перехваченного запроса не является корректным JSON, повтор не вызывается."""
    broken_req = FakeRequest(
        url="https://offers-api.tutu.ru/avia/offers/v2",
        method="POST",
        post_data="not-a-json-object",
    )
    page = FakeReplayPage(requests_on_goto=[broken_req])
    pw = FakePlaywrightManager(page)
    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig(
        name="tutu",
        url_template="https://avia.tutu.ru",
        settle_ms=100,
        scrolls=0,
        replay_post={"url_contains": "/avia/offers/v2", "set": {"sorting": "cheapest"}},
    )

    snap = await fetch_snapshot("https://avia.tutu.ru", site)

    assert page.replay_evaluated is False
    assert len(snap.json_payloads) == 0
    assert snap.blocked is False


@pytest.mark.asyncio
async def test_replay_swallows_evaluate_exception(monkeypatch):
    """Исключение при выполнении evaluate глотается, снимок возвращается как обычно."""
    matching_req = FakeRequest(
        url="https://offers-api.tutu.ru/avia/offers/v2",
        method="POST",
        post_data=json.dumps({"searchId": "sid"}),
    )
    page = FakeReplayPage(
        requests_on_goto=[matching_req],
        exc_on_evaluate=RuntimeError("JS context evaluation failed"),
    )
    pw = FakePlaywrightManager(page)
    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig(
        name="tutu",
        url_template="https://avia.tutu.ru",
        settle_ms=100,
        scrolls=0,
        replay_post={"url_contains": "/avia/offers/v2", "set": {"sorting": "cheapest"}},
    )

    snap = await fetch_snapshot("https://avia.tutu.ru", site)

    # Исключение не проброшено наружу, снимок вернулся целым
    assert snap.url == "https://example.com/search"
    assert len(snap.json_payloads) == 0
    assert snap.blocked is False


@pytest.mark.asyncio
async def test_replay_swallows_non_200_status(monkeypatch):
    """Если повтор вернул статус != 200, ответ не добавляется в payloads, ошибки не возникает."""
    matching_req = FakeRequest(
        url="https://offers-api.tutu.ru/avia/offers/v2",
        method="POST",
        post_data=json.dumps({"searchId": "sid"}),
    )
    page = FakeReplayPage(
        requests_on_goto=[matching_req],
        evaluate_result={"status": 403, "text": "Forbidden"},
    )
    pw = FakePlaywrightManager(page)
    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig(
        name="tutu",
        url_template="https://avia.tutu.ru",
        settle_ms=100,
        scrolls=0,
        replay_post={"url_contains": "/avia/offers/v2", "set": {"sorting": "cheapest"}},
    )

    snap = await fetch_snapshot("https://avia.tutu.ru", site)

    assert len(snap.json_payloads) == 0
    assert snap.blocked is False


@pytest.mark.asyncio
async def test_replay_not_performed_on_captcha_network(monkeypatch):
    """При обнаружении капчи в сети повтор не выполняется, snapshot.blocked=True."""
    matching_req = FakeRequest(
        url="https://offers-api.tutu.ru/avia/offers/v2",
        method="POST",
        post_data=json.dumps({"searchId": "sid"}),
    )
    captcha_resp = FakeResponse(
        url="https://example.com/smartcaptcha/verify",
        body=b"captcha challenge",
    )
    page = FakeReplayPage(
        requests_on_goto=[matching_req],
        responses_on_goto=[captcha_resp],
    )
    pw = FakePlaywrightManager(page)
    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig(
        name="tutu",
        url_template="https://avia.tutu.ru",
        settle_ms=100,
        scrolls=0,
        replay_post={"url_contains": "/avia/offers/v2", "set": {"sorting": "cheapest"}},
    )

    snap = await fetch_snapshot("https://avia.tutu.ru", site)

    assert page.replay_evaluated is False
    assert snap.blocked is True
    assert len(snap.json_payloads) == 0


@pytest.mark.asyncio
async def test_replay_not_performed_on_captcha_dom(monkeypatch):
    """При обнаружении капчи в DOM повтор не выполняется, snapshot.blocked=True."""
    matching_req = FakeRequest(
        url="https://offers-api.tutu.ru/avia/offers/v2",
        method="POST",
        post_data=json.dumps({"searchId": "sid"}),
    )
    page = FakeReplayPage(
        requests_on_goto=[matching_req],
        evaluate_captcha_dom=True,
    )
    pw = FakePlaywrightManager(page)
    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig(
        name="tutu",
        url_template="https://avia.tutu.ru",
        settle_ms=100,
        scrolls=0,
        replay_post={"url_contains": "/avia/offers/v2", "set": {"sorting": "cheapest"}},
    )

    snap = await fetch_snapshot("https://avia.tutu.ru", site)

    assert page.replay_evaluated is False
    assert snap.blocked is True
    assert len(snap.json_payloads) == 0


@pytest.mark.asyncio
async def test_replay_not_performed_on_captcha_text(monkeypatch):
    """При тексте блокировки на странице повтор не выполняется, snapshot.blocked=True."""
    matching_req = FakeRequest(
        url="https://offers-api.tutu.ru/avia/offers/v2",
        method="POST",
        post_data=json.dumps({"searchId": "sid"}),
    )
    page = FakeReplayPage(
        requests_on_goto=[matching_req],
        inner_text="Доступ заблокирован. Подтвердите, что вы не робот. Captcha",
    )
    pw = FakePlaywrightManager(page)
    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig(
        name="tutu",
        url_template="https://avia.tutu.ru",
        settle_ms=100,
        scrolls=0,
        replay_post={"url_contains": "/avia/offers/v2", "set": {"sorting": "cheapest"}},
    )

    snap = await fetch_snapshot("https://avia.tutu.ru", site)

    assert page.replay_evaluated is False
    assert snap.blocked is True
    assert len(snap.json_payloads) == 0


# --- Тест парсера Туту на фикстуре ---


def test_tutu_parser_on_rt_cheapest_kuf_evn_fixture():
    """Проверка парсера parse_tutu (зарегистрирован как 'tutu') на фикстуре offers_rt_cheapest_kuf_evn.json.

    Query('KUF', 'EVN', date(2026, 12, 8), return_date=date(2026, 12, 13))
    должен вернуть ровно 8 офферов, минимальная цена равна 33 481 руб., у каждого оффера есть back.
    """
    fixture_path = ROOT / "tests" / "fixtures" / "tutu" / "offers_rt_cheapest_kuf_evn.json"
    body = fixture_path.read_text(encoding="utf-8")

    snap = Snapshot(
        url="https://avia.tutu.ru/f/samara/erevan/?route[0]=119-08122026-174&route[1]=174-13122026-119",
        text="",
        json_payloads=[("https://offers-api.tutu.ru/avia/offers/v2", body)],
    )
    query = Query(
        "KUF",
        "EVN",
        date(2026, 12, 8),
        return_date=date(2026, 12, 13),
    )

    parser = PARSERS.get("tutu")
    assert parser is not None, "Парсер 'tutu' должен быть зарегистрирован в PARSERS"

    offers = parser(snap, query)

    # Ровно 8 офферов в разобранной выдаче
    assert len(offers) == 8

    # Минимальная цена равна 33481 (минимальная цена среди 8 офферов фикстуры)
    min_price = min(o.price for o in offers)
    assert min_price == 33481
    assert offers[0].price == 33481

    # У каждого оффера есть плечо обратно (back)
    for offer in offers:
        assert offer.site == "tutu"
        assert offer.back is not None
        assert offer.airline is not None
        assert offer.depart_time is not None
        assert offer.arrive_time is not None
        assert offer.back.airline is not None
        assert offer.back.depart_time is not None
        assert offer.back.arrive_time is not None
