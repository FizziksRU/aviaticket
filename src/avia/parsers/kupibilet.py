"""Парсер живой выдачи Купибилет по XHR/JSON-ответам /frontend_search."""
from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from ..collector import Snapshot
from ..models import Offer, Query
from . import register_parser
from .travelpayouts import format_duration


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


def _calculate_duration(dep_iso: Any, arr_iso: Any) -> str | None:
    """Вычисляет длительность перелёта как разность абсолютных моментов с учётом часовых поясов."""
    if not dep_iso or not arr_iso or not isinstance(dep_iso, str) or not isinstance(arr_iso, str):
        return None
    try:
        dep_dt = datetime.fromisoformat(dep_iso)
        arr_dt = datetime.fromisoformat(arr_iso)
    except (ValueError, TypeError):
        return None
    # Без часовой зоны длительность не считаем
    if dep_dt.tzinfo is None or arr_dt.tzinfo is None:
        return None
    diff_sec = (arr_dt - dep_dt).total_seconds()
    if diff_sec <= 0:
        return None
    minutes = int(round(diff_sec / 60))
    return format_duration(minutes)


def results_complete(snapshot: Snapshot | None) -> bool:
    """Проверяет полноту результатов поиска Купибилет по снимку сетевых ответов.

    Живой сайт Купибилет при поиске делает POST на api-rs-lb.kupibilet.ru/frontend_search.
    За один поиск приходят два ответа: быстрый (около 8 вариантов) и полный
    (около 200 вариантов, через ~3 секунды после первого).

    Эмпирический признак: выдача считается полной (True), если среди ответов,
    содержащих '/frontend_search', есть хотя бы два ответа, или один с числом
    вариантов >= 20 (признак подтверждён на двух запусках). Иначе False.
    """
    if not snapshot or not snapshot.json_payloads:
        return False

    frontend_search_count = 0
    for url, body in snapshot.json_payloads:
        if "/frontend_search" not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        frontend_search_count += 1
        variants = data.get("variants")
        if isinstance(variants, list) and len(variants) >= 20:
            return True

    return frontend_search_count >= 2


@register_parser("kupibilet")
def parse_kupibilet(snapshot: Snapshot, query: Query) -> list[Offer]:
    """Разбирает JSON-ответы Купибилет (/frontend_search) из snapshot в список Offer."""
    if not snapshot or not snapshot.json_payloads:
        return []

    # Берём ответы, чей URL содержит '/frontend_search'.
    # Если пришло несколько, побеждает ответ с большим числом вариантов (при равенстве более поздний).
    best_payload: dict[str, Any] | None = None
    best_count = -1

    for url, body in snapshot.json_payloads:
        if "/frontend_search" not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        variants_raw = data.get("variants")
        count = len(variants_raw) if isinstance(variants_raw, list) else 0
        if count >= best_count:
            best_count = count
            best_payload = data

    if best_payload is None:
        return []

    variants_list = best_payload.get("variants")
    if not isinstance(variants_list, list) or not variants_list:
        return []

    flights = best_payload.get("flights")
    if not isinstance(flights, dict):
        flights = {}

    airlines = best_payload.get("airlines")
    if not isinstance(airlines, dict):
        airlines = {}

    offers: list[Offer] = []

    for variant in variants_list:
        if not isinstance(variant, dict):
            continue
        try:
            price_dict = variant.get("price")
            if not isinstance(price_dict, dict):
                continue
            raw_amount = price_dict.get("amount")
            if raw_amount is None or isinstance(raw_amount, bool):
                continue
            price = int(float(raw_amount))
            if price <= 0:
                continue

            currency = str(price_dict.get("currency") or (query.currency if query is not None else "RUB") or "RUB")

            segments = variant.get("segments")
            if not isinstance(segments, list) or not segments:
                continue
            first_seg = segments[0]
            if not isinstance(first_seg, dict):
                continue
            flight_ids = first_seg.get("flights")
            if not isinstance(flight_ids, list) or not flight_ids:
                continue

            # Проверяем все перелёты в первом сегменте
            seg_flights: list[dict[str, Any]] = []
            valid_flights = True
            for fid in flight_ids:
                fl = flights.get(fid)
                if not isinstance(fl, dict):
                    valid_flights = False
                    break
                # Только transport_kind 'airplane' или без этого поля (поезда пропускаем)
                tk = fl.get("transport_kind")
                if tk is not None and str(tk).strip().lower() != "airplane":
                    valid_flights = False
                    break
                seg_flights.append(fl)

            if not valid_flights or not seg_flights:
                continue

            # Пересадки = число перелётов в первом сегменте минус 1
            transfers = len(seg_flights) - 1

            first_fl = seg_flights[0]
            last_fl = seg_flights[-1]

            # Авиакомпания: название по коду operating_carrier первого перелёта из airlines, иначе сам код
            carrier_code = first_fl.get("operating_carrier")
            airline: str | None = None
            if carrier_code and isinstance(carrier_code, str):
                airline_info = airlines.get(carrier_code)
                if isinstance(airline_info, dict) and airline_info.get("name"):
                    airline = str(airline_info["name"])
                elif isinstance(airline_info, str):
                    airline = airline_info
                else:
                    airline = carrier_code

            # Время вылета и прилёта (местное время аэропортов без пересчёта поясов)
            depart_time = _extract_time(first_fl.get("departure_datetime"))
            arrive_time = _extract_time(last_fl.get("arrival_datetime"))

            # Длительность перелёта с учётом часовых поясов
            duration = _calculate_duration(
                first_fl.get("departure_datetime"),
                last_fl.get("arrival_datetime"),
            )

            # Багаж
            baggage_info = variant.get("baggage")
            baggage_count = 0
            if isinstance(baggage_info, dict):
                raw_cnt = baggage_info.get("count")
                if raw_cnt is not None and not isinstance(raw_cnt, bool):
                    try:
                        baggage_count = int(raw_cnt)
                    except (ValueError, TypeError):
                        baggage_count = 0
            baggage = "с багажом" if baggage_count > 0 else "без багажа"

            offers.append(
                Offer(
                    site="kupibilet",
                    price=price,
                    currency=currency,
                    airline=airline,
                    depart_time=depart_time,
                    arrive_time=arrive_time,
                    duration=duration,
                    transfers=transfers,
                    baggage=baggage,
                    url=None,
                )
            )
        except Exception:
            # Битые варианты пропускаем, не падая
            continue

    offers.sort(key=lambda o: o.price)
    return offers


# Привязываем results_complete к функции парсера для pipeline.py
parse_kupibilet.results_complete = results_complete  # type: ignore[attr-defined]
