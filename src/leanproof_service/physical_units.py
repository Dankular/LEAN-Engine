"""Curated, explicit physical-unit vocabulary shared by symbolic paths."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PhysicalUnit:
    name: str
    display: str
    sympy_name: str
    wolfram_name: str
    aliases: tuple[str, ...]


# Quantity parsing avoids one-letter aliases so forms like `3 x` and `10 g`
# retain the symbolic/list interpretations unless a unit is spelled out.
_UNITS = (
    PhysicalUnit(
        "meter", "meters", "meter", "Meters",
        ("meter", "meters", "metre", "metres"),
    ),
    PhysicalUnit(
        "kilometer", "kilometers", "kilometer", "Kilometers",
        ("km", "kilometer", "kilometers", "kilometre", "kilometres"),
    ),
    PhysicalUnit(
        "centimeter", "centimeters", "centimeter", "Centimeters",
        ("cm", "centimeter", "centimeters", "centimetre", "centimetres"),
    ),
    PhysicalUnit(
        "millimeter", "millimeters", "millimeter", "Millimeters",
        ("mm", "millimeter", "millimeters", "millimetre", "millimetres"),
    ),
    PhysicalUnit("mile", "miles", "mile", "Miles", ("mi", "mile", "miles")),
    PhysicalUnit("foot", "feet", "foot", "Feet", ("ft", "foot", "feet")),
    PhysicalUnit("inch", "inches", "inch", "Inches", ("in", "inch", "inches")),
    PhysicalUnit("yard", "yards", "yard", "Yards", ("yd", "yard", "yards")),
    PhysicalUnit(
        "second", "seconds", "second", "Seconds",
        ("sec", "secs", "second", "seconds"),
    ),
    PhysicalUnit("minute", "minutes", "minute", "Minutes", ("min", "minute", "minutes")),
    PhysicalUnit(
        "hour", "hours", "hour", "Hours", ("hr", "hrs", "hour", "hours")
    ),
    PhysicalUnit("day", "days", "day", "Days", ("day", "days")),
    PhysicalUnit("gram", "grams", "gram", "Grams", ("gram", "grams")),
    PhysicalUnit(
        "kilogram", "kilograms", "kilogram", "Kilograms", ("kg", "kilogram", "kilograms")
    ),
    PhysicalUnit("pound", "pounds", "pound", "Pounds", ("lb", "lbs", "pound", "pounds")),
    PhysicalUnit(
        "liter", "liters", "liter", "Liters", ("liter", "liters", "litre", "litres")
    ),
    PhysicalUnit(
        "milliliter", "milliliters", "milliliter", "Milliliters",
        ("ml", "milliliter", "milliliters", "millilitre", "millilitres"),
    ),
    PhysicalUnit("hertz", "hertz", "hertz", "Hertz", ("hz", "hertz")),
    PhysicalUnit("newton", "newtons", "newton", "Newtons", ("newton", "newtons")),
    PhysicalUnit("joule", "joules", "joule", "Joules", ("joule", "joules")),
    PhysicalUnit("watt", "watts", "watt", "Watts", ("watt", "watts")),
    PhysicalUnit("pascal", "pascals", "pascal", "Pascals", ("pa", "pascal", "pascals")),
    PhysicalUnit("kelvin", "kelvins", "kelvin", "Kelvins", ("kelvin", "kelvins")),
    PhysicalUnit("degree", "degrees", "degree", "Degrees", ("deg", "degree", "degrees")),
    PhysicalUnit("radian", "radians", "radian", "Radians", ("rad", "radian", "radians")),
)

PHYSICAL_UNITS = {unit.name: unit for unit in _UNITS}
UNIT_ALIAS_TO_NAME = {
    alias.casefold(): unit.name
    for unit in _UNITS
    for alias in unit.aliases
}
UNIT_ALIASES_BY_LENGTH = tuple(
    sorted(UNIT_ALIAS_TO_NAME, key=lambda alias: (-len(alias), alias))
)
SUPPORTED_UNIT_NAMES = (*PHYSICAL_UNITS, "us_minim")
SUPPORTED_WOLFRAM_UNIT_NAMES = frozenset(
    {unit.wolfram_name for unit in _UNITS} | {"MinimsUS"}
)
