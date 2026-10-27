"""Парсер живой выдачи OneTwoTrip по XHR/JSON-ответам /_avia-search-proxy/search/v3."""
from __future__ import annotations

from datetime import datetime
import json
import math
from typing import Any

from ..collector import Snapshot
from ..models import Leg, Offer, Query
from .. import parsing
from ..parsing import city_name
from . import register_parser
from .travelpayouts import format_duration

# Справочник названий авиакомпаний по IATA-коду
CARRIER_NAMES: dict[str, str] = {
    "SU": "Аэрофлот",
    "S7": "S7 Airlines",
    "DP": "Победа",
    "UT": "Utair",
    "A4": "Азимут",
    "3F": "FLYONE Armenia",
    "EK": "Emirates",
    "FZ": "flydubai",
    "TK": "Turkish Airlines",
    "U6": "Уральские авиалинии",
    "N4": "Nordwind Airlines",
    "FV": "Россия",
    "HY": "Uzbekistan Airways",
    "KC": "Air Astana",
    "J2": "AZAL",
    "QR": "Qatar Airways",
    "PC": "Pegasus",
    "W6": "Wizz Air",
    "B2": "Belavia",
    "R3": "Якутия",
    "5N": "Smartavia",
    "WZ": "Red Wings",
}


def _extract_time(dt_str: Any) -> str | None:
    """Извлекает время в формате 'HH:MM' из строки даты/времени ISO без часового пояса."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    for sep in ("T", " "):
        if sep in dt_str:
            parts = dt_str.split(sep, 1)
            if len(parts) > 1 and len(parts[1]) >= 5:
                return parts[1][:5]
    return dt_str[:5] if len(dt_str) >= 5 else None


def _format_local_iso(dt_str: Any) -> str | None:
    """Преобразует строку даты/времени в формат местного ISO вида 'YYYY-MM-DDTHH:MM'."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    cleaned = dt_str.strip()
    if not cleaned:
        return None
    if " " in cleaned:
        cleaned = cleaned.replace(" ", "T", 1)
    if "T" in cleaned:
        d_part, t_part = cleaned.split("T", 1)
        if len(t_part) >= 5:
            return f"{d_part}T{t_part[:5]}"
    return None


def _get_city_name(code: Any, airports: dict[str, Any], cities: dict[str, Any]) -> str:
    """Возвращает название города пересадки: русское через parsing.city_name, либо английское из references."""
    if not code or not isinstance(code, str):
        return ""
    code_str = code.strip().upper()
    if not code_str:
        return ""

    city_code = code_str
    apt = airports.get(code_str) if isinstance(airports, dict) else None
    if isinstance(apt, dict):
        c_code = apt.get("city")
        if c_code and isinstance(c_code, str) and c_code.strip():
            city_code = c_code.strip().upper()

    # Сначала русское название города через parsing.city_name
    ru_name = parsing.city_name(city_code)
    if ru_name != city_code:
        return str(ru_name)

    # Если city_name вернул сам код (не знает), тогда английское name из references.cities/airports
    if isinstance(cities, dict):
        for key in (city_code, code_str):
            city_entry = cities.get(key)
            if isinstance(city_entry, dict):
                c_name = city_entry.get("name")
                if c_name and isinstance(c_name, str) and c_name.strip():
                    return c_name.strip()

    if isinstance(apt, dict):
        apt_name = apt.get("name")
        if apt_name and isinstance(apt_name, str) and apt_name.strip():
            return apt_name.strip()
    elif isinstance(airports, dict):
        for a_key in (code_str, city_code):
            a_entry = airports.get(a_key)
            if isinstance(a_entry, dict):
                apt_name = a_entry.get("name")
                if apt_name and isinstance(apt_name, str) and apt_name.strip():
                    return apt_name.strip()

    # Иначе код аэропорта (в верхнем регистре)
    return code_str


def _calculate_layover_wait(end_iso: Any, start_iso: Any) -> str:
    """Вычисляет длительность пересадки по локальному времени аэропорта (startDateTime след. − endDateTime пред.)."""
    if not end_iso or not start_iso:
        return "0 мин"
    try:
        dt_end = datetime.fromisoformat(end_iso)
        dt_start = datetime.fromisoformat(start_iso)
        diff_sec = (dt_start - dt_end).total_seconds()
    except (ValueError, TypeError):
        return "0 мин"
    if diff_sec <= 0:
        return "0 мин"
    diff_min = int(round(diff_sec / 60))
    h = diff_min // 60
    m = diff_min % 60
    if h > 0 and m > 0:
        return f"{h} ч {m} мин"
    if h > 0:
        return f"{h} ч"
    return f"{m} мин"


def _has_baggage(variants_list: list[dict[str, Any]], services: dict[str, Any]) -> bool:
    """Проверяет включение багажа: по ВСЕМ направлениям и перелётам должен быть baggage с code не на '0'."""
    for v in variants_list:
        v_sids = v.get("serviceIds") or []
        if not any(
            isinstance(services.get(sid), dict)
            and services[sid].get("type") == "baggage"
            and not str(services[sid].get("code") or "").startswith("0")
            for sid in v_sids
        ):
            return False
        for tr in v.get("tripRefs", []):
            tr_sids = tr.get("serviceIds") or []
            if not any(
                isinstance(services.get(sid), dict)
                and services[sid].get("type") == "baggage"
                and not str(services[sid].get("code") or "").startswith("0")
                for sid in tr_sids
            ):
                return False
    return True


def _has_cabin_baggage(variants_list: list[dict[str, Any]], services: dict[str, Any]) -> str | None:
    """Проверяет наличие услуги cabinBaggage в списке вариантов и перелётов."""
    for v in variants_list:
        sids = list(v.get("serviceIds") or [])
        for tr in v.get("tripRefs", []):
            sids.extend(tr.get("serviceIds") or [])
        for sid in sids:
            s = services.get(sid)
            if isinstance(s, dict) and s.get("type") == "cabinBaggage":
                return "ручная кладь есть"
    return None


def _get_chain(variant: dict[str, Any]) -> tuple[str, ...]:
    """Возвращает кортеж идентификаторов перелётов цепочки (tripId)."""
    return tuple(str(tr.get("tripId")) for tr in variant.get("tripRefs", []) if tr.get("tripId"))


def _parse_variant(
    variant: dict[str, Any],
    trips: dict[str, Any],
    equipment: dict[str, Any],
    cities: dict[str, Any],
    airports: dict[str, Any],
) -> dict[str, Any] | None:
    """Разбирает вариант перелёта одного направления (цепочку tripRefs)."""
    trip_refs = variant.get("tripRefs")
    if not isinstance(trip_refs, list) or not trip_refs:
        return None
    trip_list: list[dict[str, Any]] = []
    for tr in trip_refs:
        tid = tr.get("tripId")
        if not tid or tid not in trips:
            return None
        t = trips[tid]
        if not isinstance(t, dict):
            return None
        trip_list.append(t)
    if not trip_list:
        return None

    t0 = trip_list[0]
    t_last = trip_list[-1]
    carrier = str(t0.get("carrier") or t0.get("operatingCarrier") or "").strip()
    airline = CARRIER_NAMES.get(carrier, carrier) if carrier else None

    depart_time = _extract_time(t0.get("startDateTime"))
    arrive_time = _extract_time(t_last.get("endDateTime"))
    depart_at = _format_local_iso(t0.get("startDateTime"))
    arrive_at = _format_local_iso(t_last.get("endDateTime"))
    duration = format_duration(variant.get("totalJourneyTimeMinutes"))
    transfers = len(trip_list) - 1

    flight_numbers = [
        f"{t.get('carrier')} {t.get('carrierTripNumber')}".strip()
        for t in trip_list
        if t.get("carrier") and t.get("carrierTripNumber")
    ]

    seen_eq = set()
    aircraft_names: list[str] = []
    for t in trip_list:
        eq = t.get("equipment")
        if eq:
            eq_name = str(equipment.get(eq) or eq).strip()
            if eq_name and eq_name not in seen_eq:
                seen_eq.add(eq_name)
                aircraft_names.append(eq_name)
    aircraft = ", ".join(aircraft_names) if aircraft_names else None

    layovers: list[str] | None = None
    if transfers > 0:
        layovers_list: list[str] = []
        for prev_trip, next_trip in zip(trip_list[:-1], trip_list[1:]):
            code = prev_trip.get("to") or next_trip.get("from")
            transfer_city = _get_city_name(code, airports, cities)
            wait_str = _calculate_layover_wait(prev_trip.get("endDateTime"), next_trip.get("startDateTime"))
            layovers_list.append(f"{transfer_city}, {wait_str}")
        layovers = layovers_list if layovers_list else None

    return {
        "airline": airline,
        "depart_time": depart_time,
        "arrive_time": arrive_time,
        "duration": duration,
        "transfers": transfers,
        "flight_numbers": flight_numbers,
        "aircraft": aircraft,
        "layovers": layovers,
        "depart_at": depart_at,
        "arrive_at": arrive_at,
    }


def results_complete(snapshot: Snapshot | None) -> bool:
    """Проверяет полноту результатов поиска OneTwoTrip по сетевым ответам.

    True, если среди payloads с '/search/v3' есть ответ с непустым словарём prices.
    """
    if not snapshot or not snapshot.json_payloads:
        return False
    for url, body in snapshot.json_payloads:
        if "/search/v3" not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        prices = data.get("prices")
        if isinstance(prices, dict) and len(prices) > 0:
            return True
    return False


@register_parser("onetwotrip")
def parse_onetwotrip(snapshot: Snapshot, query: Query) -> list[Offer]:
    """Разбирает JSON-ответы OneTwoTrip (/_avia-search-proxy/search/v3) из snapshot в список Offer."""
    if not snapshot or not snapshot.json_payloads:
        return []

    # Выбираем ответ /search/v3 с наибольшим числом цен (prices)
    best_payload: dict[str, Any] | None = None
    best_prices_count = -1

    for url, body in snapshot.json_payloads:
        if "/search/v3" not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        prices_dict = data.get("prices")
        count = len(prices_dict) if isinstance(prices_dict, dict) else 0
        if count > best_prices_count:
            best_prices_count = count
            best_payload = data

    if best_payload is None or best_prices_count <= 0:
        return []

    is_roundtrip = query.return_date is not None
    req_info = best_payload.get("requestInfo")
    if not isinstance(req_info, dict):
        return []

    directions = req_info.get("directions")
    expected_directions = 2 if is_roundtrip else 1
    if not isinstance(directions, list) or len(directions) != expected_directions:
        return []

    # Защита от чужих дат: сверяем даты в directions с датами запроса
    depart_iso = query.depart.isoformat()
    if not isinstance(directions[0], dict) or directions[0].get("date") != depart_iso:
        return []

    return_iso = query.return_date.isoformat() if is_roundtrip else None
    if is_roundtrip:
        if not isinstance(directions[1], dict) or directions[1].get("date") != return_iso:
            return []

    references = best_payload.get("references", {})
    equipment = references.get("equipment", {})
    cities = references.get("cities", {})
    airports = references.get("airports", {})
    services = references.get("services", {})
    trips = best_payload.get("trips", {})
    variants = best_payload.get("transportationVariants", {})
    prices = best_payload.get("prices", {})
    currency = str(best_payload.get("currency") or "RUB")

    if is_roundtrip:
        candidates: list[tuple[float, dict[str, Any], dict[str, Any]]] = []

        # (а) Готовые цены туда-обратно с 2 variantIds
        for price_obj in prices.values():
            if not isinstance(price_obj, dict):
                continue
            vids = price_obj.get("transportationVariantIds")
            if not isinstance(vids, list) or len(vids) != 2:
                continue
            v0 = variants.get(vids[0])
            v1 = variants.get(vids[1])
            if not isinstance(v0, dict) or not isinstance(v1, dict):
                continue
            if v0.get("directionIndex") == 1 and v1.get("directionIndex") == 0:
                v0, v1 = v1, v0
            if v0.get("directionIndex") != 0 or v1.get("directionIndex") != 1:
                continue
            amt = price_obj.get("totalAmount")
            if amt is None:
                continue
            try:
                cost = float(amt)
            except (ValueError, TypeError):
                continue
            candidates.append((cost, v0, v1))

        # (б) Цены-половинки направления 0 и 1
        halves_dir0: list[tuple[float, dict[str, Any]]] = []
        halves_dir1: list[tuple[float, dict[str, Any]]] = []
        for price_obj in prices.values():
            if not isinstance(price_obj, dict):
                continue
            vids = price_obj.get("transportationVariantIds")
            if not isinstance(vids, list) or len(vids) != 1:
                continue
            v = variants.get(vids[0])
            if not isinstance(v, dict):
                continue
            amt = price_obj.get("totalAmount")
            if amt is None:
                continue
            try:
                cost = float(amt)
            except (ValueError, TypeError):
                continue
            didx = v.get("directionIndex")
            if didx == 0:
                halves_dir0.append((cost, v))
            elif didx == 1:
                halves_dir1.append((cost, v))

        for c0, v0 in halves_dir0:
            for c1, v1 in halves_dir1:
                candidates.append((c0 + c1, v0, v1))

        # Сортируем кандидатов по стоимости (возрастание)
        candidates.sort(key=lambda item: item[0])

        # Дедупликация по кортежу (цепочка туда, цепочка обратно) с ограничением до 2000 пар
        seen_chains: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
        deduped: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for cost, v0, v1 in candidates:
            chain0 = _get_chain(v0)
            chain1 = _get_chain(v1)
            pair_key = (chain0, chain1)
            if pair_key in seen_chains:
                continue
            seen_chains.add(pair_key)
            deduped.append((math.ceil(round(cost, 2)), v0, v1))
            if len(deduped) >= 2000:
                break

        offers: list[Offer] = []
        for price_int, v0, v1 in deduped:
            info0 = _parse_variant(v0, trips, equipment, cities, airports)
            info1 = _parse_variant(v1, trips, equipment, cities, airports)
            if info0 is None or info1 is None:
                continue

            # Дополнительная проверка дат вылета первого перелёта
            t0_start = info0.get("depart_at")
            t1_start = info1.get("depart_at")
            if not t0_start or t0_start[:10] != depart_iso:
                continue
            if not t1_start or t1_start[:10] != return_iso:
                continue

            baggage = "с багажом" if _has_baggage([v0, v1], services) else "без багажа"
            carry_on = _has_cabin_baggage([v0, v1], services)

            back_leg = Leg(
                airline=info1["airline"],
                depart_time=info1["depart_time"],
                arrive_time=info1["arrive_time"],
                duration=info1["duration"],
                transfers=info1["transfers"],
                flight_numbers=info1["flight_numbers"],
                aircraft=info1["aircraft"],
                layovers=info1["layovers"],
                depart_at=info1["depart_at"],
                arrive_at=info1["arrive_at"],
            )

            offer = Offer(
                site="onetwotrip",
                price=price_int,
                currency=currency,
                airline=info0["airline"],
                depart_time=info0["depart_time"],
                arrive_time=info0["arrive_time"],
                duration=info0["duration"],
                transfers=info0["transfers"],
                baggage=baggage,
                url=None,
                flight_numbers=info0["flight_numbers"],
                aircraft=info0["aircraft"],
                layovers=info0["layovers"],
                depart_at=info0["depart_at"],
                arrive_at=info0["arrive_at"],
                carry_on=carry_on,
                back=back_leg,
            )
            offers.append(offer)

        offers.sort(key=lambda o: o.price)
        return offers

    else:
        # В одну сторону (OW)
        offers = []
        for price_obj in prices.values():
            if not isinstance(price_obj, dict):
                continue
            vids = price_obj.get("transportationVariantIds")
            if not isinstance(vids, list) or len(vids) != 1:
                continue
            v0 = variants.get(vids[0])
            if not isinstance(v0, dict) or v0.get("directionIndex") != 0:
                continue
            amt = price_obj.get("totalAmount")
            if amt is None:
                continue
            try:
                cost = math.ceil(round(float(amt), 2))
            except (ValueError, TypeError):
                continue
            info0 = _parse_variant(v0, trips, equipment, cities, airports)
            if info0 is None:
                continue

            t0_start = info0.get("depart_at")
            if not t0_start or t0_start[:10] != depart_iso:
                continue

            baggage = "с багажом" if _has_baggage([v0], services) else "без багажа"
            carry_on = _has_cabin_baggage([v0], services)

            offer = Offer(
                site="onetwotrip",
                price=cost,
                currency=currency,
                airline=info0["airline"],
                depart_time=info0["depart_time"],
                arrive_time=info0["arrive_time"],
                duration=info0["duration"],
                transfers=info0["transfers"],
                baggage=baggage,
                url=None,
                flight_numbers=info0["flight_numbers"],
                aircraft=info0["aircraft"],
                layovers=info0["layovers"],
                depart_at=info0["depart_at"],
                arrive_at=info0["arrive_at"],
                carry_on=carry_on,
                back=None,
            )
            offers.append(offer)

        offers.sort(key=lambda o: o.price)
        return offers


parse_onetwotrip.results_complete = results_complete  # type: ignore[attr-defined]
