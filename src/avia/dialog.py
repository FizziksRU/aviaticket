from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from enum import Enum
from html import escape
import secrets

from .criteria import (
    ALLOWED_DAY_PARTS,
    ALLOWED_MAX_TRANSFERS,
    DAY_PARTS,
    SearchCriteria,
)
from .formatting import (
    build_compact_report,
    build_more_report,
    build_report,
    format_details,
)
from .models import Offer, Query
from .parsing import (
    ParseError,
    UnknownCityError,
    city_name,
    parse_date,
    parse_passengers,
    parse_query,
    parse_return_date,
    suggest_cities,
    to_iata,
)
from .store import ResultStore, SearchSnapshot

# сайты, умеющие «туда и обратно» (все пять сайтов); пустое множество включает заглушку «скоро»
ROUND_TRIP_SITES: frozenset[str] = frozenset({"aviasales", "kupibilet", "yandex_travel", "tutu", "onetwotrip"})

HELP = (
    "Пришлите команду:\n"
    "<code>/price Москва Алматы 10.12</code>\n"
    "<code>/price MOW TAS 2026-12-10 2</code> (2 пассажира)\n\n"
    "Города по-русски или кодом IATA."
)


class Step(str, Enum):
    MENU = "MENU"
    ORIGIN = "ORIGIN"
    DEST = "DEST"
    DATE = "DATE"
    TRIP = "TRIP"
    RETURN_DATE = "RETURN_DATE"
    BAGGAGE = "BAGGAGE"
    SEARCHING = "SEARCHING"
    DONE = "DONE"


@dataclass
class Reply:
    text: str = ""
    keyboard: list[list[tuple[str, str]]] | None = None
    edit: bool = False
    answer: str | None = None


@dataclass
class SearchRequest:
    query: Query
    baggage_mode: str = "any"
    criteria: SearchCriteria = field(default_factory=SearchCriteria)


@dataclass
class Session:
    step: Step = Step.MENU
    origin: str | None = None
    destination: str | None = None
    depart: date | None = None
    return_date: date | None = None
    adults: int = 1
    baggage_mode: str = "any"
    dialog_id: str = field(default_factory=lambda: secrets.token_hex(2))
    criteria: SearchCriteria = field(default_factory=SearchCriteria)


def to_reply_markup(keyboard: list[list[tuple[str, str]]] | None) -> dict | None:
    if not keyboard:
        return None
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": cb} for text, cb in row]
            for row in keyboard
        ]
    }


def compact_keyboard(dialog_id: str, *, has_more: bool) -> list[list[tuple[str, str]]]:
    rows: list[list[tuple[str, str]]] = []
    if has_more:
        rows.append([("➕ Показать ещё 5", f"more:{dialog_id}")])
    rows.append([
        ("⚙ Изменить фильтры", f"filters:{dialog_id}"),
        ("📋 Полный список", f"full:{dialog_id}"),
    ])
    rows.append([("🔎 Новый поиск", f"new:{dialog_id}")])
    return rows


def full_keyboard(dialog_id: str) -> list[list[tuple[str, str]]]:
    return [
        [
            ("⬅ К сводке", f"list:{dialog_id}:compact"),
            ("🔎 Новый поиск", f"new:{dialog_id}"),
        ]
    ]


class Dialog:
    def __init__(
        self,
        sessions: dict[int | str, Session] | None = None,
        store: ResultStore | None = None,
        today: Callable[[], date] | None = None,
    ):
        self.sessions: dict[int | str, Session] = sessions if sessions is not None else {}
        self.store: ResultStore = store if store is not None else ResultStore()
        self.today: Callable[[], date] = today if today is not None else date.today

    def start_menu(self, chat_id: int | str) -> Reply:
        self.sessions[chat_id] = Session(step=Step.MENU, dialog_id=secrets.token_hex(2))
        return Reply(
            text="Главное меню:",
            keyboard=[
                [("🔎 Найти билет", "menu:go")],
                [("🔔 Мои отслеживания", "menu:track")],
                [("ℹ️ Помощь", "menu:help")],
            ],
        )

    def _start_search(self, chat_id: int | str) -> Reply:
        session = Session(step=Step.ORIGIN, dialog_id=secrets.token_hex(2))
        self.sessions[chat_id] = session
        return Reply(
            text="Откуда? Укажите город или код IATA:",
            keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
        )

    def _baggage_reply(self, session: Session, *, edit: bool = False) -> Reply:
        desc = session.criteria.describe()
        adv_text = f"⚙ Расширенные настройки ({len(desc)})" if desc else "⚙ Расширенные настройки"
        return Reply(
            text="Багаж?",
            keyboard=[
                [
                    ("С багажом", f"bag:{session.dialog_id}:with"),
                    ("Без багажа", f"bag:{session.dialog_id}:without"),
                    ("Не важно", f"bag:{session.dialog_id}:any"),
                ],
                [(f"Пассажиров: {session.adults}", f"pax:{session.dialog_id}:+")],
                [(adv_text, f"adv:{session.dialog_id}:menu")],
                [("✖ Отмена", f"cancel:{session.dialog_id}")],
            ],
            edit=edit,
        )

    def _adv_menu_reply(self, session: Session) -> Reply:
        desc = session.criteria.describe()
        body = "\n".join(desc) if desc else "Всё не важно"
        text = f"⚙ Расширенные настройки\n\n{body}"
        return Reply(
            text=text,
            keyboard=[
                [("Пересадки", f"adv:{session.dialog_id}:s_tr")],
                [("Время в пути", f"adv:{session.dialog_id}:s_dur")],
                [("Время вылета", f"adv:{session.dialog_id}:s_dp")],
                [("Время прилёта", f"adv:{session.dialog_id}:s_ap")],
                [("Баланс цены и времени", f"adv:{session.dialog_id}:s_hc")],
                [("Сбросить всё", f"adv:{session.dialog_id}:reset")],
                [("✅ Готово", f"adv:{session.dialog_id}:done")],
                [("✖ Отмена", f"cancel:{session.dialog_id}")],
            ],
            edit=True,
        )

    def _adv_transfers_reply(self, session: Session) -> Reply:
        return Reply(
            text="⚙ Пересадки",
            keyboard=[
                [("Только прямые", f"adv:{session.dialog_id}:tr_0")],
                [("До 1", f"adv:{session.dialog_id}:tr_1")],
                [("До 2", f"adv:{session.dialog_id}:tr_2")],
                [("Не важно", f"adv:{session.dialog_id}:tr_any")],
                [("⬅ Назад", f"adv:{session.dialog_id}:menu")],
            ],
            edit=True,
        )

    def _adv_duration_reply(self, session: Session) -> Reply:
        return Reply(
            text="⚙ Время в пути",
            keyboard=[
                [("До 8 ч", f"adv:{session.dialog_id}:dur_8")],
                [("До 15 ч", f"adv:{session.dialog_id}:dur_15")],
                [("До 24 ч", f"adv:{session.dialog_id}:dur_24")],
                [("Не важно", f"adv:{session.dialog_id}:dur_any")],
                [("⬅ Назад", f"adv:{session.dialog_id}:menu")],
            ],
            edit=True,
        )

    def _adv_depart_reply(self, session: Session) -> Reply:
        labels = {
            "morning": "утро 06–12",
            "day": "день 12–18",
            "evening": "вечер 18–24",
            "night": "ночь 00–06",
        }
        rows: list[list[tuple[str, str]]] = []
        for part in DAY_PARTS:
            lbl = labels.get(part, part)
            if part in session.criteria.depart_parts:
                lbl = f"✓ {lbl}"
            rows.append([(lbl, f"adv:{session.dialog_id}:dp_{part}")])
        rows.append([("Не важно", f"adv:{session.dialog_id}:dp_any")])
        rows.append([("⬅ Назад", f"adv:{session.dialog_id}:menu")])
        return Reply(text="⚙ Время вылета", keyboard=rows, edit=True)

    def _adv_arrive_reply(self, session: Session) -> Reply:
        labels = {
            "morning": "утро 06–12",
            "day": "день 12–18",
            "evening": "вечер 18–24",
            "night": "ночь 00–06",
        }
        rows: list[list[tuple[str, str]]] = []
        for part in DAY_PARTS:
            lbl = labels.get(part, part)
            if part in session.criteria.arrive_parts:
                lbl = f"✓ {lbl}"
            rows.append([(lbl, f"adv:{session.dialog_id}:ap_{part}")])
        rows.append([("Не важно", f"adv:{session.dialog_id}:ap_any")])
        rows.append([("⬅ Назад", f"adv:{session.dialog_id}:menu")])
        return Reply(text="⚙ Время прилёта", keyboard=rows, edit=True)

    def _adv_balance_reply(self, session: Session) -> Reply:
        text = (
            "⚙ Баланс цены и времени\n\n"
            "Цена + стоимость часа в пути, показывается отдельная карточка «Лучший баланс»."
        )
        keyboard = [
            [("500 ₽/ч", f"adv:{session.dialog_id}:hc_500")],
            [("1000 ₽/ч", f"adv:{session.dialog_id}:hc_1000")],
            [("2000 ₽/ч", f"adv:{session.dialog_id}:hc_2000")],
            [("Выключить", f"adv:{session.dialog_id}:hc_off")],
            [("⬅ Назад", f"adv:{session.dialog_id}:menu")],
        ]
        return Reply(text=text, keyboard=keyboard, edit=True)

    def _on_adv_callback(self, session: Session, value: str) -> Reply:
        if value == "menu":
            return self._adv_menu_reply(session)
        if value == "s_tr":
            return self._adv_transfers_reply(session)
        if value == "s_dur":
            return self._adv_duration_reply(session)
        if value == "s_dp":
            return self._adv_depart_reply(session)
        if value == "s_ap":
            return self._adv_arrive_reply(session)
        if value == "s_hc":
            return self._adv_balance_reply(session)
        if value == "reset":
            session.criteria = SearchCriteria()
            return self._adv_menu_reply(session)
        if value == "done":
            return self._baggage_reply(session, edit=True)

        if value == "tr_any":
            session.criteria = replace(session.criteria, max_transfers=None)
            return self._adv_menu_reply(session)
        if value.startswith("tr_"):
            try:
                num = int(value.removeprefix("tr_"))
                if num in ALLOWED_MAX_TRANSFERS:
                    session.criteria = replace(session.criteria, max_transfers=num)
                    return self._adv_menu_reply(session)
            except ValueError:
                pass
            return self._adv_menu_reply(session)

        if value == "dur_any":
            session.criteria = replace(session.criteria, max_duration_hours=None)
            return self._adv_menu_reply(session)
        if value.startswith("dur_"):
            try:
                hours = int(value.removeprefix("dur_"))
                if hours in (8, 15, 24):
                    session.criteria = replace(session.criteria, max_duration_hours=hours)
                    return self._adv_menu_reply(session)
            except ValueError:
                pass
            return self._adv_menu_reply(session)

        if value == "dp_any":
            session.criteria = replace(session.criteria, depart_parts=frozenset())
            return self._adv_depart_reply(session)
        if value.startswith("dp_"):
            part = value.removeprefix("dp_")
            if part in ALLOWED_DAY_PARTS:
                cur = set(session.criteria.depart_parts)
                if part in cur:
                    cur.remove(part)
                else:
                    cur.add(part)
                session.criteria = replace(session.criteria, depart_parts=frozenset(cur))
                return self._adv_depart_reply(session)
            return self._adv_menu_reply(session)

        if value == "ap_any":
            session.criteria = replace(session.criteria, arrive_parts=frozenset())
            return self._adv_arrive_reply(session)
        if value.startswith("ap_"):
            part = value.removeprefix("ap_")
            if part in ALLOWED_DAY_PARTS:
                cur = set(session.criteria.arrive_parts)
                if part in cur:
                    cur.remove(part)
                else:
                    cur.add(part)
                session.criteria = replace(session.criteria, arrive_parts=frozenset(cur))
                return self._adv_arrive_reply(session)
            return self._adv_menu_reply(session)

        if value in ("hc_off", "hc_any"):
            session.criteria = replace(session.criteria, hour_cost=None)
            return self._adv_menu_reply(session)
        if value.startswith("hc_"):
            try:
                cost = int(value.removeprefix("hc_"))
                if cost in (500, 1000, 2000):
                    session.criteria = replace(session.criteria, hour_cost=cost)
                    return self._adv_menu_reply(session)
            except ValueError:
                pass
            return self._adv_menu_reply(session)

        return self._adv_menu_reply(session)

    def _sug_code(self, sugg: str) -> str:
        try:
            return to_iata(sugg)
        except Exception:
            return sugg[:10]

    def _expired_reply(self) -> Reply:
        return Reply(
            text="Диалог устарел, начните заново",
            keyboard=[[("🔎 Найти билет", "menu:go")]],
            answer="Диалог устарел, начните заново",
        )

    def on_callback(self, chat_id: int | str, data: str) -> list[Reply | SearchRequest]:
        if data == "menu:go":
            return [self._start_search(chat_id)]
        if data == "menu:track":
            return [
                Reply(
                    text="🔔 Раздел «Мои отслеживания» скоро появится.",
                    answer="Скоро",
                    keyboard=[[("🔎 Найти билет", "menu:go")]],
                )
            ]
        if data == "menu:help":
            return [
                Reply(
                    text=HELP,
                    answer="Помощь",
                    keyboard=[[("🔎 Найти билет", "menu:go")]],
                )
            ]
        if data == "menu:menu":
            return [self.start_menu(chat_id)]

        parts = data.split(":")
        action = parts[0]
        cb_dialog_id = parts[1] if len(parts) > 1 else ""
        value = parts[2] if len(parts) > 2 else ""

        session = self.sessions.get(chat_id)
        if session is None or session.dialog_id != cb_dialog_id:
            return [self._expired_reply()]

        if action == "cancel":
            self.sessions.pop(chat_id, None)
            return [self.start_menu(chat_id)]

        if action == "sug":
            iata = value
            if session.step == Step.ORIGIN:
                session.origin = iata
                session.step = Step.DEST
                return [
                    Reply(
                        text="Куда? Укажите город или код IATA:",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]
            elif session.step == Step.DEST:
                if iata == session.origin:
                    return [
                        Reply(
                            text=f"⚠️ {escape('города вылета и прилёта совпадают')}\n\nКуда? Укажите город или код IATA:",
                            keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                        )
                    ]
                session.destination = iata
                session.step = Step.DATE
                return [
                    Reply(
                        text="Дата вылета (12.12, 12.12.2026, 2026-12-12):",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]
            return []

        if action == "trip":
            if value == "one":
                session.return_date = None
                session.step = Step.BAGGAGE
                return [self._baggage_reply(session, edit=False)]
            elif value == "rt":
                session.step = Step.RETURN_DATE
                return [
                    Reply(
                        text="Дата возвращения (12.12, 12.12.2026, 2026-12-12):",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]
            return []

        if action == "pax":
            if session.step == Step.BAGGAGE:
                session.adults = (session.adults % 9) + 1
                return [self._baggage_reply(session, edit=True)]
            return []

        if action == "adv":
            if session.step != Step.BAGGAGE:
                return []
            if value == "cancel":
                self.sessions.pop(chat_id, None)
                return [self.start_menu(chat_id)]
            return [self._on_adv_callback(session, value)]

        if action == "bag":
            mode = value if value in {"with", "without", "any"} else "any"
            session.baggage_mode = mode
            if session.return_date is not None and not ROUND_TRIP_SITES:
                return [
                    Reply(
                        text="Обратные билеты скоро: пока ищу только в одну сторону",
                        keyboard=[
                            [("Искать только туда", f"go:{session.dialog_id}")],
                            [("✖ Отмена", f"cancel:{session.dialog_id}")],
                        ],
                        edit=True,
                    )
                ]
            session.step = Step.SEARCHING
            q = Query(
                origin=session.origin or "",
                destination=session.destination or "",
                depart=session.depart or self.today(),
                adults=session.adults,
                return_date=session.return_date,
            )
            return [
                SearchRequest(
                    query=q,
                    baggage_mode=session.baggage_mode,
                    criteria=session.criteria,
                )
            ]

        if action == "go":
            session.return_date = None
            session.step = Step.SEARCHING
            q = Query(
                origin=session.origin or "",
                destination=session.destination or "",
                depart=session.depart or self.today(),
                adults=session.adults,
                return_date=None,
            )
            return [
                SearchRequest(
                    query=q,
                    baggage_mode=session.baggage_mode,
                    criteria=session.criteria,
                )
            ]

        if action == "track":
            return [Reply(answer="Отслеживание появится позже")]

        if action == "new":
            return [self._start_search(chat_id)]

        if action == "more":
            snap = self.store.get(chat_id)
            if snap is None:
                return [
                    Reply(
                        text="Результаты поиска устарели, начните новый поиск",
                        keyboard=[[("🔎 Найти билет", "menu:go")]],
                    )
                ]
            text, new_offers = build_more_report(
                snap.query,
                snap.pool,
                snap.flat,
                n=5,
                start_number=len(snap.flat) + 1,
            )
            if not text or not new_offers:
                return []
            snap.flat.extend(new_offers)
            snap.shown += len(new_offers)
            shown_ids = {o.offer_id for o in snap.flat if o.offer_id}
            has_more = any(o.offer_id not in shown_ids for o in snap.pool)
            kb = compact_keyboard(session.dialog_id, has_more=has_more)
            return [Reply(text=text, keyboard=kb, edit=False)]

        if action == "full":
            snap = self.store.get(chat_id)
            if snap is None:
                return [
                    Reply(
                        text="Результаты поиска устарели, начните новый поиск",
                        keyboard=[[("🔎 Найти билет", "menu:go")]],
                    )
                ]
            chunks, full_flat = build_report(
                snap.query,
                snap.results,
                baggage_mode=snap.baggage_mode,
                dropped=snap.dropped_baggage,
            )
            snap.flat = full_flat
            snap.view = "full"
            kb = full_keyboard(session.dialog_id)
            replies = [Reply(text=chunk, edit=False) for chunk in chunks[:-1]]
            replies.append(Reply(text=chunks[-1], keyboard=kb, edit=False))
            return replies

        if action == "list":
            snap = self.store.get(chat_id)
            if snap is None:
                return [
                    Reply(
                        text="Результаты поиска устарели, начните новый поиск",
                        keyboard=[[("🔎 Найти билет", "menu:go")]],
                    )
                ]
            target_view = "compact" if (value == "compact" or snap.view == "compact") else "full"
            if target_view == "compact":
                snap.view = "compact"
                chunks, compact_flat, pool = build_compact_report(
                    snap.query,
                    snap.results,
                    snap.criteria,
                    baggage_mode=snap.baggage_mode,
                    dropped_baggage=snap.dropped_baggage,
                    dropped_criteria=snap.dropped_criteria,
                )
                snap.flat = compact_flat
                snap.pool = pool
                snap.shown = len(compact_flat)
                shown_ids = {o.offer_id for o in compact_flat if o.offer_id}
                has_more = any(o.offer_id not in shown_ids for o in pool)
                kb = compact_keyboard(session.dialog_id, has_more=has_more)
                replies = [Reply(text=chunk, edit=False) for chunk in chunks[:-1]]
                replies.append(Reply(text=chunks[-1], keyboard=kb, edit=False))
                return replies
            else:
                snap.view = "full"
                chunks, full_flat = build_report(
                    snap.query,
                    snap.results,
                    baggage_mode=snap.baggage_mode,
                    dropped=snap.dropped_baggage,
                )
                snap.flat = full_flat
                kb = full_keyboard(session.dialog_id)
                replies = [Reply(text=chunk, edit=False) for chunk in chunks[:-1]]
                replies.append(Reply(text=chunks[-1], keyboard=kb, edit=False))
                return replies

        if action == "filters":
            snap = self.store.get(chat_id)
            if snap is None:
                return [
                    Reply(
                        text="Результаты поиска устарели, начните новый поиск",
                        keyboard=[[("🔎 Найти билет", "menu:go")]],
                    )
                ]
            new_session = Session(
                step=Step.BAGGAGE,
                origin=snap.query.origin,
                destination=snap.query.destination,
                depart=snap.query.depart,
                return_date=snap.query.return_date,
                adults=snap.query.adults,
                baggage_mode=snap.baggage_mode,
                criteria=snap.criteria,
                dialog_id=secrets.token_hex(2),
            )
            self.sessions[chat_id] = new_session
            return [self._baggage_reply(new_session, edit=False)]

        return []

    def on_text(self, chat_id: int | str, text: str) -> list[Reply | SearchRequest]:
        text = text.strip()

        if text.startswith(("/start", "/menu")):
            return [self.start_menu(chat_id)]

        if text == "/cancel":
            self.sessions.pop(chat_id, None)
            return [self.start_menu(chat_id)]

        if text == "/help":
            return [Reply(text=HELP, keyboard=[[("🔎 Найти билет", "menu:go")]])]

        if text.startswith("/price"):
            args = text.removeprefix("/price").strip()
            try:
                q = parse_query(args, today=self.today())
                self.sessions[chat_id] = Session(
                    step=Step.SEARCHING,
                    origin=q.origin,
                    destination=q.destination,
                    depart=q.depart,
                    adults=q.adults,
                    dialog_id=secrets.token_hex(2),
                )
                return [SearchRequest(query=q, baggage_mode="any")]
            except ParseError as e:
                return [Reply(text=f"⚠️ {escape(str(e))}")]

        session = self.sessions.get(chat_id)

        if session is not None and session.step == Step.ORIGIN:
            try:
                iata = to_iata(text)
                session.origin = iata
                session.step = Step.DEST
                return [
                    Reply(
                        text="Куда? Укажите город или код IATA:",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]
            except UnknownCityError as e:
                kb = [
                    [(sugg, f"sug:{session.dialog_id}:{self._sug_code(sugg)}")]
                    for sugg in e.suggestions
                ] + [[("✖ Отмена", f"cancel:{session.dialog_id}")]]
                return [
                    Reply(
                        text=f"⚠️ {escape(str(e))}\n\nОткуда? Укажите город или код IATA:",
                        keyboard=kb,
                    )
                ]
            except ParseError as e:
                return [
                    Reply(
                        text=f"⚠️ {escape(str(e))}\n\nОткуда? Укажите город или код IATA:",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]

        if session is not None and session.step == Step.DEST:
            try:
                iata = to_iata(text)
                if iata == session.origin:
                    raise ParseError("города вылета и прилёта совпадают")
                session.destination = iata
                session.step = Step.DATE
                return [
                    Reply(
                        text="Дата вылета (12.12, 12.12.2026, 2026-12-12):",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]
            except UnknownCityError as e:
                kb = [
                    [(sugg, f"sug:{session.dialog_id}:{self._sug_code(sugg)}")]
                    for sugg in e.suggestions
                ] + [[("✖ Отмена", f"cancel:{session.dialog_id}")]]
                return [
                    Reply(
                        text=f"⚠️ {escape(str(e))}\n\nКуда? Укажите город или код IATA:",
                        keyboard=kb,
                    )
                ]
            except ParseError as e:
                return [
                    Reply(
                        text=f"⚠️ {escape(str(e))}\n\nКуда? Укажите город или код IATA:",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]

        if session is not None and session.step == Step.DATE:
            try:
                d = parse_date(text, today=self.today())
                session.depart = d
                session.step = Step.TRIP
                return [
                    Reply(
                        text="Нужен обратный билет?",
                        keyboard=[
                            [
                                ("Только туда", f"trip:{session.dialog_id}:one"),
                                ("Туда и обратно", f"trip:{session.dialog_id}:rt"),
                            ],
                            [("✖ Отмена", f"cancel:{session.dialog_id}")],
                        ],
                    )
                ]
            except ParseError as e:
                return [
                    Reply(
                        text=f"⚠️ {escape(str(e))}\n\nДата вылета (12.12, 12.12.2026, 2026-12-12):",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]

        if session is not None and session.step == Step.TRIP:
            return [
                Reply(
                    text="Пожалуйста, выберите вариант с помощью кнопок:\nНужен обратный билет?",
                    keyboard=[
                        [
                            ("Только туда", f"trip:{session.dialog_id}:one"),
                            ("Туда и обратно", f"trip:{session.dialog_id}:rt"),
                        ],
                        [("✖ Отмена", f"cancel:{session.dialog_id}")],
                    ],
                )
            ]

        if session is not None and session.step == Step.RETURN_DATE:
            try:
                ret_d = parse_return_date(text, session.depart or self.today(), today=self.today())
                session.return_date = ret_d
                session.step = Step.BAGGAGE
                return [self._baggage_reply(session, edit=False)]
            except ParseError as e:
                return [
                    Reply(
                        text=f"⚠️ {escape(str(e))}\n\nДата возвращения (12.12, 12.12.2026, 2026-12-12):",
                        keyboard=[[("✖ Отмена", f"cancel:{session.dialog_id}")]],
                    )
                ]

        if session is not None and session.step == Step.BAGGAGE:
            return [
                Reply(
                    text="Пожалуйста, выберите параметры багажа с помощью кнопок:\nБагаж?",
                    keyboard=self._baggage_reply(session).keyboard,
                )
            ]

        if session is not None and session.step == Step.SEARCHING:
            return [Reply(text="Поиск ещё идёт, подождите завершения.")]

        # Outside dialog: step MENU, DONE or no session
        snap = self.store.get(chat_id)
        is_num = text.isdigit() or (text.startswith("-") and text[1:].isdigit())

        if is_num:
            if snap is None:
                return [
                    Reply(
                        text="Результаты поиска устарели, начните новый поиск",
                        keyboard=[[("🔎 Найти билет", "menu:go")]],
                    )
                ]
            n = int(text)
            offer = snap.offer_by_number(n)
            if offer is not None:
                if session is None or not session.dialog_id:
                    session = Session(step=Step.DONE, dialog_id=secrets.token_hex(2))
                    self.sessions[chat_id] = session
                card = format_details(offer, snap.query)
                return [
                    Reply(
                        text=card,
                        keyboard=[
                            [
                                ("⬅ К списку", f"list:{session.dialog_id}"),
                                ("🔔 Отслеживать цену", f"track:{session.dialog_id}"),
                            ],
                            [("🔎 Новый поиск", f"new:{session.dialog_id}")],
                        ],
                    )
                ]
            return [Reply(text=f"Нет варианта с таким номером, всего: {len(snap.flat)}")]

        if snap is not None:
            return [Reply(text=f"Нет варианта с таким номером, всего: {len(snap.flat)}")]

        return [self.start_menu(chat_id)]
