"""Admin request provenance without adding tokens to every form.

Cookie-free device/API requests keep their own authentication. Legacy clients
with no Fetch Metadata, Origin or Referer are allowed; an explicitly opaque,
invalid or cross-origin provenance is refused. ProxyFix resolves the actual
request origin; public_url is an additional permitted origin.
"""

from __future__ import annotations

import logging
from threading import Lock
from urllib.parse import urlsplit

from flask import Flask, request
from werkzeug.wrappers import Response

logger = logging.getLogger(__name__)

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# These blueprints authenticate independently of browser sessions (tokens or
# pairing codes). Only cookie-free calls bypass provenance checks.
# Never exempt /api/* by path: some API routes
# are admin/session actions, including REST device pairing-code issuance.
_COOKIE_FREE_BLUEPRINTS = frozenset({"webhook", "companion_api", "trmnl_api", "bloomin8_api"})
_COOKIE_FREE_ENDPOINTS = frozenset(
    {
        "rest_api.post_tap",
        "rest_api.post_status",
        "rest_api.post_interact",
        "rest_api.post_log",
        "rest_api.post_discover",
        "rest_api.post_register",
    }
)


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
    """Check unsafe admin requests before auth or route handlers can mutate."""
    logged_routes: set[str] = set()
    log_lock = Lock()

    @app.before_request
    def _check_provenance() -> Response | None:
        if request.method not in _UNSAFE_METHODS:
            return None
        cookie_name = app.config["SESSION_COOKIE_NAME"]
        if cookie_name not in request.cookies:
            if (
                request.blueprint in _COOKIE_FREE_BLUEPRINTS
                or request.endpoint in _COOKIE_FREE_ENDPOINTS
            ):
                return None
            if request.blueprint == "mcp_api":
                # Loopback identifies the connection, not the initiating page.
                # Only an explicit valid credential exempts browser provenance.
                from app.mcp_api import token_is_authorised

                if token_is_authorised():
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
            actual_origin = request.environ.get("tesserae.request_origin", request.host_url)
            allowed = source is None or (
                source_origin is not None
                and source_origin in {_origin(actual_origin), _origin(request.host_url)}
            )
        if allowed:
            return None

        # Route patterns bound cardinality and avoid logging tokens, referrers,
        # ingress prefixes or user-provided path segments. Once per app process.
        route = request.url_rule.rule if request.url_rule is not None else "<unmatched>"
        with log_lock:
            if route not in logged_routes:
                logged_routes.add(route)
                logger.warning("CSRF: blocked cross-origin admin request to %s", route)
        return Response(
            "Forbidden: cross-origin admin request blocked (CSRF).",
            status=403,
            content_type="text/plain; charset=utf-8",
        )
