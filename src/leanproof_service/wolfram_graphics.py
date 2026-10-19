from __future__ import annotations

from collections import OrderedDict
import re
from threading import Lock
from time import monotonic
from uuid import uuid4


_CIRCLE_PATTERN = re.compile(
    r"\s*Graphics\s*\[\s*\{\s*(?P<color>Red|Blue|Green|Black)\s*,\s*"
    r"Circle\s*\[\s*\]\s*\}\s*\]\s*"
)
_COLORS = {
    "Red": "#d62828",
    "Blue": "#1565c0",
    "Green": "#2e7d32",
    "Black": "#202124",
}
_GRAPHIC_TTL_SECONDS = 15 * 60
_GRAPHIC_CAPACITY = 256
_graphics_lock = Lock()
_graphics: OrderedDict[str, tuple[float, str]] = OrderedDict()


def render_wolfram_graphics(code: str) -> str | None:
    """Render the deliberately small, static Graphics syntax supported here."""
    match = _CIRCLE_PATTERN.fullmatch(code)
    if match is None:
        return None
    stroke = _COLORS[match.group("color")]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" '
        'viewBox="0 0 300 300" role="img" aria-label="Circle">'
        f'<circle cx="150" cy="150" r="100" fill="none" stroke="{stroke}" '
        'stroke-width="5"/></svg>'
    )


def publish_graphic(svg: str) -> str:
    token = uuid4().hex
    now = monotonic()
    with _graphics_lock:
        expired = [key for key, (expiry, _) in _graphics.items() if expiry <= now]
        for key in expired:
            del _graphics[key]
        while len(_graphics) >= _GRAPHIC_CAPACITY:
            _graphics.popitem(last=False)
        _graphics[token] = (now + _GRAPHIC_TTL_SECONDS, svg)
    return f"/api/cag/v1/graphics/{token}.svg"


def get_published_graphic(token: str) -> str | None:
    if re.fullmatch(r"[0-9a-f]{32}", token) is None:
        return None
    now = monotonic()
    with _graphics_lock:
        entry = _graphics.get(token)
        if entry is None:
            return None
        expiry, svg = entry
        if expiry <= now:
            del _graphics[token]
            return None
        return svg
