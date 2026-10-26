"""Сбор страницы выдачи через Playwright.

Отдаём наружу два источника данных:
  * видимый текст страницы после того как выдача догрузилась;
  * JSON-ответы, которые страница сама запрашивала (если сайт отдаёт цены XHR-ом,
    это надёжнее и дешевле, чем разбирать текст).
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
import re
from dataclasses import dataclass, field

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
IS_OVER_RE = re.compile(r'"is_over"\s*:\s*(true\b|"true"|1\b)', re.I)


@dataclass
class Snapshot:
    url: str
    text: str
    json_payloads: list[tuple[str, str]] = field(default_factory=list)
    blocked: bool = False


def is_blocked(text: str) -> bool:
    """Определяет, заблокирована ли страница капчей или сообщением об ограничении доступа."""
    low = text.lower()
    return any(m in low for m in BLOCK_MARKERS) and len(text) < 3000


def should_save_response(url: str, length: int) -> bool:
    """Решает, нужно ли сохранять JSON-ответ по URL и длине тела.

    Для URL '/search/v3.2/results' сохраняются любые ответы (включая короткие >= 0).
    Для остальных URL проверяется совпадение с ключевыми словами и длина более 200 байт.
    Длина не должна превышать MAX_JSON_BYTES.
    """
    if not (0 <= length <= MAX_JSON_BYTES):
        return False
    if "/search/v3.2/results" in url:
        return True
    return bool(JSON_URL_HINT.search(url)) and length > 200


def has_is_over(body: str) -> bool:
    """Проверяет наличие признака завершения выдачи ('is_over': true/1) в теле ответа."""
    return bool(IS_OVER_RE.search(body))


def is_search_over(payloads: list[tuple[str, str]]) -> bool:
    """Проверяет, пришел ли завершающий ответ для /search/v3.2/results среди payloads."""
    return any(
        "/search/v3.2/results" in u and has_is_over(b)
        for u, b in payloads
    )


def compact_json(payload: str, limit: int = 30_000) -> str:
    """Минифицируем JSON, чтобы он влез в промпт."""
    try:
        return json.dumps(json.loads(payload), ensure_ascii=False, separators=(",", ":"))[:limit]
    except ValueError:
        return payload[:limit]


async def _safe_close(resource: object | None) -> None:
    """Безопасно закрывает ресурс (контекст или браузер), не маскируя исходную ошибку."""
    if resource is None or not hasattr(resource, "close"):
        return
    try:
        close_fn = getattr(resource, "close")
        if callable(close_fn):
            res = close_fn()
            if inspect.isawaitable(res):
                await res
    except Exception:
        pass


async def fetch_snapshot(
    url: str,
    site: SiteConfig,
    *,
    headless: bool = True,
    profile_dir: Path | None = None,
    debug_dir: Path | None = None,
) -> Snapshot:
    payloads: list[tuple[str, str]] = []
    text = ""
    final_url = url
    browser = None
    ctx = None

    async with async_playwright() as pw:
        try:
            if profile_dir is not None:
                p_dir = Path(profile_dir)
                p_dir.mkdir(parents=True, exist_ok=True)
                ctx = await pw.chromium.launch_persistent_context(
                    str(p_dir),
                    headless=headless,
                    locale="ru-RU",
                    timezone_id="Europe/Moscow",
                    viewport={"width": 1366, "height": 900},
                )
                browser = getattr(ctx, "browser", None)
            else:
                browser = await pw.chromium.launch(headless=headless)
                ctx = await browser.new_context(
                    locale="ru-RU",
                    timezone_id="Europe/Moscow",
                    viewport={"width": 1366, "height": 900},
                )

            page = ctx.pages[0] if getattr(ctx, "pages", None) else await ctx.new_page()

            async def on_response(resp):
                try:
                    ctype = resp.headers.get("content-type", "")
                    if "json" not in ctype:
                        return
                    body = await resp.body()
                    if should_save_response(resp.url, len(body)):
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

            # Опрос результатов: для URL с '/search/v3.2/results' опрашиваем накопленные
            # payloads с интервалом poll_interval_ms до появления признака завершения
            # 'is_over': true (проверяется динамически на каждом шаге), либо до таймаута settle_ms.
            poll_interval_ms = 250
            elapsed_ms = 0
            while elapsed_ms < site.settle_ms:
                if is_search_over(payloads):
                    break
                step = min(poll_interval_ms, site.settle_ms - elapsed_ms)
                if step <= 0:
                    break
                await page.wait_for_timeout(step)
                elapsed_ms += step

            for _ in range(site.scrolls):
                await page.mouse.wheel(0, 1800)
                await page.wait_for_timeout(1200)

            text = await page.inner_text("body")
            final_url = page.url

            if debug_dir:
                d_dir = Path(debug_dir)
                d_dir.mkdir(parents=True, exist_ok=True)
                stem = re.sub(r"\W+", "_", site.name)
                await page.screenshot(path=str(d_dir / f"{stem}.png"), full_page=False)
                (d_dir / f"{stem}.txt").write_text(text, encoding="utf-8")

        finally:
            await _safe_close(ctx)
            if browser is not ctx:
                await _safe_close(browser)

    blocked = is_blocked(text)
    return Snapshot(url=final_url, text=text, json_payloads=payloads, blocked=blocked)
