from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from html import escape
import secrets

from .formatting import build_report, format_details
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


def to_reply_markup(keyboard: list[list[tuple[str, str]]] | None) -> dict | None:
    if not keyboard:
        return None
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": cb} for text, cb in row]
            for row in keyboard
        ]
    }


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
        return Reply(
            text="Багаж?",
            keyboard=[
                [
                    ("С багажом", f"bag:{session.dialog_id}:with"),
                    ("Без багажа", f"bag:{session.dialog_id}:without"),
                    ("Не важно", f"bag:{session.dialog_id}:any"),
                ],
                [(f"Пассажиров: {session.adults}", f"pax:{session.dialog_id}:+")],
                [("✖ Отмена", f"cancel:{session.dialog_id}")],
            ],
            edit=edit,
        )

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
            return [SearchRequest(query=q, baggage_mode=session.baggage_mode)]

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
            return [SearchRequest(query=q, baggage_mode=session.baggage_mode)]

        if action == "track":
            return [Reply(answer="Отслеживание появится позже")]

        if action == "new":
            return [self._start_search(chat_id)]

        if action == "list":
            snap = self.store.get(chat_id)
            if snap is None:
                return [
                    Reply(
                        text="Результаты поиска устарели, начните новый поиск",
                        keyboard=[[("🔎 Найти билет", "menu:go")]],
                    )
                ]
            chunks, _ = build_report(
                snap.query,
                snap.results,
                baggage_mode=snap.baggage_mode,
            )
            return [Reply(text=chunk) for chunk in chunks]

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
