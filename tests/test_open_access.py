"""Открытый режим бота: регистрация по tg id, блокировка, лимит поисков."""
import pytest

from avia.config import Settings
from avia.users import UserStore
from tests.test_bot_flow import RecordingBot


def make_bot(**kw) -> RecordingBot:
    s = Settings(sites=[], tg_token="t", tg_allowed_ids={1}, tg_open=True, **kw)
    return RecordingBot(s)


def msg(uid, text="/start", chat_type="private"):
    return {"chat": {"id": uid, "type": chat_type}, "from": {"id": uid, "username": f"u{uid}", "first_name": "Имя"}, "text": text}


@pytest.mark.asyncio
async def test_stranger_registered_and_served():
    bot = make_bot()
    await bot.handle(msg(555))
    assert any(m == "sendMessage" for m, _ in bot.calls)
    u = bot.users.get(555)
    assert u and u.username == "u555" and not u.blocked


@pytest.mark.asyncio
async def test_closed_mode_ignores_stranger():
    bot = make_bot()
    bot.s.tg_open = False
    await bot.handle(msg(555))
    assert not bot.calls and bot.users.get(555) is None


@pytest.mark.asyncio
async def test_blocked_user_ignored_and_groups_ignored():
    bot = make_bot()
    bot.users.register(7)
    bot.users.set_blocked(7, True)
    await bot.handle(msg(7))
    await bot.handle(msg(8, chat_type="supergroup"))
    assert not bot.calls and bot.users.get(8) is None


@pytest.mark.asyncio
async def test_admin_cannot_be_blocked_out():
    bot = make_bot()
    bot.users.register(1)
    bot.users.set_blocked(1, True)
    await bot.handle(msg(1))
    assert bot.calls


def test_daily_limit(tmp_path):
    us = UserStore(tmp_path / "u.sqlite")
    us.register(5, now=1_000_000)
    assert us.try_use_search(5, 2, now=1_000_000)
    assert us.try_use_search(5, 2, now=1_000_100)
    assert not us.try_use_search(5, 2, now=1_000_200)
    assert us.try_use_search(5, 2, now=1_000_000 + 86400 * 2)
    assert us.try_use_search(5, 0, now=1_000_300)
    assert us.get(5).total_searches == 4


def test_subscriptions_are_per_user(tmp_path):
    from datetime import date
    from avia.criteria import SearchCriteria
    from avia.models import Query
    from avia.subscriptions import SubscriptionStore

    st = SubscriptionStore(tmp_path / "s.sqlite")
    q = Query("KUF", "EVN", date(2030, 12, 1))
    sub = st.create(100, q, "any", SearchCriteria())
    assert st.count_active(100) == 1 and st.count_active(200) == 0
    assert not st.deactivate(sub.id, 200)
    assert st.deactivate(sub.id, 100)


def sent_texts(bot):
    return [p.get("text", "") for m, p in bot.calls if m == "sendMessage"]


@pytest.mark.asyncio
async def test_admin_block_unblock_stats():
    bot = make_bot()
    await bot.handle(msg(555))
    bot.calls.clear()
    await bot.handle(msg(1, "/block 555"))
    assert bot.users.get(555).blocked
    bot.calls.clear()
    await bot.handle(msg(555))
    assert not bot.calls
    await bot.handle(msg(1, "/unblock 555"))
    assert not bot.users.get(555).blocked
    bot.calls.clear()
    await bot.handle(msg(1, "/stats"))
    assert "Людей: 2" in sent_texts(bot)[0] and "555" in sent_texts(bot)[0]


@pytest.mark.asyncio
async def test_admin_commands_closed_to_others():
    bot = make_bot()
    await bot.handle(msg(555))
    bot.calls.clear()
    await bot.handle(msg(555, "/block 1"))
    await bot.handle(msg(555, "/stats"))
    assert not bot.calls


@pytest.mark.asyncio
async def test_cannot_block_admin():
    bot = make_bot()
    await bot.handle(msg(1, "/block 1"))
    assert "нельзя" in sent_texts(bot)[0] and not bot.users.get(1).blocked
