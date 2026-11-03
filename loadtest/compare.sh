#!/usr/bin/env bash
# Сравнение «до/после» на одном профиле мока: loadtest/compare.sh "A" "50 100" "2 4 8"
# Варианты: base (текущий код, ветка work), all (scale-audit-all без флагов),
# phones (+PHONE_MAP по замерам, RETRY_OTHER_PHONE=1), tuned (+guard 10 с, параллельность по числу телефонов),
# parallel (как tuned, но интервал guard прежний — 30 с).
set -uo pipefail
cd "$(dirname "$0")/.."
WORK="$PWD"; DEV="${DEV:-$HOME/avia-scale/dev}"
unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy
VARIANTS="${VARIANTS:-base all phones tuned}"
for v in $VARIANTS; do for s in $1; do for u in $2; do for p in $3; do echo "$v $s $u $p"; done; done; done; done |
  xargs -P "${JOBS:-3}" -L 1 sh -c '
    v=$0 s=$1 u=$2 p=$3
    src="'"$DEV"'/src"; extra=""
    case $v in
      base) src="'"$WORK"'/src" ;;
      all) ;;
      phones) extra="--phone-map optimal"; export RETRY_OTHER_PHONE=1 ;;
      tuned) extra="--phone-map optimal"; export RETRY_OTHER_PHONE=1 GUARD_MIN_INTERVAL=10 SITES_PER_SEARCH=6 \
               MAX_PARALLEL_SEARCHES=$p GUARD_SITE_CONCURRENCY=$(( p / 2 > 0 ? p / 2 : 1 )) SEARCH_PRIORITY=1 ;;
      parallel) extra="--phone-map optimal"; export RETRY_OTHER_PHONE=1 GUARD_MIN_INTERVAL=30 SITES_PER_SEARCH=6 \
               MAX_PARALLEL_SEARCHES=$p GUARD_SITE_CONCURRENCY=$(( p / 2 > 0 ? p / 2 : 1 )) SEARCH_PRIORITY=1 ;;
    esac
    PYTHONPATH="$src:'"$WORK"'" '"$WORK"'/.venv/bin/python -m loadtest.run --scenario "$s" --users "$u" --phones "$p" \
      $extra --tag "_v2$v" 2>/dev/null | grep -E "^[ABC]_u" | cut -c1-160'
