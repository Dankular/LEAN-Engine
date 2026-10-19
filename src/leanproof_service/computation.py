from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
from math import isqrt
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
    proof_result_authoritative: bool = False
    numeric_precision: int | None = None
    numeric_error_bound: str | None = None


@dataclass(frozen=True)
class _RealPolynomialRoot:
    lean_expression: str
    wolfram_expression: str


_DECIMAL_LITERAL = re.compile(
    r"(?<![A-Za-z0-9_])(?:(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)"
)
_WOLFRAM_REAL_FUNCTION = re.compile(
    r"\b(?P<function>"
    r"Sin|Cos|Tan|Sec|Csc|Coth|Cot|Sech|Csch|ArcSin|ArcCos|ArcTan|ArcSinh|ArcCosh|ArcTanh|Sqrt|Exp|Sinh|Cosh|Tanh|Log"
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
_TRIG_EXPAND_MAX_MULTIPLE = 12
_MAX_DYADIC_PI_DENOMINATOR_EXPONENT = 12
_MAX_RECURSIVE_HALF_ANGLE_STEPS = 8
_PI_LOWER_D20 = Fraction(314159265358979323846, 10**20)
_PI_UPPER_D20 = Fraction(314159265358979323847, 10**20)
_EXACT_PI_TRIG_VALUES: dict[
    str, dict[int, tuple[str, str, str]]
] = {
    "sin": {
        1: ("0", "0", "Real.sin_pi"),
        2: ("1", "1", "Real.sin_pi_div_two"),
        3: ("sqrt(3)/2", "Real.sqrt 3 / 2", "Real.sin_pi_div_three"),
        4: ("sqrt(2)/2", "Real.sqrt 2 / 2", "Real.sin_pi_div_four"),
        6: ("1/2", "1 / 2", "Real.sin_pi_div_six"),
        5: (
            "sqrt(10 - 2*sqrt(5))/4",
            "Real.sqrt (10 - 2 * Real.sqrt 5) / 4",
            "wolfram_sin_pi_div_five",
        ),
        10: (
            "(sqrt(5) - 1)/4",
            "(Real.sqrt 5 - 1) / 4",
            "wolfram_sin_pi_div_ten",
        ),
        8: (
            "sqrt(2 - sqrt(2))/2",
            "Real.sqrt (2 - Real.sqrt 2) / 2",
            "Real.sin_pi_div_eight",
        ),
        16: (
            "sqrt(2 - sqrt(2 + sqrt(2)))/2",
            "Real.sqrt (2 - Real.sqrt (2 + Real.sqrt 2)) / 2",
            "Real.sin_pi_div_sixteen",
        ),
        32: (
            "sqrt(2 - sqrt(2 + sqrt(2 + sqrt(2))))/2",
            "Real.sqrt (2 - Real.sqrt (2 + Real.sqrt (2 + Real.sqrt 2))) / 2",
            "Real.sin_pi_div_thirty_two",
        ),
    },
    "cos": {
        1: ("-1", "-1", "Real.cos_pi"),
        2: ("0", "0", "Real.cos_pi_div_two"),
        3: ("1/2", "1 / 2", "Real.cos_pi_div_three"),
        4: ("sqrt(2)/2", "Real.sqrt 2 / 2", "Real.cos_pi_div_four"),
        6: ("sqrt(3)/2", "Real.sqrt 3 / 2", "Real.cos_pi_div_six"),
        5: (
            "(1 + sqrt(5))/4",
            "(1 + Real.sqrt 5) / 4",
            "Real.cos_pi_div_five",
        ),
        10: (
            "sqrt(10 + 2*sqrt(5))/4",
            "Real.sqrt (10 + 2 * Real.sqrt 5) / 4",
            "wolfram_cos_pi_div_ten",
        ),
        8: (
            "sqrt(2 + sqrt(2))/2",
            "Real.sqrt (2 + Real.sqrt 2) / 2",
            "Real.cos_pi_div_eight",
        ),
        16: (
            "sqrt(2 + sqrt(2 + sqrt(2)))/2",
            "Real.sqrt (2 + Real.sqrt (2 + Real.sqrt 2)) / 2",
            "Real.cos_pi_div_sixteen",
        ),
        32: (
            "sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2))))/2",
            "Real.sqrt (2 + Real.sqrt (2 + Real.sqrt (2 + Real.sqrt 2))) / 2",
            "Real.cos_pi_div_thirty_two",
        ),
    },
    "tan": {
        1: ("0", "0", "Real.tan_pi"),
        3: ("sqrt(3)", "Real.sqrt 3", "Real.tan_pi_div_three"),
        4: ("1", "1", "Real.tan_pi_div_four"),
        6: ("1/sqrt(3)", "1 / Real.sqrt 3", "Real.tan_pi_div_six"),
    },
}
_EXACT_PI_TRIG_SUPPORTING_LEMMAS = {
    "wolfram_sin_pi_div_five": """theorem wolfram_sin_pi_div_five :
    Real.sin (Real.pi / 5) = Real.sqrt (10 - 2 * Real.sqrt 5) / 4 := by
  have h5 : Real.sqrt 5 ^ 2 = (5 : ℝ) := by
    rw [Real.sq_sqrt (by norm_num : (0 : ℝ) ≤ 5)]
  have h0 : 0 < Real.pi / 5 := by positivity
  have hpi : Real.pi / 5 < Real.pi := by nlinarith [Real.pi_pos]
  rw [Real.sin_eq_sqrt_one_sub_cos_sq h0.le hpi.le, Real.cos_pi_div_five]
  have hrad : 0 ≤ 10 - 2 * Real.sqrt 5 := by nlinarith [h5]
  have hsqrt := Real.sq_sqrt hrad
  have hrad_eq :
      1 - ((1 + Real.sqrt 5) / 4) ^ 2 =
        (Real.sqrt (10 - 2 * Real.sqrt 5) / 4) ^ 2 := by
    nlinarith [h5, hsqrt]
  rw [hrad_eq, Real.sqrt_sq_eq_abs, abs_of_nonneg]
  positivity
""",
    "wolfram_sin_pi_div_ten": """theorem wolfram_sin_pi_div_ten :
    Real.sin (Real.pi / 10) = (Real.sqrt 5 - 1) / 4 := by
  have h5 : Real.sqrt 5 ^ 2 = (5 : ℝ) := by
    rw [Real.sq_sqrt (by norm_num : (0 : ℝ) ≤ 5)]
  have h0 : 0 ≤ Real.pi / 5 := by positivity
  have h2 : Real.pi / 5 ≤ 2 * Real.pi := by nlinarith [Real.pi_pos]
  rw [show Real.pi / 10 = (Real.pi / 5) / 2 by ring,
    Real.sin_half_eq_sqrt h0 h2, Real.cos_pi_div_five]
  have hrad_eq :
      (1 - (1 + Real.sqrt 5) / 4) / 2 =
        ((Real.sqrt 5 - 1) / 4) ^ 2 := by
    nlinarith [h5]
  rw [hrad_eq, Real.sqrt_sq_eq_abs, abs_of_nonneg]
  nlinarith [h5, Real.sqrt_nonneg (5 : ℝ)]
""",
    "wolfram_cos_pi_div_ten": """theorem wolfram_cos_pi_div_ten :
    Real.cos (Real.pi / 10) = Real.sqrt (10 + 2 * Real.sqrt 5) / 4 := by
  have h5 : Real.sqrt 5 ^ 2 = (5 : ℝ) := by
    rw [Real.sq_sqrt (by norm_num : (0 : ℝ) ≤ 5)]
  have hlo : -Real.pi ≤ Real.pi / 5 := by nlinarith [Real.pi_pos]
  have hhi : Real.pi / 5 ≤ Real.pi := by nlinarith [Real.pi_pos]
  rw [show Real.pi / 10 = (Real.pi / 5) / 2 by ring,
    Real.cos_half hlo hhi, Real.cos_pi_div_five]
  have hrad : 0 ≤ 10 + 2 * Real.sqrt 5 := by positivity
  have hsqrt := Real.sq_sqrt hrad
  have hrad_eq :
      (1 + (1 + Real.sqrt 5) / 4) / 2 =
        (Real.sqrt (10 + 2 * Real.sqrt 5) / 4) ^ 2 := by
    nlinarith [h5, hsqrt]
  rw [hrad_eq, Real.sqrt_sq_eq_abs, abs_of_nonneg]
  positivity
""",
}


def _sqrt_two_add_series_expression(depth: int, *, lean: bool) -> str:
    if depth <= 0:
        return "0"
    sqrt = "Real.sqrt" if lean else "sqrt"
    expression = f"{sqrt} 2" if lean else "sqrt(2)"
    for _ in range(1, depth):
        expression = (
            f"{sqrt} (2 + {expression})"
            if lean
            else f"sqrt(2 + {expression})"
        )
    return expression


def _exact_dyadic_pi_trig_value(
    function: str,
    denominator: int,
) -> tuple[tuple[str, str, str], str] | None:
    if (
        function not in {"sin", "cos"}
        or denominator < 64
        or denominator & (denominator - 1)
    ):
        return None
    exponent = denominator.bit_length() - 1
    if exponent > _MAX_DYADIC_PI_DENOMINATOR_EXPONENT:
        return None

    if function == "cos":
        depth = exponent - 1
        display_root = _sqrt_two_add_series_expression(depth, lean=False)
        lean_root = _sqrt_two_add_series_expression(depth, lean=True)
        display_result = f"{display_root}/2"
        lean_result = f"({lean_root}) / 2"
        supporting_index = depth
        mathlib_theorem = "cos_pi_over_two_pow"
    else:
        depth = exponent - 2
        display_inner = _sqrt_two_add_series_expression(depth, lean=False)
        lean_inner = _sqrt_two_add_series_expression(depth, lean=True)
        display_result = f"sqrt(2 - {display_inner})/2"
        lean_result = f"Real.sqrt (2 - {lean_inner}) / 2"
        supporting_index = depth
        mathlib_theorem = "sin_pi_over_two_pow_succ"

    lemma_name = f"wolfram_{function}_pi_div_{denominator}"
    theorem_source = f"""theorem {lemma_name} :
    Real.{function} (Real.pi / {denominator}) = {lean_result} := by
  rw [show Real.pi / {denominator} = Real.pi / 2 ^ {exponent} by norm_num,
    Real.{mathlib_theorem} {supporting_index}]
  simp [Real.sqrtTwoAddSeries]
"""
    return (display_result, lean_result, lemma_name), theorem_source


def _exact_iterated_half_pi_trig_value(
    function: str,
    denominator: int,
) -> tuple[tuple[str, str, str], str] | None:
    if function not in {"sin", "cos"}:
        return None

    base_denominator = None
    denominator_ratio = 0
    for candidate in (6, 10):
        candidate_ratio, remainder = divmod(denominator, candidate)
        if (
            candidate_ratio > 1
            and remainder == 0
            and candidate_ratio & (candidate_ratio - 1) == 0
        ):
            base_denominator = candidate
            denominator_ratio = candidate_ratio
            break
    if base_denominator is None:
        return None
    half_angle_steps = denominator_ratio.bit_length() - 1
    if not 1 <= half_angle_steps <= _MAX_RECURSIVE_HALF_ANGLE_STEPS:
        return None

    base_value = _EXACT_PI_TRIG_VALUES["cos"][base_denominator]
    display_cosine = base_value[0]
    lean_cosine = base_value[1]
    base_lemma = base_value[2]
    statements = [
        f"  have hcos0 : Real.cos (Real.pi / {base_denominator}) = "
        f"{lean_cosine} := {base_lemma}"
    ]
    previous_denominator = base_denominator
    cosine_steps = half_angle_steps if function == "cos" else half_angle_steps - 1
    for step in range(1, cosine_steps + 1):
        current_denominator = base_denominator * (2**step)
        next_cosine_lean = f"Real.sqrt ((1 + {lean_cosine}) / 2)"
        next_cosine_display = f"sqrt((1 + {display_cosine})/2)"
        statements.extend(
            [
                f"  have hcos{step} : Real.cos (Real.pi / {current_denominator}) = "
                f"{next_cosine_lean} := by",
                "    rw [show Real.pi / "
                f"{current_denominator} = (Real.pi / {previous_denominator}) / 2 by",
                "      norm_num",
                "      all_goals ring]",
                "    rw [Real.cos_half (by nlinarith [Real.pi_pos]) "
                "(by nlinarith [Real.pi_pos]), hcos"
                f"{step - 1}]",
            ]
        )
        previous_denominator = current_denominator
        lean_cosine = next_cosine_lean
        display_cosine = next_cosine_display

    if function == "cos":
        display_result = display_cosine
        lean_result = lean_cosine
        final_proof = f"  exact hcos{half_angle_steps}"
    else:
        display_result = f"sqrt((1 - {display_cosine})/2)"
        lean_result = f"Real.sqrt ((1 - {lean_cosine}) / 2)"
        statements.extend(
            [
                f"  have hsin{half_angle_steps} : Real.sin (Real.pi / {denominator}) = "
                f"{lean_result} := by",
                "    rw [show Real.pi / "
                f"{denominator} = (Real.pi / {previous_denominator}) / 2 by",
                "      norm_num",
                "      all_goals ring]",
                "    rw [Real.sin_half_eq_sqrt (by positivity) "
                "(by nlinarith [Real.pi_pos]), hcos"
                f"{half_angle_steps - 1}]",
            ]
        )
        final_proof = f"  exact hsin{half_angle_steps}"

    lemma_name = f"wolfram_{function}_pi_div_{denominator}"
    theorem_source = "\n".join(
        [
            "theorem "
            f"{lemma_name} : Real.{function} (Real.pi / {denominator}) = "
            f"{lean_result} := by",
            *statements,
            final_proof,
        ]
    )
    base_support = _EXACT_PI_TRIG_SUPPORTING_LEMMAS.get(base_lemma, "")
    return (
        (display_result, lean_result, lemma_name),
        f"{base_support}\n{theorem_source}",
    )


def prepare_computation(code: str, time_limit_seconds: float = 5.0) -> Computation | None:
    # Wolfram Language uses case-sensitive `Pi`; the shared parser reserves lowercase `pi`.
    if re.search(r"\bpi\b|π", code):
        return None

    numeric_call = re.fullmatch(
        r"(?is)\s*N\s*\[(?P<arguments>.*)\]\s*",
        code,
    )
    if numeric_call is not None:
        arguments = _split_top_level_commas(numeric_call.group("arguments"))
        if arguments is None or len(arguments) not in (1, 2):
            return None
        precision = 16
        if len(arguments) == 2:
            if re.fullmatch(r"\d{1,3}", arguments[1]) is None:
                return None
            precision = int(arguments[1])
            if not 1 <= precision <= 500:
                return None
        exact_value = _parse_exact_rational(arguments[0])
        if exact_value is not None:
            return _prepare_rational_numeric_approximation(arguments[0], precision)
        rational_sqrt = _prepare_rational_sqrt_numeric_approximation(
            arguments[0],
            precision,
        )
        if rational_sqrt is not None:
            return rational_sqrt
        affine_pi = _prepare_affine_pi_numeric_approximation(arguments[0], precision)
        if affine_pi is not None:
            return affine_pi
        exact_trig_numeric = _prepare_exact_trig_rational_numeric_approximation(
            arguments[0],
            precision,
        )
        if exact_trig_numeric is not None:
            return exact_trig_numeric
        return _prepare_general_interval_numeric_approximation(
            arguments[0],
            precision,
        )

    exact_trig_constant = _prepare_exact_pi_trigonometric_constant(code)
    if exact_trig_constant is not None:
        return exact_trig_constant

    simplified_expression = re.fullmatch(
        r"(?is)\s*Simplify\s*\[\s*(?P<expression>.*)\s*\]\s*",
        code,
    )
    if simplified_expression is not None:
        exact_trig_expression = _prepare_exact_pi_trigonometric_expression(
            simplified_expression.group("expression"),
        )
        if exact_trig_expression is not None:
            return exact_trig_expression

    trig_expand = re.fullmatch(
        r"(?is)\s*TrigExpand\s*(?:@\s*(?P<at_expression>.+?)|\[\s*(?P<bracket_expression>.*)\s*\])\s*",
        code,
    )
    if trig_expand is not None:
        expression_source = (
            trig_expand.group("at_expression")
            or trig_expand.group("bracket_expression")
            or ""
        ).strip()
        return _prepare_trig_expand(expression_source, time_limit_seconds)

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
        pi_bounded_trig_integral = _prepare_pi_bounded_sin_cos_integral(
            definite_integral.group("expression"),
            definite_integral.group("variable"),
            definite_integral.group("lower"),
            definite_integral.group("upper"),
            time_limit_seconds,
        )
        if pi_bounded_trig_integral is not None:
            return pi_bounded_trig_integral
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


def _prepare_rational_numeric_approximation(
    expression_source: str,
    precision: int,
) -> Computation | None:
    exact_value = _parse_exact_rational(expression_source)
    if exact_value is None:
        return None

    try:
        with localcontext() as context:
            context.prec = precision
            context.rounding = ROUND_HALF_EVEN
            decimal_value = Decimal(exact_value.numerator) / Decimal(exact_value.denominator)
    except (InvalidOperation, OverflowError, ValueError):
        return None

    approximation = Fraction(decimal_value)
    error_bound = _rounding_error_bound(exact_value, precision)
    display_result = format(decimal_value, "f")
    if display_result in {"-0", "-0.0"}:
        display_result = "0"

    lean_source = f"""import Mathlib.Data.Real.Basic
import Mathlib.Tactic.NormNum

theorem wolfram_language_compute :
  |({_lean_rational(exact_value)} - {_lean_rational(approximation)})| ≤ {_lean_rational(error_bound)} := by
  norm_num

#print axioms wolfram_language_compute
"""
    return Computation(
        display_result,
        lean_source,
        numeric_precision=precision,
        numeric_error_bound=_fraction_text(error_bound),
    )


def _prepare_affine_pi_numeric_approximation(
    expression_source: str,
    precision: int,
) -> Computation | None:
    normalized_source = _normalize_wolfram_real_functions(expression_source)
    exact_source = _normalize_decimal_literals(normalized_source)
    if exact_source is None:
        return None
    try:
        parsed = parse_math_input(exact_source)
        tree = ast.parse(parsed.expression, mode="eval")
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None

    affine = _rational_pi_form(tree.body)
    if affine is None:
        return None
    pi_coefficient, rational_offset = affine
    if (
        abs(pi_coefficient.numerator).bit_length() > 256
        or pi_coefficient.denominator.bit_length() > 256
        or abs(rational_offset.numerator).bit_length() > 256
        or rational_offset.denominator.bit_length() > 256
    ):
        return None
    if pi_coefficient == 0:
        return _prepare_rational_numeric_approximation(
            _fraction_text(rational_offset),
            precision,
        )

    if precision > 100:
        return None
    if precision <= 20:
        pi_bound_digits = [20]
    else:
        maximum_bound_digits = min(200, precision + 80)
        pi_bound_digits = sorted(
            {
                min(precision + guard, maximum_bound_digits)
                for guard in (8, 24, 48, 80)
            }
        )

    selected: tuple[Decimal, Fraction, Fraction, list[str]] | None = None
    for decimal_digits in pi_bound_digits:
        pi_interval = _certified_pi_interval(decimal_digits)
        if pi_interval is None:
            continue
        pi_lower, pi_upper, pi_proof = pi_interval
        if pi_coefficient > 0:
            lower = pi_coefficient * pi_lower + rational_offset
            upper = pi_coefficient * pi_upper + rational_offset
        else:
            lower = pi_coefficient * pi_upper + rational_offset
            upper = pi_coefficient * pi_lower + rational_offset

        if lower > 0 and upper > 0:
            lower_exponent = _decimal_exponent(lower)
            upper_exponent = _decimal_exponent(upper)
        elif lower < 0 and upper < 0:
            lower_exponent = _decimal_exponent(abs(lower))
            upper_exponent = _decimal_exponent(abs(upper))
        else:
            continue
        if lower_exponent != upper_exponent:
            continue

        midpoint = (lower + upper) / 2
        try:
            with localcontext() as context:
                context.prec = precision
                context.rounding = ROUND_HALF_EVEN
                decimal_value = Decimal(midpoint.numerator) / Decimal(midpoint.denominator)
        except (InvalidOperation, OverflowError, ValueError):
            return None
        approximation = Fraction(decimal_value)
        error_bound = _rounding_error_bound_at_exponent(lower_exponent, precision)
        if max(abs(lower - approximation), abs(upper - approximation)) > error_bound:
            continue
        selected = (decimal_value, approximation, error_bound, pi_proof)
        break
    if selected is None:
        return None
    decimal_value, approximation, error_bound, pi_proof = selected

    pi_expression = (
        f"({_lean_rational(pi_coefficient)} * Real.pi + "
        f"{_lean_rational(rational_offset)})"
    )
    pi_proof_body = "\n".join(f"  {line}" for line in pi_proof)
    lean_source = f"""import Mathlib.Analysis.Real.Pi.Bounds
import Mathlib.Tactic.Linarith
import Mathlib.Tactic.NormNum

theorem wolfram_language_compute :
  |{pi_expression} - {_lean_rational(approximation)}| ≤ {_lean_rational(error_bound)} := by
{pi_proof_body}
  rw [abs_le]
  constructor <;> linarith [h_pi_lower, h_pi_upper]

#print axioms wolfram_language_compute
"""
    return Computation(
        format(decimal_value, "f"),
        lean_source,
        numeric_precision=precision,
        numeric_error_bound=_fraction_text(error_bound),
    )


def _certified_pi_interval(
    decimal_digits: int,
) -> tuple[Fraction, Fraction, list[str]] | None:
    if decimal_digits <= 20:
        lower, upper = _PI_LOWER_D20, _PI_UPPER_D20
        return (
            lower,
            upper,
            [
                f"have h_pi_lower : {_lean_rational(lower)} < Real.pi := by linarith "
                "[Real.pi_gt_d20]",
                f"have h_pi_upper : Real.pi < {_lean_rational(upper)} := by linarith "
                "[Real.pi_lt_d20]",
            ],
        )

    # SymPy proposes endpoints only; Lean proves each bound from rational witnesses.
    try:
        import sympy as sp

        candidate = Fraction(Decimal(str(sp.N(sp.pi, decimal_digits + 25))))
    except (ArithmeticError, InvalidOperation, OverflowError, ValueError):
        return None

    decimal_scale = 10**decimal_digits
    scaled_floor = candidate.numerator * decimal_scale // candidate.denominator
    lower = Fraction(scaled_floor, decimal_scale)
    upper = lower + Fraction(1, decimal_scale)

    witness_digits = 3 * decimal_digits + 30
    witness_scale = 10**witness_digits
    iterations = (18 * decimal_digits + 9) // 10 + 5
    lower_witnesses: list[Fraction] = []
    upper_witnesses: list[Fraction] = []
    lower_value = Fraction(0)
    upper_value = Fraction(0)

    for _ in range(iterations):
        lower_radicand = 2 + lower_value
        lower_numerator = lower_radicand.numerator * witness_scale * witness_scale
        lower_denominator = lower_radicand.denominator
        lower_root = isqrt(lower_numerator // lower_denominator)
        if lower_root * lower_root * lower_denominator < lower_numerator:
            lower_root += 1
        lower_value = Fraction(lower_root, witness_scale)
        lower_witnesses.append(lower_value)

        upper_radicand = 2 + upper_value
        upper_numerator = upper_radicand.numerator * witness_scale * witness_scale
        upper_denominator = upper_radicand.denominator
        upper_root = isqrt(upper_numerator // upper_denominator)
        upper_value = Fraction(upper_root, witness_scale)
        upper_witnesses.append(upper_value)

    lower_condition = 2 - (lower / 2 ** (iterations + 1)) ** 2
    upper_rhs = (upper - Fraction(1, 4**iterations)) / 2 ** (iterations + 1)
    if (
        lower_value > lower_condition
        or Fraction(1, 4**iterations) > upper
        or 2 - upper_rhs**2 > upper_value
    ):
        return None

    def tactic_lines(
        name: str,
        target: str,
        tactic: str,
        values: list[Fraction],
    ) -> list[str]:
        witness_terms = [f"{value.numerator}/{value.denominator}" for value in values]
        grouped = [
            ", ".join(witness_terms[index : index + 4])
            for index in range(0, len(witness_terms), 4)
        ]
        grouped = [f"{line}," for line in grouped[:-1]] + grouped[-1:]
        return [
            f"have {name} : {target} := by",
            f"  {tactic} [",
            *(f"    {line}" for line in grouped),
            "  ]",
        ]

    proof_lines = [
        *tactic_lines(
            "h_pi_lower",
            f"{_lean_rational(lower)} < Real.pi",
            "pi_lower_bound",
            lower_witnesses,
        ),
        *tactic_lines(
            "h_pi_upper",
            f"Real.pi < {_lean_rational(upper)}",
            "pi_upper_bound",
            upper_witnesses,
        ),
    ]
    return lower, upper, proof_lines


def _prepare_rational_sqrt_numeric_approximation(
    expression_source: str,
    precision: int,
) -> Computation | None:
    normalized_source = _normalize_wolfram_real_functions(expression_source)
    exact_source = _normalize_decimal_literals(normalized_source)
    if exact_source is None:
        return None
    try:
        parsed = parse_math_input(exact_source)
        tree = ast.parse(parsed.expression, mode="eval")
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None

    sign = 1
    square_root_node = tree.body
    if isinstance(square_root_node, ast.UnaryOp):
        if isinstance(square_root_node.op, ast.USub):
            sign = -1
        elif not isinstance(square_root_node.op, ast.UAdd):
            return None
        square_root_node = square_root_node.operand
    if (
        not isinstance(square_root_node, ast.Call)
        or not isinstance(square_root_node.func, ast.Name)
        or square_root_node.func.id != "sqrt"
        or len(square_root_node.args) != 1
        or square_root_node.keywords
    ):
        return None

    radicand = _constant_fraction(square_root_node.args[0])
    if radicand is None or radicand < 0:
        return None

    try:
        with localcontext() as context:
            context.prec = precision + 8
            context.rounding = ROUND_HALF_EVEN
            decimal_radicand = Decimal(radicand.numerator) / Decimal(radicand.denominator)
            decimal_root = decimal_radicand.sqrt()
        with localcontext() as context:
            context.prec = precision
            context.rounding = ROUND_HALF_EVEN
            decimal_value = +decimal_root
    except (InvalidOperation, OverflowError, ValueError):
        return None

    if sign < 0:
        decimal_value = -decimal_value
    approximation = Fraction(decimal_value)
    if radicand == 0:
        error_bound = Fraction(0)
    else:
        root_exponent = _decimal_exponent(radicand) // 2
        error_bound = _rounding_error_bound_at_exponent(root_exponent, precision)

    root = f"Real.sqrt ({_lean_rational(radicand)})"
    signed_root = root if sign > 0 else f"(-{root})"
    positive_approximation = abs(approximation)
    lean_source = f"""import Mathlib.Analysis.SpecialFunctions.Sqrt
import Mathlib.Tactic.Linarith
import Mathlib.Tactic.NormNum

theorem wolfram_language_compute :
  |{signed_root} - {_lean_rational(approximation)}| ≤ {_lean_rational(error_bound)} := by
  have hroot_lower :
      ({_lean_rational(positive_approximation)} - {_lean_rational(error_bound)}) ≤ {root} := by
    apply Real.le_sqrt_of_sq_le
    norm_num
  have hroot_upper :
      {root} ≤ ({_lean_rational(positive_approximation)} + {_lean_rational(error_bound)}) := by
    calc
      {root} ≤ Real.sqrt
          (({_lean_rational(positive_approximation)} + {_lean_rational(error_bound)}) ^ 2) :=
        Real.sqrt_le_sqrt (by norm_num)
      _ = {_lean_rational(positive_approximation)} + {_lean_rational(error_bound)} :=
        Real.sqrt_sq (by norm_num)
  rw [abs_le]
  constructor <;> linarith [hroot_lower, hroot_upper]

#print axioms wolfram_language_compute
"""
    display_result = format(decimal_value, "f")
    if display_result in {"-0", "-0.0"}:
        display_result = "0"
    return Computation(
        display_result,
        lean_source,
        numeric_precision=precision,
        numeric_error_bound=_fraction_text(error_bound),
    )


def _prepare_exact_trig_rational_numeric_approximation(
    expression_source: str,
    precision: int,
) -> Computation | None:
    exact_computation = _prepare_exact_pi_trigonometric_expression(expression_source)
    if exact_computation is None:
        return None
    exact_value = _parse_exact_rational(exact_computation.display_result)
    equation = _exact_computation_equation_lean(exact_computation)
    if equation is None:
        return None
    if exact_value is None:
        return _prepare_exact_trig_radical_numeric_approximation(
            exact_computation,
            equation,
            precision,
        )
    exact_lhs, _exact_rhs = equation

    numeric_bound = _prepare_rational_numeric_approximation(
        _fraction_text(exact_value),
        precision,
    )
    if numeric_bound is None:
        return None

    exact_source_lines = exact_computation.lean_source.splitlines()
    numeric_source_lines = numeric_bound.lean_source.splitlines()
    imports = list(dict.fromkeys(
        line for line in exact_source_lines + numeric_source_lines
        if line.startswith("import ")
    ))
    exact_body = [
        line for line in exact_source_lines
        if not line.startswith("import ")
        and line != "#print axioms wolfram_language_compute"
    ]
    exact_body = [
        re.sub(
            r"^theorem wolfram_language_compute\s*:",
            "theorem wolfram_exact_trigonometric_expression:",
            line,
            count=1,
        )
        for line in exact_body
    ]
    numeric_body = [
        line for line in numeric_source_lines
        if not line.startswith("import ")
        and line != "#print axioms wolfram_language_compute"
    ]
    numeric_body = [
        re.sub(
            r"^theorem wolfram_language_compute :",
            "theorem wolfram_numeric_error_bound :",
            line,
            count=1,
        )
        for line in numeric_body
    ]
    theorem = f"""theorem wolfram_language_compute :
  |{exact_lhs} - {_lean_rational(Fraction(Decimal(numeric_bound.display_result)))}| ≤ {_lean_rational(Fraction(numeric_bound.numeric_error_bound))} := by
  have h_exact : {exact_lhs} = {_lean_rational(exact_value)} :=
    wolfram_exact_trigonometric_expression
  rw [h_exact]
  exact wolfram_numeric_error_bound

#print axioms wolfram_language_compute
"""
    source = "\n".join(
        [*imports, "", *exact_body, "", *numeric_body, "", theorem]
    )
    return Computation(
        numeric_bound.display_result,
        source,
        numeric_precision=precision,
        numeric_error_bound=numeric_bound.numeric_error_bound,
    )


def _prepare_exact_trig_radical_numeric_approximation(
    exact_computation: Computation,
    equation: tuple[str, str],
    precision: int,
) -> Computation | None:
    exact_source = _normalize_decimal_literals(exact_computation.display_result)
    if exact_source is None:
        return None
    try:
        parsed = parse_math_input(exact_source)
        tree = ast.parse(parsed.expression, mode="eval")
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None

    exact_lhs, exact_rhs = equation
    selected: tuple[
        Fraction, Fraction, Fraction, str, list[str], str, str, str
    ] | None = None
    for guard_digits in (12, 32, 64, 96):
        scale = 10 ** (precision + guard_digits)
        proof_lines: list[str] = []
        state = {"next_name": 0}
        interval = _build_exact_radical_interval(
            tree.body,
            scale,
            proof_lines,
            state,
        )
        if interval is None:
            return None
        lower, upper, lean_expression, hypothesis = interval
        if lower > 0:
            magnitude_lower, magnitude_upper = lower, upper
        elif upper < 0:
            magnitude_lower, magnitude_upper = -upper, -lower
        else:
            continue
        lower_exponent = _decimal_exponent(magnitude_lower)
        upper_exponent = _decimal_exponent(magnitude_upper)
        if lower_exponent != upper_exponent:
            continue

        midpoint = (lower + upper) / 2
        try:
            with localcontext() as context:
                context.prec = precision + guard_digits
                context.rounding = ROUND_HALF_EVEN
                midpoint_decimal = Decimal(midpoint.numerator) / Decimal(midpoint.denominator)
            with localcontext() as context:
                context.prec = precision
                context.rounding = ROUND_HALF_EVEN
                decimal_value = +midpoint_decimal
        except (InvalidOperation, OverflowError, ValueError):
            return None

        approximation = Fraction(decimal_value)
        error_bound = _rounding_error_bound_at_exponent(lower_exponent, precision)
        if lower < approximation - error_bound or upper > approximation + error_bound:
            continue
        display_result = format(decimal_value, "f")
        if display_result in {"-0", "-0.0"}:
            display_result = "0"
        selected = (
            lower,
            upper,
            approximation,
            _fraction_text(error_bound),
            proof_lines,
            display_result,
            lean_expression,
            hypothesis,
        )
        break

    if selected is None:
        return None

    (
        lower,
        upper,
        approximation,
        error_bound_text,
        interval_proof,
        display_result,
        lean_expression,
        hypothesis,
    ) = selected

    exact_source_lines = exact_computation.lean_source.splitlines()
    imports = list(dict.fromkeys(
        [
            line for line in exact_source_lines if line.startswith("import ")
        ]
        + [
            "import Mathlib.Analysis.Real.Pi.Bounds",
            "import Mathlib.Analysis.SpecialFunctions.Sqrt",
            "import Mathlib.Algebra.Order.Ring.Basic",
            "import Mathlib.Tactic.Linarith",
            "import Mathlib.Tactic.NormNum",
            "import Mathlib.Tactic.Ring",
        ]
    ))
    exact_body = [
        line for line in exact_source_lines
        if not line.startswith("import ")
        and line != "#print axioms wolfram_language_compute"
    ]
    exact_body = [
        re.sub(
            r"^theorem wolfram_language_compute\s*:",
            "theorem wolfram_exact_trigonometric_expression:",
            line,
            count=1,
        )
        for line in exact_body
    ]
    interval_body = "\n".join(
        f"  {nested_line}"
        for line in interval_proof
        for nested_line in line.splitlines()
    )
    theorem = f"""theorem wolfram_language_compute :
  |{exact_lhs} - {_lean_rational(approximation)}| ≤ {_lean_rational(Fraction(error_bound_text))} := by
  have h_exact : {exact_lhs} = {exact_rhs} :=
    wolfram_exact_trigonometric_expression
  rw [h_exact]
  have h_exact_rhs : {exact_rhs} = {lean_expression} := by
    ring
  rw [h_exact_rhs]
{interval_body}
  rw [abs_le]
  constructor
  · linarith [{hypothesis}.1]
  · linarith [{hypothesis}.2]

#print axioms wolfram_language_compute
"""
    source = "\n".join([*imports, "", *exact_body, "", theorem])
    return Computation(
        display_result,
        source,
        numeric_precision=precision,
        numeric_error_bound=error_bound_text,
    )


def _prepare_general_interval_numeric_approximation(
    expression_source: str,
    precision: int,
) -> Computation | None:
    normalized_source = _normalize_wolfram_real_functions(expression_source)
    exact_source = _normalize_decimal_literals(normalized_source)
    if exact_source is None:
        return None
    try:
        parsed = parse_math_input(exact_source)
        tree = ast.parse(parsed.expression, mode="eval")
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None

    contains_pi = any(
        isinstance(node, ast.Name) and node.id == "pi"
        for node in ast.walk(tree.body)
    )
    if contains_pi and precision > 100:
        return None

    selected: tuple[
        Fraction, Fraction, Fraction, str, list[str], str, str, str, str
    ] | None = None
    for guard_digits in (12, 32, 64, 96):
        scale = 10 ** (precision + guard_digits)
        proof_lines: list[str] = []
        pi_interval = None
        if contains_pi and precision > 20:
            certified_pi = _certified_pi_interval(
                min(200, precision + guard_digits + 8)
            )
            if certified_pi is None:
                continue
            pi_lower, pi_upper, pi_proof = certified_pi
            pi_interval = (pi_lower, pi_upper)
            proof_lines.extend(pi_proof)
        interval = _build_exact_radical_interval(
            tree.body,
            scale,
            proof_lines,
            {"next_name": 0},
            pi_interval=pi_interval,
        )
        if interval is None:
            return None
        lower, upper, interval_expression, hypothesis = interval
        if lower > 0:
            magnitude_lower, magnitude_upper = lower, upper
        elif upper < 0:
            magnitude_lower, magnitude_upper = -upper, -lower
        else:
            continue
        lower_exponent = _decimal_exponent(magnitude_lower)
        upper_exponent = _decimal_exponent(magnitude_upper)
        if lower_exponent != upper_exponent:
            continue

        midpoint = (lower + upper) / 2
        try:
            with localcontext() as context:
                context.prec = precision + guard_digits
                context.rounding = ROUND_HALF_EVEN
                midpoint_decimal = Decimal(midpoint.numerator) / Decimal(midpoint.denominator)
            with localcontext() as context:
                context.prec = precision
                context.rounding = ROUND_HALF_EVEN
                decimal_value = +midpoint_decimal
        except (InvalidOperation, OverflowError, ValueError):
            return None

        approximation = Fraction(decimal_value)
        error_bound = _rounding_error_bound_at_exponent(lower_exponent, precision)
        if lower < approximation - error_bound or upper > approximation + error_bound:
            continue
        display_result = format(decimal_value, "f")
        if display_result in {"-0", "-0.0"}:
            display_result = "0"
        lean_expression = _render_exact_radical_lean(tree.body)
        if lean_expression is None:
            return None
        selected = (
            lower,
            upper,
            approximation,
            _fraction_text(error_bound),
            proof_lines,
            display_result,
            lean_expression,
            interval_expression,
            hypothesis,
        )
        break

    if selected is None:
        return None

    (
        lower,
        upper,
        approximation,
        error_bound_text,
        interval_proof,
        display_result,
        lean_expression,
        interval_expression,
        hypothesis,
    ) = selected
    interval_body = "\n".join(
        f"  {nested_line}"
        for line in interval_proof
        for nested_line in line.splitlines()
    )
    lean_source = f"""import Mathlib.Analysis.Real.Pi.Bounds
import Mathlib.Analysis.SpecialFunctions.Sqrt
import Mathlib.Algebra.Order.Ring.Basic
import Mathlib.Tactic.Linarith
import Mathlib.Tactic.NormNum
import Mathlib.Tactic.Ring

theorem wolfram_language_compute :
  |{lean_expression} - {_lean_rational(approximation)}| ≤ {_lean_rational(Fraction(error_bound_text))} := by
  have h_expression : {lean_expression} = {interval_expression} := by
    ring
  rw [h_expression]
{interval_body}
  rw [abs_le]
  constructor
  · linarith [{hypothesis}.1]
  · linarith [{hypothesis}.2]

#print axioms wolfram_language_compute
"""
    return Computation(
        display_result,
        lean_source,
        numeric_precision=precision,
        numeric_error_bound=error_bound_text,
    )


def _build_exact_radical_interval(
    node: ast.AST,
    scale: int,
    proof_lines: list[str],
    state: dict[str, int],
    pi_interval: tuple[Fraction, Fraction] | None = None,
) -> tuple[Fraction, Fraction, str, str] | None:
    exact_rational = _constant_fraction(node)
    if exact_rational is not None:
        name = _next_numeric_interval_name(state)
        expression = _lean_rational(exact_rational)
        proof_lines.append(
            f"have {name} : {expression} ≤ {expression} ∧ "
            f"{expression} ≤ {expression} := by norm_num"
        )
        return exact_rational, exact_rational, expression, name

    if isinstance(node, ast.Name) and node.id == "pi":
        name = _next_numeric_interval_name(state)
        pi_lower, pi_upper = pi_interval or (_PI_LOWER_D20, _PI_UPPER_D20)
        if pi_interval is None:
            proof = (
                "by\n"
                "  constructor <;> linarith [Real.pi_gt_d20, Real.pi_lt_d20]"
            )
        else:
            proof = "by\n  constructor <;> linarith [h_pi_lower, h_pi_upper]"
        proof_lines.append(
            f"have {name} : {_lean_rational(pi_lower)} ≤ Real.pi ∧ "
            f"Real.pi ≤ {_lean_rational(pi_upper)} := {proof}"
        )
        return pi_lower, pi_upper, "Real.pi", name

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        child = _build_exact_radical_interval(
            node.operand, scale, proof_lines, state, pi_interval
        )
        if child is None:
            return None
        child_lower, child_upper, child_expression, child_name = child
        if isinstance(node.op, ast.UAdd):
            lower, upper = child_lower, child_upper
            expression = f"({child_expression})"
            body = f"exact {child_name}"
        else:
            lower, upper = -child_upper, -child_lower
            expression = f"(-({child_expression}))"
            body = (
                f"constructor <;> linarith [{child_name}.1, {child_name}.2]"
            )
        name = _next_numeric_interval_name(state)
        proof_lines.append(
            f"have {name} : {_lean_rational(lower)} ≤ {expression} ∧ "
            f"{expression} ≤ {_lean_rational(upper)} := by {body}"
        )
        return lower, upper, expression, name

    if isinstance(node, ast.Call):
        if (
            not isinstance(node.func, ast.Name)
            or node.func.id != "sqrt"
            or len(node.args) != 1
            or node.keywords
        ):
            return None
        radicand = _build_exact_radical_interval(
            node.args[0], scale, proof_lines, state, pi_interval
        )
        if radicand is None or radicand[0] < 0:
            return None
        radicand_lower, radicand_upper, radicand_expression, radicand_name = radicand
        lower, _ = _sqrt_fraction_interval(radicand_lower, scale)
        _, upper = _sqrt_fraction_interval(radicand_upper, scale)
        expression = f"Real.sqrt ({radicand_expression})"
        name = _next_numeric_interval_name(state)
        lower_name = f"{name}_lower"
        upper_name = f"{name}_upper"
        lower_comparison = (
            "norm_num"
            if radicand_lower == radicand_upper
            else f"nlinarith [{radicand_name}.1]"
        )
        upper_comparison = (
            "norm_num"
            if radicand_lower == radicand_upper
            else f"nlinarith [{radicand_name}.2]"
        )
        proof_lines.extend(
            [
                f"have {lower_name} : {_lean_rational(lower)} ≤ {expression} := by",
                "  apply Real.le_sqrt_of_sq_le",
                f"  {lower_comparison}",
                f"have {upper_name} : {expression} ≤ {_lean_rational(upper)} := by",
                "  calc",
                f"    {expression} ≤ Real.sqrt ({_lean_rational(upper)} ^ 2) := by",
                "      apply Real.sqrt_le_sqrt",
                f"      {upper_comparison}",
                f"    _ = {_lean_rational(upper)} := Real.sqrt_sq (by norm_num)",
                f"have {name} : {_lean_rational(lower)} ≤ {expression} ∧ "
                f"{expression} ≤ {_lean_rational(upper)} := by",
                f"  constructor\n  · exact {lower_name}\n  · exact {upper_name}",
            ]
        )
        return lower, upper, expression, name

    if not isinstance(node, ast.BinOp):
        return None

    if isinstance(node.op, ast.Pow):
        exponent = _constant_fraction(node.right)
        if (
            exponent is None
            or exponent.denominator != 1
            or not 0 <= exponent <= 16
        ):
            return None
        base = _build_exact_radical_interval(
            node.left, scale, proof_lines, state, pi_interval
        )
        if base is None:
            return None
        base_lower, base_upper, base_expression, base_name = base
        power = exponent.numerator
        expression = f"({base_expression} ^ {power})"
        name = _next_numeric_interval_name(state)
        if power == 0:
            lower = upper = Fraction(1)
            proof_lines.append(
                f"have {name} : {_lean_rational(lower)} ≤ {expression} ∧ "
                f"{expression} ≤ {_lean_rational(upper)} := by norm_num"
            )
            return lower, upper, expression, name
        if base_lower < 0:
            if base_upper > 0:
                return None
            positive_power_node = ast.BinOp(
                left=ast.UnaryOp(op=ast.USub(), operand=node.left),
                op=ast.Pow(),
                right=node.right,
            )
            positive_power = _build_exact_radical_interval(
                positive_power_node,
                scale,
                proof_lines,
                state,
                pi_interval,
            )
            if positive_power is None:
                return None
            positive_lower, positive_upper, positive_expression, positive_name = positive_power
            if power % 2 == 0:
                lower, upper = positive_lower, positive_upper
                equality = f"{expression} = {positive_expression}"
                result_proof = f"exact {positive_name}"
            else:
                lower, upper = -positive_upper, -positive_lower
                equality = f"{expression} = -({positive_expression})"
                result_proof = (
                    f"constructor <;> linarith [{positive_name}.1, {positive_name}.2]"
                )
            proof_lines.extend(
                [
                    f"have {name}_eq : {equality} := by ring",
                    f"have {name} : {_lean_rational(lower)} \u2264 {expression} \u2227 "
                    f"{expression} \u2264 {_lean_rational(upper)} := by",
                    f"  rw [{name}_eq]",
                    f"  {result_proof}",
                ]
            )
            return lower, upper, expression, name
        lower, upper = _quantize_rational_interval(
            base_lower**power,
            base_upper**power,
            scale,
        )
        lower_exact = f"({_lean_rational(base_lower)} ^ {power})"
        upper_exact = f"({_lean_rational(base_upper)} ^ {power})"
        lower_name = f"{name}_lower"
        upper_name = f"{name}_upper"
        proof_lines.extend(
            [
                f"have {lower_name} : {lower_exact} ≤ {expression} := by",
                f"  exact pow_le_pow_left₀ (by norm_num) {base_name}.1 {power}",
                f"have {upper_name} : {expression} ≤ {upper_exact} := by",
                f"  exact pow_le_pow_left₀ (by linarith [{base_name}.1]) {base_name}.2 {power}",
                f"have {name} : {_lean_rational(lower)} ≤ {expression} ∧ "
                f"{expression} ≤ {_lean_rational(upper)} := by",
                "  constructor",
                f"  · calc\n      {_lean_rational(lower)} ≤ {lower_exact} := by norm_num\n      _ ≤ {expression} := {lower_name}",
                f"  · calc\n      {expression} ≤ {upper_exact} := {upper_name}\n      _ ≤ {_lean_rational(upper)} := by norm_num",
            ]
        )
        return lower, upper, expression, name

    if isinstance(node.op, (ast.Mult, ast.Div)):
        if isinstance(node.op, ast.Div):
            denominator = _constant_fraction(node.right)
            if denominator is None:
                numerator_interval = _build_exact_radical_interval(
                    node.left,
                    scale,
                    proof_lines,
                    state,
                    pi_interval,
                )
                denominator_interval = _build_exact_radical_interval(
                    node.right,
                    scale,
                    proof_lines,
                    state,
                    pi_interval,
                )
                if numerator_interval is None or denominator_interval is None:
                    return None
                numerator_lower, numerator_upper, numerator_expression, numerator_name = numerator_interval
                denominator_lower, denominator_upper, denominator_expression, denominator_name = denominator_interval
                if denominator_upper < 0:
                    positive_denominator_node = ast.UnaryOp(
                        op=ast.USub(),
                        operand=node.right,
                    )
                    positive_quotient_node = ast.BinOp(
                        left=node.left,
                        op=ast.Div(),
                        right=positive_denominator_node,
                    )
                    positive_quotient = _build_exact_radical_interval(
                        positive_quotient_node,
                        scale,
                        proof_lines,
                        state,
                        pi_interval,
                    )
                    if positive_quotient is None:
                        return None
                    positive_lower, positive_upper, positive_expression, positive_name = positive_quotient
                    lower, upper = -positive_upper, -positive_lower
                    expression = (
                        f"(({numerator_expression}) / ({denominator_expression}))"
                    )
                    name = _next_numeric_interval_name(state)
                    proof_lines.extend(
                        [
                            f"have {name}_eq : {expression} = -({positive_expression}) := by",
                            f"  simpa only [neg_neg] using (div_neg_eq_neg_div "
                            f"(-({denominator_expression})) ({numerator_expression}))",
                            f"have {name} : {_lean_rational(lower)} \u2264 {expression} \u2227 "
                            f"{expression} \u2264 {_lean_rational(upper)} := by",
                            f"  rw [{name}_eq]",
                            f"  constructor <;> linarith [{positive_name}.1, {positive_name}.2]",
                        ]
                    )
                    return lower, upper, expression, name
                if denominator_lower <= 0:
                    return None
                endpoints = tuple(
                    numerator_endpoint / denominator_endpoint
                    for numerator_endpoint in (numerator_lower, numerator_upper)
                    for denominator_endpoint in (denominator_lower, denominator_upper)
                )
                lower, upper = _quantize_rational_interval(
                    min(endpoints),
                    max(endpoints),
                    scale,
                )
                expression = (
                    f"(({numerator_expression}) / ({denominator_expression}))"
                )
                name = _next_numeric_interval_name(state)
                positive_name = f"{name}_denominator_positive"
                lower_name = f"{name}_lower"
                upper_name = f"{name}_upper"
                proof_lines.extend(
                    [
                        f"have {positive_name} : 0 < ({denominator_expression}) := by",
                        f"  linarith [{denominator_name}.1]",
                        f"have {lower_name} : {_lean_rational(lower)} ≤ {expression} := by",
                        f"  rw [le_div_iff₀ {positive_name}]",
                        f"  linarith [{numerator_name}.1, {numerator_name}.2, "
                        f"{denominator_name}.1, {denominator_name}.2]",
                        f"have {upper_name} : {expression} ≤ {_lean_rational(upper)} := by",
                        f"  rw [div_le_iff₀ {positive_name}]",
                        f"  linarith [{numerator_name}.1, {numerator_name}.2, "
                        f"{denominator_name}.1, {denominator_name}.2]",
                        f"have {name} : {_lean_rational(lower)} ≤ {expression} ∧ "
                        f"{expression} ≤ {_lean_rational(upper)} := by",
                        f"  constructor\n  · exact {lower_name}\n  · exact {upper_name}",
                    ]
                )
                return lower, upper, expression, name

        coefficient = _constant_fraction(node.left)
        other_node = node.right
        if isinstance(node.op, ast.Div):
            denominator = _constant_fraction(node.right)
            if denominator is None or denominator == 0:
                return None
            coefficient = Fraction(1, 1) / denominator
            other_node = node.left
        elif coefficient is None:
            coefficient = _constant_fraction(node.right)
            other_node = node.left
        if coefficient is not None:
            child = _build_exact_radical_interval(
                other_node, scale, proof_lines, state, pi_interval
            )
            if child is None:
                return None
            child_lower, child_upper, child_expression, child_name = child
            lower, upper = _scale_rational_interval(child_lower, child_upper, coefficient, scale)
            expression = f"({_lean_rational(coefficient)} * ({child_expression}))"
            name = _next_numeric_interval_name(state)
            proof_lines.append(
                f"have {name} : {_lean_rational(lower)} ≤ {expression} ∧ "
                f"{expression} ≤ {_lean_rational(upper)} := by\n"
                f"  constructor <;> linarith [{child_name}.1, {child_name}.2]"
            )
            return lower, upper, expression, name
        if isinstance(node.op, ast.Mult):
            left = _build_exact_radical_interval(
                node.left, scale, proof_lines, state, pi_interval
            )
            right = _build_exact_radical_interval(
                node.right, scale, proof_lines, state, pi_interval
            )
            if left is None or right is None:
                return None
            return _build_sign_definite_product_interval(
                left,
                right,
                scale,
                proof_lines,
                state,
            )
        return None

    if isinstance(node.op, (ast.Add, ast.Sub)):
        left = _build_exact_radical_interval(
            node.left, scale, proof_lines, state, pi_interval
        )
        right = _build_exact_radical_interval(
            node.right, scale, proof_lines, state, pi_interval
        )
        if left is None or right is None:
            return None
        left_lower, left_upper, left_expression, left_name = left
        right_lower, right_upper, right_expression, right_name = right
        if isinstance(node.op, ast.Add):
            lower, upper = _quantize_rational_interval(
                left_lower + right_lower,
                left_upper + right_upper,
                scale,
            )
            expression = f"(({left_expression}) + ({right_expression}))"
        else:
            lower, upper = _quantize_rational_interval(
                left_lower - right_upper,
                left_upper - right_lower,
                scale,
            )
            expression = f"(({left_expression}) - ({right_expression}))"
        name = _next_numeric_interval_name(state)
        proof_lines.append(
            f"have {name} : {_lean_rational(lower)} ≤ {expression} ∧ "
            f"{expression} ≤ {_lean_rational(upper)} := by\n"
            f"  constructor <;> linarith "
            f"[{left_name}.1, {left_name}.2, {right_name}.1, {right_name}.2]"
        )
        return lower, upper, expression, name

    return None


def _build_nonnegative_product_interval(
    left: tuple[Fraction, Fraction, str, str],
    right: tuple[Fraction, Fraction, str, str],
    scale: int,
    proof_lines: list[str],
    state: dict[str, int],
) -> tuple[Fraction, Fraction, str, str] | None:
    left_lower, left_upper, left_expression, left_name = left
    right_lower, right_upper, right_expression, right_name = right
    if left_lower < 0 or right_lower < 0:
        return None
    lower, upper = _quantize_rational_interval(
        left_lower * right_lower,
        left_upper * right_upper,
        scale,
    )
    expression = f"(({left_expression}) * ({right_expression}))"
    lower_product = (
        f"({_lean_rational(left_lower)} * {_lean_rational(right_lower)})"
    )
    upper_product = (
        f"({_lean_rational(left_upper)} * {_lean_rational(right_upper)})"
    )
    name = _next_numeric_interval_name(state)
    proof_lines.extend(
        [
            f"have {name} : {_lean_rational(lower)} ≤ {expression} ∧ "
            f"{expression} ≤ {_lean_rational(upper)} := by",
            "  constructor",
            "  · calc",
            f"      {_lean_rational(lower)} ≤ {lower_product} := by norm_num",
            f"      _ ≤ ({left_expression}) * {_lean_rational(right_lower)} := "
            f"mul_le_mul_of_nonneg_right {left_name}.1 (by norm_num)",
            f"      _ ≤ {expression} := mul_le_mul_of_nonneg_left "
            f"{right_name}.1 (by linarith [{left_name}.1])",
            "  · calc",
            f"      {expression} ≤ {_lean_rational(left_upper)} * ({right_expression}) := "
            f"mul_le_mul_of_nonneg_right {left_name}.2 (by linarith [{right_name}.1])",
            f"      _ ≤ {upper_product} := mul_le_mul_of_nonneg_left "
            f"{right_name}.2 (by norm_num)",
            f"      _ ≤ {_lean_rational(upper)} := by norm_num",
        ]
    )
    return lower, upper, expression, name


def _negate_certified_interval(
    interval: tuple[Fraction, Fraction, str, str],
    proof_lines: list[str],
    state: dict[str, int],
) -> tuple[Fraction, Fraction, str, str]:
    lower, upper, expression, interval_name = interval
    negated_expression = f"(-({expression}))"
    negated_name = _next_numeric_interval_name(state)
    proof_lines.append(
        f"have {negated_name} : {_lean_rational(-upper)} \u2264 {negated_expression} \u2227 "
        f"{negated_expression} \u2264 {_lean_rational(-lower)} := by "
        f"constructor <;> linarith [{interval_name}.1, {interval_name}.2]"
    )
    return -upper, -lower, negated_expression, negated_name


def _build_sign_definite_product_interval(
    left: tuple[Fraction, Fraction, str, str],
    right: tuple[Fraction, Fraction, str, str],
    scale: int,
    proof_lines: list[str],
    state: dict[str, int],
) -> tuple[Fraction, Fraction, str, str] | None:
    left_lower, left_upper, left_expression, _ = left
    right_lower, right_upper, right_expression, _ = right
    expression = f"(({left_expression}) * ({right_expression}))"

    if left_lower >= 0 and right_lower >= 0:
        return _build_nonnegative_product_interval(
            left, right, scale, proof_lines, state
        )

    if left_upper <= 0 and right_upper <= 0:
        positive_left = _negate_certified_interval(left, proof_lines, state)
        positive_right = _negate_certified_interval(right, proof_lines, state)
        positive_product = _build_nonnegative_product_interval(
            positive_left, positive_right, scale, proof_lines, state
        )
        if positive_product is None:
            return None
        lower, upper, positive_expression, positive_name = positive_product
        name = _next_numeric_interval_name(state)
        proof_lines.extend(
            [
                f"have {name}_eq : {expression} = {positive_expression} := by ring",
                f"have {name} : {_lean_rational(lower)} \u2264 {expression} \u2227 "
                f"{expression} \u2264 {_lean_rational(upper)} := by",
                f"  rw [{name}_eq]",
                f"  exact {positive_name}",
            ]
        )
        return lower, upper, expression, name

    if left_upper <= 0 and right_lower >= 0:
        positive_left = _negate_certified_interval(left, proof_lines, state)
        positive_product = _build_nonnegative_product_interval(
            positive_left, right, scale, proof_lines, state
        )
        if positive_product is None:
            return None
        positive_lower, positive_upper, positive_expression, positive_name = positive_product
        lower, upper = -positive_upper, -positive_lower
        name = _next_numeric_interval_name(state)
        proof_lines.extend(
            [
                f"have {name}_eq : {expression} = -({positive_expression}) := by ring",
                f"have {name} : {_lean_rational(lower)} \u2264 {expression} \u2227 "
                f"{expression} \u2264 {_lean_rational(upper)} := by",
                f"  rw [{name}_eq]",
                f"  constructor <;> linarith [{positive_name}.1, {positive_name}.2]",
            ]
        )
        return lower, upper, expression, name

    if left_lower >= 0 and right_upper <= 0:
        positive_right = _negate_certified_interval(right, proof_lines, state)
        positive_product = _build_nonnegative_product_interval(
            left, positive_right, scale, proof_lines, state
        )
        if positive_product is None:
            return None
        positive_lower, positive_upper, positive_expression, positive_name = positive_product
        lower, upper = -positive_upper, -positive_lower
        name = _next_numeric_interval_name(state)
        proof_lines.extend(
            [
                f"have {name}_eq : {expression} = -({positive_expression}) := by ring",
                f"have {name} : {_lean_rational(lower)} \u2264 {expression} \u2227 "
                f"{expression} \u2264 {_lean_rational(upper)} := by",
                f"  rw [{name}_eq]",
                f"  constructor <;> linarith [{positive_name}.1, {positive_name}.2]",
            ]
        )
        return lower, upper, expression, name

    return None


def _render_exact_radical_lean(node: ast.AST) -> str | None:
    rational = _constant_fraction(node)
    if rational is not None:
        return _lean_rational(rational)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        child = _render_exact_radical_lean(node.operand)
        if child is None:
            return None
        return child if isinstance(node.op, ast.UAdd) else f"(-({child}))"
    if isinstance(node, ast.Call):
        if (
            not isinstance(node.func, ast.Name)
            or node.func.id != "sqrt"
            or len(node.args) != 1
            or node.keywords
        ):
            return None
        argument = _render_exact_radical_lean(node.args[0])
        return f"Real.sqrt ({argument})" if argument is not None else None
    if isinstance(node, ast.Name) and node.id == "pi":
        return "Real.pi"
    if not isinstance(node, ast.BinOp):
        return None
    if isinstance(node.op, (ast.Add, ast.Sub)):
        left = _render_exact_radical_lean(node.left)
        right = _render_exact_radical_lean(node.right)
        if left is None or right is None:
            return None
        operator = "+" if isinstance(node.op, ast.Add) else "-"
        return f"(({left}) {operator} ({right}))"
    if isinstance(node.op, ast.Mult):
        left_rational = _constant_fraction(node.left)
        right_rational = _constant_fraction(node.right)
        if left_rational is not None:
            other = _render_exact_radical_lean(node.right)
            return (
                f"({_lean_rational(left_rational)} * ({other}))"
                if other is not None
                else None
            )
        if right_rational is not None:
            other = _render_exact_radical_lean(node.left)
            return (
                f"({_lean_rational(right_rational)} * ({other}))"
                if other is not None
                else None
            )
        left = _render_exact_radical_lean(node.left)
        right = _render_exact_radical_lean(node.right)
        if left is None or right is None:
            return None
        return f"(({left}) * ({right}))"
    if isinstance(node.op, ast.Div):
        denominator = _constant_fraction(node.right)
        numerator = _render_exact_radical_lean(node.left)
        if numerator is None:
            return None
        if denominator is not None:
            if denominator == 0:
                return None
            return f"({_lean_rational(Fraction(1, 1) / denominator)} * ({numerator}))"
        denominator_expression = _render_exact_radical_lean(node.right)
        if denominator_expression is None:
            return None
        return (
            f"(({numerator}) / "
            f"({denominator_expression}))"
        )
    if isinstance(node.op, ast.Pow):
        exponent = _constant_fraction(node.right)
        if (
            exponent is None
            or exponent.denominator != 1
            or not 0 <= exponent <= 16
        ):
            return None
        base = _render_exact_radical_lean(node.left)
        return f"({base} ^ {exponent.numerator})" if base is not None else None
    return None


def _sqrt_fraction_interval(value: Fraction, scale: int) -> tuple[Fraction, Fraction]:
    if value < 0:
        raise ValueError("square root interval requires a nonnegative rational")
    scaled_square = value.numerator * scale * scale
    floor_root = isqrt(scaled_square // value.denominator)
    lower = Fraction(floor_root, scale)
    if floor_root * floor_root * value.denominator == scaled_square:
        return lower, lower
    return lower, Fraction(floor_root + 1, scale)


def _scale_rational_interval(
    lower: Fraction,
    upper: Fraction,
    coefficient: Fraction,
    scale: int,
) -> tuple[Fraction, Fraction]:
    if coefficient >= 0:
        return _quantize_rational_interval(lower * coefficient, upper * coefficient, scale)
    return _quantize_rational_interval(upper * coefficient, lower * coefficient, scale)


def _quantize_rational_interval(
    lower: Fraction,
    upper: Fraction,
    scale: int,
) -> tuple[Fraction, Fraction]:
    scaled_lower = lower * scale
    scaled_upper = upper * scale
    lower_integer = scaled_lower.numerator // scaled_lower.denominator
    upper_integer = -((-scaled_upper.numerator) // scaled_upper.denominator)
    return Fraction(lower_integer, scale), Fraction(upper_integer, scale)


def _next_numeric_interval_name(state: dict[str, int]) -> str:
    index = state["next_name"]
    state["next_name"] = index + 1
    return f"h_numeric_interval_{index}"


def _rounding_error_bound(value: Fraction, precision: int) -> Fraction:
    if value == 0:
        return Fraction(0)

    return _rounding_error_bound_at_exponent(_decimal_exponent(value), precision)


def _decimal_exponent(value: Fraction) -> int:
    numerator = abs(value.numerator)
    denominator = value.denominator
    exponent = len(str(numerator)) - len(str(denominator))
    if exponent >= 0:
        if numerator < denominator * (10 ** exponent):
            exponent -= 1
    elif numerator * (10 ** -exponent) < denominator:
        exponent -= 1
    return exponent


def _rounding_error_bound_at_exponent(exponent: int, precision: int) -> Fraction:
    decimal_place = exponent - precision + 1
    if decimal_place >= 0:
        return Fraction(10 ** decimal_place, 2)
    return Fraction(1, 2 * (10 ** -decimal_place))


def _prepare_exact_pi_trigonometric_constant(code: str) -> Computation | None:
    match = re.fullmatch(
        r"(?is)\s*(?P<function>Sin|Cos|Tan)\s*\[\s*"
        r"(?P<sign>[+-]?)\s*(?:(?P<numerator>\d+)\s*\*?\s*)?Pi"
        r"(?:\s*/\s*(?P<denominator>\d+))?\s*\]\s*",
        code,
    )
    if match is None:
        return None

    function = match.group("function").lower()
    dynamic_supporting_lemmas: dict[str, str] = {}

    def exact_trig_value(
        requested_function: str,
        requested_denominator: int,
    ) -> tuple[str, str, str] | None:
        value = _EXACT_PI_TRIG_VALUES.get(requested_function, {}).get(
            requested_denominator
        )
        if value is not None:
            return value
        if requested_function == "tan":
            tangent_value = _exact_tangent_from_sine_cosine(requested_denominator)
            if tangent_value is not None:
                value, source = tangent_value
                dynamic_supporting_lemmas[value[2]] = source
                return value
        dyadic_value = _exact_dyadic_pi_trig_value(
            requested_function,
            requested_denominator,
        )
        if dyadic_value is not None:
            value, source = dyadic_value
            dynamic_supporting_lemmas[value[2]] = source
            return value
        halved_value = _exact_iterated_half_pi_trig_value(
            requested_function,
            requested_denominator,
        )
        if halved_value is None:
            return None
        value, source = halved_value
        dynamic_supporting_lemmas[value[2]] = source
        return value

    try:
        numerator = int(match.group("numerator") or "1")
        denominator = int(match.group("denominator") or "1")
    except ValueError:
        return None
    if (
        denominator <= 0
        or abs(numerator).bit_length() > 256
        or denominator.bit_length() > 256
    ):
        return None
    if match.group("sign") == "-":
        numerator = -numerator
    pi_multiple = Fraction(numerator, denominator)

    integer_shift = (
        2 * pi_multiple.numerator + pi_multiple.denominator
    ) // (2 * pi_multiple.denominator)
    angle_remainder = pi_multiple - integer_shift
    subtract_base = angle_remainder < 0
    base_angle = abs(angle_remainder)
    if base_angle == 0:
        value = {
            "sin": ("0", "0", "Real.sin_zero"),
            "cos": ("1", "1", "Real.cos_zero"),
            "tan": ("0", "0", "Real.tan_zero"),
        }[function]
        base_expression = "0"
        base_lemmas = [value[2]]
    else:
        reciprocal = 1 / base_angle
        if reciprocal.denominator == 1:
            base_denominator = reciprocal.numerator
            value = exact_trig_value(function, base_denominator)
            if value is None:
                return None
            base_expression = f"(Real.pi / {base_denominator})"
            base_lemmas = [value[2]]
        elif function in {"sin", "cos"}:
            complement = Fraction(1, 2) - base_angle
            complement_reciprocal = 1 / complement if complement > 0 else None
            if (
                complement_reciprocal is None
                or complement_reciprocal.denominator != 1
            ):
                return None
            base_denominator = complement_reciprocal.numerator
            complementary_function = "cos" if function == "sin" else "sin"
            value = exact_trig_value(complementary_function, base_denominator)
            if value is None:
                return None
            base_expression = (
                f"(Real.pi / 2 - Real.pi / {base_denominator})"
            )
            cofunction_lemma = (
                "Real.sin_pi_div_two_sub"
                if function == "sin"
                else "Real.cos_pi_div_two_sub"
            )
            base_lemmas = [cofunction_lemma, value[2]]
        elif function == "tan":
            complement = Fraction(1, 2) - base_angle
            complement_reciprocal = 1 / complement if complement > 0 else None
            if (
                complement_reciprocal is None
                or complement_reciprocal.denominator != 1
            ):
                return None
            base_denominator = complement_reciprocal.numerator
            value = exact_trig_value("tan", base_denominator)
            if value is None:
                return None
            complementary_lemma = f"wolfram_tan_pi_div_two_sub_{base_denominator}"
            base_tangent_support = dynamic_supporting_lemmas.get(
                value[2],
                _EXACT_PI_TRIG_SUPPORTING_LEMMAS.get(value[2], ""),
            )
            complementary_source = "\n\n".join(
                part for part in (
                    base_tangent_support,
                    f"""theorem {complementary_lemma} :
    Real.tan (Real.pi / 2 - Real.pi / {base_denominator}) =
      ({value[1]})⁻¹ := by
  rw [Real.tan_pi_div_two_sub, {value[2]}]
""",
                ) if part
            )
            dynamic_supporting_lemmas[complementary_lemma] = complementary_source
            value = (
                f"1/({value[0]})",
                f"({value[1]})⁻¹",
                complementary_lemma,
            )
            base_expression = f"(Real.pi / 2 - Real.pi / {base_denominator})"
            base_lemmas = [complementary_lemma]
        else:
            return None

    display_result, lean_result, _base_lemma = value
    parity_sign = -1 if integer_shift % 2 else 1
    if function == "sin":
        result_sign = -parity_sign if subtract_base else parity_sign
    elif function == "cos":
        result_sign = parity_sign
    else:
        result_sign = -1 if subtract_base else 1
    if result_sign < 0 and display_result != "0":
        display_result = f"-{display_result}"
        lean_result = f"-({lean_result})"

    integer_term = f"({integer_shift} : ℤ) * Real.pi"
    angle_decomposition = (
        f"{integer_term} - {base_expression}"
        if subtract_base
        else f"{base_expression} + {integer_term}"
    )
    identity = (
        f"Real.{function}_int_mul_pi_sub"
        if subtract_base
        else f"Real.{function}_add_int_mul_pi"
    )
    lean_multiple = _lean_rational(pi_multiple)
    proof = f"""by
  have hangle : {lean_multiple} * Real.pi = {angle_decomposition} := by
    norm_num
    all_goals ring
  rw [hangle, {identity}]
  rw [{", ".join(base_lemmas)}] <;> norm_num
"""
    supporting_lemmas = "\n".join(
        dynamic_supporting_lemmas.get(
            lemma,
            _EXACT_PI_TRIG_SUPPORTING_LEMMAS.get(lemma, ""),
        )
        for lemma in base_lemmas
        if (
            lemma in dynamic_supporting_lemmas
            or lemma in _EXACT_PI_TRIG_SUPPORTING_LEMMAS
        )
    )
    source = f"""import Mathlib.Analysis.SpecialFunctions.Trigonometric.Basic

{supporting_lemmas}
theorem wolfram_language_compute :
    Real.{function} ({lean_multiple} * Real.pi) = {lean_result} := {proof}

#print axioms wolfram_language_compute
"""
    return Computation(display_result, source, proof_result_authoritative=True)


def _exact_tangent_from_sine_cosine(
    denominator: int,
) -> tuple[tuple[str, str, str], str] | None:
    # For pi/d with d > 2, cos(pi/d) is nonzero. The exact Sin and Cos
    # backends supply proof terms for both components of the quotient.
    if denominator <= 2:
        return None
    sine = _prepare_exact_pi_trigonometric_constant(f"Sin[Pi/{denominator}]")
    cosine = _prepare_exact_pi_trigonometric_constant(f"Cos[Pi/{denominator}]")
    if sine is None or cosine is None or cosine.display_result == "0":
        return None

    sine_result = _exact_computation_result_lean(sine)
    cosine_result = _exact_computation_result_lean(cosine)
    if sine_result is None or cosine_result is None:
        return None

    sine_prefix = f"exact_tan_sine_{denominator}"
    cosine_prefix = f"exact_tan_cosine_{denominator}"
    sine_source, sine_names = _rename_exact_trig_declarations(
        sine.lean_source,
        sine_prefix,
    )
    cosine_source, cosine_names = _rename_exact_trig_declarations(
        cosine.lean_source,
        cosine_prefix,
    )
    if not sine_names or not cosine_names:
        return None

    sine_theorem = f"{sine_prefix}_wolfram_language_compute"
    cosine_theorem = f"{cosine_prefix}_wolfram_language_compute"
    tangent_theorem = f"wolfram_tan_pi_div_{denominator}"
    theorem_source = "\n".join([
        sine_source,
        "",
        cosine_source,
        "",
        f"theorem {tangent_theorem} :",
        f"    Real.tan (Real.pi / {denominator}) = ({sine_result}) / ({cosine_result}) := by",
        f"  have hangle : ((1 / {denominator} : \u211d) * Real.pi) = Real.pi / {denominator} := by ring",
        f"  have hsin : Real.sin (Real.pi / {denominator}) = {sine_result} := by",
        "    rw [\u2190hangle]",
        f"    exact {sine_theorem}",
        f"  have hcos : Real.cos (Real.pi / {denominator}) = {cosine_result} := by",
        "    rw [\u2190hangle]",
        f"    exact {cosine_theorem}",
        "  rw [Real.tan_eq_sin_div_cos, hsin, hcos]",
    ])
    return (
        (
            f"({sine.display_result})/({cosine.display_result})",
            f"({sine_result}) / ({cosine_result})",
            tangent_theorem,
        ),
        theorem_source,
    )


def _exact_computation_result_lean(computation: Computation) -> str | None:
    equation = _exact_computation_equation_lean(computation)
    return equation[1] if equation is not None else None


def _exact_computation_equation_lean(
    computation: Computation,
) -> tuple[str, str] | None:
    match = re.search(
        r"(?s)theorem\s+wolfram_language_compute\s*:\s*(.*?)\s*:=\s*by\b",
        computation.lean_source,
    )
    if match is None:
        return None
    statement = match.group(1)
    equality_index = statement.rfind(" = ")
    if equality_index < 0:
        return None
    return (
        statement[:equality_index].strip(),
        statement[equality_index + 3:].strip(),
    )


def _prepare_exact_pi_trigonometric_expression(
    expression_source: str,
) -> Computation | None:
    normalized_source = _normalize_wolfram_real_functions(expression_source)
    exact_source = _normalize_decimal_literals(normalized_source)
    if exact_source is None:
        return None
    try:
        parsed = parse_math_input(exact_source)
        expression_tree = ast.parse(parsed.expression, mode="eval")
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None

    declarations: list[tuple[str, str]] = []
    rendered = _render_exact_pi_trig_expression_node(
        expression_tree.body,
        declarations,
        [0],
        0,
    )
    if rendered is None:
        return None
    expression_lean, display_result, result_lean, contains_trig = rendered
    if not contains_trig or not declarations:
        return None

    try:
        display_tree = ast.parse(display_result.replace("^", "**"), mode="eval")
    except (SyntaxError, RecursionError, ValueError):
        return None
    rational_result = _constant_fraction(display_tree.body)
    if rational_result is not None:
        display_result = _fraction_text(rational_result)
        result_lean = _lean_rational(rational_result)

    declaration_names = [name for name, _ in declarations]
    declaration_sources = "\n\n".join(source for _, source in declarations)
    source_lines = [
        "import Mathlib.Analysis.SpecialFunctions.Trigonometric.Basic",
        "import Mathlib.Tactic.Ring",
        "",
        declaration_sources,
        "",
        "theorem wolfram_language_compute :",
        f"    {expression_lean} = {result_lean} := by",
        f"  simp only [{', '.join(declaration_names)}] <;> ring",
        "",
        "#print axioms wolfram_language_compute",
    ]
    return Computation(
        display_result,
        "\n".join(source_lines),
        proof_result_authoritative=True,
    )


def _render_exact_pi_trig_expression_node(
    node: ast.AST,
    declarations: list[tuple[str, str]],
    node_count: list[int],
    depth: int,
) -> tuple[str, str, str, bool] | None:
    node_count[0] += 1
    if node_count[0] > 256 or depth > 32:
        return None

    rational = _constant_fraction(node)
    if rational is not None:
        lean_value = _lean_rational(rational)
        return lean_value, _fraction_text(rational), lean_value, False

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        inner = _render_exact_pi_trig_expression_node(
            node.operand, declarations, node_count, depth + 1
        )
        if inner is None:
            return None
        expression, display, result, contains_trig = inner
        if isinstance(node.op, ast.UAdd):
            return expression, display, result, contains_trig
        return f"(-{expression})", f"-({display})", f"(-{result})", contains_trig

    if isinstance(node, ast.BinOp):
        left = _render_exact_pi_trig_expression_node(
            node.left, declarations, node_count, depth + 1
        )
        right = _render_exact_pi_trig_expression_node(
            node.right, declarations, node_count, depth + 1
        )
        if left is None or right is None:
            return None
        left_expression, left_display, left_result, left_has_trig = left
        right_expression, right_display, right_result, right_has_trig = right
        if not left_has_trig and not right_has_trig:
            return None

        operator = {
            ast.Add: "+",
            ast.Sub: "-",
            ast.Mult: "*",
            ast.Div: "/",
            ast.Pow: "^",
        }.get(type(node.op))
        if operator is None:
            return None
        if isinstance(node.op, ast.Div):
            denominator = _constant_fraction(node.right)
            if denominator is None or denominator == 0:
                return None
        if isinstance(node.op, ast.Pow):
            exponent = _constant_fraction(node.right)
            if (
                exponent is None
                or exponent.denominator != 1
                or not 0 <= exponent <= 16
            ):
                return None
            right_expression = str(exponent.numerator)
            right_display = right_expression
            right_result = right_expression

        return (
            f"({left_expression} {operator} {right_expression})",
            f"({left_display} {operator} {right_display})",
            f"({left_result} {operator} {right_result})",
            left_has_trig or right_has_trig,
        )

    if (
        not isinstance(node, ast.Call)
        or not isinstance(node.func, ast.Name)
        or node.func.id not in {"sin", "cos", "tan"}
        or len(node.args) != 1
        or node.keywords
    ):
        return None
    pi_multiple = _exact_pi_multiple_from_ast(node.args[0])
    if (
        pi_multiple is None
        or abs(pi_multiple.numerator).bit_length() > 256
        or pi_multiple.denominator.bit_length() > 256
    ):
        return None

    sign = "-" if pi_multiple < 0 else ""
    numerator = abs(pi_multiple.numerator)
    numerator_text = "" if numerator == 1 else f"{numerator}*"
    angle = f"{sign}{numerator_text}Pi"
    if pi_multiple.denominator != 1:
        angle += f"/{pi_multiple.denominator}"
    computation = _prepare_exact_pi_trigonometric_constant(
        f"{node.func.id.title()}[{angle}]"
    )
    if computation is None:
        return None

    result_lean = _exact_computation_result_lean(computation)
    if result_lean is None:
        return None

    index = len(declarations)
    if index >= 16:
        return None
    prefix = f"exact_trig_{index}"
    renamed_source, local_names = _rename_exact_trig_declarations(
        computation.lean_source,
        prefix,
    )
    if not local_names:
        return None
    declarations.append((f"{prefix}_wolfram_language_compute", renamed_source))
    argument_lean = _lean_rational(pi_multiple)
    return (
        f"(Real.{node.func.id} ({argument_lean} * Real.pi))",
        computation.display_result,
        result_lean,
        True,
    )


def _exact_pi_multiple_from_ast(node: ast.AST) -> Fraction | None:
    if isinstance(node, ast.Name) and node.id == "pi":
        return Fraction(1)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _exact_pi_multiple_from_ast(node.operand)
        if value is None:
            return None
        return value if isinstance(node.op, ast.UAdd) else -value
    if not isinstance(node, ast.BinOp):
        return None
    if isinstance(node.op, (ast.Add, ast.Sub)):
        left = _exact_pi_multiple_from_ast(node.left)
        right = _exact_pi_multiple_from_ast(node.right)
        if left is None or right is None:
            return None
        return left + right if isinstance(node.op, ast.Add) else left - right
    if isinstance(node.op, ast.Mult):
        left_pi = _exact_pi_multiple_from_ast(node.left)
        right_pi = _exact_pi_multiple_from_ast(node.right)
        left_rational = _constant_fraction(node.left)
        right_rational = _constant_fraction(node.right)
        if left_pi is not None and right_rational is not None:
            return left_pi * right_rational
        if right_pi is not None and left_rational is not None:
            return right_pi * left_rational
    if isinstance(node.op, ast.Div):
        numerator = _exact_pi_multiple_from_ast(node.left)
        denominator = _constant_fraction(node.right)
        if numerator is not None and denominator not in {None, 0}:
            return numerator / denominator
    return None


def _rename_exact_trig_declarations(
    source: str,
    prefix: str,
) -> tuple[str, list[str]]:
    body = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("import ")
        and not line.lstrip().startswith("#print axioms ")
    ).strip()
    names = re.findall(
        r"(?m)^\s*(?:private\s+)?(?:theorem|lemma)\s+([A-Za-z_][A-Za-z0-9_']*)\b",
        body,
    )
    for name in names:
        body = re.sub(rf"\b{re.escape(name)}\b", f"{prefix}_{name}", body)
    return body, names


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


def _prepare_trig_expand(
    expression_source: str,
    time_limit_seconds: float,
) -> Computation | None:
    normalized_source = _normalize_wolfram_real_functions(expression_source)
    exact_source = _normalize_decimal_literals(normalized_source)
    if exact_source is None:
        return None
    try:
        parsed = parse_math_input(exact_source)
        expression_tree = ast.parse(parsed.expression, mode="eval")
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None
    calls = [node for node in ast.walk(expression_tree.body) if isinstance(node, ast.Call)]
    if not calls or any(
        not isinstance(node.func, ast.Name)
        or node.func.id not in {"sin", "cos"}
        or len(node.args) != 1
        or bool(node.keywords)
        or not _trigexpand_argument_supported(node.args[0])
        for node in calls
    ):
        return None
    if not any(
        _trigexpand_argument_has_sum(node.args[0])
        or _trigexpand_argument_has_scaled_term(node.args[0])
        for node in calls
    ):
        return None
    scaled_terms = _trigexpand_scaled_terms(expression_tree.body)

    symbolic = run_symbolic(SymbolicRequest(
        operation="trigexpand",
        expression=parsed.expression,
        time_limit_seconds=max(0.001, min(time_limit_seconds, 60.0)),
    ))
    if symbolic.get("timed_out"):
        return Computation("", "", timed_out=True)
    if symbolic.get("error") or not isinstance(symbolic.get("result"), str):
        diagnostic = symbolic.get("diagnostics") or symbolic.get("error") or (
            "Symbolic worker returned no trigonometric expansion"
        )
        return Computation("", "", backend_error=str(diagnostic)[:1000])
    result = symbolic["result"]
    if len(result) > 4000:
        return None
    try:
        result_tree = ast.parse(result, mode="eval")
    except (ValueError, SyntaxError, RecursionError):
        return None
    if sum(1 for _ in ast.walk(result_tree)) > 256:
        return None
    expanded_calls = [node for node in ast.walk(result_tree.body) if isinstance(node, ast.Call)]
    if any(
        not isinstance(node.func, ast.Name)
        or node.func.id not in {"sin", "cos"}
        or len(node.args) != 1
        or bool(node.keywords)
        for node in expanded_calls
    ):
        return None
    original_lean, original_symbols = _render_real_calculus_lean(parsed.expression)
    expanded_lean, expanded_symbols = _render_real_calculus_lean(result)
    if original_lean is None or expanded_lean is None:
        return None
    pythagorean_quotients = _trigexpand_pythagorean_quotients(
        expression_tree.body,
        result_tree.body,
        sorted(original_symbols | expanded_symbols),
    )
    if pythagorean_quotients is None:
        return None
    binders = " ".join(
        f"({name} : ℝ)" for name in sorted(original_symbols | expanded_symbols)
    )
    theorem_binders = f" {binders}" if binders else ""
    rewrite_names: list[str] = []
    rewrite_declarations: list[str] = []
    used_names = set(original_symbols | expanded_symbols)
    max_by_variable: dict[str, int] = {}
    right_multiples_by_variable: dict[str, set[int]] = {}
    for multiple, variable_name, coefficient_on_right in scaled_terms:
        max_by_variable[variable_name] = max(
            multiple,
            max_by_variable.get(variable_name, 1),
        )
        if coefficient_on_right:
            right_multiples_by_variable.setdefault(variable_name, set()).add(multiple)
    for variable_index, variable_name in enumerate(sorted(original_symbols)):
        for multiple in range(2, max_by_variable.get(variable_name, 1) + 1):
            left_name = f"h_trig_left_{variable_index}_{multiple}"
            while left_name in used_names:
                left_name = f"_{left_name}"
            used_names.add(left_name)
            left_angle = f"({multiple} : ℝ) * {variable_name}"
            if multiple == 2:
                split_angle = f"{variable_name} + {variable_name}"
            else:
                split_angle = f"({multiple - 1} : ℝ) * {variable_name} + {variable_name}"
            rewrite_declarations.append(
                f"  have {left_name} : {left_angle} = {split_angle} := by ring"
            )
            rewrite_names.append(left_name)
            if multiple in right_multiples_by_variable.get(variable_name, set()):
                right_name = f"h_trig_right_{variable_index}_{multiple}"
                while right_name in used_names:
                    right_name = f"_{right_name}"
                used_names.add(right_name)
                rewrite_declarations.append(
                    f"  have {right_name} : {variable_name} * ({multiple} : ℝ) = "
                    f"{split_angle} := by rw [mul_comm, {left_name}]"
                )
                rewrite_names.append(right_name)
    rewrite_rules = [
        *rewrite_names,
        "Real.sin_add", "Real.sin_sub", "Real.cos_add", "Real.cos_sub",
        "Real.sin_neg", "Real.cos_neg",
    ]
    norm_declarations: list[str] = []
    quotient_terms: list[str] = []
    for variable_index, (variable_name, quotient) in enumerate(pythagorean_quotients):
        norm_name = f"h_trig_norm_{variable_index}"
        while norm_name in used_names:
            norm_name = f"_{norm_name}"
        used_names.add(norm_name)
        norm_declarations.append(
            f"  have {norm_name} : Real.sin {variable_name} ^ 2 + "
            f"Real.cos {variable_name} ^ 2 = 1 := Real.sin_sq_add_cos_sq {variable_name}"
        )
        quotient_terms.append(f"({quotient}) * {norm_name}")
    rewrite_declarations.extend(norm_declarations)
    rewrite_block = "\n".join(rewrite_declarations)
    rewrite_prelude = f"{rewrite_block}\n" if rewrite_block else ""
    equality_tactic = (
        f"linear_combination (norm := ring_nf) {' + '.join(quotient_terms)}"
        if quotient_terms
        else "ring"
    )
    source = f"""import Mathlib.Analysis.SpecialFunctions.Trigonometric.Basic
import Mathlib.Tactic.Ring

theorem wolfram_language_compute{theorem_binders} :
    {original_lean} = {expanded_lean} := by
{rewrite_prelude}  simp only [{", ".join(rewrite_rules)}]
  {equality_tactic}

#print axioms wolfram_language_compute
"""
    return Computation(_real_ast_to_wolfram(result_tree.body), source)


def _trigexpand_argument_supported(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id != "pi" and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", node.id) is not None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _trigexpand_argument_supported(node.operand)
    if _trigexpand_scaled_term(node) is not None:
        return True
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, (ast.Add, ast.Sub))
        and _trigexpand_argument_supported(node.left)
        and _trigexpand_argument_supported(node.right)
    )


def _trigexpand_argument_has_sum(node: ast.AST) -> bool:
    return isinstance(node, ast.BinOp) and (
        isinstance(node.op, (ast.Add, ast.Sub))
        or _trigexpand_argument_has_sum(node.left)
        or _trigexpand_argument_has_sum(node.right)
    )


def _trigexpand_argument_has_scaled_term(node: ast.AST) -> bool:
    if _trigexpand_scaled_term(node) is not None:
        return True
    if isinstance(node, ast.BinOp):
        return _trigexpand_argument_has_scaled_term(node.left) or _trigexpand_argument_has_scaled_term(node.right)
    if isinstance(node, ast.UnaryOp):
        return _trigexpand_argument_has_scaled_term(node.operand)
    return False


def _trigexpand_scaled_term(node: ast.AST) -> tuple[int, str, bool] | None:
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mult):
        return None
    if (
        isinstance(node.left, ast.Constant)
        and type(node.left.value) is int
        and 2 <= node.left.value <= _TRIG_EXPAND_MAX_MULTIPLE
        and isinstance(node.right, ast.Name)
    ):
        return node.left.value, node.right.id, False
    if (
        isinstance(node.right, ast.Constant)
        and type(node.right.value) is int
        and 2 <= node.right.value <= _TRIG_EXPAND_MAX_MULTIPLE
        and isinstance(node.left, ast.Name)
    ):
        return node.right.value, node.left.id, True
    return None


def _trigexpand_scaled_terms(node: ast.AST) -> set[tuple[int, str, bool]]:
    result: set[tuple[int, str, bool]] = set()
    if isinstance(node, ast.BinOp):
        scaled = _trigexpand_scaled_term(node)
        if scaled is not None:
            result.add(scaled)
        result.update(_trigexpand_scaled_terms(node.left))
        result.update(_trigexpand_scaled_terms(node.right))
    elif isinstance(node, ast.UnaryOp):
        result.update(_trigexpand_scaled_terms(node.operand))
    elif isinstance(node, ast.Call):
        for argument in node.args:
            result.update(_trigexpand_scaled_terms(argument))
    return result


def _trigexpand_argument_linear_form(node: ast.AST) -> dict[str, int] | None:
    coefficients: dict[str, int] = {}

    def collect(current: ast.AST, sign: int) -> bool:
        if isinstance(current, ast.Name):
            coefficients[current.id] = coefficients.get(current.id, 0) + sign
            return True
        if isinstance(current, ast.UnaryOp) and isinstance(current.op, (ast.UAdd, ast.USub)):
            return collect(current.operand, sign if isinstance(current.op, ast.UAdd) else -sign)
        if isinstance(current, ast.BinOp) and isinstance(current.op, (ast.Add, ast.Sub)):
            if not collect(current.left, sign):
                return False
            return collect(
                current.right,
                sign if isinstance(current.op, ast.Add) else -sign,
            )
        scaled = _trigexpand_scaled_term(current)
        if scaled is not None:
            multiple, variable, _coefficient_on_right = scaled
            coefficients[variable] = coefficients.get(variable, 0) + sign * multiple
            return True
        return False

    if not collect(node, 1):
        return None
    return {name: value for name, value in coefficients.items() if value != 0}


def _trigexpand_pythagorean_quotients(
    original: ast.AST,
    expanded: ast.AST,
    variables: list[str],
) -> list[tuple[str, str]] | None:
    import sympy as sp

    trig_symbols = {
        name: (sp.Symbol(f"__trig_sin_{index}"), sp.Symbol(f"__trig_cos_{index}"))
        for index, name in enumerate(sorted(variables))
    }
    multiple_cache: dict[tuple[str, int], tuple[sp.Expr, sp.Expr]] = {}

    def multiple_pair(name: str, multiple: int) -> tuple[sp.Expr, sp.Expr]:
        key = name, multiple
        cached = multiple_cache.get(key)
        if cached is not None:
            return cached
        sine_atom, cosine_atom = trig_symbols[name]
        if multiple == 0:
            return sp.Integer(0), sp.Integer(1)
        if multiple < 0:
            positive_sine, positive_cosine = multiple_pair(name, -multiple)
            result = -positive_sine, positive_cosine
            multiple_cache[key] = result
            return result
        sine, cosine = sine_atom, cosine_atom
        for _ in range(2, multiple + 1):
            sine, cosine = (
                sp.expand(sine * cosine_atom + cosine * sine_atom),
                sp.expand(cosine * cosine_atom - sine * sine_atom),
            )
        result = sine, cosine
        multiple_cache[key] = result
        return result

    def angle_pair(argument: ast.AST) -> tuple[sp.Expr, sp.Expr] | None:
        linear_form = _trigexpand_argument_linear_form(argument)
        if linear_form is None:
            return None
        sine, cosine = sp.Integer(0), sp.Integer(1)
        for name, multiple in sorted(linear_form.items()):
            if name not in trig_symbols:
                return None
            term_sine, term_cosine = multiple_pair(name, multiple)
            sine, cosine = (
                sp.expand(sine * term_cosine + cosine * term_sine),
                sp.expand(cosine * term_cosine - sine * term_sine),
            )
        return sine, cosine

    def polynomial(node: ast.AST) -> sp.Expr | None:
        if isinstance(node, ast.Constant):
            if type(node.value) is int:
                return sp.Integer(node.value)
            if isinstance(node.value, float):
                return sp.Rational(str(node.value))
            return None
        if isinstance(node, ast.Name):
            return sp.Symbol(node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = polynomial(node.operand)
            if value is None:
                return None
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left = polynomial(node.left)
            right = polynomial(node.right)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return sp.expand(left + right)
            if isinstance(node.op, ast.Sub):
                return sp.expand(left - right)
            if isinstance(node.op, ast.Mult):
                return sp.expand(left * right)
            if isinstance(node.op, ast.Div) and right.is_Rational and right != 0:
                return sp.expand(left / right)
            if isinstance(node.op, ast.Pow) and right.is_Integer and 0 <= right <= 16:
                return sp.expand(left ** int(right))
            return None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            pair = angle_pair(node.args[0]) if len(node.args) == 1 and not node.keywords else None
            if pair is None:
                return None
            if node.func.id == "sin":
                return pair[0]
            if node.func.id == "cos":
                return pair[1]
        return None

    try:
        difference = polynomial(original)
        candidate = polynomial(expanded)
        if difference is None or candidate is None:
            return None
        difference = sp.expand(difference - candidate)
        if difference == 0:
            return []
        sine_cosine_relations = [
            sine_atom ** 2 + cosine_atom ** 2 - 1
            for sine_atom, cosine_atom in trig_symbols.values()
        ]
        if not sine_cosine_relations:
            return None
        generators = sorted(
            set().union(*(relation.free_symbols for relation in sine_cosine_relations))
            | difference.free_symbols,
            key=str,
        )
        quotients, remainder = sp.reduced(
            difference,
            sine_cosine_relations,
            *generators,
            domain=sp.QQ,
        )
        if remainder != 0:
            return None
        result: list[tuple[str, str]] = []
        for (name, (sine_atom, cosine_atom)), quotient in zip(
            trig_symbols.items(),
            quotients,
        ):
            quotient = sp.expand(quotient)
            if quotient == 0:
                continue
            source = sp.sstr(quotient).replace("**", "^")
            for variable_name, (atom_sine, atom_cosine) in trig_symbols.items():
                source = re.sub(
                    rf"(?<![A-Za-z0-9_]){re.escape(str(atom_sine))}(?![A-Za-z0-9_])",
                    f"(Real.sin {variable_name})",
                    source,
                )
                source = re.sub(
                    rf"(?<![A-Za-z0-9_]){re.escape(str(atom_cosine))}(?![A-Za-z0-9_])",
                    f"(Real.cos {variable_name})",
                    source,
                )
            result.append((name, source))
        return result
    except (ArithmeticError, RecursionError, TypeError, ValueError, sp.PolynomialError):
        return None


def _real_ast_to_wolfram(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return str(node.value)
    if isinstance(node, ast.Name):
        return "Pi" if node.id == "pi" else node.id
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        inner = _real_ast_to_wolfram(node.operand)
        return inner if isinstance(node.op, ast.UAdd) else f"-({inner})"
    if isinstance(node, ast.BinOp):
        operators = {
            ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Pow: "^",
        }
        operator = operators.get(type(node.op))
        if operator is not None:
            left = _real_ast_to_wolfram(node.left)
            right = _real_ast_to_wolfram(node.right)
            return f"({left}{operator}{right})"
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"sin", "cos"}
        and len(node.args) == 1
        and not node.keywords
    ):
        name = "Sin" if node.func.id == "sin" else "Cos"
        return f"{name}[{_real_ast_to_wolfram(node.args[0])}]"
    raise ValueError("unsupported AST in trigonometric result formatter")


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


def _linear_rational_pi_form(
    node: ast.AST,
    variable: str,
) -> tuple[Fraction, Fraction, Fraction] | None:
    """Return the x slope, Pi coefficient, and rational offset of an affine form."""
    constant = _constant_fraction(node)
    if constant is not None:
        return Fraction(0), Fraction(0), constant
    if isinstance(node, ast.Name):
        if node.id == variable:
            return Fraction(1), Fraction(0), Fraction(0)
        if node.id == "pi":
            return Fraction(0), Fraction(1), Fraction(0)
        return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _linear_rational_pi_form(node.operand, variable)
        if value is None or isinstance(node.op, ast.UAdd):
            return value
        return -value[0], -value[1], -value[2]
    if not isinstance(node, ast.BinOp):
        return None
    left = _linear_rational_pi_form(node.left, variable)
    right = _linear_rational_pi_form(node.right, variable)
    if left is None or right is None:
        return None
    if isinstance(node.op, ast.Add):
        return tuple(left[index] + right[index] for index in range(3))
    if isinstance(node.op, ast.Sub):
        return tuple(left[index] - right[index] for index in range(3))
    if isinstance(node.op, ast.Mult):
        left_is_rational = left[0] == 0 and left[1] == 0
        right_is_rational = right[0] == 0 and right[1] == 0
        if left_is_rational:
            return tuple(value * left[2] for value in right)
        if right_is_rational:
            return tuple(value * right[2] for value in left)
        return None
    if isinstance(node.op, ast.Div) and right[0] == 0 and right[1] == 0 and right[2] != 0:
        return tuple(value / right[2] for value in left)
    if isinstance(node.op, ast.Pow):
        exponent = _constant_fraction(node.right)
        if exponent is None or exponent.denominator != 1:
            return None
        if exponent == 0:
            return Fraction(0), Fraction(0), Fraction(1)
        if exponent == 1:
            return left
    return None


def _prepare_pi_bounded_sin_cos_integral(
    expression_source: str,
    variable: str,
    lower_source: str,
    upper_source: str,
    time_limit_seconds: float,
) -> Computation | None:
    if "π" in expression_source or any(
        match.group(0) != "Pi"
        for match in re.finditer(r"\bpi\b", expression_source, re.IGNORECASE)
    ):
        return None
    normalized_source = _normalize_wolfram_real_functions(expression_source)
    exact_source = _normalize_decimal_literals(normalized_source)
    if exact_source is None:
        return None
    try:
        parsed = parse_math_input(exact_source)
        expression_tree = ast.parse(parsed.expression, mode="eval")
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None
    integrand = expression_tree.body
    if not (
        isinstance(integrand, ast.Call)
        and isinstance(integrand.func, ast.Name)
        and integrand.func.id in {"sin", "cos"}
        and len(integrand.args) == 1
        and not integrand.keywords
    ):
        return None
    argument_source = ast.unparse(integrand.args[0])
    argument_lean, _ = _render_real_calculus_lean(
        argument_source, allow_variable_denominators=False
    )
    integrand_lean, _ = _render_real_calculus_lean(
        parsed.expression, allow_variable_denominators=False
    )
    argument_form = _linear_rational_pi_form(integrand.args[0], variable)
    if argument_form is None:
        return None
    slope, phase_offset, rational_offset = argument_form
    if (
        slope == 0
        or rational_offset != 0
        or abs(slope.numerator).bit_length() > 256
        or slope.denominator.bit_length() > 256
        or abs(phase_offset.numerator).bit_length() > 256
        or phase_offset.denominator.bit_length() > 256
        or argument_lean is None
        or integrand_lean is None
    ):
        return None

    if any(re.search(r"\bpi\b|π", bound) for bound in (lower_source, upper_source)):
        return None
    normalized_lower = re.sub(r"\bPi\b", "pi", lower_source)
    normalized_upper = re.sub(r"\bPi\b", "pi", upper_source)
    lower_multiple = _parse_rational_pi_multiple(normalized_lower)
    upper_multiple = _parse_rational_pi_multiple(normalized_upper)
    allowed_multiples = {
        Fraction(-1), Fraction(-1, 2), Fraction(-1, 4), Fraction(-1, 8),
        Fraction(-1, 16), Fraction(-1, 32), Fraction(0), Fraction(1, 32),
        Fraction(1, 16), Fraction(1, 8), Fraction(1, 4), Fraction(1, 2), Fraction(1),
    }
    if lower_multiple not in allowed_multiples or upper_multiple not in allowed_multiples:
        return None
    endpoint_multiples = {
        Fraction(-2), Fraction(-1), Fraction(-1, 2), Fraction(-1, 4),
        Fraction(-1, 8), Fraction(-1, 16), Fraction(-1, 32), Fraction(0),
        Fraction(1, 32), Fraction(1, 16), Fraction(1, 8), Fraction(1, 4),
        Fraction(1, 2), Fraction(1), Fraction(2),
    }
    lower_phase = slope * lower_multiple + phase_offset
    upper_phase = slope * upper_multiple + phase_offset
    if lower_phase not in endpoint_multiples or upper_phase not in endpoint_multiples:
        return None

    symbolic = run_symbolic(SymbolicRequest(
        operation="integrate",
        expression=parsed.expression,
        variable=variable,
        lower=normalized_lower,
        upper=normalized_upper,
        time_limit_seconds=max(0.001, min(time_limit_seconds, 60.0)),
    ))
    if symbolic.get("timed_out"):
        return Computation("", "", timed_out=True)
    if symbolic.get("error") or not isinstance(symbolic.get("result"), str):
        diagnostic = symbolic.get("diagnostics") or symbolic.get("error") or (
            "Symbolic worker returned no exact trigonometric integral"
        )
        return Computation("", "", backend_error=str(diagnostic)[:1000])
    exact_radical_result = _parse_exact_radical_basis(symbolic["result"])
    if exact_radical_result is None:
        return None
    if any(
        abs(value.numerator).bit_length() > 256
        or value.denominator.bit_length() > 256
        for value in exact_radical_result
    ):
        return None

    lean_lower = _lean_pi_multiple(lower_multiple)
    lean_upper = _lean_pi_multiple(upper_multiple)
    lean_lower_phase = _lean_pi_multiple(lower_phase)
    lean_upper_phase = _lean_pi_multiple(upper_phase)
    lean_slope = _lean_rational(slope)
    lean_phase_offset = _lean_rational(phase_offset)
    lean_result = _lean_exact_radical_basis(exact_radical_result)
    if integrand.func.id == "sin":
        endpoint_function = "cos"
        primitive = "fun t : ℝ => -Real.cos (a * t + b * Real.pi) / a"
        primitive_derivative = (
            f"have harg : {argument_lean} = a * {variable} + b * Real.pi := by\n"
            f"      norm_num [a, b] <;> ring\n"
            f"    have hlinear : HasDerivAt (fun y : ℝ => a * y + b * Real.pi) a {variable} := by\n"
            f"      convert (hasDerivAt_const_mul a).add_const (b * Real.pi) using 1\n"
            f"    have hcos : HasDerivAt (fun y : ℝ => Real.cos (a * y + b * Real.pi)) "
            f"(-Real.sin (a * {variable} + b * Real.pi) * a) {variable} := by\n"
            f"      convert (Real.hasDerivAt_cos (a * {variable} + b * Real.pi)).comp {variable} hlinear using 1\n"
            f"      ext y\n"
            f"      simp\n"
            f"    have hprimitive : HasDerivAt F (Real.sin (a * {variable} + b * Real.pi)) {variable} := by\n"
            f"      convert hcos.neg.div_const a using 1\n"
            f"      field_simp [ha]\n"
            f"      <;> ring\n"
            f"    have hfunction : Real.sin {argument_lean} = "
            f"Real.sin (a * {variable} + b * Real.pi) := congrArg Real.sin harg\n"
            f"    rw [hfunction]\n"
            f"    exact hprimitive"
        )
    else:
        endpoint_function = "sin"
        primitive = "fun t : ℝ => Real.sin (a * t + b * Real.pi) / a"
        primitive_derivative = (
            f"have harg : {argument_lean} = a * {variable} + b * Real.pi := by\n"
            f"      norm_num [a, b] <;> ring\n"
            f"    have hlinear : HasDerivAt (fun y : ℝ => a * y + b * Real.pi) a {variable} := by\n"
            f"      convert (hasDerivAt_const_mul a).add_const (b * Real.pi) using 1\n"
            f"    have hsin : HasDerivAt (fun y : ℝ => Real.sin (a * y + b * Real.pi)) "
            f"(Real.cos (a * {variable} + b * Real.pi) * a) {variable} := by\n"
            f"      convert (Real.hasDerivAt_sin (a * {variable} + b * Real.pi)).comp {variable} hlinear using 1\n"
            f"      ext y\n"
            f"      simp\n"
            f"    have hprimitive : HasDerivAt F (Real.cos (a * {variable} + b * Real.pi)) {variable} := by\n"
            f"      convert hsin.div_const a using 1\n"
            f"      field_simp [ha]\n"
            f"      <;> ring\n"
            f"    have hfunction : Real.cos {argument_lean} = "
            f"Real.cos (a * {variable} + b * Real.pi) := congrArg Real.cos harg\n"
            f"    rw [hfunction]\n"
            f"    exact hprimitive"
        )
    special_endpoint_phases = {
        Fraction(1, 32), Fraction(1, 16), Fraction(1, 8), Fraction(1, 4)
    }
    if any(abs(phase) in special_endpoint_phases for phase in (lower_phase, upper_phase)):
        endpoint_lemmas = _pi_trig_simplification_lemmas(
            endpoint_function, (lower_phase, upper_phase)
        )
        endpoint_evaluation = f"simp only [a, {', '.join(endpoint_lemmas)}] <;> ring"
    else:
        endpoint_evaluation = (
            "norm_num [a, b, Real.sin_pi_div_two, Real.sin_pi, Real.sin_neg, "
            "Real.sin_two_pi, Real.cos_pi_div_two, Real.cos_neg, "
            "Real.cos_pi, Real.cos_two_pi]"
        )
    source = f"""import Mathlib.Analysis.SpecialFunctions.Trigonometric.Basic
import Mathlib.Analysis.SpecialFunctions.Trigonometric.Deriv
import Mathlib.MeasureTheory.Integral.IntervalIntegral.FundThmCalculus
import Mathlib.Tactic.FunProp
import Mathlib.Tactic.NormNum
import Mathlib.Tactic.FieldSimp

theorem wolfram_language_compute :
    (∫ {variable} in {lean_lower}..{lean_upper}, {integrand_lean}) = {lean_result} := by
  let a : ℝ := {lean_slope}
  let b : ℝ := {lean_phase_offset}
  have ha : a ≠ 0 := by norm_num [a]
  let F : ℝ → ℝ := {primitive}
  have hderiv : ∀ {variable} ∈ Set.uIcc {lean_lower} {lean_upper},
      HasDerivAt F (Real.{integrand.func.id} {argument_lean}) {variable} := by
    intro {variable} hx
    {primitive_derivative}
  have hcont : ContinuousOn
      (fun {variable} : ℝ => {integrand_lean})
      (Set.uIcc {lean_lower} {lean_upper}) := by
    fun_prop
  have hint : IntervalIntegrable
      (fun {variable} : ℝ => {integrand_lean})
      MeasureTheory.volume {lean_lower} {lean_upper} := by
    exact hcont.intervalIntegrable
  have h_lower_phase : a * {lean_lower} + b * Real.pi = {lean_lower_phase} := by
    norm_num [a, b] <;> ring
  have h_upper_phase : a * {lean_upper} + b * Real.pi = {lean_upper_phase} := by
    norm_num [a, b] <;> ring
  rw [intervalIntegral.integral_eq_sub_of_hasDerivAt hderiv hint]
  simp only [F]
  rw [h_upper_phase, h_lower_phase]
  {endpoint_evaluation}

#print axioms wolfram_language_compute
"""
    return Computation(_format_exact_radical_basis(exact_radical_result), source)


def _parse_rational_pi_multiple(source: str) -> Fraction | None:
    try:
        parsed = parse_math_input(source)
        tree = ast.parse(parsed.expression, mode="eval")
    except (MathInputError, ValueError, SyntaxError, RecursionError):
        return None
    form = _rational_pi_form(tree.body)
    if form is None or form[1] != 0:
        return None
    coefficient = form[0]
    if coefficient.numerator.bit_length() > 256 or coefficient.denominator.bit_length() > 256:
        return None
    return coefficient


def _rational_pi_form(node: ast.AST) -> tuple[Fraction, Fraction] | None:
    if isinstance(node, ast.Name):
        return (Fraction(1), Fraction(0)) if node.id == "pi" else None
    constant = _constant_fraction(node)
    if constant is not None:
        return Fraction(0), constant
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _rational_pi_form(node.operand)
        if value is None or isinstance(node.op, ast.UAdd):
            return value
        return -value[0], -value[1]
    if not isinstance(node, ast.BinOp):
        return None
    left = _rational_pi_form(node.left)
    right = _rational_pi_form(node.right)
    if left is None or right is None:
        return None
    if isinstance(node.op, ast.Add):
        return left[0] + right[0], left[1] + right[1]
    if isinstance(node.op, ast.Sub):
        return left[0] - right[0], left[1] - right[1]
    if isinstance(node.op, ast.Mult):
        if left[0] and right[0]:
            return None
        return left[0] * right[1] + right[0] * left[1], left[1] * right[1]
    if isinstance(node.op, ast.Div) and right[0] == 0 and right[1] != 0:
        return left[0] / right[1], left[1] / right[1]
    return None


def _lean_pi_multiple(coefficient: Fraction) -> str:
    if coefficient == 2:
        return "(2 * Real.pi)"
    if coefficient == -2:
        return "(-(2 * Real.pi))"
    if coefficient == 0:
        return "(0)"
    if coefficient == 1:
        return "(Real.pi)"
    if coefficient == -1:
        return "(-Real.pi)"
    if coefficient == Fraction(1, 2):
        return "(Real.pi / 2)"
    if coefficient == Fraction(-1, 2):
        return "(-(Real.pi / 2))"
    if coefficient == Fraction(1, 4):
        return "(Real.pi / 4)"
    if coefficient == Fraction(-1, 4):
        return "(-(Real.pi / 4))"
    if coefficient == Fraction(1, 8):
        return "(Real.pi / 8)"
    if coefficient == Fraction(-1, 8):
        return "(-(Real.pi / 8))"
    if coefficient == Fraction(1, 16):
        return "(Real.pi / 16)"
    if coefficient == Fraction(-1, 16):
        return "(-(Real.pi / 16))"
    if coefficient == Fraction(1, 32):
        return "(Real.pi / 32)"
    if coefficient == Fraction(-1, 32):
        return "(-(Real.pi / 32))"
    raise ValueError("unsupported rational multiple of pi")


def _pi_trig_simplification_lemmas(
    function: str,
    coefficients: tuple[Fraction, Fraction],
) -> tuple[str, ...]:
    names: list[str] = []
    value_lemmas = {
        Fraction(0): f"Real.{function}_zero",
        Fraction(1, 32): f"Real.{function}_pi_div_thirty_two",
        Fraction(1, 16): f"Real.{function}_pi_div_sixteen",
        Fraction(1, 8): f"Real.{function}_pi_div_eight",
        Fraction(1, 4): f"Real.{function}_pi_div_four",
        Fraction(1, 2): f"Real.{function}_pi_div_two",
        Fraction(1): f"Real.{function}_pi",
        Fraction(2): f"Real.{function}_two_pi",
    }
    for coefficient in coefficients:
        magnitude = abs(coefficient)
        lemma = value_lemmas.get(magnitude)
        if lemma is None:
            raise ValueError("unsupported rational multiple of pi")
        if lemma not in names:
            names.append(lemma)
        if coefficient < 0:
            sign_lemma = f"Real.{function}_neg"
            if sign_lemma not in names:
                names.append(sign_lemma)
    return tuple(names)


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
    csch_arguments = _csch_arguments(expression_tree.body)
    if csch_arguments is None:
        return None
    csch_affine = _direct_csch_affine_integrand(
        expression_tree.body, variable
    )
    if csch_arguments and csch_affine is None:
        return None
    sech_arguments = _sech_arguments(expression_tree.body)
    sech_affine = _direct_sech_affine_integrand(
        expression_tree.body, variable
    )
    if sech_arguments and sech_affine is None:
        return None
    coth_arguments = _coth_arguments(expression_tree.body)
    if coth_arguments is None:
        return None
    coth_affine = _direct_coth_affine_integrand(
        expression_tree.body, variable
    )
    if coth_arguments and coth_affine is None:
        return None
    tanh_arguments = [
        node.args[0]
        for node in ast.walk(expression_tree.body)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "tanh"
        and len(node.args) == 1
        and not node.keywords
    ]
    tanh_affine = _direct_tanh_affine_integrand(
        expression_tree.body, variable
    )
    if (
        any(_contains_calculus_variable(argument) for argument in tanh_arguments)
        and tanh_affine is None
    ):
        return None
    explicit_base_log_arguments = {
        ast.dump(node.args[0], include_attributes=False)
        for node in ast.walk(expression_tree.body)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "log"
        and len(node.args) == 2
    }

    lower = _parse_exact_rational(lower_source)
    upper = _parse_exact_rational(upper_source)
    if lower is None or upper is None:
        return None
    csch_endpoint_arguments: tuple[Fraction, Fraction] | None = None
    if csch_affine is not None:
        argument = csch_affine[0]
        lower_argument = _exact_rational_node_value(argument, variable, lower)
        upper_argument = _exact_rational_node_value(argument, variable, upper)
        if (
            lower_argument is None
            or upper_argument is None
            or lower_argument == 0
            or upper_argument == 0
            or (lower_argument < 0) != (upper_argument < 0)
        ):
            return None
        csch_endpoint_arguments = (lower_argument, upper_argument)
    sech_endpoint_arguments: tuple[Fraction, Fraction] | None = None
    if sech_affine is not None:
        argument = sech_affine[0]
        lower_argument = _exact_rational_node_value(argument, variable, lower)
        upper_argument = _exact_rational_node_value(argument, variable, upper)
        if lower_argument is None or upper_argument is None:
            return None
        sech_endpoint_arguments = (lower_argument, upper_argument)
    coth_endpoint_arguments: tuple[Fraction, Fraction] | None = None
    if coth_affine is not None:
        argument = coth_affine[0]
        lower_argument = _exact_rational_node_value(argument, variable, lower)
        upper_argument = _exact_rational_node_value(argument, variable, upper)
        if (
            lower_argument is None
            or upper_argument is None
            or lower_argument == 0
            or upper_argument == 0
            or (lower_argument < 0) != (upper_argument < 0)
        ):
            return None
        coth_endpoint_arguments = (lower_argument, upper_argument)
    tanh_endpoint_arguments: tuple[Fraction, Fraction] | None = None
    if tanh_affine is not None:
        argument = tanh_affine[0]
        lower_argument = _exact_rational_node_value(argument, variable, lower)
        upper_argument = _exact_rational_node_value(argument, variable, upper)
        if lower_argument is None or upper_argument is None:
            return None
        tanh_endpoint_arguments = (lower_argument, upper_argument)
    for node in ast.walk(expression_tree.body):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Name)
            or node.func.id != "log"
            or len(node.args) != 2
        ):
            continue
        argument_constant = _constant_fraction(node.args[0])
        if argument_constant is not None:
            if argument_constant <= 0:
                return None
            continue
        argument_interval = _affine_interval_values(
            node.args[0], variable, lower, upper
        )
        if argument_interval is None or min(argument_interval) <= 0:
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
    if csch_affine is not None:
        argument_text = ast.unparse(csch_affine[0])
        antiderivative = (
            "log(abs(tanh(("
            f"{argument_text})/2)))/({_fraction_text(csch_affine[1])})"
        )
    elif coth_affine is not None:
        argument_text = ast.unparse(coth_affine[0])
        log_sinh = f"log(abs(sinh({argument_text})))"
        antiderivative = (
            log_sinh
            if coth_affine[1] == 1
            else f"({log_sinh})/({_fraction_text(coth_affine[1])})"
        )
    elif tanh_affine is not None:
        argument_text = ast.unparse(tanh_affine[0])
        log_cosh = f"log(cosh({argument_text}))"
        antiderivative = (
            log_cosh
            if tanh_affine[1] == 1
            else f"({log_cosh})/({_fraction_text(tanh_affine[1])})"
        )
    elif sech_affine is not None:
        argument_text = ast.unparse(sech_affine[0])
        antiderivative = (
            f"atan(sinh({argument_text}))/({_fraction_text(sech_affine[1])})"
        )
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
    if csch_endpoint_arguments is not None and csch_affine is not None:
        lower_argument, upper_argument = csch_endpoint_arguments
        lower_tanh_log = (
            f"log(abs(tanh(({_fraction_text(lower_argument)})/2)))"
        )
        upper_tanh_log = (
            f"log(abs(tanh(({_fraction_text(upper_argument)})/2)))"
        )
        endpoint_difference = f"({upper_tanh_log} - {lower_tanh_log})"
        definite_result = (
            endpoint_difference
            if csch_affine[1] == 1
            else f"({endpoint_difference})/({_fraction_text(csch_affine[1])})"
        )
    elif sech_endpoint_arguments is not None and sech_affine is not None:
        lower_argument, upper_argument = sech_endpoint_arguments
        lower_atan = f"atan(sinh({_fraction_text(lower_argument)}))"
        upper_atan = f"atan(sinh({_fraction_text(upper_argument)}))"
        endpoint_difference = f"({upper_atan} - {lower_atan})"
        definite_result = (
            endpoint_difference
            if sech_affine[1] == 1
            else f"({endpoint_difference})/({_fraction_text(sech_affine[1])})"
        )
    elif coth_endpoint_arguments is not None and coth_affine is not None:
        lower_argument, upper_argument = coth_endpoint_arguments
        lower_log = f"log(abs(sinh({_fraction_text(lower_argument)})))"
        upper_log = f"log(abs(sinh({_fraction_text(upper_argument)})))"
        endpoint_difference = f"({upper_log} - {lower_log})"
        definite_result = (
            endpoint_difference
            if coth_affine[1] == 1
            else f"({endpoint_difference})/({_fraction_text(coth_affine[1])})"
        )
    elif tanh_endpoint_arguments is not None and tanh_affine is not None:
        lower_argument, upper_argument = tanh_endpoint_arguments
        lower_log = f"log(cosh({_fraction_text(lower_argument)}))"
        upper_log = f"log(cosh({_fraction_text(upper_argument)}))"
        endpoint_difference = f"({upper_log} - {lower_log})"
        definite_result = (
            endpoint_difference
            if tanh_affine[1] == 1
            else f"({endpoint_difference})/({_fraction_text(tanh_affine[1])})"
        )
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

    log_base_proofs = _log_base_nonzero_proofs(
        expression_tree.body,
        antiderivative_tree.body,
        definite_result_tree.body,
    )
    if log_base_proofs is None:
        return None

    for expression_root in (expression_tree.body, antiderivative_tree.body):
        domain_candidates = [
            *_variable_denominators(expression_root),
            *_log_arguments(expression_root),
        ]
        if expression_root is expression_tree.body and csch_arguments:
            domain_candidates.extend(
                _sinh_call(argument) for argument in csch_arguments
            )
        if expression_root is expression_tree.body and coth_arguments:
            domain_candidates.extend(
                _sinh_call(argument) for argument in coth_arguments
            )
        tangent_cosines = _tan_cosine_arguments(expression_root)
        if tangent_cosines is None:
            return None
        domain_candidates.extend(tangent_cosines)
        for candidate in domain_candidates:
            key = ast.dump(candidate, include_attributes=False)
            if key in log_base_proofs:
                continue
            if key in seen_domain_expressions:
                continue
            constant = _constant_fraction(candidate)
            if constant is not None:
                if constant == 0:
                    return None
                if key in explicit_base_log_arguments and constant < 0:
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

    domain_hypotheses: dict[str, str] = dict(log_base_proofs)
    positive_hypotheses: dict[str, str] = {}
    interval_domain_binders: list[str] = []
    interval_domain_proofs: list[str] = list(log_base_proofs.values())
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
        if (
            isinstance(domain_expression, ast.Call)
            and isinstance(domain_expression.func, ast.Name)
            and domain_expression.func.id == "cosh"
            and len(domain_expression.args) == 1
            and not domain_expression.keywords
        ):
            cosh_argument = _render_real_calculus_node(
                domain_expression.args[0],
                set(),
                {"nodes": 0},
                0,
                substitutions={variable: point_variable},
                allow_variable_denominators=True,
            )
            if cosh_argument is None:
                return None
            cosh_nonzero = f"ne_of_gt (Real.cosh_pos {cosh_argument})"
            domain_hypotheses[key] = cosh_nonzero
            interval_domain_proofs.append(cosh_nonzero)
            continue
        affine_values = _affine_interval_values(
            domain_expression, variable, lower, upper
        )
        if key in explicit_base_log_arguments:
            if affine_values is not None:
                affine_lower, affine_upper = affine_values
                if min(affine_lower, affine_upper) <= 0:
                    return None
                hypothesis_name = fresh_name(
                    f"h_interval_auto_positive_{index}", used_names
                )
                positive_hypotheses[key] = (
                    f"({hypothesis_name} {point_variable} hpoint)"
                )
                domain_hypotheses[key] = (
                    f"ne_of_gt ({hypothesis_name} {point_variable} hpoint)"
                )
                interval_domain_lemmas.append(
                    interval_lemma_source(
                        hypothesis_name, f"0 < {rendered_domain}"
                    )
                )
                interval_domain_proofs.append(
                    f"ne_of_gt ({hypothesis_name} {point_variable} hpoint)"
                )
                continue
            hypothesis_name = fresh_name(
                f"h_interval_positive_{index}", used_names
            )
            positive_hypotheses[key] = (
                f"({hypothesis_name} {point_variable} hpoint)"
            )
            domain_hypotheses[key] = (
                f"ne_of_gt ({hypothesis_name} {point_variable} hpoint)"
            )
            interval_domain_binders.append(
                f"({hypothesis_name} : ∀ {point_variable} : ℝ, "
                f"{point_variable} ∈ Set.uIcc {lean_lower} {lean_upper} → "
                f"0 < {rendered_domain})"
            )
            interval_domain_proofs.append(
                f"ne_of_gt ({hypothesis_name} {point_variable} hpoint)"
            )
            source_expression = ast.unparse(domain_expression).replace("**", "^")
            domain_conditions.append(
                f"throughout the closed interval with endpoints {_fraction_text(lower)} "
                f"and {_fraction_text(upper)}, {source_expression} > 0"
            )
            continue
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
        elif (
            isinstance(domain_expression, ast.Call)
            and isinstance(domain_expression.func, ast.Name)
            and domain_expression.func.id == "sinh"
        ):
            source_expression = (
                f"Sinh[{ast.unparse(domain_expression.args[0]).replace('**', '^')}]"
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

    log_argument_keys = {
        ast.dump(argument, include_attributes=False)
        for expression_root in (expression_tree.body, antiderivative_tree.body)
        for argument in _log_arguments(expression_root)
    }
    for index, key in enumerate(sorted(log_argument_keys)):
        if key not in domain_hypotheses:
            continue
        argument = next(
            candidate
            for expression_root in (expression_tree.body, antiderivative_tree.body)
            for candidate in _log_arguments(expression_root)
            if ast.dump(candidate, include_attributes=False) == key
        )
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
        lemma_name = fresh_name(f"h_interval_log_nonzero_{index}", used_names)
        interval_domain_lemmas.append(
            f"  have {lemma_name} : ∀ {point_variable} : ℝ, "
            f"{point_variable} ∈ Set.uIcc {lean_lower} {lean_upper} → "
            f"{rendered_argument} ≠ 0 := by\n"
            f"    intro {point_variable} hpoint\n"
            f"    exact {domain_hypotheses[key]}"
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
    log_reciprocal_lemmas: list[tuple[str, str, str]] = []
    seen_log_equivalences: set[frozenset[str]] = set()
    if len(rendered_log_arguments) <= 32:
        for index, (left_source, left_expression) in enumerate(rendered_log_arguments):
            for right_source, right_expression in rendered_log_arguments[index + 1:]:
                equivalence_key = frozenset((left_source, right_source))
                if equivalence_key in seen_log_equivalences:
                    continue
                if _sympy.simplify(left_expression + right_expression) == 0:
                    seen_log_equivalences.add(equivalence_key)
                    lemma_name = fresh_name(
                        f"h_log_sign_{len(log_equivalence_lemmas)}",
                        used_names,
                    )
                    log_equivalence_lemmas.append(
                        (lemma_name, left_source, right_source)
                    )
                elif (
                    left_expression.is_Rational is True
                    and right_expression.is_Rational is True
                    and left_expression.is_positive is True
                    and right_expression.is_positive is True
                    and _sympy.simplify(left_expression * right_expression - 1) == 0
                ):
                    seen_log_equivalences.add(equivalence_key)
                    lemma_name = fresh_name(
                        f"h_log_reciprocal_{len(log_reciprocal_lemmas)}",
                        used_names,
                    )
                    log_reciprocal_lemmas.append(
                        (lemma_name, left_source, right_source)
                    )

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
    if csch_affine is not None:
        csch_tanh_arguments = _log_arguments(antiderivative_tree.body)
        if not csch_tanh_arguments:
            return None
        csch_tanh_hypothesis = domain_hypotheses.get(
            ast.dump(csch_tanh_arguments[0], include_attributes=False)
        )
        csch_sinh_hypothesis = domain_hypotheses.get(
            ast.dump(_sinh_call(csch_affine[0]), include_attributes=False)
        )
        csch_argument_at_point = _render_real_calculus_node(
            csch_affine[0],
            set(),
            {"nodes": 0},
            0,
            substitutions={variable: point_variable},
            allow_variable_denominators=True,
        )
        csch_argument_derivative_proof = _derivative_proof_term(
            csch_affine[0],
            variable,
            function_variable,
            point_variable,
            {"nodes": 0},
            0,
            domain_hypotheses=domain_hypotheses,
            positive_hypotheses=positive_hypotheses,
        )
        if (
            csch_tanh_hypothesis is None
            or csch_sinh_hypothesis is None
            or csch_argument_at_point is None
            or csch_argument_derivative_proof is None
        ):
            return None
        proof = (
            "((wolfram_hasDerivAt_log_tanh_half "
            f"{csch_argument_at_point} {csch_tanh_hypothesis} "
            f"{csch_sinh_hypothesis}).comp {point_variable} "
            f"({csch_argument_derivative_proof})).div_const "
            f"({_lean_rational(csch_affine[1])})"
        )
    elif sech_affine is not None:
        sech_argument_at_point = _render_real_calculus_node(
            sech_affine[0],
            set(),
            {"nodes": 0},
            0,
            substitutions={variable: point_variable},
            allow_variable_denominators=True,
        )
        sech_argument_derivative_proof = _derivative_proof_term(
            sech_affine[0],
            variable,
            function_variable,
            point_variable,
            {"nodes": 0},
            0,
            domain_hypotheses=domain_hypotheses,
            positive_hypotheses=positive_hypotheses,
        )
        if (
            sech_argument_at_point is None
            or sech_argument_derivative_proof is None
        ):
            return None
        proof = (
            f"((wolfram_hasDerivAt_arctan_sinh {sech_argument_at_point}).comp "
            f"{point_variable} ({sech_argument_derivative_proof})).div_const "
            f"({_lean_rational(sech_affine[1])})"
        )
    elif coth_affine is not None:
        coth_sinh_hypothesis = domain_hypotheses.get(
            ast.dump(_sinh_call(coth_affine[0]), include_attributes=False)
        )
        coth_argument_at_point = _render_real_calculus_node(
            coth_affine[0],
            set(),
            {"nodes": 0},
            0,
            substitutions={variable: point_variable},
            allow_variable_denominators=True,
        )
        coth_argument_derivative_proof = _derivative_proof_term(
            coth_affine[0],
            variable,
            function_variable,
            point_variable,
            {"nodes": 0},
            0,
            domain_hypotheses=domain_hypotheses,
            positive_hypotheses=positive_hypotheses,
        )
        if (
            coth_sinh_hypothesis is None
            or coth_argument_at_point is None
            or coth_argument_derivative_proof is None
        ):
            return None
        proof = (
            f"((wolfram_hasDerivAt_log_abs_sinh {coth_argument_at_point} "
            f"{coth_sinh_hypothesis}).comp {point_variable} "
            f"({coth_argument_derivative_proof})).div_const "
            f"({_lean_rational(coth_affine[1])})"
        )
    elif tanh_affine is not None:
        tanh_argument_at_point = _render_real_calculus_node(
            tanh_affine[0],
            set(),
            {"nodes": 0},
            0,
            substitutions={variable: point_variable},
            allow_variable_denominators=True,
        )
        tanh_argument_derivative_proof = _derivative_proof_term(
            tanh_affine[0],
            variable,
            function_variable,
            point_variable,
            {"nodes": 0},
            0,
            domain_hypotheses=domain_hypotheses,
            positive_hypotheses=positive_hypotheses,
        )
        if (
            tanh_argument_at_point is None
            or tanh_argument_derivative_proof is None
        ):
            return None
        proof = (
            f"((wolfram_hasDerivAt_log_cosh {tanh_argument_at_point}).comp "
            f"{point_variable} ({tanh_argument_derivative_proof})).div_const "
            f"({_lean_rational(tanh_affine[1])})"
        )
    if proof is None:
        return None

    field_simp_arguments = (
        f" [{', '.join(interval_domain_proofs)}]" if interval_domain_proofs else ""
    )
    log_lemma_names = [name for name, _, _ in log_equivalence_lemmas]
    log_sign_lemma_source = "\n".join(
        f"  have {name} : Real.log ({left}) = Real.log ({right}) := by\n"
        f"    rw [show ({left}) = -({right}) by ring]\n"
        f"    exact Real.log_neg_eq_log ({right})"
        for name, left, right in log_equivalence_lemmas
    )
    log_reciprocal_lemma_source = "\n".join(
        f"  have {name} : Real.log ({left}) = -Real.log ({right}) := by\n"
        f"    rw [show ({left} : ℝ) = ({right} : ℝ)⁻¹ by norm_num, Real.log_inv]"
        for name, left, right in log_reciprocal_lemmas
    )
    if coth_affine is not None or tanh_affine is not None:
        log_equivalence_lemmas.clear()
        log_reciprocal_lemmas.clear()
        log_sign_lemma_source = ""
        log_reciprocal_lemma_source = ""
    log_lemma_source = "\n".join(
        part for part in (log_sign_lemma_source, log_reciprocal_lemma_source) if part
    )
    if log_lemma_source:
        log_lemma_source = "\n" + log_lemma_source
    final_simp_names = [
        *log_lemma_names,
        *[name for name, _, _ in log_reciprocal_lemmas],
        *([arcsin_half_lemma_name] if arcsin_half_lemma_name is not None else []),
    ]
    final_simplifier = (
        f"simp only [{', '.join(final_simp_names)}] <;> "
        if final_simp_names
        else ""
    )
    final_integral_tactic = (
        f"norm_num [F] <;> {final_simplifier}field_simp <;> ring"
    )
    if coth_affine is not None:
        final_integral_tactic = (
            "try (simp only [F] <;> "
            "rw [wolfram_log_sinh_abs, wolfram_log_sinh_abs] <;> "
            "norm_num <;> ring_nf)"
        )
    elif tanh_affine is not None:
        final_integral_tactic = (
            "try (simp [F, Real.cosh_neg] <;> ring_nf)"
        )
    interval_domain_lemma_source = "\n".join(interval_domain_lemmas)
    if interval_domain_lemma_source:
        interval_domain_lemma_source = "\n" + interval_domain_lemma_source

    csch_helper_source = ""
    sech_helper_source = ""
    coth_helper_source = ""
    tanh_helper_source = ""
    hderiv_function_proof = "simp [F, id_eq] <;> ring"
    hderiv_value_proof = (
        f"simp [F, id_eq] <;> field_simp{field_simp_arguments} <;> ring"
    )
    hcont_proof = "fun_prop"
    if csch_affine is not None:
        csch_helper_source = "\n".join(
            [
                _tanh_derivative_lemma_source(),
                _sinh_double_lemma_source(),
                _csch_primitive_derivative_lemma_source(),
            ]
        )
        if csch_sinh_hypothesis is None:
            return None
        hderiv_function_proof = (
            "simp [F, Function.comp_def, id_eq] <;> ring"
        )
        hderiv_value_proof = (
            f"simp [F, Function.comp_def, id_eq] <;> "
            f"field_simp{field_simp_arguments} <;> ring"
        )
        hcont_proof = (
            "apply ContinuousOn.div₀\n"
            "    · exact continuousOn_const\n"
            "    · fun_prop\n"
            f"    · intro {point_variable} hpoint\n"
            f"      exact {csch_sinh_hypothesis}"
        )
    elif sech_affine is not None:
        sech_helper_source = _sech_primitive_derivative_lemma_source()
        sech_cosh_nonzero = (
            f"ne_of_gt (Real.cosh_pos {sech_argument_at_point})"
        )
        hderiv_function_proof = (
            "simp [F, Function.comp_def, id_eq] <;> ring"
        )
        hderiv_value_proof = (
            f"simp [F, Function.comp_def, id_eq] <;> "
            f"field_simp [{', '.join([*interval_domain_proofs, sech_cosh_nonzero])}] <;> "
            "ring"
        )
        hcont_proof = (
            "apply ContinuousOn.div₀\n"
            "    · exact continuousOn_const\n"
            "    · fun_prop\n"
            f"    · intro {point_variable} hpoint\n"
            f"      exact ne_of_gt (Real.cosh_pos {sech_argument_at_point})"
        )
    elif coth_affine is not None:
        coth_helper_source = _coth_primitive_derivative_lemma_source()
        if coth_sinh_hypothesis is None:
            return None
        hderiv_function_proof = (
            "simp [F, Function.comp_def, id_eq] <;> ring"
        )
        hderiv_value_proof = (
            f"simp [F, Function.comp_def, id_eq] <;> "
            f"field_simp{field_simp_arguments} <;> ring"
        )
        hcont_proof = (
            "apply ContinuousOn.div₀\n"
            "    · fun_prop\n"
            "    · fun_prop\n"
            f"    · intro {point_variable} hpoint\n"
            f"      exact {coth_sinh_hypothesis}"
        )
    elif tanh_affine is not None:
        tanh_helper_source = _tanh_primitive_derivative_lemma_source()
        tanh_argument_at_integral_var = _render_real_calculus_node(
            tanh_affine[0],
            set(),
            {"nodes": 0},
            0,
            substitutions={variable: integration_variable},
            allow_variable_denominators=True,
        )
        tanh_argument_at_point = _render_real_calculus_node(
            tanh_affine[0],
            set(),
            {"nodes": 0},
            0,
            substitutions={variable: point_variable},
            allow_variable_denominators=True,
        )
        if (
            tanh_argument_at_integral_var is None
            or tanh_argument_at_point is None
        ):
            return None
        hderiv_function_proof = (
            "simp [F, Function.comp_def, id_eq] <;> ring"
        )
        hderiv_value_proof = (
            f"simp [F, Function.comp_def, id_eq] <;> "
            f"field_simp{field_simp_arguments} <;> ring"
        )
        hcont_proof = (
            f"rw [show (fun {integration_variable} : ℝ => "
            f"Real.tanh ({tanh_argument_at_integral_var})) =\n"
            f"      (fun {integration_variable} : ℝ => "
            f"Real.sinh ({tanh_argument_at_integral_var}) / "
            f"Real.cosh ({tanh_argument_at_integral_var})) by\n"
            f"      funext {integration_variable}\n"
            f"      exact Real.tanh_eq_sinh_div_cosh "
            f"({tanh_argument_at_integral_var})]\n"
            "    apply ContinuousOn.div₀\n"
            "    · fun_prop\n"
            "    · fun_prop\n"
            f"    · intro {point_variable} hpoint\n"
            f"      exact ne_of_gt (Real.cosh_pos ({tanh_argument_at_point}))"
        )

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
import Mathlib.Analysis.SpecialFunctions.Trigonometric.Basic
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
{log_lemma_source}
{arcsin_half_lemma_source}
  rw [intervalIntegral.integral_eq_sub_of_hasDerivAt hderiv hint]
  {final_integral_tactic}

#print axioms wolfram_language_compute
"""
    helper_source = "\n".join(
        part for part in (
            csch_helper_source,
            sech_helper_source,
            coth_helper_source,
            tanh_helper_source,
        ) if part
    )
    if helper_source:
        source = source.replace(
            "theorem wolfram_language_compute",
            f"{helper_source}\n\ntheorem wolfram_language_compute",
            1,
        )
    source = source.replace(
        "simp [F, id_eq] <;> ring", hderiv_function_proof, 1
    )
    source = source.replace(
        f"simp [F, id_eq] <;> field_simp{field_simp_arguments} <;> ring",
        hderiv_value_proof,
        1,
    )
    source = source.replace(
        "    fun_prop\n  have hint", f"    {hcont_proof}\n  have hint", 1
    )
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
    coth_arguments = _coth_arguments(integrand_tree.body)
    if coth_arguments is None:
        return None
    csch_arguments = _csch_arguments(integrand_tree.body)
    if csch_arguments is None:
        return None
    csch_affine = _direct_csch_affine_integrand(integrand_tree.body, variable)
    if csch_arguments and csch_affine is None:
        return None
    coth_affine = _direct_coth_affine_integrand(
        integrand_tree.body, variable
    )
    sech_affine = _direct_sech_affine_integrand(integrand_tree.body, variable)

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
    inverse_tanh_integrand_sign = _inverse_hyperbolic_tanh_integrand_sign(
        integrand_tree.body,
        variable,
    )
    if (
        isinstance(integrand_tree.body, ast.Call)
        and isinstance(integrand_tree.body.func, ast.Name)
        and integrand_tree.body.func.id == "tanh"
        and len(integrand_tree.body.args) == 1
        and isinstance(integrand_tree.body.args[0], ast.Name)
        and integrand_tree.body.args[0].id == variable
    ):
        antiderivative = f"log(cosh({variable}))"
    elif inverse_tanh_integrand_sign is not None:
        prefix = "" if inverse_tanh_integrand_sign > 0 else "-"
        antiderivative = f"{prefix}atanh({variable})"
    elif coth_affine is not None:
        argument, slope = coth_affine
        log_sinh = f"log(abs(sinh({ast.unparse(argument)})))"
        antiderivative = (
            log_sinh
            if slope == 1
            else f"({log_sinh})/({_fraction_text(slope)})"
        )
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
    elif sech_affine is not None:
        argument, slope = sech_affine
        sech_antiderivative = f"atan(sinh({ast.unparse(argument)}))"
        antiderivative = (
            sech_antiderivative
            if slope == 1
            else f"({sech_antiderivative})/({_fraction_text(slope)})"
        )
    elif csch_affine is not None:
        argument, slope = csch_affine
        csch_antiderivative = (
            f"log(abs(tanh(({ast.unparse(argument)})/2)))"
        )
        antiderivative = (
            csch_antiderivative
            if slope == 1
            else f"({csch_antiderivative})/({_fraction_text(slope)})"
        )
    if coth_arguments:
        antiderivative = _wrap_coth_log_abs(antiderivative)
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
        *(_sinh_call(argument) for argument in coth_arguments),
        *(_sinh_call(argument) for argument in csch_arguments),
    ]
    domain_hypotheses: dict[str, str] = {}
    positive_hypotheses: dict[str, str] = {}
    domain_binders: list[str] = []
    domain_condition_names: list[str] = []
    domain_conditions: list[str] = []
    used_names = set(integrand_symbols) | set(antiderivative_symbols) | {variable}

    log_base_proofs = _log_base_nonzero_proofs(integrand_tree.body, tree.body)
    if log_base_proofs is None:
        return None
    domain_hypotheses.update(log_base_proofs)
    domain_condition_names.extend(log_base_proofs.values())

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
    inverse_hyperbolic_tanh_arguments = _inverse_hyperbolic_tanh_arguments(
        tree.body
    )
    if inverse_hyperbolic_tanh_arguments is None:
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

    for index, argument in enumerate(inverse_hyperbolic_tanh_arguments):
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
        lower_name = _fresh_identifier(f"h_artanh_lower_{index}", used_names)
        upper_name = _fresh_identifier(f"h_artanh_upper_{index}", used_names)
        domain_hypotheses[
            _inverse_hyperbolic_tanh_hypothesis_key(argument, "lower")
        ] = lower_name
        domain_hypotheses[
            _inverse_hyperbolic_tanh_hypothesis_key(argument, "upper")
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
        elif (
            isinstance(domain_expression, ast.Call)
            and isinstance(domain_expression.func, ast.Name)
            and domain_expression.func.id == "sinh"
        ):
            argument_source = ast.unparse(domain_expression.args[0]).replace("**", "^")
            expression_source = f"Sinh[{argument_source}]"
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
    has_coth = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "coth"
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
    direct_sech_argument_lean = None
    if sech_affine is not None:
        direct_sech_argument_lean = _render_real_calculus_node(
            sech_affine[0],
            set(),
            {"nodes": 0},
            0,
            allow_variable_denominators=True,
        )
        if direct_sech_argument_lean is None:
            return None
    direct_csch_argument_lean = None
    csch_tanh_hypothesis = None
    if csch_affine is not None:
        direct_csch_argument_lean = _render_real_calculus_node(
            csch_affine[0],
            set(),
            {"nodes": 0},
            0,
            allow_variable_denominators=True,
        )
        if direct_csch_argument_lean is None:
            return None
        csch_tanh_arguments = _log_arguments(tree.body)
        if not csch_tanh_arguments:
            return None
        csch_tanh_hypothesis = domain_hypotheses.get(
            ast.dump(csch_tanh_arguments[0], include_attributes=False)
        )
        csch_sinh_hypothesis = domain_hypotheses.get(
            ast.dump(_sinh_call(csch_affine[0]), include_attributes=False)
        )
        csch_argument_derivative_proof = _derivative_proof_term(
            csch_affine[0],
            variable,
            function_variable,
            variable,
            {"nodes": 0},
            0,
            domain_hypotheses=domain_hypotheses,
            positive_hypotheses=positive_hypotheses,
        )
        if (
            csch_tanh_hypothesis is None
            or csch_sinh_hypothesis is None
            or csch_argument_derivative_proof is None
        ):
            return None
        proof = (
            "((wolfram_hasDerivAt_log_tanh_half "
            f"{direct_csch_argument_lean} {csch_tanh_hypothesis} "
            f"{csch_sinh_hypothesis}).comp {variable} "
            f"({csch_argument_derivative_proof})).div_const "
            f"({_lean_rational(csch_affine[1])})"
        )
    if has_coth:
        identities = _coth_identity_terms(integrand_tree.body, tree.body)
        derivative_tactic = (
            f"simp [id_eq] <;> field_simp{field_simp_arguments} <;> "
            f"nlinarith [{', '.join(identities)}]"
        ) if identities else (
            f"simp [id_eq] <;> field_simp{field_simp_arguments} <;> ring"
        )
    elif csch_affine is not None:
        derivative_tactic = f"field_simp{field_simp_arguments} <;> ring"
        function_equality_tactic = (
            f"ext {function_variable}; "
            "simp [Function.comp_def, id_eq] <;> ring"
        )
    elif has_hyperbolic_tangent:
        derivative_tactic = (
            "simp [id_eq, Real.tanh_eq_sinh_div_cosh] <;> "
            f"field_simp{field_simp_arguments} <;> ring"
        )
    elif sech_affine is not None:
        derivative_tactic = (
            f"simp [id_eq] <;> field_simp{field_simp_arguments} <;> "
            f"nlinarith [Real.cosh_sq_sub_sinh_sq {direct_sech_argument_lean}]"
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
    artanh_helper_source = (
        _artanh_derivative_lemma_source()
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "atanh"
            for node in ast.walk(tree.body)
        )
        else ""
    )
    tanh_helper_source = (
        _tanh_derivative_lemma_source() if has_hyperbolic_tangent else ""
    )
    coth_helper_source = (
        _coth_derivative_lemma_source()
        if has_coth
        else ""
    )
    sinh_double_helper_source = (
        _sinh_double_lemma_source() if csch_affine is not None else ""
    )
    csch_primitive_helper_source = (
        _csch_primitive_derivative_lemma_source()
        if csch_affine is not None
        else ""
    )
    source = f"""import Mathlib.Analysis.Calculus.Deriv.Add
import Mathlib.Analysis.Calculus.Deriv.Mul
import Mathlib.Analysis.Calculus.Deriv.Pow
import Mathlib.Analysis.Calculus.Deriv.Inv
import Mathlib.Analysis.SpecialFunctions.ExpDeriv
import Mathlib.Analysis.SpecialFunctions.Log.Deriv
import Mathlib.Analysis.SpecialFunctions.Arsinh
import Mathlib.Analysis.SpecialFunctions.Arcosh
import Mathlib.Analysis.SpecialFunctions.Artanh
import Mathlib.Analysis.SpecialFunctions.Sqrt
import Mathlib.Topology.Basic
import Mathlib.Analysis.SpecialFunctions.Trigonometric.ArctanDeriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.Deriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.DerivHyp
import Mathlib.Analysis.SpecialFunctions.Trigonometric.InverseDeriv
import Mathlib.Tactic.FieldSimp
import Mathlib.Tactic.Linarith
import Mathlib.Tactic.Ring

{artanh_helper_source}
{tanh_helper_source}
{coth_helper_source}
{sinh_double_helper_source}
{csch_primitive_helper_source}

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
    inverse_hyperbolic_tanh_arguments = _inverse_hyperbolic_tanh_arguments(
        tree.body
    )
    if inverse_hyperbolic_tanh_arguments is None:
        return None
    coth_arguments = _coth_arguments(tree.body)
    if coth_arguments is None:
        return None
    csch_arguments = _csch_arguments(tree.body)
    if csch_arguments is None:
        return None
    sech_arguments = _sech_arguments(tree.body)

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
    domain_denominators = [
        *_variable_denominators(tree.body),
        *(_sinh_call(argument) for argument in coth_arguments),
        *(_sinh_call(argument) for argument in csch_arguments),
    ]
    domain_hypotheses: dict[str, str] = {}
    positive_hypotheses: dict[str, str] = {}
    domain_binders: list[str] = []
    domain_condition_names: list[str] = []
    domain_conditions: list[str] = []
    log_base_proofs = _log_base_nonzero_proofs(tree.body)
    if log_base_proofs is None:
        return None
    domain_hypotheses.update(log_base_proofs)
    domain_condition_names.extend(log_base_proofs.values())
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
        elif (
            isinstance(denominator, ast.Call)
            and isinstance(denominator.func, ast.Name)
            and denominator.func.id == "sinh"
        ):
            argument_source = ast.unparse(denominator.args[0]).replace("**", "^")
            denominator_source = f"Sinh[{argument_source}]"
        else:
            denominator_source = ast.unparse(denominator).replace("**", "^")
        domain_conditions.append(f"{denominator_source} ≠ 0")

    for argument in sech_arguments:
        argument_lean = _render_real_calculus_node(
            argument,
            set(),
            {"nodes": 0},
            0,
            allow_variable_denominators=True,
        )
        if argument_lean is None:
            return None
        cosh_nonzero_proof = f"(ne_of_gt (Real.cosh_pos {argument_lean}))"
        domain_hypotheses[
            ast.dump(_cosh_call(argument), include_attributes=False)
        ] = cosh_nonzero_proof
        domain_condition_names.append(cosh_nonzero_proof)

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

    for index, argument in enumerate(inverse_hyperbolic_tanh_arguments):
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
        lower_name = _fresh_identifier(f"h_artanh_lower_{index}", used_names)
        upper_name = _fresh_identifier(f"h_artanh_upper_{index}", used_names)
        domain_hypotheses[
            _inverse_hyperbolic_tanh_hypothesis_key(argument, "lower")
        ] = lower_name
        domain_hypotheses[
            _inverse_hyperbolic_tanh_hypothesis_key(argument, "upper")
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
    has_coth_derivative = bool(coth_arguments)
    has_csch_derivative = bool(csch_arguments)
    has_sech_derivative = bool(sech_arguments)
    if has_coth_derivative:
        identity_terms = _coth_identity_terms(tree.body)
        derivative_tactic = (
            f"simp [id_eq] <;> field_simp{field_simp_arguments} <;> "
            f"nlinarith [{', '.join(identity_terms)}]"
        )
    elif has_sech_derivative:
        derivative_tactic = (
            "simp [id_eq, Real.tanh_eq_sinh_div_cosh] <;> "
            f"field_simp{field_simp_arguments} <;> ring"
        )
    elif has_tangent_derivative:
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
    artanh_helper_source = (
        _artanh_derivative_lemma_source()
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "atanh"
            for node in ast.walk(tree.body)
        )
        else ""
    )
    coth_helper_source = (
        _coth_derivative_lemma_source()
        if has_coth_derivative
        else ""
    )
    csch_helper_source = (
        _csch_derivative_lemma_source()
        if has_csch_derivative
        else ""
    )
    sech_helper_source = (
        _sech_derivative_lemma_source()
        if has_sech_derivative
        else ""
    )
    source = f"""import Mathlib.Analysis.Calculus.Deriv.Add
import Mathlib.Analysis.Calculus.Deriv.Mul
import Mathlib.Analysis.Calculus.Deriv.Pow
import Mathlib.Analysis.Calculus.Deriv.Inv
import Mathlib.Analysis.SpecialFunctions.ExpDeriv
import Mathlib.Analysis.SpecialFunctions.Log.Deriv
import Mathlib.Analysis.SpecialFunctions.Arsinh
import Mathlib.Analysis.SpecialFunctions.Arcosh
import Mathlib.Analysis.SpecialFunctions.Artanh
import Mathlib.Analysis.SpecialFunctions.Sqrt
import Mathlib.Topology.Basic
import Mathlib.Analysis.SpecialFunctions.Trigonometric.ArctanDeriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.Deriv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.DerivHyp
import Mathlib.Analysis.SpecialFunctions.Trigonometric.InverseDeriv
import Mathlib.Tactic.FieldSimp
import Mathlib.Tactic.Linarith
import Mathlib.Tactic.Ring

{tanh_helper_source}
{artanh_helper_source}
{coth_helper_source}
{csch_helper_source}
{sech_helper_source}

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


def prepare_coordinate_divergence(
    components: list[str],
    coordinate_system: str,
    derivative_terms: list[str],
    expected_result: str,
) -> Computation | None:
    """Prepare a Lean theorem for a selected polynomial 3D divergence chart."""
    if (
        len(components) != 3
        or len(derivative_terms) != 3
        or coordinate_system not in {"Cartesian3D", "Cylindrical3D", "Spherical3D"}
    ):
        return None

    if coordinate_system == "Cartesian3D":
        derivative_sources = list(zip(components, ("x", "y", "z"), derivative_terms, strict=True))
        coordinate_variables = {"x", "y", "z"}
        domain_conditions: tuple[str, ...] = ()
    elif coordinate_system == "Cylindrical3D":
        derivative_sources = [
            (f"rho*({components[0]})", "rho", derivative_terms[0]),
            (components[1], "theta", derivative_terms[1]),
            (components[2], "z", derivative_terms[2]),
        ]
        coordinate_variables = {"rho", "theta", "z"}
        domain_conditions = ("rho > 0",)
    else:
        derivative_sources = [
            (f"r^2*({components[0]})", "r", derivative_terms[0]),
            (f"sin(theta)*({components[1]})", "theta", derivative_terms[1]),
            (components[2], "phi", derivative_terms[2]),
        ]
        coordinate_variables = {"r", "theta", "phi"}
        domain_conditions = ("r > 0", "sin(theta) != 0")

    rendered_derivatives: list[str] = []
    derivative_proofs: list[str] = []
    used_names = set(coordinate_variables)
    for index, (expression_source, variable, derivative_source) in enumerate(derivative_sources):
        exact_expression = _normalize_decimal_literals(expression_source)
        exact_derivative = _normalize_decimal_literals(derivative_source)
        if exact_expression is None or exact_derivative is None:
            return None
        try:
            parsed_expression = parse_math_input(exact_expression)
            parsed_derivative = parse_math_input(exact_derivative)
            expression_tree = ast.parse(parsed_expression.expression, mode="eval")
        except (MathInputError, SyntaxError, ValueError, RecursionError):
            return None
        if any(
            isinstance(node, ast.Call)
            and not (
                coordinate_system == "Spherical3D"
                and isinstance(node.func, ast.Name)
                and node.func.id == "sin"
                and len(node.args) == 1
                and not node.keywords
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "theta"
            )
            for node in ast.walk(expression_tree)
        ):
            return None

        expression_names: set[str] = set()
        rendered_function = _render_real_calculus_node(
            expression_tree.body,
            expression_names,
            {"nodes": 0},
            0,
            substitutions={variable: f"coordinate_deriv_var_{index}"},
            allow_variable_denominators=True,
        )
        derivative_lean, derivative_names = _render_real_calculus_lean(
            parsed_derivative.expression,
            allow_variable_denominators=True,
        )
        function_variable = f"coordinate_deriv_var_{index}"
        if (
            rendered_function is None
            or derivative_lean is None
            or function_variable in expression_names
        ):
            return None
        proof = _derivative_proof_term(
            expression_tree.body,
            variable,
            function_variable,
            variable,
            {"nodes": 0},
            0,
        )
        if proof is None:
            return None

        derivative_function = f"(fun {function_variable} : ℝ => {rendered_function})"
        rendered_derivatives.append(f"(deriv {derivative_function} {variable})")
        derivative_proofs.append(
            f"  have h_coordinate_derivative_{index} : HasDerivAt {derivative_function} "
            f"{derivative_lean} {variable} := by\n"
            f"    convert ({proof}) using 1\n"
            f"    all_goals\n"
            f"      first\n"
            f"      | ext {function_variable}; simp [id_eq] <;> ring\n"
            f"      | simp [id_eq] <;> ring"
        )
        used_names.update(expression_names)
        used_names.update(derivative_names)

    expected_exact = _normalize_decimal_literals(expected_result)
    if expected_exact is None:
        return None
    try:
        parsed_expected = parse_math_input(expected_exact)
    except MathInputError:
        return None
    expected_lean, expected_names = _render_real_calculus_lean(
        parsed_expected.expression,
        allow_variable_denominators=True,
    )
    if expected_lean is None:
        return None
    used_names.update(expected_names)
    used_names.update(coordinate_variables)
    if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name) for name in used_names):
        return None

    binders = " ".join(f"({name} : ℝ)" for name in sorted(used_names))
    if coordinate_system == "Cylindrical3D":
        binders = f"{binders} (h_rho : 0 < rho)"
    elif coordinate_system == "Spherical3D":
        binders = (
            f"{binders} (h_spherical_radius : 0 < r) "
            "(h_spherical_sine : Real.sin theta ≠ 0)"
        )
    operator_terms = (
        " + ".join(rendered_derivatives)
        if coordinate_system == "Cartesian3D"
        else f"({rendered_derivatives[0]}) / rho + "
        f"({rendered_derivatives[1]}) / rho + {rendered_derivatives[2]}"
    )
    if coordinate_system == "Spherical3D":
        operator_terms = (
            f"({rendered_derivatives[0]}) / (r ^ 2) + "
            f"({rendered_derivatives[1]}) / (r * Real.sin theta) + "
            f"({rendered_derivatives[2]}) / (r * Real.sin theta)"
        )
    imports = """import Mathlib.Analysis.Calculus.Deriv.Add
import Mathlib.Analysis.Calculus.Deriv.Mul
import Mathlib.Analysis.Calculus.Deriv.Pow
import Mathlib.Analysis.Calculus.Deriv.Inv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.Deriv
import Mathlib.Tactic.FieldSimp
import Mathlib.Tactic.Ring"""
    final_tactic = (
        "field_simp [ne_of_gt h_rho] <;> ring"
        if coordinate_system == "Cylindrical3D"
        else "ring"
    )
    if coordinate_system == "Spherical3D":
        final_tactic = (
            "field_simp [ne_of_gt h_spherical_radius, h_spherical_sine] <;> ring"
        )
    theorem_name = "coordinate_divergence"
    source = (
        f"{imports}\n\n"
        f"theorem {theorem_name} {binders} :\n"
        f"  ({operator_terms}) = {expected_lean} := by\n"
        + "\n".join(derivative_proofs)
        + "\n  rw ["
        + ", ".join(
            f"h_coordinate_derivative_{index}.deriv" for index in range(3)
        )
        + f"]\n  {final_tactic}\n\n#print axioms {theorem_name}\n"
    )
    return Computation(
        expected_result.replace("**", "^"),
        source,
        domain_conditions=domain_conditions,
        real_parameters=tuple(sorted(used_names - coordinate_variables)),
    )


def prepare_coordinate_gradient(
    expression: str,
    coordinate_system: str,
    derivative_terms: list[str],
    expected_components: list[str],
) -> Computation | None:
    """Prepare a Lean theorem for a selected polynomial 3D gradient chart."""
    if (
        len(derivative_terms) != 3
        or len(expected_components) != 3
        or coordinate_system not in {"Cartesian3D", "Cylindrical3D", "Spherical3D"}
    ):
        return None

    coordinate_names = {
        "Cartesian3D": ("x", "y", "z"),
        "Cylindrical3D": ("rho", "theta", "z"),
        "Spherical3D": ("r", "theta", "phi"),
    }[coordinate_system]
    coordinate_variables = set(coordinate_names)
    domain_conditions: tuple[str, ...] = (
        ("rho > 0",)
        if coordinate_system == "Cylindrical3D"
        else ("r > 0", "sin(theta) != 0")
        if coordinate_system == "Spherical3D"
        else ()
    )

    exact_expression = _normalize_decimal_literals(expression)
    if exact_expression is None:
        return None
    try:
        parsed_expression = parse_math_input(exact_expression)
        expression_tree = ast.parse(parsed_expression.expression, mode="eval")
    except (MathInputError, SyntaxError, ValueError, RecursionError):
        return None
    if any(isinstance(node, ast.Call) for node in ast.walk(expression_tree)):
        return None

    used_names = set(coordinate_variables)
    rendered_derivatives: list[str] = []
    derivative_proofs: list[str] = []
    for index, (variable, derivative_source) in enumerate(
        zip(coordinate_names, derivative_terms, strict=True)
    ):
        exact_derivative = _normalize_decimal_literals(derivative_source)
        if exact_derivative is None:
            return None
        try:
            parsed_derivative = parse_math_input(exact_derivative)
        except MathInputError:
            return None

        expression_names: set[str] = set()
        function_variable = f"coordinate_gradient_var_{index}"
        rendered_function = _render_real_calculus_node(
            expression_tree.body,
            expression_names,
            {"nodes": 0},
            0,
            substitutions={variable: function_variable},
            allow_variable_denominators=True,
        )
        derivative_lean, derivative_names = _render_real_calculus_lean(
            parsed_derivative.expression,
            allow_variable_denominators=True,
        )
        if (
            rendered_function is None
            or derivative_lean is None
            or function_variable in expression_names
        ):
            return None
        proof = _derivative_proof_term(
            expression_tree.body,
            variable,
            function_variable,
            variable,
            {"nodes": 0},
            0,
        )
        if proof is None:
            return None

        derivative_function = f"(fun {function_variable} : ℝ => {rendered_function})"
        rendered_derivatives.append(f"(deriv {derivative_function} {variable})")
        derivative_proofs.append(
            f"  have h_coordinate_gradient_{index} : HasDerivAt {derivative_function} "
            f"{derivative_lean} {variable} := by\n"
            f"    convert ({proof}) using 1\n"
            "    all_goals\n"
            "      first\n"
            f"      | ext {function_variable}; simp [id_eq] <;> ring\n"
            "      | simp [id_eq] <;> ring"
        )
        used_names.update(expression_names)
        used_names.update(derivative_names)

    rendered_components: list[str] = []
    for component in expected_components:
        exact_component = _normalize_decimal_literals(component)
        if exact_component is None:
            return None
        component_lean, component_names = _render_real_calculus_lean(
            exact_component,
            allow_variable_denominators=True,
        )
        if component_lean is None:
            return None
        rendered_components.append(component_lean)
        used_names.update(component_names)

    used_names.update(coordinate_variables)
    if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name) for name in used_names):
        return None

    binders = " ".join(f"({name} : ℝ)" for name in sorted(used_names))
    if coordinate_system == "Cylindrical3D":
        binders = f"{binders} (h_rho : 0 < rho)"
        gradient_terms = [
            rendered_derivatives[0],
            f"({rendered_derivatives[1]}) / rho",
            rendered_derivatives[2],
        ]
    elif coordinate_system == "Spherical3D":
        binders = (
            f"{binders} (h_spherical_radius : 0 < r) "
            "(h_spherical_sine : Real.sin theta ≠ 0)"
        )
        gradient_terms = [
            rendered_derivatives[0],
            f"({rendered_derivatives[1]}) / r",
            f"({rendered_derivatives[2]}) / (r * Real.sin theta)",
        ]
    else:
        gradient_terms = rendered_derivatives

    actual_tuple = f"(({gradient_terms[0]}, {gradient_terms[1]}), {gradient_terms[2]})"
    expected_tuple = (
        f"(({rendered_components[0]}, {rendered_components[1]}), "
        f"{rendered_components[2]})"
    )
    final_tactic = (
        "field_simp [ne_of_gt h_rho] <;> ring"
        if coordinate_system == "Cylindrical3D"
        else "field_simp [ne_of_gt h_spherical_radius, h_spherical_sine] <;> ring"
        if coordinate_system == "Spherical3D"
        else "all_goals ring"
    )
    imports = """import Mathlib.Analysis.Calculus.Deriv.Add
import Mathlib.Analysis.Calculus.Deriv.Mul
import Mathlib.Analysis.Calculus.Deriv.Pow
import Mathlib.Analysis.Calculus.Deriv.Inv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.Deriv
import Mathlib.Tactic.FieldSimp
import Mathlib.Tactic.Ring"""
    theorem_name = "coordinate_gradient"
    source = (
        f"{imports}\n\n"
        f"theorem {theorem_name} {binders} :\n"
        f"  {actual_tuple} = {expected_tuple} := by\n"
        + "\n".join(derivative_proofs)
        + "\n  rw ["
        + ", ".join(
            f"h_coordinate_gradient_{index}.deriv" for index in range(3)
        )
        + f"]\n  {final_tactic}\n\n#print axioms {theorem_name}\n"
    )
    display_result = f"({', '.join(component.replace('**', '^') for component in expected_components)})"
    return Computation(
        display_result,
        source,
        domain_conditions=domain_conditions,
        real_parameters=tuple(sorted(used_names - coordinate_variables)),
        proof_result_authoritative=True,
    )


def prepare_coordinate_curl(
    components: list[str],
    coordinate_system: str,
    derivative_terms: list[str],
    expected_components: list[str],
) -> Computation | None:
    """Prepare a Lean theorem for a polynomial curl in one selected 3D chart."""
    if (
        len(components) != 3
        or len(derivative_terms) != 6
        or len(expected_components) != 3
        or coordinate_system not in {"Cartesian3D", "Cylindrical3D", "Spherical3D"}
    ):
        return None

    if coordinate_system == "Cartesian3D":
        derivative_sources = [
            (components[2], "y", derivative_terms[0]),
            (components[1], "z", derivative_terms[1]),
            (components[0], "z", derivative_terms[2]),
            (components[2], "x", derivative_terms[3]),
            (components[1], "x", derivative_terms[4]),
            (components[0], "y", derivative_terms[5]),
        ]
        coordinate_variables = {"x", "y", "z"}
        domain_conditions: tuple[str, ...] = ()
    elif coordinate_system == "Cylindrical3D":
        derivative_sources = [
            (components[2], "theta", derivative_terms[0]),
            (f"rho*({components[1]})", "z", derivative_terms[1]),
            (components[0], "z", derivative_terms[2]),
            (components[2], "rho", derivative_terms[3]),
            (f"rho*({components[1]})", "rho", derivative_terms[4]),
            (components[0], "theta", derivative_terms[5]),
        ]
        coordinate_variables = {"rho", "theta", "z"}
        domain_conditions = ("rho > 0",)
    else:
        derivative_sources = [
            (f"sin(theta)*({components[2]})", "theta", derivative_terms[0]),
            (components[1], "phi", derivative_terms[1]),
            (components[0], "phi", derivative_terms[2]),
            (f"r*({components[2]})", "r", derivative_terms[3]),
            (f"r*({components[1]})", "r", derivative_terms[4]),
            (components[0], "theta", derivative_terms[5]),
        ]
        coordinate_variables = {"r", "theta", "phi"}
        domain_conditions = ("r > 0", "sin(theta) != 0")

    used_names = set(coordinate_variables)
    rendered_derivatives: list[str] = []
    derivative_proofs: list[str] = []
    for index, (expression_source, variable, derivative_source) in enumerate(derivative_sources):
        exact_expression = _normalize_decimal_literals(expression_source)
        exact_derivative = _normalize_decimal_literals(derivative_source)
        if exact_expression is None or exact_derivative is None:
            return None
        try:
            parsed_expression = parse_math_input(exact_expression)
            parsed_derivative = parse_math_input(exact_derivative)
            expression_tree = ast.parse(parsed_expression.expression, mode="eval")
        except (MathInputError, SyntaxError, ValueError, RecursionError):
            return None
        if any(
            isinstance(node, ast.Call)
            and not (
                coordinate_system == "Spherical3D"
                and isinstance(node.func, ast.Name)
                and node.func.id == "sin"
                and len(node.args) == 1
                and not node.keywords
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "theta"
            )
            for node in ast.walk(expression_tree)
        ):
            return None

        expression_names: set[str] = set()
        function_variable = f"coordinate_curl_var_{index}"
        rendered_function = _render_real_calculus_node(
            expression_tree.body,
            expression_names,
            {"nodes": 0},
            0,
            substitutions={variable: function_variable},
            allow_variable_denominators=True,
        )
        derivative_lean, derivative_names = _render_real_calculus_lean(
            parsed_derivative.expression,
            allow_variable_denominators=True,
        )
        if (
            rendered_function is None
            or derivative_lean is None
            or function_variable in expression_names
        ):
            return None
        proof = _derivative_proof_term(
            expression_tree.body,
            variable,
            function_variable,
            variable,
            {"nodes": 0},
            0,
        )
        if proof is None:
            return None
        derivative_function = f"(fun {function_variable} : ℝ => {rendered_function})"
        rendered_derivatives.append(f"(deriv {derivative_function} {variable})")
        derivative_proofs.append(
            f"  have h_coordinate_curl_{index} : HasDerivAt {derivative_function} "
            f"{derivative_lean} {variable} := by\n"
            f"    convert ({proof}) using 1\n"
            "    all_goals\n"
            "      first\n"
            f"      | ext {function_variable}; simp [id_eq] <;> ring\n"
            "      | simp [id_eq] <;> ring"
        )
        used_names.update(expression_names)
        used_names.update(derivative_names)

    rendered_components: list[str] = []
    for component in expected_components:
        exact_component = _normalize_decimal_literals(component)
        if exact_component is None:
            return None
        component_lean, component_names = _render_real_calculus_lean(
            exact_component,
            allow_variable_denominators=True,
        )
        if component_lean is None:
            return None
        rendered_components.append(component_lean)
        used_names.update(component_names)

    used_names.update(coordinate_variables)
    if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name) for name in used_names):
        return None
    binders = " ".join(f"({name} : ℝ)" for name in sorted(used_names))

    if coordinate_system == "Cartesian3D":
        curl_terms = [
            f"({rendered_derivatives[0]}) - ({rendered_derivatives[1]})",
            f"({rendered_derivatives[2]}) - ({rendered_derivatives[3]})",
            f"({rendered_derivatives[4]}) - ({rendered_derivatives[5]})",
        ]
        final_tactic = "all_goals ring"
    elif coordinate_system == "Cylindrical3D":
        binders = f"{binders} (h_rho : 0 < rho)"
        curl_terms = [
            f"(({rendered_derivatives[0]}) - ({rendered_derivatives[1]})) / rho",
            f"({rendered_derivatives[2]}) - ({rendered_derivatives[3]})",
            f"(({rendered_derivatives[4]}) - ({rendered_derivatives[5]})) / rho",
        ]
        final_tactic = "all_goals (field_simp [ne_of_gt h_rho] <;> ring)"
    else:
        binders = (
            f"{binders} (h_spherical_radius : 0 < r) "
            "(h_spherical_sine : Real.sin theta ≠ 0)"
        )
        curl_terms = [
            f"(({rendered_derivatives[0]}) - ({rendered_derivatives[1]})) / "
            "(r * Real.sin theta)",
            f"(({rendered_derivatives[2]}) - Real.sin theta * "
            f"({rendered_derivatives[3]})) / (r * Real.sin theta)",
            f"(({rendered_derivatives[4]}) - ({rendered_derivatives[5]})) / r",
        ]
        final_tactic = (
            "apply Prod.ext\n"
            "  · apply Prod.ext\n"
            "    · field_simp [ne_of_gt h_spherical_radius, h_spherical_sine] <;> ring\n"
            "    · field_simp [ne_of_gt h_spherical_radius, h_spherical_sine] <;> ring\n"
            "  · field_simp [ne_of_gt h_spherical_radius, h_spherical_sine] <;> ring"
        )

    actual_tuple = f"(({curl_terms[0]}, {curl_terms[1]}), {curl_terms[2]})"
    expected_tuple = (
        f"(({rendered_components[0]}, {rendered_components[1]}), "
        f"{rendered_components[2]})"
    )
    imports = """import Mathlib.Analysis.Calculus.Deriv.Add
import Mathlib.Analysis.Calculus.Deriv.Mul
import Mathlib.Analysis.Calculus.Deriv.Pow
import Mathlib.Analysis.Calculus.Deriv.Inv
import Mathlib.Analysis.SpecialFunctions.Trigonometric.Deriv
import Mathlib.Tactic.FieldSimp
import Mathlib.Tactic.Ring"""
    source = (
        f"{imports}\n\n"
        f"theorem coordinate_curl {binders} :\n"
        f"  {actual_tuple} = {expected_tuple} := by\n"
        + "\n".join(derivative_proofs)
        + "\n  simp only ["
        + ", ".join(f"h_coordinate_curl_{index}.deriv" for index in range(6))
        + f"]\n  {final_tactic}\n\n#print axioms coordinate_curl\n"
    )
    display_result = f"({', '.join(component.replace('**', '^') for component in expected_components)})"
    return Computation(
        display_result,
        source,
        domain_conditions=domain_conditions,
        real_parameters=tuple(sorted(used_names - coordinate_variables)),
        proof_result_authoritative=True,
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
    if lhs_degree is None or rhs_degree is None or max(lhs_degree, rhs_degree) > 8:
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
    if polynomial.is_zero or polynomial.degree() > 8:
        return None
    coefficient_bit_limit = 128 if polynomial.degree() > 4 else 256
    for coefficient in polynomial.all_coeffs():
        if (
            abs(int(coefficient.p)).bit_length() > coefficient_bit_limit
            or int(coefficient.q).bit_length() > coefficient_bit_limit
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
            # Above quadratics, only return a solution set when SymPy found every
            # real root as an exact rational. Lean checks the resulting set
            # against the original polynomial before it is returned.
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
    normalized = re.sub(r"\bPi\b", "pi", source)
    for _ in range(64):
        def normalize_function(match: re.Match[str]) -> str:
            function_name = match.group("function").lower()
            function_name = {
                "arcsin": "asin",
                "arccos": "acos",
                "arctan": "atan",
                "arcsinh": "asinh",
                "arccosh": "acosh",
                "arctanh": "atanh",
            }.get(function_name, function_name)
            argument = match.group("argument")
            if function_name == "log":
                parts = _split_wolfram_arguments(argument)
                if parts is not None and len(parts) == 2:
                    try:
                        base = parse_math_input(parts[0]).expression
                        logarithm_argument = parse_math_input(parts[1]).expression
                    except MathInputError:
                        return match.group(0)
                    return f"log({logarithm_argument},{base})"
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


def _split_wolfram_arguments(source: str) -> list[str] | None:
    pairs = {"(": ")", "[": "]", "{": "}"}
    closing = {value: key for key, value in pairs.items()}
    stack: list[str] = []
    parts: list[str] = []
    start = 0
    for index, character in enumerate(source):
        if character in pairs:
            stack.append(character)
        elif character in closing:
            if not stack or stack.pop() != closing[character]:
                return None
        elif character == "," and not stack:
            parts.append(source[start:index].strip())
            start = index + 1
    if stack:
        return None
    parts.append(source[start:].strip())
    return parts if len(parts) > 1 else None


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
            or len(node.args) not in {1, 2}
            or node.keywords
        ):
            continue
        if len(node.args) == 2:
            base = _constant_fraction(node.args[1])
            if base is None or base <= 0 or base == 1:
                return None
        raw_argument = node.args[0]
        argument = _unwrap_real_abs(raw_argument)
        constant = _constant_fraction(argument)
        is_absolute_log = argument is not raw_argument
        if is_absolute_log:
            if len(node.args) == 2:
                return None
            if constant == 0:
                return None
            continue
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
            or len(node.args) not in {1, 2}
            or node.keywords
        ):
            continue
        argument = _unwrap_real_abs(node.args[0])
        key = ast.dump(argument, include_attributes=False)
        if key not in seen:
            seen.add(key)
            arguments.append(argument)
    return arguments


def _log_base_call(base: ast.AST) -> ast.Call:
    return ast.Call(
        func=ast.Name(id="log", ctx=ast.Load()),
        args=[base],
        keywords=[],
    )


def _log_base_nonzero_proofs(*roots: ast.AST) -> dict[str, str] | None:
    proofs: dict[str, str] = {}
    for root in roots:
        for node in ast.walk(root):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Name)
                or node.func.id != "log"
                or len(node.args) != 2
                or node.keywords
            ):
                continue
            base = node.args[1]
            base_fraction = _constant_fraction(base)
            if base_fraction is None or base_fraction <= 0 or base_fraction == 1:
                return None
            base_lean = _render_real_calculus_node(
                base,
                set(),
                {"nodes": 0},
                0,
                allow_variable_denominators=True,
            )
            if base_lean is None:
                return None
            proofs[ast.dump(_log_base_call(base), include_attributes=False)] = (
                "(Real.log_ne_zero_of_pos_of_ne_one "
                f"(by norm_num : 0 < ({base_lean} : ℝ)) "
                f"(by norm_num : ({base_lean} : ℝ) ≠ 1))"
            )
    return proofs


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


def _inverse_hyperbolic_tanh_arguments(root: ast.AST) -> list[ast.AST] | None:
    import sympy as _sympy

    arguments: list[ast.AST] = []
    seen: set[str] = set()
    for node in ast.walk(root):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Name)
            or node.func.id != "atanh"
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
                or _sympy.simplify(value > -1) is not _sympy.true
                or _sympy.simplify(value < 1) is not _sympy.true
            ):
                return None
            continue
        key = ast.dump(argument, include_attributes=False)
        if key not in seen:
            seen.add(key)
            arguments.append(argument)
    return arguments


def _inverse_hyperbolic_tanh_hypothesis_key(
    argument: ast.AST,
    endpoint: str,
) -> str:
    return (
        f"{ast.dump(argument, include_attributes=False)}:"
        f"inverse-hyperbolic-tanh:{endpoint}"
    )


def _inverse_hyperbolic_tanh_integrand_sign(
    integrand: ast.AST,
    variable: str,
) -> int | None:
    import sympy as _sympy

    expression = _real_sympy_expression(integrand)
    if expression is None:
        return None
    symbol = _sympy.Symbol(variable)
    if expression.free_symbols - {symbol}:
        return None
    reciprocal_derivative = 1 / (1 - symbol**2)
    if _sympy.simplify(expression - reciprocal_derivative) == 0:
        return 1
    if _sympy.simplify(expression + reciprocal_derivative) == 0:
        return -1
    return None


def _artanh_derivative_lemma_source() -> str:
    return """theorem wolfram_hasDerivAt_artanh {x : ℝ}
    (h_lower : -1 < x) (h_upper : x < 1) :
    HasDerivAt Real.artanh (1 / (1 - x ^ 2)) x := by
  have h_den : 1 - x ≠ 0 := by linarith
  have h_num : 1 + x ≠ 0 := by linarith
  have h_sq : 1 - x ^ 2 ≠ 0 := by
    have h_lower_pos : 0 < 1 + x := by linarith
    have h_upper_pos : 0 < 1 - x := by linarith
    have h_product : 0 < (1 - x) * (1 + x) := mul_pos h_upper_pos h_lower_pos
    nlinarith [h_product]
  have hq : HasDerivAt (fun y : ℝ => (1 + y) / (1 - y))
      (((1 : ℝ) - x + (1 + x)) / (1 - x) ^ 2) x := by
    convert ((hasDerivAt_const x (1 : ℝ)).add (hasDerivAt_id x)).div
      ((hasDerivAt_const x (1 : ℝ)).sub (hasDerivAt_id x)) h_den using 1
    · ext y
      rfl
    · simp only [Pi.add_apply, Pi.sub_apply, id_eq]
      field_simp [h_den] <;> ring
  have hq_ne : (1 + x) / (1 - x) ≠ 0 := div_ne_zero h_num h_den
  have hscaled := HasDerivAt.const_mul (1 / 2 : ℝ) (hq.log hq_ne)
  have hscaled' : HasDerivAt
      (fun y : ℝ => (1 / 2 : ℝ) * Real.log ((1 + y) / (1 - y)))
      (1 / (1 - x ^ 2)) x := by
    convert hscaled using 1 <;> simp <;> field_simp [h_den, h_num, h_sq] <;> ring
  have hlocal : (fun y : ℝ => Real.artanh y) =ᶠ[nhds x]
      (fun y => (1 / 2 : ℝ) * Real.log ((1 + y) / (1 - y))) := by
    filter_upwards [isOpen_Ioo.mem_nhds ⟨h_lower, h_upper⟩] with y hy
    have hycc : y ∈ Set.Icc (-1) 1 := ⟨le_of_lt hy.1, le_of_lt hy.2⟩
    simp [Real.artanh_eq_half_log hycc]
  exact hscaled'.congr_of_eventuallyEq hlocal

"""


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


def _sinh_call(argument: ast.AST) -> ast.Call:
    return ast.Call(
        func=ast.Name(id="sinh", ctx=ast.Load()),
        args=[argument],
        keywords=[],
    )


def _cosh_call(argument: ast.AST) -> ast.Call:
    return ast.Call(
        func=ast.Name(id="cosh", ctx=ast.Load()),
        args=[argument],
        keywords=[],
    )


def _coth_arguments(root: ast.AST) -> list[ast.AST] | None:
    import sympy as _sympy

    arguments: list[ast.AST] = []
    seen: set[str] = set()
    for node in ast.walk(root):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Name)
            or node.func.id != "coth"
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
                or _sympy.simplify(_sympy.sinh(value) != 0) is not _sympy.true
            ):
                return None
            continue
        key = ast.dump(argument, include_attributes=False)
        if key not in seen:
            seen.add(key)
            arguments.append(argument)
    return arguments


def _csch_arguments(root: ast.AST) -> list[ast.AST] | None:
    import sympy as _sympy

    arguments: list[ast.AST] = []
    seen: set[str] = set()
    for node in ast.walk(root):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Name)
            or node.func.id != "csch"
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
                or _sympy.simplify(_sympy.sinh(value) != 0) is not _sympy.true
            ):
                return None
            continue
        key = ast.dump(argument, include_attributes=False)
        if key not in seen:
            seen.add(key)
            arguments.append(argument)
    return arguments


def _sech_arguments(root: ast.AST) -> list[ast.AST]:
    arguments: list[ast.AST] = []
    seen: set[str] = set()
    for node in ast.walk(root):
        if (
            not isinstance(node, ast.Call)
            or not isinstance(node.func, ast.Name)
            or node.func.id != "sech"
            or len(node.args) != 1
            or node.keywords
        ):
            continue
        argument = node.args[0]
        if not _contains_calculus_variable(argument):
            continue
        key = ast.dump(argument, include_attributes=False)
        if key not in seen:
            seen.add(key)
            arguments.append(argument)
    return arguments


def _direct_coth_affine_integrand(
    root: ast.AST,
    variable: str,
) -> tuple[ast.AST, Fraction] | None:
    import sympy as _sympy

    if (
        not isinstance(root, ast.Call)
        or not isinstance(root.func, ast.Name)
        or root.func.id != "coth"
        or len(root.args) != 1
        or root.keywords
    ):
        return None
    argument = root.args[0]
    expression = _real_sympy_expression(argument)
    symbol = _sympy.Symbol(variable)
    if expression is None or expression.free_symbols - {symbol}:
        return None
    try:
        polynomial = _sympy.Poly(expression, symbol, domain=_sympy.QQ)
    except (TypeError, ValueError, _sympy.PolynomialError):
        return None
    if polynomial.degree() > 1:
        return None
    coefficient = polynomial.coeff_monomial(symbol)
    if not isinstance(coefficient, _sympy.Rational) or coefficient == 0:
        return None
    slope = Fraction(int(coefficient.p), int(coefficient.q))
    return argument, slope


def _direct_tanh_affine_integrand(
    root: ast.AST,
    variable: str,
) -> tuple[ast.AST, Fraction] | None:
    import sympy as _sympy

    if (
        not isinstance(root, ast.Call)
        or not isinstance(root.func, ast.Name)
        or root.func.id != "tanh"
        or len(root.args) != 1
        or root.keywords
    ):
        return None
    argument = root.args[0]
    expression = _real_sympy_expression(argument)
    symbol = _sympy.Symbol(variable)
    if expression is None or expression.free_symbols - {symbol}:
        return None
    try:
        polynomial = _sympy.Poly(expression, symbol, domain=_sympy.QQ)
    except (TypeError, ValueError, _sympy.PolynomialError):
        return None
    if polynomial.degree() > 1:
        return None
    coefficient = polynomial.coeff_monomial(symbol)
    if not isinstance(coefficient, _sympy.Rational) or coefficient == 0:
        return None
    slope = Fraction(int(coefficient.p), int(coefficient.q))
    return argument, slope


def _direct_sech_affine_integrand(
    root: ast.AST,
    variable: str,
) -> tuple[ast.AST, Fraction] | None:
    import sympy as _sympy

    if (
        not isinstance(root, ast.Call)
        or not isinstance(root.func, ast.Name)
        or root.func.id != "sech"
        or len(root.args) != 1
        or root.keywords
    ):
        return None
    argument = root.args[0]
    expression = _real_sympy_expression(argument)
    symbol = _sympy.Symbol(variable)
    if expression is None or expression.free_symbols - {symbol}:
        return None
    try:
        polynomial = _sympy.Poly(expression, symbol, domain=_sympy.QQ)
    except (TypeError, ValueError, _sympy.PolynomialError):
        return None
    if polynomial.degree() > 1:
        return None
    coefficient = polynomial.coeff_monomial(symbol)
    if not isinstance(coefficient, _sympy.Rational) or coefficient == 0:
        return None
    slope = Fraction(int(coefficient.p), int(coefficient.q))
    return argument, slope


def _direct_csch_affine_integrand(
    root: ast.AST,
    variable: str,
) -> tuple[ast.AST, Fraction] | None:
    import sympy as _sympy

    if (
        not isinstance(root, ast.Call)
        or not isinstance(root.func, ast.Name)
        or root.func.id != "csch"
        or len(root.args) != 1
        or root.keywords
    ):
        return None
    argument = root.args[0]
    expression = _real_sympy_expression(argument)
    symbol = _sympy.Symbol(variable)
    if expression is None or expression.free_symbols - {symbol}:
        return None
    try:
        polynomial = _sympy.Poly(expression, symbol, domain=_sympy.QQ)
    except (TypeError, ValueError, _sympy.PolynomialError):
        return None
    if polynomial.degree() > 1:
        return None
    coefficient = polynomial.coeff_monomial(symbol)
    if (
        not isinstance(coefficient, _sympy.Rational)
        or coefficient == 0
    ):
        return None
    slope = Fraction(int(coefficient.p), int(coefficient.q))
    return argument, slope


def _coth_identity_terms(*roots: ast.AST) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for root in roots:
        arguments = _coth_arguments(root)
        if arguments is None:
            continue
        for argument in arguments:
            key = ast.dump(argument, include_attributes=False)
            if key in seen:
                continue
            seen.add(key)
            rendered = _render_real_calculus_node(
                argument,
                set(),
                {"nodes": 0},
                0,
                allow_variable_denominators=True,
            )
            if rendered is not None:
                terms.append(f"Real.cosh_sq_sub_sinh_sq {rendered}")
    return terms


def _wrap_coth_log_abs(source: str) -> str:
    class _WrapCothLogAbs(ast.NodeTransformer):
        def visit_Call(self, node: ast.Call) -> ast.AST:
            node = self.generic_visit(node)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "log"
                and len(node.args) == 1
                and not node.keywords
                and isinstance(node.args[0], ast.Call)
                and isinstance(node.args[0].func, ast.Name)
                and node.args[0].func.id == "sinh"
            ):
                node.args[0] = ast.Call(
                    func=ast.Name(id="abs", ctx=ast.Load()),
                    args=[node.args[0]],
                    keywords=[],
                )
            return node

    tree = ast.parse(source, mode="eval")
    wrapped = _WrapCothLogAbs().visit(tree)
    return ast.unparse(ast.fix_missing_locations(wrapped.body))


def _coth_derivative_lemma_source() -> str:
    return """theorem wolfram_hasDerivAt_coth {x : ℝ}
    (hsinh : Real.sinh x ≠ 0) :
    HasDerivAt (fun y : ℝ => Real.cosh y / Real.sinh y)
      (-(1 / (Real.sinh x) ^ 2)) x := by
  have hquot := (Real.hasDerivAt_cosh x).div (Real.hasDerivAt_sinh x) hsinh
  convert hquot using 1
  field_simp [hsinh]
  nlinarith [Real.cosh_sq_sub_sinh_sq x]

"""


def _csch_derivative_lemma_source() -> str:
    return """theorem wolfram_hasDerivAt_csch {x : ℝ}
    (hsinh : Real.sinh x ≠ 0) :
    HasDerivAt (fun y : ℝ => 1 / Real.sinh y)
      (-(Real.cosh x / (Real.sinh x) ^ 2)) x := by
  have hquot := (hasDerivAt_const x (1 : ℝ)).div (Real.hasDerivAt_sinh x) hsinh
  convert hquot using 1
  field_simp [hsinh]
  ring

"""


def _tanh_derivative_lemma_source() -> str:
    return """theorem wolfram_hasDerivAt_tanh (x : ℝ) :
    HasDerivAt Real.tanh (1 - (Real.tanh x) ^ 2) x := by
  have hcosh : Real.cosh x ≠ 0 := ne_of_gt (Real.cosh_pos x)
  convert (Real.hasDerivAt_sinh x).div (Real.hasDerivAt_cosh x) hcosh using 1
  all_goals
    first
    | ext y; simp [Real.tanh_eq_sinh_div_cosh]
    | rw [Real.tanh_eq_sinh_div_cosh]; field_simp [hcosh] <;> ring

"""


def _sinh_double_lemma_source() -> str:
    return """theorem wolfram_sinh_ne_zero_of_tanh (x : ℝ)
    (h : Real.tanh x ≠ 0) : Real.sinh x ≠ 0 := by
  rw [Real.tanh_eq_sinh_div_cosh] at h
  exact (div_ne_zero_iff.mp h).1

theorem wolfram_sinh_double (x : ℝ) :
    Real.sinh x = 2 * Real.sinh (x / 2) * Real.cosh (x / 2) := by
  rw [show x = x / 2 + x / 2 by ring, Real.sinh_add]
  ring

theorem wolfram_csch_identity (x : ℝ) :
    2 * Real.sinh (x / 2) * Real.cosh (x / 2) =
      Real.sinh x * (Real.cosh (x / 2) ^ 2 - Real.sinh (x / 2) ^ 2) := by
  rw [Real.cosh_sq_sub_sinh_sq, ← wolfram_sinh_double]
  ring

"""


def _csch_primitive_derivative_lemma_source() -> str:
    return """theorem wolfram_hasDerivAt_log_tanh_half (x : ℝ)
    (h_tanh : Real.tanh (x / 2) ≠ 0)
    (h_sinh : Real.sinh x ≠ 0) :
    HasDerivAt (fun y : ℝ => Real.log (Real.tanh (y / 2)))
      (1 / Real.sinh x) x := by
  convert (((wolfram_hasDerivAt_tanh (x / 2)).comp x
    (((hasDerivAt_id x)).div_const (2 : ℝ))).log
      (by simpa [Function.comp_def] using h_tanh)) using 1
  all_goals
    first
    | (ext y; simp [id_eq] <;> ring <;> done)
    | (simp [id_eq, Real.tanh_eq_sinh_div_cosh] <;>
       field_simp [h_tanh, h_sinh, ne_of_gt (Real.cosh_pos (x / 2)),
         (wolfram_sinh_ne_zero_of_tanh (x / 2) h_tanh)] <;>
       nlinarith [wolfram_csch_identity x] <;> done)

"""


def _sech_primitive_derivative_lemma_source() -> str:
    return """theorem wolfram_hasDerivAt_arctan_sinh (x : ℝ) :
    HasDerivAt (fun y : ℝ => Real.arctan (Real.sinh y))
      (1 / Real.cosh x) x := by
  convert ((Real.hasDerivAt_sinh x).arctan) using 1
  field_simp [ne_of_gt (Real.cosh_pos x)]
  nlinarith [Real.cosh_sq_sub_sinh_sq x]

"""


def _coth_primitive_derivative_lemma_source() -> str:
    return """theorem wolfram_hasDerivAt_log_abs_sinh (x : ℝ)
    (hsinh : Real.sinh x ≠ 0) :
    HasDerivAt (fun y : ℝ => Real.log |Real.sinh y|)
      (Real.cosh x / Real.sinh x) x := by
  simpa only [Real.log_abs] using (Real.hasDerivAt_sinh x).log hsinh

theorem wolfram_log_sinh_abs (x : ℝ) :
    Real.log (Real.sinh x) = Real.log (Real.sinh |x|) := by
  rw [← Real.log_abs, Real.abs_sinh]

"""


def _tanh_primitive_derivative_lemma_source() -> str:
    return """theorem wolfram_hasDerivAt_log_cosh (x : ℝ) :
    HasDerivAt (fun y : ℝ => Real.log (Real.cosh y)) (Real.tanh x) x := by
  have hcosh : Real.cosh x ≠ 0 := ne_of_gt (Real.cosh_pos x)
  simpa [Real.tanh_eq_sinh_div_cosh] using (Real.hasDerivAt_cosh x).log hcosh

"""


def _sech_derivative_lemma_source() -> str:
    return """theorem wolfram_hasDerivAt_sech {x : ℝ} :
    HasDerivAt (fun y : ℝ => 1 / Real.cosh y)
      (-(Real.sinh x / (Real.cosh x) ^ 2)) x := by
  have hcosh : Real.cosh x ≠ 0 := ne_of_gt (Real.cosh_pos x)
  have hquot := (hasDerivAt_const x (1 : ℝ)).div (Real.hasDerivAt_cosh x) hcosh
  convert hquot using 1
  field_simp [hcosh]
  ring

"""


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
            "atanh": _sympy.atanh,
            "exp": _sympy.exp,
            "sinh": _sympy.sinh,
            "cosh": _sympy.cosh,
            "tanh": _sympy.tanh,
            "coth": _sympy.coth,
            "sech": _sympy.sech,
            "csch": _sympy.csch,
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
            "atanh": _sympy.atanh,
            "asinh": _sympy.asinh,
            "acosh": _sympy.acosh,
            "exp": _sympy.exp,
            "sinh": _sympy.sinh,
            "cosh": _sympy.cosh,
            "tanh": _sympy.tanh,
            "coth": _sympy.coth,
            "sech": _sympy.sech,
            "csch": _sympy.csch,
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
        return _lean_rational(value) if value is not None else None
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
            or len(node.args) not in ({1, 2} if node.func.id == "log" else {1})
        ):
            return None
        if node.func.id == "log" and len(node.args) == 2:
            base = _constant_fraction(node.args[1])
            if base is None or base <= 0 or base == 1:
                return None
            argument = _render_real_calculus_node(
                node.args[0],
                names,
                state,
                depth + 1,
                substitutions,
                allow_variable_denominators,
            )
            rendered_base = _render_real_calculus_node(
                node.args[1],
                names,
                state,
                depth + 1,
                substitutions,
                allow_variable_denominators,
            )
            if argument is None or rendered_base is None:
                return None
            return f"((Real.log {argument}) / (Real.log {rendered_base}))"
        if node.func.id == "coth":
            argument = _render_real_calculus_node(
                node.args[0],
                names,
                state,
                depth + 1,
                substitutions,
                allow_variable_denominators,
            )
            if argument is None:
                return None
            return f"((Real.cosh {argument}) / (Real.sinh {argument}))"
        if node.func.id in {"csch", "sech"}:
            argument = _render_real_calculus_node(
                node.args[0],
                names,
                state,
                depth + 1,
                substitutions,
                allow_variable_denominators,
            )
            if argument is None:
                return None
            hyperbolic_function = "sinh" if node.func.id == "csch" else "cosh"
            return f"(1 / (Real.{hyperbolic_function} {argument}))"
        function_name = {
            "sin": "Real.sin",
            "cos": "Real.cos",
            "tan": "Real.tan",
            "asin": "Real.arcsin",
            "acos": "Real.arccos",
            "atan": "Real.arctan",
            "atanh": "Real.artanh",
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
            or len(node.args) not in ({1, 2} if node.func.id == "log" else {1})
        ):
            return None
        if node.func.id == "log":
            if len(node.args) == 2:
                base = node.args[1]
                base_fraction = _constant_fraction(base)
                if base_fraction is None or base_fraction <= 0 or base_fraction == 1:
                    return None
                base_key = ast.dump(_log_base_call(base), include_attributes=False)
                base_nonzero = (domain_hypotheses or {}).get(base_key)
                rendered_base = _render_real_calculus_node(
                    base,
                    set(),
                    {"nodes": 0},
                    0,
                    allow_variable_denominators=True,
                )
                if base_nonzero is None or rendered_base is None:
                    return None
                numerator = _derivative_proof_term(
                    _log_base_call(node.args[0]),
                    variable,
                    function_variable,
                    point,
                    state,
                    depth + 1,
                    domain_hypotheses,
                    positive_hypotheses,
                )
                if numerator is None:
                    return None
                denominator = (
                    f"(hasDerivAt_const {point} (Real.log {rendered_base}))"
                )
                return f"({numerator}).div ({denominator}) {base_nonzero}"
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
            return (
                f"({inner}).log (by simpa [Function.comp_def] using {domain_proof})"
                if inner is not None
                else None
            )
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
        if node.func.id == "sech":
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
                    f"(1 / (Real.cosh {rendered_argument})))"
                )
            rendered_argument = _render_real_calculus_node(
                argument, set(), {"nodes": 0}, 0,
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
                f"(wolfram_hasDerivAt_sech (x := {rendered_argument})).comp "
                f"{point} ({inner})"
            )
        if node.func.id == "csch":
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
                    f"(1 / (Real.sinh {rendered_argument})))"
                )
            rendered_argument = _render_real_calculus_node(
                argument, set(), {"nodes": 0}, 0,
                allow_variable_denominators=True,
            )
            sinh_hypothesis = (domain_hypotheses or {}).get(
                ast.dump(_sinh_call(argument), include_attributes=False)
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
            if rendered_argument is None or sinh_hypothesis is None or inner is None:
                return None
            return (
                f"(wolfram_hasDerivAt_csch (x := {rendered_argument}) "
                f"{sinh_hypothesis}).comp {point} ({inner})"
            )
        if node.func.id == "coth":
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
                    f"((Real.cosh {rendered_argument}) / "
                    f"(Real.sinh {rendered_argument})))"
                )
            rendered_argument = _render_real_calculus_node(
                argument,
                set(),
                {"nodes": 0},
                0,
                allow_variable_denominators=True,
            )
            sinh_hypothesis = (domain_hypotheses or {}).get(
                ast.dump(_sinh_call(argument), include_attributes=False)
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
            if rendered_argument is None or sinh_hypothesis is None or inner is None:
                return None
            return (
                f"(wolfram_hasDerivAt_coth (x := {rendered_argument}) "
                f"{sinh_hypothesis}).comp {point} ({inner})"
            )
        if node.func.id == "atanh":
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
                    f"(Real.artanh {rendered_argument}))"
                )
            rendered_argument = _render_real_calculus_node(
                argument,
                set(),
                {"nodes": 0},
                0,
                allow_variable_denominators=True,
            )
            lower_hypothesis = (domain_hypotheses or {}).get(
                _inverse_hyperbolic_tanh_hypothesis_key(argument, "lower")
            )
            upper_hypothesis = (domain_hypotheses or {}).get(
                _inverse_hyperbolic_tanh_hypothesis_key(argument, "upper")
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
            if (
                rendered_argument is None
                or lower_hypothesis is None
                or upper_hypothesis is None
                or inner is None
            ):
                return None
            return (
                "(wolfram_hasDerivAt_artanh "
                f"(x := {rendered_argument}) {lower_hypothesis} "
                f"{upper_hypothesis}).comp {point} ({inner})"
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


def _parse_exact_radical_basis(
    source: str,
) -> tuple[
    Fraction, Fraction, Fraction, Fraction,
    Fraction, Fraction, Fraction, Fraction,
] | None:
    """Parse exact sums of rational multiples of the supported trig radicals."""
    exact_source = _normalize_decimal_literals(source)
    if exact_source is None:
        return None
    try:
        tree = ast.parse(exact_source, mode="eval")
    except (ValueError, SyntaxError, RecursionError):
        return None

    Value = tuple[
        Fraction, Fraction, Fraction, Fraction,
        Fraction, Fraction, Fraction, Fraction,
    ]

    def vector(*coefficients: int | Fraction) -> Value:
        values = tuple(Fraction(value) for value in coefficients)
        return values + (Fraction(0),) * (8 - len(values))  # type: ignore[return-value]

    zero = vector()
    one = vector(1)
    sqrt_two = vector(0, 1)
    sqrt_two_plus = vector(0, 0, 1)
    sqrt_two_minus = vector(0, 0, 0, 1)
    sqrt_two_plus_nested = vector(0, 0, 0, 0, 1)
    sqrt_two_minus_nested = vector(0, 0, 0, 0, 0, 1)
    sqrt_two_plus_deep = vector(0, 0, 0, 0, 0, 0, 1)
    sqrt_two_minus_deep = vector(0, 0, 0, 0, 0, 0, 0, 1)
    half_sqrt_two = vector(0, Fraction(1, 2))
    half_sqrt_two_plus = vector(0, 0, Fraction(1, 2))
    half_sqrt_two_minus = vector(0, 0, 0, Fraction(1, 2))
    half_sqrt_two_plus_nested = vector(0, 0, 0, 0, Fraction(1, 2))
    half_sqrt_two_minus_nested = vector(0, 0, 0, 0, 0, Fraction(1, 2))
    half_sqrt_two_plus_deep = vector(0, 0, 0, 0, 0, 0, Fraction(1, 2))
    half_sqrt_two_minus_deep = vector(0, 0, 0, 0, 0, 0, 0, Fraction(1, 2))

    def negate(value: Value) -> Value:
        return tuple(-component for component in value)  # type: ignore[return-value]

    def add(left: Value, right: Value) -> Value:
        return tuple(a + b for a, b in zip(left, right))  # type: ignore[return-value]

    def subtract(left: Value, right: Value) -> Value:
        return tuple(a - b for a, b in zip(left, right))  # type: ignore[return-value]

    basis_products = {
        (0, 0): one,
        (0, 1): sqrt_two,
        (0, 2): sqrt_two_plus,
        (0, 3): sqrt_two_minus,
        (0, 4): sqrt_two_plus_nested,
        (0, 5): sqrt_two_minus_nested,
        (0, 6): sqrt_two_plus_deep,
        (0, 7): sqrt_two_minus_deep,
        (1, 1): vector(2),
        (1, 2): add(sqrt_two_plus, sqrt_two_minus),
        (1, 3): subtract(sqrt_two_plus, sqrt_two_minus),
        (2, 2): vector(2, 1),
        (2, 3): sqrt_two,
        (3, 3): vector(2, -1),
        (4, 4): vector(2, 0, 1),
        (4, 5): sqrt_two_minus,
        (5, 5): vector(2, 0, -1),
        (6, 6): vector(2, 0, 0, 0, 1),
        (6, 7): sqrt_two_minus,
        (7, 7): vector(2, 0, 0, 0, -1),
    }

    def multiply(left: Value, right: Value) -> Value | None:
        result = [Fraction(0)] * 8
        for left_index, left_coefficient in enumerate(left):
            if left_coefficient == 0:
                continue
            for right_index, right_coefficient in enumerate(right):
                if right_coefficient == 0:
                    continue
                product = basis_products.get(tuple(sorted((left_index, right_index))))
                if product is None:
                    return None
                for result_index, value in enumerate(product):
                    result[result_index] += left_coefficient * right_coefficient * value
        return tuple(result)  # type: ignore[return-value]

    def form(
        node: ast.AST,
    ) -> Value | None:
        rational = _constant_fraction(node)
        if rational is not None:
            return vector(rational)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and len(node.args) == 1 and not node.keywords:
                if node.func.id in {"sin", "cos"}:
                    angle_form = _rational_pi_form(node.args[0])
                    if angle_form is not None and angle_form[1] == 0:
                        coefficient = angle_form[0]
                        magnitude = abs(coefficient)
                        if node.func.id == "sin":
                            trig_values = {
                                Fraction(0): zero,
                                Fraction(1, 32): half_sqrt_two_minus_deep,
                                Fraction(1, 16): half_sqrt_two_minus_nested,
                                Fraction(1, 8): half_sqrt_two_minus,
                                Fraction(1, 4): half_sqrt_two,
                                Fraction(1, 2): one,
                                Fraction(1): zero,
                                Fraction(2): zero,
                            }
                            value = trig_values.get(magnitude)
                            return negate(value) if value is not None and coefficient < 0 else value
                        trig_values = {
                            Fraction(0): one,
                            Fraction(1, 32): half_sqrt_two_plus_deep,
                            Fraction(1, 16): half_sqrt_two_plus_nested,
                            Fraction(1, 8): half_sqrt_two_plus,
                            Fraction(1, 4): half_sqrt_two,
                            Fraction(1, 2): zero,
                            Fraction(1): vector(-1),
                            Fraction(2): one,
                        }
                        return trig_values.get(magnitude)
                if node.func.id != "sqrt":
                    return None
                radicand = form(node.args[0])
                if radicand == vector(2):
                    return sqrt_two
                if radicand == vector(2, 1):
                    return sqrt_two_plus
                if radicand == vector(2, -1):
                    return sqrt_two_minus
                if radicand == vector(Fraction(1, 2), Fraction(1, 4)):
                    return half_sqrt_two_plus
                if radicand == vector(Fraction(1, 2), Fraction(-1, 4)):
                    return half_sqrt_two_minus
                if radicand == vector(2, 0, 1):
                    return sqrt_two_plus_nested
                if radicand == vector(2, 0, -1):
                    return sqrt_two_minus_nested
                if radicand == vector(2, 0, 0, 0, 1):
                    return sqrt_two_plus_deep
                if radicand == vector(2, 0, 0, 0, -1):
                    return sqrt_two_minus_deep
                if radicand == vector(Fraction(1, 2), 0, Fraction(1, 4)):
                    return half_sqrt_two_plus_nested
                if radicand == vector(Fraction(1, 2), 0, Fraction(-1, 4)):
                    return half_sqrt_two_minus_nested
                if radicand == vector(Fraction(1, 2), 0, 0, 0, Fraction(1, 4)):
                    return half_sqrt_two_plus_deep
                if radicand == vector(Fraction(1, 2), 0, 0, 0, Fraction(-1, 4)):
                    return half_sqrt_two_minus_deep
            return None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = form(node.operand)
            if value is None or isinstance(node.op, ast.UAdd):
                return value
            return negate(value)
        if not isinstance(node, ast.BinOp):
            return None
        left = form(node.left)
        right = form(node.right)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return add(left, right)
        if isinstance(node.op, ast.Sub):
            return subtract(left, right)
        if isinstance(node.op, ast.Mult):
            return multiply(left, right)
        if isinstance(node.op, ast.Div) and right[1:] == zero[1:] and right[0] != 0:
            return tuple(value / right[0] for value in left)  # type: ignore[return-value]
        if isinstance(node.op, ast.Pow):
            exponent = _constant_fraction(node.right)
            if exponent is None or exponent.denominator != 1 or not 0 <= exponent <= 16:
                return None
            result = one
            for _ in range(exponent.numerator):
                product = multiply(result, left)
                if product is None:
                    return None
                result = product
            return result
        return None

    try:
        return form(tree.body)
    except RecursionError:
        return None


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


def _lean_exact_radical_basis(
    coefficients: tuple[
        Fraction, Fraction, Fraction, Fraction,
        Fraction, Fraction, Fraction, Fraction,
    ],
) -> str:
    basis = (
        "",
        "Real.sqrt 2",
        "Real.sqrt (2 + Real.sqrt 2)",
        "Real.sqrt (2 - Real.sqrt 2)",
        "Real.sqrt (2 + Real.sqrt (2 + Real.sqrt 2))",
        "Real.sqrt (2 - Real.sqrt (2 + Real.sqrt 2))",
        "Real.sqrt (2 + Real.sqrt (2 + Real.sqrt (2 + Real.sqrt 2)))",
        "Real.sqrt (2 - Real.sqrt (2 + Real.sqrt (2 + Real.sqrt 2)))",
    )
    terms: list[str] = []
    for coefficient, radical in zip(coefficients, basis):
        if coefficient == 0:
            continue
        if radical:
            term = f"({_lean_rational(abs(coefficient))} * {radical})"
        else:
            term = _lean_rational(abs(coefficient))
        if not terms:
            terms.append(f"-{term}" if coefficient < 0 else term)
        else:
            operation = "-" if coefficient < 0 else "+"
            terms.append(f" {operation} {term}")
    if not terms:
        return _lean_rational(Fraction(0))
    return f"({' '.join(terms)})"


def _format_exact_radical_basis(
    coefficients: tuple[
        Fraction, Fraction, Fraction, Fraction,
        Fraction, Fraction, Fraction, Fraction,
    ],
) -> str:
    basis = (
        "",
        "sqrt(2)",
        "sqrt(2 + sqrt(2))",
        "sqrt(2 - sqrt(2))",
        "sqrt(2 + sqrt(2 + sqrt(2)))",
        "sqrt(2 - sqrt(2 + sqrt(2)))",
        "sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2))))",
        "sqrt(2 - sqrt(2 + sqrt(2 + sqrt(2))))",
    )
    terms: list[str] = []
    for coefficient, radical in zip(coefficients, basis):
        if coefficient == 0:
            continue
        magnitude = abs(coefficient)
        if not radical:
            term = _fraction_text(magnitude)
        else:
            numerator = "" if magnitude.numerator == 1 else f"{magnitude.numerator}*"
            term = f"{numerator}{radical}"
            if magnitude.denominator != 1:
                term += f"/{magnitude.denominator}"
        if not terms:
            terms.append(f"-{term}" if coefficient < 0 else term)
        else:
            operation = "-" if coefficient < 0 else "+"
            terms.append(f" {operation} {term}")
    return "".join(terms) if terms else "0"
