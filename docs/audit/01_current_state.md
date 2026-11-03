# 01. Текущее состояние

Срез: `scale-audit` от `upstream/main` @ `10d8667` (2026-11-03), 100 коммитов: 50 от pazrabi, 50 от `gemini-pool`, из них 49 с префиксом `pool`. Python-кода 9 470 строк в `src/avia/`.

## 1. Запускается ли

| Проверка | Результат |
|---|---|
| venv (Python 3.12.13, uv) + `requirements.txt` | ок |
| `playwright install chromium` | ок, 1 мин 23 с |
| `pytest -q` | **462 passed, 0 failed, 0 skipped, 11,5 с** (если в окружении нет `ALL_PROXY`/`HTTP(S)_PROXY`) |
| `pytest -q` с системным SOCKS-прокси в окружении | 22 failed: `test_bot_flow`, `test_compact`, `test_open_access`, `test_pipeline::test_bot_friendly_error_and_auth`. Бот создаёт `httpx.AsyncClient`, тот подхватывает `ALL_PROXY=socks5h://…` и падает с ImportError без `socksio`. Тесты не изолированы от окружения, см. фазу 4 |
| `python -m avia.cli …` по README | **не работает**: `ModuleNotFoundError: No module named 'avia'`. Пакет не устанавливается: в `pyproject.toml` нет `[build-system]`, а README не говорит про `pip install -e .` или `PYTHONPATH=src`. pytest проходит только за счёт `pythonpath = ["src"]` в `pyproject.toml:7` |
| `PYTHONPATH=src python -m avia.cli Москва Санкт-Петербург 20.11` (desktop headless Playwright, без DroidHub, IP этой машины) | **работает 1 из 6 сайтов, 100 с.** tutu дал 5 офферов за 26 с (от 2 379 ₽). aviasales, yandex_travel и onetwotrip показали капчу или блокировку. kupibilet и ozon_travel вернули 0 офферов с пометкой «неполная выдача». Отсюда понятно, зачем проекту телефоны: голый headless Chromium антиботы режут. Мелочь: CLI вырезает теги, но не раскодирует сущности, поэтому в выводе виден `&lt;сайт&gt;` |

## 2. Карта модулей

```mermaid
flowchart LR
  subgraph TG[Telegram]
    U((Пользователь))
  end
  subgraph Proc[Один процесс python -m avia.bot]
    BOT[bot.py<br/>long polling, busy, _search_sem=2]
    DLG[dialog.py<br/>FSM, sessions в памяти]
    STORE[store.py<br/>ResultStore в памяти, TTL 30 мин]
    SCH[scheduler.py<br/>тик 300 с]
    PIPE[pipeline.py<br/>search: Semaphore 2 на поиск]
    GUARD[guard.py<br/>SearchGuard: lock на сайт, 30 с, кэш 5 мин]
    DH[droidhub.py<br/>пул в глобалах модуля]
    COL[collector.py<br/>Playwright fallback]
    PAR[parsers/*]
    FMT[formatting.py]
    IDS[tutu_ids.py / yandex_ids.py]
  end
  subgraph DB[SQLite, синхронно]
    USERS[(users.sqlite)]
    HIST[(history.sqlite)]
    SUBS[(subscriptions.sqlite)]
  end
  subgraph Ext[Внешние]
    TAPI[Telegram Bot API]
    DHUB[DroidHub API<br/>/api/devices/{id}/fetch_page]
    PH1[[realme]]
    PH2[[redmi]]
    SITES[(aviasales, kupibilet, tutu,<br/>yandex_travel, onetwotrip, ozon)]
    TUTUS[suggester-avia.tutu.ru]
  end
  U <--> TAPI <--> BOT
  BOT --> DLG --> STORE
  DLG --> SUBS
  BOT --> USERS
  BOT --> HIST
  BOT --> PIPE
  SCH --> PIPE
  SCH --> SUBS
  BOT --> SCH
  PIPE --> GUARD --> DH --> DHUB --> PH1 & PH2 --> SITES
  GUARD --> COL --> SITES
  PIPE --> PAR
  PIPE --> IDS --> TUTUS
  BOT --> FMT
```

Кто кого вызывает:
- `bot.py` → `dialog.Dialog`, `pipeline.search`, `scheduler.Scheduler`, `ResultStore`, `SubscriptionStore`, `UserStore`, `PriceHistory`, `formatting`, `filters`, `offers`, `guard.friendly_error`.
- `dialog.py` → `parsing` (город, дата, подсказки), `store`, `subscriptions`, `formatting`, `criteria`. Синхронный, без сети.
- `pipeline.py` → `guard.SearchGuard` (глобальный объект `pipeline.guard`), `droidhub`, `collector.fetch_snapshot` (если DroidHub не настроен), `parsers.PARSERS`, `extract` (LLM, по умолчанию выключен), `travelpayouts`, `sites.build_url`, `tutu_ids.resolve_city`, `yandex_ids.city_id`.
- `scheduler.py` → `subscriptions` (выборка due, запись цен), `history`, `filters`, `offers`, `formatting`.
- `cli.py` → `pipeline.search`, `formatting.format_report`. `recon.py` → `collector` (разведка), `smoke.py` → `extract`.

## 3. Поток одного поиска

```mermaid
sequenceDiagram
  autonumber
  participant U as Пользователь
  participant B as bot.py
  participant D as dialog.py
  participant P as pipeline.search
  participant G as SearchGuard
  participant H as droidhub.py
  participant DH as DroidHub
  participant S as Сайт (в Chrome телефона)
  U->>B: /price или кнопка «Багаж» (getUpdates, create_task)
  B->>B: _admit → users.register (SQLite, синхронно)
  B->>D: on_text / on_callback → SearchRequest
  B->>B: busy-проверка, users.try_use_search (лимит 40/сутки)
  B->>U: sendMessage «Ищу…, около минуты»
  B->>B: async with _search_sem (2 на весь бот, общий с планировщиком)
  B->>P: search(q)
  loop 6 сайтов, не больше 2 одновременно (Semaphore(2) в search)
    P->>G: run(site, q): кэш 5 мин → схлопывание → lock сайта
    G->>G: ждать min_interval 30 с с прошлого старта этого сайта
    G->>H: fetch_page_snapshot_any(candidates)
    H->>H: acquire телефон (auto: realme/redmi; tutu/yandex/ott: только realme)
    H->>DH: POST /api/devices/{id}/fetch_page {url, wait_seconds, capture_json, wait_for}
    DH->>S: открыть URL, ждать wait_for или wait_seconds
    DH-->>H: {text, json_responses, captcha, finished}
    H-->>G: Snapshot
    G->>P: парсер → офферы (0 офферов → пауза 3 с и повтор внутри lock)
  end
  P-->>B: list[SiteResult]
  B->>B: history.annotate (to_thread), фильтры, build_compact_report
  B->>U: sendMessage x N (≤4000 символов на часть), deleteMessage «Ищу…»
  B->>B: store.put (в памяти)
```

## 4. Где живёт состояние

| Что | Где | Переживает рестарт | Мешает 2+ процессам |
|---|---|---|---|
| Сессии диалога `Dialog.sessions` | память, `dialog.py:145` | нет | да |
| Результаты поиска `ResultStore._data` (кнопки «ещё», «подробнее», «отслеживать») | память, `store.py:40`, удаление только при `get` | нет | да |
| `Bot.busy`, `Bot._search_sem` | память, `bot.py:50,52` | нет | да (лимит параллелизма станет N×2) |
| Кэш, inflight, lock и `last_starts` guard | память, глобал `pipeline.guard` (`pipeline.py:31`) | нет | да |
| Пул телефонов: `_device_locks`, `_busy_devices`, `_pool_condition`, `_rr_counter` | глобалы модуля, `droidhub.py:17-23` | нет | да (две копии будут слать на один телефон) |
| Кэш id городов tutu | глобал `tutu_ids._CACHE` | нет | нет (только кэш) |
| Пользователи, дневной счётчик | `state/users.sqlite` | да | частично (гонка read-modify-write в `users.py:94-104`) |
| История цен | `state/history.sqlite` | да | да |
| Подписки, цены по подпискам, уведомления | `state/subscriptions.sqlite` | да | планировщик в каждом процессе = двойной опрос |
| Профили Playwright | `state/profiles/<site>` | да | нет |
| Отладочные снимки пустой выдачи | `debug/*.json` | да | нет |

## 5. Сеть, блокировки, семафоры, кэши

| Механизм | Где | Значение | Настраивается |
|---|---|---|---|
| Общий семафор поисков (люди + планировщик) | `bot.py:52`, `bot.py:166`, `bot.py:338` | 2 | `MAX_PARALLEL_SEARCHES` |
| Семафор сайтов внутри поиска | `pipeline.py:358` | 2 | нет |
| Lock на сайт, держится **всё время fetch + повтор** | `guard.py:121` | 1 запрос на сайт на процесс | нет |
| Пауза между стартами сайта | `guard.py:45,129-136` | 30 с | нет |
| Кэш результатов сайта | `guard.py:46` | 300 с, ключ (сайт, Query) | нет |
| Повтор при сетевой ошибке | `guard.py:141-143` | 1 раз, +2 с | нет |
| Повтор при 0 офферов | `pipeline.py:280-293` | 1 раз, +3 с, внутри lock сайта | нет |
| Lock на телефон + пул с round-robin | `droidhub.py:50-143` | 1 запрос на телефон | `DROIDHUB_DEVICES` |
| Ожидание на телефоне | `droidhub.py:184` | `phone_wait_seconds`, потолок 60 | `sites.yaml` |
| HTTP-таймаут к DroidHub | `droidhub.py:185` | wait+60 с, **новый AsyncClient на каждый запрос** | нет |
| Тик планировщика | `scheduler.py:184` | 300 с, группы идут последовательно | нет |
| Интервал опроса подписки | `subscriptions.py:151-158` | 1800 / 2700 / 3600 с (≤7 / 8–30 / >30 дней до вылета) | нет |
| TTL результатов для кнопок | `store.py:37` | 1800 с | нет |
| Лимиты пользователя | `config.py:66`, `dialog.py:39` | 40 поисков в сутки, 1 подписка | `DAILY_SEARCH_LIMIT`; подписка нет |
| Telegram long polling | `bot.py:357-372` | timeout 50, `create_task` на каждый апдейт без лимита | нет |

Сетевые вызовы: Telegram Bot API (`bot.py:65`, общий клиент, timeout 70), DroidHub (`droidhub.py:197-202`), подсказчик tutu (`tutu_ids.py:91`, первый раз на город, потом кэш процесса), Travelpayouts (`parsers/travelpayouts.py`, источник выключен), Playwright к сайтам (`collector.py:327`, только без DroidHub), LLM по SSH (`extract.py`, выключен).

## 6. Источники (`config/sites.yaml`)

| Сайт | Вкл. | Метод | Телефон | `phone_wait_seconds` | `phone_wait_for` | `capture_json` | Парсер | Туда-обратно | Особенности |
|---|---|---|---|---|---|---|---|---|---|
| aviasales_cache | **нет** | api, Travelpayouts Data API | — | — | — | — | travelpayouts | нет | кэш ~48 ч, нужен `TP_TOKEN` |
| aviasales | да | телефон / Playwright | auto | 45 | `"last_update_timestamp":0` | `/search/v3.2/results` | aviasales | да | единственный с признаком конца выдачи |
| kupibilet | да | телефон / Playwright | auto | 30 | — | `/frontend_search` | kupibilet | да | |
| tutu | да | телефон / Playwright | **realme** | 25 | — | `/avia/offers/v2` | tutu | да | `id_resolver: tutu` (HTTP-подсказчик + статическая таблица); `replay_post` (cheapest, limit 100) работает **только в Playwright-пути**, в телефонном игнорируется |
| yandex_travel | да | телефон / Playwright | **realme** | 30 | — | `/api/avia/search/results` | yandex_travel | да | `id_resolver: yandex`: статическая таблица на 20 городов (`yandex_ids.py:5-26`), для остальных городов сайт падает с ошибкой |
| onetwotrip | да | телефон / Playwright | **realme** | 30 | — | `/search/v3` | onetwotrip | да | |
| ozon_travel | да | телефон / Playwright | auto | 30 | — | `aviaSearchResult` | ozon_travel | да | |

Сумма `phone_wait_seconds` по 6 сайтам 190 с, из них 85 с приходится на сайты, прибитые к realme. Как DroidHub ведёт себя без `wait_for` (ждёт полный `wait_seconds` или возвращает раньше), измеряется в фазе 2.

## 7. Переменные окружения

| Переменная | Читается в `config.py` | `.env.example` | README |
|---|---|---|---|
| TG_BOT_TOKEN, TG_ALLOWED_IDS | да | да | да |
| **TG_OPEN** | да (`:105`) | **нет** | нет; README утверждает обратное («бот отвечает только id из TG_ALLOWED_IDS») |
| **DAILY_SEARCH_LIMIT** | да (`:106`, 40) | **нет** | нет |
| **MAX_PARALLEL_SEARCHES** | да (`:107`, 2) | **нет** | нет |
| **DROIDHUB_URL, DROIDHUB_TOKEN, DROIDHUB_DEVICES** | да (`:82-91`) | **нет** | нет, телефонов в README нет вообще |
| TP_TOKEN, TP_MARKER | да | да | да |
| HEADLESS, TOP_N | да | да | частично |
| LLM_BACKEND, GEMINI_CMD, GEMINI_MODEL, GEMINI_TIMEOUT | да | да | да |
| AGY_SSH_HOST, AGY_SSH_KEY, AGY_ACCOUNT, AGY_PROXY | да | да | да |
| **AGY_WRAPPER** | да (`:100`) | **нет** | нет |

В коде зашиты и не настраиваются: `min_interval` 30 с и TTL кэша 300 с у guard, семафор сайтов 2, пауза повтора 3 с, тик планировщика 300 с, интервалы опроса подписок, `MAX_SUBSCRIPTIONS=1`, TTL `ResultStore` 1800 с, потолок ожидания на телефоне 60 с.

## 8. Расхождения документации и кода

| # | Где | Что написано | Как на самом деле |
|---|---|---|---|
| 1 | README «Установка» | `pip install -r requirements.txt`, затем `python -m avia.cli` | падает с `ModuleNotFoundError`, нужен `PYTHONPATH=src` или `pip install -e .` |
| 2 | README, схема | сбор только через api или Playwright | основной путь в проде идёт через телефоны DroidHub, о них ни слова |
| 3 | README «Что работает сразу» | aviasales_cache | выключен (`sites.yaml:18`) |
| 4 | README «Безопасность» | «Бот отвечает только id из TG_ALLOWED_IDS» | при `TG_OPEN=1` отвечает всем (`bot.py:243-249`) |
| 5 | README | `cd avia-bot` | репозиторий называется `aviaticket` |
| 6 | README, разделы про LLM | пути `/opt/gemini-pool`, аккаунт `acc4`, порт прокси пула | инфраструктурные детали в публичном репо |
| 7 | `config.py:53-54` | комментарии с IP сервера (логин root) и именем SSH-ключа | **IP сервера и имя ключа в публичном репо** |
| 8 | `sites.yaml:50` | «Заглушки: шаблон URL нужно снять руками…» (tutu, yandex) | оба сайта рабочие, с парсерами |
| 9 | `sites.yaml:85` | onetwotrip «Выключен, пока нет парсера» | `enabled: true`, парсер есть |
| 10 | `sites.yaml:99` | ozon «Выключен: антибот…» | `enabled: true` |
| 11 | `sites.yaml:13` | aviasales_cache «Работает сразу» | `enabled: false` |
| 12 | `dialog.py:41` | «все пять сайтов» | в множестве 6 сайтов |
| 13 | `store.py:35` | «SQLite понадобится при реализации отслеживания» | отслеживание уже в SQLite, а результаты по-прежнему только в памяти |
| 14 | `bot.py:161` | «это займёт около минуты» | фактическое время см. фазу 2 |
| 15 | `guard.py:172` | пользователю показывается `python -m avia.recon <сайт> ... --show` | в открытом режиме внутренняя команда уходит посторонним людям |
| 16 | `.env.example` | нет 7 переменных (см. §7) | |
