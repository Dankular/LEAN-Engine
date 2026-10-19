from __future__ import annotations

import ast
import json
import math
import os
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from time import perf_counter
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from .input_syntax import MathInputError, parse_math_input
from .models import StrictModel
from .physical_units import PHYSICAL_UNITS


SymbolName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$")]
AssumptionName = Literal[
    "real", "positive", "nonnegative", "negative", "nonpositive", "integer", "rational"
]
UnitName = Literal[
    "meter", "kilometer", "centimeter", "millimeter", "mile", "foot", "inch", "yard",
    "second", "minute", "hour", "day", "gram", "kilogram", "pound", "liter",
    "milliliter", "hertz", "newton", "joule", "watt", "pascal", "kelvin", "degree",
    "radian", "us_minim",
]
_DECIMAL_LITERAL = re.compile(
    r"(?<![A-Za-z0-9_])(?:(?:\d(?:_?\d)*\.\d(?:_?\d)*|"
    r"\d(?:_?\d)*\.|\.\d(?:_?\d)*)(?:[eE][+-]?\d(?:_?\d)*)?|"
    r"\d(?:_?\d)*[eE][+-]?\d(?:_?\d)*)"
)
_MAX_DECIMAL_BITS = 256
SymbolicOperation = Literal[
    "validate", "simplify", "factor", "expand", "differentiate", "integrate",
    "limit", "series", "sum", "product", "solve", "identity", "plot",
]


class SymbolicRequest(StrictModel):
    operation: SymbolicOperation
    expression: str | None = Field(default=None, min_length=1, max_length=4000)
    lhs: str | None = Field(default=None, min_length=1, max_length=4000)
    rhs: str | None = Field(default=None, min_length=1, max_length=4000)
    variable: SymbolName | None = None
    lower: str | None = Field(default=None, min_length=1, max_length=256)
    upper: str | None = Field(default=None, min_length=1, max_length=256)
    plot_width: int = Field(default=720, ge=100, le=1600)
    point: str | None = Field(default=None, min_length=1, max_length=256)
    order: int | None = Field(default=None, strict=True, ge=1, le=32)
    assumptions: dict[SymbolName, list[AssumptionName]] = Field(default_factory=dict, max_length=64)
    named_units: dict[SymbolName, UnitName] = Field(default_factory=dict, max_length=16)
    time_limit_seconds: float = Field(default=5.0, ge=0.001, le=60.0)
    numeric_precision: int = Field(default=50, ge=10, le=500)

    @model_validator(mode="after")
    def validate_operation_fields(self) -> SymbolicRequest:
        if self.operation == "identity":
            if self.expression is not None or self.lhs is None or self.rhs is None:
                raise ValueError("identity requires lhs and rhs and does not accept expression")
            if self.variable is not None or self.lower is not None or self.upper is not None:
                raise ValueError("identity does not accept variable or integration bounds")
        else:
            if self.expression is None or self.lhs is not None or self.rhs is not None:
                raise ValueError("this operation requires expression and does not accept lhs/rhs")
        if self.operation == "validate":
            if self.variable is not None or self.lower is not None or self.upper is not None:
                raise ValueError("validate does not accept a variable or integration bounds")
            if self.assumptions:
                raise ValueError("validate does not accept symbolic assumptions")
        variable_operations = {
            "differentiate", "integrate", "limit", "series", "sum", "product", "solve", "plot"
        }
        if self.operation in variable_operations and self.variable is None:
            raise ValueError(f"{self.operation} requires variable")
        if self.operation not in variable_operations and self.variable is not None:
            raise ValueError("variable is only used by differentiate, integrate, limit, series, sum, product, solve, or plot")
        if self.operation == "integrate":
            if (self.lower is None) != (self.upper is None):
                raise ValueError("definite integration requires both lower and upper")
        elif self.operation in {"sum", "product", "plot"}:
            if self.lower is None or self.upper is None:
                raise ValueError(f"{self.operation} requires lower and upper bounds")
        elif self.lower is not None or self.upper is not None:
            raise ValueError("lower and upper are only supported by integrate, sum, product, or plot")
        if self.operation in {"limit", "series"}:
            if self.point is None:
                raise ValueError(f"{self.operation} requires a point")
        elif self.point is not None:
            raise ValueError("point is only supported by limit or series")
        if self.operation == "series":
            if self.order is None:
                raise ValueError("series requires an expansion order")
        elif self.order is not None:
            raise ValueError("order is only supported by series")
        if any(len(set(values)) != len(values) for values in self.assumptions.values()):
            raise ValueError("assumption lists cannot contain duplicates")
        if set(self.assumptions) & set(self.named_units):
            raise ValueError("a name cannot be both a symbolic assumption and a unit")
        if self.operation in {"series", "sum", "product", "plot"} and self.variable in self.named_units:
            raise ValueError("series/sum/product/plot variables cannot be bound to named physical units")
        if self.operation != "plot" and self.plot_width != 720:
            raise ValueError("plot_width is only supported by plot")
        return self


class SymbolicInputError(ValueError):
    pass


def run_symbolic(request: SymbolicRequest) -> dict[str, object]:
    """Evaluate in a child process so a slow symbolic operation can be terminated."""
    command = [sys.executable, "-m", "leanproof_service.symbolic", "--symbolic-worker"]
    child_environment = os.environ.copy()
    child_environment["PYTHONIOENCODING"] = "utf-8"
    try:
        process = subprocess.run(
            command,
            input=request.model_dump_json(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=request.time_limit_seconds,
            check=False,
            env=child_environment,
        )
    except subprocess.TimeoutExpired:
        return {"timed_out": True, "limit_seconds": request.time_limit_seconds}
    if process.returncode != 0:
        return {"error": "Symbolic worker failed", "diagnostics": process.stderr[-2000:]}
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError:
        return {"error": "Symbolic worker returned invalid output"}
    return result


def _execute(request: SymbolicRequest) -> dict[str, object]:
    import sympy as sp

    symbols = {
        name: sp.Symbol(name, **{assumption: True for assumption in assumptions})
        for name, assumptions in request.assumptions.items()
    }
    if request.named_units:
        from sympy.physics import units as sympy_units
        from sympy.physics.units import Quantity, liter

        unit_catalog: dict[str, object] = {
            name: getattr(sympy_units, unit.sympy_name)
            for name, unit in PHYSICAL_UNITS.items()
        }
        if "us_minim" in request.named_units.values():
            us_minim = Quantity("minimUS")
            us_minim.set_global_relative_scale_factor(sp.Rational("3.785411784") / 61440, liter)
            unit_catalog["us_minim"] = us_minim
        symbols.update({name: unit_catalog[unit] for name, unit in request.named_units.items()})
    parser = _SafeParser(sp, symbols)
    if request.operation == "identity":
        parsing_started = perf_counter()
        lhs = parser.parse(request.lhs or "")
        rhs = parser.parse(request.rhs or "")
        if request.named_units:
            _check_unit_dimensions(sp, lhs - rhs)
        parse_timing = perf_counter() - parsing_started
        difference = sp.simplify(lhs - rhs)
        equal = difference == 0
        return {
            "operation": request.operation,
            "result": "0" if equal else sp.sstr(difference),
            "symbolic_check": "simplified_to_zero" if equal else "not_simplified_to_zero",
            "parse_timing_seconds": parse_timing,
            "input_mathml": _mathml_output(sp, sp.Eq(lhs, rhs, evaluate=False)),
            "result_mathml": _mathml_output(sp, difference),
            "assumptions": _assumptions_payload(request),
            "evidence": _symbolic_evidence(sp),
            "qualification": "A SymPy simplification result is not a formal proof.",
        }

    parsing_started = perf_counter()
    expression = parser.parse(request.expression or "")
    if request.named_units:
        _check_unit_dimensions(sp, expression)
    parse_timing = perf_counter() - parsing_started
    if request.operation == "validate":
        return {
            "operation": "validate",
            "parsed": True,
            "normalized_expression": sp.sstr(expression),
            "parse_timing_seconds": parse_timing,
            "input_mathml": _mathml_output(sp, expression),
            "evidence": {
                "kind": "symbolic_parse",
                "engine": "sympy",
                "version": sp.__version__,
                "verified": False,
            },
        }
    variable = _get_symbol(sp, symbols, request.variable) if request.variable else None
    if request.operation == "simplify":
        result = sp.simplify(expression)
    elif request.operation == "factor":
        result = sp.factor(expression)
    elif request.operation == "expand":
        result = sp.expand(expression)
    elif request.operation == "differentiate":
        result = sp.diff(expression, variable)
    elif request.operation == "limit":
        point = parser.parse(request.point or "")
        if point.has(variable):
            raise SymbolicInputError("limit point cannot depend on the limit variable")
        limit_details: dict[str, object]
        if point == sp.oo:
            result = sp.limit(expression, variable, point, dir="+")
            limit_details = {"direction": "positive_infinity"}
        elif point == -sp.oo:
            result = sp.limit(expression, variable, point, dir="-")
            limit_details = {"direction": "negative_infinity"}
        else:
            if point.is_real is not True:
                raise SymbolicInputError(
                    "finite limit points must be known real; declare symbolic points as real"
                )
            left_limit = sp.limit(expression, variable, point, dir="-")
            right_limit = sp.limit(expression, variable, point, dir="+")
            equal_sides = (
                left_limit == right_limit
                and not isinstance(left_limit, sp.AccumBounds)
            )
            if not equal_sides:
                try:
                    equal_sides = sp.simplify(left_limit - right_limit) == 0
                except (TypeError, ValueError):
                    equal_sides = False
            if equal_sides:
                result = left_limit
            else:
                result = (
                    "DoesNotExist (two-sided limit; "
                    f"left={sp.sstr(left_limit)}, right={sp.sstr(right_limit)})"
                )
            limit_details = {
                "left": sp.sstr(left_limit),
                "right": sp.sstr(right_limit),
                "exists": equal_sides,
            }
    elif request.operation == "series":
        point = parser.parse(request.point or "")
        if point.has(variable):
            raise SymbolicInputError("series point cannot depend on the expansion variable")
        result = sp.series(expression, variable, point, request.order)
        series_data = _series_data(sp, result, variable, point, request.order)
    elif request.operation == "integrate":
        if request.lower is not None and request.upper is not None:
            lower = parser.parse(request.lower)
            upper = parser.parse(request.upper)
            result = sp.integrate(expression, (variable, lower, upper))
        else:
            result = sp.integrate(expression, variable)
            if isinstance(result, sp.Basic):
                result = result.replace(
                    lambda node: (
                        node.func == sp.log
                        and variable in node.args[0].free_symbols
                    ),
                    lambda node: sp.log(sp.Abs(node.args[0])),
                )
    elif request.operation in {"sum", "product"}:
        lower = parser.parse(request.lower or "")
        upper = parser.parse(request.upper or "")
        if lower.is_integer is not True or upper.is_integer is not True:
            raise SymbolicInputError(
                f"{request.operation} bounds must be known integers; declare symbolic bounds as integer"
            )
        reversed_finite_range = (upper - lower).is_negative is True
        if reversed_finite_range and request.operation == "sum":
            result = sp.Integer(0)
        elif reversed_finite_range and request.operation == "product":
            result = sp.Integer(1)
        elif request.operation == "sum":
            result = sp.summation(expression, (variable, lower, upper))
        else:
            result = sp.product(expression, (variable, lower, upper))
    elif request.operation == "plot":
        lower = parser.parse(request.lower or "")
        upper = parser.parse(request.upper or "")
        if expression.free_symbols - {variable}:
            raise SymbolicInputError("plots may contain only the selected variable")
        if lower.free_symbols or upper.free_symbols:
            raise SymbolicInputError("plot bounds must be numeric")
        if lower.is_real is not True or upper.is_real is not True:
            raise SymbolicInputError("plot bounds must be real")
        try:
            lower_value = float(sp.N(lower, 30))
            upper_value = float(sp.N(upper, 30))
        except (OverflowError, TypeError, ValueError) as exc:
            raise SymbolicInputError("plot bounds must be finite numeric values") from exc
        if not math.isfinite(lower_value) or not math.isfinite(upper_value):
            raise SymbolicInputError("plot bounds must be finite numeric values")
        if lower_value >= upper_value:
            raise SymbolicInputError("plot lower bound must be less than upper bound")
        if not math.isfinite(upper_value - lower_value):
            raise SymbolicInputError("plot interval is too wide to render")
        result = (
            f"Plot of {sp.sstr(expression)} for {variable} in "
            f"[{sp.sstr(lower)}, {sp.sstr(upper)}]"
        )
        plot_svg = _plot_svg(
            sp,
            expression,
            variable,
            lower_value,
            upper_value,
            request.plot_width,
        )
    else:
        result = sp.solve(expression, variable, dict=True)
        result = [
            {str(key): sp.sstr(value) for key, value in solution.items()}
            for solution in result
        ]
    numeric_approximation = None
    if (
        isinstance(result, sp.Basic)
        and result.is_number
        and result.is_finite is not False
        and result.is_integer is not True
    ):
        numeric_approximation = {
            "value": str(sp.N(result, request.numeric_precision)),
            "precision": request.numeric_precision,
        }
    response: dict[str, object] = {
        "operation": request.operation,
        "result": result if isinstance(result, list) else sp.sstr(result),
        "numeric_approximation": numeric_approximation,
        "parse_timing_seconds": parse_timing,
        "input_mathml": _mathml_output(sp, expression),
        "result_mathml": _mathml_output(sp, result) if isinstance(result, sp.Basic) else None,
        "assumptions": _assumptions_payload(request),
        "evidence": _symbolic_evidence(sp),
        "qualification": (
            "Computed as a two-sided limit over the real line; this symbolic result is not a Lean proof."
            if request.operation == "limit"
            else "Symbolic computation result; this response is not a Lean proof."
        ),
    }
    if request.operation == "limit":
        response["limit_details"] = limit_details
    if request.operation == "series":
        response["series_order"] = request.order
        response["series_data"] = series_data
    if request.operation == "plot":
        response["plot_svg"] = plot_svg
    return response


def _plot_svg(
    sympy: object,
    expression: object,
    variable: object,
    lower: float,
    upper: float,
    width: int,
) -> str:
    import html

    height = round(width * 2 / 3)
    scale = width / 720
    left, right, top, bottom = (
        max(28, round(76 * scale)),
        max(8, round(24 * scale)),
        max(20, round(46 * scale)),
        max(25, round(56 * scale)),
    )
    font_size = max(7, 13 * scale)
    plot_width = width - left - right
    plot_height = height - top - bottom
    sample_count = 321
    x_span = upper - lower
    samples: list[tuple[float, float] | None] = []
    for index in range(sample_count):
        x_value = lower + x_span * (index / (sample_count - 1))
        try:
            evaluated = sympy.N(
                expression.subs(variable, sympy.Float(x_value)),
                20,
            )
            if evaluated.is_real is not True:
                samples.append(None)
                continue
            y_value = float(evaluated)
        except (AttributeError, OverflowError, TypeError, ValueError, ZeroDivisionError):
            samples.append(None)
            continue
        samples.append((x_value, y_value) if math.isfinite(y_value) else None)

    finite_y = sorted(point[1] for point in samples if point is not None)
    if not finite_y:
        raise SymbolicInputError("plot produced no finite real samples over the requested interval")
    lower_index = int((len(finite_y) - 1) * 0.02)
    upper_index = int((len(finite_y) - 1) * 0.98)
    raw_y_min = finite_y[lower_index]
    raw_y_max = finite_y[upper_index]
    y_scale = max(abs(raw_y_min), abs(raw_y_max), 1.0)
    y_min = raw_y_min / y_scale
    y_max = raw_y_max / y_scale
    max_scaled_y = sys.float_info.max / y_scale
    if y_min == y_max:
        padding = max(abs(y_min) * 0.08, 0.05)
        y_min = max(-max_scaled_y, y_min - padding)
        y_max = min(max_scaled_y, y_max + padding)
    else:
        margin = (y_max - y_min) * 0.08
        y_min = max(-max_scaled_y, y_min - margin)
        y_max = min(max_scaled_y, y_max + margin)
    if y_min == y_max:
        raise SymbolicInputError("plot output range is too narrow to render")
    y_span = y_max - y_min

    def sx(value: float) -> float:
        return left + ((value - lower) / x_span) * plot_width

    def sy(value: float) -> float:
        clipped = min(y_max, max(y_min, value / y_scale))
        return top + (y_max - clipped) * plot_height / y_span

    grid: list[str] = []
    for index in range(5):
        x_value = lower + (upper - lower) * index / 4
        y_scaled = y_min + y_span * index / 4
        y_value = y_scaled * y_scale
        if not math.isfinite(y_value):
            y_value = math.copysign(sys.float_info.max, y_scaled)
        x_position = sx(x_value)
        y_position = sy(y_value)
        grid.append(
            f'<line x1="{x_position:.2f}" y1="{top}" x2="{x_position:.2f}" '
            f'y2="{top + plot_height}" class="grid"/>'
        )
        grid.append(
            f'<line x1="{left}" y1="{y_position:.2f}" x2="{left + plot_width}" '
            f'y2="{y_position:.2f}" class="grid"/>'
        )
        grid.append(
            f'<text x="{x_position:.2f}" y="{top + plot_height + 24}" '
            f'text-anchor="middle">{x_value:.3g}</text>'
        )
        grid.append(
            f'<text x="{left - 10}" y="{y_position + 4:.2f}" '
            f'text-anchor="end">{y_value:.3g}</text>'
        )

    axis_y = sy(0.0) if y_min <= 0.0 <= y_max else top + plot_height
    axis_x = sx(0.0) if lower <= 0.0 <= upper else left
    paths: list[str] = []
    segment: list[tuple[float, float]] = []
    previous_y: float | None = None
    for point in samples:
        if point is None or (
            previous_y is not None and abs(point[1] - previous_y) > y_span * 1.5
        ):
            if segment:
                path = " ".join(
                    ("M" if index == 0 else "L") + f" {x:.2f} {y:.2f}"
                    for index, (x, y) in enumerate(segment)
                )
                paths.append(f'<path d="{path}" class="curve"/>')
            segment = []
        if point is not None:
            segment.append((sx(point[0]), sy(point[1])))
            previous_y = point[1]
        else:
            previous_y = None
    if segment:
        path = " ".join(
            ("M" if index == 0 else "L") + f" {x:.2f} {y:.2f}"
            for index, (x, y) in enumerate(segment)
        )
        paths.append(f'<path d="{path}" class="curve"/>')

    title = html.escape(f"y = {sympy.sstr(expression)}", quote=True)
    variable_label = html.escape(str(variable), quote=True)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{title}">'
        f'<style>text{{font:{font_size:.2f}px sans-serif;fill:#334155}}'
        '.grid{stroke:#e2e8f0;stroke-width:1}'
        '.axis{stroke:#64748b;stroke-width:1.4}.curve{fill:none;stroke:#2563eb;'
        'stroke-width:2.6;stroke-linecap:round;stroke-linejoin:round}</style>'
        f'<text x="{width / 2:.0f}" y="24" text-anchor="middle">{title}</text>'
        f'{"".join(grid)}'
        f'<line x1="{left}" y1="{axis_y:.2f}" x2="{left + plot_width}" '
        f'y2="{axis_y:.2f}" class="axis"/>'
        f'<line x1="{axis_x:.2f}" y1="{top}" x2="{axis_x:.2f}" '
        f'y2="{top + plot_height}" class="axis"/>'
        f'{"".join(paths)}'
        f'<text x="{left + plot_width / 2:.0f}" y="{height - 8}" '
        f'text-anchor="middle">{variable_label}</text></svg>'
    )


def _series_data(sympy: object, series: object, variable: object, point: object, order: int | None) -> dict[str, object] | None:
    if order is None or not isinstance(variable, sympy.Symbol) or point in {sympy.oo, -sympy.oo}:
        return None
    parameter = sympy.Dummy("series_parameter")
    try:
        shifted_series = sympy.expand(series.removeO().subs(variable, point + parameter))
        polynomial = sympy.Poly(shifted_series, parameter)
    except (AttributeError, sympy.PolynomialError, TypeError, ValueError):
        return None
    if any(exponents[0] < 0 or exponents[0] >= order for exponents, _ in polynomial.terms()):
        return None
    return {
        "variable": str(variable),
        "point": sympy.sstr(point),
        "coefficients": [sympy.sstr(polynomial.nth(index)) for index in range(order)],
        "lowest_power": 0,
        "highest_power_exclusive": order,
        "denominator": 1,
    }


def _normalize_decimal_literals(source: str) -> str:
    """Replace decimal spellings with bounded exact ratios before Python parses floats."""
    invalid = False

    def replace(match: re.Match[str]) -> str:
        nonlocal invalid
        token = match.group(0).replace("_", "")
        try:
            decimal = Decimal(token)
            _, digits, exponent = decimal.as_tuple()
            if len(digits) > 1233 or abs(exponent) > 1233:
                invalid = True
                return match.group(0)
            if exponent >= 0 and len(digits) + exponent > 1233:
                invalid = True
                return match.group(0)
            value = Fraction(decimal)
        except (InvalidOperation, ValueError, OverflowError):
            invalid = True
            return match.group(0)

        if (
            value.numerator.bit_length() > _MAX_DECIMAL_BITS
            or value.denominator.bit_length() > _MAX_DECIMAL_BITS
        ):
            invalid = True
            return match.group(0)
        if value.denominator == 1:
            return str(value.numerator)
        return f"({value.numerator}/{value.denominator})"

    normalized = _DECIMAL_LITERAL.sub(replace, source)
    if invalid:
        raise SymbolicInputError("decimal literals exceed the exact-rational size limit")
    return normalized


def _check_unit_dimensions(sympy: object, expression: object) -> None:
    from sympy.physics.units.util import check_dimensions

    try:
        check_dimensions(expression)
    except ValueError as exc:
        raise SymbolicInputError("expression contains incompatible physical dimensions") from exc


def _mathml_output(sympy: object, expression: object) -> str | None:
    try:
        result = sympy.mathml(expression, printer="presentation")
    except Exception:
        return None
    if not result.startswith("<math"):
        result = (
            '<math xmlns="http://www.w3.org/1998/Math/MathML">'
            f"{result}</math>"
        )
    return result if len(result) <= 50_000 else None


def _get_symbol(sp: object, symbols: dict[str, object], name: str | None) -> object:
    if name is None:
        raise SymbolicInputError("variable is required")
    if name not in symbols:
        symbols[name] = sp.Symbol(name)
    return symbols[name]


def _assumptions_payload(request: SymbolicRequest) -> dict[str, list[str]]:
    return {name: list(values) for name, values in request.assumptions.items()}


def _symbolic_evidence(sympy: object) -> dict[str, object]:
    return {"kind": "symbolic_result", "engine": "sympy", "version": sympy.__version__, "verified": False}


class _SafeParser:
    _functions = {
        "sin": "sin", "cos": "cos", "tan": "tan", "asin": "asin", "acos": "acos",
        "atan": "atan", "asinh": "asinh", "arcsinh": "asinh",
        "acosh": "acosh", "arccosh": "acosh",
        "atanh": "atanh", "arctanh": "atanh", "artanh": "atanh",
        "sinh": "sinh", "cosh": "cosh", "tanh": "tanh", "coth": "coth", "sech": "sech", "csch": "csch", "exp": "exp",
        "sec": "sec", "csc": "csc", "cot": "cot",
        "log": "log", "sqrt": "sqrt", "root": "root", "Abs": "Abs", "abs": "Abs", "floor": "floor",
        "ceiling": "ceiling",
    }
    _constants = {"pi": "pi", "E": "E", "I": "I", "oo": "oo"}
    _identifier = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")

    def __init__(self, sympy: object, symbols: dict[str, object]) -> None:
        self.sp = sympy
        self.symbols = symbols
        self.nodes = 0

    def parse(self, source: str) -> object:
        if not source or len(source) > 4000:
            raise SymbolicInputError("expression must contain 1 to 4000 characters")
        try:
            source = parse_math_input(source).expression
        except MathInputError as exc:
            raise SymbolicInputError(str(exc)) from exc
        source = _normalize_decimal_literals(source)
        try:
            tree = ast.parse(source, mode="eval")
        except (SyntaxError, RecursionError, ValueError) as exc:
            raise SymbolicInputError("expression syntax is invalid") from exc
        return self._visit(tree.body, 0)

    def _visit(self, node: ast.AST, depth: int) -> object:
        self.nodes += 1
        if self.nodes > 256 or depth > 32:
            raise SymbolicInputError("expression exceeds the 256-node or 32-level limit")
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise SymbolicInputError("only integer and decimal numeric literals are allowed")
            if isinstance(node.value, int):
                if abs(node.value).bit_length() > 256:
                    raise SymbolicInputError("integer literals are limited to 256 bits")
                return self.sp.Integer(node.value)
            raise SymbolicInputError("decimal literal could not be preserved exactly")
        if isinstance(node, ast.Name):
            name = node.id
            if name in self._constants:
                return getattr(self.sp, self._constants[name])
            if name in self._functions:
                raise SymbolicInputError("function names must be called")
            if not self._identifier.fullmatch(name):
                raise SymbolicInputError("symbol name is invalid")
            if name not in self.symbols:
                self.symbols[name] = self.sp.Symbol(name)
            return self.symbols[name]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = self._visit(node.operand, depth + 1)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Pow) and not self._valid_exponent(node.right):
                raise SymbolicInputError(
                    "exponents must use bounded numeric values, symbols, or basic arithmetic"
                )
            left = self._visit(node.left, depth + 1)
            right = self._visit(node.right, depth + 1)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.Pow):
                return left**right
            raise SymbolicInputError("operator is not supported")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            function_name = self._functions.get(node.func.id)
            if function_name is None or node.keywords:
                raise SymbolicInputError("function is not supported")
            if function_name == "log" and len(node.args) not in {1, 2}:
                raise SymbolicInputError("log takes one or two arguments")
            elif function_name == "root" and len(node.args) != 2:
                raise SymbolicInputError("root takes a radicand and an integer index")
            elif function_name != "log" and len(node.args) != 1:
                if function_name != "root":
                    raise SymbolicInputError("function takes exactly one argument")
            if function_name == "root":
                index = self._integer_literal(node.args[1])
                if index is None or index < 2 or index > 32:
                    raise SymbolicInputError("root index must be an integer from 2 to 32")
                radicand = self._visit(node.args[0], depth + 1)
                return self.sp.real_root(radicand, index) if index % 2 else self.sp.root(radicand, index)
            args = [self._visit(argument, depth + 1) for argument in node.args]
            return getattr(self.sp, function_name)(*args)
        raise SymbolicInputError("expression contains an unsupported construct")

    @staticmethod
    def _integer_literal(node: ast.AST) -> int | None:
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            if isinstance(node.operand, ast.Constant) and type(node.operand.value) is int:
                return node.operand.value if isinstance(node.op, ast.UAdd) else -node.operand.value
        return None

    @staticmethod
    def _valid_exponent(node: ast.AST) -> bool:
        if isinstance(node, ast.Constant):
            return _SafeParser._bounded_exponent_number(node.value)
        if isinstance(node, ast.Name):
            return _SafeParser._identifier.fullmatch(node.id) is not None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            return _SafeParser._valid_exponent(node.operand)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            numerator, denominator = node.left, node.right
            return (
                isinstance(numerator, ast.Constant)
                and _SafeParser._bounded_exponent_number(numerator.value)
                and isinstance(denominator, ast.Constant)
                and _SafeParser._bounded_exponent_number(denominator.value)
                and denominator.value != 0
            )
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
            return _SafeParser._valid_exponent(node.left) and _SafeParser._valid_exponent(node.right)
        return False

    @staticmethod
    def _bounded_exponent_number(value: object) -> bool:
        if type(value) is int:
            return abs(value) <= 100
        if type(value) is float:
            return math.isfinite(value) and abs(value) <= 100
        return False


def _symbolic_worker() -> None:
    try:
        request = SymbolicRequest.model_validate_json(sys.stdin.read(32_768))
        response = _execute(request)
    except Exception as exc:
        if isinstance(exc, (SymbolicInputError, ValueError)):
            response = {"error": str(exc)}
        else:
            response = {"error": "Symbolic operation failed", "diagnostics": str(exc)[:1000]}
    encoded = json.dumps(response, separators=(",", ":"))
    if len(encoded) > 100_000:
        encoded = json.dumps({"error": "Symbolic result exceeded the 100 KB response limit"})
    sys.stdout.write(encoded)


if __name__ == "__main__" and sys.argv[1:] == ["--symbolic-worker"]:
    _symbolic_worker()
