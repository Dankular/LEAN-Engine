from __future__ import annotations

import html
import json
import os
import re
from datetime import date, datetime
from time import perf_counter
from functools import lru_cache
from pathlib import Path
from typing import Any

from .input_syntax import MathInputError, parse_math_input
from .physical_units import (
    PHYSICAL_UNITS,
    UNIT_ALIASES_BY_LENGTH,
    UNIT_ALIAS_TO_NAME,
)
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
_UNIT_LITERAL_NAMES = "|".join(re.escape(alias) for alias in (*UNIT_ALIASES_BY_LENGTH, "m"))
_UNIT_LITERAL = re.compile(
    rf"^\s*(?P<number>{_NUMBER_PATTERN})\s*(?P<unit>{_UNIT_LITERAL_NAMES})\s*$",
    re.IGNORECASE,
)
_UNIT_M_IN_EXPRESSION = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<number>{_NUMBER_PATTERN})\s*(?:\*\s*)?m(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_EXPLICIT_UNIT_ALIASES = "|".join(re.escape(alias) for alias in UNIT_ALIASES_BY_LENGTH)
_EXPLICIT_UNIT_IN_SOURCE = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<number>{_NUMBER_PATTERN})\s*(?P<unit>{_EXPLICIT_UNIT_ALIASES})(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_PHYSICAL_UNIT_NAMES = "|".join(
    re.escape(name) for name in sorted(PHYSICAL_UNITS, key=lambda item: -len(item))
)
_PHYSICAL_UNIT_IN_EXPRESSION = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<number>{_NUMBER_PATTERN})\s*\*\s*"
    rf"(?P<symbol>unit_(?:{_PHYSICAL_UNIT_NAMES}))(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_NUMERIC_DATE = re.compile(
    r"^\s*(?P<first>\d{1,2})\s*/\s*(?P<second>\d{1,2})\s*/\s*(?P<year>\d{4})\s*$"
)
_CLOCK_TIME = re.compile(
    r"^\s*(?P<hour>\d{1,2}):(?P<minute>\d{2})(?:\s*(?P<meridiem>am|pm))?\s*$",
    re.IGNORECASE,
)
_MIXED_FRACTION_LITERAL = re.compile(
    r"^\s*(?P<whole>\d+)\s+(?P<numerator>\d+)\s*/\s*(?P<denominator>\d+)\s*$"
)
_LIST_OR_TIMES_LITERAL = re.compile(
    rf"^\s*(?P<number>{_NUMBER_PATTERN})\s+(?P<symbol>[A-Za-z])\s*$"
)
_LIST_OR_NUMBER_LITERAL = re.compile(
    r"^\s*(?P<sign>[+-]?)(?P<first>[1-9]\d{0,2}),(?P<second>\d{3}(?:\.\d+)?)\s*$"
)
_NUMBER_BASE_LITERAL = re.compile(r"^\s*(?P<digits>[01]{8,64})\s*$")
_IMAGINARY_I_LITERAL = re.compile(r"(?<![A-Za-z0-9_])i(?![A-Za-z0-9_])")
_IMAGINARY_I_COEFFICIENT = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<number>{_NUMBER_PATTERN})i(?![A-Za-z0-9_])"
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
_LOG_FUNCTION_IN_EXPRESSION = re.compile(
    rf"(?<![A-Za-z0-9_])log\s+(?P<number>{_NUMBER_PATTERN})(?![A-Za-z0-9_.])",
    re.IGNORECASE,
)
_COORDINATE_DIVERGENCE_QUERY = re.compile(
    r"^\s*div\s*\((?P<arguments>.*)\)\s*$", re.IGNORECASE | re.DOTALL
)
_COORDINATE_GRADIENT_QUERY = re.compile(
    r"^\s*(?:grad|gradient)\s*\((?P<expression>.*)\)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_COORDINATE_LAPLACIAN_QUERY = re.compile(
    r"^\s*(?:lap|laplacian)\s*\((?P<expression>.*)\)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_COORDINATE_CURL_QUERY = re.compile(
    r"^\s*curl\s*\((?P<arguments>.*)\)\s*$", re.IGNORECASE | re.DOTALL
)


class InterpretationError(ValueError):
    pass


class InterpretationValidationError(InterpretationError):
    def __init__(
        self,
        message: str,
        *,
        assumptions: list[dict[str, Any]],
        interpretation: dict[str, Any],
        classification: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.assumptions = assumptions
        self.interpretation = interpretation
        self.classification = classification


class AssumptionSelectionError(InterpretationError):
    pass


class InterpretationTimeout(InterpretationError):
    def __init__(self, parse_timing_seconds: float) -> None:
        super().__init__("input parsing exceeded its time limit")
        self.parse_timing_seconds = parse_timing_seconds


def _split_assumption_selector(selection: str) -> tuple[str, int | None]:
    """Split a service assumption ID from its optional one-based occurrence."""
    if "@" not in selection:
        return selection, None
    assumption_id, possible_index = selection.rsplit("@", 1)
    if not possible_index.isdecimal():
        return selection, None
    if len(possible_index) > 9:
        raise AssumptionSelectionError("assumption occurrence index is out of range")
    occurrence_index = int(possible_index)
    if occurrence_index < 1:
        raise AssumptionSelectionError("assumption occurrence indexes start at 1")
    return assumption_id, occurrence_index


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
    _allow_i_assumption: bool = True,
) -> dict[str, Any]:
    if not query or len(query) > 4000:
        raise InterpretationError("input must contain 1 to 4000 characters")

    parsing_started = perf_counter()
    source_query = query
    coordinate_divergence = _COORDINATE_DIVERGENCE_QUERY.fullmatch(source_query)
    if coordinate_divergence is not None:
        return _interpret_coordinate_divergence(
            source_query,
            coordinate_divergence.group("arguments"),
            selections,
            compute=compute,
            numeric_precision=numeric_precision,
            time_limit_seconds=time_limit_seconds,
            parse_timeout_seconds=parse_timeout_seconds,
            pod_timeout_seconds=pod_timeout_seconds,
            deadline=deadline,
            parse_timing=perf_counter() - parsing_started,
        )
    coordinate_gradient = _COORDINATE_GRADIENT_QUERY.fullmatch(source_query)
    if coordinate_gradient is not None:
        return _interpret_coordinate_gradient(
            source_query,
            coordinate_gradient.group("expression"),
            selections,
            compute=compute,
            numeric_precision=numeric_precision,
            time_limit_seconds=time_limit_seconds,
            parse_timeout_seconds=parse_timeout_seconds,
            pod_timeout_seconds=pod_timeout_seconds,
            deadline=deadline,
            parse_timing=perf_counter() - parsing_started,
        )
    coordinate_laplacian = _COORDINATE_LAPLACIAN_QUERY.fullmatch(source_query)
    if coordinate_laplacian is not None:
        return _interpret_coordinate_laplacian(
            source_query,
            coordinate_laplacian.group("expression"),
            selections,
            compute=compute,
            numeric_precision=numeric_precision,
            time_limit_seconds=time_limit_seconds,
            parse_timeout_seconds=parse_timeout_seconds,
            pod_timeout_seconds=pod_timeout_seconds,
            deadline=deadline,
            parse_timing=perf_counter() - parsing_started,
        )
    coordinate_curl = _COORDINATE_CURL_QUERY.fullmatch(source_query)
    if coordinate_curl is not None:
        return _interpret_coordinate_curl(
            source_query,
            coordinate_curl.group("arguments"),
            selections,
            compute=compute,
            numeric_precision=numeric_precision,
            time_limit_seconds=time_limit_seconds,
            parse_timeout_seconds=parse_timeout_seconds,
            pod_timeout_seconds=pod_timeout_seconds,
            deadline=deadline,
            parse_timing=perf_counter() - parsing_started,
        )
    date_input = numeric_date_options(source_query)
    if date_input is not None:
        return _interpret_numeric_date(
            source_query,
            date_input,
            selections,
            compute=compute,
            parse_timing=perf_counter() - parsing_started,
        )
    clock_time = _CLOCK_TIME.fullmatch(source_query)
    if clock_time is not None:
        return _interpret_clock_time(
            source_query,
            clock_time,
            selections,
            compute=compute,
            parse_timing=perf_counter() - parsing_started,
        )
    number_base = _number_base_digits(source_query)
    if number_base is not None:
        return _interpret_number_base(
            source_query,
            number_base,
            selections,
            compute=compute,
            parse_timing=perf_counter() - parsing_started,
        )
    mixed_fraction = _mixed_fraction_parts(source_query)
    if mixed_fraction is not None:
        return _interpret_mixed_fraction(
            source_query,
            mixed_fraction,
            selections,
            compute=compute,
            numeric_precision=numeric_precision,
            time_limit_seconds=time_limit_seconds,
            parse_timeout_seconds=parse_timeout_seconds,
            pod_timeout_seconds=pod_timeout_seconds,
            deadline=deadline,
            parse_timing=perf_counter() - parsing_started,
        )
    list_or_number = _list_or_number_parts(source_query)
    if list_or_number is not None:
        return _interpret_list_or_number(
            source_query,
            list_or_number,
            selections,
            compute=compute,
            numeric_precision=numeric_precision,
            time_limit_seconds=time_limit_seconds,
            parse_timeout_seconds=parse_timeout_seconds,
            pod_timeout_seconds=pod_timeout_seconds,
            deadline=deadline,
            parse_timing=perf_counter() - parsing_started,
        )
    if _LOG_FUNCTION_IN_EXPRESSION.search(source_query):
        return _interpret_function_log(
            source_query,
            selections,
            compute=compute,
            numeric_precision=numeric_precision,
            time_limit_seconds=time_limit_seconds,
            parse_timeout_seconds=parse_timeout_seconds,
            pod_timeout_seconds=pod_timeout_seconds,
            deadline=deadline,
            parse_timing=perf_counter() - parsing_started,
        )
    if _allow_i_assumption and (
        _IMAGINARY_I_LITERAL.search(source_query)
        or _IMAGINARY_I_COEFFICIENT.search(source_query)
    ):
        return _interpret_imaginary_i(
            source_query,
            selections,
            compute=compute,
            numeric_precision=numeric_precision,
            time_limit_seconds=time_limit_seconds,
            parse_timeout_seconds=parse_timeout_seconds,
            pod_timeout_seconds=pod_timeout_seconds,
            deadline=deadline,
            parse_timing=perf_counter() - parsing_started,
        )
    list_or_times = _list_or_times_parts(source_query)
    if list_or_times is not None:
        return _interpret_list_or_times(
            source_query,
            list_or_times,
            selections,
            compute=compute,
            numeric_precision=numeric_precision,
            time_limit_seconds=time_limit_seconds,
            parse_timeout_seconds=parse_timeout_seconds,
            pod_timeout_seconds=pod_timeout_seconds,
            deadline=deadline,
            parse_timing=perf_counter() - parsing_started,
        )
    try:
        parsed_input = parse_math_input(_normalize_ambiguous_unit_spacing(query))
    except MathInputError as exc:
        raise InterpretationError(str(exc)) from exc
    query = parsed_input.expression

    registry = assumption_registry()
    by_id = {item["id"]: item for item in registry["assumptions"]}
    unit_literal = _UNIT_LITERAL.fullmatch(source_query) or _UNIT_LITERAL.fullmatch(query)
    trig = _TRIG_QUERY.fullmatch(query)
    compound = (
        _compound_interpretation(query, selections, registry, by_id)
        if any("@" in selection for selection in selections)
        else None
    )
    explicit_typed_quantity: dict[str, Any] | None = None
    if compound is not None:
        expression = compound["expression"]
        typed_ast = compound["typed_ast"]
        classifier = compound["classifier"]
        assumption_payload = compound["assumptions"]
    elif unit_literal:
        number = unit_literal.group("number")
        unit_alias = unit_literal.group("unit").casefold()
        if unit_alias == "m":
            candidates = [item for item in registry["assumptions"] if item["type"] == "Unit"]
            candidate_ids = {item["id"] for item in candidates}
        else:
            unit = PHYSICAL_UNITS[UNIT_ALIAS_TO_NAME[unit_alias]]
            unit_symbol = f"unit_{unit.name}"
            explicit_typed_quantity = {
                "node": "quantity",
                "magnitude": number,
                "unit": unit.display,
                "unit_symbol": unit_symbol,
                "unit_type": unit.name,
                "wolfram_unit": unit.wolfram_name,
                "unit_alias": unit_alias,
                "selection_source": "explicit_input",
            }
            candidates = []
            candidate_ids = set()
            expression = f"{number}*{unit_symbol}"
        explicit_unit = None
        if unit_alias == "m":
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
        elif explicit_typed_quantity is not None:
            typed_ast = explicit_typed_quantity
            classifier = {
                "name": "not_applicable",
                "method": "explicit unit resolved from input",
                "ranks_candidates_only": True,
                "selection_authority": False,
            }
            assumption_payload = []
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
        raise InterpretationValidationError(
            parsed.get("error", "input could not be parsed"),
            assumptions=assumption_payload,
            interpretation={
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
            classification=classifier,
        )
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


def _interpret_coordinate_divergence(
    source_query: str,
    arguments_source: str,
    selections: list[str],
    *,
    compute: bool,
    numeric_precision: int,
    time_limit_seconds: float,
    parse_timeout_seconds: float,
    pod_timeout_seconds: float | None,
    deadline: float | None,
    parse_timing: float,
) -> dict[str, Any]:
    """Interpret the audited three-component divergence ambiguity."""
    components_source = _split_top_level_arguments(arguments_source)
    if components_source is None or len(components_source) != 3:
        raise InterpretationError(
            "coordinate-system support currently requires div(component1, component2, component3)"
        )

    registry = assumption_registry()
    candidates = [
        item for item in registry["assumptions"]
        if item.get("type") == "CoordinateSystem"
    ]
    if not candidates or sum(bool(item.get("service_default")) for item in candidates) != 1:
        raise RuntimeError("CoordinateSystem registry must define one service default")
    by_id = {item["id"]: item for item in candidates}
    if len(selections) != len(set(selections)):
        raise AssumptionSelectionError("assumption selections cannot contain duplicates")
    selected_item: dict[str, Any] | None = None
    selected_occurrence: int | None = None
    for selection in selections:
        assumption_id, occurrence_index = _split_assumption_selector(selection)
        if assumption_id not in by_id:
            if assumption_id not in {item["id"] for item in registry["assumptions"]}:
                raise AssumptionSelectionError(f"unknown service assumption ID: {selection}")
            raise AssumptionSelectionError(
                f"assumption {assumption_id} is incompatible with this input"
            )
        if occurrence_index not in (None, 1):
            raise AssumptionSelectionError(
                "coordinate-system occurrence indexes are limited to the single divergence call"
            )
        if selected_item is not None:
            raise AssumptionSelectionError(
                "select at most one coordinate system for this divergence call"
            )
        selected_item = by_id[assumption_id]
        selected_occurrence = occurrence_index

    if selected_item is None:
        selected_item = next(item for item in candidates if item["service_default"])
        selection_source = "service_default"
    else:
        selection_source = (
            "per_occurrence_override" if selected_occurrence is not None else "caller_override"
        )

    components = [_coordinate_component_expression(source) for source in components_source]
    coordinate_system = selected_item["coordinate_system"]
    remaining = min(
        time_limit_seconds,
        parse_timeout_seconds,
        deadline - perf_counter() if deadline is not None else 60.0,
        pod_timeout_seconds if pod_timeout_seconds is not None else 60.0,
    )
    if remaining <= 0.001:
        raise InterpretationTimeout(parse_timing)
    symbolic = run_symbolic(SymbolicRequest(
        operation="coordinate_divergence",
        components=components,
        coordinate_system=coordinate_system,
        time_limit_seconds=min(60.0, remaining),
    ))
    if symbolic.get("timed_out"):
        raise InterpretationTimeout(parse_timing + float(
            symbolic.get("parse_timing_seconds", 0.0)
        ))
    if symbolic.get("error"):
        raise InterpretationError(str(symbolic["error"]))
    parse_timing += float(symbolic.get("parse_timing_seconds", 0.0))
    resolved_expression = str(symbolic.get("result", ""))
    domain_conditions = list(symbolic.get("domain_conditions", []))

    assumption_payload = [{
        "type": "CoordinateSystem",
        "selected": selected_item["id"],
        "selection_source": selection_source,
        "registry_version": registry["registry_version"],
        "occurrences": [{
            "id": "CoordinateSystem:1",
            "selected": selected_item["id"],
            "selection_source": selection_source,
        }],
        "values": [
            {
                "input": item["id"],
                "desc": item["description"],
                "entry_version": item["entry_version"],
            }
            for item in candidates
        ],
    }]
    classification = {
        "name": "neutral_coordinate_system_candidate_ranker",
        "method": "coordinate-system alternatives receive tied scores; proof success does not select a meaning",
        "ranks_candidates_only": True,
        "selection_authority": False,
        "scores_calibrated_for_service": False,
        "ranked": [{"input": item["id"], "score": 0.5} for item in candidates],
    }
    typed_ast = {
        "node": "vector_calculus_operation",
        "operation": "divergence",
        "coordinate_system": coordinate_system,
        "components": components,
        "coordinate_variables": (
            ["x", "y", "z"]
            if coordinate_system == "Cartesian3D"
            else ["rho", "theta", "z"]
            if coordinate_system == "Cylindrical3D"
            else ["r", "theta", "phi"]
        ),
        "derivative_terms": symbolic.get("derivative_terms", []),
        "resolved_expression": resolved_expression,
        "display_expression": source_query,
        "assumption_id": selected_item["id"],
        "selection_source": selection_source,
    }

    outcome = "computed" if compute else "validated"
    result = resolved_expression if compute else None

    return {
        "input": source_query,
        "outcome": outcome,
        **({"timeout_stage": "pod"} if outcome == "timeout" else {}),
        "parse_timing": parse_timing,
        "assumptions": assumption_payload,
        "interpretation": {
            "kind": "typed_symbolic_expression",
            "typed_ast": typed_ast,
            "expression": resolved_expression,
            "operation": "divergence",
            "solver_route": "sympy",
        },
        "classification": classification,
        "result": result,
        "numeric_approximation": None,
        "input_mathml": None,
        "result_mathml": symbolic.get("result_mathml") if compute else None,
        "evidence": {
            "kind": "coordinate_system_symbolic_result",
            "engine": "sympy",
            "verified": False,
            "coordinate_system": coordinate_system,
            "domain_conditions": domain_conditions,
        },
        "qualification": (
            "The divergence was computed from the caller-selected coordinate interpretation. "
            "The chart formula can be checked with verify=true; that check does not prove the "
            "English interpretation."
        ),
    }


def _interpret_coordinate_curl(
    source_query: str,
    arguments_source: str,
    selections: list[str],
    *,
    compute: bool,
    numeric_precision: int,
    time_limit_seconds: float,
    parse_timeout_seconds: float,
    pod_timeout_seconds: float | None,
    deadline: float | None,
    parse_timing: float,
) -> dict[str, Any]:
    """Interpret polynomial vector components and select a curl chart."""
    components_source = _split_top_level_arguments(arguments_source)
    if components_source is None or len(components_source) != 3:
        raise InterpretationError("coordinate-system curl requires three vector components")

    registry = assumption_registry()
    candidates = [
        item for item in registry["assumptions"]
        if item.get("type") == "CoordinateSystem"
    ]
    if not candidates or sum(bool(item.get("service_default")) for item in candidates) != 1:
        raise RuntimeError("CoordinateSystem registry must define one service default")
    by_id = {item["id"]: item for item in candidates}
    if len(selections) != len(set(selections)):
        raise AssumptionSelectionError("assumption selections cannot contain duplicates")
    selected_item: dict[str, Any] | None = None
    selected_occurrence: int | None = None
    for selection in selections:
        assumption_id, occurrence_index = _split_assumption_selector(selection)
        if assumption_id not in by_id:
            if assumption_id not in {item["id"] for item in registry["assumptions"]}:
                raise AssumptionSelectionError(f"unknown service assumption ID: {selection}")
            raise AssumptionSelectionError(
                f"assumption {assumption_id} is incompatible with this input"
            )
        if occurrence_index not in (None, 1):
            raise AssumptionSelectionError(
                "coordinate-system occurrence indexes are limited to the single curl call"
            )
        if selected_item is not None:
            raise AssumptionSelectionError("select at most one coordinate system for this curl call")
        selected_item = by_id[assumption_id]
        selected_occurrence = occurrence_index

    if selected_item is None:
        selected_item = next(item for item in candidates if item["service_default"])
        selection_source = "service_default"
    else:
        selection_source = (
            "per_occurrence_override" if selected_occurrence is not None else "caller_override"
        )

    components = [_coordinate_component_expression(source) for source in components_source]
    coordinate_system = selected_item["coordinate_system"]
    remaining = min(
        time_limit_seconds,
        parse_timeout_seconds,
        deadline - perf_counter() if deadline is not None else 60.0,
        pod_timeout_seconds if pod_timeout_seconds is not None else 60.0,
    )
    if remaining <= 0.001:
        raise InterpretationTimeout(parse_timing)
    symbolic = run_symbolic(SymbolicRequest(
        operation="coordinate_curl",
        components=components,
        coordinate_system=coordinate_system,
        time_limit_seconds=min(60.0, remaining),
    ))
    if symbolic.get("timed_out"):
        raise InterpretationTimeout(parse_timing + float(
            symbolic.get("parse_timing_seconds", 0.0)
        ))
    if symbolic.get("error"):
        raise InterpretationError(str(symbolic["error"]))
    parse_timing += float(symbolic.get("parse_timing_seconds", 0.0))
    resolved_expression = str(symbolic.get("result", ""))
    resolved_components = list(symbolic.get("curl_components", []))
    domain_conditions = list(symbolic.get("domain_conditions", []))

    assumption_payload = [{
        "type": "CoordinateSystem",
        "selected": selected_item["id"],
        "selection_source": selection_source,
        "registry_version": registry["registry_version"],
        "occurrences": [{
            "id": "CoordinateSystem:1",
            "selected": selected_item["id"],
            "selection_source": selection_source,
        }],
        "values": [
            {
                "input": item["id"],
                "desc": item["description"],
                "entry_version": item["entry_version"],
            }
            for item in candidates
        ],
    }]
    classification = {
        "name": "neutral_coordinate_system_candidate_ranker",
        "method": "coordinate-system alternatives receive tied scores; proof success does not select a meaning",
        "ranks_candidates_only": True,
        "selection_authority": False,
        "scores_calibrated_for_service": False,
        "ranked": [{"input": item["id"], "score": 0.5} for item in candidates],
    }
    coordinate_variables = {
        "Cartesian3D": ["x", "y", "z"],
        "Cylindrical3D": ["rho", "theta", "z"],
        "Spherical3D": ["r", "theta", "phi"],
    }[coordinate_system]
    typed_ast = {
        "node": "vector_calculus_operation",
        "operation": "curl",
        "coordinate_system": coordinate_system,
        "components": components,
        "coordinate_variables": coordinate_variables,
        "derivative_terms": list(symbolic.get("derivative_terms", [])),
        "resolved_components": resolved_components,
        "resolved_expression": resolved_expression,
        "display_expression": source_query,
        "assumption_id": selected_item["id"],
        "selection_source": selection_source,
        "basis": "orthonormal_coordinate_basis",
    }
    outcome = "computed" if compute else "validated"
    return {
        "input": source_query,
        "outcome": outcome,
        "parse_timing": parse_timing,
        "assumptions": assumption_payload,
        "interpretation": {
            "kind": "typed_symbolic_expression",
            "typed_ast": typed_ast,
            "expression": resolved_expression,
            "operation": "curl",
            "solver_route": "sympy",
        },
        "classification": classification,
        "result": resolved_expression if compute else None,
        "numeric_approximation": None,
        "input_mathml": None,
        "result_mathml": symbolic.get("result_mathml") if compute else None,
        "evidence": {
            "kind": "coordinate_system_symbolic_result",
            "engine": "sympy",
            "verified": False,
            "coordinate_system": coordinate_system,
            "domain_conditions": domain_conditions,
        },
        "qualification": (
            "The curl was computed from the caller-selected orthonormal coordinate interpretation. "
            "verify=true can request a Lean check of the chart formula; it does not prove the "
            "English interpretation."
        ),
    }


def _interpret_coordinate_gradient(
    source_query: str,
    expression_source: str,
    selections: list[str],
    *,
    compute: bool,
    numeric_precision: int,
    time_limit_seconds: float,
    parse_timeout_seconds: float,
    pod_timeout_seconds: float | None,
    deadline: float | None,
    parse_timing: float,
) -> dict[str, Any]:
    """Interpret a bounded polynomial gradient with an explicit chart choice."""
    registry = assumption_registry()
    candidates = [
        item for item in registry["assumptions"]
        if item.get("type") == "CoordinateSystem"
    ]
    if not candidates or sum(bool(item.get("service_default")) for item in candidates) != 1:
        raise RuntimeError("CoordinateSystem registry must define one service default")
    by_id = {item["id"]: item for item in candidates}
    if len(selections) != len(set(selections)):
        raise AssumptionSelectionError("assumption selections cannot contain duplicates")
    selected_item: dict[str, Any] | None = None
    selected_occurrence: int | None = None
    for selection in selections:
        assumption_id, occurrence_index = _split_assumption_selector(selection)
        if assumption_id not in by_id:
            if assumption_id not in {item["id"] for item in registry["assumptions"]}:
                raise AssumptionSelectionError(f"unknown service assumption ID: {selection}")
            raise AssumptionSelectionError(
                f"assumption {assumption_id} is incompatible with this input"
            )
        if occurrence_index not in (None, 1):
            raise AssumptionSelectionError(
                "coordinate-system occurrence indexes are limited to the single gradient call"
            )
        if selected_item is not None:
            raise AssumptionSelectionError(
                "select at most one coordinate system for this gradient call"
            )
        selected_item = by_id[assumption_id]
        selected_occurrence = occurrence_index

    if selected_item is None:
        selected_item = next(item for item in candidates if item["service_default"])
        selection_source = "service_default"
    else:
        selection_source = (
            "per_occurrence_override" if selected_occurrence is not None else "caller_override"
        )

    expression = _coordinate_component_expression(expression_source)
    coordinate_system = selected_item["coordinate_system"]
    remaining = min(
        time_limit_seconds,
        parse_timeout_seconds,
        deadline - perf_counter() if deadline is not None else 60.0,
        pod_timeout_seconds if pod_timeout_seconds is not None else 60.0,
    )
    if remaining <= 0.001:
        raise InterpretationTimeout(parse_timing)
    symbolic = run_symbolic(SymbolicRequest(
        operation="coordinate_gradient",
        expression=expression,
        coordinate_system=coordinate_system,
        time_limit_seconds=min(60.0, remaining),
    ))
    if symbolic.get("timed_out"):
        raise InterpretationTimeout(parse_timing + float(
            symbolic.get("parse_timing_seconds", 0.0)
        ))
    if symbolic.get("error"):
        raise InterpretationError(str(symbolic["error"]))
    parse_timing += float(symbolic.get("parse_timing_seconds", 0.0))
    resolved_expression = str(symbolic.get("result", ""))
    domain_conditions = list(symbolic.get("domain_conditions", []))

    assumption_payload = [{
        "type": "CoordinateSystem",
        "selected": selected_item["id"],
        "selection_source": selection_source,
        "registry_version": registry["registry_version"],
        "occurrences": [{
            "id": "CoordinateSystem:1",
            "selected": selected_item["id"],
            "selection_source": selection_source,
        }],
        "values": [
            {
                "input": item["id"],
                "desc": item["description"],
                "entry_version": item["entry_version"],
            }
            for item in candidates
        ],
    }]
    classification = {
        "name": "neutral_coordinate_system_candidate_ranker",
        "method": "coordinate-system alternatives receive tied scores; proof success does not select a meaning",
        "ranks_candidates_only": True,
        "selection_authority": False,
        "scores_calibrated_for_service": False,
        "ranked": [{"input": item["id"], "score": 0.5} for item in candidates],
    }
    gradient_components = list(symbolic.get("gradient_components", []))
    typed_ast = {
        "node": "vector_calculus_operation",
        "operation": "gradient",
        "expression": expression,
        "coordinate_system": coordinate_system,
        "coordinate_variables": list(symbolic.get("coordinate_variables", [])),
        "derivative_terms": list(symbolic.get("derivative_terms", [])),
        "resolved_components": gradient_components,
        "resolved_expression": resolved_expression,
        "display_expression": source_query,
        "assumption_id": selected_item["id"],
        "selection_source": selection_source,
    }
    outcome = "computed" if compute else "validated"
    return {
        "input": source_query,
        "outcome": outcome,
        "parse_timing": parse_timing,
        "assumptions": assumption_payload,
        "interpretation": {
            "kind": "typed_symbolic_expression",
            "typed_ast": typed_ast,
            "expression": resolved_expression,
            "operation": "gradient",
            "solver_route": "sympy",
        },
        "classification": classification,
        "result": resolved_expression if compute else None,
        "numeric_approximation": None,
        "input_mathml": symbolic.get("input_mathml") if compute else None,
        "result_mathml": symbolic.get("result_mathml") if compute else None,
        "evidence": {
            "kind": "coordinate_system_symbolic_result",
            "engine": "sympy",
            "verified": False,
            "coordinate_system": coordinate_system,
            "domain_conditions": domain_conditions,
        },
        "qualification": (
            "The gradient was computed from the caller-selected coordinate interpretation. "
            "verify=true can request a Lean check of the chart formula; it does not prove the "
            "English interpretation."
        ),
    }


def _interpret_coordinate_laplacian(
    source_query: str,
    expression_source: str,
    selections: list[str],
    *,
    compute: bool,
    numeric_precision: int,
    time_limit_seconds: float,
    parse_timeout_seconds: float,
    pod_timeout_seconds: float | None,
    deadline: float | None,
    parse_timing: float,
) -> dict[str, Any]:
    """Interpret a bounded scalar Laplacian with an explicit chart choice."""
    registry = assumption_registry()
    candidates = [
        item for item in registry["assumptions"]
        if item.get("type") == "CoordinateSystem"
    ]
    if not candidates or sum(bool(item.get("service_default")) for item in candidates) != 1:
        raise RuntimeError("CoordinateSystem registry must define one service default")
    by_id = {item["id"]: item for item in candidates}
    if len(selections) != len(set(selections)):
        raise AssumptionSelectionError("assumption selections cannot contain duplicates")
    selected_item: dict[str, Any] | None = None
    selected_occurrence: int | None = None
    for selection in selections:
        assumption_id, occurrence_index = _split_assumption_selector(selection)
        if assumption_id not in by_id:
            if assumption_id not in {item["id"] for item in registry["assumptions"]}:
                raise AssumptionSelectionError(f"unknown service assumption ID: {selection}")
            raise AssumptionSelectionError(
                f"assumption {assumption_id} is incompatible with this input"
            )
        if occurrence_index not in (None, 1):
            raise AssumptionSelectionError(
                "coordinate-system occurrence indexes are limited to the single Laplacian call"
            )
        if selected_item is not None:
            raise AssumptionSelectionError(
                "select at most one coordinate system for this Laplacian"
            )
        selected_item = by_id[assumption_id]
        selected_occurrence = occurrence_index

    if selected_item is None:
        selected_item = next(item for item in candidates if item["service_default"])
        selection_source = "service_default"
    else:
        selection_source = (
            "per_occurrence_override" if selected_occurrence is not None else "caller_override"
        )

    expression = _coordinate_component_expression(expression_source)
    coordinate_system = selected_item["coordinate_system"]
    remaining = min(
        time_limit_seconds,
        parse_timeout_seconds,
        deadline - perf_counter() if deadline is not None else 60.0,
        pod_timeout_seconds if pod_timeout_seconds is not None else 60.0,
    )
    if remaining <= 0.001:
        raise InterpretationTimeout(parse_timing)
    symbolic = run_symbolic(SymbolicRequest(
        operation="coordinate_laplacian",
        expression=expression,
        coordinate_system=coordinate_system,
        time_limit_seconds=min(60.0, remaining),
    ))
    if symbolic.get("timed_out"):
        raise InterpretationTimeout(parse_timing + float(
            symbolic.get("parse_timing_seconds", 0.0)
        ))
    if symbolic.get("error"):
        raise InterpretationError(str(symbolic["error"]))
    parse_timing += float(symbolic.get("parse_timing_seconds", 0.0))
    resolved_expression = str(symbolic.get("result", ""))
    domain_conditions = list(symbolic.get("domain_conditions", []))

    assumption_payload = [{
        "type": "CoordinateSystem",
        "selected": selected_item["id"],
        "selection_source": selection_source,
        "registry_version": registry["registry_version"],
        "occurrences": [{
            "id": "CoordinateSystem:1",
            "selected": selected_item["id"],
            "selection_source": selection_source,
        }],
        "values": [
            {
                "input": item["id"],
                "desc": item["description"],
                "entry_version": item["entry_version"],
            }
            for item in candidates
        ],
    }]
    classification = {
        "name": "neutral_coordinate_system_candidate_ranker",
        "method": "coordinate-system alternatives receive tied scores; proof success does not select a meaning",
        "ranks_candidates_only": True,
        "selection_authority": False,
        "scores_calibrated_for_service": False,
        "ranked": [{"input": item["id"], "score": 0.5} for item in candidates],
    }
    first_derivative_terms = list(symbolic.get("first_derivative_terms", []))
    second_derivative_terms = list(symbolic.get("second_derivative_terms", []))
    typed_ast = {
        "node": "vector_calculus_operation",
        "operation": "laplacian",
        "expression": expression,
        "coordinate_system": coordinate_system,
        "coordinate_variables": list(symbolic.get("coordinate_variables", [])),
        "first_derivative_terms": first_derivative_terms,
        "second_derivative_terms": second_derivative_terms,
        "resolved_expression": resolved_expression,
        "display_expression": source_query,
        "assumption_id": selected_item["id"],
        "selection_source": selection_source,
    }
    outcome = "computed" if compute else "validated"
    return {
        "input": source_query,
        "outcome": outcome,
        "parse_timing": parse_timing,
        "assumptions": assumption_payload,
        "interpretation": {
            "kind": "typed_symbolic_expression",
            "typed_ast": typed_ast,
            "expression": resolved_expression,
            "operation": "laplacian",
            "solver_route": "sympy",
        },
        "classification": classification,
        "result": resolved_expression if compute else None,
        "numeric_approximation": None,
        "input_mathml": symbolic.get("input_mathml") if compute else None,
        "result_mathml": symbolic.get("result_mathml") if compute else None,
        "evidence": {
            "kind": "coordinate_system_symbolic_result",
            "engine": "sympy",
            "verified": False,
            "coordinate_system": coordinate_system,
            "domain_conditions": domain_conditions,
        },
        "qualification": (
            "The scalar Laplacian was computed from the caller-selected coordinate interpretation. "
            "verify=true can request a Lean check of the chart formula; it does not prove the "
            "English interpretation."
        ),
    }


def _coordinate_component_expression(source: str) -> str:
    # The audited notation uses whitespace for multiplication (for example `x rho`).
    normalized = re.sub(
        r"(?<=[A-Za-z0-9_)])\s+(?=[A-Za-z_(])", "*", source.strip()
    )
    try:
        return parse_math_input(normalized).expression
    except MathInputError as exc:
        raise InterpretationError("vector components must be valid mathematical expressions") from exc


def _split_top_level_arguments(source: str) -> list[str] | None:
    arguments: list[str] = []
    depth = 0
    start = 0
    for index, character in enumerate(source):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                return None
        elif character == "," and depth == 0:
            arguments.append(source[start:index].strip())
            start = index + 1
    if depth != 0:
        return None
    arguments.append(source[start:].strip())
    return arguments


def _interpret_function_log(
    source_query: str,
    selections: list[str],
    *,
    compute: bool,
    numeric_precision: int,
    time_limit_seconds: float,
    parse_timeout_seconds: float,
    pod_timeout_seconds: float | None,
    deadline: float | None,
    parse_timing: float,
) -> dict[str, Any]:
    registry = assumption_registry()
    candidates = [item for item in registry["assumptions"] if item["type"] == "Function"]
    all_by_id = {item["id"]: item for item in registry["assumptions"]}
    if len(candidates) != 2:
        raise RuntimeError("Function registry must contain two values")
    if len(selections) != len(set(selections)):
        raise AssumptionSelectionError("assumption selections cannot contain duplicates")

    matches = list(_LOG_FUNCTION_IN_EXPRESSION.finditer(source_query))
    if not matches:
        raise RuntimeError("Function log handler was called without a matching input")

    global_choice: dict[str, Any] | None = None
    per_occurrence_choices: dict[int, dict[str, Any]] = {}
    remaining_selections: list[str] = []
    for selection in selections:
        assumption_id, occurrence_index = _split_assumption_selector(selection)
        item = all_by_id.get(assumption_id)
        if item is None:
            raise AssumptionSelectionError(f"unknown service assumption ID: {selection}")
        if item["type"] != "Function":
            remaining_selections.append(selection)
            continue
        if occurrence_index is None:
            if per_occurrence_choices:
                raise AssumptionSelectionError(
                    "use either a query-wide or per-occurrence selection for Function"
                )
            if global_choice is not None:
                raise AssumptionSelectionError("select at most one Function assumption value")
            global_choice = item
        else:
            if occurrence_index > len(matches):
                raise AssumptionSelectionError(
                    f"assumption occurrence Function:{occurrence_index} is not present in this input"
                )
            if global_choice is not None:
                raise AssumptionSelectionError(
                    "use either a query-wide or per-occurrence selection for Function"
                )
            if occurrence_index in per_occurrence_choices:
                raise AssumptionSelectionError(
                    f"select at most one value for Function occurrence {occurrence_index}"
                )
            per_occurrence_choices[occurrence_index] = item

    defaults = [item for item in candidates if item.get("service_default")]
    if len(defaults) != 1:
        raise RuntimeError("Function registry must have exactly one service default")
    default_choice = defaults[0]
    if global_choice is not None:
        selection_source = "caller_override"
    elif per_occurrence_choices:
        selection_source = "per_occurrence_override"
    else:
        selection_source = "service_default"

    occurrence_choices = [
        global_choice or per_occurrence_choices.get(index) or default_choice
        for index, _match in enumerate(matches, 1)
    ]
    occurrence_sources = [
        "caller_override"
        if global_choice is not None
        else "per_occurrence_override"
        if index in per_occurrence_choices
        else "service_default"
        for index, _match in enumerate(matches, 1)
    ]

    normalized_expression = source_query
    for match, chosen in reversed(list(zip(matches, occurrence_choices, strict=True))):
        number = match.group("number")
        base = chosen["argument_base"]
        replacement = f"log({number},10)" if base == "10" else f"log({number},E)"
        normalized_expression = (
            normalized_expression[:match.start()]
            + replacement
            + normalized_expression[match.end():]
        )

    resolved = query_with_interpretations(
        normalized_expression,
        remaining_selections,
        compute=compute,
        numeric_precision=numeric_precision,
        time_limit_seconds=time_limit_seconds,
        parse_timeout_seconds=parse_timeout_seconds,
        pod_timeout_seconds=pod_timeout_seconds,
        deadline=deadline,
    )

    inner_assumptions = resolved.get("assumptions", [])
    occurrences = [
        {
            "id": f"Function:{index}",
            "function": "log",
            "argument": match.group("number"),
            "selected": chosen["id"],
            "selection_source": occurrence_sources[index - 1],
        }
        for index, (match, chosen) in enumerate(
            zip(matches, occurrence_choices, strict=True), 1
        )
    ]
    selected_ids = {item["selected"] for item in occurrences}
    function_values = candidates
    if len(selected_ids) == 1:
        active_choice = all_by_id[next(iter(selected_ids))]
        function_values = [
            active_choice,
            *(item for item in candidates if item["id"] != active_choice["id"]),
        ]
    function_payload = {
        "type": "Function",
        "word": "log",
        "selection_source": selection_source,
        "registry_version": registry["registry_version"],
        "occurrences": occurrences,
        "values": [
            {
                "input": item["id"],
                "desc": item["description"],
                "entry_version": item["entry_version"],
            }
            for item in function_values
        ],
    }
    if len(selected_ids) == 1:
        function_payload["selected"] = next(iter(selected_ids))
    resolved["input"] = source_query
    resolved["parse_timing"] = parse_timing + float(resolved["parse_timing"])
    resolved["assumptions"] = [*inner_assumptions, function_payload]
    original_interpretation = resolved["interpretation"]
    original_ast = original_interpretation.get("typed_ast")
    function_ast: dict[str, Any] = {
        "node": "function_assumption",
        "function": "log",
        "occurrences": occurrences,
        "resolved_expression": normalized_expression,
        "expression": original_ast,
    }
    if len(selected_ids) == 1:
        selected_choice = occurrence_choices[0]
        function_ast["meaning"] = selected_choice["meaning"]
        function_ast["base"] = selected_choice["argument_base"]
    resolved["interpretation"] = {
        **original_interpretation,
        "typed_ast": function_ast,
    }
    nested_classification = resolved.get("classification")
    resolved["classification"] = {
        **_rank_candidates(source_query, candidates),
        "nested_classification": nested_classification,
    }
    return resolved


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


def _interpret_clock_time(
    source_query: str,
    match: re.Match[str],
    selections: list[str],
    *,
    compute: bool,
    parse_timing: float,
) -> dict[str, Any]:
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    explicit_meridiem = match.group("meridiem")
    if hour > 23 or minute > 59:
        raise InterpretationError("clock time must use an hour from 0 to 23 and a minute from 00 to 59")
    if explicit_meridiem and not 1 <= hour <= 12:
        raise InterpretationError("AM/PM clock input must use an hour from 1 to 12")

    registry = assumption_registry()
    candidates = [item for item in registry["assumptions"] if item["type"] == "TimeAMOrPM"]
    by_id = {item["id"]: item for item in candidates}
    candidate_ids = set(by_id)
    if len(candidates) != 2:
        raise RuntimeError("TimeAMOrPM registry must contain exactly two values")
    if len(selections) != len(set(selections)):
        raise AssumptionSelectionError("assumption selections cannot contain duplicates")
    all_ids = {item["id"] for item in registry["assumptions"]}
    for selection in selections:
        if selection not in all_ids:
            raise AssumptionSelectionError(f"unknown service assumption ID: {selection}")

    ambiguous = explicit_meridiem is None and 1 <= hour <= 12
    if ambiguous:
        for selection in selections:
            if selection not in candidate_ids:
                raise AssumptionSelectionError(
                    f"assumption {selection} is incompatible with this input"
                )
        if len(selections) > 1:
            raise AssumptionSelectionError("select at most one TimeAMOrPM assumption value")
        if selections:
            chosen = by_id[selections[0]]
            selection_source = "caller_override"
        else:
            policy = registry.get("default_policies", {}).get("TimeAMOrPM")
            if policy == "server_local_meridiem":
                meaning = "am" if datetime.now().astimezone().hour < 12 else "pm"
                chosen = next(item for item in candidates if item["meaning"] == meaning)
            else:
                defaults = [item for item in candidates if item.get("service_default")]
                if len(defaults) != 1:
                    raise RuntimeError("TimeAMOrPM registry has no valid default policy")
                chosen = defaults[0]
            selection_source = "service_default"
        meridiem = chosen["meaning"]
    else:
        if selections:
            raise AssumptionSelectionError(
                f"assumption {selections[0]} is incompatible with this input"
            )
        meridiem = explicit_meridiem.lower() if explicit_meridiem else None
        chosen = None
        selection_source = "explicit_input"

    if meridiem:
        hour_24 = (hour % 12) + (12 if meridiem == "pm" else 0)
        display = f"{hour}:{minute:02d} {meridiem.upper()}"
    else:
        hour_24 = hour
        display = f"{hour:02d}:{minute:02d}"
    normalized_time = f"{hour_24:02d}:{minute:02d}"
    assumption_payload: list[dict[str, Any]] = []
    if ambiguous:
        ordered_candidates = [chosen, *(item for item in candidates if item["id"] != chosen["id"])]
        assumption_payload = [{
            "type": "TimeAMOrPM",
            "selected": chosen["id"],
            "selection_source": selection_source,
            "registry_version": registry["registry_version"],
            "values": [
                {
                    "input": item["id"],
                    "desc": item["description"],
                    "entry_version": item["entry_version"],
                }
                for item in ordered_candidates
            ],
        }]
    typed_ast = {
        "node": "clock_time",
        "input_hour": hour,
        "input_minute": minute,
        "notation": "12_hour" if meridiem else "24_hour",
        "meridiem": meridiem.upper() if meridiem else None,
        "hour_24": hour_24,
        "minute": minute,
        "iso_time": normalized_time,
        "display": display,
    }
    if ambiguous:
        classification = {
            "name": "deterministic_lexical_baseline",
            "method": "neutral ranking for AM and PM readings of a clock time",
            "ranks_candidates_only": True,
            "selection_authority": False,
            "scores_calibrated_for_service": False,
            "ranked": [{"input": item["id"], "score": 0.5} for item in ordered_candidates],
        }
    else:
        classification = {
            "name": "not_applicable",
            "method": "explicit or 24-hour clock notation removes the AM/PM ambiguity",
            "ranks_candidates_only": True,
        }
    return {
        "input": source_query,
        "outcome": "computed" if compute else "validated",
        "parse_timing": parse_timing,
        "assumptions": assumption_payload,
        "interpretation": {
            "kind": "typed_time",
            "typed_ast": typed_ast,
            "expression": f"Time[{hour_24}, {minute}]",
            "solver_route": "deterministic_clock_interpretation",
        },
        "classification": classification,
        "result": display if compute else None,
        "numeric_approximation": None,
        "input_mathml": _text_mathml(source_query.strip()),
        "result_mathml": _text_mathml(display) if compute else None,
        "evidence": {
            "kind": "deterministic_time_interpretation" if compute else "interpretation_only",
            "engine": "python.datetime",
            "verified": False,
        },
        "qualification": (
            "The selected AM/PM value resolves a local clock time only; no date, time zone, "
            "or daylight-saving rule is inferred."
        ),
    }


def _text_mathml(value: str) -> str:
    return (
        '<math xmlns="http://www.w3.org/1998/Math/MathML"><mtext>'
        f"{html.escape(value)}"
        "</mtext></math>"
    )


def _mixed_fraction_parts(query: str) -> dict[str, int] | None:
    match = _MIXED_FRACTION_LITERAL.fullmatch(query)
    if match is None:
        return None
    whole = int(match.group("whole"))
    numerator = int(match.group("numerator"))
    denominator = int(match.group("denominator"))
    if whole <= 0 or numerator <= 0 or denominator <= numerator:
        return None
    return {"whole": whole, "numerator": numerator, "denominator": denominator}


def _interpret_mixed_fraction(
    source_query: str,
    parts: dict[str, int],
    selections: list[str],
    *,
    compute: bool,
    numeric_precision: int,
    time_limit_seconds: float,
    parse_timeout_seconds: float,
    pod_timeout_seconds: float | None,
    deadline: float | None,
    parse_timing: float,
) -> dict[str, Any]:
    registry = assumption_registry()
    candidates = [item for item in registry["assumptions"] if item["type"] == "MixedFraction"]
    by_id = {item["id"]: item for item in candidates}
    candidate_ids = set(by_id)
    if len(candidates) != 2:
        raise RuntimeError("MixedFraction registry must contain exactly two values")
    if len(selections) != len(set(selections)):
        raise AssumptionSelectionError("assumption selections cannot contain duplicates")
    if len(selections) > 1:
        raise AssumptionSelectionError("select at most one assumption value for MixedFraction")
    for selection in selections:
        if selection not in candidate_ids:
            all_ids = {item["id"] for item in registry["assumptions"]}
            if selection not in all_ids:
                raise AssumptionSelectionError(f"unknown service assumption ID: {selection}")
            raise AssumptionSelectionError(f"assumption {selection} is incompatible with this input")

    if selections:
        chosen = by_id[selections[0]]
        selection_source = "caller_override"
    else:
        chosen = next((item for item in candidates if item.get("service_default")), None)
        if chosen is None:
            raise RuntimeError("MixedFraction registry has no service default")
        selection_source = "service_default"

    whole = parts["whole"]
    numerator = parts["numerator"]
    denominator = parts["denominator"]
    if chosen["meaning"] == "mixed_fraction":
        expression = f"{whole} + ({numerator})/({denominator})"
    elif chosen["meaning"] == "product":
        expression = f"{whole}*(({numerator})/({denominator}))"
    else:
        raise RuntimeError("MixedFraction registry contains an unsupported meaning")

    resolved = query_with_interpretations(
        expression,
        [],
        compute=compute,
        numeric_precision=numeric_precision,
        time_limit_seconds=time_limit_seconds,
        parse_timeout_seconds=parse_timeout_seconds,
        pod_timeout_seconds=pod_timeout_seconds,
        deadline=deadline,
    )
    assumption_payload = [{
        "type": "MixedFraction",
        "selected": chosen["id"],
        "selection_source": selection_source,
        "registry_version": registry["registry_version"],
        "values": [
            {
                "input": item["id"],
                "desc": item["description"],
                "entry_version": item["entry_version"],
            }
            for item in candidates
        ],
    }]
    resolved["input"] = source_query
    resolved["parse_timing"] = parse_timing + float(resolved["parse_timing"])
    resolved["assumptions"] = assumption_payload
    resolved["interpretation"] = {
        **resolved["interpretation"],
        "typed_ast": {
            "node": "mixed_fraction_interpretation",
            **parts,
            "meaning": chosen["meaning"],
            "resolved_expression": expression,
        },
    }
    resolved["classification"] = {
        "name": "deterministic_lexical_baseline",
        "method": "neutral ranking for mixed-fraction readings",
        "ranks_candidates_only": True,
        "selection_authority": False,
        "scores_calibrated_for_service": False,
        "ranked": [{"input": item["id"], "score": 0.5} for item in candidates],
    }
    return resolved


def _list_or_number_parts(query: str) -> dict[str, str] | None:
    match = _LIST_OR_NUMBER_LITERAL.fullmatch(query)
    if match is None:
        return None
    sign = match.group("sign")
    first = match.group("first")
    second = match.group("second")
    return {
        "first": f"{sign}{first}",
        "second": second,
        "normalized_number": f"{sign}{first}{second}",
    }


def _number_base_digits(query: str) -> str | None:
    match = _NUMBER_BASE_LITERAL.fullmatch(query)
    return match.group("digits") if match is not None else None


def _interpret_number_base(
    source_query: str,
    digits: str,
    selections: list[str],
    *,
    compute: bool,
    parse_timing: float,
) -> dict[str, Any]:
    registry = assumption_registry()
    candidates = [item for item in registry["assumptions"] if item["type"] == "NumberBase"]
    by_id = {item["id"]: item for item in candidates}
    candidate_ids = set(by_id)
    if len(candidates) != 2:
        raise RuntimeError("NumberBase registry must contain exactly two values")
    if len(selections) != len(set(selections)):
        raise AssumptionSelectionError("assumption selections cannot contain duplicates")
    if len(selections) > 1:
        raise AssumptionSelectionError("select at most one assumption value for NumberBase")
    all_ids = {item["id"] for item in registry["assumptions"]}
    for selection in selections:
        if selection not in candidate_ids:
            if selection not in all_ids:
                raise AssumptionSelectionError(f"unknown service assumption ID: {selection}")
            raise AssumptionSelectionError(f"assumption {selection} is incompatible with this input")

    if selections:
        chosen = by_id[selections[0]]
        selection_source = "caller_override"
    else:
        defaults = [item for item in candidates if item.get("service_default")]
        if len(defaults) != 1:
            raise RuntimeError("NumberBase registry must have exactly one service default")
        chosen = defaults[0]
        selection_source = "service_default"

    base = 2 if chosen["meaning"] == "binary" else 10 if chosen["meaning"] == "decimal" else None
    if base is None:
        raise RuntimeError("NumberBase registry contains an unsupported meaning")
    value = int(digits, base)
    expression = f"FromDigits[{json.dumps(digits)}, {base}]"
    display = f"{digits} (base {base})"
    classification = {
        "name": "deterministic_lexical_baseline",
        "method": "neutral ranking for binary-looking and decimal integer readings",
        "ranks_candidates_only": True,
        "selection_authority": False,
        "scores_calibrated_for_service": False,
        "ranked": [{"input": item["id"], "score": 0.5} for item in candidates],
    }
    assumption_payload = [{
        "type": "NumberBase",
        "selected": chosen["id"],
        "selection_source": selection_source,
        "registry_version": registry["registry_version"],
        "values": [
            {
                "input": item["id"],
                "desc": item["description"],
                "entry_version": item["entry_version"],
            }
            for item in candidates
        ],
    }]
    return {
        "input": source_query,
        "outcome": "computed" if compute else "validated",
        "parse_timing": parse_timing,
        "assumptions": assumption_payload,
        "interpretation": {
            "kind": "typed_number",
            "typed_ast": {
                "node": "number_base_conversion",
                "digits": digits,
                "base": base,
                "value": str(value),
                "display": display,
            },
            "expression": expression,
            "solver_route": "integer_base_conversion",
        },
        "classification": classification,
        "result": str(value) if compute else None,
        "numeric_approximation": None,
        "input_mathml": _integer_mathml(digits, base),
        "result_mathml": _integer_mathml(str(value)) if compute else None,
        "evidence": {
            "kind": "deterministic_radix_conversion" if compute else "interpretation_only",
            "engine": "python.int",
            "verified": False,
        },
        "qualification": "Integer base conversion follows the selected NumberBase interpretation; it is not a formal proof.",
    }


def _integer_mathml(value: str, base: int | None = None) -> str:
    rendered = f"<mn>{html.escape(value)}</mn>"
    if base is not None:
        rendered = f"<msub>{rendered}<mn>{base}</mn></msub>"
    return (
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        f"{rendered}</math>"
    )


def _interpret_list_or_number(
    source_query: str,
    parts: dict[str, str],
    selections: list[str],
    *,
    compute: bool,
    numeric_precision: int,
    time_limit_seconds: float,
    parse_timeout_seconds: float,
    pod_timeout_seconds: float | None,
    deadline: float | None,
    parse_timing: float,
) -> dict[str, Any]:
    registry = assumption_registry()
    candidates = [item for item in registry["assumptions"] if item["type"] == "ListOrNumber"]
    by_id = {item["id"]: item for item in candidates}
    candidate_ids = set(by_id)
    if len(candidates) != 2:
        raise RuntimeError("ListOrNumber registry must contain exactly two values")
    if len(selections) != len(set(selections)):
        raise AssumptionSelectionError("assumption selections cannot contain duplicates")
    if len(selections) > 1:
        raise AssumptionSelectionError("select at most one assumption value for ListOrNumber")
    all_ids = {item["id"] for item in registry["assumptions"]}
    for selection in selections:
        if selection not in candidate_ids:
            if selection not in all_ids:
                raise AssumptionSelectionError(f"unknown service assumption ID: {selection}")
            raise AssumptionSelectionError(f"assumption {selection} is incompatible with this input")

    if selections:
        chosen = by_id[selections[0]]
        selection_source = "caller_override"
    else:
        defaults = [item for item in candidates if item.get("service_default")]
        if len(defaults) != 1:
            raise RuntimeError("ListOrNumber registry must have exactly one service default")
        chosen = defaults[0]
        selection_source = "service_default"

    classification = {
        "name": "deterministic_lexical_baseline",
        "method": "neutral ranking for grouped-number and list readings",
        "ranks_candidates_only": True,
        "selection_authority": False,
        "scores_calibrated_for_service": False,
        "ranked": [{"input": item["id"], "score": 0.5} for item in candidates],
    }
    assumption_payload = [{
        "type": "ListOrNumber",
        "selected": chosen["id"],
        "selection_source": selection_source,
        "registry_version": registry["registry_version"],
        "values": [
            {
                "input": item["id"],
                "desc": item["description"],
                "entry_version": item["entry_version"],
            }
            for item in candidates
        ],
    }]

    if chosen["meaning"] == "number":
        expression = parts["normalized_number"]
        resolved = query_with_interpretations(
            expression,
            [],
            compute=compute,
            numeric_precision=numeric_precision,
            time_limit_seconds=time_limit_seconds,
            parse_timeout_seconds=parse_timeout_seconds,
            pod_timeout_seconds=pod_timeout_seconds,
            deadline=deadline,
        )
        resolved["input"] = source_query
        resolved["parse_timing"] = parse_timing + float(resolved["parse_timing"])
        resolved["assumptions"] = assumption_payload
        resolved["interpretation"] = {
            **resolved["interpretation"],
            "typed_ast": {
                "node": "comma_grouped_number_interpretation",
                "source": source_query.strip(),
                "normalized_number": expression,
                "meaning": "number",
            },
        }
        resolved["classification"] = classification
        return resolved

    if chosen["meaning"] != "list":
        raise RuntimeError("ListOrNumber registry contains an unsupported meaning")
    first = parts["first"]
    second = parts["second"]
    display = f"{{{first}, {second}}}"
    expression = f"List[{first}, {second}]"
    mathml = _list_mathml([("number", first), ("number", second)])
    return {
        "input": source_query,
        "outcome": "computed" if compute else "validated",
        "parse_timing": parse_timing,
        "assumptions": assumption_payload,
        "interpretation": {
            "kind": "typed_list",
            "typed_ast": {
                "node": "list",
                "items": [
                    {"kind": "number", "value": first},
                    {"kind": "number", "value": second},
                ],
                "meaning": "list",
                "display": display,
            },
            "expression": expression,
            "solver_route": "typed_parser",
        },
        "classification": classification,
        "result": display if compute else None,
        "numeric_approximation": None,
        "input_mathml": mathml,
        "result_mathml": mathml if compute else None,
        "evidence": {
            "kind": "deterministic_interpretation" if compute else "interpretation_only",
            "engine": "typed_parser",
            "verified": False,
        },
        "qualification": "List construction follows the selected interpretation; it is not a formal proof.",
    }


def _list_or_times_parts(query: str) -> dict[str, str] | None:
    match = _LIST_OR_TIMES_LITERAL.fullmatch(query)
    if match is None or match.group("symbol").lower() == "m":
        return None
    return {"number": match.group("number"), "symbol": match.group("symbol")}


def _interpret_list_or_times(
    source_query: str,
    parts: dict[str, str],
    selections: list[str],
    *,
    compute: bool,
    numeric_precision: int,
    time_limit_seconds: float,
    parse_timeout_seconds: float,
    pod_timeout_seconds: float | None,
    deadline: float | None,
    parse_timing: float,
) -> dict[str, Any]:
    registry = assumption_registry()
    candidates = [item for item in registry["assumptions"] if item["type"] == "ListOrTimes"]
    by_id = {item["id"]: item for item in candidates}
    candidate_ids = set(by_id)
    if len(candidates) != 2:
        raise RuntimeError("ListOrTimes registry must contain exactly two values")
    if len(selections) != len(set(selections)):
        raise AssumptionSelectionError("assumption selections cannot contain duplicates")
    if len(selections) > 1:
        raise AssumptionSelectionError("select at most one assumption value for ListOrTimes")
    all_ids = {item["id"] for item in registry["assumptions"]}
    for selection in selections:
        if selection not in candidate_ids:
            if selection not in all_ids:
                raise AssumptionSelectionError(f"unknown service assumption ID: {selection}")
            raise AssumptionSelectionError(f"assumption {selection} is incompatible with this input")

    if selections:
        chosen = by_id[selections[0]]
        selection_source = "caller_override"
    else:
        defaults = [item for item in candidates if item.get("service_default")]
        if len(defaults) != 1:
            raise RuntimeError("ListOrTimes registry must have exactly one service default")
        chosen = defaults[0]
        selection_source = "service_default"

    number = parts["number"]
    symbol = parts["symbol"]
    classification = {
        "name": "deterministic_lexical_baseline",
        "method": "neutral ranking for multiplication and list readings",
        "ranks_candidates_only": True,
        "selection_authority": False,
        "scores_calibrated_for_service": False,
        "ranked": [{"input": item["id"], "score": 0.5} for item in candidates],
    }
    assumption_payload = [{
        "type": "ListOrTimes",
        "selected": chosen["id"],
        "selection_source": selection_source,
        "registry_version": registry["registry_version"],
        "values": [
            {
                "input": item["id"],
                "desc": item["description"],
                "entry_version": item["entry_version"],
            }
            for item in candidates
        ],
    }]

    if chosen["meaning"] == "multiplication":
        expression = f"({number})*{symbol}"
        resolved = query_with_interpretations(
            expression,
            [],
            compute=compute,
            numeric_precision=numeric_precision,
            time_limit_seconds=time_limit_seconds,
            parse_timeout_seconds=parse_timeout_seconds,
            pod_timeout_seconds=pod_timeout_seconds,
            deadline=deadline,
            _allow_i_assumption=False,
        )
        resolved["input"] = source_query
        resolved["parse_timing"] = parse_timing + float(resolved["parse_timing"])
        resolved["assumptions"] = assumption_payload
        resolved["interpretation"] = {
            **resolved["interpretation"],
            "typed_ast": {
                "node": "space_product_interpretation",
                "number": number,
                "symbol": symbol,
                "meaning": "multiplication",
                "resolved_expression": expression,
            },
        }
        resolved["classification"] = classification
        return resolved

    if chosen["meaning"] != "list":
        raise RuntimeError("ListOrTimes registry contains an unsupported meaning")
    display = f"{{{number}, {symbol}}}"
    expression = f"List[{number}, {symbol}]"
    mathml = _list_mathml([("number", number), ("symbol", symbol)])
    return {
        "input": source_query,
        "outcome": "computed" if compute else "validated",
        "parse_timing": parse_timing,
        "assumptions": assumption_payload,
        "interpretation": {
            "kind": "typed_list",
            "typed_ast": {
                "node": "list",
                "items": [
                    {"kind": "number", "value": number},
                    {"kind": "symbol", "name": symbol},
                ],
                "meaning": "list",
                "display": display,
            },
            "expression": expression,
            "solver_route": "typed_parser",
        },
        "classification": classification,
        "result": display if compute else None,
        "numeric_approximation": None,
        "input_mathml": mathml,
        "result_mathml": mathml if compute else None,
        "evidence": {
            "kind": "deterministic_interpretation" if compute else "interpretation_only",
            "engine": "typed_parser",
            "verified": False,
        },
        "qualification": "List construction follows the selected interpretation; it is not a formal proof.",
    }


def _interpret_imaginary_i(
    source_query: str,
    selections: list[str],
    *,
    compute: bool,
    numeric_precision: int,
    time_limit_seconds: float,
    parse_timeout_seconds: float,
    pod_timeout_seconds: float | None,
    deadline: float | None,
    parse_timing: float,
) -> dict[str, Any]:
    registry = assumption_registry()
    candidates = [item for item in registry["assumptions"] if item["type"] == "I"]
    by_id = {item["id"]: item for item in candidates}
    candidate_ids = set(by_id)
    if len(candidates) != 2:
        raise RuntimeError("I registry must contain exactly two values")
    if len(selections) != len(set(selections)):
        raise AssumptionSelectionError("assumption selections cannot contain duplicates")
    all_ids = {item["id"] for item in registry["assumptions"]}
    for selection in selections:
        if selection not in all_ids:
            raise AssumptionSelectionError(f"unknown service assumption ID: {selection}")

    i_selections = [selection for selection in selections if selection in candidate_ids]
    if len(i_selections) > 1:
        raise AssumptionSelectionError("select at most one I assumption value")
    if i_selections:
        chosen = by_id[i_selections[0]]
        selection_source = "caller_override"
    else:
        defaults = [item for item in candidates if item.get("service_default")]
        if len(defaults) != 1:
            raise RuntimeError("I registry must have exactly one service default")
        chosen = defaults[0]
        selection_source = "service_default"
    if chosen["meaning"] not in {"imaginary", "variable"}:
        raise RuntimeError("I registry contains an unsupported meaning")

    replacement = "I" if chosen["meaning"] == "imaginary" else "i"
    resolved_expression = _IMAGINARY_I_LITERAL.sub(replacement, source_query)
    resolved_expression = _IMAGINARY_I_COEFFICIENT.sub(
        lambda match: f"{match.group('number')}*{replacement}", resolved_expression
    )
    remaining_selections = [selection for selection in selections if selection not in candidate_ids]
    resolved = query_with_interpretations(
        resolved_expression,
        remaining_selections,
        compute=compute,
        numeric_precision=numeric_precision,
        time_limit_seconds=time_limit_seconds,
        parse_timeout_seconds=parse_timeout_seconds,
        pod_timeout_seconds=pod_timeout_seconds,
        deadline=deadline,
        _allow_i_assumption=False,
    )

    assumption_payload = {
        "type": "I",
        "selected": chosen["id"],
        "selection_source": selection_source,
        "registry_version": registry["registry_version"],
        "values": [
            {
                "input": item["id"],
                "desc": item["description"],
                "entry_version": item["entry_version"],
            }
            for item in candidates
        ],
    }
    resolved["input"] = source_query
    resolved["parse_timing"] = parse_timing + float(resolved["parse_timing"])
    resolved["assumptions"] = [*resolved.get("assumptions", []), assumption_payload]
    original_interpretation = resolved["interpretation"]
    original_ast = original_interpretation.get("typed_ast")
    resolved["interpretation"] = {
        **original_interpretation,
        "typed_ast": {
            "node": "i_interpretation",
            "meaning": chosen["meaning"],
            "symbol": replacement,
            "resolved_expression": resolved_expression,
            "expression": original_ast,
        },
    }
    inner_classification = resolved.get("classification")
    resolved["classification"] = {
        "name": "deterministic_lexical_baseline",
        "method": "neutral ranking for imaginary-unit and variable readings of lowercase i",
        "ranks_candidates_only": True,
        "selection_authority": False,
        "scores_calibrated_for_service": False,
        "ranked": [{"input": item["id"], "score": 0.5} for item in candidates],
        "nested_classification": inner_classification,
    }
    return resolved


def _list_mathml(items: list[tuple[str, str]]) -> str:
    tag_by_kind = {"number": "mn", "symbol": "mi"}
    rendered_items = "<mo>,</mo>".join(
        f"<{tag_by_kind[kind]}>{html.escape(value)}</{tag_by_kind[kind]}>"
        for kind, value in items
    )
    return (
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        '<mfenced open="{" close="}"><mrow>'
        f"{rendered_items}"
        "</mrow></mfenced></math>"
    )


def _compound_interpretation(
    query: str,
    selections: list[str],
    registry: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Resolve supported trig, bare-log, and ambiguous-unit inputs in expressions."""
    trig_matches = list(_TRIG_IN_EXPRESSION.finditer(query))
    log_matches = list(_LOG_IN_EXPRESSION.finditer(query))
    unit_matches = list(_UNIT_M_IN_EXPRESSION.finditer(query))
    physical_unit_matches = list(_PHYSICAL_UNIT_IN_EXPRESSION.finditer(query))
    if not trig_matches and not log_matches and not unit_matches and not physical_unit_matches:
        return None

    candidates_by_type = {
        "AngleUnit": [item for item in registry["assumptions"] if item["type"] == "AngleUnit"],
        "LogBase": [item for item in registry["assumptions"] if item["type"] == "LogBase"],
        "Unit": [item for item in registry["assumptions"] if item["type"] == "Unit"],
    }
    grouped_matches: dict[str, list[re.Match[str]]] = {
        "AngleUnit": [], "LogBase": [], "Unit": unit_matches,
    }
    replacements: list[tuple[int, int, str]] = []
    resolved_calls: list[dict[str, Any]] = []
    unit_bindings: dict[str, str] = {}

    for match in physical_unit_matches:
        number = match.group("number")
        unit_symbol = match.group("symbol")
        unit_name = unit_symbol.removeprefix("unit_")
        unit_bindings[unit_symbol] = unit_name
        resolved_calls.append({
            "function": "quantity",
            "argument": {"kind": "number", "value": number},
            "unit": PHYSICAL_UNITS[unit_name].display,
            "unit_display": PHYSICAL_UNITS[unit_name].display,
            "unit_symbol": unit_symbol,
            "wolfram_unit": PHYSICAL_UNITS[unit_name].wolfram_name,
            "selection_source": "explicit_input",
        })

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
    selected_by_occurrence: dict[str, dict[int, dict[str, Any]]] = {}
    if len(selections) != len(set(selections)):
        raise AssumptionSelectionError("assumption selections cannot contain duplicates")
    if selections and not active_types:
        raise AssumptionSelectionError("assumption selection is not applicable to this input")
    for selection in selections:
        assumption_id, occurrence_index = _split_assumption_selector(selection)
        item = by_id.get(assumption_id)
        if item is None:
            raise AssumptionSelectionError(f"unknown service assumption ID: {selection}")
        kind = item["type"]
        if kind not in active_types or assumption_id not in {
            candidate["id"] for candidate in candidates_by_type.get(kind, [])
        }:
            raise AssumptionSelectionError(
                f"assumption {assumption_id} is incompatible with this input"
            )
        if occurrence_index is not None:
            if occurrence_index > len(grouped_matches[kind]):
                raise AssumptionSelectionError(
                    f"assumption occurrence {kind}:{occurrence_index} is not present in this input"
                )
            if kind in selected_by_type:
                raise AssumptionSelectionError(
                    f"use either a query-wide or per-occurrence selection for {kind}"
                )
            occurrence_choices = selected_by_occurrence.setdefault(kind, {})
            if occurrence_index in occurrence_choices:
                raise AssumptionSelectionError(
                    f"select at most one value for {kind} occurrence {occurrence_index}"
                )
            occurrence_choices[occurrence_index] = item
        else:
            if kind in selected_by_occurrence:
                raise AssumptionSelectionError(
                    f"use either a query-wide or per-occurrence selection for {kind}"
                )
            if kind in selected_by_type:
                raise AssumptionSelectionError(
                    "select at most one assumption value for each assumption type"
                )
            selected_by_type[kind] = item

    assumption_payload: list[dict[str, Any]] = []
    ambiguity_rankings: list[dict[str, Any]] = []
    for kind in ("AngleUnit", "LogBase", "Unit"):
        matches = grouped_matches[kind]
        if not matches:
            continue
        candidates = candidates_by_type[kind]
        ranking = _rank_candidates(query, candidates)
        default_choice = next(item for item in candidates if item["service_default"])
        global_choice = selected_by_type.get(kind)
        per_occurrence_choices = selected_by_occurrence.get(kind, {})
        if global_choice is not None:
            selection_source = "caller_override"
        elif per_occurrence_choices:
            selection_source = "per_occurrence_override"
        else:
            selection_source = "service_default"
        occurrence_values = []
        for index, match in enumerate(matches, 1):
            occurrence = {
                "id": f"{kind}:{index}",
                "argument": match.group("number"),
                "selected": (
                    global_choice or per_occurrence_choices.get(index) or default_choice
                )["id"],
                "selection_source": (
                    "caller_override"
                    if global_choice is not None
                    else "per_occurrence_override"
                    if index in per_occurrence_choices
                    else "service_default"
                ),
            }
            if kind == "AngleUnit":
                occurrence["function"] = match.group("function").lower()
            elif kind == "LogBase":
                occurrence["function"] = "log"
            else:
                occurrence["unit_abbreviation"] = "m"
            occurrence_values.append(occurrence)
        ambiguity_rankings.append({
            "type": kind,
            "occurrences": occurrence_values,
            "ranking": ranking,
        })
        assumption_item = {
            "type": kind,
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
        }
        selected_ids = {item["selected"] for item in occurrence_values}
        if len(selected_ids) == 1:
            assumption_item["selected"] = next(iter(selected_ids))
        assumption_payload.append(assumption_item)

        for index, match in enumerate(matches, 1):
            chosen = global_choice or per_occurrence_choices.get(index) or default_choice
            occurrence_id = f"{kind}:{index}"
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
                    "selection_source": occurrence_values[index - 1]["selection_source"],
                    "occurrence_id": occurrence_id,
                })
            elif kind == "LogBase":
                number = match.group("number")
                base = chosen["argument_base"]
                replacement = f"log({number}, 10)" if base == "10" else f"log({number})"
                resolved_calls.append({
                    "function": "log",
                    "argument": {"kind": "number", "value": number},
                    "base": base,
                    "selection_source": occurrence_values[index - 1]["selection_source"],
                    "occurrence_id": occurrence_id,
                })
            else:
                number = match.group("number")
                replacement = f"{number}*{chosen['symbolic_unit']}"
                resolved_calls.append({
                    "function": "quantity",
                    "argument": {"kind": "number", "value": number},
                    "unit": chosen["argument_unit"],
                    "unit_display": chosen["argument_unit"],
                    "unit_symbol": chosen["symbolic_unit"],
                    "wolfram_unit": chosen["wolfram_unit"],
                    "selection_source": occurrence_values[index - 1]["selection_source"],
                    "occurrence_id": occurrence_id,
                })
                unit_bindings[chosen["symbolic_unit"]] = chosen["symbolic_unit_type"]
            replacements.append((match.start(), match.end(), replacement))

    expression = query
    for start, end, replacement in sorted(replacements, reverse=True):
        expression = expression[:start] + replacement + expression[end:]

    display_expression = expression
    wolfram_input_expression = expression
    for call in resolved_calls:
        if call.get("function") != "quantity":
            continue
        number = str(call["argument"]["value"])
        unit_symbol = str(call["unit_symbol"])
        quantity_term = f"{number}*{unit_symbol}"
        term_pattern = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(quantity_term)}(?![A-Za-z0-9_])"
        )
        display_expression = term_pattern.sub(
            f"{number}*{call['unit_display']}", display_expression
        )
        wolfram_input_expression = term_pattern.sub(
            f"Quantity[{number}, {json.dumps(call['wolfram_unit'])}]",
            wolfram_input_expression,
        )

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
            **({
                "display_expression": display_expression,
                "wolfram_input_expression": wolfram_input_expression,
            } if unit_bindings else {}),
            **({"unit_bindings": unit_bindings} if unit_bindings else {}),
        },
        "classifier": classifier,
        "assumptions": assumption_payload,
    }


def _normalize_unit(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.lower()
    return "degrees" if normalized in {"degree", "degrees", "deg"} else "radians"


def _normalize_ambiguous_unit_spacing(source: str) -> str:
    """Convert supported unit literals to named symbols before safe math parsing."""
    if source.lstrip().startswith("<"):
        return source

    def replace_explicit_unit(match: re.Match[str]) -> str:
        number = match.group("number")
        alias = match.group("unit").casefold()
        return f"{number}*unit_{UNIT_ALIAS_TO_NAME[alias]}"

    normalized = _EXPLICIT_UNIT_IN_SOURCE.sub(replace_explicit_unit, source)
    return _UNIT_M_IN_EXPRESSION.sub(lambda match: f"{match.group('number')}*m", normalized)


def _named_unit_bindings(typed_ast: dict[str, Any]) -> dict[str, str]:
    if typed_ast.get("node") == "quantity":
        return {str(typed_ast["unit_symbol"]): str(typed_ast["unit_type"])}
    if typed_ast.get("node") == "symbolic_expression":
        bindings = typed_ast.get("unit_bindings", {})
        if isinstance(bindings, dict):
            return {str(symbol): str(unit_type) for symbol, unit_type in bindings.items()}
    return {}


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
