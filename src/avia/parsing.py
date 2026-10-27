"""Разбор команды пользователя: /price Москва Алматы 10.12 [2]"""
from __future__ import annotations

from datetime import date
import difflib
import re

from .models import Query

CITIES = {
    "москва": "MOW", "санкт-петербург": "LED", "питер": "LED", "спб": "LED",
    "самара": "KUF", "екатеринбург": "SVX", "казань": "KZN", "сочи": "AER",
    "новосибирск": "OVB", "уфа": "UFA", "минск": "MSQ", "алматы": "ALA",
    "астана": "NQZ", "ташкент": "TAS", "самарканд": "SKD", "бишкек": "FRU",
    "ереван": "EVN", "баку": "GYD", "тбилиси": "TBS", "душанбе": "DYU",
    "стамбул": "IST", "дубай": "DXB",
    "санкт петербург": "LED", "ростов": "ROV", "краснодар": "KRR",
    "нижний новгород": "GOJ", "владивосток": "VVO", "калининград": "KGD",
    "нур-султан": "NQZ", "алма-ата": "ALA", "шымкент": "CIT", "ош": "OSS",
    "нукус": "NCU",
}

CITY_NAMES: dict[str, str] = {
    "MOW": "Москва", "LED": "Санкт-Петербург", "KUF": "Самара", "SVX": "Екатеринбург",
    "KZN": "Казань", "AER": "Сочи", "OVB": "Новосибирск", "UFA": "Уфа", "MSQ": "Минск",
    "ALA": "Алматы", "NQZ": "Астана", "TAS": "Ташкент", "SKD": "Самарканд", "FRU": "Бишкек",
    "EVN": "Ереван", "GYD": "Баку", "TBS": "Тбилиси", "DYU": "Душанбе", "IST": "Стамбул",
    "DXB": "Дубай", "ROV": "Ростов", "KRR": "Краснодар", "GOJ": "Нижний Новгород",
    "VVO": "Владивосток", "KGD": "Калининград", "CIT": "Шымкент", "OSS": "Ош", "NCU": "Нукус",
}


class ParseError(ValueError):
    pass


class UnknownCityError(ParseError):
    def __init__(self, message: str, suggestions: list[str] | None = None) -> None:
        super().__init__(message)
        self.suggestions: list[str] = list(suggestions) if suggestions is not None else []


def city_name(code: str) -> str:
    code_upper = code.strip().upper()
    return CITY_NAMES.get(code_upper, code)


def suggest_cities(word: str, limit: int = 3) -> list[str]:
    w = re.sub(r"\s+", " ", word.strip()).lower().replace("ё", "е")
    if not w or limit <= 0:
        return []
    matches = difflib.get_close_matches(w, CITIES.keys(), n=len(CITIES), cutoff=0.6)
    res: list[str] = []
    seen: set[str] = set()
    for m in matches:
        code = CITIES[m]
        if code not in seen:
            seen.add(code)
            res.append(city_name(code))
            if len(res) == limit:
                break
    return res


def to_iata(word: str) -> str:
    raw = word.strip()
    w = re.sub(r"\s+", " ", raw).lower().replace("ё", "е")
    if w in CITIES:
        return CITIES[w]
    w_hyphen = w.replace(" ", "-")
    if w_hyphen in CITIES:
        return CITIES[w_hyphen]
    w_space = w.replace("-", " ")
    if w_space in CITIES:
        return CITIES[w_space]
    if re.fullmatch(r"[a-zA-Z]{3}", w):
        return w.upper()
    suggestions = suggest_cities(w)
    msg = f"не знаю город «{raw}». Укажите код IATA, например ALA"
    if suggestions:
        msg += f". Возможно, вы имели в виду: {', '.join(suggestions)}?"
    raise UnknownCityError(msg, suggestions=suggestions)


def _parse_date_parts(s: str) -> tuple[int, int, int | None]:
    s = s.strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return int(m[3]), int(m[2]), int(m[1])
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}|\d{2}))?", s)
    if not m:
        raise ParseError(f"не понял дату «{s}». Формат: 10.12 или 2026-12-10")
    day, month = int(m[1]), int(m[2])
    year = None
    if m[3]:
        year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
    return day, month, year


def parse_date(s: str, today: date | None = None) -> date:
    today = today or date.today()
    day, month, year = _parse_date_parts(s)
    has_year = year is not None
    if year is None:
        year = today.year
    try:
        d = date(year, month, day)
    except ValueError as e:
        raise ParseError(f"такой даты нет: {s}") from e
    if not has_year and d < today:
        try:
            d = date(year + 1, month, day)
        except ValueError as e:
            raise ParseError(f"такой даты нет: {s}") from e
    if d < today:
        raise ParseError("эта дата уже в прошлом, введите будущую дату")
    return d


def parse_return_date(s: str | date, depart: date, today: date | None = None) -> date:
    today = today or date.today()
    if isinstance(s, date):
        d = s
    else:
        day, month, year = _parse_date_parts(s)
        has_year = year is not None
        if year is None:
            year = depart.year
        try:
            d = date(year, month, day)
        except ValueError as e:
            raise ParseError(f"такой даты нет: {s}") from e
        if not has_year and d < depart:
            try:
                d = date(year + 1, month, day)
            except ValueError as e:
                raise ParseError(f"такой даты нет: {s}") from e
    if d < depart:
        raise ParseError("обратная дата раньше даты вылета")
    if d < today:
        raise ParseError("эта дата уже в прошлом, введите будущую дату")
    return d


def parse_passengers(s: str) -> int:
    s = str(s).strip()
    if s.isdigit() and 1 <= int(s) <= 9:
        return int(s)
    raise ParseError("число пассажиров: от 1 до 9")


def parse_query(text: str, today: date | None = None) -> Query:
    """Принимает аргументы после команды: «Москва Алматы 10.12 2»."""
    args = text.split()
    if len(args) < 3:
        raise ParseError("Формат: /price Москва Алматы 10.12 [пассажиров]")
    adults = 1
    if len(args) >= 4:
        adults = parse_passengers(args[3])
    o, d = to_iata(args[0]), to_iata(args[1])
    if o == d:
        raise ParseError("города вылета и прилёта совпадают")
    return Query(o, d, parse_date(args[2], today), adults)
