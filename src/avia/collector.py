"""Сбор страницы выдачи через Playwright.

Отдаём наружу два источника данных:
  * видимый текст страницы после того как выдача догрузилась;
  * JSON-ответы, которые страница сама запрашивала (если сайт отдаёт цены XHR-ом,
    это надёжнее и дешевле, чем разбирать текст).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import async_playwright

from .config import SiteConfig

JSON_URL_HINT = re.compile(r"search|ticket|price|offer|fare|avia|flight|result", re.I)
MAX_JSON_BYTES = 400_000
BLOCK_MARKERS = (
    "captcha",
    "подтвердите, что вы не робот",
    "вы не робот",
    "access denied",
    "are you a human",
)


@dataclass
class Snapshot:
    url: str
    text: str
    json_payloads: list[tuple[str, str]] = field(default_factory=list)
    blocked: bool = False


async def fetch_snapshot(
    url: str,
    site: SiteConfig,
    *,
    headless: bool = True,
    storage_state: Path | None = None,
    debug_dir: Path | None = None,
) -> Snapshot:
    payloads: list[tuple[str, str]] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx_kwargs = dict(
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1366, "height": 900},
        )
        if storage_state and storage_state.exists():
            ctx_kwargs["storage_state"] = str(storage_state)
        ctx = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()

        async def on_response(resp):
            try:
                ctype = resp.headers.get("content-type", "")
                if "json" not in ctype or not JSON_URL_HINT.search(resp.url):
                    return
                body = await resp.body()
                if 200 < len(body) <= MAX_JSON_BYTES:
                    payloads.append((resp.url, body.decode("utf-8", "replace")))
            except Exception:
                pass  # ответ мог закрыться вместе со страницей

        page.on("response", on_response)

        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        if site.wait_selector:
            try:
                await page.wait_for_selector(site.wait_selector, timeout=45_000)
            except Exception:
                pass
        await page.wait_for_timeout(site.settle_ms)
        for _ in range(site.scrolls):
            await page.mouse.wheel(0, 1800)
            await page.wait_for_timeout(1200)

        text = await page.inner_text("body")
        final_url = page.url

        if debug_dir:
            debug_dir.mkdir(parents=True, exist_ok=True)
            stem = re.sub(r"\W+", "_", site.name)
            await page.screenshot(path=str(debug_dir / f"{stem}.png"), full_page=False)
            (debug_dir / f"{stem}.txt").write_text(text, encoding="utf-8")

        if storage_state:
            storage_state.parent.mkdir(parents=True, exist_ok=True)
            await ctx.storage_state(path=str(storage_state))

        await browser.close()

    low = text.lower()
    blocked = any(m in low for m in BLOCK_MARKERS) and len(text) < 3000
    return Snapshot(url=final_url, text=text, json_payloads=payloads, blocked=blocked)


def compact_json(payload: str, limit: int = 30_000) -> str:
    """Минифицируем JSON, чтобы он влез в промпт."""
    try:
        return json.dumps(json.loads(payload), ensure_ascii=False, separators=(",", ":"))[:limit]
    except ValueError:
        return payload[:limit]
