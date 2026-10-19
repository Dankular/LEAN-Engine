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


@dataclass(frozen=True)
class _RealPolynomialRoot:
    lean_expression: str
    wolfram_expression: str


_DECIMAL_LITERAL = re.compile(
    r"(?<![A-Za-z0-9_])(?:(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)"
)
_WOLFRAM_REAL_FUNCTION = re.compile(
    r"\b(?P<function>Sin|Cos|Tan|ArcSin|ArcCos|ArcTan|Sqrt|Exp|Sinh|Cosh|Log)\s*\[(?P<argument>[^\[\]]*)\]",
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
_REAL_POLYNOMIAL_LIMIT = re.compile(
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

    real_polynomial_limit = _REAL_POLYNOMIAL_LIMIT.fullmatch(code)
    if real_polynomial_limit is not None:
        return _prepare_real_polynomial_limit(
            real_polynomial_limit.group("expression"),
            real_polynomial_limit.group("variable"),
            real_polynomial_limit.group("point"),
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


def _prepare_real_polynomial_limit(
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
        expression_lean, expression_symbols = _render_polynomial_lean(parsed.expression)
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None
    if expression_lean is None:
        return None

    point = _parse_exact_rational(point_source)
    if (
        point is None
        or abs(point.numerator).bit_length() > 256
        or point.denominator.bit_length() > 256
    ):
        return None

    symbolic = run_symbolic(SymbolicRequest(
        operation="limit",
        expression=parsed.expression,
        variable=variable,
        point=_fraction_text(point),
        time_limit_seconds=max(0.001, min(time_limit_seconds, 60.0)),
    ))
    if symbolic.get("timed_out"):
        return Computation("", "", timed_out=True)
    if symbolic.get("error") or not isinstance(symbolic.get("result"), str):
        diagnostic = symbolic.get("diagnostics") or symbolic.get("error") or (
            "Symbolic worker returned no finite limit"
        )
        return Computation("", "", backend_error=str(diagnostic)[:1000])

    result = symbolic["result"]
    result_lean, result_symbols = _render_polynomial_lean(result)
    if result_lean is None:
        return None

    used_names = set(expression_symbols) | set(result_symbols)
    limit_variable = _fresh_identifier("limit_variable", used_names)
    limit_body = _render_polynomial_node(
        expression_tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: limit_variable},
    )
    value_at_point = _render_polynomial_node(
        expression_tree.body,
        set(),
        {"nodes": 0},
        0,
        substitutions={variable: _lean_rational(point)},
    )
    if limit_body is None or value_at_point is None:
        return None
    continuity_proof = _polynomial_continuity_proof(
        expression_tree.body,
        variable,
        {"nodes": 0},
        0,
    )
    if continuity_proof is None:
        return None

    theorem_symbols = sorted(
        (expression_symbols | result_symbols) - {variable, limit_variable}
    )
    binders = " ".join(f"({name} : ℝ)" for name in theorem_symbols)
    theorem_binders = f" {binders}" if binders else ""
    point_lean = _lean_rational(point)
    source = f"""import Mathlib.Basic.Real.Basic
import Mathlib.Topology.Instances.Real.Lemmas
import Mathlib.Topology.Continuous
import Mathlib.Tactic.Ring

theorem wolfram_language_compute{theorem_binders} :
    Filter.Tendsto (fun {limit_variable} : ℝ => {limit_body})
      (nhds {point_lean}) (nhds {result_lean}) := by
  have hcont : ContinuousAt (fun {limit_variable} : ℝ => {limit_body})
      {point_lean} := {continuity_proof}
  have hvalue : {value_at_point} = {result_lean} := by ring
  simpa only [hvalue] using hcont.tendsto

#print axioms wolfram_language_compute
"""
    return Computation(result.replace("**", "^"), source)


def _polynomial_continuity_proof(
    node: ast.AST,
    variable: str,
    state: dict[str, int],
    depth: int,
) -> str | None:
    state["nodes"] += 1
    if state["nodes"] > 256 or depth > 32:
        return None
    if _constant_fraction(node) is not None:
        return "continuousAt_const"
    if isinstance(node, ast.Name):
        return "continuousAt_id" if node.id == variable else "continuousAt_const"
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        inner = _polynomial_continuity_proof(
            node.operand, variable, state, depth + 1
        )
        if inner is None:
            return None
        return inner if isinstance(node.op, ast.UAdd) else f"({inner}).neg"
    if not isinstance(node, ast.BinOp):
        return None

    left = _polynomial_continuity_proof(node.left, variable, state, depth + 1)
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
        if denominator is None or denominator == 0:
            return None
        return f"({left}).div_const {_lean_rational(denominator)}"

    right = _polynomial_continuity_proof(node.right, variable, state, depth + 1)
    if right is None:
        return None
    method = {
        ast.Add: "add",
        ast.Sub: "sub",
        ast.Mult: "mul",
    }.get(type(node.op))
    return f"({left}).{method} ({right})" if method is not None else None


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

    for index, domain_expression in enumerate(domain_expressions):
        key = ast.dump(domain_expression, include_attributes=False)
        if key in domain_hypotheses or key in positive_hypotheses:
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
    has_tangent = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "tan"
        for expression_tree in (integrand_tree, tree)
        for node in ast.walk(expression_tree.body)
    )
    tangent_simp = ", Real.tan_eq_sin_div_cos" if has_tangent else ""
    derivative_tactic = (
        f"simp [id_eq{tangent_simp}] <;> "
        f"field_simp{field_simp_arguments} <;> ring"
    )
    source = f"""import Mathlib.Analysis.Calculus.Deriv.Add
import Mathlib.Analysis.Calculus.Deriv.Mul
import Mathlib.Analysis.Calculus.Deriv.Pow
import Mathlib.Analysis.Calculus.Deriv.Inv
import Mathlib.Analysis.SpecialFunctions.ExpDeriv
import Mathlib.Analysis.SpecialFunctions.Log.Deriv
import Mathlib.Analysis.SpecialFunctions.Sqrt
import Mathlib.Analysis.SpecialFunctions.Trigonometric.ArctanDeriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.Deriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.DerivHyp
import Mathlib.Tactic.FieldSimp
import Mathlib.Tactic.Ring

theorem wolfram_language_compute{theorem_binders} :
    HasDerivAt (fun {function_variable} : ℝ => {rendered}) {integrand_lean} {variable} := by
  convert ({proof}) using 1
  all_goals
    first
    | {function_equality_tactic}
    | {derivative_tactic}

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
    source = f"""import Mathlib.Analysis.Calculus.Deriv.Add
import Mathlib.Analysis.Calculus.Deriv.Mul
import Mathlib.Analysis.Calculus.Deriv.Pow
import Mathlib.Analysis.Calculus.Deriv.Inv
import Mathlib.Analysis.SpecialFunctions.ExpDeriv
import Mathlib.Analysis.SpecialFunctions.Log.Deriv
import Mathlib.Analysis.SpecialFunctions.Sqrt
import Mathlib.Analysis.SpecialFunctions.Trigonometric.ArctanDeriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.Deriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.DerivHyp
import Mathlib.Analysis.SpecialFunctions.Trigonometric.InverseDeriv
import Mathlib.Tactic.FieldSimp
import Mathlib.Tactic.Ring

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
            }.get(function_name, function_name)
            argument = match.group("argument")
            try:
                argument = parse_math_input(argument).expression
            except MathInputError:
                pass
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
            "exp": _sympy.exp,
            "sinh": _sympy.sinh,
            "cosh": _sympy.cosh,
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
            "exp": "Real.exp",
            "sinh": "Real.sinh",
            "cosh": "Real.cosh",
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
