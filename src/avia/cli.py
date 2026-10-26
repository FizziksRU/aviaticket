"""Запуск из консоли без Telegram: python -m avia.cli Москва Алматы 10.12"""
from __future__ import annotations

import asyncio
import re
import sys

from .config import load_settings
from .formatting import format_report
from .parsing import ParseError, parse_query
from .pipeline import search


def main():
    try:
        q = parse_query(" ".join(sys.argv[1:]))
    except ParseError as e:
        sys.exit(str(e))
    s = load_settings()
    s.headless = "--show" not in sys.argv  # --show: видеть браузер
    results = asyncio.run(search(q, s))
    print(re.sub(r"<[^>]+>", "", format_report(q, results, s.top_n)))


if __name__ == "__main__":
    main()
