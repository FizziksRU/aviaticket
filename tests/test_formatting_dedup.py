from datetime import date

from avia.formatting import format_report
from avia.models import Offer, Query, SiteResult


def _offer(price: int, dep: str = "22:50") -> Offer:
    return Offer(site="x", price=price, airline="Nordwind", depart_time=dep, arrive_time="18:25", transfers=1)


def test_identical_offers_shown_once_and_top_n_filled_with_unique():
    q = Query("MOW", "ALA", date(2026, 12, 12))
    offers = [_offer(16403)] * 3 + [_offer(17241, "19:30"), _offer(17500, "14:15")]
    text = format_report(q, [SiteResult("x", True, offers)], top_n=3)
    assert text.count("16 403") == 2  # строка «Дешевле всего» + один пункт списка
    assert text.count("•") == 3
    assert "17 500" in text
