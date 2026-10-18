from __future__ import annotations

import html
import json
import os
import re
from datetime import date
from time import perf_counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from .input_syntax import MathInputError, parse_math_input
from .symbolic import SymbolicRequest, run_symbolic


_REGISTRY_PATH = Path(__file__).resolve().parent / "assumption_registry.json"
_TRIG_QUERY = re.compile(
    r"^\s*(sin|cos|tan)\s*\(\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\s*(°)?\s*\)"
    r"\s*(?:(?:in\s+)?(degrees?|radians?|deg|rad))?\s*$",
    re.IGNORECASE,
)
_TRIG_LITERAL = re.compile(r"^\s*(sin|cos|tan)\s*\(\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\s*\)\s*$", re.I)
_LOG_LITERAL = re.compile(r"^\s*log\s*\(\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\s*\)\s*$", re.I)
_NUMBER_PATTERN = r"-?(?:\d+(?:\.\d*)?|\.\d+)"
_UNIT_LITERAL = re.compile(rf"^\s*(?P<number>{_NUMBER_PATTERN})\s*(?P<unit>m)\s*$")
_NUMERIC_DATE = re.compile(
    r"^\s*(?P<first>\d{1,2})\s*/\s*(?P<second>\d{1,2})\s*/\s*(?P<year>\d{4})\s*$"
)
_TRIG_IN_EXPRESSION = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<function>sin|cos|tan)\s*\(\s*"
    rf"(?P<number>{_NUMBER_PATTERN})\s*(?P<degree_mark>\u00b0)?\s*\)"
    rf"(?:\s*(?:in\s+)?(?P<unit>degrees?|radians?|deg|rad)\b)?",
    re.IGNORECASE,
)
_LOG_IN_EXPRESSION = re.compile(
    rf"(?<![A-Za-z0-9_])log\s*\(\s*(?P<number>{_NUMBER_PATTERN})\s*\)",
    re.IGNORECASE,
)


class InterpretationError(ValueError):
    pass


class AssumptionSelectionError(InterpretationError):
    pass


class InterpretationTimeout(InterpretationError):
    def __init__(self, parse_timing_seconds: float) -> None:
        super().__init__("input parsing exceeded its time limit")
        self.parse_timing_seconds = parse_timing_seconds


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
    numeric_precision: int = 50,
    time_limit_seconds: float = 5.0,
    parse_timeout_seconds: float = 5.0,
    pod_timeout_seconds: float | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    if not query or len(query) > 4000:
        raise InterpretationError("input must contain 1 to 4000 characters")

    parsing_started = perf_counter()
    source_query = query
    date_input = numeric_date_options(source_query)
    if date_input is not None:
        return _interpret_numeric_date(
            source_query,
            date_input,
            selections,
            compute=compute,
            parse_timing=perf_counter() - parsing_started,
        )
    try:
        parsed_input = parse_math_input(query)
    except MathInputError as exc:
        raise InterpretationError(str(exc)) from exc
    query = parsed_input.expression

    registry = assumption_registry()
    by_id = {item["id"]: item for item in registry["assumptions"]}
    unit_literal = _UNIT_LITERAL.fullmatch(source_query) or _UNIT_LITERAL.fullmatch(query)
    trig = _TRIG_QUERY.fullmatch(query)
    compound = None
    if unit_literal:
        number = unit_literal.group("number")
        candidates = [item for item in registry["assumptions"] if item["type"] == "Unit"]
        candidate_ids = {item["id"] for item in candidates}
        explicit_unit = None
        expression = query
    elif trig:
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
                compound = _compound_interpretation(query, selections, registry, by_id)
                if compound is not None:
                    expression = compound["expression"]
                    typed_ast = compound["typed_ast"]
                    classifier = compound["classifier"]
                    assumption_payload = compound["assumptions"]
                else:
                    expression = query.strip()
                    candidates = []
                    candidate_ids = set()
                    explicit_unit = None

    if compound is None:
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
            elif chosen["type"] == "LogBase":
                base = chosen["argument_base"]
                expression = f"log({number}, 10)" if base == "10" else f"log({number})"
                typed_ast = {
                    "node": "call",
                    "function": "log",
                    "argument": {"kind": "number", "value": number},
                    "base": base,
                }
            else:
                expression = f"{number}*{chosen['symbolic_unit']}"
                typed_ast = {
                    "node": "quantity",
                    "magnitude": number,
                    "unit": chosen["argument_unit"],
                    "unit_symbol": chosen["symbolic_unit"],
                    "unit_type": chosen["symbolic_unit_type"],
                    "wolfram_unit": chosen["wolfram_unit"],
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
    effective_parse_timeout = parse_timeout_seconds
    if deadline is not None:
        effective_parse_timeout = min(effective_parse_timeout, deadline - perf_counter())
    if effective_parse_timeout <= 0.001:
        raise InterpretationTimeout(perf_counter() - parsing_started)
    named_units = _named_unit_bindings(typed_ast)
    parsed = run_symbolic(SymbolicRequest(
        operation="validate",
        expression=expression,
        named_units=named_units,
        time_limit_seconds=effective_parse_timeout,
    ))
    if parsed.get("timed_out"):
        raise InterpretationTimeout(perf_counter() - parsing_started)
    if parsed.get("error"):
        raise InterpretationError(parsed.get("error", "input could not be parsed"))
    parse_timing = preparation_timing + float(parsed.get("parse_timing_seconds", 0.0))

    if not compute:
        symbolic = parsed
        outcome = "validated"
        result = None
    else:
        remaining_seconds = time_limit_seconds
        if deadline is not None:
            remaining_seconds = min(remaining_seconds, deadline - perf_counter())
        if pod_timeout_seconds is not None:
            remaining_seconds = min(remaining_seconds, pod_timeout_seconds)
        if remaining_seconds <= 0.001:
            symbolic = {"timed_out": True, "limit_seconds": max(0.0, remaining_seconds)}
            outcome = "timeout"
            result = None
        else:
            symbolic = run_symbolic(SymbolicRequest(
                operation="simplify",
                expression=expression,
                named_units=named_units,
                time_limit_seconds=remaining_seconds,
                numeric_precision=numeric_precision,
            ))
            parse_timing += float(symbolic.get("parse_timing_seconds", 0.0))
            if symbolic.get("timed_out"):
                outcome = "timeout"
                result = None
            elif symbolic.get("error"):
                raise InterpretationError(symbolic.get("error", "symbolic evaluation failed"))
            else:
                outcome = "computed"
                result = symbolic.get("result")
                if typed_ast.get("node") == "quantity":
                    result = f"{typed_ast['magnitude']} {typed_ast['unit']}"

    input_mathml = symbolic.get("input_mathml")
    result_mathml = symbolic.get("result_mathml")
    if typed_ast.get("node") == "quantity":
        input_mathml = _quantity_mathml(typed_ast)
        if outcome == "computed":
            result_mathml = input_mathml

    return {
        "input": source_query,
        "outcome": outcome,
        **({"timeout_stage": "pod"} if outcome == "timeout" else {}),
        "parse_timing": parse_timing,
        "assumptions": assumption_payload,
        "interpretation": {
            "kind": (
                "typed_quantity"
                if typed_ast.get("node") == "quantity"
                else "typed_symbolic_expression"
            ),
            "typed_ast": typed_ast,
            "expression": expression,
            "solver_route": "sympy",
            **({"input_format": parsed_input.input_format} if parsed_input.input_format != "native" else {}),
        },
        "classification": classifier,
        "result": result,
        "numeric_approximation": symbolic.get("numeric_approximation"),
        "input_mathml": input_mathml,
        "result_mathml": result_mathml,
        "evidence": symbolic.get("evidence", {
            "kind": "interpretation_only" if not compute else "symbolic_timeout",
            "engine": "typed_parser" if not compute else "sympy",
            "verified": False,
        }),
        "qualification": "Candidate ranking and symbolic computation are not formal proof.",
    }


def numeric_date_options(query: str) -> list[dict[str, Any]] | None:
    """Return valid month/day/year and day/month/year readings for a numeric date."""
    match = _NUMERIC_DATE.fullmatch(query)
    if match is None:
        return None
    first = int(match.group("first"))
    second = int(match.group("second"))
    year = int(match.group("year"))
    options: list[dict[str, Any]] = []
    for assumption_id, input_order, month, day in (
        ("date:month-day-year", "month/day/year", first, second),
        ("date:day-month-year", "day/month/year", second, first),
    ):
        try:
            parsed_date = date(year, month, day)
        except ValueError:
            continue
        options.append({
            "assumption_id": assumption_id,
            "input_order": input_order,
            "year": parsed_date.year,
            "month": parsed_date.month,
            "day": parsed_date.day,
            "iso_date": parsed_date.isoformat(),
            "display": f"{parsed_date.strftime('%B')} {parsed_date.day}, {parsed_date.year}",
        })
    return options or None


def _interpret_numeric_date(
    source_query: str,
    options: list[dict[str, Any]],
    selections: list[str],
    *,
    compute: bool,
    parse_timing: float,
) -> dict[str, Any]:
    registry = assumption_registry()
    by_id = {item["id"]: item for item in registry["assumptions"]}
    candidate_options = [item for item in options if item["assumption_id"] in by_id]
    if len(candidate_options) != len(options):
        raise RuntimeError("assumption registry is missing a valid DateOrder value")
    candidate_ids = {item["assumption_id"] for item in candidate_options}

    if len(selections) != len(set(selections)):
        raise AssumptionSelectionError("assumption selections cannot contain duplicates")
    selected_items: list[dict[str, Any]] = []
    for selection in selections:
        if selection not in by_id:
            raise AssumptionSelectionError(f"unknown service assumption ID: {selection}")
        if selection not in candidate_ids:
            raise AssumptionSelectionError(f"assumption {selection} is incompatible with this input")
        selected_items.append(by_id[selection])
    if len(selected_items) > 1:
        raise AssumptionSelectionError("select at most one assumption value for DateOrder")

    if selected_items:
        chosen_id = selected_items[0]["id"]
        selection_source = "caller_override"
    elif len(candidate_options) == 1:
        chosen_id = candidate_options[0]["assumption_id"]
        selection_source = "unambiguous_input"
    else:
        default = next(
            (
                item for item in candidate_options
                if by_id[item["assumption_id"]].get("service_default")
            ),
            None,
        )
        if default is None:
            raise RuntimeError("DateOrder registry has no service default")
        chosen_id = default["assumption_id"]
        selection_source = "service_default"
    chosen = next(item for item in candidate_options if item["assumption_id"] == chosen_id)

    assumption_payload: list[dict[str, Any]] = []
    if len(candidate_options) > 1:
        assumption_payload = [{
            "type": "DateOrder",
            "selected": chosen_id,
            "selection_source": selection_source,
            "registry_version": registry["registry_version"],
            "values": [
                {
                    "input": item["assumption_id"],
                    "desc": by_id[item["assumption_id"]]["description"],
                    "entry_version": by_id[item["assumption_id"]]["entry_version"],
                }
                for item in candidate_options
            ],
        }]

    typed_ast = {
        "node": "date",
        "calendar": "gregorian",
        "input": source_query.strip(),
        "input_order": chosen["input_order"],
        "year": chosen["year"],
        "month": chosen["month"],
        "day": chosen["day"],
        "iso_date": chosen["iso_date"],
        "display": chosen["display"],
    }
    classifier = {
        "name": "deterministic_lexical_baseline",
        "method": "neutral ranking for valid numeric date orders",
        "ranks_candidates_only": True,
        "selection_authority": False,
        "scores_calibrated_for_service": False,
        "ranked": [
            {"input": item["assumption_id"], "score": 0.5}
            for item in candidate_options
        ],
    }
    date_object = f"DateObject[{{{chosen['year']}, {chosen['month']}, {chosen['day']}}}]"
    return {
        "input": source_query,
        "outcome": "computed" if compute else "validated",
        "parse_timing": parse_timing,
        "assumptions": assumption_payload,
        "interpretation": {
            "kind": "typed_date",
            "typed_ast": typed_ast,
            "expression": date_object,
            "solver_route": "gregorian_calendar",
        },
        "classification": classifier,
        "result": chosen["display"] if compute else None,
        "numeric_approximation": None,
        "input_mathml": _text_mathml(source_query.strip()),
        "result_mathml": _text_mathml(chosen["display"]) if compute else None,
        "evidence": {
            "kind": "deterministic_calendar_interpretation" if compute else "interpretation_only",
            "engine": "python.datetime",
            "verified": False,
        },
        "qualification": "Date parsing follows the selected service date-order interpretation; it is not a formal proof.",
    }


def _text_mathml(value: str) -> str:
    return (
        '<math xmlns="http://www.w3.org/1998/Math/MathML"><mtext>'
        f"{html.escape(value)}"
        "</mtext></math>"
    )


def _compound_interpretation(
    query: str,
    selections: list[str],
    registry: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Resolve supported numeric trig and bare-log ambiguities inside expressions."""
    trig_matches = list(_TRIG_IN_EXPRESSION.finditer(query))
    log_matches = list(_LOG_IN_EXPRESSION.finditer(query))
    if not trig_matches and not log_matches:
        return None

    candidates_by_type = {
        "AngleUnit": [item for item in registry["assumptions"] if item["type"] == "AngleUnit"],
        "LogBase": [item for item in registry["assumptions"] if item["type"] == "LogBase"],
    }
    grouped_matches: dict[str, list[re.Match[str]]] = {"AngleUnit": [], "LogBase": []}
    replacements: list[tuple[int, int, str]] = []
    resolved_calls: list[dict[str, Any]] = []

    for match in trig_matches:
        function = match.group("function").lower()
        number = match.group("number")
        unit_word = match.group("unit")
        explicit_unit = (
            _normalize_unit(unit_word)
            if unit_word
            else ("degrees" if match.group("degree_mark") else None)
        )
        if explicit_unit:
            replacement = (
                f"{function}(({number})*pi/180)"
                if explicit_unit == "degrees"
                else f"{function}({number})"
            )
            replacements.append((match.start(), match.end(), replacement))
            resolved_calls.append({
                "function": function,
                "argument": {"kind": "quantity", "magnitude": number, "unit": explicit_unit},
                "selection_source": "explicit_input",
            })
        else:
            grouped_matches["AngleUnit"].append(match)

    grouped_matches["LogBase"].extend(log_matches)
    active_types = {kind for kind, matches in grouped_matches.items() if matches}
    selected_by_type: dict[str, dict[str, Any]] = {}
    if len(selections) != len(set(selections)):
        raise AssumptionSelectionError("assumption selections cannot contain duplicates")
    if selections and not active_types:
        raise AssumptionSelectionError("assumption selection is not applicable to this input")
    for selection in selections:
        item = by_id.get(selection)
        if item is None:
            raise AssumptionSelectionError(f"unknown service assumption ID: {selection}")
        kind = item["type"]
        if kind not in active_types or selection not in {
            candidate["id"] for candidate in candidates_by_type[kind]
        }:
            raise AssumptionSelectionError(f"assumption {selection} is incompatible with this input")
        if kind in selected_by_type:
            raise AssumptionSelectionError("select at most one assumption value for each assumption type")
        selected_by_type[kind] = item

    assumption_payload: list[dict[str, Any]] = []
    ambiguity_rankings: list[dict[str, Any]] = []
    for kind in ("AngleUnit", "LogBase"):
        matches = grouped_matches[kind]
        if not matches:
            continue
        candidates = candidates_by_type[kind]
        ranking = _rank_candidates(query, candidates)
        chosen = selected_by_type.get(kind) or next(
            item for item in candidates if item["service_default"]
        )
        selection_source = "caller_override" if kind in selected_by_type else "service_default"
        occurrence_values = [
            {
                "function": match.group("function").lower() if kind == "AngleUnit" else "log",
                "argument": match.group("number"),
            }
            for match in matches
        ]
        ambiguity_rankings.append({
            "type": kind,
            "occurrences": occurrence_values,
            "ranking": ranking,
        })
        assumption_payload.append({
            "type": kind,
            "selected": chosen["id"],
            "selection_source": selection_source,
            "registry_version": registry["registry_version"],
            "occurrences": occurrence_values,
            "values": [
                {
                    "input": item["id"],
                    "desc": item.get("argument_unit", item.get("argument_base", item["description"])),
                    "entry_version": item["entry_version"],
                }
                for item in candidates
            ],
        })

        for match in matches:
            if kind == "AngleUnit":
                function = match.group("function").lower()
                number = match.group("number")
                unit = chosen["argument_unit"]
                replacement = (
                    f"{function}(({number})*pi/180)" if unit == "degrees" else f"{function}({number})"
                )
                resolved_calls.append({
                    "function": function,
                    "argument": {"kind": "quantity", "magnitude": number, "unit": unit},
                    "selection_source": selection_source,
                })
            else:
                number = match.group("number")
                base = chosen["argument_base"]
                replacement = f"log({number}, 10)" if base == "10" else f"log({number})"
                resolved_calls.append({
                    "function": "log",
                    "argument": {"kind": "number", "value": number},
                    "base": base,
                    "selection_source": selection_source,
                })
            replacements.append((match.start(), match.end(), replacement))

    expression = query
    for start, end, replacement in sorted(replacements, reverse=True):
        expression = expression[:start] + replacement + expression[end:]

    if ambiguity_rankings:
        classifier = {
            "name": "family_specific_candidate_ranker",
            "method": "independent lexical ranking for each ambiguous assumption family",
            "ranks_candidates_only": True,
            "selection_authority": False,
            "rankings": ambiguity_rankings,
        }
    else:
        classifier = {
            "name": "not_applicable",
            "method": "explicit units resolved from input",
            "ranks_candidates_only": True,
            "selection_authority": False,
        }

    return {
        "expression": expression,
        "typed_ast": {
            "node": "symbolic_expression",
            "source": query,
            "resolved_expression": expression,
            "resolved_calls": resolved_calls,
        },
        "classifier": classifier,
        "assumptions": assumption_payload,
    }


def _normalize_unit(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.lower()
    return "degrees" if normalized in {"degree", "degrees", "deg"} else "radians"


def _named_unit_bindings(typed_ast: dict[str, Any]) -> dict[str, str]:
    if typed_ast.get("node") != "quantity":
        return {}
    return {str(typed_ast["unit_symbol"]): str(typed_ast["unit_type"])}


def _quantity_mathml(typed_ast: dict[str, Any]) -> str:
    magnitude = str(typed_ast["magnitude"])
    unit = html.escape(str(typed_ast["unit"]))
    return (
        '<math xmlns="http://www.w3.org/1998/Math/MathML"><mrow>'
        f"<mn>{magnitude}</mn><mspace width=\"thinmathspace\"/><mtext>{unit}</mtext>"
        "</mrow></math>"
    )


def _rank_candidates(query: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    if candidates[0]["type"] == "Unit":
        return {
            "name": "deterministic_lexical_baseline",
            "method": "neutral ranking for the ambiguous unit abbreviation",
            "ranks_candidates_only": True,
            "selection_authority": False,
            "scores_calibrated_for_service": False,
            "ranked": [{"input": item["id"], "score": 0.5} for item in candidates],
        }
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
