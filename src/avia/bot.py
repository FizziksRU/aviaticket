"""Telegram-бот на long polling (только httpx, без лишних зависимостей)."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from html import escape
import logging
import secrets
import signal
import time
from typing import Any

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
from . import pipeline as pipeline_mod
from .pipeline import RT_UNSUPPORTED, search
from .queueing import BACKGROUND, INTERACTIVE, PrioritySlots
from .scheduler import Scheduler
from .store import ResultStore
from .subscriptions import SubscriptionStore
from .tg_text import html_to_plain, split_html
from .users import UserStore

log = logging.getLogger("avia.bot")
API = "https://api.telegram.org/bot{token}/{method}"
# 429 от Telegram: повторяем после retry_after, но не дольше минуты на попытку и не больше 3 раз
TG_MAX_RETRIES = 3
TG_MAX_RETRY_AFTER = 60.0
# сколько ждать идущие поиски и ответы при остановке, прежде чем отменить их
SHUTDOWN_GRACE_SECONDS = 30.0
SEARCH_AGAIN_KB = [[("🔎 Найти билет", "menu:go")]]
LIMIT_KB = [[("🔔 Мои отслеживания", "menu:track")]]


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
        self._sleep = asyncio.sleep
        self.busy: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        self._scheduler_task: asyncio.Task | None = None
        self.users = UserStore(settings.storage_dir / "users.sqlite")
        # общая очередь поисков людей и планировщика; SEARCH_PRIORITY=1 ставит людей вперёд
        self._search_sem = PrioritySlots(settings.max_parallel_searches)
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

    def _api_url(self, method: str) -> str:
        base = self.s.tg_api_base.strip().rstrip("/")
        if base:
            return f"{base}/bot{self.s.tg_token}/{method}"
        return API.format(token=self.s.tg_token, method=method)

    async def call(self, method: str, **params):
        payload = {k: v for k, v in params.items() if v is not None}
        for attempt in range(TG_MAX_RETRIES + 1):
            r = await self.http.post(self._api_url(method), json=payload)
            data = r.json()
            if data.get("ok"):
                return data["result"]
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if data.get("error_code") == 429 and retry_after is not None and attempt < TG_MAX_RETRIES:
                log.warning("telegram %s: 429, повтор через %s с", method, retry_after)
                await self._sleep(min(float(retry_after), TG_MAX_RETRY_AFTER))
                continue
            raise RuntimeError(f"telegram {method}: {data}")
        raise RuntimeError(f"telegram {method}: не удалось после {TG_MAX_RETRIES} повторов")

    async def send(self, chat_id: int, text: str, reply_markup: dict | None = None):
        chunks = split_html(text)
        last_res = None
        for i, chunk in enumerate(chunks):
            params: dict = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if i == len(chunks) - 1 and reply_markup is not None:
                params["reply_markup"] = reply_markup
            try:
                last_res = await self.call("sendMessage", **params)
            except RuntimeError as e:
                if "can't parse entities" not in str(e):
                    raise
                # разметка сломана (например, неэкранированный текст сайта): лучше без форматирования, чем ничего
                log.warning("telegram отверг HTML, отправляю текстом: %s", e)
                params.pop("parse_mode")
                params["text"] = html_to_plain(chunk)
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
            self.dialog.search_finished(chat_id, req.dialog_id)
            await self.send(chat_id, "Предыдущий поиск ещё идёт, подождите.")
            return
        charged = chat_id not in self.s.tg_allowed_ids
        if charged and not self.users.try_use_search(chat_id, self.s.daily_search_limit):
            self.dialog.search_finished(chat_id, req.dialog_id)
            await self.send(
                chat_id,
                f"На сегодня лимит поисков исчерпан ({self.s.daily_search_limit}). Завтра можно снова; "
                "отслеживания продолжают работать.",
                reply_markup=to_reply_markup(LIMIT_KB),
            )
            return
        self.busy.add(chat_id)
        q = req.query
        baggage_mode = req.baggage_mode
        wait_msg_id = None
        useful = False      # хотя бы один сайт ответил (пусть и «рейсов нет»)
        delivered = False   # хотя бы один кусок выдачи дошёл до человека
        try:
            wait_msg = await self.send(
                chat_id,
                f"Ищу {q.origin} → {q.destination} на {q.depart:%d.%m}, это займёт около минуты…",
            )
            if isinstance(wait_msg, dict):
                wait_msg_id = wait_msg.get("message_id")

            on_wait = self._queue_notice(chat_id, wait_msg_id, q) if self.s.queue_notice else None
            results = await self._timed_search(q, kind="user", priority=INTERACTIVE, on_wait=on_wait)
            useful = any(r.ok or r.empty for r in results)
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

            d_id = self._session_after_search(chat_id, req)
            shown_ids = {o.offer_id for o in flat if o.offer_id}
            has_more = any(o.offer_id not in shown_ids for o in pool)
            kb = compact_keyboard(d_id, has_more=has_more)
            reply_markup = to_reply_markup(kb)

            # снимок сохраняем до отправки: если упадёт второй кусок, кнопки первого всё равно работают
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
            for i, chunk in enumerate(chunks):
                rm = reply_markup if i == len(chunks) - 1 else None
                await self.send(chat_id, chunk, reply_markup=rm)
                delivered = True
        except Exception as e:
            log.exception("search failed")
            self.dialog.search_finished(chat_id, req.dialog_id)
            try:
                await self.send(chat_id, f"⚠️ {escape(friendly_error(e))}", reply_markup=to_reply_markup(SEARCH_AGAIN_KB))
            except Exception:
                log.exception("не удалось сообщить об ошибке поиска")
        finally:
            self.busy.discard(chat_id)
            if charged and not (useful and delivered):
                # ни один сайт не ответил, поиск упал или выдача не дошла: попытку не засчитываем
                try:
                    self.users.refund_search(chat_id)
                except Exception:
                    log.exception("не удалось вернуть попытку поиска")
            if wait_msg_id is not None:
                try:
                    await self.call("deleteMessage", chat_id=chat_id, message_id=wait_msg_id)
                except Exception:
                    try:
                        await self.call("editMessageText", chat_id=chat_id, message_id=wait_msg_id, text="Готово")
                    except Exception:
                        pass

    def _session_after_search(self, chat_id: int, req: SearchRequest) -> str:
        """Выводит сессию поиска в DONE и возвращает dialog_id для кнопок выдачи.

        Если человек за время поиска начал новый диалог, его сессию не трогаем:
        выдача уходит со своим dialog_id, и её кнопки ответят «устарели».
        """
        owned = self.dialog.search_finished(chat_id, req.dialog_id)
        if owned is not None and owned.dialog_id:
            return owned.dialog_id
        if req.dialog_id and chat_id in self.dialog.sessions:
            return req.dialog_id
        q = req.query
        session = Session(
            step=Step.DONE,
            origin=q.origin,
            destination=q.destination,
            depart=q.depart,
            return_date=q.return_date,
            adults=q.adults,
            baggage_mode=req.baggage_mode,
            criteria=req.criteria,
            dialog_id=req.dialog_id or secrets.token_hex(2),
        )
        self.dialog.sessions[chat_id] = session
        return session.dialog_id

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

            try:
                if answer_text:
                    await self.call("answerCallbackQuery", callback_query_id=cq_id, text=answer_text)
                else:
                    await self.call("answerCallbackQuery", callback_query_id=cq_id)
            except Exception:
                # «query is too old» или 429: кнопка уже нажата, состояние диалога сменилось — действие выполняем
                log.warning("answerCallbackQuery не прошёл, выполняю действие без ответа", exc_info=True)
            answered = True

            await self._process_actions(chat_id, actions, message_id=message_id)
        finally:
            if not answered:
                try:
                    await self.call("answerCallbackQuery", callback_query_id=cq_id)
                except Exception:
                    pass

    def _queue_notice(self, chat_id: int, wait_msg_id: int | None, q):
        """Колбэк очереди: показать человеку его место и примерное ожидание вместо «около минуты»."""

        async def notice(position: int) -> None:
            if wait_msg_id is None:
                return
            minutes = max(1, round((self._search_sem.eta_seconds(position) + self._search_sem.eta_seconds(1)) / 60))
            text = (
                f"Вы {position}-й в очереди, примерно {minutes} мин. "
                f"Потом ищу {q.origin} → {q.destination} на {q.depart:%d.%m}…"
            )
            try:
                await self.call("editMessageText", chat_id=chat_id, message_id=wait_msg_id, text=text)
            except Exception:
                log.debug("не удалось показать место в очереди", exc_info=True)

        return notice

    async def _scheduler_search(self, q):
        # телефоны общие: фоновый опрос стоит в той же очереди, что и поиски людей
        priority = BACKGROUND if self.s.search_priority else INTERACTIVE
        return await self._timed_search(q, kind="scheduler", priority=priority)

    async def _timed_search(
        self, q, *, kind: str, priority: int = INTERACTIVE, on_wait: Callable[[int], Awaitable[None]] | None = None
    ) -> list[SiteResult]:
        """Поиск через общий слот с учётом в /status, строкой лога и сигналом о сломанных сайтах."""
        t0 = time.monotonic()
        self._queued += 1
        acquired = False
        try:
            async with self._search_sem.slot(priority, on_wait=on_wait):
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
        pipeline_mod.configure(self.s)
        log.info("bot started")
        if self.subs is not None:
            scheduler = Scheduler(
                self.subs,
                self._scheduler_search,
                self._scheduler_notify,
                history=self.history,
            )
            self._scheduler_task = asyncio.create_task(scheduler.run_forever())
        try:
            await self._poll(offset)
        finally:
            await self.shutdown()

    async def _poll(self, offset: int) -> None:
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
                    self._spawn(self.handle(u["message"]))
                elif "callback_query" in u:
                    self._spawn(self.handle_callback(u["callback_query"]))

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        """Запускает обработчик апдейта, держит ссылку на задачу и логирует её падение."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("обработчик апдейта упал", exc_info=task.exception())

    async def shutdown(self, grace: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """Останавливает планировщик, даёт идущим поискам до grace секунд, закрывает HTTP-клиент."""
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            await asyncio.gather(self._scheduler_task, return_exceptions=True)
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            log.info("остановка: жду %d задач до %.0f с", len(pending), grace)
            _, still = await asyncio.wait(pending, timeout=grace)
            for task in still:
                task.cancel()
            await asyncio.gather(*still, return_exceptions=True)
        await self.http.aclose()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # httpx на уровне INFO пишет полный URL запроса, а в нём токен бота
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    asyncio.run(_serve(Bot(load_settings())))


async def _serve(bot: Bot) -> None:
    # SIGTERM (systemd, docker stop) ведёт себя как Ctrl+C: отмена и аккуратная остановка с кодом 0
    task = asyncio.current_task()
    stopping = asyncio.Event()

    def on_sigterm() -> None:
        stopping.set()
        if task is not None:
            task.cancel()

    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, on_sigterm)
    except (NotImplementedError, RuntimeError):
        pass
    try:
        await bot.run()
    except asyncio.CancelledError:
        if not stopping.is_set():
            raise
        log.info("остановлен по SIGTERM")


if __name__ == "__main__":
    main()
