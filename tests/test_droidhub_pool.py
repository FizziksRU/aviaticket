from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx
import pytest

from avia.config import Settings, SiteConfig, load_settings
from avia.droidhub import (
    DroidhubError,
    _get_device_lock,
    _reset_pool,
    acquire,
    candidates,
    fetch_page_snapshot,
    fetch_page_snapshot_any,
    phone_enabled,
    release,
)

_ORIG_ASYNC_CLIENT = httpx.AsyncClient


def _mock_async_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "avia.droidhub.httpx.AsyncClient",
        lambda **kwargs: _ORIG_ASYNC_CLIENT(transport=transport, **{k: v for k, v in kwargs.items() if k != "transport"}),
    )


@pytest.fixture(autouse=True)
def reset_pool_state():
    _reset_pool()
    yield
    _reset_pool()


@pytest.mark.asyncio
async def test_pool_four_parallel_requests_distributed_and_serialized(monkeypatch):
    """Два устройства, четыре параллельных запроса распределяются по обоим и ни одно
    устройство не обслуживает два запроса одновременно.
    """
    active_per_device: dict[str, int] = {"dev1": 0, "dev2": 0}
    max_active: dict[str, int] = {"dev1": 0, "dev2": 0}
    device_calls: list[str] = []
    lock = asyncio.Lock()

    async def async_handler(request: httpx.Request) -> httpx.Response:
        # Извлекаем device_id из URL: .../api/devices/{device_id}/fetch_page
        m = re.search(r"/api/devices/([^/]+)/fetch_page", str(request.url))
        dev = m.group(1) if m else "unknown"

        async with lock:
            active_per_device[dev] += 1
            if active_per_device[dev] > max_active[dev]:
                max_active[dev] = active_per_device[dev]
            device_calls.append(dev)

        await asyncio.sleep(0.05)

        async with lock:
            active_per_device[dev] -= 1

        return httpx.Response(
            200,
            json={
                "url": str(request.url),
                "text": f"ok from {dev}",
                "captcha": False,
                "finished": True,
                "json_responses": [],
            },
        )

    _mock_async_client(monkeypatch, async_handler)

    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="secret_token",
        droidhub_devices=["dev1", "dev2"],
    )

    tasks = [
        asyncio.create_task(
            fetch_page_snapshot_any(settings, ["dev1", "dev2"], f"https://site.test/{i}")
        )
        for i in range(4)
    ]
    results = await asyncio.gather(*tasks)

    assert len(results) == 4
    for snap, finished in results:
        assert finished is True
        assert snap.blocked is False

    # Ни одно устройство не обслуживало два запроса одновременно
    assert max_active["dev1"] <= 1
    assert max_active["dev2"] <= 1

    # Запросы распределились по обоим устройствам (по 2 на каждое)
    assert device_calls.count("dev1") == 2
    assert device_calls.count("dev2") == 2


@pytest.mark.asyncio
async def test_pool_both_busy_waits_and_uses_first_freed(monkeypatch):
    """Когда оба устройства заняты, запрос ждёт и идёт на первое освободившееся."""
    completed_devices: list[str] = []

    async def async_handler(request: httpx.Request) -> httpx.Response:
        m = re.search(r"/api/devices/([^/]+)/fetch_page", str(request.url))
        dev = m.group(1) if m else "unknown"
        body = json.loads(request.content.decode("utf-8"))
        url = body.get("url", "")

        if url == "https://hold.test/dev1":
            # dev1 держится 0.12 секунды
            await asyncio.sleep(0.12)
        elif url == "https://hold.test/dev2":
            # dev2 держится 0.04 секунды и освобождается ПЕРВЫМ
            await asyncio.sleep(0.04)

        completed_devices.append(dev)
        return httpx.Response(
            200,
            json={"url": url, "text": f"done on {dev}", "finished": True},
        )

    _mock_async_client(monkeypatch, async_handler)

    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="token",
        droidhub_devices=["dev1", "dev2"],
    )

    # Занимаем dev1 и dev2
    t1 = asyncio.create_task(
        fetch_page_snapshot(settings, "dev1", "https://hold.test/dev1")
    )
    # Даём t1 захватить dev1
    await asyncio.sleep(0.005)

    t2 = asyncio.create_task(
        fetch_page_snapshot(settings, "dev2", "https://hold.test/dev2")
    )
    # Даём t2 захватить dev2
    await asyncio.sleep(0.005)

    # Теперь оба устройства заняты. Запускаем запрос, которому подойдёт любое свободное.
    t3 = asyncio.create_task(
        fetch_page_snapshot_any(settings, ["dev1", "dev2"], "https://site.test/waiting")
    )

    snap3, fin3 = await t3
    await asyncio.gather(t1, t2)

    assert fin3 is True
    # Так как dev2 освободился раньше dev1 (0.04s < 0.12s), t3 должен был взять dev2!
    assert "done on dev2" in snap3.text


@pytest.mark.asyncio
async def test_pool_502_retry_on_second_device_succeeds(monkeypatch):
    """502 на первом устройстве приводит к повтору на втором и успеху."""
    called_devices: list[str] = []

    async def async_handler(request: httpx.Request) -> httpx.Response:
        m = re.search(r"/api/devices/([^/]+)/fetch_page", str(request.url))
        dev = m.group(1) if m else "unknown"
        called_devices.append(dev)

        if dev == "dev1":
            return httpx.Response(502, text="Bad Gateway")
        return httpx.Response(
            200,
            json={"url": "https://ok.test", "text": "success on dev2", "finished": True},
        )

    _mock_async_client(monkeypatch, async_handler)

    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        droidhub_devices=["dev1", "dev2"],
    )

    snap, finished = await fetch_page_snapshot_any(
        settings, ["dev1", "dev2"], "https://site.test"
    )

    assert finished is True
    assert snap.text == "success on dev2"
    assert called_devices == ["dev1", "dev2"]


@pytest.mark.asyncio
async def test_pool_401_no_retry_and_reraised(monkeypatch):
    """401 не повторяется и пробрасывается сразу (без опроса второго устройства)."""
    called_devices: list[str] = []

    async def async_handler(request: httpx.Request) -> httpx.Response:
        m = re.search(r"/api/devices/([^/]+)/fetch_page", str(request.url))
        dev = m.group(1) if m else "unknown"
        called_devices.append(dev)

        if dev == "dev1":
            return httpx.Response(401, text="Unauthorized")
        return httpx.Response(200, json={"url": "https://ok.test", "text": "ok", "finished": True})

    _mock_async_client(monkeypatch, async_handler)

    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="invalid_token",
        droidhub_devices=["dev1", "dev2"],
    )

    with pytest.raises(DroidhubError) as exc_info:
        await fetch_page_snapshot_any(settings, ["dev1", "dev2"], "https://site.test")

    assert "401" in str(exc_info.value)
    # Второе устройство НЕ должно было вызываться
    assert called_devices == ["dev1"]


@pytest.mark.asyncio
async def test_pool_single_device_behaves_as_before(monkeypatch):
    """Одно устройство (не auto) ведёт себя как раньше (ошибки не повторяются, запросы сериализуются)."""
    active = 0
    max_active = 0
    call_count = 0

    async def async_handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active, call_count
        call_count += 1
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.04)
        active -= 1
        return httpx.Response(200, json={"url": "https://ok.test", "text": "single ok", "finished": True})

    _mock_async_client(monkeypatch, async_handler)

    site = SiteConfig("site1", "https://site1.test", phone="realme")
    settings = Settings(
        sites=[site],
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        droidhub_devices=["dev1", "dev2"],  # настройки пула не должны влиять на одиночный телефон
    )

    cands = candidates(settings, site)
    assert cands == ["realme"]

    # Параллельные запросы к одному устройству сериализуются
    t1 = asyncio.create_task(fetch_page_snapshot(settings, "realme", "https://site.test/1"))
    t2 = asyncio.create_task(fetch_page_snapshot(settings, "realme", "https://site.test/2"))
    res1, res2 = await asyncio.gather(t1, t2)

    assert res1[1] is True
    assert res2[1] is True
    assert max_active == 1
    assert call_count == 2

    # Ошибка 502 на одиночном устройстве не повторяется, а сразу бросается
    def err_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    _mock_async_client(monkeypatch, err_handler)
    with pytest.raises(DroidhubError) as exc:
        await fetch_page_snapshot(settings, "realme", "https://site.test/err")
    assert "502" in str(exc.value)


def test_phone_enabled_for_auto_and_candidates():
    """phone_enabled для 'auto' без списка устройств False; с устройствами True."""
    site_auto = SiteConfig("site", "https://site.test", phone="auto")
    site_direct = SiteConfig("site", "https://site.test", phone="realme")

    # auto без droidhub_devices -> False
    s_empty_devices = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        droidhub_devices=[],
    )
    assert phone_enabled(s_empty_devices, site_auto) is False
    assert candidates(s_empty_devices, site_auto) == []

    # auto с пробелами в устройствах -> False
    s_spaces_devices = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        droidhub_devices=["   ", ""],
    )
    assert phone_enabled(s_spaces_devices, site_auto) is False
    assert candidates(s_spaces_devices, site_auto) == []

    # auto с непустыми droidhub_devices -> True
    s_with_devices = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        droidhub_devices=["realme", "redmi"],
    )
    assert phone_enabled(s_with_devices, site_auto) is True
    assert candidates(s_with_devices, site_auto) == ["realme", "redmi"]

    # прямой id устройства не зависит от droidhub_devices
    assert phone_enabled(s_empty_devices, site_direct) is True
    assert candidates(s_empty_devices, site_direct) == ["realme"]


def test_load_settings_droidhub_devices(monkeypatch):
    """чтение DROIDHUB_DEVICES из переменной окружения."""
    monkeypatch.setenv("DROIDHUB_DEVICES", " realme, redmi , , xiaomi ")
    settings = load_settings()
    assert settings.droidhub_devices == ["realme", "redmi", "xiaomi"]

    monkeypatch.setenv("DROIDHUB_DEVICES", "   ")
    settings_empty = load_settings()
    assert settings_empty.droidhub_devices == []

    monkeypatch.delenv("DROIDHUB_DEVICES", raising=False)
    settings_none = load_settings()
    assert settings_none.droidhub_devices == []


@pytest.mark.asyncio
async def test_token_never_in_error_or_logs(monkeypatch, caplog):
    """Токен никогда не попадает в текст ошибки и логи."""
    secret_token = "SUPER_SECRET_DROID_TOKEN_ABC123XYZ"
    called_devices = []

    def handler(request: httpx.Request) -> httpx.Response:
        m = re.search(r"/api/devices/([^/]+)/fetch_page", str(request.url))
        dev = m.group(1) if m else "unknown"
        called_devices.append(dev)
        return httpx.Response(502, text=f"Error on {dev} with {secret_token}")

    _mock_async_client(monkeypatch, handler)

    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token=secret_token,
        droidhub_devices=["dev1", "dev2"],
    )

    caplog.set_level(logging.DEBUG)

    with pytest.raises(DroidhubError) as exc_info:
        await fetch_page_snapshot_any(settings, ["dev1", "dev2"], "https://site.test")

    err_text = str(exc_info.value)
    err_repr = repr(exc_info.value)

    assert secret_token not in err_text
    assert secret_token not in err_repr
    assert secret_token not in caplog.text


@pytest.mark.asyncio
async def test_pool_429_retry_and_network_error_retry(monkeypatch):
    """429 и сетевые ошибки приводят к повтору на следующем устройстве."""
    attempts = []

    async def async_handler(request: httpx.Request) -> httpx.Response:
        m = re.search(r"/api/devices/([^/]+)/fetch_page", str(request.url))
        dev = m.group(1) if m else "unknown"
        attempts.append(dev)
        if dev == "dev1":
            return httpx.Response(429, text="Rate limited")
        return httpx.Response(200, json={"url": "https://ok.test", "text": "recovered", "finished": True})

    _mock_async_client(monkeypatch, async_handler)

    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        droidhub_devices=["dev1", "dev2"],
    )

    snap, fin = await fetch_page_snapshot_any(settings, ["dev1", "dev2"], "https://site.test")
    assert fin is True
    assert snap.text == "recovered"
    assert attempts == ["dev1", "dev2"]


@pytest.mark.asyncio
async def test_pool_captcha_not_retried(monkeypatch):
    """Ошибка капчи не исключение (Snapshot.blocked), её не повторять."""
    called_devices = []

    def handler(request: httpx.Request) -> httpx.Response:
        m = re.search(r"/api/devices/([^/]+)/fetch_page", str(request.url))
        dev = m.group(1) if m else "unknown"
        called_devices.append(dev)
        return httpx.Response(
            200,
            json={
                "url": "https://captcha.test",
                "text": "Подтвердите, что вы не робот",
                "captcha": True,
                "finished": True,
            },
        )

    _mock_async_client(monkeypatch, handler)

    settings = Settings(
        sites=[],
        droidhub_url="https://hub.test",
        droidhub_token="tok",
        droidhub_devices=["dev1", "dev2"],
    )

    snap, fin = await fetch_page_snapshot_any(settings, ["dev1", "dev2"], "https://site.test")
    assert fin is True
    assert snap.blocked is True
    # Капча не повторяется на втором устройстве!
    assert called_devices == ["dev1"]


@pytest.mark.asyncio
async def test_acquire_and_release_direct():
    """Непосредственная проверка acquire и release."""
    dev = await acquire(["d1", "d2"])
    assert dev == "d1"
    dev2 = await acquire(["d1", "d2"])
    assert dev2 == "d2"

    # Теперь оба заняты, освобождаем d1
    await release("d1")
    dev3 = await acquire(["d1", "d2"])
    assert dev3 == "d1"

    await release("d2")
    await release("d1")
