"""Парсер живой выдачи Яндекс Путешествий (travel.yandex.ru) по XHR/JSON-ответам /api/avia/search/results.

Схема данных API Яндекс Путешествий (/api/avia/search/results):
---------------------------------------------------------------
Страница опрашивает API порциями GET https://travel.yandex.ru/api/avia/search/results?qid=...&cont=0|1|2...
Каждая порция содержит:
1. `reference`:
   - `flights`: список перелётов `[{key, number, company, aviaCompany, from, to, departure:{local, offset, tzname}, arrival:{...}, companyTariff, ...}]`;
   - `companies`: список компаний `[{id, title, ...}]`;
   - `aviaCompanies`: список авиакомпаний `[{id, title, costType, baggageRules, ...}]`;
   - `companyTariffs`: справочник тарифов компании `[{id, baggageAllowed, carryon, baggageNorm, carryonNorm}]`;
   - `stations`: справочник станций и аэропортов;
   - `partners`: справочник партнёров `[{id, code, title, ...}]`.
2. `variants`:
   - `fares`: список тарифов `[{route: [[рейсы_туда], [рейсы_обратно]], prices: [...], hybrid}]`.
     `route[0]` содержит ключи рейсов outbound-маршрута. Число пересадок = len(route[0]) - 1.
3. `cont`: маркер продолжения пагинации (число 1, 2, ... на промежуточных ответах, null/None на финальном).
4. `progress`: прогресс опроса партнёров `{'current': 4, 'all': 20}`.
5. `partners`: статусы опроса партнёров `{'done' | 'querying'}`.

Цена и пассажиры:
-----------------
Поиск выполняется с параметром adult_seats=1 (и 0 детей/младенцев). Поле `tariff.value` в объекте
цены предложения партнёра отражает итоговую стоимость билета для запрошенного числа пассажиров
(в рублях, валюта 'RUR', приводится к общему стандарту 'RUB'). Дополнительного умножения
на query.adults не требуется.

Нормы багажа:
-------------
Формат строки нормы багажа в `price.baggage[0]` представляет собой строковый код вида:
`<мест_багажа><тип><мест_ручной_клади><тип><норма_веса><тип>`, например:
- '0d1d15d' — 0 мест зарегистрированного багажа, 1 место ручной клади (норма 15 кг);
- '0p0p0p' — 0 мест багажа и 0 мест ручной клади;
- '1p1p23p' — 1 место багажа (до 23 кг), 1 место ручной клади;
- '1p1pN' — 1 место багажа без явного лимита веса.
Тариф считается 'с багажом', если в выбранном самом дешёвом предложении хотя бы для одного
сегмента маршрута (`baggage[0]`) число мест багажа > 0 (первая цифра кода > 0). Если поле
`baggage` отсутствует или пусто, используется флаг `companyTariffs.baggageAllowed` первого рейса.
Во всех остальных случаях тариф считается 'без багажа'.

Эмпирический признак полноты выдачи (results_complete):
------------------------------------------------------
Выдача считается завершённой (`results_complete(snapshot) == True`), если среди ответов
'/api/avia/search/results' есть чанк с `cont` равным null (или отсутствующим) и
`progress.current == progress.all` (если блок `progress` присутствует). Иначе False.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
import re
from typing import Any

from ..collector import Snapshot
from ..models import Offer, Query
from . import register_parser
from .travelpayouts import format_duration

_BAGGAGE_PIECES_RE = re.compile(r"^(\d+)[a-zA-Z]")


def _parse_baggage_pieces(code: str) -> int:
    """Извлекает число мест багажа из строки тарифа (например, '0d1d15d' -> 0, '1p1p23p' -> 1)."""
    if not code or not isinstance(code, str):
        return 0
    m = _BAGGAGE_PIECES_RE.match(code)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, TypeError):
            return 0
    return 0


def _has_baggage(
    best_price: dict[str, Any],
    first_flight: dict[str, Any] | None,
    company_tariffs: dict[Any, dict[str, Any]],
) -> bool:
    """Определяет, включен ли багаж в выбранное ценовое предложение."""
    baggage_data = best_price.get("baggage")
    if (
        isinstance(baggage_data, list)
        and len(baggage_data) > 0
        and isinstance(baggage_data[0], list)
        and baggage_data[0]
    ):
        for code in baggage_data[0]:
            if isinstance(code, str) and _parse_baggage_pieces(code) > 0:
                return True
        return False

    # Запасной вариант: проверяем companyTariffs первого рейса
    if first_flight and isinstance(first_flight, dict):
        tariff_id = first_flight.get("companyTariff")
        if tariff_id is not None and tariff_id in company_tariffs:
            ct = company_tariffs[tariff_id]
            if isinstance(ct, dict) and ct.get("baggageAllowed") is True:
                return True

    return False


def _extract_time(dt_str: Any) -> str | None:
    """Извлекает время в формате HH:MM из строки даты/времени без пересчёта часовых поясов."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    for sep in ("T", " "):
        if sep in dt_str:
            parts = dt_str.split(sep, 1)
            if len(parts) > 1 and len(parts[1]) >= 5:
                return parts[1][:5]
    return dt_str[:5] if len(dt_str) >= 5 else None


def _calculate_duration(dep_dict: Any, arr_dict: Any) -> str | None:
    """Вычисляет длительность перелёта как разность абсолютных моментов (local минус offset минут)."""
    if not isinstance(dep_dict, dict) or not isinstance(arr_dict, dict):
        return None
    dep_local = dep_dict.get("local")
    arr_local = arr_dict.get("local")
    dep_offset = dep_dict.get("offset")
    arr_offset = arr_dict.get("offset")

    if not isinstance(dep_local, str) or not isinstance(arr_local, str):
        return None
    if not isinstance(dep_offset, (int, float)) or not isinstance(arr_offset, (int, float)):
        return None

    try:
        dep_dt = datetime.fromisoformat(dep_local).replace(
            tzinfo=timezone(timedelta(minutes=int(dep_offset)))
        )
        arr_dt = datetime.fromisoformat(arr_local).replace(
            tzinfo=timezone(timedelta(minutes=int(arr_offset)))
        )
    except (ValueError, TypeError):
        return None

    diff_sec = (arr_dt - dep_dt).total_seconds()
    if diff_sec <= 0:
        return None
    minutes = int(round(diff_sec / 60))
    return format_duration(minutes)


def _get_airline_name(
    flight: dict[str, Any] | None,
    companies: dict[Any, dict[str, Any]],
    avia_companies: dict[Any, dict[str, Any]],
) -> str | None:
    """Возвращает название авиакомпании из справочников companies / aviaCompanies."""
    if not flight or not isinstance(flight, dict):
        return None
    company_id = flight.get("company")
    if company_id is None:
        company_id = flight.get("aviaCompany")
    if company_id is None:
        return None

    comp = companies.get(company_id)
    if isinstance(comp, dict):
        title = comp.get("title")
        if title:
            return str(title)

    ac = avia_companies.get(company_id)
    if isinstance(ac, dict):
        title = ac.get("title")
        if title:
            return str(title)

    return str(company_id)


def _unwrap_payload(payload: Any) -> dict[str, Any] | None:
    """Извлекает корневой словарь ответа (поддерживает как прямой JSON, так и обёртку {'data': ...})."""
    if not isinstance(payload, dict):
        return None
    if "data" in payload and isinstance(payload["data"], dict):
        data_inner = payload["data"]
        if "variants" in data_inner or "reference" in data_inner or "progress" in data_inner:
            return data_inner
    return payload


def results_complete(snapshot: Snapshot | None) -> bool:
    """Проверяет полноту результатов поиска Яндекс Путешествий по снимку сетевых ответов.

    True, если среди ответов '/api/avia/search/results' есть чанк с cont равным
    null/отсутствующим и progress.current == progress.all (если progress есть),
    иначе False.
    """
    if not snapshot or not snapshot.json_payloads:
        return False

    for url, body in snapshot.json_payloads:
        if "/api/avia/search/results" not in url:
            continue
        try:
            raw_data = json.loads(body)
        except Exception:
            continue
        data = _unwrap_payload(raw_data)
        if not isinstance(data, dict):
            continue

        if data.get("cont") is not None:
            continue

        progress = data.get("progress")
        if isinstance(progress, dict):
            cur = progress.get("current")
            total = progress.get("all")
            if cur is not None and total is not None:
                if cur == total:
                    return True
                continue
            return True

        return True

    return False


@register_parser("yandex_travel")
def parse_yandex_travel(snapshot: Snapshot, query: Query) -> list[Offer]:
    """Разбирает JSON-ответы Яндекс Путешествий (/api/avia/search/results) из snapshot в список Offer."""
    if not snapshot or not snapshot.json_payloads:
        return []

    flights_ref: dict[str, Any] = {}
    companies_ref: dict[Any, dict[str, Any]] = {}
    avia_companies_ref: dict[Any, dict[str, Any]] = {}
    company_tariffs_ref: dict[Any, dict[str, Any]] = {}

    valid_chunks: list[dict[str, Any]] = []
    seen_portions: set[Any] = set()

    for url, body in snapshot.json_payloads:
        if "/api/avia/search/results" not in url:
            continue
        try:
            raw_data = json.loads(body)
        except Exception:
            continue
        data = _unwrap_payload(raw_data)
        if not isinstance(data, dict):
            continue

        # Объединяем справочники всех порций
        ref = data.get("reference")
        if isinstance(ref, dict):
            for fl in ref.get("flights") or []:
                if isinstance(fl, dict) and "key" in fl:
                    flights_ref[str(fl["key"])] = fl
            for c in ref.get("companies") or []:
                if isinstance(c, dict) and "id" in c:
                    companies_ref[c["id"]] = c
            for ac in ref.get("aviaCompanies") or []:
                if isinstance(ac, dict) and "id" in ac:
                    avia_companies_ref[ac["id"]] = ac
            for ct in ref.get("companyTariffs") or []:
                if isinstance(ct, dict) and "id" in ct:
                    company_tariffs_ref[ct["id"]] = ct

        # Дедупликация повторной подачи одной и той же порции
        cont = data.get("cont")
        prog = data.get("progress") if isinstance(data.get("progress"), dict) else {}
        fares_raw = data.get("variants", {}).get("fares") if isinstance(data.get("variants"), dict) else []
        fares_sig = tuple(
            tuple(tuple(leg) for leg in fare.get("route", []))
            for fare in (fares_raw or [])
            if isinstance(fare, dict) and "route" in fare
        )
        portion_id = (cont, prog.get("current"), prog.get("all"), fares_sig)
        if portion_id in seen_portions:
            continue
        seen_portions.add(portion_id)
        valid_chunks.append(data)

    offers: list[Offer] = []

    for data in valid_chunks:
        variants = data.get("variants")
        if not isinstance(variants, dict):
            continue
        fares = variants.get("fares")
        if not isinstance(fares, list):
            continue

        for fare in fares:
            if not isinstance(fare, dict):
                continue
            try:
                route = fare.get("route")
                if (
                    not isinstance(route, list)
                    or not route
                    or not isinstance(route[0], list)
                    or not route[0]
                ):
                    continue

                first_key = str(route[0][0])
                last_key = str(route[0][-1])
                f0 = flights_ref.get(first_key)
                f_last = flights_ref.get(last_key)
                if not f0 or not f_last:
                    continue

                prices = fare.get("prices")
                if not isinstance(prices, list) or not prices:
                    continue

                best_prop: dict[str, Any] | None = None
                best_price: int | None = None
                for p in prices:
                    if not isinstance(p, dict):
                        continue
                    tariff = p.get("tariff")
                    if not isinstance(tariff, dict):
                        continue
                    val = tariff.get("value")
                    if val is None or isinstance(val, bool):
                        continue
                    try:
                        val_int = int(round(float(val)))
                    except (ValueError, TypeError):
                        continue
                    if val_int <= 0:
                        continue
                    if best_price is None or val_int < best_price:
                        best_price = val_int
                        best_prop = p

                if best_price is None or best_prop is None:
                    continue

                curr = best_prop.get("tariff", {}).get("currency")
                if str(curr).upper() in ("RUR", "RUB"):
                    currency = "RUB"
                else:
                    currency = str(curr or query.currency or "RUB").upper()

                transfers = len(route[0]) - 1
                airline = _get_airline_name(f0, companies_ref, avia_companies_ref)
                dep_time = _extract_time(
                    f0.get("departure", {}).get("local")
                    if isinstance(f0.get("departure"), dict)
                    else None
                )
                arrive_time = _extract_time(
                    f_last.get("arrival", {}).get("local")
                    if isinstance(f_last.get("arrival"), dict)
                    else None
                )
                duration = _calculate_duration(f0.get("departure"), f_last.get("arrival"))
                baggage = (
                    "с багажом"
                    if _has_baggage(best_prop, f0, company_tariffs_ref)
                    else "без багажа"
                )

                offers.append(
                    Offer(
                        site="yandex_travel",
                        price=best_price,
                        currency=currency,
                        airline=airline,
                        depart_time=dep_time,
                        arrive_time=arrive_time,
                        duration=duration,
                        transfers=transfers,
                        baggage=baggage,
                        url=None,
                    )
                )
            except Exception:
                continue

    offers.sort(key=lambda o: o.price)
    return offers


# Привязываем results_complete к функции парсера для pipeline.py
parse_yandex_travel.results_complete = results_complete  # type: ignore[attr-defined]
