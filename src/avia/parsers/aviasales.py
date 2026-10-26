"""Парсер живой выдачи Aviasales по XHR-ответам API search/v3.2/results."""
from __future__ import annotations

import json
import re
from typing import Any

from ..collector import Snapshot
from ..models import Offer, Query
from . import register_parser
from .travelpayouts import format_duration


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Безопасно преобразует значение в float (исключая bool)."""
    if val is None or isinstance(val, bool):
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _extract_time(dt_str: str | None) -> str | None:
    """Извлекает время вида HH:MM из строки даты/времени (например, 'YYYY-MM-DD HH:MM')."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    if " " in dt_str:
        parts = dt_str.split(" ", 1)
        if len(parts) > 1 and len(parts[1]) >= 5:
            return parts[1][:5]
    if "T" in dt_str:
        parts = dt_str.split("T", 1)
        if len(parts) > 1 and len(parts[1]) >= 5:
            return parts[1][:5]
    return dt_str[:5] if len(dt_str) >= 5 else None


def _get_airline_name(carrier_code: str | None, airlines: dict[str, Any]) -> str | None:
    """Возвращает русское название авиакомпании по коду перевозчика."""
    if not carrier_code or not isinstance(carrier_code, str):
        return None
    airline_info = airlines.get(carrier_code)
    if isinstance(airline_info, dict):
        name_info = airline_info.get("name")
        if isinstance(name_info, dict):
            ru_info = name_info.get("ru")
            if isinstance(ru_info, dict):
                default_name = ru_info.get("default")
                if default_name:
                    return str(default_name)
            # Запасной вариант: берем любое имеющееся значение
            for val in name_info.values():
                if isinstance(val, dict) and "default" in val:
                    return str(val["default"])
                if isinstance(val, str):
                    return val
        elif isinstance(name_info, str):
            return name_info
    return carrier_code


def results_complete(snapshot: Snapshot | None) -> bool:
    """Проверяет, завершился ли опрос результатов поиска Aviasales.

    Выдача догружается периодическим опросом (polling). Официального или
    гарантированного флага завершения в структуре данных Aviasales нет,
    поэтому признак является эмпирическим:
    - возвращается True, если среди ответов /search/v3.2/results присутствует
      чанк с chunk_id == 'results' и last_update_timestamp == 0 (число 0;
      эмпирический признак финального ответа, подтверждён на двух запусках);
    - либо проверяется наличие старых флагов завершения is_over=True, completed,
      is_complete, finished в чанках или их метаданных.
    Иначе возвращается False.
    """
    if not snapshot or not snapshot.json_payloads:
        return False

    for url, body in snapshot.json_payloads:
        if "/search/v3.2/results" not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            # На случай битого JSON проверяем текстовое вхождение
            if re.search(r'"is_over"\s*:\s*true', body, re.IGNORECASE):
                return True
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue

            # Эмпирический признак финального ответа: chunk_id == 'results' и last_update_timestamp == 0 (число 0)
            if item.get("chunk_id") == "results":
                ts = item.get("last_update_timestamp")
                if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts == 0:
                    return True

            for key in ("is_over", "completed", "is_complete", "finished"):
                val = item.get(key)
                if val is True or val == "true" or val == 1:
                    return True
                meta = item.get("meta")
                if isinstance(meta, dict):
                    mval = meta.get(key)
                    if mval is True or mval == "true" or mval == 1:
                        return True
    return False


@register_parser("aviasales")
def parse_aviasales(snapshot: Snapshot, query: Query) -> list[Offer]:
    """Разбирает XHR-ответы Aviasales (/search/v3.2/results) из snapshot в список Offer."""
    if not snapshot or not snapshot.json_payloads:
        return []

    # Словарь дедупликации: ticket_key -> (priority, Offer)
    # Приоритет определяется порядком прихода ответа в snapshot.json_payloads,
    # а запасным признаком служит last_update_timestamp (ts == 0 означает финальный).
    tickets_by_key: dict[str, tuple[tuple[int, float], Offer]] = {}
    id_to_ticket_key: dict[str, str] = {}
    unkeyed_offers: list[Offer] = []

    for payload_idx, (url, body) in enumerate(snapshot.json_payloads):
        if "/search/v3.2/results" not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue

        chunks = data if isinstance(data, list) else [data]
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            # Интересуют чанки с результатами
            if (
                chunk.get("chunk_id") != "results"
                and "tickets" not in chunk
                and "cheapest_ticket" not in chunk
                and "filtered_cheapest_ticket" not in chunk
            ):
                continue

            raw_ts = chunk.get("last_update_timestamp")
            if isinstance(raw_ts, bool) or raw_ts is None:
                effective_ts = -1.0
            else:
                try:
                    val = float(raw_ts)
                    effective_ts = float("inf") if val == 0 else val
                except (TypeError, ValueError):
                    effective_ts = -1.0

            current_priority = (payload_idx, effective_ts)

            flight_legs = chunk.get("flight_legs") or []
            airlines = chunk.get("airlines") or {}
            raw_tickets = chunk.get("tickets") or []
            if not isinstance(flight_legs, list):
                continue

            # Добавляем обычные билеты, а также cheapest_ticket и filtered_cheapest_ticket
            all_raw_tickets: list[dict[str, Any]] = []
            if isinstance(raw_tickets, list):
                for t in raw_tickets:
                    if isinstance(t, dict):
                        all_raw_tickets.append(t)
            for key in ("cheapest_ticket", "filtered_cheapest_ticket"):
                val = chunk.get(key)
                if isinstance(val, dict):
                    all_raw_tickets.append(val)

            for t in all_raw_tickets:
                if not isinstance(t, dict):
                    continue
                try:
                    # 1. Поиск самого дешевого предложения (proposal)
                    proposals = t.get("proposals")
                    if not isinstance(proposals, list) or not proposals:
                        continue

                    cheapest_prop = None
                    best_price = None
                    for p in proposals:
                        if not isinstance(p, dict):
                            continue
                        p_price = p.get("price")
                        if not isinstance(p_price, dict):
                            continue
                        raw_val = p_price.get("value")
                        if isinstance(raw_val, bool):
                            continue
                        try:
                            val_int = int(round(float(raw_val)))
                        except (TypeError, ValueError):
                            continue
                        if val_int <= 0:
                            continue
                        if best_price is None or val_int < best_price:
                            best_price = val_int
                            cheapest_prop = p

                    if cheapest_prop is None or best_price is None:
                        continue

                    currency = str(cheapest_prop.get("price", {}).get("currency_code") or query.currency or "RUB").upper()

                    # 2. Сегменты и перелёты
                    segments = t.get("segments")
                    if not isinstance(segments, list) or not segments:
                        continue
                    seg0 = segments[0]
                    if not isinstance(seg0, dict):
                        continue
                    flight_indices = seg0.get("flights")
                    if not isinstance(flight_indices, list) or not flight_indices:
                        continue

                    # Проверяем корректность индексов в flight_legs
                    valid_legs: list[tuple[int, dict[str, Any]]] = []
                    valid = True
                    for idx in flight_indices:
                        if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0 or idx >= len(flight_legs):
                            valid = False
                            break
                        leg = flight_legs[idx]
                        if not isinstance(leg, dict):
                            valid = False
                            break
                        valid_legs.append((idx, leg))

                    if not valid or not valid_legs:
                        continue

                    # Сортируем перелёты по departure_unix_timestamp
                    sorted_legs = sorted(
                        valid_legs,
                        key=lambda item: _safe_float(item[1].get("departure_unix_timestamp")),
                    )

                    first_f_idx, first_leg = sorted_legs[0]
                    last_f_idx, last_leg = sorted_legs[-1]

                    # 3. Пересадки
                    transfers = len(sorted_legs) - 1

                    # 4. Время вылета и прилёта
                    depart_time = _extract_time(first_leg.get("local_departure_date_time"))
                    arrive_time = _extract_time(last_leg.get("local_arrival_date_time"))

                    # 5. Длительность
                    duration = None
                    first_dep_ts = _safe_float(first_leg.get("departure_unix_timestamp"), -1.0)
                    last_arr_ts = _safe_float(last_leg.get("arrival_unix_timestamp"), -1.0)
                    if first_dep_ts > 0 and last_arr_ts > first_dep_ts:
                        diff_sec = last_arr_ts - first_dep_ts
                        duration = format_duration(int(diff_sec // 60))

                    # 6. Авиакомпания
                    carrier_code = None
                    carr_des = first_leg.get("operating_carrier_designator")
                    if isinstance(carr_des, dict):
                        carrier_code = carr_des.get("carrier")
                    if not carrier_code:
                        for _, leg in sorted_legs:
                            cd = leg.get("operating_carrier_designator")
                            if isinstance(cd, dict) and cd.get("carrier"):
                                carrier_code = cd.get("carrier")
                                break
                    airline = _get_airline_name(carrier_code, airlines)

                    # 7. Багаж
                    # flight_terms содержит условия для каждого индекса перелёта
                    flight_terms = cheapest_prop.get("flight_terms") or {}
                    b_counts: list[int] = []
                    if isinstance(flight_terms, dict):
                        for f_idx, _ in sorted_legs:
                            term = flight_terms.get(str(f_idx))
                            if term is None:
                                term = flight_terms.get(f_idx)
                            if isinstance(term, dict):
                                raw_b = term.get("total_baggage_count", 0)
                                if isinstance(raw_b, bool):
                                    b_counts.append(0)
                                else:
                                    try:
                                        b_counts.append(int(raw_b or 0))
                                    except (TypeError, ValueError):
                                        b_counts.append(0)
                            else:
                                b_counts.append(0)

                    has_baggage = bool(b_counts) and all(c > 0 for c in b_counts)
                    baggage = "с багажом" if has_baggage else "без багажа"

                    offer = Offer(
                        site="aviasales",
                        price=best_price,
                        currency=currency,
                        airline=airline,
                        depart_time=depart_time,
                        arrive_time=arrive_time,
                        duration=duration,
                        transfers=transfers,
                        baggage=baggage,
                        url=snapshot.url or None,
                    )

                    # Дедупликация по signature (или id без суффиксов _cheapest) + первому вылету
                    raw_id = t.get("id")
                    clean_id = re.sub(r"_(?:filtered_)?cheapest$", "", str(raw_id)) if raw_id else None
                    sig = t.get("signature")
                    sig_key = str(sig) if sig else clean_id

                    first_dep = first_dep_ts if first_dep_ts > 0 else (depart_time or "")
                    ticket_key = f"{sig_key}_{first_dep}" if sig_key else None

                    # Если у билета нет signature, но ранее встречался билет с таким же clean_id и вылетом,
                    # используем уже известный канонический ticket_key (и переносим его при появлении signature).
                    id_key = f"{clean_id}_{first_dep}" if clean_id else None
                    if ticket_key and id_key:
                        if sig and id_key in id_to_ticket_key:
                            prev_canonical = id_to_ticket_key[id_key]
                            if prev_canonical != ticket_key and prev_canonical in tickets_by_key:
                                tickets_by_key[ticket_key] = tickets_by_key.pop(prev_canonical)
                            id_to_ticket_key[id_key] = ticket_key
                        elif not sig and id_key in id_to_ticket_key:
                            ticket_key = id_to_ticket_key[id_key]
                        else:
                            id_to_ticket_key[id_key] = ticket_key

                    if ticket_key:
                        prev = tickets_by_key.get(ticket_key)
                        if (
                            prev is None
                            or current_priority > prev[0]
                            or (current_priority == prev[0] and offer.price <= prev[1].price)
                        ):
                            tickets_by_key[ticket_key] = (current_priority, offer)
                    else:
                        unkeyed_offers.append(offer)

                except Exception:
                    # Битые билеты пропускаем, не падая
                    continue

    all_offers = [offer for _, offer in tickets_by_key.values()] + unkeyed_offers
    all_offers.sort(key=lambda o: o.price)
    return all_offers


# Привязываем results_complete к функции парсера для удобного вызова из pipeline
parse_aviasales.results_complete = results_complete  # type: ignore[attr-defined]
