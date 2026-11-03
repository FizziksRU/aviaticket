"""Здоровье источников: учёт исходов, сигнал о сломанном сайте, /status и алерт админам."""
from __future__ import annotations

from datetime import date

import pytest

import avia.bot as bot_mod
from avia.bot import Bot
from avia.config import Settings, load_settings
from avia.health import Health
from avia.models import Offer, Query, SiteResult
from avia.pipeline import RT_UNSUPPORTED

ADMIN = 123
Q = Query("MOW", "LED", date(2026, 12, 1))


def ok(site: str = "kupibilet", partial: bool = False) -> SiteResult:
    return SiteResult(site=site, ok=True, offers=[Offer(site=site, price=5000)], elapsed=6.0, partial=partial)


def fail(site: str = "ozon_travel", error: str = "формат выдачи мог измениться") -> SiteResult:
    return SiteResult(site=site, ok=False, offers=[], error=error, elapsed=31.0)


def test_site_is_reported_broken_once_after_three_failures_in_row():
    h = Health()
    assert h.record([fail()]) == []
    assert h.record([fail()]) == []
    assert h.record([fail()]) == ["ozon_travel"]
    assert h.record([fail()]) == []  # уже сообщили, пока сайт не восстановится
    h.record([ok("ozon_travel")])
    assert h.failures_in_row("ozon_travel") == 0
    assert [h.record([fail()]) for _ in range(3)][-1] == ["ozon_travel"]


def test_cache_hits_and_unsupported_requests_are_not_failures():
    h = Health(skip_errors=(RT_UNSUPPORTED,))
    cached = SiteResult(site="tutu", ok=True, offers=[], cached=True)
    for _ in range(5):
        h.record([cached, fail("onetwotrip", RT_UNSUPPORTED)])
    assert h.stats() == []
    assert h.counters["site_cache_hits"] == 5


def test_stats_and_percentiles():
    h = Health(history=3)
    h.record([ok(partial=True)], seconds=10)
    h.record([fail("kupibilet", "timeout")], seconds=30)
    h.record([ok()], kind="scheduler", seconds=20)
    h.record([ok()], seconds=40)
    (st,) = h.stats()
    assert (st.site, st.total, st.ok, st.partial) == ("kupibilet", 3, 2, 0)  # хранится 3 последних
    assert st.last_error == "timeout" and st.failures_in_row == 0
    assert h.search_percentiles() == (30.0, 40.0)
    assert h.counters["searches_user"] == 3 and h.counters["searches_scheduler"] == 1


class RecordingBot(Bot):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.sent: list[tuple[int, str]] = []

    async def call(self, method: str, **params):
        if method == "sendMessage":
            self.sent.append((params["chat_id"], params["text"]))
            return {"message_id": len(self.sent)}
        return True


def make_bot(tmp_path, alerts: bool) -> RecordingBot:
    return RecordingBot(Settings(sites=[], tg_token="t", tg_allowed_ids={ADMIN}, storage_dir=tmp_path,
                                 health_alerts=alerts))


@pytest.mark.asyncio
@pytest.mark.parametrize("alerts", [False, True])
async def test_admins_get_alert_only_when_enabled(tmp_path, monkeypatch, alerts):
    async def broken_search(q, s):
        return [ok(), fail()]

    monkeypatch.setattr(bot_mod, "search", broken_search)
    bot = make_bot(tmp_path, alerts)
    for _ in range(3):
        await bot._scheduler_search(Q)
    to_admin = [text for chat, text in bot.sent if chat == ADMIN]
    assert bool(to_admin) is alerts
    if alerts:
        assert "ozon_travel: 3 раза подряд" in to_admin[0]


@pytest.mark.asyncio
async def test_status_command_shows_sites_and_queue(tmp_path, monkeypatch):
    async def search(q, s):
        return [ok(), fail()]

    monkeypatch.setattr(bot_mod, "search", search)
    bot = make_bot(tmp_path, alerts=False)
    await bot._scheduler_search(Q)
    await bot.handle({"chat": {"id": ADMIN}, "from": {"id": ADMIN}, "text": "/status"})
    text = bot.sent[-1][1]
    assert "подписок 1" in text and "Сейчас ищут 0 из 2, в очереди 0" in text
    assert "🟢 kupibilet: 1/1 ok" in text
    assert "🟡 ozon_travel: 0/1 ok" in text and "подряд без результата 1" in text


@pytest.mark.asyncio
async def test_status_is_admin_only(tmp_path):
    bot = RecordingBot(Settings(sites=[], tg_token="t", tg_allowed_ids={ADMIN}, tg_open=True, storage_dir=tmp_path))
    await bot.handle({"chat": {"id": 777}, "from": {"id": 777}, "text": "/status"})
    assert bot.sent == []


def test_health_alerts_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("HEALTH_ALERTS", raising=False)
    assert load_settings().health_alerts is False
    monkeypatch.setenv("HEALTH_ALERTS", "1")
    assert load_settings().health_alerts is True
