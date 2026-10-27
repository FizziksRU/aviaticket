from __future__ import annotations

import dataclasses
import hashlib
import time
from typing import Sequence

from .models import Leg, Offer, SiteResult


def _leg_parts(leg: Offer | Leg) -> list[str]:
    fn = ",".join(leg.flight_numbers) if leg.flight_numbers else ""
    dep = leg.depart_at or leg.depart_time or ""
    arr = leg.arrive_at or leg.arrive_time or ""
    dur = leg.duration or ""
    transfers = "" if leg.transfers is None else str(leg.transfers)
    airline = leg.airline or ""
    return [airline, fn, dep, arr, dur, transfers]


def compute_offer_id(o: Offer) -> str:
    """Вычисляет стабильный 12-значный sha1-хэш идентификатора билета без цены и ссылки."""
    parts = [o.site, *_leg_parts(o)]
    if o.back is not None:
        parts.extend(_leg_parts(o.back))
    payload = "|".join(parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def baggage_state(o: Offer) -> bool | None:
    """Определяет наличие багажа: True (с багажом), False (без багажа), None (не указано)."""
    if not o.baggage:
        return None
    val = o.baggage.lower()
    if "без багажа" in val:
        return False
    if "с багажом" in val:
        return True
    return None


def filter_by_baggage(results: Sequence[SiteResult], mode: str) -> tuple[list[SiteResult], int]:
    """Фильтрует результаты по наличию багажа, не мутируя исходные объекты."""
    if mode not in {"any", "with", "without"}:
        raise ValueError(f"Неизвестный режим фильтрации багажа: {mode}")

    new_results: list[SiteResult] = []
    dropped_count = 0

    for r in results:
        if mode == "any":
            kept_offers = list(r.offers)
        elif mode == "with":
            kept_offers = [o for o in r.offers if baggage_state(o) is not False]
            dropped_count += len(r.offers) - len(kept_offers)
        elif mode == "without":
            kept_offers = [o for o in r.offers if baggage_state(o) is not True]
            dropped_count += len(r.offers) - len(kept_offers)

        new_results.append(dataclasses.replace(r, offers=kept_offers))

    return new_results, dropped_count


def assign_ids(results: Sequence[SiteResult], now: float | None = None) -> Sequence[SiteResult]:
    """Проставляет offer_id и fetched_at там, где они не заполнены."""
    ts = time.time() if now is None else now
    for r in results:
        for o in r.offers:
            if not o.offer_id:
                o.offer_id = compute_offer_id(o)
            if o.fetched_at is None:
                o.fetched_at = ts
    return results
