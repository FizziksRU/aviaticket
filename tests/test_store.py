from datetime import date

from avia.criteria import SearchCriteria
from avia.models import Offer, Query, SiteResult
from avia.store import ResultStore, SearchSnapshot


def test_result_store_put_get_and_offer_by_number():
    """Проверка сохранения, получения и выборки билета по номеру (нумерация с 1)."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    o1 = Offer("site1", 10000)
    o2 = Offer("site2", 12000)
    results = [SiteResult("site1", True, [o1]), SiteResult("site2", True, [o2])]
    flat = [o1, o2]

    fake_time = 100000.0
    store = ResultStore(ttl=1800.0, clock=lambda: fake_time)

    snap = store.put(
        chat_id=12345,
        query=q,
        results=results,
        baggage_mode="with",
        flat=flat,
    )

    assert isinstance(snap, SearchSnapshot)
    assert snap.query == q
    assert snap.results == results
    assert snap.baggage_mode == "with"
    assert snap.created_at == fake_time
    assert snap.flat == flat

    # Проверка метода offer_by_number
    assert snap.offer_by_number(1) is o1
    assert snap.offer_by_number(2) is o2
    assert snap.offer_by_number(0) is None
    assert snap.offer_by_number(3) is None
    assert snap.offer_by_number(-1) is None

    # Получение из хранилища
    retrieved = store.get(12345)
    assert retrieved is snap


def test_result_store_ttl_expiration_with_mock_clock():
    """Проверка устаревания снимка по TTL с подменой часов clock."""
    q = Query("MOW", "LED", date(2026, 12, 1))
    current_time = 1000.0

    def mock_clock() -> float:
        return current_time

    store = ResultStore(ttl=1800.0, clock=mock_clock)
    store.put(999, q, [], "any", [])

    # В пределах TTL снимок доступен
    current_time = 1000.0 + 1799.0
    assert store.get(999) is not None

    # По истечении TTL возвращается None, а запись удаляется из памяти
    current_time = 1000.0 + 1801.0
    assert store.get(999) is None
    assert 999 not in store._data


def test_result_store_docstring_explains_in_memory_choice():
    """Docstring класса ResultStore объясняет хранение в памяти (один процесс, SQLite при отслеживании)."""
    doc = ResultStore.__doc__ or ""
    assert "один процесс" in doc
    assert "SQLite" in doc
    assert "отслеживани" in doc


def test_result_store_put_with_optional_fields():
    """Проверка сохранения снимка с дополнительными полями (criteria, pool, shown, view, dropped)."""
    q = Query("MOW", "ALA", date(2026, 12, 10))
    crit = SearchCriteria(max_transfers=1)
    o1 = Offer("site1", 10000, offer_id="id1")
    pool = [o1]
    store = ResultStore()

    snap = store.put(
        chat_id=555,
        query=q,
        results=[],
        baggage_mode="with",
        flat=[o1],
        criteria=crit,
        pool=pool,
        shown=1,
        view="compact",
        dropped_baggage=2,
        dropped_criteria=3,
    )

    assert snap.criteria == crit
    assert snap.pool == pool
    assert snap.shown == 1
    assert snap.view == "compact"
    assert snap.dropped_baggage == 2
    assert snap.dropped_criteria == 3
