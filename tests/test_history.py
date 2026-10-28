from datetime import date

from avia.formatting import format_details, offer_line, price_change_card, price_change_short
from avia.history import PriceHistory
from avia.models import Offer, Query, SiteResult

Q = Query(origin="KUF", destination="EVN", depart=date(2026, 12, 1))


def _res(price, offer_id="abc"):
    return [SiteResult(site="s", ok=True, offers=[Offer(site="s", price=price, offer_id=offer_id)])]


def test_first_observation_has_no_previous(tmp_path):
    h = PriceHistory(tmp_path / "h.sqlite")
    res = _res(10000)
    h.annotate(Q, res, now=1000.0)
    o = res[0].offers[0]
    assert o.prev_price is None and o.min_price is None


def test_price_drop_and_min(tmp_path):
    h = PriceHistory(tmp_path / "h.sqlite")
    h.annotate(Q, _res(10000), now=1000.0)
    h.annotate(Q, _res(9000), now=5000.0)
    res = _res(9500)
    h.annotate(Q, res, now=9000.0)
    o = res[0].offers[0]
    assert (o.prev_price, o.prev_at, o.min_price) == (9000, 5000.0, 9000)
    assert "↑" in price_change_short(o)
    assert "выросла" in price_change_card(o, now=9000.0 + 7200)


def test_same_price_within_ttl_not_duplicated(tmp_path):
    h = PriceHistory(tmp_path / "h.sqlite")
    h.annotate(Q, _res(10000), now=1000.0)
    h.annotate(Q, _res(10000), now=1100.0)
    with h._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1


def test_other_route_and_failed_site_ignored(tmp_path):
    h = PriceHistory(tmp_path / "h.sqlite")
    h.annotate(Q, [SiteResult(site="s", ok=False, offers=[Offer(site="s", price=1, offer_id="z")])], now=1.0)
    h.annotate(Q, _res(10000, offer_id="x"), now=1.0)
    res = _res(8000, offer_id="y")
    h.annotate(Q, res, now=2.0)
    assert res[0].offers[0].prev_price is None


def test_formatting_without_history_unchanged():
    o = Offer(site="s", price=10000)
    assert price_change_short(o) == "" and price_change_card(o) is None
    assert "↓" not in offer_line(o) and "упала" not in format_details(o, Q)


def test_drop_shown_in_line_and_card():
    o = Offer(site="s", price=9000, prev_price=10000, prev_at=0.0, min_price=9000)
    assert "↓" in offer_line(o)
    assert "упала" in price_change_card(o, now=3 * 86400)
    assert "3 дн назад" in price_change_card(o, now=3 * 86400)
