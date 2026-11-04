"""The items a lineup step's dashboard shows one per visit, and how to ask
for a particular one.

A dashboard only has choosable items when exactly one of its widgets (a grid
cell, or a widget element on a canvas) walks a list and that widget can be
pinned to one entry through an option:

* ``picture_gallery`` walks its folder's photos; ``file`` (not a manifest
  option, read by the widget's ``fetch``) pins a photo by name.
* ``news_front_pages`` rotates through the paper list; its manifest option
  ``paper`` pins a paper by code. A cell already pinned to one paper has one
  item.

A second mode, "draw next", is for widgets whose items cannot be named but whose
output differs from one render to the next, so the arrows draw the dashboard
again instead of choosing:

* ``picture_google_album`` / ``picture_apple_album`` pick a random photo on
  every fetch (a preview too). A sequential album would move its real cursor on
  each fetch (they do not look at ``ctx["preview"]``), so a draw forces
  ``mode`` to random and never costs the frame its place.
* ``news_illustrated`` and ``comics_daily`` only move their cursor on a settled
  render and hold a preview still, so each reads a hidden ``_browse`` option
  (how many turns past the current one to show, read-only) that a draw sets.

A draw's key is ``draw:<nonce>:<n>``: the nonce makes each drawing its own
cached file, ``n`` is how far ahead of the current turn it looks.

Everything else shows one thing and reports one item, so the Lineups page draws
no arrows for it.

``pin_item`` returns the page definition with the chosen item (or the draw's
options) written into that placement's options. The composer applies it for
``/compose/<page>?item=`` (a preview or a push render); nothing is saved to the
page.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from flask import current_app, url_for

from app.lineups_view import ItemInfo

#: Items listed for a multi-item step (a gallery folder): enough for the peek
#: grid, without walking a folder of thousands of photos into the page.
MAX_STEP_ITEMS = 60

#: folder -> (stamp, total count, first MAX_STEP_ITEMS names). The stamp is
#: the folder's mtime (adding or removing a photo changes it), or a one-minute
#: bucket when the folder path can't be resolved, so a polling page doesn't
#: list a large folder on every refresh.
_GALLERY_CACHE: dict[str, tuple[float, int, tuple[str, ...]]] = {}


@dataclass(frozen=True)
class StepItems:
    """What a step's dashboard can show: the widget cell, the option that pins
    one item, the full count and the listed items (at most MAX_STEP_ITEMS)."""

    cell_id: str
    option: str
    count: int
    items: tuple[ItemInfo, ...]
    #: "pick": items are named and listed; "draw": no list, the arrows draw again
    mode: str = "pick"


def _plugin_module(plugin_id: str) -> Any:
    reg = current_app.config.get("PLUGIN_REGISTRY")
    plugin = reg.get(plugin_id) if reg is not None else None
    return getattr(plugin, "server_module", None) if plugin is not None else None


def _gallery_files(module: Any, folder: str) -> tuple[int, tuple[str, ...]] | None:
    stamp: float = float(int(time.time() // 60))
    resolve = getattr(module, "_folder_path", None)
    data_dir = getattr(module, "_data_dir", None)
    if callable(resolve) and callable(data_dir):
        try:
            path = resolve(folder, data_dir())
            if path is not None:
                stamp = -float(path.stat().st_mtime_ns)
        except Exception:
            pass
    cached = _GALLERY_CACHE.get(folder)
    if cached is not None and cached[0] == stamp:
        return cached[1], cached[2]
    lister = getattr(module, "list_folder_files", None)
    if not callable(lister):
        return None
    try:
        files = [str(f) for f in lister(folder)]
    except Exception:
        return None
    entry = (stamp, len(files), tuple(files[:MAX_STEP_ITEMS]))
    _GALLERY_CACHE[folder] = entry
    return entry[1], entry[2]


def _gallery_items(cell: Any) -> StepItems | None:
    folder = str(cell.options.get("folder") or "")
    module = _plugin_module("picture_gallery")
    listed = _gallery_files(module, folder) if module is not None else None
    if listed is None or listed[0] <= 1:
        return None
    count, names = listed
    version = getattr(module, "thumb_version", None)
    items: list[ItemInfo] = []
    for i, name in enumerate(names):
        ver = ""
        if callable(version):
            try:
                ver = str(version(folder, name))
            except Exception:
                ver = ""
        try:
            thumb = url_for(
                "picture_gallery_admin.serve_thumbnail",
                folder=folder or "_root",
                filename=name,
                v=ver or None,
            )
        except Exception:
            thumb = ""
        items.append(ItemInfo(i, name, thumb, key=name, version=ver))
    return StepItems(str(cell.id), "file", count, tuple(items))


def _news_items(cell: Any) -> StepItems | None:
    if str(cell.options.get("paper") or "").strip():
        return None  # pinned to one paper: nothing to choose
    module = _plugin_module("news_front_pages")
    lister = getattr(module, "rotation_papers", None)
    if not callable(lister):
        return None
    try:
        papers = [(str(c), str(label)) for c, label in lister()]
        stamp = str(module.edition_stamp())
    except Exception:
        return None
    if len(papers) <= 1:
        return None
    items = tuple(
        ItemInfo(i, label, "", key=code, version=stamp) for i, (code, label) in enumerate(papers)
    )
    return StepItems(str(cell.id), "paper", len(items), items)


def _draw_items(cell: Any) -> StepItems | None:
    return StepItems(str(cell.id), "_draw", 0, (), mode="draw")


def _news_illustrated_items(cell: Any) -> StepItems | None:
    """Stories can only be browsed when there is more than one."""
    module = _plugin_module("news_illustrated")
    reg = current_app.config.get("PLUGIN_REGISTRY")
    plugin = reg.get("news_illustrated") if reg is not None else None
    store = getattr(module, "store", None)
    if plugin is None or store is None:
        return None
    try:
        if len(store.load_stories(plugin.data_dir)) <= 1:
            return None
    except Exception:
        return None
    return _draw_items(cell)


#: plugin id -> reader of that cell's items. A widget joins by being pinnable
#: through one cell option and listing what it would walk.
_PROVIDERS: dict[str, Callable[[Any], StepItems | None]] = {
    "picture_gallery": _gallery_items,
    "news_front_pages": _news_items,
    "picture_google_album": _draw_items,
    "picture_apple_album": _draw_items,
    "comics_daily": _draw_items,
    "news_illustrated": _news_illustrated_items,
}

#: A draw's key; the nonce is the page's own random token, ``n`` the turns ahead.
DRAW_KEY = re.compile(r"draw:([0-9a-f]{8,16}):(\d{1,2})")


def _placements(page: Any) -> list[tuple[str, Any]]:
    """Every list-walking widget on ``page`` as ``(plugin id, placement)``. Both
    a grid cell and a canvas element carry the ``id`` and ``options`` a provider
    reads; a canvas's other element kinds only borrow a widget's data."""
    found = [
        (str(c.plugin), c)
        for c in getattr(page, "cells", None) or []
        if getattr(c, "plugin", None) in _PROVIDERS
    ]
    canvas = getattr(page, "canvas", None)
    found += [
        (str(e.widget), e)
        for e in getattr(canvas, "els", None) or []
        if getattr(e, "kind", "widget") == "widget" and getattr(e, "widget", None) in _PROVIDERS
    ]
    return found


def step_choices(page: Any) -> StepItems | None:
    """The choosable items of ``page``, or None: exactly one placement may walk
    a list, otherwise which one a pin would apply to is ambiguous."""
    placements = _placements(page)
    if len(placements) != 1:
        return None
    plugin_id, placement = placements[0]
    return _PROVIDERS[plugin_id](placement)


def step_items(page: Any) -> tuple[int, tuple[ItemInfo, ...]]:
    """``(count, items)`` for the Lineups view: one item unless the dashboard
    walks a list."""
    found = step_choices(page)
    return (found.count, found.items) if found is not None and found.mode == "pick" else (1, ())


def step_draws(page: Any) -> bool:
    """True when the arrows draw the dashboard again rather than choose."""
    found = step_choices(page)
    return found is not None and found.mode == "draw"


def find_item(page: Any, key: str) -> ItemInfo | None:
    """The listed item with this key, or None (unknown, or not choosable)."""
    found = step_choices(page)
    if found is None or not key:
        return None
    if found.mode == "draw":
        m = DRAW_KEY.fullmatch(key)
        return ItemInfo(int(m.group(2)), "", key=key) if m else None
    return next((it for it in found.items if it.key == key), None)


def pin_item(page: Any, page_dict: dict[str, Any], key: str) -> dict[str, Any] | None:
    """``page_dict`` with the widget's pin option set to ``key``, or None when
    ``key`` is not one of the page's items."""
    found = step_choices(page)
    if found is None or find_item(page, key) is None:
        return None
    options: dict[str, Any] = {found.option: key}
    if found.mode == "draw":
        m = DRAW_KEY.fullmatch(key)
        assert m is not None
        options = {"_draw": m.group(1), "_browse": int(m.group(2))}

    def pinned(placements: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for one in placements:
            if str(one.get("id")) == found.cell_id:
                merged = {**(one.get("options") or {}), **options}
                # A sequential album moves its real cursor on every fetch, a
                # preview too: draw at random instead of costing the frame a photo.
                if found.mode == "draw" and merged.get("mode") == "sequential":
                    merged["mode"] = "random"
                one = {**one, "options": merged}
            out.append(one)
        return out

    out = {**page_dict, "cells": pinned(page_dict.get("cells") or [])}
    canvas = page_dict.get("canvas")
    if isinstance(canvas, dict):
        out["canvas"] = {**canvas, "els": pinned(canvas.get("els") or [])}
    return out
