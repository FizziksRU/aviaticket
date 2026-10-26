from __future__ import annotations

from .config import SiteConfig
from .models import Query


def build_url(site: SiteConfig, q: Query, extra: dict[str, str] | None = None) -> str:
    params: dict[str, str | int] = {
        "origin": q.origin,
        "destination": q.destination,
        "date_iso": q.depart.isoformat(),
        "ddmm": q.depart.strftime("%d%m"),
        "ddmmyyyy": q.depart.strftime("%d%m%Y"),
        "adults": q.adults,
    }
    if extra:
        params.update(extra)
    return site.url_template.format(**params)
