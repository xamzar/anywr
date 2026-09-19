"""The base tools: view / click / fill / select / read / session_status /
handoff, over one already-running page, plus the recording M5 promotes.

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

from kernel import digest, handoff, session

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
# A recorded path is a menu walk, not a session. Anything longer is a sign the
# recording was left on, and a 500-step adapter is not something anyone meant.
MAX_RECORDED = 40
# The ceiling on an adapter's wait step. Long enough for a slow Banner redirect,
# short enough that a wait cannot be used to pin the one page the kernel owns.
MAX_WAIT_MS = 10_000


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

# A password field is never recorded, so it has to be recognisable. Asked of the
# resolved element, not of the page: session.JS_PASSWORD_FIELD answers "is there
# one here", which is a different question from "is this one".
JS_IS_PASSWORD = "el => el.type === 'password'"


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
    """view / click / fill / select / read / session_status / handoff against one
    page, with the last view() remembered."""

    def __init__(self, page, *, max_tokens=digest.DEFAULT_MAX_TOKENS):
        self._page = page
        self._max_tokens = max_tokens
        self._shown = None  # (url, [Element]) from the last view(), or None
        # None means not recording. A live recording is in memory and nowhere
        # else: it is a draft of a path, it is discarded on a restart, and
        # keeping it off the volume means there is no half-written file for a
        # value to end up in before export_adapter() ever validates anything.
        self._rec = None

    async def view(self):
        """The page as refs. Every other tool needs this to have run first."""
        handle, raw = await self._extract()
        await handle.dispose()  # view acts on nothing, so the handles are dead weight
        text = await self._page.evaluate(digest.JS_TEXT)
        self._shown = (self._page.url, digest.numbered(raw))
        return digest.build_digest(self._page.url, await self._page.title(), raw,
                                   text=text, max_tokens=self._max_tokens)

    async def click(self, ref):
        """Click the element at `ref`, then return the resulting page."""
        el = await self._resolve(ref)
        step = self._step("click", ref)
        log.debug("click ref=%s", ref)
        try:
            await el.click(timeout=ACT_TIMEOUT_MS)
        except PlaywrightTimeout as exc:
            raise KernelError(f"ref [{ref}] did not become clickable within "
                              f"{ACT_TIMEOUT_MS // 1000}s") from exc
        self._keep(step)
        await self._settle()
        return await self.view()

    async def fill(self, ref, value):
        """Replace the value of the field at `ref`, then return the page.

        The value is never logged and never echoed: it leaves this frame only
        into the field itself.
        """
        el = await self._resolve(ref)
        # A password never becomes a step. The value would not be recorded
        # either way, but a recorded password *field* turns into a required
        # argument on a promoted tool -- which is an invitation to hand a
        # credential to a replay. handoff() is the answer to a password, so the
        # step is dropped here and export_adapter() refuses the recording
        # outright rather than exporting a path with a hole in it.
        secret = bool(await el.evaluate(JS_IS_PASSWORD))
        step = None if secret else self._step("fill", ref)
        if secret and self._rec and self._rec["on"]:
            self._rec["dropped"] += 1
            log.warning("recording: a fill into a password field was not recorded")
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
        self._keep(step)
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
        # The label, never the option's value: the label is what the digest
        # showed and what the model meant, and it is the string replay has to
        # match against a dropdown that may have been rebuilt since.
        step = self._step("select", ref,
                          option=next(lab or val for j, lab, val in live if j == i))
        try:
            await el.select_option(index=i, timeout=ACT_TIMEOUT_MS)
        except PlaywrightTimeout as exc:
            raise KernelError(f"ref [{ref}] did not become selectable within "
                              f"{ACT_TIMEOUT_MS // 1000}s") from exc
        self._keep(step)
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
        text = await self._page.evaluate(digest.JS_TEXT)
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
        text = await self._page.evaluate(digest.JS_TEXT)
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

    async def handoff(self, reason, **kw):
        """Stop, let a human drive this browser, and pick the page up after them.

        The only tool here that waits on a person. Everything else fails fast;
        this one blocks, because a password prompt, a 2FA push or a captcha has
        no machine answer and asking is the whole of the right response.

        Takes no ref and returns a fresh view(), always. Whatever the human did,
        they did it in this browser, so every ref from before this call is a
        guess -- re-viewing is not a courtesy, it is the only honest thing to
        return. The wait itself lives in handoff.py, which shares a file with
        the control server because the Done button is in another process.
        """
        if not handoff.clean_reason(reason):
            raise KernelError(
                "handoff(reason) needs a reason — it is shown to the person on their phone "
                "and is the only thing telling them what to do. Say what is blocking you and "
                "what they should finish, e.g. 'sign in to AIMS: it wants your CityU password "
                "and a Duo push'.")
        try:
            res = await handoff.wait(reason, **kw)
        except OSError as exc:
            # Nobody was asked, so waiting would have been theatre. This is a
            # deployment fault (no /state volume), not a call to retry.
            raise KernelError(
                f"the handoff could not be recorded ({type(exc).__name__}: {exc}). The viewer "
                f"reads {handoff.path()} to know you are waiting, so nobody would ever have "
                "been shown your request. Nothing is blocked and nothing was handed over; "
                "report this rather than retrying.") from exc
        try:
            return f"{res.message}\n\n{await self.view()}"
        except Exception as exc:  # noqa: BLE001 - the outcome must survive a bad re-read
            return (f"{res.message}\n\n(the page could not be read back afterwards: "
                    f"{type(exc).__name__} — call view() yourself before acting.)")

    # --- recording ----------------------------------------------------------
    # Exploration is expensive and repetition should be free, so every
    # successful act is kept -- but kept as what identifies the element
    # *tomorrow*. A ref is a position in a list and means nothing by then;
    # (role, name) is the pair _resolve() already trusts enough to act on, and
    # `occurrence` is the only thing it does not carry, because _resolve() has a
    # ref to disambiguate with and a replay does not.

    def record(self, on):
        """Start or stop recording. Returns what is in the recording now.

        One tool with a flag rather than two tools: the model reads the whole
        tool list on every call, and a second entry is a permanent cost for a
        boolean. record(true) starts a fresh recording -- resuming into an old
        one is how a path acquires a step nobody meant -- and record(false)
        stops appending but keeps what it has, because export_adapter() is the
        next call and it needs the steps.
        """
        if not isinstance(on, bool):
            raise KernelError(f"record(on) takes true or false, not {on!r}")
        if on:
            self._rec = {"on": True, "steps": [], "dropped": 0, "full": False}
            return ("recording — every click, fill and select that succeeds from here is a "
                    "step. Walk the path once, then call record(false) and export_adapter().")
        if self._rec is None:
            return "nothing was being recorded. Call record(true) first, then walk the path."
        self._rec["on"] = False
        return f"recording stopped — {self.recording_summary()}"

    def recording(self):
        """(steps, dropped) as export_adapter() needs them."""
        rec = self._rec or {"steps": [], "dropped": 0}
        return list(rec["steps"]), rec["dropped"]

    def recording_summary(self):
        rec = self._rec
        if rec is None:
            return "nothing has been recorded; call record(true) before you explore."
        if not rec["steps"] and not rec["dropped"]:
            return "no steps. Every click, fill and select that succeeds is added."
        verbs = "; ".join(f"{i + 1}. {v} {b['name']!r}"
                          for i, s in enumerate(rec["steps"]) for v, b in s.items())
        note = ""
        if rec["dropped"]:
            note = (f" {rec['dropped']} fill(s) into a password field were NOT recorded — an "
                    "adapter may not type a credential, so this path cannot be exported. Use "
                    "handoff() for the sign-in and record the part after it.")
        if rec["full"]:
            note += f" The recording filled up at {MAX_RECORDED} steps; later acts were dropped."
        return f"{len(rec['steps'])} step(s): {verbs}.{note}"

    def _step(self, verb, ref, **extra):
        """The recordable form of an act about to happen on `ref`.

        Built before the act, kept after it: a click that times out is not a
        step, and the page it would have moved to is not part of the path.
        """
        if not (self._rec and self._rec["on"]):
            return None
        shown = self._shown[1]
        el = shown[ref - 1]
        occurrence = 1 + sum(1 for e in shown[:ref - 1] if (e.role, e.name) == (el.role, el.name))
        return {verb: {"role": el.role, "name": el.name, "occurrence": occurrence, **extra}}

    def _keep(self, step):
        if step is None or not (self._rec and self._rec["on"]):
            return
        if len(self._rec["steps"]) >= MAX_RECORDED:
            self._rec["full"] = True
            return
        self._rec["steps"].append(step)

    # --- what the adapter interpreter walks on ------------------------------
    # replay.py drives the tools above and these three, and holds no selector,
    # no evaluate and no page of its own. Everything that touches the DOM is
    # here, which is what keeps "an adapter is data" true of the runtime too.

    def shown(self):
        """The Elements of the last view(), or (). replay.py turns a recorded
        (role, name, occurrence) back into a ref through this."""
        return self._shown[1] if self._shown else ()

    async def wait(self, ms):
        """The `wait` verb. Bounded: an adapter holds the lock on the one page
        this server owns, so it does not get to hold it indefinitely."""
        await self._page.wait_for_timeout(max(0, min(int(ms), MAX_WAIT_MS)))

    async def table_rows(self, selector, skip=0):
        """Rows of the table `selector` names, as lists of cell text.

        The selector is data passed to a fixed function; see JS_TABLE.
        """
        got = await self._page.evaluate(digest.JS_TABLE, [str(selector), max(0, int(skip))])
        if got.get("error") == "selector":
            raise KernelError(f"{selector!r} is not a valid CSS selector")
        if got.get("error") == "missing":
            raise KernelError(f"nothing on this page matches {selector!r}")
        return got["rows"]

    async def best_table(self):
        """(selector, row count) for the likeliest data table here, or None."""
        got = await self._page.evaluate(digest.JS_BEST_TABLE)
        return (got["selector"], got["rows"]) if got else None

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
