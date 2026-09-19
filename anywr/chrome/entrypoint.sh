#!/bin/sh
# Chrome only binds CDP to localhost, so socat re-exports it on :9223 and
# websockify serves noVNC on :6080. Both are reachable only from this user's
# own Docker network, which holds nothing but this container, the api and Caddy.
set -u
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 /profile/Singleton*

Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
sleep 1
fluxbox >/dev/null 2>&1 &
x11vnc -display :99 -nopw -localhost -shared -forever -quiet -rfbport 5900 &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

google-chrome-stable \
  --user-data-dir=/profile \
  --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 \
  --disable-backgrounding-occluded-windows --disable-renderer-backgrounding \
  --disable-background-timer-throttling \
  --no-first-run --no-default-browser-check \
  --window-position=0,0 --window-size=1440,860 \
  about:blank &
CHROME=$!
socat TCP-LISTEN:9223,fork,reuseaddr TCP:127.0.0.1:9222 &

# SIGTERM -> Chrome flushes its cookie store before the container goes away.
trap 'kill -TERM "$CHROME" 2>/dev/null; wait "$CHROME"; exit 0' TERM INT
wait "$CHROME"
exit 1
