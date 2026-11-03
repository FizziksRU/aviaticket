#!/usr/bin/env python3
"""Локальный шлюз секретов для живых тестов приложения. Запускает ВЛАДЕЛЕЦ:

    python3 tools/tg_gateway.py start|stop|status   (из ~/avia-scale/aviaticket, где лежит .env)

Слушает только 127.0.0.1:8787 и подставляет секреты из .env:
  /bot<любое>/<method>  → https://api.telegram.org/bot<TG_BOT_TOKEN>/<method>
  /droidhub/<path>      → <DROIDHUB_URL>/<path> с Authorization: Bearer <DROIDHUB_TOKEN>
                          (fetch_page ограничен PHONE_BUDGET_PER_HOUR, по умолчанию 40, журнал phone_budget.log)
  /meta                 → {"owner_id": TG_REPORT_CHAT_ID, "droidhub": true|false}
Сообщение «стоп» от владельца, увиденное в ответе getUpdates, создаёт ~/avia-scale/STOP.
Агент запускает приложение с фиктивными токенами и адресами шлюза и сам секретов не видит.
При старте шлюз останавливает tools/tg_relay.sh: у бота может быть только один потребитель getUpdates.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BOX = Path(os.environ.get("AVIA_OUTBOX", REPO.parent / "outbox"))
PID = BOX / "gateway.pid"
LOG = BOX / "gateway.log"
STOP_FILE = REPO.parent / "STOP"
BUDGET_LOG = Path(os.environ.get("AVIA_BUDGET_LOG", REPO.parent / "work" / "docs" / "audit" / "phone_budget.log"))
HOST, PORT = "127.0.0.1", int(os.environ.get("AVIA_GATEWAY_PORT", "8787"))
TIMEOUT = 130


def _env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


ENV: dict[str, str] = {}


def _scrub(data: bytes) -> bytes:
    for key in ("TG_BOT_TOKEN", "DROIDHUB_TOKEN"):
        val = ENV.get(key, "")
        if val:
            data = data.replace(val.encode(), b"<TOKEN>")
    return data


def _budget_ok(limit: int) -> bool:
    if not BUDGET_LOG.exists():
        return True
    now = dt.datetime.now(dt.timezone.utc)
    used = 0
    for line in BUDGET_LOG.read_text(encoding="utf-8").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) > 1 and parts[1] in ("mcp_fetch_page", "gateway_fetch_page"):
            ts = dt.datetime.fromisoformat(parts[0].replace("Z", "+00:00"))
            used += (now - ts).total_seconds() < 3600
    return used < limit


def _watch_stop(body: bytes) -> None:
    owner = ENV.get("TG_REPORT_CHAT_ID", "")
    try:
        for u in json.loads(body).get("result", []):
            m = u.get("message") or {}
            if str(m.get("from", {}).get("id")) == owner and (m.get("text") or "").strip().lower().lstrip("/") in ("стоп", "stop"):
                STOP_FILE.write_text(dt.datetime.now(dt.timezone.utc).isoformat() + " from telegram via gateway\n")
    except (ValueError, AttributeError):
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:  # без токенов: путь уже без секрета
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {self.command} {self.path.split('?')[0]} {fmt % args}\n")

    def _reply(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self, url: str, headers: dict[str, str]) -> tuple[int, bytes, str]:
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else None
        if self.headers.get("Content-Type"):
            headers["Content-Type"] = self.headers["Content-Type"]
        req = urllib.request.Request(url, data=data, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type", "application/json")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return 502, json.dumps({"ok": False, "description": f"gateway: {type(e).__name__}"}).encode(), "application/json"

    def _handle(self) -> None:
        path = self.path
        if path == "/meta":
            meta = {"owner_id": ENV.get("TG_REPORT_CHAT_ID", ""), "droidhub": bool(ENV.get("DROIDHUB_URL"))}
            return self._reply(200, json.dumps(meta).encode())
        if path.startswith("/bot") and "/" in path[4:]:
            method = path[4:].split("/", 1)[1]
            status, body, ctype = self._forward(f"https://api.telegram.org/bot{ENV['TG_BOT_TOKEN']}/{method}", {})
            if method.startswith("getUpdates"):
                _watch_stop(body)
            return self._reply(status, _scrub(body), ctype)
        if path.startswith("/droidhub/"):
            if not ENV.get("DROIDHUB_URL"):
                return self._reply(503, b'{"detail":"DROIDHUB_URL not configured"}')
            rest = path[len("/droidhub/"):]
            if rest.endswith("/fetch_page"):
                if not _budget_ok(int(os.environ.get("PHONE_BUDGET_PER_HOUR", "40"))):
                    return self._reply(429, b'{"detail":"phone budget exhausted"}')
                device = rest.split("/")[-2]
                with BUDGET_LOG.open("a", encoding="utf-8") as fh:
                    fh.write(f"{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\tgateway_fetch_page\t{device}\t-\t-\tapp via gateway\n")
            url = ENV["DROIDHUB_URL"].rstrip("/") + "/" + rest
            status, body, ctype = self._forward(url, {"Authorization": f"Bearer {ENV.get('DROIDHUB_TOKEN', '')}"})
            return self._reply(status, _scrub(body), ctype)
        return self._reply(404, b'{"detail":"unknown route"}')

    do_GET = _handle
    do_POST = _handle


def _running() -> int | None:
    try:
        pid = int(PID.read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    BOX.mkdir(parents=True, exist_ok=True)
    if cmd == "status":
        pid = _running()
        print(f"running pid {pid} on {HOST}:{PORT}" if pid else "stopped")
    elif cmd == "stop":
        pid = _running()
        if pid:
            os.kill(pid, signal.SIGTERM)
        PID.unlink(missing_ok=True)
        print("gateway остановлен" if pid else "gateway не запущен")
    elif cmd == "start":
        if _running():
            print("gateway уже работает")
            return
        subprocess.run([str(HERE / "tg_relay.sh"), "stop"], check=False)
        proc = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "run"],
                                stdout=open(LOG, "a"), stderr=subprocess.STDOUT, start_new_session=True)
        PID.write_text(str(proc.pid))
        print(f"gateway запущен (pid {proc.pid}) на {HOST}:{PORT}; relay остановлен")
    elif cmd == "run":
        ENV.update(_env())
        if not ENV.get("TG_BOT_TOKEN"):
            raise SystemExit("в .env нет TG_BOT_TOKEN")
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
