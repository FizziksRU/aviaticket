from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
from pathlib import Path
import re
import time

from .collector import fetch_snapshot, Snapshot
from .config import Settings, SiteConfig
from .extract import extract_offers
from .guard import SearchGuard, friendly_error
from .models import Offer, Query, SiteResult
from .parsers import PARSERS
from .parsers.travelpayouts import fetch_travelpayouts, search_url
from .sites import build_url
from .tutu_ids import resolve_city
from .yandex_ids import city_id

log = logging.getLogger("avia.pipeline")

# Общий SearchGuard на процесс (модульный объект для тестов с подменой)
guard = SearchGuard()


class ParserEmptyError(Exception):
    """Исключение при 0 вариантов от парсера на непустой странице."""

    def __init__(self, message: str, page_url: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.page_url = page_url


def _save_empty_snapshot(site: SiteConfig, snap: Snapshot, settings: Settings) -> None:
    """Сохраняет отладочный снимок при 0 вариантов и непустой странице (без токенов)."""
    try:
        debug_dir = Path(settings.debug_dir)
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = debug_dir / f"{site.name}_empty_{ts}.json"

        data = {
            "url": snap.url,
            "text": snap.text,
            "json_payloads": snap.json_payloads,
        }
        content = json.dumps(data, ensure_ascii=False, indent=2)
        if settings.tp_token:
            content = content.replace(settings.tp_token, "[REDACTED]")
        if settings.tg_token:
            content = content.replace(settings.tg_token, "[REDACTED]")
        content = re.sub(
            r'([?&](?:token|access_token|api_key|key|secret)=)[^&"\s]+',
            r'\1[REDACTED]',
            content,
            flags=re.I,
        )
        target.write_text(content, encoding="utf-8")
    except Exception as e:
        log.warning("Не удалось сохранить отладочный снимок для %s: %s", site.name, e)


async def _from_api(site: SiteConfig, q: Query, settings: Settings) -> tuple[list[Offer], str | None]:
    if site.parser == "travelpayouts":
        offers = await fetch_travelpayouts(q, settings.tp_token, settings.tp_marker)
        return offers, search_url(q, settings.tp_marker)
    raise RuntimeError(f"неизвестный api-источник: {site.parser!r}")


async def _from_browser(site: SiteConfig, q: Query, settings: Settings) -> tuple[list[Offer], str | None, bool]:
    extra: dict[str, str] | None = None
    if site.id_resolver == "tutu":
        origin_id, origin_seo = await resolve_city(q.origin)
        dest_id, dest_seo = await resolve_city(q.destination)
        extra = {
            "origin_id": origin_id,
            "destination_id": dest_id,
            "origin_seo": origin_seo,
            "destination_seo": dest_seo,
        }
    elif site.id_resolver == "yandex":
        extra = {
            "origin_id": city_id(q.origin),
            "destination_id": city_id(q.destination),
        }
    url = build_url(site, q, extra=extra)
    snap = await fetch_snapshot(
        url,
        site,
        headless=settings.headless,
        profile_dir=settings.storage_dir / "profiles" / site.name,
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

    # Проверка: 0 вариантов при непустой странице (> 200 символов) и не капча
    if not offers and parser:
        text_stripped = snap.text.strip()
        is_captcha = snap.blocked
        if len(text_stripped) > 200 and not is_captcha:
            _save_empty_snapshot(site, snap, settings)
            raise ParserEmptyError(
                "формат выдачи мог измениться, 0 вариантов при непустой странице",
                page_url=snap.url,
            )

    for o in offers:
        o.url = o.url or snap.url
    return sorted(offers, key=lambda o: o.price), snap.url, partial


async def _run_site(
    site: SiteConfig,
    q: Query,
    settings: Settings,
    *,
    guard: SearchGuard | None = None,
) -> SiteResult:
    t0 = time.monotonic()
    g = guard if guard is not None else globals()["guard"]

    async def _fetch() -> tuple[list[Offer], str | None, bool]:
        if site.source == "api":
            offers, page_url = await _from_api(site, q, settings)
            return offers, page_url, False
        else:
            return await _from_browser(site, q, settings)

    try:
        (offers, page_url, partial), from_cache = await g.run(
            key_site=site.name,
            key_query=q,
            factory=_fetch,
        )
        return SiteResult(
            site=site.name,
            ok=True,
            offers=list(offers),
            error=None,
            page_url=page_url,
            elapsed=time.monotonic() - t0,
            partial=partial,
            cached=from_cache,
        )
    except ParserEmptyError as e:
        log.warning("Парсер сайта %s вернул 0 вариантов: %s", site.name, e)
        return SiteResult(
            site=site.name,
            ok=False,
            offers=[],
            error=e.message,
            page_url=e.page_url,
            elapsed=time.monotonic() - t0,
            partial=False,
            cached=False,
        )
    except Exception as e:
        log.exception("Ошибка при поиске на сайте %s: %s", site.name, e)
        err_msg = str(e) if "TP_TOKEN" in str(e) else friendly_error(e)
        return SiteResult(
            site=site.name,
            ok=False,
            offers=[],
            error=err_msg,
            page_url=None,
            elapsed=time.monotonic() - t0,
            partial=False,
            cached=False,
        )


async def search(
    q: Query,
    settings: Settings,
    *,
    guard: SearchGuard | None = None,
) -> list[SiteResult]:
    sites = [s for s in settings.sites if s.enabled]
    # браузеры тяжёлые: не больше 2 одновременно (api-источники это не касается, но их мало)
    sem = asyncio.Semaphore(2)
    g = guard if guard is not None else globals()["guard"]

    async def guarded(s: SiteConfig) -> SiteResult:
        async with sem:
            return await _run_site(s, q, settings, guard=g)

    return await asyncio.gather(*(guarded(s) for s in sites))
