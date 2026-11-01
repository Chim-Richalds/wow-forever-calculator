#!/usr/bin/env bash
# One-click launcher for the talent calculator.
#   ./launch.sh         start the page, or open it if it is already running. A running
#                       server is restarted first if the code, the active build or the
#                       hand-collected data changed since it started.
#   ./launch.sh stop    stop the background server
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${WOWCALC_PORT:-8765}"
URL="http://127.0.0.1:${PORT}"
LOG="${WOWCALC_LOG:-${ROOT}/data/ui.log}"
PIDFILE="${WOWCALC_PIDFILE:-${ROOT}/data/ui.pid}"

is_running() {
    python3 -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(s.connect_ex(('127.0.0.1', ${PORT})))" 2>/dev/null
}

notify() {
    notify-send "$@" 2>/dev/null || echo "${*: -1}"
}

# Stop the server this launcher started; wait until the port is free. Fails if there is none.
stop_server() {
    [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null || { rm -f "$PIDFILE"; return 1; }
    rm -f "$PIDFILE"
    for _ in $(seq 1 20); do
        is_running || return 0
        sleep 0.25
    done
    return 0
}

# True if code, the active build or data/external changed after the server started.
is_stale() {
    [ -f "$PIDFILE" ] || return 1
    [ -n "$(find "$ROOT/src/wowcalc" "$ROOT/data/active.json" "$ROOT/data/external" \
        -type f \( -name '*.py' -o -name '*.html' -o -name '*.json' -o -name '*.tsv' -o -name '*.csv' \) \
        -newer "$PIDFILE" -print -quit 2>/dev/null)" ]
}

if [ "${1:-}" = "stop" ]; then
    if stop_server; then
        notify "WoW Forever Calculator" "Stopped."
    else
        echo "Not running (or not started by this launcher)."
    fi
    exit 0
fi

if is_running; then
    if is_stale; then
        notify "WoW Forever Calculator" "Restarting with the latest code and data."
        stop_server
    else
        if [ ! -f "$PIDFILE" ]; then
            notify "WoW Forever Calculator" "A calculator this launcher did not start is already running on port $PORT; it may be out of date."
        fi
        xdg-open "$URL" >/dev/null 2>&1 &
        exit 0
    fi
fi
if is_running; then
    notify -u critical "WoW Forever Calculator" "The old server on port $PORT did not stop. Stop it and try again."
    exit 1
fi

cd "$ROOT"
PYTHONPATH="$ROOT/src" nohup python3 -m wowcalc.ui --port "$PORT" --no-browser >"$LOG" 2>&1 &
echo $! >"$PIDFILE"

for _ in $(seq 1 20); do
    sleep 0.5
    if is_running; then
        xdg-open "$URL" >/dev/null 2>&1 &
        exit 0
    fi
    if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then break; fi
done

rm -f "$PIDFILE"
notify-send -u critical "WoW Forever Calculator" "Failed to start. See $LOG" 2>/dev/null
echo "Failed to start. See $LOG" >&2
exit 1
