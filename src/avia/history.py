"""История цен в SQLite: запоминает цену каждого билета и показывает, как она менялась."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable

from .models import Offer, Query, SiteResult

# Если цена билета не изменилась и последняя запись свежее этого срока, новую строку не пишем
SAME_PRICE_TTL = 600.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id    TEXT NOT NULL,
    site        TEXT NOT NULL,
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart      TEXT NOT NULL,
    return_date TEXT,
    adults      INTEGER NOT NULL,
    price       INTEGER NOT NULL,
    currency    TEXT NOT NULL,
    observed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_price_offer ON price_history (offer_id, observed_at);
CREATE INDEX IF NOT EXISTS ix_price_route ON price_history (origin, destination, depart, return_date);
"""


class PriceHistory:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def annotate(
        self,
        q: Query,
        results: Iterable[SiteResult],
        now: float | None = None,
    ) -> None:
        """Проставляет prev_price/prev_at/min_price у билетов и записывает текущие цены."""
        now = time.time() if now is None else now
        offers = [o for r in results if r.ok for o in r.offers if o.offer_id]
        if not offers:
            return
        ret = q.return_date.isoformat() if q.return_date else None
        with self._connect() as db:
            for o in offers:
                last = db.execute(
                    "SELECT price, observed_at FROM price_history "
                    "WHERE offer_id = ? AND currency = ? ORDER BY observed_at DESC, id DESC LIMIT 1",
                    (o.offer_id, o.currency),
                ).fetchone()
                low = db.execute(
                    "SELECT MIN(price) FROM price_history WHERE offer_id = ? AND currency = ?",
                    (o.offer_id, o.currency),
                ).fetchone()
                if last is not None:
                    o.prev_price, o.prev_at = int(last[0]), float(last[1])
                    o.min_price = int(low[0]) if low and low[0] is not None else None
                fresh_same = last is not None and int(last[0]) == o.price and now - float(last[1]) < SAME_PRICE_TTL
                if fresh_same:
                    continue
                db.execute(
                    "INSERT INTO price_history "
                    "(offer_id, site, origin, destination, depart, return_date, adults, price, currency, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        o.offer_id, o.site, q.origin, q.destination, q.depart.isoformat(),
                        ret, q.adults, o.price, o.currency, now,
                    ),
                )
