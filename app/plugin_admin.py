"""Shared settings form for widgets that keep settings on their admin page."""

from typing import Any

from flask import url_for

from app import repeater
from app.plugin_loader import Plugin


def settings_in_admin(plugin: Plugin) -> bool:
    return plugin.has_admin and getattr(plugin.server_module, "ADMIN_SETTINGS", False) is True


def settings_section(plugin: Plugin) -> dict[str, Any]:
    # Import at use time: settings routes also use this view builder.
    from app.settings._shared import mark_unreadable_secrets, render_for_admin

    fields = mark_unreadable_secrets(
        list(plugin.manifest.get("settings", [])), "plugins", plugin.id
    )
    sid = f"plugin-{plugin.id}"
    return {
        "id": sid,
        "kind": "plugin",
        "title": f"Plugin: {plugin.name}",
        "blurb": plugin.manifest.get("description") or "",
        "fields": fields,
        "state": repeater.prepare_state(
            fields,
            render_for_admin("plugins", plugin.id, fields),
            getattr(plugin.server_module, "legacy_rows", None),
        ),
        "endpoint": url_for("auth.settings_update", section_kind=sid),
    }
