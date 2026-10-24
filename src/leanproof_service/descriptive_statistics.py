"""Bounded exact descriptive statistics for lists of rational numbers."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import re

from sympy import Expr, Integer, Rational, sqrt, sstr

from .number_theory import NumberTheoryPod
from .numeric_format import approximate, display_exact

_MAX_VALUES = 1000
_NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:\s*/\s*\d+)?"
_STATISTICS = {
    "mean": "mean", "average": "mean", "arithmetic mean": "mean",
    "median": "median", "mode": "mode", "modes": "mode",
    "variance": "variance", "sample variance": "variance",
    "population variance": "population_variance",
    "standard deviation": "stdev", "std dev": "stdev", "stdev": "stdev",
    "sample standard deviation": "stdev",
    "population standard deviation": "population_stdev",
    "minimum": "min", "min": "min", "maximum": "max", "max": "max",
    "range": "range", "sum": "sum", "total": "sum", "count": "count",
    "geometric mean": "geometric_mean", "harmonic mean": "harmonic_mean",
    "statistics": "summary", "summary statistics": "summary",
}


class StatisticsError(ValueError):
    """A recognized statistics request is undefined or outside the service bounds."""


@dataclass(frozen=True)
class StatisticsResult:
    operation: str
    arguments: tuple[str, ...]
    input_text: str
    result_text: str
    extra_pods: tuple[NumberTheoryPod, ...] = field(default_factory=tuple)
    kind: str = "descriptive_statistics_result"
    scanner: str = "Statistics"


def evaluate_statistics(source: str) -> StatisticsResult | None:
    query = re.sub(r"\s+", " ", source.strip().rstrip("?.! ").strip())
    if not query or len(query) > 8000:
        return None
    body = re.sub(
        r"^(?:please\s+)?(?:(?:what\s+is|what's|calculate|compute|find|determine)\s+)?(?:the\s+)?",
        "", query, count=1, flags=re.IGNORECASE,
    )
    candidates = (
        re.fullmatch(r"([A-Za-z ]+?)\s+(?:of|for)\s+(.+)", body, re.IGNORECASE),
        re.fullmatch(r"([A-Za-z ]+?)\s*[(\[{]\s*(.+?)\s*[)\]}]", body),
    )
    for match in candidates:
        if match is None:
            continue
        operation = _STATISTICS.get(re.sub(r"\s+", " ", match.group(1).strip().lower()))
        values = _values(match.group(2)) if operation is not None else None
        if operation is not None and values is not None:
            break
    else:
        return None
    if len(values) > _MAX_VALUES:
        raise StatisticsError(f"statistics are limited to {_MAX_VALUES} values")
    return _compute(operation, values)


def _values(text: str) -> list[Rational] | None:
    inner = text.strip()
    inner = re.sub(r"^[({\[]\s*|\s*[)}\]]$", "", inner)
    parts = [part for part in re.split(r"\s*(?:,|;|\band\b|\s)\s*", inner) if part]
    if not parts or not all(re.fullmatch(_NUMBER, part) for part in parts):
        return None
    result = []
    for part in parts:
        if "/" in part:
            numerator, denominator = part.replace(" ", "").split("/", 1)
            if int(denominator) == 0:
                raise StatisticsError("division by zero in a data value")
            result.append(Rational(numerator) / Rational(denominator))
        else:
            result.append(Rational(part))
    return result


def _shown(value: Expr) -> str:
    return display_exact(value)


def _statistic(operation: str, data: list[Rational]) -> Expr:
    n = len(data)
    total = sum(data, Integer(0))
    mean = total / n
    ordered = sorted(data)
    if operation == "mean":
        return mean
    if operation == "sum":
        return total
    if operation == "count":
        return Integer(n)
    if operation == "min":
        return ordered[0]
    if operation == "max":
        return ordered[-1]
    if operation == "range":
        return ordered[-1] - ordered[0]
    if operation == "median":
        middle = n // 2
        return ordered[middle] if n % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    if operation in {"variance", "stdev", "population_variance", "population_stdev"}:
        population = operation.startswith("population")
        if n < 2 and not population:
            raise StatisticsError("sample variance needs at least two values")
        squares = sum(((value - mean) ** 2 for value in data), Integer(0))
        variance = squares / (n if population else n - 1)
        return sqrt(variance) if operation.endswith("stdev") else variance
    if operation == "geometric_mean":
        if any(value <= 0 for value in data):
            raise StatisticsError("geometric mean requires positive values")
        product = Integer(1)
        for value in data:
            product *= value
        return product ** Rational(1, n)
    if operation == "harmonic_mean":
        if any(value <= 0 for value in data):
            raise StatisticsError("harmonic mean requires positive values")
        return n / sum((1 / value for value in data), Integer(0))
    raise StatisticsError("unsupported statistic")


def _compute(operation: str, data: list[Rational]) -> StatisticsResult:
    listed = "{" + ", ".join(sstr(value) for value in data[:20]) + (", …" if len(data) > 20 else "") + "}"
    arguments = tuple(sstr(value) for value in data)
    if operation == "mode":
        counts = Counter(data)
        top = max(counts.values())
        modes = sorted(value for value, count in counts.items() if count == top)
        text = "no unique mode (all values occur once)" if top == 1 and len(counts) > 1 else (
            "{" + ", ".join(_shown(mode) for mode in modes) + "}"
        )
        return StatisticsResult("mode", arguments, f"mode of {listed}", text)
    if operation == "summary":
        names = ("mean", "median", "min", "max", "range", "sum", "count")
        lines = [f"{name}: {_shown(_statistic(name, data))}" for name in names]
        if len(data) > 1:
            lines.append(f"sample standard deviation: {_shown(_statistic('stdev', data))}")
        return StatisticsResult("summary", arguments, f"statistics of {listed}", "\n".join(lines))
    value = _statistic(operation, data)
    extras = []
    if not isinstance(value, (Integer,)) and not (isinstance(value, Rational) and _shown(value) != sstr(value)):
        extras.append(NumberTheoryPod("DecimalApproximation", "Decimal approximation", approximate(value)))
    if operation in {"variance", "stdev"} and len(data) > 1:
        population = _statistic(f"population_{operation}", data)
        extras.append(NumberTheoryPod(
            "PopulationValue", f"Population {'variance' if operation == 'variance' else 'standard deviation'}",
            _shown(population),
        ))
    label = operation.replace("_", " ").replace("stdev", "standard deviation")
    return StatisticsResult(operation, arguments, f"{label} of {listed}", _shown(value), tuple(extras))
