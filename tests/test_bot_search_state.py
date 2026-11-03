"""Состояние диалога и квота вокруг Bot._run_search: сбои, отказы, новый диалог во время поиска."""
from __future__ import annotations

import asyncio
import logging
from datetime import date

import pytest

import avia.bot as bot_mod
from avia.bot import Bot
from avia.config import Settings
from avia.dialog import SearchRequest, Session, Step
from avia.models import Offer, Query, SiteResult

USER = 555
ADMIN = 123
Q = Query("MOW", "LED", date(2026, 12, 1))


class RecordingBot(Bot):
    def __init__(self, settings: Settings, **kwargs):
        super().__init__(settings, **kwargs)
        self.calls: list[tuple[str, dict]] = []
        self.fail_send_after = -1  # номер sendMessage, начиная с которого отправка падает
        self._sent = 0

    async def call(self, method: str, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self._sent += 1
            if 0 <= self.fail_send_after < self._sent:
                raise RuntimeError("Telegram API error: Bad Request")
            return {"message_id": 1000 + self._sent}
        return True

    def texts(self) -> list[str]:
        return [p.get("text", "") for m, p in self.calls if m == "sendMessage"]

    def last_markup(self) -> dict | None:
        return [p for m, p in self.calls if m == "sendMessage"][-1].get("reply_markup")


def make_bot(tmp_path, *, limit: int = 40) -> RecordingBot:
    s = Settings(sites=[], tg_token="t", tg_allowed_ids={ADMIN}, tg_open=True,
                 daily_search_limit=limit, storage_dir=tmp_path)
    bot = RecordingBot(s)
    bot.users.register(USER, "u", "U")
    return bot


def ok_result() -> list[SiteResult]:
    offer = Offer(site="kupibilet", price=5000, currency="RUB", airline="S7",
                  depart_time="10:00", arrive_time="11:30", duration="1ч 30м")
    return [SiteResult(site="kupibilet", ok=True, offers=[offer])]


def searching_session(dialog_id: str) -> Session:
    return Session(step=Step.SEARCHING, origin="MOW", destination="LED", depart=Q.depart, dialog_id=dialog_id)


def search_count(bot: Bot) -> int:
    with bot.users._connect() as db:
        return int(db.execute("SELECT search_count FROM users WHERE tg_id = ?", (USER,)).fetchone()[0])


@pytest.mark.asyncio
async def test_failed_search_leaves_dialog_usable(tmp_path, monkeypatch):
    async def boom(q, s):
        raise RuntimeError("droidhub down")

    monkeypatch.setattr(bot_mod, "search", boom)
    bot = make_bot(tmp_path)
    bot.dialog.sessions[USER] = searching_session("aaaa")
    await bot._run_search(USER, SearchRequest(query=Q, dialog_id="aaaa"))
    assert bot.dialog.sessions[USER].step == Step.DONE
    assert bot.last_markup() == {"inline_keyboard": [[{"text": "🔎 Найти билет", "callback_data": "menu:go"}]]}
    replies = bot.dialog.on_text(USER, "Москва")
    assert all("Поиск ещё идёт" not in r.text for r in replies if hasattr(r, "text"))


@pytest.mark.asyncio
async def test_daily_limit_refusal_resets_step_and_offers_subscriptions(tmp_path):
    bot = make_bot(tmp_path, limit=1)
    assert bot.users.try_use_search(USER, 1)
    bot.dialog.sessions[USER] = searching_session("aaaa")
    await bot._run_search(USER, SearchRequest(query=Q, dialog_id="aaaa"))
    assert "лимит поисков исчерпан" in bot.texts()[-1]
    assert bot.dialog.sessions[USER].step == Step.DONE
    assert bot.last_markup()["inline_keyboard"][0][0]["callback_data"] == "menu:track"


@pytest.mark.asyncio
async def test_busy_refusal_does_not_strand_new_dialog(tmp_path):
    bot = make_bot(tmp_path)
    bot.busy.add(USER)
    bot.dialog.sessions[USER] = searching_session("bbbb")
    await bot._run_search(USER, SearchRequest(query=Q, dialog_id="bbbb"))
    assert bot.texts()[-1].startswith("Предыдущий поиск ещё идёт")
    assert bot.dialog.sessions[USER].step == Step.DONE


@pytest.mark.asyncio
async def test_new_dialog_started_during_search_is_not_clobbered(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    bot.dialog.sessions[USER] = searching_session("aaaa")

    async def slow_search(q, s):
        # человек нажал «Новый поиск» и уже выбирает город отправления
        bot.dialog.sessions[USER] = Session(step=Step.ORIGIN, dialog_id="bbbb")
        return ok_result()

    monkeypatch.setattr(bot_mod, "search", slow_search)
    await bot._run_search(USER, SearchRequest(query=Q, dialog_id="aaaa"))
    session = bot.dialog.sessions[USER]
    assert (session.step, session.dialog_id) == (Step.ORIGIN, "bbbb")
    buttons = [b["callback_data"] for row in bot.last_markup()["inline_keyboard"] for b in row]
    assert "new:aaaa" in buttons


@pytest.mark.asyncio
async def test_successful_search_marks_own_session_done(tmp_path, monkeypatch):
    async def fine(q, s):
        return ok_result()

    monkeypatch.setattr(bot_mod, "search", fine)
    bot = make_bot(tmp_path)
    bot.dialog.sessions[USER] = searching_session("aaaa")
    await bot._run_search(USER, SearchRequest(query=Q, dialog_id="aaaa"))
    assert bot.dialog.sessions[USER].step == Step.DONE
    assert bot.store.get(USER) is not None
    assert search_count(bot) == 1


@pytest.mark.asyncio
async def test_quota_is_refunded_when_no_site_answers(tmp_path, monkeypatch):
    async def all_failed(q, s):
        return [SiteResult(site="kupibilet", ok=False, error="timeout")]

    monkeypatch.setattr(bot_mod, "search", all_failed)
    bot = make_bot(tmp_path)
    await bot._run_search(USER, SearchRequest(query=Q))
    assert search_count(bot) == 0


@pytest.mark.asyncio
async def test_quota_is_refunded_when_search_raises(tmp_path, monkeypatch):
    async def boom(q, s):
        raise RuntimeError("x")

    monkeypatch.setattr(bot_mod, "search", boom)
    bot = make_bot(tmp_path)
    await bot._run_search(USER, SearchRequest(query=Q))
    assert search_count(bot) == 0


@pytest.mark.asyncio
async def test_snapshot_is_stored_even_if_sending_results_fails(tmp_path, monkeypatch):
    async def fine(q, s):
        return ok_result()

    monkeypatch.setattr(bot_mod, "search", fine)
    bot = make_bot(tmp_path)
    bot.fail_send_after = 1  # «Ищу…» уходит, первый кусок выдачи падает
    await bot._run_search(USER, SearchRequest(query=Q))
    assert bot.store.get(USER) is not None


def test_refund_never_goes_below_zero(tmp_path):
    bot = make_bot(tmp_path)
    bot.users.refund_search(USER)
    assert search_count(bot) == 0


@pytest.mark.asyncio
async def test_spawn_logs_handler_crash(tmp_path, caplog):
    bot = make_bot(tmp_path)

    async def crash():
        raise ValueError("handler bug")

    with caplog.at_level(logging.ERROR, logger="avia.bot"):
        task = bot._spawn(crash())
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
    assert "обработчик апдейта упал" in caplog.text
    assert not bot._tasks


@pytest.mark.asyncio
async def test_shutdown_waits_for_running_handlers_then_cancels_the_rest(tmp_path):
    bot = make_bot(tmp_path)
    finished: list[str] = []

    async def quick():
        await asyncio.sleep(0)
        finished.append("quick")

    async def stuck():
        await asyncio.Event().wait()

    bot._spawn(quick())
    hung = bot._spawn(stuck())
    await bot.shutdown(grace=0.05)
    assert finished == ["quick"]
    assert hung.cancelled()
    assert bot.http.is_closed


@pytest.mark.asyncio
async def test_no_flights_page_is_charged_like_a_normal_search(tmp_path, monkeypatch):
    async def no_flights(q, s):
        return [SiteResult(site="kupibilet", ok=False, offers=[], error="0 вариантов", empty=True)]

    monkeypatch.setattr(bot_mod, "search", no_flights)
    bot = make_bot(tmp_path)
    await bot._run_search(USER, SearchRequest(query=Q))
    assert search_count(bot) == 1


@pytest.mark.asyncio
async def test_quota_is_refunded_when_results_were_not_delivered(tmp_path, monkeypatch):
    async def fine(q, s):
        return ok_result()

    monkeypatch.setattr(bot_mod, "search", fine)
    bot = make_bot(tmp_path)
    bot.fail_send_after = 1
    await bot._run_search(USER, SearchRequest(query=Q))
    assert search_count(bot) == 0


@pytest.mark.asyncio
async def test_failed_callback_answer_still_runs_the_action(tmp_path, monkeypatch):
    started: list[str] = []

    async def fine(q, s):
        started.append("search")
        return ok_result()

    monkeypatch.setattr(bot_mod, "search", fine)

    class StaleCallbackBot(RecordingBot):
        async def call(self, method, **params):
            if method == "answerCallbackQuery":
                raise RuntimeError("Telegram API error: query is too old")
            return await super().call(method, **params)

    bot = StaleCallbackBot(Settings(sites=[], tg_token="t", tg_allowed_ids={ADMIN}, storage_dir=tmp_path))
    session = searching_session("aaaa")
    session.step = Step.BAGGAGE
    bot.dialog.sessions[ADMIN] = session
    await bot.handle_callback({"id": "1", "from": {"id": ADMIN}, "data": "bag:aaaa:any",
                               "message": {"chat": {"id": ADMIN}, "message_id": 5}})
    assert started == ["search"]
    assert bot.dialog.sessions[ADMIN].step == Step.DONE


@pytest.mark.asyncio
async def test_sigterm_stops_cleanly(monkeypatch):
    handlers = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: handlers.setdefault(sig, cb))

    class Forever:
        async def run(self):
            await asyncio.Event().wait()

    task = asyncio.create_task(bot_mod._serve(Forever()))
    await asyncio.sleep(0)
    handlers[bot_mod.signal.SIGTERM]()
    assert await task is None
