"""Замеры фазы 2: URL для телефонов и разбор ответов MCP fetch_page тем же кодом, что в боте.

    PYTHONPATH=src .venv/bin/python tools/phase2.py urls
    PYTHONPATH=src .venv/bin/python tools/phase2.py ingest FILE SITE PHONE ROUTE

`ingest` подаёт сохранённый ответ DroidHub в droidhub._fetch_device_snapshot через
httpx.MockTransport (тот же разбор, что в проде), затем в pipeline._process_snapshot
(парсер, results_complete, фильтр дат), пишет строку в docs/audit/phase2_runs.csv
и сырой снимок в debug/samples/.
"""
from __future__ import annotations

import asyncio
import csv
import json
import sys
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from avia.config import load_settings
from avia.droidhub import _fetch_device_snapshot
from avia.models import Query
from avia.pipeline import ParserEmptyError, _process_snapshot, resolve_extra
from avia.sites import build_url

ROOT = Path(__file__).resolve().parents[1]
RUNS_CSV = ROOT / "docs" / "audit" / "phase2_runs.csv"
SAMPLES = ROOT / "debug" / "samples"

ROUTES: dict[str, Query] = {
    "near": Query(origin="MOW", destination="LED", depart=date(2026, 11, 20)),
    "far": Query(origin="MOW", destination="AER", depart=date(2027, 1, 22)),
    "rt": Query(origin="MOW", destination="LED", depart=date(2026, 11, 20), return_date=date(2026, 11, 27)),
}
FIELDS = [
    "ts", "site", "phone", "route", "seconds", "finished", "captcha", "text_len",
    "n_json", "json_bytes", "offers", "partial", "min_price", "error",
]


def _settings():
    s = load_settings()
    # фиктивные значения: запрос уходит в MockTransport, а не в сеть
    return replace(s, droidhub_url="http://droidhub.mock", droidhub_token="mock", droidhub_devices=["realme", "redmi"])


def _site(settings, name: str):
    for s in settings.sites:
        if s.name == name:
            return s
    raise SystemExit(f"нет сайта {name}")


async def _urls() -> dict:
    settings = _settings()
    out: dict = {}
    for site in settings.sites:
        if not site.enabled or site.source != "browser":
            continue
        out[site.name] = {
            "phone": site.phone,
            "wait_seconds": site.phone_wait_seconds,
            "capture_json": site.phone_capture_json,
            "wait_for": site.phone_wait_for or None,
            "routes": {},
        }
        for tag, q in ROUTES.items():
            extra = await resolve_extra(site, q)
            out[site.name]["routes"][tag] = build_url(site, q, extra=extra)
    return out


def _load_mcp(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("type") == "text":
        data = json.loads(data[0]["text"])
    if not isinstance(data, dict):
        raise SystemExit("неожиданный формат ответа")
    return data


async def _ingest(path: Path, site_name: str, phone: str, route: str) -> dict:
    settings = _settings()
    site = _site(settings, site_name)
    q = ROUTES[route]
    data = _load_mcp(path)

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=data))
    async with httpx.AsyncClient(transport=transport) as client:
        snap, finished = await _fetch_device_snapshot(
            settings, phone, data.get("url", ""), wait_seconds=site.phone_wait_seconds,
            capture_json=site.phone_capture_json, wait_for=site.phone_wait_for, client=client,
        )
    row = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "site": site_name, "phone": phone, "route": route,
        "seconds": data.get("seconds"), "finished": finished, "captcha": snap.blocked,
        "text_len": len(snap.text), "n_json": len(snap.json_payloads),
        "json_bytes": sum(len(b) for _, b in snap.json_payloads),
        "offers": 0, "partial": "", "min_price": "", "error": "",
    }
    if snap.blocked:
        row["error"] = "captcha"
    else:
        try:
            offers, _, partial = await _process_snapshot(site, snap, q, settings, partial=not finished)
            row.update(offers=len(offers), partial=partial, min_price=offers[0].price if offers else "")
        except ParserEmptyError as exc:
            row["error"] = f"ParserEmptyError: {exc.message}"
        except Exception as exc:  # фиксируем любую ошибку разбора как результат замера
            row["error"] = f"{type(exc).__name__}: {exc}"

    SAMPLES.mkdir(parents=True, exist_ok=True)
    stamp = row["ts"].replace(":", "").replace("-", "")
    (SAMPLES / f"{site_name}_{phone}_{route}_{stamp}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    new_file = not RUNS_CSV.exists()
    with RUNS_CSV.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)
    return row


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "urls":
        print(json.dumps(asyncio.run(_urls()), ensure_ascii=False, indent=1))
        return
    if len(sys.argv) == 6 and sys.argv[1] == "ingest":
        print(json.dumps(asyncio.run(_ingest(Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5])), ensure_ascii=False))
        return
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
