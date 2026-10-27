"""News widget settings stay with their authenticated admin pages."""

import pytest

from app.state.settings_store import SECRET_MASK


@pytest.mark.parametrize(
    "pid,field", [("news_front_pages", "papers"), ("news_illustrated", "sources")]
)
def test_news_settings_live_on_the_admin_page(app, client, pid, field):
    page = client.get(f"/plugins/{pid}/")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert f'action="/settings/plugin-{pid}"' in body
    assert f'name="{field}"' in body
    # Both admin pages are reachable from Widgets, without a duplicate form.
    general = client.get("/settings/plugins").get_data(as_text=True)
    assert f'href="/plugins/{pid}/"' in general
    assert f'id="plugin-{pid}"' not in general


def test_ai_settings_save_returns_to_admin_and_preserves_a_masked_key(app, client):
    plugin = app.config["PLUGIN_REGISTRY"].get("news_illustrated")
    fields = plugin.manifest["settings"]
    store = app.config["SETTINGS_STORE"]
    store.update_for_namespace("plugins", plugin.id, {"groq_api_key": "private-test-key"}, fields)
    page = client.get("/plugins/news_illustrated/").get_data(as_text=True)
    assert "private-test-key" not in page
    assert 'name="groq_api_key"' in page
    result = client.post(
        "/settings/plugin-news_illustrated",
        data={
            "sources": "Local | https://example.test/rss",
            "groq_api_key": SECRET_MASK,
            "run_at": "07:15",
        },
    )
    assert result.location == "/plugins/news_illustrated/#plugin-news_illustrated"
    saved = store.get_for_runtime("plugins", plugin.id, fields)
    assert saved["groq_api_key"] == "private-test-key"
    assert saved["sources"] == "Local | https://example.test/rss"
    assert saved["run_at"] == "07:15"


def test_news_admin_is_not_a_render_safe_endpoint(app, client):
    app.config["TESTING"] = False
    store = app.config["SETTINGS_STORE"]
    store.patch_section("app", {"onboarded": True})
    store.patch_section("auth", {"disabled": False})
    from app.auth import install_gate, set_password

    set_password(store, "test-admin-password")
    install_gate(app, store)
    for pid in ("news_front_pages", "news_illustrated"):
        response = client.get(f"/plugins/{pid}/")
        assert response.status_code == 302
        assert "/login" in response.location


def test_news_settings_save_and_form_action_preserve_a_mount_prefix(client):
    env = {"SCRIPT_NAME": "/tesserae"}
    page = client.get("/plugins/news_front_pages/", environ_overrides=env)
    assert 'action="/tesserae/settings/plugin-news_front_pages"' in page.get_data(as_text=True)
    result = client.post(
        "/settings/plugin-news_front_pages", data={"papers": "[]"}, environ_overrides=env
    )
    assert result.location == "/tesserae/plugins/news_front_pages/#plugin-news_front_pages"


def test_ai_news_page_reads_stories_run_then_settings(app, client):
    """AI news: today's stories, the last run, then the settings as one form
    over four cards (open, not folded), saved by one bar."""
    plugin = app.config["PLUGIN_REGISTRY"].get("news_illustrated")
    (plugin.data_dir / "state.json").write_text(
        '{"last_run": {"reason": "manual", "started": 1, "finished": 2, "log": ["Run started"]}}',
        encoding="utf-8",
    )
    body = client.get("/plugins/news_illustrated/").get_data(as_text=True)
    order = [body.index(f'id="{i}"') for i in ("stories", "last-run", "plugin-news_illustrated")]
    assert order == sorted(order)
    form = body[body.index('id="plugin-news_illustrated"') :]
    form = form[: form.index("</form>")]
    for card in ("ain-set-stories", "ain-set-summaries", "ain-set-pictures", "ain-set-frame"):
        assert f'id="{card}"' in form
    assert form.count("data-save-bar") == 1 and "data-fold" not in form
    assert "data-log-tail" in body[body.index('id="last-run"') :]


def test_an_admin_page_is_titled_like_its_widget_unless_it_says_otherwise(app):
    registry = app.config["PLUGIN_REGISTRY"]
    assert registry.get("news_illustrated").admin_title == "AI Illustrated News"
    assert registry.get("news_front_pages").admin_title == "Newspapers"


def test_the_admin_title_is_escaped_in_the_menu(app, client):
    plugin = app.config["PLUGIN_REGISTRY"].get("news_front_pages")
    plugin.manifest["admin_title"] = "<b>Papers</b> & co"
    try:
        body = client.get("/plugins/news_illustrated/").get_data(as_text=True)
    finally:
        plugin.manifest["admin_title"] = "Newspapers"
    assert "&lt;b&gt;Papers&lt;/b&gt; &amp; co" in body
    assert "<b>Papers</b>" not in body
