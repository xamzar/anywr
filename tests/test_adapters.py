"""The guardrails, with no browser anywhere near them.

An adapter is data a fixed interpreter walks, so almost everything that keeps it
safe is decidable from the data alone. That is the point of the design and it is
why this file is the biggest of the three: if these pass, the runtime has
nothing dangerous left to be careful about.
"""
import json
import os

import pytest

yaml = pytest.importorskip("yaml")

from kernel.adapters import (  # noqa: E402
    RESERVED, VERBS, AdapterError, audit_path, directory, dump, from_recording, load, load_all,
    save, spec_path, validate, validate_name,
)

SECRET = "hunter2-never-on-disk"


def spec(**over):
    out = {
        "name": "aims_grades",
        "description": "Current semester grades as typed rows",
        "steps": [
            {"click": {"role": "link", "name": "Student Record"}},
            {"click": {"role": "link", "name": "Grade Display"}},
        ],
        "extract": {"kind": "table", "selector": "table.datadisplaytable",
                    "fields": {"course": 0, "title": 1, "grade": 3}},
        "returns": [{"course": "str", "title": "str", "grade": "str"}],
    }
    out.update(over)
    return out


@pytest.fixture
def adapters_dir(tmp_path, monkeypatch):
    d = tmp_path / "adapters"
    d.mkdir()
    monkeypatch.setenv("KERNEL_ADAPTERS_DIR", str(d))
    return d


# --- reserved names -----------------------------------------------------------
# The eventual product lets a user's own agent write these. The one thing it must
# never be able to do is give an adapter a base tool's name.

@pytest.mark.parametrize("name", sorted(RESERVED))
def test_no_adapter_may_take_a_base_tools_name(name):
    with pytest.raises(AdapterError) as err:
        validate_name(name)
    assert "base tool" in str(err.value)


@pytest.mark.parametrize("name", sorted(RESERVED))
def test_a_reserved_name_is_refused_by_the_whole_validator_too(adapters_dir, name):
    """Not only by validate_name(): save() is the door, and it has to be shut
    there as well as at the check save() happens to call."""
    with pytest.raises(AdapterError):
        save(spec(name=name))
    assert not list(adapters_dir.iterdir())


def test_the_reserved_set_is_frozen():
    assert isinstance(RESERVED, frozenset)


# --- name validation ----------------------------------------------------------

@pytest.mark.parametrize("name", [
    "ab",                     # two characters
    "A_grades",               # uppercase
    "1grades",                # leading digit
    "_grades",                # leading underscore
    "aims-grades",            # hyphen
    "aims grades",            # space
    "aims.grades",            # dot: the extension is ours to add, not theirs
    "aims/grades",            # separator
    "aims\\grades",
    "grades\n",
    "g" * 42,                 # one over
    "",
    "..",
])
def test_a_name_that_is_not_a_bare_lowercase_word_is_refused(name):
    with pytest.raises(AdapterError):
        validate_name(name)


@pytest.mark.parametrize("name", ["abc", "aims_grades", "a1_2", "g" * 41])
def test_a_plain_lowercase_name_is_accepted(name):
    assert validate_name(name) == name


@pytest.mark.parametrize("name", [1, None, True, ["aims_grades"]])
def test_a_name_that_is_not_text_is_refused(name):
    with pytest.raises(AdapterError):
        validate_name(name)


# --- path confinement ---------------------------------------------------------

@pytest.mark.parametrize("name", [
    "../escape", "../../etc/passwd", "/etc/passwd", "/tmp/elsewhere",
    "sub/../../out", "./../out",
])
def test_a_name_that_resolves_outside_the_directory_is_refused(adapters_dir, name):
    with pytest.raises(AdapterError) as err:
        spec_path(name)
    assert "outside" in str(err.value) or "symlink" in str(err.value)


def test_a_symlink_is_refused_without_being_read_or_written(adapters_dir, tmp_path):
    outside = tmp_path / "outside.yaml"
    outside.write_text(yaml.safe_dump(spec(name="elsewhere")))
    os.symlink(outside, adapters_dir / "elsewhere.yaml")

    with pytest.raises(AdapterError) as err:
        spec_path("elsewhere")
    assert "symlink" in str(err.value)
    with pytest.raises(AdapterError):
        load("elsewhere")
    with pytest.raises(AdapterError):
        save(spec(name="elsewhere"))
    assert outside.read_text() == yaml.safe_dump(spec(name="elsewhere")), "the target was written"


def test_a_symlinked_directory_still_resolves_to_itself(tmp_path, monkeypatch):
    """realpath() on both sides, so a /state that is itself a link is fine —
    only a path that resolves somewhere *else* is refused."""
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(real, tmp_path / "link")
    monkeypatch.setenv("KERNEL_ADAPTERS_DIR", str(tmp_path / "link"))
    assert spec_path("aims_grades") == str(real / "aims_grades.yaml")


def test_the_good_path_is_inside_the_directory(adapters_dir):
    assert spec_path("aims_grades") == str(adapters_dir / "aims_grades.yaml")


# --- unknown keys -------------------------------------------------------------
# An ignored key is how a tool ends up doing something other than what it says,
# and the agent that wrote it has no way to notice.

@pytest.mark.parametrize("bad", [
    {"on_error": "continue"},
    {"timeout": 30},
    {"script": "print(1)"},
    {"command": "ls"},
])
def test_an_unknown_top_level_key_is_an_error(bad):
    with pytest.raises(AdapterError) as err:
        validate(spec(**bad))
    assert "unknown key" in str(err.value)


def test_an_unknown_key_inside_a_step_is_an_error():
    with pytest.raises(AdapterError) as err:
        validate(spec(steps=[{"click": {"role": "link", "name": "X", "js": "alert(1)"}}]))
    assert "unknown key" in str(err.value) and "js" in str(err.value)


def test_an_unknown_key_inside_extract_is_an_error():
    e = {"kind": "table", "selector": "table", "fields": {"a": 0}, "transform": "eval"}
    with pytest.raises(AdapterError) as err:
        validate(spec(extract=e, returns=[{"a": "str"}]))
    assert "unknown key" in str(err.value)


# --- the closed verb set ------------------------------------------------------

@pytest.mark.parametrize("verb", ["eval", "exec", "fetch", "run", "open", "shell", "import",
                                  "require", "evaluate", "screenshot", "handoff", "goto",
                                  "read_file", "http"])
def test_a_verb_outside_the_set_is_refused_at_validation_time(verb):
    with pytest.raises(AdapterError) as err:
        validate(spec(steps=[{verb: {"role": "link", "name": "X"}}]))
    msg = str(err.value)
    assert "not a step verb" in msg and "closed" in msg


def test_the_verb_set_is_what_it_says_it_is():
    assert VERBS == ("click", "fill", "select", "read", "wait")


def test_a_step_holding_two_verbs_is_refused():
    with pytest.raises(AdapterError):
        validate(spec(steps=[{"click": {"role": "link", "name": "X"},
                              "eval": {"role": "link", "name": "Y"}}]))


def test_a_step_holding_no_verb_is_refused():
    with pytest.raises(AdapterError):
        validate(spec(steps=[{}]))


def test_a_role_the_digest_does_not_address_is_refused():
    with pytest.raises(AdapterError) as err:
        validate(spec(steps=[{"click": {"role": "iframe", "name": "X"}}]))
    assert "not a role the digest addresses" in str(err.value)


def test_an_empty_step_list_is_refused():
    with pytest.raises(AdapterError):
        validate(spec(steps=[]))


@pytest.mark.parametrize("ms", [-1, 60_000, "500", 1.5, True])
def test_a_wait_outside_its_bounds_is_refused(ms):
    with pytest.raises(AdapterError):
        validate(spec(steps=[{"wait": {"ms": ms}}]))


# --- the value that is not there ----------------------------------------------

def test_a_fill_step_has_nowhere_to_put_a_value():
    """The strongest form of "a fill value is never written down": not a rule
    the writer follows, but a schema with no key for one."""
    with pytest.raises(AdapterError) as err:
        validate(spec(steps=[{"fill": {"role": "textbox", "name": "Course",
                                       "value": SECRET}}]))
    assert "unknown key" in str(err.value) and "value_from" in str(err.value)
    assert SECRET not in str(err.value)


def test_a_fill_needs_an_argument_to_take_its_value_from():
    with pytest.raises(AdapterError) as err:
        validate(spec(steps=[{"fill": {"role": "textbox", "name": "Course"}}]))
    assert "value_from" in str(err.value)


def test_params_must_be_exactly_the_arguments_the_fills_name():
    s = spec(steps=[{"fill": {"role": "textbox", "name": "Course",
                              "value_from": "course_code"}}])
    assert validate({**s, "params": ["course_code"]})["params"] == ["course_code"]
    for bad in ([], ["other"], ["course_code", "extra"], "course_code"):
        with pytest.raises(AdapterError):
            validate({**s, "params": bad})


def test_params_is_absent_when_nothing_is_filled():
    assert "params" not in validate(spec())


# --- returns ------------------------------------------------------------------

def test_returns_must_declare_the_same_columns_extract_produces():
    with pytest.raises(AdapterError) as err:
        validate(spec(returns=[{"course": "str", "title": "str"}]))
    assert "same columns" in str(err.value)


@pytest.mark.parametrize("kind", ["bytes", "object", "any", "Course", 3, None])
def test_a_column_type_outside_the_set_is_refused(kind):
    with pytest.raises(AdapterError):
        validate(spec(returns=[{"course": kind, "title": "str", "grade": "str"}]))


def test_returns_must_be_one_row_shape():
    for bad in ([], [{"course": "str"}, {"course": "str"}], {"course": "str"}):
        with pytest.raises(AdapterError):
            validate(spec(returns=bad))


@pytest.mark.parametrize("field", ["Course", "1st", "course-code", "", "course code"])
def test_a_column_name_that_is_not_an_identifier_is_refused(field):
    with pytest.raises(AdapterError):
        validate(spec(extract={"kind": "table", "selector": "t", "fields": {field: 0}},
                      returns=[{field: "str"}]))


def test_an_extract_kind_outside_the_set_is_refused():
    with pytest.raises(AdapterError):
        validate(spec(extract={"kind": "json", "selector": "t", "fields": {"course": 0}},
                      returns=[{"course": "str"}]))


# --- the file and the audit log -----------------------------------------------

def test_save_writes_a_file_that_loads_back_identically(adapters_dir):
    path = save(spec())
    assert path == str(adapters_dir / "aims_grades.yaml")
    assert load("aims_grades") == validate(spec())


def test_the_file_is_yaml_with_no_python_in_it(adapters_dir):
    save(spec())
    text = (adapters_dir / "aims_grades.yaml").read_text()
    assert "!!python" not in text                       # safe_dump, so never a tag
    assert yaml.safe_load(text)["name"] == "aims_grades"


def test_a_yaml_tag_that_would_construct_an_object_is_not_loaded(adapters_dir):
    (adapters_dir / "evil.yaml").write_text(
        "name: evil_one\n!!python/object/apply:os.system ['echo hi']\n")
    with pytest.raises(AdapterError):
        load("evil_one")
    specs, problems = load_all()
    assert specs == [] and len(problems) == 1


def test_the_audit_log_gains_exactly_one_line_per_write(adapters_dir):
    assert not os.path.exists(audit_path())
    for i in range(3):
        save(spec(description=f"take {i}"))
        assert len(open(audit_path()).read().splitlines()) == i + 1


def test_an_audit_line_names_the_time_the_adapter_and_the_spec_hash(adapters_dir):
    import hashlib
    save(spec())
    line = json.loads(open(audit_path()).read().splitlines()[-1])
    assert line["name"] == "aims_grades"
    assert line["ts"].startswith("20") and line["ts"].endswith("+00:00")
    blob = (adapters_dir / "aims_grades.yaml").read_bytes()
    assert line["sha256"] == hashlib.sha256(blob).hexdigest()


def test_a_refused_adapter_writes_neither_a_file_nor_an_audit_line(adapters_dir):
    for bad in (spec(name="view"), spec(steps=[{"eval": {"role": "link", "name": "x"}}]),
                spec(**{"extra": 1})):
        with pytest.raises(AdapterError):
            save(bad)
    assert list(adapters_dir.iterdir()) == []


def test_the_audit_log_is_never_loaded_as_an_adapter(adapters_dir):
    save(spec())
    specs, problems = load_all()
    assert [s["name"] for s in specs] == ["aims_grades"] and problems == []


# --- startup survives a bad file ----------------------------------------------

def test_one_unreadable_adapter_does_not_cost_the_others(adapters_dir):
    save(spec())
    save(spec(name="other_one"))
    (adapters_dir / "broken.yaml").write_text("steps: [{eval: {role: link, name: x}}]\n")
    (adapters_dir / "garbage.yaml").write_text("\x00\x01 not: [yaml\n")
    (adapters_dir / "empty.yaml").write_text("")

    specs, problems = load_all()
    assert sorted(s["name"] for s in specs) == ["aims_grades", "other_one"]
    assert sorted(f for f, _ in problems) == ["broken.yaml", "empty.yaml", "garbage.yaml"]
    assert all(why for _, why in problems), "every skip has to say why"


def test_a_missing_directory_is_no_adapters_rather_than_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("KERNEL_ADAPTERS_DIR", str(tmp_path / "nothing-here"))
    assert load_all() == ([], [])


def test_a_file_whose_name_disagrees_with_its_spec_is_refused(adapters_dir):
    (adapters_dir / "aims_grades.yaml").write_text(yaml.safe_dump(spec(name="something_else")))
    with pytest.raises(AdapterError) as err:
        load("aims_grades")
    assert "must agree" in str(err.value)


def test_load_of_a_name_that_is_not_there_says_so(adapters_dir):
    with pytest.raises(AdapterError) as err:
        load("aims_grades")
    assert "no adapter called" in str(err.value)


# --- building one from a recording --------------------------------------------

def test_a_recorded_fill_becomes_an_argument_named_after_its_field(adapters_dir):
    built = from_recording(
        "course_lookup", "Rooms for a course",
        [{"fill": {"role": "textbox", "name": "Course code", "occurrence": 1}},
         {"click": {"role": "button", "name": "Search", "occurrence": 1}}],
        {"kind": "table", "selector": "#results", "fields": {"course": 0, "room": 1}})
    assert built["params"] == ["course_code"]
    assert built["steps"][0]["fill"]["value_from"] == "course_code"
    assert "value" not in built["steps"][0]["fill"]
    assert SECRET not in dump(built).decode()


def test_two_fields_with_one_label_get_two_arguments():
    built = from_recording(
        "two_fields", "two",
        [{"fill": {"role": "textbox", "name": "Code", "occurrence": 1}},
         {"fill": {"role": "textbox", "name": "Code", "occurrence": 2}}],
        {"kind": "table", "selector": "t", "fields": {"a": 0}})
    assert built["params"] == ["code", "code_2"]


def test_a_field_label_that_makes_no_identifier_still_makes_an_argument():
    built = from_recording(
        "odd_label", "odd", [{"fill": {"role": "textbox", "name": "#!", "occurrence": 1}}],
        {"kind": "table", "selector": "t", "fields": {"a": 0}})
    assert built["params"] == ["value"]


def test_types_make_a_column_a_number():
    built = from_recording(
        "credits_view", "credits",
        [{"click": {"role": "link", "name": "X", "occurrence": 1}}],
        {"kind": "table", "selector": "t", "fields": {"course": 0, "credits": 2}},
        types={"credits": "int"})
    assert built["returns"] == [{"course": "str", "credits": "int"}]


def test_the_directory_is_configuration_not_a_constant(monkeypatch):
    monkeypatch.setenv("KERNEL_ADAPTERS_DIR", "/somewhere/else")
    assert directory() == "/somewhere/else"
    monkeypatch.delenv("KERNEL_ADAPTERS_DIR")
    assert directory() == "/state/adapters"
