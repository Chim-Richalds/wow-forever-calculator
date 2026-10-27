#!/usr/bin/env bash
# One-click launcher for the talent calculator.
#   ./launch.sh         start the page (or just open it if it is already running)
#   ./launch.sh stop    stop the background server
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${WOWCALC_PORT:-8765}"
URL="http://127.0.0.1:${PORT}"
LOG="${ROOT}/data/ui.log"
PIDFILE="${ROOT}/data/ui.pid"

is_running() {
    python3 -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(s.connect_ex(('127.0.0.1', ${PORT})))" 2>/dev/null
}

if [ "${1:-}" = "stop" ]; then
    if [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null; then
        rm -f "$PIDFILE"
        notify-send "WoW Forever Calculator" "Stopped." 2>/dev/null || echo "Stopped."
    else
        rm -f "$PIDFILE"
        echo "Not running."
    fi
    exit 0
fi

if is_running; then
    xdg-open "$URL" >/dev/null 2>&1 &
    exit 0
fi

cd "$ROOT"
PYTHONPATH="$ROOT/src" nohup python3 -m wowcalc.ui --port "$PORT" >"$LOG" 2>&1 &
echo $! >"$PIDFILE"

# The server opens the browser itself; just check that it came up.
for _ in $(seq 1 20); do
    sleep 0.5
    if is_running; then exit 0; fi
    if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then break; fi
done

rm -f "$PIDFILE"
notify-send -u critical "WoW Forever Calculator" "Failed to start. See $LOG" 2>/dev/null
echo "Failed to start. See $LOG" >&2
exit 1
