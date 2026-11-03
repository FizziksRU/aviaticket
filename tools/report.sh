#!/usr/bin/env bash
# Отчёт владельцу без доступа к секретам: кладёт текст в outbox, отправляет tools/tg_relay.sh.
#   tools/report.sh "текст"
set -euo pipefail
BOX="${AVIA_OUTBOX:-$(cd "$(dirname "$0")/../.." && pwd)/outbox}"
mkdir -p "$BOX/pending"
F="$BOX/pending/$(date -u +%Y%m%dT%H%M%S)-$$.txt"
printf '%s' "${1:?usage: tools/report.sh \"text\"}" > "$F.tmp" && mv "$F.tmp" "$F"
echo "queued $(basename "$F")"
