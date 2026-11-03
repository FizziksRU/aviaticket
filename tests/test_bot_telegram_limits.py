from __future__ import annotations

import json

import httpx
import pytest

from avia.bot import TG_MAX_RETRIES, Bot
from avia.config import Settings
from avia.scheduler import build_notification


def make_bot(handler, tmp_path, **settings_kw) -> tuple[Bot, list[float]]:
    settings = Settings(sites=[], tg_token="T0KEN", tg_allowed_ids={1}, storage_dir=tmp_path, **settings_kw)
    bot = Bot(settings)
    bot.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    slept: list[float] = []

    async def fake_sleep(sec: float) -> None:
        slept.append(sec)

    bot._sleep = fake_sleep
    return bot, slept


@pytest.mark.asyncio
async def test_call_retries_after_429_retry_after(tmp_path):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "error_code": 429,
                                             "description": "Too Many Requests: retry after 7",
                                             "parameters": {"retry_after": 7}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    bot, slept = make_bot(handler, tmp_path)
    res = await bot.call("sendMessage", chat_id=1, text="hi")
    assert res == {"message_id": 5}
    assert slept == [7.0]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_call_gives_up_after_max_retries(tmp_path):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 1}})

    bot, slept = make_bot(handler, tmp_path)
    with pytest.raises(RuntimeError, match="429"):
        await bot.call("sendMessage", chat_id=1, text="hi")
    assert len(slept) == TG_MAX_RETRIES


@pytest.mark.asyncio
async def test_retry_after_is_capped(tmp_path):
    n = []

    def handler(req: httpx.Request) -> httpx.Response:
        n.append(1)
        if len(n) == 1:
            return httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 3600}})
        return httpx.Response(200, json={"ok": True, "result": True})

    bot, slept = make_bot(handler, tmp_path)
    await bot.call("answerCallbackQuery", callback_query_id="x")
    assert slept == [60.0]


@pytest.mark.asyncio
async def test_other_errors_are_not_retried(tmp_path):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})

    bot, slept = make_bot(handler, tmp_path)
    with pytest.raises(RuntimeError, match="chat not found"):
        await bot.call("sendMessage", chat_id=1, text="hi")
    assert slept == []


@pytest.mark.asyncio
async def test_api_base_override_and_default(tmp_path):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, json={"ok": True, "result": True})

    bot, _ = make_bot(handler, tmp_path)
    await bot.call("getMe")
    bot2, _ = make_bot(handler, tmp_path, tg_api_base="http://127.0.0.1:8787/")
    await bot2.call("getMe")
    assert seen == ["https://api.telegram.org/botT0KEN/getMe", "http://127.0.0.1:8787/botT0KEN/getMe"]


@pytest.mark.asyncio
async def test_send_splits_long_html_into_balanced_chunks(tmp_path):
    texts = []

    def handler(req: httpx.Request) -> httpx.Response:
        texts.append(json.loads(req.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(texts)}})

    bot, _ = make_bot(handler, tmp_path)
    text = "<blockquote>" + "\n".join(f"{i}. <b>12 345 ₽</b> &amp; багаж" for i in range(600)) + "</blockquote>"
    await bot.send(1, text, reply_markup={"inline_keyboard": []})
    assert len(texts) > 1
    assert all(len(t) <= 4096 for t in texts)
    assert all(t.startswith("<blockquote>") and t.endswith("</blockquote>") for t in texts)


@pytest.mark.asyncio
async def test_send_falls_back_to_plain_text_when_html_rejected(tmp_path):
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        bodies.append(body)
        if body.get("parse_mode") == "HTML":
            return httpx.Response(400, json={"ok": False, "error_code": 400,
                                             "description": "Bad Request: can't parse entities: unsupported start tag"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    bot, _ = make_bot(handler, tmp_path)
    res = await bot.send(1, "<b>Цена</b> 5 000 ₽ &amp; <x>")
    assert res == {"message_id": 9}
    assert bodies[-1]["text"] == "Цена 5 000 ₽ & "
    assert "parse_mode" not in bodies[-1]


def test_long_notification_is_cut_on_tag_boundary():
    from datetime import date

    from avia.criteria import SearchCriteria
    from avia.models import Offer
    from avia.subscriptions import Subscription

    sub = Subscription(id=1, chat_id=1, origin="MOW", destination="LED", depart=date(2026, 12, 1), return_date=None,
                       adults=1, baggage_mode="any", criteria=SearchCriteria(), threshold=None, active=True,
                       created_at=0.0, last_checked_at=None, last_notified_at=None, last_notified_price=None,
                       last_notified_offer_id=None, min_price=None)
    offer = Offer(price=5000, site="aviasales", url="https://x.test/?a=1&b=2")
    text = build_notification(sub, offer, ["x" * 5000])
    assert len(text) <= 4096
    assert text.count("<b>") == text.count("</b>")
