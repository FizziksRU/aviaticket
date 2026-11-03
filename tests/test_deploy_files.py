"""Файлы деплоя: секреты не попадают в образ, бот не работает от root, данные на томах."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerignore_keeps_secrets_out_of_image():
    ignored = {line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()}
    assert {".env", ".secrets_ok", "state", "debug", ".git"} <= ignored


def test_dockerfile_runs_bot_as_unprivileged_user():
    text = (ROOT / "Dockerfile").read_text()
    assert "USER avia" in text
    assert 'CMD ["python", "-m", "avia.bot"]' in text
    assert "COPY .env" not in text


def test_compose_reads_env_file_and_mounts_state():
    text = (ROOT / "docker-compose.yml").read_text()
    assert "env_file: .env" in text
    assert "./state:/app/state" in text
    assert "TOKEN" not in text
