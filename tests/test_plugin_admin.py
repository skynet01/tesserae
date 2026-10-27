"""A widget that sets ``ADMIN_SETTINGS`` keeps its settings card on its own
admin page (app/plugin_admin.py) instead of Settings > Widgets, and a save
from there returns to that page."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app.main import create_app

SERVER_PY = '''
from flask import Blueprint, current_app, render_template_string

ADMIN_SETTINGS = True

PAGE = """{% extends "_base.html" %}
{% block main %}{% include "_plugin_admin_settings.html" %}{% endblock %}"""


def blueprint():
    bp = Blueprint("demo_admin", __name__)

    @bp.get("/")
    def index():
        from app.plugin_admin import settings_section

        plugin = current_app.config["PLUGIN_REGISTRY"].get("demo_admin")
        return render_template_string(PAGE, section=settings_section(plugin, title="Feeds"))

    return bp
'''


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    folder = tmp_path / "plugins" / "demo_admin"
    folder.mkdir(parents=True)
    manifest = {
        "tesserae_compat": "1.x",
        "name": "Demo Admin",
        "admin_title": "Demo <Feeds>",
        "version": "0.0.1",
        "kind": "widget",
        "supports": {"sizes": ["sm"]},
        "settings": [{"name": "feed", "type": "string", "label": "Feed", "default": ""}],
    }
    (folder / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "client.js").write_text("export default function () {}\n", encoding="utf-8")
    (folder / "server.py").write_text(SERVER_PY, encoding="utf-8")
    return create_app(testing=True, data_root=tmp_path / "data", plugins_dir=tmp_path / "plugins")


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def test_the_settings_card_lives_on_the_admin_page_only(client: FlaskClient) -> None:
    page = client.get("/plugins/demo_admin/").get_data(as_text=True)
    assert 'action="/settings/plugin-demo_admin"' in page
    assert 'name="feed"' in page
    widgets = client.get("/settings/plugins").get_data(as_text=True)
    assert 'href="/plugins/demo_admin/"' in widgets
    assert 'id="plugin-demo_admin"' not in widgets


def test_a_save_returns_to_the_admin_page(app: Flask, client: FlaskClient) -> None:
    result = client.post("/settings/plugin-demo_admin", data={"feed": "https://example.test/rss"})
    assert result.location == "/plugins/demo_admin/#plugin-demo_admin"
    plugin = app.config["PLUGIN_REGISTRY"].get("demo_admin")
    stored = app.config["SETTINGS_STORE"].get_for_runtime(
        "plugins", plugin.id, plugin.manifest["settings"]
    )
    assert stored["feed"] == "https://example.test/rss"


def test_the_menu_uses_the_escaped_admin_title(client: FlaskClient) -> None:
    body = client.get("/plugins/demo_admin/").get_data(as_text=True)
    assert "Demo &lt;Feeds&gt;" in body
    assert "Demo <Feeds>" not in body
