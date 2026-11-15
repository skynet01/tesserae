"""ha_dashboard, an existing Home Assistant dashboard captured as a frame.

The recurring ask is "show the dashboard I already built in Home Assistant
on the panel, without rebuilding it as Tesserae widgets". Two things stop
the ``webpage`` widget from doing that: Home Assistant sends
``X-Frame-Options: SAMEORIGIN``, so the composer's iframe is refused, and
the frontend signs in from tokens in ``localStorage``, so a bearer header
on the navigation does nothing.

This widget goes around both. The server opens the dashboard as a
top-level page in headless Chromium (no iframe, so the frame header never
applies), and an init script seeds ``localStorage`` with a session built
from the same long-lived token ``ha_core`` already holds, plus the
frontend's own "sidebar always hidden" preference. The app header and its
layout space are hidden inside the Lovelace shadow root before capture.
Headerless dashboards keep their first row, and the viewport stays at the
cell's exact size.

Timing is the constraint that shapes the rest. A widget ``fetch()`` runs
inside the composer's 6 s hydrate budget and a cold Chromium plus a
dashboard load does not fit, so the render runs on its own thread and the
result is cached per (path, size, zoom, theme). ``fetch()`` returns the
cached frame when it is younger than ``refresh_seconds``, otherwise it
starts a render and returns the previous frame while that runs. Only the
very first render for a cell is waited for, and only up to ``FIRST_WAIT_S``,
so a panel's first paint never stalls on it. A cold render rarely fits in
that window, so the first fetch for a cell size usually goes out as
``pending`` and the cell shows a placeholder. That result carries a
``next_change_at`` hint (#243) so the device path re-renders the page once
the frame has landed, instead of leaving the placeholder on glass until
the next Send. Renders are single-flight per key and serialised across keys, so a
page with three dashboard cells opens one browser at a time. The pooled
browser is not used on purpose: its worker is the thread rendering the
composer page this fetch is part of, so queuing on it would deadlock.

The token reaches Chromium only through the init script on the render
request; it is never placed in the data returned to the cell, in the
cache, or in a log line.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import threading
import time
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from flask import current_app

logger = logging.getLogger(__name__)

# How long a first-ever render for a cell may hold the fetch. Under the
# composer's 6 s per-widget budget with room for the return trip.
FIRST_WAIT_S = 5.0
# After a failed render, how long the cell shows the error before a fetch
# tries again. Stops an editor preview that polls every few seconds from
# relaunching Chromium against a dead host each time.
ERROR_RETRY_S = 60.0
# Playwright budget for one render: launch + navigation + ready wait fit
# comfortably; a dashboard that takes longer than this is not going to
# paint.
RENDER_TIMEOUT_MS = 25_000
# How far out a ``pending`` result declares its next change. A cold render
# (launch + navigation + ready wait + settle) lands well inside this, and the
# device path adds its own render margin before it polls again; a render
# that overruns simply declares once more on the re-render.
PENDING_RETRY_S = 30.0
READY_TIMEOUT_MS = 10_000
DEFAULT_PATH = "lovelace/0"
DEFAULT_REFRESH_S = 300
DEFAULT_SETTLE_S = 2.0
THREAD_NAME = "ha-dashboard-render"

# Polled after networkidle until the frontend reports its panel resolved
# and loaded. Lifted from Puppet's wait, which tracks the frontend's own
# ``_loading`` flags rather than any element that might restyle. A landing
# on the login page counts as ready too, so a bad token is reported at
# once rather than after the wait times out.
READY_JS = """() => {
  if (location.pathname.startsWith("/auth/")) return true;
  const haEl = document.querySelector("home-assistant");
  if (!haEl) return false;
  const mainEl = haEl.shadowRoot?.querySelector("home-assistant-main");
  if (!mainEl) return false;
  const resolver = mainEl.shadowRoot?.querySelector("partial-panel-resolver");
  if (!resolver || resolver._loading) return false;
  const panel = resolver.children[0];
  if (!panel) return false;
  return !("_loading" in panel) || !panel._loading;
}"""


class Params(NamedTuple):
    """Everything that changes what gets captured; doubles as the cache key.

    NamedTuple rather than dataclass so the module also loads standalone
    (the test suite executes it outside ``sys.modules``, where a dataclass
    with postponed annotations cannot resolve its own module)."""

    path: str
    w: int
    h: int
    zoom: float
    dark: bool
    kiosk: bool
    settle_s: float
    offset_x: int = 0
    offset_y: int = 0
    hide_header: bool = True
    ha_theme: str = ""
    language: str = ""


class Frame(NamedTuple):
    png: bytes
    rendered_at: float
    w: int
    h: int


class _Job:
    """One in-flight render, so concurrent fetches for the same key share it."""

    __slots__ = ("done",)

    def __init__(self) -> None:
        self.done = threading.Event()


_frames: dict[Params, Frame] = {}
_errors: dict[Params, tuple[float, str]] = {}
_jobs: dict[Params, _Job] = {}
_state_lock = threading.Lock()
# One Chromium at a time across every key: a page with several dashboard
# cells should not launch several browsers at once on a small host.
_render_lock = threading.Lock()


def _reset() -> None:
    """Test hook: forget every frame, error, and in-flight job."""
    with _state_lock:
        _frames.clear()
        _errors.clear()
        _jobs.clear()


def _core() -> Any:
    plugin = current_app.config["PLUGIN_REGISTRY"].get("ha_core")
    return plugin.server_module if plugin is not None else None


# -- option shaping ---------------------------------------------------------


def normalise_path(raw: Any) -> str:
    """Turn what the operator typed into a site-relative path.

    Accepts ``lovelace/0``, ``/lovelace/0``, a pasted full URL on the
    configured host (the host part is dropped), or blank for the default
    dashboard. A query string the operator included is kept."""
    text = str(raw or "").strip()
    if not text:
        return "/" + DEFAULT_PATH
    if text.startswith(("http://", "https://")):
        parts = urlsplit(text)
        text = parts.path or "/"
        if parts.query:
            text += "?" + parts.query
    if not text.startswith("/"):
        text = "/" + text
    return text


def _float_option(
    options: dict[str, Any], name: str, default: float, lo: float, hi: float
) -> float:
    try:
        value = float(options.get(name, default))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return min(max(value, lo), hi)


def _params(options: dict[str, Any], ctx: dict[str, Any]) -> Params:
    w = int(ctx.get("cell_w") or 0) or int(ctx.get("panel_w") or 0) or 800
    h = int(ctx.get("cell_h") or 0) or int(ctx.get("panel_h") or 0) or 480
    return Params(
        path=normalise_path(options.get("path")),
        w=max(w, 1),
        h=max(h, 1),
        zoom=_float_option(options, "zoom", 1.0, 0.5, 3.0),
        dark=options.get("dark_mode") is True,
        kiosk=options.get("kiosk", True) is not False,
        settle_s=_float_option(options, "settle_seconds", DEFAULT_SETTLE_S, 0.0, 12.0),
        offset_x=round(_float_option(options, "offset_x", 0, -4096, 4096)),
        offset_y=round(_float_option(options, "offset_y", 0, -4096, 4096)),
        hide_header=options.get("hide_header", True) is not False,
        ha_theme=str(options.get("ha_theme") or "").strip()[:200],
        language=str(options.get("language") or "").strip()[:35],
    )


def _refresh_s(options: dict[str, Any]) -> float:
    return _float_option(options, "refresh_seconds", DEFAULT_REFRESH_S, 30.0, 86400.0)


# -- the render -------------------------------------------------------------


def init_script(base_url: str, token: str, params: Params) -> str:
    """JavaScript the renderer runs before any page script.

    ``hassTokens`` is the shape the frontend's auth module reads back from
    ``localStorage``: a long-lived token as the access token, an expiry far
    enough out that it never tries to refresh, and ``clientId`` set to the
    site root, which is what the frontend compares against ``hassUrl``.
    ``dockedSidebar: always_hidden`` is the user preference that keeps the
    sidebar closed; ``selectedTheme`` pins light or dark so the capture does
    not depend on the browser's colour scheme.

    The zoom is applied on ``<body>`` once it exists, before capture."""
    root = base_url.rstrip("/") + "/"
    tokens = {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": 1800,
        "hassUrl": base_url.rstrip("/"),
        "clientId": root,
        "expires": 9999999999999,
        "refresh_token": "",
    }
    theme: dict[str, Any] = {"dark": params.dark}
    if params.ha_theme:
        theme["theme"] = params.ha_theme
    entries = {
        "hassTokens": json.dumps(tokens),
        "dockedSidebar": json.dumps("always_hidden"),
        "selectedTheme": json.dumps(theme),
    }
    if params.language:
        entries["selectedLanguage"] = json.dumps(params.language)
    return (
        "(() => {\n"
        f"  const entries = {json.dumps(entries)};\n"
        "  for (const [key, value] of Object.entries(entries)) {\n"
        "    try { localStorage.setItem(key, value); } catch (err) {}\n"
        "  }\n"
        f"  const zoom = {json.dumps(params.zoom)};\n"
        "  const applyZoom = () => { if (document.body) document.body.style.zoom = String(zoom); };\n"
        "  if (document.readyState === 'loading') {\n"
        "    document.addEventListener('DOMContentLoaded', applyZoom, { once: true });\n"
        "  } else { applyZoom(); }\n"
        "})();"
    )


def dashboard_url(base_url: str, params: Params) -> str:
    url = base_url.rstrip("/") + params.path
    if params.kiosk:
        url += "&kiosk" if "?" in url else "?kiosk"
    return url


def ready_script(params: Params) -> str:
    """Hide HA's own header without cropping dashboard content or resizing it."""
    preferences: dict[str, Any] = {"dark": params.dark}
    if params.ha_theme:
        preferences["theme"] = params.ha_theme
    header_style = (
        """
  const root = panel.shadowRoot?.querySelector("hui-root")?.shadowRoot;
  if (!root) return false;
  if (!root.querySelector("#tesserae-capture-style")) {
    const style = document.createElement("style");
    style.id = "tesserae-capture-style";
    style.textContent = `:host { --header-height: 0px !important; }
      .header { display: none !important; }
      #view { padding-top: 0 !important; }`;
    root.appendChild(style);
  }
"""
        if params.hide_header
        else ""
    )
    body = (
        'if ("_loading" in panel && panel._loading) return false;\n'
        f"  const preferences = {json.dumps(preferences)};\n"
        f"  const language = {json.dumps(params.language)};\n"
        + """
  // HA can load account preferences after localStorage. Pin capture-only
  // preferences on this browser instance without saving them to the account.
  if (!haEl.__tesseraePreferences && haEl.hass && haEl._applyTheme) {
    haEl.__tesseraePreferences = true;
    const applyTheme = haEl._applyTheme.bind(haEl);
    haEl._applyTheme = (dark) => {
      haEl._updateHass({selectedTheme: {...haEl.hass.selectedTheme, ...preferences}});
      return applyTheme(dark);
    };
    haEl._applyTheme(false);
    if (language && haEl._selectLanguage) {
      const selectLanguage = haEl._selectLanguage.bind(haEl);
      haEl._selectLanguage = () => selectLanguage(language, false);
      haEl._selectLanguage();
    }
  }
"""
        + header_style
        + "  return true;"
    )
    return READY_JS.replace('return !("_loading" in panel) || !panel._loading;', body)


def offset_frame(png: bytes, params: Params) -> bytes:
    """Translate the captured canvas in output pixels, clipping at the cell edges."""
    from PIL import Image

    if not params.offset_x and not params.offset_y:
        return png
    with Image.open(io.BytesIO(png)) as im:
        frame = Image.new("RGB", (params.w, params.h), "black" if params.dark else "white")
        frame.paste(im.convert("RGB"), (params.offset_x, params.offset_y))
        out = io.BytesIO()
        frame.save(out, format="PNG", optimize=True)
        return out.getvalue()


def _capture(request: Any, script: str) -> tuple[bytes, Any]:
    """Indirection so tests can swap the browser out."""
    from app.renderer import CaptureRequest, capture_composed

    return capture_composed(CaptureRequest(render=request, script=script))


def _timezone_id() -> str | None:
    try:
        from app.push import resolve_render_timezone_id

        return resolve_render_timezone_id(current_app.config["SETTINGS_STORE"])
    except Exception:
        return None


def render_frame(
    params: Params,
    *,
    base_url: str,
    token: str,
    verify_tls: bool = True,
    timezone_id: str | None = None,
) -> bytes:
    """Open the dashboard and capture it at the cell's size. Raises on a login
    redirect (bad token) or a renderer failure; callers decide what the
    cell shows."""
    from app.renderer import RenderRequest

    request = RenderRequest(
        url=dashboard_url(base_url, params),
        viewport_w=params.w,
        viewport_h=params.h,
        timeout_ms=RENDER_TIMEOUT_MS,
        max_attempts=1,
        is_composer=False,
        timezone_id=timezone_id,
        init_script=init_script(base_url, token, params),
        ready_js=ready_script(params),
        ready_timeout_ms=READY_TIMEOUT_MS,
        settle_ms=int(params.settle_s * 1000),
        ignore_https_errors=not verify_tls,
    )
    png, href = _capture(request, "location.href")
    landed = urlsplit(str(href or "")).path
    if landed.startswith("/auth/"):
        raise RuntimeError(
            "Home Assistant showed the login page instead of the dashboard; "
            "check the access token in Settings → Widgets → Home Assistant Core"
        )
    return offset_frame(png, params)


def _run_job(params: Params, job: _Job, render_kwargs: dict[str, Any]) -> None:
    try:
        with _render_lock:
            png = render_frame(params, **render_kwargs)
    except Exception as err:
        logger.warning("ha_dashboard render of %s failed: %s", params.path, err)
        with _state_lock:
            _errors[params] = (time.monotonic(), f"{type(err).__name__}: {err}")
    else:
        with _state_lock:
            _frames[params] = Frame(png=png, rendered_at=time.time(), w=params.w, h=params.h)
            _errors.pop(params, None)
    finally:
        with _state_lock:
            _jobs.pop(params, None)
        job.done.set()


def _start_render(params: Params, render_kwargs: dict[str, Any]) -> _Job:
    """Start a render for ``params`` unless one is already running; either
    way return the job to wait on."""
    with _state_lock:
        job = _jobs.get(params)
        if job is not None:
            return job
        job = _Job()
        _jobs[params] = job
    thread = threading.Thread(
        target=_run_job, args=(params, job, render_kwargs), name=THREAD_NAME, daemon=True
    )
    thread.start()
    return job


# -- widget entry point -----------------------------------------------------


def _shape(frame: Frame, params: Params, *, stale: bool) -> dict[str, Any]:
    with _state_lock:
        capturing = params in _jobs
        error = _errors.get(params)
    data = {
        "frame": "data:image/png;base64," + base64.b64encode(frame.png).decode("ascii"),
        "w": frame.w,
        "h": frame.h,
        "path": params.path,
        "rendered_at": frame.rendered_at,
        "stale": stale,
        "capturing": capturing,
    }
    if error is not None:
        data["capture_error"] = error[1]
    if capturing:
        data["live_refresh_s"] = 2
    return data


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings
    core = _core()
    if core is None:
        return {"error": "Install the Home Assistant Core plugin to use this widget."}
    if not core.is_configured():
        return {
            "error": "Set the Home Assistant URL and token in Settings → Widgets → Home Assistant Core."
        }

    params = _params(options, ctx)
    refresh_s = _refresh_s(options)
    now = time.time()

    with _state_lock:
        frame = _frames.get(params)
        error = _errors.get(params)
    fresh = frame is not None and (now - frame.rendered_at) < refresh_s and not ctx.get("fresh")
    if fresh and frame is not None:
        return _shape(frame, params, stale=False)

    if error is not None and not ctx.get("fresh"):
        age = time.monotonic() - error[0]
        if age < ERROR_RETRY_S:
            if frame is not None:
                return _shape(frame, params, stale=True)
            return {"error": error[1], "path": params.path}

    render_kwargs = {
        "base_url": core.base_url(),
        "token": core.token(),
        "verify_tls": _verify_tls(core),
        "timezone_id": _timezone_id(),
    }
    job = _start_render(params, render_kwargs)
    if frame is not None and not ctx.get("fresh"):
        # Stale-while-revalidate: the previous frame goes out now, the one
        # rendering lands for the next refresh.
        return _shape(frame, params, stale=True)

    job.done.wait(FIRST_WAIT_S)
    with _state_lock:
        frame = _frames.get(params)
        error = _errors.get(params)
    if frame is not None:
        return _shape(frame, params, stale=not job.done.is_set())
    if error is not None:
        return {"error": error[1], "path": params.path}
    return {
        "pending": True,
        "live_refresh_s": 2,
        "path": params.path,
        "next_change_at": time.time() + PENDING_RETRY_S,
    }


def _verify_tls(core: Any) -> bool:
    checker = getattr(core, "_verify_tls", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return True
    return True
