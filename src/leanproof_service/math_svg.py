"""Small, bounded renderer for presentation MathML used by result images."""

from __future__ import annotations

import base64
import html
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass


_MATHML_NS = "http://www.w3.org/1998/Math/MathML"
_MAX_SOURCE_CHARS = 50_000
_MAX_NODES = 256
_MAX_DEPTH = 32
_MAX_TOKEN_CHARS = 512
_MAX_NATURAL_WIDTH = 4_000
_FONT_SIZE = 24
_INVISIBLE_OPERATORS = {"\u2061", "\u2062", "\u2063"}


@dataclass(frozen=True)
class _Box:
    width: float
    ascent: float
    descent: float
    body: str


def mathml_image(
    source: str | None,
    alt: str,
    *,
    max_width: int = 500,
) -> dict[str, object] | None:
    """Render allowlisted presentation MathML to an inline SVG image.

    Unsupported or oversized MathML returns ``None`` so callers can use their
    escaped-text rendering fallback.
    """
    if not source or len(source) > _MAX_SOURCE_CHARS:
        return None
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", source, re.IGNORECASE):
        return None
    try:
        root = ET.fromstring(source)
        if sum(1 for _ in root.iter()) > _MAX_NODES:
            return None
        box = _layout(root, 0, [0])
    except (ET.ParseError, ValueError, OverflowError):
        return None
    if box is None or box.width <= 0 or box.width > _MAX_NATURAL_WIDTH:
        return None

    safe_alt = _xml_safe(alt)
    title_text = safe_alt[:240] + ("..." if len(safe_alt) > 240 else "")
    title = html.escape(title_text, quote=True)
    padding_x = 12
    padding_y = 10
    natural_width = max(24, math.ceil(box.width + padding_x * 2))
    natural_height = max(28, math.ceil(box.ascent + box.descent + padding_y * 2))
    output_width = min(max(24, max_width), natural_width)
    scale = min(1.0, output_width / natural_width)
    output_height = max(24, math.ceil(natural_height * scale))
    baseline = padding_y + box.ascent
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{output_width}" '
        f'height="{output_height}" viewBox="0 0 {natural_width} {natural_height}" '
        'preserveAspectRatio="xMidYMid meet" role="img" '
        f'aria-label="{title}"><title>{title}</title>'
        '<rect width="100%" height="100%" fill="#ffffff"/>'
        '<g fill="#111827" stroke="#111827" stroke-width="1.5" '
        'font-family="Cambria Math,STIX Two Math,DejaVu Serif,serif">'
        f'<g transform="translate({padding_x} {baseline})">{box.body}</g>'
        '</g></svg>'
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return {
        "src": f"data:image/svg+xml;base64,{encoded}",
        "alt": title_text,
        "title": title_text,
        "width": output_width,
        "height": output_height,
    }


def _layout(element: ET.Element, depth: int, count: list[int]) -> _Box | None:
    count[0] += 1
    if count[0] > _MAX_NODES or depth > _MAX_DEPTH:
        raise ValueError("MathML exceeds rendering bounds")
    tag, namespace = _tag_name(element.tag)
    if namespace not in {"", _MATHML_NS}:
        raise ValueError("unsupported MathML namespace")

    children = list(element)
    if tag not in {"mi", "mn", "mo", "mtext"} and (element.text or "").strip():
        raise ValueError("unexpected text outside a MathML token")
    if any((child.tail or "").strip() for child in children):
        raise ValueError("unexpected text between MathML elements")
    if tag in {"math", "mrow", "mstyle"}:
        return _row(_required_children(children, depth, count))
    if tag == "semantics":
        for child in children:
            if _tag_name(child.tag)[0] not in {"annotation", "annotation-xml"}:
                return _layout(child, depth + 1, count)
        raise ValueError("MathML semantics has no presentation child")
    if tag in {"mi", "mn", "mo", "mtext"}:
        if children:
            raise ValueError("MathML tokens cannot contain child elements")
        text = element.text or ""
        if tag != "mtext":
            text = text.strip()
        if not text or len(text) > _MAX_TOKEN_CHARS:
            raise ValueError("MathML token is empty or too long")
        safe_text = _xml_safe(text)
        if not safe_text:
            raise ValueError("MathML token has no displayable characters")
        display = "" if safe_text in _INVISIBLE_OPERATORS else safe_text
        size = _FONT_SIZE if tag != "mtext" else 20
        width = _text_width(display, size)
        if safe_text in _INVISIBLE_OPERATORS:
            width = 3.0
        escaped = html.escape(display, quote=False)
        body = (
            f'<text x="0" y="0" font-size="{size}" '
            f'font-style="{"italic" if tag == "mi" else "normal"}" '
            f'fill="#111827" stroke="none">{escaped}</text>'
            if display
            else ""
        )
        return _Box(width, 18.0, 5.0, body)
    if tag == "mspace":
        if children:
            raise ValueError("MathML mspace cannot contain children")
        return _Box(_space_width(element.attrib.get("width", "")), 0.0, 0.0, "")
    if tag in {"mfrac", "msup", "msub", "msubsup", "msqrt", "mroot", "mfenced"}:
        laid_out = _required_children(children, depth, count)
        if tag == "mfrac" and len(laid_out) == 2:
            return _fraction(laid_out[0], laid_out[1])
        if tag == "msup" and len(laid_out) == 2:
            return _scripts(laid_out[0], None, laid_out[1])
        if tag == "msub" and len(laid_out) == 2:
            return _scripts(laid_out[0], laid_out[1], None)
        if tag == "msubsup" and len(laid_out) == 3:
            return _scripts(laid_out[0], laid_out[1], laid_out[2])
        if tag == "msqrt" and len(laid_out) == 1:
            return _sqrt(laid_out[0], None)
        if tag == "mroot" and len(laid_out) == 2:
            return _sqrt(laid_out[0], laid_out[1])
        if tag == "mfenced":
            return _fenced(element, laid_out)
        raise ValueError(f"invalid MathML child count for {tag}")
    raise ValueError(f"unsupported MathML element {tag}")


def _required_children(
    children: list[ET.Element], depth: int, count: list[int]
) -> list[_Box]:
    boxes = [_layout(child, depth + 1, count) for child in children]
    if any(box is None for box in boxes):
        raise ValueError("MathML contains an empty presentation element")
    return [box for box in boxes if box is not None]


def _row(boxes: list[_Box]) -> _Box:
    if not boxes:
        raise ValueError("MathML row is empty")
    width = sum(box.width for box in boxes)
    ascent = max(box.ascent for box in boxes)
    descent = max(box.descent for box in boxes)
    if width > _MAX_NATURAL_WIDTH:
        raise ValueError("MathML row is too wide")
    parts: list[str] = []
    x = 0.0
    for box in boxes:
        parts.append(f'<g transform="translate({_number(x)} 0)">{box.body}</g>')
        x += box.width
    return _Box(width, ascent, descent, "".join(parts))


def _fraction(numerator: _Box, denominator: _Box) -> _Box:
    scale = 0.78
    pad = 8.0
    gap = 6.0
    child_width = max(numerator.width, denominator.width) * scale
    width = child_width + pad * 2
    num_ascent, num_descent = numerator.ascent * scale, numerator.descent * scale
    den_ascent, den_descent = denominator.ascent * scale, denominator.descent * scale
    num_baseline = -(gap + num_descent)
    den_baseline = gap + den_ascent
    num_x = (width - numerator.width * scale) / 2
    den_x = (width - denominator.width * scale) / 2
    body = (
        f'<g transform="translate({_number(num_x)} {_number(num_baseline)}) scale({scale})">'
        f'{numerator.body}</g>'
        f'<path d="M 1 0 H {_number(width - 1)}" fill="none"/>'
        f'<g transform="translate({_number(den_x)} {_number(den_baseline)}) scale({scale})">'
        f'{denominator.body}</g>'
    )
    return _Box(
        width,
        gap + num_descent + num_ascent,
        gap + den_ascent + den_descent,
        body,
    )


def _scripts(base: _Box, sub: _Box | None, sup: _Box | None) -> _Box:
    scale = 0.68
    gap = 3.0
    script_width = max(sub.width if sub else 0.0, sup.width if sup else 0.0) * scale
    width = base.width + script_width + (4.0 if script_width else 0.0)
    sup_ascent = sup.ascent * scale if sup else 0.0
    sup_descent = sup.descent * scale if sup else 0.0
    sub_ascent = sub.ascent * scale if sub else 0.0
    sub_descent = sub.descent * scale if sub else 0.0
    sup_baseline = -(base.ascent * 0.72 + sup_descent) if sup else 0.0
    sub_baseline = base.descent + sub_ascent + gap if sub else 0.0
    body = f'<g transform="translate(0 0)">{base.body}</g>'
    if sup is not None:
        body += (
            f'<g transform="translate({_number(base.width + 4)} '
            f'{_number(sup_baseline)}) scale({scale})">{sup.body}</g>'
        )
    if sub is not None:
        body += (
            f'<g transform="translate({_number(base.width + 4)} '
            f'{_number(sub_baseline)}) scale({scale})">{sub.body}</g>'
        )
    ascent = max(base.ascent, -sup_baseline + sup_ascent if sup else 0.0)
    descent = max(base.descent, sub_baseline + sub_descent if sub else 0.0)
    return _Box(width, ascent, descent, body)


def _sqrt(radicand: _Box, index: _Box | None) -> _Box:
    root_x = 10.0 if index is not None else 0.0
    sign_width = 21.0
    inner_x = root_x + sign_width
    ascent = max(radicand.ascent + 5.0, 23.0)
    descent = radicand.descent
    top = -ascent + 2.0
    parts = [
        f'<path d="M {_number(root_x)} -2 l 5 5 4 -{_number(ascent * 0.63)} '
        f'h {_number(max(3.0, radicand.width + 8))}" fill="none" stroke-linecap="round"/>'
    ]
    if index is not None:
        index_baseline = top + index.ascent * 0.55
        parts.append(
            f'<g transform="translate(0 {_number(index_baseline)}) scale(0.55)">'
            f'{index.body}</g>'
        )
    parts.append(
        f'<g transform="translate({_number(inner_x)} 0)">{radicand.body}</g>'
    )
    return _Box(inner_x + radicand.width, ascent, descent, "".join(parts))


def _fenced(element: ET.Element, boxes: list[_Box]) -> _Box:
    opening = element.attrib.get("open", "(")
    closing = element.attrib.get("close", ")")
    separators = element.attrib.get("separators", ",")
    if opening not in {"(", "[", "{"} or closing not in {")", "]", "}"
    }:
        raise ValueError("unsupported MathML fence")
    expected = {"(": ")", "[": "]", "{": "}"}
    if expected[opening] != closing or len(separators) > 4 or any(
        char not in {",", ";", " "} for char in separators
    ):
        raise ValueError("unsupported MathML fence attributes")
    if not boxes:
        raise ValueError("MathML fence is empty")
    fence_left = _token_box(opening)
    fence_right = _token_box(closing)
    pieces = [fence_left]
    for index, box in enumerate(boxes):
        if index and separators:
            separator_index = min(index - 1, len(separators) - 1)
            separator = separators[separator_index]
            if separator != " ":
                pieces.append(_token_box(separator))
        pieces.append(box)
    pieces.append(fence_right)
    return _row(pieces)


def _token_box(text: str) -> _Box:
    escaped = html.escape(text, quote=False)
    return _Box(
        _text_width(text, _FONT_SIZE),
        18.0,
        5.0,
        f'<text x="0" y="0" font-size="{_FONT_SIZE}" font-style="normal" '
        f'fill="#111827" stroke="none">{escaped}</text>',
    )


def _text_width(text: str, size: int) -> float:
    total = 0.0
    for char in text:
        if char.isspace():
            factor = 0.32
        elif char in "il.,:;!|'`[](){}":
            factor = 0.34
        elif char in "MW@%&":
            factor = 0.82
        else:
            factor = 0.58
        total += size * factor
    return max(2.0, total)


def _space_width(value: str) -> float:
    if not value:
        return 8.0
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(em|ex|px)?\s*", value)
    if match:
        amount = float(match.group(1))
        unit = match.group(2) or "em"
        width = amount * {"em": _FONT_SIZE, "ex": _FONT_SIZE * 0.5, "px": 1.0}[unit]
        if math.isfinite(width) and 0 <= width <= 200:
            return width
    named = {"verythinmathspace": 2.0, "thinmathspace": 4.0, "mediummathspace": 8.0,
             "thickmathspace": 12.0, "verythickmathspace": 16.0}
    if value in named:
        return named[value]
    raise ValueError("unsupported MathML spacing")


def _tag_name(tag: str) -> tuple[str, str]:
    if tag.startswith("{") and "}" in tag:
        namespace, name = tag[1:].split("}", 1)
        return name, namespace
    if ":" in tag:
        return tag.rsplit(":", 1)[-1], ""
    return tag, ""


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("invalid SVG coordinate")
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _xml_safe(value: str) -> str:
    def is_xml_char(char: str) -> bool:
        codepoint = ord(char)
        return (
            codepoint in {0x09, 0x0A, 0x0D}
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or (
                0x10000 <= codepoint <= 0x10FFFF
                and codepoint & 0xFFFF not in {0xFFFE, 0xFFFF}
            )
        )

    return "".join(char for char in value if is_xml_char(char))
