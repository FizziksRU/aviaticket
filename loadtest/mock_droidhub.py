"""Мок DroidHub: N виртуальных телефонов, ответы из реальных снимков фазы 2, задержки и сбои по замерам.

ASGI-приложение без фреймворков: в симуляции подключается через httpx.ASGITransport (в процессе,
виртуальное время), отдельно запускается как сервер: `python -m loadtest.mock_droidhub --port 8765`.

Контракт (восстановлен по droidhub.py и ответам pool-avia):
  POST /api/devices/{id}/fetch_page  {url, wait_seconds<=60, capture_json[], wait_for?, max_chars}
    200 {url, title, text, truncated, captcha, json_responses:[{url,status,body}], finished, seconds}
    401/403 токен, 404 нет телефона, 429 телефон занят (если queue=False), 502 телефон/страница недоступны
  GET  /api/devices → [{id, name, online, battery, temperature, screen_on}]
"""
from __future__ import annotations

import asyncio
import glob
import json
import random
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = ROOT / "debug" / "samples"
SAMPLE_DEPART = date(2026, 11, 20)
SAMPLE_RETURN = date(2026, 11, 27)

# Замеры фазы 2 (docs/audit/phase2_runs.csv): секунды успешного fetch по классу телефона.
# fast = realme C21Y, slow = Redmi Note 11.
# Профиль v2 (после добора в 21:00–21:12): прогоны A до этого момента и B/C считались на v1,
# где aviasales@slow промахивался в 0,67 случаев (1/3), ozon@fast — всегда (0/2).
TIMINGS: dict[str, dict[str, list[float]]] = {
    "aviasales": {"fast": [22.9, 22.0], "slow": [37.7, 39.2, 30.1]},
    "aviasales_rt": {"fast": [46.0], "slow": [46.3]},
    "kupibilet": {"fast": [6.0, 7.0, 6.3, 6.3, 6.0, 5.4, 5.7, 5.2, 5.2], "slow": [12.8, 12.3, 14.9]},
    "tutu": {"fast": [5.5, 5.2, 12.7], "slow": [13.9, 12.1, 16.3, 12.0, 10.3, 10.7, 10.1, 9.8, 9.9]},
    "yandex_travel": {"fast": [7.7, 6.5], "slow": [15.5, 13.0, 12.2]},
    "onetwotrip": {"fast": [7.3, 8.3], "slow": [30.0]},
    "ozon_travel": {"fast": [6.3], "slow": [9.1, 9.7, 8.7]},
}
# Доля «JSON не перехвачен» (страница есть, json_responses пуст) и «ошибка инструмента» (502).
MISS: dict[tuple[str, str, bool], float] = {
    ("aviasales", "slow", False): 0.4, ("aviasales", "slow", True): 1.0,
    ("ozon_travel", "fast", False): 0.75, ("ozon_travel", "fast", True): 1.0,
    ("onetwotrip", "fast", True): 1.0,
    ("yandex_travel", "fast", True): 0.5,
}
ERROR_502: dict[tuple[str, str], float] = {("onetwotrip", "slow"): 1.0}
MISS_SECONDS: dict[str, float] = {
    "aviasales": 46.5, "ozon_travel": 31.2, "onetwotrip": 31.2, "yandex_travel": 8.4,
    "kupibilet": 31.0, "tutu": 26.0,
}
HOSTS = {
    "aviasales.ru": "aviasales", "kupibilet.ru": "kupibilet", "tutu.ru": "tutu",
    "travel.yandex.ru": "yandex_travel", "onetwotrip.com": "onetwotrip", "ozon.ru": "ozon_travel",
}


@dataclass
class Phone:
    id: str
    klass: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    busy_seconds: float = 0.0
    requests: int = 0
    by_site: dict[str, int] = field(default_factory=dict)


@dataclass
class Profile:
    """Какие сбои включены: 'measured' (как в фазе 2) или 'ideal' (без сбоев, для «после»)."""

    failures: bool = True
    captcha_rate: float = 0.0
    queue: bool = True
    jitter: float = 0.15
    extra_429_rate: float = 0.0  # имитация чужой нагрузки (боевой бот на тех же телефонах)


def site_of(url: str) -> str | None:
    for host, site in HOSTS.items():
        if host in url:
            return site
    return None


def is_round_trip(url: str) -> bool:
    return bool(
        "route%5B1%5D" in url or "route[1]" in url or "oneway=2" in url
        or re.search(r"aviasales\.ru/search/[A-Z]{3}\d{4}[A-Z]{3}\d{4}", url)
        or re.search(r"onetwotrip\.com/ru/f/search/\d{4}[A-Z]{6}\d{4}", url)
        or re.search(r"dates=d[\d-]+d[\d-]+", url)
    )


def _near_future(day: int, month: int, today: date) -> date:
    for year in (today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d >= today - timedelta(days=1):
            return d
    return date(today.year + 1, month, day)


def dates_from_url(site: str, url: str, today: date) -> list[date]:
    when = re.search(r"when=(\d{4}-\d{2}-\d{2})", url)
    if when:  # яндекс: return_date стоит в URL раньше when
        back = re.search(r"return_date=(\d{4}-\d{2}-\d{2})", url)
        return [date.fromisoformat(when.group(1))] + ([date.fromisoformat(back.group(1))] if back else [])
    iso = list(dict.fromkeys(date.fromisoformat(m) for m in re.findall(r"(\d{4}-\d{2}-\d{2})", url)))
    if iso:
        return iso[:2]
    if site == "tutu":
        return [date(int(y), int(m), int(d)) for d, m, y in re.findall(r"-(\d{2})(\d{2})(\d{4})-", url)][:2]
    m = re.search(r"/search/(?:[A-Z]{3})?(\d{2})(\d{2})(?:[A-Z]{3,6})(?:(\d{2})(\d{2}))?", url)
    if m:
        out = [_near_future(int(m.group(1)), int(m.group(2)), today)]
        if m.group(3):
            out.append(_near_future(int(m.group(3)), int(m.group(4)), today))
        return out
    return []


def _rewrite_dates(raw: str, src: date, dst: date) -> str:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d%m%Y"):
        raw = raw.replace(src.strftime(fmt), dst.strftime(fmt))
    return raw


class MockDroidHub:
    def __init__(self, n_phones: int, profile: Profile | None = None, seed: int = 1,
                 today: date | None = None, token: str = "mock") -> None:
        self.profile = profile or Profile()
        self.rng = random.Random(seed)
        self.today = today or date(2026, 11, 4)
        self.token = token
        names = ["realme", "redmi"] + [f"phone{i}" for i in range(3, n_phones + 1)]
        self.phones = {n: Phone(n, "fast" if i % 2 == 0 else "slow") for i, n in enumerate(names[:n_phones])}
        self.bodies = self._load_bodies()
        self.started = 0
        self.log: list[dict[str, Any]] = []

    @staticmethod
    def _load_bodies() -> dict[tuple[str, bool], tuple[str, date, date]]:
        """(сайт, туда-обратно) → (сырой ответ DroidHub, дата вылета в снимке, дата возврата в снимке)."""
        out: dict[tuple[str, bool], tuple[str, date, date]] = {}
        for f in sorted(glob.glob(str(SAMPLES_DIR / "*.json"))):
            name = Path(f).name
            route = "rt" if "_rt_" in name else ("near" if "_near_" in name else "far")
            if route == "far":
                continue
            site = name.split("_realme")[0].split("_redmi")[0]
            raw = Path(f).read_text(encoding="utf-8")
            data = json.loads(raw)
            if not data.get("json_responses"):
                continue
            key = (site, route == "rt")
            # берём самый полный снимок (больше JSON = ближе к худшему случаю по CPU)
            if key not in out or len(raw) > len(out[key][0]):
                out[key] = (raw, SAMPLE_DEPART, SAMPLE_RETURN)
        if ("onetwotrip", True) not in out:  # живого RT-снимка нет (0/1 на realme), берём фикстуру из tests/
            fixture = ROOT / "tests" / "fixtures" / "onetwotrip" / "search_rt_kuf_evn_0212_1012.json"
            wrapped = {"json_responses": [{"url": "https://www.onetwotrip.com/_avia-search-proxy/search/v3",
                                           "status": 200, "body": fixture.read_text(encoding="utf-8")}]}
            out[("onetwotrip", True)] = (json.dumps(wrapped, ensure_ascii=False), date(2026, 12, 2), date(2026, 12, 10))
        return out

    def _duration(self, site: str, klass: str, rt: bool) -> float:
        key = f"{site}_rt" if rt and f"{site}_rt" in TIMINGS else site
        base = self.rng.choice(TIMINGS[key][klass])
        return max(1.0, base * (1 + self.rng.uniform(-self.profile.jitter, self.profile.jitter)))

    def utilization(self, elapsed: float) -> dict[str, float]:
        return {p.id: round(p.busy_seconds / elapsed, 3) if elapsed > 0 else 0.0 for p in self.phones.values()}

    async def fetch_page(self, device_id: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        phone = self.phones.get(device_id)
        if phone is None:
            return 404, {"detail": "unknown device"}
        if not self.profile.queue and phone.lock.locked():
            return 429, {"detail": "device busy"}
        if self.profile.extra_429_rate and self.rng.random() < self.profile.extra_429_rate:
            return 429, {"detail": "queue busy"}
        url = str(payload.get("url", ""))
        site = site_of(url) or "unknown"
        rt = is_round_trip(url)
        wait_cap = min(60.0, float(payload.get("wait_seconds", 40)))
        async with phone.lock:
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            phone.requests += 1
            phone.by_site[site] = phone.by_site.get(site, 0) + 1
            outcome = "ok"
            if self.profile.failures and self.rng.random() < ERROR_502.get((site, phone.klass), 0.0):
                outcome = "error"
                dur = 30.0
            elif self.profile.failures and self.rng.random() < MISS.get((site, phone.klass, rt), 0.0):
                outcome = "miss"
                dur = min(wait_cap + 1.3, MISS_SECONDS.get(site, wait_cap))
            elif self.rng.random() < self.profile.captcha_rate:
                outcome = "captcha"
                dur = 4.0
            else:
                dur = min(wait_cap + 1.3, self._duration(site, phone.klass, rt) if site in TIMINGS else 10.0)
            await asyncio.sleep(dur)
            phone.busy_seconds += loop.time() - t0
            self.log.append({"t": t0, "device": device_id, "site": site, "rt": rt, "outcome": outcome, "dur": dur})
        if outcome == "error":
            return 502, {"detail": "page failed"}
        sample = self.bodies.get((site, rt)) if outcome == "ok" else None
        responses: list[dict[str, Any]] = []
        if sample is not None:
            data = json.loads(self._for_dates(sample, site, url))
            responses = data.get("json_responses", [])
        return 200, {
            "url": url, "title": site, "text": "x" * 3000, "truncated": False,
            "captcha": outcome == "captcha", "json_responses": responses,
            "finished": outcome == "ok", "seconds": round(dur, 1),
        }

    def _for_dates(self, sample: tuple[str, date, date], site: str, url: str) -> str:
        body, src_depart, src_return = sample
        wanted = dates_from_url(site, url, self.today)
        if not wanted:
            return body
        body = _rewrite_dates(body, src_depart, wanted[0])
        if len(wanted) > 1:
            body = _rewrite_dates(body, src_return, wanted[1])
        return body

    # --- ASGI ---
    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        path = scope["path"]
        headers = dict(scope.get("headers") or [])
        if headers.get(b"authorization", b"").decode() != f"Bearer {self.token}":
            status, data = 401, {"detail": "unauthorized"}
        elif scope["method"] == "GET" and path.rstrip("/") == "/api/devices":
            status, data = 200, [
                {"id": p.id, "name": p.id, "online": True, "battery": 80, "temperature": 30.0, "screen_on": False}
                for p in self.phones.values()
            ]
        elif m := re.fullmatch(r"/api/devices/([^/]+)/fetch_page", path):
            status, data = await self.fetch_page(m.group(1), json.loads(body or b"{}"))
        else:
            status, data = 404, {"detail": "not found"}
        raw = json.dumps(data, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": raw})


def main() -> None:  # pragma: no cover - ручной запуск сервера
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--phones", type=int, default=2)
    ap.add_argument("--ideal", action="store_true")
    args = ap.parse_args()
    uvicorn.run(MockDroidHub(args.phones, Profile(failures=not args.ideal)), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
