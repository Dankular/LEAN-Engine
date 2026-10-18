from __future__ import annotations

import ast
import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .models import (
    BinaryTerm,
    Budget,
    ClaimRequest,
    Comparison,
    NumberTerm,
    PowerTerm,
    Term,
    Variable,
    VariableTerm,
)


FormulaText = Annotated[str, StringConstraints(min_length=1, max_length=4000)]
_CARET = re.compile(r"(?<!\*)\^(?!\*)")
_COMPARISON_OPERATORS = {
    ast.Eq: "eq",
    ast.NotEq: "ne",
    ast.Lt: "lt",
    ast.LtE: "le",
    ast.Gt: "gt",
    ast.GtE: "ge",
}
_MAX_FORMULA_NODES = 256
_MAX_FORMULA_DEPTH = 32


class FormulaProofRequest(BaseModel):
    """A textual polynomial claim with explicitly declared variable sorts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    formula: FormulaText
    variables: list[Variable] = Field(default_factory=list, max_length=64)
    assumptions: list[FormulaText] = Field(default_factory=list, max_length=64)
    budget: Budget = Field(default_factory=Budget)


class FormulaInputError(ValueError):
    pass


def compile_formula(request: FormulaProofRequest) -> ClaimRequest:
    variables = {item.name: item for item in request.variables}
    claim = _parse_comparison(request.formula, variables)
    assumptions = [_parse_comparison(item, variables) for item in request.assumptions]
    return ClaimRequest(
        variables=request.variables,
        assumptions=assumptions,
        claim=claim,
        source_text=request.formula,
        budget=request.budget,
    )


def _parse_comparison(source: str, variables: dict[str, Variable]) -> Comparison:
    normalized = _CARET.sub("**", source)
    try:
        tree = ast.parse(normalized, mode="eval")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise FormulaInputError("formula syntax is invalid") from exc
    _validate_tree_size(tree)
    expression = tree.body
    if not isinstance(expression, ast.Compare) or len(expression.ops) != 1:
        raise FormulaInputError("formula must contain exactly one comparison")
    operator = _COMPARISON_OPERATORS.get(type(expression.ops[0]))
    if operator is None:
        raise FormulaInputError("comparison operator is unsupported")
    return Comparison(
        op=operator,
        left=_parse_term(expression.left, variables, depth=0, source=normalized),
        right=_parse_term(expression.comparators[0], variables, depth=0, source=normalized),
    )


def _validate_tree_size(tree: ast.AST) -> None:
    pending: list[tuple[ast.AST, int]] = [(tree, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > _MAX_FORMULA_NODES:
            raise FormulaInputError(f"formula exceeds {_MAX_FORMULA_NODES} syntax nodes")
        if depth > _MAX_FORMULA_DEPTH:
            raise FormulaInputError(f"formula nesting exceeds {_MAX_FORMULA_DEPTH} levels")
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))


def _parse_term(
    node: ast.AST,
    variables: dict[str, Variable],
    depth: int,
    source: str,
) -> Term:
    if depth > _MAX_FORMULA_DEPTH:
        raise FormulaInputError(f"formula nesting exceeds {_MAX_FORMULA_DEPTH} levels")

    constant = _constant_fraction(node, source)
    if constant is not None:
        return _number_term(constant)

    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise FormulaInputError(f"variable '{node.id}' must have an explicit declared sort")
        return VariableTerm(kind="var", name=node.id)

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = _parse_term(node.operand, variables, depth + 1, source)
        if isinstance(node.op, ast.UAdd):
            return operand
        return BinaryTerm(
            kind="mul",
            left=NumberTerm(kind="number", value="-1"),
            right=operand,
        )

    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Pow):
            exponent = _constant_fraction(node.right, source)
            if exponent is None or exponent.denominator != 1 or not 0 <= exponent <= 16:
                raise FormulaInputError("power exponents must be integers from 0 through 16")
            return PowerTerm(
                kind="pow",
                base=_parse_term(node.left, variables, depth + 1, source),
                exponent=int(exponent),
            )
        if isinstance(node.op, ast.Div):
            divisor = _constant_fraction(node.right, source)
            if divisor is None:
                raise FormulaInputError("division is supported only by a numeric constant")
            if divisor == 0:
                raise FormulaInputError("division by zero is not supported")
            return BinaryTerm(
                kind="mul",
                left=_parse_term(node.left, variables, depth + 1, source),
                right=_number_term(1 / divisor),
            )
        operators = {ast.Add: "add", ast.Sub: "sub", ast.Mult: "mul"}
        kind = operators.get(type(node.op))
        if kind is not None:
            return BinaryTerm(
                kind=kind,
                left=_parse_term(node.left, variables, depth + 1, source),
                right=_parse_term(node.right, variables, depth + 1, source),
            )

    raise FormulaInputError(f"unsupported formula syntax: {type(node).__name__}")


def _constant_fraction(node: ast.AST, source: str) -> Fraction | None:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        if abs(node.value).bit_length() > 256:
            raise FormulaInputError("numeric constants are limited to 256 bits")
        return Fraction(node.value)
    if isinstance(node, ast.Constant) and type(node.value) is float:
        literal = ast.get_source_segment(source, node)
        if literal is None:
            return None
        try:
            decimal_value = Decimal(literal.replace("_", ""))
        except (InvalidOperation, ValueError):
            return None
        decimal_tuple = decimal_value.as_tuple()
        if (
            not decimal_value.is_finite()
            or len(decimal_tuple.digits) > 78
            or abs(decimal_tuple.exponent) > 80
        ):
            raise FormulaInputError("decimal constants exceed the exact 256-bit limit")
        result = Fraction(decimal_value)
        if abs(result.numerator).bit_length() > 256 or result.denominator.bit_length() > 256:
            return None
        return result
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _constant_fraction(node.operand, source)
        if value is not None:
            return value if isinstance(node.op, ast.UAdd) else -value
        return None
    if not isinstance(node, ast.BinOp):
        return None

    left = _constant_fraction(node.left, source)
    right = _constant_fraction(node.right, source)
    if left is None or right is None:
        return None
    try:
        if isinstance(node.op, ast.Add):
            result = left + right
        elif isinstance(node.op, ast.Sub):
            result = left - right
        elif isinstance(node.op, ast.Mult):
            result = left * right
        elif isinstance(node.op, ast.Div):
            if right == 0:
                return None
            result = left / right
        elif isinstance(node.op, ast.Pow):
            if right.denominator != 1 or not 0 <= right <= 16:
                return None
            result = left ** int(right)
        else:
            return None
    except (OverflowError, ValueError, ZeroDivisionError):
        return None
    if abs(result.numerator).bit_length() > 256 or result.denominator.bit_length() > 256:
        return None
    return result


def _number_term(value: Fraction) -> NumberTerm:
    rendered = str(value.numerator)
    if value.denominator != 1:
        rendered += f"/{value.denominator}"
    return NumberTerm(kind="number", value=rendered)
