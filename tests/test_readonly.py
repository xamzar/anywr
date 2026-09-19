"""Fails if the probe path contains a mutating Playwright call."""
import re
from pathlib import Path

from probe import ACTION

FORBIDDEN = r"\.(click|dblclick|fill|type|press|press_sequentially|select_option|check|uncheck|set_checked|" \
            r"tap|hover|drag_to|set_input_files|dispatch_event|evaluate|evaluate_handle|add_init_script|" \
            r"expose_function|expose_binding|clear_cookies|add_cookies|keyboard|mouse)\b"


def test_no_mutating_calls():
    for f in ("src/probe.py", "src/snapshot_cookies.py", "src/classify.py"):
        hits = re.findall(FORBIDDEN, Path(f).read_text())
        assert not hits, f"{f} uses forbidden call(s): {hits}"


def test_action_backstop():
    for bad in ("logout", "P_Logout", "signout", "sign-out", "account/delete", "form/submit", "transfer", "pay/now"):
        assert ACTION.search(bad), bad
    for ok in ("display", "settings/profile", "paypal", "feed/"):
        assert not ACTION.search(ok), ok
