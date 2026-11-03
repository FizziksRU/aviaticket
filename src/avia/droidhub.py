"""Интеграция с сервисом droidhub для сбора страниц через Android-телефон."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
import logging
from typing import Any

import httpx

from .collector import Snapshot
from .config import Settings, SiteConfig

logger = logging.getLogger(__name__)

# Блокировки по device_id для сериализации запросов к каждому конкретному телефону
_device_locks: dict[str, asyncio.Lock] = {}

# Состояние пула устройств
_busy_devices: set[str] = set()
_pool_condition: asyncio.Condition | None = None
_pool_loop: asyncio.AbstractEventLoop | None = None
_rr_counter: int = 0


class DroidhubError(Exception):
    """Исключение при ошибках обращения к сервису droidhub."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _sync_loop_state() -> asyncio.AbstractEventLoop | None:
    """Синхронизирует блокировки и состояние пула с текущим циклом событий."""
    global _pool_condition, _pool_loop, _busy_devices, _device_locks
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _pool_loop is not loop:
        _pool_condition = None
        _pool_loop = loop
        _busy_devices.clear()
        _device_locks.clear()
    return loop


def _get_device_lock(device_id: str) -> asyncio.Lock:
    """Возвращает или создаёт asyncio.Lock для конкретного device_id."""
    clean_id = device_id.strip()
    _sync_loop_state()
    lock = _device_locks.get(clean_id)
    if lock is None:
        lock = asyncio.Lock()
        _device_locks[clean_id] = lock
    return lock


def _get_pool_condition() -> asyncio.Condition:
    """Возвращает или создаёт asyncio.Condition, привязанный к текущему циклу событий."""
    global _pool_condition
    _sync_loop_state()
    if _pool_condition is None:
        _pool_condition = asyncio.Condition()
    return _pool_condition


def _reset_pool() -> None:
    """Сброс состояния пула (для изоляции тестов)."""
    global _pool_condition, _pool_loop, _rr_counter
    _busy_devices.clear()
    _device_locks.clear()
    _pool_condition = None
    _pool_loop = None
    _rr_counter = 0


def candidates(settings: Settings, site: SiteConfig) -> list[str]:
    """Возвращает список идентификаторов устройств для сайта.

    Для 'auto' возвращает список устройств из настроек, иначе [site.phone].
    PHONE_MAP (settings.phone_map) переопределяет phone из sites.yaml.
    """
    mapped = (getattr(settings, "phone_map", None) or {}).get(site.name)
    if mapped:
        return list(mapped)
    phone = (getattr(site, "phone", None) or "").strip()
    if phone == "auto":
        devs = getattr(settings, "droidhub_devices", None) or []
        return [d.strip() for d in devs if d and d.strip()]
    return [phone] if phone else []


def phone_enabled(settings: Settings, site: SiteConfig) -> bool:
    """True, если заданы droidhub_url и droidhub_token, site.phone непустой и
    (site.phone != 'auto' или settings.droidhub_devices непустой).
    """
    phone = (getattr(site, "phone", None) or "").strip()
    if (getattr(settings, "phone_map", None) or {}).get(site.name):
        phone = "mapped"
    url = (getattr(settings, "droidhub_url", None) or "").strip()
    token = (getattr(settings, "droidhub_token", None) or "").strip()
    if not (phone and url and token):
        return False
    if phone in ("auto", "mapped"):
        return bool(candidates(settings, site))
    return True


def retry_candidates(settings: Settings, site: SiteConfig, failed_device: str | None) -> list[str]:
    """Телефоны для повтора пустой выдачи: при RETRY_OTHER_PHONE любой из пула, кроме failed_device.

    Выдача зависит от пары «сайт × телефон», поэтому повтор на том же телефоне часто снова пуст.
    Если других телефонов нет или режим выключен, возвращает обычных кандидатов сайта.
    """
    base = candidates(settings, site)
    if not getattr(settings, "retry_other_phone", False) or not failed_device:
        return base
    pool = base + [d for d in (getattr(settings, "droidhub_devices", None) or []) if d not in base]
    others = [d for d in pool if d != failed_device]
    return others or base


async def acquire(candidates: list[str]) -> str:
    """Ожидает, пока не освободится хотя бы одно устройство из списка кандидатов.

    Берёт первое свободное устройство, начиная со смещения round-robin,
    помечает его занятым и продвигает счётчик round-robin.
    """
    clean: list[str] = []
    for c in candidates:
        s = c.strip()
        if s and s not in clean:
            clean.append(s)
    if not clean:
        raise ValueError("список устройств-кандидатов пуст")

    cond = _get_pool_condition()
    async with cond:
        while True:
            global _rr_counter
            n = len(clean)
            offset = _rr_counter % n
            for i in range(n):
                dev = clean[(offset + i) % n]
                lock = _device_locks.get(dev)
                is_locked = lock.locked() if lock is not None else False
                if dev not in _busy_devices and not is_locked:
                    _busy_devices.add(dev)
                    _rr_counter = (_rr_counter + 1) % 1_000_000
                    return dev
            await cond.wait()


async def release(device_id: str) -> None:
    """Освобождает устройство и будит ожидающие запросы."""
    clean = device_id.strip()
    cond = _get_pool_condition()
    async with cond:
        _busy_devices.discard(clean)
        cond.notify_all()


def _is_retryable_error(exc: Exception) -> bool:
    """True для ошибок, при которых имеет смысл повторить запрос на другом телефоне.

    Повторяем при:
    - 502 (телефон недоступен или страница не открылась);
    - 429 (очередь занята);
    - сетевых ошибках и таймаутах.
    Ошибки авторизации (401/403) не повторяем.
    """
    if not isinstance(exc, DroidhubError):
        return False
    status = getattr(exc, "status_code", None)
    if status in (401, 403):
        return False
    msg = str(exc).lower()
    if "401" in msg or "403" in msg or "авторизац" in msg:
        return False
    if status in (502, 429):
        return True
    if "502" in msg or "429" in msg or "таймаут" in msg or "сетевая ошибка" in msg or "timeout" in msg:
        return True
    return False


async def _fetch_device_snapshot(
    settings: Settings,
    device_id: str,
    url: str,
    *,
    wait_seconds: int = 40,
    capture_json: list[str] | None = None,
    wait_for: str = "",
    client: httpx.AsyncClient | None = None,
) -> tuple[Snapshot, bool]:
    """Выполняет непосредственный запрос к сервису droidhub для конкретного устройства."""
    device_id = device_id.strip()
    lock = _get_device_lock(device_id)
    async with lock:
        wait_val = max(1, min(int(wait_seconds), 60))
        timeout = float(wait_seconds + 60)
        endpoint = f"{settings.droidhub_url.strip().rstrip('/')}/api/devices/{device_id}/fetch_page"
        headers = {"Authorization": f"Bearer {settings.droidhub_token.strip()}"}
        payload: dict[str, Any] = {
            "url": url,
            "wait_seconds": wait_val,
            "capture_json": list(capture_json) if capture_json is not None else [],
            "max_chars": 200000,
        }
        if wait_for:
            payload["wait_for"] = wait_for

        try:
            if client is not None:
                resp = await client.post(endpoint, json=payload, headers=headers)
            else:
                async with httpx.AsyncClient(timeout=timeout) as cl:
                    resp = await cl.post(endpoint, json=payload, headers=headers)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            raise DroidhubError("таймаут при обращении к droidhub")
        except (httpx.RequestError, ConnectionError, OSError):
            raise DroidhubError("сетевая ошибка при обращении к droidhub")

        if resp.status_code in (401, 403):
            raise DroidhubError(
                f"ошибка авторизации droidhub ({resp.status_code}): неверный токен",
                status_code=resp.status_code,
            )
        if resp.status_code == 429:
            raise DroidhubError("очередь droidhub занята (429)", status_code=429)
        if resp.status_code == 502:
            raise DroidhubError("телефон недоступен или страница не открылась (502)", status_code=502)
        if resp.status_code != 200:
            raise DroidhubError(f"ошибка droidhub (код {resp.status_code})", status_code=resp.status_code)

        try:
            data = resp.json()
        except Exception:
            raise DroidhubError("некорректный JSON в ответе droidhub")

        if not isinstance(data, dict):
            raise DroidhubError("некорректная структура ответа droidhub")

        resp_url = str(data.get("url") or url)
        resp_text = data.get("text") or ""
        if not isinstance(resp_text, str):
            raise DroidhubError("некорректный формат текста в ответе droidhub")

        captcha = bool(data.get("captcha", False))
        finished = bool(data.get("finished", True))

        raw_responses = data.get("json_responses")
        if raw_responses is None:
            raw_responses = []
        elif not isinstance(raw_responses, list):
            raise DroidhubError("некорректный формат json_responses в ответе droidhub")

        json_payloads: list[tuple[str, str]] = []
        for item in raw_responses:
            if not isinstance(item, dict):
                raise DroidhubError("некорректный элемент в json_responses")
            item_url = str(item.get("url", ""))
            raw_body = item.get("body", "")
            if isinstance(raw_body, str):
                body_str = raw_body
            elif isinstance(raw_body, (dict, list)):
                body_str = json.dumps(raw_body, ensure_ascii=False)
            elif raw_body is None:
                body_str = ""
            else:
                body_str = str(raw_body)
            json_payloads.append((item_url, body_str))

        snap = Snapshot(
            url=resp_url,
            text=resp_text,
            json_payloads=json_payloads,
            blocked=captcha,
        )
        return snap, finished


async def fetch_page_snapshot_any(
    settings: Settings,
    candidates: list[str],
    url: str,
    *,
    wait_seconds: int = 40,
    capture_json: list[str] | None = None,
    wait_for: str = "",
    client: httpx.AsyncClient | None = None,
) -> tuple[Snapshot, bool]:
    """Открывает страницу на любом свободном телефоне из candidates.

    Берёт свободное устройство через acquire, делает запрос. Если телефон
    недоступен (502), очередь занята (429) или возникла сетевая ошибка/таймаут,
    и есть другие непробованные кандидаты — повторяет на следующем свободном устройстве.
    Каждое устройство опрашивается максимум один раз за вызов.
    Ошибки авторизации (401/403) пробрасываются сразу.
    Капча не считается исключением и не повторяется.
    """
    clean_candidates: list[str] = []
    for c in candidates:
        s = c.strip()
        if s and s not in clean_candidates:
            clean_candidates.append(s)

    if not clean_candidates:
        raise DroidhubError("список устройств пуст")

    tried: set[str] = set()
    last_exc: Exception | None = None

    while len(tried) < len(clean_candidates):
        remaining = [c for c in clean_candidates if c not in tried]
        if not remaining:
            break
        dev = await acquire(remaining)
        tried.add(dev)
        try:
            snap, finished = await _fetch_device_snapshot(
                settings,
                dev,
                url,
                wait_seconds=wait_seconds,
                capture_json=capture_json,
                wait_for=wait_for,
                client=client,
            )
            return replace(snap, device=dev), finished
        except Exception as exc:
            last_exc = exc
            if not _is_retryable_error(exc):
                raise
            if not any(c for c in clean_candidates if c not in tried):
                raise
            log_exc = str(exc)
            token = (getattr(settings, "droidhub_token", "") or "").strip()
            if token and token in log_exc:
                log_exc = log_exc.replace(token, "[REDACTED]")
            logger.warning("Устройство %s вернуло ошибку (%s), пробуем следующее устройство", dev, log_exc)
        finally:
            await release(dev)

    if last_exc is not None:
        raise last_exc
    raise DroidhubError("не удалось получить снимок страницы ни с одного устройства")


async def fetch_page_snapshot(
    settings: Settings,
    device_id: str | list[str],
    url: str,
    *,
    wait_seconds: int = 40,
    capture_json: list[str] | None = None,
    wait_for: str = "",
    client: httpx.AsyncClient | None = None,
) -> tuple[Snapshot, bool]:
    """Открывает страницу на телефоне через droidhub и возвращает (Snapshot, finished).

    Сохраняет совместимость с прежней сигнатурой (device_id: str) и поддерживает
    передачу списка кандидатов (device_id: list[str]).
    """
    if isinstance(device_id, (list, tuple)):
        return await fetch_page_snapshot_any(
            settings,
            list(device_id),
            url,
            wait_seconds=wait_seconds,
            capture_json=capture_json,
            wait_for=wait_for,
            client=client,
        )
    return await fetch_page_snapshot_any(
        settings,
        [device_id],
        url,
        wait_seconds=wait_seconds,
        capture_json=capture_json,
        wait_for=wait_for,
        client=client,
    )
