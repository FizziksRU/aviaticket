"""Форматирование отчётов о поиске авиабилетов и карточек предложений."""
from __future__ import annotations

from html import escape
import time

from .models import Leg, Offer, Query, SiteResult
from .offers import baggage_state

SYMBOLS = {"RUB": "₽", "KZT": "₸", "UZS": "сум", "USD": "$", "EUR": "€"}
MAX_PART_LEN = 4000


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

    st = baggage_state(o)
    if st is not None:
        if o.baggage:
            parts.append(escape(o.baggage))
    else:
        if o.baggage and "багаж не указан" not in o.baggage.lower():
            parts.append(escape(o.baggage))
        parts.append("багаж не указан")

    return " · ".join(parts)


def _format_leg_info(
    leg: Offer | Leg,
    default_airline: str | None = None,
    default_date: object | None = None,
) -> list[str]:
    lines: list[str] = []

    dep_time = leg.depart_at
    if not dep_time:
        if default_date and leg.depart_time:
            dep_time = f"{default_date:%d.%m.%Y} {leg.depart_time}"
        elif leg.depart_time:
            dep_time = leg.depart_time
        elif default_date:
            dep_time = f"{default_date:%d.%m.%Y}"
        else:
            dep_time = "не указано"

    arr_time = leg.arrive_at
    if not arr_time:
        if default_date and leg.arrive_time:
            arr_time = f"{default_date:%d.%m.%Y} {leg.arrive_time}"
        elif leg.arrive_time:
            arr_time = leg.arrive_time
        else:
            arr_time = "не указано"

    lines.append(f"Вылет: {escape(dep_time)}")
    lines.append(f"Прилёт: {escape(arr_time)}")

    airline = leg.airline or default_airline or "не указана"
    lines.append(f"Авиакомпания: {escape(airline)}")

    if leg.flight_numbers:
        lines.append(f"Рейс: {escape(', '.join(leg.flight_numbers))}")
    if leg.aircraft:
        lines.append(f"Самолёт: {escape(leg.aircraft)}")

    lines.append(f"В пути: {escape(leg.duration or 'не указано')}")

    if leg.layovers:
        lines.append(f"Пересадки: {escape(', '.join(leg.layovers))}")
    elif leg.transfers is not None:
        lines.append("Пересадки: прямой" if leg.transfers == 0 else f"Пересадки: {leg.transfers}")
    else:
        lines.append("Пересадки: не указано")

    return lines


def format_details(o: Offer, q: Query, *, fetched_at: float | None = None) -> str:
    """Форматирует подробную карточку одного варианта перелёта."""
    lines: list[str] = [
        f"✈️ <b>{escape(q.origin)} → {escape(q.destination)}</b>",
    ]
    lines.extend(_format_leg_info(o, default_date=q.depart))

    lines.append(f"Багаж: {escape(o.baggage or 'не указано')}")
    lines.append(f"Ручная кладь: {escape(o.carry_on or 'не указано')}")

    if o.back is not None:
        lines.append("")
        lines.append(f"<b>Обратно ({escape(q.destination)} → {escape(q.origin)}):</b>")
        lines.extend(_format_leg_info(o.back, default_airline=o.airline, default_date=q.return_date))

    lines.append(f"Цена: <b>{money(o.price, o.currency)}</b>")
    lines.append(f"Сайт: {escape(o.site)}")

    if o.url:
        lines.append(f'<a href="{escape(o.url, quote=True)}">купить/открыть</a>')

    ts = fetched_at if fetched_at is not None else o.fetched_at
    if ts is not None:
        time_str = time.strftime("%H:%M", time.localtime(ts))
        lines.append(f"Цена снята: {time_str}")

    return "\n".join(lines).strip()


def _add_block(parts: list[str], current_lines: list[str], block: list[str]) -> None:
    test_text = "\n".join(current_lines + block)
    if len(test_text) <= MAX_PART_LEN:
        current_lines.extend(block)
    else:
        if current_lines:
            parts.append("\n".join(current_lines).strip())
        current_lines.clear()
        first = 1 if block and block[0] == "" else 0
        current_lines.extend(block[first:])


def build_report(
    q: Query,
    results: list[SiteResult],
    top_n: int = 5,
    *,
    baggage_mode: str = "any",
    dropped: int = 0,
) -> tuple[list[str], list[Offer]]:
    """Строит отчёт о поиске, разбивая его на части (каждая <= 4000 символов).

    Возвращает:
      - список сообщений parts (каждое <= 4000 символов)
      - сквозной плоский список показанных Offer в порядке их номеров
    """
    if q.return_date:
        dates_str = f"туда {q.depart:%d.%m.%Y}, обратно {q.return_date:%d.%m.%Y}"
    else:
        dates_str = f"{q.depart:%d.%m.%Y}"

    head = f"✈️ <b>{escape(q.origin)} → {escape(q.destination)}</b>, {dates_str}, пассажиров: {q.adults}"
    header_lines = [head]

    if baggage_mode != "any":
        b_mode_str = "только с багажом" if baggage_mode == "with" else "только без багажа"
        header_lines.append(
            f"Багаж: {b_mode_str}, скрыто вариантов: {dropped} (вариантов без данных о багаже показаны с пометкой)"
        )
    header_lines.append("")

    all_offers = sorted((o for r in results for o in r.offers), key=lambda o: (o.currency, o.price))
    if all_offers:
        best = min(all_offers, key=lambda o: o.price)
        header_lines.append(f"Дешевле всего: {offer_line(best)} (<i>{escape(best.site)}</i>)")
        header_lines.append("")

    parts: list[str] = []
    current_lines: list[str] = list(header_lines)
    flat: list[Offer] = []
    num = 0

    for r in results:
        if not r.ok:
            block = ["", f"⚠️ <b>{escape(r.site)}</b>: {escape(r.error or 'ошибка')}"]
            _add_block(parts, current_lines, block)
            continue
        if not r.offers:
            block = ["", f"<b>{escape(r.site)}</b>: билетов не нашлось"]
            if r.partial:
                block.append(escape("неполная выдача: сайт ещё догружал варианты"))
            if r.cached:
                block.append(escape("из кэша, цены могли измениться"))
            _add_block(parts, current_lines, block)
            continue

        site_header = ["", f"<b>{escape(r.site)}</b> ({r.elapsed:.0f} с)"]
        if r.partial:
            site_header.append(escape("неполная выдача: сайт ещё догружал варианты"))
        if r.cached:
            site_header.append(escape("из кэша, цены могли измениться"))

        _add_block(parts, current_lines, site_header)

        shown: set[str] = set()
        for o in r.offers:
            line = offer_line(o)
            if line in shown:
                continue
            shown.add(line)
            num += 1
            flat.append(o)
            var_line = f"{num}. {line}"

            test_text = "\n".join(current_lines + [var_line])
            if len(test_text) <= MAX_PART_LEN:
                current_lines.append(var_line)
            else:
                parts.append("\n".join(current_lines).strip())
                cont_header = f"<b>{escape(r.site)}</b> (продолжение)"
                current_lines = [cont_header, var_line]

            if len(shown) >= top_n:
                break

        if r.page_url:
            link_line = f'<a href="{escape(r.page_url, quote=True)}">открыть выдачу</a>'
            test_text = "\n".join(current_lines + [link_line])
            if len(test_text) <= MAX_PART_LEN:
                current_lines.append(link_line)
            else:
                parts.append("\n".join(current_lines).strip())
                current_lines = [f"<b>{escape(r.site)}</b> (продолжение)", link_line]

    footer_lines = [
        "",
        "Напишите номер билета, чтобы увидеть подробности",
        "<i>Цены сняты со страницы сейчас, при покупке могут измениться.</i>",
    ]
    test_text = "\n".join(current_lines + footer_lines)
    if len(test_text) <= MAX_PART_LEN:
        current_lines.extend(footer_lines)
    else:
        parts.append("\n".join(current_lines).strip())
        first = 1 if footer_lines and footer_lines[0] == "" else 0
        current_lines = list(footer_lines[first:])

    if current_lines:
        parts.append("\n".join(current_lines).strip())

    if not parts:
        parts = [""]

    return parts, flat


def format_report(
    q: Query,
    results: list[SiteResult],
    top_n: int = 5,
    *,
    baggage_mode: str = "any",
    dropped: int = 0,
) -> str:
    """Форматирует общий текстовый отчёт о поиске (обратная совместимость)."""
    parts, _ = build_report(q, results, top_n, baggage_mode=baggage_mode, dropped=dropped)
    return "\n".join(parts)
