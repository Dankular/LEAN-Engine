from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

from pydantic import BaseModel, ConfigDict, Field

from .lean_backend import LeanResult, run_generated_source


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


def prepare_computation(code: str) -> Computation | None:
    if re.fullmatch(r"\s*Sin\s*\[\s*Pi\s*\]\s*", code):
        source = """import Mathlib.Analysis.SpecialFunctions.Trigonometric.Basic

theorem wolfram_language_compute : Real.sin Real.pi = 0 := by
  simp

#print wolfram_language_compute
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

#print wolfram_language_compute
#print axioms wolfram_language_compute
"""
        return Computation("2*Cos[x]*Sin[x]", source)

    match = re.fullmatch(
        r"\s*Integrate\s*\[\s*x\s*,\s*\{\s*x\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\}\s*\]\s*",
        code,
    )
    if match:
        lower, upper = (int(value) for value in match.groups())
        exact_value = Fraction(upper * upper - lower * lower, 2)
        rendered_value = str(exact_value.numerator)
        if exact_value.denominator != 1:
            rendered_value += f"/{exact_value.denominator}"
        lean_lower = f"({lower} : ℝ)"
        lean_upper = f"({upper} : ℝ)"
        lean_value = _lean_rational(exact_value)
        source = f"""import Mathlib.MeasureTheory.Integral.IntervalIntegral.Basic
import Mathlib.Tactic.NormNum

theorem wolfram_language_compute :
    (∫ x in {lean_lower}..{lean_upper}, x) = {lean_value} := by
  norm_num [intervalIntegral.integral_id]

#print wolfram_language_compute
#print axioms wolfram_language_compute
"""
        return Computation(rendered_value, source)

    return None


def check_computation(computation: Computation, timeout_seconds: float) -> LeanResult:
    return run_generated_source(computation.lean_source, timeout_seconds)


def _lean_rational(value: Fraction) -> str:
    if value.denominator == 1:
        return f"({value.numerator} : ℝ)"
    return f"(({value.numerator} : ℝ) / {value.denominator})"
