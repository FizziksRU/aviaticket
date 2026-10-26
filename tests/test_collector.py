"""Тесты для src/avia/collector.py и интеграции с src/avia/recon.py.

Все тесты работают без сети и без реального Playwright через чистые функции
и подмену async_playwright фейковыми объектами.
"""
from __future__ import annotations

from pathlib import Path
import pytest

from avia.collector import (
    BLOCK_MARKERS,
    MAX_JSON_BYTES,
    Snapshot,
    fetch_snapshot,
    has_is_over,
    is_blocked,
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
    """Для URL '/search/v3.2/results' сохраняются любые ответы (включая короткие >= 0)."""
    results_url = "https://aviasales.ru/search/v3.2/results?uuid=123"

    # Короткие ответы
    assert should_save_response(results_url, 0) is True
    assert should_save_response(results_url, 15) is True
    assert should_save_response(results_url, 150) is True

    # Стандартные ответы
    assert should_save_response(results_url, 500) is True
    assert should_save_response(results_url, MAX_JSON_BYTES) is True

    # Превышение лимита или отрицательная длина
    assert should_save_response(results_url, MAX_JSON_BYTES + 1) is False
    assert should_save_response(results_url, -1) is False


def test_should_save_response_other_urls():
    """Для прочих URL сохранение зависит от наличия подсказок в URL и длины > 200 байт."""
    # URL с ключевыми словами
    for word in ("search", "ticket", "price", "offer", "fare", "avia", "flight", "result"):
        url = f"https://example.com/api/v1/{word}/data"
        # Короткий ответ (<= 200) отбрасывается
        assert should_save_response(url, 0) is False
        assert should_save_response(url, 200) is False
        # Длинный ответ (> 200 и <= MAX) сохраняется
        assert should_save_response(url, 201) is True
        assert should_save_response(url, 10_000) is True
        # Превышающий максимум отбрасывается
        assert should_save_response(url, MAX_JSON_BYTES + 1) is False

    # URL без ключевых слов отбрасывается независимо от длины
    assert should_save_response("https://example.com/static/style.css", 500) is False
    assert should_save_response("https://example.com/analytics/collect", 5000) is False
    assert should_save_response("https://example.com/image.png", 50) is False


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

    # Ответ от /search/v3.2/results, но с is_over: false
    assert is_search_over([(results_url, '{"is_over": false}')]) is False

    # Ответ от /search/v3.2/results с is_over: true
    assert is_search_over([
        (other_url, '{"price": 100}'),
        (results_url, '{"is_over": true}'),
    ]) is True


# --- Фейки для тестирования жизненного цикла браузера и контекста ---


class FakePage:
    def __init__(self, exc_on_goto: Exception | None = None, exc_on_screenshot: Exception | None = None) -> None:
        self.url = "https://example.com/final"
        self._exc_on_goto = exc_on_goto
        self._exc_on_screenshot = exc_on_screenshot
        self.callbacks: list = []
        self.wait_timeouts: list[int] = []

    def on(self, event, callback):
        self.callbacks.append(callback)

    async def goto(self, url, **kwargs):
        if self._exc_on_goto:
            raise self._exc_on_goto

    async def wait_for_selector(self, selector, **kwargs):
        pass

    async def wait_for_timeout(self, ms):
        self.wait_timeouts.append(ms)

    class mouse:
        @staticmethod
        async def wheel(x, y):
            pass

    async def inner_text(self, selector):
        return "Результаты поиска авиабилетов"

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
