"""Bounded SVG and text presentations for Open-Meteo daily forecasts."""

from __future__ import annotations

import base64
import html
import math
import re
from datetime import datetime
from typing import Any


def forecast_plaintext(data: Any) -> str | None:
    rows = _forecast_rows(data)
    if rows is None:
        return None
    daily_units = _daily_units(data)
    temperature_unit = _unit(daily_units.get("temperature_2m_max"), {"°C", "°F"}, "°C")
    precipitation_unit = _unit(daily_units.get("precipitation_sum"), {"mm", "inch"}, "mm")
    lines = [f"7-day weather forecast ({temperature_unit}, {precipitation_unit})"]
    for day in rows:
        precipitation = (
            f"{day['precipitation']:.1f} {precipitation_unit} precipitation"
            if day["precipitation"] is not None
            else "precipitation unavailable"
        )
        lines.append(
            f"{day['date']}: low {day['minimum']:.1f}{temperature_unit}, "
            f"high {day['maximum']:.1f}{temperature_unit}, {precipitation}"
        )
    return "\n".join(lines)


def forecast_chart_image(data: Any, *, width: int = 200) -> dict[str, Any] | None:
    rows = _forecast_rows(data)
    if rows is None or not 100 <= width <= 1600:
        return None

    daily_units = _daily_units(data)
    temperature_unit = _unit(daily_units.get("temperature_2m_max"), {"°C", "°F"}, "°C")
    precipitation_unit = _unit(daily_units.get("precipitation_sum"), {"mm", "inch"}, "mm")
    height = 250
    left, right = 32.0, float(width - 10)
    top, temp_bottom = 54.0, 137.0
    precip_top, precip_bottom = 177.0, 219.0
    plot_width = max(1.0, right - left)
    lows = [day["minimum"] for day in rows]
    highs = [day["maximum"] for day in rows]
    temp_min = min(lows)
    temp_max = max(highs)
    if temp_min == temp_max:
        temp_min -= 1.0
        temp_max += 1.0
    temp_pad = (temp_max - temp_min) * 0.08
    temp_min -= temp_pad
    temp_max += temp_pad

    def x_at(index: int) -> float:
        if len(rows) == 1:
            return left + plot_width / 2
        return left + index * plot_width / (len(rows) - 1)

    def y_temp(value: float) -> float:
        return temp_bottom - (value - temp_min) * (temp_bottom - top) / (temp_max - temp_min)

    temp_ticks = [temp_min + (temp_max - temp_min) * step / 2 for step in range(3)]
    grid = "".join(
        f'<path d="M {left:.2f} {y_temp(value):.2f} H {right:.2f}" '
        'fill="none" stroke="#e5e7eb" stroke-width="1"/>'
        for value in temp_ticks
    )
    max_points = " ".join(
        f"{x_at(index):.2f},{y_temp(day['maximum']):.2f}"
        for index, day in enumerate(rows)
    )
    min_points = " ".join(
        f"{x_at(index):.2f},{y_temp(day['minimum']):.2f}"
        for index, day in enumerate(rows)
    )
    markers = "".join(
        f'<circle cx="{x_at(index):.2f}" cy="{y_temp(day["maximum"]):.2f}" '
        'r="2.6" fill="#dc2626" stroke="none"/>'
        f'<circle cx="{x_at(index):.2f}" cy="{y_temp(day["minimum"]):.2f}" '
        'r="2.6" fill="#2563eb" stroke="none"/>'
        for index, day in enumerate(rows)
    )
    precipitation = [day["precipitation"] or 0.0 for day in rows]
    max_precipitation = max(precipitation) or 1.0
    bar_width = max(2.0, min(12.0, plot_width / max(len(rows) * 2, 1)))
    bars = "".join(
        f'<rect x="{x_at(index) - bar_width / 2:.2f}" '
        f'y="{precip_bottom - value / max_precipitation * (precip_bottom - precip_top):.2f}" '
        f'width="{bar_width:.2f}" '
        f'height="{value / max_precipitation * (precip_bottom - precip_top):.2f}" '
        'rx="1.5" fill="#60a5fa"/>'
        for index, value in enumerate(precipitation)
    )
    label_stride = max(1, math.ceil(26 / max(plot_width / max(len(rows) - 1, 1), 1)))
    labels = "".join(
        f'<text x="{x_at(index):.2f}" y="{height - 14}" text-anchor="middle" '
        'font-size="9" fill="#374151" stroke="none">'
        f'{html.escape(day["weekday"], quote=False)}</text>'
        for index, day in enumerate(rows)
        if index % label_stride == 0 or index == len(rows) - 1
    )
    escaped_temp_unit = html.escape(temperature_unit, quote=False)
    escaped_precip_unit = html.escape(precipitation_unit, quote=False)
    alt = (
        "Seven-day weather forecast chart showing daily low and high temperatures "
        f"in {temperature_unit} and precipitation in {precipitation_unit}."
    )
    escaped_alt = html.escape(alt, quote=True)
    if width < 180:
        legend = (
            '<path d="M 10 36 h 8" stroke="#dc2626" stroke-width="2"/>'
            '<text x="20" y="39" font-size="8" font-family="sans-serif" '
            'fill="#374151" stroke="none">Hi</text>'
            '<path d="M 46 36 h 8" stroke="#2563eb" stroke-width="2"/>'
            '<text x="56" y="39" font-size="8" font-family="sans-serif" '
            'fill="#374151" stroke="none">Lo</text>'
            '<rect x="81" y="32" width="7" height="7" fill="#60a5fa"/>'
            '<text x="89" y="39" font-size="8" font-family="sans-serif" '
            'fill="#374151" stroke="none">P</text>'
        )
    else:
        legend = (
            '<path d="M 12 36 h 10" stroke="#dc2626" stroke-width="2"/>'
            '<text x="25" y="39" font-size="9" font-family="sans-serif" '
            'fill="#374151" stroke="none">High</text>'
            '<path d="M 68 36 h 10" stroke="#2563eb" stroke-width="2"/>'
            '<text x="81" y="39" font-size="9" font-family="sans-serif" '
            'fill="#374151" stroke="none">Low</text>'
            '<rect x="116" y="31" width="8" height="8" fill="#60a5fa"/>'
            '<text x="127" y="39" font-size="9" font-family="sans-serif" '
            'fill="#374151" stroke="none">Precip</text>'
        )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escaped_alt}">'
        f'<title>{html.escape(alt, quote=False)}</title>'
        '<rect width="100%" height="100%" fill="#ffffff"/>'
        '<text x="10" y="20" font-size="12" font-weight="600" '
        'font-family="sans-serif" fill="#111827" stroke="none">7-day forecast</text>'
        f'{legend}'
        f'<text x="4" y="{top + 4:.0f}" font-size="8" font-family="sans-serif" '
        f'fill="#6b7280" stroke="none">{escaped_temp_unit}</text>'
        f'{grid}'
        f'<polyline points="{max_points}" fill="none" stroke="#dc2626" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        f'<polyline points="{min_points}" fill="none" stroke="#2563eb" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        f'{markers}'
        f'<path d="M {left:.2f} {precip_top:.2f} H {right:.2f}" '
        'fill="none" stroke="#d1d5db" stroke-width="1"/>'
        f'{bars}'
        f'<text x="4" y="{precip_top + 9:.0f}" font-size="8" font-family="sans-serif" '
        f'fill="#6b7280" stroke="none">{escaped_precip_unit}</text>'
        f'{labels}</svg>'
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return {
        "src": f"data:image/svg+xml;base64,{encoded}",
        "alt": alt,
        "title": alt,
        "width": width,
        "height": height,
    }


def _forecast_rows(data: Any) -> list[dict[str, Any]] | None:
    if not isinstance(data, dict):
        return None
    daily = data.get("daily")
    if not isinstance(daily, dict):
        return None
    dates = daily.get("time")
    minimums = daily.get("temperature_2m_min")
    maximums = daily.get("temperature_2m_max")
    precipitation = daily.get("precipitation_sum")
    if not all(isinstance(values, list) for values in (dates, minimums, maximums, precipitation)):
        return None
    lengths = {len(dates), len(minimums), len(maximums), len(precipitation)}
    if len(lengths) != 1 or not dates or len(dates) > 7:
        return None

    rows: list[dict[str, Any]] = []
    for date_value, minimum_value, maximum_value, precipitation_value in zip(
        dates, minimums, maximums, precipitation
    ):
        if not isinstance(date_value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value):
            return None
        try:
            weekday = datetime.strptime(date_value, "%Y-%m-%d").strftime("%a")
        except ValueError:
            return None
        minimum = _finite_number(minimum_value, lower=-150, upper=200)
        maximum = _finite_number(maximum_value, lower=-150, upper=200)
        rain = _finite_number(precipitation_value, lower=0, upper=10_000, allow_none=True)
        if minimum is None or maximum is None or minimum > maximum:
            return None
        if precipitation_value is not None and rain is None:
            return None
        rows.append({
            "date": date_value,
            "weekday": weekday,
            "minimum": minimum,
            "maximum": maximum,
            "precipitation": rain,
        })
    return rows


def _finite_number(
    value: Any,
    *,
    lower: float,
    upper: float,
    allow_none: bool = False,
) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or not lower <= result <= upper:
        return None
    return result


def _daily_units(data: dict[str, Any]) -> dict[str, Any]:
    units = data.get("daily_units")
    return units if isinstance(units, dict) else {}


def _unit(value: Any, allowed: set[str], fallback: str) -> str:
    if value in allowed:
        return value
    return fallback
