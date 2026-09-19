# One user's browser: Google Chrome on Xvfb, noVNC for the human, CDP for the agent.
# Same recipe as workspace-hub's soak image, minus the probe tooling.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
 && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update && apt-get install -y --no-install-recommends \
      google-chrome-stable xvfb x11vnc fluxbox socat novnc websockify fonts-noto-cjk fonts-noto-color-emoji tini \
 && rm -rf /var/lib/apt/lists/*

# Restore the last session (session cookies survive a stop), no password manager.
COPY policy.json /etc/opt/chrome/policies/managed/anywr.json
COPY entrypoint.sh /entrypoint.sh

RUN useradd -m -u 1000 anywr && mkdir -p /profile && chown anywr:anywr /profile
USER anywr
ENV DISPLAY=:99 TZ=UTC
ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
