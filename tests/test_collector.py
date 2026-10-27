"""Тесты для src/avia/collector.py и интеграции с src/avia/recon.py.

Все тесты работают без сети и без реального Playwright через чистые функции
и подмену async_playwright фейковыми объектами.
"""
from __future__ import annotations

from pathlib import Path
import pytest

from avia.collector import (
    BLOCK_MARKERS,
    LAST_UPDATE_ZERO_RE,
    MAX_JSON_BYTES,
    MAX_RESULTS_BYTES,
    RESULTS_URL_MARKERS,
    Snapshot,
    check_captcha_dom,
    decide_blocked,
    fetch_snapshot,
    has_is_over,
    has_search_tickets,
    has_tickets,
    is_blocked,
    is_captcha_url,
    is_search_over,
    should_save_response,
)
from avia.config import SiteConfig


# --- Тесты чистых функций ---


def test_is_blocked_detection():
    """Проверка определения блокировки/капчи по тексту страницы."""
    # Обычный текст выдачи — не заблокирован
    assert not is_blocked("Купить билет Москва — Сочи от 5000 рублей")
    assert not is_blocked("")

    # Все ключевые маркеры при длине текста < 3000
    for marker in BLOCK_MARKERS:
        assert is_blocked(f"Внимание: {marker.upper()}! Доступ ограничен.")
        assert is_blocked(f"Текст страницы {marker} текст страницы")

    # Смешанный регистр
    assert is_blocked("Пожалуйста, Подтвердите, Что Вы Не Робот для продолжения")
    assert is_blocked("Ошибка 403: Access Denied")
    assert is_blocked("Please confirm: Are You A Human?")

    # Если текст длинный (>= 3000 символов), маркер в тексте (например в футере/правилах) не считается блокировкой
    long_text = "нормальная выдача с ценами " * 200 + " captcha "
    assert len(long_text) >= 3000
    assert not is_blocked(long_text)


def test_should_save_response_results_endpoint():
    """Для URL '/search/v3.2/results' сохраняются любые ответы от 0 до MAX_RESULTS_BYTES."""
    results_url = "https://aviasales.ru/search/v3.2/results?uuid=123"

    # Короткие ответы
    assert should_save_response(results_url, 0) is True
    assert should_save_response(results_url, 15) is True
    assert should_save_response(results_url, 150) is True

    # Стандартные ответы
    assert should_save_response(results_url, 500) is True
    assert should_save_response(results_url, MAX_JSON_BYTES) is True

    # Ответы крупнее MAX_JSON_BYTES (400 КБ), включая реальные 1.0-1.3 МБ и до потолка MAX_RESULTS_BYTES
    assert should_save_response(results_url, MAX_JSON_BYTES + 1) is True
    assert should_save_response(results_url, 1_200_000) is True
    assert should_save_response(results_url, MAX_RESULTS_BYTES) is True

    # Превышение отдельного потолка MAX_RESULTS_BYTES или отрицательная длина
    assert should_save_response(results_url, MAX_RESULTS_BYTES + 1) is False
    assert should_save_response(results_url, -1) is False


def test_should_save_response_other_urls():
    """Для прочих URL сохранение зависит от наличия подсказок в URL и длины > 200 байт."""
    # URL с ключевыми словами
    for word in ("search", "ticket", "price", "offer", "fare", "avia", "flight", "result"):
        url = f"https://example.com/api/v1/{word}/data"
        # Короткий ответ (<= 200) отбрасывается
        assert should_save_response(url, 0) is False
        assert should_save_response(url, 200) is False
        # Длинный ответ (> 200 и <= MAX_JSON_BYTES) сохраняется
        assert should_save_response(url, 201) is True
        assert should_save_response(url, 10_000) is True
        # Превышающий максимум MAX_JSON_BYTES (400 КБ) для чужого URL отбрасывается
        assert should_save_response(url, MAX_JSON_BYTES + 1) is False
        assert should_save_response(url, 1_200_000) is False
        assert should_save_response(url, MAX_RESULTS_BYTES) is False

    # URL без ключевых слов отбрасывается независимо от длины
    assert should_save_response("https://example.com/static/style.css", 500) is False
    assert should_save_response("https://example.com/analytics/collect", 5000) is False
    assert should_save_response("https://example.com/image.png", 50) is False


def test_should_save_response_yandex_endpoint():
    """Для URL Яндекс Путешествий (/api/avia/search/results) сохраняются ответы до MAX_RESULTS_BYTES."""
    yandex_url = "https://travel.yandex.ru/api/avia/search/results?qid=123&cont=0"

    # Ответы любой длины от 0 до MAX_RESULTS_BYTES (8 МБ) сохраняются без проверки JSON_URL_HINT
    assert should_save_response(yandex_url, 0) is True
    assert should_save_response(yandex_url, 150) is True
    assert should_save_response(yandex_url, 500) is True
    assert should_save_response(yandex_url, MAX_JSON_BYTES) is True
    assert should_save_response(yandex_url, 1_700_000) is True  # типичный чанк Яндекса (~1.7 МБ)
    assert should_save_response(yandex_url, MAX_RESULTS_BYTES) is True

    # Свыше 8 МБ или отрицательная длина отбрасываются
    assert should_save_response(yandex_url, MAX_RESULTS_BYTES + 1) is False
    assert should_save_response(yandex_url, -1) is False

    # Чужой URL работает по-старому (до 400 КБ и с хинтом)
    foreign_hint = "https://example.com/search/fares"
    foreign_no_hint = "https://example.com/static/bundle.js"
    assert should_save_response(foreign_no_hint, 500) is False
    assert should_save_response(foreign_hint, 10_000) is True
    assert should_save_response(foreign_hint, MAX_JSON_BYTES + 1) is False
    assert should_save_response(foreign_hint, MAX_RESULTS_BYTES) is False


def test_has_is_over():
    """Проверка наличия признака 'is_over': true / 1 в теле ответа."""
    # Положительные срабатывания
    assert has_is_over('{"is_over": true}') is True
    assert has_is_over('{"is_over": "true"}') is True
    assert has_is_over('{"is_over": 1}') is True
    assert has_is_over('{"is_over"  :   true, "other": 1}') is True
    assert has_is_over('{"IS_OVER": TRUE}') is True

    # Отрицательные срабатывания
    assert has_is_over('{"is_over": false}') is False
    assert has_is_over('{"is_over": "false"}') is False
    assert has_is_over('{"is_over": 0}') is False
    assert has_is_over('{"is_over": null}') is False
    assert has_is_over('{"is_overload": true}') is False
    assert has_is_over('{}') is False


def test_is_search_over():
    """Проверка завершения выдачи по списку накопленных payloads."""
    results_url = "https://aviasales.ru/search/v3.2/results?id=1"
    other_url = "https://example.com/search/fares"

    # Пустой список
    assert is_search_over([]) is False

    # Ответ не от /search/v3.2/results даже с is_over: true
    assert is_search_over([(other_url, '{"is_over": true}')]) is False

    # Ответ от /search/v3.2/results, но с is_over: false и без признака завершения
    assert is_search_over([(results_url, '{"is_over": false}')]) is False

    # Ответ от /search/v3.2/results с is_over: true (старый протокол)
    assert is_search_over([
        (other_url, '{"price": 100}'),
        (results_url, '{"is_over": true}'),
    ]) is True

    # 1. Финальный ответ с last_update_timestamp: 0 (число ноль) распознан
    assert is_search_over([(results_url, '{"last_update_timestamp": 0, "tickets": []}')]) is True
    assert is_search_over([(results_url, '{"last_update_timestamp":0}')]) is True
    assert is_search_over([(results_url, '{"last_update_timestamp" : 0}')]) is True
    assert is_search_over([(results_url, '{"other": 1, "last_update_timestamp":0, "done": true}')]) is True

    # 2. Промежуточный ответ (last_update_timestamp > 0) не считается завершением
    assert is_search_over([(results_url, '{"last_update_timestamp": 1714567890, "tickets": []}')]) is False
    assert is_search_over([(results_url, '{"last_update_timestamp": 1}')]) is False
    assert is_search_over([(results_url, '{"last_update_timestamp": 12345}')]) is False

    # 3. '"last_update_timestamp":0' внутри чужого URL игнорируется
    assert is_search_over([(other_url, '{"last_update_timestamp": 0}')]) is False
    assert is_search_over([("https://example.com/api/fares", '{"last_update_timestamp": 0}')]) is False
    assert is_search_over([("https://tickets-api.aviasales.ru/other/endpoint", '{"last_update_timestamp": 0}')]) is False

    # 4. Реалистичная последовательность опроса: промежуточный, затем финальный
    assert is_search_over([
        (results_url, '{"last_update_timestamp": 1690000000}'),
        (results_url, '{"last_update_timestamp": 0, "tickets": [{"id": "t1"}]}'),
    ]) is True


def test_is_search_over_yandex():
    """Проверка завершения выдачи Яндекс Путешествий по cont и progress."""
    yandex_url = "https://travel.yandex.ru/api/avia/search/results?qid=abc&cont=0"
    other_url = "https://example.com/api/fares"

    # 1. cont:1 progress 4/20 -> False
    body_cont1 = '{"cont": 1, "progress": {"current": 4, "all": 20}}'
    assert is_search_over([(yandex_url, body_cont1)]) is False

    # 2. cont:null и 20/20 -> True
    body_done = '{"cont": null, "progress": {"current": 20, "all": 20}}'
    assert is_search_over([(yandex_url, body_done)]) is True

    # 3. cont:null без progress -> True
    body_no_progress = '{"cont": null, "tickets": []}'
    assert is_search_over([(yandex_url, body_no_progress)]) is True

    # 4. cont:null но 4/20 -> False
    body_incomplete = '{"cont": null, "progress": {"current": 4, "all": 20}}'
    assert is_search_over([(yandex_url, body_incomplete)]) is False

    # cont:null на чужом URL не считается завершением
    assert is_search_over([(other_url, body_done)]) is False

    # Последовательность: промежуточный, затем финальный
    assert is_search_over([
        (yandex_url, body_cont1),
        (yandex_url, body_done),
    ]) is True


def test_is_captcha_url():
    """Проверка определения URL капчи (наличие /captcha/ или smartcaptcha, без учета регистра)."""
    # URL с /captcha/
    assert is_captcha_url("https://aviasales.ru/captcha/verify") is True
    assert is_captcha_url("https://captcha.yandex.net/captcha/image.png") is True
    assert is_captcha_url("https://example.com/api/v1/CAPTCHA/check") is True

    # URL со smartcaptcha
    assert is_captcha_url("https://yastatic.net/smartcaptcha-frame/index.html") is True
    assert is_captcha_url("https://smartcaptcha.yandexcloud.net/smartcaptcha.js") is True
    assert is_captcha_url("https://example.com/SMARTCAPTCHA/bundle.js") is True

    # Обычные URL не должны определяться как капча
    assert is_captcha_url("https://aviasales.ru/search/v3.2/results?id=1") is False
    assert is_captcha_url("https://aviasales.ru/static/rules.html") is False
    assert is_captcha_url("https://example.com/static/style.css") is False
    assert is_captcha_url("") is False


def test_has_search_tickets():
    """Проверка определения непустого списка билетов в ответах /search/v3.2/results."""
    results_url = "https://aviasales.ru/search/v3.2/results?id=123"
    other_url = "https://example.com/api/fares"

    # Пустой список ответов
    assert has_search_tickets([]) is False
    assert has_tickets([]) is False

    # Ответ не от /search/v3.2/results, даже если содержит tickets
    assert has_search_tickets([(other_url, '{"tickets": [{"id": "t1"}]}')]) is False

    # Ответ от /search/v3.2/results без билетов или с пустым списком
    assert has_search_tickets([(results_url, '{"is_over": true}')]) is False
    assert has_search_tickets([(results_url, '{"tickets": []}')]) is False
    assert has_search_tickets([(results_url, '[{"tickets": []}]')]) is False

    # Ответ от /search/v3.2/results с непустым списком билетов (dict)
    assert has_search_tickets([(results_url, '{"tickets": [{"id": "t1"}]}')]) is True

    # Ответ от /search/v3.2/results с непустым списком билетов (list of dicts)
    assert has_search_tickets([(results_url, '[{"chunk_id": "results", "tickets": [{"id": "t1"}]}]')]) is True

    # Несколько ответов, где билеты есть во втором
    assert has_search_tickets([
        (results_url, '{"tickets": []}'),
        (results_url, '{"tickets": [{"id": "t2"}]}'),
    ]) is True

    # Прямая передача строки тела ответа или одиночного кортежа
    assert has_search_tickets('{"tickets": [{"id": "t1"}]}') is True
    assert has_search_tickets('{"tickets": []}') is False
    assert has_search_tickets((results_url, '{"tickets": [{"id": "t1"}]}')) is True

    # Запасной разбор при невалидном/обрезанном JSON
    assert has_search_tickets([(results_url, 'invalid json {"tickets": [{"id": 1')]) is True


@pytest.mark.parametrize(
    ("is_blocked_text", "captcha_seen", "captcha_dom", "has_tickets", "expected"),
    [
        # Когда текст страницы не заблокирован (is_blocked_text=False):
        # 1. Нет капчи нигде
        (False, False, False, False, False),
        (False, False, False, True,  False),
        # 2. Капча только в DOM: если есть билеты — не блок (показана поверх выдачи), иначе — блок
        (False, False, True,  False, True),
        (False, False, True,  True,  False),
        # 3. Капча только в сети: если есть билеты — не блок (пройдена автоматически), иначе — блок
        (False, True,  False, False, True),
        (False, True,  False, True,  False),
        # 4. Капча и в сети, и в DOM: если есть билеты — не блок, иначе — блок
        (False, True,  True,  False, True),
        (False, True,  True,  True,  False),

        # Когда в тексте страницы явный маркер блокировки (is_blocked_text=True) — всегда блокировка:
        (True,  False, False, False, True),
        (True,  False, False, True,  True),
        (True,  False, True,  False, True),
        (True,  False, True,  True,  True),
        (True,  True,  False, False, True),
        (True,  True,  False, True,  True),
        (True,  True,  True,  False, True),
        (True,  True,  True,  True,  True),
    ],
)
def test_decide_blocked_all_16_combinations(is_blocked_text, captcha_seen, captcha_dom, has_tickets, expected):
    """Проверка всех 16 комбинаций флагов в чистой функции decide_blocked."""
    text_content = "Внимание: captcha! Доступ ограничен." if is_blocked_text else "Поиск авиабилетов: Москва — Сочи"

    # Проверка при передаче реального текста страницы
    assert decide_blocked(
        text=text_content,
        captcha_seen=captcha_seen,
        captcha_dom=captcha_dom,
        has_tickets=has_tickets,
    ) is expected

    # Проверка при прямой передаче булевого флага блокировки текста
    assert decide_blocked(
        text=is_blocked_text,
        captcha_seen=captcha_seen,
        captcha_dom=captcha_dom,
        has_tickets=has_tickets,
    ) is expected


@pytest.mark.asyncio
async def test_check_captcha_dom():
    """Проверка функции check_captcha_dom при разных реализациях объекта страницы."""
    # 1. Страница с evaluate, возвращающим True
    class PageWithEvalTrue:
        async def evaluate(self, expr):
            return True

    assert await check_captcha_dom(PageWithEvalTrue()) is True

    # 2. Страница с evaluate, возвращающим False
    class PageWithEvalFalse:
        async def evaluate(self, expr):
            return False

    assert await check_captcha_dom(PageWithEvalFalse()) is False

    # 3. Страница с evaluate, бросающим исключение
    class PageWithEvalError:
        async def evaluate(self, expr):
            raise RuntimeError("Page crashed")

    assert await check_captcha_dom(PageWithEvalError()) is False

    # 4. Страница только с query_selector_all (без evaluate)
    class PageWithQuerySelector:
        async def query_selector_all(self, selector):
            return [object()]

    assert await check_captcha_dom(PageWithQuerySelector()) is True

    class PageWithQuerySelectorEmpty:
        async def query_selector_all(self, selector):
            return []

    assert await check_captcha_dom(PageWithQuerySelectorEmpty()) is False

    # 5. Страница без evaluate и query_selector_all
    assert await check_captcha_dom(object()) is False


# --- Фейки для тестирования жизненного цикла браузера и контекста ---


class FakeResponse:
    def __init__(self, url: str, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.url = url
        self._body = body
        self.headers = headers if headers is not None else {"content-type": "application/json"}

    async def body(self) -> bytes:
        return self._body


class FakePage:
    def __init__(
        self,
        exc_on_goto: Exception | None = None,
        exc_on_screenshot: Exception | None = None,
        evaluate_result: bool = False,
        inner_text: str = "Результаты поиска авиабилетов",
        url: str = "https://example.com/final",
        responses_on_goto: list[FakeResponse] | None = None,
    ) -> None:
        self.url = url
        self._exc_on_goto = exc_on_goto
        self._exc_on_screenshot = exc_on_screenshot
        self._evaluate_result = evaluate_result
        self._inner_text = inner_text
        self._responses_on_goto = responses_on_goto or []
        self.callbacks: list = []
        self.wait_timeouts: list[int] = []

    def on(self, event, callback):
        self.callbacks.append(callback)

    async def goto(self, url, **kwargs):
        if self._exc_on_goto:
            raise self._exc_on_goto
        import inspect
        for resp in self._responses_on_goto:
            for cb in self.callbacks:
                res = cb(resp)
                if inspect.isawaitable(res):
                    await res

    async def wait_for_selector(self, selector, **kwargs):
        pass

    async def wait_for_timeout(self, ms):
        self.wait_timeouts.append(ms)

    class mouse:
        @staticmethod
        async def wheel(x, y):
            pass

    async def inner_text(self, selector):
        return self._inner_text

    async def evaluate(self, expression, *args):
        return self._evaluate_result

    async def query_selector_all(self, selector):
        return [object()] if self._evaluate_result else []

    async def screenshot(self, path, **kwargs):
        if self._exc_on_screenshot:
            raise self._exc_on_screenshot


class FakeContext:
    def __init__(self, page: FakePage, exc_on_close: Exception | None = None) -> None:
        self.page = page
        self.pages = [page]
        self._exc_on_close = exc_on_close
        self.close_called = False
        self.browser = None

    async def new_page(self):
        return self.page

    async def close(self):
        self.close_called = True
        if self._exc_on_close:
            raise self._exc_on_close


class FakeBrowser:
    def __init__(self, context: FakeContext, exc_on_close: Exception | None = None) -> None:
        self.context = context
        self.context.browser = self
        self._exc_on_close = exc_on_close
        self.close_called = False

    async def new_context(self, **kwargs):
        return self.context

    async def close(self):
        self.close_called = True
        if self._exc_on_close:
            raise self._exc_on_close


class FakePlaywrightManager:
    def __init__(self, browser: FakeBrowser | None = None, persistent_context: FakeContext | None = None) -> None:
        self.browser = browser
        self.persistent_context = persistent_context
        self.persistent_launch_args = None

        class FakeChromium:
            pass

        self.chromium = FakeChromium()
        if browser:
            async def launch(**kwargs):
                return browser
            self.chromium.launch = launch

        if persistent_context:
            async def launch_persistent_context(user_data_dir, **kwargs):
                self.persistent_launch_args = (user_data_dir, kwargs)
                return persistent_context
            self.chromium.launch_persistent_context = launch_persistent_context

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# --- Тесты закрытия ресурсов при ошибках ---


@pytest.mark.asyncio
async def test_fetch_snapshot_closes_resources_on_goto_exception(monkeypatch):
    """Исключение внутри goto вызывает close у контекста и браузера, исключение пробрасывается."""
    page = FakePage(exc_on_goto=RuntimeError("Ошибка сети при открытии страницы"))
    ctx = FakeContext(page)
    browser = FakeBrowser(ctx)
    pw = FakePlaywrightManager(browser=browser)

    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig("test_site", "https://example.com", settle_ms=1000, scrolls=0)

    with pytest.raises(RuntimeError, match="Ошибка сети при открытии страницы"):
        await fetch_snapshot("https://example.com", site, headless=True)

    assert ctx.close_called is True
    assert browser.close_called is True


@pytest.mark.asyncio
async def test_fetch_snapshot_persistent_profile_lifecycle(monkeypatch, tmp_path: Path):
    """При указании profile_dir запускается launch_persistent_context, директория создается, ресурсы закрываются."""
    profile_dir = tmp_path / "profiles" / "test_site"
    assert not profile_dir.exists()

    page = FakePage(exc_on_goto=RuntimeError("Сбой при переходе"))
    ctx = FakeContext(page)
    pw = FakePlaywrightManager(persistent_context=ctx)

    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig("test_site", "https://example.com", settle_ms=1000, scrolls=0)

    with pytest.raises(RuntimeError, match="Сбой при переходе"):
        await fetch_snapshot("https://example.com", site, headless=True, profile_dir=profile_dir)

    # Директория профиля должна быть автоматически создана
    assert profile_dir.is_dir()
    # Контекст запущен с нужными параметрами
    assert pw.persistent_launch_args is not None
    user_data_dir, kwargs = pw.persistent_launch_args
    assert user_data_dir == str(profile_dir)
    assert kwargs.get("headless") is True
    assert kwargs.get("locale") == "ru-RU"
    assert kwargs.get("timezone_id") == "Europe/Moscow"
    assert kwargs.get("viewport") == {"width": 1366, "height": 900}
    # Контекст закрыт
    assert ctx.close_called is True


@pytest.mark.asyncio
async def test_fetch_snapshot_close_failure_does_not_mask_original_error(monkeypatch):
    """Ошибка при закрытии контекста не маскирует исходную ошибку goto и не препятствует закрытию браузера."""
    page = FakePage(exc_on_goto=RuntimeError("Исходная ошибка загрузки"))
    ctx = FakeContext(page, exc_on_close=Exception("Ошибка закрытия контекста"))
    browser = FakeBrowser(ctx)
    pw = FakePlaywrightManager(browser=browser)

    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig("test_site", "https://example.com", settle_ms=1000, scrolls=0)

    # Исходная ошибка должна быть проброшена
    with pytest.raises(RuntimeError, match="Исходная ошибка загрузки"):
        await fetch_snapshot("https://example.com", site)

    # Оба ресурса должны были попытаться закрыться
    assert ctx.close_called is True
    assert browser.close_called is True


@pytest.mark.asyncio
async def test_fetch_snapshot_closes_resources_on_screenshot_exception(monkeypatch, tmp_path: Path):
    """Сбой при сохранении снимка (debug_dir) также гарантирует закрытие ресурсов."""
    page = FakePage(exc_on_screenshot=RuntimeError("Сбой скриншота"))
    ctx = FakeContext(page)
    browser = FakeBrowser(ctx)
    pw = FakePlaywrightManager(browser=browser)

    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig("test_site", "https://example.com", settle_ms=1000, scrolls=0)
    debug_dir = tmp_path / "debug"

    with pytest.raises(RuntimeError, match="Сбой скриншота"):
        await fetch_snapshot("https://example.com", site, debug_dir=debug_dir)

    assert ctx.close_called is True
    assert browser.close_called is True


@pytest.mark.asyncio
async def test_fetch_snapshot_success_normal_lifecycle(monkeypatch):
    """При нормальном выполнении контекст и браузер закрываются, возвращается корректный Snapshot."""
    page = FakePage()
    ctx = FakeContext(page)
    browser = FakeBrowser(ctx)
    pw = FakePlaywrightManager(browser=browser)

    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig("test_site", "https://example.com", settle_ms=1000, scrolls=0)
    snap = await fetch_snapshot("https://example.com", site)

    assert ctx.close_called is True
    assert browser.close_called is True
    assert snap.url == "https://example.com/final"
    assert "авиабилетов" in snap.text
    assert snap.blocked is False


@pytest.mark.asyncio
async def test_fetch_snapshot_blocked_by_captcha_response(monkeypatch):
    """Ответ с URL капчи без билетов приводит к snapshot.blocked=True, тело капчи не сохраняется."""
    captcha_resp = FakeResponse(
        url="https://captcha.yandex.net/captcha/verify?id=123",
        body=b"<html>captcha challenge</html>",
        headers={"content-type": "text/html"},
    )
    page = FakePage(responses_on_goto=[captcha_resp])
    ctx = FakeContext(page)
    browser = FakeBrowser(ctx)
    pw = FakePlaywrightManager(browser=browser)

    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig("test_site", "https://example.com", settle_ms=1000, scrolls=0)
    snap = await fetch_snapshot("https://example.com", site)

    # Капча зафиксирована
    assert snap.blocked is True
    # Тело капчи не сохранено в json_payloads
    assert len(snap.json_payloads) == 0


@pytest.mark.asyncio
async def test_fetch_snapshot_not_blocked_when_captcha_seen_but_tickets_present(monkeypatch):
    """Капча в ответах сети не считается блокировкой, если уже получен непустой список билетов."""
    captcha_resp = FakeResponse(
        url="https://smartcaptcha.yandexcloud.net/smartcaptcha.js",
        body=b"var c = 1;",
        headers={"content-type": "application/javascript"},
    )
    results_resp = FakeResponse(
        url="https://aviasales.ru/search/v3.2/results?uuid=test",
        body=b'{"chunk_id": "results", "tickets": [{"id": "t1"}], "is_over": true}',
        headers={"content-type": "application/json"},
    )
    page = FakePage(responses_on_goto=[captcha_resp, results_resp])
    ctx = FakeContext(page)
    browser = FakeBrowser(ctx)
    pw = FakePlaywrightManager(browser=browser)

    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig("test_site", "https://example.com", settle_ms=1000, scrolls=0)
    snap = await fetch_snapshot("https://example.com", site)

    # Наличие билетов снимает флаг блокировки
    assert snap.blocked is False
    assert len(snap.json_payloads) == 1
    assert "/search/v3.2/results" in snap.json_payloads[0][0]


@pytest.mark.asyncio
async def test_fetch_snapshot_blocked_by_captcha_dom(monkeypatch):
    """Наличие капчи в DOM (evaluate=True) без билетов приводит к snapshot.blocked=True."""
    page = FakePage(evaluate_result=True)
    ctx = FakeContext(page)
    browser = FakeBrowser(ctx)
    pw = FakePlaywrightManager(browser=browser)

    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig("test_site", "https://example.com", settle_ms=1000, scrolls=0)
    snap = await fetch_snapshot("https://example.com", site)

    assert snap.blocked is True


@pytest.mark.asyncio
async def test_fetch_snapshot_not_blocked_when_captcha_dom_but_tickets_present(monkeypatch):
    """Капча в DOM не считается блокировкой, если билеты уже получены (капча поверх готовой выдачи)."""
    results_resp = FakeResponse(
        url="https://aviasales.ru/search/v3.2/results?uuid=test",
        body=b'{"tickets": [{"id": "t1"}], "is_over": true}',
        headers={"content-type": "application/json"},
    )
    page = FakePage(evaluate_result=True, responses_on_goto=[results_resp])
    ctx = FakeContext(page)
    browser = FakeBrowser(ctx)
    pw = FakePlaywrightManager(browser=browser)

    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig("test_site", "https://example.com", settle_ms=1000, scrolls=0)
    snap = await fetch_snapshot("https://example.com", site)

    assert snap.blocked is False
    assert len(snap.json_payloads) == 1


@pytest.mark.asyncio
async def test_fetch_snapshot_large_results_with_last_update_zero(monkeypatch):
    """Финальный ответ > 400 КБ для results сохраняется, чужой > 400 КБ отбрасывается, выход без ожидания."""
    intermediate_resp = FakeResponse(
        url="https://tickets-api.aviasales.ru/search/v3.2/results?uuid=test",
        body=b'{"last_update_timestamp": 1710000000, "tickets": []}',
        headers={"content-type": "application/json"},
    )
    # Чужой URL с телом > 400 КБ — должен быть отброшен
    foreign_large_resp = FakeResponse(
        url="https://example.com/api/v1/search/fares",
        body=b'{"last_update_timestamp": 0, "pad": "' + b"x" * 500_000 + b'"}',
        headers={"content-type": "application/json"},
    )
    # Финальный ответ results с телом > 400 КБ (1.1 МБ) и "last_update_timestamp": 0
    final_large_resp = FakeResponse(
        url="https://tickets-api.aviasales.ru/search/v3.2/results?uuid=test",
        body=b'{"last_update_timestamp": 0, "tickets": [{"id": "t1"}], "pad": "' + b"y" * 1_100_000 + b'"}',
        headers={"content-type": "application/json"},
    )

    page = FakePage(responses_on_goto=[intermediate_resp, foreign_large_resp, final_large_resp])
    ctx = FakeContext(page)
    browser = FakeBrowser(ctx)
    pw = FakePlaywrightManager(browser=browser)

    monkeypatch.setattr("avia.collector.async_playwright", lambda: pw)

    site = SiteConfig("test_site", "https://example.com", settle_ms=10_000, scrolls=2)
    snap = await fetch_snapshot("https://example.com", site)

    # 1. Сохранены только results (промежуточный и крупный финальный 1.1 МБ); чужой крупный отброшен
    saved_urls = [u for u, _ in snap.json_payloads]
    assert len(saved_urls) == 2
    assert all("/search/v3.2/results" in u for u in saved_urls)
    assert any(len(b) > 1_000_000 for _, b in snap.json_payloads)

    # 2. Не ждали settle_ms (10 сек): poll timeouts отсутствуют, остались только scroll timeouts
    assert page.wait_timeouts == [1200, 1200]
    assert snap.blocked is False


# --- Тесты recon.py ---


@pytest.mark.asyncio
async def test_recon_profile_dir_and_blocked_message(monkeypatch, capsys):
    """recon.py передает правильный profile_dir и выводит понятную подсказку при blocked=True."""
    from avia import recon
    from avia.config import ROOT, Settings, SiteConfig

    captured_kwargs = {}

    async def fake_fetch_snapshot(url, site, *, headless=True, profile_dir=None, debug_dir=None):
        captured_kwargs["profile_dir"] = profile_dir
        captured_kwargs["headless"] = headless
        return Snapshot(
            url="https://aviasales.ru/blocked",
            text="Вы робот? Captcha",
            json_payloads=[],
            blocked=True,
        )

    test_site = SiteConfig("aviasales", "https://aviasales.ru/{origin}", source="browser")
    settings = Settings(sites=[test_site])

    monkeypatch.setattr("avia.recon.fetch_snapshot", fake_fetch_snapshot)
    monkeypatch.setattr("avia.recon.load_settings", lambda: settings)
    monkeypatch.setattr("sys.argv", ["recon.py", "aviasales", "Москва", "Алматы", "10.12"])

    ret = await recon.main()
    assert ret == 0

    expected_profile = ROOT / "state" / "profiles" / "aviasales"
    assert captured_kwargs["profile_dir"] == expected_profile

    captured = capsys.readouterr().out
    # Подсказка на русском при blocked=True в выводе
    assert "сайт показал капчу: запустите с --show и пройдите её руками, сессия сохранится в state/profiles/aviasales" in captured
    # Путь профиля показан в выводе
    assert str(expected_profile) in captured


def test_is_blocked_ozon_challenge_page():
    from avia.collector import is_blocked

    text = "Похоже, нет соединения\nИнцидент: fab_chlg_20261027011106_01M5XEJD20TBDT7PFFHPR8BP9T"
    assert is_blocked(text)
