"""Тесты для ResultStore и SearchSnapshot."""
from datetime import date
from pathlib import Path
import sys

_src = str(Path(__file__).resolve().parent.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

import avia
_avia_path = str(Path(_src) / "avia")
if _avia_path not in avia.__path__:
    avia.__path__ = [_avia_path] + list(avia.__path__)

for _mod in ["avia.models", "avia.formatting", "avia.offers", "avia.store"]:
    sys.modules.pop(_mod, None)

from avia.models import Offer, Query, SiteResult
from avia.store import ResultStore, SearchSnapshot


def test_result_store_docstring():
    """Проверка наличия объяснения in-memory / SQLite в docstring ResultStore."""
    doc = ResultStore.__doc__ or ""
    assert "SQLite" in doc
    assert ("памят" in doc.lower() or "memory" in doc.lower())


def test_result_store_put_get_and_ttl():
    """Тест сохранения, получения и истечения TTL с подменой clock."""
    current_time = 1000.0

    def fake_clock() -> float:
        return current_time

    store = ResultStore(ttl=1800.0, clock=fake_clock)
    q = Query("MOW", "ALA", date(2026, 12, 12))
    offers = [Offer(site="x", price=10000, airline="Nordwind", depart_time="10:00")]
    res = [SiteResult(site="x", ok=True, offers=offers)]

    # Сохраняем в момент 1000.0
    snap = store.put(chat_id=123, query=q, results=res, baggage_mode="with")
    assert isinstance(snap, SearchSnapshot)
    assert snap.query == q
    assert snap.baggage_mode == "with"
    assert snap.created_at == 1000.0
    assert len(snap.flat) == 1
    assert snap.flat[0] == offers[0]

    # Получаем до истечения TTL (через 1000 секунд)
    current_time = 2000.0
    got = store.get(123)
    assert got is not None
    assert got.query == q

    # Прошло больше TTL (1000 + 1801 = 2801)
    current_time = 2801.0
    expired = store.get(123)
    assert expired is None

    # Проверяем, что запись удалена из внутреннего хранилища
    assert 123 not in store._store


def test_search_snapshot_offer_by_number():
    """Тест поиска предложения по сквозному номеру с 1."""
    q = Query("MOW", "ALA", date(2026, 12, 12))
    o1 = Offer(site="s1", price=1000)
    o2 = Offer(site="s2", price=2000)
    o3 = Offer(site="s2", price=3000)

    snap = SearchSnapshot(
        query=q,
        results=[],
        baggage_mode="any",
        created_at=100.0,
        flat=[o1, o2, o3],
    )

    # Нумерация с 1
    assert snap.offer_by_number(1) == o1
    assert snap.offer_by_number(2) == o2
    assert snap.offer_by_number(3) == o3

    # Выход за границы диапазона
    assert snap.offer_by_number(0) is None
    assert snap.offer_by_number(-1) is None
    assert snap.offer_by_number(4) is None
