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

``comics_daily`` shows whichever strips fit and takes turns by a per-placement
cursor with no way to name a turn, and the other widgets show one thing; they
report one item, so the Lineups page draws no arrows for them.

``pin_item`` returns the page definition with the chosen item written into
that cell's options. The composer applies it for ``/compose/<page>?item=``
(a preview or a push render); nothing is saved to the page.
"""

from __future__ import annotations

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


#: plugin id -> reader of that cell's items. A widget joins by being pinnable
#: through one cell option and listing what it would walk.
_PROVIDERS: dict[str, Callable[[Any], StepItems | None]] = {
    "picture_gallery": _gallery_items,
    "news_front_pages": _news_items,
}


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
    return (found.count, found.items) if found is not None else (1, ())


def find_item(page: Any, key: str) -> ItemInfo | None:
    """The listed item with this key, or None (unknown, or not choosable)."""
    found = step_choices(page)
    if found is None or not key:
        return None
    return next((it for it in found.items if it.key == key), None)


def pin_item(page: Any, page_dict: dict[str, Any], key: str) -> dict[str, Any] | None:
    """``page_dict`` with the widget's pin option set to ``key``, or None when
    ``key`` is not one of the page's items."""
    found = step_choices(page)
    if found is None or not any(it.key == key for it in found.items):
        return None

    def pinned(placements: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {**one, "options": {**(one.get("options") or {}), found.option: key}}
            if str(one.get("id")) == found.cell_id
            else one
            for one in placements
        ]

    out = {**page_dict, "cells": pinned(page_dict.get("cells") or [])}
    canvas = page_dict.get("canvas")
    if isinstance(canvas, dict):
        out["canvas"] = {**canvas, "els": pinned(canvas.get("els") or [])}
    return out
