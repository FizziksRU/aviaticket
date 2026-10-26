from __future__ import annotations

from .config import SiteConfig
from .models import Query


def build_url(site: SiteConfig, q: Query) -> str:
    return site.url_template.format(
        origin=q.origin,
        destination=q.destination,
        date_iso=q.depart.isoformat(),
        ddmm=q.depart.strftime("%d%m"),
        ddmmyyyy=q.depart.strftime("%d%m%Y"),
        adults=q.adults,
    )
