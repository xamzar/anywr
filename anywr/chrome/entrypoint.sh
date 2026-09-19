#!/bin/sh
# KasmVNC's Xvnc is the X display and the web viewer (client + websocket on
# :6080). Chrome only binds CDP to localhost, so socat re-exports it on :9223.
# Both are reachable only from this user's own Docker network, which holds
# nothing but this container, the api and Caddy -- so no VNC password, and
# Caddy's forward_auth (session cookie) is the gate.
set -u
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 /profile/Singleton*

Xvnc :99 -geometry 1440x900 -depth 24 -interface 0.0.0.0 -UseIPv6 0 \
  -websocketPort 6080 -httpd /usr/share/kasmvnc/www -sslOnly 0 \
  -DisableBasicAuth -SecurityTypes None -AlwaysShared -FrameRate 30 &
sleep 1
fluxbox >/dev/null 2>&1 &

google-chrome-stable \
  --user-data-dir=/profile \
  --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 \
  --disable-backgrounding-occluded-windows --disable-renderer-backgrounding \
  --disable-background-timer-throttling \
  --no-first-run --no-default-browser-check \
  --start-maximized \
  about:blank &
CHROME=$!
socat TCP-LISTEN:9223,fork,reuseaddr TCP:127.0.0.1:9222 &

# SIGTERM -> Chrome flushes its cookie store before the container goes away.
trap 'kill -TERM "$CHROME" 2>/dev/null; wait "$CHROME"; exit 0' TERM INT
wait "$CHROME"
exit 1
