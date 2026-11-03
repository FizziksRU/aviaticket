from __future__ import annotations

import asyncio
from datetime import date
import json
from pathlib import Path

import httpx
import pytest

from avia.bot import Bot
from avia.collector import Snapshot
from avia.config import Settings, SiteConfig
from avia.formatting import format_report
from avia.guard import SearchGuard
from avia.models import Offer, Query, SiteResult
from avia.parsers import PARSERS
from avia.pipeline import guard as default_guard, search


@pytest.fixture(autouse=True)
def clean_guard():
    """Сбрасываем состояние общего SearchGuard перед и после каждого теста."""
    default_guard.clear()
    orig_interval = default_guard.min_interval
    orig_retry = default_guard.retry_delay
    default_guard.min_interval = 0.0
    default_guard.retry_delay = 0.0
    yield
    default_guard.clear()
    default_guard.min_interval = orig_interval
    default_guard.retry_delay = orig_retry


@pytest.mark.asyncio
async def test_pipeline_cache(monkeypatch):
    """Кэш: второй вызов с тем же запросом не вызывает fetch_snapshot и возвращает cached=True."""
    calls = 0

    async def fake_fetch_snapshot(url, site, **kwargs):
        nonlocal calls
        calls += 1
        return Snapshot(
            url="https://site.test/result",
            text="page with results",
            json_payloads=[],
        )

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_fetch_snapshot)

    def dummy_parser(snap, q):
        return [Offer(site="site1", price=5000, airline="Airline")]

    monkeypatch.setitem(PARSERS, "test_parser", dummy_parser)

    site = SiteConfig("site1", "https://site.test/{origin}{destination}", parser="test_parser")
    settings = Settings(sites=[site])
    q = Query("MOW", "ALA", date(2026, 12, 10))

    test_guard = SearchGuard(min_interval=0.0, retry_delay=0.0)

    # Первый вызов: промах кэша, запуск фейка
    results1 = await search(q, settings, guard=test_guard)
    assert len(results1) == 1
    assert calls == 1
    assert results1[0].ok is True
    assert results1[0].cached is False
    assert len(results1[0].offers) == 1

    # Второй вызов: попадание в кэш, фейк не запускается, cached=True
    results2 = await search(q, settings, guard=test_guard)
    assert len(results2) == 1
    assert calls == 1  # фейк не вызывался повторно
    assert results2[0].ok is True
    assert results2[0].cached is True
    assert len(results2[0].offers) == 1


@pytest.mark.asyncio
async def test_pipeline_error_isolation(monkeypatch):
    """Ошибка одного сайта не роняет остальные сайты в поиске."""
    async def fake_fetch_snapshot(url, site, **kwargs):
        if site.name == "bad_site":
            raise httpx.ConnectError("connection refused")
        return Snapshot(
            url="https://good.test/result",
            text="good page",
            json_payloads=[],
        )

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_fetch_snapshot)

    def dummy_parser(snap, q):
        return [Offer(site=snap.url, price=3000)]

    monkeypatch.setitem(PARSERS, "good_parser", dummy_parser)

    site_good = SiteConfig("good_site", "https://good.test/{origin}{destination}", parser="good_parser")
    site_bad = SiteConfig("bad_site", "https://bad.test/{origin}{destination}", parser="bad_parser")
    settings = Settings(sites=[site_good, site_bad])
    q = Query("MOW", "ALA", date(2026, 12, 10))

    results = await search(q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results) == 2

    good_res = next(r for r in results if r.site == "good_site")
    bad_res = next(r for r in results if r.site == "bad_site")

    assert good_res.ok is True
    assert len(good_res.offers) == 1

    assert bad_res.ok is False
    assert bad_res.offers == []
    assert bad_res.error == "нет сети"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc,expected_error",
    [
        (httpx.ConnectError("network disconnected"), "нет сети"),
        (ConnectionResetError("peer reset"), "нет сети"),
        (asyncio.TimeoutError(), "таймаут"),
        (httpx.ReadTimeout("read timeout"), "таймаут"),
        (RuntimeError("сайт показал капчу или блокировку"), "сайт показал капчу, попробуйте позже"),
        (ValueError("sensitive_internal_token_secret"), "неизвестная ошибка (ValueError)"),
    ],
)
async def test_pipeline_friendly_error_mapping(monkeypatch, exc, expected_error):
    """friendly_error корректно попадает в SiteResult.error без технических деталей."""
    async def fake_fetch_snapshot(url, site, **kwargs):
        raise exc

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_fetch_snapshot)

    site = SiteConfig("site1", "https://site.test/{origin}{destination}", parser="any_parser")
    settings = Settings(sites=[site])
    q = Query("MOW", "ALA", date(2026, 12, 10))

    results = await search(q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].error == expected_error
    if isinstance(exc, ValueError):
        assert "sensitive_internal_token_secret" not in results[0].error


@pytest.mark.asyncio
async def test_pipeline_empty_long_text_creates_debug_snapshot(monkeypatch, tmp_path: Path):
    """0 вариантов при непустой странице (> 200 символов) даёт ошибку и сохраняет json в debug_dir."""
    long_text = "Билеты на самолёт Москва - Алматы. " * 10
    assert len(long_text.strip()) > 200

    secret_tp = "SECRET_TP_12345"
    secret_tg = "SECRET_TG_67890"

    async def fake_fetch_snapshot(url, site, **kwargs):
        return Snapshot(
            url=f"https://site.test/search?token=SECRET_QUERY_TOKEN",
            text=f"{long_text} token={secret_tp}",
            json_payloads=[("https://site.test/api?key=SECRET_KEY", f'{{"tp": "{secret_tp}", "tg": "{secret_tg}"}}')],
            blocked=False,
        )

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_fetch_snapshot)

    def empty_parser(snap, q):
        return []

    monkeypatch.setitem(PARSERS, "empty_parser", empty_parser)

    site = SiteConfig("air_site", "https://site.test/{origin}{destination}", parser="empty_parser")
    settings = Settings(
        sites=[site],
        debug_dir=tmp_path,
        tp_token=secret_tp,
        tg_token=secret_tg,
    )
    q = Query("MOW", "ALA", date(2026, 12, 10))

    results = await search(q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results) == 1
    res = results[0]

    assert res.ok is False
    assert res.error == "формат выдачи мог измениться, 0 вариантов при непустой странице"

    # Проверяем, что в debug_dir создался файл
    dump_files = list(tmp_path.glob("air_site_empty_*.json"))
    assert len(dump_files) == 1
    dump_file = dump_files[0]

    content = dump_file.read_text(encoding="utf-8")
    data = json.loads(content)

    assert "url" in data and "text" in data and "json_payloads" in data
    assert data["url"].startswith("https://site.test/search")

    # Проверяем, что секреты не попали в файл
    assert secret_tp not in content
    assert secret_tg not in content
    assert "SECRET_QUERY_TOKEN" not in content
    assert "SECRET_KEY" not in content


@pytest.mark.asyncio
async def test_pipeline_empty_long_text_does_not_crash_if_dir_cannot_be_created(monkeypatch):
    """Если папку debug_dir нельзя создать, поиск не падает и возвращает понятную ошибку."""
    long_text = "Билеты на самолёт Москва - Алматы. " * 10

    async def fake_fetch_snapshot(url, site, **kwargs):
        return Snapshot(
            url="https://site.test/search",
            text=long_text,
            json_payloads=[],
            blocked=False,
        )

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_fetch_snapshot)

    def empty_parser(snap, q):
        return []

    monkeypatch.setitem(PARSERS, "empty_parser", empty_parser)

    def failing_mkdir(self, *args, **kwargs):
        raise PermissionError("Permission denied: cannot create directory")

    monkeypatch.setattr(Path, "mkdir", failing_mkdir)

    site = SiteConfig("air_site", "https://site.test/{origin}{destination}", parser="empty_parser")
    settings = Settings(sites=[site], debug_dir=Path("/root/forbidden_debug"))
    q = Query("MOW", "ALA", date(2026, 12, 10))

    results = await search(q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].error == "формат выдачи мог измениться, 0 вариантов при непустой странице"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "",  # пустой текст
        "   \n\t  ",  # только пробелы
        "Короткий текст выдачи",  # менее 200 символов
    ],
)
async def test_pipeline_empty_short_or_empty_text_no_file(monkeypatch, tmp_path: Path, text: str):
    """При коротком или пустом тексте (<= 200) файл снимка не создаётся, ok=True, error=None."""
    async def fake_fetch_snapshot(url, site, **kwargs):
        return Snapshot(
            url="https://site.test/search",
            text=text,
            json_payloads=[],
            blocked=False,
        )

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_fetch_snapshot)

    def empty_parser(snap, q):
        return []

    monkeypatch.setitem(PARSERS, "empty_parser", empty_parser)

    site = SiteConfig("air_site", "https://site.test/{origin}{destination}", parser="empty_parser")
    settings = Settings(sites=[site], debug_dir=tmp_path)
    q = Query("MOW", "ALA", date(2026, 12, 10))

    results = await search(q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results) == 1
    res = results[0]

    assert res.ok is True
    assert res.error is None
    assert res.offers == []

    # Файл не создаётся
    dump_files = list(tmp_path.glob("air_site_empty_*.json"))
    assert dump_files == []


def test_formatting_escapes_html_and_shows_cached():
    """formatting экранирует HTML и показывает 'из кэша, цены могли измениться'."""
    q = Query("<MOW>", "<ALA>", date(2026, 12, 10))
    offer = Offer(site="<safe_site>", price=7770, airline="<Airline&Co>")
    res_cached = SiteResult(
        site="<safe_site>",
        ok=True,
        offers=[offer],
        cached=True,
    )
    res_err = SiteResult(
        site="<bad_site>",
        ok=False,
        offers=[],
        error="<script>alert(1)</script>",
    )
    res_empty_cached = SiteResult(
        site="<empty_site>",
        ok=True,
        offers=[],
        cached=True,
    )

    report = format_report(q, [res_cached, res_err, res_empty_cached])

    # Проверка отображения информации о кэше
    assert "из кэша, цены могли измениться" in report
    assert "из кэша" in report

    # Проверка экранирования HTML
    assert "<MOW>" not in report
    assert "&lt;MOW&gt;" in report

    assert "<safe_site>" not in report
    assert "&lt;safe_site&gt;" in report

    assert "<Airline&Co>" not in report
    assert "&lt;Airline&amp;Co&gt;" in report

    assert "<script>alert(1)</script>" not in report
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in report


@pytest.mark.asyncio
async def test_bot_friendly_error_and_auth(monkeypatch):
    """Бот отвечает понятной ошибкой (friendly_error) и игнорирует неразрешённых пользователей."""
    sent_messages: list[tuple[int, str]] = []

    settings = Settings(
        sites=[],
        tg_token="dummy_token",
        tg_allowed_ids={12345},
    )
    bot = Bot(settings)

    async def mock_call(method: str, **params):
        if method == "sendMessage":
            sent_messages.append((params["chat_id"], params["text"]))
            return {"message_id": 1}
        return {}

    monkeypatch.setattr(bot, "call", mock_call)

    # 1. Запрос от неразрешённого пользователя (id=99999) - игнорируется
    unauth_msg = {
        "chat": {"id": 111},
        "from": {"id": 99999},
        "text": "/price MOW ALA 10.12",
    }
    await bot.handle(unauth_msg)
    assert len(sent_messages) == 0

    # 2. Запрос от разрешённого пользователя (id=12345), когда search падает с сетевой ошибкой
    async def mock_failing_search(q, s):
        raise httpx.ConnectError("network unreachable")

    monkeypatch.setattr("avia.bot.search", mock_failing_search)

    auth_msg = {
        "chat": {"id": 222},
        "from": {"id": 12345},
        "text": "/price MOW ALA 10.12",
    }
    await bot.handle(auth_msg)

    # Должно быть отправлено: сообщение "Ищу..." и сообщение об ошибке с friendly_error
    assert len(sent_messages) == 2
    err_msg = sent_messages[1][1]
    assert "нет сети" in err_msg
    assert "Traceback" not in err_msg


def test_cli_friendly_error(monkeypatch, capsys):
    """CLI при ошибке поиска выводит понятную ошибку friendly_error и завершается без traceback."""
    import sys
    from avia.cli import main

    monkeypatch.setattr(sys, "argv", ["avia.cli", "MOW", "ALA", "10.12"])

    def mock_failing_search(q, s):
        raise httpx.ConnectError("connection failed")

    monkeypatch.setattr("avia.cli.search", mock_failing_search)

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert "нет сети" in str(exc_info.value)
    assert "Traceback" not in str(exc_info.value)
