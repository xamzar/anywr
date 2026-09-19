"""FastMCP wiring for the kernel: view / click / fill over one browser.

One browser, one page, and no `workspace` argument anywhere: the CDP target is
configuration (KERNEL_CDP), not something the model picks. The three tools are
the whole surface on purpose -- no open(), no evaluate(), no screenshot -- so
what M1 measures is the kernel and not a smaller Playwright server.

Not in here, deliberately: no db, no AGENT_ACTION logging. The kernel is not
part of the soak experiment.
"""
import argparse
import asyncio
import logging
import os
import sys
import traceback

from fastmcp import FastMCP
from playwright.async_api import async_playwright

from kernel import base, digest

log = logging.getLogger("kernel.server")

DEFAULT_CDP = "work:9223"

mcp = FastMCP("kernel", instructions=(
    "One browser, one page, three tools. view() returns the page as a numbered list of the "
    "elements you can act on; click(ref) and fill(ref, value) take one of those numbers and "
    "return the page's new digest. A ref only means anything against the digest it came from "
    "— any change to the page renumbers them, so act on the most recent digest and call view() "
    "again whenever a tool tells you a ref is stale. There is no way to navigate by URL: the "
    "browser starts on whatever page it is already showing, and you move by clicking."))

# One page can only do one thing at a time, so the lock covers whole tool calls
# rather than just the connect: a click landing between another call's view()
# and its ref resolution is exactly the race base.py refuses to guess about.
_lock = asyncio.Lock()
_pw = None
_kernel = None


async def _attach():
    """The Kernel, connected on first use and re-connected after a drop.

    Lazily rather than at startup: `work` is a separate container that may not
    be up yet, and a server that refuses to start is a crash loop nobody reads.
    The same laziness is the recovery path -- a dead CDP connection costs one
    failed tool call, not a restart, because _run() clears the cache and the
    next call lands back here.
    """
    global _pw, _kernel
    if _kernel is not None:
        return _kernel
    cdp = os.environ.get("KERNEL_CDP", DEFAULT_CDP)
    try:
        _pw = _pw or await async_playwright().start()
        _kernel = await base.attach(
            _pw, cdp,
            tab=int(os.environ.get("KERNEL_TAB", "0")),
            max_tokens=int(os.environ.get("KERNEL_MAX_TOKENS", digest.DEFAULT_MAX_TOKENS)))
    except base.KernelError:
        raise
    except Exception as exc:  # noqa: BLE001 - unreachable browser is the model's to retry
        raise base.KernelError(
            f"cannot reach the browser at {cdp}: {type(exc).__name__} — it may still be "
            "starting; try the same call again in a moment") from exc
    return _kernel


def _scrub(text, secret):
    """fill()'s value must not survive into a log line, not even inside a
    traceback that quoted it."""
    return text.replace(secret, "***") if secret else text


async def _run(name, op, *, secret=None):
    """Every tool body goes through here.

    A KernelError is written for the model, so it is the result. Anything else
    is a bug or a dropped connection: swallowing it silently would hide the bug,
    and returning the traceback would spend the model's context on something it
    cannot act on -- so the traceback goes to the log, the model gets one line,
    and the cached connection is dropped because it is the prime suspect.
    """
    global _kernel
    async with _lock:
        try:
            return await op(await _attach())
        except base.KernelError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - the model gets a result, never a stack trace
            _kernel = None
            log.error("%s failed:\n%s", name, _scrub(traceback.format_exc(), secret))
            return (f"{name} failed unexpectedly ({type(exc).__name__}); the server logged the "
                    "details. Call view() to see where the page actually is.")


# --- the three tools --------------------------------------------------------
# The docstrings are the model's instructions, so every one of them says where
# refs come from and when they stop being true.

@mcp.tool
async def view() -> str:
    """Show the current page as the numbered things you can act on.

    Returns the url, the title, and one `[ref] role name` line per interactive
    element, followed by a count of anything left out. Refs are 1-based and
    belong to this digest only: when the page changes they are renumbered.
    Call this first — click() and fill() take refs, and only refs from the most
    recent digest are valid.
    """
    return await _run("view", lambda k: k.view())


@mcp.tool
async def click(ref: int) -> str:
    """Click the element at `ref`, then return the page as it now is.

    `ref` is a number from the most recent digest (from view(), click() or
    fill()). If the page moved under that ref the click is refused instead of
    guessed at — call view() again and use the numbers it returns. The return
    value is a fresh digest whose refs replace the ones you were using.
    """
    return await _run("click", lambda k: k.click(ref))


@mcp.tool
async def fill(ref: int, value: str) -> str:
    """Type `value` into the field at `ref`, replacing what is there, then
    return the page as it now is.

    `ref` comes from the most recent digest, under the same rule as click():
    stale refs are refused, not guessed. Only fields take a value (textbox,
    searchbox, combobox and the like); use click() for links and buttons.
    `value` is never logged and never echoed back in a result.
    """
    return await _run("fill", lambda k: k.fill(ref, value), secret=value)


# --- transports -------------------------------------------------------------
# One server definition, two ways in: http is the deployed path, stdio is what
# Claude Desktop attaches to over an SSH tunnel.

def main(argv=None):
    logging.basicConfig(level=os.environ.get("KERNEL_LOG", "INFO").upper(), stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="MCP kernel: view / click / fill over one browser")
    ap.add_argument("--transport", choices=("http", "stdio"),
                    default=os.environ.get("KERNEL_TRANSPORT", "http"))
    args = ap.parse_args(argv)
    if args.transport == "stdio":
        # stdout is the protocol here: logs went to stderr above, banner off.
        asyncio.run(mcp.run_async(transport="stdio", show_banner=False))
    else:
        asyncio.run(mcp.run_async(transport="http", show_banner=False,
                                  host=os.environ.get("KERNEL_HOST", "0.0.0.0"),
                                  port=int(os.environ.get("KERNEL_PORT", "8000"))))


if __name__ == "__main__":
    main()
