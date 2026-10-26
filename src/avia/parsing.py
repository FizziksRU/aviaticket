"""Разбор команды пользователя: /price Москва Алматы 10.12 [2]"""
from __future__ import annotations

import re
from datetime import date

from .models import Query

CITIES = {
    "москва": "MOW", "санкт-петербург": "LED", "питер": "LED", "спб": "LED",
    "самара": "KUF", "екатеринбург": "SVX", "казань": "KZN", "сочи": "AER",
    "новосибирск": "OVB", "уфа": "UFA", "минск": "MSQ", "алматы": "ALA",
    "астана": "NQZ", "ташкент": "TAS", "самарканд": "SKD", "бишкек": "FRU",
    "ереван": "EVN", "баку": "GYD", "тбилиси": "TBS", "душанбе": "DYU",
    "стамбул": "IST", "дубай": "DXB",
}


class ParseError(ValueError):
    pass


def to_iata(word: str) -> str:
    w = word.strip().lower()
    if w in CITIES:
        return CITIES[w]
    if re.fullmatch(r"[a-zA-Z]{3}", w):
        return w.upper()
    raise ParseError(f"не знаю город «{word}». Укажите код IATA, например ALA")


def parse_date(s: str, today: date | None = None) -> date:
    today = today or date.today()
    s = s.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        d = date(int(m[1]), int(m[2]), int(m[3]))
    else:
        m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", s)
        if not m:
            raise ParseError(f"не понял дату «{s}». Формат: 10.12 или 2026-12-10")
        day, month = int(m[1]), int(m[2])
        if m[3]:
            year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        else:
            year = today.year
        try:
            d = date(year, month, day)
        except ValueError as e:
            raise ParseError(f"такой даты нет: {s}") from e
        if not m[3] and d < today:
            d = date(year + 1, month, day)
    if d < today:
        raise ParseError("эта дата уже в прошлом")
    return d


def parse_query(text: str, today: date | None = None) -> Query:
    """Принимает аргументы после команды: «Москва Алматы 10.12 2»."""
    args = text.split()
    if len(args) < 3:
        raise ParseError("Формат: /price Москва Алматы 10.12 [пассажиров]")
    adults = 1
    if len(args) >= 4:
        if not args[3].isdigit() or not (1 <= int(args[3]) <= 9):
            raise ParseError("число пассажиров: от 1 до 9")
        adults = int(args[3])
    o, d = to_iata(args[0]), to_iata(args[1])
    if o == d:
        raise ParseError("города вылета и прилёта совпадают")
    return Query(o, d, parse_date(args[2], today), adults)
