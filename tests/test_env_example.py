"""Каждая переменная, которую читает config.py, описана в .env.example (и наоборот)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_env_example_matches_config():
    code = set(re.findall(r'os\.getenv\("([A-Z_]+)"', (ROOT / "src/avia/config.py").read_text(encoding="utf-8")))
    example = set(re.findall(r"^#?\s*([A-Z_]+)=", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M))
    assert code - example == set(), "добавьте в .env.example"
    assert example - code == set(), "переменная из .env.example нигде не читается"
