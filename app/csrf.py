"""Cookie request provenance without adding tokens to every admin form.

Cookie-free device/API requests keep their own authentication. Legacy clients
with no Fetch Metadata, Origin or Referer are allowed; an explicitly opaque,
invalid or cross-origin provenance is refused. ProxyFix and public_url have
already resolved the browser-facing origin before this hook runs.
"""

from __future__ import annotations

import logging
from threading import Lock
from urllib.parse import urlsplit

from flask import Flask, request
from werkzeug.wrappers import Response

logger = logging.getLogger(__name__)

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _origin(url: str) -> tuple[str, str, int] | None:
    """Canonical HTTP origin, including default ports; invalid/opaque is None."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        port = parts.port
        default_port = 443 if parts.scheme == "https" else 80
        return parts.scheme, parts.hostname, port if port is not None else default_port
    except ValueError:
        return None


def install_protection(app: Flask) -> None:
    """Check unsafe cookie requests before auth or route handlers can mutate."""
    logged_routes: set[str] = set()
    log_lock = Lock()

    @app.before_request
    def _check_provenance() -> Response | None:
        if request.method not in _UNSAFE_METHODS:
            return None
        cookie_name = app.config["SESSION_COOKIE_NAME"]
        if cookie_name not in request.cookies:
            return None

        fetch_site = request.headers.get("Sec-Fetch-Site")
        if fetch_site is not None:
            allowed = fetch_site in {"same-origin", "none"}
        else:
            source = request.headers.get("Origin")
            if source is None:
                source = request.headers.get("Referer")
            # Preserve header-less legacy browsers and scripted session clients.
            source_origin = _origin(source) if source is not None else None
            allowed = source is None or (
                source_origin is not None and source_origin == _origin(request.host_url)
            )
        if allowed:
            return None

        # Route patterns bound cardinality and avoid logging tokens, referrers,
        # ingress prefixes or user-provided path segments. Once per app process.
        route = request.url_rule.rule if request.url_rule is not None else "<unmatched>"
        with log_lock:
            if route not in logged_routes:
                logged_routes.add(route)
                logger.warning("CSRF: blocked cross-origin session request to %s", route)
        return Response(
            "Forbidden: cross-origin session request blocked (CSRF).",
            status=403,
            content_type="text/plain; charset=utf-8",
        )
