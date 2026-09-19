# One user's browser: Google Chrome on KasmVNC's Xvnc (X server + web viewer in
# one), CDP for the agent. Same Chrome recipe as workspace-hub's soak image.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
 && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update && apt-get install -y --no-install-recommends \
      google-chrome-stable fluxbox socat fonts-noto-cjk fonts-noto-color-emoji tini \
 && curl -fsSL -o /tmp/kasmvnc.deb https://github.com/kasmtech/KasmVNC/releases/download/v1.5.0/kasmvncserver_bookworm_1.5.0_amd64.deb \
 && apt-get install -y --no-install-recommends /tmp/kasmvnc.deb && rm /tmp/kasmvnc.deb \
 && rm -rf /var/lib/apt/lists/*

# Restore the last session (session cookies survive a stop), no password manager.
COPY policy.json /etc/opt/chrome/policies/managed/anywr.json
COPY entrypoint.sh /entrypoint.sh

RUN useradd -m -u 1000 anywr && mkdir -p /profile && chown anywr:anywr /profile
# Fluxbox only keeps Chrome maximized, borderless, with no toolbar, so the viewer's
# size (KasmVNC resize=remote) becomes Chrome's size.
RUN mkdir -p /home/anywr/.fluxbox \
 && printf 'session.screen0.toolbar.visible: false\n' > /home/anywr/.fluxbox/init \
 && printf '[app] (.*)\n  [Maximized] {yes}\n  [Deco] {NONE}\n[end]\n' > /home/anywr/.fluxbox/apps \
 && chown -R anywr:anywr /home/anywr/.fluxbox
USER anywr
ENV DISPLAY=:99 TZ=UTC
ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
