

import pytest


@pytest.fixture(autouse=True)
def _isolated_price_history(tmp_path, monkeypatch):
    """Тесты бота не должны писать в настоящую историю цен state/history.sqlite."""
    import avia.bot
    from avia.history import PriceHistory

    monkeypatch.setattr(avia.bot, "PriceHistory", lambda _path: PriceHistory(tmp_path / "history.sqlite"))
