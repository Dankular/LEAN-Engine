from __future__ import annotations

import ast
import json
import math
import os
import re
import subprocess
import sys
from time import perf_counter
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from .input_syntax import MathInputError, parse_math_input
from .models import StrictModel


SymbolName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$")]
AssumptionName = Literal[
    "real", "positive", "nonnegative", "negative", "nonpositive", "integer", "rational"
]
UnitName = Literal["meter", "minute", "us_minim"]
SymbolicOperation = Literal["validate", "simplify", "differentiate", "integrate", "solve", "identity"]


class SymbolicRequest(StrictModel):
    operation: SymbolicOperation
    expression: str | None = Field(default=None, min_length=1, max_length=4000)
    lhs: str | None = Field(default=None, min_length=1, max_length=4000)
    rhs: str | None = Field(default=None, min_length=1, max_length=4000)
    variable: SymbolName | None = None
    lower: str | None = Field(default=None, min_length=1, max_length=256)
    upper: str | None = Field(default=None, min_length=1, max_length=256)
    assumptions: dict[SymbolName, list[AssumptionName]] = Field(default_factory=dict, max_length=64)
    named_units: dict[SymbolName, UnitName] = Field(default_factory=dict, max_length=16)
    time_limit_seconds: float = Field(default=5.0, ge=0.001, le=30.0)
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
        if self.operation in {"differentiate", "integrate", "solve"} and self.variable is None:
            raise ValueError(f"{self.operation} requires variable")
        if self.operation not in {"differentiate", "integrate", "solve"} and self.variable is not None:
            raise ValueError("variable is only used by differentiate, integrate, or solve")
        if self.operation == "integrate":
            if (self.lower is None) != (self.upper is None):
                raise ValueError("definite integration requires both lower and upper")
        elif self.lower is not None or self.upper is not None:
            raise ValueError("lower and upper are only supported by integrate")
        if any(len(set(values)) != len(values) for values in self.assumptions.values()):
            raise ValueError("assumption lists cannot contain duplicates")
        if set(self.assumptions) & set(self.named_units):
            raise ValueError("a name cannot be both a symbolic assumption and a unit")
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
        from sympy.physics.units import Quantity, liter, meter, minute

        unit_catalog: dict[str, object] = {"meter": meter, "minute": minute}
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
    elif request.operation == "differentiate":
        result = sp.diff(expression, variable)
    elif request.operation == "integrate":
        if request.lower is not None and request.upper is not None:
            lower = parser.parse(request.lower)
            upper = parser.parse(request.upper)
            result = sp.integrate(expression, (variable, lower, upper))
        else:
            result = sp.integrate(expression, variable)
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
    return {
        "operation": request.operation,
        "result": result if isinstance(result, list) else sp.sstr(result),
        "numeric_approximation": numeric_approximation,
        "parse_timing_seconds": parse_timing,
        "input_mathml": _mathml_output(sp, expression),
        "result_mathml": _mathml_output(sp, result) if isinstance(result, sp.Basic) else None,
        "assumptions": _assumptions_payload(request),
        "evidence": _symbolic_evidence(sp),
        "qualification": "Symbolic computation result; this response is not a Lean proof.",
    }


def _check_unit_dimensions(sympy: object, expression: object) -> None:
    from sympy.physics.units.util import check_dimensions

    try:
        check_dimensions(expression)
    except ValueError as exc:
        raise SymbolicInputError("expression contains incompatible physical dimensions") from exc


def _mathml_output(sympy: object, expression: object) -> str | None:
    try:
        result = sympy.mathml(expression)
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
        "atan": "atan", "sinh": "sinh", "cosh": "cosh", "tanh": "tanh", "exp": "exp",
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
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
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
            if not math.isfinite(node.value):
                raise SymbolicInputError("decimal literals must be finite")
            return self.sp.Rational(str(node.value))
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
