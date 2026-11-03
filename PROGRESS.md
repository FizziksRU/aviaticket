# PROGRESS

**Статус:** фаза 2 ✅, 3 ⏳ (B/C считаются), 4 ✅, 6 ⏳ (6 веток готовы).
**Обновлено:** 2026-11-03 21:14 UTC

## Как продолжить после обрыва сессии
- Работа идёт в worktree `~/avia-scale/work`, ветка `scale-audit`. `~/avia-scale/aviaticket` — checkout владельца с `.env`, туда не заходить.
- **Отчёты в Telegram НЕ слать** (владелец: бот только для тестов приложения). Relay владельца нужен только для «стоп» → `~/avia-scale/STOP`.
- Живой прогон приложения: владелец запускает `python3 tools/tg_gateway.py start` в `~/avia-scale/aviaticket` (после `git checkout --detach scale-audit`), агент — `PYTHONPATH=src .venv/bin/python tools/run_bot_gateway.py`.
- Стоп-кран: файлы `~/avia-scale/STOP` или `~/avia-scale/work/STOP`, сообщения владельца в `~/avia-scale/outbox/inbox.log`.
- Тесты: `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy .venv/bin/python -m pytest -q`.
- Телефоны: только MCP `pool-avia`, бюджет ≤40 fetch/ч, лог в `docs/audit/phone_budget.log`.

## Фазы
| # | Фаза | Статус | Артефакт |
|---|------|--------|----------|
| 0 | Форк, секреты, Telegram | ✅ | `tools/`, `DECISIONS.md` |
| 1 | Инвентаризация, тесты, CLI | ✅ | `docs/audit/01_current_state.md` |
| 2 | DroidHub: контракт и замеры | ✅ | `docs/audit/02_droidhub.md` |
| 3 | Модель мощности и моки | ⏳ | `docs/audit/03_capacity.md`, `loadtest/` |
| 4 | Техдолг | ✅ | `docs/audit/04_tech_debt.md` |
| 5 | Roadmap и ADR | — | `docs/audit/05_roadmap.md`, `docs/adr/` |
| 6 | Быстрые улучшения | ⏳ | ветки `scale-audit-<тема>` в worktree `~/avia-scale/dev`, `docs/audit/06_changes.md` |
| 7 | Итог | — | `docs/audit/00_SUMMARY.md` |

## Журнал
- 19:48 форк и клон, ветка `scale-audit`, инструменты секретов и relay.
- 19:56 владелец выполнил `setup_secrets.sh`; 19:59 relay запущен, первое сообщение доставлено.
- 19:55 venv 3.12, зависимости, Chromium; pytest 462 passed / 0 failed / 0 skipped за 11,5 с (без прокси-переменных; с ними 22 failed).
- 20:00 фаза 1 закоммичена (c9cf57f), отчёт ушёл в Telegram. Владелец: «бот не для отчётов, а для тестов приложения» → отчёты в бот больше не шлю (DECISIONS #10).
- 20:01–20:24 фаза 2: 37 fetch_page через MCP (лог docs/audit/phone_budget.log), разбор кодом бота: tools/phase2.py → docs/audit/phase2_runs.csv, снимки в debug/samples/.
  Итоги: realme быстрее redmi в 1,5–3 раза; ozon@realme 0/2, aviasales@redmi 1/3, onetwotrip@redmi 0/3 (ошибка MCP), onetwotrip RT@realme и yandex RT@realme без JSON.
  yandex без wait_for: 2/20 партнёров, мин. цена 2404; с wait_for '"cont":null' 39 с, 20/20, 1485 офферов, мин. 2194.
- Следующее: в 21:05 UTC (когда освободится бюджет) серия на деградацию: 6×kupibilet@realme + 6×tutu@redmi подряд, плюс добор aviasales@redmi, ozon@realme. Пока пишу 02_droidhub.md, мок DroidHub и мок Telegram (loadtest/).
- 20:25–20:40 фаза 3: `loadtest/` — виртуальное время (vclock.py), мок DroidHub (замеры фазы 2, реальные снимки с подменой дат), мок Telegram (лимиты 30/с, 1/с на чат, 429), виртуальные пользователи по кнопкам диалога, раннер `python -m loadtest.run`. Матрица A: `loadtest/matrix.sh` → `loadtest/results/`.
  Первые цифры (текущий код): 10 чел/2 тел p50 5 мин p95 11 мин; 50/2 p50 27 мин p95 52 мин; 10/16 p95 5 мин (потолок guard 30 с × N).
- 20:35 шлюз секретов `tools/tg_gateway.py` + `tools/run_bot_gateway.py` проверены на фиктивном .env. Субагенты: ревью парсеров и ревью бота/диалога (фаза 4) запущены в фоне.
- 20:40 ревью бота/диалога и ревью парсеров (субагенты) готовы; полный текст парсеров: `/private/tmp/claude-501/-Users-a1/de8f9cd5-5a18-4c32-a241-c5b37d6592ed/tasks/afb6cd4c03f5199f3.output` — перенести в 04_tech_debt.md (P0: нет сигнала о поломке парсера; P1: onetwotrip падает целиком на кривом элементе, «без багажа» при отсутствии поля, ozon даты из запроса, yandex дубли офферов, guard кеширует partial, парсер блокирует event loop).
- 20:44 ветка `scale-audit-telegram` (4b351e3): разбиение HTML на 4096, ретрай 429, fallback без parse_mode. Ветка `scale-audit-queue` (464153e): PrioritySlots (SEARCH_PRIORITY), «Вы N-й в очереди» (QUEUE_NOTICE), GUARD_MIN_INTERVAL/CACHE_TTL/SITE_CONCURRENCY, SITES_PER_SEARCH. 473 passed. Обе в `~/avia-scale/dev` (worktree), от `scale-audit`.
- 20:45 матрица A (patience 4 ч) готова: `python3 loadtest/table.py 'loadtest/results/A_*.json'`. 10/2 p50 5 мин; 50/4 p50 20 мин p95 37 мин; 100/8 p50 37 мин p95 71 мин; 50/16 всё ещё p50 15 мин → потолок guard, телефоны не помогают.
- 20:48 запущены B и C (сутки, users 10/50/100 × phones 2/4) параллельно: `JOBS=6 loadtest/par.sh "B C" "10 50 100" "2 4"` → `loadtest/results/matrix_BC.txt` и `B_*.json`, `C_*.json`. B u10p2 ≈ 5 мин реального времени.
- 20:50–21:13 фаза 4 закоммичена (`04_tech_debt.md`), IP сервера убран из отчётов (DECISIONS #15). Ветки фазы 6 в `~/avia-scale/dev` (все от `scale-audit`, тесты зелёные):
  `scale-audit-telegram` 4b351e3 · `scale-audit-queue` 464153e · `scale-audit-dialog` 6809085 (P0 застревание в SEARCHING, затирание сессии, возврат лимита, put до send, _spawn + graceful shutdown) · `scale-audit-phones` fe9948d (PHONE_MAP, RETRY_OTHER_PHONE) · `scale-audit-health` 2ff782e (/status, HEALTH_ALERTS, строка лога на поиск) · `scale-audit-docs` d297001 (README, .env.example + тест, sites.yaml, config.py, капча без recon).
- 21:00–21:12 серия на деградацию: 6×kupibilet@realme 5,2–6,3 с, 6×tutu@redmi 9,8–12 с, деградации нет, 0 капч, realme +0,9 °C, экран realme остался включён. Добор: aviasales@redmi 2/2 успешно (теперь 3/5), ozon@realme 1/2 (теперь 1/4). Мок переведён на профиль v2 (DECISIONS #16). 02_droidhub.md закрыт.
- Результаты MCP fetch_page > лимита сами сохраняются в `~/.claude/projects/.../tool-results/`, разбор: `tools/ing2.sh TS_A SITE PHONE TS_B SITE PHONE`.
- Дальше: ветка `scale-audit-deploy` (Dockerfile, compose), интеграция `scale-audit-all`, прогоны «до/после» на v2 (`--phone-map optimal`, флаги из .env), таблица B/C, 05_roadmap + ADR, 06_changes, 00_SUMMARY.
