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

from avia.formatting import format_report, money
from avia.models import Offer, Query, SiteResult


def _offer(price: int, dep: str = "22:50") -> Offer:
    return Offer(site="x", price=price, airline="Nordwind", depart_time=dep, arrive_time="18:25", transfers=1)


def test_identical_offers_shown_once_and_top_n_filled_with_unique():
    q = Query("MOW", "ALA", date(2026, 12, 12))
    offers = [_offer(16403)] * 3 + [_offer(17241, "19:30"), _offer(17500, "14:15")]
    text = format_report(q, [SiteResult("x", True, offers)], top_n=3)
    assert text.count(money(16403, "RUB")) == 2  # строка «Дешевле всего» + один пункт списка
    assert "1. " in text and "2. " in text and "3. " in text
    assert money(17500, "RUB") in text
