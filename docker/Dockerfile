FROM python:3.12-slim-bookworm

# Real Google Chrome, not Playwright's Chromium-for-Testing: Google sign-in
# fingerprints the build, and the production machine will run a stock browser.
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
 && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update && apt-get install -y --no-install-recommends \
      google-chrome-stable xvfb x11vnc fluxbox novnc websockify fonts-noto-cjk fonts-noto-color-emoji tini \
 && rm -rf /var/lib/apt/lists/*

# Playwright is only a CDP client here (connect_over_cdp); no bundled browsers.
RUN pip install --no-cache-dir playwright==1.55.0 pyyaml==6.0.2 pytest==8.3.3

# Session cookies only survive a restart when the browser restores the last
# session; no password manager, so the profile never holds a credential.
COPY docker/chrome-policy.json /etc/opt/chrome/policies/managed/soak.json

RUN useradd -m -u 1000 soak && mkdir -p /profile /data && chown soak:soak /profile /data
COPY --chown=soak:soak . /app
WORKDIR /app
USER soak
ENV DISPLAY=:99 PYTHONUNBUFFERED=1 TZ=UTC

ENTRYPOINT ["tini", "--", "/app/docker/entrypoint.sh"]
