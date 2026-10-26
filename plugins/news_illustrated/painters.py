"""The illustration: a cascade of image providers, first success wins.

kie.ai, Gemini, Pollinations and HuggingFace in the configured order, each
skipped when it has no key. One story's illustration has a time budget; a
provider is not started with too little of it left, since a half-finished
call is paid for and thrown away. Whatever comes back must decode as a real
picture of a sensible size before it counts: providers answer errors with
HTML or JSON and a 200 often enough.

The provider quirks below were learned on the PHP original and are kept on
purpose: kie.ai wants the Bearer header on the poll as well as the create
and sizes with ``input.image_size`` (``aspect_ratio`` is silently ignored);
Gemini needs ``imageSize`` next to ``aspectRatio`` or it sometimes paints a
square; Pollinations needs a short prompt to keep its GET URL under
Cloudflare's limit; HuggingFace serves from ``router.huggingface.co`` only.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import net, text

PROVIDERS = ("kie", "gemini", "pollinations", "huggingface")
BUDGET_S = 150.0
MIN_PROVIDER_S = 20.0
SHORT_PROMPT_CHARS = 400
MIN_EDGE_PX = 256
# The panel this was made for is 1440x2560; nothing gains from a bigger file.
MAX_EDGE_PX = 2560
JPEG_QUALITY = 85

KIE_CREATE_URL = "https://api.kie.ai/api/v1/jobs/createTask"
KIE_RECORD_URL = "https://api.kie.ai/api/v1/jobs/recordInfo"
KIE_POLL_EVERY_S = 5.0
KIE_POLL_MAX_S = 120.0
# Kept back from the budget for downloading the finished picture.
KIE_DOWNLOAD_RESERVE_S = 15.0
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
POLLINATIONS_URL = "https://gen.pollinations.ai/image/"
HUGGINGFACE_URL = "https://router.huggingface.co/hf-inference/models/"
# 768x1440 is an exact 8:15 portrait; the FLUX providers take explicit sizes.
FLUX_WIDTH, FLUX_HEIGHT = 768, 1440
DEFAULT_IMAGE_PROMPT = (
    "Single-panel Far Side-style black and white ink comic. Topic: {title}. {summary}. "
    "No content in bottom 25%."
)


class PaintError(Exception):
    """A provider produced no usable picture; the message goes in the run log."""


@dataclass
class Budget:
    """The seconds one illustration may take, on a clock tests can drive."""

    seconds: float = BUDGET_S
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    deadline: float = field(init=False)

    def __post_init__(self) -> None:
        self.deadline = self.clock() + self.seconds

    def left(self) -> float:
        return self.deadline - self.clock()

    def timeout(self, cap: float, floor: float = 5.0) -> float:
        """A request timeout: what is left of the budget, within [floor, cap]."""
        return max(floor, min(cap, self.left()))


@dataclass(frozen=True)
class Settings:
    """What the cascade needs from the widget's settings."""

    keys: dict[str, str]
    order: tuple[str, ...] = PROVIDERS
    kie_model: str = "google/nano-banana"
    gemini_model: str = "gemini-2.5-flash-image"
    pollinations_model: str = "flux"
    huggingface_model: str = "black-forest-labs/FLUX.1-schnell"
    image_prompt: str = ""


@dataclass(frozen=True)
class Painting:
    jpeg: bytes
    provider: str
    size: tuple[int, int]


def prompts(title: str, summary: str, template: str) -> tuple[str, str]:
    """(detailed prompt for instruction-following models, short keyword-style
    prompt for the FLUX diffusion models)."""
    detailed = (template or DEFAULT_IMAGE_PROMPT).replace("{title}", title)
    detailed = detailed.replace("{summary}", summary).strip()
    short = (
        "Far Side-style black and white ink comic, crosshatching, bold outlines. "
        f"{title}. {summary}. No content in bottom 25% or near edges."
    )
    if len(short) > SHORT_PROMPT_CHARS:
        short = short[: SHORT_PROMPT_CHARS - 3].rstrip() + "..."
    return detailed, short


def paint(
    title: str,
    summary: str,
    settings: Settings,
    *,
    budget: Budget,
    log: Callable[[str], None],
    stopped: Callable[[], bool] = lambda: False,
) -> Painting | None:
    """The first provider's picture, as a JPEG; None when none produced one."""
    detailed, short = prompts(title, summary, settings.image_prompt)
    for name in settings.order:
        key = settings.keys.get(name, "")
        if name not in _CALLS or not key:
            continue
        if stopped():
            return None
        left = budget.left()
        if left < MIN_PROVIDER_S:
            log(f"Image time budget used up ({left:.0f}s left), skipping the other providers")
            return None
        log(f"Painting with {name}")
        try:
            raw = _CALLS[name](settings, key, detailed, short, budget, stopped)
            jpeg, size = to_jpeg(raw)
        except PaintError as err:
            log(f"{name}: {err}")
            continue
        log(f"{name}: {size[0]}x{size[1]} picture")
        return Painting(jpeg, name, size)
    return None


def to_jpeg(raw: bytes) -> tuple[bytes, tuple[int, int]]:
    """Check ``raw`` is a real picture and re-encode it as a JPEG no larger
    than the panel needs. Raises PaintError for anything else (an HTML error
    page, a JSON error, a truncated download, a thumbnail)."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not raw:
        raise PaintError("empty response")
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened) or opened
            image = image.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as err:
        raise PaintError(f"response is not a picture ({len(raw)} bytes)") from err
    if min(image.size) < MIN_EDGE_PX:
        raise PaintError(f"picture too small ({image.size[0]}x{image.size[1]})")
    image.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return out.getvalue(), image.size


# ----------------------------------------------------------------------
# Providers. Each returns the raw picture bytes or raises PaintError.
# ----------------------------------------------------------------------


def kie(
    settings: Settings,
    key: str,
    detailed: str,
    short: str,
    budget: Budget,
    stopped: Callable[[], bool],
) -> bytes:
    """kie.ai: create a task, poll it until it has a picture, download that."""
    del short
    auth = {"Authorization": f"Bearer {key}"}
    created = net.post_json(
        KIE_CREATE_URL,
        {"model": settings.kie_model, "input": {"prompt": detailed, "image_size": "9:16"}},
        timeout=budget.timeout(30),
        headers=auth,
    )
    answer = created.json()
    task_id = _dig(answer, "data", "taskId")
    if not created.ok or not task_id:
        raise PaintError(f"task not created ({_message(answer) or created.reason()})")
    poll_until = min(budget.clock() + KIE_POLL_MAX_S, budget.deadline - KIE_DOWNLOAD_RESERVE_S)
    url = f"{KIE_RECORD_URL}?{urllib.parse.urlencode({'taskId': str(task_id)})}"
    while budget.clock() < poll_until:
        budget.sleep(KIE_POLL_EVERY_S)
        if stopped():
            raise PaintError("stopped")
        polled = net.request(url, timeout=budget.timeout(15), headers=auth)
        record = polled.json()
        if not polled.ok:
            continue
        state = str(_dig(record, "data", "state") or "unknown")
        if state == "success":
            image_url = _kie_result_url(_dig(record, "data", "resultJson"))
            if not image_url:
                raise PaintError("finished without a picture URL")
            got = net.request(image_url, timeout=budget.timeout(60, 15))
            if not got.ok:
                raise PaintError(f"download failed ({got.reason()})")
            return got.body
        if state == "fail":
            raise PaintError(
                f"generation failed ({_dig(record, 'data', 'failMsg') or 'no reason'})"
            )
        # waiting / queuing / generating: keep polling
    raise PaintError("timed out waiting for the picture")


def gemini(
    settings: Settings,
    key: str,
    detailed: str,
    short: str,
    budget: Budget,
    stopped: Callable[[], bool],
) -> bytes:
    """Gemini image generation: one call, the picture inline as base64."""
    del short, stopped
    reply = net.post_json(
        GEMINI_URL.format(model=urllib.parse.quote(settings.gemini_model, safe="-._")),
        {
            "contents": [{"parts": [{"text": detailed}]}],
            "generationConfig": {"imageConfig": {"aspectRatio": "9:16", "imageSize": "2K"}},
        },
        timeout=budget.timeout(90),
        # The header rather than ?key= keeps the key out of any URL that gets logged.
        headers={"x-goog-api-key": key},
    )
    answer = reply.json()
    if isinstance(answer, dict) and isinstance(answer.get("error"), dict):
        raise PaintError(_message(answer) or reply.reason())
    if not reply.ok:
        raise PaintError(reply.reason())
    parts = _dig(answer, "candidates", 0, "content", "parts")
    for part in parts if isinstance(parts, list) else []:
        data = _dig(part, "inlineData", "data")
        if data:
            try:
                return base64.b64decode(str(data), validate=True)
            except (binascii.Error, ValueError) as err:
                raise PaintError("picture data is not valid base64") from err
    raise PaintError("no picture in the answer")


def pollinations(
    settings: Settings,
    key: str,
    detailed: str,
    short: str,
    budget: Budget,
    stopped: Callable[[], bool],
) -> bytes:
    """Pollinations (FLUX): a GET whose path is the prompt."""
    del detailed, stopped
    query = urllib.parse.urlencode(
        {
            "width": FLUX_WIDTH,
            "height": FLUX_HEIGHT,
            "model": settings.pollinations_model,
            "nologo": "true",
            # Keep the prompt as written rather than letting the service rewrite it.
            "enhance": "false",
            "key": key,
        }
    )
    reply = net.request(
        f"{POLLINATIONS_URL}{urllib.parse.quote(short, safe='')}?{query}",
        timeout=budget.timeout(90),
    )
    if not reply.ok:
        raise PaintError(reply.reason())
    return reply.body


def huggingface(
    settings: Settings,
    key: str,
    detailed: str,
    short: str,
    budget: Budget,
    stopped: Callable[[], bool],
) -> bytes:
    """HuggingFace Inference (FLUX.1-schnell): the picture is the response body."""
    del detailed, stopped
    reply = net.post_json(
        HUGGINGFACE_URL + urllib.parse.quote(settings.huggingface_model, safe="/-._"),
        {"inputs": short, "parameters": {"width": FLUX_WIDTH, "height": FLUX_HEIGHT}},
        timeout=budget.timeout(90),
        headers={"Authorization": f"Bearer {key}", "Accept": "image/jpeg"},
    )
    if not reply.ok:
        raise PaintError(_message(reply.json()) or reply.reason())
    return reply.body


_CALLS: dict[str, Callable[..., bytes]] = {
    "kie": kie,
    "gemini": gemini,
    "pollinations": pollinations,
    "huggingface": huggingface,
}


def _kie_result_url(result: Any) -> str:
    """``resultJson`` is a JSON string (sometimes already an object) holding
    ``resultUrls``."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return ""
    urls = result.get("resultUrls") if isinstance(result, dict) else None
    if isinstance(urls, list) and urls and isinstance(urls[0], str):
        return urls[0]
    return ""


def _dig(value: Any, *path: str | int) -> Any:
    for step in path:
        if isinstance(step, int):
            if not isinstance(value, list) or len(value) <= step:
                return None
            value = value[step]
        else:
            if not isinstance(value, dict):
                return None
            value = value.get(step)
    return value


def _message(answer: Any) -> str:
    """The error sentence an API put in its JSON, if any."""
    if not isinstance(answer, dict):
        return ""
    error = answer.get("error")
    found = error.get("message") if isinstance(error, dict) else error
    found = found or answer.get("msg") or answer.get("message")
    return text.squash(str(found or ""))[:200]
