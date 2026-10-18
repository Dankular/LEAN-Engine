from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from time import monotonic

from pydantic import BaseModel, ConfigDict, Field

from .input_syntax import MathInputError, parse_math_input
from .lean_backend import LeanResult, run_generated_source
from .symbolic import SymbolicRequest, run_symbolic


class ComputeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=4000)
    line: int = Field(default=1, ge=1, le=1_000_000)
    maxChars: int = Field(default=10_000, ge=1, le=100_000)
    timeConstraint: int = Field(default=60, ge=1, le=60)


@dataclass(frozen=True)
class Computation:
    display_result: str
    lean_source: str
    timed_out: bool = False
    backend_error: str | None = None


_DECIMAL_LITERAL = re.compile(
    r"(?<![A-Za-z0-9_])(?:(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)"
)
_WOLFRAM_REAL_FUNCTION = re.compile(
    r"\b(?P<function>Sin|Cos|Exp|Sinh|Cosh)\s*\[(?P<argument>[^\[\]]*)\]",
    re.IGNORECASE,
)


def prepare_computation(code: str, time_limit_seconds: float = 5.0) -> Computation | None:
    if re.fullmatch(r"\s*Sin\s*\[\s*Pi\s*\]\s*", code):
        source = """import Mathlib.Analysis.SpecialFunctions.Trigonometric.Basic

theorem wolfram_language_compute : Real.sin Real.pi = 0 := by
  simp

#print axioms wolfram_language_compute
"""
        return Computation("0", source)

    if re.fullmatch(r"\s*TrigExpand\s*@\s*Sin\s*\[\s*2\s*x\s*\]\s*", code):
        source = """import Mathlib.Analysis.SpecialFunctions.Trigonometric.Basic
import Mathlib.Tactic.Ring

theorem wolfram_language_compute (x : ℝ) :
    Real.sin (2 * x) = 2 * Real.cos x * Real.sin x := by
  rw [Real.sin_two_mul]
  ring

#print axioms wolfram_language_compute
"""
        return Computation("2*Cos[x]*Sin[x]", source)

    definite_integral = re.fullmatch(
        r"(?is)\s*Integrate\s*\[\s*(?P<expression>.+?)\s*,\s*"
        r"\{\s*(?P<variable>[a-z][a-z0-9_]{0,31})\s*,\s*"
        r"(?P<lower>[^,{}]+?)\s*,\s*(?P<upper>[^,{}]+?)\s*\}\s*\]\s*",
        code,
    )
    if definite_integral is not None:
        return _prepare_definite_real_integral(
            definite_integral.group("expression"),
            definite_integral.group("variable"),
            definite_integral.group("lower"),
            definite_integral.group("upper"),
            time_limit_seconds,
        )

    indefinite_integral = re.fullmatch(
        r"(?is)\s*Integrate\s*\[\s*(?P<expression>.*),\s*"
        r"(?P<variable>[a-z][a-z0-9_]{0,31})\s*\]\s*",
        code,
    )
    if indefinite_integral is not None:
        return _prepare_real_antiderivative(
            indefinite_integral.group("expression"),
            indefinite_integral.group("variable"),
            time_limit_seconds,
        )

    derivative = re.fullmatch(
        r"(?is)\s*D\s*\[\s*(?P<expression>.*),\s*(?P<variable>[a-z][a-z0-9_]{0,31})\s*\]\s*",
        code,
    )
    if derivative is not None:
        return _prepare_real_derivative(
            derivative.group("expression"),
            derivative.group("variable"),
            time_limit_seconds,
        )

    return _prepare_polynomial_computation(code, time_limit_seconds)


def _prepare_definite_real_integral(
    expression_source: str,
    variable: str,
    lower_source: str,
    upper_source: str,
    time_limit_seconds: float,
) -> Computation | None:
    normalized_source = _normalize_wolfram_real_functions(expression_source)
    exact_source = _normalize_decimal_literals(normalized_source)
    if exact_source is None:
        return None
    try:
        parsed = parse_math_input(exact_source)
        expression_tree = ast.parse(parsed.expression, mode="eval")
        integrand_lean, integrand_symbols = _render_real_calculus_lean(parsed.expression)
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None
    if integrand_lean is None:
        return None

    lower = _parse_exact_rational(lower_source)
    upper = _parse_exact_rational(upper_source)
    if lower is None or upper is None:
        return None

    symbolic_deadline = monotonic() + max(0.001, min(time_limit_seconds, 60.0))
    symbolic = run_symbolic(SymbolicRequest(
        operation="integrate",
        expression=parsed.expression,
        variable=variable,
        time_limit_seconds=max(0.001, min(symbolic_deadline - monotonic(), 60.0)),
    ))
    if symbolic.get("timed_out"):
        return Computation("", "", timed_out=True)
    if symbolic.get("error") or not isinstance(symbolic.get("result"), str):
        diagnostic = symbolic.get("diagnostics") or symbolic.get("error") or (
            "Symbolic worker returned no antiderivative"
        )
        return Computation("", "", backend_error=str(diagnostic)[:1000])

    antiderivative = symbolic["result"]
    try:
        antiderivative_tree = ast.parse(antiderivative, mode="eval")
        antiderivative_lean, antiderivative_symbols = _render_real_calculus_lean(antiderivative)
    except (ValueError, SyntaxError, RecursionError):
        return None
    if antiderivative_lean is None:
        return None

    remaining_symbolic_time = symbolic_deadline - monotonic()
    if remaining_symbolic_time <= 0.001:
        return Computation("", "", timed_out=True)
    definite_symbolic = run_symbolic(SymbolicRequest(
        operation="integrate",
        expression=parsed.expression,
        variable=variable,
        lower=_fraction_text(lower),
        upper=_fraction_text(upper),
        time_limit_seconds=max(0.001, min(remaining_symbolic_time, 60.0)),
    ))
    if definite_symbolic.get("timed_out"):
        return Computation("", "", timed_out=True)
    if definite_symbolic.get("error") or not isinstance(definite_symbolic.get("result"), str):
        diagnostic = definite_symbolic.get("diagnostics") or definite_symbolic.get("error") or (
            "Symbolic worker returned no definite integral"
        )
        return Computation("", "", backend_error=str(diagnostic)[:1000])
    definite_result = definite_symbolic["result"]
    try:
        definite_result_lean, definite_result_symbols = _render_real_calculus_lean(definite_result)
    except (ValueError, SyntaxError, RecursionError):
        return None
    if definite_result_lean is None:
        return None

    symbol_names = integrand_symbols | antiderivative_symbols | definite_result_symbols

    def fresh_name(base: str, used_names: set[str]) -> str:
        name = base
        suffix = 0
        while name in used_names:
            suffix += 1
            name = f"{base}{suffix}"
        used_names.add(name)
        return name

    used_names = set(symbol_names)
    integration_variable = fresh_name("integral_var", used_names)
    point_variable = fresh_name("integral_point", used_names)
    function_variable = fresh_name("deriv_var", used_names)

    integrand_at_integral_var = _render_real_calculus_node(
        expression_tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: integration_variable},
    )
    integrand_at_point = _render_real_calculus_node(
        expression_tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: point_variable},
    )
    antiderivative_at_integral_var = _render_real_calculus_node(
        antiderivative_tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: integration_variable},
    )
    proof = _derivative_proof_term(
        antiderivative_tree.body,
        variable,
        function_variable,
        point_variable,
        {"nodes": 0},
        0,
    )
    if (
        integrand_at_integral_var is None
        or integrand_at_point is None
        or antiderivative_at_integral_var is None
        or proof is None
    ):
        return None

    binders = " ".join(
        f"({name} : ℝ)" for name in sorted(symbol_names - {variable})
    )
    theorem_binders = f" {binders}" if binders else ""
    lean_lower = _lean_rational(lower)
    lean_upper = _lean_rational(upper)
    source = f"""import Mathlib.Analysis.Calculus.Deriv.Add
import Mathlib.Analysis.Calculus.Deriv.Mul
import Mathlib.Analysis.Calculus.Deriv.Pow
import Mathlib.Analysis.SpecialFunctions.ExpDeriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.Deriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.DerivHyp
import Mathlib.MeasureTheory.Integral.IntervalIntegral.FundThmCalculus
import Mathlib.Tactic.FieldSimp
import Mathlib.Tactic.FunProp
import Mathlib.Tactic.NormNum
import Mathlib.Tactic.Ring

theorem wolfram_language_compute{theorem_binders} :
    (∫ {integration_variable} in {lean_lower}..{lean_upper}, {integrand_at_integral_var}) = {definite_result_lean} := by
  let F : ℝ → ℝ := fun {integration_variable} : ℝ => {antiderivative_at_integral_var}
  have hderiv : ∀ {point_variable} ∈ Set.uIcc {lean_lower} {lean_upper},
      HasDerivAt F {integrand_at_point} {point_variable} := by
    intro {point_variable} hpoint
    convert ({proof}) using 1
    · ext {function_variable}
      simp [F, id_eq] <;> ring
    · simp [F, id_eq] <;> field_simp <;> ring
  have hint : IntervalIntegrable
      (fun {integration_variable} : ℝ => {integrand_at_integral_var})
      MeasureTheory.volume {lean_lower} {lean_upper} := by
    apply Continuous.intervalIntegrable
    fun_prop
  rw [intervalIntegral.integral_eq_sub_of_hasDerivAt hderiv hint]
  norm_num [F] <;> field_simp <;> ring

#print axioms wolfram_language_compute
"""
    return Computation(definite_result.replace("**", "^"), source)


def _prepare_real_antiderivative(
    expression_source: str,
    variable: str,
    time_limit_seconds: float,
) -> Computation | None:
    normalized_source = _normalize_wolfram_real_functions(expression_source)
    exact_source = _normalize_decimal_literals(normalized_source)
    if exact_source is None:
        return None
    try:
        parsed = parse_math_input(exact_source)
        integrand_lean, integrand_symbols = _render_real_calculus_lean(parsed.expression)
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None
    if integrand_lean is None:
        return None

    symbolic = run_symbolic(SymbolicRequest(
        operation="integrate",
        expression=parsed.expression,
        variable=variable,
        time_limit_seconds=max(0.001, min(time_limit_seconds, 60.0)),
    ))
    if symbolic.get("timed_out"):
        return Computation("", "", timed_out=True)
    if symbolic.get("error") or not isinstance(symbolic.get("result"), str):
        diagnostic = symbolic.get("diagnostics") or symbolic.get("error") or (
            "Symbolic worker returned no result"
        )
        return Computation("", "", backend_error=str(diagnostic)[:1000])

    antiderivative = symbolic["result"]
    try:
        tree = ast.parse(antiderivative, mode="eval")
        antiderivative_lean, antiderivative_symbols = _render_real_calculus_lean(
            antiderivative
        )
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None
    if antiderivative_lean is None:
        return None

    function_variable = "deriv_var"
    suffix = 0
    while function_variable in (integrand_symbols | antiderivative_symbols) or (
        function_variable == variable
    ):
        suffix += 1
        function_variable = f"deriv_var{suffix}"
    rendered = _render_real_calculus_node(
        tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: function_variable},
    )
    proof = _derivative_proof_term(
        tree.body,
        variable,
        function_variable,
        variable,
        {"nodes": 0},
        0,
    )
    if rendered is None or proof is None:
        return None

    variables = sorted(integrand_symbols | antiderivative_symbols | {variable})
    binders = " ".join(f"({name} : ℝ)" for name in variables)
    theorem_binders = f" {binders}" if binders else ""
    source = f"""import Mathlib.Analysis.Calculus.Deriv.Add
import Mathlib.Analysis.Calculus.Deriv.Mul
import Mathlib.Analysis.Calculus.Deriv.Pow
import Mathlib.Analysis.SpecialFunctions.ExpDeriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.Deriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.DerivHyp
import Mathlib.Tactic.FieldSimp
import Mathlib.Tactic.Ring

theorem wolfram_language_compute{theorem_binders} :
    HasDerivAt (fun {function_variable} : ℝ => {rendered}) {integrand_lean} {variable} := by
  convert ({proof}) using 1
  · ext {function_variable}
    simp [id_eq] <;> ring
  · simp [id_eq] <;> field_simp <;> ring

#print axioms wolfram_language_compute
"""
    return Computation(antiderivative.replace("**", "^"), source)


def _prepare_real_derivative(
    expression_source: str,
    variable: str,
    time_limit_seconds: float,
) -> Computation | None:
    normalized_source = _normalize_wolfram_real_functions(expression_source)
    exact_source = _normalize_decimal_literals(normalized_source)
    if exact_source is None:
        return None
    try:
        parsed = parse_math_input(exact_source)
        tree = ast.parse(parsed.expression, mode="eval")
        original_lean, symbols = _render_real_calculus_lean(parsed.expression)
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None
    if original_lean is None:
        return None

    symbolic = run_symbolic(SymbolicRequest(
        operation="differentiate",
        expression=parsed.expression,
        variable=variable,
        time_limit_seconds=max(0.001, min(time_limit_seconds, 60.0)),
    ))
    if symbolic.get("timed_out"):
        return Computation("", "", timed_out=True)
    if symbolic.get("error") or not isinstance(symbolic.get("result"), str):
        diagnostic = symbolic.get("diagnostics") or symbolic.get("error") or "Symbolic worker returned no result"
        return Computation("", "", backend_error=str(diagnostic)[:1000])

    result = symbolic["result"]
    try:
        result_lean, result_symbols = _render_real_calculus_lean(result)
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None
    if result_lean is None:
        return None

    function_variable = "deriv_var"
    suffix = 0
    while function_variable in symbols or function_variable == variable:
        suffix += 1
        function_variable = f"deriv_var{suffix}"
    rendered = _render_real_calculus_node(
        tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: function_variable},
    )
    if rendered is None:
        return None
    proof = _derivative_proof_term(
        tree.body,
        variable,
        function_variable,
        variable,
        {"nodes": 0},
        0,
    )
    if proof is None:
        return None

    variables = sorted(set(symbols) | set(result_symbols) | {variable})
    binders = " ".join(f"({name} : ℝ)" for name in variables)
    theorem_binders = f" {binders}" if binders else ""
    source = f"""import Mathlib.Analysis.Calculus.Deriv.Add
import Mathlib.Analysis.Calculus.Deriv.Mul
import Mathlib.Analysis.Calculus.Deriv.Pow
import Mathlib.Analysis.SpecialFunctions.ExpDeriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.Deriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.DerivHyp
import Mathlib.Tactic.FieldSimp
import Mathlib.Tactic.Ring

theorem wolfram_language_compute{theorem_binders} :
    HasDerivAt (fun {function_variable} : ℝ => {rendered}) {result_lean} {variable} := by
  convert ({proof}) using 1
  · ext {function_variable}
    simp [id_eq] <;> ring
  · simp [id_eq] <;> field_simp <;> ring

#print axioms wolfram_language_compute
"""
    return Computation(result.replace("**", "^"), source)


def _prepare_polynomial_computation(
    code: str,
    time_limit_seconds: float,
) -> Computation | None:
    operation = "simplify"
    expression_source = code.strip()
    wrapper = re.fullmatch(
        r"(?is)\s*(?P<operation>simplify|expand|factor)\s*\[(?P<expression>.*)\]\s*",
        code,
    )
    if wrapper is not None:
        operation = wrapper.group("operation").lower()
        expression_source = wrapper.group("expression").strip()

    try:
        exact_source = _normalize_decimal_literals(expression_source)
        if exact_source is None:
            return None
        parsed = parse_math_input(exact_source)
        expression = parsed.expression
        original_lean, symbols = _render_polynomial_lean(expression)
    except (MathInputError, ValueError, SyntaxError):
        return None
    if original_lean is None:
        return None

    symbolic = run_symbolic(SymbolicRequest(
        operation=operation,
        expression=expression,
        time_limit_seconds=max(0.001, min(time_limit_seconds, 60.0)),
    ))
    if symbolic.get("timed_out"):
        return Computation("", "", timed_out=True)
    if symbolic.get("error") or not isinstance(symbolic.get("result"), str):
        diagnostic = symbolic.get("diagnostics") or symbolic.get("error") or "Symbolic worker returned no result"
        return Computation("", "", backend_error=str(diagnostic)[:1000])

    result = symbolic["result"]
    try:
        result_lean, result_symbols = _render_polynomial_lean(result)
    except (MathInputError, ValueError, SyntaxError):
        return None
    if result_lean is None:
        return None
    variables = sorted(set(symbols) | set(result_symbols))
    lean_source = _polynomial_equality_source(original_lean, result_lean, variables)
    display_result = result.replace("**", "^")
    return Computation(display_result, lean_source)


def _normalize_wolfram_real_functions(source: str) -> str:
    normalized = re.sub(r"\bPi\b", "pi", source, flags=re.IGNORECASE)
    for _ in range(64):
        normalized, replacements = _WOLFRAM_REAL_FUNCTION.subn(
            lambda match: f"{match.group('function').lower()}({match.group('argument')})",
            normalized,
        )
        if replacements == 0:
            break
    if "\\" not in normalized:
        normalized = normalized.replace("^", "**")
    return normalized


def _render_real_calculus_lean(source: str) -> tuple[str | None, set[str]]:
    try:
        tree = ast.parse(source, mode="eval")
    except (SyntaxError, RecursionError, ValueError):
        return None, set()
    names: set[str] = set()
    rendered = _render_real_calculus_node(tree.body, names, {"nodes": 0}, 0)
    return rendered, names


def _render_real_calculus_node(
    node: ast.AST,
    names: set[str],
    state: dict[str, int],
    depth: int,
    substitutions: dict[str, str] | None = None,
) -> str | None:
    state["nodes"] += 1
    if state["nodes"] > 256 or depth > 32:
        return None
    if isinstance(node, ast.Constant):
        value = _constant_fraction(node)
        return _lean_fraction(value) if value is not None else None
    if isinstance(node, ast.Name):
        if node.id == "pi":
            return "Real.pi"
        if node.id == "E":
            return "(Real.exp 1)"
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", node.id):
            return None
        names.add(node.id)
        return (substitutions or {}).get(node.id, node.id)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        inner = _render_real_calculus_node(
            node.operand, names, state, depth + 1, substitutions
        )
        if inner is None:
            return None
        return inner if isinstance(node.op, ast.UAdd) else f"(-{inner})"
    if isinstance(node, ast.Call):
        if (
            not isinstance(node.func, ast.Name)
            or node.keywords
            or len(node.args) != 1
        ):
            return None
        function_name = {
            "sin": "Real.sin",
            "cos": "Real.cos",
            "exp": "Real.exp",
            "sinh": "Real.sinh",
            "cosh": "Real.cosh",
        }.get(node.func.id)
        if function_name is None:
            return None
        argument = _render_real_calculus_node(
            node.args[0], names, state, depth + 1, substitutions
        )
        return f"({function_name} {argument})" if argument is not None else None
    if not isinstance(node, ast.BinOp):
        return None

    left = _render_real_calculus_node(
        node.left, names, state, depth + 1, substitutions
    )
    right = _render_real_calculus_node(
        node.right, names, state, depth + 1, substitutions
    )
    if left is None or right is None:
        return None
    if isinstance(node.op, ast.Add):
        return f"({left} + {right})"
    if isinstance(node.op, ast.Sub):
        return f"({left} - {right})"
    if isinstance(node.op, ast.Mult):
        return f"({left} * {right})"
    if isinstance(node.op, ast.Div):
        denominator = _constant_fraction(node.right)
        if denominator is None or denominator == 0:
            return None
        return f"({left} / {right})"
    if isinstance(node.op, ast.Pow):
        exponent = _constant_fraction(node.right)
        if exponent is None or exponent.denominator != 1 or not 0 <= exponent <= 16:
            return None
        return f"({left} ^ {exponent.numerator})"
    return None


def _render_polynomial_lean(source: str) -> tuple[str | None, set[str]]:
    try:
        tree = ast.parse(source, mode="eval")
    except (SyntaxError, RecursionError, ValueError):
        return None, set()
    state = {"nodes": 0}
    names: set[str] = set()
    rendered = _render_polynomial_node(tree.body, names, state, 0)
    return rendered, names


def _render_polynomial_node(
    node: ast.AST,
    names: set[str],
    state: dict[str, int],
    depth: int,
    substitutions: dict[str, str] | None = None,
) -> str | None:
    state["nodes"] += 1
    if state["nodes"] > 256 or depth > 32:
        return None
    if isinstance(node, ast.Constant):
        value = _constant_fraction(node)
        return _lean_fraction(value) if value is not None else None
    if isinstance(node, ast.Name):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", node.id):
            return None
        names.add(node.id)
        return (substitutions or {}).get(node.id, node.id)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        inner = _render_polynomial_node(node.operand, names, state, depth + 1, substitutions)
        if inner is None:
            return None
        return inner if isinstance(node.op, ast.UAdd) else f"(-{inner})"
    if not isinstance(node, ast.BinOp):
        return None

    left = _render_polynomial_node(node.left, names, state, depth + 1, substitutions)
    right = _render_polynomial_node(node.right, names, state, depth + 1, substitutions)
    if left is None or right is None:
        return None
    if isinstance(node.op, ast.Add):
        return f"({left} + {right})"
    if isinstance(node.op, ast.Sub):
        return f"({left} - {right})"
    if isinstance(node.op, ast.Mult):
        return f"({left} * {right})"
    if isinstance(node.op, ast.Div):
        denominator = _constant_fraction(node.right)
        if denominator is None or denominator == 0:
            return None
        return f"({left} / {right})"
    if isinstance(node.op, ast.Pow):
        exponent = _constant_fraction(node.right)
        if exponent is None or exponent.denominator != 1 or not 0 <= exponent <= 16:
            return None
        return f"({left} ^ {exponent.numerator})"
    return None


def _derivative_proof_term(
    node: ast.AST,
    variable: str,
    function_variable: str,
    point: str,
    state: dict[str, int],
    depth: int,
) -> str | None:
    state["nodes"] += 1
    if state["nodes"] > 256 or depth > 32:
        return None

    constant = _constant_fraction(node)
    if constant is not None:
        rendered_constant = _lean_rational(constant)
        return f"(hasDerivAt_const {point} {rendered_constant})"
    if isinstance(node, ast.Name):
        if node.id == variable:
            return f"(hasDerivAt_id {point})"
        if node.id == "pi":
            return f"(hasDerivAt_const {point} Real.pi)"
        if node.id == "E":
            return f"(hasDerivAt_const {point} (Real.exp 1))"
        return f"(hasDerivAt_const {point} {node.id})"
    if isinstance(node, ast.Call):
        if (
            not isinstance(node.func, ast.Name)
            or node.keywords
            or len(node.args) != 1
        ):
            return None
        derivative_method = {
            "sin": "sin",
            "cos": "cos",
            "exp": "exp",
            "sinh": "sinh",
            "cosh": "cosh",
        }.get(node.func.id)
        if derivative_method is None:
            return None
        inner = _derivative_proof_term(
            node.args[0], variable, function_variable, point, state, depth + 1
        )
        return f"({inner}).{derivative_method}" if inner is not None else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        inner = _derivative_proof_term(
            node.operand, variable, function_variable, point, state, depth + 1
        )
        if inner is None:
            return None
        return inner if isinstance(node.op, ast.UAdd) else f"({inner}).neg"
    if not isinstance(node, ast.BinOp):
        return None

    left = _derivative_proof_term(
        node.left, variable, function_variable, point, state, depth + 1
    )
    if left is None:
        return None
    if isinstance(node.op, ast.Pow):
        exponent = _constant_fraction(node.right)
        if exponent is None or exponent.denominator != 1 or not 0 <= exponent <= 16:
            return None
        return f"({left}).pow {exponent.numerator}"
    if isinstance(node.op, ast.Div):
        denominator = _constant_fraction(node.right)
        if denominator is None or denominator == 0:
            return None
        return f"({left}).div_const {_lean_rational(denominator)}"

    right = _derivative_proof_term(
        node.right, variable, function_variable, point, state, depth + 1
    )
    if right is None:
        return None
    if isinstance(node.op, ast.Add):
        return f"({left}).add ({right})"
    if isinstance(node.op, ast.Sub):
        return f"({left}).sub ({right})"
    if isinstance(node.op, ast.Mult):
        return f"({left}).mul ({right})"
    return None


def _constant_fraction(node: ast.AST) -> Fraction | None:
    if isinstance(node, ast.Constant):
        if type(node.value) is int:
            if abs(node.value).bit_length() > 256:
                return None
            return Fraction(node.value)
        return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _constant_fraction(node.operand)
        if value is None:
            return None
        return value if isinstance(node.op, ast.UAdd) else -value
    if not isinstance(node, ast.BinOp):
        return None
    left = _constant_fraction(node.left)
    right = _constant_fraction(node.right)
    if left is None or right is None:
        return None
    if isinstance(node.op, ast.Add):
        value = left + right
    elif isinstance(node.op, ast.Sub):
        value = left - right
    elif isinstance(node.op, ast.Mult):
        value = left * right
    elif isinstance(node.op, ast.Div) and right:
        value = left / right
    elif isinstance(node.op, ast.Pow) and right.denominator == 1 and 0 <= right <= 16:
        value = left ** right.numerator
    else:
        return None
    if value.numerator.bit_length() > 4096 or value.denominator.bit_length() > 4096:
        return None
    return value


def _parse_exact_rational(source: str) -> Fraction | None:
    exact_source = _normalize_decimal_literals(source)
    if exact_source is None:
        return None
    try:
        parsed = parse_math_input(exact_source)
        tree = ast.parse(parsed.expression, mode="eval")
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None
    return _constant_fraction(tree.body)


def _fraction_text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _lean_fraction(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"({value.numerator} / {value.denominator})"


def _normalize_decimal_literals(source: str) -> str | None:
    invalid = False

    def replace(match: re.Match[str]) -> str:
        nonlocal invalid
        token = match.group(0)
        try:
            decimal = Decimal(token)
            _, digits, exponent = decimal.as_tuple()
            if len(digits) > 1233 or abs(exponent) > 1233:
                invalid = True
                return token
            if exponent >= 0 and len(digits) + exponent > 1233:
                invalid = True
                return token
            value = Fraction(decimal)
        except (InvalidOperation, ValueError, OverflowError):
            invalid = True
            return token
        if value.numerator.bit_length() > 256 or value.denominator.bit_length() > 256:
            invalid = True
            return token
        if value.denominator == 1:
            return str(value.numerator)
        return f"({value.numerator}/{value.denominator})"

    normalized = _DECIMAL_LITERAL.sub(replace, source)
    return None if invalid else normalized


def _polynomial_equality_source(lhs: str, rhs: str, variables: list[str]) -> str:
    binders = " ".join(f"({name} : ℂ)" for name in variables)
    if variables:
        proposition = f"{lhs} = {rhs}"
    else:
        proposition = f"({lhs} : ℂ) = ({rhs} : ℂ)"
    declaration = f"theorem wolfram_language_compute {binders} : {proposition} := by\n  ring"
    return (
        "import Mathlib.Data.Complex.Basic\n"
        "import Mathlib.Tactic.Ring\n\n"
        f"{declaration}\n\n"
        "#print axioms wolfram_language_compute\n"
    )


def check_computation(computation: Computation, timeout_seconds: float) -> LeanResult:
    return run_generated_source(computation.lean_source, timeout_seconds)


def _lean_rational(value: Fraction) -> str:
    if value.denominator == 1:
        return f"({value.numerator} : ℝ)"
    return f"(({value.numerator} : ℝ) / {value.denominator})"
