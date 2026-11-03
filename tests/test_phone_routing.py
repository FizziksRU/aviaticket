"""PHONE_MAP (сайт → телефоны) и повтор пустой выдачи на другом телефоне (RETRY_OTHER_PHONE)."""
from __future__ import annotations

from datetime import date

import httpx
import pytest

import avia.pipeline as pipeline
from avia.collector import Snapshot
from avia.config import Settings, SiteConfig, load_settings, parse_phone_map
from avia.droidhub import _reset_pool, candidates, fetch_page_snapshot_any, phone_enabled, retry_candidates
from avia.guard import SearchGuard
from avia.models import Offer, Query
from avia.parsers import PARSERS

_ORIG_ASYNC_CLIENT = httpx.AsyncClient
Q = Query("MOW", "LED", date(2026, 12, 1))


@pytest.fixture(autouse=True)
def reset_pool_state():
    _reset_pool()
    yield
    _reset_pool()


def hub_settings(**kw) -> Settings:
    base = dict(sites=[], droidhub_url="https://hub.test", droidhub_token="tok", droidhub_devices=["realme", "redmi"])
    base.update(kw)
    return Settings(**base)


def test_parse_phone_map_accepts_lists_and_blanks():
    assert parse_phone_map("") == {}
    assert parse_phone_map(" ozon_travel = redmi ; aviasales=realme,redmi; ") == {
        "ozon_travel": ["redmi"], "aviasales": ["realme", "redmi"]}


@pytest.mark.parametrize("raw", ["ozon_travel", "=redmi", "ozon_travel=", "ozon_travel= , "])
def test_parse_phone_map_rejects_malformed(raw):
    with pytest.raises(ValueError, match="PHONE_MAP"):
        parse_phone_map(raw)


def test_load_settings_reads_phone_flags(monkeypatch):
    monkeypatch.setenv("PHONE_MAP", "ozon_travel=redmi")
    monkeypatch.setenv("RETRY_OTHER_PHONE", "1")
    s = load_settings()
    assert s.phone_map == {"ozon_travel": ["redmi"]} and s.retry_other_phone is True


def test_load_settings_defaults_keep_sites_yaml_routing(monkeypatch):
    monkeypatch.delenv("PHONE_MAP", raising=False)
    monkeypatch.delenv("RETRY_OTHER_PHONE", raising=False)
    s = load_settings()
    assert s.phone_map == {} and s.retry_other_phone is False


def test_phone_map_overrides_sites_yaml():
    site = SiteConfig("ozon_travel", "https://x.test", phone="auto")
    assert candidates(hub_settings(), site) == ["realme", "redmi"]
    assert candidates(hub_settings(phone_map={"ozon_travel": ["redmi"]}), site) == ["redmi"]


def test_phone_map_can_move_desktop_site_to_phone():
    site = SiteConfig("tutu", "https://x.test", phone="")
    assert not phone_enabled(hub_settings(), site)
    assert phone_enabled(hub_settings(phone_map={"tutu": ["redmi"]}), site)


def test_retry_prefers_other_phones_of_the_site_before_the_rest_of_pool():
    s = hub_settings(retry_other_phone=True, droidhub_devices=["realme", "redmi", "phone3", "phone4"],
                     phone_map={"onetwotrip": ["realme", "phone3"]})
    site = SiteConfig("onetwotrip", "https://x.test", phone="realme")
    assert retry_candidates(s, site, "realme") == ["phone3"]
    lone = hub_settings(retry_other_phone=True, phone_map={"onetwotrip": ["realme"]})
    assert retry_candidates(lone, site, "realme") == ["redmi"]


def test_retry_candidates_exclude_failed_phone_only_when_enabled():
    site = SiteConfig("ozon_travel", "https://x.test", phone="realme")
    assert retry_candidates(hub_settings(), site, "realme") == ["realme"]
    assert retry_candidates(hub_settings(retry_other_phone=True), site, "realme") == ["redmi"]
    lonely = hub_settings(retry_other_phone=True, droidhub_devices=["realme"])
    assert retry_candidates(lonely, site, "realme") == ["realme"]


@pytest.mark.asyncio
async def test_snapshot_remembers_device(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"url": "https://ok.test", "text": "x", "finished": True})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("avia.droidhub.httpx.AsyncClient",
                        lambda **kw: _ORIG_ASYNC_CLIENT(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}))
    snap, _ = await fetch_page_snapshot_any(hub_settings(), ["redmi"], "https://site.test")
    assert snap.device == "redmi"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled, expected", [(False, [["realme"], ["realme"]]), (True, [["realme"], ["redmi"]])])
async def test_empty_result_retry_goes_to_other_phone(monkeypatch, enabled, expected):
    seen: list[list[str]] = []

    def parser(snap, q):
        return [Offer(site="ozon_travel", price=7000)] if snap.device == "redmi" else []

    parser.results_complete = lambda snap: True  # type: ignore[attr-defined]
    monkeypatch.setitem(PARSERS, "routing_test_parser", parser)
    monkeypatch.setattr(pipeline, "EMPTY_RETRY_DELAY", 0.0)

    async def fake_any(settings, cands, url, **kwargs):
        seen.append(list(cands))
        return Snapshot(url=url, text="страница с ценами " * 30, device=cands[0]), True

    monkeypatch.setattr(pipeline, "fetch_page_snapshot_any", fake_any)
    site = SiteConfig("ozon_travel", "https://ozon.test/{origin}{destination}", phone="realme",
                      parser="routing_test_parser")
    settings = hub_settings(sites=[site], retry_other_phone=enabled, debug_dir=None)
    results = await pipeline.search(Q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert seen == expected
    assert results[0].ok is enabled
