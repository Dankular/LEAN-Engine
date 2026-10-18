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


_DECIMAL_LITERAL = re.compile(
    r"(?<![A-Za-z0-9_])(?:(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)"
)
_WOLFRAM_REAL_FUNCTION = re.compile(
    r"\b(?P<function>Sin|Cos|Tan|ArcSin|ArcCos|ArcTan|Sqrt|Exp|Sinh|Cosh|Log)\s*\[(?P<argument>[^\[\]]*)\]",
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
