from __future__ import annotations

from collections.abc import Sequence
import dataclasses
from datetime import datetime
import re

from .criteria import SearchCriteria
from .models import Offer, SiteResult

_DURATION_REGEX = re.compile(
    r"^(?:(?P<days>\d+)\s*(?:д(?:ней|ня|ень|н)?|d(?:ays?)?)\.?)?\s*"
    r"(?:(?P<hours>\d+)\s*(?:ч(?:асов|аса|ас)?|h(?:ours?|r)?)\.?)?\s*"
    r"(?:(?P<minutes>\d+)\s*(?:м(?:инут[ыа]?|ин)?|m(?:in(?:utes?)?)?)\.?)?$",
    re.IGNORECASE,
)


def parse_duration_minutes(text: str | None) -> int | None:
    if not isinstance(text, str):
        return None
    cleaned = text.replace("\u00a0", " ").strip()
    if not cleaned:
        return None
    m = _DURATION_REGEX.match(cleaned)
    if not m:
        return None
    days_s = m.group("days")
    hours_s = m.group("hours")
    minutes_s = m.group("minutes")
    if days_s is None and hours_s is None and minutes_s is None:
        return None
    days = int(days_s) if days_s else 0
    hours = int(hours_s) if hours_s else 0
    minutes = int(minutes_s) if minutes_s else 0
    total = days * 1440 + hours * 60 + minutes
    return total if total >= 0 else None


def _extract_time(time_str: str | None, iso_str: str | None) -> tuple[int, int] | None:
    if isinstance(time_str, str):
        s = time_str.strip()
        parts = s.split(":")
        if len(parts) >= 2:
            try:
                h, m = int(parts[0]), int(parts[1])
                if 0 <= h <= 23 and 0 <= m <= 59:
                    return h, m
            except (ValueError, TypeError):
                pass
    if isinstance(iso_str, str):
        s = iso_str.strip()
        if "T" not in s and " " not in s and ":" not in s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.hour, dt.minute
        except (ValueError, TypeError):
            m = re.search(r"(?:^|[T\s])(\d{1,2}):(\d{2})", s)
            if m:
                try:
                    h, minute = int(m.group(1)), int(m.group(2))
                    if 0 <= h <= 23 and 0 <= minute <= 59:
                        return h, minute
                except (ValueError, TypeError):
                    pass
    return None


def _get_part_of_day(hour: int, minute: int) -> str | None:
    if 0 <= hour <= 5:
        return "night"
    elif 6 <= hour <= 11:
        return "morning"
    elif 12 <= hour <= 17:
        return "day"
    elif 18 <= hour <= 23:
        return "evening"
    return None


def total_duration_minutes(offer: Offer) -> int | None:
    dur1 = parse_duration_minutes(offer.duration)
    if offer.back is None:
        return dur1
    dur2 = parse_duration_minutes(offer.back.duration)
    if dur1 is not None and dur2 is not None:
        return dur1 + dur2
    return None


def matches(offer: Offer, criteria: SearchCriteria) -> bool:
    if criteria.is_default():
        return True

    # 1. max_transfers (applied to both legs if back is present)
    if criteria.max_transfers is not None:
        if offer.transfers is not None and offer.transfers > criteria.max_transfers:
            return False
        if offer.back is not None:
            if offer.back.transfers is not None and offer.back.transfers > criteria.max_transfers:
                return False

    # 2. max_duration_hours (each leg checked separately)
    if criteria.max_duration_hours is not None:
        max_mins = criteria.max_duration_hours * 60
        out_dur = parse_duration_minutes(offer.duration)
        if out_dur is not None and out_dur > max_mins:
            return False
        if offer.back is not None:
            back_dur = parse_duration_minutes(offer.back.duration)
            if back_dur is not None and back_dur > max_mins:
                return False

    # 3. depart_parts (only outbound leg)
    if criteria.depart_parts:
        t = _extract_time(offer.depart_time, offer.depart_at)
        if t is not None:
            part = _get_part_of_day(t[0], t[1])
            if part is not None and part not in criteria.depart_parts:
                return False

    # 4. arrive_parts (only outbound leg)
    if criteria.arrive_parts:
        t = _extract_time(offer.arrive_time, offer.arrive_at)
        if t is not None:
            part = _get_part_of_day(t[0], t[1])
            if part is not None and part not in criteria.arrive_parts:
                return False

    return True


def apply_criteria(
    results: Sequence[SiteResult], criteria: SearchCriteria
) -> tuple[list[SiteResult], int]:
    if criteria.is_default():
        return list(results), 0

    new_results: list[SiteResult] = []
    dropped_count = 0

    for r in results:
        kept_offers = [o for o in r.offers if matches(o, criteria)]
        dropped_count += len(r.offers) - len(kept_offers)
        new_results.append(dataclasses.replace(r, offers=kept_offers))

    return new_results, dropped_count


def pareto_front(offers: Sequence[Offer]) -> list[Offer]:
    durations = {id(o): total_duration_minutes(o) for o in offers}
    kept: list[Offer] = []

    for o in offers:
        dur_o = durations[id(o)]
        if dur_o is None:
            # Билеты без известного времени не отбрасывать и не использовать для доминирования
            kept.append(o)
            continue

        dominated = False
        for other in offers:
            if other is o:
                continue
            dur_other = durations[id(other)]
            if dur_other is None:
                continue

            if (other.price <= o.price and dur_other <= dur_o) and (
                other.price < o.price or dur_other < dur_o
            ):
                dominated = True
                break

        if not dominated:
            kept.append(o)

    kept.sort(key=lambda o: o.price)
    return kept


def balance_score(offer: Offer, hour_cost: int | float) -> float:
    tot_dur = total_duration_minutes(offer)
    dur_cost = (tot_dur / 60.0 * hour_cost) if tot_dur is not None else 0.0

    transfers = 0
    if offer.transfers is not None:
        transfers += offer.transfers
    if offer.back is not None and offer.back.transfers is not None:
        transfers += offer.back.transfers
    transfers_cost = 500.0 * transfers

    return float(offer.price + dur_cost + transfers_cost)


def _is_same_offer(a: Offer, b: Offer) -> bool:
    if a is b:
        return True
    if a.offer_id and b.offer_id and a.offer_id == b.offer_id:
        return True
    return a == b


def pick_cards(
    offers: Sequence[Offer], criteria: SearchCriteria
) -> list[tuple[str, Offer]]:
    if not offers:
        return []

    cards: list[tuple[str, Offer]] = []
    chosen: list[Offer] = []

    # 1. («Самый дешёвый», min цена)
    cheapest = min(offers, key=lambda o: o.price)
    cards.append(("Самый дешёвый", cheapest))
    chosen.append(cheapest)

    # 2. («Самый быстрый», min время, только если это другой билет и время известно)
    offers_with_time = [o for o in offers if total_duration_minutes(o) is not None]
    if offers_with_time:
        fastest = min(offers_with_time, key=lambda o: (total_duration_minutes(o), o.price))
        if not any(_is_same_offer(fastest, x) for x in chosen):
            cards.append(("Самый быстрый", fastest))
            chosen.append(fastest)

    # 3. («Лучший баланс», min balance_score, только если criteria.hour_cost задан и билет отличается от уже выбранных)
    if criteria.hour_cost is not None:
        best_balance = min(
            offers, key=lambda o: (balance_score(o, criteria.hour_cost), o.price)
        )
        if not any(_is_same_offer(best_balance, x) for x in chosen):
            cards.append(("Лучший баланс", best_balance))
            chosen.append(best_balance)

    return cards
