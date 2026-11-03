#!/usr/bin/env bash
# Проверка staged-изменений на секреты по шаблонам (без чтения .env).
# Использование: tools/secret_scan.sh   (перед каждым git commit)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
if git diff --cached --name-only | grep -qE '(^|/)\.env$|\.secrets_ok$|id_rsa|\.pem$'; then
  echo "secret_scan: в индексе запрещённый файл" >&2; exit 1
fi
PATTERNS='[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|sk-[A-Za-z0-9]{32,}|Bearer [A-Za-z0-9._-]{20,}'
HITS=$(git diff --cached -U0 | grep -E '^\+' | grep -vE '^\+\+\+' | grep -nE "$PATTERNS" || true)
if [ -n "$HITS" ]; then
  echo "secret_scan: найдены похожие на секреты строки:" >&2
  echo "$HITS" | sed -E 's/([A-Za-z0-9_-]{6})[A-Za-z0-9_-]{10,}/\1…/g' >&2; exit 1
fi
if command -v gitleaks >/dev/null; then gitleaks protect --staged --no-banner --redact -v >/dev/null; fi
echo "secret_scan: ok"
