from __future__ import annotations

import hashlib
import json
from fractions import Fraction
import time
from typing import Any

import z3

from .lean_backend import check_with_lean, render_statement
from .models import BinaryTerm, ClaimRequest, Comparison, NumberTerm, PowerTerm, Sort, Term, VariableTerm


def analyze(request: ClaimRequest, *, attempt_lean: bool = True) -> dict[str, Any]:
    deadline = time.monotonic() + request.budget.wall_seconds
    sort = request.variables[0].sort if request.variables else Sort.INT
    z3_vars = {
        variable.name: z3.Int(variable.name) if variable.sort is Sort.INT else z3.Real(variable.name)
        for variable in request.variables
    }
    assumptions = [to_z3(item, z3_vars, sort) for item in request.assumptions]
    claim = to_z3(request.claim, z3_vars, sort)

    consistency = z3.Solver()
    consistency.set(timeout=max(1, _remaining_ms(deadline)))
    consistency.add(*assumptions)
    assumptions_result = consistency.check()
    if assumptions_result == z3.unsat:
        return _result(
            request, "inconsistent_assumptions", "unsat", "not_attempted",
            assumption_consistency="unsat",
            diagnostics="The assumptions contradict one another in the submitted arithmetic model.",
        )
    if assumptions_result == z3.unknown:
        return _result(
            request, "unknown", "unknown", "not_attempted",
            assumption_consistency="unknown", diagnostics=consistency.reason_unknown(),
        )

    solver = z3.Solver()
    remaining_ms = _remaining_ms(deadline)
    if remaining_ms <= 0:
        return _result(
            request, "timeout", "not_completed", "not_attempted",
            assumption_consistency="sat", diagnostics="The request wall-clock budget expired.",
        )
    solver.set(timeout=remaining_ms)
    solver.add(*assumptions)
    solver.add(z3.Not(claim))
    result = solver.check()
    if result == z3.sat:
        model = solver.model()
        try:
            witness = {
                name: _model_value(model.eval(variable, model_completion=True), sort)
                for name, variable in z3_vars.items()
            }
        except ValueError:
            return _result(
                request, "unknown", "sat", "not_attempted",
                assumption_consistency="sat",
                diagnostics=(
                    "Z3 found a real algebraic counterexample that this service cannot "
                    "replay as an exact rational witness."
                ),
            )
        if not validate_witness(request, witness):
            return _result(
                request, "error", "sat", "not_attempted",
                assumption_consistency="sat",
                diagnostics="Z3 returned a model that failed independent AST replay.",
            )
        return _result(
            request, "counterexample_found", "sat", "not_attempted",
            assumption_consistency="sat", counterexample=witness,
        )
    if result == z3.unknown:
        return _result(
            request, "unknown", "unknown", "not_attempted",
            assumption_consistency="sat", diagnostics=solver.reason_unknown(),
        )

    remaining_seconds = deadline - time.monotonic()
    if attempt_lean and remaining_seconds <= 0:
        return _result(
            request, "timeout", "unsat", "not_attempted",
            assumption_consistency="sat", diagnostics="The request wall-clock budget expired before Lean checking.",
        )
    lean_result = check_with_lean(request, remaining_seconds) if attempt_lean else None
    lean_status = lean_result.status if lean_result else "not_attempted"
    outcome = "verified" if lean_result and lean_result.status == "checked" else "solver_unsat"
    response = _result(
        request, outcome, "unsat", lean_status,
        assumption_consistency="sat",
        diagnostics=lean_result.diagnostics if lean_result else None,
        axiom_audit=list(lean_result.axioms) if lean_result and lean_result.status == "checked" else None,
    )
    if lean_result:
        response["evidence"]["lean_source"] = lean_result.source
        response["evidence"]["lean_source_sha256"] = hashlib.sha256(
            lean_result.source.encode("utf-8")
        ).hexdigest()
    return response


def _remaining_ms(deadline: float) -> int:
    return int((deadline - time.monotonic()) * 1000)


def to_z3(comparison: Comparison, variables: dict[str, Any], sort: Sort) -> Any:
    left = term_to_z3(comparison.left, variables, sort)
    right = term_to_z3(comparison.right, variables, sort)
    return {
        "eq": left == right,
        "ne": left != right,
        "lt": left < right,
        "le": left <= right,
        "gt": left > right,
        "ge": left >= right,
    }[comparison.op]


def term_to_z3(term: Term, variables: dict[str, Any], sort: Sort) -> Any:
    if isinstance(term, VariableTerm):
        return variables[term.name]
    if isinstance(term, NumberTerm):
        value = Fraction(term.value)
        if sort is Sort.INT:
            return z3.IntVal(value.numerator)
        return z3.RealVal(f"{value.numerator}/{value.denominator}")
    if isinstance(term, PowerTerm):
        if term.exponent == 0:
            return z3.IntVal(1) if sort is Sort.INT else z3.RealVal(1)
        if term.exponent == 1:
            return term_to_z3(term.base, variables, sort)
        return term_to_z3(term.base, variables, sort) ** term.exponent
    if isinstance(term, BinaryTerm):
        left = term_to_z3(term.left, variables, sort)
        right = term_to_z3(term.right, variables, sort)
        if term.kind == "add":
            return left + right
        if term.kind == "sub":
            return left - right
        return left * right
    raise TypeError(f"unsupported term type: {type(term)!r}")


def validate_witness(request: ClaimRequest, witness: dict[str, str]) -> bool:
    try:
        values = {
            variable.name: int(witness[variable.name])
            if variable.sort is Sort.INT
            else Fraction(witness[variable.name])
            for variable in request.variables
        }
        return all(eval_comparison(item, values) for item in request.assumptions) and not eval_comparison(
            request.claim, values
        )
    except (KeyError, ValueError, ZeroDivisionError, TypeError):
        return False


def eval_term(term: Term, values: dict[str, int | Fraction]) -> int | Fraction:
    if isinstance(term, VariableTerm):
        return values[term.name]
    if isinstance(term, NumberTerm):
        value = Fraction(term.value)
        return value.numerator if value.denominator == 1 else value
    if isinstance(term, PowerTerm):
        return eval_term(term.base, values) ** term.exponent
    if isinstance(term, BinaryTerm):
        left = eval_term(term.left, values)
        right = eval_term(term.right, values)
        if term.kind == "add":
            return left + right
        if term.kind == "sub":
            return left - right
        return left * right
    raise TypeError(f"unsupported term type: {type(term)!r}")


def eval_comparison(comparison: Comparison, values: dict[str, int | Fraction]) -> bool:
    left = eval_term(comparison.left, values)
    right = eval_term(comparison.right, values)
    return {
        "eq": left == right,
        "ne": left != right,
        "lt": left < right,
        "le": left <= right,
        "gt": left > right,
        "ge": left >= right,
    }[comparison.op]


def _model_value(value: Any, sort: Sort) -> str:
    if sort is Sort.INT:
        if not z3.is_int_value(value):
            raise ValueError("expected an integer model value")
        return str(value.as_long())
    if z3.is_rational_value(value):
        rational = value.as_fraction()
        return str(rational.numerator) if rational.denominator == 1 else f"{rational.numerator}/{rational.denominator}"
    raise ValueError("expected an exact rational model value")


def _result(
    request: ClaimRequest,
    outcome: str,
    solver_status: str,
    lean_status: str,
    *,
    assumption_consistency: str,
    counterexample: dict[str, str] | None = None,
    diagnostics: str | None = None,
    axiom_audit: list[str] | None = None,
) -> dict[str, Any]:
    sort = request.variables[0].sort if request.variables else Sort.INT
    evidence: dict[str, Any] = {
        "kind": "lean_kernel_checked" if outcome == "verified" else "z3_result",
        "solver": "z3",
        "solver_version": z3.get_version_string(),
        "solver_status": solver_status,
        "lean_status": lean_status,
    }
    if axiom_audit is not None:
        evidence["axiom_audit"] = {"status": "passed", "dependencies": axiom_audit}
    payload: dict[str, Any] = {
        "outcome": outcome,
        "formal_statement": render_statement(request),
        "formalization_fidelity": "not_checked",
        "specification_sha256": _specification_hash(request),
        "assumption_consistency": assumption_consistency,
        "evidence": evidence,
        "qualification": (
            "The submitted typed statement and assumptions are what was checked. "
            "Natural-language fidelity and the truth of empirical premises are separate."
        ),
        "arithmetic_sort": sort.value,
    }
    if request.source_text is not None:
        payload["source_text_sha256"] = hashlib.sha256(
            request.source_text.encode("utf-8")
        ).hexdigest()
    if counterexample is not None:
        payload["counterexample"] = counterexample
        payload["evidence"]["kind"] = "validated_counterexample"
    if diagnostics:
        payload["diagnostics"] = diagnostics
    return payload


def _specification_hash(request: ClaimRequest) -> str:
    normalized = request.model_dump(mode="json", exclude={"source_text", "budget"})
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
