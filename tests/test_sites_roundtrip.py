from __future__ import annotations

import asyncio
from datetime import date
import pytest

from avia.collector import Snapshot
from avia.config import Settings, SiteConfig, load_settings
from avia.guard import SearchGuard
from avia.models import Offer, Query
from avia.parsers import PARSERS
from avia.parsing import ParseError
from avia.pipeline import search
from avia.recon import parse_recon_args, parse_recon_return_date
from avia.sites import build_url


def test_build_url_one_way():
    """Без return_date build_url использует url_template и старые плейсхолдеры."""
    site = SiteConfig(
        name="test_site",
        url_template="https://site.test/{origin}_{destination}_{date_iso}_{ddmm}_{ddmmyyyy}_{adults}",
        url_template_rt="https://site.test/rt/{origin}_{destination}",
    )
    q = Query(origin="MOW", destination="ALA", depart=date(2026, 12, 10), adults=2)
    url = build_url(site, q)
    assert url == "https://site.test/MOW_ALA_2026-12-10_1012_10122026_2"


def test_build_url_roundtrip_placeholders():
    """С return_date build_url берёт url_template_rt и подставляет return плейсхолдеры."""
    site = SiteConfig(
        name="test_site",
        url_template="https://site.test/oneway",
        url_template_rt="https://site.test/rt/{origin}_{destination}_{date_iso}_{date_iso_ret}_{ddmm}_{ddmm_ret}_{ddmmyyyy}_{ddmmyyyy_ret}_{adults}",
    )
    q = Query(
        origin="MOW",
        destination="ALA",
        depart=date(2026, 12, 10),
        adults=3,
        return_date=date(2026, 12, 25),
    )
    url = build_url(site, q)
    assert url == "https://site.test/rt/MOW_ALA_2026-12-10_2026-12-25_1012_2512_10122026_25122026_3"


def test_build_url_aviasales_yaml():
    """Проверка шаблона aviasales из config/sites.yaml: туда-обратно формирует верный URL."""
    settings = load_settings()
    aviasales = next((s for s in settings.sites if s.name == "aviasales"), None)
    assert aviasales is not None
    assert aviasales.url_template_rt == "https://www.aviasales.ru/search/{origin}{ddmm}{destination}{ddmm_ret}{adults}"

    q = Query(
        origin="MOW",
        destination="ALA",
        depart=date(2026, 12, 12),
        adults=1,
        return_date=date(2026, 12, 19),
    )
    url = build_url(aviasales, q)
    assert url == "https://www.aviasales.ru/search/MOW1212ALA19121"


def test_build_url_unsupported_roundtrip_raises_value_error():
    """Если url_template_rt пуст, а return_date задан — поднимается ValueError."""
    site = SiteConfig(
        name="no_rt_site",
        url_template="https://site.test/{origin}{destination}",
        url_template_rt="",
    )
    q = Query(
        origin="MOW",
        destination="ALA",
        depart=date(2026, 12, 10),
        return_date=date(2026, 12, 20),
    )
    with pytest.raises(ValueError, match="для этого сайта обратные билеты пока не поддерживаются"):
        build_url(site, q)


def test_recon_argument_parsing():
    """Тестирование разбора аргументов recon, включая 5-й аргумент (дата возврата)."""
    today = date(2026, 10, 1)

    # 4 аргумента: дата возврата отсутствует
    site_name, q = parse_recon_args(["aviasales", "Москва", "Алматы", "10.12"], today=today)
    assert site_name == "aviasales"
    assert q.origin == "MOW"
    assert q.destination == "ALA"
    assert q.depart == date(2026, 12, 10)
    assert q.return_date is None

    # 5 аргументов: дата возврата задана (формат DD.MM)
    site_name, q = parse_recon_args(["aviasales", "Москва", "Алматы", "10.12", "20.12"], today=today)
    assert site_name == "aviasales"
    assert q.depart == date(2026, 12, 10)
    assert q.return_date == date(2026, 12, 20)

    # Тестирование маленькой функции parse_recon_return_date
    dep = date(2026, 12, 10)
    ret = parse_recon_return_date("25.12", dep, today=today)
    assert ret == date(2026, 12, 25)

    # Дата возврата раньше вылета вызывает ParseError
    with pytest.raises(ParseError, match="обратная дата раньше даты вылета"):
        parse_recon_return_date("05.12.2026", dep, today=today)

    # Некорректный формат вызывает ParseError
    with pytest.raises(ParseError):
        parse_recon_return_date("invalid_date", dep, today=today)


@pytest.mark.asyncio
async def test_pipeline_roundtrip_skips_unsupported_site_without_browser(monkeypatch):
    """Pipeline: при q.return_date сайт с пустым url_template_rt не запускает браузер."""
    fetch_calls: list[str] = []

    async def fake_fetch_snapshot(url, site, **kwargs):
        fetch_calls.append(site.name)
        return Snapshot(
            url="https://site.test/result",
            text="page content",
            json_payloads=[],
        )

    monkeypatch.setattr("avia.pipeline.fetch_snapshot", fake_fetch_snapshot)

    def dummy_parser(snap, q):
        return [Offer(site="supported_site", price=5000)]

    monkeypatch.setitem(PARSERS, "rt_parser", dummy_parser)

    site_no_rt = SiteConfig(
        name="no_rt_site",
        url_template="https://no-rt.test/{origin}{destination}",
        url_template_rt="",
        parser="rt_parser",
    )
    site_with_rt = SiteConfig(
        name="supported_site",
        url_template="https://rt.test/{origin}{destination}",
        url_template_rt="https://rt.test/{origin}{destination}{ddmm_ret}",
        parser="rt_parser",
    )

    settings = Settings(sites=[site_no_rt, site_with_rt])
    q = Query(
        origin="MOW",
        destination="ALA",
        depart=date(2026, 12, 10),
        return_date=date(2026, 12, 20),
    )

    results = await search(q, settings, guard=SearchGuard(min_interval=0.0, retry_delay=0.0))
    assert len(results) == 2

    res_no_rt = next(r for r in results if r.site == "no_rt_site")
    res_with_rt = next(r for r in results if r.site == "supported_site")

    # Сайт без rt вернул ошибку
    assert res_no_rt.ok is False
    assert res_no_rt.offers == []
    assert res_no_rt.error == "для этого сайта обратные билеты пока не поддерживаются"

    # Сайт с rt выполнил поиск
    assert res_with_rt.ok is True
    assert len(res_with_rt.offers) == 1

    # Браузер (fetch_snapshot) вызывался ТОЛЬКО для supported_site
    assert fetch_calls == ["supported_site"]
