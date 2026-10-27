from __future__ import annotations

from datetime import date, datetime
from html import escape

from .models import Leg, Offer, Query, SiteResult
from .offers import baggage_state
from .parsing import city_name

SYMBOLS = {"RUB": "₽", "KZT": "₸", "UZS": "сум", "USD": "$", "EUR": "€"}


def money(value: int, currency: str) -> str:
    s = f"{value:,}".replace(",", " ")
    return f"{s} {SYMBOLS.get(currency, currency)}"


def _airline_label(o: Offer) -> str | None:
    if o.back is not None:
        a1 = escape(o.airline) if o.airline else None
        a2 = escape(o.back.airline) if o.back.airline else None
        if a1 and a2:
            return a1 if a1 == a2 else f"{a1} / {a2}"
        return a1 or a2
    return escape(o.airline) if o.airline else None


def _format_leg_brief(leg: Offer | Leg, prefix: str, *, include_duration: bool = True) -> str:
    time_str = ""
    if leg.depart_time and leg.arrive_time:
        time_str = f"{escape(leg.depart_time)}→{escape(leg.arrive_time)}"
    elif leg.depart_time:
        time_str = f"вылет {escape(leg.depart_time)}"
    elif leg.arrive_time:
        time_str = f"прилёт {escape(leg.arrive_time)}"

    dur_str = escape(leg.duration) if (include_duration and leg.duration) else None

    trans_str = None
    if leg.transfers is not None:
        if leg.transfers == 0:
            trans_str = "прямой"
        elif leg.transfers == 1:
            trans_str = "1 пересадка"
        else:
            trans_str = f"пересадок: {leg.transfers}"

    parts: list[str] = []
    if time_str:
        parts.append(time_str)
    if dur_str:
        parts.append(dur_str)

    res = ", ".join(parts)
    if trans_str:
        if res:
            res = f"{res} ({trans_str})"
        else:
            res = f"({trans_str})"

    return f"{prefix}{res}".strip()


def _build_roundtrip_line(o: Offer, *, baggage_mode: str, include_duration: bool) -> str:
    parts = [f"<b>{money(o.price, o.currency)}</b>"]
    airline = _airline_label(o)
    if airline:
        parts.append(airline)
    outbound_str = _format_leg_brief(o, "туда ", include_duration=include_duration)
    if outbound_str:
        parts.append(outbound_str)
    assert o.back is not None
    return_str = _format_leg_brief(o.back, "обратно ", include_duration=include_duration)
    if return_str:
        parts.append(return_str)
    if o.baggage:
        parts.append(escape(o.baggage))
    if baggage_mode != "any" and baggage_state(o) is None:
        if not o.baggage or "багаж не указан" not in o.baggage.lower():
            parts.append("багаж не указан")
    return " · ".join(parts)


def offer_line(o: Offer, *, baggage_mode: str = "any") -> str:
    if o.back is not None:
        line = _build_roundtrip_line(o, baggage_mode=baggage_mode, include_duration=True)
        if len(line) > 160:
            line = _build_roundtrip_line(o, baggage_mode=baggage_mode, include_duration=False)
        return line

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
    if baggage_mode != "any" and baggage_state(o) is None:
        if not o.baggage or "багаж не указан" not in o.baggage.lower():
            parts.append("багаж не указан")
    return " · ".join(parts)


def build_report(
    q: Query,
    results: list[SiteResult],
    top_n: int = 5,
    *,
    baggage_mode: str = "any",
    dropped: int = 0,
) -> tuple[list[str], list[Offer]]:
    """Формирует части отчёта (каждая ≤ 4000 символов) и плоский список показанных билетов."""
    orig_name = city_name(q.origin)
    dest_name = city_name(q.destination)
    route_head = f"✈️ <b>{escape(orig_name)} → {escape(dest_name)}</b>"
    if q.return_date:
        date_str = f"туда {q.depart:%d.%m.%Y}, обратно {q.return_date:%d.%m.%Y}"
        head = (
            f"{route_head}, "
            f"{date_str}, пассажиров: {q.adults} (цена общая за оба плеча)"
        )
    else:
        date_str = f"{q.depart:%d.%m.%Y}"
        head = f"{route_head}, {date_str}, пассажиров: {q.adults}"

    header_lines = [head]
    if baggage_mode != "any":
        b_mode_str = "только с багажом" if baggage_mode == "with" else "только без багажа"
        header_lines.append(
            f"Багаж: {b_mode_str}, скрыто вариантов: {dropped} (варианты без данных о багаже показаны)"
        )
    header_lines.append("")

    all_offers = sorted((o for r in results for o in r.offers), key=lambda o: (o.currency, o.price))
    if all_offers:
        best = min(all_offers, key=lambda o: o.price)
        header_lines.append(
            f"Дешевле всего: {offer_line(best, baggage_mode=baggage_mode)} (<i>{escape(best.site)}</i>)"
        )
        header_lines.append("")

    chunks: list[str] = []
    current_lines: list[str] = []
    flat: list[Offer] = []
    counter = 1

    def flush() -> None:
        nonlocal current_lines
        if current_lines:
            text = "\n".join(current_lines).strip()
            if text:
                chunks.append(text)
            current_lines = []

    def would_fit(lines_to_add: list[str]) -> bool:
        if not current_lines:
            return True
        return len("\n".join(current_lines + lines_to_add)) <= 4000

    current_lines.extend(header_lines)

    for r in results:
        if not r.ok:
            block = [f"⚠️ <b>{escape(r.site)}</b>: {escape(r.error or 'ошибка')}", ""]
            if current_lines and not would_fit(block):
                flush()
            current_lines.extend(block)
            continue

        if not r.offers:
            block = [f"<b>{escape(r.site)}</b>: билетов не нашлось"]
            if r.partial:
                block.append(escape("неполная выдача: сайт ещё догружал варианты"))
            if r.cached:
                block.append(escape("из кэша, цены могли измениться"))
            block.append("")
            if current_lines and not would_fit(block):
                flush()
            current_lines.extend(block)
            continue

        site_header = [f"<b>{escape(r.site)}</b> ({r.elapsed:.0f} с)"]
        if r.partial:
            site_header.append(escape("неполная выдача: сайт ещё догружал варианты"))
        if r.cached:
            site_header.append(escape("из кэша, цены могли измениться"))

        cont_header = [f"<b>{escape(r.site)}</b> (продолжение)"]

        shown: set[str] = set()
        site_offers: list[tuple[Offer, str]] = []
        for o in r.offers:
            line = offer_line(o, baggage_mode=baggage_mode)
            if line in shown:
                continue
            shown.add(line)
            site_offers.append((o, line))
            if len(shown) >= top_n:
                break

        first_variant = [f"{counter}. {site_offers[0][1]}"] if site_offers else []
        if current_lines and not would_fit(site_header + first_variant):
            flush()

        current_lines.extend(site_header)

        for o, line in site_offers:
            num_line = f"{counter}. {line}"
            flat.append(o)
            counter += 1
            if not would_fit([num_line]):
                flush()
                current_lines.extend(cont_header)
            current_lines.append(num_line)

        url_lines = []
        if r.page_url:
            url_lines.append(f'<a href="{escape(r.page_url, quote=True)}">открыть выдачу</a>')
        url_lines.append("")

        if not would_fit(url_lines):
            flush()
            current_lines.extend(cont_header)
        current_lines.extend(url_lines)

    footer_lines = ["<i>Цены сняты со страницы сейчас, при покупке могут измениться.</i>"]
    if flat:
        footer_lines.append("Напишите номер билета, чтобы увидеть подробности.")

    if current_lines and not would_fit(footer_lines):
        flush()

    current_lines.extend(footer_lines)
    flush()

    return chunks, flat


def format_report(
    q: Query,
    results: list[SiteResult],
    top_n: int = 5,
    *,
    baggage_mode: str = "any",
    dropped: int = 0,
) -> str:
    """Формирует текстовый отчёт со сквозной нумерацией вариантов."""
    parts, _ = build_report(q, results, top_n, baggage_mode=baggage_mode, dropped=dropped)
    return "\n".join(parts)


def _format_datetime(
    iso_str: str | None,
    time_str: str | None,
    fallback_date: date | None,
    *,
    is_departure: bool = True,
) -> str:
    if iso_str:
        try:
            dt = datetime.fromisoformat(iso_str)
            return dt.strftime("%d.%m.%Y %H:%M")
        except (ValueError, TypeError):
            return escape(iso_str)
    if time_str and fallback_date:
        return f"{fallback_date:%d.%m.%Y} {escape(time_str)}"
    if time_str:
        return escape(time_str)
    if is_departure and fallback_date:
        return f"{fallback_date:%d.%m.%Y}"
    return "не указано"


def _format_transfers(transfers: int | None, layovers: list[str] | None) -> str:
    if layovers:
        return ", ".join(escape(l) for l in layovers)
    if transfers is not None:
        return "0 (прямой)" if transfers == 0 else str(transfers)
    return "не указано"


def _format_leg_details(
    leg: Offer | Leg,
    orig: str,
    dest: str,
    leg_date: date | None,
    *,
    is_back: bool = False,
) -> list[str]:
    lines: list[str] = []
    if is_back:
        lines.append("<b>Обратно</b>")
    orig_name = city_name(orig)
    dest_name = city_name(dest)
    header_line = f"✈️ <b>{escape(orig_name)} → {escape(dest_name)}</b>"
    if orig_name != orig or dest_name != dest:
        header_line += f"<!-- {escape(orig)} → {escape(dest)} -->"
    lines.append(header_line)

    dep_str = _format_datetime(leg.depart_at, leg.depart_time, leg_date, is_departure=True)
    arr_str = _format_datetime(leg.arrive_at, leg.arrive_time, leg_date, is_departure=False)
    lines.append(f"Вылет: {dep_str}")
    lines.append(f"Прилёт: {arr_str}")

    airline_str = escape(leg.airline) if leg.airline else "не указано"
    lines.append(f"Авиакомпания: {airline_str}")

    if leg.flight_numbers:
        fn_label = "Рейс" if len(leg.flight_numbers) == 1 else "Рейсы"
        fn_val = ", ".join(escape(fn) for fn in leg.flight_numbers)
        lines.append(f"{fn_label}: {fn_val}")
    if leg.aircraft:
        lines.append(f"Самолёт: {escape(leg.aircraft)}")

    dur_str = escape(leg.duration) if leg.duration else "не указано"
    lines.append(f"Длительность: {dur_str}")

    lines.append(f"Пересадки: {_format_transfers(leg.transfers, leg.layovers)}")
    return lines


def format_details(o: Offer, q: Query) -> str:
    """Формирует подробную карточку билета для отправки в чат."""
    lines = _format_leg_details(o, q.origin, q.destination, q.depart, is_back=False)

    if o.back is not None:
        lines.append("")
        back_date = q.return_date or q.depart
        lines.extend(_format_leg_details(o.back, q.destination, q.origin, back_date, is_back=True))

    lines.append("")
    baggage_val = escape(o.baggage) if o.baggage else "не указано"
    carry_on_val = escape(o.carry_on) if o.carry_on else "не указано"
    lines.append(f"Багаж: {baggage_val}")
    lines.append(f"Ручная кладь: {carry_on_val}")
    lines.append(f"Цена: <b>{money(o.price, o.currency)}</b>")
    lines.append(f"Сайт: {escape(o.site)}")

    if o.url:
        caption = "купить билет" if o.url_is_direct else "открыть выдачу сайта"
        lines.append(f'Ссылка: <a href="{escape(o.url, quote=True)}">{caption}</a>')
        if not o.url_is_direct:
            criteria: list[str] = []
            has_time = bool(o.depart_time or o.depart_at or (o.back and (o.back.depart_time or o.back.depart_at)))
            has_fn = bool(o.flight_numbers or (o.back and o.back.flight_numbers))
            if has_time:
                criteria.append("времени вылета")
            if has_fn:
                criteria.append("номеру рейса")
            if criteria:
                lines.append(f"Найдите рейс в выдаче по {' и '.join(criteria)}")

    if o.fetched_at is not None:
        t_str = datetime.fromtimestamp(o.fetched_at).strftime("%H:%M")
        lines.append(f"Цена снята: {t_str}")

    return "\n".join(lines).strip()
