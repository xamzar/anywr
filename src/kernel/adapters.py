"""Adapters: a path the agent found once, stored as data rather than as code.

A spec is YAML that a fixed interpreter walks (replay.py). Nothing in the schema
below can name a function, a file or a host, so there is nothing in an adapter
for a bad one to reach for -- the safety property is the shape of the data, not
a check somewhere that has to be right.

Two things the schema deliberately cannot express:

  * a `fill` value. There is no key for one. `fill` carries `value_from`, the
    name of an argument the caller supplies at run time, so a password cannot be
    written into an adapter file even by a caller who wants to. fill()'s value
    is never logged anywhere in this codebase and an adapter file is not going
    to be the exception.
  * a url. Banner's SSO hops carry tokens in the query string, and a recording
    is written to a volume and hashed into an audit line. Steps are identified
    by (role, name) -- what _resolve() already trusts -- and by nothing else.

Every read is total in the same sense handoff.py's are: load_all() answers with
what parsed and a list of what did not, because one unreadable file must not
stop a server from starting.
"""
import datetime as dt
import hashlib
import json
import logging
import os
import re
import tempfile

import yaml

from kernel import digest

log = logging.getLogger("kernel.adapters")

DEFAULT_DIR = "/state/adapters"
AUDIT_NAME = ".audit.jsonl"

# The base surface, frozen here rather than read off the server: a set that is
# derived from the live registry would shrink the moment a tool failed to
# register, and the one thing an adapter must never be able to do is take a base
# tool's name. tests/test_adapters.py checks this covers what server.py exposes.
RESERVED = frozenset({
    "view", "click", "fill", "select", "read", "session_status", "handoff",
    "record", "export_adapter", "verify",
})

# \Z, not $. Python's $ also matches just before a trailing newline, so "view\n"
# would pass a check written as ^...$ and then become a filename with a newline
# in it. The rule is the documented ^[a-z][a-z0-9_]{2,40}$ and this is how it is
# spelled to actually mean it.
NAME_RE = re.compile(r"[a-z][a-z0-9_]{2,40}\Z")
FIELD_RE = re.compile(r"[a-z][a-z0-9_]{0,40}\Z")

# The closed set. Each verb is one call on Kernel and nothing else; adding one
# is a change to this tuple *and* to replay.py, which is the point.
VERBS = ("click", "fill", "select", "read", "wait")
KINDS = ("table",)
TYPES = {"str": str, "int": int, "float": float}

MAX_STEPS = 40
MAX_WAIT_MS = 10_000
MAX_SELECTOR = 300
MAX_FIELDS = 24
MAX_COLUMN = 200
MAX_DESCRIPTION = 300
MAX_OCCURRENCE = 200


class AdapterError(Exception):
    """Written for the model, like KernelError: what is wrong and what to do."""


# --- where they live --------------------------------------------------------

def directory():
    return os.environ.get("KERNEL_ADAPTERS_DIR", DEFAULT_DIR)


def audit_path():
    return os.path.join(directory(), AUDIT_NAME)


def validate_name(name):
    """The name, or AdapterError. Checked before the name ever becomes a path."""
    if not isinstance(name, str):
        raise AdapterError(f"an adapter name must be text, not {type(name).__name__}")
    if name in RESERVED:
        raise AdapterError(
            f"{name!r} is the name of a base tool, which an adapter may never take. The base "
            f"tools are {', '.join(sorted(RESERVED))} — pick a name that says what this adapter "
            "returns instead, like 'aims_grades'.")
    if not NAME_RE.match(name):
        raise AdapterError(
            f"{name!r} is not a usable adapter name. It must be 3–41 characters, start with a "
            "lowercase letter and hold only lowercase letters, digits and underscores "
            "(^[a-z][a-z0-9_]{2,40}$) — it becomes both a filename and a tool name.")
    return name


def spec_path(name):
    """The file this name means, proved to be inside the adapters directory.

    The name is regex-checked before it gets here, so `../` cannot reach this
    function through any normal path -- which is exactly why the check is done
    again by resolution rather than by inspecting the string. A symlink is
    refused too: it is the one way a name that *is* confined can still name a
    file that is not.
    """
    root = os.path.realpath(directory())
    raw = os.path.join(root, f"{name}.yaml")
    if os.path.islink(raw):
        raise AdapterError(f"{name!r} is a symlink, which an adapter file may never be — "
                           "refused without reading or writing it.")
    real = os.path.realpath(raw)
    if os.path.dirname(real) != root or os.path.basename(real) != f"{name}.yaml":
        raise AdapterError(f"{name!r} resolves to {real}, which is outside the adapters "
                           f"directory ({root}) — refused.")
    return real


# --- validation -------------------------------------------------------------
# Unknown keys are an error everywhere. Dropping a key the agent thought
# mattered is how a tool ends up doing something other than what it says, and
# the agent has no way to notice a key that was silently ignored.

def _dict(obj, where):
    if not isinstance(obj, dict):
        raise AdapterError(f"{where} must be a mapping, not {type(obj).__name__}")
    return obj


def _keys(obj, where, required, optional=()):
    unknown = sorted(set(obj) - set(required) - set(optional))
    if unknown:
        allowed = ", ".join(sorted(set(required) | set(optional)))
        raise AdapterError(f"{where} has unknown key(s) {unknown} — it takes only: {allowed}. "
                           "An unknown key is an error rather than something to ignore, "
                           "because ignoring it would leave the adapter doing something other "
                           "than what it says.")
    missing = sorted(set(required) - set(obj))
    if missing:
        raise AdapterError(f"{where} is missing required key(s) {missing}")
    return obj


def _text(obj, where, *, cap, allow_empty=False):
    if not isinstance(obj, str):
        raise AdapterError(f"{where} must be text, not {type(obj).__name__}")
    s = digest.clean(obj)
    if not s and not allow_empty:
        raise AdapterError(f"{where} must not be empty")
    if len(s) > cap:
        raise AdapterError(f"{where} is longer than {cap} characters")
    return s


def _whole(obj, where, *, low, high):
    if isinstance(obj, bool) or not isinstance(obj, int):
        raise AdapterError(f"{where} must be a whole number, not {obj!r}")
    if not low <= obj <= high:
        raise AdapterError(f"{where} must be between {low} and {high}, not {obj}")
    return obj


def _target(step, where, extra=()):
    """The (role, name, occurrence) every acting verb identifies its element by.

    The same pair _resolve() verifies before it acts. `occurrence` breaks a tie
    where a page offers the same role and name twice -- two links called
    "Details", say -- and is 1 for the overwhelming majority of steps.
    """
    _keys(step, where, ("role", "name"), ("occurrence", *extra))
    role = _text(step["role"], f"{where}.role", cap=32).lower()
    if role not in digest.INTERACTIVE:
        raise AdapterError(f"{where}.role is {role!r}, which is not a role the digest "
                           f"addresses. It must be one of: {', '.join(sorted(digest.INTERACTIVE))}")
    out = {"role": role, "name": _text(step["name"], f"{where}.name", cap=digest.MAX_NAME)}
    out["occurrence"] = _whole(step.get("occurrence", 1), f"{where}.occurrence",
                               low=1, high=MAX_OCCURRENCE)
    return out


def _step(raw, i):
    where = f"steps[{i}]"
    _dict(raw, where)
    if len(raw) != 1:
        raise AdapterError(f"{where} must hold exactly one verb, not {sorted(raw) or 'nothing'}. "
                           f"The verbs are: {', '.join(VERBS)}.")
    verb, body = next(iter(raw.items()))
    if verb not in VERBS:
        raise AdapterError(
            f"{where} uses {verb!r}, which is not a step verb. The set is closed — "
            f"{', '.join(VERBS)} — and there is deliberately no verb for running code, reading "
            "a file or making a request, so an adapter cannot do any of those things.")
    body = _dict(body, f"{where}.{verb}")
    where = f"{where}.{verb}"
    if verb == "click":
        return {"click": _target(body, where)}
    if verb == "fill":
        # No `value` key exists, here or anywhere below. A fill's value arrives
        # as an argument when the adapter is called and is never on disk.
        out = _target(body, where, extra=("value_from",))
        _keys(body, where, ("role", "name", "value_from"), ("occurrence",))
        out["value_from"] = _text(body["value_from"], f"{where}.value_from", cap=41)
        if not NAME_RE.match(out["value_from"]):
            raise AdapterError(f"{where}.value_from must be an argument name matching "
                               f"^[a-z][a-z0-9_]{{2,40}}$, not {out['value_from']!r}")
        return {"fill": out}
    if verb == "select":
        out = _target(body, where, extra=("option",))
        _keys(body, where, ("role", "name", "option"), ("occurrence",))
        out["option"] = _text(body["option"], f"{where}.option", cap=digest.MAX_NAME)
        return {"select": out}
    if verb == "read":
        _keys(body, where, ("contains",))
        return {"read": {"contains": _text(body["contains"], f"{where}.contains",
                                           cap=digest.MAX_NAME)}}
    _keys(body, where, ("ms",))
    return {"wait": {"ms": _whole(body["ms"], f"{where}.ms", low=0, high=MAX_WAIT_MS)}}


def _extract(raw):
    _keys(_dict(raw, "extract"), "extract", ("kind", "selector", "fields"), ("skip",))
    kind = _text(raw["kind"], "extract.kind", cap=16)
    if kind not in KINDS:
        raise AdapterError(f"extract.kind is {kind!r}; the kinds are: {', '.join(KINDS)}")
    selector = _text(raw["selector"], "extract.selector", cap=MAX_SELECTOR)
    fields = _dict(raw["fields"], "extract.fields")
    if not fields:
        raise AdapterError("extract.fields is empty — an adapter that returns no columns "
                           "returns nothing worth calling it for")
    if len(fields) > MAX_FIELDS:
        raise AdapterError(f"extract.fields has {len(fields)} columns; the cap is {MAX_FIELDS}")
    out = {}
    for key, column in fields.items():
        if not isinstance(key, str) or not FIELD_RE.match(key):
            raise AdapterError(f"extract.fields key {key!r} must match ^[a-z][a-z0-9_]{{0,40}}$ "
                               "— it becomes a column name in every row returned")
        out[key] = _whole(column, f"extract.fields.{key}", low=0, high=MAX_COLUMN)
    return {"kind": kind, "selector": selector, "fields": out,
            "skip": _whole(raw.get("skip", 0), "extract.skip", low=0, high=MAX_COLUMN)}


def _returns(raw, fields):
    if not isinstance(raw, list) or len(raw) != 1:
        raise AdapterError("returns must be a list holding exactly one row shape, e.g. "
                           "[{course: str, grade: str}]")
    row = _dict(raw[0], "returns[0]")
    if set(row) != set(fields):
        raise AdapterError(
            f"returns[0] declares {sorted(row)} but extract.fields gives {sorted(fields)}. They "
            "have to be the same columns: the declaration is what the caller is promised and "
            "the fields are what it would actually get.")
    out = {}
    for key, kind in row.items():
        name = kind if isinstance(kind, str) else ""
        if name not in TYPES:
            raise AdapterError(f"returns[0].{key} is {kind!r}; a column type is one of: "
                               f"{', '.join(TYPES)}")
        out[key] = name
    return [out]


def validate(spec):
    """The spec, normalised, or AdapterError. The only way into a file."""
    _keys(_dict(spec, "the adapter"), "the adapter",
          ("name", "description", "steps", "extract", "returns"), ("params",))
    name = validate_name(spec["name"])
    description = _text(spec["description"], "description", cap=MAX_DESCRIPTION)

    raw_steps = spec["steps"]
    if not isinstance(raw_steps, list) or not raw_steps:
        raise AdapterError("steps must be a non-empty list — an adapter with no steps replays "
                           "nothing and would extract from whatever page happened to be open")
    if len(raw_steps) > MAX_STEPS:
        raise AdapterError(f"steps has {len(raw_steps)} entries; the cap is {MAX_STEPS}")
    steps = [_step(s, i) for i, s in enumerate(raw_steps)]

    extract = _extract(spec["extract"])
    out = {"name": name, "description": description, "steps": steps,
           "extract": extract, "returns": _returns(spec["returns"], extract["fields"])}

    # params is the tool's signature, in call order. Derived from the fill steps
    # rather than trusted, so the file cannot declare an argument no step uses
    # or use one it never declared.
    wanted = []
    for step in steps:
        if "fill" in step and step["fill"]["value_from"] not in wanted:
            wanted.append(step["fill"]["value_from"])
    given = spec.get("params", [] if not wanted else None)
    if given is None:
        raise AdapterError(f"params is missing; the fill steps need arguments {wanted}")
    if not isinstance(given, list) or list(given) != wanted:
        raise AdapterError(f"params must be exactly {wanted} — the arguments the fill steps "
                           f"name, in the order they are first used — not {given!r}")
    if wanted:
        out["params"] = wanted
    return out


# --- the files --------------------------------------------------------------

def dump(spec):
    """The spec as the bytes that go on disk. Canonical, so the audit hash of an
    unchanged adapter is stable."""
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True,
                          default_flow_style=False).encode("utf-8")


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def audit(name, blob, action="export"):
    """Append one line for one write. Raises OSError if it cannot.

    Never holds anything derived from a fill value, because no fill value ever
    reaches a spec -- the hash is over the file, and the file has no value in it.
    """
    line = json.dumps({"ts": _now(), "action": action, "name": name,
                       "sha256": hashlib.sha256(blob).hexdigest(),
                       "bytes": len(blob)}, sort_keys=True) + "\n"
    os.makedirs(directory(), exist_ok=True)
    with open(audit_path(), "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
    return line


def save(spec):
    """Validate, write whole-or-not-at-all, audit. Returns the path written.

    The audit line is the invariant, so a spec whose line could not be appended
    is removed again: an adapter on disk that no audit line mentions is worse
    than no adapter.
    """
    spec = validate(spec)
    dest = spec_path(spec["name"])
    blob = dump(spec)
    folder = os.path.dirname(dest)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".adapter-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except BaseException:
        _unlink(tmp)
        raise
    try:
        audit(spec["name"], blob)
    except OSError:
        _unlink(dest)
        raise
    return dest


def _unlink(p):
    try:
        os.unlink(p)
    except OSError:
        pass


def load(name):
    """One adapter by name, validated. AdapterError for anything unusable."""
    validate_name(name)
    path = spec_path(name)
    try:
        with open(path, "rb") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        raise AdapterError(f"there is no adapter called {name!r}") from None
    except (OSError, yaml.YAMLError) as exc:
        raise AdapterError(f"adapter {name!r} could not be read: {type(exc).__name__}: "
                           f"{exc}") from exc
    spec = validate(raw)
    if spec["name"] != name:
        raise AdapterError(f"adapter file {os.path.basename(path)} calls itself "
                           f"{spec['name']!r}; the name and the filename must agree")
    return spec


def load_all():
    """(specs, problems) for every *.yaml in the directory.

    Total on purpose. A file that fails validation is a problem to log loudly,
    never an exception that stops a server from starting -- one bad adapter must
    not cost the other nine and the seven base tools.
    """
    specs, problems = [], []
    try:
        names = sorted(os.listdir(directory()))
    except OSError:
        return specs, problems      # no volume, no adapters; not an error
    for entry in names:
        if not entry.endswith(".yaml") or entry.startswith("."):
            continue
        name = entry[:-len(".yaml")]
        try:
            specs.append(load(name))
        except AdapterError as exc:
            problems.append((entry, str(exc)))
        except Exception as exc:  # noqa: BLE001 - startup survives anything in here
            problems.append((entry, f"{type(exc).__name__}: {exc}"))
    return specs, problems


# --- building one from a recording ------------------------------------------

def _slug(text, taken):
    s = re.sub(r"[^a-z0-9]+", "_", digest.clean(text).lower()).strip("_") or "value"
    if not NAME_RE.match(s):
        s = f"value_{s}"[:41].rstrip("_")
    if not NAME_RE.match(s):
        s = "value"
    base, n = s, 2
    while s in taken:
        s, n = f"{base}_{n}"[:41], n + 1
    taken.add(s)
    return s


def from_recording(name, description, steps, extract, types=None):
    """A validated spec from recorded steps plus the extraction the agent chose.

    A recorded fill has no value -- it never had one here -- so this is where it
    becomes a named argument, taken from the field's own label so the resulting
    tool reads as `aims_search(search_term=...)`.
    """
    taken, out = set(), []
    for step in steps:
        step = dict(step)
        if "fill" in step:
            body = dict(step["fill"])
            body["value_from"] = _slug(body["name"], taken)
            step = {"fill": body}
        out.append(step)
    fields = dict(extract.get("fields") or {})
    types = types or {}
    returns = [{key: types.get(key, "str") for key in fields}]
    spec = {"name": name, "description": description, "steps": out,
            "extract": extract, "returns": returns}
    params = [s["fill"]["value_from"] for s in out if "fill" in s]
    if params:
        spec["params"] = params
    return validate(spec)
