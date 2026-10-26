"""Парсер живой выдачи Туту (avia.tutu.ru) по XHR/JSON-ответам /avia/offers/v2.

Схема данных API Туту (/avia/offers/v2):
----------------------------------------
Страница avia.tutu.ru отправляет POST-запрос на https://offers-api.tutu.ru/avia/offers/v2.
Ответ представляет собой JSON-массив из одного объекта-чанка со следующей структурой:
1. `summary`:
   - `offersCurrentAmount`: число предложений в текущем снимке/выдаче (например, 65, 87);
   - `offersTotalAmount`: общее число найденных предложений;
   - `currentMinPrice`, `totalMinPrice`: минимальная цена по всей выборке;
   - `avia`: объект со сводкой (`minPrice`, `direct`, `withLuggage`).
2. `pagination`:
   - `limit`: ограничение числа предложений на страницу (по умолчанию 10);
   - `offset`: смещение пагинации.
3. `offers`:
   - `actual`: словарь предложений `{<offer_id>: offer_data}`, где каждое предложение содержит:
     * `id`: строковый идентификатор предложения;
     * `pinLink`: прямая ссылка на предложение вида https://avia.tutu.ru/offers/?...&pin=...;
     * `routeIds`: массив идентификаторов маршрутов, ссылающихся на `dictionary.common.routes`;
     * `offerVariants`: массив тарифных вариантов для данного перелёта.
4. `dictionary`:
   - `common.routes`: словарь маршрутов `{<route_id>: {id, segmentIds, transferIds}}`.
     Число пересадок маршрута определяется как `len(transferIds)` либо `len(segmentIds) - 1`.
   - `common.segments`: словарь сегментов перелёта `{<segment_id>: {...}}`:
     * `departureDateTime`: время вылета в ISO-формате с таймзоной (например, '2026-12-12T12:35:00+03:00');
     * `arrivalDateTime`: время прилёта в ISO-формате с таймзоной (например, '2026-12-13T00:50:00+05:00');
     * `duration`: длительность сегмента в минутах;
     * `carriers`: массив перевозчиков `[{id: str, type: 'marketing' | 'operating'}]`;
     * `voyageNumber`: номер рейса (например, 'DV-1010');
     * `vehicleId`: идентификатор борта из `dictionary.avia.vehicles`;
     * `departureGeoPointId`, `arrivalGeoPointId`: гео-точки из `dictionary.avia.points` / `dictionary.common.cities`.
   - `common.carriers`: справочник перевозчиков `{<carrier_id>: {name, displaySettings, uri}}`.
   - `common.fareApplications`: справочник применений тарифов `{<fare_app_hash>: {segmentConditions, segmentHash, serviceClass, ...}}`.
   - `avia.conditions`: справочник условий тарифа `{<condition_id>: {baggageKg: {defined, value}, baggagePc: {defined, value}, fareFamily, ...}}`.
     Включение багажа определяется по полям `baggageKg.value > 0` или `baggagePc.value > 0`.

Ограничение по объёму выдачи:
------------------------------
API Туту возвращает только одну страницу (limit=10 предложений), тогда как глобальный минимум
по всему пулу найденных вариантов содержится в `summary.totalMinPrice` / `summary.avia.minPrice`.
Если среди разобранных предложений (до 10 штук на страницу) нет цены, равной `totalMinPrice`,
это нормальное поведение из-за пагинации; парсер извлекает и возвращает все фактически присутствующие
в ответе предложения.

Эмпирический признак полноты выдачи (results_complete):
------------------------------------------------------
Поиск Туту подгружает предложения асинхронно. На практике при поиске возвращается сначала
промежуточный чанк (около 65 предложений), а затем финальный (около 87 предложений).
Выдача считается полной (`results_complete(snapshot) == True`), если среди ответов '/avia/offers/v2'
присутствует хотя бы два ответа, либо один с `offersCurrentAmount >= 20`. Иначе `False`.
"""
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


def _calculate_duration(dep_iso: Any, arr_iso: Any, fallback_duration: Any = None) -> str | None:
    """Вычисляет длительность перелёта как разность абсолютных моментов или использует запасное поле длительности."""
    if isinstance(dep_iso, str) and isinstance(arr_iso, str):
        try:
            dep_dt = datetime.fromisoformat(dep_iso)
            arr_dt = datetime.fromisoformat(arr_iso)
            if dep_dt.tzinfo is not None and arr_dt.tzinfo is not None:
                diff_sec = (arr_dt - dep_dt).total_seconds()
                if diff_sec > 0:
                    minutes = int(round(diff_sec / 60))
                    return format_duration(minutes)
        except (ValueError, TypeError):
            pass
    if fallback_duration is not None and not isinstance(fallback_duration, bool):
        try:
            return format_duration(int(fallback_duration))
        except (ValueError, TypeError):
            pass
    return None


def _get_airline_name(seg: dict[str, Any], carriers_dict: dict[str, Any]) -> str | None:
    """Возвращает название авиакомпании из справочника common.carriers, иначе код."""
    carriers_list = seg.get("carriers")
    carrier_ref = None
    if isinstance(carriers_list, list) and carriers_list:
        for c in carriers_list:
            if isinstance(c, dict) and c.get("type") == "operating":
                carrier_ref = c
                break
        if carrier_ref is None:
            for c in carriers_list:
                if isinstance(c, dict):
                    carrier_ref = c
                    break

    carrier_id = str(carrier_ref.get("id")) if carrier_ref and carrier_ref.get("id") is not None else None
    if carrier_id and carrier_id in carriers_dict:
        c_info = carriers_dict[carrier_id]
        if isinstance(c_info, dict):
            name = c_info.get("name")
            if name:
                return str(name)
            code = c_info.get("code") or c_info.get("iata")
            if code:
                return str(code)

    voyage = seg.get("voyageNumber")
    if isinstance(voyage, str) and "-" in voyage:
        return voyage.split("-", 1)[0]
    if carrier_id:
        return str(carrier_id)
    return None


def _has_baggage(
    variant: dict[str, Any],
    fare_apps_dict: dict[str, Any],
    conditions_dict: dict[str, Any],
) -> bool:
    """Определяет, включён ли багаж в тариф (baggageKg > 0 или baggagePc > 0 в условиях)."""
    fare_applications = variant.get("fareApplications")
    if not isinstance(fare_applications, dict):
        return False

    for _seg_id, hashes in fare_applications.items():
        hash_list = hashes if isinstance(hashes, list) else [hashes]
        for fh in hash_list:
            if not isinstance(fh, str):
                continue
            fa = fare_apps_dict.get(fh)
            if not isinstance(fa, dict):
                continue
            cond_id = fa.get("segmentConditions")
            if not cond_id or not isinstance(cond_id, str):
                continue
            cond = conditions_dict.get(cond_id)
            if not isinstance(cond, dict):
                continue

            for field_name in ("baggageKg", "baggagePc"):
                b_val = cond.get(field_name)
                val = None
                if isinstance(b_val, dict):
                    val = b_val.get("value")
                elif isinstance(b_val, (int, float)) and not isinstance(b_val, bool):
                    val = b_val
                if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
                    return True

    return False


def results_complete(snapshot: Snapshot | None) -> bool:
    """Проверяет полноту результатов поиска Туту по снимку сетевых ответов.

    Выдача считается полной (True), если среди ответов '/avia/offers/v2' есть хотя бы два,
    или один с offersCurrentAmount >= 20. Иначе False.
    """
    if not snapshot or not snapshot.json_payloads:
        return False

    v2_count = 0
    for url, body in snapshot.json_payloads:
        if "/avia/offers/v2" not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue

        if isinstance(data, list) and data:
            chunk = data[0]
        elif isinstance(data, dict):
            chunk = data
        else:
            continue

        if not isinstance(chunk, dict):
            continue

        v2_count += 1
        summary = chunk.get("summary")
        if isinstance(summary, dict):
            raw_amt = summary.get("offersCurrentAmount")
            if isinstance(raw_amt, (int, float)) and not isinstance(raw_amt, bool) and raw_amt >= 20:
                return True

    return v2_count >= 2


@register_parser("tutu")
def parse_tutu(snapshot: Snapshot, query: Query) -> list[Offer]:
    """Разбирает JSON-ответы Туту (/avia/offers/v2) из snapshot в список Offer."""
    if not snapshot or not snapshot.json_payloads:
        return []

    # Берём ответы, чей URL содержит '/avia/offers/v2'.
    # Если пришло несколько, побеждает ответ с большим summary.offersCurrentAmount (при равенстве более поздний).
    best_chunk: dict[str, Any] | None = None
    best_amount = -1

    for url, body in snapshot.json_payloads:
        if "/avia/offers/v2" not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue

        if isinstance(data, list) and data:
            chunk = data[0]
        elif isinstance(data, dict):
            chunk = data
        else:
            continue

        if not isinstance(chunk, dict):
            continue

        summary = chunk.get("summary")
        amount = -1
        if isinstance(summary, dict):
            raw_amt = summary.get("offersCurrentAmount")
            if isinstance(raw_amt, (int, float)) and not isinstance(raw_amt, bool):
                amount = int(raw_amt)

        if amount >= best_amount:
            best_amount = amount
            best_chunk = chunk

    if best_chunk is None:
        return []

    offers_data = best_chunk.get("offers")
    if not isinstance(offers_data, dict):
        return []

    actual_offers = offers_data.get("actual")
    if not isinstance(actual_offers, dict) or not actual_offers:
        return []

    dictionary = best_chunk.get("dictionary")
    if not isinstance(dictionary, dict):
        dictionary = {}

    common_dict = dictionary.get("common")
    if not isinstance(common_dict, dict):
        common_dict = {}

    avia_dict = dictionary.get("avia")
    if not isinstance(avia_dict, dict):
        avia_dict = {}

    routes_dict = common_dict.get("routes")
    if not isinstance(routes_dict, dict):
        routes_dict = {}

    segments_dict = common_dict.get("segments")
    if not isinstance(segments_dict, dict):
        segments_dict = {}

    carriers_dict = common_dict.get("carriers")
    if not isinstance(carriers_dict, dict):
        carriers_dict = {}

    fare_apps_dict = common_dict.get("fareApplications")
    if not isinstance(fare_apps_dict, dict):
        fare_apps_dict = {}

    conditions_dict = avia_dict.get("conditions")
    if not isinstance(conditions_dict, dict):
        conditions_dict = {}

    offers: list[Offer] = []

    for _offer_id, offer in actual_offers.items():
        if not isinstance(offer, dict):
            continue
        try:
            variants = offer.get("offerVariants")
            if not isinstance(variants, list) or not variants:
                continue

            # Для каждого предложения берём САМЫЙ ДЕШЁВЫЙ из offerVariants
            best_var_price = float("inf")
            best_variant: dict[str, Any] | None = None
            currency = str((query.currency if query is not None else "RUB") or "RUB")

            for v in variants:
                if not isinstance(v, dict):
                    continue
                p_dict = v.get("price")
                if not isinstance(p_dict, dict):
                    continue
                val_dict = p_dict.get("value")
                if not isinstance(val_dict, dict):
                    continue
                raw_amount = val_dict.get("amount")
                raw_fraction = val_dict.get("fraction", 100)
                if raw_amount is None or isinstance(raw_amount, bool):
                    continue
                if raw_fraction is None or isinstance(raw_fraction, bool):
                    continue
                try:
                    amt = int(raw_amount)
                    frac = int(raw_fraction)
                    if frac <= 0 or amt <= 0:
                        continue
                    p = amt // frac
                    if p <= 0:
                        continue
                except (ValueError, TypeError, ZeroDivisionError):
                    continue

                curr = str(
                    val_dict.get("currencyCode")
                    or (query.currency if query is not None else "RUB")
                    or "RUB"
                )

                if p < best_var_price:
                    best_var_price = p
                    best_variant = v
                    currency = curr

            if best_variant is None or best_var_price == float("inf"):
                continue

            price = int(best_var_price)

            # Извлечение маршрута и сегментов
            route_ids = offer.get("routeIds")
            if not isinstance(route_ids, list) or not route_ids:
                continue

            first_route_id = route_ids[0]
            route = routes_dict.get(first_route_id)
            if not isinstance(route, dict):
                continue

            segment_ids = route.get("segmentIds")
            if not isinstance(segment_ids, list) or not segment_ids:
                continue

            first_seg = segments_dict.get(segment_ids[0])
            last_seg = segments_dict.get(segment_ids[-1])
            if not isinstance(first_seg, dict) or not isinstance(last_seg, dict):
                continue

            # Пересадки: число пересадок в первом/единственном маршруте
            transfer_ids = route.get("transferIds")
            if isinstance(transfer_ids, list):
                transfers = len(transfer_ids)
            else:
                transfers = max(0, len(segment_ids) - 1)

            # Авиакомпания перевозчика
            airline = _get_airline_name(first_seg, carriers_dict)

            # Время вылета и прилёта (местное время как дано в HH:MM)
            depart_time = _extract_time(first_seg.get("departureDateTime"))
            arrive_time = _extract_time(last_seg.get("arrivalDateTime"))

            # Длительность перелёта
            duration = _calculate_duration(
                first_seg.get("departureDateTime"),
                last_seg.get("arrivalDateTime"),
                fallback_duration=first_seg.get("duration"),
            )

            # Багаж
            baggage = (
                "с багажом"
                if _has_baggage(best_variant, fare_apps_dict, conditions_dict)
                else "без багажа"
            )

            # Ссылка на предложение
            pin_link = offer.get("pinLink")
            url = (
                pin_link
                if isinstance(pin_link, str) and pin_link.startswith("https://avia.tutu.ru")
                else None
            )

            offers.append(
                Offer(
                    site="tutu",
                    price=price,
                    currency=currency,
                    airline=airline,
                    depart_time=depart_time,
                    arrive_time=arrive_time,
                    duration=duration,
                    transfers=transfers,
                    baggage=baggage,
                    url=url,
                )
            )
        except Exception:
            # Битые предложения пропускать, не падать
            continue

    offers.sort(key=lambda o: o.price)
    return offers


# Привязываем results_complete к функции парсера для pipeline.py
parse_tutu.results_complete = results_complete  # type: ignore[attr-defined]
