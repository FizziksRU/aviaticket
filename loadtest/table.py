"""Сводная таблица по results/*.json: python loadtest/table.py [glob]."""
from __future__ import annotations

import glob
import json
import sys


def row(d: dict) -> str:
    lat = d.get("result_latency", {})
    wo = d.get("wait_over", {})
    util = d.get("phones_util", {})
    top = max(util.values()) if util else 0.0
    mean = sum(util.values()) / len(util) if util else 0.0
    po = d.get("phone_outcomes", {})
    ps = d.get("pipeline_searches", {})
    outcomes = ",".join(f"{k}:{v}" for k, v in sorted(d.get("outcomes", {}).items()))
    return (f"| {d['scenario']} | {d['users']} | {d['phones']} | {lat.get('p50', '-')} | {lat.get('p95', '-')} | "
            f"{lat.get('max', '-')} | {wo.get('>5min', '-')} | {wo.get('>15min', '-')} | {outcomes} | "
            f"{top:.2f}/{mean:.2f} | {po.get('miss', 0)}/{sum(po.values())} | {ps.get('user', 0)}+{ps.get('scheduler', 0)} | "
            f"{d.get('memory', {}).get('peak_mb', '-')} |")


def main() -> None:
    pattern = sys.argv[1] if len(sys.argv) > 1 else "loadtest/results/*.json"
    files = sorted(glob.glob(pattern), key=lambda f: (f.split("/")[-1].split("_")[0], *(int(x[1:]) for x in f.split("/")[-1].removesuffix(".json").split("_")[1:3] if x[1:].isdigit())))
    print("| сц. | users | phones | p50, с | p95, с | max, с | >5 мин | >15 мин | исходы | util max/mean | miss/запросов | поисков user+sched | RAM peak, MB |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for f in files:
        print(row(json.load(open(f))))


if __name__ == "__main__":
    main()
