"""Разведка сайта: python -m avia.recon <сайт из sites.yaml> Москва Алматы 10.12 [--show]

Открывает выдачу в Playwright и сохраняет в debug/recon/<сайт>/:
  * все JSON-ответы, которые страница запрашивала (NN_<путь>.json);
  * page.txt (видимый текст) и page.png (скриншот);
  * index.txt: список ответов, самые похожие на выдачу цен сверху.
Эти файлы нужны, чтобы написать точный парсер без LLM.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from .collector import fetch_snapshot
from .config import ROOT, load_settings
from .parsing import ParseError, parse_query
from .sites import build_url

PRICE_HINTS = ("price", "amount", "cost", "fare", "total", "unified_price", "rub")


def score(body: str) -> int:
    low = body.lower()
    return sum(low.count(h) for h in PRICE_HINTS)


def slug(url: str, n: int) -> str:
    path = urlparse(url).path.strip("/").replace("/", "_") or "root"
    return f"{n:02d}_{re.sub(r'[^A-Za-z0-9_.-]', '', path)[:60]}.json"


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 4:
        print("Формат: python -m avia.recon <сайт> Москва Алматы 10.12 [--show]")
        return 2
    site_name, rest = args[0], " ".join(args[1:])
    s = load_settings()
    site = next((x for x in s.sites if x.name == site_name), None)
    if site is None or site.source != "browser":
        print(f"Сайт {site_name!r} не найден среди браузерных в config/sites.yaml")
        return 2
    try:
        q = parse_query(rest)
    except ParseError as e:
        print(e)
        return 2

    out = ROOT / "debug" / "recon" / site_name
    out.mkdir(parents=True, exist_ok=True)
    s.headless = "--show" not in sys.argv
    profile_dir = ROOT / "state" / "profiles" / site_name
    snap = await fetch_snapshot(
        build_url(site, q),
        site,
        headless=s.headless,
        profile_dir=profile_dir,
        debug_dir=out,
    )
    (out / "page.txt").write_text(snap.text, encoding="utf-8")

    rows = []
    for i, (url, body) in enumerate(snap.json_payloads, 1):
        name = slug(url, i)
        (out / name).write_text(body, encoding="utf-8")
        rows.append((score(body), len(body), name, url.split("?")[0]))
    rows.sort(reverse=True)
    index = "\n".join(f"score={sc:5d} size={sz:7d} {name}  {u}" for sc, sz, name, u in rows)
    (out / "index.txt").write_text(index + "\n", encoding="utf-8")

    if snap.blocked:
        print(f"сайт показал капчу: запустите с --show и пройдите её руками, сессия сохранится в state/profiles/{site_name}")
    print(f"Профиль: {profile_dir}")
    print(f"Сохранено в {out}")
    print(f"JSON-ответов: {len(rows)}, блокировка: {snap.blocked}")
    print(index[:2000] or "JSON-ответов не поймано, пригодится page.txt")
    print("\nПришлите мне папку (или хотя бы index.txt и 2-3 файла с самым высоким score).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
