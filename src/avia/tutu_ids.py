"""Разрешение IATA-кодов городов во внутренние идентификаторы Туту (city_id, seo_id)."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

SUGGEST_URL = "https://suggester-avia.tutu.ru/api/location_suggest/v2"
DEFAULT_USER_AGENT = "Mozilla/5.0"
TIMEOUT_SECONDS = 10.0

# Встроенный словарь-запас для частых городов: {IATA: (city_id, seo_id)}
STATIC_CITIES: dict[str, tuple[str, str]] = {
    "MOW": ("491", "Moskva"),
    "ALA": ("117", "Almati"),
}

# Кэш в памяти на процесс: {IATA: (city_id, seo_id)}
_CACHE: dict[str, tuple[str, str]] = {}


def parse_suggestions(data: Any, iata: str) -> tuple[str, str]:
    """Извлекает (city_id, seo_id) для запрошенного IATA-кода из JSON-ответа подсказчика.

    Выбирает первую подсказку с geo_type == 'LOCALITY' и city.code == iata (без учёта регистра).
    """
    target_code = iata.strip().upper()
    if not isinstance(data, dict):
        raise RuntimeError(f"Некорректный ответ сервиса подсказок Туту для {target_code}")

    suggestions = data.get("suggestions")
    if not isinstance(suggestions, list) or not suggestions:
        raise RuntimeError(f"Пустой ответ сервиса подсказок Туту: город {target_code} не найден")

    for item in suggestions:
        if not isinstance(item, dict):
            continue
        if item.get("geo_type") != "LOCALITY":
            continue
        city = item.get("city")
        if not isinstance(city, dict):
            continue
        code = str(city.get("code") or "").strip().upper()
        if code == target_code:
            city_id = str(city.get("id") or "").strip()
            seo_id = str(city.get("seo_id") or "").strip()
            if city_id and seo_id:
                return city_id, seo_id

    raise RuntimeError(f"Город {target_code} не найден в подсказках Туту")


async def resolve_city(iata: str, client: httpx.AsyncClient | None = None) -> tuple[str, str]:
    """Возвращает (city_id, seo_id) для переданного IATA-кода города.

    Использует кэш в памяти на процесс и встроенный словарь-запас для частых городов.
    Для неизвестных городов делает запрос к публичному подсказчику Туту.
    """
    code = iata.strip().upper()
    if not code:
        raise RuntimeError("Не указан IATA-код города")

    if code in _CACHE:
        return _CACHE[code]

    # Если клиент не передан явно (боевой режим), сначала проверяем словарь-запас
    if client is None and code in STATIC_CITIES:
        res = STATIC_CITIES[code]
        _CACHE[code] = res
        return res

    params = {
        "name": code,
        "direction": "to",
        "location_id": "491",
    }
    headers = {"User-Agent": DEFAULT_USER_AGENT}

    try:
        if client is not None:
            resp = await client.get(
                SUGGEST_URL,
                params=params,
                headers=headers,
                timeout=TIMEOUT_SECONDS,
            )
        else:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, headers=headers) as c:
                resp = await c.get(
                    SUGGEST_URL,
                    params=params,
                )
    except (httpx.HTTPError, httpx.RequestError, asyncio.TimeoutError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Ошибка сети при запросе подсказок Туту для {code}: {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(f"Ошибка сервиса подсказок Туту для {code} (статус {resp.status_code})")

    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"Некорректный JSON в ответе подсказчика Туту для {code}: {exc}") from exc

    res = parse_suggestions(data, code)
    _CACHE[code] = res
    return res
