from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class SiteConfig:
    name: str
    url_template: str
    enabled: bool = True
    wait_selector: str | None = None
    settle_ms: int = 8000
    scrolls: int = 3
    note: str = ""
    # source: "browser" (Playwright + парсер) или "api" (HTTP-клиент без браузера)
    source: str = "browser"
    # parser: имя зарегистрированного парсера (для api это имя клиента)
    parser: str = ""
    # use_llm: если парсера нет или он ничего не нашёл, отдать страницу в LLM (по умолчанию выключено)
    use_llm: bool = False
    # id_resolver: имя резолвера id (например, 'tutu' включает разрешение id городов)
    id_resolver: str = ""
    url_template_rt: str = ""
    # replay_post: конфигурация повтора POST-запроса {"url_contains": "...", "set": {...}}
    replay_post: dict | None = None
    phone: str = ""
    phone_capture_json: list[str] = field(default_factory=list)
    phone_wait_seconds: int = 40
    # подстрока в ответе, по которой видно конец выдачи (хаб ждёт её появления)
    phone_wait_for: str = ""


@dataclass
class Settings:
    sites: list[SiteConfig]
    tp_token: str = ""            # Travelpayouts API token
    tp_marker: str = ""           # партнёрский marker для ссылок
    droidhub_url: str = ""        # адрес сервиса droidhub (HTTP API)
    droidhub_token: str = ""      # токен авторизации droidhub
    droidhub_devices: list[str] = field(default_factory=list)  # пул телефонов droidhub
    # llm_backend нужен только сайтам с use_llm: true
    # "agy_ssh" (аккаунт пула на VPS через SSH) или "gemini_local" (локальный gemini CLI)
    llm_backend: str = "gemini_local"
    gemini_cmd: str = "gemini"
    gemini_model: str | None = None
    gemini_timeout: int = 180
    ssh_host: str = ""            # например user@host
    ssh_key: str = ""             # например ~/.ssh/id_ed25519
    agy_account: str = "acc4"
    agy_proxy: str = ""           # http://127.0.0.1:3105, исходящий канал аккаунта на сервере
    agy_wrapper: str = "/opt/gemini-pool/bin/avia-llm"
    headless: bool = True
    storage_dir: Path = field(default_factory=lambda: ROOT / "state")
    debug_dir: Path = field(default_factory=lambda: ROOT / "debug")
    top_n: int = 5
    tg_token: str = ""
    tg_api_base: str = ""          # пусто = https://api.telegram.org; для моков и локального шлюза
    tg_allowed_ids: set[int] = field(default_factory=set)   # администраторы; без лимитов поисков
    tg_open: bool = False          # True: бот отвечает всем, пользователи регистрируются в users.sqlite
    daily_search_limit: int = 40   # поисков в сутки на человека (0 = без лимита)
    max_parallel_searches: int = 2  # одновременных поисков во всём боте (телефоны одни на всех)
    # SearchGuard: пауза между стартами поисков одного сайта, кэш выдачи, параллельных поисков сайта
    guard_min_interval: float = 30.0
    guard_cache_ttl: float = 300.0
    guard_site_concurrency: int = 1
    sites_per_search: int = 2       # сайтов одновременно внутри одного поиска
    search_priority: bool = False   # True: поиски людей впереди фонового опроса подписок
    queue_notice: bool = False      # True: писать человеку «вы N-й в очереди, примерно M мин»
    # PHONE_MAP: сайт → телефоны поверх phone из sites.yaml («ozon_travel=redmi;aviasales=realme»)
    phone_map: dict[str, list[str]] = field(default_factory=dict)
    retry_other_phone: bool = False  # RETRY_OTHER_PHONE=1: повтор пустой выдачи на другом телефоне


def parse_phone_map(raw: str) -> dict[str, list[str]]:
    """«site=phone[,phone];site2=phone» → {site: [phone, ...]}; ошибка формата — ValueError."""
    result: dict[str, list[str]] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        site, sep, phones = part.partition("=")
        devices = [d.strip() for d in phones.split(",") if d.strip()]
        if not sep or not site.strip() or not devices:
            raise ValueError(f"PHONE_MAP: ожидается «сайт=телефон[,телефон]», получено «{part}»")
        result[site.strip()] = devices
    return result


def load_settings(path: Path | None = None) -> Settings:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    path = path or ROOT / "config" / "sites.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    sites = [SiteConfig(**s) for s in raw.get("sites", [])]
    allowed = {
        int(x) for x in os.getenv("TG_ALLOWED_IDS", "").replace(" ", "").split(",") if x
    }
    droidhub_devices = [
        x.strip() for x in os.getenv("DROIDHUB_DEVICES", "").split(",") if x.strip()
    ]
    return Settings(
        sites=sites,
        tp_token=os.getenv("TP_TOKEN", ""),
        tp_marker=os.getenv("TP_MARKER", ""),
        droidhub_url=os.getenv("DROIDHUB_URL", ""),
        droidhub_token=os.getenv("DROIDHUB_TOKEN", ""),
        droidhub_devices=droidhub_devices,
        llm_backend=os.getenv("LLM_BACKEND", "gemini_local"),
        gemini_cmd=os.getenv("GEMINI_CMD", "gemini"),
        gemini_model=os.getenv("GEMINI_MODEL") or None,
        gemini_timeout=int(os.getenv("GEMINI_TIMEOUT", "180")),
        ssh_host=os.getenv("AGY_SSH_HOST", ""),
        ssh_key=os.path.expanduser(os.getenv("AGY_SSH_KEY", "")),
        agy_account=os.getenv("AGY_ACCOUNT", "acc4"),
        agy_proxy=os.getenv("AGY_PROXY", ""),
        agy_wrapper=os.getenv("AGY_WRAPPER", "/opt/gemini-pool/bin/avia-llm"),
        headless=os.getenv("HEADLESS", "1") != "0",
        top_n=int(os.getenv("TOP_N", "5")),
        tg_token=os.getenv("TG_BOT_TOKEN", ""),
        tg_api_base=os.getenv("TG_API_BASE", ""),
        tg_allowed_ids=allowed,
        tg_open=os.getenv("TG_OPEN", "0") == "1",
        daily_search_limit=int(os.getenv("DAILY_SEARCH_LIMIT", "40")),
        max_parallel_searches=max(1, int(os.getenv("MAX_PARALLEL_SEARCHES", "2"))),
        guard_min_interval=max(0.0, float(os.getenv("GUARD_MIN_INTERVAL", "30"))),
        guard_cache_ttl=max(0.0, float(os.getenv("GUARD_CACHE_TTL", "300"))),
        guard_site_concurrency=max(1, int(os.getenv("GUARD_SITE_CONCURRENCY", "1"))),
        sites_per_search=max(1, int(os.getenv("SITES_PER_SEARCH", "2"))),
        search_priority=os.getenv("SEARCH_PRIORITY", "0") == "1",
        queue_notice=os.getenv("QUEUE_NOTICE", "0") == "1",
        phone_map=parse_phone_map(os.getenv("PHONE_MAP", "")),
        retry_other_phone=os.getenv("RETRY_OTHER_PHONE", "0") == "1",
    )
