#!/bin/sh
# One long-lived Chrome on Xvfb, plus x11vnc -> websockify/noVNC for the human.
# On SIGTERM Chrome gets SIGTERM and we wait for it to exit, so it flushes its
# cookie store before the container dies (a SIGKILL would fake a RED).
set -u

rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 /profile/Singleton*
python /app/src/db.py boot   # records CONTAINER_RESTART or HOST_REBOOT

Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
sleep 1
# A window manager, so OAuth popups get a title bar and a taskbar entry
# instead of covering the whole screen with no way back.
fluxbox >/dev/null 2>&1 &

# No VNC password: x11vnc listens on the container's localhost and noVNC on the
# host's loopback, so the SSH tunnel is the only way in.
x11vnc -display :99 -nopw -localhost -shared -forever -quiet -rfbport 5900 &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

# No --start-maximized: Chrome applies it to popups too, so the Google sign-in
# window would open small and then blow up to full screen. 860 leaves room for
# fluxbox's titlebar (19px) and taskbar (20px); at 900 the top gets pushed off-screen.
google-chrome-stable \
  --user-data-dir=/profile \
  --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --disable-background-timer-throttling \
  --no-first-run --no-default-browser-check \
  --window-position=0,0 --window-size=1440,860 \
  about:blank &
CHROME=$!

# Chrome only binds CDP to localhost; workspaces the MCP server reaches over
# the Docker network re-export it. (soak does not: the MCP shares its netns.)
[ -n "${CDP_PROXY:-}" ] && socat TCP-LISTEN:9223,fork,reuseaddr TCP:127.0.0.1:9222 &

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
