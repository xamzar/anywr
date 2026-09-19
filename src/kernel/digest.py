"""Page digest: what the model can act on, and nothing else.

build_digest() is pure -- dicts in, one string out -- so the format, the ref
numbering and the token cap are testable without a browser. extract_elements()
is the only part that touches Playwright and it makes no formatting decisions,
so the two move independently and the expensive half needs no browser to test.

Not in here, deliberately: no CDP, no MCP, no db. The kernel is not part of the
soak experiment and logs no AGENT_ACTION events.
"""
import math
import re
from dataclasses import dataclass

# HTML's own interactive elements -- <a>, <button>, <input>, <select>,
# <textarea> -- land on exactly these ARIA roles. The set is closed on purpose:
# M1's kernel is view/click/fill, and every role added here costs tokens on
# every single view() whether or not the page uses it.
INTERACTIVE = frozenset({
    "link", "button", "textbox", "searchbox", "checkbox", "radio",
    "combobox", "listbox", "spinbutton", "slider", "switch",
})

DEFAULT_MAX_TOKENS = 1000
MAX_NAME = 80

# ~4 characters per token. Short ASCII lines of this shape measure 3.5-4.5
# chars per BPE token, so 4 sizes a budget closely enough to truncate on. The
# M1 measurement counts real tokens; this only decides where the cut falls.
CHARS_PER_TOKEN = 4

_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Element:
    """One addressable thing on the page. `ref` is its position, 1-based.

    `index` is where it sat in the raw sequence this was numbered from. base.py
    needs it to get from a ref back to a handle; nothing here prints it.
    """
    ref: int
    role: str
    name: str
    index: int = -1
    href: str = ""
    ctx: str = ""


def _field(e, key):
    return e.get(key, "") if isinstance(e, dict) else getattr(e, key, "")


def clean(s):
    """Flatten to one line of bracket-free text.

    Whitespace collapses because a newline inside a page's own text would
    otherwise let the page forge a `[n] role name` line of its own. Angle
    brackets go because the digest is flat text, not markup: nothing a page
    controls should be able to look like a tag once it is in the context.

    Public, not private, because the digest is no longer the only page-derived
    text that reaches the model: base.py quotes option labels back in select()'s
    errors, and those need the same scrubbing for the same reason.
    """
    return _WS.sub(" ", str(s or "").replace("<", " ").replace(">", " ")).strip()


def _cap(s):
    return s[:MAX_NAME - 1] + "…" if len(s) > MAX_NAME else s


def _norm(e):
    """(role, name) for one raw element, cleaned and length-capped.

    A form control with no accessible name falls back to the text it sits in.
    Banner's programme radio is labelled only by its table row, and dropping it
    for being unnamed makes the form that reaches the grades unusable. Links do
    not get this: an unnamed link is usually decoration, and its surroundings
    are the text of whatever it decorates.
    """
    role = clean(_field(e, "role")).lower()
    name = clean(_field(e, "name"))
    if not name and role in INTERACTIVE and role != "link":
        name = clean(_field(e, "ctx")) or f"(unlabelled {role})"
    return role, _cap(name)


def _rank(raw, name):
    """How good a label this is for a target two links share."""
    return (bool(_field(raw, "own")), len(name))


def numbered(elements):
    """Addressable elements in document order, refs assigned 1..n.

    Refs are positions in this list, so they are dense by construction. base.py
    must resolve a ref through this same function, or the ref it clicks is not
    the one the model was shown.
    """
    out = []
    for i, (role, name) in enumerate(map(_norm, elements)):
        if role not in INTERACTIVE or not name:
            continue
        href = clean(_field(elements[i], "href"))
        # Banner gives every menu item a bullet image wrapped in its own link to
        # the same target, so half of AIMS's refs are decorative twins of the
        # next one. Collapse a run of links sharing one href and keep the
        # longest name -- "Employment History" over "Blue ball graphic". Only
        # links, and only adjacent ones: a nav bar repeating a link far down the
        # page is a real second way to get there.
        if href and role == "link" and out and out[-1].role == "link" and out[-1].href == href:
            # Naming itself beats being named by a decoration; length only
            # breaks a tie. Ranking on length alone loses "My Benefits" to
            # "Blue ball graphic", which is the whole failure this guards.
            if _rank(elements[i], name) > _rank(elements[out[-1].index], out[-1].name):
                out[-1] = Element(out[-1].ref, role, name, i, href, clean(_field(elements[i], "ctx")))
            continue
        out.append(Element(len(out) + 1, role, name, i, href, clean(_field(elements[i], "ctx"))))
    return _disambiguate(out)


def _disambiguate(els):
    """Two controls with one name are two refs the model must guess between.

    Banner's Grade Display has a "Go" for the page search and a "Go" that
    submits the programme form. Told apart only by the text around them, so
    where a name repeats, that text is appended -- and only there, because it is
    noise on every element that was already distinct.
    """
    seen = {}
    for e in els:
        seen.setdefault((e.role, e.name), []).append(e)
    for (role, name), group in seen.items():
        if len(group) < 2 or role == "link":
            continue
        if len({e.ctx for e in group}) < len(group):
            continue  # the context does not separate them either; do not pretend
        for e in group:
            els[e.ref - 1] = Element(e.ref, role, _cap(f"{name} — {e.ctx}"), e.index, e.href, e.ctx)
    return els


def tokens(text_or_chars):
    """Estimated tokens. Public because base.py's read() budgets the same way
    the digest does, and two estimators would truncate at two different sizes."""
    n = text_or_chars if isinstance(text_or_chars, int) else len(text_or_chars)
    return math.ceil(n / CHARS_PER_TOKEN)


def _line(el, ref_w, role_w):
    # Padded columns cost a few tokens a page and buy the model a format it can
    # read positionally instead of parsing.
    return f"[{el.ref}]".ljust(ref_w) + "  " + el.role.ljust(role_w) + "  " + el.name


def _plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _more(k):
    return f"({_plural(k, 'more element')} — not shown)\n"


def build_digest(url, title, elements, *, text="", max_tokens=DEFAULT_MAX_TOKENS):
    """The page as refs: a header, one line per addressable element, and a
    footer that owns up to everything the filter or the cap left out.

    `elements` are raw dicts (or anything with .role/.name) in document order.
    `text` is the page's visible text; it is counted, never quoted.
    """
    els = numbered(elements)
    # Named by nothing at all -- no aria-label, no text, no placeholder. A ref
    # the model cannot tell apart from its neighbours is worse than no ref, so
    # these are dropped, but the count is stated rather than hidden.
    unnamed = sum(1 for role, name in map(_norm, elements) if role in INTERACTIVE and not name)
    # numbered() folds a link's decorative twin into it. Nothing reachable is
    # lost -- they share an href -- but a merge the digest did not admit to is
    # still a silent drop, so it is counted here like the rest.
    named = sum(1 for role, name in map(_norm, elements) if role in INTERACTIVE and name)
    merged = named - len(els)

    head = f"url: {clean(url)}\ntitle: {clean(title)}\n"
    foot = ""
    if unnamed:
        foot += f"({_plural(unnamed, 'unnamed element')} — not addressable)\n"
    if merged:
        foot += f"({_plural(merged, 'duplicate link')} — merged into the ref above)\n"
    if words := len(str(text or "").split()):
        foot += f"({words} words of page text — not shown)\n"

    if not els:
        return f"{head}\n(no interactive elements)\n{foot}"

    ref_w = len(f"[{len(els)}]")
    role_w = max(len(e.role) for e in els)
    lines = [_line(e, ref_w, role_w) for e in els]

    whole = head + "\n" + "".join(f"{ln}\n" for ln in lines) + foot
    if tokens(len(whole)) <= max_tokens:
        return whole

    # Truncate from the tail. The reserve is the widest the omission line can
    # ever get, so the line that admits the truncation can never itself push
    # the digest back over the cap. The header is not negotiable: a digest with
    # no url is not worth having, so a cap too small for it is simply exceeded.
    used = len(head) + 1 + len(foot) + len(_more(len(lines)))
    kept = 0
    for ln in lines:
        if tokens(used + len(ln) + 1) > max_tokens:
            break
        used += len(ln) + 1
        kept += 1
    body = "".join(f"{ln}\n" for ln in lines[:kept])
    return head + "\n" + body + _more(len(lines) - kept) + foot


# Which nodes are candidates at all, as live elements in document order. This is
# the *one* definition of that set: base.py evaluates this same source to turn a
# ref back into a handle, so the extractor and the resolver cannot drift apart.
# Hidden and disabled are filtered here because not actionable means not
# addressable, and a gap between the two lists is a silent wrong click.
JS_CANDIDATES = """() =>
  [...document.querySelectorAll('a[href], button, input, select, textarea, [role]')]
    .filter(el => !el.disabled && el.getAttribute('aria-disabled') !== 'true'
                  && el.type !== 'hidden' && el.getClientRects().length)"""

# Candidates -> {role, name}, positionally. Names are sliced in the page because
# a role-bearing container's innerText can be the entire document.
JS_DESCRIBE = """els => {
  const TAG = {A: 'link', BUTTON: 'button', SELECT: 'combobox', TEXTAREA: 'textbox'};
  const TYPE = {checkbox: 'checkbox', radio: 'radio', search: 'searchbox', range: 'slider',
                number: 'spinbutton', submit: 'button', reset: 'button', button: 'button',
                image: 'button'};
  const role = el => el.getAttribute('role') || TAG[el.tagName] || TYPE[el.type] || 'textbox';
  // Absolute, via the property rather than the attribute, so two links to one
  // target compare equal however each wrote its path.
  const href = el => el.tagName === 'A' ? el.href : '';
  // Whether the element says its own name. A link holding only a bullet image
  // is named by that image's alt, which is about the decoration and not the
  // destination -- so it loses to its twin whatever the two are called.
  const own = el => !!(el.innerText || '').trim();
  // The text a control sits in. Banner labels a radio by the table row around
  // it and gives two different forms a "Go" button each, so for form controls
  // the surrounding text is the only thing that says which is which.
  const ctx = el => {
    let n = el.parentElement, d = 0;
    while (n && d++ < 4) {
      const t = (n.innerText || '').replace(/\\s+/g, ' ').trim();
      if (t && t.length <= 120) return t;
      n = n.parentElement;
    }
    return '';
  };
  const name = el => el.getAttribute('aria-label')
    || (el.labels && el.labels[0] ? el.labels[0].innerText : '')
    || el.innerText || el.getAttribute('placeholder') || el.getAttribute('title')
    || (['submit', 'reset', 'button'].includes(el.type) ? el.value : '')
    || ((el.querySelector('img') || {}).alt) || '';
  return els.map(el => ({role: role(el), name: (name(el) || '').slice(0, 200),
                         href: href(el), own: own(el), ctx: ctx(el).slice(0, 120)}));
}"""

# One evaluate, not one locator call per element: 200 elements over CDP is 200
# round trips. Order is document order, which is what makes refs stable for a
# page load.
_JS = f"() => ({JS_DESCRIBE})(({JS_CANDIDATES})())"


async def extract_elements(page):
    """Raw {role, name} dicts off a live page, in document order.

    Hidden and disabled controls never come back: not actionable means not
    addressable. Everything else -- filtering by role, naming, numbering,
    truncating -- is build_digest()'s call, not this function's.
    """
    return await page.evaluate(_JS)


async def digest_page(page, *, max_tokens=DEFAULT_MAX_TOKENS):
    """view()'s body: a live page in, a digest out."""
    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    return build_digest(page.url, await page.title(), await extract_elements(page),
                        text=text, max_tokens=max_tokens)
