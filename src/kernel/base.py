"""The base tools: view / click / fill / select / session_status, over one
already-running page.

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

from kernel import digest, session

log = logging.getLogger("kernel.base")

ACT_TIMEOUT_MS = 15_000
LOAD_TIMEOUT_MS = 30_000
# How many option labels an error lists before it starts counting instead. A
# term dropdown holds a dozen; a country dropdown holds two hundred and would
# cost more context than the digest it came from.
MAX_OPTIONS = 40
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

# Options as the page has them. `null` for anything that is not a dropdown, so
# one evaluate answers both "is this a select?" and "what is in it?" -- two
# would leave room for the element to change between them.
JS_OPTIONS = """el => el.tagName !== 'SELECT' ? null
  : [...el.options].map(o => ({label: o.label, value: o.value, disabled: o.disabled}))"""


def _options_are(live, total):
    """The options that exist, for the model to pick from.

    A guessed option string is the likeliest way select() fails on a real page --
    "Semester A 2026" against a dropdown that says "Semester A 2026/27" -- and
    the real strings are the only thing that makes the next call right.
    """
    if not live:
        return "It has no option that can be chosen."
    names = [(label or value)[:digest.MAX_NAME] for _, label, value in live]
    more = f" (+{len(names) - MAX_OPTIONS} more)" if len(names) > MAX_OPTIONS else ""
    off = total - len(live)
    # Counted rather than listed, in the digest's own style: offering an option
    # that cannot be chosen would only buy a second failed call.
    disabled = f" ({off} disabled option{'' if off == 1 else 's'} — not selectable)" if off else ""
    return (f"Its options are: {', '.join(repr(n) for n in names[:MAX_OPTIONS])}{more}."
            f"{disabled} Pass one of these exactly, as its label or as its value.")


class Kernel:
    """view / click / fill / select / session_status against one page, with the
    last view() remembered."""

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
            # it cannot quote the value, only the element. A dropdown is the one
            # wrong kind that now has a right answer, so it gets named: refusing
            # without it is what left the model with nowhere to go.
            hint = (" — this is a dropdown; use select(ref, option) instead"
                    if self._shown[1][ref - 1].role == "combobox" else "")
            raise KernelError(
                f"ref [{ref}] cannot be filled: {exc.message.splitlines()[0]}{hint}") from exc
        return await self.view()

    async def select(self, ref, option):
        """Choose `option` in the dropdown at `ref`, then return the page.

        Label first, value second. Playwright's select_option() takes a bare
        string as either and keeps whichever option comes first in the document,
        so on a page where one option is *labelled* "202630" and another one
        *has that value*, which you get depends on the page's ordering. Here the
        label always wins, because the label is what the digest showed the model
        and what it therefore meant.
        """
        el = await self._resolve(ref)
        shown = self._shown[1][ref - 1]  # _resolve just proved this still describes it
        opts = await el.evaluate(JS_OPTIONS)
        if opts is None:
            raise KernelError(
                f"ref [{ref}] is a {shown.role} ({shown.name!r}), not a dropdown, so there is "
                f"nothing to select in it. select() works only on a dropdown (combobox); use "
                f"click() for links and buttons and fill() for text fields.")
        # A disabled option cannot be chosen, so offering it in the error would
        # only buy a second failed call. Banner's "None" placeholder is one.
        live = [(i, digest.clean(o["label"]), digest.clean(o["value"]))
                for i, o in enumerate(opts) if not o["disabled"]]
        want = digest.clean(option)
        i = next((i for i, label, _ in live if label == want), None)
        if i is None:
            i = next((i for i, _, value in live if value == want), None)
        if i is None:
            raise KernelError(f"ref [{ref}] has no option {want!r}. {_options_are(live, len(opts))}")
        log.debug("select ref=%s", ref)
        try:
            await el.select_option(index=i, timeout=ACT_TIMEOUT_MS)
        except PlaywrightTimeout as exc:
            raise KernelError(f"ref [{ref}] did not become selectable within "
                              f"{ACT_TIMEOUT_MS // 1000}s") from exc
        # A term dropdown that submits on change navigates; one that waits for a
        # button does not. _settle() covers both, as it does for click().
        await self._settle()
        return await self.view()

    async def session_status(self):
        """Whether this page is a signed-in page: AUTHED / LOGGED_OUT / UNKNOWN.

        Reads only -- it clicks nothing, needs no ref and leaves the last view()
        standing, so it is always safe to ask before believing a result. The
        elements it judges on are the ones view() numbers, so "a sign-out
        control is present" means one the model could actually have clicked.
        """
        handle, raw = await self._extract()
        await handle.dispose()
        text = await self._page.evaluate("() => document.body ? document.body.innerText : ''")
        return session.status(
            self._page.url, await self._page.title(), text, digest.numbered(raw),
            password_field=await self._page.evaluate(session.JS_PASSWORD_FIELD))

    async def read(self, contains=None, max_tokens=digest.DEFAULT_MAX_TOKENS):
        """The page's visible text, which view() counts but never quotes.

        view() shows what can be acted on; the payload of a portal page -- a
        grade table, a balance, a timetable -- is text and has no ref. Reading
        it is the one place the model is meant to pay for detail, so it is a
        separate call it makes once rather than a cost on every step. That is
        the whole shape of the digest thesis: cheap to move, pay to look.

        `contains` keeps only lines holding that substring, case-insensitively.
        """
        text = await self._page.evaluate("() => document.body ? document.body.innerText : ''")
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
        if contains:
            needle = contains.lower()
            lines = [ln for ln in lines if needle in ln.lower()]
            if not lines:
                return (f"no line on this page contains {contains!r}. Call read() with no "
                        "filter to see the whole page, or view() if you meant to act on it.")
        head = f"url: {digest.clean(self._page.url)}\n\n"
        # Truncate from the tail with the omission stated, exactly as the digest
        # does: a silent cut here would look like a page that simply ends.
        budget, kept = max_tokens - digest.tokens(head), []
        for ln in lines:
            if digest.tokens("\n".join(kept + [ln])) > budget:
                return head + "\n".join(kept) + f"\n({len(lines) - len(kept)} more lines — not shown)"
            kept.append(ln)
        return head + "\n".join(kept)

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
