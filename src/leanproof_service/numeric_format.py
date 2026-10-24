"""Display helpers for exact SymPy numbers."""

from __future__ import annotations

from decimal import Decimal, getcontext

from sympy import Expr, Integer, N, Rational, sstr


def terminating_decimal(value: Rational) -> str | None:
    """Return the exact decimal expansion when the denominator has only factors 2 and 5."""
    denominator = value.q
    for factor in (2, 5):
        while denominator % factor == 0:
            denominator //= factor
    if denominator != 1:
        return None
    getcontext().prec = 80
    text = format(Decimal(int(value.p)) / Decimal(int(value.q)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def approximate(value: Expr, digits: int = 10) -> str:
    text = sstr(N(value, digits))
    if "e" not in text and "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def display_exact(value: Expr) -> str:
    """Integers and terminating decimals are shown exactly; others in exact form."""
    if isinstance(value, Integer):
        return str(value)
    if isinstance(value, Rational):
        return terminating_decimal(value) or sstr(value)
    return sstr(value).replace("**", "^")
