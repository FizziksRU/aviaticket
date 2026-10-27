from __future__ import annotations

from datetime import date
import json
import logging
from pathlib import Path

import pytest

from avia.collector import Snapshot
from avia.config import Settings, SiteConfig
from avia.guard import SearchGuard
from avia.models import Leg, Offer, Query
from avia.parsers import PARSERS
from avia.pipeline import ParserEmptyError, _from_browser, filter_wrong_dates, search


def test_filter_wrong_dates_mixed():
    """filter_wrong_dates на смешанном списке:

    - верная дата вылета туда и обратно;
    - неверная дата вылета туда;
    - верная дата туда и неверная обратная;
    - depart_at=None остаётся (не отбрасывается).
    """
    q = Query(
        origin="MOW",
        destination="LED",
        depart=date(2026, 12, 16),
        return_date=date(2026, 12, 20),
    )

    offer_ok = Offer(
        site="s",
        price=5000,
        depart_at="2026-12-16T10:15:00",
        back=Leg(depart_at="2026-12-20T18:30:00"),
    )
    offer_wrong_depart = Offer(
        site="s",
        price=4500,
        depart_at="2026-12-13T10:15:00",
        back=Leg(depart_at="2026-12-20T18:30:00"),
    )
    offer_wrong_back = Offer(
        site="s",
        price=4700,
        depart_at="2026-12-16T10:15:00",
        back=Leg(depart_at="2026-12-17T18:30:00"),
    )
    offer_no_dates = Offer(
        site="s",
        price=5200,
        depart_at=None,
        back=None,
    )
    offer_ok_no_back_date = Offer(
        site="s",
        price=5300,
        depart_at="2026-12-16T14:00:00",
        back=Leg(depart_at=None),
    )

    offers = [
        offer_ok,
        offer_wrong_depart,
        offer_wrong_back,
        offer_no_dates,
        offer_ok_no_back_date,
    ]

    kept, dropped = filter_wrong_dates(offers, q)

    assert dropped == 2
    assert kept == [offer_ok, offer_no_dates, offer_ok_no_back_date]


def test_filter_wrong_dates_one_way():
    """filter_wrong_dates при запросе в одну сторону (без return_date)."""
    q = Query(origin="MOW", destination="LED", depart=date(2026, 12, 16))

    o_ok = Offer(site="s", price=3000, depart_at="2026-12-16T09:00:00")
    o_wrong = Offer(site="s", price=2500, depart_at="2026-12-15T09:00:00")
    o_none = Offer(site="s", price=3200, depart_at=None)

    kept, dropped = filter_wrong_dates([o_ok, o_wrong, o_none], q)
    assert dropped == 1
    assert kept == [o_ok, o_none]


@pytest.mark.asyncio
async def test_from_browser_all_wrong_dates_raises_error_and_dumps_snapshot(monkeypatch, tmp_path: Path):
    """Интеграционный тест _from_browser: когда все офферы на другие даты,

    поднимается ParserEmptyError и сохраняется файл *_wrongdates_*.json в tmp debug_dir.
    """
    async def fake_fetch_snapshot(url, site, **kwargs):
        return Snapshot(
            url="https://site.test/search?token=SECRET123",
            text="Результаты поиска билетов " * 10,
            json_payloads=[("https://site.test/api", '{"data": "val"}')],
            blocked=False,
        )

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_fetch_snapshot)

    def wrong_dates_parser(snap, q):
        return [
            Offer(
                site="",
                price=5000,
                depart_at="2026-12-13T10:00",
                back=Leg(depart_at="2026-12-17T18:00"),
            ),
            Offer(
                site="",
                price=6000,
                depart_at="2026-12-14T12:00",
                back=Leg(depart_at="2026-12-18T20:00"),
            ),
        ]

    monkeypatch.setitem(PARSERS, "test_wrong_parser", wrong_dates_parser)

    site = SiteConfig(
        "mock_site",
        "https://site.test/{origin}{destination}",
        url_template_rt="https://site.test/{origin}{destination}/{date_iso}/{date_iso_ret}",
        parser="test_wrong_parser",
    )
    settings = Settings(sites=[site], debug_dir=tmp_path, tp_token="SECRET123")
    q = Query("MOW", "LED", depart=date(2026, 12, 16), return_date=date(2026, 12, 20))

    with pytest.raises(ParserEmptyError) as exc_info:
        await _from_browser(site, q, settings)

    assert exc_info.value.message == "сайт вернул билеты на другие даты, а не на запрошенные"
    assert exc_info.value.page_url == "https://site.test/search?token=SECRET123"

    dump_files = list(tmp_path.glob("*_wrongdates_*.json"))
    assert len(dump_files) == 1
    dump_path = dump_files[0]
    assert dump_path.name.startswith("mock_site_wrongdates_")

    content = dump_path.read_text(encoding="utf-8")
    data = json.loads(content)
    assert data["url"] == "https://site.test/search?token=[REDACTED]"
    assert "SECRET123" not in content


@pytest.mark.asyncio
async def test_from_browser_mixed_dates_keeps_only_correct_and_logs(monkeypatch, tmp_path: Path, caplog):
    """Интеграционный тест _from_browser: при смешанных датах остаются только верные

    офферы, отброшенные логируются в warning, и файл снимка не создаётся.
    """
    async def fake_fetch_snapshot(url, site, **kwargs):
        return Snapshot(
            url="https://site.test/search",
            text="Результаты поиска билетов " * 10,
            json_payloads=[],
            blocked=False,
        )

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_fetch_snapshot)

    def mixed_parser(snap, q):
        return [
            Offer(
                site="",
                price=7000,
                depart_at="2026-12-16T12:00",
                back=Leg(depart_at="2026-12-20T20:00"),
            ),
            Offer(
                site="",
                price=3000,
                depart_at="2026-12-13T10:00",
                back=Leg(depart_at="2026-12-17T18:00"),
            ),
            Offer(
                site="",
                price=5000,
                depart_at="2026-12-16T08:00",
                back=Leg(depart_at="2026-12-20T18:00"),
            ),
        ]

    monkeypatch.setitem(PARSERS, "test_mixed_parser", mixed_parser)

    site = SiteConfig(
        "mock_site",
        "https://site.test/{origin}{destination}",
        url_template_rt="https://site.test/{origin}{destination}/{date_iso}/{date_iso_ret}",
        parser="test_mixed_parser",
    )
    settings = Settings(sites=[site], debug_dir=tmp_path)
    q = Query("MOW", "LED", depart=date(2026, 12, 16), return_date=date(2026, 12, 20))

    with caplog.at_level(logging.WARNING):
        offers, page_url, partial = await _from_browser(site, q, settings)

    assert len(offers) == 2
    # Сортировка по цене
    assert [o.price for o in offers] == [5000, 7000]
    assert offers[0].depart_at == "2026-12-16T08:00"
    assert offers[1].depart_at == "2026-12-16T12:00"
    assert offers[0].url == "https://site.test/search"
    assert offers[0].site == "mock_site"

    # Предупреждение в логе с именем сайта и числом отброшенных
    assert "mock_site" in caplog.text
    assert "отброшено 1" in caplog.text

    # Файл снимка не должен создаваться при успешной фильтрации части офферов
    assert list(tmp_path.glob("*_wrongdates_*.json")) == []


@pytest.mark.asyncio
async def test_from_browser_site_without_depart_at(monkeypatch, tmp_path: Path):
    """Офферы без depart_at не отбрасываются (поведение для сайтов без depart_at не меняется)."""
    async def fake_fetch_snapshot(url, site, **kwargs):
        return Snapshot(
            url="https://site.test/search",
            text="Результаты поиска билетов " * 10,
            json_payloads=[],
            blocked=False,
        )

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_fetch_snapshot)

    def nodate_parser(snap, q):
        return [
            Offer(site="", price=3500, depart_at=None),
            Offer(site="", price=4500, depart_at=None),
        ]

    monkeypatch.setitem(PARSERS, "test_nodate_parser", nodate_parser)

    site = SiteConfig("nodate_site", "https://site.test/{origin}{destination}", parser="test_nodate_parser")
    settings = Settings(sites=[site], debug_dir=tmp_path)
    q = Query("MOW", "LED", depart=date(2026, 12, 16))

    offers, page_url, partial = await _from_browser(site, q, settings)
    assert len(offers) == 2
    assert [o.price for o in offers] == [3500, 4500]


@pytest.mark.asyncio
async def test_search_pipeline_wrong_dates_handled(monkeypatch, tmp_path: Path):
    """В search() при неверных датах сайт возвращает ok=False с сообщением ParserEmptyError."""
    async def fake_fetch_snapshot(url, site, **kwargs):
        return Snapshot(
            url="https://site.test/search",
            text="Результаты поиска билетов " * 10,
            json_payloads=[],
            blocked=False,
        )

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_fetch_snapshot)

    def wrong_parser(snap, q):
        return [Offer(site="", price=1000, depart_at="2026-12-13T10:00")]

    monkeypatch.setitem(PARSERS, "test_wrong_parser", wrong_parser)

    site = SiteConfig(
        "mock_site",
        "https://site.test/{origin}{destination}",
        url_template_rt="https://site.test/{origin}{destination}/{date_iso}/{date_iso_ret}",
        parser="test_wrong_parser",
    )
    settings = Settings(sites=[site], debug_dir=tmp_path)
    q = Query("MOW", "LED", depart=date(2026, 12, 16))

    results = await search(q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].error == "сайт вернул билеты на другие даты, а не на запрошенные"
    assert results[0].page_url == "https://site.test/search"
    assert len(list(tmp_path.glob("*_wrongdates_*.json"))) == 1
