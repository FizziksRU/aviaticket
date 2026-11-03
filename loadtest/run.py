"""Нагрузочный прогон бота на моках в виртуальном времени.

    PYTHONPATH=src:. python -m loadtest.run --scenario A --users 50 --phones 2
    PYTHONPATH=src:. python -m loadtest.run --scenario B --users 100 --phones 4 --hours 24

Код бота не меняется: Bot(...).run() работает как в проде, а harness лишь подменяет сеть
(httpx → ASGI-моки), часы (виртуальное время) и снимает метрики обёртками.
Сценарии:
  A — пик: все пользователи жмут поиск в течение 10 с, по одному поиску, без подписок.
  B — день: поиски по Пуассону с 8:00 до 24:00 (90% по 3 в день, 10% «тяжёлых» по 20), после первого
      поиска каждый ставит отслеживание; DAILY_SEARCH_LIMIT=0 (без лимита).
  C — как B, но с лимитом из кода (40 поисков в сутки) и «тяжёлыми» по 60 в день.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import logging
import os
import random
import statistics
import tempfile
import tracemalloc
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from . import vclock
from .mock_droidhub import MockDroidHub, Profile
from .mock_telegram import MockTelegram
from .vusers import RouteChooser, SearchRecord, VirtualUser

RESULTS = Path(__file__).resolve().parent / "results"
TODAY = date(2026, 11, 4)
_kind: contextvars.ContextVar[str] = contextvars.ContextVar("kind", default="user")


class Router(httpx.AsyncBaseTransport):
    def __init__(self, apps: dict[str, Any]) -> None:
        self.routes = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        tr = self.routes.get(request.url.host)
        if tr is None:
            raise httpx.ConnectError(f"no route to {request.url.host}", request=request)
        return await tr.handle_async_request(request)


class LogCounter(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.counts: Counter[str] = Counter()

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage().split("\n")[0][:70]
        self.counts[f"{record.levelname} {record.name}: {msg}"] += 1


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round(p / 100 * (len(xs) - 1)))))
    return round(xs[k], 1)


def _patch(clock: vclock.VirtualClock, router: Router) -> list[Any]:
    """Подмена сети и часов, захваченных в аргументах по умолчанию при импорте модулей бота."""
    import avia.guard
    import avia.scheduler
    import avia.store
    import avia.tutu_ids

    undo: list[Any] = []
    orig_init = httpx.AsyncClient.__init__

    def init(self: httpx.AsyncClient, *a: Any, **kw: Any) -> None:
        if kw.get("transport") is None:
            kw["transport"] = router
        orig_init(self, *a, **kw)

    httpx.AsyncClient.__init__ = init  # type: ignore[method-assign]
    undo.append(lambda: setattr(httpx.AsyncClient, "__init__", orig_init))

    def vtoday() -> date:
        return date.fromtimestamp(clock.time())

    for fn in (avia.guard.SearchGuard.__init__, avia.store.ResultStore.__init__, avia.scheduler.Scheduler.__init__):
        saved = fn.__defaults__
        fn.__defaults__ = tuple(
            clock.monotonic if x is vclock._real_monotonic
            else clock.time if x is vclock._real_time
            else vtoday if x == date.today
            else x
            for x in (saved or ())
        )
        undo.append(lambda fn=fn, saved=saved: setattr(fn, "__defaults__", saved))
    avia.tutu_ids._CACHE.update({"LED": ("75", "Sankt-peterburg"), "AER": ("78", "Sochi")})
    return undo


async def _scenario(args: argparse.Namespace, clock: vclock.VirtualClock) -> dict[str, Any]:
    import avia.bot as botmod
    import avia.droidhub as dh
    import avia.pipeline as pipeline
    from avia.config import load_settings
    from avia.guard import SearchGuard

    loop = asyncio.get_running_loop()
    rng = random.Random(args.seed)
    tg = MockTelegram()
    hub = MockDroidHub(args.phones, Profile(failures=not args.ideal), seed=args.seed, today=TODAY)
    undo = _patch(clock, Router({"api.telegram.org": tg, "droidhub.mock": hub}))
    logc = LogCounter()
    logging.getLogger().addHandler(logc)
    try:
        dh._reset_pool()
        pipeline.guard = SearchGuard()
        tmp = Path(tempfile.mkdtemp(prefix="avia-load-"))
        if args.phone_map == "optimal":
            # измерено в фазе 2: aviasales, onetwotrip, yandex работают на «быстром» классе (realme),
            # ozon, kupibilet, tutu — на «медленном» (redmi); код без PHONE_MAP переменную игнорирует
            fast = ",".join(p.id for p in hub.phones.values() if p.klass == "fast")
            slow = ",".join(p.id for p in hub.phones.values() if p.klass == "slow") or fast
            os.environ["PHONE_MAP"] = ";".join(
                [f"{s}={fast}" for s in ("aviasales", "onetwotrip", "yandex_travel")]
                + [f"{s}={slow}" for s in ("ozon_travel", "kupibilet", "tutu")]
            )
        base = load_settings()
        limit = {"A": 0, "B": 0, "C": 40}[args.scenario]
        settings = replace(
            base, tg_token="TEST", tg_allowed_ids={1}, tg_open=True, daily_search_limit=limit,
            droidhub_url="http://droidhub.mock", droidhub_token="mock",
            droidhub_devices=list(hub.phones), storage_dir=tmp, debug_dir=tmp / "debug",
            max_parallel_searches=args.max_parallel or base.max_parallel_searches,
        )
        if args.sites_auto:
            settings = replace(settings, sites=[replace(s, phone="auto") if s.phone else s for s in settings.sites])

        searches: list[dict[str, Any]] = []
        orig_search = botmod.search

        async def traced_search(q: Any, s: Any, **kw: Any) -> Any:
            t0 = loop.time()
            res = await orig_search(q, s, **kw)
            searches.append({
                "kind": _kind.get(), "t0": t0, "t1": loop.time(), "rt": q.return_date is not None,
                "key": (q.origin, q.destination, q.depart.isoformat(), q.return_date.isoformat() if q.return_date else None),
                "sites": [(r.site, r.ok, len(r.offers), r.cached, round(r.elapsed, 1), r.partial, (r.error or "")[:40])
                          for r in res],
            })
            return res

        botmod.search = traced_search
        undo.append(lambda: setattr(botmod, "search", orig_search))
        bot = botmod.Bot(settings)
        orig_sched = bot._scheduler_search

        async def sched_search(q: Any) -> Any:
            _kind.set("scheduler")
            return await orig_sched(q)

        bot._scheduler_search = sched_search  # type: ignore[method-assign]
        tracemalloc.start(1)
        mem0 = tracemalloc.get_traced_memory()[0]
        t_start = loop.time()
        real0 = vclock.real_perf_counter()
        bot_task = asyncio.create_task(bot.run())
        chooser = RouteChooser(rng, TODAY)
        records: list[SearchRecord] = []
        mem_samples: list[tuple[float, int]] = []

        async def sample_mem() -> None:
            while True:
                mem_samples.append((round(loop.time() - t_start), tracemalloc.get_traced_memory()[0]))
                await asyncio.sleep(600)

        mem_task = asyncio.create_task(sample_mem())

        async def one_search(u: VirtualUser, delay: float, track: bool) -> None:
            await asyncio.sleep(delay)
            d, r = chooser.pick()
            records.append(await u.search(d, r, track))

        if args.scenario == "A":
            users = [VirtualUser(tg, 100_000 + i, random.Random(args.seed * 1000 + i), TODAY, patience=args.patience)
                     for i in range(args.users)]
            await asyncio.gather(*(one_search(u, rng.uniform(0, 10), False) for u in users))
        else:
            heavy_rate = 20 if args.scenario == "B" else 60

            async def day(i: int) -> None:
                u = VirtualUser(tg, 100_000 + i, random.Random(args.seed * 1000 + i), TODAY, patience=args.patience)
                rate = heavy_rate if i % 10 == 0 else 3
                n = max(1, sum(1 for _ in range(rate * 4) if rng.random() < 0.25))
                times = sorted(rng.uniform(8 * 3600, 24 * 3600) for _ in range(n))
                times = [t * args.hours / 24 for t in times]
                last = 0.0
                for k, t in enumerate(times):
                    await asyncio.sleep(max(0.0, t - last))
                    t_before = loop.time()
                    d, r = chooser.pick()
                    records.append(await u.search(d, r, track=(k == 0 and not args.no_track)))
                    last = t + (loop.time() - t_before)

            await asyncio.gather(*(day(i) for i in range(args.users)))
            # дать планировщику доработать до конца суток
            await asyncio.sleep(max(0.0, args.hours * 3600 - (loop.time() - t_start)))

        t_end = loop.time()
        real = vclock.real_perf_counter() - real0
        mem1, mem_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        for t in (bot_task, mem_task, getattr(bot, "_scheduler_task", None)):
            if t is not None:
                t.cancel()
        await asyncio.gather(bot_task, mem_task, return_exceptions=True)
        return _report(args, records, searches, hub, tg, logc, bot, pipeline, t_start, t_end, real,
                       mem0, mem1, mem_peak, mem_samples)
    finally:
        logging.getLogger().removeHandler(logc)
        for u in reversed(undo):
            u()


def _report(args: argparse.Namespace, records: list[SearchRecord], searches: list[dict[str, Any]],
            hub: MockDroidHub, tg: MockTelegram, logc: LogCounter, bot: Any, pipeline: Any,
            t_start: float, t_end: float, real: float, mem0: int, mem1: int, mem_peak: int,
            mem_samples: list[tuple[float, int]]) -> dict[str, Any]:
    elapsed = t_end - t_start
    ok = [r for r in records if r.outcome == "ok"]
    lat = [r.t_result - r.t_click for r in ok if r.t_result is not None]
    first = [r.step_latencies[0] for r in records if r.step_latencies]
    steps = [x for r in records for x in r.step_latencies]
    user_s = [s for s in searches if s["kind"] == "user"]
    sched_s = [s for s in searches if s["kind"] == "scheduler"]
    site_stats: dict[str, Counter[str]] = defaultdict(Counter)
    for s in searches:
        for site, okk, n, cached, el, partial, err in s["sites"]:
            c = site_stats[site]
            c["n"] += 1
            c["ok"] += int(okk)
            c["cached"] += int(cached)
            c["partial"] += int(bool(partial))
            c["elapsed_sum"] += el
    outcomes = Counter(r.outcome for r in records)
    subs = _subscription_stats(bot.s.storage_dir / "subscriptions.sqlite")
    return {
        "scenario": args.scenario, "users": args.users, "phones": args.phones, "ideal": args.ideal,
        "sites_auto": args.sites_auto, "max_parallel": args.max_parallel, "no_track": args.no_track, "seed": args.seed,
        "virtual_seconds": round(elapsed), "real_seconds": round(real, 1),
        "searches_requested": len(records), "outcomes": dict(outcomes),
        "result_latency": {"p50": pct(lat, 50), "p95": pct(lat, 95), "max": pct(lat, 100), "n": len(lat)},
        "wait_over": {f">{m}min": sum(1 for x in lat if x > m * 60) for m in (2, 5, 15, 60)},
        "first_response": {"p50": pct(first, 50), "p95": pct(first, 95), "max": pct(first, 100)},
        "dialog_step": {"p50": pct(steps, 50), "p95": pct(steps, 95), "max": pct(steps, 100)},
        "tracked": subs.get("total", 0),
        "subscriptions": subs,
        "pipeline_searches": {"user": len(user_s), "scheduler": len(sched_s)},
        "search_wall": {"p50": pct([s["t1"] - s["t0"] for s in user_s], 50),
                        "p95": pct([s["t1"] - s["t0"] for s in user_s], 95)},
        "sites": {k: {"n": v["n"], "ok": v["ok"], "cached": v["cached"], "partial": v["partial"],
                      "avg_elapsed": round(v["elapsed_sum"] / v["n"], 1) if v["n"] else None}
                  for k, v in site_stats.items()},
        "phones_util": hub.utilization(elapsed),
        "phone_requests": {p.id: p.requests for p in hub.phones.values()},
        "phone_by_site": {p.id: p.by_site for p in hub.phones.values()},
        "phone_outcomes": dict(Counter(x["outcome"] for x in hub.log)),
        "telegram": {"stats": tg.stats, "errors": tg.errors[:5], "n_errors": len(tg.errors)},
        "memory": {"start_mb": round(mem0 / 1e6, 1), "end_mb": round(mem1 / 1e6, 1), "peak_mb": round(mem_peak / 1e6, 1),
                   "samples_mb": [(t, round(m / 1e6, 1)) for t, m in mem_samples[:: max(1, len(mem_samples) // 12)]]},
        "state_sizes": {"sessions": len(bot.dialog.sessions), "result_store": len(bot.store._data),
                        "guard_cache": len(pipeline.guard._cache)},
        "log": dict(logc.counts.most_common(12)),
        "sched_intervals": _poll_lag(sched_s, args.hours),
        "examples": [asdict(r) for r in records[:3]],
        "scheduler_checks": _sched_checks(sched_s),
    }


def _subscription_stats(path: Path) -> dict[str, int]:
    """Сколько подписок создали виртуальные люди и сколько у них разных групп (маршрут + даты)."""
    import sqlite3

    if not path.exists():
        return {}
    with sqlite3.connect(path) as db:
        total, active, groups = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(active), 0), "
            "COUNT(DISTINCT origin || destination || depart || COALESCE(return_date, '')) FROM subscriptions"
        ).fetchone()
    return {"total": total, "active": active, "groups": groups}


def _poll_lag(sched_s: list[dict[str, Any]], hours: float) -> dict[str, Any]:
    """Насколько реальный интервал опроса группы подписок больше обещанного poll_interval_seconds."""
    from avia.subscriptions import poll_interval_seconds

    by_key: dict[Any, list[float]] = defaultdict(list)
    for s in sched_s:
        by_key[tuple(s["key"])].append(s["t0"])
    ratios: list[float] = []
    gaps_min: list[float] = []
    for key, ts in by_key.items():
        ts.sort()
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        if not gaps:
            continue
        nominal = poll_interval_seconds(date.fromisoformat(key[2]), TODAY)
        median_gap = statistics.median(gaps)
        ratios.append(median_gap / nominal)
        gaps_min.append(max(gaps) / 60)
    if not ratios:
        return {"groups_polled": len(by_key)}
    return {"groups_polled": len(by_key), "groups_with_gaps": len(ratios),
            "gap_to_nominal_p50": round(pct(ratios, 50), 2), "gap_to_nominal_p95": round(pct(ratios, 95), 2),
            "max_gap_min_p95": round(pct(gaps_min, 95), 1)}


def _sched_checks(sched_s: list[dict[str, Any]]) -> dict[str, Any]:
    if not sched_s:
        return {}
    ts = sorted(s["t0"] for s in sched_s)
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    walls = [s["t1"] - s["t0"] for s in sched_s]
    return {"n": len(sched_s), "wall_p50": pct(walls, 50), "wall_p95": pct(walls, 95),
            "busy_share": round(sum(walls) / (ts[-1] - ts[0] + walls[-1]), 2) if len(ts) > 1 else None,
            "gap_p50": pct(gaps, 50)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["A", "B", "C"], default="A")
    ap.add_argument("--users", type=int, default=10)
    ap.add_argument("--phones", type=int, default=2)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--ideal", action="store_true", help="без сбоев телефонов (для сравнения)")
    ap.add_argument("--sites-auto", action="store_true", help="все сайты phone: auto (эксперимент)")
    ap.add_argument("--max-parallel", type=int, default=0)
    ap.add_argument("--no-track", action="store_true", help="B/C без отслеживаний: сколько подписки отнимают у людей")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--patience", type=float, default=4 * 3600, help="сколько виртуальный человек ждёт выдачу, с")
    ap.add_argument("--phone-map", choices=["", "optimal"], default="",
                    help="optimal: PHONE_MAP по замерам фазы 2 (нужна ветка scale-audit-phones)")
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    logging.getLogger().setLevel(logging.WARNING)
    result, _ = vclock.run(lambda clock: _scenario(args, clock))
    RESULTS.mkdir(exist_ok=True)
    name = f"{args.scenario}_u{args.users}_p{args.phones}{'_ideal' if args.ideal else ''}{'_auto' if args.sites_auto else ''}{'_notrack' if args.no_track else ''}{args.tag}"
    (RESULTS / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    keys = ("virtual_seconds", "real_seconds", "outcomes", "result_latency", "first_response", "phones_util",
            "pipeline_searches")
    print(name, json.dumps({k: result[k] for k in keys}, ensure_ascii=False))


if __name__ == "__main__":
    main()
