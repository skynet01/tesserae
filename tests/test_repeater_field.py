"""The ``repeater`` settings field (app/repeater.py): a list of rows of
sub-fields, declared in a manifest's ``settings``, edited on the widget's
settings card and stored as a JSON list of objects.

Covers the declaration checks (schema and loader), the strict save path, the
forgiving read path, the ``legacy_rows`` hook, and the settings card end to
end: rows drawn on load, a save round trip, and a refused save that leaves
the stored rows alone."""

from __future__ import annotations

import html as html_lib
import json
import re
import time
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from flask import Flask

from app import repeater
from app.authored_widgets import InstallError, _validate_manifest
from app.main import REPO_ROOT, create_app
from app.repeater import RepeaterValueError, normalise_rows, parse_rows, spec_errors

SCHEMA = json.loads((REPO_ROOT / "schema" / "plugin.schema.json").read_text(encoding="utf-8"))
SETTING_SCHEMA = SCHEMA["properties"]["settings"]["items"]


def _spec(**over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "name": "items",
        "type": "repeater",
        "label": "Items",
        "add_label": "Add item",
        "row_label": "Item",
        "max_rows": 4,
        "fields": [
            {"name": "on", "type": "boolean", "label": "On", "default": True},
            {
                "name": "code",
                "type": "string",
                "label": "Code",
                "required": True,
                "unique": True,
                "max_length": 10,
                "pattern": "[A-Za-z0-9_\\-]+",
                "pattern_hint": "letters, digits, _ and - only",
            },
            {"name": "note", "type": "textarea", "label": "Note", "max_length": 40},
            {"name": "n", "type": "number", "label": "N", "min": 0, "max": 10},
            {
                "name": "mode",
                "type": "select",
                "label": "Mode",
                "default": "a",
                "choices": [{"value": "a", "label": "A"}, {"value": 2, "label": "Two"}],
            },
        ],
        "default": [{"on": True, "code": "X1", "note": "", "n": 3, "mode": 2}],
    }
    spec.update(over)
    return spec


def _sub(spec: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in spec["fields"] if s["name"] == name)


ROW = {"on": True, "code": "AB", "note": "", "n": None, "mode": "a"}


# ----- the declaration -------------------------------------------------------


def test_a_good_declaration_passes_both_checks() -> None:
    spec = _spec()
    assert spec_errors(spec) == []
    jsonschema.validate(spec, SETTING_SCHEMA)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda s: s.pop("fields"), "fields must be a non-empty list"),
        (lambda s: s.update(fields=[]), "fields must be a non-empty list"),
        (
            lambda s: s["fields"].append(
                {"name": "inner", "type": "repeater", "label": "Inner", "fields": []}
            ),
            "can't contain another repeater",
        ),
        (
            lambda s: s["fields"].append({"name": "c", "type": "color", "label": "C"}),
            "type must be one of",
        ),
        (
            lambda s: s["fields"].append({"name": "code", "type": "string", "label": "Again"}),
            "declared twice",
        ),
        (
            lambda s: s["fields"].append({"name": "Bad Name", "type": "string", "label": "X"}),
            "lower case letters",
        ),
        (
            lambda s: s["fields"].append({"name": "x", "type": "string", "label": ""}),
            "label is required",
        ),
        (
            lambda s: s["fields"].append({"name": "x", "type": "select", "label": "X"}),
            "choices must be a non-empty list",
        ),
        (
            lambda s: s["fields"].append(
                {
                    "name": "x",
                    "type": "select",
                    "label": "X",
                    "choices": [{"value": [1], "label": "L"}],
                }
            ),
            "strings or numbers",
        ),
        (
            lambda s: s["fields"].append(
                {"name": "x", "type": "string", "label": "X", "secret": True}
            ),
            "secret isn't supported inside a repeater",
        ),
        (
            lambda s: s["fields"].append(
                {"name": "x", "type": "string", "label": "X", "show_if": {"on": True}}
            ),
            "show_if isn't supported",
        ),
        (
            lambda s: s["fields"].append(
                {"name": "x", "type": "select", "label": "X", "choices_from": "things"}
            ),
            "choices_from isn't supported",
        ),
        (
            lambda s: s["fields"].append(
                {"name": "x", "type": "string", "label": "X", "max_length": 10**9}
            ),
            "max_length must be an integer",
        ),
        (
            lambda s: s["fields"].append(
                {"name": "x", "type": "string", "label": "X", "pattern": "[a-z"}
            ),
            "pattern has a [ that isn't closed",
        ),
        (
            lambda s: s["fields"].append(
                {"name": "x", "type": "number", "label": "X", "min": 5, "max": 1}
            ),
            "min is above its max",
        ),
        (lambda s: s.update(secret=True), "secret isn't supported on a repeater"),
        (lambda s: s.update(min_rows=5, max_rows=2), "min_rows is above max_rows"),
        (lambda s: s.update(max_rows=0), "max_rows must be at least 1"),
        (lambda s: s.update(max_rows=10_000), "max_rows must be an integer"),
        (lambda s: s.update(add_label="x" * 80), "add_label must be a string"),
        (lambda s: s.update(default=[{"code": ""}]), "default: Items, row 1: Code is required"),
        (lambda s: s.update(default="NY_NYT"), "default must be a list of rows"),
    ],
)
def test_bad_declarations_are_refused(change: Any, message: str) -> None:
    spec = _spec()
    change(spec)
    errors = spec_errors(spec)
    assert errors, "expected the declaration to be refused"
    assert any(message in e for e in errors), errors
    assert all(e.startswith("settings 'items': ") for e in errors)


def test_schema_refuses_a_repeater_without_fields_or_with_nesting() -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"name": "x", "type": "repeater", "label": "X"}, SETTING_SCHEMA)
    nested = _spec()
    nested["fields"] = [{"name": "inner", "type": "repeater", "label": "Inner"}]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(nested, SETTING_SCHEMA)
    # Other setting types keep their free-form shape.
    jsonschema.validate({"name": "x", "type": "string", "label": "X", "fields": 3}, SETTING_SCHEMA)


def test_manifest_errors_only_looks_at_repeaters() -> None:
    manifest = {"settings": [{"name": "a", "type": "string", "label": "A"}, _spec()]}
    assert repeater.manifest_errors(manifest) == []
    manifest["settings"].append(_spec(name="b", fields=[]))
    assert [e.split(":")[0] for e in repeater.manifest_errors(manifest)] == ["settings 'b'"]


def test_authored_install_refuses_a_bad_repeater() -> None:
    manifest = {
        "tesserae_compat": "1.x",
        "name": "Bad",
        "version": "0.0.1",
        "kind": "widget",
        "supports": {"sizes": ["sm"]},
        "settings": [_spec(min_rows=3, max_rows=1)],
    }
    with pytest.raises(InstallError) as caught:
        _validate_manifest(manifest, SCHEMA)
    assert caught.value.status == 422
    assert "min_rows is above max_rows" in caught.value.message


# ----- saving (strict) -------------------------------------------------------


def test_rows_round_trip_with_types_coerced_and_extras_dropped() -> None:
    raw = json.dumps(
        [
            {"on": False, "code": "ab", "note": "line 1\r\nline 2", "n": "7", "mode": "2", "x": 1},
            {"code": "CD"},
        ]
    )
    rows = parse_rows(_spec(), raw)
    assert rows == [
        {"on": False, "code": "ab", "note": "line 1\nline 2", "n": 7, "mode": 2},
        {"on": True, "code": "CD", "note": "", "n": None, "mode": "a"},
    ]
    assert parse_rows(_spec(), json.dumps(rows)) == rows
    assert parse_rows(_spec(), rows) == rows


@pytest.mark.parametrize(
    ("value", "flag"),
    [
        (True, True),
        ("true", True),
        ("on", True),
        (1, True),
        ("", False),
        ("off", False),
        (0, False),
    ],
)
def test_booleans_accept_the_usual_spellings(value: Any, flag: bool) -> None:
    assert parse_rows(_spec(), [{**ROW, "on": value}])[0]["on"] is flag


def test_blank_means_no_rows() -> None:
    assert parse_rows(_spec(), "") == []
    assert parse_rows(_spec(), "[]") == []


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("{not json", "didn't arrive as JSON"),
        ('{"code": "A"}', "expected a list of rows"),
        (json.dumps(["A"]), "row 1: expected an object"),
        (json.dumps([ROW] * 5), "at most 4 rows, not 5"),
        (json.dumps([{**ROW, "code": ["A"]}]), "Code can't hold a list or an object"),
        (json.dumps([{**ROW, "note": {"a": 1}}]), "Note can't hold a list or an object"),
        (json.dumps([{**ROW, "code": 12}]), "Code must be text"),
        (json.dumps([{**ROW, "code": "A" * 11}]), "Code is longer than 10 characters"),
        (json.dumps([{**ROW, "code": "A\nB"}]), "Code must be one line"),
        (json.dumps([{**ROW, "note": "x" * 41}]), "Note is longer than 40 characters"),
        (json.dumps([{**ROW, "code": "  "}]), "row 1: Code is required"),
        (json.dumps([{**ROW, "code": "a b"}]), "Code: letters, digits, _ and - only"),
        (json.dumps([ROW, {**ROW, "code": " ab "}]), "row 2: ab is already in another row"),
        (json.dumps([{**ROW, "n": 11}]), "N must be at most 10"),
        (json.dumps([{**ROW, "n": -1}]), "N must be at least 0"),
        (json.dumps([{**ROW, "n": "many"}]), "N must be a number"),
        (json.dumps([{**ROW, "n": True}]), "N must be a number"),
        ('[{"code": "AB", "n": NaN}]', "N must be a finite number"),
        (json.dumps([{**ROW, "mode": "b"}]), "'b' isn't one of the choices for Mode"),
        (json.dumps([{**ROW, "on": "maybe"}]), "On must be true or false"),
    ],
)
def test_bad_rows_are_refused(raw: str, message: str) -> None:
    with pytest.raises(RepeaterValueError) as caught:
        parse_rows(_spec(), raw)
    assert message in str(caught.value)
    assert str(caught.value).startswith("Items")


def test_min_rows_is_enforced() -> None:
    with pytest.raises(RepeaterValueError, match="at least 1 rows"):
        parse_rows(_spec(min_rows=1), "[]")


def test_uniqueness_ignores_blank_values() -> None:
    spec = _spec()
    _sub(spec, "code")["required"] = False
    rows = parse_rows(spec, [{**ROW, "code": ""}, {**ROW, "code": ""}])
    assert [r["code"] for r in rows] == ["", ""]


# ----- showing (forgiving) ---------------------------------------------------


def test_normalise_keeps_what_it_can_and_never_raises() -> None:
    stored = [
        {"on": "yes", "code": "A" * 50, "note": "x\r\ny", "n": 99, "mode": "zzz", "extra": 1},
        "not a row",
        {"code": ["nested"], "n": "nope"},
        None,
    ]
    assert normalise_rows(_spec(), stored) == [
        {"on": True, "code": "A" * 10, "note": "x\ny", "n": 10, "mode": "a"},
        {"on": True, "code": "", "note": "", "n": None, "mode": "a"},
    ]


@pytest.mark.parametrize("value", [None, "", "NY_NYT 3.5", 7, {"code": "A"}, "[broken"])
def test_normalise_shows_anything_else_as_no_rows(value: Any) -> None:
    assert normalise_rows(_spec(), value) == []


def test_normalise_reads_a_json_string_and_cuts_at_max_rows() -> None:
    assert len(normalise_rows(_spec(), json.dumps([ROW] * 9))) == 4


def test_legacy_rows_hook_upgrades_an_old_value_for_the_form() -> None:
    seen: list[tuple[str, Any]] = []

    def legacy(name: str, value: Any) -> Any:
        seen.append((name, value))
        return [{"code": c} for c in str(value).split()]

    rows = repeater.rows_for_form(_spec(), "AB CD", legacy)
    assert seen == [("items", "AB CD")]
    assert [r["code"] for r in rows] == ["AB", "CD"]
    # A list (or a JSON list) is already rows: the hook isn't asked.
    assert repeater.rows_for_form(_spec(), [ROW], legacy) == [ROW]
    assert repeater.rows_for_form(_spec(), json.dumps([ROW]), legacy) == [ROW]
    assert len(seen) == 1


def test_a_failing_legacy_hook_shows_no_rows() -> None:
    def broken(name: str, value: Any) -> Any:
        raise RuntimeError("boom")

    assert repeater.rows_for_form(_spec(), "AB", broken) == []
    assert repeater.rows_for_form(_spec(), "AB", lambda n, v: "junk") == []


# ----- the settings card, end to end -----------------------------------------

LEGACY_HOOK = """
def legacy_rows(name, value):
    if name != "items":
        return None
    return [{"code": c, "on": True} for c in str(value).split()]
"""


def _write_plugin(
    root: Path, plugin_id: str, settings: list[dict[str, Any]], server: str = ""
) -> None:
    folder = root / plugin_id
    folder.mkdir(parents=True)
    manifest = {
        "tesserae_compat": "1.x",
        "name": plugin_id.replace("_", " ").title(),
        "version": "0.0.1",
        "kind": "widget",
        "supports": {"sizes": ["sm"]},
        "settings": settings,
    }
    (folder / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "client.js").write_text("export default function () {}\n", encoding="utf-8")
    if server:
        (folder / "server.py").write_text(server, encoding="utf-8")


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    plugins_dir = tmp_path / "plugins"
    _write_plugin(
        plugins_dir,
        "rep_demo",
        [{"name": "title", "type": "string", "label": "Title", "default": ""}, _spec()],
        LEGACY_HOOK,
    )
    _write_plugin(
        plugins_dir, "rep_broken", [_spec(fields=[{"name": "x", "type": "repeater", "label": "X"}])]
    )
    a = create_app(testing=False, data_root=tmp_path / "data", plugins_dir=plugins_dir)
    a.config["TESTING"] = True
    return a


def _client(app: Flask) -> Any:
    client = app.test_client()
    client.post("/setup", data={"password": "abcdefgh", "password_confirm": "abcdefgh"})
    return client


def _card(client: Any) -> str:
    page = client.get("/settings/plugins").get_data(as_text=True)
    card = page[page.index('id="plugin-rep_demo"') :]
    return card[: card.index("</section>")]


def _storage(card: str) -> list[dict[str, Any]]:
    match = re.search(r'<input type="hidden" name="items" value="([^"]*)"', card)
    assert match, "the hidden rows input is missing"
    value: list[dict[str, Any]] = json.loads(html_lib.unescape(match.group(1)))
    return value


def _stored(app: Flask) -> dict[str, Any]:
    store = app.config["SETTINGS_STORE"]
    plugin = app.config["PLUGIN_REGISTRY"].get("rep_demo")
    result: dict[str, Any] = store.get_for_runtime(
        "plugins", "rep_demo", plugin.manifest["settings"]
    )
    return result


def test_a_bad_repeater_keeps_the_plugin_from_loading(app: Flask) -> None:
    registry = app.config["PLUGIN_REGISTRY"]
    assert registry.get("rep_broken") is None
    assert any(e.plugin_id == "rep_broken" and "repeater" in e.message for e in registry.errors), [
        e.message for e in registry.errors
    ]
    assert registry.get("rep_demo") is not None


def test_the_card_draws_the_default_rows_and_a_template(app: Flask) -> None:
    card = _card(_client(app))
    assert _storage(card) == [{"on": True, "code": "X1", "note": "", "n": 3, "mode": 2}]
    rows = card[card.index("data-repeater-list") : card.index("data-repeater-template")]
    assert rows.count("data-repeater-row") == 1
    assert 'value="X1"' in rows
    assert '<option value="2" selected>Two</option>' in rows
    # The row controls are never submitted on their own.
    assert not re.search(r'<(input|textarea|select)[^>]*\bname="(on|code|note|n|mode)"', card)
    template = card[card.index("<template data-repeater-template>") :]
    assert 'id="plugin-rep_demo-items-__i__-code"' in template
    assert 'data-repeater-max="4"' in card and "Add item" in card


def test_a_save_stores_the_rows_as_a_list(app: Flask) -> None:
    client = _client(app)
    rows = [{"on": False, "code": "B2", "note": "hi", "n": "4", "mode": "a"}, {"code": "c3"}]
    resp = client.post("/settings/plugin-rep_demo", data={"title": "T", "items": json.dumps(rows)})
    assert resp.status_code == 302
    saved = _stored(app)
    assert saved["title"] == "T"
    assert saved["items"] == [
        {"on": False, "code": "B2", "note": "hi", "n": 4, "mode": "a"},
        {"on": True, "code": "c3", "note": "", "n": None, "mode": "a"},
    ]
    assert _storage(_card(client)) == saved["items"]
    # Removing every row is a save too.
    client.post("/settings/plugin-rep_demo", data={"title": "T", "items": "[]"})
    assert _stored(app)["items"] == []


def test_a_bad_save_changes_nothing_and_says_why(app: Flask) -> None:
    client = _client(app)
    client.post("/settings/plugin-rep_demo", data={"title": "T", "items": json.dumps([ROW])})
    resp = client.post(
        "/settings/plugin-rep_demo",
        data={"title": "Changed", "items": json.dumps([ROW, ROW])},
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "settings not saved" in body
    assert "AB is already in another row" in body
    saved = _stored(app)
    assert saved["title"] == "T"  # the whole card waits for the rows
    assert saved["items"] == [ROW]
    for raw in ("not json", '{"code": "A"}', json.dumps([ROW] * 9)):
        client.post("/settings/plugin-rep_demo", data={"title": "T", "items": raw})
        assert _stored(app)["items"] == [ROW]


def test_a_form_without_the_repeater_leaves_the_rows_alone(app: Flask) -> None:
    client = _client(app)
    client.post("/settings/plugin-rep_demo", data={"title": "T", "items": json.dumps([ROW])})
    client.post("/settings/plugin-rep_demo", data={"title": "Other"})
    saved = _stored(app)
    assert (saved["title"], saved["items"]) == ("Other", [ROW])


def test_values_are_escaped_where_they_are_drawn(app: Flask) -> None:
    client = _client(app)
    evil = '"><script>alert(1)</script>'
    rows = [{**ROW, "code": "OK", "note": evil}]
    client.post("/settings/plugin-rep_demo", data={"title": "", "items": json.dumps(rows)})
    card = _card(client)
    assert "<script>alert(1)" not in card
    assert _storage(card)[0]["note"] == evil


def test_a_value_stored_before_the_repeater_shows_as_rows_until_saved(app: Flask) -> None:
    store = app.config["SETTINGS_STORE"]
    store.patch_section("plugins", {"rep_demo": {"items": "AA BB"}})
    client = _client(app)
    shown = _storage(_card(client))
    assert [r["code"] for r in shown] == ["AA", "BB"]
    assert _stored(app)["items"] == "AA BB"  # reading the card doesn't write
    client.post("/settings/plugin-rep_demo", data={"title": "", "items": json.dumps(shown)})
    assert _stored(app)["items"] == shown


# ----- audit regressions: patterns, defaults, huge numbers ----------------------


@pytest.mark.parametrize(
    ("pattern", "problem"),
    [
        ("(a+)+$", "uses ("),  # the classic catastrophic backtracker
        ("(a|aa)*", "uses ("),
        ("a|b", "uses |"),
        ("^abc", "uses ^"),
        ("abc$", "uses $"),
        ("(?:ab)", "uses ("),
        ("(a)\\1", "uses ("),
        ("a\\1", "uses \\1"),
        ("\\s+", "uses \\s"),
        ("a**", "counts one part twice"),
        ("a+?", "counts one part twice"),
        ("a{2}+", "counts one part twice"),
        ("[a-z]+[0-9]+", "only one part vary"),  # polynomial backtracking in re
        ("a*a*a*b", "only one part vary"),
        ("+a", "nothing before it"),
        ("[a-z-]", "has - inside [ ]"),
        ("[-a]", "has - inside [ ]"),
        ("[a/b]", "has / inside [ ]"),
        ("[a&&b]", "reserves"),
        ("[z-a]", "bad range"),
        ("[]", "empty [ ]"),
        ("[abc", "isn't closed"),
        ("a{5000}", "count over"),
        ("a{3,1}", "count over"),
        ("a{x}", "isn't a count"),
        ("x" * 201, "must be 1 to 200"),
        ("", "must be 1 to 200"),
    ],
)
def test_patterns_outside_the_safe_subset_are_refused(pattern: str, problem: str) -> None:
    spec = _spec()
    spec["fields"].append({"name": "x", "type": "string", "label": "X", "pattern": pattern})
    errors = spec_errors(spec)
    assert any(problem in e for e in errors), errors


@pytest.mark.parametrize(
    "pattern",
    [
        "[A-Za-z0-9_\\-]+",
        "[A-Z]{2}_[A-Z0-9]+",
        "#[0-9a-fA-F]{6}",
        "\\d{3}-\\d{4}",
        "[^ ]*",
        "v\\.\\d?",
        "x.y",
        "[\\w.]{1,8}",
        "a/b",
        "[a\\-z]+",
        "\\(\\d+\\)",
    ],
)
def test_the_safe_subset_matches_like_python_re(pattern: str) -> None:
    """Every pattern the subset accepts means what ``re`` means by it; the
    matcher just never backtracks. Checked on a spread of inputs."""
    assert repeater.pattern_error(pattern) is None
    parts = repeater._compile_pattern(pattern)
    samples = [
        "",
        "a",
        "A_9-z",
        "AB_X1",
        "AB_",
        "ab_X1",
        "#00ffAA",
        "#00ffA",
        "#00ffAAA",
        "555-1234",
        "5551234",
        "no spaces",
        "nospaces",
        "v.",
        "v.1",
        "v.12",
        "xzy",
        "x\ny",
        "a.b_c",
        "abcdefghi",
        "a/b",
        "a-z",
        "(42)",
        "(4a)",
    ]
    for text in samples:
        expected = re.fullmatch(pattern, text, flags=re.ASCII) is not None
        assert repeater._pattern_matches(parts, text) == expected, (pattern, text)


def test_a_pattern_never_takes_long() -> None:
    parts = repeater._compile_pattern("[a-z]+")
    start = time.monotonic()
    for text in ("a" * 20_000 + "!", "a" * 20_000):
        repeater._pattern_matches(parts, text)
    assert time.monotonic() - start < 0.5


def test_a_save_with_a_pattern_is_checked_by_the_safe_matcher() -> None:
    spec = _spec()
    _sub(spec, "code")["pattern"] = "[a-z]+"
    _sub(spec, "code")["max_length"] = 500
    start = time.monotonic()
    with pytest.raises(RepeaterValueError, match="letters, digits"):
        parse_rows(spec, [{**ROW, "code": "a" * 400 + "!"}])
    assert time.monotonic() - start < 0.5
    assert parse_rows(spec, [{**ROW, "code": "abc"}])[0]["code"] == "abc"


@pytest.mark.parametrize(
    ("sub", "problem"),
    [
        ({"type": "string", "default": ["not text"]}, "can't hold a list"),
        ({"type": "string", "default": 5}, "must be text"),
        ({"type": "string", "default": "x" * 20, "max_length": 10}, "longer than 10"),
        ({"type": "string", "default": "a b", "pattern": "[a-z]+"}, "expected format"),
        ({"type": "string", "default": "two\nlines"}, "one line"),
        ({"type": "textarea", "default": {"a": 1}}, "can't hold a list"),
        ({"type": "number", "default": 50, "max": 10}, "at most 10"),
        ({"type": "number", "default": "5"}, "as stored"),
        ({"type": "number", "default": True}, "must be a number"),
        ({"type": "boolean", "default": "yes"}, "as stored"),
        ({"type": "boolean", "default": 1}, "as stored"),
        (
            {"type": "select", "default": "c", "choices": [{"value": "a", "label": "A"}]},
            "isn't one of the choices",
        ),
        (
            {"type": "select", "default": "2", "choices": [{"value": 2, "label": "Two"}]},
            "as stored",
        ),
    ],
)
def test_sub_field_defaults_are_checked_at_load(sub: dict[str, Any], problem: str) -> None:
    spec = _spec()
    spec["fields"].append({"name": "x", "label": "X", **sub})
    errors = spec_errors(spec)
    assert any(problem in e for e in errors), errors


def test_a_row_saved_without_a_key_gets_a_checked_default() -> None:
    assert parse_rows(_spec(), [{"code": "AB"}]) == [
        {"on": True, "code": "AB", "note": "", "n": None, "mode": "a"}
    ]
    # A declaration that never went through the load checks still can't
    # slip a bad default into storage.
    unchecked = _spec()
    unchecked["fields"].append({"name": "x", "type": "string", "label": "X", "default": ["no"]})
    with pytest.raises(RepeaterValueError, match="X can't hold a list"):
        parse_rows(unchecked, [{"code": "AB"}])
    unchecked = _spec()
    _sub(unchecked, "n")["default"] = 99
    with pytest.raises(RepeaterValueError, match="N must be at most 10"):
        parse_rows(unchecked, [{"code": "AB", "n": ""}])


@pytest.mark.parametrize(
    "raw",
    [
        [{**ROW, "n": 10**400}],
        '[{"code": "AB", "n": 1' + "0" * 400 + "}]",
        [{**ROW, "n": "1" + "0" * 400}],
        '[{"code": "AB", "n": 1e400}]',
    ],
    ids=["int", "json-int", "text", "json-float"],
)
def test_a_number_too_big_for_a_float_is_refused_not_raised(raw: Any) -> None:
    with pytest.raises(RepeaterValueError, match="finite number"):
        parse_rows(_spec(), raw)
    assert normalise_rows(_spec(), [{**ROW, "n": 10**400}])[0]["n"] is None
    spec = _spec()
    _sub(spec, "n")["max"] = 10**400
    assert any("max must be a number" in e for e in spec_errors(spec))


# ----- audit re-check regressions ---------------------------------------------


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_number_default_is_refused_without_recursing(blank: str) -> None:
    spec = _spec()
    spec["fields"].append({"name": "x", "type": "number", "label": "X", "default": blank})
    errors = spec_errors(spec)  # used to recurse until RecursionError
    assert any("must be a number value as stored" in e for e in errors), errors
    manifest = {
        "tesserae_compat": "1.x",
        "name": "Blank",
        "version": "0.0.1",
        "kind": "widget",
        "supports": {"sizes": ["sm"]},
        "settings": [spec],
    }
    with pytest.raises(InstallError) as caught:
        _validate_manifest(manifest, SCHEMA)
    assert caught.value.status == 422
    # An unchecked declaration saves the row with no number, once.
    assert parse_rows(spec, [{"code": "AB"}])[0]["x"] is None
    assert parse_rows(spec, [{"code": "AB", "x": blank}])[0]["x"] is None


def test_a_blank_number_default_keeps_a_plugin_from_loading(tmp_path: Path) -> None:
    spec = _spec()
    spec["fields"].append({"name": "x", "type": "number", "label": "X", "default": ""})
    _write_plugin(tmp_path / "plugins", "rep_blank", [spec])
    app = create_app(testing=True, data_root=tmp_path / "data", plugins_dir=tmp_path / "plugins")
    registry = app.config["PLUGIN_REGISTRY"]
    assert registry.get("rep_blank") is None
    assert any("number value as stored" in e.message for e in registry.errors)


@pytest.mark.parametrize("pattern", ["a{١}", "a{１}", "a{1,٣}", "a{１,2}"])
def test_counts_take_ascii_digits_only(pattern: str) -> None:
    assert repeater.pattern_error(pattern) == "has a { that isn't a count like {3} or {1,8}"


def _list_field(**extra: object) -> dict[str, object]:
    return {
        "name": "items",
        "type": "repeater",
        "label": "Items",
        "fields": [
            {"name": "on", "type": "boolean", "label": "On"},
            {"name": "label", "type": "string", "label": "Label"},
            {"name": "slug", "type": "string", "label": "Slug"},
            {"name": "note", "type": "textarea", "label": "Note"},
        ],
        **extra,
    }


def test_list_options_default_and_declared() -> None:
    assert repeater.list_options(_list_field()) == {
        "title": ["label"],
        "summary": "note",
        "fixed": {},
        "add": True,
    }
    declared = _list_field(
        list={"title": ["label", "slug"], "summary": "slug", "fixed": {"slug": ["x"]}, "add": False}
    )
    assert repeater.spec_errors(declared) == []
    assert repeater.list_options(declared) == {
        "title": ["label", "slug"],
        "summary": "slug",
        "fixed": {"slug": ["x"]},
        "add": False,
    }


@pytest.mark.parametrize(
    "bad",
    [
        "rows",
        {"title": ["note"]},
        {"title": "label"},
        {"summary": "nope"},
        {"fixed": {"slug": "x"}},
        {"fixed": {"nope": ["x"]}},
        {"add": "no"},
        {"sort": True},
    ],
)
def test_list_options_errors_are_reported_and_ignored(bad: object) -> None:
    field = _list_field(list=bad)
    assert repeater.spec_errors(field)
    assert repeater.list_options(field)["title"] in (["label"], [])
