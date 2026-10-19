from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
import re
from threading import Lock
from time import monotonic
from uuid import uuid4


_COLORS = {
    "Red": "#d62828",
    "Blue": "#1565c0",
    "Green": "#2e7d32",
    "Black": "#202124",
    "White": "#ffffff",
    "Gray": "#808080",
    "Orange": "#ed7d31",
    "Purple": "#7030a0",
    "Cyan": "#00a6b2",
    "Magenta": "#c000c0",
    "Yellow": "#f2c811",
    "Pink": "#e78ac3",
    "Brown": "#8c564b",
}
_TOKEN = re.compile(
    r"\s*(?:(?P<rule>->)|(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)|"
    r"(?P<name>[A-Za-z][A-Za-z0-9]*)|(?P<punct>[{}\[\],+\-/]))"
)
_MAX_GRAPHIC_INPUT = 4000
_MAX_GRAPHIC_TOKENS = 2048
_MAX_GRAPHIC_DEPTH = 24
_MAX_GRAPHIC_NODES = 1024
_MAX_GRAPHIC_PRIMITIVES = 128
_MAX_GRAPHIC_VERTICES = 512
_MAX_COORDINATE = 1_000_000.0
_GRAPHIC_TTL_SECONDS = 15 * 60
_GRAPHIC_CAPACITY = 256
_graphics_lock = Lock()
_graphics: OrderedDict[str, tuple[float, str]] = OrderedDict()


@dataclass(frozen=True)
class _Symbol:
    name: str


@dataclass(frozen=True)
class _Call:
    name: str
    args: tuple[object, ...]


@dataclass(frozen=True)
class _Rule:
    left: object
    right: object


@dataclass
class _Style:
    color: str = "#5e81ac"
    opacity: float = 1.0
    stroke_width: float = 1.5
    point_radius: float = 3.0

    def copy(self) -> _Style:
        return _Style(self.color, self.opacity, self.stroke_width, self.point_radius)


@dataclass(frozen=True)
class _Primitive:
    kind: str
    points: tuple[tuple[float, float], ...]
    color: str
    opacity: float
    stroke_width: float
    point_radius: float
    radii: tuple[float, float] | None = None


class _GraphicsParseError(ValueError):
    pass


class _Parser:
    def __init__(self, code: str) -> None:
        if len(code) > _MAX_GRAPHIC_INPUT:
            raise _GraphicsParseError("graphics input is too long")
        self.tokens: list[str] = []
        position = 0
        while position < len(code):
            if not code[position:].strip():
                break
            match = _TOKEN.match(code, position)
            if match is None:
                raise _GraphicsParseError("unsupported graphics syntax")
            self.tokens.append(next(group for group in match.groups() if group is not None))
            position = match.end()
            if len(self.tokens) > _MAX_GRAPHIC_TOKENS:
                raise _GraphicsParseError("graphics expression has too many tokens")
        self.index = 0
        self.nodes = 0

    def parse(self) -> object:
        value = self._expression(0)
        if self.index != len(self.tokens):
            raise _GraphicsParseError("unexpected trailing graphics input")
        return value

    def _expression(self, depth: int) -> object:
        if depth > _MAX_GRAPHIC_DEPTH:
            raise _GraphicsParseError("graphics expression is nested too deeply")
        self.nodes += 1
        if self.nodes > _MAX_GRAPHIC_NODES:
            raise _GraphicsParseError("graphics expression has too many nodes")
        left = self._primary(depth + 1)
        if self._accept("->"):
            return _Rule(left, self._expression(depth + 1))
        return left

    def _primary(self, depth: int) -> object:
        token = self._peek()
        if token is None:
            raise _GraphicsParseError("unexpected end of graphics input")
        if token in {"+", "-"}:
            self.index += 1
            value = self._numeric_token()
            return value if token == "+" else -value
        if token == "{":
            self.index += 1
            values: list[object] = []
            if self._accept("}"):
                return values
            while True:
                values.append(self._expression(depth + 1))
                if self._accept("}"):
                    return values
                self._expect(",")
        if token == "[":
            raise _GraphicsParseError("unexpected opening bracket")
        if token[0].isdigit() or token.startswith("."):
            return self._numeric_token()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", token):
            self.index += 1
            if self._accept("["):
                args: list[object] = []
                if self._accept("]"):
                    return _Call(token, tuple(args))
                while True:
                    args.append(self._expression(depth + 1))
                    if self._accept("]"):
                        return _Call(token, tuple(args))
                    self._expect(",")
            return _Symbol(token)
        raise _GraphicsParseError("unsupported graphics token")

    def _numeric_token(self) -> float:
        token = self._peek()
        if token is None or re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", token) is None:
            raise _GraphicsParseError("a finite numeric coordinate was expected")
        self.index += 1
        value = float(token)
        if not math.isfinite(value) or abs(value) > _MAX_COORDINATE:
            raise _GraphicsParseError("numeric graphics values are outside the supported range")
        if self._accept("/"):
            denominator = self._numeric_token()
            if denominator == 0:
                raise _GraphicsParseError("a rational coordinate cannot have a zero denominator")
            value /= denominator
        return value

    def _peek(self) -> str | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _accept(self, expected: str) -> bool:
        if self._peek() != expected:
            return False
        self.index += 1
        return True

    def _expect(self, expected: str) -> None:
        if not self._accept(expected):
            raise _GraphicsParseError(f"expected {expected!r}")


def render_wolfram_graphics(code: str) -> str | None:
    """Render a bounded, data-only subset of Wolfram 2D Graphics as SVG."""
    try:
        expression = _Parser(code).parse()
        if not isinstance(expression, _Call) or expression.name != "Graphics":
            return None
        if not expression.args:
            items: object = []
            options: tuple[object, ...] = ()
        else:
            items = expression.args[0]
            options = expression.args[1:]
        size = 300
        plot_range: tuple[float, float, float, float] | None = None
        background: str | None = None
        for option in options:
            if not isinstance(option, _Rule) or not isinstance(option.left, _Symbol):
                raise _GraphicsParseError("unsupported Graphics option")
            if option.left.name == "ImageSize":
                number = _number(option.right)
                if number is None or not number.is_integer() or not 100 <= number <= 1200:
                    raise _GraphicsParseError("ImageSize must be an integer from 100 to 1200")
                size = int(number)
            elif option.left.name == "PlotRange":
                if isinstance(option.right, _Symbol) and option.right.name == "All":
                    plot_range = None
                else:
                    plot_range = _parse_plot_range(option.right)
                    if plot_range is None:
                        raise _GraphicsParseError("PlotRange must be All or two finite coordinate ranges")
            elif option.left.name == "Background":
                background = _color(option.right)
                if background is None:
                    raise _GraphicsParseError("unsupported Graphics background color")
            else:
                raise _GraphicsParseError("unsupported Graphics option")

        primitives: list[_Primitive] = []
        _render_items(items, _Style(), primitives)
        if len(primitives) > _MAX_GRAPHIC_PRIMITIVES:
            raise _GraphicsParseError("graphics contains too many primitives")
        return _svg(primitives, size=size, plot_range=plot_range, background=background)
    except (_GraphicsParseError, OverflowError, ValueError):
        return None


def _render_items(items: object, style: _Style, output: list[_Primitive]) -> None:
    if isinstance(items, list):
        scoped_style = style.copy()
        for item in items:
            if isinstance(item, list):
                _render_items(item, scoped_style.copy(), output)
            elif not _apply_directive(item, scoped_style):
                primitive = _primitive(item, scoped_style)
                if primitive is None:
                    raise _GraphicsParseError("unsupported graphics directive or primitive")
                output.extend(primitive)
        return
    if not _apply_directive(items, style):
        primitive = _primitive(items, style)
        if primitive is None:
            raise _GraphicsParseError("unsupported graphics directive or primitive")
        output.extend(primitive)


def _apply_directive(value: object, style: _Style) -> bool:
    color = _color(value)
    if color is not None:
        style.color = color
        return True
    if isinstance(value, _Symbol):
        if value.name == "Thick":
            style.stroke_width = 5.0
            return True
        if value.name == "Thin":
            style.stroke_width = 1.0
            return True
        return False
    if not isinstance(value, _Call):
        return False
    if value.name == "Directive":
        for directive in value.args:
            if not _apply_directive(directive, style):
                return False
        return True
    if value.name == "Opacity" and len(value.args) == 1:
        opacity = _number(value.args[0])
        if opacity is None or not 0.0 <= opacity <= 1.0:
            return False
        style.opacity = opacity
        return True
    if value.name == "AbsoluteThickness" and len(value.args) == 1:
        thickness = _number(value.args[0])
        if thickness is None or not 0.0 < thickness <= 20.0:
            return False
        style.stroke_width = thickness
        return True
    if value.name == "PointSize" and len(value.args) == 1:
        point_size = _number(value.args[0])
        if point_size is None or not 0.0 < point_size <= 0.1:
            return False
        style.point_radius = max(1.0, point_size * 300.0 / 2.0)
        return True
    return False


def _primitive(value: object, style: _Style) -> list[_Primitive] | None:
    if not isinstance(value, _Call):
        return None
    name = value.name
    if name in {"Circle", "Disk"}:
        center = (0.0, 0.0)
        radii = (1.0, 1.0)
        if len(value.args) > 2:
            return None
        if value.args:
            parsed_center = _point(value.args[0])
            if parsed_center is None:
                return None
            center = parsed_center
        if len(value.args) == 2:
            second = value.args[1]
            if isinstance(second, list):
                if len(second) != 2:
                    return None
                rx, ry = _number(second[0]), _number(second[1])
                if rx is None or ry is None:
                    return None
                radii = (abs(rx), abs(ry))
            else:
                radius = _number(second)
                if radius is None:
                    return None
                radii = (abs(radius), abs(radius))
        if radii[0] == 0 or radii[1] == 0:
            return None
        return [_make_primitive(name.lower(), (center,), style, radii=radii)]
    if name == "Rectangle":
        if not value.args:
            low, high = (0.0, 0.0), (1.0, 1.0)
        elif len(value.args) == 1:
            low = _point(value.args[0])
            if low is None:
                return None
            high = (low[0] + 1.0, low[1] + 1.0)
        elif len(value.args) == 2:
            low, high = _point(value.args[0]), _point(value.args[1])
            if low is None or high is None:
                return None
        else:
            return None
        return [_make_primitive("rectangle", (low, high), style)]
    if name in {"Line", "Polygon", "Point"} and len(value.args) == 1:
        paths = _coordinate_paths(value.args[0])
        if paths is None:
            return None
        minimum = 1 if name == "Point" else (3 if name == "Polygon" else 2)
        if any(len(path) < minimum for path in paths):
            return None
        if sum(len(path) for path in paths) > _MAX_GRAPHIC_VERTICES:
            return None
        kind = name.lower()
        return [_make_primitive(kind, path, style) for path in paths]
    return None


def _make_primitive(
    kind: str,
    points: tuple[tuple[float, float], ...],
    style: _Style,
    *,
    radii: tuple[float, float] | None = None,
) -> _Primitive:
    return _Primitive(
        kind=kind,
        points=points,
        color=style.color,
        opacity=style.opacity,
        stroke_width=style.stroke_width,
        point_radius=style.point_radius,
        radii=radii,
    )


def _coordinate_paths(value: object) -> list[tuple[tuple[float, float], ...]] | None:
    if isinstance(value, list) and _point(value) is not None:
        return [(_point(value) or (0.0, 0.0),)]
    if not isinstance(value, list) or not value:
        return None
    if all(_point(item) is not None for item in value):
        return [tuple(_point(item) for item in value if _point(item) is not None)]
    paths: list[tuple[tuple[float, float], ...]] = []
    for item in value:
        if not isinstance(item, list) or not item or not all(_point(point) is not None for point in item):
            return None
        paths.append(tuple(_point(point) for point in item if _point(point) is not None))
    return paths


def _point(value: object) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    x, y = _number(value[0]), _number(value[1])
    if x is None or y is None:
        return None
    return x, y


def _number(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    if not math.isfinite(number) or abs(number) > _MAX_COORDINATE:
        return None
    return number


def _color(value: object) -> str | None:
    if isinstance(value, _Symbol):
        return _COLORS.get(value.name)
    if isinstance(value, _Call) and value.name == "RGBColor" and len(value.args) == 3:
        components = [_number(component) for component in value.args]
        if any(component is None or not 0.0 <= component <= 1.0 for component in components):
            return None
        red, green, blue = (round(component * 255) for component in components if component is not None)
        return f"#{red:02x}{green:02x}{blue:02x}"
    if isinstance(value, _Call) and value.name == "GrayLevel" and len(value.args) == 1:
        level = _number(value.args[0])
        if level is None or not 0.0 <= level <= 1.0:
            return None
        gray = round(level * 255)
        return f"#{gray:02x}{gray:02x}{gray:02x}"
    return None


def _parse_plot_range(value: object) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    x_range, y_range = value
    if not isinstance(x_range, list) or not isinstance(y_range, list):
        return None
    if len(x_range) != 2 or len(y_range) != 2:
        return None
    numbers = tuple(_number(item) for item in (*x_range, *y_range))
    if any(number is None for number in numbers):
        return None
    x_min, x_max, y_min, y_max = (number for number in numbers if number is not None)
    if x_min >= x_max or y_min >= y_max:
        return None
    return x_min, x_max, y_min, y_max


def _svg(
    primitives: list[_Primitive],
    *,
    size: int,
    plot_range: tuple[float, float, float, float] | None,
    background: str | None,
) -> str:
    bounds: list[tuple[float, float]] = []
    for primitive in primitives:
        if primitive.kind in {"circle", "disk"} and primitive.radii is not None:
            center = primitive.points[0]
            rx, ry = primitive.radii
            bounds.extend(((center[0] - rx, center[1] - ry), (center[0] + rx, center[1] + ry)))
        else:
            bounds.extend(primitive.points)
    if plot_range is None:
        if bounds:
            x_min = min(point[0] for point in bounds)
            x_max = max(point[0] for point in bounds)
            y_min = min(point[1] for point in bounds)
            y_max = max(point[1] for point in bounds)
        else:
            x_min, x_max, y_min, y_max = -1.0, 1.0, -1.0, 1.0
    else:
        x_min, x_max, y_min, y_max = plot_range
    if x_min == x_max:
        x_min -= 0.5
        x_max += 0.5
    if y_min == y_max:
        y_min -= 0.5
        y_max += 0.5
    margin = min(50.0, size * 0.18)
    scale = min(
        (size - 2.0 * margin) / (x_max - x_min),
        (size - 2.0 * margin) / (y_max - y_min),
    )
    scaled_width = (x_max - x_min) * scale
    scaled_height = (y_max - y_min) * scale
    pad_x = (size - scaled_width) / 2.0
    pad_y = (size - scaled_height) / 2.0

    def point(x: float, y: float) -> tuple[float, float]:
        return pad_x + (x - x_min) * scale, size - pad_y - (y - y_min) * scale

    elements: list[str] = []
    if background is not None:
        elements.append(f'<rect width="{size}" height="{size}" fill="{background}"/>')
    for primitive in primitives:
        color = primitive.color
        opacity = _fmt(primitive.opacity)
        width = _fmt(primitive.stroke_width)
        if primitive.kind in {"circle", "disk"} and primitive.radii is not None:
            cx, cy = point(*primitive.points[0])
            rx, ry = primitive.radii[0] * scale, primitive.radii[1] * scale
            if primitive.kind == "circle" and abs(rx - ry) < 1e-9:
                elements.append(
                    f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(rx)}" '
                    f'fill="none" stroke="{color}" stroke-opacity="{opacity}" '
                    f'stroke-width="{width}"/>'
                )
            else:
                fill = "none" if primitive.kind == "circle" else color
                elements.append(
                    f'<ellipse cx="{_fmt(cx)}" cy="{_fmt(cy)}" rx="{_fmt(rx)}" ry="{_fmt(ry)}" '
                    f'fill="{fill}" fill-opacity="{opacity}" stroke="{color}" '
                    f'stroke-opacity="{opacity}" stroke-width="{width}"/>'
                )
        elif primitive.kind == "rectangle":
            (x1, y1), (x2, y2) = primitive.points
            left, top = point(min(x1, x2), max(y1, y2))
            right, bottom = point(max(x1, x2), min(y1, y2))
            elements.append(
                f'<rect x="{_fmt(left)}" y="{_fmt(top)}" width="{_fmt(right-left)}" '
                f'height="{_fmt(bottom-top)}" fill="{color}" fill-opacity="{opacity}" '
                f'stroke="{color}" stroke-opacity="{opacity}" stroke-width="{width}"/>'
            )
        elif primitive.kind in {"line", "polygon"}:
            coordinates = " ".join(
                f"{_fmt(x)},{_fmt(y)}" for x, y in (point(*coordinate) for coordinate in primitive.points)
            )
            if primitive.kind == "line":
                elements.append(
                    f'<polyline points="{coordinates}" fill="none" stroke="{color}" '
                    f'stroke-opacity="{opacity}" stroke-width="{width}" stroke-linecap="round" '
                    'stroke-linejoin="round"/>'
                )
            else:
                elements.append(
                    f'<polygon points="{coordinates}" fill="{color}" fill-opacity="{opacity}" '
                    f'stroke="{color}" stroke-opacity="{opacity}" stroke-width="{width}" '
                    'stroke-linejoin="round"/>'
                )
        elif primitive.kind == "point":
            for coordinate in primitive.points:
                cx, cy = point(*coordinate)
                elements.append(
                    f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(primitive.point_radius)}" '
                    f'fill="{color}" fill-opacity="{opacity}"/>'
                )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" role="img" aria-label="Wolfram graphics">'
        + "".join(elements)
        + "</svg>"
    )


def _fmt(value: float) -> str:
    return format(value, ".6g")


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
