import os
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from app.main import create_app
from plugins.news_front_pages import server


@pytest.mark.parametrize("mode", ["display", "none", "floyd-steinberg", "atkinson"])
def test_dithering_round_trip(tmp_path, mode):
    saved = server.save_config(tmp_path, {"dithering": mode, "clean_white": "strong"})
    assert saved["dithering"] == mode
    assert server.load_config(tmp_path)["dithering"] == mode
    assert saved["clean_white"] == "strong"


def test_dithering_defaults_preserve_display_processing(tmp_path):
    assert server.load_config(tmp_path)["dithering"] == "display"
    assert server.save_config(tmp_path, {"dithering": "broken"})["dithering"] == "display"


@pytest.mark.parametrize("mode", ["display", "none", "floyd-steinberg", "atkinson"])
def test_dithering_survives_form_save_and_invalid_rows(app, mode):
    client = app.test_client()
    data = {
        "count": "1",
        "row-0-code": "NY_NYT",
        "row-0-enabled": "on",
        "op": "save",
        "dithering": mode,
        "clean_white": "strong",
    }
    assert client.post("/plugins/news_front_pages/papers", data=data).status_code == 302
    plugin = app.config["PLUGIN_REGISTRY"].get("news_front_pages")
    assert plugin.server_module.load_config(Path(plugin.data_dir))["dithering"] == mode
    data["row-0-code"] = "!bad"
    response = client.post("/plugins/news_front_pages/papers", data=data)
    assert response.status_code == 400
    assert f'value="{mode}" selected'.encode() in response.data


@pytest.fixture
def newspaper_live(tmp_path):
    app = create_app(
        testing=True,
        data_root=tmp_path,
        plugins_dir=Path(os.environ["TESSERAE_TEST_PLUGINS"])
        if "TESSERAE_TEST_PLUGINS" in os.environ
        else None,
    )
    app.config["SETTINGS_STORE"].patch_section("app", {"onboarded": True})
    app.config["SETTINGS_STORE"].patch_section("auth", {"disabled": True})
    live = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=live.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{live.server_port}", app
    live.shutdown()
    thread.join(timeout=10)


@pytest.mark.parametrize(
    "design,width", [("paper", 1440), ("classic", 1440), ("paper", 390), ("classic", 390)]
)
def test_newspaper_previews_fetches_and_responsiveness(newspaper_live, design, width):
    from playwright.sync_api import sync_playwright

    base, app = newspaper_live
    app.config["SETTINGS_STORE"].patch_section("app", {"ui_design": design})
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        page = browser.new_page(viewport={"width": width, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/plugins/news_front_pages/", wait_until="networkidle")
        page.wait_for_function(
            "document.querySelectorAll('[data-nx-preview][data-ready]').length === 4"
        )
        assert page.locator("#fetches").evaluate("el => el.open") is False
        page.locator("#fetches > summary").focus()
        page.keyboard.press("Enter")
        page.reload(wait_until="networkidle")
        assert page.locator("#fetches").evaluate("el => el.open") is True
        page.locator("#fetches > summary").click()
        for name in ("text_blackness", "text_sharpness", "clean_white", "dithering"):
            canvas = page.locator(f'[data-nx-preview="{name}"] canvas').last
            before = canvas.evaluate("el => el.toDataURL()")
            field = page.locator(f'select[name="{name}"]')
            field.select_option("none" if name == "dithering" else "strong")
            page.wait_for_function(
                "(args) => document.querySelector(args[0]).toDataURL() !== args[1]",
                timeout=5000,
                arg=[f'[data-nx-preview="{name}"] > div:last-child canvas', before],
            )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.locator("[data-nx-preview] canvas[aria-label]").count() == 8
        page.locator("[data-nx-discard]").click()
        assert page.locator('select[name="dithering"]').input_value() == "display"
        assert errors == []
        browser.close()


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("display", [8, 8, 8, 8]),
        ("none", [0, 0, 0, 0]),
        ("floyd-steinberg", [0, 17, 0, 17]),
        ("atkinson", [0, 17, 0, 0]),
    ],
)
def test_dithering_pixel_results(newspaper_live, mode, expected):
    from playwright.sync_api import sync_playwright

    base, _ = newspaper_live
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        page = browser.new_page()
        page.goto(base + "/plugins/news_front_pages/")
        actual = page.evaluate(
            """async mode => {
            const { processCanvas } = await import('/plugins/news_front_pages/client.js');
            const canvas = document.createElement('canvas');
            canvas.width = 4; canvas.height = 1;
            const ctx = canvas.getContext('2d');
            ctx.putImageData(new ImageData(new Uint8ClampedArray([8,8,8,255, 8,8,8,255, 8,8,8,255, 8,8,8,255]), 4, 1), 0, 0);
            processCanvas(canvas, null, null, mode);
            const px = ctx.getImageData(0,0,4,1).data;
            return Array.from(px).filter((_, index) => index % 4 === 0);
        }""",
            mode,
        )
        assert actual == expected
        browser.close()


def test_fetches_toggles_when_storage_is_blocked(newspaper_live):
    from playwright.sync_api import sync_playwright

    base, _ = newspaper_live
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        )
        page = browser.new_page()
        page.add_init_script(
            "Object.defineProperty(window, 'localStorage', {get() { throw new Error('blocked'); }});"
        )
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/plugins/news_front_pages/", wait_until="networkidle")
        assert page.locator("#fetches").evaluate("el => el.open") is False
        page.locator("#fetches > summary").click()
        assert page.locator("#fetches").evaluate("el => el.open") is True
        page.reload(wait_until="networkidle")
        assert page.locator("#fetches").evaluate("el => el.open") is False
        assert errors == []
        browser.close()
