"""Bounded exact unit conversion for Full Results.

Conversion factors are exact rationals (plus pi for angles) taken from the definitions in
the table below. Results are exact SymPy arithmetic, not Lean proofs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
import re

from sympy import Expr, Integer, Rational, pi, sstr

from .number_theory import NumberTheoryPod
from .numeric_format import approximate, display_exact, terminating_decimal

# Dimension order: length, mass, time, current, temperature, data, angle
_ZERO = (0, 0, 0, 0, 0, 0, 0)


def _dim(**exponents: int) -> tuple[int, ...]:
    names = ("L", "M", "T", "I", "K", "B", "A")
    return tuple(exponents.get(name, 0) for name in names)


_LENGTH, _MASS, _TIME = _dim(L=1), _dim(M=1), _dim(T=1)
_VOLUME, _AREA = _dim(L=3), _dim(L=2)
_SPEED = _dim(L=1, T=-1)
_ENERGY = _dim(L=2, M=1, T=-2)
_POWER = _dim(L=2, M=1, T=-3)
_FORCE = _dim(L=1, M=1, T=-2)
_PRESSURE = _dim(L=-1, M=1, T=-2)
_DATA, _ANGLE, _FREQUENCY = _dim(B=1), _dim(A=1), _dim(T=-1)

_G0 = Rational("9.80665")
_LBM = Rational("0.45359237")
_FOOT = Rational("0.3048")
_INCH = Rational("0.0254")
_LBF = _LBM * _G0

# name -> (plural display, factor to SI base, dimension, case-insensitive words, case-sensitive symbols)
_DEFINITIONS: tuple[tuple[str, Expr, tuple[int, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    ("meters", Integer(1), _LENGTH, ("meter", "meters", "metre", "metres"), ("m",)),
    ("kilometers", Integer(1000), _LENGTH, ("kilometer", "kilometers", "kilometre", "kilometres"), ("km",)),
    ("centimeters", Rational(1, 100), _LENGTH, ("centimeter", "centimeters", "centimetre", "centimetres"), ("cm",)),
    ("millimeters", Rational(1, 1000), _LENGTH, ("millimeter", "millimeters", "millimetre", "millimetres"), ("mm",)),
    ("micrometers", Rational(1, 10**6), _LENGTH, ("micrometer", "micrometers", "micron", "microns"), ("um",)),
    ("nanometers", Rational(1, 10**9), _LENGTH, ("nanometer", "nanometers"), ("nm",)),
    ("miles", Rational("1609.344"), _LENGTH, ("mile", "miles"), ("mi",)),
    ("yards", Rational("0.9144"), _LENGTH, ("yard", "yards"), ("yd",)),
    ("feet", _FOOT, _LENGTH, ("foot", "feet"), ("ft",)),
    ("inches", _INCH, _LENGTH, ("inch", "inches"), ("in",)),
    ("nautical miles", Integer(1852), _LENGTH, ("nautical mile", "nautical miles"), ("nmi",)),
    ("astronomical units", Integer(149597870700), _LENGTH, ("astronomical unit", "astronomical units"), ("au",)),
    ("grams", Rational(1, 1000), _MASS, ("gram", "grams"), ("g",)),
    ("kilograms", Integer(1), _MASS, ("kilogram", "kilograms"), ("kg",)),
    ("milligrams", Rational(1, 10**6), _MASS, ("milligram", "milligrams"), ("mg",)),
    ("metric tons", Integer(1000), _MASS, ("tonne", "tonnes", "metric ton", "metric tons"), ()),
    ("pounds", _LBM, _MASS, ("pound", "pounds"), ("lb", "lbs")),
    ("ounces", _LBM / 16, _MASS, ("ounce", "ounces"), ("oz",)),
    ("short tons", _LBM * 2000, _MASS, ("ton", "tons", "short ton", "short tons"), ()),
    ("seconds", Integer(1), _TIME, ("second", "seconds", "sec", "secs"), ("s",)),
    ("milliseconds", Rational(1, 1000), _TIME, ("millisecond", "milliseconds"), ("ms",)),
    ("minutes", Integer(60), _TIME, ("minute", "minutes", "min", "mins"), ()),
    ("hours", Integer(3600), _TIME, ("hour", "hours", "hr", "hrs"), ("h",)),
    ("days", Integer(86400), _TIME, ("day", "days"), ()),
    ("weeks", Integer(604800), _TIME, ("week", "weeks"), ()),
    ("liters", Rational(1, 1000), _VOLUME, ("liter", "liters", "litre", "litres"), ("l", "L")),
    ("milliliters", Rational(1, 10**6), _VOLUME, ("milliliter", "milliliters", "millilitre", "millilitres"), ("ml", "mL")),
    ("US gallons", Rational("0.003785411784"), _VOLUME, ("gallon", "gallons", "us gallon", "us gallons"), ("gal",)),
    ("US quarts", Rational("0.000946352946"), _VOLUME, ("quart", "quarts"), ("qt",)),
    ("US pints", Rational("0.000473176473"), _VOLUME, ("pint", "pints"), ("pt",)),
    ("US cups", Rational("0.0002365882365"), _VOLUME, ("cup", "cups"), ()),
    ("US fluid ounces", Rational("0.0000295735295625"), _VOLUME, ("fluid ounce", "fluid ounces"), ("floz",)),
    ("tablespoons", Rational("0.000014786764781250"), _VOLUME, ("tablespoon", "tablespoons"), ("tbsp",)),
    ("teaspoons", Rational("0.00000492892159375"), _VOLUME, ("teaspoon", "teaspoons"), ("tsp",)),
    ("acres", Rational("4046.8564224"), _AREA, ("acre", "acres"), ()),
    ("hectares", Integer(10000), _AREA, ("hectare", "hectares"), ("ha",)),
    ("miles per hour", Rational("1609.344") / 3600, _SPEED, ("mph", "miles per hour"), ()),
    ("kilometers per hour", Rational(1000, 3600), _SPEED, ("kph", "kmh", "kilometers per hour"), ()),
    ("knots", Rational(1852, 3600), _SPEED, ("knot", "knots", "kt", "kts"), ()),
    ("joules", Integer(1), _ENERGY, ("joule", "joules"), ("J",)),
    ("kilojoules", Integer(1000), _ENERGY, ("kilojoule", "kilojoules"), ("kJ",)),
    ("calories", Rational("4.184"), _ENERGY, ("calorie", "calories", "cal"), ()),
    ("kilocalories", Integer(4184), _ENERGY, ("kilocalorie", "kilocalories", "kcal"), ("Cal",)),
    ("kilowatt hours", Integer(3600000), _ENERGY, ("kilowatt hour", "kilowatt hours", "kwh"), ()),
    ("electronvolts", Rational("1.602176634") / 10**19, _ENERGY, ("electronvolt", "electronvolts"), ("eV",)),
    ("BTU", Rational("1055.05585262"), _ENERGY, ("btu", "btus"), ()),
    ("watts", Integer(1), _POWER, ("watt", "watts"), ("W",)),
    ("kilowatts", Integer(1000), _POWER, ("kilowatt", "kilowatts"), ("kW",)),
    ("megawatts", Integer(10**6), _POWER, ("megawatt", "megawatts"), ("MW",)),
    ("horsepower", 33000 * _FOOT * _LBF / 60, _POWER, ("horsepower", "hp"), ()),
    ("newtons", Integer(1), _FORCE, ("newton", "newtons"), ("N",)),
    ("kilonewtons", Integer(1000), _FORCE, ("kilonewton", "kilonewtons"), ("kN",)),
    ("pounds-force", _LBF, _FORCE, ("pound-force", "pounds-force", "lbf"), ()),
    ("pascals", Integer(1), _PRESSURE, ("pascal", "pascals"), ("Pa",)),
    ("kilopascals", Integer(1000), _PRESSURE, ("kilopascal", "kilopascals"), ("kPa",)),
    ("bars", Integer(100000), _PRESSURE, ("bar", "bars"), ()),
    ("atmospheres", Integer(101325), _PRESSURE, ("atmosphere", "atmospheres", "atm"), ()),
    ("psi", _LBF / _INCH**2, _PRESSURE, ("psi",), ()),
    ("hertz", Integer(1), _FREQUENCY, ("hertz",), ("Hz",)),
    ("kilohertz", Integer(1000), _FREQUENCY, ("kilohertz",), ("kHz",)),
    ("megahertz", Integer(10**6), _FREQUENCY, ("megahertz",), ("MHz",)),
    ("gigahertz", Integer(10**9), _FREQUENCY, ("gigahertz",), ("GHz",)),
    ("bits", Rational(1, 8), _DATA, ("bit", "bits"), ()),
    ("bytes", Integer(1), _DATA, ("byte", "bytes"), ("B",)),
    ("kilobytes", Integer(1000), _DATA, ("kilobyte", "kilobytes"), ("kB", "KB")),
    ("megabytes", Integer(10**6), _DATA, ("megabyte", "megabytes"), ("MB",)),
    ("gigabytes", Integer(10**9), _DATA, ("gigabyte", "gigabytes"), ("GB",)),
    ("terabytes", Integer(10**12), _DATA, ("terabyte", "terabytes"), ("TB",)),
    ("kibibytes", Integer(2**10), _DATA, ("kibibyte", "kibibytes"), ("KiB",)),
    ("mebibytes", Integer(2**20), _DATA, ("mebibyte", "mebibytes"), ("MiB",)),
    ("gibibytes", Integer(2**30), _DATA, ("gibibyte", "gibibytes"), ("GiB",)),
    ("radians", Integer(1), _ANGLE, ("radian", "radians", "rad"), ()),
    ("degrees", pi / 180, _ANGLE, ("degree", "degrees", "deg"), ("°",)),
    ("turns", 2 * pi, _ANGLE, ("turn", "turns", "revolution", "revolutions"), ()),
)
_WORDS: dict[str, tuple[Expr, tuple[int, ...]]] = {}
_SYMBOLS: dict[str, tuple[Expr, tuple[int, ...]]] = {}
for _name, _factor, _dimension, _words, _symbols in _DEFINITIONS:
    for _word in _words:
        _WORDS[_word] = (_factor, _dimension)
    for _symbol in _symbols:
        _SYMBOLS[_symbol] = (_factor, _dimension)

_TEMPERATURES = {
    "celsius": "C", "centigrade": "C", "c": "C", "°c": "C", "degrees celsius": "C",
    "degree celsius": "C", "deg c": "C",
    "fahrenheit": "F", "f": "F", "°f": "F", "degrees fahrenheit": "F",
    "degree fahrenheit": "F", "deg f": "F",
    "kelvin": "K", "kelvins": "K", "k": "K",
}
_TEMPERATURE_NAMES = {"C": "degrees Celsius", "F": "degrees Fahrenheit", "K": "kelvins"}
_NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d{1,3})?(?:\s*/\s*\d+)?"
_MAX_EXPONENT = 3


class UnitConversionError(ValueError):
    """A recognized conversion is dimensionally invalid or outside the service bounds."""


@dataclass(frozen=True)
class UnitConversionResult:
    operation: str
    arguments: tuple[str, ...]
    input_text: str
    result_text: str
    extra_pods: tuple[NumberTheoryPod, ...] = field(default_factory=tuple)
    kind: str = "unit_conversion_result"
    scanner: str = "UnitConversion"


def evaluate_unit_conversion(source: str) -> UnitConversionResult | None:
    query = re.sub(r"\s+", " ", source.strip().rstrip("?.! ").strip())
    if not query or len(query) > 200:
        return None
    matched = _match(query)
    if matched is None:
        return None
    amount_text, source_text, target_text = matched
    amount = _parse_number(amount_text)
    if amount is None:
        return None
    temperature = _convert_temperature(amount, source_text, target_text)
    if temperature is not None:
        return temperature
    source_unit = _parse_unit(source_text)
    target_unit = _parse_unit(target_text)
    if source_unit is None or target_unit is None:
        return None
    if source_unit[1] != target_unit[1]:
        raise UnitConversionError(
            f"cannot convert {source_text} to {target_text}: the units measure different quantities"
        )
    value = amount * source_unit[0] / target_unit[0]
    return _result(amount_text, source_text, target_text, value)


def _match(query: str) -> tuple[str, str, str] | None:
    body = re.sub(
        r"^(?:please\s+)?(?:(?:what\s+is|what's|convert|calculate|compute)\s+)?", "", query,
        count=1, flags=re.IGNORECASE,
    )
    how_many = re.fullmatch(
        rf"how\s+many\s+(.+?)\s+(?:are\s+)?(?:there\s+)?(?:in|are\s+in)\s+"
        rf"(?:(?P<n>{_NUMBER})|(?:a|an|one))\s*(?P<unit>.+)",
        body, re.IGNORECASE,
    )
    if how_many:
        return how_many.group("n") or "1", how_many.group("unit").strip(), how_many.group(1).strip()
    plain = re.fullmatch(
        rf"(?:(?P<n>{_NUMBER})|(?:a|an|one))\s*(?P<source>[^\d].*?)\s+"
        rf"(?:to|in|into|as|->|=)\s+(?P<target>.+)",
        body, re.IGNORECASE,
    )
    if plain:
        return plain.group("n") or "1", plain.group("source").strip(), plain.group("target").strip()
    return None


def _parse_number(text: str) -> Rational | None:
    try:
        cleaned = text.replace(" ", "")
        if "/" in cleaned:
            numerator, denominator = cleaned.split("/", 1)
            if Rational(denominator) == 0:
                return None
            return Rational(numerator) / Rational(denominator)
        value = Rational(cleaned)
        return value if abs(value.p) < 10**60 and value.q < 10**60 else None
    except (ValueError, TypeError):
        return None


def _lookup(name: str) -> tuple[Expr, tuple[int, ...]] | None:
    name = name.strip()
    if name in _SYMBOLS:
        return _SYMBOLS[name]
    lowered = name.casefold()
    return _WORDS.get(lowered)


def _parse_unit(text: str) -> tuple[Expr, tuple[int, ...]] | None:
    text = text.strip().casefold() if text.strip().casefold() in _WORDS else text.strip()
    whole = _lookup(text)
    if whole is not None:
        return whole
    factor: Expr = Integer(1)
    dimension = list(_ZERO)
    parts = re.split(r"\s*(/|\bper\b|\*|·)\s*", text)
    sign = 1
    expecting_unit = True
    for part in parts:
        if part in {"/", "per"}:
            sign = -1
            continue
        if part in {"*", "·"} or not part:
            continue
        match = re.fullmatch(r"(.+?)\s*(?:\^|\*\*)\s*(-?\d)|(.+?)\s+(squared|cubed)|(.+?)([²³])", part)
        exponent = 1
        name = part
        if match:
            if match.group(1):
                name, exponent = match.group(1), int(match.group(2))
            elif match.group(3):
                name, exponent = match.group(3), 2 if match.group(4) == "squared" else 3
            else:
                name, exponent = match.group(5), 2 if match.group(6) == "²" else 3
        if abs(exponent) > _MAX_EXPONENT or exponent == 0:
            return None
        unit = _lookup(name)
        if unit is None:
            return None
        factor *= unit[0] ** (sign * exponent)
        for index, power in enumerate(unit[1]):
            dimension[index] += sign * exponent * power
    return factor, tuple(dimension)


def _convert_temperature(amount: Rational, source: str, target: str) -> UnitConversionResult | None:
    left = _TEMPERATURES.get(source.strip().casefold())
    right = _TEMPERATURES.get(target.strip().casefold())
    if left is None or right is None:
        return None
    kelvin = {
        "C": amount + Rational("273.15"),
        "F": (amount - 32) * Rational(5, 9) + Rational("273.15"),
        "K": amount,
    }[left]
    if kelvin < 0:
        raise UnitConversionError("temperature is below absolute zero")
    value = {
        "C": kelvin - Rational("273.15"),
        "F": (kelvin - Rational("273.15")) * Rational(9, 5) + 32,
        "K": kelvin,
    }[right]
    return _result(
        str(amount), source, target, value,
        target_label=_TEMPERATURE_NAMES[right],
    )


def _result(
    amount_text: str, source: str, target: str, value: Expr, *, target_label: str | None = None
) -> UnitConversionResult:
    label = target_label or target
    exact = display_exact(value)
    is_exact_decimal = not isinstance(value, Rational) or value.q == 1 or terminating_decimal(value) is not None
    extras = []
    if is_exact_decimal:
        shown = f"{exact} {label}"
    else:
        shown = f"≈ {approximate(value)} {label}"
        extras.append(NumberTheoryPod("ExactResult", "Exact result", f"{sstr(value).replace('**', '^')} {label}"))
    return UnitConversionResult(
        "convert",
        (amount_text, source, target),
        f"convert {amount_text.strip()} {source} to {target}",
        shown,
        tuple(extras),
    )
