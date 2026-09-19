# Findings — M1, the digest kernel

**Date:** 2026-09-19 · **Browser:** `work` (not the soak experiment) · **Verdict: the digest
thesis holds.**

The task: *get my current semester grades from AIMS.* Run through the digest kernel and
through the stock Playwright surface, same pages, same session, minutes apart.

## The number

| Step | Kernel | Stock |
|---|---:|---:|
| view (start page) | 300 | 4,577 |
| click Student Record | 398 | 3,077 |
| click My Academic Record | 290 | 1,976 |
| click Grade Display | 313 | 1,878 |
| click Go (submit programme) | 300 | 4,577 |
| read(contains='CS') | 150 | — |
| **Total** | **~1,753 tok** | **~16,087 tok** | 

**9.2× less context for the same completed task.** Per page the range was 6.9×–15.3×; the
ratio is widest on the grades page itself, where the stock snapshot carries the whole table.

Both surfaces finished the task. The kernel returned eleven graded courses with codes,
titles, credits and grades.

## How to read this, honestly

- **Tokens are estimated at 4 characters each, not counted with a real tokenizer.** The
  ratio is robust — an order of magnitude does not come from an estimator being 15% off —
  but the absolute figures are approximate. A real count is worth doing before this number
  goes in a deck.
- **The control is `aria_snapshot()` as `src/mcp_server.py` calls it**, capped at 40,000
  characters. That is the surface we would otherwise have shipped, and it is the right
  comparison for *our* decision. It is not identical to upstream Playwright MCP, so this is
  not a benchmark against that project.
- **`read()` is where the kernel pays.** The stock snapshot includes the grade table in every
  step whether or not it is wanted; the kernel carries none of it until asked, then spends
  150 tokens once. That asymmetry *is* the thesis: cheap to move, pay to look. The measured
  ratio depends on a task that reads one thing at the end. A task that reads on every page
  would narrow it.
- **One user, one portal, one session.** Nothing here says anything about sites other than
  Banner.

## What the live run changed

Four things were wrong or missing, and none of them was visible from fixtures. This is the
argument for running against a real portal early.

1. **Banner wraps every menu item's bullet image in its own link to the same target.** Half
   the refs on a menu page were decorative twins. Merging adjacent duplicate hrefs took the
   Personal Information page from 6.6× to 8.3×.
2. **The first fix silently deleted real menu items.** Ranking merge candidates by name
   length kept `Blue ball graphic` (17 chars) over `My Benefits` (11). Caught by reading the
   output, not by a test. The rule is now structural — a link that names itself beats one
   named by a decoration it contains — and there is a test named for that failure.
3. **The programme radio has no accessible name**, so the digest dropped it as unaddressable
   and the form that reaches the grades could not be completed. Form controls now fall back
   to the text around them: `[23] radio BSCCCU4 (CSC1) - Bachelor of Science in Computer
   Science`. Links still drop, because an unnamed link is usually decoration.
4. **Grade Display has two buttons both called `Go`** — one for the page search, one to
   submit the programme form. Where a name repeats, the surrounding text is appended, and
   only there: `Go — Find a Page` against `Go — BSCCCU4 (CSC1) - …`.

## Scope: the kernel is six tools, not three

The timeline specified `view` / `click` / `fill`. Three more were added, each because the
task could not complete without it. A fair reading of the number above must account for
this: it was measured against a six-tool kernel.

- **`select(ref, option)`** — added for a term dropdown that, on this page, turned out not to
  exist. Banner uses a radio here. It is tested but was not exercised by this run.
- **`session_status()`** — see below.
- **`read(contains=…)`** — without it the kernel could navigate to the grades and not report
  them. `view()` shows what can be acted on; a grade table is text and has no ref.

`open(url)` and `evaluate(js)` remain deliberately absent.

## The finding that matters most

At 12:0x an idled-out Banner session (15 minutes) rendered a digest that looked **completely
normal** — full menu, 39 refs, no signal of any kind. An agent would have kept clicking and
reported empty results as fact.

This is precisely what the design synthesis predicted: *"`session_status()` distinguishes
'logged out' from 'no data' — otherwise an expired session silently reads as an empty
result."* It was observed on the real portal, not reasoned about.

`session_status()` now catches it, and on the live timed-out page returned:

> LOGGED_OUT — the page itself says "session has been timeout". The menu it still shows is
> part of the signed-out page. Nothing it lists is your data, so do not report what you read
> here as a result.

Its `AUTHED` path was the untested half — it depends on Banner's sign-out being labelled
`Exit` with `P_Logout` in the href, taken from `config/targets.yaml` rather than observed.
Confirmed live: it returns AUTHED on signed-in pages for that reason.

**Known limitation:** a site whose sign-out lives inside a dropdown — GitHub, for instance —
reads `UNKNOWN` while genuinely signed in. That is the safe direction, never a false
`AUTHED`, but it means `UNKNOWN` is the common answer on real pages and is only useful if the
model treats it as "not confirmed" rather than "fine".

## What M1 did not do

- No real-tokenizer count.
- `select()` unexercised against a live dropdown.
- Not reachable through the gateway; spoken to over `docker compose exec` on stdio.
- Never pointed at Claude Desktop end to end, which is the other half of the timeline's
  "done when".
- 144 tests, all against local fixtures except this run.

## Recommendation

Proceed to M2. The thesis is not in doubt at 9.2×, and the remaining M1 gaps are reporting
polish rather than open questions. The `session_status` result argues for bringing
`handoff()` forward: a dead session is now *detectable*, and the only thing to do about it is
hand control to the human — which is M2 and is the demo anyway.
