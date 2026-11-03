# PROGRESS

**Статус:** фаза 1 (инвентаризация), в работе.
**Обновлено:** 2026-11-03 20:00 UTC

## Как продолжить после обрыва сессии
- Работа идёт в worktree `~/avia-scale/work`, ветка `scale-audit`. `~/avia-scale/aviaticket` — checkout владельца с `.env`, туда не заходить.
- Отчёты в Telegram: `tools/report.sh "текст"` (relay владельца, `~/avia-scale/aviaticket/tools/tg_relay.sh status`).
- Стоп-кран: файлы `~/avia-scale/STOP` или `~/avia-scale/work/STOP`, сообщения владельца в `~/avia-scale/outbox/inbox.log`.
- Тесты: `env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy .venv/bin/python -m pytest -q`.
- Телефоны: только MCP `pool-avia`, бюджет ≤40 fetch/ч, лог в `docs/audit/phone_budget.log`.

## Фазы
| # | Фаза | Статус | Артефакт |
|---|------|--------|----------|
| 0 | Форк, секреты, Telegram | ✅ | `tools/`, `DECISIONS.md` |
| 1 | Инвентаризация, тесты, CLI | ⏳ | `docs/audit/01_current_state.md` |
| 2 | DroidHub: контракт и замеры | — | `docs/audit/02_droidhub.md` |
| 3 | Модель мощности и моки | — | `docs/audit/03_capacity.md`, `loadtest/` |
| 4 | Техдолг | — | `docs/audit/04_tech_debt.md` |
| 5 | Roadmap и ADR | — | `docs/audit/05_roadmap.md`, `docs/adr/` |
| 6 | Быстрые улучшения | — | ветки `scale-audit/*`, `docs/audit/06_changes.md` |
| 7 | Итог | — | `docs/audit/00_SUMMARY.md` |

## Журнал
- 19:48 форк и клон, ветка `scale-audit`, инструменты секретов и relay.
- 19:56 владелец выполнил `setup_secrets.sh`; 19:59 relay запущен, первое сообщение доставлено.
- 19:55 venv 3.12, зависимости, Chromium; pytest 462 passed / 0 failed / 0 skipped за 11,5 с (без прокси-переменных; с ними 22 failed).
