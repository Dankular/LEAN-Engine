from __future__ import annotations

from enum import Enum
from fractions import Fraction
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$")]
RationalText = Annotated[str, StringConstraints(pattern=r"^-?\d+(?:/\d+)?$", max_length=80)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Sort(str, Enum):
    INT = "Int"
    REAL = "Real"


class Variable(StrictModel):
    name: Identifier
    sort: Sort


class VariableTerm(StrictModel):
    kind: Literal["var"]
    name: Identifier


class NumberTerm(StrictModel):
    kind: Literal["number"]
    value: RationalText

    @model_validator(mode="after")
    def denominator_is_nonzero(self) -> NumberTerm:
        try:
            Fraction(self.value)
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError("number must be an integer or a rational such as '3/4'") from exc
        return self


class BinaryTerm(StrictModel):
    kind: Literal["add", "sub", "mul"]
    left: Term
    right: Term


Term = Annotated[Union[VariableTerm, NumberTerm, BinaryTerm], Field(discriminator="kind")]
BinaryTerm.model_rebuild()


class Comparison(StrictModel):
    op: Literal["eq", "ne", "lt", "le", "gt", "ge"]
    left: Term
    right: Term


class Budget(StrictModel):
    wall_seconds: int = Field(default=30, ge=1, le=30)


class ClaimRequest(StrictModel):
    """Free variables are universally quantified over the declared arithmetic sort."""

    variables: list[Variable] = Field(default_factory=list, max_length=64)
    assumptions: list[Comparison] = Field(default_factory=list, max_length=64)
    claim: Comparison
    source_text: str | None = Field(default=None, max_length=4000)
    budget: Budget = Field(default_factory=Budget)

    @model_validator(mode="after")
    def validate_typed_specification(self) -> ClaimRequest:
        names = [item.name for item in self.variables]
        if len(names) != len(set(names)):
            raise ValueError("variable names must be unique")
        sorts = {item.sort for item in self.variables}
        if len(sorts) > 1:
            raise ValueError("a request must use one arithmetic sort: Int or Real")
        sort = next(iter(sorts), Sort.INT)
        known_names = set(names)
        node_count = [0]
        for comparison in [*self.assumptions, self.claim]:
            refs = term_variables(comparison.left) | term_variables(comparison.right)
            undeclared = refs - known_names
            if undeclared:
                raise ValueError(f"undeclared variables: {', '.join(sorted(undeclared))}")
            for term in (comparison.left, comparison.right):
                check_term(term, sort, depth=0, node_count=node_count)
        return self


def number_value(term: NumberTerm) -> Fraction:
    return Fraction(term.value)


def term_variables(term: Term) -> set[str]:
    if isinstance(term, VariableTerm):
        return {term.name}
    if isinstance(term, NumberTerm):
        return set()
    return term_variables(term.left) | term_variables(term.right)


def check_term(
    term: Term, sort: Sort, depth: int, node_count: list[int] | None = None
) -> dict[str, Fraction]:
    """Validate the supported linear fragment and return variable coefficients."""
    if node_count is None:
        node_count = [0]
    node_count[0] += 1
    if node_count[0] > 4096:
        raise ValueError("expression tree exceeds 4096 nodes")
    if depth > 24:
        raise ValueError("expression nesting exceeds 24 levels")
    if isinstance(term, VariableTerm):
        return {term.name: Fraction(1)}
    if isinstance(term, NumberTerm):
        value = number_value(term)
        if sort is Sort.INT and value.denominator != 1:
            raise ValueError("Int expressions require integer constants")
        if abs(value.numerator).bit_length() > 256 or value.denominator.bit_length() > 256:
            raise ValueError("numeric constants are limited to 256 bits")
        return {}

    left = check_term(term.left, sort, depth + 1, node_count)
    right = check_term(term.right, sort, depth + 1, node_count)
    if term.kind == "add":
        return _combine(left, right, Fraction(1))
    if term.kind == "sub":
        return _combine(left, right, Fraction(-1))
    if left and right:
        raise ValueError("nonlinear multiplication is outside the supported fragment")

    scalar_term = term.left if not left else term.right
    scalar = _constant_value(scalar_term)
    coefficients = right if not left else left
    return {name: value * scalar for name, value in coefficients.items() if value * scalar}


def _constant_value(term: Term) -> Fraction:
    if isinstance(term, NumberTerm):
        return number_value(term)
    if isinstance(term, BinaryTerm):
        left = _constant_value(term.left)
        right = _constant_value(term.right)
        if term.kind == "add":
            return left + right
        if term.kind == "sub":
            return left - right
        return left * right
    raise ValueError("multiplication is supported only by a constant")


def _combine(
    left: dict[str, Fraction], right: dict[str, Fraction], sign: Fraction
) -> dict[str, Fraction]:
    result = dict(left)
    for name, coefficient in right.items():
        result[name] = result.get(name, Fraction(0)) + sign * coefficient
    return {name: value for name, value in result.items() if value}
