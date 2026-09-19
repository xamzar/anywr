"""The three base tools: view / click / fill, over one already-running page.

A ref is a *position in the filtered list* digest.py produced, so it only means
anything against the page state that produced it. Everything below exists to
make sure the element a ref reaches is the element the model was shown:
resolution runs through digest.JS_CANDIDATES + digest.numbered(), the same path
view() used, and refuses rather than guessing when the page has moved under it.

Not in here, deliberately: no MCP (step 3), no db. The kernel is not part of the
soak experiment and logs no AGENT_ACTION events.
"""
import json
import logging
import socket
import urllib.request

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from kernel import digest

log = logging.getLogger("kernel.base")

ACT_TIMEOUT_MS = 15_000
LOAD_TIMEOUT_MS = 30_000
# A click may or may not navigate, and Playwright cannot tell us which before it
# happens. Wait a beat so a navigation that is coming has committed, then wait
# on the document that is actually there.
SETTLE_MS = 500


class KernelError(Exception):
    """Anything the model should read and retry differently. Step 3 hands the
    message straight back as the tool result, so every one of these is written
    to tell the model what to do next."""


# --- attach -----------------------------------------------------------------
# Deliberately not imported from src/mcp_server.py: that file is M1's control
# condition and must stay untouched, and it reads /app/config at import time.

def _ws_url(cdp):
    """Chrome answers /json only for an IP or localhost Host header, and its
    webSocketDebuggerUrl always says 127.0.0.1 -- which, from this container,
    may be a different workspace's Chrome. So resolve, fetch, and re-point it."""
    host, port = cdp.split(":")
    ip = socket.gethostbyname(host)
    info = json.load(urllib.request.urlopen(f"http://{ip}:{port}/json/version", timeout=10))
    return info["webSocketDebuggerUrl"].replace("127.0.0.1:9222", f"{ip}:{port}", 1)


async def attach(playwright, cdp, *, tab=0, max_tokens=digest.DEFAULT_MAX_TOKENS):
    """Kernel over the Chrome already listening at `cdp` (host:port).

    Never launches: the browser is a long-lived profile that is already logged
    in, and contexts[0] is that profile. A fresh context would be a stranger.
    """
    browser = await playwright.chromium.connect_over_cdp(_ws_url(cdp))
    pages = browser.contexts[0].pages
    if not 0 <= tab < len(pages):
        raise KernelError(f"no tab {tab} at {cdp}; it has {len(pages)} tabs")
    return Kernel(pages[tab], max_tokens=max_tokens)


# --- the tools --------------------------------------------------------------

class Kernel:
    """view / click / fill against one page, with the last view() remembered."""

    def __init__(self, page, *, max_tokens=digest.DEFAULT_MAX_TOKENS):
        self._page = page
        self._max_tokens = max_tokens
        self._shown = None  # (url, [Element]) from the last view(), or None

    async def view(self):
        """The page as refs. Every other tool needs this to have run first."""
        handle, raw = await self._extract()
        await handle.dispose()  # view acts on nothing, so the handles are dead weight
        text = await self._page.evaluate("() => document.body ? document.body.innerText : ''")
        self._shown = (self._page.url, digest.numbered(raw))
        return digest.build_digest(self._page.url, await self._page.title(), raw,
                                   text=text, max_tokens=self._max_tokens)

    async def click(self, ref):
        """Click the element at `ref`, then return the resulting page."""
        el = await self._resolve(ref)
        log.debug("click ref=%s", ref)
        try:
            await el.click(timeout=ACT_TIMEOUT_MS)
        except PlaywrightTimeout as exc:
            raise KernelError(f"ref [{ref}] did not become clickable within "
                              f"{ACT_TIMEOUT_MS // 1000}s") from exc
        await self._settle()
        return await self.view()

    async def fill(self, ref, value):
        """Replace the value of the field at `ref`, then return the page.

        The value is never logged and never echoed: it leaves this frame only
        into the field itself.
        """
        el = await self._resolve(ref)
        log.debug("fill ref=%s", ref)  # no value, here or anywhere
        try:
            await el.fill(value, timeout=ACT_TIMEOUT_MS)
        except PlaywrightTimeout as exc:
            raise KernelError(f"ref [{ref}] did not become fillable within "
                              f"{ACT_TIMEOUT_MS // 1000}s") from exc
        except PlaywrightError as exc:
            # Wrong kind of element is the common case and the message says so;
            # it cannot quote the value, only the element.
            raise KernelError(f"ref [{ref}] cannot be filled: {exc.message.splitlines()[0]}") from exc
        return await self.view()

    # --- ref resolution -----------------------------------------------------
    # The whole point of this module. Re-running JS_CANDIDATES is chosen over
    # stamping a data-* attribute during view(): stamping mutates a page the
    # kernel does not own, and a framework re-render drops the attribute anyway
    # -- trading a detectable staleness for a silent one. Re-querying costs one
    # evaluate and, because the query lives in digest.py and is imported rather
    # than copied, cannot fall out of step with what view() numbered.

    async def _extract(self):
        """(handle to the live candidate array, its [{role, name}]).

        Both come from the *same* array object, so the description at position i
        and the element at position i are the same node even if the page mutates
        between the two calls.
        """
        handle = await self._page.evaluate_handle(digest.JS_CANDIDATES)
        return handle, await handle.evaluate(digest.JS_DESCRIBE)

    async def _resolve(self, ref):
        if isinstance(ref, bool) or not isinstance(ref, int):
            raise KernelError(f"ref must be a whole number like 1 or 7, not {ref!r}")
        if self._shown is None:
            raise KernelError("no view() yet on this page — call view() first; refs only "
                              "mean anything against the page view() showed you")
        url, shown = self._shown
        if self._page.url != url:
            self._shown = None
            raise KernelError(f"the page navigated since view() (was {url}, now "
                              f"{self._page.url}) — call view() again for fresh refs")
        if not 1 <= ref <= len(shown):
            have = f"refs 1–{len(shown)}" if shown else "nothing to act on"
            raise KernelError(f"no ref [{ref}]: the last view() showed {have}")
        want = shown[ref - 1]

        handle, raw = await self._extract()
        try:
            now = digest.numbered(raw)
            # Verify before acting. A re-render, a late-loading menu or an
            # expanded dropdown renumbers everything after it, so the same ref
            # can now be a different element. Refusing costs one view(); acting
            # on the guess costs whatever the wrong element does.
            if ref > len(now) or (now[ref - 1].role, now[ref - 1].name) != (want.role, want.name):
                got = (f"{now[ref - 1].role} {now[ref - 1].name!r}"
                       if ref <= len(now) else "nothing")
                self._shown = None
                raise KernelError(
                    f"the page changed since view(): ref [{ref}] was {want.role} "
                    f"{want.name!r} and is now {got} — call view() again and use the "
                    f"refs it returns. Nothing was clicked or typed.")
            el = (await handle.evaluate_handle("(els, i) => els[i]",
                                               now[ref - 1].index)).as_element()
        finally:
            await handle.dispose()
        if el is None:  # the node left the document between the two evaluates
            raise KernelError(f"ref [{ref}] is no longer on the page — call view() again")
        return el

    async def _settle(self):
        await self._page.wait_for_timeout(SETTLE_MS)
        try:
            await self._page.wait_for_load_state("load", timeout=LOAD_TIMEOUT_MS)
        except PlaywrightTimeout as exc:
            raise KernelError(f"the page was still loading after {LOAD_TIMEOUT_MS // 1000}s; "
                              "call view() to see where it got to") from exc
