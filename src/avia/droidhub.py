"""Интеграция с сервисом droidhub для сбора страниц через Android-телефон."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from .collector import Snapshot
from .config import Settings, SiteConfig

logger = logging.getLogger(__name__)

# Блокировки по device_id для сериализации запросов к каждому конкретному телефону
_device_locks: dict[str, asyncio.Lock] = {}


class DroidhubError(Exception):
    """Исключение при ошибках обращения к сервису droidhub."""


def _get_device_lock(device_id: str) -> asyncio.Lock:
    """Возвращает или создаёт asyncio.Lock для конкретного device_id."""
    lock = _device_locks.get(device_id)
    if lock is None:
        lock = asyncio.Lock()
        _device_locks[device_id] = lock
    return lock


def phone_enabled(settings: Settings, site: SiteConfig) -> bool:
    """True только если site.phone, settings.droidhub_url и settings.droidhub_token заданы."""
    return bool(
        getattr(site, "phone", None)
        and site.phone.strip()
        and getattr(settings, "droidhub_url", None)
        and settings.droidhub_url.strip()
        and getattr(settings, "droidhub_token", None)
        and settings.droidhub_token.strip()
    )


async def fetch_page_snapshot(
    settings: Settings,
    device_id: str,
    url: str,
    *,
    wait_seconds: int = 40,
    capture_json: list[str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[Snapshot, bool]:
    """Открывает страницу на телефоне через droidhub и возвращает (Snapshot, finished).

    Snapshot содержит:
      - url: итоговый адрес страницы;
      - text: полный видимый текст страницы;
      - json_payloads: список пар (url, body) перехваченных JSON-ответов;
      - blocked: True, если обнаружена капча (captcha=true).
    finished: False, если выдача не догрузилась до конца за отведённое время.
    """
    lock = _get_device_lock(device_id)
    async with lock:
        wait_val = min(int(wait_seconds), 60)
        timeout = float(wait_seconds + 60)
        endpoint = f"{settings.droidhub_url.rstrip('/')}/api/devices/{device_id}/fetch_page"
        headers = {"Authorization": f"Bearer {settings.droidhub_token}"}
        payload: dict[str, Any] = {
            "url": url,
            "wait_seconds": wait_val,
            "capture_json": list(capture_json) if capture_json is not None else [],
            "max_chars": 200000,
        }

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
            raise DroidhubError(f"ошибка авторизации droidhub ({resp.status_code}): неверный токен")
        if resp.status_code == 429:
            raise DroidhubError("очередь droidhub занята (429)")
        if resp.status_code == 502:
            raise DroidhubError("телефон недоступен или страница не открылась (502)")
        if resp.status_code != 200:
            raise DroidhubError(f"ошибка droidhub (код {resp.status_code})")

        try:
            data = resp.json()
        except Exception:
            raise DroidhubError("некорректный JSON в ответе droidhub")

        if not isinstance(data, dict):
            raise DroidhubError("некорректная структура ответа droidhub")

        resp_url = str(data.get("url") or url)
        resp_text = data.get("text", "")
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
