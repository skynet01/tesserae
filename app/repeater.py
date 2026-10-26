"""The ``repeater`` settings field: a list of rows, each row a small fixed set
of sub-fields, edited as one control (add, remove, move up and down).

A manifest declares it in ``settings``::

    {
      "name": "papers", "type": "repeater", "label": "Papers",
      "fields": [
        {"name": "enabled", "type": "boolean", "label": "In rotation", "default": true},
        {"name": "code", "type": "string", "label": "Code", "required": true},
        {"name": "css", "type": "textarea", "label": "CSS", "max_length": 1000}
      ],
      "min_rows": 0, "max_rows": 30, "add_label": "Add paper", "row_label": "Paper",
      "default": [{"enabled": true, "code": "NY_NYT", "css": ""}]
    }

The stored value is a JSON list of objects, one per row, each carrying every
declared sub-field and nothing else. Sub-fields are ``string``, ``textarea``,
``boolean``, ``number`` and ``select``; a repeater can't hold another one.

Three ways in, three levels of trust:

* :func:`spec_errors` / :func:`manifest_errors` check a declaration when a
  plugin loads (and when an authored widget is installed), on top of what
  ``schema/plugin.schema.json`` already checks structurally.
* :func:`parse_rows` is the strict save path. The settings form posts the
  rows as JSON in one hidden input; anything malformed raises
  :class:`RepeaterValueError` and nothing is saved, so a bad post can't
  wipe the stored rows. Unknown keys in a row are dropped.
* :func:`normalise_rows` is the forgiving read path, for showing a stored
  value in the form: whatever doesn't fit is dropped or falls back to the
  sub-field's default, never raised.

A plugin whose setting used to be another type (a textarea with one item per
line, say) can export ``legacy_rows(name, value)`` from its ``server.py``.
When the stored value isn't a list of rows, the settings page asks it for
rows to show; the next save stores them. Without it, such a value shows as
no rows.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, TypeGuard

logger = logging.getLogger(__name__)

TYPE = "repeater"
SUBFIELD_TYPES = ("string", "textarea", "boolean", "number", "select")

# Bounds a manifest can't raise. A settings card with hundreds of rows isn't a
# form anyone can edit, and every row round-trips through settings.json.
HARD_MAX_ROWS = 200
DEFAULT_MAX_ROWS = 50
MAX_SUBFIELDS = 12
HARD_MAX_LENGTH = 20_000
DEFAULT_MAX_LENGTH = {"string": 500, "textarea": 5000}
MAX_LABEL_CHARS = 40

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TRUE = frozenset({"true", "on", "1", "yes"})
_FALSE = frozenset({"false", "off", "0", "no", ""})
# Keys that mean something on a top-level setting but would be silently
# ignored on a sub-field. Refused rather than ignored, so a manifest doesn't
# believe a row value is encrypted, conditional or filled in dynamically.
_UNSUPPORTED_SUBFIELD_KEYS = (
    "secret",
    "show_if",
    "choices_from",
    "fields",
    "advanced",
    "fill_from",
)

LegacyRows = Callable[[str, Any], Any]


class RepeaterValueError(ValueError):
    """A submitted repeater value that can't be saved as it is. The message
    names the field and row and is fit to show the person who saved it."""


# ----- the declaration -------------------------------------------------------


def is_repeater(field: Mapping[str, Any]) -> bool:
    return field.get("type") == TYPE


def subfields(field: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The declared sub-fields that can hold a value. A declaration that
    never went through :func:`spec_errors` (a hand-built form) loses the
    entries it couldn't use rather than breaking the page."""
    raw = field.get("fields")
    if not isinstance(raw, list):
        return []
    return [
        dict(sub)
        for sub in raw
        if isinstance(sub, Mapping)
        and isinstance(sub.get("name"), str)
        and sub.get("type") in SUBFIELD_TYPES
    ]


def _int_or(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value


def max_rows(field: Mapping[str, Any]) -> int:
    return max(1, min(_int_or(field.get("max_rows"), DEFAULT_MAX_ROWS), HARD_MAX_ROWS))


def min_rows(field: Mapping[str, Any]) -> int:
    return max(0, min(_int_or(field.get("min_rows"), 0), max_rows(field)))


def max_length(sub: Mapping[str, Any]) -> int:
    fallback = DEFAULT_MAX_LENGTH.get(str(sub.get("type")), DEFAULT_MAX_LENGTH["string"])
    return max(1, min(_int_or(sub.get("max_length"), fallback), HARD_MAX_LENGTH))


def _label(field: Mapping[str, Any]) -> str:
    return str(field.get("label") or field.get("name") or "Setting")


def _choice_values(sub: Mapping[str, Any]) -> list[Any]:
    choices = sub.get("choices")
    if not isinstance(choices, list):
        return []
    return [c.get("value") for c in choices if isinstance(c, Mapping) and "value" in c]


def _is_number(value: Any) -> TypeGuard[int | float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def subfield_default(sub: Mapping[str, Any]) -> Any:
    """What a new row holds for ``sub``: its declared default, else empty
    (false, no number, the first choice)."""
    if "default" in sub:
        return sub["default"]
    kind = sub.get("type")
    if kind == "boolean":
        return False
    if kind == "number":
        return None
    if kind == "select":
        values = _choice_values(sub)
        return values[0] if values else ""
    return ""


def row_defaults(field: Mapping[str, Any]) -> dict[str, Any]:
    return {str(sub["name"]): subfield_default(sub) for sub in subfields(field) if "name" in sub}


def _subfield_errors(sub: Any, index: int, seen: set[str]) -> list[str]:
    where = f"fields[{index}]"
    if not isinstance(sub, Mapping):
        return [f"{where} must be an object"]
    errors: list[str] = []
    name = sub.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        errors.append(f"{where}.name must be lower case letters, digits and _ (max 32)")
    elif name in seen:
        errors.append(f"{where}.name {name!r} is declared twice")
    else:
        seen.add(name)
    if not isinstance(sub.get("label"), str) or not sub["label"].strip():
        errors.append(f"{where}.label is required")
    kind = sub.get("type")
    if kind == TYPE:
        errors.append(f"{where}: a repeater can't contain another repeater")
    elif kind not in SUBFIELD_TYPES:
        errors.append(f"{where}.type must be one of {', '.join(SUBFIELD_TYPES)}, not {kind!r}")
    for key in _UNSUPPORTED_SUBFIELD_KEYS:
        if key in sub:
            errors.append(f"{where}.{key} isn't supported inside a repeater")
    if "max_length" in sub:
        limit = sub["max_length"]
        if kind not in ("string", "textarea"):
            errors.append(f"{where}.max_length only applies to string and textarea")
        elif (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= HARD_MAX_LENGTH
        ):
            errors.append(f"{where}.max_length must be an integer from 1 to {HARD_MAX_LENGTH}")
    if "pattern" in sub:
        if kind != "string":
            errors.append(f"{where}.pattern only applies to string")
        elif not isinstance(sub["pattern"], str):
            errors.append(f"{where}.pattern must be a string")
        else:
            try:
                re.compile(sub["pattern"])
            except re.error as err:
                errors.append(f"{where}.pattern doesn't compile: {err}")
    if "unique" in sub and kind not in ("string", "select"):
        errors.append(f"{where}.unique only applies to string and select")
    if kind == "select":
        values = _choice_values(sub)
        if not values or len(values) != len(sub.get("choices") or []):
            errors.append(f"{where}.choices must be a non-empty list of {{value, label}}")
        elif any(not isinstance(v, (str, int, float)) or isinstance(v, bool) for v in values):
            errors.append(f"{where}.choices values must be strings or numbers")
        elif len({str(v) for v in values}) != len(values):
            errors.append(f"{where}.choices values must be unique")
    if kind == "number":
        for key in ("min", "max", "step"):
            if key in sub and not _is_number(sub[key]):
                errors.append(f"{where}.{key} must be a number")
        low, high = sub.get("min"), sub.get("max")
        if _is_number(low) and _is_number(high) and low > high:
            errors.append(f"{where}.min is above its max")
    return errors


def spec_errors(field: Mapping[str, Any]) -> list[str]:
    """Problems with one ``repeater`` declaration, worded for a manifest
    author; [] when it is usable."""
    if not is_repeater(field):
        return []
    name = field.get("name")
    errors: list[str] = []
    if field.get("secret"):
        errors.append("secret isn't supported on a repeater (row values are stored in plain text)")
    raw = field.get("fields")
    if not isinstance(raw, list) or not raw:
        errors.append("fields must be a non-empty list of sub-fields")
        raw = []
    elif len(raw) > MAX_SUBFIELDS:
        errors.append(f"fields can hold at most {MAX_SUBFIELDS} sub-fields")
    seen: set[str] = set()
    for index, sub in enumerate(raw):
        errors.extend(_subfield_errors(sub, index, seen))
    for key in ("min_rows", "max_rows"):
        value = field.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= HARD_MAX_ROWS
        ):
            errors.append(f"{key} must be an integer from 0 to {HARD_MAX_ROWS}")
    low, high = field.get("min_rows"), field.get("max_rows")
    if high == 0:
        errors.append("max_rows must be at least 1")
    if isinstance(low, int) and isinstance(high, int) and low > high:
        errors.append("min_rows is above max_rows")
    for key in ("add_label", "row_label"):
        value = field.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > MAX_LABEL_CHARS):
            errors.append(f"{key} must be a string of at most {MAX_LABEL_CHARS} characters")
    if not errors and "default" in field:
        if not isinstance(field["default"], list):
            errors.append("default must be a list of rows")
        else:
            try:
                parse_rows(field, field["default"])
            except RepeaterValueError as err:
                errors.append(f"default: {err}")
    return [f"settings {name!r}: {e}" for e in errors]


def manifest_errors(manifest: Mapping[str, Any]) -> list[str]:
    """Problems with every repeater a plugin manifest declares."""
    errors: list[str] = []
    settings = manifest.get("settings")
    for field in settings if isinstance(settings, list) else []:
        if isinstance(field, Mapping):
            errors.extend(spec_errors(field))
    return errors


# ----- saving (strict) -------------------------------------------------------


def _strict_value(sub: Mapping[str, Any], raw: Any, where: str) -> Any:
    kind = sub.get("type")
    label = str(sub.get("label") or sub.get("name"))
    if raw is None:
        return subfield_default(sub)
    if isinstance(raw, (dict, list)):
        raise RepeaterValueError(f"{where}: {label} can't hold a list or an object")
    if kind in ("string", "textarea"):
        if not isinstance(raw, str):
            raise RepeaterValueError(f"{where}: {label} must be text")
        value = raw.replace("\r\n", "\n").replace("\r", "\n")
        limit = max_length(sub)
        if len(value) > limit:
            raise RepeaterValueError(f"{where}: {label} is longer than {limit} characters")
        if kind == "string" and "\n" in value:
            raise RepeaterValueError(f"{where}: {label} must be one line")
        pattern = sub.get("pattern")
        if isinstance(pattern, str) and value.strip() and not re.fullmatch(pattern, value.strip()):
            hint = sub.get("pattern_hint")
            if isinstance(hint, str) and hint.strip():
                raise RepeaterValueError(f"{where}: {label}: {hint.strip()}")
            raise RepeaterValueError(f"{where}: {label} is not in the expected format")
        return value
    if kind == "boolean":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, int) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str) and raw.strip().lower() in _TRUE | _FALSE:
            return raw.strip().lower() in _TRUE
        raise RepeaterValueError(f"{where}: {label} must be true or false")
    if kind == "number":
        number: int | float
        if isinstance(raw, bool):
            raise RepeaterValueError(f"{where}: {label} must be a number")
        if isinstance(raw, (int, float)):
            number = raw
        elif isinstance(raw, str):
            text = raw.strip()
            if not text:
                return subfield_default(sub)
            try:
                number = int(text)
            except ValueError:
                try:
                    number = float(text)
                except ValueError:
                    raise RepeaterValueError(f"{where}: {label} must be a number") from None
        else:
            raise RepeaterValueError(f"{where}: {label} must be a number")
        if not math.isfinite(number):
            raise RepeaterValueError(f"{where}: {label} must be a finite number")
        low, high = sub.get("min"), sub.get("max")
        if _is_number(low) and number < low:
            raise RepeaterValueError(f"{where}: {label} must be at least {low}")
        if _is_number(high) and number > high:
            raise RepeaterValueError(f"{where}: {label} must be at most {high}")
        return number
    if kind == "select":
        for value in _choice_values(sub):
            if str(value) == str(raw):
                return value
        raise RepeaterValueError(f"{where}: {raw!r} isn't one of the choices for {label}")
    raise RepeaterValueError(f"{where}: {label} has an unsupported type")


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def parse_rows(field: Mapping[str, Any], raw: Any) -> list[dict[str, Any]]:
    """The rows to store for a submitted value: a JSON string (what the form
    posts) or an already-decoded list. Raises :class:`RepeaterValueError`
    when the value isn't a list of row objects, has too many or too few
    rows, or a row value is the wrong type, out of range, too long, blank
    where ``required`` or repeated where ``unique``. A blank string means no
    rows. Keys a row carries beyond the declared sub-fields are dropped;
    missing ones get the sub-field's default."""
    label = _label(field)
    if isinstance(raw, str):
        if not raw.strip():
            raw = []
        else:
            try:
                raw = json.loads(raw)
            except ValueError:
                raise RepeaterValueError(f"{label}: the rows didn't arrive as JSON") from None
    if not isinstance(raw, list):
        raise RepeaterValueError(f"{label}: expected a list of rows")
    subs = subfields(field)
    low, high = min_rows(field), max_rows(field)
    if len(raw) > high:
        raise RepeaterValueError(f"{label}: at most {high} rows, not {len(raw)}")
    if len(raw) < low:
        raise RepeaterValueError(f"{label}: at least {low} rows")
    rows: list[dict[str, Any]] = []
    seen: dict[str, set[str]] = {}
    for index, item in enumerate(raw):
        where = f"{label}, row {index + 1}"
        if not isinstance(item, dict):
            raise RepeaterValueError(f"{where}: expected an object")
        row: dict[str, Any] = {}
        for sub in subs:
            name = str(sub["name"])
            value = _strict_value(sub, item.get(name), where)
            if sub.get("required") and _is_blank(value):
                raise RepeaterValueError(f"{where}: {sub.get('label') or name} is required")
            if sub.get("unique") and not _is_blank(value):
                key = str(value).strip().casefold()
                if key in seen.setdefault(name, set()):
                    raise RepeaterValueError(
                        f"{where}: {str(value).strip()} is already in another row"
                    )
                seen[name].add(key)
            row[name] = value
        rows.append(row)
    return rows


# ----- showing (forgiving) ---------------------------------------------------


def _lenient_value(sub: Mapping[str, Any], raw: Any) -> Any:
    kind = sub.get("type")
    if (
        kind in ("string", "textarea")
        and isinstance(raw, (int, float))
        and not isinstance(raw, bool)
    ):
        raw = str(raw)
    if kind in ("string", "textarea") and isinstance(raw, str):
        value = raw.replace("\r\n", "\n").replace("\r", "\n")
        if kind == "string":
            value = value.replace("\n", " ")
        return value[: max_length(sub)]
    if kind == "number" and _is_number(raw):
        number: int | float = raw
        low, high = sub.get("min"), sub.get("max")
        if _is_number(low):
            number = max(number, low)
        if _is_number(high):
            number = min(number, high)
        return number
    try:
        return _strict_value(sub, raw, "")
    except RepeaterValueError:
        return subfield_default(sub)


def _decoded(value: Any) -> Any:
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def normalise_rows(field: Mapping[str, Any], value: Any) -> list[dict[str, Any]]:
    """``value`` as rows, never raising: a list (or a JSON string of one)
    keeps its object items, each with exactly the declared sub-fields; a
    value that doesn't fit a sub-field falls back to its default, text is
    cut to ``max_length`` and numbers are clamped. Anything that isn't a
    list of rows is no rows. Rows past ``max_rows`` are dropped."""
    items = _decoded(value)
    if not isinstance(items, list):
        return []
    subs = subfields(field)
    rows: list[dict[str, Any]] = []
    for item in items:
        if len(rows) >= max_rows(field):
            break
        if not isinstance(item, Mapping):
            continue
        rows.append({str(sub["name"]): _lenient_value(sub, item.get(sub["name"])) for sub in subs})
    return rows


def rows_for_form(
    field: Mapping[str, Any], value: Any, legacy: LegacyRows | None = None
) -> list[dict[str, Any]]:
    """The rows the settings form shows for a stored ``value``. A value that
    isn't a list of rows (the setting used to be another type) goes through
    the owning plugin's ``legacy_rows(name, value)`` first, when it has one;
    a hook that fails or returns something unusable shows no rows."""
    if (
        legacy is not None
        and not isinstance(_decoded(value), list)
        and value is not None
        and value != ""
    ):
        try:
            value = legacy(str(field.get("name")), value)
        except Exception:
            logger.exception("legacy_rows failed for setting %r", field.get("name"))
            value = None
    return normalise_rows(field, value)


def prepare_state(
    fields: Iterable[Mapping[str, Any]],
    state: Mapping[str, Any],
    legacy: LegacyRows | None = None,
) -> dict[str, Any]:
    """``state`` (a settings section as the form will show it) with every
    repeater value replaced by its rows."""
    out = dict(state)
    for field in fields:
        if is_repeater(field) and field.get("name") in out:
            name = str(field["name"])
            out[name] = rows_for_form(field, out[name], legacy)
    return out


def view(field: Mapping[str, Any], value: Any) -> dict[str, Any]:
    """Everything the form macro needs to draw ``field`` holding ``value``:
    the rows, the sub-fields with their limits filled in, a new row's
    values and the row bounds. ``usable`` is False for a declaration with no
    sub-field it can draw, which the macro shows as a plain text box."""
    subs = []
    for sub in subfields(field):
        drawn = dict(sub)
        if drawn["type"] in ("string", "textarea"):
            drawn["max_length"] = max_length(sub)
        if drawn["type"] == "textarea":
            drawn["rows"] = max(1, min(_int_or(sub.get("rows"), 3), 20))
        if drawn["type"] == "select":
            drawn["choices"] = [
                c for c in sub.get("choices") or [] if isinstance(c, Mapping) and "value" in c
            ]
        subs.append(drawn)
    add_label = field.get("add_label")
    row_label = field.get("row_label")
    return {
        "usable": bool(subs),
        "rows": normalise_rows(field, value),
        "subs": subs,
        "defaults": row_defaults(field),
        "min_rows": min_rows(field),
        "max_rows": max_rows(field),
        "add_label": add_label if isinstance(add_label, str) and add_label.strip() else "Add row",
        "row_label": row_label if isinstance(row_label, str) and row_label.strip() else "Row",
    }
