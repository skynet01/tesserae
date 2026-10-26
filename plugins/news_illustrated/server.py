"""news_illustrated: a daily news story per feed, summarised by Groq and
illustrated as a satirical ink cartoon, shown one story at a time.

A port of the Visionect "ainews" module. The work is split so a render never
waits on it:

* ``fetch()`` reads the stories already on disk, picks the one this placement
  shows next, and on a settled render (a push, a warm-ahead, a scheduled
  refresh) starts the day's run in the background when it is due.
* ``pipeline`` makes the stories (feed, article, summary, illustration) on a
  daemon thread, reporting to the job tray.
* The widget's page (``/plugins/news_illustrated/``) shows the stories and the
  last run, and has Generate now and Stop.

The helper modules beside this file are imported relatively: the plugin
folder is registered as a package under the name the host gave this module,
since the host loads ``server.py`` by path (docs/widgets.md, "Helper modules
beside server.py").
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import time
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    send_file,
    url_for,
)
from werkzeug.wrappers import Response

_HERE = Path(__file__).resolve().parent


def _register_package() -> None:
    """Make ``from . import x`` work when the host loaded this file by path as
    ``_tesserae_plugins.<id>.server``: the plugin folder becomes that package,
    under an empty ``_tesserae_plugins`` (the import system wants every
    ancestor). Helpers cached from an earlier load are dropped so an in-place
    reload picks up their new code too. Under the test suite the folder is
    already a real package and nothing is done."""
    package = __name__.rpartition(".")[0]
    root = package.partition(".")[0]
    if root != "_tesserae_plugins":
        return
    for name in [m for m in sys.modules if m.startswith(package + ".") and m != __name__]:
        del sys.modules[name]
    for name, search in ((root, []), (package, [str(_HERE)])):
        if name == root and name in sys.modules:
            continue
        spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
        spec.submodule_search_locations = search
        sys.modules[name] = importlib.util.module_from_spec(spec)


_register_package()

from . import pipeline, store  # noqa: E402

PLUGIN_ID = pipeline.PLUGIN_ID
ADMIN_SETTINGS = True
# How often editors and the push follow-ups look again while a run is going.
LIVE_REFRESH_S = 15

RENDER_SAFE_ENDPOINTS: tuple[str, ...] = ("news_illustrated_admin.image",)


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    """The story this placement shows now. Never makes stories itself."""
    del options
    data_dir = Path(ctx["data_dir"])
    cfg = pipeline.Config.from_settings(settings)
    if not cfg.sources:
        return {"error": "Add a feed under Sources in this widget's settings."}
    preview = bool(ctx.get("preview"))
    settled = bool(ctx.get("settled", not preview))
    tz = _timezone()
    if settled and not pipeline.RUNNER.running(data_dir):
        reason = pipeline.due(data_dir, cfg.run_at, datetime.now(tz))
        if reason:
            pipeline.RUNNER.start(cfg, data_dir, tz=tz, reason=reason)

    out: dict[str, Any] = {"run_at": cfg.run_at, "running": pipeline.RUNNER.running(data_dir)}
    if out["running"]:
        out["live_refresh_s"] = LIVE_REFRESH_S
        out["live_pending"] = pipeline.RUNNER.pending(data_dir)
    if not cfg.painters_ready:
        out["notice"] = "No image provider has a key yet, so stories are text only."

    stories = store.load_stories(data_dir)
    if not stories:
        return out
    placement = str(ctx.get("cell_id") or "default")
    index = store.pick(data_dir, placement, len(stories), advance=not preview, now=time.time())
    story = stories[index]
    image = str(story.get("image") or "")
    out.update(
        {
            "title": str(story.get("title") or ""),
            "summary": str(story.get("summary") or ""),
            "source": str(story.get("source") or ""),
            "date": str(story.get("date") or ""),
            "url": str(story.get("url") or ""),
            "image": (
                f"/plugins/{PLUGIN_ID}/image/{image}"
                if image and store.image_path(data_dir, image)
                else ""
            ),
            "index": index + 1,
            "count": len(stories),
        }
    )
    return out


def blueprint() -> Blueprint:
    """``/plugins/news_illustrated/``: the stories, the last run, Generate now."""
    bp = Blueprint("news_illustrated_admin", __name__, template_folder="templates")

    @bp.get("/")
    def index() -> str:
        from app.plugin_admin import settings_section

        data_dir = _data_dir()
        cfg = pipeline.Config.from_settings(_settings())
        tz = _timezone()
        state = store.load_state(data_dir)
        current = pipeline.edition(datetime.now(tz), cfg.run_at)
        last = state.get("last_run") if isinstance(state.get("last_run"), dict) else {}
        return render_template(
            "news_illustrated/index.html",
            stories=store.load_stories(data_dir),
            cfg=cfg,
            running=pipeline.RUNNER.running(data_dir),
            edition_done=state.get("edition") == current,
            last=last,
            when=lambda ts: datetime.fromtimestamp(int(ts or 0), tz).strftime("%Y-%m-%d %H:%M"),
            section=settings_section(
                current_app.config["PLUGIN_REGISTRY"].get(PLUGIN_ID),
                title="Settings",
                blurb="Feeds, the daily run time and provider keys. Saving doesn't start a run.",
                icon="sliders-horizontal",
                fold="closed",
            ),
        )

    @bp.get("/image/<name>")
    def image(name: str) -> Response:
        path = store.image_path(_data_dir(), name)
        if path is None:
            abort(404)
        response = send_file(path, mimetype="image/jpeg", conditional=True)
        # The name carries a hash of the picture, so it never changes under a URL.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @bp.post("/run")
    def run_now() -> Response:
        cfg = pipeline.Config.from_settings(_settings())
        if not cfg.sources:
            flash("Add a feed under Sources in this widget's settings first.", "warn")
        elif pipeline.RUNNER.start(cfg, _data_dir(), tz=_timezone(), reason="manual", force=True):
            flash("Making new stories now; the job tray shows how far it got.", "ok")
        else:
            flash("A run is already going.", "warn")
        return redirect(url_for("news_illustrated_admin.index"))

    @bp.post("/stop")
    def stop() -> Response:
        if pipeline.RUNNER.stop(_data_dir()):
            flash("Stopping after the step under way.", "ok")
        else:
            flash("No run is going in this server.", "warn")
        return redirect(url_for("news_illustrated_admin.index"))

    return bp


def _data_dir() -> Path:
    plugin = current_app.config["PLUGIN_REGISTRY"].get(PLUGIN_ID)
    if plugin is None:
        abort(404)
    path: Path = plugin.data_dir
    return path


def _settings() -> dict[str, Any]:
    """This widget's settings with secrets readable, as ``fetch()`` gets them."""
    settings_store = current_app.config.get("SETTINGS_STORE")
    plugin = current_app.config["PLUGIN_REGISTRY"].get(PLUGIN_ID)
    if settings_store is None or plugin is None:
        return {}
    fields = plugin.manifest.get("settings", [])
    got: dict[str, Any] = settings_store.get_for_runtime("plugins", PLUGIN_ID, fields)
    return got


def _timezone() -> tzinfo | None:
    """The app's time zone (Settings, else the host's); None is the host's local time."""
    try:
        from app.tz_resolve import app_timezone

        return app_timezone()
    except Exception:
        return None
