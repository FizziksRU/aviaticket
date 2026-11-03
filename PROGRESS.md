# PROGRESS

**Статус:** фаза 2 (DroidHub), базовая серия замеров собрана, жду окно бюджета для серии на деградацию.
**Обновлено:** 2026-11-03 20:40 UTC

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
| 2 | DroidHub: контракт и замеры | ⏳ | `docs/audit/02_droidhub.md` |
| 3 | Модель мощности и моки | — | `docs/audit/03_capacity.md`, `loadtest/` |
| 4 | Техдолг | — | `docs/audit/04_tech_debt.md` |
| 5 | Roadmap и ADR | — | `docs/audit/05_roadmap.md`, `docs/adr/` |
| 6 | Быстрые улучшения | — | ветки `scale-audit/*`, `docs/audit/06_changes.md` |
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
