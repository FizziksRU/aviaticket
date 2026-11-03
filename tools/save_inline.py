"""Сохранить inline-ответ MCP без перехваченного JSON как снимок-маркер (текст заменён 'x' той же длины).

    python3 tools/save_inline.py SITE PHONE ROUTE URL SECONDS TEXT_LEN FINISHED(0/1) [CAPTCHA(0/1)]
Печатает путь к файлу для tools/ingest.sh.
"""
import datetime as dt
import json
import pathlib
import sys

site, phone, route, url, seconds, text_len, finished = sys.argv[1:8]
captcha = len(sys.argv) > 8 and sys.argv[8] == "1"
out = pathlib.Path(__file__).resolve().parents[1] / "debug" / "samples"
out.mkdir(parents=True, exist_ok=True)
stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
sample = {"url": url, "text": "x" * int(text_len), "truncated": False, "captcha": captcha,
          "json_responses": [], "finished": finished == "1", "seconds": float(seconds), "inline_marker": True}
path = out / f"{site}_{phone}_{route}_{stamp}.json"
path.write_text(json.dumps(sample), encoding="utf-8")
print(path)
