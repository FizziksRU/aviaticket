"""Telegram-бот на long polling (только httpx, без лишних зависимостей)."""
from __future__ import annotations

import asyncio
from html import escape
import logging
import secrets
import time

import httpx

from .config import Settings, load_settings
from .dialog import (
    HELP,
    Dialog,
    Reply,
    SearchRequest,
    Session,
    Step,
    compact_keyboard,
    to_reply_markup,
)
from .filters import apply_criteria
from .formatting import build_compact_report, build_report, format_details
from .guard import friendly_error
from .health import Health
from .history import PriceHistory
from .models import SiteResult
from .offers import assign_ids, filter_by_baggage
from .pipeline import RT_UNSUPPORTED, search
from .scheduler import Scheduler
from .store import ResultStore
from .subscriptions import SubscriptionStore
from .users import UserStore

log = logging.getLogger("avia.bot")
API = "https://api.telegram.org/bot{token}/{method}"


def _site_brief(r: SiteResult) -> str:
    """kupibilet:ok/200/6s, aviasales:err/0/46s, tutu:cache/10/0s — для строки лога поиска."""
    state = "cache" if r.cached else ("partial" if r.ok and r.partial else ("ok" if r.ok else "err"))
    return f"{r.site}:{state}/{len(r.offers)}/{r.elapsed:.0f}s"


class Bot:
    def __init__(
        self,
        settings: Settings,
        store: ResultStore | None = None,
        dialog: Dialog | None = None,
    ):
        if not settings.tg_token:
            raise SystemExit("Задайте TG_BOT_TOKEN в .env")
        if not settings.tg_allowed_ids and not settings.tg_open:
            raise SystemExit("Задайте TG_ALLOWED_IDS в .env (ваш Telegram id) или TG_OPEN=1, иначе некому отвечать")
        self.s = settings
        self.http = httpx.AsyncClient(timeout=70)
        self.busy: set[int] = set()
        self.users = UserStore(settings.storage_dir / "users.sqlite")
        self._search_sem = asyncio.Semaphore(settings.max_parallel_searches)
        self.health = Health(skip_errors=(RT_UNSUPPORTED,))
        self._alerts: set[asyncio.Task] = set()  # алерты админам не держат выдачу человеку
        self._queued = 0   # ждут слота поиска
        self._active = 0   # ищут сейчас
        self.history = PriceHistory(settings.storage_dir / "history.sqlite")
        if dialog is not None:
            self.dialog = dialog
            self.store = store if store is not None else dialog.store
            self.subs = dialog.subs
        else:
            self.subs = SubscriptionStore(settings.storage_dir / "subscriptions.sqlite")
            self.store = store if store is not None else ResultStore()
            self.dialog = Dialog(store=self.store, subs=self.subs)

    async def call(self, method: str, **params):
        payload = {k: v for k, v in params.items() if v is not None}
        r = await self.http.post(API.format(token=self.s.tg_token, method=method), json=payload)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data}")
        return data["result"]

    async def send(self, chat_id: int, text: str, reply_markup: dict | None = None):
        if len(text) <= 4096:
            params: dict = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                params["reply_markup"] = reply_markup
            return await self.call("sendMessage", **params)

        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in text.splitlines(keepends=True):
            if current_len + len(line) > 4096:
                if current:
                    chunks.append("".join(current))
                    current = []
                    current_len = 0
                while len(line) > 4096:
                    chunks.append(line[:4096])
                    line = line[4096:]
            current.append(line)
            current_len += len(line)
        if current:
            chunks.append("".join(current))

        last_res = None
        for i, chunk in enumerate(chunks):
            params = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if i == len(chunks) - 1 and reply_markup is not None:
                params["reply_markup"] = reply_markup
            last_res = await self.call("sendMessage", **params)
        return last_res

    async def _process_actions(
        self,
        chat_id: int,
        actions: list[Reply | SearchRequest],
        message_id: int | None = None,
    ):
        for item in actions:
            if isinstance(item, Reply):
                reply_markup = to_reply_markup(item.keyboard)
                if item.edit and message_id is not None:
                    try:
                        await self.call(
                            "editMessageText",
                            chat_id=chat_id,
                            message_id=message_id,
                            text=item.text,
                            parse_mode="HTML",
                            reply_markup=reply_markup,
                        )
                    except Exception:
                        await self.send(chat_id, item.text, reply_markup=reply_markup)
                else:
                    if item.text:
                        await self.send(chat_id, item.text, reply_markup=reply_markup)
            elif isinstance(item, SearchRequest):
                await self._run_search(chat_id, item)

    async def _run_search(self, chat_id: int, req: SearchRequest):
        if chat_id in self.busy:
            await self.send(chat_id, "Предыдущий поиск ещё идёт, подождите.")
            return
        if (
            chat_id not in self.s.tg_allowed_ids
            and not self.users.try_use_search(chat_id, self.s.daily_search_limit)
        ):
            await self.send(
                chat_id,
                f"На сегодня лимит поисков исчерпан ({self.s.daily_search_limit}). Завтра можно снова; "
                "отслеживания продолжают работать.",
            )
            return
        self.busy.add(chat_id)
        q = req.query
        baggage_mode = req.baggage_mode
        wait_msg_id = None
        try:
            wait_msg = await self.send(
                chat_id,
                f"Ищу {q.origin} → {q.destination} на {q.depart:%d.%m}, это займёт около минуты…",
            )
            if isinstance(wait_msg, dict):
                wait_msg_id = wait_msg.get("message_id")

            results = await self._timed_search(q, kind="user")
            results = assign_ids(results)
            try:
                await asyncio.to_thread(self.history.annotate, q, results)
            except Exception:
                log.exception("история цен недоступна")
            filtered_crit, dropped_criteria = apply_criteria(results, req.criteria)
            filtered, dropped_baggage = filter_by_baggage(filtered_crit, baggage_mode)
            chunks, flat, pool = build_compact_report(
                q,
                filtered,
                req.criteria,
                baggage_mode=baggage_mode,
                dropped_baggage=dropped_baggage,
                dropped_criteria=dropped_criteria,
            )

            session = self.dialog.sessions.get(chat_id)
            if session is None or not session.dialog_id:
                session = Session(
                    step=Step.DONE,
                    origin=q.origin,
                    destination=q.destination,
                    depart=q.depart,
                    return_date=q.return_date,
                    adults=q.adults,
                    baggage_mode=baggage_mode,
                    criteria=req.criteria,
                    dialog_id=secrets.token_hex(2),
                )
                self.dialog.sessions[chat_id] = session
            else:
                session.step = Step.DONE

            d_id = session.dialog_id
            shown_ids = {o.offer_id for o in flat if o.offer_id}
            has_more = any(o.offer_id not in shown_ids for o in pool)
            kb = compact_keyboard(d_id, has_more=has_more)
            reply_markup = to_reply_markup(kb)

            for i, chunk in enumerate(chunks):
                rm = reply_markup if i == len(chunks) - 1 else None
                await self.send(chat_id, chunk, reply_markup=rm)

            self.store.put(
                chat_id,
                q,
                filtered,
                baggage_mode,
                flat,
                criteria=req.criteria,
                pool=pool,
                shown=len(flat),
                view="compact",
                dropped_baggage=dropped_baggage,
                dropped_criteria=dropped_criteria,
            )
        except Exception as e:
            log.exception("search failed")
            await self.send(chat_id, f"⚠️ {escape(friendly_error(e))}")
        finally:
            self.busy.discard(chat_id)
            if wait_msg_id is not None:
                try:
                    await self.call("deleteMessage", chat_id=chat_id, message_id=wait_msg_id)
                except Exception:
                    try:
                        await self.call("editMessageText", chat_id=chat_id, message_id=wait_msg_id, text="Готово")
                    except Exception:
                        pass

    def _admit(self, user: dict, chat: dict | None) -> bool:
        """Пускает администраторов и (в открытом режиме) любого человека из личного чата; регистрирует в users."""
        user_id = user.get("id")
        if not isinstance(user_id, int) or user.get("is_bot"):
            return False
        is_admin = user_id in self.s.tg_allowed_ids
        if not is_admin:
            if not self.s.tg_open:
                return False
            # подписки привязаны к tg id = id личного чата, группы и каналы не обслуживаем
            if chat is not None and chat.get("type", "private") != "private":
                return False
        try:
            rec = self.users.register(user_id, user.get("username"), user.get("first_name"))
        except Exception:
            log.exception("не удалось записать пользователя")
            return is_admin
        return is_admin or not rec.blocked

    async def handle(self, msg: dict):
        chat_id = msg["chat"]["id"]
        if not self._admit(msg.get("from", {}), msg.get("chat")):
            return
        text = (msg.get("text") or "").strip()
        if text.startswith(("/stats", "/status", "/block", "/unblock")):
            if msg.get("from", {}).get("id") in self.s.tg_allowed_ids:
                await self._admin_command(chat_id, text)
            return
        actions = self.dialog.on_text(chat_id, text)
        await self._process_actions(chat_id, actions)

    async def _admin_command(self, chat_id: int, text: str) -> None:
        """/stats — сводка и последние люди; /block <id>, /unblock <id> — закрыть или вернуть доступ."""
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0]
        if cmd == "/status":
            await self.send(chat_id, self._status_text())
            return
        if cmd == "/stats":
            st = self.users.stats()
            lines = [
                f"Людей: {st['users']} (заблокировано {st['blocked']})",
                f"Поисков сегодня: {st['today']}, всего: {st['searches']}",
                "",
                "Последние:",
            ]
            for u in self.users.recent(10):
                name = f"@{u.username}" if u.username else (u.first_name or "—")
                mark = " 🚫" if u.blocked else ""
                lines.append(f"{u.tg_id} {escape(name)} · поисков {u.total_searches}{mark}")
            await self.send(chat_id, "\n".join(lines))
            return
        arg = arg.strip()
        if not arg.lstrip("-").isdigit():
            await self.send(chat_id, f"Формат: {cmd} <tg id>")
            return
        target = int(arg)
        if target in self.s.tg_allowed_ids:
            await self.send(chat_id, "Администратора заблокировать нельзя.")
            return
        ok = self.users.set_blocked(target, cmd == "/block")
        if not ok:
            await self.send(chat_id, "Такого пользователя нет в базе.")
        else:
            await self.send(chat_id, f"{target}: {'заблокирован' if cmd == '/block' else 'разблокирован'}")

    async def handle_callback(self, cq: dict):
        user_id = cq.get("from", {}).get("id")
        msg = cq.get("message") or {}
        if not self._admit(cq.get("from", {}), msg.get("chat")):
            return
        cq_id = cq["id"]
        chat_id = msg.get("chat", {}).get("id")
        if chat_id is None:
            chat_id = user_id
        message_id = msg.get("message_id")
        data = cq.get("data", "")

        answered = False
        try:
            actions = self.dialog.on_callback(chat_id, data)
            answer_text = None
            for item in actions:
                if isinstance(item, Reply) and item.answer:
                    answer_text = item.answer
                    break

            if answer_text:
                await self.call("answerCallbackQuery", callback_query_id=cq_id, text=answer_text)
            else:
                await self.call("answerCallbackQuery", callback_query_id=cq_id)
            answered = True

            await self._process_actions(chat_id, actions, message_id=message_id)
        finally:
            if not answered:
                try:
                    await self.call("answerCallbackQuery", callback_query_id=cq_id)
                except Exception:
                    pass

    async def _scheduler_search(self, q):
        # телефоны общие: фоновый опрос стоит в той же очереди, что и поиски людей
        return await self._timed_search(q, kind="scheduler")

    async def _timed_search(self, q, *, kind: str) -> list[SiteResult]:
        """Поиск через общий слот с учётом в /status, строкой лога и сигналом о сломанных сайтах."""
        t0 = time.monotonic()
        self._queued += 1
        acquired = False
        try:
            async with self._search_sem:
                self._queued -= 1
                acquired = True
                self._active += 1
                t_start = time.monotonic()
                try:
                    results = await search(q, self.s)
                finally:
                    self._active -= 1
        finally:
            if not acquired:
                self._queued -= 1
        waited, took = t_start - t0, time.monotonic() - t_start
        # «от нажатия до выдачи» — только для людей: подписки при SEARCH_PRIORITY=1 ждут долго намеренно
        broken = self.health.record(results, kind=kind, seconds=waited + took if kind == "user" else None)
        log.info(
            "search kind=%s %s-%s %s rt=%s wait=%.1fs took=%.1fs sites=%s",
            kind, q.origin, q.destination, q.depart, q.return_date or "-", waited, took,
            " ".join(_site_brief(r) for r in results),
        )
        if broken:
            task = asyncio.create_task(self._alert_broken(broken))
            self._alerts.add(task)
            task.add_done_callback(self._alerts.discard)
        return results

    async def _alert_broken(self, sites: list[str]) -> None:
        stats = {st.site: st for st in self.health.stats()}
        for site in sites:
            st = stats[site]
            log.warning("сайт %s: %d сборов подряд без результата (%s)", site, st.failures_in_row, st.last_error)
        if not self.s.health_alerts:
            return
        lines = ["⚠️ Источники перестали отвечать:"]
        for site in sites:
            st = stats[site]
            lines.append(f"• {escape(site)}: {st.failures_in_row} раза подряд, {escape(st.last_error or 'без текста ошибки')}")
        lines.append("Подробности: /status")
        for admin in self.s.tg_allowed_ids:
            try:
                await self.send(admin, "\n".join(lines))
            except Exception:
                log.exception("не удалось отправить сигнал о сломанном сайте админу %s", admin)

    def _status_text(self) -> str:
        h = self.health
        up_min = int((h.clock() - h.started_at) // 60)
        lines = [
            f"Работает {up_min} мин. Поиски: людей {h.counters['searches_user']}, "
            f"подписок {h.counters['searches_scheduler']}, ответов сайтов из кэша {h.counters['site_cache_hits']}.",
            f"Сейчас ищут {self._active} из {self.s.max_parallel_searches}, в очереди {self._queued}, "
            f"ждут выдачу {len(self.busy)} чел.",
        ]
        pct = h.search_percentiles()
        if pct is not None:
            lines.append(f"От нажатия до выдачи: p50 {pct[0]:.0f} с, p95 {pct[1]:.0f} с")
        lines.append("")
        stats = h.stats()
        if not stats:
            lines.append("Сборов пока не было.")
        for st in stats:
            mark = "🔴" if st.failures_in_row >= h.alert_after else ("🟡" if st.ok < st.total else "🟢")
            line = f"{mark} {escape(st.site)}: {st.ok}/{st.total} ok, partial {st.partial}, {st.avg_elapsed:.0f} с"
            if st.failures_in_row:
                line += f", подряд без результата {st.failures_in_row}: {escape(st.last_error)}"
            lines.append(line)
        return "\n".join(lines)

    async def _scheduler_notify(self, chat_id: int, text: str, reply_markup: dict | None) -> None:
        await self.send(chat_id, text, reply_markup=reply_markup)

    async def run(self):
        offset = 0
        log.info("bot started")
        if self.subs is not None:
            scheduler = Scheduler(
                self.subs,
                self._scheduler_search,
                self._scheduler_notify,
                history=self.history,
            )
            self._scheduler_task = asyncio.create_task(scheduler.run_forever())
        while True:
            try:
                updates = await self.call(
                    "getUpdates",
                    offset=offset,
                    timeout=50,
                    allowed_updates=["message", "callback_query"],
                )
            except Exception:
                log.exception("getUpdates")
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                if "message" in u:
                    asyncio.create_task(self.handle(u["message"]))
                elif "callback_query" in u:
                    asyncio.create_task(self.handle_callback(u["callback_query"]))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # httpx на уровне INFO пишет полный URL запроса, а в нём токен бота
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    asyncio.run(Bot(load_settings()).run())


if __name__ == "__main__":
    main()
