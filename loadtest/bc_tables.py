"""Таблицы сценариев B/C для 03_capacity.md §5: python3 loadtest/bc_tables.py."""
from __future__ import annotations

import json
from pathlib import Path

RESULTS = Path(__file__).parent / "results"
VARIANTS = [
    ("notrack_v2base", "без подписок, текущий код"),
    ("v2base", "текущий код"),
    ("v2stage10", "флаги ступени 10"),
    ("v2parallel", "+ параллельность, guard 30 с"),
    ("v2tuned", "+ guard 10 с"),
]


def _min(seconds: float) -> str:
    return f"{seconds / 60:.1f}".replace(".", ",")


def _num(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def _load(name: str) -> dict | None:
    path = RESULTS / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def v1_table() -> list[str]:
    lines = ["| Сц. | Людей | Тел. | Люди p50 / p95 / max, мин | Поисков людей + подписок | Отказ по лимиту | Загрузка тел. max / ср. | RAM peak, МБ |",
             "|---|---|---|---|---|---|---|---|"]
    for scen in ("B", "C"):
        for users in (10, 50, 100):
            for phones in (2, 4):
                d, label = _load(f"{scen}_u{users}_p{phones}"), scen
                if d is None:
                    d, label = _load(f"{scen}_u{users}_p{phones}_v2base"), f"{scen} (v2)"
                if d is None:
                    continue
                lat, util, ps = d["result_latency"], d["phones_util"], d["pipeline_searches"]
                refused = d["outcomes"].get("refused", 0)
                lines.append(
                    f"| {label} | {users} | {phones} | {_min(lat['p50'])} / {_min(lat['p95'])} / {_min(lat['max'])} | "
                    f"{ps['user']} + {ps['scheduler']} | {refused} | {_num(max(util.values()))} / {_num(sum(util.values()) / len(util))} | "
                    f"{d['memory']['peak_mb']:.0f} |")
    return lines


def v2_table() -> list[str]:
    lines = ["| Людей | Тел. | Вариант | Люди p50 / p95 / max, мин | Опросов подписок за сутки | Интервал опроса / обещанный, p50 / p95 | Самый длинный пропуск, p95, мин |",
             "|---|---|---|---|---|---|---|"]
    for users in (100, 50):
        for phones in (2, 4):
            for suffix, label in VARIANTS:
                d = _load(f"B_u{users}_p{phones}_{suffix}")
                if d is None:
                    continue
                lat, si = d["result_latency"], d.get("sched_intervals", {})
                polls = d["pipeline_searches"]["scheduler"]
                lag = (f"{si['gap_to_nominal_p50']}× / {si['gap_to_nominal_p95']}×".replace(".", ",")
                       if "gap_to_nominal_p50" in si else "—")
                gap = f"{si['max_gap_min_p95']:.0f}" if "max_gap_min_p95" in si else "—"
                lines.append(f"| {users} | {phones} | {label} | {_min(lat['p50'])} / {_min(lat['p95'])} / {_min(lat['max'])} | "
                             f"{polls or '—'} | {lag} | {gap} |")
    return lines


if __name__ == "__main__":
    print("\n".join(v1_table()))
    print()
    print("\n".join(v2_table()))
