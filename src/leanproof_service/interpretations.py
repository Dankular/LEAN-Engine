from __future__ import annotations

import json
import os
import re
from time import perf_counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from .symbolic import SymbolicRequest, run_symbolic


_REGISTRY_PATH = Path(__file__).resolve().parent / "assumption_registry.json"
_TRIG_QUERY = re.compile(
    r"^\s*(sin|cos|tan)\s*\(\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\s*(°)?\s*\)"
    r"\s*(?:(?:in\s+)?(degrees?|radians?|deg|rad))?\s*$",
    re.IGNORECASE,
)
_TRIG_LITERAL = re.compile(r"^\s*(sin|cos|tan)\s*\(\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\s*\)\s*$", re.I)
_LOG_LITERAL = re.compile(r"^\s*log\s*\(\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\s*\)\s*$", re.I)


class InterpretationError(ValueError):
    pass


class AssumptionSelectionError(InterpretationError):
    pass


def assumption_registry() -> dict[str, Any]:
    """Read the checked-in, versioned service-owned assumption catalog."""
    data = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    ids = [item["id"] for item in data["assumptions"]]
    if len(ids) != len(set(ids)):
        raise RuntimeError("assumption registry contains duplicate IDs")
    return data


def query_with_interpretations(
    query: str,
    selections: list[str],
    *,
    compute: bool = True,
) -> dict[str, Any]:
    if not query or len(query) > 4000:
        raise InterpretationError("input must contain 1 to 4000 characters")

    parsing_started = perf_counter()
    registry = assumption_registry()
    by_id = {item["id"]: item for item in registry["assumptions"]}
    trig = _TRIG_QUERY.fullmatch(query)
    if trig:
        function, number, degree_mark, unit_word = trig.groups()
        expression = f"{function.lower()}({number})"
        explicit_unit = _normalize_unit(unit_word) if unit_word else ("degrees" if degree_mark else None)
        candidates = [item for item in registry["assumptions"] if item["type"] == "AngleUnit"]
        candidate_ids = {item["id"] for item in candidates}
    else:
        # Bare sin(30) is an ambiguity candidate. Other expressions continue through
        # the existing safe symbolic grammar and have no AngleUnit assumption.
        bare_trig = _TRIG_LITERAL.fullmatch(query)
        if bare_trig:
            function, number = bare_trig.groups()
            expression = f"{function.lower()}({number})"
            candidates = [item for item in registry["assumptions"] if item["type"] == "AngleUnit"]
            candidate_ids = {item["id"] for item in candidates}
            explicit_unit = None
        else:
            bare_log = _LOG_LITERAL.fullmatch(query)
            if bare_log:
                function, number = "log", bare_log.group(1)
                expression = f"log({number})"
                candidates = [item for item in registry["assumptions"] if item["type"] == "LogBase"]
                candidate_ids = {item["id"] for item in candidates}
                explicit_unit = None
            else:
                expression = query.strip()
                candidates = []
                candidate_ids = set()
                explicit_unit = None

    selected_items: list[dict[str, Any]] = []
    if selections and not candidates:
        raise AssumptionSelectionError("assumption selection is not applicable to this input")
    if len(selections) != len(set(selections)):
        raise AssumptionSelectionError("assumption selections cannot contain duplicates")
    for selection in selections:
        if selection not in by_id:
            raise AssumptionSelectionError(f"unknown service assumption ID: {selection}")
        if selection not in candidate_ids:
            raise AssumptionSelectionError(f"assumption {selection} is incompatible with this input")
        selected_items.append(by_id[selection])

    if len(selected_items) > len({item["type"] for item in selected_items}):
        raise AssumptionSelectionError("select at most one assumption value for each assumption type")

    typed_ast: dict[str, Any]
    if candidates:
        classifier = _rank_candidates(query, candidates)
        if selected_items:
            chosen = selected_items[0]
            selection_source = "caller_override"
            if (
                chosen["type"] == "AngleUnit"
                and explicit_unit
                and chosen["argument_unit"] != explicit_unit
            ):
                raise AssumptionSelectionError(
                    f"{chosen['id']} conflicts with the explicit {explicit_unit} unit in input"
                )
        elif explicit_unit:
            chosen = next(item for item in candidates if item["argument_unit"] == explicit_unit)
            selection_source = "explicit_input"
        else:
            chosen = next(item for item in candidates if item["service_default"])
            selection_source = "service_default"

        if chosen["type"] == "AngleUnit":
            if chosen["argument_unit"] == "degrees":
                expression = f"{function.lower()}(({number})*pi/180)"
            typed_ast = {
                "node": "call",
                "function": function.lower(),
                "argument": {
                    "kind": "quantity", "magnitude": number, "unit": chosen["argument_unit"]
                },
            }
        else:
            base = chosen["argument_base"]
            expression = f"log({number}, 10)" if base == "10" else f"log({number})"
            typed_ast = {
                "node": "call",
                "function": "log",
                "argument": {"kind": "number", "value": number},
                "base": base,
            }

        assumption_payload = [{
            "type": chosen["type"],
            "selected": chosen["id"],
            "selection_source": selection_source,
            "registry_version": registry["registry_version"],
            "values": [
                {
                    "input": item["id"],
                    "desc": item.get("argument_unit", item.get("argument_base", item["description"])),
                    "entry_version": item["entry_version"],
                }
                for item in candidates
            ],
        }]
    else:
        typed_ast = {"node": "symbolic_expression", "source": expression}
        classifier = {
            "name": "not_applicable",
            "method": "no ambiguity candidates generated",
            "ranks_candidates_only": True,
        }
        assumption_payload = []

    preparation_timing = perf_counter() - parsing_started
    if compute:
        parsed = None
        parse_timing = preparation_timing
    else:
        parsed = run_symbolic(SymbolicRequest(operation="validate", expression=expression))
        if parsed.get("timed_out"):
            raise InterpretationError("input parsing exceeded its time limit")
        if parsed.get("error"):
            raise InterpretationError(parsed.get("error", "input could not be parsed"))
        parse_timing = preparation_timing + float(parsed.get("parse_timing_seconds", 0.0))

    if not compute:
        symbolic = parsed or {}
        outcome = "validated"
        result = None
    else:
        symbolic = run_symbolic(SymbolicRequest(operation="simplify", expression=expression))
        parse_timing += float(symbolic.get("parse_timing_seconds", 0.0))
        if symbolic.get("timed_out"):
            outcome = "timeout"
            result = None
        elif symbolic.get("error"):
            raise InterpretationError(symbolic.get("error", "symbolic evaluation failed"))
        else:
            outcome = "computed"
            result = symbolic.get("result")

    return {
        "input": query,
        "outcome": outcome,
        "parse_timing": parse_timing,
        "assumptions": assumption_payload,
        "interpretation": {
            "kind": "typed_symbolic_expression",
            "typed_ast": typed_ast,
            "expression": expression,
            "solver_route": "sympy",
        },
        "classification": classifier,
        "result": result,
        "input_mathml": symbolic.get("input_mathml"),
        "result_mathml": symbolic.get("result_mathml"),
        "evidence": symbolic.get("evidence", {
            "kind": "interpretation_only" if not compute else "symbolic_timeout",
            "engine": "typed_parser" if not compute else "sympy",
            "verified": False,
        }),
        "qualification": "Candidate ranking and symbolic computation are not formal proof.",
    }


def _normalize_unit(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.lower()
    return "degrees" if normalized in {"degree", "degrees", "deg"} else "radians"


def _rank_candidates(query: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if candidates[0]["type"] != "AngleUnit":
        scores = _log_base_scores(query, candidates)
        ranked = sorted(candidates, key=lambda item: -scores[item["id"]])
        return {
            "name": "deterministic_lexical_baseline",
            "method": "lexical_base_cues_with_neutral_tie",
            "ranks_candidates_only": True,
            "selection_authority": False,
            "scores_calibrated_for_service": False,
            "ranked": [
                {"input": item["id"], "score": round(scores[item["id"]], 6)}
                for item in ranked
            ],
        }
    requested = os.environ.get("ASSUMPTION_CLASSIFIER", "heuristic").lower()
    if requested == "decima":
        try:
            decima_scores = _decima_scores(query, candidates)
            scores = decima_scores
            name = os.environ.get("DECIMA_MODEL_ID", "amyrmahdy/decima-small")
            method = "optional_decima_choose"
            fallback_reason = None
        except Exception as exc:
            scores = _heuristic_scores(query, candidates)
            name = "deterministic_lexical_baseline"
            method = "fallback_after_optional_decima_unavailable"
            fallback_reason = str(exc)[:240]
    else:
        scores = _heuristic_scores(query, candidates)
        name = "deterministic_lexical_baseline"
        method = "lexical_unit_cues_with_neutral_tie"
        fallback_reason = None

    ranked = sorted(candidates, key=lambda item: -scores[item["id"]])
    return {
        "name": name,
        "method": method,
        "ranks_candidates_only": True,
        "selection_authority": False,
        "scores_calibrated_for_service": False,
        "ranked": [{"input": item["id"], "score": round(scores[item["id"]], 6)} for item in ranked],
        **({"fallback_reason": fallback_reason} if fallback_reason else {}),
    }


def _heuristic_scores(query: str, candidates: list[dict[str, Any]]) -> dict[str, float]:
    lowered = query.lower()
    degree_cue = bool(re.search(r"\b(degrees?|deg)\b|°", lowered))
    radian_cue = bool(re.search(r"\b(radians?|rad)\b", lowered))
    scores: dict[str, float] = {}
    for item in candidates:
        unit = item["argument_unit"]
        if degree_cue and not radian_cue:
            scores[item["id"]] = 0.8 if unit == "degrees" else 0.2
        elif radian_cue and not degree_cue:
            scores[item["id"]] = 0.8 if unit == "radians" else 0.2
        else:
            scores[item["id"]] = 0.5
    return scores


def _log_base_scores(query: str, candidates: list[dict[str, Any]]) -> dict[str, float]:
    lowered = query.lower()
    base10_cue = bool(re.search(r"\b(log10|common logarithm|base\s*10)\b", lowered))
    natural_cue = bool(re.search(r"\b(ln|natural logarithm|base\s*e)\b", lowered))
    scores: dict[str, float] = {}
    for item in candidates:
        base = item["argument_base"]
        if base10_cue and not natural_cue:
            scores[item["id"]] = 0.8 if base == "10" else 0.2
        elif natural_cue and not base10_cue:
            scores[item["id"]] = 0.8 if base == "e" else 0.2
        else:
            scores[item["id"]] = 0.5
    return scores


@lru_cache(maxsize=1)
def _load_decima() -> Any:
    from decima import Decima

    model_id = os.environ.get("DECIMA_MODEL_ID", "amyrmahdy/decima-small")
    return Decima.from_pretrained(model_id)


def _decima_scores(query: str, candidates: list[dict[str, Any]]) -> dict[str, float]:
    from decima import Question

    model = _load_decima()
    options = [f"{item['id']}: {item['description']}" for item in candidates]
    question = Question(
        "Which angle-unit interpretation best matches the query?",
        options,
    )
    probabilities = model.decide(query, question)
    return {
        item["id"]: float(probabilities[option])
        for item, option in zip(candidates, options, strict=True)
    }
