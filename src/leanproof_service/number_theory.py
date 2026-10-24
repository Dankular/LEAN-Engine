"""Bounded exact integer number-theory queries for Full Results.

Inputs are matched by strict templates over integer literals only. Computation uses
SymPy's exact integer routines; results are not Lean proofs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re

from sympy import binomial, divisors, factorint, isprime, nextprime, prime, totient


_FACTOR_LIMIT = 10**18
_GCD_LIMIT_DIGITS = 100
_MAX_ARGUMENTS = 8
_MAX_FACTORIAL = 500
_MAX_BINOMIAL_N = 2000
_MAX_NTH_PRIME = 100_000
_MAX_LISTED_DIVISORS = 64
_MAX_MODULAR_DIGITS = 100
_INTEGER = r"[+-]?\d{1,%d}" % (_GCD_LIMIT_DIGITS + 1)
_PREFIX = re.compile(
    r"^(?:(?:please\s+)?(?:(?:what\s+is|what's|calculate|compute|find|determine|evaluate)\s+)?"
    r"(?:the\s+)?)",
    re.IGNORECASE,
)


class NumberTheoryError(ValueError):
    """A recognized number-theory request is outside the supported bounds or domain."""


@dataclass(frozen=True)
class NumberTheoryPod:
    pod_id: str
    title: str
    text: str


@dataclass(frozen=True)
class NumberTheoryResult:
    operation: str
    arguments: tuple[int, ...]
    input_text: str
    result_text: str
    extra_pods: tuple[NumberTheoryPod, ...] = field(default_factory=tuple)
    kind: str = "number_theory_result"
    scanner: str = "Integer"


def evaluate_number_theory(source: str) -> NumberTheoryResult | None:
    """Return a result for a recognized integer request, or None if it does not match.

    Raises NumberTheoryError when the template matches but the request is undefined or
    exceeds the service bounds.
    """
    query = source.strip()
    if not re.search(r"\d\s*!+$", query):
        query = query.rstrip("!")
    query = query.rstrip("?. ").strip()
    if not query or len(query) > 400:
        return None
    query = re.sub(r"\s+", " ", query)
    matched = _match_call(query) or _match_phrase(query)
    if matched is None:
        return None
    operation, arguments = matched
    handler = _HANDLERS[operation]
    return handler(arguments)


def _integers(text: str) -> tuple[int, ...] | None:
    parts = re.split(r"\s*(?:,|\band\b)\s*", text.strip(), flags=re.IGNORECASE)
    parts = [part for part in parts if part]
    if not parts or not all(re.fullmatch(_INTEGER, part) for part in parts):
        return None
    return tuple(int(part) for part in parts)


_CALL_NAMES = {
    "gcd": "gcd", "lcm": "lcm", "factorial": "factorial", "binomial": "binomial",
    "totient": "totient", "eulerphi": "totient", "isprime": "isprime",
    "nextprime": "nextprime", "prime": "nthprime", "mod": "mod",
    "primefactors": "factor", "divisors": "divisors",
}


def _match_call(query: str) -> tuple[str, tuple[int, ...]] | None:
    call = re.fullmatch(r"([A-Za-z]+)\s*[(\[]\s*(.+?)\s*[)\]]", query)
    if call is None:
        return None
    operation = _CALL_NAMES.get(call.group(1).lower())
    arguments = _integers(call.group(2))
    if operation is None or arguments is None:
        return None
    return operation, arguments


def _match_phrase(query: str) -> tuple[str, tuple[int, ...]] | None:
    body = _PREFIX.sub("", query, count=1)
    patterns: list[tuple[str, str]] = [
        ("factor", rf"(?:the\s+)?(?:prime\s+)?factori[sz]ation\s+of\s+({_INTEGER})"),
        ("factor", rf"(?:prime\s+)?factori[sz]e\s+({_INTEGER})"),
        ("factor", rf"factor\s+({_INTEGER})(?:\s+into\s+primes)?"),
        ("factor", rf"prime\s+factors\s+of\s+({_INTEGER})"),
        ("isprime", rf"is\s+({_INTEGER})\s+(?:a\s+)?prime(?:\s+number)?"),
        ("isprime", rf"({_INTEGER})\s+is\s+(?:a\s+)?prime(?:\s+number)?"),
        ("gcd", rf"(?:the\s+)?(?:gcd|greatest\s+common\s+(?:divisor|factor))\s+(?:of\s+)?(.+)"),
        ("lcm", rf"(?:the\s+)?(?:lcm|least\s+common\s+multiple)\s+(?:of\s+)?(.+)"),
        ("totient", rf"(?:euler(?:'s)?\s+)?(?:phi|totient)\s+(?:function\s+)?(?:of\s+)?({_INTEGER})"),
        ("divisors", rf"(?:the\s+)?divisors\s+of\s+({_INTEGER})"),
        ("nextprime", rf"(?:the\s+)?next\s+prime\s+(?:after|greater\s+than)\s+({_INTEGER})"),
        ("nthprime", r"(?:the\s+)?(\d{1,7})(?:st|nd|rd|th)\s+prime(?:\s+number)?"),
        ("factorial", rf"factorial\s+of\s+({_INTEGER})"),
    ]
    for operation, pattern in patterns:
        match = re.fullmatch(pattern, body, re.IGNORECASE)
        if match is None:
            continue
        arguments = _integers(match.group(1))
        if arguments is not None:
            return operation, arguments
    factorial = re.fullmatch(r"(\d{1,5})\s*!", body)
    if factorial:
        return "factorial", (int(factorial.group(1)),)
    choose = re.fullmatch(rf"({_INTEGER})\s+choose\s+({_INTEGER})", body, re.IGNORECASE)
    if choose:
        return "binomial", (int(choose.group(1)), int(choose.group(2)))
    modular = re.fullmatch(
        rf"({_INTEGER})(?:\s*\^\s*({_INTEGER}))?\s+(?:mod|modulo)\s+({_INTEGER})",
        body,
        re.IGNORECASE,
    )
    if modular:
        base, exponent, modulus = modular.groups()
        if exponent is None:
            return "mod", (int(base), int(modulus))
        return "powmod", (int(base), int(exponent), int(modulus))
    remainder = re.fullmatch(
        rf"(?:the\s+)?remainder\s+(?:of|when)\s+({_INTEGER})\s+(?:is\s+)?(?:divided\s+by|/)\s+({_INTEGER})",
        body,
        re.IGNORECASE,
    )
    if remainder:
        return "mod", (int(remainder.group(1)), int(remainder.group(2)))
    return None


def _format_factorization(factors: dict[int, int], sign: int = 1) -> str:
    terms = [str(prime_) if power == 1 else f"{prime_}^{power}" for prime_, power in sorted(factors.items())]
    if sign < 0:
        terms.insert(0, "-1")
    return " × ".join(terms)


def _check_factor_bound(value: int) -> None:
    if abs(value) >= _FACTOR_LIMIT:
        raise NumberTheoryError("integer factoring is limited to absolute values below 10^18")


def _factorization_text(value: int) -> str:
    factors = factorint(abs(value))
    if len(factors) == 1 and next(iter(factors.values())) == 1 and value > 0:
        return f"{value} (prime)"
    return _format_factorization(factors, -1 if value < 0 else 1)


def _divisor_pod(value: int) -> NumberTheoryPod:
    values = divisors(abs(value))
    count = len(values)
    if count <= _MAX_LISTED_DIVISORS:
        text = f"{count} divisors: " + ", ".join(str(item) for item in values)
    else:
        text = f"{count} divisors (list omitted above {_MAX_LISTED_DIVISORS})"
    return NumberTheoryPod("Divisors", "Divisors", text)


def _do_factor(arguments: tuple[int, ...]) -> NumberTheoryResult:
    (value,) = _exactly(arguments, 1, "factorization")
    if value in {0, 1, -1}:
        raise NumberTheoryError(f"{value} has no prime factorization")
    _check_factor_bound(value)
    return NumberTheoryResult(
        "factor", arguments, f"prime factorization of {value}", _factorization_text(value),
        (_divisor_pod(value),),
    )


def _do_isprime(arguments: tuple[int, ...]) -> NumberTheoryResult:
    (value,) = _exactly(arguments, 1, "primality")
    _check_factor_bound(value)
    is_prime = value > 1 and bool(isprime(value))
    extras: tuple[NumberTheoryPod, ...] = ()
    if value > 1 and not is_prime:
        extras = (NumberTheoryPod(
            "PrimeFactorization", "Prime factorization", _factorization_text(value)
        ),)
    verdict = "is" if is_prime else "is not"
    return NumberTheoryResult(
        "isprime", arguments, f"is {value} prime?", f"{value} {verdict} a prime number", extras
    )


def _many(arguments: tuple[int, ...], name: str) -> tuple[int, ...]:
    if not 2 <= len(arguments) <= _MAX_ARGUMENTS:
        raise NumberTheoryError(f"{name} needs between 2 and {_MAX_ARGUMENTS} integers")
    return arguments


def _exactly(arguments: tuple[int, ...], count: int, name: str) -> tuple[int, ...]:
    if len(arguments) != count:
        raise NumberTheoryError(f"{name} needs exactly {count} integer argument(s)")
    return arguments


def _do_gcd(arguments: tuple[int, ...]) -> NumberTheoryResult:
    values = _many(arguments, "GCD")
    listed = ", ".join(str(value) for value in values)
    return NumberTheoryResult(
        "gcd", values, f"GCD of {listed}", str(math.gcd(*values))
    )


def _do_lcm(arguments: tuple[int, ...]) -> NumberTheoryResult:
    values = _many(arguments, "LCM")
    listed = ", ".join(str(value) for value in values)
    return NumberTheoryResult("lcm", values, f"LCM of {listed}", str(math.lcm(*values)))


def _do_factorial(arguments: tuple[int, ...]) -> NumberTheoryResult:
    (value,) = _exactly(arguments, 1, "factorial")
    if value < 0:
        raise NumberTheoryError("factorial is defined here for nonnegative integers")
    if value > _MAX_FACTORIAL:
        raise NumberTheoryError(f"factorial is limited to arguments of at most {_MAX_FACTORIAL}")
    return NumberTheoryResult("factorial", arguments, f"{value}!", str(math.factorial(value)))


def _do_binomial(arguments: tuple[int, ...]) -> NumberTheoryResult:
    n, k = _exactly(arguments, 2, "binomial")
    if n < 0 or k < 0:
        raise NumberTheoryError("binomial is defined here for nonnegative integers")
    if n > _MAX_BINOMIAL_N:
        raise NumberTheoryError(f"binomial is limited to n of at most {_MAX_BINOMIAL_N}")
    return NumberTheoryResult(
        "binomial", arguments, f"{n} choose {k}", str(math.comb(n, k))
    )


def _do_totient(arguments: tuple[int, ...]) -> NumberTheoryResult:
    (value,) = _exactly(arguments, 1, "Euler phi")
    if value < 1:
        raise NumberTheoryError("Euler phi is defined here for positive integers")
    _check_factor_bound(value)
    return NumberTheoryResult(
        "totient", arguments, f"Euler phi of {value}", str(totient(value))
    )


def _do_divisors(arguments: tuple[int, ...]) -> NumberTheoryResult:
    (value,) = _exactly(arguments, 1, "divisors")
    if value == 0:
        raise NumberTheoryError("0 has no finite set of divisors")
    _check_factor_bound(value)
    pod = _divisor_pod(value)
    return NumberTheoryResult("divisors", arguments, f"divisors of {value}", pod.text)


def _do_nextprime(arguments: tuple[int, ...]) -> NumberTheoryResult:
    (value,) = _exactly(arguments, 1, "next prime")
    _check_factor_bound(value)
    return NumberTheoryResult(
        "nextprime", arguments, f"next prime after {value}", str(nextprime(value))
    )


def _do_nthprime(arguments: tuple[int, ...]) -> NumberTheoryResult:
    (value,) = _exactly(arguments, 1, "nth prime")
    if not 1 <= value <= _MAX_NTH_PRIME:
        raise NumberTheoryError(f"nth prime is limited to 1 through {_MAX_NTH_PRIME}")
    return NumberTheoryResult("nthprime", arguments, f"prime number {value}", str(prime(value)))


def _do_mod(arguments: tuple[int, ...]) -> NumberTheoryResult:
    value, modulus = _exactly(arguments, 2, "mod")
    if modulus == 0:
        raise NumberTheoryError("modulus must be nonzero")
    return NumberTheoryResult("mod", arguments, f"{value} mod {modulus}", str(value % modulus))


def _do_powmod(arguments: tuple[int, ...]) -> NumberTheoryResult:
    base, exponent, modulus = _exactly(arguments, 3, "modular power")
    if modulus == 0:
        raise NumberTheoryError("modulus must be nonzero")
    if exponent < 0:
        raise NumberTheoryError("negative exponents are not supported for modular powers")
    if max(len(str(abs(base))), len(str(exponent)), len(str(abs(modulus)))) > _MAX_MODULAR_DIGITS:
        raise NumberTheoryError(
            f"modular power operands are limited to {_MAX_MODULAR_DIGITS} digits"
        )
    return NumberTheoryResult(
        "powmod", arguments, f"{base}^{exponent} mod {modulus}", str(pow(base, exponent, modulus))
    )


_HANDLERS = {
    "factor": _do_factor,
    "isprime": _do_isprime,
    "gcd": _do_gcd,
    "lcm": _do_lcm,
    "factorial": _do_factorial,
    "binomial": _do_binomial,
    "totient": _do_totient,
    "divisors": _do_divisors,
    "nextprime": _do_nextprime,
    "nthprime": _do_nthprime,
    "mod": _do_mod,
    "powmod": _do_powmod,
}
