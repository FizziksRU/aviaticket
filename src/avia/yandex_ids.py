"""Разрешение IATA-кодов городов во внутренние идентификаторы Яндекс Путешествий."""
from __future__ import annotations

# Статичная проверенная таблица IATA -> id Яндекс Путешествий
STATIC_CITIES: dict[str, str] = {
    "MOW": "c213",
    "LED": "c2",
    "KUF": "c51",
    "SVX": "c54",
    "KZN": "c43",
    "AER": "c239",
    "OVB": "c65",
    "UFA": "c172",
    "MSQ": "c157",
    "ALA": "c22177",
    "NQZ": "c163",
    "TAS": "c10335",
    "SKD": "c10334",
    "FRU": "c10309",
    "EVN": "c10262",
    "GYD": "c10253",
    "TBS": "c10277",
    "DYU": "c10318",
    "IST": "c11508",
    "DXB": "c11499",
}


def city_id(iata: str) -> str:
    """Возвращает внутренний идентификатор Яндекс Путешествий (например, 'c213') для IATA-кода."""
    code = iata.strip().upper()
    if code in STATIC_CITIES:
        return STATIC_CITIES[code]
    raise RuntimeError(
        f"Яндекс Путешествия: город {iata} пока не поддерживается (нет id в таблице yandex_ids.py)"
    )
