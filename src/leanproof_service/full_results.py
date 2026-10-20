from __future__ import annotations

import ast
import base64
import copy
import fnmatch
import hashlib
import html
import json
import math
import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4
import xml.etree.ElementTree as ET

from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .computation import (
    _constant_fraction,
    _normalize_decimal_literals,
    _normalize_wolfram_real_functions,
    _real_sympy_expression,
    _split_top_level_commas,
    check_computation,
    prepare_coordinate_curl,
    prepare_coordinate_divergence,
    prepare_coordinate_gradient,
    prepare_coordinate_laplacian,
    prepare_computation,
)
from .interpretations import (
    AssumptionSelectionError,
    InterpretationError,
    InterpretationTimeout,
    InterpretationValidationError,
    assumption_registry,
    numeric_date_options,
    query_with_interpretations,
)
from .input_syntax import MathInputError, parse_math_input
from .knowledge import (
    KnowledgeDeadlineExceeded,
    KnowledgeProviderError,
    KnowledgeRequest,
    query_knowledge,
    supported_nist_constants,
)
from .math_svg import mathml_image
from .natural_language_query import (
    NaturalLanguageClarification,
    NaturalLanguageOperation,
    free_symbols,
    plan_natural_language_operation,
)
from .query_language_model import generate_query_candidate
from .physical_units import (
    PHYSICAL_UNITS,
    SUPPORTED_WOLFRAM_UNIT_NAMES,
    UNIT_ALIASES_BY_LENGTH,
    UNIT_ALIAS_TO_NAME,
)
from .symbolic import SymbolicRequest, run_symbolic
from .weather_svg import forecast_chart_image, forecast_plaintext


_API_VERSION = "2.0"
_SUPPORTED_FORMATS = {"plaintext", "minput", "moutput", "mathml", "cell"}
_KNOWN_FORMATS = {
    "image", "imagemap", "plaintext", "minput", "moutput", "cell", "mathml", "sound", "wav"
}
_XML_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
_DECIMAL_APPROXIMATION_PRECISIONS = (50, 100, 200, 400, 500)
_DECIMAL_MORE_STATE = "DecimalApproximation__More digits"
_DECIMAL_FEWER_STATE = "DecimalApproximation__Fewer digits"
_FORMULA_VARIABLE_NUMBER = (
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
    r"(?:/[+-]?(?:\d+(?:\.\d*)?|\.\d+))?"
)
_FORMULA_VARIABLE_UNIT_ALIASES = "|".join(
    re.escape(alias) for alias in UNIT_ALIASES_BY_LENGTH
)
_FORMULA_VARIABLE_EXPLICIT_UNIT = re.compile(
    rf"(?<![A-Za-z0-9_])(?P<number>{_FORMULA_VARIABLE_NUMBER})\s*"
    rf"(?:\*\s*)?(?P<unit>{_FORMULA_VARIABLE_UNIT_ALIASES})(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_FORMULA_VARIABLE_UNIT_SYMBOL = re.compile(
    r"(?<![A-Za-z0-9_])(?P<symbol>unit_(?P<name>[A-Za-z][A-Za-z0-9_]*))(?![A-Za-z0-9_])"
)


class _FormatStageBudget:
    """Track bounded per-pod formatting work and discard output over budget."""

    def __init__(
        self,
        *,
        format_timeout_seconds: float,
        pod_timeout_seconds: float,
        total_deadline: float,
    ) -> None:
        self.started = perf_counter()
        self.deadline = min(
            self.started + format_timeout_seconds,
            total_deadline,
        )
        self.pod_timeout_seconds = pod_timeout_seconds
        self.timed_out_pods: list[str] = []

    def render(self, title: str, operation: Callable[[], Any]) -> Any | None:
        started = perf_counter()
        if started >= self.deadline:
            self.timed_out_pods.append(title)
            return None
        result = operation()
        finished = perf_counter()
        if (
            finished - started > self.pod_timeout_seconds
            or finished > self.deadline
        ):
            self.timed_out_pods.append(title)
            return None
        return result


class FullResultsRequestError(ValueError):
    pass


class FullResultsBackendError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int = 500,
        error_code: str = "COMPUTATION_FAILURE",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class ProviderContextNeeded(ValueError):
    pass


class ProviderAssumptionNeeded(ProviderContextNeeded):
    def __init__(self, message: str, assumption: dict[str, Any]) -> None:
        super().__init__(message)
        self.assumption = assumption


def normalize_output(value: str) -> str:
    output = value.strip().lower()
    if output not in {"xml", "json"}:
        raise FullResultsRequestError("output must be either 'xml' or 'json'")
    return output


def normalize_async(value: str) -> float | None:
    normalized = value.strip().lower()
    if normalized == "false":
        return None
    if normalized == "true":
        return 0.4
    try:
        wait_seconds = float(normalized)
    except ValueError as exc:
        raise FullResultsRequestError(
            "async must be 'true', 'false', or a wait time in seconds"
        ) from exc
    if not math.isfinite(wait_seconds) or wait_seconds < 0 or wait_seconds > 30:
        raise FullResultsRequestError("numeric async wait time must be between 0 and 30 seconds")
    return wait_seconds


def normalize_formats(values: list[str] | None) -> tuple[list[str], list[str]]:
    if not values:
        return ["plaintext", "image"], []
    formats = _split_values(values)
    if not formats:
        raise FullResultsRequestError("format must contain at least one format name")
    unknown = sorted(set(formats) - _KNOWN_FORMATS)
    if unknown:
        raise FullResultsRequestError(f"unsupported format name: {', '.join(unknown)}")
    unique = list(dict.fromkeys(formats))
    omitted = [item for item in unique if item not in _SUPPORTED_FORMATS]
    return unique, omitted


def async_pod_descriptor(
    input_text: str,
    *,
    location: str | None = None,
    latlong: str | None = None,
    units: str | None = None,
    assumptions: list[str] | None = None,
    series_offset: int = 0,
) -> tuple[str, str, str]:
    try:
        gdp_resolution = _resolve_gdp_request(input_text, assumptions or [])
        if gdp_resolution is not None and any(value is not None for value in (location, latlong, units)):
            raise FullResultsRequestError(
                "location, latlong, and units are currently supported for weather queries"
            )
        request = (
            gdp_resolution[0]
            if gdp_resolution is not None
            else _provider_request(input_text, location, latlong, units, series_offset)
        )
    except ProviderAssumptionNeeded:
        raise
    except ProviderContextNeeded:
        if series_offset:
            raise FullResultsRequestError(
                "seriesoffset is only supported for DBnomics series search"
            )
        return "Result", "Result", "Mathematical"
    if series_offset and not (
        request is not None
        and request.provider == "dbnomics"
        and request.operation == "series_search"
    ):
        raise FullResultsRequestError(
            "seriesoffset is only supported for DBnomics series search"
        )
    if request is None:
        if numeric_date_options(input_text) is not None:
            return "Date", "Result", "Date"
        return "Result", "Result", "Mathematical"
    return {
        "open_meteo": (
            ("Weather forecast", "WeatherForecast", "Data")
            if request.operation == "weather_forecast"
            else ("Current weather", "Weather", "Data")
        ),
        "nist_codata": ("Physical constant", "PhysicalConstant", "Data"),
        "wikidata": _wikidata_async_descriptor(request),
        "dbnomics": (
            ("Economic datasets", "EconomicDatasetSearch", "Data")
            if request.operation == "search"
            else (
                ("Economic series matches", "EconomicSeriesSearch", "Data")
                if request.operation == "series_search"
                else ("Economic time series", "EconomicSeries", "Data")
            )
        ),
        "geonames": ("Place matches", "PlaceSearch", "Data"),
    }[request.provider]


def _wikidata_async_descriptor(request: KnowledgeRequest) -> tuple[str, str, str]:
    property_id = _wikidata_property_id(request)
    if property_id == "P17":
        return "Country", "Country", "Data"
    if property_id == "P30":
        return "Continent", "Continent", "Data"
    if property_id == "P1082":
        return "Population", "Population", "Data"
    if property_id == "P36":
        return "Capital", "Capital", "Data"
    if property_id == "P37":
        return "Official languages", "OfficialLanguages", "Data"
    if property_id == "P38":
        return "Currency", "Currency", "Data"
    if property_id == "P2044":
        return "Elevation", "Elevation", "Data"
    if property_id == "P2046":
        return "Area", "Area", "Data"
    if property_id == "P625":
        return "Coordinates", "Coordinates", "Data"
    if property_id == "P2131":
        return "Nominal GDP", "NominalGDP", "Data"
    if property_id == "P4010":
        return "GDP at purchasing power parity", "GDP_PPP", "Data"
    if property_id == "P2132":
        return "Nominal GDP per capita", "GDPPerCapita", "Data"
    if property_id == "P2299":
        return "GDP per capita at purchasing power parity", "GDPPerCapitaPPP", "Data"
    return "Entity matches", "EntitySearch", "Data"


def _wikidata_property_id(request: KnowledgeRequest) -> str | None:
    if request.provider != "wikidata":
        return None
    if request.operation == "property":
        return request.property_id
    return {"population": "P1082", "capital": "P36"}.get(request.operation)


def _wikidata_provider_presentation(
    request: KnowledgeRequest,
) -> tuple[str, str, str]:
    return {
        "P17": ("Country", "Country", "Geography"),
        "P30": ("Continent", "Continent", "Geography"),
        "P1082": ("Population", "Population", "Population"),
        "P36": ("Capital", "Capital", "Geography"),
        "P37": ("Official languages", "OfficialLanguages", "Languages"),
        "P38": ("Currency", "Currency", "Economics"),
        "P2044": ("Elevation", "Elevation", "Geography"),
        "P2046": ("Area", "Area", "Geography"),
        "P625": ("Coordinates", "Coordinates", "Geography"),
        "P2131": ("Nominal GDP", "NominalGDP", "Economics"),
        "P4010": ("GDP at purchasing power parity", "GDP_PPP", "Economics"),
        "P2132": ("Nominal GDP per capita", "GDPPerCapita", "Economics"),
        "P2299": (
            "GDP per capita at purchasing power parity",
            "GDPPerCapitaPPP",
            "Economics",
        ),
    }.get(_wikidata_property_id(request), ("Entity matches", "EntitySearch", "Entity"))


def async_query_payload(
    input_text: str,
    *,
    pod_id: str,
    pod_title: str,
    scanner: str,
    async_url: str,
    async_query_url: str | None = None,
    formats: list[str],
    elapsed: float,
    text_width: int = 500,
    verify_requested: bool = False,
) -> dict[str, Any]:
    input_image = _svg_text_image(input_text, width=text_width) if "image" in formats else None
    input_pod = _initial_pods(
        input_text, input_text, None, formats, input_image=input_image
    )[0]
    pending_pod = {
        "title": pod_title,
        "scanner": scanner,
        "id": pod_id,
        "position": 200,
        "error": False,
        "numsubpods": 0,
        "primary": True,
        "async": async_url,
        "status": "pending",
    }
    payload = _query_envelope(
        success=True,
        error=False,
        pods=[input_pod, pending_pod],
        elapsed=elapsed,
        parse_timing=0.0,
        datatypes="Mathematical" if scanner == "Mathematical" else "Data",
        warnings=_format_warnings(
            formats,
            _SUPPORTED_FORMATS | ({"image"} if input_image is not None else set()),
        ),
        assumptions=[],
        evidence={
            "kind": "asynchronous_query",
            "verified": False,
            **({
                "formal_verification": {
                    "status": "pending",
                    "verified": False,
                    "reason": "The requested proof stage will run with the result pod.",
                }
            } if verify_requested else {}),
        },
        qualification=(
            "The result is being computed asynchronously; use the pod async URL for the primary "
            "pod or the query-level asyncquery URL for the complete query result."
        ),
    )
    payload["queryresult"]["input"] = input_text
    if async_query_url is not None:
        payload["queryresult"]["asyncquery"] = async_query_url
    return payload


def render_pod_response(pod: dict[str, Any], output: str) -> Response:
    if output == "json":
        return JSONResponse(content={
            "pod": pod,
            **({"evidence": pod["evidence"]} if pod.get("evidence") is not None else {}),
        })
    wrapper = ET.Element("wrapper")
    _append_pod(wrapper, pod)
    if isinstance(pod.get("evidence"), dict):
        evidence = ET.SubElement(wrapper[0], "evidence")
        for key, value in pod["evidence"].items():
            if isinstance(value, (str, int, float, bool)):
                evidence.set(key, _xml_value(value))
            else:
                nested = ET.SubElement(evidence, key)
                nested.text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return Response(
        content=ET.tostring(wrapper[0], encoding="unicode", short_empty_elements=True),
        media_type="application/xml; charset=utf-8",
    )


def execute_query(
    input_text: str,
    assumptions: list[str] | None,
    formats: list[str],
    *,
    location: str | None = None,
    latlong: str | None = None,
    units: str | None = None,
    series_offset: int = 0,
    pod_states: list[str] | None = None,
    include_pod_ids: list[str] | None = None,
    exclude_pod_ids: list[str] | None = None,
    pod_titles: list[str] | None = None,
    pod_indexes: list[str] | None = None,
    scanners: list[str] | None = None,
    ignorecase: bool = False,
    parse_timeout_seconds: float = 5.0,
    scan_timeout_seconds: float = 3.0,
    format_timeout_seconds: float = 8.0,
    pod_timeout_seconds: float = 4.0,
    total_timeout_seconds: float = 20.0,
    text_width: int = 500,
    max_image_width: int = 500,
    magnification: float = 1.0,
    plot_width: int = 200,
    verify: bool = False,
    proof_timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    started = perf_counter()
    deadline = started + total_timeout_seconds
    planning_input = input_text.casefold() if ignorecase else input_text
    requested_pod_states = _split_values(pod_states or [])
    if pod_states is not None and not requested_pod_states:
        raise FullResultsRequestError("podstate must contain a supported state input")
    decimal_state_level = _decimal_state_level(requested_pod_states)
    numeric_precision = _DECIMAL_APPROXIMATION_PRECISIONS[decimal_state_level]
    provider_parse_started = perf_counter()
    try:
        non_entity_selections, entity_selection = _split_entity_assumption_selections(
            assumptions or []
        )
        gdp_resolution = _resolve_gdp_request(input_text, non_entity_selections)
        if gdp_resolution is not None and any(value is not None for value in (location, latlong, units)):
            raise FullResultsRequestError(
                "location, latlong, and units are currently supported for weather queries"
            )
        if gdp_resolution is None:
            provider_request = _provider_request(
                input_text, location, latlong, units, series_offset
            )
            provider_assumptions: list[dict[str, Any]] = []
        else:
            provider_request, gdp_assumption = gdp_resolution
            provider_assumptions = [gdp_assumption]
        if entity_selection is not None and not _supports_wikidata_entity_selection(
            provider_request
        ):
            raise AssumptionSelectionError(
                "entity:wikidata selections apply only to Wikidata property lookups"
            )
    except ProviderAssumptionNeeded as exc:
        return _provider_assumption_clarification(
            input_text, exc, formats, perf_counter() - started, text_width
        )
    except ProviderContextNeeded as exc:
        return _unrecognized_query(input_text, str(exc), perf_counter() - started)
    provider_parse_timing = perf_counter() - provider_parse_started
    if series_offset and not (
        provider_request is not None
        and provider_request.provider == "dbnomics"
        and provider_request.operation == "series_search"
    ):
        raise FullResultsRequestError(
            "seriesoffset is only supported for DBnomics series search"
        )
    if provider_request is not None:
        if requested_pod_states:
            raise FullResultsRequestError(
                "podstate is currently supported for mathematical decimal approximations"
            )
        if non_entity_selections and not provider_assumptions:
            raise AssumptionSelectionError(
                "mathematical assumption selections do not apply to this data query"
            )
        payload = _execute_provider_query(
            input_text,
            provider_request,
            formats,
            started,
            provider_parse_timing,
            include_pod_ids=include_pod_ids,
            exclude_pod_ids=exclude_pod_ids,
            pod_titles=pod_titles,
            pod_indexes=pod_indexes,
            scanners=scanners,
            deadline=deadline,
            scan_timeout_seconds=scan_timeout_seconds,
            format_timeout_seconds=format_timeout_seconds,
            pod_timeout_seconds=pod_timeout_seconds,
            text_width=text_width,
            plot_width=plot_width,
            assumptions=provider_assumptions,
            entity_selection=entity_selection,
        )
        if verify:
            payload["queryresult"].setdefault("evidence", {})["formal_verification"] = {
                "status": "unsupported",
                "verified": False,
                "reason": (
                    "Retrieved provider data is source-backed input, not a formal premise; "
                    "Lean does not verify the external source's factual truth."
                ),
            }
            payload["queryresult"]["qualification"] = (
                "The provider value is source-backed retrieved data. Lean verification was "
                "not performed because external factual claims require a formalized premise."
            )
        return payload
    remaining_seconds = deadline - perf_counter()
    if remaining_seconds <= 0:
        return _symbolic_timeout_response(
            input_text, started, formats, parse_timing=provider_parse_timing,
            text_width=text_width,
        )
    try:
        interpreted, symbolic_request = _prepare_query(
            planning_input,
            assumptions or [],
            validate_only=False,
            numeric_precision=numeric_precision,
            time_limit_seconds=remaining_seconds,
            parse_timeout_seconds=parse_timeout_seconds,
            pod_timeout_seconds=scan_timeout_seconds,
            deadline=deadline,
        )
    except AssumptionSelectionError:
        raise
    except InterpretationTimeout as exc:
        if deadline - perf_counter() <= 0.001:
            return _symbolic_timeout_response(
                input_text, started, formats, parse_timing=exc.parse_timing_seconds,
                text_width=text_width,
            )
        return _parse_timeout_response(
            input_text, started, parse_timing=exc.parse_timing_seconds
        )
    except InterpretationError as exc:
        return _unrecognized_query(
            input_text,
            str(exc),
            perf_counter() - started,
            assumptions=getattr(exc, "assumptions", []),
            interpretation=getattr(exc, "interpretation", None),
            classification=getattr(exc, "classification", None),
        )

    if interpreted.get("outcome") == "timeout":
        if deadline - perf_counter() <= 0.001:
            interpreted["timeout_stage"] = "total"
        else:
            interpreted["timeout_stage"] = "scan"

    parse_timing = float(interpreted["parse_timing"])
    expression = interpreted["interpretation"]["expression"]
    typed_ast = interpreted["interpretation"].get("typed_ast", {})
    is_typed_quantity = interpreted["interpretation"].get("kind") == "typed_quantity"
    is_typed_date = interpreted["interpretation"].get("kind") == "typed_date"
    is_typed_list = interpreted["interpretation"].get("kind") == "typed_list"
    is_typed_number = interpreted["interpretation"].get("kind") == "typed_number"
    input_display = (
        f"{typed_ast['magnitude']} {typed_ast['unit']}"
        if is_typed_quantity
        else typed_ast.get("input", expression)
        if is_typed_date
        else typed_ast.get("display", expression)
        if is_typed_list
        else typed_ast.get("display", expression)
        if is_typed_number
        else typed_ast.get("display_expression", expression)
    )
    if interpreted.get("outcome") == "computed":
        symbolic = {
            "result": interpreted.get("result"),
            "input_mathml": interpreted.get("input_mathml"),
            "result_mathml": interpreted.get("result_mathml"),
            "numeric_approximation": interpreted.get("numeric_approximation"),
            "evidence": interpreted.get("evidence"),
        }
    elif interpreted.get("outcome") == "timeout":
        symbolic = {
            "timed_out": True,
            "timeout_stage": interpreted.get(
                "timeout_stage",
                "total" if deadline - perf_counter() <= 0.001 else "pod",
            ),
            "evidence": interpreted.get("evidence"),
        }
    else:
        remaining_seconds = deadline - perf_counter()
        if remaining_seconds <= 0:
            symbolic = {"timed_out": True, "timeout_stage": "total"}
        else:
            computation_seconds = min(remaining_seconds, scan_timeout_seconds)
            symbolic = run_symbolic(symbolic_request.model_copy(update={
                "time_limit_seconds": computation_seconds,
                **({"plot_width": plot_width} if symbolic_request.operation == "plot" else {}),
            }))
            if symbolic.get("timed_out"):
                symbolic["timeout_stage"] = (
                    "total"
                    if deadline - perf_counter() <= 0.001
                    else "scan"
                )
        parse_timing += float(symbolic.get("parse_timing_seconds", 0.0))
    elapsed = perf_counter() - started
    if elapsed > total_timeout_seconds and not symbolic.get("timed_out"):
        symbolic = {
            "timed_out": True,
            "timeout_stage": "total",
            "limit_seconds": total_timeout_seconds,
        }

    input_mathml = interpreted.get("input_mathml") if interpreted.get("outcome") == "computed" else None
    if symbolic.get("timed_out"):
        timeout_stage = symbolic.get("timeout_stage", "total")
        timeout_message = {
            "scan": "The mathematical scan exceeded its scan time limit.",
            "total": "The symbolic result exceeded its total execution time limit.",
        }.get(timeout_stage, "The symbolic computation timed out.")
        timeout_qualification = {
            "scan": "The mathematical scan exceeded its local scan budget; no result was produced.",
            "total": "A timeout contains no result and is not a proof.",
        }.get(timeout_stage, "A timeout contains no result and is not a proof.")
        format_budget = _FormatStageBudget(
            format_timeout_seconds=format_timeout_seconds,
            pod_timeout_seconds=pod_timeout_seconds,
            total_deadline=deadline,
        )
        timeout_candidates = [
            {"title": "Input interpretation", "scanner": "Identity", "id": "Input", "position": 100},
            {"title": "Result", "scanner": "Mathematical", "id": "Result", "position": 200},
        ]
        timeout_selected_ids = {
            pod["id"]
            for pod in _select_pods(
                timeout_candidates,
                include_pod_ids,
                exclude_pod_ids,
                pod_titles,
                pod_indexes,
                scanners,
            )
        }

        def format_timeout_input() -> dict[str, Any]:
            if is_typed_quantity:
                input_minput = _wolfram_quantity(typed_ast)
            elif is_typed_date or is_typed_list or is_typed_number:
                input_minput = expression
            elif isinstance(typed_ast.get("wolfram_input_expression"), str):
                input_minput = typed_ast["wolfram_input_expression"]
            else:
                input_minput = _wolfram_input_for_query(
                    input_display,
                    symbolic_request,
                    is_explicit_operation=(
                        interpreted["interpretation"].get("kind") == "typed_symbolic_operation"
                    ),
                )
            input_image = None
            if "image" in formats:
                input_image = mathml_image(
                    input_mathml,
                    input_display,
                    max_width=max_image_width,
                    magnification=magnification,
                ) or _svg_text_image(input_display, width=text_width)
            return _input_pod(
                input_display,
                formats,
                input_mathml=input_mathml,
                input_minput=input_minput,
                input_image=input_image,
            )

        pods = []
        if "Input" in timeout_selected_ids:
            timeout_input = format_budget.render(
                "Input interpretation", format_timeout_input
            )
            if timeout_input is not None:
                pods.append(timeout_input)
        if "Result" in timeout_selected_ids:
            timeout_result = format_budget.render(
                "Result", lambda: _result_pod(None, formats)
            )
            if timeout_result is not None:
                pods.append(timeout_result)
        warnings = _format_warnings(
            formats,
            _SUPPORTED_FORMATS | ({"image"} if "image" in formats else set()),
        )
        if format_budget.timed_out_pods:
            warnings.append(_format_timeout_warning(format_budget.timed_out_pods))
        payload = _query_envelope(
            success=False,
            error=False,
            pods=pods,
            elapsed=perf_counter() - started,
            parse_timing=parse_timing,
            datatypes="Mathematical",
            timedout="Mathematical",
            timedoutpods=",".join(dict.fromkeys(["Result", *format_budget.timed_out_pods])),
            warnings=[*warnings, {"text": timeout_message}],
            assumptions=interpreted["assumptions"],
            interpretation=interpreted["interpretation"],
            classification=interpreted["classification"],
            evidence={
                "kind": "symbolic_timeout",
                "engine": "sympy",
                "timeout_stage": timeout_stage,
                "verified": False,
            },
            qualification=timeout_qualification,
        )
        payload["queryresult"]["input"] = input_text
        return filter_response_pods(
            payload, include_pod_ids, exclude_pod_ids, pod_titles, pod_indexes, scanners
        )

    if symbolic.get("error"):
        if "diagnostics" in symbolic:
            raise FullResultsBackendError(str(symbolic.get("error")))
        return _unrecognized_query(input_text, str(symbolic.get("error")), elapsed)

    numeric_approximation = symbolic.get("numeric_approximation")
    if requested_pod_states and not numeric_approximation:
        raise FullResultsRequestError(
            "podstate is only available when the query has a non-integer numeric result"
        )

    result_text = _display_physical_unit_symbols(
        str(symbolic.get("result", "")),
        typed_ast.get("unit_bindings"),
    )
    formal_verification = None
    if verify:
        proof_started = perf_counter()
        proof_deadline = proof_started + proof_timeout_seconds
        formal_verification = _verify_query_computation(
            interpreted,
            symbolic_request,
            result_text,
            max(0.001, proof_deadline - perf_counter()),
            symbolic_result=symbolic.get("result"),
        )
        if symbolic_request.operation == "identity":
            formal_verification.setdefault("claim_status", "not_established")
            formal_verification.setdefault(
                "claim_basis", "No Lean-checked difference result is available"
            )
            formal_verification.setdefault(
                "claim_scope",
                "universal equality over real values of free variables in the supported fragment",
            )
            if formal_verification["claim_status"] == "not_established":
                counterexample = _find_identity_counterexample(
                    symbolic_request,
                    proof_deadline,
                )
                if counterexample is not None:
                    formal_verification["claim_status"] = "refuted"
                    formal_verification["claim_basis"] = (
                        "A Z3-generated exact rational witness was independently replayed "
                        "against both sides of the identity"
                    )
                    formal_verification["counterexample"] = counterexample
                else:
                    nonzero_evidence = _certify_nonzero_identity_difference(
                        symbolic_request,
                        proof_deadline,
                    )
                    if nonzero_evidence is not None:
                        formal_verification["claim_status"] = "refuted"
                        formal_verification["claim_basis"] = (
                            "A Lean-checked rational error bound places the exact difference "
                            "strictly away from zero"
                        )
                        formal_verification["claim_refutation_evidence"] = nonzero_evidence
        # The Lean proof stage has its own prooftimeout, separate from the query
        # scan/format deadline. Preserve the full formatting budget after it ends.
        deadline += perf_counter() - proof_started
        verified_result = formal_verification.pop("_verified_result", None)
        if isinstance(verified_result, str):
            # Exact trigonometric values can have different exact radical forms in
            # SymPy and the Lean-backed CAG route. Display the value Lean proved.
            result_text = verified_result
            if (
                symbolic_request.operation == "numeric"
                and formal_verification["status"] == "checked"
            ):
                symbolic["result"] = verified_result
                symbolic["result_mathml"] = None
        elapsed = perf_counter() - started
    candidate_pods = [
        {"title": "Input interpretation", "scanner": "Identity", "id": "Input", "position": 100},
        {
            "title": "Result",
            "scanner": "Date" if is_typed_date else "Mathematical",
            "id": "Result",
            "position": 200,
        },
    ]
    if numeric_approximation is not None:
        candidate_pods.append({
            "title": "Decimal approximation",
            "scanner": "Numeric",
            "id": "DecimalApproximation",
            "position": 300,
        })
    selected_pod_ids = {
        pod["id"]
        for pod in _select_pods(
            candidate_pods,
            include_pod_ids,
            exclude_pod_ids,
            pod_titles,
            pod_indexes,
            scanners,
        )
    }
    format_budget = _FormatStageBudget(
        format_timeout_seconds=format_timeout_seconds,
        pod_timeout_seconds=pod_timeout_seconds,
        total_deadline=deadline,
    )

    def format_input_pod() -> dict[str, Any]:
        if is_typed_quantity:
            input_minput = _wolfram_quantity(typed_ast)
        elif is_typed_date or is_typed_list or is_typed_number:
            input_minput = expression
        elif isinstance(typed_ast.get("wolfram_input_expression"), str):
            input_minput = typed_ast["wolfram_input_expression"]
        else:
            input_minput = _wolfram_input_for_query(
                input_display,
                symbolic_request,
                is_explicit_operation=(
                    interpreted["interpretation"].get("kind") == "typed_symbolic_operation"
                ),
            )
        input_image = None
        if "image" in formats:
            input_image = mathml_image(
                input_mathml,
                input_display,
                max_width=max_image_width,
                magnification=magnification,
            ) or _svg_text_image(input_display, width=text_width)
        return _input_pod(
            input_display,
            formats,
            input_mathml=input_mathml,
            input_minput=input_minput,
            input_image=input_image,
        )

    def format_result_pod() -> dict[str, Any]:
        if is_typed_quantity:
            result_moutput = _wolfram_quantity(typed_ast)
        elif is_typed_date or is_typed_list:
            result_moutput = expression
        elif symbolic_request.operation == "series":
            result_moutput = _wolfram_series_output(symbolic.get("series_data"))
        elif symbolic_request.operation == "plot":
            result_moutput = _wolfram_input_for_query(
                expression,
                symbolic_request,
                is_explicit_operation=True,
            )
        else:
            result_moutput = _wolfram_value(symbolic.get("result"))
        result_image = None
        plot_svg = symbolic.get("plot_svg")
        if "image" in formats and isinstance(plot_svg, str):
            encoded_image = base64.b64encode(plot_svg.encode("utf-8")).decode("ascii")
            result_image = {
                "src": f"data:image/svg+xml;base64,{encoded_image}",
                "alt": result_text,
                "title": result_text,
                "width": plot_width,
                "height": round(plot_width * 2 / 3),
            }
        if "image" in formats and result_image is None:
            result_image = mathml_image(
                symbolic.get("result_mathml"),
                result_text,
                max_width=max_image_width,
                magnification=magnification,
            ) or _svg_text_image(result_text, width=text_width)
        return _result_pod(
            result_text,
            formats,
            result_mathml=symbolic.get("result_mathml"),
            result_moutput=result_moutput,
            result_image=result_image,
            result_scanner="Date" if is_typed_date else "Mathematical",
        )

    pods = []
    if "Input" in selected_pod_ids:
        input_pod = format_budget.render("Input interpretation", format_input_pod)
        if input_pod is not None:
            pods.append(input_pod)
    if "Result" in selected_pod_ids:
        result_pod = format_budget.render("Result", format_result_pod)
        if result_pod is not None:
            pods.append(result_pod)
    if numeric_approximation is not None and "DecimalApproximation" in selected_pod_ids:
        def format_approximation_pod() -> dict[str, Any]:
            approximation_image = (
                _svg_text_image(str(numeric_approximation["value"]), width=text_width)
                if "image" in formats
                else None
            )
            return _decimal_approximation_pod(
                numeric_approximation,
                formats,
                approximation_image=approximation_image,
                decimal_state_level=decimal_state_level,
            )

        approximation_pod = format_budget.render(
            "Decimal approximation", format_approximation_pod
        )
        if approximation_pod is not None:
            pods.append(approximation_pod)
    warnings = _format_warnings(
        formats,
        _SUPPORTED_FORMATS | ({"image"} if "image" in formats else set()),
    )
    if format_budget.timed_out_pods:
        warnings.append(_format_timeout_warning(format_budget.timed_out_pods))
    elapsed = perf_counter() - started
    filtered = _select_pods(
        pods, include_pod_ids, exclude_pod_ids, pod_titles, pod_indexes, scanners
    )
    evidence = symbolic.get("evidence", {
        "kind": "symbolic_result", "engine": "sympy", "verified": False
    })
    qualification = interpreted.get(
        "qualification", "Symbolic computation is not a Lean proof."
    )
    if formal_verification is not None:
        evidence = {
            **evidence,
            "kind": "symbolic_result_with_lean_verification",
            "verified": formal_verification["verified"],
            "verification_scope": (
                "checks the selected typed interpretation and computed result; it does not "
                "prove that a natural-language phrase was translated faithfully"
            ),
            "formal_verification": formal_verification,
        }
        if (
            symbolic_request.operation == "identity"
            and formal_verification.get("claim_status") == "not_established"
            and formal_verification["status"] != "checked"
        ):
            qualification = (
                "The requested identity is not established. The displayed difference is a "
                "symbolic result without a Lean proof or validated rational counterexample."
            )
        elif (
            symbolic_request.operation == "identity"
            and formal_verification.get("claim_status") == "refuted"
            and isinstance(formal_verification.get("counterexample"), dict)
        ):
            qualification = (
                "The selected identity is refuted by an exact rational counterexample "
                "independently replayed by the service."
            )
        elif (
            symbolic_request.operation == "identity"
            and formal_verification.get("claim_status") == "refuted"
            and isinstance(formal_verification.get("claim_refutation_evidence"), dict)
        ):
            qualification = (
                "The selected identity is refuted because a Lean-checked rational error "
                "bound proves its exact difference cannot be zero."
            )
        elif formal_verification["status"] == "checked":
            if symbolic_request.operation == "numeric":
                approximation = formal_verification["numeric_approximation"]
                qualification = (
                    "Lean proved that the displayed decimal differs from the exact "
                    f"value by at most {approximation['absolute_error_bound']}."
                )
            elif symbolic_request.operation == "identity":
                claim_status = formal_verification["claim_status"]
                if claim_status == "proved":
                    qualification = (
                        "Lean proved the left-minus-right difference equals zero, establishing "
                        "the selected identity for all real values of its free variables in "
                        "the supported formula fragment."
                    )
                elif claim_status == "refuted":
                    qualification = (
                        "Lean proved the left-minus-right difference is a nonzero constant, "
                        "which refutes the selected identity for all real values of its "
                        "free variables."
                    )
                else:
                    qualification = (
                        "Lean checked the selected difference result, but did not prove "
                        "or refute the identity. See formal_verification.claim_status."
                    )
            else:
                qualification = (
                    "Lean checked the computation theorem for the selected interpretation. "
                    "The theorem source and assumption choices are included in the response."
                )
        else:
            qualification = (
                f"The symbolic result is not Lean-verified: {formal_verification['reason']}"
            )
    payload = _query_envelope(
        success=not (format_budget.timed_out_pods and not filtered),
        error=False,
        pods=filtered,
        elapsed=elapsed,
        parse_timing=parse_timing,
        datatypes="Date" if is_typed_date else "Mathematical",
        warnings=warnings,
        assumptions=interpreted["assumptions"],
        interpretation=interpreted["interpretation"],
        classification=interpreted["classification"],
        evidence=evidence,
        qualification=qualification,
        timedoutpods=",".join(format_budget.timed_out_pods),
    )
    payload["queryresult"]["input"] = input_text
    return payload


def _verify_query_computation(
    interpreted: dict[str, Any],
    request: SymbolicRequest,
    query_result: str,
    timeout_seconds: float,
    *,
    symbolic_result: object,
) -> dict[str, Any]:
    typed_ast = interpreted.get("interpretation", {}).get("typed_ast", {})
    is_coordinate_calculus = typed_ast.get("node") == "vector_calculus_operation"
    coordinate_operation = typed_ast.get("operation")
    is_coordinate_divergence = is_coordinate_calculus and coordinate_operation == "divergence"
    is_coordinate_gradient = is_coordinate_calculus and coordinate_operation == "gradient"
    is_coordinate_curl = is_coordinate_calculus and coordinate_operation == "curl"
    is_coordinate_laplacian = is_coordinate_calculus and coordinate_operation == "laplacian"
    code = (
        "CoordinateDivergence"
        if is_coordinate_divergence
        else "CoordinateGradient"
        if is_coordinate_gradient
        else "CoordinateCurl"
        if is_coordinate_curl
        else "CoordinateLaplacian"
        if is_coordinate_laplacian
        else _query_computation_code(request)
    )
    if code is None:
        return {
            "status": "unsupported",
            "verified": False,
            "reason": "This operation is outside the bounded Lean computation fragment.",
        }

    proof_deadline = perf_counter() + max(0.001, min(timeout_seconds, 180.0))
    if is_coordinate_divergence:
        computation = prepare_coordinate_divergence(
            typed_ast.get("components", []),
            str(typed_ast.get("coordinate_system", "")),
            typed_ast.get("derivative_terms", []),
            query_result,
        )
    elif is_coordinate_gradient:
        computation = prepare_coordinate_gradient(
            str(typed_ast.get("expression", "")),
            str(typed_ast.get("coordinate_system", "")),
            typed_ast.get("derivative_terms", []),
            typed_ast.get("resolved_components", []),
        )
    elif is_coordinate_curl:
        computation = prepare_coordinate_curl(
            typed_ast.get("components", []),
            str(typed_ast.get("coordinate_system", "")),
            typed_ast.get("derivative_terms", []),
            typed_ast.get("resolved_components", []),
        )
    elif is_coordinate_laplacian:
        computation = prepare_coordinate_laplacian(
            str(typed_ast.get("expression", "")),
            str(typed_ast.get("coordinate_system", "")),
            typed_ast.get("first_derivative_terms", []),
            typed_ast.get("second_derivative_terms", []),
            query_result,
        )
    else:
        computation = prepare_computation(
            code,
            max(0.001, min(timeout_seconds, 60.0)),
        )
    if computation is None:
        if request.operation == "numeric":
            return {
                "status": "unsupported",
                "verified": False,
                "reason": (
                    "This numeric expression is outside the certified CAG approximation "
                    "fragment or exceeds its supported precision."
                ),
            }
        return {
            "status": "unsupported",
            "verified": False,
            "reason": (
                "The selected interpretation is not a bounded rational polynomial or "
                "supported calculus expression."
            ),
        }
    if computation.timed_out:
        return {
            "status": "timeout",
            "verified": False,
            "reason": "Symbolic candidate generation exceeded the proof-stage time budget.",
        }
    if computation.backend_error is not None:
        return {
            "status": "backend_error",
            "verified": False,
            "reason": computation.backend_error,
        }
    if request.operation == "numeric" and (
        computation.numeric_precision is None
        or computation.numeric_error_bound is None
    ):
        return {
            "status": "unsupported",
            "verified": False,
            "reason": "The numeric backend did not produce a certified approximation bound.",
        }

    if (
        request.operation == "numeric"
        or is_coordinate_gradient
        or is_coordinate_curl
        or is_coordinate_laplacian
    ):
        query_normalized = candidate_normalized = None
    elif request.operation == "solve" and request.variable is not None:
        query_normalized = _symbolic_solve_signature(
            symbolic_result,
            request.variable,
        )
        candidate_normalized = _wolfram_solve_signature(
            computation.display_result,
            request.variable,
        )
    else:
        query_normalized = _canonical_formula(query_result)
        candidate_normalized = _canonical_formula(
            computation.display_result.replace("^", "**")
        )
    if request.operation != "numeric" and (
        query_normalized is None
        or candidate_normalized is None
        or query_normalized != candidate_normalized
    ) and not computation.proof_result_authoritative:
        return {
            "status": "candidate_mismatch",
            "verified": False,
            "reason": (
                "The Lean proof candidate did not match the result pod, so no proof was attached."
            ),
            "query_result": query_result,
            "proof_candidate": computation.display_result,
        }

    remaining = proof_deadline - perf_counter()
    if remaining <= 0.001:
        return {
            "status": "timeout",
            "verified": False,
            "reason": "The proof-stage time budget expired before Lean checking began.",
        }
    lean_result = check_computation(computation, remaining)
    verification: dict[str, Any] = {
        "status": lean_result.status,
        "verified": lean_result.status == "checked",
        "claim_type": (
            "numeric_approximation"
            if request.operation == "numeric"
            else "coordinate_divergence"
            if is_coordinate_divergence
            else "coordinate_gradient"
            if is_coordinate_gradient
            else "coordinate_curl"
            if is_coordinate_curl
            else "coordinate_laplacian"
            if is_coordinate_laplacian
            else _query_claim_type(request, code)
        ),
        "interpretation_kind": interpreted.get("interpretation", {}).get("kind"),
        "assumptions": interpreted.get("assumptions", []),
        "scope": (
            (
                "proves that the exact selected numeric expression differs from the "
                "reported decimal by no more than the stated rational absolute-error bound"
            )
            if request.operation == "numeric"
            else (
                "Lean proves the selected three-component polynomial divergence formula "
                "in the chosen Cartesian3D, Cylindrical3D, or Spherical3D coordinate chart, "
                "under the "
                "listed chart-domain conditions"
                if is_coordinate_divergence
                else (
                    "Lean proves the selected polynomial gradient components and coordinate "
                    "chart factors in the chosen Cartesian3D, Cylindrical3D, or Spherical3D "
                    "chart, under the listed chart-domain conditions"
                    if is_coordinate_gradient
                    else (
                        "Lean proves the selected polynomial curl components in the chosen "
                        "Cartesian3D, Cylindrical3D, or Spherical3D orthonormal chart, under "
                        "the listed chart-domain conditions"
                        if is_coordinate_curl
                        else (
                            "Lean proves the selected scalar Laplacian formula and second derivatives "
                            "in the chosen Cartesian3D, Cylindrical3D, or Spherical3D chart, under "
                            "the listed chart-domain conditions"
                            if is_coordinate_laplacian
                            else
                            "checks the exact selected formula and result under the listed real-parameter "
                            "and domain conditions; "
                            "it does not verify the English language-to-formula translation"
                        )
                    )
                )
            )
        ),
        "lean_source_sha256": hashlib.sha256(lean_result.source.encode("utf-8")).hexdigest(),
        "lean_source": lean_result.source,
        "result_match": not computation.proof_result_authoritative,
    }
    if request.operation == "identity":
        verification["claim_status"] = _identity_claim_status(
            computation.display_result,
            lean_result.status == "checked",
        )
        verification["claim_basis"] = (
            f"Lean proved left-minus-right equals {computation.display_result}"
            if lean_result.status == "checked"
            else "No Lean-checked difference result is available"
        )
        verification["claim_scope"] = (
            "universal equality over real values of free variables in the supported fragment"
        )
    if request.operation == "numeric":
        certified_value = computation.display_result
        try:
            if Fraction(Decimal(query_result)) == Fraction(Decimal(certified_value)):
                # Preserve the query's requested decimal formatting when it denotes
                # the same exact rational as the value Lean checked.
                certified_value = query_result
        except (InvalidOperation, OverflowError, ValueError):
            pass
        verification["numeric_approximation"] = {
            "value": certified_value,
            "precision_significant_digits": computation.numeric_precision,
            "absolute_error_bound": computation.numeric_error_bound,
            "claim": (
                "the exact mathematical value differs from value by at most this bound"
            ),
        }
        verification["result_match"] = lean_result.status == "checked"
        if lean_result.status == "checked":
            verification["_verified_result"] = certified_value
    elif lean_result.status == "checked" and computation.proof_result_authoritative:
        verification["result_match"] = True
        verification["_verified_result"] = computation.display_result
    if computation.domain_conditions:
        verification["domain_conditions"] = list(computation.domain_conditions)
    if computation.real_parameters:
        verification["real_parameters"] = list(computation.real_parameters)
    if lean_result.status == "checked":
        verification["axiom_audit"] = {
            "status": "passed",
            "dependencies": list(lean_result.axioms),
        }
    elif lean_result.diagnostics:
        verification["reason"] = lean_result.diagnostics
    return verification


def _query_computation_code(request: SymbolicRequest) -> str | None:
    if request.operation == "numeric" and request.expression is not None:
        expression = re.sub(
            r"\bpi\b",
            "Pi",
            request.expression,
            flags=re.IGNORECASE,
        )
        return f"N[{expression}, {request.numeric_precision}]"
    if request.operation in {"simplify", "expand", "factor"} and request.expression is not None:
        exact_trig_code = _query_exact_trigonometric_code(request.expression)
        if request.operation == "simplify" and exact_trig_code is not None:
            return exact_trig_code
        if (
            request.operation == "simplify"
            and re.search(r"\b(?:sin|cos|tan)\s*\(", request.expression, re.IGNORECASE)
            and re.search(r"\bpi\b", request.expression, re.IGNORECASE)
        ):
            exact_pi_expression = re.sub(
                r"\bpi\b",
                "Pi",
                request.expression,
                flags=re.IGNORECASE,
            )
            return f"Simplify[{exact_pi_expression}]"
        return f"{request.operation.title()}[{request.expression}]"
    if request.operation == "solve" and request.expression is not None and request.variable:
        return f"Solve[({request.expression}) == 0, {request.variable}, Reals]"
    if request.operation == "differentiate" and request.expression is not None and request.variable:
        return f"D[{request.expression}, {request.variable}]"
    if (
        request.operation == "limit"
        and request.expression is not None
        and request.variable is not None
        and request.point is not None
    ):
        return f"Limit[{request.expression}, {{{request.variable}, {request.point}}}]"
    if request.operation == "identity" and request.lhs is not None and request.rhs is not None:
        lhs = re.sub(r"\bpi\b", "Pi", request.lhs, flags=re.IGNORECASE)
        rhs = re.sub(r"\bpi\b", "Pi", request.rhs, flags=re.IGNORECASE)
        return f"Simplify[({lhs}) - ({rhs})]"
    if (
        request.operation == "integrate"
        and request.expression is not None
        and request.variable is not None
        and request.lower is not None
        and request.upper is not None
    ):
        return (
            f"Integrate[{request.expression}, "
            f"{{{request.variable}, {request.lower}, {request.upper}}}]"
        )
    if (
        request.operation in {"sum", "product"}
        and request.expression is not None
        and request.variable is not None
        and request.lower is not None
        and request.upper is not None
    ):
        operation = "Sum" if request.operation == "sum" else "Product"
        return (
            f"{operation}[{request.expression}, "
            f"{{{request.variable}, {request.lower}, {request.upper}}}]"
        )
    if (
        request.operation == "integrate"
        and request.expression is not None
        and request.variable is not None
        and request.lower is None
        and request.upper is None
    ):
        return f"Integrate[{request.expression}, {request.variable}]"
    return None


def _query_exact_trigonometric_code(expression: str) -> str | None:
    """Transpile a single exact sin/cos/tan of a rational multiple of pi."""
    try:
        parsed = parse_math_input(expression)
        node = ast.parse(parsed.expression, mode="eval").body
    except (MathInputError, SyntaxError, RecursionError, ValueError):
        return None
    if (
        not isinstance(node, ast.Call)
        or not isinstance(node.func, ast.Name)
        or node.func.id not in {"sin", "cos", "tan"}
        or len(node.args) != 1
        or node.keywords
    ):
        return None
    multiple = _query_pi_multiple(node.args[0])
    if (
        multiple is None
        or abs(multiple.numerator).bit_length() > 256
        or multiple.denominator.bit_length() > 256
    ):
        return None
    sign = "-" if multiple < 0 else ""
    numerator = abs(multiple.numerator)
    numerator_text = "" if numerator == 1 else f"{numerator}*"
    angle = f"{sign}{numerator_text}Pi"
    if multiple.denominator != 1:
        angle += f"/{multiple.denominator}"
    return f"{node.func.id.title()}[{angle}]"


def _query_pi_multiple(node: ast.AST) -> Fraction | None:
    if isinstance(node, ast.Name) and node.id == "pi":
        return Fraction(1)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _query_pi_multiple(node.operand)
        if value is None:
            return None
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, (ast.Add, ast.Sub)):
            left = _query_pi_multiple(node.left)
            right = _query_pi_multiple(node.right)
            if left is None or right is None:
                return None
            return left + right if isinstance(node.op, ast.Add) else left - right
        if isinstance(node.op, ast.Mult):
            left_pi = _query_pi_multiple(node.left)
            right_pi = _query_pi_multiple(node.right)
            left_rational = _constant_fraction(node.left)
            right_rational = _constant_fraction(node.right)
            if left_pi is not None and right_rational is not None:
                return left_pi * right_rational
            if right_pi is not None and left_rational is not None:
                return right_pi * left_rational
        if isinstance(node.op, ast.Div):
            numerator = _query_pi_multiple(node.left)
            denominator = _constant_fraction(node.right)
            if numerator is not None and denominator not in {None, 0}:
                return numerator / denominator
    return None


def _query_claim_type(request: SymbolicRequest, code: str) -> str:
    if (
        request.operation == "simplify"
        and request.expression is not None
        and re.search(
            r"\b(?:sin|cos|tan)\s*\(",
            request.expression,
            re.IGNORECASE,
        )
        and re.search(r"\bpi\b", request.expression, re.IGNORECASE)
    ):
        return "exact_trigonometric_expression_equality"
    if request.operation == "solve":
        return "real_solution_set_equivalence"
    if request.operation == "limit":
        return "rational_function_limit"
    if request.operation == "sum":
        return "finite_sum_equality"
    if request.operation == "product":
        return "finite_product_equality"
    if request.operation == "differentiate":
        return "HasDerivAt"
    if (
        request.operation == "integrate"
        and request.lower is None
        and request.upper is None
    ):
        return "HasDerivAt"
    if code.startswith("Integrate["):
        return "definite_integral_equality"
    if request.operation == "identity":
        return "universal_identity"
    return "polynomial_equality"


def _identity_claim_status(difference: str, checked: bool) -> str:
    """Separate checking the difference computation from proving the identity."""
    if not checked:
        return "not_established"
    normalized = difference.strip()
    if normalized in {"0", "+0", "-0"}:
        return "proved"
    try:
        exact_difference = Fraction(normalized)
    except (ValueError, ZeroDivisionError, OverflowError):
        return "not_established"
    return "refuted" if exact_difference else "proved"


def _find_identity_counterexample(
    request: SymbolicRequest,
    proof_deadline: float,
) -> dict[str, Any] | None:
    """Find a rational polynomial witness and validate it by exact replay."""
    if request.operation != "identity" or request.lhs is None or request.rhs is None:
        return None
    try:
        trees = [
            ast.parse(source, mode="eval")
            for source in (request.lhs, request.rhs)
        ]
    except (SyntaxError, ValueError, RecursionError):
        return None
    if any(
        isinstance(node, ast.Call)
        for tree in trees
        for node in ast.walk(tree)
    ):
        return None
    names = {
        node.id
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    if not names or names & {"e", "E", "I", "pi", "oo", "zoo", "nan"}:
        return None
    remaining_seconds = proof_deadline - perf_counter()
    if remaining_seconds < 1.0:
        return None

    from .formula_input import FormulaInputError, FormulaProofRequest, compile_formula
    from .models import Budget, Sort, Variable
    from .reasoner import analyze

    try:
        proof_request = FormulaProofRequest(
            formula=f"({request.lhs}) == ({request.rhs})",
            variables=[Variable(name=name, sort=Sort.REAL) for name in sorted(names)],
            budget=Budget(wall_seconds=min(5, int(remaining_seconds))),
        )
        claim_request = compile_formula(proof_request)
        response = analyze(claim_request, attempt_lean=False)
    except (FormulaInputError, ValidationError, ValueError, TypeError, OverflowError, RecursionError):
        return None

    witness = response.get("counterexample")
    evidence = response.get("evidence")
    if (
        response.get("outcome") != "counterexample_found"
        or not isinstance(witness, dict)
        or not isinstance(evidence, dict)
        or evidence.get("kind") != "validated_counterexample"
    ):
        return None
    return {
        "assignments": {str(name): str(value) for name, value in witness.items()},
        "solver": "z3",
        "validation": "independent_exact_rational_replay",
    }


def _certify_nonzero_identity_difference(
    request: SymbolicRequest,
    proof_deadline: float,
) -> dict[str, Any] | None:
    """Use a Lean-certified approximation to refute a closed nonzero difference."""
    if request.operation != "identity" or request.lhs is None or request.rhs is None:
        return None
    remaining_seconds = proof_deadline - perf_counter()
    if remaining_seconds <= 0.001:
        return None
    lhs = re.sub(r"\bpi\b", "Pi", request.lhs, flags=re.IGNORECASE)
    rhs = re.sub(r"\bpi\b", "Pi", request.rhs, flags=re.IGNORECASE)
    computation = prepare_computation(
        f"N[({lhs}) - ({rhs}), 50]",
        max(0.001, min(remaining_seconds, 5.0)),
    )
    if (
        computation is None
        or computation.numeric_error_bound is None
        or computation.numeric_precision is None
    ):
        return None
    try:
        approximation = Fraction(Decimal(computation.display_result))
        error_bound = Fraction(computation.numeric_error_bound)
    except (InvalidOperation, OverflowError, ValueError, ZeroDivisionError):
        return None
    distance_from_zero = abs(approximation) - error_bound
    if distance_from_zero <= 0:
        return None

    remaining_seconds = proof_deadline - perf_counter()
    if remaining_seconds <= 0.001:
        return None
    lean_result = check_computation(
        computation,
        min(remaining_seconds, 60.0),
    )
    if lean_result.status != "checked":
        return None
    return {
        "kind": "lean_checked_nonzero_interval",
        "approximation": computation.display_result,
        "precision_significant_digits": computation.numeric_precision,
        "absolute_error_bound": computation.numeric_error_bound,
        "distance_from_zero_lower_bound": str(distance_from_zero),
        "lean_source_sha256": hashlib.sha256(lean_result.source.encode("utf-8")).hexdigest(),
        "lean_source": lean_result.source,
        "axiom_audit": {
            "status": "passed",
            "dependencies": list(lean_result.axioms),
        },
    }


def _canonical_formula(source: str) -> str | None:
    try:
        parsed = parse_math_input(source)
        exact = _normalize_decimal_literals(parsed.expression)
        if exact is None:
            return None
        node = ast.parse(exact, mode="eval").body
    except (MathInputError, SyntaxError, RecursionError, ValueError):
        return None
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _symbolic_solve_signature(
    symbolic_result: object,
    variable: str,
) -> tuple[str, ...] | None:
    if not isinstance(symbolic_result, list):
        return None
    expressions: list[str] = []
    for solution in symbolic_result:
        if not isinstance(solution, dict) or set(solution) != {variable}:
            return None
        expressions.append(str(solution[variable]))
    return _real_root_signature(expressions, wolfram=False)


def _wolfram_solve_signature(
    source: str,
    variable: str,
) -> tuple[str, ...] | None:
    value = source.strip()
    if value == "{}":
        return ()
    if not value.startswith("{") or not value.endswith("}"):
        return None
    solutions = _split_top_level_commas(value[1:-1])
    if solutions is None:
        return None

    expressions: list[str] = []
    for solution in solutions:
        solution = solution.strip()
        if not solution.startswith("{") or not solution.endswith("}"):
            return None
        bindings = _split_top_level_commas(solution[1:-1])
        if bindings is None or len(bindings) != 1:
            return None
        binding = re.fullmatch(
            rf"\s*{re.escape(variable)}\s*->\s*(?P<value>.+?)\s*",
            bindings[0],
            flags=re.DOTALL,
        )
        if binding is None:
            return None
        expressions.append(binding.group("value"))
    return _real_root_signature(expressions, wolfram=True)


def _real_root_signature(
    expressions: list[str],
    *,
    wolfram: bool,
) -> tuple[str, ...] | None:
    import sympy

    signatures: set[str] = set()
    for expression in expressions:
        source = (
            _normalize_wolfram_real_functions(expression)
            if wolfram
            else expression
        )
        try:
            parsed = parse_math_input(source)
            node = ast.parse(parsed.expression, mode="eval").body
        except (MathInputError, RecursionError, SyntaxError, ValueError):
            return None
        value = _real_sympy_expression(node)
        if value is None or value.free_symbols or value.is_real is not True:
            return None
        value = sympy.simplify(value)
        if value.free_symbols or value.is_real is not True:
            return None
        signatures.add(sympy.srepr(value))
    return tuple(sorted(signatures))


def validate_query(
    input_text: str,
    assumptions: list[str] | None,
    *,
    location: str | None = None,
    latlong: str | None = None,
    units: str | None = None,
    series_offset: int = 0,
    ignorecase: bool = False,
    parse_timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    started = perf_counter()
    planning_input = input_text.casefold() if ignorecase else input_text
    try:
        non_entity_selections, entity_selection = _split_entity_assumption_selections(
            assumptions or []
        )
        gdp_resolution = _resolve_gdp_request(input_text, non_entity_selections)
        if gdp_resolution is not None and any(value is not None for value in (location, latlong, units)):
            raise FullResultsRequestError(
                "location, latlong, and units are currently supported for weather queries"
            )
        if gdp_resolution is None:
            provider_request = _provider_request(
                input_text, location, latlong, units, series_offset
            )
            provider_assumptions: list[dict[str, Any]] = []
        else:
            provider_request, gdp_assumption = gdp_resolution
            provider_assumptions = [gdp_assumption]
        if entity_selection is not None and not _supports_wikidata_entity_selection(
            provider_request
        ):
            raise AssumptionSelectionError(
                "entity:wikidata selections apply only to Wikidata property lookups"
            )
        if series_offset and not (
            provider_request is not None
            and provider_request.provider == "dbnomics"
            and provider_request.operation == "series_search"
        ):
            raise FullResultsRequestError(
                "seriesoffset is only supported for DBnomics series search"
            )
        if provider_request is not None:
            if non_entity_selections and not provider_assumptions:
                raise AssumptionSelectionError(
                    "mathematical assumption selections do not apply to this data query"
                )
            if entity_selection is not None:
                provider_assumptions.append(
                    _wikidata_entity_assumption(
                        [],
                        selected_id=entity_selection.rsplit(":", 1)[-1],
                        candidates_available=False,
                    )
                )
            provider_name = provider_request.provider
            elapsed = perf_counter() - started
            return {
                "validatequeryresult": {
                    "success": True,
                    "error": False,
                    "timing": _seconds(elapsed),
                    "parsetiming": _seconds(elapsed),
                    "version": _API_VERSION,
                    "service": "LEAN Engine",
                    "input": input_text,
                    "assumptions": provider_assumptions,
                    "interpretation": {
                        "kind": "typed_data_request",
                        "provider": provider_name,
                        "operation": provider_request.operation,
                        "request": provider_request.model_dump(exclude_none=True),
                    },
                }
            }
        interpreted, _ = _prepare_query(
            planning_input,
            assumptions or [],
            validate_only=True,
            parse_timeout_seconds=parse_timeout_seconds,
        )
    except ProviderAssumptionNeeded as exc:
        elapsed = perf_counter() - started
        return {
            "validatequeryresult": {
                "success": False,
                "error": False,
                "timing": _seconds(elapsed),
                "parsetiming": _seconds(elapsed),
                "version": _API_VERSION,
                "service": "LEAN Engine",
                "input": input_text,
                "assumptions": [exc.assumption],
                "interpretation": {
                    "kind": "ambiguous_data_request",
                    "provider": "wikidata",
                    "operation": (
                        "gdp_per_capita"
                        if exc.assumption["type"] == "GDPPerCapitaType"
                        else "gdp"
                    ),
                    "selection_required": True,
                },
                "tips": [{"text": str(exc)}],
            }
        }
    except ProviderContextNeeded as exc:
        elapsed = perf_counter() - started
        return {
            "validatequeryresult": {
                "success": False,
                "error": False,
                "timing": _seconds(elapsed),
                "parsetiming": _seconds(elapsed),
                "version": _API_VERSION,
                "service": "LEAN Engine",
                "input": input_text,
                "assumptions": [],
                "tips": [{"text": str(exc)}],
            }
        }
    except AssumptionSelectionError:
        raise
    except InterpretationTimeout as exc:
        elapsed = perf_counter() - started
        return {
            "validatequeryresult": {
                "success": False,
                "error": False,
                "timing": _seconds(elapsed),
                "parsetiming": _seconds(exc.parse_timing_seconds),
                "parsetimedout": True,
                "version": _API_VERSION,
                "service": "LEAN Engine",
                "input": input_text,
                "assumptions": [],
                "tips": [{"text": str(exc)}],
            }
        }
    except InterpretationError as exc:
        elapsed = perf_counter() - started
        result = {
            "success": False,
            "error": False,
            "timing": _seconds(elapsed),
            "parsetiming": _seconds(elapsed),
            "version": _API_VERSION,
            "service": "LEAN Engine",
            "input": input_text,
            "assumptions": getattr(exc, "assumptions", []),
            "tips": [{"text": str(exc)}],
        }
        if hasattr(exc, "interpretation"):
            result["interpretation"] = exc.interpretation
            result["classification"] = exc.classification
        return {"validatequeryresult": result}
    elapsed = perf_counter() - started
    return {
        "validatequeryresult": {
            "success": True,
            "error": False,
            "timing": _seconds(elapsed),
            "parsetiming": _seconds(float(interpreted["parse_timing"])),
            "version": _API_VERSION,
            "service": "LEAN Engine",
            "input": input_text,
            "assumptions": interpreted["assumptions"],
            "interpretation": interpreted["interpretation"],
            "classification": interpreted["classification"],
        }
    }


def _split_formula_variable_values(
    selections: list[str],
) -> tuple[list[str], dict[str, str]]:
    remaining: list[str] = []
    assignments: dict[str, str] = {}
    for selection in selections:
        if not selection.startswith("formula:variable:"):
            remaining.append(selection)
            continue
        match = re.fullmatch(
            r"formula:variable:([A-Za-z][A-Za-z0-9_]{0,31})=(.{1,256})",
            selection,
        )
        if match is None:
            raise AssumptionSelectionError(
                "FormulaVariable values must use the form formula:variable:x=<value>"
            )
        variable, value = match.groups()
        if variable in assignments:
            raise AssumptionSelectionError(
                f"provide only one FormulaVariable value for {variable}"
            )
        assignments[variable] = value
    return remaining, assignments


def _formula_variable_value_assumption(
    variable: str,
    value: str | None,
    normalized_value: str | None = None,
) -> dict[str, Any]:
    registry = assumption_registry()
    scheme = next(
        (
            item
            for item in registry.get("dynamic_assumptions", [])
            if item.get("type") == "FormulaVariable"
        ),
        None,
    )
    if scheme is None:
        raise RuntimeError("FormulaVariable dynamic assumption scheme is not registered")
    selected_id = f"formula:variable:{variable}={value}" if value is not None else None
    return {
        "type": "FormulaVariable",
        "description": f"Supply a value for formula variable {variable}.",
        "selected": selected_id,
        "selection_source": "caller_override" if selected_id else "awaiting_value",
        "values": (
            [{
                "input": selected_id,
                "desc": value,
                "valid": True,
                "variable": variable,
                **({"normalized_value": normalized_value}
                   if normalized_value is not None else {}),
            }]
            if selected_id is not None
            else []
        ),
        "value_input_pattern": f"formula:variable:{variable}=<value>",
        "registry_version": registry["registry_version"],
        "entry_version": scheme["entry_version"],
        "candidate_validation": (
            "free_symbol_bounded_expression_and_unit_dimension_validation_before_solver_routing"
        ),
    }


def _normalize_formula_variable_unit_value(value: str) -> str | None:
    """Normalize a scalar quantity such as `6.5 m/s` to named unit symbols."""
    match = re.fullmatch(
        rf"\s*(?P<magnitude>{_FORMULA_VARIABLE_NUMBER})\s*(?P<units>.+?)\s*",
        value,
    )
    if match is None:
        return None

    units = re.sub(r"\s+", "", match.group("units"))
    if not units:
        return None
    # Single-letter symbols are accepted only as part of a compound unit. A
    # standalone `m` remains ambiguous in the service's ordinary math grammar.
    compound_unit = "/" in units or "*" in units
    aliases = dict(UNIT_ALIAS_TO_NAME)
    if compound_unit:
        aliases.update({"m": "meter", "s": "second", "g": "gram"})

    factor_pattern = re.compile(r"(?P<name>[A-Za-z][A-Za-z0-9]*)(?:\^(?P<power>[+-]?\d+))?")
    factors = re.split(r"([*/])", units)
    if not factors or len(factors) % 2 == 0:
        return None

    normalized_factors: list[str] = []
    bindings: set[str] = set()
    for index, factor in enumerate(factors):
        if index % 2:
            if factor not in {"*", "/"}:
                return None
            normalized_factors.append(factor)
            continue
        factor_match = factor_pattern.fullmatch(factor)
        if factor_match is None:
            return None
        alias = factor_match.group("name").casefold()
        unit_name = aliases.get(alias)
        if unit_name is None or unit_name not in PHYSICAL_UNITS:
            return None
        exponent = int(factor_match.group("power") or "1")
        if abs(exponent) > 8:
            raise AssumptionSelectionError(
                "FormulaVariable unit exponents must be between -8 and 8"
            )
        unit_symbol = f"unit_{unit_name}"
        if exponent == 0:
            normalized_factors.append("1")
        else:
            bindings.add(unit_name)
            normalized_factors.append(
                unit_symbol if exponent == 1 else f"{unit_symbol}**{exponent}"
            )

    normalized_unit = "".join(normalized_factors)
    if not compound_unit and len(bindings) == 1:
        # Do not turn a lone `m` into meters without an explicit unit name.
        only_alias = factor_pattern.fullmatch(units)
        if only_alias and only_alias.group("name").casefold() in {"m", "s", "g"}:
            return None
    return f"({match.group('magnitude')})*({normalized_unit})"


def _normalize_formula_variable_unit_literals(expression: str) -> str:
    return _FORMULA_VARIABLE_EXPLICIT_UNIT.sub(
        lambda match: (
            f"{match.group('number')}*unit_"
            f"{UNIT_ALIAS_TO_NAME[match.group('unit').casefold()]}"
        ),
        expression,
    )


def _formula_unit_bindings(expression: str) -> dict[str, str]:
    return {
        match.group("symbol"): match.group("name")
        for match in _FORMULA_VARIABLE_UNIT_SYMBOL.finditer(expression)
        if match.group("name") in PHYSICAL_UNITS
    }


def _display_formula_variable_expression(
    expression: str,
    submitted_values: dict[str, str],
) -> str:
    displayed = expression
    for variable, value in submitted_values.items():
        displayed = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(variable)}(?![A-Za-z0-9_])",
            lambda _match, selected=value: f"({selected})",
            displayed,
        )
    return displayed


def _display_physical_unit_symbols(value: str, unit_bindings: object) -> str:
    if not isinstance(unit_bindings, dict):
        return value
    displayed = value
    for unit_type in set(unit_bindings.values()):
        if not isinstance(unit_type, str) or unit_type not in PHYSICAL_UNITS:
            continue
        unit = PHYSICAL_UNITS[unit_type]
        displayed = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(unit.sympy_name)}(?![A-Za-z0-9_])",
            unit.display,
            displayed,
        )
    return displayed


def _apply_formula_variable_values(
    expression: str,
    assignments: dict[str, str],
    *,
    parse_timeout_seconds: float = 5.0,
    deadline: float | None = None,
) -> tuple[str, tuple[str, ...], dict[str, str], dict[str, str]]:
    validation_started = perf_counter()
    try:
        parsed_expression = parse_math_input(
            _normalize_formula_variable_unit_literals(expression)
        )
    except MathInputError as exc:
        raise InterpretationError(str(exc)) from exc
    expression_units = _formula_unit_bindings(parsed_expression.expression)
    variables = tuple(
        symbol
        for symbol in free_symbols(parsed_expression.expression)
        if symbol not in expression_units
    )
    unknown_variables = sorted(set(assignments) - set(variables))
    if unknown_variables:
        raise AssumptionSelectionError(
            "FormulaVariable selection names symbols not present in this expression: "
            + ", ".join(unknown_variables)
        )

    normalized_values: dict[str, str] = {}
    submitted_values: dict[str, str] = {}
    replacement_nodes: dict[str, ast.expr] = {}
    for variable, value in assignments.items():
        try:
            normalized_value = _normalize_formula_variable_unit_value(value)
            if normalized_value is None:
                normalized_value = _normalize_formula_variable_unit_literals(value)
            parsed_value = parse_math_input(normalized_value)
            value_units = _formula_unit_bindings(parsed_value.expression)
            unresolved_symbols = [
                symbol
                for symbol in free_symbols(parsed_value.expression)
                if symbol not in value_units
            ]
            if unresolved_symbols:
                raise AssumptionSelectionError(
                    f"FormulaVariable value for {variable} must not contain free variables"
                )
            replacement_nodes[variable] = ast.parse(
                parsed_value.expression, mode="eval"
            ).body
        except MathInputError as exc:
            raise AssumptionSelectionError(
                f"FormulaVariable value for {variable} is not a supported expression: {exc}"
            ) from exc
        except SyntaxError as exc:
            raise AssumptionSelectionError(
                f"FormulaVariable value for {variable} is not a supported expression"
            ) from exc
        if value_units:
            remaining_parse_seconds = parse_timeout_seconds - (
                perf_counter() - validation_started
            )
            if deadline is not None:
                remaining_parse_seconds = min(
                    remaining_parse_seconds,
                    deadline - perf_counter(),
                )
            if remaining_parse_seconds <= 0.001:
                raise InterpretationTimeout(perf_counter() - validation_started)
            try:
                value_interpretation = query_with_interpretations(
                    parsed_value.expression,
                    [],
                    compute=False,
                    time_limit_seconds=remaining_parse_seconds,
                    parse_timeout_seconds=remaining_parse_seconds,
                    deadline=deadline,
                )
            except InterpretationTimeout:
                raise
            except InterpretationError as exc:
                raise AssumptionSelectionError(
                    f"FormulaVariable value for {variable} has invalid physical units: {exc}"
                ) from exc
            if value_interpretation.get("outcome") != "validated":
                raise AssumptionSelectionError(
                    f"FormulaVariable value for {variable} could not be validated"
                )
        normalized_values[variable] = parsed_value.expression
        submitted_values[variable] = value

    class _SubstituteVariables(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.expr:
            replacement = replacement_nodes.get(node.id)
            return copy.deepcopy(replacement) if replacement is not None else node

    try:
        tree = ast.parse(parsed_expression.expression, mode="eval")
    except SyntaxError as exc:
        raise InterpretationError("input could not be parsed as a mathematical expression") from exc
    substituted = _SubstituteVariables().visit(tree)
    ast.fix_missing_locations(substituted)
    return ast.unparse(substituted.body), variables, normalized_values, submitted_values


def _split_operation_variable_selection(
    selections: list[str],
) -> tuple[list[str], str | None]:
    remaining: list[str] = []
    selected_variable: str | None = None
    for selection in selections:
        if not selection.startswith("operation:variable:"):
            remaining.append(selection)
            continue
        match = re.fullmatch(r"operation:variable:([A-Za-z][A-Za-z0-9_]{0,31})", selection)
        if match is None:
            raise AssumptionSelectionError(
                "OperationVariable selections must use the form operation:variable:x"
            )
        if selected_variable is not None:
            raise AssumptionSelectionError(
                "select only one OperationVariable alternative per natural-language operation"
            )
        selected_variable = match.group(1)
    return remaining, selected_variable


def _operation_variable_assumption(
    variables: tuple[str, ...],
    operation: str,
    *,
    selected_variable: str | None = None,
) -> dict[str, Any]:
    registry = assumption_registry()
    scheme = next(
        (
            item
            for item in registry.get("dynamic_assumptions", [])
            if item.get("type") == "OperationVariable"
        ),
        None,
    )
    if scheme is None:
        raise RuntimeError("OperationVariable dynamic assumption scheme is not registered")
    if selected_variable is not None and selected_variable not in variables:
        available = ", ".join(f"operation:variable:{item}" for item in variables)
        raise AssumptionSelectionError(
            "OperationVariable selection is not among the candidates for this query"
            + (f"; available candidates: {available}" if available else "")
        )
    selected_id = (
        f"operation:variable:{selected_variable}" if selected_variable is not None else None
    )
    return {
        "type": "OperationVariable",
        "description": f"Choose the variable for the natural-language {operation} request.",
        "selected": selected_id,
        "selection_source": "caller_override" if selected_id else "needs_clarification",
        "values": [
            {
                "input": f"operation:variable:{variable}",
                "desc": f"Treat {variable} as the {operation} variable.",
                "variable": variable,
            }
            for variable in variables
        ],
        "registry_version": registry["registry_version"],
        "entry_version": scheme["entry_version"],
        "candidate_validation": "matched_against_expression_symbols_before_solver_routing",
    }


def _operation_variable_ambiguity(
    exc: NaturalLanguageClarification,
) -> InterpretationValidationError:
    assert exc.operation is not None and exc.expression is not None
    assumption = _operation_variable_assumption(exc.candidate_variables, exc.operation)
    return InterpretationValidationError(
        str(exc),
        assumptions=[assumption],
        interpretation={
            "kind": "ambiguous_natural_language_operation",
            "operation": exc.operation,
            "expression": exc.expression,
            "selection_required": True,
        },
        classification={
            "name": "operation_variable_candidates",
            "method": "deterministic free-variable extraction",
            "candidate_count": len(exc.candidate_variables),
            "model_ranked": False,
            "ranks_candidates_only": True,
            "selection_authority": False,
        },
    )


def _provider_request(
    input_text: str,
    location: str | None,
    latlong: str | None,
    units: str | None,
    series_offset: int = 0,
) -> KnowledgeRequest | None:
    unit_system = units.strip().lower() if units is not None else None
    if unit_system is not None and unit_system not in {"metric", "nonmetric"}:
        raise FullResultsRequestError("units must be either 'metric' or 'nonmetric'")
    forecast = re.fullmatch(
        r"\s*(?:(?:what\s+is|what's)\s+)?(?:the\s+)?"
        r"(?:(?:7[-\s]?day)\s+)?(?:weather\s+)?forecast"
        r"(?:\s+(?:in|at|for)\s+(.+?))?\s*[?.!]*\s*",
        input_text,
        re.IGNORECASE,
    )
    weather = re.fullmatch(
        r"\s*(?:(?:what\s+is|what's)\s+)?(?:the\s+)?(?:current\s+)?weather"
        r"(?:\s+(?:in|at|for)\s+(.+?))?\s*[?.!]*\s*",
        input_text,
        re.IGNORECASE,
    )
    weather_match = forecast or weather
    if weather_match:
        embedded_location = (weather_match.group(1) or "").strip()
        operation = "weather_forecast" if forecast else "weather"
        if embedded_location and (location or latlong):
            raise FullResultsRequestError(
                "specify weather location in either input, location, or latlong, not more than one"
            )
        if location and latlong:
            raise FullResultsRequestError("location and latlong cannot be combined")
        if embedded_location:
            return _knowledge_request(
                provider="open_meteo", operation=operation, location=embedded_location,
                units=unit_system,
            )
        if location:
            return _knowledge_request(
                provider="open_meteo", operation=operation, location=location.strip(),
                units=unit_system,
            )
        if latlong:
            pieces = latlong.split(",")
            if len(pieces) != 2:
                raise FullResultsRequestError("latlong must be a latitude,longitude pair")
            try:
                latitude, longitude = (float(piece.strip()) for piece in pieces)
            except ValueError as exc:
                raise FullResultsRequestError("latlong must contain numeric coordinates") from exc
            return _knowledge_request(
                provider="open_meteo",
                operation=operation,
                latitude=latitude,
                longitude=longitude,
                units=unit_system,
            )
        raise ProviderContextNeeded(
            "weather needs an explicit location; use 'weather in <place>' or "
            "'weather forecast in <place>', location, or latlong"
        )

    if location is not None or latlong is not None or unit_system is not None:
        raise FullResultsRequestError(
            "location, latlong, and units are currently supported for weather queries"
        )

    capital = re.fullmatch(
        r"\s*(?:(?:what\s+is|what's|tell\s+me)\s+)?(?:the\s+)?"
        r"capital(?:\s+city)?\s+(?:(?:of|in)\s+)?(.+?)\s*[?.!]*\s*",
        input_text,
        re.IGNORECASE,
    )
    capital_possessive = re.fullmatch(
        r"\s*(?:(?:what\s+is|what's)\s+)?(.+?)(?:'s|’s)\s+capital\s*[?.!]*\s*",
        input_text,
        re.IGNORECASE,
    )
    if capital or capital_possessive:
        query = (capital or capital_possessive).group(1).strip()
        if query.casefold() in {"of", "in"}:
            raise ProviderContextNeeded("capital lookup needs an entity name")
        return _knowledge_request(
            provider="wikidata", operation="property", property_id="P36",
            query=query, max_results=3,
        )

    country_of = re.fullmatch(
        r"\s*(?:(?:what\s+is|what's|which\s+is|name)\s+)?(?:the\s+)?"
        r"country\s+(?:of|for)\s+(.+?)\s*[?.!]*\s*",
        input_text,
        re.IGNORECASE,
    )
    country_in = re.fullmatch(
        r"\s*(?:(?:what|which)\s+)?country\s+is\s+(.+?)\s+in\s*[?.!]*\s*",
        input_text,
        re.IGNORECASE,
    )
    if country_of or country_in:
        query = (country_of or country_in).group(1).strip()
        if not query:
            raise ProviderContextNeeded("country lookup needs an entity name")
        return _knowledge_request(
            provider="wikidata", operation="property", property_id="P17",
            query=query, max_results=3,
        )

    continent_of = re.fullmatch(
        r"\s*(?:(?:what\s+is|what's|which\s+is|name)\s+)?(?:the\s+)?"
        r"continent\s+(?:of|for)\s+(.+?)\s*[?.!]*\s*",
        input_text,
        re.IGNORECASE,
    )
    continent_in = re.fullmatch(
        r"\s*(?:(?:what|which)\s+)?continent\s+is\s+(.+?)\s+in\s*[?.!]*\s*",
        input_text,
        re.IGNORECASE,
    )
    if continent_of or continent_in:
        query = (continent_of or continent_in).group(1).strip()
        if not query:
            raise ProviderContextNeeded("continent lookup needs an entity name")
        return _knowledge_request(
            provider="wikidata", operation="property", property_id="P30",
            query=query, max_results=3,
        )

    population = re.fullmatch(
        r"\s*(?:(?:what\s+is|what's|give\s+me)\s+)?(?:the\s+)?"
        r"population\s+(?:(?:of|in)\s+)?(.+?)\s*[?.!]*\s*",
        input_text,
        re.IGNORECASE,
    )
    population_suffix = re.fullmatch(
        r"\s*(.+?)\s+population\s*[?.!]*\s*",
        input_text,
        re.IGNORECASE,
    )
    population = population or population_suffix
    if population:
        query = population.group(1).strip()
        if query.casefold() in {"of", "in"}:
            raise ProviderContextNeeded("population lookup needs an entity name")
        return _knowledge_request(
            provider="wikidata", operation="property", property_id="P1082",
            query=query, max_results=3,
        )

    elevation = re.fullmatch(
        r"\s*(?:(?:what\s+is|what's|give\s+me|tell\s+me)\s+)?(?:the\s+)?"
        r"(?:elevation|altitude)(?:\s+above\s+sea\s+level)?\s+(?:of|in|for)\s+(.+?)\s*[?.!]*\s*",
        input_text,
        re.IGNORECASE,
    )
    if elevation:
        query = elevation.group(1).strip()
        if not query:
            raise ProviderContextNeeded("elevation lookup needs a place or entity name")
        return _knowledge_request(
            provider="wikidata", operation="property", property_id="P2044",
            query=query, max_results=3,
        )

    area = re.fullmatch(
        r"\s*(?:(?:what\s+is|what's|give\s+me|tell\s+me)\s+)?(?:the\s+)?"
        r"(?:(?:land|geographic(?:al)?)\s+)?area\s+(?:of|in|for)\s+(.+?)\s*[?.!]*\s*",
        input_text,
        re.IGNORECASE,
    )
    if area:
        query = area.group(1).strip()
        if not query:
            raise ProviderContextNeeded("area lookup needs a place or entity name")
        return _knowledge_request(
            provider="wikidata", operation="property", property_id="P2046",
            query=query, max_results=3,
        )

    coordinates = re.fullmatch(
        r"\s*(?:(?:what\s+are|what\s+is|give\s+me|tell\s+me)\s+)?(?:the\s+)?"
        r"(?:GPS\s+|geographic(?:al)?\s+)?(?:coordinates|coordinate\s+location)\s+"
        r"(?:of|for|in)\s+(.+?)\s*[?.!]*\s*",
        input_text,
        re.IGNORECASE,
    )
    if coordinates:
        query = coordinates.group(1).strip()
        if not query:
            raise ProviderContextNeeded("coordinate lookup needs a place or entity name")
        return _knowledge_request(
            provider="wikidata", operation="property", property_id="P625",
            query=query, max_results=3,
        )

    official_languages = re.fullmatch(
        r"\s*(?:(?:what\s+is|what\s+are|what's|name)\s+)?(?:the\s+)?"
        r"official\s+languages?\s+(?:(?:of|in)\s+)?(.+?)\s*[?.!]*\s*",
        input_text,
        re.IGNORECASE,
    )
    if official_languages:
        query = official_languages.group(1).strip()
        if query.casefold() in {"of", "in"}:
            raise ProviderContextNeeded("official-language lookup needs an entity name")
        return _knowledge_request(
            provider="wikidata", operation="property", property_id="P37",
            query=query, max_results=3,
        )

    currency = re.fullmatch(
        r"\s*(?:(?:what\s+is|what's|which\s+is|name)\s+)?(?:the\s+)?"
        r"currency\s+(?:(?:of|in)\s+|used\s+in\s+)?(.+?)\s*[?.!]*\s*",
        input_text,
        re.IGNORECASE,
    )
    if currency:
        query = currency.group(1).strip()
        if query.casefold() in {"of", "in", "used in"}:
            raise ProviderContextNeeded("currency lookup needs an entity name")
        return _knowledge_request(
            provider="wikidata", operation="property", property_id="P38",
            query=query, max_results=3,
        )

    entity = re.fullmatch(r"\s*(?:entity|wikidata)\s*:\s*(.+?)\s*[?.!]*\s*", input_text, re.I)
    if entity:
        query = entity.group(1).strip()
        if not query:
            raise FullResultsRequestError("entity search needs a non-empty query")
        return _knowledge_request(provider="wikidata", operation="search", query=query)

    place = re.fullmatch(r"\s*(?:place|geonames)\s*:\s*(.+?)\s*[?.!]*\s*", input_text, re.I)
    if place:
        query = place.group(1).strip()
        if not query:
            raise FullResultsRequestError("place search needs a non-empty query")
        return _knowledge_request(provider="geonames", operation="search", query=query)

    series_search = re.fullmatch(
        r"\s*(?:series|dbnomics)\s+search\s*:\s*(.+?)\s+(?:within|in)\s+"
        r"([^/\s]+)/([^/\s]+)\s*[?.!]*\s*",
        input_text,
        re.I,
    )
    if series_search:
        query = series_search.group(1).strip()
        if not query:
            raise FullResultsRequestError("DBnomics series search needs a non-empty query")
        return _knowledge_request(
            provider="dbnomics",
            operation="series_search",
            query=query,
            provider_code=series_search.group(2),
            dataset_code=series_search.group(3),
            max_results=5,
            offset=series_offset,
        )

    series_search = re.fullmatch(
        r"\s*(?:series|dbnomics)\s+search\s*:\s*(.+?)\s*[?.!]*\s*",
        input_text,
        re.I,
    )
    if series_search:
        query = series_search.group(1).strip()
        if not query:
            raise FullResultsRequestError("DBnomics dataset search needs a non-empty query")
        return _knowledge_request(
            provider="dbnomics",
            operation="search",
            query=query,
            max_results=5,
        )
    if re.match(r"\s*(?:series|dbnomics)\s+search\s*:", input_text, re.I):
        raise ProviderContextNeeded("DBnomics dataset search needs a search phrase")

    series = re.fullmatch(
        r"\s*(?:series|dbnomics)\s*:\s*([^/\s]+)/([^/\s]+)/([^/\s]+)\s*[?.!]*\s*",
        input_text,
        re.I,
    )
    if series:
        return _knowledge_request(
            provider="dbnomics",
            operation="series",
            provider_code=series.group(1),
            dataset_code=series.group(2),
            series_code=series.group(3).rstrip("?!"),
        )
    if re.match(r"\s*(?:series|dbnomics)\s*:", input_text, re.I):
        raise ProviderContextNeeded(
            "DBnomics series syntax is series: provider/dataset/series"
        )

    explicit_constant = re.fullmatch(r"\s*(?:constant|codata)\s*:\s*(.+?)\s*[?.!]*\s*", input_text, re.I)
    if explicit_constant:
        constant = re.sub(r"[^a-z0-9]+", "_", explicit_constant.group(1).lower()).strip("_")
        if constant not in supported_nist_constants():
            raise ProviderContextNeeded(
                "that constant is not in the bundled CODATA 2022 subset"
            )
        return _knowledge_request(
            provider="nist_codata", operation="constant", constant=constant
        )

    phrase = re.sub(r"^\s*(?:(?:what\s+is|what's|value\s+of|the)\s+)+", "", input_text, flags=re.I)
    phrase = re.sub(r"[?.!]+\s*$", "", phrase).strip().lower()
    phrase = re.sub(r"[-_]+", " ", phrase)
    phrase = re.sub(r"\s+", " ", phrase)
    aliases = {
        "speed of light": "speed_of_light",
        "speed of light in vacuum": "speed_of_light",
        "planck constant": "planck_constant",
        "planck's constant": "planck_constant",
        "elementary charge": "elementary_charge",
        "boltzmann constant": "boltzmann_constant",
        "avogadro constant": "avogadro_constant",
        "avogadro's constant": "avogadro_constant",
        "newtonian constant of gravitation": "newtonian_constant_of_gravitation",
        "gravitational constant": "newtonian_constant_of_gravitation",
        "fine structure constant": "fine_structure_constant",
        "electron mass": "electron_mass",
        "proton mass": "proton_mass",
    }
    constant = aliases.get(phrase)
    if constant is not None:
        return _knowledge_request(provider="nist_codata", operation="constant", constant=constant)
    return None


def _knowledge_request(**values: Any) -> KnowledgeRequest:
    try:
        return KnowledgeRequest.model_validate(values)
    except ValidationError as exc:
        messages = "; ".join(error["msg"] for error in exc.errors())
        raise FullResultsRequestError(f"invalid data query: {messages}") from exc


def _gdp_query_parts(input_text: str) -> tuple[str, str] | None:
    query = re.sub(
        r"^\s*(?:(?:what\s+is|what's|give\s+me|tell\s+me|estimate|find|show)\s+)?(?:the\s+)?",
        "",
        input_text,
        count=1,
        flags=re.IGNORECASE,
    )
    query = re.sub(r"[?.!]+\s*$", "", query).strip()
    patterns = (
        (
            "ppp_per_capita",
            r"(?:gdp|gross\s+domestic\s+product)\s*(?:\(\s*ppp\s*\)|at\s+purchasing\s+power\s+parity)\s+per\s+capita\s+(?:of|in)\s+(.+?)\s*",
        ),
        (
            "ppp_per_capita",
            r"ppp\s+(?:gdp|gross\s+domestic\s+product)\s+per\s+capita\s+(?:of|in)\s+(.+?)\s*",
        ),
        (
            "ppp_per_capita",
            r"(?:ppp\s+(?:gdp|gross\s+domestic\s+product)|(?:gdp|gross\s+domestic\s+product)\s+per\s+capita\s*(?:\(\s*ppp\s*\)|at\s+purchasing\s+power\s+parity))\s+(?:of|in)\s+(.+?)\s*",
        ),
        (
            "nominal_per_capita",
            r"nominal\s+(?:gdp|gross\s+domestic\s+product)\s+per\s+capita\s+(?:of|in)\s+(.+?)\s*",
        ),
        (
            "nominal_per_capita",
            r"(?:gdp|gross\s+domestic\s+product)\s*\(\s*nominal\s*\)\s+per\s+capita\s+(?:of|in)\s+(.+?)\s*",
        ),
        (
            "nominal_per_capita",
            r"(?:gdp|gross\s+domestic\s+product)\s+per\s+capita\s*\(\s*nominal\s*\)\s+(?:of|in)\s+(.+?)\s*",
        ),
        (
            "generic_per_capita",
            r"(?:gdp|gross\s+domestic\s+product)\s+per\s+capita\s+(?:of|in)\s+(.+?)\s*",
        ),
        (
            "generic_per_capita",
            r"(?:gdp|gross\s+domestic\s+product)\s+(?:of|in)\s+(.+?)\s+per\s+capita\s*",
        ),
        ("ppp", r"(?:gdp|gross\s+domestic\s+product)\s*(?:\(\s*ppp\s*\)|at\s+purchasing\s+power\s+parity)\s+(?:of|in)\s+(.+?)\s*"),
        ("ppp", r"ppp\s+(?:gdp|gross\s+domestic\s+product)\s+(?:of|in)\s+(.+?)\s*"),
        ("nominal", r"nominal\s+(?:gdp|gross\s+domestic\s+product)\s+(?:of|in)\s+(.+?)\s*"),
        ("nominal", r"(?:gdp|gross\s+domestic\s+product)\s*\(\s*nominal\s*\)\s+(?:of|in)\s+(.+?)\s*"),
        ("generic", r"(?:gdp|gross\s+domestic\s+product)\s+(?:of|in)\s+(.+?)\s*"),
    )
    for kind, pattern in patterns:
        match = re.fullmatch(pattern, query, re.IGNORECASE)
        if match:
            return kind, match.group(1).strip()
    if re.fullmatch(
        r"(?:gdp|gross\s+domestic\s+product)(?:\s+per\s+capita)?", query, re.IGNORECASE
    ):
        raise ProviderContextNeeded("GDP lookup needs an entity name")
    if re.search(r"\b(?:gdp|gross\s+domestic\s+product)\b.*\bper\s+capita\b", query, re.IGNORECASE):
        raise ProviderContextNeeded(
            "GDP per capita lookup needs a supported form such as 'GDP per capita of <entity>'"
        )
    return None


def _resolve_gdp_request(
    input_text: str, selections: list[str]
) -> tuple[KnowledgeRequest, dict[str, Any]] | None:
    parts = _gdp_query_parts(input_text)
    if parts is None:
        return None
    kind, entity_query = parts
    per_capita = kind.endswith("_per_capita")
    measure_kind = kind.removesuffix("_per_capita")
    assumption_type = "GDPPerCapitaType" if per_capita else "GDPType"
    if not entity_query or entity_query.casefold() in {"of", "in"}:
        raise ProviderContextNeeded("GDP lookup needs an entity name")

    registry = assumption_registry()
    candidates = [
        item for item in registry["assumptions"] if item.get("type") == assumption_type
    ]
    candidates_by_id = {item["id"]: item for item in candidates}
    if len(candidates_by_id) != 2:
        raise RuntimeError(f"{assumption_type} registry must contain nominal and PPP choices")
    if len(selections) > 1:
        raise AssumptionSelectionError(f"select one {assumption_type} assumption")
    selected = None
    if selections:
        selected = candidates_by_id.get(selections[0])
        if selected is None:
            raise AssumptionSelectionError(
                f"{assumption_type} queries accept one of: "
                + ", ".join(sorted(candidates_by_id))
            )
    id_prefix = "gdp-per-capita:type:" if per_capita else "gdp:type:"
    explicit_id = {
        "nominal": f"{id_prefix}nominal",
        "ppp": f"{id_prefix}ppp",
    }.get(measure_kind)
    if selected is not None and explicit_id is not None and selected["id"] != explicit_id:
        raise AssumptionSelectionError(
            f"the {assumption_type} assumption conflicts with the measure named in the input"
        )

    chosen = selected
    if chosen is None and explicit_id is not None:
        chosen = candidates_by_id[explicit_id]
    assumption = {
        "type": assumption_type,
        "selected": chosen["id"] if chosen is not None else None,
        "selection_source": (
            "caller_override"
            if selected is not None
            else "explicit_input"
            if explicit_id is not None
            else "needs_clarification"
        ),
        "registry_version": registry["registry_version"],
        "values": [
            {
                "input": candidate["id"],
                "desc": candidate["description"],
                "entry_version": candidate["entry_version"],
            }
            for candidate in candidates
        ],
    }
    if chosen is None:
        if per_capita:
            prompt = (
                "GDP per capita can mean nominal GDP per capita or PPP GDP per capita; "
                "select one with assumption=gdp-per-capita:type:nominal or "
                "assumption=gdp-per-capita:type:ppp"
            )
        else:
            prompt = (
                "GDP can mean nominal GDP or GDP at purchasing power parity; select one "
                "with assumption=gdp:type:nominal or assumption=gdp:type:ppp"
            )
        raise ProviderAssumptionNeeded(
            prompt, assumption,
        )
    request = _knowledge_request(
        provider="wikidata",
        operation="property",
        property_id=chosen["wikidata_property_id"],
        query=entity_query,
        max_results=3,
    )
    return request, assumption


def _split_entity_assumption_selections(
    selections: list[str],
) -> tuple[list[str], str | None]:
    """Separate a Wikidata entity selector from static assumption IDs."""
    remaining: list[str] = []
    entity_selection: str | None = None
    for selection in selections:
        if not selection.startswith("entity:"):
            remaining.append(selection)
            continue
        if re.fullmatch(r"entity:wikidata:Q[1-9][0-9]*", selection) is None:
            raise AssumptionSelectionError(
                "entity selection must use the form entity:wikidata:Q123"
            )
        if entity_selection is not None:
            raise AssumptionSelectionError("select only one Wikidata entity")
        entity_selection = selection
    return remaining, entity_selection


def _supports_wikidata_entity_selection(request: KnowledgeRequest | None) -> bool:
    return request is not None and _wikidata_property_id(request) is not None


def _wikidata_entity_assumption(
    matches: list[dict[str, Any]],
    *,
    selected_id: str | None = None,
    candidates_available: bool = True,
) -> dict[str, Any]:
    registry = assumption_registry()
    dynamic_spec = next(
        item for item in registry.get("dynamic_assumptions", [])
        if item.get("type") == "WikidataEntity"
    )
    entry_version = dynamic_spec["entry_version"]
    values: list[dict[str, Any]] = []
    for match in matches:
        entity_id = match.get("id")
        if not isinstance(entity_id, str) or re.fullmatch(r"Q[1-9][0-9]*", entity_id) is None:
            continue
        label = match.get("label") if isinstance(match.get("label"), str) else entity_id
        description = match.get("description")
        desc = f"{label} [{entity_id}]"
        if isinstance(description, str) and description.strip():
            desc += f": {description.strip()}"
        values.append({
            "input": f"entity:wikidata:{entity_id}",
            "desc": desc,
            "entity_id": entity_id,
            "entry_version": entry_version,
        })
    assumption: dict[str, Any] = {
        "type": "WikidataEntity",
        "selected": f"entity:wikidata:{selected_id}" if selected_id else None,
        "selection_source": (
            "caller_override" if selected_id else
            "needs_clarification" if values else
            "candidate_generation_pending" if not candidates_available else
            "no_candidates"
        ),
        "registry_version": registry["registry_version"],
        "entry_version": entry_version,
        "values": values,
    }
    if not candidates_available:
        assumption["candidate_validation"] = "deferred_to_query_execution"
    return assumption


def _execute_provider_query(
    input_text: str,
    request: KnowledgeRequest,
    formats: list[str],
    started: float,
    parse_timing: float,
    *,
    include_pod_ids: list[str] | None,
    exclude_pod_ids: list[str] | None,
    pod_titles: list[str] | None,
    pod_indexes: list[str] | None,
    scanners: list[str] | None,
    deadline: float,
    scan_timeout_seconds: float,
    format_timeout_seconds: float,
    pod_timeout_seconds: float,
    text_width: int,
    plot_width: int,
    assumptions: list[dict[str, Any]],
    entity_selection: str | None,
) -> dict[str, Any]:
    provider_started = perf_counter()
    provider_deadline = min(
        deadline,
        provider_started + scan_timeout_seconds,
    )
    try:
        knowledge = query_knowledge(request, deadline=provider_deadline)
    except KnowledgeDeadlineExceeded:
        provider_titles = {
            "open_meteo": (
                "Weather forecast"
                if request.operation == "weather_forecast"
                else "Current weather"
            ),
            "nist_codata": "Physical constant",
            "wikidata": _wikidata_async_descriptor(request)[0],
            "dbnomics": (
                "Economic datasets"
                if request.operation == "search"
                else (
                    "Economic series matches"
                    if request.operation == "series_search"
                    else "Economic time series"
                )
            ),
            "geonames": "Place matches",
        }
        format_budget = _FormatStageBudget(
            format_timeout_seconds=format_timeout_seconds,
            pod_timeout_seconds=pod_timeout_seconds,
            total_deadline=deadline,
        )

        def format_timeout_input() -> dict[str, Any]:
            input_image = (
                _svg_text_image(input_text, width=text_width) if "image" in formats else None
            )
            return _input_pod(
                input_text,
                formats,
                input_mathml=_mathml(input_text),
                input_image=input_image,
            )

        input_selected = bool(_select_pods(
            [{"title": "Input interpretation", "scanner": "Identity", "id": "Input", "position": 100}],
            include_pod_ids,
            exclude_pod_ids,
            pod_titles,
            pod_indexes,
            scanners,
        ))
        timeout_input_pod = (
            format_budget.render("Input interpretation", format_timeout_input)
            if input_selected
            else None
        )
        pods = [timeout_input_pod] if timeout_input_pod is not None else []
        if deadline - perf_counter() <= 0.001:
            timeout_stage = "total"
        else:
            timeout_stage = "scan"
        timeout_message = {
            "scan": "The data scan exceeded its scan time limit.",
            "total": "The data retrieval exceeded the total query time limit.",
        }[timeout_stage]
        timeout_warnings = _format_warnings(
            formats,
            _SUPPORTED_FORMATS | ({"image"} if "image" in formats else set()),
        )
        timeout_warnings.append({"text": timeout_message})
        if format_budget.timed_out_pods:
            timeout_warnings.append(_format_timeout_warning(format_budget.timed_out_pods))
        payload = _query_envelope(
            success=False,
            error=False,
            pods=pods,
            elapsed=perf_counter() - started,
            parse_timing=parse_timing,
            datatypes="Data",
            warnings=timeout_warnings,
            assumptions=assumptions,
            timedout="Data",
            timedoutpods=",".join([
                provider_titles[request.provider], *format_budget.timed_out_pods
            ]),
            evidence={
                "kind": "provider_timeout", "provider": request.provider,
                "timeout_stage": timeout_stage, "verified": False,
            },
            qualification=(
                timeout_message
            ),
        )
        payload["queryresult"]["input"] = input_text
        return filter_response_pods(
            payload, include_pod_ids, exclude_pod_ids, pod_titles, pod_indexes, scanners
        )
    except KnowledgeProviderError as exc:
        raise FullResultsBackendError(
            str(exc), status_code=exc.status_code, error_code="PROVIDER_FAILURE"
        ) from exc

    wikidata_entity_assumption: dict[str, Any] | None = None
    if _supports_wikidata_entity_selection(request):
        data = knowledge.get("data")
        matches = data.get("matches", []) if isinstance(data, dict) else []
        if not isinstance(matches, list):
            matches = []
        candidate_ids = {
            match.get("id")
            for match in matches
            if isinstance(match, dict) and isinstance(match.get("id"), str)
        }
        selected_qid = entity_selection.rsplit(":", 1)[-1] if entity_selection else None
        if selected_qid is not None and selected_qid not in candidate_ids:
            choices = ", ".join(
                f"entity:wikidata:{entity_id}" for entity_id in sorted(candidate_ids)
            )
            detail = f" Current candidates: {choices}." if choices else " No candidates were found."
            raise AssumptionSelectionError(
                "entity:wikidata selection must identify a candidate returned for this query."
                + detail
            )
        wikidata_entity_assumption = _wikidata_entity_assumption(
            matches,
            selected_id=selected_qid,
            candidates_available=True,
        )
        if selected_qid is not None and isinstance(data, dict):
            data["matches"] = [
                match for match in matches
                if isinstance(match, dict) and match.get("id") == selected_qid
            ]
            data["selection_rule"] = (
                f"Caller selected {entity_selection} from the current Wikidata candidates; "
                "only that entity's statements are returned."
            )
        if wikidata_entity_assumption["values"]:
            assumptions.append(wikidata_entity_assumption)

    provider_presentation = {
        "open_meteo": (
            ("Weather forecast", "WeatherForecast", "Weather")
            if request.operation == "weather_forecast"
            else ("Current weather", "Weather", "Weather")
        ),
        "nist_codata": ("Physical constant", "PhysicalConstant", "ScientificConstant"),
        "wikidata": _wikidata_provider_presentation(request),
        "dbnomics": (
            ("Economic datasets", "EconomicDatasetSearch", "Economics")
            if request.operation == "search"
            else (
                ("Economic series matches", "EconomicSeriesSearch", "Economics")
                if request.operation == "series_search"
                else ("Economic time series", "EconomicSeries", "Economics")
            )
        ),
        "geonames": ("Place matches", "PlaceSearch", "Geography"),
    }
    title, pod_id, datatype = provider_presentation[request.provider]
    selected_formats = formats or ["plaintext"]
    selected_pod_ids = {
        pod["id"]
        for pod in _select_pods(
            [
                {"title": "Input interpretation", "scanner": "Identity", "id": "Input", "position": 100},
                {"title": title, "scanner": "Data", "id": pod_id, "position": 200},
            ],
            include_pod_ids,
            exclude_pod_ids,
            pod_titles,
            pod_indexes,
            scanners,
        )
    }
    format_budget = _FormatStageBudget(
        format_timeout_seconds=format_timeout_seconds,
        pod_timeout_seconds=pod_timeout_seconds,
        total_deadline=deadline,
    )

    def format_provider_input() -> dict[str, Any]:
        input_image = (
            _svg_text_image(input_text, width=text_width) if "image" in selected_formats else None
        )
        return _input_pod(
            input_text,
            selected_formats,
            input_mathml=_mathml(input_text),
            input_image=input_image,
        )

    def format_provider_result() -> dict[str, Any]:
        plaintext = (
            forecast_plaintext(knowledge["data"])
            if request.provider == "open_meteo" and request.operation == "weather_forecast"
            else None
        ) or _provider_plaintext(request.provider, knowledge["data"])
        result_image = None
        if "image" in selected_formats:
            if request.provider == "open_meteo" and request.operation == "weather_forecast":
                result_image = forecast_chart_image(knowledge["data"], width=plot_width)
            if result_image is None:
                result_image = _svg_text_image(plaintext, width=text_width)
        result_subpod: dict[str, Any] = {"title": "", "data": knowledge["data"]}
        if "plaintext" in selected_formats:
            result_subpod["plaintext"] = plaintext
        if "mathml" in selected_formats:
            result_subpod["mathml"] = _mathml(plaintext)
        if "cell" in selected_formats:
            result_subpod["cell"] = _wolfram_cell(plaintext)
        if result_image is not None:
            result_subpod["img"] = result_image
        return {
            "title": title,
            "scanner": "Data",
            "id": pod_id,
            "position": 200,
            "error": False,
            "numsubpods": 1,
            "primary": True,
            "subpods": [result_subpod],
        }

    pods = []
    if "Input" in selected_pod_ids:
        provider_input = format_budget.render("Input interpretation", format_provider_input)
        if provider_input is not None:
            pods.append(provider_input)
    if pod_id in selected_pod_ids:
        provider_result = format_budget.render(title, format_provider_result)
        if provider_result is not None:
            pods.append(provider_result)
    filtered = _select_pods(
        pods, include_pod_ids, exclude_pod_ids, pod_titles, pod_indexes, scanners
    )
    elapsed = perf_counter() - started
    warnings = _format_warnings(
        formats,
        {"plaintext", "mathml", "cell"}
        | ({"image"} if "image" in selected_formats else set()),
    )
    if format_budget.timed_out_pods:
        warnings.append(_format_timeout_warning(format_budget.timed_out_pods))
    wikidata_property_id = _wikidata_property_id(request)
    wikidata_property_name = {
        "P17": "country",
        "P30": "continent",
        "P1082": "population",
        "P36": "capital",
        "P37": "official-language",
        "P38": "currency",
        "P2044": "elevation above sea level",
        "P2046": "area",
        "P625": "coordinates",
        "P2131": "nominal GDP",
        "P4010": "GDP at purchasing-power parity",
        "P2132": "nominal GDP per capita",
        "P2299": "GDP per capita at purchasing-power parity",
    }.get(wikidata_property_id)
    wikidata_property_note = {
        "P17": "Country P17 is intended for geographic places, not people.",
        "P30": "Wikidata may list multiple continents when a subject spans continents.",
        "P2044": "Elevation measurements can vary by source and unit; inspect the reported unit and statement qualifiers.",
        "P625": "Coordinate statements include the reported globe and precision; check both when interpreting the values.",
    }.get(wikidata_property_id)
    payload = _query_envelope(
        success=not (format_budget.timed_out_pods and not filtered),
        error=False,
        pods=filtered,
        elapsed=elapsed,
        parse_timing=parse_timing,
        datatypes=datatype,
        warnings=warnings,
        assumptions=assumptions,
        interpretation={
            "kind": "typed_data_request",
            "provider": knowledge["provider"],
            "operation": knowledge["operation"],
            "request": request.model_dump(exclude_none=True),
        },
        classification={
            "name": "deterministic_provider_router",
            "method": (
                "DBnomics full-text dataset search; results are ranked candidates"
                if request.provider == "dbnomics" and request.operation == "search"
                else (
                    "DBnomics full-text series search within the selected dataset; results are ranked candidates"
                    if request.provider == "dbnomics" and request.operation == "series_search"
                    else (
                        "Wikidata label search; ranked entity candidates are retained"
                        if request.provider == "wikidata" and _wikidata_property_id(request) is not None
                        else "explicit query pattern or supported constant alias"
                    )
                )
            ),
            "ranks_candidates_only": True,
            "selection_authority": False,
        },
        evidence=knowledge["evidence"],
        qualification=(
            "Wikidata's ranked entity matches are returned as candidates. "
            f"{wikidata_property_name.capitalize()} statements are retrieved data, not a formal proof."
            + (f" {wikidata_property_note}" if wikidata_property_note else "")
            if wikidata_property_name is not None
            else (
                "DBnomics search results are dataset candidates. Select a provider and dataset explicitly, then request a specific series; no dataset is selected automatically. Retrieved data is not a formal proof."
                if request.provider == "dbnomics" and request.operation == "search"
                else (
                    "DBnomics series search results are candidates within the selected provider and dataset. Select a series code explicitly before requesting observations; no series is selected automatically. Retrieved data is not a formal proof."
                    if request.provider == "dbnomics" and request.operation == "series_search"
                    else knowledge["qualification"]
                )
            )
        ),
        sources=knowledge["sources"],
        retrieved_at=knowledge["retrieved_at"],
        data=None if title in format_budget.timed_out_pods else knowledge["data"],
        timedoutpods=",".join(format_budget.timed_out_pods),
    )
    payload["queryresult"]["input"] = input_text
    return payload


def _provider_plaintext(provider: str, data: Any) -> str:
    if provider == "nist_codata" and isinstance(data, dict):
        name = data.get("name", "Physical constant")
        symbol = data.get("symbol")
        value = data.get("value", "")
        unit = data.get("unit", "")
        label = f"{name} ({symbol})" if symbol else str(name)
        result = f"{label} = {value} {unit}".strip()
        uncertainty = data.get("standard_uncertainty")
        if uncertainty:
            result += f"; standard uncertainty: {uncertainty}"
        if data.get("release"):
            result += f"; release: {data['release']}"
        return result
    if provider == "wikidata" and isinstance(data, list):
        rows = []
        for entity in data:
            if not isinstance(entity, dict):
                continue
            identifier = entity.get("id", "")
            label = entity.get("label", identifier or "Entity")
            description = entity.get("description")
            rows.append(f"{label} [{identifier}]: {description}" if description else f"{label} [{identifier}]")
        return "\n".join(rows) if rows else "No matching entities were found."
    if (
        provider == "wikidata"
        and isinstance(data, dict)
        and data.get("property_id") == "P625"
    ):
        matches = data.get("matches")
        rows = []
        if isinstance(matches, list):
            for match in matches:
                if not isinstance(match, dict):
                    continue
                label = str(match.get("label") or match.get("id") or "Entity")
                identifier = str(match.get("id") or "")
                prefix = f"{label} [{identifier}]" if identifier else label
                statements = []
                coordinates = match.get("coordinates")
                if isinstance(coordinates, list):
                    for value in coordinates:
                        if not isinstance(value, dict):
                            continue
                        latitude = value.get("latitude")
                        longitude = value.get("longitude")
                        if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
                            continue
                        position = f"{latitude}, {longitude}"
                        precision = value.get("precision")
                        if isinstance(precision, (int, float)):
                            position += f" (precision {precision}°)"
                        globe_id = value.get("globe_id")
                        globe = value.get("globe")
                        if isinstance(globe_id, str):
                            position += f"; globe {globe_id}"
                        elif isinstance(globe, str):
                            position += f"; globe {globe}"
                        statements.append(position)
                detail = "; ".join(statements) or "no coordinate statement found"
                rows.append(f"Coordinates: {prefix}: {detail}")
        return "\n".join(rows) if rows else "No Wikidata entity matches were found."
    wikidata_quantities = {
        "P2044": ("elevation", "Elevation above sea level"),
        "P2046": ("area", "Area"),
    }
    if (
        provider == "wikidata"
        and isinstance(data, dict)
        and data.get("property_id") in wikidata_quantities
    ):
        value_key, measure_name = wikidata_quantities[str(data["property_id"])]
        matches = data.get("matches")
        rows = []
        if isinstance(matches, list):
            for match in matches:
                if not isinstance(match, dict):
                    continue
                label = str(match.get("label") or match.get("id") or "Entity")
                identifier = str(match.get("id") or "")
                prefix = f"{label} [{identifier}]" if identifier else label
                values = match.get(value_key)
                statements = []
                if isinstance(values, list):
                    for value in values:
                        if not isinstance(value, dict):
                            continue
                        amount = _display_wikidata_amount(value.get("amount"))
                        lower = _display_wikidata_amount(value.get("lowerBound"))
                        upper = _display_wikidata_amount(value.get("upperBound"))
                        if lower is not None and upper is not None:
                            quantity = f"between {lower} and {upper}"
                        else:
                            quantity = amount or "amount not reported"
                        unit = value.get("unit_label") or value.get("unit") or value.get("unit_id")
                        if unit:
                            quantity += f" {unit}"
                        point_in_time = value.get("point_in_time")
                        if isinstance(point_in_time, str):
                            date = point_in_time.removeprefix("+").split("T", 1)[0]
                            quantity += f" (as of {date})"
                        statements.append(quantity)
                detail = "; ".join(statements) or f"no usable {measure_name.lower()} statement found"
                rows.append(f"{measure_name}: {prefix}: {detail}")
        return "\n".join(rows) if rows else "No Wikidata entity matches were found."
    if (
        provider == "wikidata"
        and isinstance(data, dict)
        and data.get("property_id") == "P1082"
    ):
        matches = data.get("matches")
        rows = []
        if isinstance(matches, list):
            for match in matches:
                if not isinstance(match, dict):
                    continue
                label = str(match.get("label") or match.get("id") or "Entity")
                identifier = str(match.get("id") or "")
                prefix = f"{label} [{identifier}]" if identifier else label
                description = match.get("description")
                if isinstance(description, str) and description:
                    prefix += f" — {description}"
                values = match.get("population")
                statements = []
                if isinstance(values, list):
                    for value in values:
                        if not isinstance(value, dict):
                            continue
                        amount = _display_wikidata_amount(value.get("amount"))
                        lower = _display_wikidata_amount(value.get("lowerBound"))
                        upper = _display_wikidata_amount(value.get("upperBound"))
                        if lower is not None and upper is not None:
                            population = f"between {lower} and {upper} people"
                        elif amount is not None:
                            population = f"{amount} people"
                        else:
                            population = "population reported"
                        point_in_time = value.get("point_in_time")
                        if isinstance(point_in_time, str):
                            date = point_in_time.removeprefix("+").split("T", 1)[0]
                            population += f" (as of {date})"
                        statements.append(population)
                detail = "; ".join(statements) or "no usable population statement found"
                rows.append(f"{prefix}: {detail}")
        return "\n".join(rows) if rows else "No Wikidata entity matches were found."
    wikidata_gdp = {
        "P2131": ("nominal_gdp", "Nominal GDP"),
        "P4010": ("gdp_ppp", "GDP at purchasing power parity"),
        "P2132": ("nominal_gdp_per_capita", "Nominal GDP per capita"),
        "P2299": ("gdp_ppp_per_capita", "GDP per capita at purchasing power parity"),
    }
    if (
        provider == "wikidata"
        and isinstance(data, dict)
        and data.get("property_id") in wikidata_gdp
    ):
        value_key, measure_name = wikidata_gdp[str(data["property_id"])]
        matches = data.get("matches")
        rows = []
        if isinstance(matches, list):
            for match in matches:
                if not isinstance(match, dict):
                    continue
                label = str(match.get("label") or match.get("id") or "Entity")
                identifier = str(match.get("id") or "")
                prefix = f"{label} [{identifier}]" if identifier else label
                values = match.get(value_key)
                statements = []
                if isinstance(values, list):
                    for value in values:
                        if not isinstance(value, dict):
                            continue
                        amount = _display_wikidata_amount(value.get("amount"))
                        lower = _display_wikidata_amount(value.get("lowerBound"))
                        upper = _display_wikidata_amount(value.get("upperBound"))
                        unit = value.get("unit_label") or value.get("unit") or value.get("unit_id")
                        if lower is not None and upper is not None:
                            quantity = f"between {lower} and {upper}"
                        else:
                            quantity = amount or "amount not reported"
                        if unit:
                            quantity += f" {unit}"
                        point_in_time = value.get("point_in_time")
                        if isinstance(point_in_time, str):
                            date = point_in_time.removeprefix("+").split("T", 1)[0]
                            quantity += f" (as of {date})"
                        statements.append(quantity)
                detail = "; ".join(statements) or f"no usable {measure_name} statement found"
                rows.append(f"{measure_name}: {prefix}: {detail}")
        return "\n".join(rows) if rows else "No Wikidata entity matches were found."
    wikidata_relationship = {
        "P17": ("countries", "country"),
        "P30": ("continents", "continent"),
        "P36": ("capitals", "capital"),
        "P37": ("languages", "official language"),
        "P38": ("currencies", "currency"),
    }
    if (
        provider == "wikidata"
        and isinstance(data, dict)
        and data.get("property_id") in wikidata_relationship
    ):
        value_key, property_name = wikidata_relationship[str(data["property_id"])]
        matches = data.get("matches")
        rows = []
        if isinstance(matches, list):
            for match in matches:
                if not isinstance(match, dict):
                    continue
                label = str(match.get("label") or match.get("id") or "Entity")
                identifier = str(match.get("id") or "")
                prefix = f"{label} [{identifier}]" if identifier else label
                description = match.get("description")
                if isinstance(description, str) and description:
                    prefix += f" — {description}"
                relationships = match.get(value_key)
                values = []
                if isinstance(relationships, list):
                    for relationship in relationships:
                        if not isinstance(relationship, dict):
                            continue
                        label = str(
                            relationship.get("label") or relationship.get("id") or "Unknown"
                        )
                        if relationship.get("id"):
                            label += f" [{relationship['id']}]"
                        interval = []
                        for key, caption in (
                            ("start_time", "from"),
                            ("end_time", "until"),
                            ("point_in_time", "on"),
                        ):
                            time_value = relationship.get(key)
                            if isinstance(time_value, str):
                                interval.append(
                                    f"{caption} {time_value.removeprefix('+').split('T', 1)[0]}"
                                )
                        if interval:
                            label += f" ({', '.join(interval)})"
                        values.append(label)
                detail = "; ".join(values) if values else f"no {property_name} statement found"
                rows.append(f"{prefix}: {detail}")
        return "\n".join(rows) if rows else "No Wikidata entity matches were found."
    if provider == "geonames" and isinstance(data, list):
        rows = []
        for place in data:
            if not isinstance(place, dict):
                continue
            name = str(place.get("name") or "Unknown place")
            location = ", ".join(
                str(value)
                for value in (place.get("adminName1"), place.get("countryName"))
                if value
            )
            coordinates = ", ".join(
                str(value) for value in (place.get("lat"), place.get("lng")) if value is not None
            )
            details = [value for value in (location, coordinates) if value]
            population = place.get("population")
            if population:
                details.append(f"population {population}")
            rows.append(f"{name} ({'; '.join(details)})" if details else name)
        return "\n".join(rows) if rows else "No matching places were found."
    if provider == "dbnomics" and isinstance(data, dict):
        candidates = data.get("candidates")
        if isinstance(candidates, list):
            if data.get("search_kind") == "series":
                blocks = []
                for candidate in candidates[:10]:
                    if not isinstance(candidate, dict):
                        continue
                    name = candidate.get("name") or candidate.get("series_code") or "Series"
                    code = candidate.get("series_code")
                    label = f"{name} [{code}]" if code else str(name)
                    dimensions = candidate.get("dimensions")
                    if isinstance(dimensions, dict) and dimensions:
                        rendered_dimensions = ", ".join(
                            f"{key}={value}" for key, value in sorted(dimensions.items())
                        )
                        label += f" ({rendered_dimensions})"
                    blocks.append(label)
                return "\n".join(blocks) if blocks else "No matching DBnomics series were found."
            blocks = []
            for candidate in candidates[:10]:
                if not isinstance(candidate, dict):
                    continue
                provider_name = candidate.get("provider_name") or candidate.get("provider_code") or "Provider"
                name = candidate.get("name") or candidate.get("dataset_code") or "Dataset"
                provider_code = candidate.get("provider_code", "")
                dataset_code = candidate.get("dataset_code", "")
                matching = candidate.get("matching_series_count")
                suffix = f" ({matching} matching series)" if isinstance(matching, int) else ""
                blocks.append(f"{name} — {provider_name} [{provider_code}/{dataset_code}]{suffix}")
            if blocks:
                return "\n".join(blocks)
            return "No matching DBnomics datasets were found."
        documents = data.get("docs")
        if isinstance(documents, list):
            blocks = []
            for document in documents[:10]:
                if not isinstance(document, dict):
                    continue
                code = document.get("series_code")
                name = document.get("series_name") or code or "Economic series"
                periods = document.get("period")
                values = document.get("value")
                if not isinstance(periods, list) or not isinstance(values, list):
                    blocks.append(f"{name} [{code}]" if code else str(name))
                    continue
                observations = list(zip(periods, values))
                visible = observations[-20:]
                lines = [f"{name} [{code}]" if code else str(name)]
                lines.extend(
                    f"{period}: {value if value is not None else 'NA'}"
                    for period, value in visible
                )
                if len(observations) > len(visible):
                    lines.append(
                        f"Showing the latest {len(visible)} of {len(observations)} observations."
                    )
                blocks.append("\n".join(lines))
            if blocks:
                return "\n\n".join(blocks)
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def _display_wikidata_amount(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        number = Decimal(value.removeprefix("+"))
    except InvalidOperation:
        return value.removeprefix("+")
    if not number.is_finite() or abs(number.adjusted()) > 100:
        return value.removeprefix("+")
    if number == number.to_integral_value():
        return f"{int(number):,}"
    return format(number.normalize(), "f")


def _prepare_query(
    input_text: str,
    assumptions: list[str],
    *,
    validate_only: bool,
    numeric_precision: int = 50,
    time_limit_seconds: float = 5.0,
    parse_timeout_seconds: float = 5.0,
    pod_timeout_seconds: float | None = None,
    deadline: float | None = None,
) -> tuple[dict[str, Any], SymbolicRequest]:
    natural_language_plan: NaturalLanguageOperation | None = None
    model_candidate_elapsed = 0.0
    non_formula_assumptions, formula_variable_values = _split_formula_variable_values(
        assumptions
    )
    non_variable_assumptions, selected_operation_variable = _split_operation_variable_selection(
        non_formula_assumptions
    )
    try:
        operation_request = _parse_operation_call(input_text)
        if operation_request is None:
            natural_language_plan = plan_natural_language_operation(
                input_text,
                variable_assumption=selected_operation_variable,
            )
            if natural_language_plan is None:
                if selected_operation_variable is not None:
                    raise AssumptionSelectionError(
                        "OperationVariable selections apply only to supported natural-language "
                        "symbolic operations"
                    )
                model_timeout = parse_timeout_seconds
                if deadline is not None:
                    model_timeout = min(model_timeout, deadline - perf_counter())
                if model_timeout > 0.001:
                    model_started = perf_counter()
                    natural_language_plan = generate_query_candidate(
                        input_text,
                        model_timeout,
                    )
                    model_candidate_elapsed = perf_counter() - model_started
            if (
                natural_language_plan is not None
                and natural_language_plan.candidate_generator == "language_model"
            ):
                if selected_operation_variable is not None:
                    raise AssumptionSelectionError(
                        "OperationVariable selections cannot override model-generated candidates"
                    )
                if model_candidate_elapsed >= parse_timeout_seconds:
                    raise InterpretationTimeout(model_candidate_elapsed)
                parse_timeout_seconds -= model_candidate_elapsed
            if natural_language_plan is not None and natural_language_plan.operation not in {
                "evaluate",
                "coordinate_gradient",
                "coordinate_divergence",
                "coordinate_curl",
                "coordinate_laplacian",
            }:
                operation_request = _request_from_natural_language_plan(natural_language_plan)
    except NaturalLanguageClarification as exc:
        if selected_operation_variable is not None:
            raise AssumptionSelectionError(str(exc)) from exc
        if exc.candidate_variables:
            raise _operation_variable_ambiguity(exc) from exc
        raise InterpretationError(str(exc)) from exc
    except ValidationError as exc:
        raise InterpretationError("symbolic operation arguments are invalid") from exc
    if selected_operation_variable is not None and (
        natural_language_plan is None
        or natural_language_plan.variable != selected_operation_variable
        or natural_language_plan.candidate_generator != "deterministic_template"
    ):
        raise AssumptionSelectionError(
            "OperationVariable selections apply only to a matching deterministic "
            "natural-language operation"
        )
    if formula_variable_values and (
        operation_request is not None or natural_language_plan is not None
    ):
        raise AssumptionSelectionError(
            "FormulaVariable value assignments currently apply to standalone symbolic expressions"
        )
    if operation_request is None:
        if natural_language_plan is not None and natural_language_plan.operation == "evaluate":
            interpreted = query_with_interpretations(
                natural_language_plan.expression,
                assumptions,
                compute=not validate_only,
                numeric_precision=numeric_precision,
                time_limit_seconds=time_limit_seconds,
                parse_timeout_seconds=parse_timeout_seconds,
                pod_timeout_seconds=pod_timeout_seconds,
                deadline=deadline,
            )
            interpreted["input"] = input_text
            interpreted["interpretation"]["natural_language_plan"] = {
                "rule_id": natural_language_plan.rule_id,
                "candidate_count": 1,
                "selected_candidate": {
                    "operation": "evaluate",
                    "expression": natural_language_plan.expression,
                },
                "selection_source": (
                    "model_candidate"
                    if natural_language_plan.candidate_generator == "language_model"
                    else "deterministic_template"
                ),
                "candidate_generator": natural_language_plan.candidate_generator,
                "semantic_fidelity": "english_to_formula_correspondence_unverified",
                **({"model": natural_language_plan.model}
                   if natural_language_plan.model is not None else {}),
            }
            interpreted["classification"]["query_intent"] = {
                "operation": "evaluate",
                "method": (
                    "model_generated_candidate"
                    if natural_language_plan.candidate_generator == "language_model"
                    else "deterministic_template_match"
                ),
                "model_ranked": False,
                "candidate_generator": natural_language_plan.candidate_generator,
                "semantic_alignment_verified": False,
            }
            symbolic_request = SymbolicRequest(
                operation="simplify",
                expression=str(interpreted["interpretation"]["expression"]),
                time_limit_seconds=time_limit_seconds,
                numeric_precision=numeric_precision,
            )
            return interpreted, symbolic_request
        interpretation_input = input_text
        interpretation_assumptions = non_variable_assumptions
        formula_variable_symbols: tuple[str, ...] = ()
        normalized_formula_values: dict[str, str] = {}
        standalone_expression = natural_language_plan is None
        submitted_formula_values: dict[str, str] = {}
        if standalone_expression and formula_variable_values:
            formula_values_started = perf_counter()
            (
                interpretation_input,
                formula_variable_symbols,
                normalized_formula_values,
                submitted_formula_values,
            ) = _apply_formula_variable_values(
                input_text,
                formula_variable_values,
                parse_timeout_seconds=parse_timeout_seconds,
                deadline=deadline,
            )
            parse_timeout_seconds -= perf_counter() - formula_values_started
            if parse_timeout_seconds <= 0.001:
                raise InterpretationTimeout(perf_counter() - formula_values_started)
        is_natural_language_coordinate = (
            natural_language_plan is not None
            and natural_language_plan.operation in {
                "coordinate_gradient",
                "coordinate_divergence",
                "coordinate_curl",
                "coordinate_laplacian",
            }
        )
        if is_natural_language_coordinate:
            assert natural_language_plan is not None
            if selected_operation_variable is not None:
                raise AssumptionSelectionError(
                    "OperationVariable selections do not apply to coordinate-calculus operations"
                )
            interpretation_input = _coordinate_query_from_natural_language_plan(
                natural_language_plan
            )
            interpretation_assumptions = list(assumptions)
            if natural_language_plan.coordinate_system is not None:
                coordinate_id = {
                    "Cartesian3D": "coordinate:cartesian-3d",
                    "Cylindrical3D": "coordinate:cylindrical-3d",
                    "Spherical3D": "coordinate:spherical-3d",
                }[natural_language_plan.coordinate_system]
                existing_coordinate_ids = {
                    selection.rsplit("@", 1)[0]
                    for selection in interpretation_assumptions
                    if selection.startswith("coordinate:")
                }
                if existing_coordinate_ids and existing_coordinate_ids != {coordinate_id}:
                    raise InterpretationError(
                        "the coordinate chart in the natural-language request conflicts "
                        "with the assumption override"
                    )
                if not existing_coordinate_ids:
                    interpretation_assumptions.append(coordinate_id)

        interpreted = query_with_interpretations(
            interpretation_input,
            (
                non_variable_assumptions
                if selected_operation_variable is not None
                else interpretation_assumptions
            ),
            compute=not validate_only,
            numeric_precision=numeric_precision,
            time_limit_seconds=time_limit_seconds,
            parse_timeout_seconds=parse_timeout_seconds,
            pod_timeout_seconds=pod_timeout_seconds,
            deadline=deadline,
        )
        if standalone_expression:
            interpreted["input"] = input_text
            if formula_variable_values:
                interpreted["interpretation"]["source_expression"] = input_text
                interpreted["interpretation"]["formula_variable_assignments"] = (
                    normalized_formula_values
                )
                if any(
                    _formula_unit_bindings(value)
                    for value in normalized_formula_values.values()
                ):
                    typed_ast = interpreted["interpretation"].get("typed_ast", {})
                    typed_ast["display_expression"] = _display_formula_variable_expression(
                        input_text,
                        submitted_formula_values,
                    )
            elif interpreted.get("interpretation", {}).get("kind") == "typed_symbolic_expression":
                interpreted_expression = str(
                    interpreted["interpretation"].get("expression", "")
                )
                interpreted_unit_symbols = _formula_unit_bindings(interpreted_expression)
                formula_variable_symbols = tuple(
                    symbol
                    for symbol in free_symbols(interpreted_expression)
                    if symbol not in interpreted_unit_symbols
                )
            if formula_variable_symbols:
                interpreted["assumptions"] = [
                    *interpreted.get("assumptions", []),
                    *[
                        _formula_variable_value_assumption(
                            variable,
                            submitted_formula_values.get(variable),
                            normalized_formula_values.get(variable),
                        )
                        for variable in formula_variable_symbols
                    ],
                ]
        if is_natural_language_coordinate:
            assert natural_language_plan is not None
            interpreted["input"] = input_text
            typed_ast = interpreted.get("interpretation", {}).get("typed_ast", {})
            typed_ast["display_expression"] = input_text
            selected_candidate: dict[str, Any] = {
                "operation": natural_language_plan.operation.removeprefix("coordinate_"),
                "coordinate_system": typed_ast.get("coordinate_system"),
                "assumption_id": typed_ast.get("assumption_id"),
                "coordinate_system_selection": typed_ast.get("selection_source"),
            }
            if natural_language_plan.components is not None:
                selected_candidate["components"] = list(natural_language_plan.components)
            else:
                selected_candidate["expression"] = natural_language_plan.expression
            interpreted["interpretation"]["natural_language_plan"] = {
                "rule_id": natural_language_plan.rule_id,
                "candidate_count": 1,
                "selected_candidate": selected_candidate,
                "selection_source": "deterministic_template",
                "semantic_fidelity": "english_to_formula_correspondence_unverified",
            }
            interpreted["classification"]["query_intent"] = {
                "operation": selected_candidate["operation"],
                "rule_id": natural_language_plan.rule_id,
                "method": "deterministic_template_match",
                "model_ranked": False,
                "semantic_alignment_verified": False,
            }
        typed_ast = interpreted.get("interpretation", {}).get("typed_ast", {})
        if typed_ast.get("node") == "vector_calculus_operation":
            coordinate_operation = typed_ast.get("operation")
            symbolic_operation = {
                "divergence": "coordinate_divergence",
                "gradient": "coordinate_gradient",
                "curl": "coordinate_curl",
                "laplacian": "coordinate_laplacian",
            }.get(coordinate_operation)
            if symbolic_operation == "coordinate_gradient":
                symbolic_request = SymbolicRequest(
                    operation=symbolic_operation,
                    expression=str(typed_ast.get("expression", "")),
                    coordinate_system=str(typed_ast.get("coordinate_system", "")),
                    time_limit_seconds=time_limit_seconds,
                    numeric_precision=numeric_precision,
                )
                return interpreted, symbolic_request
            if symbolic_operation == "coordinate_laplacian":
                symbolic_request = SymbolicRequest(
                    operation=symbolic_operation,
                    expression=str(typed_ast.get("expression", "")),
                    coordinate_system=str(typed_ast.get("coordinate_system", "")),
                    time_limit_seconds=time_limit_seconds,
                    numeric_precision=numeric_precision,
                )
                return interpreted, symbolic_request
            if symbolic_operation in {"coordinate_divergence", "coordinate_curl"}:
                symbolic_request = SymbolicRequest(
                    operation=symbolic_operation,
                    components=typed_ast.get("components", []),
                    coordinate_system=str(typed_ast.get("coordinate_system", "")),
                    time_limit_seconds=time_limit_seconds,
                    numeric_precision=numeric_precision,
                )
                return interpreted, symbolic_request
        expression = str(interpreted["interpretation"]["expression"])
        symbolic_request = SymbolicRequest(
            operation="simplify",
            expression=expression,
            time_limit_seconds=time_limit_seconds,
            numeric_precision=numeric_precision,
        )
        return interpreted, symbolic_request

    if assumptions:
        if non_variable_assumptions or selected_operation_variable is None:
            raise AssumptionSelectionError("assumption selection is not applicable to this operation")
        if (
            natural_language_plan is None
            or natural_language_plan.candidate_generator != "deterministic_template"
            or natural_language_plan.variable != selected_operation_variable
        ):
            raise AssumptionSelectionError(
                "OperationVariable selections apply only to a matching natural-language operation"
            )

    parse_started = perf_counter()
    normalized_fields: dict[str, str] = {}
    input_formats: set[str] = set()
    for field in ("expression", "lhs", "rhs", "lower", "upper", "point"):
        value = getattr(operation_request, field)
        if value is None:
            continue
        try:
            source_value = (
                _normalize_wolfram_real_functions(value)
                if operation_request.operation == "numeric"
                else value
            )
            parsed_input = parse_math_input(source_value)
        except MathInputError as exc:
            raise InterpretationError(str(exc)) from exc
        normalized_fields[field] = parsed_input.expression
        input_formats.add(parsed_input.input_format)
    operation_request = operation_request.model_copy(update={
        **normalized_fields,
        **(
            {"numeric_precision": numeric_precision}
            if operation_request.operation != "numeric"
            else {}
        ),
        "time_limit_seconds": time_limit_seconds,
    })

    expressions = [
        value for value in (
            operation_request.expression,
            operation_request.lhs,
            operation_request.rhs,
            operation_request.lower,
            operation_request.upper,
            operation_request.point,
        ) if value is not None
    ]
    parse_timing = perf_counter() - parse_started
    if expressions:
        combined_expression = " + ".join(f"({value})" for value in expressions)
        effective_parse_timeout = parse_timeout_seconds
        if deadline is not None:
            effective_parse_timeout = min(effective_parse_timeout, deadline - perf_counter())
        if effective_parse_timeout <= 0.001:
            raise InterpretationTimeout(perf_counter() - parse_started)
        parsed = run_symbolic(SymbolicRequest(
            operation="validate",
            expression=combined_expression,
            time_limit_seconds=effective_parse_timeout,
        ))
        if parsed.get("timed_out"):
            raise InterpretationTimeout(perf_counter() - parse_started)
        if parsed.get("error"):
            raise InterpretationError(str(parsed.get("error", "input could not be parsed")))
        parse_timing += float(parsed.get("parse_timing_seconds", 0.0))

    operation = operation_request.operation
    typed_ast: dict[str, Any] = {
        "node": "symbolic_operation",
        "operation": operation,
        "expression": operation_request.expression,
        "variable": operation_request.variable,
    }
    if operation_request.lhs is not None:
        typed_ast["lhs"] = operation_request.lhs
        typed_ast["rhs"] = operation_request.rhs
    if operation_request.lower is not None:
        typed_ast["lower"] = operation_request.lower
        typed_ast["upper"] = operation_request.upper
    if operation_request.point is not None:
        typed_ast["point"] = operation_request.point
    if operation_request.order is not None:
        typed_ast["order"] = operation_request.order
    if operation_request.operation == "numeric":
        typed_ast["precision"] = operation_request.numeric_precision
    interpreted = {
        "input": input_text,
        "outcome": "validated" if validate_only else "prepared",
        "parse_timing": parse_timing,
        "assumptions": [],
        "interpretation": {
            "kind": "typed_symbolic_operation",
            "typed_ast": typed_ast,
            "expression": input_text.strip(),
            "operation": operation,
            "variable": operation_request.variable,
            "solver_route": "sympy",
            **({"input_formats": sorted(input_formats - {"native"})} if input_formats - {"native"} else {}),
            **({
                "natural_language_plan": {
                    "rule_id": natural_language_plan.rule_id,
                    "candidate_count": 1,
                    "selected_candidate": {
                        "operation": operation,
                        **typed_ast,
                    },
                    "selection_source": (
                        "model_candidate"
                        if natural_language_plan.candidate_generator == "language_model"
                        else "deterministic_template"
                    ),
                    "candidate_generator": natural_language_plan.candidate_generator,
                    "semantic_fidelity": "english_to_formula_correspondence_unverified",
                    **({"model": natural_language_plan.model}
                       if natural_language_plan.model is not None else {}),
                    **({"variable_source": natural_language_plan.variable_source}
                       if natural_language_plan.variable_source else {}),
                }
            } if natural_language_plan is not None else {}),
        },
        "classification": (
            {
                "name": operation,
                "method": (
                    "model_generated_candidate"
                    if natural_language_plan.candidate_generator == "language_model"
                    else "deterministic_template_match"
                ),
                "candidate_count": 1,
                "model_ranked": False,
                "candidate_generator": natural_language_plan.candidate_generator,
                "semantic_alignment_verified": False,
            }
            if natural_language_plan is not None
            else {
                "name": "not_applicable",
                "method": "explicit operation syntax",
                "ranks_candidates_only": True,
            }
        ),
    }
    if selected_operation_variable is not None and natural_language_plan is not None:
        interpreted["assumptions"] = [
            _operation_variable_assumption(
                natural_language_plan.variable_candidates,
                natural_language_plan.operation,
                selected_variable=selected_operation_variable,
            )
        ]
    return interpreted, operation_request


def _request_from_natural_language_plan(
    plan: NaturalLanguageOperation,
) -> SymbolicRequest:
    if plan.operation == "identity":
        if plan.rhs is None:
            raise InterpretationError("prove requires an equality with two expressions")
        request = _operation_request("identity", [plan.expression, plan.rhs], None)
        if request is None:
            raise InterpretationError("natural-language identity did not produce a supported query")
        return request

    arguments = [plan.expression]
    if plan.operation in {"differentiate", "solve", "integrate", "limit", "series", "sum", "product", "plot"}:
        if plan.variable is None:
            raise InterpretationError(f"{plan.operation} requires an explicit variable")
        arguments.append(plan.variable)
    if plan.operation == "integrate":
        if plan.lower is not None and plan.upper is not None:
            arguments.extend((plan.lower, plan.upper))
    elif plan.operation == "limit":
        if plan.point is None:
            raise InterpretationError("limit requires a point")
        arguments.append(plan.point)
    elif plan.operation == "series":
        if plan.point is None or plan.order is None:
            raise InterpretationError("series requires an expansion point and order")
        arguments.extend((plan.point, str(plan.order)))
    elif plan.operation in {"sum", "product", "plot"}:
        if plan.lower is None or plan.upper is None:
            raise InterpretationError(f"{plan.operation} requires lower and upper bounds")
        arguments.extend((plan.lower, plan.upper))
    request = _operation_request(plan.operation, arguments, plan.variable)
    if request is None:
        raise InterpretationError("natural-language operation did not produce a supported query")
    return request


def _coordinate_query_from_natural_language_plan(
    plan: NaturalLanguageOperation,
) -> str:
    if plan.operation == "coordinate_gradient":
        if not plan.expression:
            raise InterpretationError("gradient requires a scalar field")
        return f"grad({plan.expression})"
    if plan.operation == "coordinate_laplacian":
        if not plan.expression:
            raise InterpretationError("Laplacian requires a scalar field")
        return f"laplacian({plan.expression})"
    function_name = {
        "coordinate_divergence": "div",
        "coordinate_curl": "curl",
    }.get(plan.operation)
    if function_name is None or plan.components is None or len(plan.components) != 3:
        raise InterpretationError(
            "divergence and curl require exactly three vector components"
        )
    return f"{function_name}({', '.join(plan.components)})"


def _parse_operation_call(query: str) -> SymbolicRequest | None:
    call = _split_operation_call(query)
    if call is not None:
        operation_name, arguments = call
        variable = arguments[1] if len(arguments) > 1 else None
        return _operation_request(operation_name, arguments, variable)
    try:
        node = ast.parse(query, mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None
    operation_name = node.func.id.lower()
    aliases = {
        "n": "numeric",
        "simplify": "simplify",
        "factor": "factor",
        "expand": "expand",
        "differentiate": "differentiate",
        "diff": "differentiate",
        "integrate": "integrate",
        "integral": "integrate",
        "limit": "limit",
        "series": "series",
        "sum": "sum",
        "summation": "sum",
        "product": "product",
        "prod": "product",
        "solve": "solve",
        "identity": "identity",
        "plot": "plot",
    }
    operation = aliases.get(operation_name)
    if operation is None:
        return None
    if node.keywords:
        raise InterpretationError("symbolic operation calls do not accept keyword arguments")
    args = node.args
    arguments = [ast.unparse(argument) for argument in args]
    variable = args[1].id if len(args) > 1 and isinstance(args[1], ast.Name) else None
    return _operation_request(operation_name, arguments, variable)


def _operation_request(
    operation_name: str,
    arguments: list[str],
    variable: str | None,
) -> SymbolicRequest | None:
    aliases = {
        "n": "numeric",
        "simplify": "simplify", "factor": "factor", "expand": "expand",
        "differentiate": "differentiate", "diff": "differentiate",
        "integrate": "integrate", "integral": "integrate", "limit": "limit", "series": "series",
        "sum": "sum", "summation": "sum", "product": "product", "prod": "product",
        "solve": "solve",
        "identity": "identity",
        "plot": "plot",
    }
    operation = aliases.get(operation_name.lower())
    if operation is None:
        return None
    if operation == "numeric":
        if len(arguments) not in {1, 2}:
            raise InterpretationError("N requires an expression and optional integer precision")
        precision = 16 if len(arguments) == 1 else _bounded_integer_literal(arguments[1])
        if precision is None or not 1 <= precision <= 500:
            raise InterpretationError("N precision must be an integer from 1 to 500")
        return SymbolicRequest(
            operation="numeric",
            expression=arguments[0],
            numeric_precision=precision,
            time_limit_seconds=5,
        )
    if operation in {"simplify", "factor", "expand"}:
        if len(arguments) != 1:
            raise InterpretationError(f"{operation} requires exactly one expression")
        return SymbolicRequest(operation=operation, expression=arguments[0], time_limit_seconds=5)
    if operation == "identity":
        if len(arguments) != 2:
            raise InterpretationError("identity requires exactly two expressions")
        return SymbolicRequest(
            operation="identity", lhs=arguments[0], rhs=arguments[1], time_limit_seconds=5
        )
    if operation in {"differentiate", "solve"}:
        if len(arguments) != 2 or variable is None or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", variable):
            raise InterpretationError(f"{operation_name} requires an expression and a variable name")
        return SymbolicRequest(
            operation=operation,
            expression=arguments[0],
            variable=variable,
            time_limit_seconds=5,
        )
    if operation == "limit":
        if len(arguments) != 3 or variable is None or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{0,31}", variable
        ):
            raise InterpretationError("limit requires an expression, a variable name, and a point")
        return SymbolicRequest(
            operation="limit",
            expression=arguments[0],
            variable=variable,
            point=arguments[2],
            assumptions=_real_limit_point_assumptions(arguments[2], variable),
            time_limit_seconds=5,
        )
    if operation == "series":
        order = _bounded_integer_literal(arguments[3]) if len(arguments) == 4 else None
        if len(arguments) != 4 or variable is None or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{0,31}", variable
        ) or order is None or not 1 <= order <= 32:
            raise InterpretationError(
                "series requires an expression, variable, expansion point, and integer order from 1 to 32"
            )
        return SymbolicRequest(
            operation="series",
            expression=arguments[0],
            variable=variable,
            point=arguments[2],
            order=order,
            time_limit_seconds=5,
        )
    if operation in {"sum", "product"}:
        if len(arguments) != 4 or variable is None or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{0,31}", variable
        ):
            raise InterpretationError(
                f"{operation} requires an expression, variable, lower bound, and upper bound"
            )
        if any(_bounded_integer_literal(bound) is None for bound in arguments[2:]):
            raise InterpretationError(
                f"{operation} query calls require integer literal bounds; use /v1/symbolic for declared symbolic bounds"
            )
        return SymbolicRequest(
            operation=operation,
            expression=arguments[0],
            variable=variable,
            lower=arguments[2],
            upper=arguments[3],
            time_limit_seconds=5,
        )
    if operation == "plot":
        if len(arguments) != 4 or variable is None or not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{0,31}", variable
        ):
            raise InterpretationError("plot requires an expression, variable, lower bound, and upper bound")
        return SymbolicRequest(
            operation="plot",
            expression=arguments[0],
            variable=variable,
            lower=arguments[2],
            upper=arguments[3],
            time_limit_seconds=10,
        )
    if len(arguments) not in {2, 4} or variable is None or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_]{0,31}", variable
    ):
        raise InterpretationError(
            "integrate requires expression and variable, with optional lower and upper bounds"
        )
    return SymbolicRequest(
        operation="integrate",
        expression=arguments[0],
        variable=variable,
        lower=arguments[2] if len(arguments) == 4 else None,
        upper=arguments[3] if len(arguments) == 4 else None,
        time_limit_seconds=5,
    )


def _real_limit_point_assumptions(point: str, variable: str) -> dict[str, list[str]]:
    try:
        tree = ast.parse(point, mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        return {}
    function_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    constants = {"pi", "E", "I", "oo"}
    names = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id not in function_names
        and node.id not in constants
        and node.id != variable
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", node.id)
    }
    return {name: ["real"] for name in sorted(names)}


def _bounded_integer_literal(source: str) -> int | None:
    if len(source) > 90:
        return None
    try:
        node = ast.parse(source, mode="eval").body
    except SyntaxError:
        return None
    if isinstance(node, ast.Constant) and type(node.value) is int:
        value = node.value
    elif (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.UAdd, ast.USub))
        and isinstance(node.operand, ast.Constant)
        and type(node.operand.value) is int
    ):
        value = node.operand.value if isinstance(node.op, ast.UAdd) else -node.operand.value
    else:
        return None
    return value if abs(value).bit_length() <= 256 else None


def _split_operation_call(query: str) -> tuple[str, list[str]] | None:
    match = re.match(r"\s*([A-Za-z][A-Za-z0-9_]*)\s*([\(\[])", query)
    if match is None:
        return None
    opening = match.group(2)
    opening_index = query.find(opening, match.start(1) + len(match.group(1)))
    if opening_index < 0:
        return None
    arguments: list[str] = []
    start = opening_index + 1
    stack = [{"(": ")", "[": "]"}[opening]]
    xml_depth = 0
    index = start
    while index < len(query):
        character = query[index]
        if character == "<":
            tag_end = query.find(">", index + 1)
            if tag_end >= 0:
                tag = query[index + 1:tag_end].strip()
                if tag.startswith("/"):
                    xml_depth = max(0, xml_depth - 1)
                elif tag and not tag.endswith("/") and not tag.startswith(("!", "?")):
                    xml_depth += 1
                index = tag_end + 1
                continue
        if xml_depth > 0:
            index += 1
            continue
        if character in "([{":
            stack.append({"(": ")", "[": "]", "{": "}"}[character])
        elif character in ")]}":
            if not stack or character != stack[-1]:
                return None
            stack.pop()
            if not stack:
                if query[index + 1:].strip():
                    return None
                final = query[start:index].strip()
                if final:
                    arguments.append(final)
                elif arguments:
                    return None
                return match.group(1).lower(), arguments
        elif character == "," and len(stack) == 1 and xml_depth == 0:
            argument = query[start:index].strip()
            if not argument:
                return None
            arguments.append(argument)
            start = index + 1
        index += 1
    return None


def query_error(
    input_text: str | None,
    message: str,
    *,
    code: str = "INVALID_REQUEST",
) -> dict[str, Any]:
    queryresult = _base_queryresult(
        success=False,
        error=True,
        numpods=0,
        elapsed=0,
        parse_timing=0,
    )
    queryresult.update({
        "input": input_text or "",
        "error_detail": {"code": code, "msg": message},
    })
    return {"queryresult": queryresult}


def validate_error(input_text: str | None, message: str) -> dict[str, Any]:
    return {
        "validatequeryresult": {
            "success": False,
            "error": True,
            "timing": 0.0,
            "parsetiming": 0.0,
            "version": _API_VERSION,
            "service": "LEAN Engine",
            "input": input_text or "",
            "error_detail": {"code": "INVALID_REQUEST", "msg": message},
        }
    }


def render_response(payload: dict[str, Any], output: str) -> Response:
    if output == "json":
        return JSONResponse(content=payload)
    xml = _payload_to_xml(payload)
    return Response(content=xml, media_type="application/xml; charset=utf-8")


def _unrecognized_query(
    input_text: str,
    message: str,
    elapsed: float,
    *,
    assumptions: list[dict[str, Any]] | None = None,
    interpretation: dict[str, Any] | None = None,
    classification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = _query_envelope(
        success=False,
        error=False,
        pods=[],
        elapsed=elapsed,
        parse_timing=elapsed,
        datatypes="Mathematical" if interpretation is not None else "",
        warnings=[],
        assumptions=assumptions or [],
        interpretation=interpretation,
        classification=classification,
        tips=[{"text": message}],
        qualification=(
            "The typed interpretation was rejected by deterministic "
            "validation; no result was computed."
            if interpretation is not None
            else "The input was not parsed; no result was computed."
        ),
    )
    result["queryresult"]["input"] = input_text
    return result


def _provider_assumption_clarification(
    input_text: str,
    error: ProviderAssumptionNeeded,
    formats: list[str],
    elapsed: float,
    text_width: int,
) -> dict[str, Any]:
    del text_width  # No result image is produced while the interpretation is unresolved.
    per_capita = error.assumption["type"] == "GDPPerCapitaType"
    operation = "gdp_per_capita" if per_capita else "gdp"
    measure = "GDP per capita" if per_capita else "GDP"
    alternatives = (
        "nominal GDP per capita and PPP GDP per capita"
        if per_capita
        else "nominal GDP and GDP at purchasing power parity"
    )
    result = _query_envelope(
        success=False,
        error=False,
        pods=[],
        elapsed=elapsed,
        parse_timing=elapsed,
        datatypes="Data",
        warnings=_format_warnings(formats),
        assumptions=[error.assumption],
        interpretation={
            "kind": "ambiguous_data_request",
            "provider": "wikidata",
            "operation": operation,
            "selection_required": True,
        },
        classification={
            "name": "deterministic_provider_router",
            "method": f"{measure} measure candidates from the typed assumption registry",
            "ranks_candidates_only": True,
            "selection_authority": False,
        },
        tips=[{"text": str(error)}],
        qualification=(
            f"The request is ambiguous between {alternatives}. No provider lookup was run "
            "until a measure was selected."
        ),
    )
    result["queryresult"]["input"] = input_text
    return result


def _symbolic_timeout_response(
    input_text: str,
    started: float,
    formats: list[str],
    *,
    parse_timing: float,
    text_width: int = 500,
) -> dict[str, Any]:
    input_image = _svg_text_image(input_text, width=text_width) if "image" in formats else None
    payload = _query_envelope(
        success=False,
        error=False,
        pods=_initial_pods(input_text, input_text, None, formats, input_image=input_image),
        elapsed=perf_counter() - started,
        parse_timing=parse_timing,
        datatypes="Mathematical",
        warnings=[
            *_format_warnings(
                formats,
                _SUPPORTED_FORMATS | ({"image"} if input_image is not None else set()),
            ),
            {"text": "The query exceeded its total execution time limit."},
        ],
        assumptions=[],
        timedout="Mathematical",
        timedoutpods="Result",
        evidence={"kind": "symbolic_timeout", "engine": "sympy", "verified": False},
        qualification="A timeout contains no result and is not a proof.",
    )
    payload["queryresult"]["input"] = input_text
    return payload


def _parse_timeout_response(
    input_text: str,
    started: float,
    *,
    parse_timing: float,
) -> dict[str, Any]:
    payload = _query_envelope(
        success=False,
        error=False,
        pods=[],
        elapsed=perf_counter() - started,
        parse_timing=parse_timing,
        datatypes="",
        warnings=[{"text": "The input exceeded its parsing time limit."}],
        assumptions=[],
        evidence={"kind": "symbolic_parse_timeout", "engine": "sympy", "verified": False},
        qualification="No computation was run because the input parser timed out.",
    )
    payload["queryresult"]["input"] = input_text
    payload["queryresult"]["parsetimedout"] = True
    return payload


def _query_envelope(
    *,
    success: bool,
    error: bool,
    pods: list[dict[str, Any]],
    elapsed: float,
    parse_timing: float,
    datatypes: str,
    warnings: list[dict[str, str]],
    assumptions: list[dict[str, Any]],
    interpretation: dict[str, Any] | None = None,
    classification: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    qualification: str | None = None,
    tips: list[dict[str, str]] | None = None,
    timedout: str = "",
    timedoutpods: str = "",
    sources: list[dict[str, Any]] | None = None,
    retrieved_at: str | None = None,
    data: Any = None,
) -> dict[str, Any]:
    _attach_pod_provenance(pods, evidence=evidence, sources=sources)
    result: dict[str, Any] = _base_queryresult(
        success=success,
        error=error,
        numpods=len(pods),
        elapsed=elapsed,
        parse_timing=parse_timing,
    )
    result.update({
        "datatypes": datatypes,
        "timedout": timedout,
        "timedoutpods": timedoutpods,
        "pods": pods,
        "assumptions": assumptions,
        "warnings": warnings,
    })
    if tips:
        result["tips"] = tips
    if interpretation is not None:
        result["interpretation"] = interpretation
    if classification is not None:
        result["classification"] = classification
    if evidence is not None:
        result["evidence"] = evidence
    if qualification is not None:
        result["qualification"] = qualification
    if sources is not None:
        result["sources"] = sources
    if retrieved_at is not None:
        result["retrieved_at"] = retrieved_at
    if data is not None:
        result["data"] = data
    return {"queryresult": result}


def _attach_pod_provenance(
    pods: list[dict[str, Any]],
    *,
    evidence: dict[str, Any] | None,
    sources: list[dict[str, Any]] | None,
) -> None:
    """Give each pod local source and verification metadata for downstream consumers."""
    query_evidence = evidence or {}
    query_sources = [source for source in (sources or []) if isinstance(source, dict)]
    evidence_kind = str(query_evidence.get("kind", "computed_result"))
    verified = query_evidence.get("verified") is True
    engine = query_evidence.get("engine")
    provider = query_evidence.get("provider")
    formal = query_evidence.get("formal_verification")

    for pod in pods:
        if pod.get("id") == "Input":
            pod_source = [{"name": "LEAN Engine", "component": "input_interpreter"}]
            pod_kind = "input_interpretation"
            pod_verified = False
        elif query_sources:
            pod_source = [dict(source) for source in query_sources]
            pod_kind = evidence_kind
            pod_verified = verified
        elif provider:
            pod_source = [{"name": str(provider), "component": "data_provider"}]
            pod_kind = evidence_kind
            pod_verified = verified
        else:
            engine_name = str(engine) if engine else "LEAN Engine"
            pod_source = [{"name": engine_name, "component": "computation_engine"}]
            if isinstance(formal, dict) and formal.get("verified") is True:
                pod_source.append({
                    "name": "Lean 4 + Mathlib",
                    "component": "proof_checker",
                })
            pod_kind = evidence_kind
            pod_verified = verified

        pod.setdefault("source", pod_source)
        pod.setdefault("evidence_kind", pod_kind)
        pod.setdefault("verified", pod_verified)
        if pod.get("id") == "Result" and isinstance(formal, dict):
            claim_status = formal.get("claim_status")
            if isinstance(claim_status, str):
                pod.setdefault("claim_status", claim_status)
            if isinstance(formal.get("counterexample"), dict):
                pod.setdefault("counterexample", formal["counterexample"])


def _base_queryresult(
    *, success: bool, error: bool, numpods: int, elapsed: float, parse_timing: float
) -> dict[str, Any]:
    return {
        "success": success,
        "error": error,
        "numpods": numpods,
        "datatypes": "",
        "timedout": "",
        "timedoutpods": "",
        "timing": _seconds(elapsed),
        "parsetiming": _seconds(parse_timing),
        "parsetimedout": False,
        "recalculate": "",
        "id": str(uuid4()),
        "version": _API_VERSION,
        "service": "LEAN Engine",
    }


def _initial_pods(
    input_text: str,
    expression: str,
    result_text: str | None,
    formats: list[str] | None = None,
    *,
    input_mathml: str | None = None,
    result_mathml: str | None = None,
    input_minput: str | None = None,
    result_moutput: str | None = None,
    input_image: dict[str, Any] | None = None,
    result_image: dict[str, Any] | None = None,
    approximation_image: dict[str, Any] | None = None,
    result_scanner: str = "Mathematical",
    numeric_approximation: dict[str, Any] | None = None,
    decimal_state_level: int = 0,
) -> list[dict[str, Any]]:
    selected_formats = formats or ["plaintext"]
    pods = [
        _input_pod(
            expression,
            selected_formats,
            input_mathml=input_mathml,
            input_minput=input_minput,
            input_image=input_image,
        ),
        _result_pod(
            result_text,
            selected_formats,
            result_mathml=result_mathml,
            result_moutput=result_moutput,
            result_image=result_image,
            result_scanner=result_scanner,
        ),
    ]
    if numeric_approximation is not None:
        pods.append(_decimal_approximation_pod(
            numeric_approximation,
            selected_formats,
            approximation_image=approximation_image,
            decimal_state_level=decimal_state_level,
        ))
    return pods


def _input_pod(
    expression: str,
    formats: list[str],
    *,
    input_mathml: str | None = None,
    input_minput: str | None = None,
    input_image: dict[str, Any] | None = None,
) -> dict[str, Any]:
    subpod_input: dict[str, Any] = {"title": ""}
    if "plaintext" in formats:
        subpod_input["plaintext"] = expression
    if "cell" in formats:
        subpod_input["cell"] = _wolfram_cell(expression)
    if "mathml" in formats:
        subpod_input["mathml"] = input_mathml or _mathml(expression)
    if "minput" in formats and input_minput is not None:
        subpod_input["minput"] = input_minput
    if "image" in formats and input_image is not None:
        subpod_input["img"] = input_image
    return {
        "title": "Input interpretation",
        "scanner": "Identity",
        "id": "Input",
        "position": 100,
        "error": False,
        "numsubpods": 1,
        "primary": False,
        "subpods": [subpod_input],
    }


def _result_pod(
    result_text: str | None,
    formats: list[str],
    *,
    result_mathml: str | None = None,
    result_moutput: str | None = None,
    result_image: dict[str, Any] | None = None,
    result_scanner: str = "Mathematical",
) -> dict[str, Any]:
    subpod_result: dict[str, Any] = {"title": ""}
    if result_text is not None and "plaintext" in formats:
        subpod_result["plaintext"] = result_text
    if result_text is not None and "cell" in formats:
        subpod_result["cell"] = _wolfram_cell(result_text)
    if result_text is not None and "mathml" in formats:
        subpod_result["mathml"] = result_mathml or _mathml(result_text)
    if "moutput" in formats and result_moutput is not None:
        subpod_result["moutput"] = result_moutput
    if "image" in formats and result_image is not None:
        subpod_result["img"] = result_image
    return {
        "title": "Result",
        "scanner": result_scanner,
        "id": "Result",
        "position": 200,
        "error": False,
        "numsubpods": 1 if result_text is not None else 0,
        "primary": True,
        "subpods": [subpod_result] if result_text is not None else [],
    }


def _decimal_approximation_pod(
    numeric_approximation: dict[str, Any],
    formats: list[str],
    *,
    approximation_image: dict[str, Any] | None = None,
    decimal_state_level: int = 0,
) -> dict[str, Any]:
    approximation = str(numeric_approximation["value"])
    approximation_subpod: dict[str, Any] = {"title": ""}
    if "plaintext" in formats:
        approximation_subpod["plaintext"] = approximation
    if "cell" in formats:
        approximation_subpod["cell"] = _wolfram_cell(approximation)
    if "mathml" in formats:
        approximation_subpod["mathml"] = _mathml(approximation)
    if "image" in formats and approximation_image is not None:
        approximation_subpod["img"] = approximation_image
    return {
        "title": "Decimal approximation",
        "scanner": "Numeric",
        "id": "DecimalApproximation",
        "position": 300,
        "error": False,
        "numsubpods": 1,
        "primary": False,
        "subpods": [approximation_subpod],
        "states": _decimal_states_for_level(decimal_state_level),
        "precision": int(numeric_approximation.get("precision", 50)),
    }


def _svg_text_image(text: str, *, width: int = 500) -> dict[str, Any] | None:
    """Render bounded plaintext as an escaped, self-contained SVG image."""
    if not text:
        return None

    safe_text = _xml_safe_text(text)
    max_chars = 4000
    truncated = len(safe_text) > max_chars
    visible_text = safe_text[:max_chars]
    if truncated:
        visible_text += "..."

    font_size = 16
    line_height = 22
    horizontal_padding = 20
    max_columns = max(12, int((width - horizontal_padding * 2) / (font_size * 0.58)))
    max_lines = 80
    lines: list[str] = []
    for paragraph in visible_text.splitlines() or [visible_text]:
        if len(lines) >= max_lines:
            truncated = True
            break
        if not paragraph:
            lines.append("")
            continue
        remaining = paragraph
        while len(remaining) > max_columns:
            split_at = remaining.rfind(" ", 0, max_columns + 1)
            if split_at <= 0:
                split_at = max_columns
            lines.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip()
            if len(lines) >= max_lines:
                truncated = True
                break
        if len(lines) >= max_lines:
            break
        lines.append(remaining)
    if not lines:
        return None
    if truncated:
        last = lines[-1]
        lines[-1] = (last[: max_columns - 3] + "...") if len(last) >= max_columns else last + "..."

    height = 24 + len(lines) * line_height
    title_text = safe_text[:240] + ("..." if len(safe_text) > 240 else "")
    title = html.escape(title_text, quote=True)
    text_elements = "".join(
        f'<text x="{horizontal_padding}" y="{26 + index * line_height}">'
        f'{html.escape(line, quote=False)}</text>'
        for index, line in enumerate(lines)
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{title}">'
        f'<title>{title}</title>'
        '<style>text{font:16px "Cambria Math","STIX Two Math",serif;fill:#111827}'
        '</style><rect width="100%" height="100%" fill="#ffffff"/>'
        f'{text_elements}</svg>'
    )
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return {
        "src": f"data:image/svg+xml;base64,{encoded}",
        "alt": title_text,
        "title": title_text,
        "width": width,
        "height": height,
    }


def _xml_safe_text(text: str) -> str:
    return "".join(
        char
        for char in text
        if char in "\t\n\r"
        or 0x20 <= ord(char) <= 0xD7FF
        or 0xE000 <= ord(char) <= 0xFFFD
        or 0x10000 <= ord(char) <= 0x10FFFF
    )


def _format_warnings(
    formats: list[str], supported_formats: set[str] | None = None
) -> list[dict[str, str]]:
    supported = supported_formats or _SUPPORTED_FORMATS
    omitted = [name for name in formats if name not in supported]
    if not omitted:
        return []
    return [{
        "text": "These requested formats are not available for this query: "
        + ", ".join(omitted)
    }]


def _format_timeout_warning(titles: list[str]) -> dict[str, str]:
    return {
        "text": (
            "Formatting exceeded its per-pod, cumulative, or total time budget; these pods were "
            "omitted: " + ", ".join(titles)
        )
    }


def _wolfram_input_for_query(
    expression: str,
    request: SymbolicRequest,
    *,
    is_explicit_operation: bool,
) -> str | None:
    if not is_explicit_operation:
        return _wolfram_expression(expression)

    rendered_expression = _wolfram_expression(request.expression or "")
    variable = _wolfram_symbol(request.variable or "")
    if request.operation == "numeric" and rendered_expression is not None:
        return f"N[{rendered_expression}, {request.numeric_precision}]"
    if request.operation == "simplify" and rendered_expression is not None:
        return f"Simplify[{rendered_expression}]"
    if request.operation == "factor" and rendered_expression is not None:
        return f"Factor[{rendered_expression}]"
    if request.operation == "expand" and rendered_expression is not None:
        return f"Expand[{rendered_expression}]"
    if request.operation == "differentiate" and rendered_expression is not None and variable:
        return f"D[{rendered_expression}, {variable}]"
    if request.operation == "solve" and rendered_expression is not None and variable:
        return f"Solve[{rendered_expression} == 0, {variable}]"
    if request.operation == "integrate" and rendered_expression is not None and variable:
        if request.lower is None or request.upper is None:
            return f"Integrate[{rendered_expression}, {variable}]"
        lower = _wolfram_expression(request.lower)
        upper = _wolfram_expression(request.upper)
        if lower is not None and upper is not None:
            return f"Integrate[{rendered_expression}, {{{variable}, {lower}, {upper}}}]"
    if request.operation in {"sum", "product"} and rendered_expression is not None and variable:
        lower = _wolfram_expression(request.lower or "")
        upper = _wolfram_expression(request.upper or "")
        if lower is not None and upper is not None:
            function = "Sum" if request.operation == "sum" else "Product"
            return f"{function}[{rendered_expression}, {{{variable}, {lower}, {upper}}}]"
    if request.operation == "limit" and rendered_expression is not None and variable:
        point = _wolfram_expression(request.point or "")
        if point is not None:
            return f"Limit[{rendered_expression}, {{{variable}, {point}}}]"
    if request.operation == "series" and rendered_expression is not None and variable:
        point = _wolfram_expression(request.point or "")
        if point is not None and request.order is not None:
            return f"Series[{rendered_expression}, {{{variable}, {point}, {request.order}}}]"
    if request.operation == "plot" and rendered_expression is not None and variable:
        lower = _wolfram_expression(request.lower or "")
        upper = _wolfram_expression(request.upper or "")
        if lower is not None and upper is not None:
            return f"Plot[{rendered_expression}, {{{variable}, {lower}, {upper}}}]"
    if request.operation == "identity":
        lhs = _wolfram_expression(request.lhs or "")
        rhs = _wolfram_expression(request.rhs or "")
        if lhs is not None and rhs is not None:
            return f"FullSimplify[{lhs} == {rhs}]"
    return None


def _wolfram_expression(source: str) -> str | None:
    try:
        node = ast.parse(source, mode="eval").body
        return _render_wolfram_node(node)
    except (SyntaxError, TypeError, ValueError):
        return None


def _wolfram_quantity(typed_ast: dict[str, Any]) -> str | None:
    value = _wolfram_expression(str(typed_ast.get("magnitude", "")))
    unit = typed_ast.get("wolfram_unit")
    if value is None or unit not in SUPPORTED_WOLFRAM_UNIT_NAMES:
        return None
    return f"Quantity[{value}, {json.dumps(unit)}]"


def _wolfram_series_output(series_data: Any) -> str | None:
    if not isinstance(series_data, dict):
        return None
    variable = series_data.get("variable")
    point = _wolfram_expression(str(series_data.get("point", "")))
    coefficients = series_data.get("coefficients")
    lower_power = series_data.get("lowest_power")
    upper_power = series_data.get("highest_power_exclusive")
    denominator = series_data.get("denominator")
    if (
        not isinstance(variable, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", variable)
        or point is None
        or not isinstance(coefficients, list)
        or not 1 <= len(coefficients) <= 32
        or type(lower_power) is not int
        or type(upper_power) is not int
        or type(denominator) is not int
        or denominator != 1
        or upper_power - lower_power != len(coefficients)
    ):
        return None
    rendered_coefficients = [_wolfram_expression(str(value)) for value in coefficients]
    if any(value is None for value in rendered_coefficients):
        return None
    coefficient_list = "{" + ", ".join(value for value in rendered_coefficients if value is not None) + "}"
    return (
        f"SeriesData[{_wolfram_symbol(variable)}, {point}, {coefficient_list}, "
        f"{lower_power}, {upper_power}, {denominator}]"
    )


def _render_wolfram_node(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        constants = {
            "pi": "Pi",
            "E": "E",
            "I": "I",
            "oo": "Infinity",
            "zoo": "ComplexInfinity",
            "nan": "Indeterminate",
        }
        return constants.get(node.id, _wolfram_symbol(node.id))
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            return "True" if node.value else "False"
        if isinstance(node.value, (int, float)):
            return _wolfram_number(node.value)
        if isinstance(node.value, str):
            return json.dumps(node.value, ensure_ascii=False)
        raise TypeError("unsupported Wolfram output literal")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        sign = "+" if isinstance(node.op, ast.UAdd) else "-"
        return f"({sign}{_render_wolfram_node(node.operand)})"
    if isinstance(node, ast.BinOp):
        operators = {
            ast.Add: "+",
            ast.Sub: "-",
            ast.Mult: "*",
            ast.Div: "/",
            ast.Pow: "^",
        }
        operator = operators.get(type(node.op))
        if operator is None:
            raise TypeError("unsupported Wolfram output operator")
        left = _render_wolfram_node(node.left)
        right = _render_wolfram_node(node.right)
        return f"({left} {operator} {right})"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        functions = {
            "sin": "Sin", "cos": "Cos", "tan": "Tan",
            "asin": "ArcSin", "acos": "ArcCos", "atan": "ArcTan",
            "sinh": "Sinh", "cosh": "Cosh", "tanh": "Tanh", "coth": "Coth", "sech": "Sech", "csch": "Csch",
            "exp": "Exp", "log": "Log", "sqrt": "Sqrt",
            "root": "Surd",
            "Abs": "Abs", "abs": "Abs", "floor": "Floor", "ceiling": "Ceiling",
        }
        function = functions.get(node.func.id)
        if function is None:
            raise TypeError("unsupported Wolfram output function")
        args = [_render_wolfram_node(arg) for arg in node.args]
        if function == "Log" and len(args) == 2:
            args.reverse()
        return f"{function}[{', '.join(args)}]"
    if isinstance(node, (ast.List, ast.Tuple)):
        return "{" + ", ".join(_render_wolfram_node(item) for item in node.elts) + "}"
    raise TypeError("unsupported Wolfram output expression")


def _wolfram_symbol(name: str) -> str:
    if re.fullmatch(r"[A-Za-z$][A-Za-z0-9$]*", name):
        return name
    return f"Symbol[{json.dumps(name, ensure_ascii=False)}]"


def _wolfram_number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    number = format(value, ".17g")
    return re.sub(r"[eE]([+-]?\d+)$", r"*^\1", number)


def _wolfram_value(value: Any) -> str | None:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return _wolfram_number(value)
    if isinstance(value, str):
        return _wolfram_expression(value)
    if isinstance(value, list):
        if all(isinstance(solution, dict) for solution in value):
            rendered = []
            for solution in value:
                rules = []
                for name, result in solution.items():
                    value_expression = _wolfram_value(result)
                    if value_expression is None:
                        return None
                    rules.append(f"{_wolfram_symbol(str(name))} -> {value_expression}")
                rendered.append("{" + ", ".join(rules) + "}")
            return "{" + ", ".join(rendered) + "}"
        rendered_items = [_wolfram_value(item) for item in value]
        if any(item is None for item in rendered_items):
            return None
        return "{" + ", ".join(item for item in rendered_items if item is not None) + "}"
    return None


def _select_pods(
    pods: list[dict[str, Any]],
    include_pod_ids: list[str] | None,
    exclude_pod_ids: list[str] | None,
    pod_titles: list[str] | None,
    pod_indexes: list[str] | None,
    scanners: list[str] | None,
) -> list[dict[str, Any]]:
    includes = _split_values(include_pod_ids or [])
    excludes = _split_values(exclude_pod_ids or [])
    titles = _split_values(pod_titles or [])
    scanner_names = _split_values(scanners or [])
    indexes = _parse_indexes(pod_indexes or [])
    if includes and excludes:
        raise FullResultsRequestError("includepodid and excludepodid cannot be combined")

    has_positive_filter = bool(includes or titles or indexes or scanner_names)
    selected: list[dict[str, Any]] = []
    for index, pod in enumerate(pods, start=1):
        if has_positive_filter:
            title_match = any(
                fnmatch.fnmatchcase(str(pod["title"]).casefold(), pattern.casefold())
                for pattern in titles
            )
            matches = (
                (str(pod["id"]) in includes)
                or title_match
                or index in indexes
                or str(pod["scanner"]).casefold() in {name.casefold() for name in scanner_names}
            )
            if not matches:
                continue
        if str(pod["id"]) in excludes:
            continue
        selected.append(pod)
    return selected


def filter_response_pods(
    payload: dict[str, Any],
    include_pod_ids: list[str] | None,
    exclude_pod_ids: list[str] | None,
    pod_titles: list[str] | None,
    pod_indexes: list[str] | None,
    scanners: list[str] | None,
) -> dict[str, Any]:
    result = payload["queryresult"]
    result["pods"] = _select_pods(
        result.get("pods", []),
        include_pod_ids,
        exclude_pod_ids,
        pod_titles,
        pod_indexes,
        scanners,
    )
    result["numpods"] = len(result["pods"])
    return payload


def _parse_indexes(values: list[str]) -> set[int]:
    parts = _split_values(values)
    indexes: set[int] = set()
    for value in parts:
        try:
            index = int(value)
        except ValueError as exc:
            raise FullResultsRequestError("podindex values must be positive integers") from exc
        if index < 1:
            raise FullResultsRequestError("podindex values must be positive integers")
        indexes.add(index)
    return indexes


def _decimal_state_level(values: list[str]) -> int:
    level = 0
    max_level = len(_DECIMAL_APPROXIMATION_PRECISIONS) - 1
    for value in values:
        repeat = 1
        state = value
        if "@" in value:
            repeat_text, state = value.split("@", 1)
            if not repeat_text.isdecimal() or int(repeat_text) < 1:
                raise FullResultsRequestError("podstate repeat count must be a positive integer")
            repeat = int(repeat_text)
            if repeat > max_level:
                raise FullResultsRequestError(
                    f"podstate supports at most {max_level} decimal precision increases per request"
                )
        if state not in {_DECIMAL_MORE_STATE, _DECIMAL_FEWER_STATE}:
            raise FullResultsRequestError(
                "supported podstate values are DecimalApproximation__More digits and "
                "DecimalApproximation__Fewer digits"
            )
        for _ in range(repeat):
            if state == _DECIMAL_MORE_STATE:
                if level == max_level:
                    raise FullResultsRequestError("decimal approximation is already at maximum precision")
                level += 1
            else:
                level = max(0, level - 1)
    return level


def _decimal_states_for_level(level: int) -> list[dict[str, str]]:
    states: list[dict[str, str]] = []
    if level > 0:
        states.append({"name": "Fewer digits", "input": _DECIMAL_FEWER_STATE})
    if level < len(_DECIMAL_APPROXIMATION_PRECISIONS) - 1:
        states.append({"name": "More digits", "input": _DECIMAL_MORE_STATE})
    return states


def _split_values(values: list[str]) -> list[str]:
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


def _seconds(value: float) -> float:
    return round(max(value, 0.0), 6)


def _wolfram_cell(text: str) -> str:
    """Wrap display text in a safe, plain Wolfram Language output cell."""
    safe_text = _xml_safe_text(text)
    box_text = json.dumps(safe_text, ensure_ascii=False)
    return (
        "Cell[BoxData[FormBox[RowBox[{"
        f"{box_text}"
        "}], TraditionalForm]], \"Output\"]"
    )


def _mathml(text: str) -> str:
    escaped = html.escape(text, quote=False)
    return (
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        f"<mtext>{escaped}</mtext></math>"
    )


def _payload_to_xml(payload: dict[str, Any]) -> str:
    root_name, data = next(iter(payload.items()))
    root = ET.Element(root_name)
    special = {
        "pods", "assumptions", "warnings", "tips", "error_detail", "evidence",
        "interpretation", "classification", "qualification", "sources", "data", "retrieved_at",
    }
    for key, value in data.items():
        if key in special:
            continue
        if isinstance(key, str) and _XML_NAME.fullmatch(key) and isinstance(
            value, (str, int, float, bool)
        ):
            serialized = _xml_value(value)
            if _xml_safe_text(serialized) == serialized:
                root.set(key, serialized)
            else:
                _append_xml_extension(root, key, value)
        else:
            _append_xml_extension(root, key, value)
    if "pods" in data:
        for pod in data["pods"]:
            _append_pod(root, pod)
    if data.get("assumptions") is not None:
        _append_assumptions(root, data["assumptions"])
    if data.get("sources") is not None:
        _append_sources(root, data["sources"])
    if data.get("data") is not None:
        element = ET.SubElement(root, "data", {"encoding": "json"})
        element.text = json.dumps(data["data"], ensure_ascii=False, separators=(",", ":"))
    if data.get("retrieved_at") is not None:
        element = ET.SubElement(root, "retrieved_at")
        element.text = _xml_safe_text(str(data["retrieved_at"]))
    _append_text_items(root, "warnings", data.get("warnings"))
    _append_text_items(root, "tips", data.get("tips"))
    if data.get("error_detail"):
        error = ET.SubElement(root, "error")
        for key, value in data["error_detail"].items():
            if key in {"code", "msg"}:
                _append_xml_text_element(error, key, value)
            else:
                _append_xml_field(error, key, value)
    for name in ("interpretation", "classification", "evidence"):
        if data.get(name) is not None:
            child = ET.SubElement(root, name)
            for key, value in data[name].items():
                if (
                    isinstance(key, str)
                    and _XML_NAME.fullmatch(key)
                    and isinstance(value, (str, int, float, bool))
                    and _xml_safe_text(_xml_value(value)) == _xml_value(value)
                ):
                    child.set(key, _xml_value(value))
                else:
                    _append_xml_extension(child, key, value)
    if data.get("qualification"):
        child = ET.SubElement(root, "qualification")
        child.text = _xml_safe_text(str(data["qualification"]))
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)


def _append_pod(root: ET.Element, pod: dict[str, Any]) -> None:
    attributes: dict[str, str] = {}
    extensions: list[tuple[object, Any]] = []
    for key, value in pod.items():
        if key in {"subpods", "states", "evidence"}:
            continue
        if (
            isinstance(key, str)
            and _XML_NAME.fullmatch(key)
            and isinstance(value, (str, int, float, bool))
        ):
            serialized = _xml_value(value)
            if _xml_safe_text(serialized) == serialized:
                attributes[key] = serialized
            else:
                extensions.append((key, value))
        else:
            extensions.append((key, value))
    element = ET.SubElement(root, "pod", attributes)
    for key, value in extensions:
        _append_xml_extension(element, key, value)
    for subpod in pod.get("subpods", []):
        subpod_element = ET.SubElement(
            element,
            "subpod",
            {"title": _xml_safe_text(str(subpod.get("title", "")))},
        )
        for key, value in subpod.items():
            if key == "title":
                continue
            if key == "mathml":
                try:
                    subpod_element.append(ET.fromstring(str(value)))
                except ET.ParseError:
                    mathml = ET.SubElement(subpod_element, "mathml")
                    mathml.text = _xml_safe_text(str(value))
            elif key == "data":
                child = ET.SubElement(subpod_element, "data", {"encoding": "json"})
                child.text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            elif key == "states":
                _append_states(subpod_element, value)
            elif key == "img" and isinstance(value, dict):
                attributes = {
                    name: _xml_safe_text(_xml_value(value[name]))
                    for name in ("src", "alt", "title", "width", "height")
                    if name in value
                }
                ET.SubElement(subpod_element, "img", attributes)
            elif key in {"plaintext", "minput", "moutput", "cell"}:
                _append_xml_text_element(subpod_element, key, value)
            else:
                _append_xml_field(subpod_element, key, value)
    _append_states(element, pod.get("states", []))


def _append_states(parent: ET.Element, states: list[dict[str, str]]) -> None:
    if not states:
        return
    state_container = ET.SubElement(parent, "states", {"count": str(len(states))})
    for state in states:
        attributes = {
            key: _xml_safe_text(str(value))
            for key, value in state.items()
            if key in {"name", "input"}
        }
        element = ET.SubElement(state_container, "state", attributes)
        for key, value in state.items():
            if key not in {"name", "input"}:
                _append_xml_field(element, key, value)


def _append_assumptions(root: ET.Element, assumptions: list[dict[str, Any]]) -> None:
    parent = ET.SubElement(root, "assumptions", {"count": str(len(assumptions))})
    for assumption in assumptions:
        attributes = {
            key: _xml_safe_text(_xml_value(value))
            for key, value in assumption.items()
            if key not in {"values", "occurrences"}
            and isinstance(key, str)
            and _XML_NAME.fullmatch(key)
            and isinstance(value, (str, int, float, bool))
        }
        item = ET.SubElement(parent, "assumption", attributes)
        for key, content in assumption.items():
            if key not in {"values", "occurrences"} and key not in attributes:
                _append_xml_field(item, key, content)
        for value in assumption.get("values", []):
            value_attributes = {
                key: _xml_safe_text(_xml_value(content))
                for key, content in value.items()
                if isinstance(key, str)
                and _XML_NAME.fullmatch(key)
                and isinstance(content, (str, int, float, bool))
            }
            value_element = ET.SubElement(item, "value", value_attributes)
            for key, content in value.items():
                if key not in value_attributes:
                    _append_xml_field(value_element, key, content)
        for occurrence in assumption.get("occurrences", []):
            if not isinstance(occurrence, dict):
                continue
            occurrence_attributes = {
                key: _xml_safe_text(_xml_value(content))
                for key, content in occurrence.items()
                if isinstance(key, str)
                and _XML_NAME.fullmatch(key)
                and isinstance(content, (str, int, float, bool))
            }
            occurrence_element = ET.SubElement(item, "occurrence", occurrence_attributes)
            for key, content in occurrence.items():
                if key not in occurrence_attributes:
                    _append_xml_field(occurrence_element, key, content)


def _append_sources(root: ET.Element, sources: list[dict[str, Any]]) -> None:
    parent = ET.SubElement(root, "sources", {"count": str(len(sources))})
    for source in sources:
        attributes = {
            key: _xml_safe_text(str(value))
            for key, value in source.items()
            if isinstance(key, str)
            and _XML_NAME.fullmatch(key)
            and isinstance(value, (str, int, float, bool))
        }
        element = ET.SubElement(parent, "source", attributes)
        for key, value in source.items():
            if key not in attributes:
                _append_xml_field(element, key, value)


def _append_text_items(root: ET.Element, name: str, items: list[dict[str, str]] | None) -> None:
    if not items:
        return
    parent = ET.SubElement(root, name, {"count": str(len(items))})
    for item in items:
        element_name = name[:-1] if name.endswith("s") else name
        attributes = {
            key: _xml_safe_text(str(value))
            for key, value in item.items()
            if isinstance(key, str)
            and _XML_NAME.fullmatch(key)
            and isinstance(value, (str, int, float, bool))
        }
        element = ET.SubElement(parent, element_name, attributes)
        for key, value in item.items():
            if key not in attributes:
                _append_xml_field(element, key, value)


def _append_xml_field(parent: ET.Element, name: object, value: Any) -> None:
    _append_xml_extension(parent, name, value)


def _append_xml_text_element(parent: ET.Element, name: object, value: Any) -> None:
    if isinstance(name, str) and _XML_NAME.fullmatch(name) and isinstance(
        value, (str, int, float, bool)
    ):
        serialized = _xml_value(value)
        if _xml_safe_text(serialized) != serialized:
            _append_xml_extension(parent, name, value)
            return
        child = ET.SubElement(parent, name)
        child.text = serialized
        return
    _append_xml_extension(parent, name, value)


def _append_xml_extension(parent: ET.Element, name: object, value: Any) -> None:
    original_name = _xml_safe_text(str(name))
    valid_name = bool(_XML_NAME.fullmatch(original_name)) and not original_name.lower().startswith("xml")
    tag = original_name if valid_name else "field"
    attributes = {"encoding": "json"}
    if not valid_name:
        attributes["name"] = original_name
    element = ET.SubElement(parent, tag, attributes)
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = json.dumps(str(value), ensure_ascii=False, separators=(",", ":"))
    element.text = _xml_safe_text(encoded)


def _xml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
