#!/bin/sh
set -eu
mkdir -p "$HOME" "$XDG_RUNTIME_DIR" "$TIKTOK_STATE_DIR" "$TIKTOK_SCRATCH_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
Xvfb :99 -screen 0 1280x720x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
xvfb_pid=$!
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if xdpyinfo -display :99 >/dev/null 2>&1; then break; fi
    sleep 1
done
x11vnc -display :99 -localhost -forever -shared -nopw -rfbport 5900 -noxdamage -quiet >/tmp/x11vnc.log 2>&1 &
vnc_pid=$!
websockify --web /usr/share/novnc 127.0.0.1:6080 127.0.0.1:5900 >/tmp/websockify.log 2>&1 &
ws_pid=$!
trap 'kill "$xvfb_pid" "$vnc_pid" "$ws_pid" 2>/dev/null || true' EXIT
python -m terex_tiktok.app
