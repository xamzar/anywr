#!/bin/sh
# One long-lived Chrome on Xvfb, plus x11vnc -> websockify/noVNC for the human.
# On SIGTERM Chrome gets SIGTERM and we wait for it to exit, so it flushes its
# cookie store before the container dies (a SIGKILL would fake a RED).
set -u

rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 /profile/Singleton*
python /app/src/db.py boot   # records CONTAINER_RESTART or HOST_REBOOT

Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
sleep 1

# The password file lives in the container only; VNC also listens on localhost
# only, and the published noVNC port is bound to the host's loopback.
x11vnc -storepasswd "${VNC_PASSWORD:?set VNC_PASSWORD in .env}" /tmp/vncpass >/dev/null
x11vnc -display :99 -rfbauth /tmp/vncpass -localhost -shared -forever -quiet -rfbport 5900 &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

google-chrome-stable \
  --user-data-dir=/profile \
  --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --disable-background-timer-throttling \
  --no-first-run --no-default-browser-check \
  --start-maximized --window-size=1440,900 \
  about:blank &
CHROME=$!

stop() {
  echo "SIGTERM: stopping Chrome gracefully"
  kill -TERM "$CHROME" 2>/dev/null
  wait "$CHROME"
  echo "Chrome exited ($?)"
  exit 0
}
trap stop TERM INT

wait "$CHROME"
echo "Chrome exited on its own ($?); container stops so Docker restarts it"
exit 1
