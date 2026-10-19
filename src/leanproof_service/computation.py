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
    domain_conditions: tuple[str, ...] = ()
    real_parameters: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RealPolynomialRoot:
    lean_expression: str
    wolfram_expression: str


_DECIMAL_LITERAL = re.compile(
    r"(?<![A-Za-z0-9_])(?:(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)"
)
_WOLFRAM_REAL_FUNCTION = re.compile(
    r"\b(?P<function>"
    r"Sin|Cos|Tan|Sec|Csc|Cot|ArcSin|ArcCos|ArcTan|ArcSinh|ArcCosh|Sqrt|Exp|Sinh|Cosh|Tanh|Log"
    r")\s*\[(?P<argument>[^\[\]]*)\]",
    re.IGNORECASE,
)
_REAL_POLYNOMIAL_SOLVE = re.compile(
    r"(?is)\s*Solve\s*\[\s*(?P<lhs>.+?)\s*==\s*(?P<rhs>.+?)\s*,\s*"
    r"(?P<variable>[a-z][a-z0-9_]{0,31})\s*,\s*Reals\s*\]\s*"
)
_REAL_LINEAR_SYSTEM_SOLVE = re.compile(
    r"(?is)\s*Solve\s*\[\s*\{(?P<equations>.*?)\}\s*,\s*"
    r"\{(?P<variables>.*?)\}\s*,\s*Reals\s*\]\s*"
)
_REAL_RATIONAL_LIMIT = re.compile(
    r"(?is)\s*Limit\s*\[\s*(?P<expression>.+?)\s*,\s*"
    r"\{\s*(?P<variable>[a-z][a-z0-9_]{0,31})\s*,\s*"
    r"(?P<point>[^,{}]+?)\s*\}\s*\]\s*"
)
_FINITE_SUM_PRODUCT = re.compile(
    r"(?is)\s*(?P<operation>Sum|Product)\s*\[\s*(?P<expression>.+?)\s*,\s*"
    r"\{\s*(?P<variable>[a-z][a-z0-9_]{0,31})\s*,\s*"
    r"(?P<lower>[^,{}]+?)\s*,\s*(?P<upper>[^,{}]+?)\s*\}\s*\]\s*"
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

    real_rational_limit = _REAL_RATIONAL_LIMIT.fullmatch(code)
    if real_rational_limit is not None:
        return _prepare_real_rational_limit(
            real_rational_limit.group("expression"),
            real_rational_limit.group("variable"),
            real_rational_limit.group("point"),
            time_limit_seconds,
        )

    finite_sum_product = _FINITE_SUM_PRODUCT.fullmatch(code)
    if finite_sum_product is not None:
        return _prepare_finite_sum_product(
            finite_sum_product.group("operation").lower(),
            finite_sum_product.group("expression"),
            finite_sum_product.group("variable"),
            finite_sum_product.group("lower"),
            finite_sum_product.group("upper"),
            time_limit_seconds,
        )

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

    real_polynomial_solve = _REAL_POLYNOMIAL_SOLVE.fullmatch(code)
    if real_polynomial_solve is not None:
        return _prepare_real_polynomial_solve(
            real_polynomial_solve.group("lhs"),
            real_polynomial_solve.group("rhs"),
            real_polynomial_solve.group("variable"),
        )

    real_linear_system_solve = _REAL_LINEAR_SYSTEM_SOLVE.fullmatch(code)
    if real_linear_system_solve is not None:
        equation_sources = _split_top_level_commas(
            real_linear_system_solve.group("equations")
        )
        variable_sources = _split_top_level_commas(
            real_linear_system_solve.group("variables")
        )
        if equation_sources is None or variable_sources is None:
            return None
        if any(
            re.fullmatch(r"[a-z][a-z0-9_]{0,31}", variable) is None
            for variable in variable_sources
        ):
            return None
        return _prepare_real_linear_system_solve(
            equation_sources,
            variable_sources,
        )

    return _prepare_polynomial_computation(code, time_limit_seconds)


def _prepare_finite_sum_product(
    operation: str,
    expression_source: str,
    variable: str,
    lower_source: str,
    upper_source: str,
    time_limit_seconds: float,
) -> Computation | None:
    exact_source = _normalize_decimal_literals(
        _normalize_wolfram_real_functions(expression_source)
    )
    if exact_source is None:
        return None
    try:
        parsed = parse_math_input(exact_source)
        expression_tree = ast.parse(parsed.expression, mode="eval")
        _, expression_symbols = _render_polynomial_lean(parsed.expression)
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None

    lower = _parse_exact_rational(lower_source)
    upper = _parse_exact_rational(upper_source)
    if (
        lower is None
        or upper is None
        or lower.denominator != 1
        or upper.denominator != 1
        or abs(lower.numerator).bit_length() > 256
        or abs(upper.numerator).bit_length() > 256
    ):
        return None

    lower_integer = lower.numerator
    upper_integer = upper.numerator
    term_count = max(0, upper_integer - lower_integer + 1)
    if term_count > 128:
        return None

    symbolic = run_symbolic(SymbolicRequest(
        operation=operation,
        expression=parsed.expression,
        variable=variable,
        lower=str(lower_integer),
        upper=str(upper_integer),
        time_limit_seconds=max(0.001, min(time_limit_seconds, 60.0)),
    ))
    if symbolic.get("timed_out"):
        return Computation("", "", timed_out=True)
    if symbolic.get("error") or not isinstance(symbolic.get("result"), str):
        diagnostic = symbolic.get("diagnostics") or symbolic.get("error") or (
            "Symbolic worker returned no finite sum or product"
        )
        return Computation("", "", backend_error=str(diagnostic)[:1000])

    result = symbolic["result"]
    result_lean, result_symbols = _render_polynomial_lean(result)
    if result_lean is None:
        return None

    used_names = set(expression_symbols) | set(result_symbols)
    index_variable = _fresh_identifier(
        f"{operation}_index", used_names
    )
    substituted_body = _render_polynomial_node(
        expression_tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={
            variable: (
                f"(({_lean_real_integer(lower_integer)}) + "
                f"({index_variable} : ℝ))"
            )
        },
    )
    if substituted_body is None:
        return None

    theorem_symbols = sorted(
        (expression_symbols | result_symbols) - {variable, index_variable}
    )
    binders = " ".join(f"({name} : ℝ)" for name in theorem_symbols)
    theorem_binders = f" {binders}" if binders else ""
    if operation == "sum":
        big_operator = f"(∑ {index_variable} ∈ Finset.range {term_count}, {substituted_body})"
        reduction_lemma = "Finset.sum_range_succ"
    else:
        big_operator = f"(∏ {index_variable} ∈ Finset.range {term_count}, {substituted_body})"
        reduction_lemma = "Finset.prod_range_succ"

    source = f"""import Mathlib.Basic.Real.Basic
import Mathlib.Algebra.BigOperators.Ring.Finset
import Mathlib.Tactic.Ring

theorem wolfram_language_compute{theorem_binders} :
    {big_operator} = {result_lean} := by
  simp [{reduction_lemma}] <;> ring

#print axioms wolfram_language_compute
"""
    return Computation(result.replace("**", "^"), source)


def _lean_real_integer(value: int) -> str:
    return f"({value} : ℝ)"


def _prepare_real_rational_limit(
    expression_source: str,
    variable: str,
    point_source: str,
    time_limit_seconds: float,
) -> Computation | None:
    exact_source = _normalize_decimal_literals(expression_source)
    if exact_source is None:
        return None
    try:
        parsed = parse_math_input(exact_source)
        expression_tree = ast.parse(parsed.expression, mode="eval")
        expression_lean, expression_symbols = _render_rational_limit_lean(
            parsed.expression
        )
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None
    if expression_lean is None:
        return None

    point = _parse_exact_rational(point_source)
    point_tree: ast.Expression | None = None
    point_symbols: set[str] = set()
    symbolic_singularity = False
    if point is not None:
        if (
            abs(point.numerator).bit_length() > 256
            or point.denominator.bit_length() > 256
        ):
            return None
        point_lean = _lean_rational(point)
        point_expression = _fraction_text(point)
        point_display = point_expression
    else:
        exact_point_source = _normalize_decimal_literals(point_source)
        if exact_point_source is None:
            return None
        try:
            parsed_point = parse_math_input(exact_point_source)
            point_tree = ast.parse(parsed_point.expression, mode="eval")
            point_lean, point_symbols = _render_rational_limit_lean(
                parsed_point.expression
            )
        except (MathInputError, ValueError, SyntaxError, RecursionError):
            return None
        if (
            point_lean is None
            or not point_symbols
            or variable in point_symbols
            or _variable_denominators(point_tree.body)
            or not _rational_polynomial_within_limits(
                point_tree.body, point_symbols
            )
        ):
            return None
        point_expression = parsed_point.expression
        point_display = point_expression.replace("**", "^")
        symbolic_singularity = _rational_denominator_vanishes_at_symbolic_point(
            expression_tree.body, variable, point_tree.body
        )

    symbolic_deadline = monotonic() + max(0.001, min(time_limit_seconds, 60.0))
    symbolic = run_symbolic(SymbolicRequest(
        operation="limit",
        expression=parsed.expression,
        variable=variable,
        point=point_expression,
        assumptions={name: ["real"] for name in point_symbols},
        time_limit_seconds=max(0.001, min(symbolic_deadline - monotonic(), 60.0)),
    ))
    if symbolic.get("timed_out"):
        return Computation("", "", timed_out=True)
    if symbolic.get("error") or not isinstance(symbolic.get("result"), str):
        diagnostic = symbolic.get("diagnostics") or symbolic.get("error") or (
            "Symbolic worker returned no finite limit"
        )
        return Computation("", "", backend_error=str(diagnostic)[:1000])

    result = symbolic["result"]
    result_lean, result_symbols = _render_rational_limit_lean(result)
    if result_lean is None:
        return None
    try:
        result_tree = ast.parse(result, mode="eval")
    except (SyntaxError, RecursionError, ValueError):
        return None

    if symbolic_singularity:
        remaining_symbolic_time = symbolic_deadline - monotonic()
        if remaining_symbolic_time <= 0.001:
            return Computation("", "", timed_out=True)
        if point_tree is None:
            return None
        return _prepare_removable_rational_limit(
            parsed.expression,
            expression_tree,
            result,
            variable,
            point,
            point_lean,
            point_display,
            point_tree.body,
            point_symbols,
            remaining_symbolic_time,
        )

    used_names = set(expression_symbols) | set(result_symbols) | set(point_symbols) | {variable}
    limit_variable = _fresh_identifier("limit_variable", used_names)
    limit_body = _render_rational_limit_node(
        expression_tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: limit_variable},
    )
    value_at_point = _render_rational_limit_node(
        expression_tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: point_lean},
    )
    if limit_body is None or value_at_point is None:
        return None
    continuity_state: dict[str, object] = {
        "nodes": 0,
        "denominator_assumptions": [],
        "assumed_denominators": [],
        "domain_binders": [],
        "domain_conditions": [],
        "used_names": used_names | {limit_variable},
    }
    continuity_proof = _rational_continuity_proof(
        expression_tree.body,
        variable,
        point,
        point_lean,
        point_display,
        continuity_state,
        0,
    )
    if continuity_proof is None:
        if point is None:
            return None
        remaining_symbolic_time = symbolic_deadline - monotonic()
        if remaining_symbolic_time <= 0.001:
            return Computation("", "", timed_out=True)
        return _prepare_removable_rational_limit(
            parsed.expression,
            expression_tree,
            result,
            variable,
            point,
            point_lean,
            point_display,
            None,
            point_symbols,
            remaining_symbolic_time,
        )

    theorem_symbols = sorted(
        (expression_symbols | result_symbols | point_symbols)
        - {variable, limit_variable}
    )
    domain_binders = continuity_state["domain_binders"]
    if not isinstance(domain_binders, list):
        return None
    binders = " ".join(
        [*(f"({name} : ℝ)" for name in theorem_symbols), *domain_binders]
    )
    theorem_binders = f" {binders}" if binders else ""
    denominator_assumptions = continuity_state["denominator_assumptions"]
    if not isinstance(denominator_assumptions, list):
        return None
    denominator_proofs = "\n".join(
        f"  have {name} : {denominator} ≠ 0 := by norm_num"
        for name, denominator in denominator_assumptions
    )
    denominator_proofs = f"{denominator_proofs}\n" if denominator_proofs else ""
    assumed_denominators = continuity_state["assumed_denominators"]
    if not isinstance(assumed_denominators, list):
        return None
    result_denominator_proofs: list[tuple[str, str, str]] = []
    for result_denominator in _variable_denominators(result_tree.body):
        denominator_value = _exact_rational_node_value(
            result_denominator, variable, point
        ) if point is not None else None
        if denominator_value is not None:
            if denominator_value == 0:
                return None
            continue
        denominator_names: set[str] = set()
        denominator_expression = _render_rational_limit_node(
            result_denominator,
            denominator_names,
            {"nodes": 0},
            0,
        )
        if denominator_expression is None or not assumed_denominators:
            return None
        used_hypothesis_names = continuity_state["used_names"]
        if not isinstance(used_hypothesis_names, set):
            return None
        hypothesis_name = _fresh_identifier(
            "h_result_denominator", used_hypothesis_names
        )
        zero_name = _fresh_identifier(
            "h_result_denominator_zero", used_hypothesis_names
        )
        proof_branches = "\n".join(
            f"    | exact {assumption_name} (by nlinarith [{zero_name}])"
            for assumption_name, _ in assumed_denominators
        )
        proof = (
            f"have {hypothesis_name} : {denominator_expression} ≠ 0 := by\n"
            f"    intro {zero_name}\n"
            "    first\n"
            f"{proof_branches}"
        )
        result_denominator_proofs.append(
            (hypothesis_name, denominator_expression, proof)
        )
    field_simp_names = [
        *(name for name, _ in assumed_denominators),
        *(name for name, _, _ in result_denominator_proofs),
    ]
    value_proof = (
        f"field_simp [{', '.join(field_simp_names)}, add_comm] <;> ring"
        if field_simp_names
        else "ring"
    )
    result_denominator_source = "\n".join(
        f"  {proof.replace(chr(10), chr(10) + '  ')}"
        for _, _, proof in result_denominator_proofs
    )
    result_denominator_source = (
        f"{result_denominator_source}\n" if result_denominator_source else ""
    )
    proof_used_names = continuity_state["used_names"]
    if not isinstance(proof_used_names, set):
        return None
    hcont = _fresh_identifier("h_limit_continuous", proof_used_names)
    hvalue = _fresh_identifier("h_limit_value", proof_used_names)
    source = f"""import Mathlib.Basic.Real.Basic
import Mathlib.Topology.Instances.Real.Lemmas
import Mathlib.Topology.Continuous
import Mathlib.Tactic.FieldSimp
import Mathlib.Tactic.NormNum
import Mathlib.Tactic.Ring

theorem wolfram_language_compute{theorem_binders} :
    Filter.Tendsto (fun {limit_variable} : ℝ => {limit_body})
      (nhds {point_lean}) (nhds {result_lean}) := by
{denominator_proofs}  have {hcont} : ContinuousAt (fun {limit_variable} : ℝ => {limit_body})
      {point_lean} := {continuity_proof}
{result_denominator_source}  have {hvalue} : {value_at_point} = {result_lean} := by {value_proof}
  simpa only [{hvalue}] using {hcont}.tendsto

#print axioms wolfram_language_compute
"""
    domain_conditions = continuity_state["domain_conditions"]
    if not isinstance(domain_conditions, list):
        return None
    return Computation(
        result.replace("**", "^"),
        source,
        domain_conditions=tuple(domain_conditions),
        real_parameters=tuple(theorem_symbols),
    )


def _rational_continuity_proof(
    node: ast.AST,
    variable: str,
    point: Fraction | None,
    point_lean: str,
    point_display: str,
    state: dict[str, object],
    depth: int,
) -> str | None:
    state["nodes"] = int(state["nodes"]) + 1
    if int(state["nodes"]) > 256 or depth > 32:
        return None
    if _constant_fraction(node) is not None:
        return "continuousAt_const"
    if isinstance(node, ast.Name):
        return "continuousAt_id" if node.id == variable else "continuousAt_const"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        inner = _rational_continuity_proof(
            node.operand, variable, point, point_lean, point_display, state, depth + 1
        )
        if inner is None:
            return None
        return inner if isinstance(node.op, ast.UAdd) else f"({inner}).neg"
    if not isinstance(node, ast.BinOp):
        return None

    left = _rational_continuity_proof(
        node.left, variable, point, point_lean, point_display, state, depth + 1
    )
    if left is None:
        return None
    if isinstance(node.op, ast.Pow):
        exponent = _constant_fraction(node.right)
        if exponent is None or exponent.denominator != 1 or not 0 <= exponent <= 16:
            return None
        if exponent == 0:
            return "continuousAt_const"
        return f"({left}).pow {exponent.numerator}"
    if isinstance(node.op, ast.Div):
        denominator = _constant_fraction(node.right)
        if denominator is not None:
            if denominator == 0:
                return None
            return f"({left}).div_const {_lean_rational(denominator)}"
        denominator_value = (
            _exact_rational_node_value(node.right, variable, point)
            if point is not None
            else None
        )
        if denominator_value == 0:
            return None
        right = _rational_continuity_proof(
            node.right, variable, point, point_lean, point_display, state, depth + 1
        )
        if right is None:
            return None
        names: set[str] = set()
        denominator_at_point = _render_rational_limit_node(
            node.right,
            names,
            {"nodes": 0},
            0,
            substitutions={variable: point_lean},
        )
        if denominator_at_point is None:
            return None
        used_names = state["used_names"]
        denominator_proofs = state["denominator_assumptions"]
        domain_binders = state["domain_binders"]
        domain_conditions = state["domain_conditions"]
        assumed_denominators = state["assumed_denominators"]
        if (
            not isinstance(used_names, set)
            or not isinstance(denominator_proofs, list)
            or not isinstance(domain_binders, list)
            or not isinstance(domain_conditions, list)
            or not isinstance(assumed_denominators, list)
        ):
            return None
        hypothesis_name = _fresh_identifier("h_limit_denominator", used_names)
        if denominator_value is None:
            domain_binders.append(f"({hypothesis_name} : {denominator_at_point} ≠ 0)")
            assumed_denominators.append((hypothesis_name, denominator_at_point))
            denominator_source = ast.unparse(node.right).replace("**", "^")
            if any(
                isinstance(child, ast.Name) and child.id == variable
                for child in ast.walk(node.right)
            ):
                condition = (
                    f"{denominator_source} ≠ 0 at {variable} = {point_display}"
                )
            else:
                condition = f"{denominator_source} ≠ 0"
            domain_conditions.append(condition)
        else:
            denominator_proofs.append((hypothesis_name, denominator_at_point))
        return f"({left}).div ({right}) {hypothesis_name}"

    right = _rational_continuity_proof(
        node.right, variable, point, point_lean, point_display, state, depth + 1
    )
    if right is None:
        return None
    method = {
        ast.Add: "add",
        ast.Sub: "sub",
        ast.Mult: "mul",
    }.get(type(node.op))
    return f"({left}).{method} ({right})" if method is not None else None


def _rational_polynomial_within_limits(
    node: ast.AST,
    symbol_names: set[str],
) -> bool:
    import sympy as _sympy

    expression = _real_sympy_expression(node)
    if expression is None:
        return False
    symbols = [_sympy.Symbol(name) for name in sorted(symbol_names)]
    try:
        polynomial = _sympy.Poly(expression, *symbols, domain=_sympy.QQ)
    except (TypeError, ValueError, _sympy.PolynomialError):
        return False
    if polynomial.total_degree() > 64:
        return False
    return all(
        abs(int(coefficient.p)).bit_length() <= 256
        and int(coefficient.q).bit_length() <= 256
        for coefficient in polynomial.coeffs()
    )


def _rational_denominator_vanishes_at_symbolic_point(
    expression: ast.AST,
    variable: str,
    point: ast.AST,
) -> bool:
    for denominator in _variable_denominators(expression):
        at_point = _rational_node_value_at_point(
            denominator, variable, None, point
        )
        if at_point is None:
            return True
        if at_point == 0 or at_point.is_zero is True:
            return True
    return False


def _rational_node_value_at_point(
    node: ast.AST,
    variable: str,
    point: Fraction | None,
    point_tree: ast.AST | None,
) -> object | None:
    import sympy as _sympy

    expression = _real_sympy_expression(node)
    if expression is None:
        return None
    if point is not None:
        point_expression = _sympy.Rational(point.numerator, point.denominator)
    elif point_tree is not None:
        point_expression = _real_sympy_expression(point_tree)
        if point_expression is None:
            return None
    else:
        return None
    try:
        return _sympy.simplify(
            expression.subs(_sympy.Symbol(variable), point_expression)
        )
    except (TypeError, ValueError, RecursionError):
        return None


def _prepare_removable_rational_limit(
    expression_source: str,
    expression_tree: ast.Expression,
    limit_result: str,
    variable: str,
    point: Fraction | None,
    point_lean: str,
    point_display: str,
    point_tree: ast.AST | None,
    point_symbols: set[str],
    time_limit_seconds: float,
) -> Computation | None:
    symbolic_deadline = monotonic() + max(0.001, min(time_limit_seconds, 60.0))
    remaining_symbolic_time = symbolic_deadline - monotonic()
    if remaining_symbolic_time <= 0.001:
        return Computation("", "", timed_out=True)
    simplified = run_symbolic(SymbolicRequest(
        operation="simplify",
        expression=expression_source,
        time_limit_seconds=max(0.001, min(remaining_symbolic_time, 60.0)),
    ))
    if simplified.get("timed_out"):
        return Computation("", "", timed_out=True)
    if simplified.get("error") or not isinstance(simplified.get("result"), str):
        return None

    simplified_source = simplified["result"]
    simplified_lean, simplified_symbols = _render_rational_limit_lean(simplified_source)
    result_lean, result_symbols = _render_rational_limit_lean(limit_result)
    expression_lean, expression_symbols = _render_rational_limit_lean(expression_source)
    if simplified_lean is None or result_lean is None or expression_lean is None:
        return None
    try:
        simplified_tree = ast.parse(simplified_source, mode="eval")
    except (SyntaxError, RecursionError, ValueError):
        return None

    denominators = _variable_denominators(expression_tree.body)
    if not denominators:
        return None
    vanishing_denominators: list[
        tuple[ast.AST, list[tuple[str, ast.AST, int, Fraction | None, int | None]]]
    ] = []
    nonvanishing_denominators: list[
        tuple[ast.AST, Fraction | None, int, int | None]
    ] = []
    for denominator in denominators:
        denominator_value = _rational_node_value_at_point(
            denominator, variable, point, point_tree
        )
        if denominator_value is None:
            return None
        if denominator_value == 0 or getattr(denominator_value, "is_zero", None) is True:
            remaining_symbolic_time = symbolic_deadline - monotonic()
            if remaining_symbolic_time <= 0.001:
                return Computation("", "", timed_out=True)
            factors, factor_timed_out = _factor_limit_denominator(
                denominator,
                max(0.001, min(remaining_symbolic_time, 60.0)),
            )
            if factor_timed_out:
                return Computation("", "", timed_out=True)
            if factors is None:
                return None
            group_index = len(vanishing_denominators)
            components: list[
                tuple[str, ast.AST, int, Fraction | None, int | None]
            ] = []
            has_vanishing_factor = False
            for factor, multiplicity in factors:
                if _variable_denominators(factor):
                    return None
                factor_value = _rational_node_value_at_point(
                    factor, variable, point, point_tree
                )
                if factor_value is None:
                    return None
                if factor_value == 0 or getattr(factor_value, "is_zero", None) is True:
                    vanishing_factor = _vanishing_affine_power(
                        factor, variable, point
                    ) if point is not None else None
                    if point is None:
                        if point_tree is None:
                            return None
                        vanishing_factor = _vanishing_affine_power_at_symbolic_point(
                            factor, variable, point_tree
                        )
                    if vanishing_factor is None:
                        return None
                    slope, affine_exponent = vanishing_factor
                    exponent = multiplicity * affine_exponent
                    if exponent > 16:
                        return None
                    components.append(("vanishing", factor, exponent, slope, None))
                    has_vanishing_factor = True
                else:
                    rational_value = None
                    if getattr(factor_value, "is_Rational", False):
                        rational_value = Fraction(
                            int(factor_value.p), int(factor_value.q)
                        )
                    factor_index = len(nonvanishing_denominators)
                    nonvanishing_denominators.append(
                        (factor, rational_value, multiplicity, group_index)
                    )
                    components.append(
                        ("nonvanishing", factor, multiplicity, None, factor_index)
                    )
            if not has_vanishing_factor:
                return None
            vanishing_denominators.append((denominator, components))
        else:
            rational_value = None
            if getattr(denominator_value, "is_Rational", False):
                rational_value = Fraction(
                    int(denominator_value.p), int(denominator_value.q)
                )
            nonvanishing_denominators.append((denominator, rational_value, 1, None))

    used_names = (
        expression_symbols | simplified_symbols | result_symbols | point_symbols | {variable}
    )
    limit_variable = _fresh_identifier("limit_variable", used_names)
    expression = _render_rational_limit_node(
        expression_tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: limit_variable},
    )
    simplified_expression = _render_rational_limit_node(
        simplified_tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: limit_variable},
    )
    simplified_value = _render_rational_limit_node(
        simplified_tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: point_lean},
    )
    if expression is None or simplified_expression is None or simplified_value is None:
        return None
    real_simplified_expression = f"({simplified_expression} : ℝ)"
    real_simplified_value = f"({simplified_value} : ℝ)"

    continuity_state: dict[str, object] = {
        "nodes": 0,
        "denominator_assumptions": [],
        "assumed_denominators": [],
        "domain_binders": [],
        "domain_conditions": [],
        "used_names": used_names | {limit_variable},
    }
    continuity_proof = _rational_continuity_proof(
        simplified_tree.body,
        variable,
        point,
        point_lean,
        point_display,
        continuity_state,
        0,
    )
    if continuity_proof is None:
        return None

    denominator_continuity_proofs: list[
        tuple[int, str, str, str, str, str, str]
    ] = []
    domain_binders = continuity_state["domain_binders"]
    domain_conditions = continuity_state["domain_conditions"]
    assumed_denominators = continuity_state["assumed_denominators"]
    denominator_assumptions = continuity_state["denominator_assumptions"]
    if (
        not isinstance(domain_binders, list)
        or not isinstance(domain_conditions, list)
        or not isinstance(assumed_denominators, list)
        or not isinstance(denominator_assumptions, list)
    ):
        return None
    for index, (denominator, rational_value, _exponent, _group_index) in enumerate(
        nonvanishing_denominators
    ):
        denominator_body = _render_rational_limit_node(
            denominator,
            set(),
            {"nodes": 0},
            0,
            substitutions={variable: limit_variable},
        )
        denominator_at_point = _render_rational_limit_node(
            denominator,
            set(),
            {"nodes": 0},
            0,
            substitutions={variable: point_lean},
        )
        if denominator_body is None or denominator_at_point is None:
            return None
        denominator_continuity = _rational_continuity_proof(
            denominator,
            variable,
            point,
            point_lean,
            point_display,
            continuity_state,
            0,
        )
        if denominator_continuity is None:
            return None

        point_nonzero = next(
            (
                name
                for name, expression in assumed_denominators
                if expression == denominator_at_point
            ),
            None,
        )
        if point_nonzero is None:
            point_nonzero = next(
                (
                    name
                    for name, expression in denominator_assumptions
                    if expression == denominator_at_point
                ),
                None,
            )
        if point_nonzero is None:
            point_nonzero = _fresh_identifier(
                f"h_limit_denominator_at_point_{index}", continuity_state["used_names"]
            )
            if rational_value is not None:
                if rational_value == 0:
                    return None
                denominator_assumptions.append((point_nonzero, denominator_at_point))
            else:
                domain_binders.append(
                    f"({point_nonzero} : ({denominator_at_point} : ℝ) ≠ 0)"
                )
                assumed_denominators.append((point_nonzero, denominator_at_point))
                denominator_source = ast.unparse(denominator).replace("**", "^")
                if any(
                    isinstance(child, ast.Name) and child.id == variable
                    for child in ast.walk(denominator)
                ):
                    condition = (
                        f"{denominator_source} ≠ 0 at {variable} = {point_display}"
                    )
                else:
                    condition = f"{denominator_source} ≠ 0"
                domain_conditions.append(condition)

        hden_continuous = _fresh_identifier(
            f"h_limit_denominator_continuous_{index}", continuity_state["used_names"]
        )
        hden_eventually = _fresh_identifier(
            f"h_limit_denominator_eventually_{index}", continuity_state["used_names"]
        )
        hden_eventually_within = _fresh_identifier(
            f"h_limit_denominator_eventually_within_{index}",
            continuity_state["used_names"],
        )
        denominator_continuity_proofs.append(
            (
                index,
                hden_continuous,
                hden_eventually,
                hden_eventually_within,
                denominator_body,
                denominator_continuity,
                point_nonzero,
            )
        )

    theorem_symbols = sorted(
        (expression_symbols | simplified_symbols | result_symbols | point_symbols)
        - {variable, limit_variable}
    )
    binders = " ".join(
        [*(f"({name} : ℝ)" for name in theorem_symbols), *domain_binders]
    )
    theorem_binders = f" {binders}" if binders else ""
    hcont = _fresh_identifier("h_limit_continuous", used_names)
    hvalue = _fresh_identifier("h_limit_value", used_names)
    heq = _fresh_identifier("h_limit_eventual_eq", used_names)
    hlim = _fresh_identifier("h_limit_tendsto", used_names)
    hcontinuous_limit = _fresh_identifier("h_limit_continuous_tendsto", used_names)
    hfilter_mem = _fresh_identifier("h_limit_filter_mem", used_names)
    hpoint = _fresh_identifier("h_limit_point", used_names)
    eventual_names: list[str] = []
    eventual_hypotheses: list[str] = []
    eventual_proof_blocks: list[str] = []
    for (
        _index,
        hden_continuous,
        hden_eventually,
        hden_eventually_within,
        denominator_body,
        denominator_continuity,
        point_nonzero,
    ) in denominator_continuity_proofs:
        event_variable = _fresh_identifier("limit_neighborhood_variable", used_names)
        event_body = f"({denominator_body.replace(limit_variable, event_variable)} : ℝ)"
        eventual_names.append(hden_eventually_within)
        eventual_hypothesis = _fresh_identifier(
            "h_limit_denominator_nonzero", used_names
        )
        eventual_hypotheses.append(eventual_hypothesis)
        eventual_proof_blocks.append(
            f"  have {hden_continuous} : ContinuousAt "
            f"(fun {event_variable} : ℝ => {event_body}) {point_lean} := "
            f"{denominator_continuity}\n"
            f"  have {hden_eventually} : ∀ᶠ {event_variable} in nhds {point_lean}, "
            f"{event_body} ≠ 0 := {hden_continuous}.eventually_ne {point_nonzero}\n"
            f"  have {hden_eventually_within} : ∀ᶠ {event_variable} in "
            f"nhdsWithin {point_lean} ({{{point_lean}}}ᶜ), {event_body} ≠ 0 := "
            f"{hden_eventually}.filter_mono nhdsWithin_le_nhds"
        )

    denominator_proofs: list[str] = []
    denominator_names: list[str] = []
    factorization_names: list[str] = []
    factor_nonzero_names: list[str] = []
    for index, (denominator, components) in enumerate(vanishing_denominators):
        denominator_used_names = continuity_state["used_names"]
        if not isinstance(denominator_used_names, set):
            return None
        hden = _fresh_identifier(f"h_limit_denominator_{index}", denominator_used_names)
        denominator_term = _render_rational_limit_node(
            denominator,
            set(),
            {"nodes": 0},
            0,
            substitutions={variable: limit_variable},
        )
        if denominator_term is None:
            return None

        component_terms: list[str] = []
        component_names: list[str] = []
        proof_lines: list[str] = []
        for component_index, (
            component_kind,
            component,
            exponent,
            slope,
            nonvanishing_index,
        ) in enumerate(components):
            component_body = _render_rational_limit_node(
                component,
                set(),
                {"nodes": 0},
                0,
                substitutions={variable: limit_variable},
            )
            if component_body is None:
                return None
            component_value = f"({component_body} : ℝ)"
            component_term = (
                f"({component_value})"
                if exponent == 1
                else f"({component_value} ^ {exponent})"
            )
            component_terms.append(component_term)
            component_name = _fresh_identifier(
                f"h_limit_denominator_factor_{index}_{component_index}",
                denominator_used_names,
            )
            component_names.append(component_name)
            factor_nonzero_names.append(component_name)

            if component_kind == "vanishing":
                if slope is None:
                    return None
                hdiff = _fresh_identifier(
                    f"h_limit_point_ne_{index}_{component_index}",
                    denominator_used_names,
                )
                hzero = _fresh_identifier(
                    f"h_limit_point_eq_{index}_{component_index}",
                    denominator_used_names,
                )
                hslope = _fresh_identifier(
                    f"h_limit_slope_{index}_{component_index}",
                    denominator_used_names,
                )
                hfactor = _fresh_identifier(
                    f"h_limit_affine_factor_{index}_{component_index}",
                    denominator_used_names,
                )
                slope_lean = _lean_rational(slope)
                affine_factor = f"({slope_lean} * ({limit_variable} - {point_lean}))"
                proof_lines.extend(
                    [
                        f"  have {hdiff} : {limit_variable} - {point_lean} ≠ 0 := by",
                        f"    intro {hzero}",
                        f"    apply {hpoint}",
                        "    linarith",
                        f"  have {hslope} : {slope_lean} ≠ 0 := by norm_num",
                        f"  have {hfactor} : {component_value} = {affine_factor} := by ring",
                        f"  have {component_name} : {component_term} ≠ 0 := by",
                        f"    rw [{hfactor}]",
                    ]
                )
                if exponent == 1:
                    proof_lines.append(
                        f"    exact mul_ne_zero {hslope} {hdiff}"
                    )
                else:
                    proof_lines.append(
                        f"    exact pow_ne_zero {exponent} "
                        f"(mul_ne_zero {hslope} {hdiff})"
                    )
            else:
                if nonvanishing_index is None:
                    return None
                factor_nonzero = eventual_hypotheses[nonvanishing_index]
                nonzero_proof = (
                    f"exact {factor_nonzero}"
                    if exponent == 1
                    else f"exact pow_ne_zero {exponent} {factor_nonzero}"
                )
                proof_lines.extend(
                    [
                        f"  have {component_name} : {component_term} ≠ 0 := by",
                        f"    {nonzero_proof}",
                    ]
                )

        hfactorization = _fresh_identifier(
            f"h_limit_denominator_product_{index}", denominator_used_names
        )
        factorization_names.append(hfactorization)
        product_term = " * ".join(component_terms)
        product_nonzero = component_names[0]
        for component_name in component_names[1:]:
            product_nonzero = f"mul_ne_zero ({product_nonzero}) {component_name}"
        proof_lines.extend(
            [
                f"  have {hfactorization} : {denominator_term} = {product_term} := by ring",
                f"  have {hden} : {denominator_term} ≠ 0 := by",
                f"    rw [{hfactorization}]",
                f"    exact {product_nonzero}",
            ]
        )
        denominator_proofs.append("\n".join(proof_lines))
        denominator_names.append(hden)

    direct_eventual_hypotheses = [
        eventual_hypotheses[index]
        for index, (_denominator, _value, _exponent, group_index)
        in enumerate(nonvanishing_denominators)
        if group_index is None
    ]
    all_denominator_names = [
        *factor_nonzero_names,
        *denominator_names,
        *direct_eventual_hypotheses,
        *factorization_names,
    ]
    field_simp = f"field_simp [{', '.join(all_denominator_names)}] <;> ring"
    point_denominator_proof_source = "\n".join(
        f"  have {name} : ({denominator} : ℝ) ≠ 0 := by norm_num"
        for name, denominator in denominator_assumptions
    )
    point_denominator_proof_source = (
        f"{point_denominator_proof_source}\n"
        if point_denominator_proof_source
        else ""
    )
    value_denominator_names = [
        *(name for name, _ in assumed_denominators),
        *(name for name, _ in denominator_assumptions),
    ]
    value_proof = (
        f"field_simp [{', '.join(value_denominator_names)}] <;> ring"
        if value_denominator_names
        else "ring"
    )
    vanishing_denominator_proof_source = "\n".join(
        "  " + line
        for proof in denominator_proofs
        for line in proof.splitlines()
    )
    eventual_proof_source = "\n".join(eventual_proof_blocks)
    eventual_proof_source = f"{eventual_proof_source}\n" if eventual_proof_source else ""
    factorization_rewrite_source = (
        f"    rw [{', '.join(factorization_names)}]\n"
        if factorization_names
        else ""
    )
    source = f"""import Mathlib.Basic.Real.Basic
import Mathlib.Topology.Instances.Real.Lemmas
import Mathlib.Topology.Continuous
import Mathlib.Tactic.FieldSimp
import Mathlib.Tactic.NormNum
import Mathlib.Tactic.Ring

theorem wolfram_language_compute{theorem_binders} :
    Filter.Tendsto (fun {limit_variable} : ℝ => {expression})
      (nhdsWithin {point_lean} ({{{point_lean}}}ᶜ)) (nhds {result_lean}) := by
{point_denominator_proof_source}  have {hcont} : ContinuousAt (fun {limit_variable} : ℝ => {real_simplified_expression})
      {point_lean} := {continuity_proof}
  have {hvalue} : {real_simplified_value} = {result_lean} := by
    {value_proof}
{eventual_proof_source}  have {heq} : (fun {limit_variable} : ℝ => {expression}) =ᶠ[
      nhdsWithin {point_lean} ({{{point_lean}}}ᶜ)]
      (fun {limit_variable} : ℝ => {real_simplified_expression}) := by
    filter_upwards [self_mem_nhdsWithin{''.join(', ' + name for name in eventual_names)}]
        with {limit_variable} {hfilter_mem}{''.join(' ' + name for name in eventual_hypotheses)}
    have {hpoint} : {limit_variable} ≠ {point_lean} := by simpa using {hfilter_mem}
{vanishing_denominator_proof_source}
{factorization_rewrite_source}
    {field_simp}
  have {hlim} : Filter.Tendsto (fun {limit_variable} : ℝ => {real_simplified_expression})
      (nhdsWithin {point_lean} ({{{point_lean}}}ᶜ)) (nhds {result_lean}) := by
    have {hcontinuous_limit} := tendsto_nhdsWithin_of_tendsto_nhds
      (s := ({{{point_lean}}}ᶜ)) {hcont}.tendsto
    simpa only [{hvalue}] using {hcontinuous_limit}
  exact (Filter.tendsto_congr' {heq}).2 {hlim}

#print axioms wolfram_language_compute
"""
    return Computation(
        limit_result.replace("**", "^"),
        source,
        domain_conditions=tuple(dict.fromkeys(domain_conditions)),
        real_parameters=tuple(theorem_symbols),
    )


def _flatten_multiplicative_factors(
    node: ast.AST,
) -> list[tuple[ast.AST, int]] | None:
    """Return explicit product factors with their bounded integer multiplicities."""
    factors: list[tuple[ast.AST, int]] = []

    def collect(current: ast.AST, multiplicity: int) -> bool:
        if isinstance(current, ast.BinOp) and isinstance(current.op, ast.Mult):
            return collect(current.left, multiplicity) and collect(
                current.right, multiplicity
            )
        if isinstance(current, ast.BinOp) and isinstance(current.op, ast.Pow):
            exponent = _constant_fraction(current.right)
            if exponent is None or exponent.denominator != 1 or exponent < 0:
                return False
            if exponent == 0:
                return True
            total_multiplicity = multiplicity * exponent.numerator
            if total_multiplicity > 16:
                return False
            return collect(current.left, total_multiplicity)
        factors.append((current, multiplicity))
        return len(factors) <= 256

    if not collect(node, 1) or not factors:
        return None
    return factors


def _factor_limit_denominator(
    node: ast.AST,
    time_limit_seconds: float,
) -> tuple[list[tuple[ast.AST, int]] | None, bool]:
    explicit_factors = _flatten_multiplicative_factors(node)
    if explicit_factors is None:
        return None, False
    try:
        expression_source = ast.unparse(node)
    except (RecursionError, ValueError):
        return explicit_factors, False
    factored = run_symbolic(SymbolicRequest(
        operation="factor",
        expression=expression_source,
        time_limit_seconds=max(0.001, min(time_limit_seconds, 60.0)),
    ))
    if factored.get("timed_out"):
        return None, True
    if factored.get("error") or not isinstance(factored.get("result"), str):
        return explicit_factors, False
    try:
        factored_tree = ast.parse(factored["result"], mode="eval")
    except (SyntaxError, RecursionError, ValueError):
        return explicit_factors, False
    factors = _flatten_multiplicative_factors(factored_tree.body)
    if factors is None or len(factors) > 256:
        return explicit_factors, False
    if any(not 1 <= multiplicity <= 16 for _factor, multiplicity in factors):
        return explicit_factors, False
    if any(
        _render_rational_limit_node(factor, set(), {"nodes": 0}, 0) is None
        or _variable_denominators(factor)
        for factor, _multiplicity in factors
    ):
        return explicit_factors, False
    return factors, False


def _vanishing_affine_power(
    node: ast.AST,
    variable: str,
    point: Fraction,
) -> tuple[Fraction, int] | None:
    exponent = 1
    affine_node = node
    while isinstance(affine_node, ast.BinOp) and isinstance(affine_node.op, ast.Pow):
        power = _constant_fraction(affine_node.right)
        if power is None or power.denominator != 1 or not 1 <= power <= 16:
            return None
        exponent *= power.numerator
        if exponent > 16:
            return None
        affine_node = affine_node.left
    linear = _linear_rational_form(affine_node, variable)
    if linear is None:
        return None
    slope, intercept = linear
    if slope == 0 or slope * point + intercept != 0:
        return None
    return slope, exponent


def _vanishing_affine_power_at_symbolic_point(
    node: ast.AST,
    variable: str,
    point: ast.AST,
) -> tuple[Fraction, int] | None:
    import sympy as _sympy

    exponent = 1
    affine_node = node
    while isinstance(affine_node, ast.BinOp) and isinstance(affine_node.op, ast.Pow):
        power = _constant_fraction(affine_node.right)
        if power is None or power.denominator != 1 or not 1 <= power <= 16:
            return None
        exponent *= power.numerator
        if exponent > 16:
            return None
        affine_node = affine_node.left

    affine_expression = _real_sympy_expression(affine_node)
    point_expression = _real_sympy_expression(point)
    if affine_expression is None or point_expression is None:
        return None
    symbol = _sympy.Symbol(variable)
    try:
        slope = _sympy.simplify(_sympy.diff(affine_expression, symbol))
        intercept = _sympy.simplify(affine_expression - slope * symbol)
        at_point = _sympy.simplify(affine_expression.subs(symbol, point_expression))
    except (TypeError, ValueError, RecursionError):
        return None
    if (
        slope.has(symbol)
        or intercept.has(symbol)
        or not slope.is_Rational
        or slope == 0
        or not (at_point == 0 or at_point.is_zero is True)
    ):
        return None
    slope_fraction = Fraction(int(slope.p), int(slope.q))
    if (
        abs(slope_fraction.numerator).bit_length() > 256
        or slope_fraction.denominator.bit_length() > 256
    ):
        return None
    return slope_fraction, exponent


def _linear_rational_form(
    node: ast.AST,
    variable: str,
) -> tuple[Fraction, Fraction] | None:
    constant = _constant_fraction(node)
    if constant is not None:
        return Fraction(0), constant
    if isinstance(node, ast.Name):
        return (Fraction(1), Fraction(0)) if node.id == variable else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        form = _linear_rational_form(node.operand, variable)
        if form is None or isinstance(node.op, ast.UAdd):
            return form
        return -form[0], -form[1]
    if not isinstance(node, ast.BinOp):
        return None
    left = _linear_rational_form(node.left, variable)
    right = _linear_rational_form(node.right, variable)
    if left is None or right is None:
        return None
    if isinstance(node.op, ast.Add):
        return left[0] + right[0], left[1] + right[1]
    if isinstance(node.op, ast.Sub):
        return left[0] - right[0], left[1] - right[1]
    if isinstance(node.op, ast.Mult):
        if left[0] == 0:
            return right[0] * left[1], right[1] * left[1]
        if right[0] == 0:
            return left[0] * right[1], left[1] * right[1]
        return None
    if isinstance(node.op, ast.Div):
        if right[0] != 0 or right[1] == 0:
            return None
        return left[0] / right[1], left[1] / right[1]
    if isinstance(node.op, ast.Pow):
        exponent = _constant_fraction(node.right)
        if exponent is None or exponent.denominator != 1:
            return None
        if exponent == 0:
            return Fraction(0), Fraction(1)
        if exponent == 1:
            return left
    return None


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
        integrand_lean, integrand_symbols = _render_real_calculus_lean(
            parsed.expression, allow_variable_denominators=True
        )
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
        antiderivative_lean, antiderivative_symbols = _render_real_calculus_lean(
            antiderivative, allow_variable_denominators=True
        )
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
        definite_result_tree = ast.parse(definite_result, mode="eval")
        definite_result_lean, definite_result_symbols = _render_real_calculus_lean(
            definite_result, allow_variable_denominators=True
        )
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
    lean_lower = _lean_rational(lower)
    lean_upper = _lean_rational(upper)

    def interval_lemma_source(name: str, property_source: str) -> str:
        return f"""  have {name} : ∀ {point_variable} : ℝ,
      {point_variable} ∈ Set.uIcc {lean_lower} {lean_upper} → {property_source} := by
    intro {point_variable} hpoint
    rw [Set.mem_uIcc] at hpoint
    rcases hpoint with hbound | hbound
    · nlinarith [hbound.1, hbound.2]
    · nlinarith [hbound.1, hbound.2]"""

    domain_expressions: list[ast.AST] = []
    seen_domain_expressions: set[str] = set()
    import sympy as _sympy

    for expression_root in (expression_tree.body, antiderivative_tree.body):
        domain_candidates = [
            *_variable_denominators(expression_root),
            *_log_arguments(expression_root),
        ]
        tangent_cosines = _tan_cosine_arguments(expression_root)
        if tangent_cosines is None:
            return None
        domain_candidates.extend(tangent_cosines)
        for candidate in domain_candidates:
            key = ast.dump(candidate, include_attributes=False)
            if key in seen_domain_expressions:
                continue
            constant = _constant_fraction(candidate)
            if constant is not None:
                if constant == 0:
                    return None
                continue
            seen_domain_expressions.add(key)
            domain_expressions.append(candidate)

    inverse_trig_arguments = _inverse_trig_arguments(antiderivative_tree.body)
    positive_sqrt_arguments = _positive_sqrt_arguments(antiderivative_tree.body)
    if inverse_trig_arguments is None or positive_sqrt_arguments is None:
        return None

    integrand_at_integral_var = _render_real_calculus_node(
        expression_tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: integration_variable},
        allow_variable_denominators=True,
    )
    integrand_at_point = _render_real_calculus_node(
        expression_tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: point_variable},
        allow_variable_denominators=True,
    )
    antiderivative_at_integral_var = _render_real_calculus_node(
        antiderivative_tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: integration_variable},
        allow_variable_denominators=True,
    )
    if (
        integrand_at_integral_var is None
        or integrand_at_point is None
        or antiderivative_at_integral_var is None
    ):
        return None

    domain_hypotheses: dict[str, str] = {}
    positive_hypotheses: dict[str, str] = {}
    interval_domain_binders: list[str] = []
    interval_domain_proofs: list[str] = []
    interval_domain_lemmas: list[str] = []
    domain_conditions: list[str] = []
    domain_condition_sympy_expressions: list[object] = []
    for index, domain_expression in enumerate(domain_expressions):
        key = ast.dump(domain_expression, include_attributes=False)
        rendered_domain = _render_real_calculus_node(
            domain_expression,
            set(),
            {"nodes": 0},
            0,
            substitutions={variable: point_variable},
            allow_variable_denominators=True,
        )
        if rendered_domain is None:
            return None
        affine_values = _affine_interval_values(
            domain_expression, variable, lower, upper
        )
        if affine_values is not None:
            affine_lower, affine_upper = affine_values
            if (affine_lower > 0 and affine_upper > 0) or (
                affine_lower < 0 and affine_upper < 0
            ):
                hypothesis_name = fresh_name(
                    f"h_interval_auto_domain_{index}", used_names
                )
                domain_hypotheses[key] = (
                    f"({hypothesis_name} {point_variable} hpoint)"
                )
                interval_domain_lemmas.append(
                    interval_lemma_source(
                        hypothesis_name, f"{rendered_domain} ≠ 0"
                    )
                )
                interval_domain_proofs.append(
                    f"{hypothesis_name} {point_variable} hpoint"
                )
                continue
        hypothesis_name = fresh_name(f"h_interval_domain_{index}", used_names)
        domain_hypotheses[key] = (
            f"({hypothesis_name} {point_variable} hpoint)"
        )
        interval_domain_binders.append(
            f"({hypothesis_name} : ∀ {point_variable} : ℝ, "
            f"{point_variable} ∈ Set.uIcc {lean_lower} {lean_upper} → "
            f"{rendered_domain} ≠ 0)"
        )
        interval_domain_proofs.append(
            f"{hypothesis_name} {point_variable} hpoint"
        )
        if (
            isinstance(domain_expression, ast.Call)
            and isinstance(domain_expression.func, ast.Name)
            and domain_expression.func.id == "cos"
        ):
            source_expression = (
                f"Cos[{ast.unparse(domain_expression.args[0]).replace('**', '^')}]"
            )
        else:
            source_expression = ast.unparse(domain_expression).replace("**", "^")
        sympy_domain_expression = _real_sympy_expression(domain_expression)
        duplicate_condition = False
        if (
            sympy_domain_expression is not None
            and len(domain_condition_sympy_expressions) < 32
        ):
            duplicate_condition = any(
                _sympy.simplify(sympy_domain_expression - existing) == 0
                or _sympy.simplify(sympy_domain_expression + existing) == 0
                for existing in domain_condition_sympy_expressions
            )
            if not duplicate_condition:
                domain_condition_sympy_expressions.append(sympy_domain_expression)
        if not duplicate_condition:
            domain_conditions.append(
                f"throughout the closed interval with endpoints {_fraction_text(lower)} "
                f"and {_fraction_text(upper)}, "
                f"{source_expression} ≠ 0"
            )

    for index, argument in enumerate(inverse_trig_arguments):
        rendered_argument = _render_real_calculus_node(
            argument,
            set(),
            {"nodes": 0},
            0,
            substitutions={variable: point_variable},
            allow_variable_denominators=True,
        )
        if rendered_argument is None:
            return None
        lower_key = _inverse_trig_hypothesis_key(argument, "lower")
        upper_key = _inverse_trig_hypothesis_key(argument, "upper")
        affine_values = _affine_interval_values(argument, variable, lower, upper)
        source_expression = ast.unparse(argument).replace("**", "^")
        if affine_values is not None:
            affine_lower, affine_upper = affine_values
            minimum, maximum = min(affine_lower, affine_upper), max(
                affine_lower, affine_upper
            )
        else:
            minimum = maximum = None

        if minimum is not None and minimum > -1:
            lower_name = fresh_name(
                f"h_interval_auto_inverse_lower_{index}", used_names
            )
            domain_hypotheses[lower_key] = (
                f"({lower_name} {point_variable} hpoint)"
            )
            interval_domain_lemmas.append(
                interval_lemma_source(
                    lower_name, f"-1 < {rendered_argument}"
                )
            )
        else:
            lower_name = fresh_name(f"h_interval_inverse_lower_{index}", used_names)
            domain_hypotheses[lower_key] = (
                f"({lower_name} {point_variable} hpoint)"
            )
            interval_domain_binders.append(
                f"({lower_name} : ∀ {point_variable} : ℝ, "
                f"{point_variable} ∈ Set.uIcc {lean_lower} {lean_upper} → "
                f"-1 < {rendered_argument})"
            )
            domain_conditions.append(
                f"throughout the closed interval with endpoints {_fraction_text(lower)} "
                f"and {_fraction_text(upper)}, -1 < {source_expression}"
            )

        if maximum is not None and maximum < 1:
            upper_name = fresh_name(
                f"h_interval_auto_inverse_upper_{index}", used_names
            )
            domain_hypotheses[upper_key] = (
                f"({upper_name} {point_variable} hpoint)"
            )
            interval_domain_lemmas.append(
                interval_lemma_source(
                    upper_name, f"{rendered_argument} < 1"
                )
            )
        else:
            upper_name = fresh_name(f"h_interval_inverse_upper_{index}", used_names)
            domain_hypotheses[upper_key] = (
                f"({upper_name} {point_variable} hpoint)"
            )
            interval_domain_binders.append(
                f"({upper_name} : ∀ {point_variable} : ℝ, "
                f"{point_variable} ∈ Set.uIcc {lean_lower} {lean_upper} → "
                f"{rendered_argument} < 1)"
            )
            domain_conditions.append(
                f"throughout the closed interval with endpoints {_fraction_text(lower)} "
                f"and {_fraction_text(upper)}, {source_expression} < 1"
            )

    for index, argument in enumerate(positive_sqrt_arguments):
        key = ast.dump(argument, include_attributes=False)
        rendered_argument = _render_real_calculus_node(
            argument,
            set(),
            {"nodes": 0},
            0,
            substitutions={variable: point_variable},
            allow_variable_denominators=True,
        )
        if rendered_argument is None:
            return None
        source_expression = ast.unparse(argument).replace("**", "^")
        affine_values = _affine_interval_values(argument, variable, lower, upper)
        if affine_values is not None and min(affine_values) > 0:
            hypothesis_name = fresh_name(
                f"h_interval_auto_positive_{index}", used_names
            )
            positive_hypotheses[key] = (
                f"({hypothesis_name} {point_variable} hpoint)"
            )
            interval_domain_lemmas.append(
                interval_lemma_source(
                    hypothesis_name, f"0 < {rendered_argument}"
                )
            )
            interval_domain_proofs.append(
                f"ne_of_gt ({hypothesis_name} {point_variable} hpoint)"
            )
        else:
            hypothesis_name = fresh_name(
                f"h_interval_positive_{index}", used_names
            )
            positive_hypotheses[key] = (
                f"({hypothesis_name} {point_variable} hpoint)"
            )
            interval_domain_binders.append(
                f"({hypothesis_name} : ∀ {point_variable} : ℝ, "
                f"{point_variable} ∈ Set.uIcc {lean_lower} {lean_upper} → "
                f"0 < {rendered_argument})"
            )
            interval_domain_proofs.append(
                f"ne_of_gt ({hypothesis_name} {point_variable} hpoint)"
            )
            domain_conditions.append(
                f"throughout the closed interval with endpoints {_fraction_text(lower)} "
                f"and {_fraction_text(upper)}, {source_expression} > 0"
            )

    rendered_log_arguments: list[tuple[str, object]] = []
    log_sources = (
        (antiderivative_tree.body, {variable: lower}),
        (antiderivative_tree.body, {variable: upper}),
        (definite_result_tree.body, {}),
    )
    for log_root, substitutions in log_sources:
        for argument in _log_arguments(log_root):
            sympy_argument = _real_sympy_expression(argument, substitutions)
            rendered_argument = _render_real_calculus_node(
                argument,
                set(),
                {"nodes": 0},
                0,
                substitutions={
                    name: _lean_rational(value)
                    for name, value in substitutions.items()
                },
                allow_variable_denominators=True,
            )
            if sympy_argument is None or rendered_argument is None:
                continue
            if all(source != rendered_argument for source, _ in rendered_log_arguments):
                rendered_log_arguments.append((rendered_argument, sympy_argument))

    arcsin_half_endpoint = False
    inverse_trig_sources = (
        (antiderivative_tree.body, {variable: lower}),
        (antiderivative_tree.body, {variable: upper}),
        (definite_result_tree.body, {}),
    )
    for inverse_root, substitutions in inverse_trig_sources:
        for node in ast.walk(inverse_root):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Name)
                or node.func.id != "asin"
                or len(node.args) != 1
                or node.keywords
            ):
                continue
            argument = _real_sympy_expression(node.args[0], substitutions)
            if argument == _sympy.Rational(1, 2):
                arcsin_half_endpoint = True
                break
        if arcsin_half_endpoint:
            break

    arcsin_half_lemma_name = (
        fresh_name("h_arcsin_half", used_names) if arcsin_half_endpoint else None
    )
    arcsin_half_lemma_source = ""
    if arcsin_half_lemma_name is not None:
        arcsin_half_lemma_source = f"""
  have {arcsin_half_lemma_name} : Real.arcsin ((1 : ℝ) / 2) = Real.pi / 6 := by
    apply Real.arcsin_eq_of_sin_eq
    · exact Real.sin_pi_div_six
    · constructor <;> nlinarith [Real.pi_pos]"""

    log_equivalence_lemmas: list[tuple[str, str, str]] = []
    seen_log_equivalences: set[frozenset[str]] = set()
    if len(rendered_log_arguments) <= 32:
        for index, (left_source, left_expression) in enumerate(rendered_log_arguments):
            for right_source, right_expression in rendered_log_arguments[index + 1:]:
                equivalence_key = frozenset((left_source, right_source))
                if equivalence_key in seen_log_equivalences:
                    continue
                if _sympy.simplify(left_expression + right_expression) != 0:
                    continue
                seen_log_equivalences.add(equivalence_key)
                lemma_name = fresh_name(
                    f"h_log_sign_{len(log_equivalence_lemmas)}",
                    used_names,
                )
                log_equivalence_lemmas.append((lemma_name, left_source, right_source))

    proof = _derivative_proof_term(
        antiderivative_tree.body,
        variable,
        function_variable,
        point_variable,
        {"nodes": 0},
        0,
        domain_hypotheses=domain_hypotheses,
        positive_hypotheses=positive_hypotheses,
    )
    if proof is None:
        return None

    field_simp_arguments = (
        f" [{', '.join(interval_domain_proofs)}]" if interval_domain_proofs else ""
    )
    log_lemma_names = [name for name, _, _ in log_equivalence_lemmas]
    log_lemma_source = "\n".join(
        f"  have {name} : Real.log ({left}) = Real.log ({right}) := by\n"
        f"    rw [show ({left}) = -({right}) by ring]\n"
        f"    exact Real.log_neg_eq_log ({right})"
        for name, left, right in log_equivalence_lemmas
    )
    if log_lemma_source:
        log_lemma_source = "\n" + log_lemma_source
    final_simp_names = [
        *log_lemma_names,
        *([arcsin_half_lemma_name] if arcsin_half_lemma_name is not None else []),
    ]
    final_simplifier = (
        f"simp only [{', '.join(final_simp_names)}] <;> "
        if final_simp_names
        else ""
    )
    interval_domain_lemma_source = "\n".join(interval_domain_lemmas)
    if interval_domain_lemma_source:
        interval_domain_lemma_source = "\n" + interval_domain_lemma_source

    binders = " ".join(
        [
            *(f"({name} : ℝ)" for name in sorted(symbol_names - {variable})),
            *interval_domain_binders,
        ]
    )
    theorem_binders = f" {binders}" if binders else ""
    source = f"""import Mathlib.Analysis.Calculus.Deriv.Add
import Mathlib.Analysis.Calculus.Deriv.Mul
import Mathlib.Analysis.Calculus.Deriv.Pow
import Mathlib.Analysis.SpecialFunctions.ExpDeriv
import Mathlib.Analysis.SpecialFunctions.Log.Deriv
import Mathlib.Analysis.SpecialFunctions.Sqrt
import Mathlib.Analysis.SpecialFunctions.Trigonometric.ArctanDeriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.Deriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.DerivHyp
import Mathlib.Analysis.SpecialFunctions.Trigonometric.InverseDeriv
import Mathlib.MeasureTheory.Integral.IntervalIntegral.FundThmCalculus
import Mathlib.Tactic.FieldSimp
import Mathlib.Tactic.FunProp
import Mathlib.Tactic.Linarith
import Mathlib.Tactic.NormNum
import Mathlib.Tactic.Ring

theorem wolfram_language_compute{theorem_binders} :
    (∫ {integration_variable} in {lean_lower}..{lean_upper}, {integrand_at_integral_var}) = {definite_result_lean} := by
  let F : ℝ → ℝ := fun {integration_variable} : ℝ => {antiderivative_at_integral_var}
{interval_domain_lemma_source}
  have hderiv : ∀ {point_variable} ∈ Set.uIcc {lean_lower} {lean_upper},
      HasDerivAt F {integrand_at_point} {point_variable} := by
    intro {point_variable} hpoint
    convert ({proof}) using 1
    · ext {function_variable}
      simp [F, id_eq] <;> ring
    · simp [F, id_eq] <;> field_simp{field_simp_arguments} <;> ring
  have hcont : ContinuousOn
      (fun {integration_variable} : ℝ => {integrand_at_integral_var})
      (Set.uIcc {lean_lower} {lean_upper}) := by
    fun_prop
  have hint : IntervalIntegrable
      (fun {integration_variable} : ℝ => {integrand_at_integral_var})
      MeasureTheory.volume {lean_lower} {lean_upper} := by
    exact hcont.intervalIntegrable
  rw [intervalIntegral.integral_eq_sub_of_hasDerivAt hderiv hint]
{log_lemma_source}
{arcsin_half_lemma_source}
  norm_num [F] <;> {final_simplifier}field_simp <;> ring

#print axioms wolfram_language_compute
"""
    return Computation(
        definite_result.replace("**", "^"),
        source,
        domain_conditions=tuple(domain_conditions),
    )


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
        integrand_tree = ast.parse(parsed.expression, mode="eval")
        integrand_lean, integrand_symbols = _render_real_calculus_lean(
            parsed.expression, allow_variable_denominators=True
        )
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None
    if integrand_lean is None:
        return None
    positive_log_arguments = _positive_log_arguments(integrand_tree.body)
    if positive_log_arguments is None:
        return None
    positive_sqrt_arguments = _positive_sqrt_arguments(integrand_tree.body)
    if positive_sqrt_arguments is None:
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
    if (
        isinstance(integrand_tree.body, ast.Call)
        and isinstance(integrand_tree.body.func, ast.Name)
        and integrand_tree.body.func.id == "tanh"
        and len(integrand_tree.body.args) == 1
        and isinstance(integrand_tree.body.args[0], ast.Name)
        and integrand_tree.body.args[0].id == variable
    ):
        antiderivative = f"log(cosh({variable}))"
    elif (
        isinstance(integrand_tree.body, ast.Call)
        and isinstance(integrand_tree.body.func, ast.Name)
        and integrand_tree.body.func.id == "acosh"
        and len(integrand_tree.body.args) == 1
        and isinstance(integrand_tree.body.args[0], ast.Name)
        and integrand_tree.body.args[0].id == variable
    ):
        antiderivative = (
            f"{variable}*acosh({variable}) - "
            f"sqrt({variable}**2 - 1)"
        )
    try:
        tree = ast.parse(antiderivative, mode="eval")
        antiderivative_lean, antiderivative_symbols = _render_real_calculus_lean(
            antiderivative, allow_variable_denominators=True
        )
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None
    if antiderivative_lean is None:
        return None
    antiderivative_positive_sqrt_arguments = _positive_sqrt_arguments(tree.body)
    if antiderivative_positive_sqrt_arguments is None:
        return None

    domain_expressions = [
        *_variable_denominators(integrand_tree.body),
        *_variable_denominators(tree.body),
        *_log_arguments(tree.body),
    ]
    domain_hypotheses: dict[str, str] = {}
    positive_hypotheses: dict[str, str] = {}
    domain_binders: list[str] = []
    domain_condition_names: list[str] = []
    domain_conditions: list[str] = []
    used_names = set(integrand_symbols) | set(antiderivative_symbols) | {variable}

    positive_arguments = [
        *positive_log_arguments,
        *positive_sqrt_arguments,
        *antiderivative_positive_sqrt_arguments,
    ]
    for index, argument in enumerate(positive_arguments):
        argument_key = ast.dump(argument, include_attributes=False)
        if argument_key in positive_hypotheses:
            continue
        argument_lean = _render_real_calculus_node(
            argument,
            set(),
            {"nodes": 0},
            0,
            allow_variable_denominators=True,
        )
        if argument_lean is None:
            return None
        hypothesis_name = _fresh_identifier(f"h_positive_{index}", used_names)
        positive_hypotheses[argument_key] = hypothesis_name
        domain_binders.append(f"({hypothesis_name} : 0 < {argument_lean})")
        domain_condition_names.append(f"ne_of_gt {hypothesis_name}")
        argument_source = ast.unparse(argument).replace("**", "^")
        domain_conditions.append(f"{argument_source} > 0")

    inverse_trig_arguments = _inverse_trig_arguments(tree.body)
    if inverse_trig_arguments is None:
        return None
    inverse_hyperbolic_cosh_arguments = _inverse_hyperbolic_cosh_arguments(
        tree.body
    )
    if inverse_hyperbolic_cosh_arguments is None:
        return None
    for index, argument in enumerate(inverse_trig_arguments):
        argument_lean = _render_real_calculus_node(
            argument,
            set(),
            {"nodes": 0},
            0,
            allow_variable_denominators=True,
        )
        if argument_lean is None:
            return None
        argument_source = ast.unparse(argument).replace("**", "^")
        lower_name = _fresh_identifier(f"h_inverse_lower_{index}", used_names)
        upper_name = _fresh_identifier(f"h_inverse_upper_{index}", used_names)
        domain_hypotheses[
            _inverse_trig_hypothesis_key(argument, "lower")
        ] = lower_name
        domain_hypotheses[
            _inverse_trig_hypothesis_key(argument, "upper")
        ] = upper_name
        domain_binders.extend(
            [
                f"({lower_name} : -1 < {argument_lean})",
                f"({upper_name} : {argument_lean} < 1)",
            ]
        )
        domain_condition_names.extend([lower_name, upper_name])
        domain_conditions.extend(
            [f"-1 < {argument_source}", f"{argument_source} < 1"]
        )

    for index, argument in enumerate(inverse_hyperbolic_cosh_arguments):
        argument_lean = _render_real_calculus_node(
            argument,
            set(),
            {"nodes": 0},
            0,
            allow_variable_denominators=True,
        )
        if argument_lean is None:
            return None
        argument_source = ast.unparse(argument).replace("**", "^")
        hypothesis_name = _fresh_identifier(f"h_arcosh_domain_{index}", used_names)
        domain_hypotheses[
            _inverse_hyperbolic_cosh_hypothesis_key(argument)
        ] = hypothesis_name
        domain_binders.append(f"({hypothesis_name} : 1 < {argument_lean})")
        domain_condition_names.append(hypothesis_name)
        domain_conditions.append(f"{argument_source} > 1")

    for index, domain_expression in enumerate(domain_expressions):
        key = ast.dump(domain_expression, include_attributes=False)
        if key in domain_hypotheses or key in positive_hypotheses:
            continue
        if (
            isinstance(domain_expression, ast.Call)
            and isinstance(domain_expression.func, ast.Name)
            and domain_expression.func.id == "cosh"
            and len(domain_expression.args) == 1
            and not domain_expression.keywords
        ):
            argument_lean = _render_real_calculus_node(
                domain_expression.args[0],
                set(),
                {"nodes": 0},
                0,
                allow_variable_denominators=True,
            )
            if argument_lean is None:
                return None
            cosh_nonzero_proof = (
                f"(ne_of_gt (Real.cosh_pos {argument_lean}))"
            )
            domain_hypotheses[key] = cosh_nonzero_proof
            domain_condition_names.append(cosh_nonzero_proof)
            continue
        constant = _constant_fraction(domain_expression)
        if constant is not None:
            if constant == 0:
                return None
            continue
        expression_lean = _render_real_calculus_node(
            domain_expression,
            set(),
            {"nodes": 0},
            0,
            allow_variable_denominators=True,
        )
        if expression_lean is None:
            return None
        hypothesis_name = _fresh_identifier(f"h_domain_{index}", used_names)
        domain_hypotheses[key] = hypothesis_name
        domain_binders.append(f"({hypothesis_name} : {expression_lean} ≠ 0)")
        domain_condition_names.append(hypothesis_name)
        if (
            isinstance(domain_expression, ast.Call)
            and isinstance(domain_expression.func, ast.Name)
            and domain_expression.func.id == "cos"
        ):
            argument_source = ast.unparse(domain_expression.args[0]).replace("**", "^")
            expression_source = f"Cos[{argument_source}]"
        else:
            expression_source = ast.unparse(domain_expression).replace("**", "^")
        domain_conditions.append(f"{expression_source} ≠ 0")

    function_variable = _fresh_identifier("deriv_var", used_names)
    rendered = _render_real_calculus_node(
        tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: function_variable},
        allow_variable_denominators=True,
    )
    proof = _derivative_proof_term(
        tree.body,
        variable,
        function_variable,
        variable,
        {"nodes": 0},
        0,
        domain_hypotheses=domain_hypotheses,
        positive_hypotheses=positive_hypotheses,
    )
    if rendered is None or proof is None:
        return None

    variables = sorted(integrand_symbols | antiderivative_symbols | {variable})
    binders = " ".join(
        [*(f"({name} : ℝ)" for name in variables), *domain_binders]
    )
    theorem_binders = f" {binders}" if binders else ""
    field_simp_arguments = (
        f" [{', '.join(domain_condition_names)}]" if domain_condition_names else ""
    )
    function_equality_tactic = f"ext {function_variable}; simp [id_eq] <;> ring"
    has_hyperbolic_tangent = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "tanh"
        for expression_tree in (integrand_tree, tree)
        for node in ast.walk(expression_tree.body)
    )
    has_tangent = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "tan"
        for expression_tree in (integrand_tree, tree)
        for node in ast.walk(expression_tree.body)
    )
    has_sine_cosine_quotient = (
        any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "sin"
            for expression_tree in (integrand_tree, tree)
            for node in ast.walk(expression_tree.body)
        )
        and any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "cos"
            for expression_tree in (integrand_tree, tree)
            for node in ast.walk(expression_tree.body)
        )
        and any(
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and _contains_calculus_variable(node.right)
            for expression_tree in (integrand_tree, tree)
            for node in ast.walk(expression_tree.body)
        )
    )
    if has_hyperbolic_tangent:
        derivative_tactic = (
            "simp [id_eq, Real.tanh_eq_sinh_div_cosh] <;> "
            f"field_simp{field_simp_arguments} <;> ring"
        )
    elif has_tangent or has_sine_cosine_quotient:
        tangent_simp = ", Real.tan_eq_sin_div_cos" if has_tangent else ""
        derivative_tactic = (
            f"simp [id_eq{tangent_simp}] <;> "
            f"field_simp{field_simp_arguments} <;> "
            f"nlinarith [Real.sin_sq_add_cos_sq {variable}]"
        )
    else:
        derivative_tactic = (
            f"simp [id_eq] <;> field_simp{field_simp_arguments} <;> ring"
        )
    source = f"""import Mathlib.Analysis.Calculus.Deriv.Add
import Mathlib.Analysis.Calculus.Deriv.Mul
import Mathlib.Analysis.Calculus.Deriv.Pow
import Mathlib.Analysis.Calculus.Deriv.Inv
import Mathlib.Analysis.SpecialFunctions.ExpDeriv
import Mathlib.Analysis.SpecialFunctions.Log.Deriv
import Mathlib.Analysis.SpecialFunctions.Arsinh
import Mathlib.Analysis.SpecialFunctions.Arcosh
import Mathlib.Analysis.SpecialFunctions.Sqrt
import Mathlib.Analysis.SpecialFunctions.Trigonometric.ArctanDeriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.Deriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.DerivHyp
import Mathlib.Analysis.SpecialFunctions.Trigonometric.InverseDeriv
import Mathlib.Tactic.FieldSimp
import Mathlib.Tactic.Ring

theorem wolfram_language_compute{theorem_binders} :
    HasDerivAt (fun {function_variable} : ℝ => {rendered}) {integrand_lean} {variable} := by
  convert ({proof}) using 1
  all_goals
    first
    | ({function_equality_tactic} <;> done)
    | ({derivative_tactic} <;> done)

#print axioms wolfram_language_compute
"""
    return Computation(
        antiderivative.replace("**", "^"),
        source,
        domain_conditions=tuple(domain_conditions),
    )


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
        original_lean, symbols = _render_real_calculus_lean(
            parsed.expression, allow_variable_denominators=True
        )
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None
    if original_lean is None:
        return None
    positive_log_arguments = _positive_log_arguments(tree.body)
    if positive_log_arguments is None:
        return None
    positive_sqrt_arguments = _positive_sqrt_arguments(tree.body)
    if positive_sqrt_arguments is None:
        return None
    tan_cosine_arguments = _tan_cosine_arguments(tree.body)
    if tan_cosine_arguments is None:
        return None
    inverse_trig_arguments = _inverse_trig_arguments(tree.body)
    if inverse_trig_arguments is None:
        return None
    inverse_hyperbolic_cosh_arguments = _inverse_hyperbolic_cosh_arguments(
        tree.body
    )
    if inverse_hyperbolic_cosh_arguments is None:
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
    if (
        isinstance(tree.body, ast.Call)
        and isinstance(tree.body.func, ast.Name)
        and tree.body.func.id == "acosh"
        and len(tree.body.args) == 1
        and isinstance(tree.body.args[0], ast.Name)
        and tree.body.args[0].id == variable
    ):
        result = f"1/sqrt({variable}**2 - 1)"
    try:
        result_lean, result_symbols = _render_real_calculus_lean(
            result, allow_variable_denominators=True
        )
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None
    if result_lean is None:
        return None

    used_names = set(symbols) | set(result_symbols) | {variable}
    function_variable = _fresh_identifier("deriv_var", used_names)
    domain_denominators = _variable_denominators(tree.body)
    domain_hypotheses: dict[str, str] = {}
    positive_hypotheses: dict[str, str] = {}
    domain_binders: list[str] = []
    domain_condition_names: list[str] = []
    domain_conditions: list[str] = []
    for index, denominator in enumerate(
        [*domain_denominators, *tan_cosine_arguments]
    ):
        denominator_key = ast.dump(denominator, include_attributes=False)
        if denominator_key in domain_hypotheses:
            continue
        denominator_lean = _render_real_calculus_node(
            denominator,
            set(),
            {"nodes": 0},
            0,
            allow_variable_denominators=True,
        )
        if denominator_lean is None:
            return None
        hypothesis_name = _fresh_identifier(f"h_domain_{index}", used_names)
        domain_hypotheses[denominator_key] = hypothesis_name
        domain_binders.append(f"({hypothesis_name} : {denominator_lean} ≠ 0)")
        domain_condition_names.append(hypothesis_name)
        if (
            isinstance(denominator, ast.Call)
            and isinstance(denominator.func, ast.Name)
            and denominator.func.id == "cos"
        ):
            argument_source = ast.unparse(denominator.args[0]).replace("**", "^")
            denominator_source = f"Cos[{argument_source}]"
        else:
            denominator_source = ast.unparse(denominator).replace("**", "^")
        domain_conditions.append(f"{denominator_source} ≠ 0")

    for index, argument in enumerate(inverse_trig_arguments):
        argument_lean = _render_real_calculus_node(
            argument,
            set(),
            {"nodes": 0},
            0,
            allow_variable_denominators=True,
        )
        if argument_lean is None:
            return None
        argument_source = ast.unparse(argument).replace("**", "^")
        lower_name = _fresh_identifier(f"h_inverse_lower_{index}", used_names)
        upper_name = _fresh_identifier(f"h_inverse_upper_{index}", used_names)
        domain_hypotheses[_inverse_trig_hypothesis_key(argument, "lower")] = lower_name
        domain_hypotheses[_inverse_trig_hypothesis_key(argument, "upper")] = upper_name
        domain_binders.extend(
            [
                f"({lower_name} : -1 < {argument_lean})",
                f"({upper_name} : {argument_lean} < 1)",
            ]
        )
        domain_condition_names.extend([lower_name, upper_name])
        domain_conditions.extend(
            [f"-1 < {argument_source}", f"{argument_source} < 1"]
        )

    for index, argument in enumerate(inverse_hyperbolic_cosh_arguments):
        argument_lean = _render_real_calculus_node(
            argument,
            set(),
            {"nodes": 0},
            0,
            allow_variable_denominators=True,
        )
        if argument_lean is None:
            return None
        argument_source = ast.unparse(argument).replace("**", "^")
        hypothesis_name = _fresh_identifier(f"h_arcosh_domain_{index}", used_names)
        domain_hypotheses[
            _inverse_hyperbolic_cosh_hypothesis_key(argument)
        ] = hypothesis_name
        domain_binders.append(f"({hypothesis_name} : 1 < {argument_lean})")
        domain_condition_names.append(hypothesis_name)
        domain_conditions.append(f"{argument_source} > 1")

    positive_arguments = [*positive_log_arguments, *positive_sqrt_arguments]
    for index, argument in enumerate(positive_arguments):
        argument_key = ast.dump(argument, include_attributes=False)
        if argument_key in positive_hypotheses:
            continue
        argument_lean = _render_real_calculus_node(
            argument,
            set(),
            {"nodes": 0},
            0,
            allow_variable_denominators=True,
        )
        if argument_lean is None:
            return None
        hypothesis_name = _fresh_identifier(f"h_positive_{index}", used_names)
        positive_hypotheses[argument_key] = hypothesis_name
        domain_binders.append(f"({hypothesis_name} : 0 < {argument_lean})")
        domain_condition_names.append(f"ne_of_gt {hypothesis_name}")
        argument_source = ast.unparse(argument).replace("**", "^")
        domain_conditions.append(f"{argument_source} > 0")

    rendered = _render_real_calculus_node(
        tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: function_variable},
        allow_variable_denominators=True,
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
        domain_hypotheses=domain_hypotheses,
        positive_hypotheses=positive_hypotheses,
    )
    if proof is None:
        return None

    variables = sorted(set(symbols) | set(result_symbols) | {variable})
    binders = " ".join(
        [*(f"({name} : ℝ)" for name in variables), *domain_binders]
    )
    theorem_binders = f" {binders}" if binders else ""
    field_simp_arguments = (
        f" [{', '.join(domain_condition_names)}]" if domain_condition_names else ""
    )
    has_tangent_derivative = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "tan"
        and len(node.args) == 1
        and any(
            isinstance(child, ast.Name) and child.id == variable
            for child in ast.walk(node.args[0])
        )
        for node in ast.walk(tree.body)
    )
    if has_tangent_derivative:
        derivative_tactic = (
            "simp [id_eq, Real.tan_eq_sin_div_cos] <;> "
            f"field_simp{field_simp_arguments} <;> "
            "simp [Real.sin_sq_add_cos_sq] <;> ring"
        )
    else:
        derivative_tactic = (
            f"simp [id_eq] <;> field_simp{field_simp_arguments} <;> ring"
        )
    function_equality_tactic = (
        f"ext {function_variable}; simp [id_eq] <;> ring"
    )
    tanh_helper_source = """theorem wolfram_hasDerivAt_tanh (x : ℝ) :
    HasDerivAt Real.tanh (1 - (Real.tanh x) ^ 2) x := by
  have hcosh : Real.cosh x ≠ 0 := ne_of_gt (Real.cosh_pos x)
  convert (Real.hasDerivAt_sinh x).div (Real.hasDerivAt_cosh x) hcosh using 1
  all_goals
    first
    | ext y; simp [Real.tanh_eq_sinh_div_cosh]
    | rw [Real.tanh_eq_sinh_div_cosh]; field_simp [hcosh] <;> ring

""" if any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "tanh"
        for node in ast.walk(tree.body)
    ) else ""
    source = f"""import Mathlib.Analysis.Calculus.Deriv.Add
import Mathlib.Analysis.Calculus.Deriv.Mul
import Mathlib.Analysis.Calculus.Deriv.Pow
import Mathlib.Analysis.Calculus.Deriv.Inv
import Mathlib.Analysis.SpecialFunctions.ExpDeriv
import Mathlib.Analysis.SpecialFunctions.Log.Deriv
import Mathlib.Analysis.SpecialFunctions.Arsinh
import Mathlib.Analysis.SpecialFunctions.Arcosh
import Mathlib.Analysis.SpecialFunctions.Sqrt
import Mathlib.Analysis.SpecialFunctions.Trigonometric.ArctanDeriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.Deriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.DerivHyp
import Mathlib.Analysis.SpecialFunctions.Trigonometric.InverseDeriv
import Mathlib.Tactic.FieldSimp
import Mathlib.Tactic.Ring

{tanh_helper_source}

theorem wolfram_language_compute{theorem_binders} :
    HasDerivAt (fun {function_variable} : ℝ => {rendered}) {result_lean} {variable} := by
  convert ({proof}) using 1
  all_goals
    first
    | {function_equality_tactic}
    | {derivative_tactic}

#print axioms wolfram_language_compute
"""
    return Computation(
        result.replace("**", "^"),
        source,
        domain_conditions=tuple(domain_conditions),
    )


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


def _prepare_real_polynomial_solve(
    lhs_source: str,
    rhs_source: str,
    variable: str,
) -> Computation | None:
    import sympy as _sympy

    try:
        parsed_lhs = parse_math_input(lhs_source)
        parsed_rhs = parse_math_input(rhs_source)
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None
    exact_lhs = _normalize_decimal_literals(parsed_lhs.expression)
    exact_rhs = _normalize_decimal_literals(parsed_rhs.expression)
    if exact_lhs is None or exact_rhs is None:
        return None
    try:
        lhs_tree = ast.parse(exact_lhs, mode="eval")
        rhs_tree = ast.parse(exact_rhs, mode="eval")
    except (ValueError, SyntaxError, RecursionError):
        return None
    lhs_degree = _bounded_polynomial_degree(lhs_tree.body, variable)
    rhs_degree = _bounded_polynomial_degree(rhs_tree.body, variable)
    if lhs_degree is None or rhs_degree is None or max(lhs_degree, rhs_degree) > 4:
        return None

    lhs_lean, lhs_symbols = _render_polynomial_lean(exact_lhs)
    rhs_lean, rhs_symbols = _render_polynomial_lean(exact_rhs)
    if (
        lhs_lean is None
        or rhs_lean is None
        or (lhs_symbols | rhs_symbols) - {variable}
    ):
        return None

    lhs_expression = _real_sympy_expression(lhs_tree.body)
    rhs_expression = _real_sympy_expression(rhs_tree.body)
    if lhs_expression is None or rhs_expression is None:
        return None
    symbol = _sympy.Symbol(variable)
    try:
        polynomial = _sympy.Poly(lhs_expression - rhs_expression, symbol, domain=_sympy.QQ)
    except (TypeError, ValueError, _sympy.PolynomialError):
        return None
    if polynomial.is_zero or polynomial.degree() > 4:
        return None
    for coefficient in polynomial.all_coeffs():
        if (
            abs(int(coefficient.p)).bit_length() > 256
            or int(coefficient.q).bit_length() > 256
        ):
            return None

    roots: list[_RealPolynomialRoot] = []
    factor_roots: list[_RealPolynomialRoot] = []
    quadratic_coefficients: tuple[Fraction, Fraction, Fraction] | None = None
    discriminant: Fraction | None = None
    if polynomial.degree() > 0:
        roots_with_multiplicity = _sympy.roots(polynomial.as_expr(), symbol)
        rational_roots_complete = (
            sum(roots_with_multiplicity.values()) == polynomial.degree()
            and all(root.is_Rational for root in roots_with_multiplicity)
        )
        if rational_roots_complete:
            rational_roots = []
            for root, multiplicity in roots_with_multiplicity.items():
                root_fraction = Fraction(int(root.p), int(root.q))
                if (
                    abs(root_fraction.numerator).bit_length() > 256
                    or root_fraction.denominator.bit_length() > 256
                ):
                    return None
                rational_roots.append((root_fraction, multiplicity))
            rational_values = sorted({root for root, _ in rational_roots})
            roots = [
                _RealPolynomialRoot(
                    _lean_rational(root),
                    _fraction_text(root),
                )
                for root in rational_values
            ]
            root_by_value = dict(zip(rational_values, roots))
            factor_roots = [
                root_by_value[root]
                for root, multiplicity in sorted(rational_roots)
                for _ in range(multiplicity)
            ]
        elif polynomial.degree() == 2:
            coefficients = [
                Fraction(int(coefficient.p), int(coefficient.q))
                for coefficient in polynomial.all_coeffs()
            ]
            leading, linear, constant = coefficients
            if leading == 0:
                return None
            discriminant = linear**2 - 4 * leading * constant
            if (
                abs(discriminant.numerator).bit_length() > 256
                or discriminant.denominator.bit_length() > 256
            ):
                return None
            if discriminant == 0:
                return None
            if discriminant > 0:
                quadratic_coefficients = (leading, linear, constant)
                b_lean = _lean_rational(linear)
                denominator_lean = _lean_rational(2 * leading)
                discriminant_lean = _lean_rational(discriminant)
                sqrt_discriminant_lean = f"Real.sqrt ({discriminant_lean})"
                sqrt_discriminant_sympy = _sympy.sqrt(
                    _sympy.Rational(
                        discriminant.numerator,
                        discriminant.denominator,
                    )
                )
                radical_signs = (-1, 1) if leading > 0 else (1, -1)
                roots = []
                for radical_sign in radical_signs:
                    operator = "+" if radical_sign > 0 else "-"
                    simplified_root = _sympy.simplify(
                        (-linear + radical_sign * sqrt_discriminant_sympy)
                        / (2 * leading)
                    )
                    roots.append(
                        _RealPolynomialRoot(
                            f"((-({b_lean}) {operator} {sqrt_discriminant_lean}) / "
                            f"({denominator_lean}))",
                            _sympy_real_root_to_wolfram(simplified_root, _sympy),
                        )
                    )
                factor_roots = roots.copy()
            else:
                quadratic_coefficients = (leading, linear, constant)
        else:
            return None

    polynomial_lean = f"({lhs_lean}) - ({rhs_lean})"
    proof_source = _real_polynomial_solve_proof_source(
        polynomial_lean,
        variable,
        roots,
        factor_roots,
        quadratic_coefficients,
        discriminant,
    )
    if roots:
        solution_values = ", ".join(
            f"{{{variable} -> {root.wolfram_expression}}}" for root in roots
        )
        display_result = "{" + solution_values + "}"
    else:
        display_result = "{}"
    return Computation(display_result, proof_source)


def _split_top_level_commas(source: str) -> list[str] | None:
    closing_for = {"(": ")", "[": "]", "{": "}"}
    closing_tokens = set(closing_for.values())
    stack: list[str] = []
    items: list[str] = []
    item_start = 0
    for index, character in enumerate(source):
        if character in closing_for:
            stack.append(closing_for[character])
        elif character in closing_tokens:
            if not stack or stack.pop() != character:
                return None
        elif character == "," and not stack:
            items.append(source[item_start:index].strip())
            item_start = index + 1
    if stack:
        return None
    items.append(source[item_start:].strip())
    return items if all(items) else None


def _linear_expression_text(
    expression: object,
    variables: list[str],
    sympy: object,
    *,
    lean: bool,
) -> str | None:
    symbols = [sympy.Symbol(variable) for variable in variables]
    try:
        polynomial = sympy.Poly(expression, *symbols, domain=sympy.QQ)
    except (TypeError, ValueError, sympy.PolynomialError):
        return None
    if polynomial.total_degree() > 1:
        return None

    terms: list[tuple[Fraction, str | None]] = []
    constant = polynomial.coeff_monomial(1)
    if not constant.is_Rational:
        return None
    constant_fraction = Fraction(int(constant.p), int(constant.q))
    if constant_fraction:
        terms.append((constant_fraction, None))

    for variable, symbol in zip(variables, symbols):
        coefficient = polynomial.coeff_monomial(symbol)
        if not coefficient.is_Rational:
            return None
        coefficient_fraction = Fraction(int(coefficient.p), int(coefficient.q))
        if coefficient_fraction:
            terms.append((coefficient_fraction, variable))

    if not terms:
        return "0"

    rendered = ""
    for index, (coefficient, variable) in enumerate(terms):
        magnitude = abs(coefficient)
        if variable is None:
            body = _lean_fraction(magnitude) if lean else _fraction_text(magnitude)
        elif magnitude == 1:
            body = variable
        else:
            number = _lean_fraction(magnitude) if lean else _fraction_text(magnitude)
            multiplication = " * " if lean else "*"
            body = f"{number}{multiplication}{variable}"

        if index == 0:
            rendered = ("-" if coefficient < 0 else "") + body
        else:
            separator = " - " if lean else "-"
            if coefficient > 0:
                separator = " + " if lean else "+"
            rendered += separator + body
    return rendered


def _prepare_real_linear_system_solve(
    equation_sources: list[str],
    variables: list[str],
) -> Computation | None:
    import sympy as _sympy

    if (
        not 2 <= len(variables) <= 4
        or not 1 <= len(equation_sources) <= 4
        or len(set(variables)) != len(variables)
    ):
        return None

    lean_equations: list[str] = []
    sympy_equations = []
    symbols = tuple(_sympy.Symbol(variable) for variable in variables)
    allowed_variables = set(variables)
    for equation_source in equation_sources:
        comparison = re.fullmatch(
            r"(?is)\s*(?P<lhs>.+?)\s*==\s*(?P<rhs>.+?)\s*",
            equation_source,
        )
        if comparison is None:
            return None
        try:
            parsed_lhs = parse_math_input(comparison.group("lhs"))
            parsed_rhs = parse_math_input(comparison.group("rhs"))
        except (MathInputError, ValueError, SyntaxError, RecursionError):
            return None
        exact_lhs = _normalize_decimal_literals(parsed_lhs.expression)
        exact_rhs = _normalize_decimal_literals(parsed_rhs.expression)
        if exact_lhs is None or exact_rhs is None:
            return None
        lhs_lean, lhs_names = _render_polynomial_lean(exact_lhs)
        rhs_lean, rhs_names = _render_polynomial_lean(exact_rhs)
        if (
            lhs_lean is None
            or rhs_lean is None
            or (lhs_names | rhs_names) - allowed_variables
        ):
            return None
        try:
            lhs_tree = ast.parse(exact_lhs, mode="eval")
            rhs_tree = ast.parse(exact_rhs, mode="eval")
        except (ValueError, SyntaxError, RecursionError):
            return None
        lhs_expression = _real_sympy_expression(lhs_tree.body)
        rhs_expression = _real_sympy_expression(rhs_tree.body)
        if lhs_expression is None or rhs_expression is None:
            return None
        try:
            polynomial = _sympy.Poly(
                lhs_expression - rhs_expression,
                *symbols,
                domain=_sympy.QQ,
            )
        except (TypeError, ValueError, _sympy.PolynomialError):
            return None
        if polynomial.total_degree() > 1:
            return None
        for coefficient in polynomial.coeffs():
            if (
                abs(int(coefficient.p)).bit_length() > 256
                or int(coefficient.q).bit_length() > 256
            ):
                return None

        sympy_equations.append(polynomial)
        lean_equations.append(f"(({lhs_lean}) - ({rhs_lean}))")

    matrix = _sympy.Matrix(
        [
            [equation.coeff_monomial(symbol) for symbol in symbols]
            for equation in sympy_equations
        ]
    )
    right_hand_side = _sympy.Matrix(
        [-equation.coeff_monomial(1) for equation in sympy_equations]
    )
    reduced, pivots = matrix.row_join(right_hand_side).rref()
    if len(variables) in pivots:
        result = "{}"
        result_proposition = "False"
        equation_hypotheses = [
            f"hequation{index + 1}" for index in range(len(equation_sources))
        ]
        if len(equation_hypotheses) == 1:
            equation_intro = f"  · intro {equation_hypotheses[0]}"
        else:
            equation_intro = f"  · rintro ⟨{', '.join(equation_hypotheses)}⟩"
        forward_proof = (
            f"{equation_intro}\n"
            f"    norm_num at {' '.join(equation_hypotheses)} <;> "
            f"linarith [{', '.join(equation_hypotheses)}]"
        )
        backward_proof = """  · intro hfalse
    exact False.elim hfalse"""
    else:
        pivot_columns = [column for column in pivots if column < len(variables)]
        free_columns = [
            column for column in range(len(variables)) if column not in pivot_columns
        ]
        assignments: list[tuple[int, object, str, str]] = []
        for row, pivot_column in enumerate(pivot_columns):
            expression = reduced[row, len(variables)]
            for free_column in free_columns:
                coefficient = reduced[row, free_column]
                if coefficient:
                    expression -= coefficient * symbols[free_column]
            expression = _sympy.cancel(expression)
            wolfram_expression = _linear_expression_text(
                expression,
                variables,
                _sympy,
                lean=False,
            )
            lean_expression = _linear_expression_text(
                expression,
                variables,
                _sympy,
                lean=True,
            )
            if wolfram_expression is None or lean_expression is None:
                return None
            assignments.append(
                (pivot_column, expression, wolfram_expression, lean_expression)
            )

        result_bindings = ", ".join(
            f"{variables[column]} -> {wolfram_expression}"
            for column, _expression, wolfram_expression, _lean_expression in assignments
        )
        result = "{{" + result_bindings + "}}"
        result_relations = [
            f"{variables[column]} = ({lean_expression})"
            for column, _expression, _wolfram_expression, lean_expression in assignments
        ]
        result_proposition = " ∧ ".join(result_relations) or "True"

        equation_hypotheses = [
            f"hequation{index + 1}" for index in range(len(equation_sources))
        ]
        solution_hypotheses = [
            f"h{variables[column]}" for column, *_rest in assignments
        ]
        linear_hypothesis_list = "[" + ", ".join(equation_hypotheses) + "]"
        if len(equation_hypotheses) == 1:
            equation_intro = f"  · intro {equation_hypotheses[0]}"
        else:
            equation_intro = f"  · rintro ⟨{', '.join(equation_hypotheses)}⟩"

        def render_conjunction_proof(
            tactics: list[str],
            indent: int = 4,
            bullet: bool = False,
        ) -> list[str]:
            prefix = " " * indent
            bullet_prefix = "· " if bullet else ""
            if len(tactics) == 1:
                return [f"{prefix}{bullet_prefix}{tactics[0]}"]
            lines = [f"{prefix}{bullet_prefix}constructor"]
            child_indent = indent + (2 if bullet else 0)
            lines.extend(
                render_conjunction_proof([tactics[0]], child_indent, bullet=True)
            )
            lines.extend(
                render_conjunction_proof(tactics[1:], child_indent, bullet=True)
            )
            return lines

        if assignments:
            forward_proof = "\n".join(
                [
                    equation_intro,
                    *render_conjunction_proof(
                        [
                            f"linarith {linear_hypothesis_list}"
                            for _assignment in assignments
                        ]
                    ),
                ]
            )
            if len(assignments) == 1:
                solution_intro = f"  · intro {solution_hypotheses[0]}"
            else:
                solution_intro = f"  · rintro ⟨{', '.join(solution_hypotheses)}⟩"
            backward_proof = "\n".join(
                [
                    solution_intro,
                    *[f"    subst {variables[column]}" for column, *_rest in assignments],
                    *render_conjunction_proof(
                        ["linarith" for _equation in equation_sources]
                    ),
                ]
            )
        else:
            forward_proof = "  · intro _\n    exact True.intro"
            backward_proof = "\n".join(
                [
                    "  · intro _",
                    *render_conjunction_proof(
                        ["norm_num" for _equation in equation_sources]
                    ),
                ]
            )

    system_proposition = " ∧ ".join(
        f"(({equation} : ℝ) = 0)" for equation in lean_equations
    )
    source = f"""import Mathlib.Data.Real.Basic
import Mathlib.Tactic.Linarith
import Mathlib.Tactic.NormNum

theorem wolfram_language_compute ({' '.join(variables)} : ℝ) :
    ({system_proposition}) ↔ ({result_proposition}) := by
  constructor
{forward_proof}
{backward_proof}

#print axioms wolfram_language_compute
"""
    return Computation(result, source)


def _sympy_real_root_to_wolfram(expression: object, sympy: object) -> str:
    rendered = sympy.sstr(expression)
    rendered = re.sub(
        r"\bsqrt\(([^()]*)\)",
        lambda match: f"Sqrt[{match.group(1)}]",
        rendered,
    )
    return rendered.replace("**", "^")


def _bounded_polynomial_degree(node: ast.AST, variable: str) -> int | None:
    if _constant_fraction(node) is not None:
        return 0
    if isinstance(node, ast.Name):
        return 1 if node.id == variable else 0
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _bounded_polynomial_degree(node.operand, variable)
    if not isinstance(node, ast.BinOp):
        return None
    left_degree = _bounded_polynomial_degree(node.left, variable)
    if left_degree is None:
        return None
    if isinstance(node.op, ast.Pow):
        exponent = _constant_fraction(node.right)
        if exponent is None or exponent.denominator != 1 or not 0 <= exponent <= 16:
            return None
        return min(3, left_degree * exponent.numerator)
    right_degree = _bounded_polynomial_degree(node.right, variable)
    if right_degree is None:
        return None
    if isinstance(node.op, (ast.Add, ast.Sub)):
        return max(left_degree, right_degree)
    if isinstance(node.op, ast.Mult):
        return min(3, left_degree + right_degree)
    if isinstance(node.op, ast.Div) and _constant_fraction(node.right) not in (None, Fraction(0)):
        return left_degree
    return None


def _real_polynomial_solve_proof_source(
    polynomial: str,
    variable: str,
    roots: list[_RealPolynomialRoot],
    factor_roots: list[_RealPolynomialRoot],
    quadratic_coefficients: tuple[Fraction, Fraction, Fraction] | None,
    discriminant: Fraction | None,
) -> str:
    if not roots and quadratic_coefficients is not None and discriminant is not None:
        leading, linear, _constant = quadratic_coefficients
        leading_lean = _lean_rational(leading)
        linear_lean = _lean_rational(linear)
        discriminant_lean = _lean_rational(discriminant)
        result_proposition = "False"
        proof = f"""  have hdiscriminant : {discriminant_lean} < 0 := by norm_num
  constructor
  · intro hzero
    have hcompleted : (2 * ({leading_lean}) * {variable} + ({linear_lean})) ^ 2 = {discriminant_lean} := by
      nlinarith [hzero]
    nlinarith [hcompleted, sq_nonneg (2 * ({leading_lean}) * {variable} + ({linear_lean}))]
  · intro hfalse
    exact False.elim hfalse"""
    elif not roots:
        result_proposition = "False"
        proof = """  constructor
  · intro hzero
    norm_num at hzero
  · intro hfalse
    exact False.elim hfalse"""
    elif len(factor_roots) == 1:
        result_proposition = f"{variable} = {factor_roots[0].lean_expression}"
        proof = f"""  constructor
  · intro hzero
    nlinarith [hzero]
  · intro hsolution
    subst {variable}
    norm_num"""
    else:
        result_proposition = " ∨ ".join(
            f"{variable} = {root.lean_expression}" for root in factor_roots
        )
        factors = " * ".join(
            f"({variable} - {root.lean_expression})" for root in factor_roots
        )
        factor_hypotheses = "[hzero]"
        branch_hypotheses = ""
        if discriminant is not None and discriminant > 0:
            discriminant_lean = _lean_rational(discriminant)
            factor_hypotheses = "[hzero, hsqrt]"
            branch_hypotheses = f"""  have hdiscriminant : 0 < {discriminant_lean} := by norm_num
  have hsqrt : (Real.sqrt ({discriminant_lean})) ^ 2 = {discriminant_lean} :=
    Real.sq_sqrt (le_of_lt hdiscriminant)
"""
        solution_tactic = "nlinarith [hsqrt]" if branch_hypotheses else "norm_num"
        solution_cases: list[str] = []

        def append_solution_cases(
            case_indent: int,
            is_nested: bool,
            remaining_roots: list[_RealPolynomialRoot],
        ) -> None:
            prefix = " " * case_indent
            if len(remaining_roots) == 1:
                bullet = "· " if is_nested else ""
                solution_cases.append(f"{prefix}{bullet}subst {variable}")
                solution_cases.append(
                    f"{' ' * (case_indent + 2)}{solution_tactic}"
                )
                return

            bullet = "· " if is_nested else ""
            solution_cases.append(
                f"{prefix}{bullet}rcases hsolutions with hsolution | hsolutions"
            )
            child_indent = case_indent + (2 if is_nested else 0)
            solution_cases.append(
                f"{' ' * child_indent}· subst {variable}"
            )
            solution_cases.append(
                f"{' ' * (child_indent + 2)}{solution_tactic}"
            )
            append_solution_cases(
                child_indent,
                True,
                remaining_roots[1:],
            )

        append_solution_cases(4, False, factor_roots)
        converse = "\n".join(solution_cases)
        proof = f"""{branch_hypotheses}  constructor
  · intro hzero
    have hfactor : {factors} = 0 := by nlinarith {factor_hypotheses}
    have hsolutions : {result_proposition} := by
      simpa only [mul_eq_zero, sub_eq_zero, or_assoc] using hfactor
    exact hsolutions
  · intro hsolutions
{converse}"""
    return f"""import Mathlib.Data.Real.Basic
import Mathlib.Analysis.SpecialFunctions.Sqrt
import Mathlib.Tactic.Linarith
import Mathlib.Tactic.NormNum

theorem wolfram_language_compute ({variable} : ℝ) :
    (({polynomial}) : ℝ) = 0 ↔ {result_proposition} := by
{proof}

#print axioms wolfram_language_compute
"""


def _normalize_wolfram_real_functions(source: str) -> str:
    normalized = re.sub(r"\bPi\b", "pi", source, flags=re.IGNORECASE)
    for _ in range(64):
        def normalize_function(match: re.Match[str]) -> str:
            function_name = match.group("function").lower()
            function_name = {
                "arcsin": "asin",
                "arccos": "acos",
                "arctan": "atan",
                "arcsinh": "asinh",
                "arccosh": "acosh",
            }.get(function_name, function_name)
            argument = match.group("argument")
            try:
                argument = parse_math_input(argument).expression
            except MathInputError:
                pass
            if function_name == "sec":
                return f"(1/cos({argument}))"
            if function_name == "csc":
                return f"(1/sin({argument}))"
            if function_name == "cot":
                return f"(cos({argument})/sin({argument}))"
            return f"{function_name}({argument})"

        normalized, replacements = _WOLFRAM_REAL_FUNCTION.subn(
            normalize_function,
            normalized,
        )
        if replacements == 0:
            break
    if "\\" not in normalized:
        normalized = normalized.replace("^", "**")
    return normalized


def _fresh_identifier(base: str, used_names: set[str]) -> str:
    name = base
    suffix = 0
    while name in used_names:
        suffix += 1
        name = f"{base}{suffix}"
    used_names.add(name)
    return name


def _variable_denominators(root: ast.AST) -> list[ast.AST]:
    denominators: list[ast.AST] = []
    seen: set[str] = set()
    for node in ast.walk(root):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        if _constant_fraction(node.right) is not None:
            continue
        key = ast.dump(node.right, include_attributes=False)
        if key not in seen:
            seen.add(key)
            denominators.append(node.right)
    return denominators


def _positive_log_arguments(root: ast.AST) -> list[ast.AST] | None:
    arguments: list[ast.AST] = []
    seen: set[str] = set()
    for node in ast.walk(root):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Name)
            or node.func.id != "log"
            or len(node.args) != 1
            or node.keywords
        ):
            continue
        argument = node.args[0]
        constant = _constant_fraction(argument)
        if constant is not None:
            if constant <= 0:
                return None
            continue
        key = ast.dump(argument, include_attributes=False)
        if key not in seen:
            seen.add(key)
            arguments.append(argument)
    return arguments


def _positive_sqrt_arguments(root: ast.AST) -> list[ast.AST] | None:
    arguments: list[ast.AST] = []
    seen: set[str] = set()
    for node in ast.walk(root):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Name)
            or node.func.id != "sqrt"
            or len(node.args) != 1
            or node.keywords
        ):
            continue
        argument = node.args[0]
        if not _contains_calculus_variable(argument):
            value = _constant_sympy_expression(argument)
            if value is None or value.is_real is not True or value.is_nonnegative is not True:
                return None
            continue
        key = ast.dump(argument, include_attributes=False)
        if key not in seen:
            seen.add(key)
            arguments.append(argument)
    return arguments


def _log_arguments(root: ast.AST) -> list[ast.AST]:
    arguments: list[ast.AST] = []
    seen: set[str] = set()
    for node in ast.walk(root):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Name)
            or node.func.id != "log"
            or len(node.args) != 1
            or node.keywords
        ):
            continue
        argument = _unwrap_real_abs(node.args[0])
        key = ast.dump(argument, include_attributes=False)
        if key not in seen:
            seen.add(key)
            arguments.append(argument)
    return arguments


def _inverse_trig_arguments(root: ast.AST) -> list[ast.AST] | None:
    import sympy as _sympy

    arguments: list[ast.AST] = []
    seen: set[str] = set()
    for node in ast.walk(root):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Name)
            or node.func.id not in {"asin", "acos"}
            or len(node.args) != 1
            or node.keywords
        ):
            continue
        argument = node.args[0]
        if not _contains_calculus_variable(argument):
            value = _constant_sympy_expression(argument)
            if value is None or value.is_real is not True:
                return None
            if (
                _sympy.simplify(value >= -1) is not _sympy.true
                or _sympy.simplify(value <= 1) is not _sympy.true
            ):
                return None
            continue
        key = ast.dump(argument, include_attributes=False)
        if key not in seen:
            seen.add(key)
            arguments.append(argument)
    return arguments


def _inverse_trig_hypothesis_key(argument: ast.AST, endpoint: str) -> str:
    key = ast.dump(argument, include_attributes=False)
    return f"{key}:inverse-trig:{endpoint}"


def _inverse_hyperbolic_cosh_arguments(root: ast.AST) -> list[ast.AST] | None:
    import sympy as _sympy

    arguments: list[ast.AST] = []
    seen: set[str] = set()
    for node in ast.walk(root):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Name)
            or node.func.id != "acosh"
            or len(node.args) != 1
            or node.keywords
        ):
            continue
        argument = node.args[0]
        if not _contains_calculus_variable(argument):
            value = _constant_sympy_expression(argument)
            if (
                value is None
                or value.is_real is not True
                or _sympy.simplify(value >= 1) is not _sympy.true
            ):
                return None
            continue
        key = ast.dump(argument, include_attributes=False)
        if key not in seen:
            seen.add(key)
            arguments.append(argument)
    return arguments


def _inverse_hyperbolic_cosh_hypothesis_key(argument: ast.AST) -> str:
    return f"{ast.dump(argument, include_attributes=False)}:inverse-hyperbolic-cosh"


def _unwrap_real_abs(node: ast.AST) -> ast.AST:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"abs", "Abs"}
        and len(node.args) == 1
        and not node.keywords
    ):
        return node.args[0]
    return node


def _tan_cosine_arguments(root: ast.AST) -> list[ast.AST] | None:
    import sympy as _sympy

    arguments: list[ast.AST] = []
    seen: set[str] = set()
    for node in ast.walk(root):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Name)
            or node.func.id != "tan"
            or len(node.args) != 1
            or node.keywords
        ):
            continue
        argument = node.args[0]
        if not _contains_calculus_variable(argument):
            constant_value = _constant_sympy_expression(argument)
            if constant_value is None:
                return None
            cosine_value = _sympy.simplify(_sympy.cos(constant_value))
            if cosine_value.is_zero is not False:
                return None
            continue
        cosine = _cosine_call(argument)
        key = ast.dump(cosine, include_attributes=False)
        if key not in seen:
            seen.add(key)
            arguments.append(cosine)
    return arguments


def _contains_calculus_variable(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Name) and child.id not in {"pi", "E"}
        for child in ast.walk(node)
    )


def _constant_sympy_expression(node: ast.AST):
    import sympy as _sympy

    fraction = _constant_fraction(node)
    if fraction is not None:
        return _sympy.Rational(fraction.numerator, fraction.denominator)
    if isinstance(node, ast.Name):
        return {"pi": _sympy.pi, "E": _sympy.E}.get(node.id)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        inner = _constant_sympy_expression(node.operand)
        if inner is None:
            return None
        return inner if isinstance(node.op, ast.UAdd) else -inner
    if isinstance(node, ast.BinOp):
        left = _constant_sympy_expression(node.left)
        right = _constant_sympy_expression(node.right)
        if left is None or right is None:
            return None
        operation = {
            ast.Add: lambda: left + right,
            ast.Sub: lambda: left - right,
            ast.Mult: lambda: left * right,
            ast.Div: lambda: left / right if right != 0 else None,
            ast.Pow: lambda: left ** right,
        }.get(type(node.op))
        return operation() if operation is not None else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and len(node.args) == 1:
        argument = _constant_sympy_expression(node.args[0])
        if argument is None or node.keywords:
            return None
        function = {
            "sin": _sympy.sin,
            "cos": _sympy.cos,
            "tan": _sympy.tan,
            "exp": _sympy.exp,
            "sinh": _sympy.sinh,
            "cosh": _sympy.cosh,
            "tanh": _sympy.tanh,
            "log": _sympy.log,
        }.get(node.func.id)
        return function(argument) if function is not None else None
    return None


def _real_sympy_expression(
    node: ast.AST,
    substitutions: dict[str, Fraction] | None = None,
):
    import sympy as _sympy

    fraction = _constant_fraction(node)
    if fraction is not None:
        return _sympy.Rational(fraction.numerator, fraction.denominator)
    if isinstance(node, ast.Name):
        if node.id == "pi":
            return _sympy.pi
        if node.id == "E":
            return _sympy.E
        value = (substitutions or {}).get(node.id)
        if value is not None:
            return _sympy.Rational(value.numerator, value.denominator)
        return _sympy.Symbol(node.id)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        inner = _real_sympy_expression(node.operand, substitutions)
        if inner is None:
            return None
        return inner if isinstance(node.op, ast.UAdd) else -inner
    if isinstance(node, ast.BinOp):
        left = _real_sympy_expression(node.left, substitutions)
        right = _real_sympy_expression(node.right, substitutions)
        if left is None or right is None:
            return None
        operation = {
            ast.Add: lambda: left + right,
            ast.Sub: lambda: left - right,
            ast.Mult: lambda: left * right,
            ast.Div: lambda: left / right if right != 0 else None,
            ast.Pow: lambda: left ** right,
        }.get(type(node.op))
        return operation() if operation is not None else None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and len(node.args) == 1
        and not node.keywords
    ):
        argument = _real_sympy_expression(node.args[0], substitutions)
        if argument is None:
            return None
        function = {
            "abs": _sympy.Abs,
            "Abs": _sympy.Abs,
            "sin": _sympy.sin,
            "cos": _sympy.cos,
            "tan": _sympy.tan,
            "asin": _sympy.asin,
            "acos": _sympy.acos,
            "atan": _sympy.atan,
            "asinh": _sympy.asinh,
            "acosh": _sympy.acosh,
            "exp": _sympy.exp,
            "sinh": _sympy.sinh,
            "cosh": _sympy.cosh,
            "tanh": _sympy.tanh,
            "log": _sympy.log,
            "sqrt": _sympy.sqrt,
        }.get(node.func.id)
        return function(argument) if function is not None else None
    return None


def _affine_interval_values(
    node: ast.AST,
    variable: str,
    lower: Fraction,
    upper: Fraction,
) -> tuple[Fraction, Fraction] | None:
    import sympy as _sympy

    expression = _real_sympy_expression(node)
    symbol = _sympy.Symbol(variable)
    if expression is None or expression.free_symbols - {symbol}:
        return None
    try:
        polynomial = _sympy.Poly(expression, symbol, domain=_sympy.QQ)
    except (TypeError, ValueError, _sympy.PolynomialError):
        return None
    if polynomial.degree() > 1:
        return None

    interval_lower = min(lower, upper)
    interval_upper = max(lower, upper)
    lower_value = polynomial.eval(
        _sympy.Rational(interval_lower.numerator, interval_lower.denominator)
    )
    upper_value = polynomial.eval(
        _sympy.Rational(interval_upper.numerator, interval_upper.denominator)
    )
    if not isinstance(lower_value, _sympy.Rational) or not isinstance(
        upper_value, _sympy.Rational
    ):
        return None
    return (
        Fraction(int(lower_value.p), int(lower_value.q)),
        Fraction(int(upper_value.p), int(upper_value.q)),
    )


def _cosine_call(argument: ast.AST) -> ast.Call:
    return ast.Call(
        func=ast.Name(id="cos", ctx=ast.Load()),
        args=[argument],
        keywords=[],
    )


def _render_real_calculus_lean(
    source: str,
    allow_variable_denominators: bool = False,
) -> tuple[str | None, set[str]]:
    try:
        tree = ast.parse(source, mode="eval")
    except (SyntaxError, RecursionError, ValueError):
        return None, set()
    names: set[str] = set()
    rendered = _render_real_calculus_node(
        tree.body,
        names,
        {"nodes": 0},
        0,
        allow_variable_denominators=allow_variable_denominators,
    )
    return rendered, names


def _render_real_calculus_node(
    node: ast.AST,
    names: set[str],
    state: dict[str, int],
    depth: int,
    substitutions: dict[str, str] | None = None,
    allow_variable_denominators: bool = False,
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
            node.operand,
            names,
            state,
            depth + 1,
            substitutions,
            allow_variable_denominators,
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
            "tan": "Real.tan",
            "asin": "Real.arcsin",
            "acos": "Real.arccos",
            "atan": "Real.arctan",
            "asinh": "Real.arsinh",
            "acosh": "Real.arcosh",
            "exp": "Real.exp",
            "sinh": "Real.sinh",
            "cosh": "Real.cosh",
            "tanh": "Real.tanh",
            "log": "Real.log",
            "sqrt": "Real.sqrt",
        }.get(node.func.id)
        if function_name is None:
            return None
        argument_node = (
            _unwrap_real_abs(node.args[0])
            if node.func.id == "log"
            else node.args[0]
        )
        argument = _render_real_calculus_node(
            argument_node,
            names,
            state,
            depth + 1,
            substitutions,
            allow_variable_denominators,
        )
        return f"({function_name} {argument})" if argument is not None else None
    if not isinstance(node, ast.BinOp):
        return None

    left = _render_real_calculus_node(
        node.left,
        names,
        state,
        depth + 1,
        substitutions,
        allow_variable_denominators,
    )
    right = _render_real_calculus_node(
        node.right,
        names,
        state,
        depth + 1,
        substitutions,
        allow_variable_denominators,
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
        if denominator == 0 or (denominator is None and not allow_variable_denominators):
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


def _render_rational_limit_lean(source: str) -> tuple[str | None, set[str]]:
    try:
        tree = ast.parse(source, mode="eval")
    except (SyntaxError, RecursionError, ValueError):
        return None, set()
    names: set[str] = set()
    rendered = _render_rational_limit_node(tree.body, names, {"nodes": 0}, 0)
    return rendered, names


def _render_rational_limit_node(
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
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", node.id)
            or node.id in {"pi", "E", "I", "oo"}
        ):
            return None
        names.add(node.id)
        return (substitutions or {}).get(node.id, node.id)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        inner = _render_rational_limit_node(
            node.operand, names, state, depth + 1, substitutions
        )
        if inner is None:
            return None
        return inner if isinstance(node.op, ast.UAdd) else f"(-{inner})"
    if not isinstance(node, ast.BinOp):
        return None

    left = _render_rational_limit_node(
        node.left, names, state, depth + 1, substitutions
    )
    right = _render_rational_limit_node(
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
        if denominator == 0:
            return None
        return f"({left} / {right})"
    if isinstance(node.op, ast.Pow):
        exponent = _constant_fraction(node.right)
        if exponent is None or exponent.denominator != 1 or not 0 <= exponent <= 16:
            return None
        return f"({left} ^ {exponent.numerator})"
    return None


def _exact_rational_node_value(
    node: ast.AST,
    variable: str,
    point: Fraction,
) -> Fraction | None:
    constant = _constant_fraction(node)
    if constant is not None:
        return constant
    if isinstance(node, ast.Name):
        return point if node.id == variable else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _exact_rational_node_value(node.operand, variable, point)
        if value is None:
            return None
        return value if isinstance(node.op, ast.UAdd) else -value
    if not isinstance(node, ast.BinOp):
        return None
    left = _exact_rational_node_value(node.left, variable, point)
    right = _exact_rational_node_value(node.right, variable, point)
    if left is None or right is None:
        return None
    if isinstance(node.op, ast.Add):
        return left + right
    if isinstance(node.op, ast.Sub):
        return left - right
    if isinstance(node.op, ast.Mult):
        return left * right
    if isinstance(node.op, ast.Div):
        return left / right if right != 0 else None
    if isinstance(node.op, ast.Pow):
        exponent = _constant_fraction(node.right)
        if exponent is None or exponent.denominator != 1 or not 0 <= exponent <= 16:
            return None
        return left ** exponent.numerator
    return None


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
    domain_hypotheses: dict[str, str] | None = None,
    positive_hypotheses: dict[str, str] | None = None,
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
        if node.func.id == "log":
            argument = _unwrap_real_abs(node.args[0])
            constant_argument = _constant_fraction(argument)
            if constant_argument is not None:
                if constant_argument <= 0:
                    return None
                domain_proof = "by norm_num"
            else:
                argument_key = ast.dump(argument, include_attributes=False)
                positive_hypothesis = (positive_hypotheses or {}).get(argument_key)
                if positive_hypothesis is not None:
                    domain_proof = f"ne_of_gt {positive_hypothesis}"
                else:
                    domain_proof = (domain_hypotheses or {}).get(argument_key)
                    if domain_proof is None:
                        return None
            inner = _derivative_proof_term(
                argument,
                variable,
                function_variable,
                point,
                state,
                depth + 1,
                domain_hypotheses,
                positive_hypotheses,
            )
            return f"({inner}).log ({domain_proof})" if inner is not None else None
        if node.func.id == "tan":
            argument = node.args[0]
            if not any(
                isinstance(child, ast.Name) and child.id == variable
                for child in ast.walk(argument)
            ):
                rendered_argument = _render_real_calculus_node(
                    argument, set(), {"nodes": 0}, 0
                )
                if rendered_argument is None:
                    return None
                return (
                    f"(hasDerivAt_const {point} "
                    f"(Real.tan {rendered_argument}))"
                )
            cosine_key = ast.dump(
                _cosine_call(argument), include_attributes=False
            )
            hypothesis_name = (domain_hypotheses or {}).get(cosine_key)
            if hypothesis_name is None:
                return None
            inner = _derivative_proof_term(
                argument,
                variable,
                function_variable,
                point,
                state,
                depth + 1,
                domain_hypotheses,
                positive_hypotheses,
            )
            if inner is None:
                return None
            return (
                f"(Real.hasDerivAt_tan {hypothesis_name}).comp "
                f"{point} ({inner})"
            )
        if node.func.id == "tanh":
            argument = node.args[0]
            if not any(
                isinstance(child, ast.Name) and child.id == variable
                for child in ast.walk(argument)
            ):
                rendered_argument = _render_real_calculus_node(
                    argument, set(), {"nodes": 0}, 0
                )
                if rendered_argument is None:
                    return None
                return (
                    f"(hasDerivAt_const {point} "
                    f"(Real.tanh {rendered_argument}))"
                )
            rendered_argument = _render_real_calculus_node(
                argument,
                set(),
                {"nodes": 0},
                0,
                allow_variable_denominators=True,
            )
            inner = _derivative_proof_term(
                argument,
                variable,
                function_variable,
                point,
                state,
                depth + 1,
                domain_hypotheses,
                positive_hypotheses,
            )
            if rendered_argument is None or inner is None:
                return None
            return (
                f"(wolfram_hasDerivAt_tanh {rendered_argument}).comp "
                f"{point} ({inner})"
            )
        if node.func.id == "acosh":
            argument = node.args[0]
            if not any(
                isinstance(child, ast.Name) and child.id == variable
                for child in ast.walk(argument)
            ):
                rendered_argument = _render_real_calculus_node(
                    argument, set(), {"nodes": 0}, 0
                )
                if rendered_argument is None:
                    return None
                return (
                    f"(hasDerivAt_const {point} "
                    f"(Real.arcosh {rendered_argument}))"
                )
            hypothesis_name = (domain_hypotheses or {}).get(
                _inverse_hyperbolic_cosh_hypothesis_key(argument)
            )
            if hypothesis_name is None:
                return None
            inner = _derivative_proof_term(
                argument,
                variable,
                function_variable,
                point,
                state,
                depth + 1,
                domain_hypotheses,
                positive_hypotheses,
            )
            if inner is None:
                return None
            return (
                f"(Real.hasDerivAt_arcosh "
                f"(Set.mem_Ioi.mpr {hypothesis_name})).comp "
                f"{point} ({inner})"
            )
        if node.func.id in {"asin", "acos"}:
            argument = node.args[0]
            function_name = (
                "Real.arcsin" if node.func.id == "asin" else "Real.arccos"
            )
            if not any(
                isinstance(child, ast.Name) and child.id == variable
                for child in ast.walk(argument)
            ):
                rendered_argument = _render_real_calculus_node(
                    argument, set(), {"nodes": 0}, 0
                )
                if rendered_argument is None:
                    return None
                return (
                    f"(hasDerivAt_const {point} "
                    f"({function_name} {rendered_argument}))"
                )
            lower_hypothesis = (domain_hypotheses or {}).get(
                _inverse_trig_hypothesis_key(argument, "lower")
            )
            upper_hypothesis = (domain_hypotheses or {}).get(
                _inverse_trig_hypothesis_key(argument, "upper")
            )
            if lower_hypothesis is None or upper_hypothesis is None:
                return None
            inner = _derivative_proof_term(
                argument,
                variable,
                function_variable,
                point,
                state,
                depth + 1,
                domain_hypotheses,
                positive_hypotheses,
            )
            if inner is None:
                return None
            theorem_name = (
                "Real.hasDerivAt_arcsin"
                if node.func.id == "asin"
                else "Real.hasDerivAt_arccos"
            )
            return (
                f"({theorem_name} (ne_of_gt {lower_hypothesis}) "
                f"(ne_of_lt {upper_hypothesis})).comp {point} ({inner})"
            )
        if node.func.id == "sqrt":
            argument = node.args[0]
            if not any(
                isinstance(child, ast.Name) and child.id == variable
                for child in ast.walk(argument)
            ):
                rendered_argument = _render_real_calculus_node(
                    argument, set(), {"nodes": 0}, 0
                )
                if rendered_argument is None:
                    return None
                return (
                    f"(hasDerivAt_const {point} "
                    f"(Real.sqrt {rendered_argument}))"
                )
            positive_hypothesis = (positive_hypotheses or {}).get(
                ast.dump(argument, include_attributes=False)
            )
            if positive_hypothesis is None:
                return None
            inner = _derivative_proof_term(
                argument,
                variable,
                function_variable,
                point,
                state,
                depth + 1,
                domain_hypotheses,
                positive_hypotheses,
            )
            if inner is None:
                return None
            return f"({inner}).sqrt (ne_of_gt {positive_hypothesis})"
        derivative_method = {
            "sin": "sin",
            "cos": "cos",
            "atan": "arctan",
            "asinh": "arsinh",
            "acosh": "arcosh",
            "exp": "exp",
            "sinh": "sinh",
            "cosh": "cosh",
        }.get(node.func.id)
        if derivative_method is None:
            return None
        inner = _derivative_proof_term(
            node.args[0],
            variable,
            function_variable,
            point,
            state,
            depth + 1,
            domain_hypotheses,
            positive_hypotheses,
        )
        return f"({inner}).{derivative_method}" if inner is not None else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        inner = _derivative_proof_term(
            node.operand,
            variable,
            function_variable,
            point,
            state,
            depth + 1,
            domain_hypotheses,
            positive_hypotheses,
        )
        if inner is None:
            return None
        return inner if isinstance(node.op, ast.UAdd) else f"({inner}).neg"
    if not isinstance(node, ast.BinOp):
        return None

    left = _derivative_proof_term(
        node.left,
        variable,
        function_variable,
        point,
        state,
        depth + 1,
        domain_hypotheses,
        positive_hypotheses,
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
        if denominator == 0:
            return None
        if denominator is not None:
            return f"({left}).div_const {_lean_rational(denominator)}"
        hypothesis_name = (domain_hypotheses or {}).get(
            ast.dump(node.right, include_attributes=False)
        )
        if hypothesis_name is None:
            return None
        right = _derivative_proof_term(
            node.right,
            variable,
            function_variable,
            point,
            state,
            depth + 1,
            domain_hypotheses,
            positive_hypotheses,
        )
        if right is None:
            return None
        return f"({left}).div ({right}) {hypothesis_name}"

    right = _derivative_proof_term(
        node.right,
        variable,
        function_variable,
        point,
        state,
        depth + 1,
        domain_hypotheses,
        positive_hypotheses,
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
