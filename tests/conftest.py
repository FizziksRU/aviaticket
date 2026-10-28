

import pytest


@pytest.fixture(autouse=True)
def _isolated_price_history(tmp_path, monkeypatch):
    """Тесты бота не должны писать в настоящую историю цен и подписки state/*.sqlite."""
    import avia.bot
    from avia.history import PriceHistory
    from avia.subscriptions import SubscriptionStore

    monkeypatch.setattr(avia.bot, "PriceHistory", lambda _path: PriceHistory(tmp_path / "history.sqlite"))
    monkeypatch.setattr(avia.bot, "SubscriptionStore", lambda _path: SubscriptionStore(tmp_path / "subscriptions.sqlite"))
