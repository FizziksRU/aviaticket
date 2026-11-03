"""Парсер выдачи Озон Тревел (ozon.ru/travel) по ответам виджета aviaSearchResult."""
from __future__ import annotations

from datetime import timedelta
import json
import logging
import re
from typing import Any
import urllib.parse

from ..collector import Snapshot
from ..models import Offer, Query
from ..offers import compute_offer_id
from . import register_parser
from .travelpayouts import format_duration

logger = logging.getLogger(__name__)

SUPERSCRIPT_TRANS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")
WIDGET_MARKER = "widgetStateId=aviaSearchResult"
OZON_BASE_URL = "https://www.ozon.ru"


def _parse_duration(text: str | None) -> str | None:
    """Разбирает текстовую длительность вида 'в пути 20ч 25м', 'в пути 1д 1ч 55м' в формат '20ч 25м'."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").strip()
    days_m = re.search(r"(\d+)\s*д", cleaned)
    hours_m = re.search(r"(\d+)\s*ч", cleaned)
    mins_m = re.search(r"(\d+)\s*м", cleaned)
    if not hours_m and not mins_m and not days_m:
        return None
    days = int(days_m.group(1)) if days_m else 0
    hours = int(hours_m.group(1)) if hours_m else 0
    mins = int(mins_m.group(1)) if mins_m else 0
    total_minutes = days * 24 * 60 + hours * 60 + mins
    return format_duration(total_minutes)


def _parse_transfers_and_layovers(transfers_list: list[Any] | None) -> tuple[int, list[str] | None]:
    """Извлекает количество пересадок и список городов пересадок с длительностью."""
    if not transfers_list or not isinstance(transfers_list, list):
        return 0, None

    first_item = transfers_list[0]
    first_text = ""
    if isinstance(first_item, dict):
        city_dict = first_item.get("city")
        if isinstance(city_dict, dict):
            first_text = city_dict.get("text", "")

    first_lower = first_text.lower().strip()
    if "прям" in first_lower:
        transfers_count = 0
    else:
        m = re.search(r"(\d+)", first_lower)
        if m:
            transfers_count = int(m.group(1))
        else:
            transfers_count = max(0, len(transfers_list) - 1)

    layovers: list[str] = []
    for item in transfers_list[1:]:
        if isinstance(item, dict):
            city_dict = item.get("city")
            if isinstance(city_dict, dict):
                city_str = city_dict.get("text", "").strip()
                if city_str:
                    layovers.append(city_str)

    if transfers_count == 0:
        return 0, None
    return transfers_count, (layovers if layovers else None)


def _parse_baggage(luggage_obj: Any) -> str | None:
    """Определяет, включён ли багаж в тариф.

    В Ozon Travel доплата за багаж отображается как 'Багаж 10 кг +6 181 ₽',
    что означает, что багаж НЕ включён в стоимость билета ('без багажа').
    """
    if not isinstance(luggage_obj, dict):
        return None
    text = luggage_obj.get("text")
    if not text or not isinstance(text, str):
        return None

    text_clean = text.replace("\xa0", " ").replace("\u2009", " ").strip()
    text_lower = text_clean.lower()

    if "+" in text_clean or "доплат" in text_lower:
        return "без багажа"
    if "ручная кладь" in text_lower and "багаж" not in text_lower:
        return "без багажа"
    if "без багажа" in text_lower:
        return "без багажа"
    if "с багажом" in text_lower or "багаж" in text_lower or "включен" in text_lower or "включён" in text_lower:
        return "с багажом"

    return None


def results_complete(snapshot: Snapshot | None) -> bool:
    """Проверяет полноту результатов поиска Озон Тревел по снимку сетевых ответов.

    Выдача считается завершённой (True), если среди ответов aviaSearchResult
    есть хотя бы один с непустым списком карточек flights.
    """
    if not snapshot or not snapshot.json_payloads or getattr(snapshot, "blocked", False):
        return False

    for url, body in snapshot.json_payloads:
        if WIDGET_MARKER not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        state = data.get("state")
        if not isinstance(state, dict):
            continue
        flights = state.get("flights")
        if isinstance(flights, list) and len(flights) > 0:
            return True

    return False


@register_parser("ozon_travel")
def parse_ozon_travel(snapshot: Snapshot, query: Query) -> list[Offer]:
    """Разбирает сетевые ответы виджета aviaSearchResult Озон Тревел в список Offer."""
    if not snapshot or not snapshot.json_payloads or getattr(snapshot, "blocked", False):
        return []

    # Для туда-обратно (query.return_date) поддержка будет добавлена позже
    if query is not None and query.return_date is not None:
        return []

    # Одни и те же виджет-ответы приходят несколько раз с обновлением;
    # выбираем ответ с наибольшим числом карточек (при равенстве более поздний).
    # Ответ с asyncErrorMessage без flights игнорируем.
    best_flights: list[dict[str, Any]] | None = None
    best_count = -1

    for url, body in snapshot.json_payloads:
        if WIDGET_MARKER not in url:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        state = data.get("state")
        if not isinstance(state, dict):
            continue

        flights_raw = state.get("flights")
        async_error = state.get("asyncErrorMessage")

        if async_error is not None and not flights_raw:
            continue

        if not isinstance(flights_raw, list):
            continue

        count = len(flights_raw)
        if count >= best_count:
            best_count = count
            best_flights = flights_raw

    if not best_flights:
        return []

    offers: list[Offer] = []
    for card in best_flights:
        if not isinstance(card, dict):
            continue
        try:
            price_container = card.get("price")
            if not isinstance(price_container, dict):
                continue
            price_list = price_container.get("price")
            if not isinstance(price_list, list) or not price_list:
                continue

            # Ищем цену со textStyle == "PRICE"
            p_text = None
            for p_item in price_list:
                if isinstance(p_item, dict) and p_item.get("textStyle") == "PRICE":
                    p_text = p_item.get("text")
                    break
            if not p_text:
                for p_item in price_list:
                    if isinstance(p_item, dict) and p_item.get("text"):
                        p_text = p_item.get("text")
                        break

            if not p_text:
                continue

            digits = "".join(re.findall(r"\d+", p_text))
            if not digits:
                continue
            price = int(digits)
            if price <= 0:
                continue

            airlines_container = card.get("airlinesText")
            airline = None
            if isinstance(airlines_container, dict):
                a_text = airlines_container.get("text")
                if a_text and isinstance(a_text, str):
                    airline = a_text.strip() or None

            way_there = card.get("wayThereDetails")
            if not isinstance(way_there, dict):
                way_there = {}

            trip_time_obj = way_there.get("tripTime")
            trip_time_text = trip_time_obj.get("text", "") if isinstance(trip_time_obj, dict) else ""
            dep_time = None
            arr_time = None
            if trip_time_text:
                parts = re.split(r"\s*[-–—]\s*", trip_time_text.strip())
                if len(parts) >= 2:
                    dep_time = parts[0].strip()
                    arr_time = parts[1].strip()
                elif len(parts) == 1 and ":" in parts[0]:
                    dep_time = parts[0].strip()

            days_offset = 0
            tdiff_obj = way_there.get("timeDifference")
            if isinstance(tdiff_obj, dict):
                raw_tdiff = tdiff_obj.get("text", "")
                if raw_tdiff:
                    norm_tdiff = raw_tdiff.translate(SUPERSCRIPT_TRANS)
                    m = re.search(r"\d+", norm_tdiff)
                    if m:
                        days_offset = int(m.group(0))

            dep_at = None
            arr_at = None
            if query is not None and query.depart is not None:
                if dep_time:
                    dep_at = f"{query.depart.isoformat()}T{dep_time}"
                if arr_time:
                    arr_date = query.depart + timedelta(days=days_offset)
                    arr_at = f"{arr_date.isoformat()}T{arr_time}"

            dur_obj = way_there.get("tripDuration")
            dur_text = dur_obj.get("text") if isinstance(dur_obj, dict) else None
            duration = _parse_duration(dur_text)

            transfers_list = way_there.get("transfers")
            transfers_count, layovers = _parse_transfers_and_layovers(transfers_list)

            luggage_obj = card.get("luggageText")
            baggage = _parse_baggage(luggage_obj)

            click_obj = card.get("cardClickAction")
            link = click_obj.get("link") if isinstance(click_obj, dict) else None
            offer_url = None
            flight_numbers = None
            if link and isinstance(link, str):
                link = link.strip()
                if link.startswith("http://") or link.startswith("https://"):
                    offer_url = link
                elif link.startswith("/"):
                    offer_url = f"{OZON_BASE_URL}{link}"
                else:
                    offer_url = f"{OZON_BASE_URL}/{link}"

                try:
                    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
                    fn_raw = qs.get("flightNumbers", [None])[0]
                    if fn_raw:
                        flight_numbers = [fn.strip() for fn in fn_raw.split(";") if fn.strip()] or None
                except Exception:
                    flight_numbers = None

            offer = Offer(
                site="ozon_travel",
                price=price,
                currency="RUB",
                airline=airline,
                depart_time=dep_time,
                arrive_time=arr_time,
                duration=duration,
                transfers=transfers_count,
                baggage=baggage,
                url=offer_url,
                flight_numbers=flight_numbers,
                layovers=layovers,
                depart_at=dep_at,
                arrive_at=arr_at,
            )
            offer.offer_id = compute_offer_id(offer)
            offers.append(offer)
        except Exception:
            continue

    # Дедупликация: при совпадении offer_id сохраняем предложение с минимальной ценой
    offers_by_id: dict[str, Offer] = {}
    for o in offers:
        if o.offer_id in offers_by_id:
            if o.price < offers_by_id[o.offer_id].price:
                offers_by_id[o.offer_id] = o
        else:
            offers_by_id[o.offer_id] = o

    result_offers = list(offers_by_id.values())
    result_offers.sort(key=lambda o: o.price)
    return result_offers


parse_ozon_travel.results_complete = results_complete  # type: ignore[attr-defined]
