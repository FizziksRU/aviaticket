

import pytest


@pytest.fixture(autouse=True)
def _isolated_price_history(tmp_path, monkeypatch):
    """Тесты бота не должны писать в настоящую историю цен и подписки state/*.sqlite."""
    import avia.bot
    from avia.history import PriceHistory
    from avia.subscriptions import SubscriptionStore

    monkeypatch.setattr(avia.bot, "PriceHistory", lambda _path: PriceHistory(tmp_path / "history.sqlite"))
    from avia.users import UserStore

    monkeypatch.setattr(avia.bot, "UserStore", lambda _path: UserStore(tmp_path / "users.sqlite"))
    monkeypatch.setattr(avia.bot, "SubscriptionStore", lambda _path: SubscriptionStore(tmp_path / "subscriptions.sqlite"))


@pytest.fixture(autouse=True)
def _no_real_droidhub(monkeypatch):
    """Тесты не ходят на телефоны: пустые значения из окружения load_dotenv из .env не перетирает."""
    for name in ("DROIDHUB_URL", "DROIDHUB_TOKEN", "DROIDHUB_DEVICES"):
        monkeypatch.setenv(name, "")
