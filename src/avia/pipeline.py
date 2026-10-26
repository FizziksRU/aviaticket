from __future__ import annotations

import asyncio
import time

from .collector import fetch_snapshot
from .config import Settings, SiteConfig
from .extract import extract_offers
from .models import Offer, Query, SiteResult
from .parsers import PARSERS
from .parsers.travelpayouts import fetch_travelpayouts, search_url
from .sites import build_url


async def _from_api(site: SiteConfig, q: Query, settings: Settings) -> tuple[list[Offer], str | None]:
    if site.parser == "travelpayouts":
        offers = await fetch_travelpayouts(q, settings.tp_token, settings.tp_marker)
        return offers, search_url(q, settings.tp_marker)
    raise RuntimeError(f"неизвестный api-источник: {site.parser!r}")


async def _from_browser(site: SiteConfig, q: Query, settings: Settings) -> tuple[list[Offer], str | None, bool]:
    url = build_url(site, q)
    snap = await fetch_snapshot(
        url,
        site,
        headless=settings.headless,
        storage_state=settings.storage_dir / f"{site.name}.json",
        debug_dir=settings.debug_dir,
    )
    if snap.blocked:
        raise RuntimeError("сайт показал капчу или блокировку")

    offers: list[Offer] = []
    partial = False
    parser = PARSERS.get(site.parser)
    if site.parser and parser is None:
        raise RuntimeError(f"парсер {site.parser!r} не зарегистрирован")
    if parser:
        offers = parser(snap, q)
        for o in offers:
            o.site = site.name
        checker = getattr(parser, "results_complete", None)
        if checker is None and hasattr(parser, "__module__"):
            import sys
            mod = sys.modules.get(parser.__module__)
            if mod:
                checker = getattr(mod, "results_complete", None)
        if callable(checker):
            try:
                if not checker(snap):
                    partial = True
            except Exception:
                pass
    if not offers and site.use_llm:
        offers = await extract_offers(snap, q, site.name, settings)
    if not offers and not parser and not site.use_llm:
        raise RuntimeError("для сайта нет парсера: запустите `python -m avia.recon` и пришлите debug/recon")
    for o in offers:
        o.url = o.url or snap.url
    return sorted(offers, key=lambda o: o.price), snap.url, partial


async def _run_site(site: SiteConfig, q: Query, settings: Settings) -> SiteResult:
    t0 = time.monotonic()
    try:
        if site.source == "api":
            offers, page_url = await _from_api(site, q, settings)
            partial = False
        else:
            offers, page_url, partial = await _from_browser(site, q, settings)
        return SiteResult(site.name, True, offers, None, page_url, time.monotonic() - t0, partial=partial)
    except Exception as e:  # один сайт не должен ронять весь поиск
        return SiteResult(site.name, False, [], f"{type(e).__name__}: {e}", None, time.monotonic() - t0, partial=False)


async def search(q: Query, settings: Settings) -> list[SiteResult]:
    sites = [s for s in settings.sites if s.enabled]
    # браузеры тяжёлые: не больше 2 одновременно (api-источники это не касается, но их мало)
    sem = asyncio.Semaphore(2)

    async def guarded(s: SiteConfig) -> SiteResult:
        async with sem:
            return await _run_site(s, q, settings)

    return await asyncio.gather(*(guarded(s) for s in sites))
