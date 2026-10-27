"""Работа с предложениями перелётов: вычисление идентификаторов и фильтрация."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import time

from .models import Leg, Offer, SiteResult


def _leg_parts(leg: Leg | Offer) -> list[str]:
    fn = ",".join(leg.flight_numbers) if leg.flight_numbers else ""
    dep = leg.depart_at or leg.depart_time or ""
    arr = leg.arrive_at or leg.arrive_time or ""
    dur = leg.duration or ""
    tr = str(leg.transfers) if leg.transfers is not None else ""
    return [leg.airline or "", fn, dep, arr, dur, tr]


def compute_offer_id(o: Offer) -> str:
    """Вычисляет стабильный 12-символьный sha1 хэш предложения.

    В хэш входят: site, airline, flight_numbers, depart_at/depart_time,
    arrive_at/arrive_time, duration, transfers и аналогичные поля o.back.
    Цена и ссылка не входят.
    """
    parts = [o.site] + _leg_parts(o)
    if o.back is not None:
        parts.append("back")
        parts.extend(_leg_parts(o.back))
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def baggage_state(o: Offer) -> bool | None:
    """Определяет наличие багажа по строке o.baggage.

    - «без багажа» -> False
    - «с багажом» -> True
    - пусто или иное -> None
    """
    if not o.baggage:
        return None
    s = o.baggage.strip().lower()
    if "без багажа" in s:
        return False
    if "с багажом" in s:
        return True
    return None


def filter_by_baggage(results: list[SiteResult], mode: str) -> tuple[list[SiteResult], int]:
    """Фильтрует варианты по наличию багажа.

    mode:
      - 'any': ничего не фильтрует
      - 'with': оставляет True и None
      - 'without': оставляет False и None

    Возвращает новые SiteResult (не мутируя исходные) и число скрытых вариантов.
    """
    if mode not in {"any", "with", "without"}:
        raise ValueError(f"Неизвестный режим фильтрации багажа: {mode}")

    if mode == "any":
        return [replace(r, offers=list(r.offers)) for r in results], 0

    new_results: list[SiteResult] = []
    dropped_count = 0

    for r in results:
        kept: list[Offer] = []
        for o in r.offers:
            state = baggage_state(o)
            if mode == "with":
                if state is True or state is None:
                    kept.append(o)
                else:
                    dropped_count += 1
            elif mode == "without":
                if state is False or state is None:
                    kept.append(o)
                else:
                    dropped_count += 1
        new_results.append(replace(r, offers=kept))

    return new_results, dropped_count


def assign_ids(results: list[SiteResult]) -> None:
    """Проставляет offer_id и fetched_at (time.time()) там, где они ещё не заполнены."""
    now = time.time()
    for r in results:
        for o in r.offers:
            if not o.offer_id:
                o.offer_id = compute_offer_id(o)
            if o.fetched_at is None:
                o.fetched_at = now
