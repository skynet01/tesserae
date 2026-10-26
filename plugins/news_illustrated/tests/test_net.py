"""The HTTP helper's guards: public addresses only (redirects too), a total
time limit, a decoded-size limit, and no secret-bearing error text. Nothing
here opens a connection."""

from __future__ import annotations

import gzip
import time
import urllib.request
import zlib
from typing import Any

import pytest

from app.net_guard import BlockedURLError
from plugins.news_illustrated import net


class _Resp:
    """A stand-in for an http.client response."""

    def __init__(self, chunks: list[bytes], headers: dict[str, str], delay: float = 0.0) -> None:
        self._chunks = list(chunks)
        self.headers = headers
        self.status = 200
        self._delay = delay

    def read1(self, n: int) -> bytes:
        time.sleep(self._delay)
        return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        return None

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        return None


def _serve(monkeypatch: pytest.MonkeyPatch, answer: Any) -> None:
    """Every request is answered by ``answer`` (a response or an exception),
    past a guard that lets example.com through without DNS."""

    class _Opener:
        def open(self, req: urllib.request.Request, timeout: float) -> Any:
            if isinstance(answer, BaseException):
                raise answer
            return answer

    monkeypatch.setattr(net, "assert_public_url", lambda url: None)
    monkeypatch.setattr(net.urllib.request, "build_opener", lambda *h: _Opener())


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765/api",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/",
        "file:///etc/passwd",
    ],
)
def test_non_public_addresses_are_refused(url: str) -> None:
    reply = net.request(url, timeout=5)
    assert reply.status == 0 and reply.error.startswith("refused")


def test_redirects_to_private_addresses_are_refused() -> None:
    handler = net._PublicRedirects()
    req = urllib.request.Request("https://news.example.com/a")
    with pytest.raises(BlockedURLError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://169.254.169.254/latest")


def test_a_refused_redirect_is_a_failed_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, BlockedURLError("private"))
    assert net.request("https://news.example.com/a", timeout=5).error == (
        "refused: redirected to a non-public address"
    )


def test_a_gzip_bomb_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    bomb = gzip.compress(b"\0" * 1_000_000)
    assert len(bomb) < 2048
    _serve(monkeypatch, _Resp([bomb], {"Content-Encoding": "gzip"}))
    reply = net.request("https://news.example.com/a", timeout=5, max_bytes=2048)
    assert reply.error == "response too large"
    with pytest.raises(net._TooLarge):
        net.decode(zlib.compress(b"\0" * 10_000), "deflate", 100)
    assert net.decode(gzip.compress(b"hello"), "gzip", 100) == b"hello"
    raw_deflate = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    body = raw_deflate.compress(b"raw") + raw_deflate.flush()
    assert net.decode(body, "deflate", 100) == b"raw"


def test_a_slow_drip_body_cannot_outlast_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _Resp([b"x"] * 1000, {}, delay=0.01))
    started = time.monotonic()
    reply = net.request("https://news.example.com/a", timeout=0.2)
    assert reply.error == "timed out"
    assert time.monotonic() - started < 1.0


def test_no_time_left_sends_nothing() -> None:
    assert net.request("https://news.example.com/a", timeout=0).error == "no time left"


def test_error_text_never_quotes_a_header(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, ValueError("Invalid header value b'Bearer gsk_secret\\nX: y'"))
    reply = net.request("https://api.example.com/", timeout=5, headers={"Authorization": "x"})
    assert reply.error == "invalid request"
    _serve(monkeypatch, OSError("[Errno 54] reset by peer while sending key=abc"))
    assert "abc" not in net.request("https://api.example.com/", timeout=5).error


def test_a_slow_error_body_is_held_to_the_same_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import email.message
    import urllib.error

    slow = _Resp([b'{"error": "x"}'] * 1000, {}, delay=0.01)
    _serve(
        monkeypatch,
        urllib.error.HTTPError(
            "https://api.example.com/", 500, "err", email.message.Message(), slow
        ),  # type: ignore[arg-type]
    )
    started = time.monotonic()
    assert net.request("https://api.example.com/", timeout=0.2).error == "timed out"
    assert time.monotonic() - started < 1.0


def test_an_error_body_is_truncated_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    import email.message
    import urllib.error

    body = _Resp([b"e" * 3000, b"e" * 3000], {})
    _serve(
        monkeypatch,
        urllib.error.HTTPError(
            "https://api.example.com/", 429, "slow down", email.message.Message(), body
        ),  # type: ignore[arg-type]
    )
    reply = net.request("https://api.example.com/", timeout=5)
    assert reply.status == 429 and reply.body == b"e" * net.ERROR_BODY_BYTES


def test_redirects_get_only_the_time_that_is_left() -> None:
    req = urllib.request.Request("https://news.example.com/a")
    req.timeout = 30  # type: ignore[attr-defined]
    handler = net._PublicRedirects(time.monotonic() + 2)
    monkeypatched_guard = net.assert_public_url
    try:
        net.assert_public_url = lambda url: None  # type: ignore[assignment]
        handler.redirect_request(req, None, 302, "Found", {}, "https://news.example.com/b")
        assert req.timeout <= 2  # type: ignore[attr-defined]
        with pytest.raises(net._OutOfTime):
            net._PublicRedirects(time.monotonic() - 1).redirect_request(
                req, None, 302, "Found", {}, "https://news.example.com/c"
            )
    finally:
        net.assert_public_url = monkeypatched_guard  # type: ignore[assignment]
