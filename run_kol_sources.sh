#!/usr/bin/env bash
set -euo pipefail

project_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
python_bin="${KOL_PYTHON:-$project_dir/.venv/bin/python}"

if [[ ! -x "$python_bin" ]]; then
    echo "KOL Python environment is missing: $python_bin" >&2
    exit 1
fi

children=()

start_source() {
    "$python_bin" "$@" &
    children+=("$!")
}

stop_sources() {
    if ((${#children[@]} == 0)); then
        return
    fi
    kill "${children[@]}" 2>/dev/null || true
    wait "${children[@]}" 2>/dev/null || true
    children=()
}

trap 'stop_sources; exit 0' INT TERM
trap stop_sources EXIT

cd "$project_dir"
start_source kol_main.py run
start_source kol_toppump.py run
start_source kol_binance_pullback_consolidation_monitor.py run
start_source kol_binance_monitor_volume_drops.py
start_source kol_instant_opportunity_hunter.py --mode monitor

set +e
wait -n "${children[@]}"
status=$?
set -e
exit "$status"
