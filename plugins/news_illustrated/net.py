"""One HTTP call shape for every upstream this widget talks to.

Feeds, article pages, Groq and four image providers all want the same
thing: a request with a hard timeout that never raises, a status code (0
when nothing answered), the body capped at a size, and a short reason on
failure. Callers branch on that instead of on urllib's exception zoo, and
the tests replace :func:`request` alone to keep every byte off the network.

Nothing here logs: URLs can carry a key in their query (Pollinations), so
callers log what they know is safe.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.plugin_http import decode_content_encoding

# A news site or an image CDN answering with something bigger than this is
# not sending what we asked for.
MAX_BYTES = 25 * 1024 * 1024
API_USER_AGENT = "tesserae/0.1 (+news_illustrated)"
# Article pages and some feeds sit behind bot filters that refuse a
# bot-shaped User-Agent outright (the PHP original learned this the hard way).
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Reply:
    """What came back. ``status`` is 0 when no HTTP answer arrived."""

    status: int
    body: bytes = b""
    content_type: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.error

    def json(self) -> Any:
        """The body as JSON, or None when it is not JSON."""
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None

    def text(self) -> str:
        """The body as text, in the charset the server named (UTF-8 otherwise)."""
        charset = "utf-8"
        for part in self.content_type.split(";")[1:]:
            name, _, value = part.strip().partition("=")
            if name.lower() == "charset" and value:
                charset = value.strip("\"' ")
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def reason(self) -> str:
        """A short, key-free sentence for the run log."""
        if self.error:
            return self.error
        return f"HTTP {self.status}"


def request(
    url: str,
    *,
    timeout: float,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    max_bytes: int = MAX_BYTES,
) -> Reply:
    """Make one request; never raises. An HTTP error status still carries its
    body (truncated), since the APIs explain themselves there."""
    sent = {"User-Agent": API_USER_AGENT, "Accept-Encoding": "identity", **(headers or {})}
    req = urllib.request.Request(url, data=body, headers=sent, method=method)
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as resp:
            raw = resp.read(max_bytes + 1)
            if len(raw) > max_bytes:
                return Reply(int(resp.status), b"", "", "response too large")
            encoding = str(resp.headers.get("Content-Encoding", "") or "")
            return Reply(
                int(resp.status),
                decode_content_encoding(raw, encoding),
                str(resp.headers.get("Content-Type", "") or ""),
            )
    except urllib.error.HTTPError as err:
        try:
            raw = err.read(4096)
        except Exception:
            raw = b""
        finally:
            err.close()
        err_headers = getattr(err, "headers", None)
        content_type = str(err_headers.get("Content-Type", "") or "") if err_headers else ""
        return Reply(int(err.code), raw, content_type)
    except urllib.error.URLError as err:
        return Reply(0, error=f"could not connect ({_short(err.reason)})")
    except TimeoutError:
        return Reply(0, error="timed out")
    except (OSError, ValueError) as err:
        return Reply(0, error=f"{type(err).__name__}: {_short(err)}")


def post_json(
    url: str, payload: dict[str, Any], *, timeout: float, headers: dict[str, str] | None = None
) -> Reply:
    """POST ``payload`` as JSON."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sent = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
    return request(url, timeout=timeout, method="POST", headers=sent, body=data)


def _short(value: object) -> str:
    return " ".join(str(value).split())[:160]
