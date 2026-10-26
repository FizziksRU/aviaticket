from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Query:
    origin: str
    destination: str
    depart: date
    adults: int = 1
    currency: str = "RUB"


@dataclass
class Offer:
    site: str
    price: int
    currency: str = "RUB"
    airline: str | None = None
    depart_time: str | None = None
    arrive_time: str | None = None
    duration: str | None = None
    transfers: int | None = None
    baggage: str | None = None
    url: str | None = None


@dataclass
class SiteResult:
    site: str
    ok: bool
    offers: list[Offer]
    error: str | None = None
    page_url: str | None = None
    elapsed: float = 0.0
    partial: bool = False
    cached: bool = False
