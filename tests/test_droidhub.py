from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from avia.config import Settings, SiteConfig
from avia.droidhub import (
    DroidhubError,
    _device_locks,
    fetch_page_snapshot,
    phone_enabled,
)


_ORIG_ASYNC_CLIENT = httpx.AsyncClient


def _mock_async_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "avia.droidhub.httpx.AsyncClient",
        lambda **kwargs: _ORIG_ASYNC_CLIENT(transport=transport, **{k: v for k, v in kwargs.items() if k != "transport"}),
    )


def test_phone_enabled():
    """phone_enabled возвращает True только если phone, droidhub_url и droidhub_token заданы."""
    site_with_phone = SiteConfig("as", "https://site.test", phone="realme")
    site_no_phone = SiteConfig("as", "https://site.test", phone="")
    site_space_phone = SiteConfig("as", "https://site.test", phone="   ")

    # Все три заданы -> True
    s_full = Settings(sites=[], droidhub_url="https://hub.test", droidhub_token="secret_token")
    assert phone_enabled(s_full, site_with_phone) is True

    # Нет токена -> False
    s_no_tok = Settings(sites=[], droidhub_url="https://hub.test", droidhub_token="")
    assert phone_enabled(s_no_tok, site_with_phone) is False

    # Токен из пробелов -> False
    s_space_tok = Settings(sites=[], droidhub_url="https://hub.test", droidhub_token="   ")
    assert phone_enabled(s_space_tok, site_with_phone) is False

    # Нет URL -> False
    s_no_url = Settings(sites=[], droidhub_url="", droidhub_token="secret_token")
    assert phone_enabled(s_no_url, site_with_phone) is False

    # URL из пробелов -> False
    s_space_url = Settings(sites=[], droidhub_url="   ", droidhub_token="secret_token")
    assert phone_enabled(s_space_url, site_with_phone) is False

    # Нет phone -> False
    assert phone_enabled(s_full, site_no_phone) is False
    assert phone_enabled(s_full, site_space_phone) is False


@pytest.mark.asyncio
async def test_fetch_page_snapshot_success(monkeypatch):
    """Успешный ответ droidhub 200 превращается в Snapshot с json_payloads."""
    secret_token = "TOP_SECRET_DROID_TOKEN_12345"
    recorded_request: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_request["url"] = str(request.url)
        recorded_request["headers"] = dict(request.headers)
        recorded_request["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "url": "https://www.aviasales.ru/search/MOW1012ALA1",
                "title": "Aviasales search",
                "text": "Билеты Москва — Алматы от 8000 руб.",
                "truncated": False,
                "captcha": False,
                "finished": True,
                "seconds": 14.2,
                "json_responses": [
                    {
                        "url": "https://www.aviasales.ru/search/v3.2/results",
                        "status": 200,
                        "body": '{"tickets": [{"price": 8000}]}',
                    },
                    {
                        "url": "https://www.aviasales.ru/api/extra",
                        "status": 200,
                        "body": {"status": "ok"},
                    },
                ],
            },
        )

    _mock_async_client(monkeypatch, handler)

    settings = Settings(
        sites=[],
        droidhub_url="https://hub.example.com",
        droidhub_token=secret_token,
    )
    snap, finished = await fetch_page_snapshot(
        settings,
        "realme",
        "https://www.aviasales.ru/search/MOW1012ALA1",
        wait_seconds=40,
        capture_json=["/search/v3.2/results"],
    )

    assert finished is True
    assert snap.url == "https://www.aviasales.ru/search/MOW1012ALA1"
    assert snap.text == "Билеты Москва — Алматы от 8000 руб."
    assert snap.blocked is False
    assert len(snap.json_payloads) == 2
    assert snap.json_payloads[0] == (
        "https://www.aviasales.ru/search/v3.2/results",
        '{"tickets": [{"price": 8000}]}',
    )
    assert snap.json_payloads[1][0] == "https://www.aviasales.ru/api/extra"
    assert json.loads(snap.json_payloads[1][1]) == {"status": "ok"}

    # Проверка переданного запроса
    assert recorded_request["url"] == "https://hub.example.com/api/devices/realme/fetch_page"
    assert recorded_request["headers"]["authorization"] == f"Bearer {secret_token}"
    assert recorded_request["body"]["url"] == "https://www.aviasales.ru/search/MOW1012ALA1"
    assert recorded_request["body"]["wait_seconds"] == 40
    assert recorded_request["body"]["capture_json"] == ["/search/v3.2/results"]
    assert recorded_request["body"]["max_chars"] == 200000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_code,expected_substr",
    [
        (502, "502"),
        (401, "401"),
        (403, "403"),
        (429, "429"),
        (500, "500"),
    ],
)
async def test_fetch_page_snapshot_http_errors_no_token(monkeypatch, status_code: int, expected_substr: str):
    """502, 401 и другие HTTP-ошибки дают DroidhubError без токена в тексте."""
    secret_token = "SUPER_CONFIDENTIAL_TOKEN_99999"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="Error page")

    _mock_async_client(monkeypatch, handler)

    settings = Settings(
        sites=[],
        droidhub_url="https://hub.example.com",
        droidhub_token=secret_token,
    )

    with pytest.raises(DroidhubError) as exc_info:
        await fetch_page_snapshot(
            settings,
            "realme",
            "https://site.test/search",
            wait_seconds=40,
        )

    err_text = str(exc_info.value)
    assert expected_substr in err_text
    assert secret_token not in err_text
    assert secret_token not in repr(exc_info.value)
    # Полный URL запроса не должен быть в тексте ошибки
    assert "https://hub.example.com" not in err_text
    assert "https://site.test/search" not in err_text


@pytest.mark.asyncio
async def test_fetch_page_snapshot_network_errors(monkeypatch):
    """Сетевые ошибки и таймаут вызывают DroidhubError без токена."""
    secret_token = "ANOTHER_SECRET_TOKEN"
    settings = Settings(
        sites=[],
        droidhub_url="https://hub.example.com",
        droidhub_token=secret_token,
    )

    # Таймаут
    def timeout_handler(request: httpx.Request):
        raise httpx.ReadTimeout("read timed out")

    _mock_async_client(monkeypatch, timeout_handler)

    with pytest.raises(DroidhubError) as exc:
        await fetch_page_snapshot(settings, "realme", "https://site.test")
    assert "таймаут" in str(exc.value)
    assert secret_token not in str(exc.value)

    # Ошибка соединения
    def connect_handler(request: httpx.Request):
        raise httpx.ConnectError("cannot connect to server")

    _mock_async_client(monkeypatch, connect_handler)

    with pytest.raises(DroidhubError) as exc2:
        await fetch_page_snapshot(settings, "realme", "https://site.test")
    assert "сетевая ошибка" in str(exc2.value)
    assert secret_token not in str(exc2.value)


@pytest.mark.asyncio
async def test_fetch_page_snapshot_bad_json(monkeypatch):
    """Некорректный JSON ответа droidhub даёт DroidhubError."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    _mock_async_client(monkeypatch, handler)

    settings = Settings(sites=[], droidhub_url="https://hub.test", droidhub_token="tok")
    with pytest.raises(DroidhubError) as exc:
        await fetch_page_snapshot(settings, "realme", "https://site.test")
    assert "некорректный JSON" in str(exc.value)


@pytest.mark.asyncio
async def test_device_locks_serialization(monkeypatch):
    """Запросы к одному и тому же device_id сериализуются (не более 1 одновременно)."""
    _device_locks.clear()
    active_requests = 0
    max_active_requests = 0

    async def async_handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_requests, max_active_requests
        active_requests += 1
        if active_requests > max_active_requests:
            max_active_requests = active_requests
        await asyncio.sleep(0.05)
        active_requests -= 1
        return httpx.Response(200, json={"url": "https://ok.test", "text": "ok", "finished": True})

    _mock_async_client(monkeypatch, async_handler)

    settings = Settings(sites=[], droidhub_url="https://hub.test", droidhub_token="tok")

    # Запускаем два параллельных запроса к одному и тому же телефону 'realme'
    task1 = asyncio.create_task(fetch_page_snapshot(settings, "realme", "https://site1.test"))
    task2 = asyncio.create_task(fetch_page_snapshot(settings, "realme", "https://site2.test"))

    res1, res2 = await asyncio.gather(task1, task2)
    assert res1[1] is True
    assert res2[1] is True
    # Сериализация: максимум 1 одновременный запрос на телефон
    assert max_active_requests == 1


def test_get_device_lock_event_loop_change():
    """_get_device_lock создаёт новый Lock, если существующий привязан к другому event loop."""
    from avia.droidhub import _device_locks, _get_device_lock

    _device_locks.clear()

    async def contend():
        lock = _get_device_lock("dev1")
        async def hold():
            async with lock:
                await asyncio.sleep(0.01)
        await asyncio.gather(hold(), hold())
        return lock

    lock1 = asyncio.run(contend())
    assert getattr(lock1, "_loop", None) is not None

    # Запуск в новом цикле событий должен создать новый Lock вместо использования старого
    lock2 = asyncio.run(contend())
    assert lock2 is not lock1


@pytest.mark.asyncio
async def test_fetch_page_snapshot_null_text(monkeypatch):
    """Ответ с text: null превращается в пустую строку текста без ошибки."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"url": "https://ok.test", "text": None, "finished": True})

    _mock_async_client(monkeypatch, handler)

    settings = Settings(sites=[], droidhub_url="https://hub.test", droidhub_token="tok")
    snap, finished = await fetch_page_snapshot(settings, "realme", "https://site.test")
    assert finished is True
    assert snap.text == ""


@pytest.mark.asyncio
async def test_fetch_page_snapshot_wait_seconds_clamp(monkeypatch):
    """wait_seconds <= 0 ограничивается значением 1."""
    recorded_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_body.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"url": "https://ok.test", "text": "ok", "finished": True})

    _mock_async_client(monkeypatch, handler)

    settings = Settings(sites=[], droidhub_url="https://hub.test", droidhub_token="tok")
    await fetch_page_snapshot(settings, "realme", "https://site.test", wait_seconds=0)
    assert recorded_body["wait_seconds"] == 1

    await fetch_page_snapshot(settings, "realme", "https://site.test", wait_seconds=-10)
    assert recorded_body["wait_seconds"] == 1


@pytest.mark.asyncio
async def test_fetch_page_snapshot_strip_credentials_and_device(monkeypatch):
    """Пробелы и переводы строк в droidhub_url, droidhub_token и device_id очищаются."""
    recorded_url = ""
    recorded_auth = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal recorded_url, recorded_auth
        recorded_url = str(request.url)
        recorded_auth = request.headers.get("authorization", "")
        return httpx.Response(200, json={"url": "https://ok.test", "text": "ok", "finished": True})

    _mock_async_client(monkeypatch, handler)

    settings = Settings(
        sites=[],
        droidhub_url="  https://hub.test/  \n",
        droidhub_token="  secret_token_123 \n",
    )
    await fetch_page_snapshot(settings, "  realme \n", "https://site.test")
    assert recorded_url == "https://hub.test/api/devices/realme/fetch_page"
    assert recorded_auth == "Bearer secret_token_123"


def test_get_device_lock_whitespace_normalization():
    """_get_device_lock нормализует идентификатор устройства с краевыми пробелами."""
    from avia.droidhub import _device_locks, _get_device_lock

    _device_locks.clear()
    lock1 = _get_device_lock("realme")
    lock2 = _get_device_lock("  realme \n\t ")
    assert lock1 is lock2


@pytest.mark.asyncio
async def test_device_locks_serialization_whitespace(monkeypatch):
    """Параллельные запросы к одному телефону с пробелами в id используют одну блокировку."""
    active_requests = 0
    max_active_requests = 0

    async def async_handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_requests, max_active_requests
        active_requests += 1
        if active_requests > max_active_requests:
            max_active_requests = active_requests
        await asyncio.sleep(0.05)
        active_requests -= 1
        return httpx.Response(200, json={"url": "https://ok.test", "text": "ok", "finished": True})

    _mock_async_client(monkeypatch, async_handler)

    settings = Settings(sites=[], droidhub_url="https://hub.test", droidhub_token="tok")

    task1 = asyncio.create_task(fetch_page_snapshot(settings, "realme", "https://site1.test"))
    task2 = asyncio.create_task(fetch_page_snapshot(settings, "  realme  ", "https://site2.test"))

    res1, res2 = await asyncio.gather(task1, task2)
    assert res1[1] is True
    assert res2[1] is True
    assert max_active_requests == 1


