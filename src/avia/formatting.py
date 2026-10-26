from __future__ import annotations

from html import escape

from .models import Offer, Query, SiteResult

SYMBOLS = {"RUB": "₽", "KZT": "₸", "UZS": "сум", "USD": "$", "EUR": "€"}


def money(value: int, currency: str) -> str:
    s = f"{value:,}".replace(",", " ")
    return f"{s} {SYMBOLS.get(currency, currency)}"


def offer_line(o: Offer) -> str:
    parts = [f"<b>{money(o.price, o.currency)}</b>"]
    if o.airline:
        parts.append(escape(o.airline))
    if o.depart_time and o.arrive_time:
        parts.append(f"{escape(o.depart_time)}→{escape(o.arrive_time)}")
    elif o.depart_time:
        parts.append(f"вылет {escape(o.depart_time)}")
    if o.duration:
        parts.append(escape(o.duration))
    if o.transfers is not None:
        parts.append("прямой" if o.transfers == 0 else f"пересадок: {o.transfers}")
    if o.baggage:
        parts.append(escape(o.baggage))
    return " · ".join(parts)


def format_report(q: Query, results: list[SiteResult], top_n: int = 5) -> str:
    head = f"✈️ <b>{escape(q.origin)} → {escape(q.destination)}</b>, {q.depart:%d.%m.%Y}, пассажиров: {q.adults}"
    lines = [head, ""]

    all_offers = sorted((o for r in results for o in r.offers), key=lambda o: (o.currency, o.price))
    if all_offers:
        best = min(all_offers, key=lambda o: o.price)
        lines.append(f"Дешевле всего: {offer_line(best)} (<i>{escape(best.site)}</i>)")
        lines.append("")

    for r in results:
        if not r.ok:
            lines.append(f"⚠️ <b>{escape(r.site)}</b>: {escape(r.error or 'ошибка')}")
            continue
        if not r.offers:
            lines.append(f"<b>{escape(r.site)}</b>: билетов не нашлось")
            if r.partial:
                lines.append(escape("неполная выдача: сайт ещё догружал варианты"))
            if r.cached:
                lines.append(escape("из кэша, цены могли измениться"))
            continue
        lines.append(f"<b>{escape(r.site)}</b> ({r.elapsed:.0f} с)")
        if r.partial:
            lines.append(escape("неполная выдача: сайт ещё догружал варианты"))
        if r.cached:
            lines.append(escape("из кэша, цены могли измениться"))
        for o in r.offers[:top_n]:
            lines.append("• " + offer_line(o))
        if r.page_url:
            lines.append(f'<a href="{escape(r.page_url, quote=True)}">открыть выдачу</a>')
        lines.append("")

    lines.append("<i>Цены сняты со страницы сейчас, при покупке могут измениться.</i>")
    return "\n".join(lines).strip()
