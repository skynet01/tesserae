"""Every ``--t-*`` token the admin UI reads must be defined somewhere.

A misspelt token never errors: the browser takes the ``var()`` fallback,
which is usually a light-theme hex, so the element looks right in light mode
and stays light in dark mode."""

from __future__ import annotations

import re

from app.main import REPO_ROOT

_DEFINE = re.compile(r"(--t-[\w-]+)\s*:")
_SET_PROPERTY = re.compile(r"setProperty\(\s*['\"](--t-[\w-]+)")
_USE = re.compile(r"var\((--t-[\w-]+)")


def _sources() -> list:
    paths = [
        *(REPO_ROOT / "static").rglob("*.css"),
        *(REPO_ROOT / "static").rglob("*.js"),
        *(REPO_ROOT / "templates").rglob("*.html"),
    ]
    return [p for p in paths if "vendor" not in p.parts]


def test_every_used_theme_token_is_defined() -> None:
    sources = {p: p.read_text(encoding="utf-8") for p in _sources()}
    defined: set[str] = set()
    for text in sources.values():
        defined.update(_DEFINE.findall(text))
        defined.update(_SET_PROPERTY.findall(text))
    missing = [
        f"{path.relative_to(REPO_ROOT)}:{lineno}: {name}"
        for path, text in sources.items()
        for lineno, line in enumerate(text.splitlines(), 1)
        for name in _USE.findall(line)
        if name not in defined
    ]
    assert not missing, "undefined theme tokens:\n" + "\n".join(missing)
