from __future__ import annotations

import ast
import fnmatch
import html
import json
import re
from time import perf_counter
from typing import Any
from uuid import uuid4
import xml.etree.ElementTree as ET

from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from .interpretations import (
    AssumptionSelectionError,
    InterpretationError,
    query_with_interpretations,
)
from .knowledge import (
    KnowledgeProviderError,
    KnowledgeRequest,
    query_knowledge,
    supported_nist_constants,
)
from .symbolic import SymbolicRequest, run_symbolic


_API_VERSION = "2.0"
_SUPPORTED_FORMATS = {"plaintext", "mathml"}
_KNOWN_FORMATS = {
    "image", "imagemap", "plaintext", "minput", "moutput", "cell", "mathml", "sound", "wav"
}


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


def normalize_output(value: str) -> str:
    output = value.strip().lower()
    if output not in {"xml", "json"}:
        raise FullResultsRequestError("output must be either 'xml' or 'json'")
    return output


def normalize_formats(values: list[str] | None) -> tuple[list[str], list[str]]:
    if not values:
        return ["plaintext"], []
    formats = _split_values(values)
    if not formats:
        raise FullResultsRequestError("format must contain at least one format name")
    unknown = sorted(set(formats) - _KNOWN_FORMATS)
    if unknown:
        raise FullResultsRequestError(f"unsupported format name: {', '.join(unknown)}")
    unique = list(dict.fromkeys(formats))
    omitted = [item for item in unique if item not in _SUPPORTED_FORMATS]
    return unique, omitted


def execute_query(
    input_text: str,
    assumptions: list[str] | None,
    formats: list[str],
    *,
    location: str | None = None,
    latlong: str | None = None,
    include_pod_ids: list[str] | None = None,
    exclude_pod_ids: list[str] | None = None,
    pod_titles: list[str] | None = None,
    pod_indexes: list[str] | None = None,
    scanners: list[str] | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    provider_parse_started = perf_counter()
    try:
        provider_request = _provider_request(input_text, location, latlong)
    except ProviderContextNeeded as exc:
        return _unrecognized_query(input_text, str(exc), perf_counter() - started)
    provider_parse_timing = perf_counter() - provider_parse_started
    if provider_request is not None:
        if assumptions:
            raise AssumptionSelectionError(
                "mathematical assumption selections do not apply to this data query"
            )
        return _execute_provider_query(
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
        )
    try:
        interpreted, symbolic_request = _prepare_query(
            input_text, assumptions or [], validate_only=False
        )
    except AssumptionSelectionError:
        raise
    except InterpretationError as exc:
        return _unrecognized_query(input_text, str(exc), perf_counter() - started)

    parse_timing = float(interpreted["parse_timing"])
    expression = interpreted["interpretation"]["expression"]
    if interpreted.get("outcome") == "computed":
        symbolic = {
            "result": interpreted.get("result"),
            "input_mathml": interpreted.get("input_mathml"),
            "result_mathml": interpreted.get("result_mathml"),
            "evidence": interpreted.get("evidence"),
        }
    elif interpreted.get("outcome") == "timeout":
        symbolic = {"timed_out": True, "evidence": interpreted.get("evidence")}
    else:
        symbolic = run_symbolic(symbolic_request)
        parse_timing += float(symbolic.get("parse_timing_seconds", 0.0))
    elapsed = perf_counter() - started

    input_mathml = interpreted.get("input_mathml") if interpreted.get("outcome") == "computed" else None
    pods = _initial_pods(input_text, expression, None, formats, input_mathml=input_mathml)
    warnings = _format_warnings(formats)
    if symbolic.get("timed_out"):
        payload = _query_envelope(
            success=False,
            error=False,
            pods=pods,
            elapsed=elapsed,
            parse_timing=parse_timing,
            datatypes="Mathematical",
            timedout="Mathematical",
            timedoutpods="Result",
            warnings=[*warnings, {"text": "The symbolic result exceeded its execution time limit."}],
            assumptions=interpreted["assumptions"],
            interpretation=interpreted["interpretation"],
            classification=interpreted["classification"],
            evidence={"kind": "symbolic_timeout", "engine": "sympy", "verified": False},
            qualification="A timeout contains no result and is not a proof.",
        )
        payload["queryresult"]["input"] = input_text
        return _filter_response_pods(
            payload, include_pod_ids, exclude_pod_ids, pod_titles, pod_indexes, scanners
        )

    if symbolic.get("error"):
        if "diagnostics" in symbolic:
            raise FullResultsBackendError(str(symbolic.get("error")))
        return _unrecognized_query(input_text, str(symbolic.get("error")), elapsed)

    result_text = str(symbolic.get("result", ""))
    pods = _initial_pods(
        input_text,
        expression,
        result_text,
        formats,
        input_mathml=input_mathml,
        result_mathml=symbolic.get("result_mathml"),
    )
    filtered = _select_pods(
        pods, include_pod_ids, exclude_pod_ids, pod_titles, pod_indexes, scanners
    )
    payload = _query_envelope(
        success=True,
        error=False,
        pods=filtered,
        elapsed=elapsed,
        parse_timing=parse_timing,
        datatypes="Mathematical",
        warnings=warnings,
        assumptions=interpreted["assumptions"],
        interpretation=interpreted["interpretation"],
        classification=interpreted["classification"],
        evidence=symbolic.get("evidence", {
            "kind": "symbolic_result", "engine": "sympy", "verified": False
        }),
        qualification="Symbolic computation is not a Lean proof.",
    )
    payload["queryresult"]["input"] = input_text
    return payload


def validate_query(
    input_text: str,
    assumptions: list[str] | None,
    *,
    location: str | None = None,
    latlong: str | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    try:
        provider_request = _provider_request(input_text, location, latlong)
        if provider_request is not None:
            if assumptions:
                raise AssumptionSelectionError(
                    "mathematical assumption selections do not apply to this data query"
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
                    "assumptions": [],
                    "interpretation": {
                        "kind": "typed_data_request",
                        "provider": provider_name,
                        "operation": provider_request.operation,
                        "location": provider_request.location,
                    },
                }
            }
        interpreted, _ = _prepare_query(input_text, assumptions or [], validate_only=True)
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
    except InterpretationError as exc:
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


def _provider_request(
    input_text: str,
    location: str | None,
    latlong: str | None,
) -> KnowledgeRequest | None:
    weather = re.fullmatch(
        r"\s*(?:(?:what\s+is|what's)\s+)?(?:the\s+)?(?:current\s+)?weather"
        r"(?:\s+(?:in|at|for)\s+(.+?))?\s*[?.!]*\s*",
        input_text,
        re.IGNORECASE,
    )
    if weather:
        embedded_location = (weather.group(1) or "").strip()
        if embedded_location and (location or latlong):
            raise FullResultsRequestError(
                "specify weather location in either input, location, or latlong, not more than one"
            )
        if location and latlong:
            raise FullResultsRequestError("location and latlong cannot be combined")
        if embedded_location:
            return _knowledge_request(
                provider="open_meteo", operation="weather", location=embedded_location
            )
        if location:
            return _knowledge_request(
                provider="open_meteo", operation="weather", location=location.strip()
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
                operation="weather",
                latitude=latitude,
                longitude=longitude,
            )
        raise ProviderContextNeeded(
            "weather needs an explicit location; use input='weather in <place>', location, or latlong"
        )

    if location is not None or latlong is not None:
        raise FullResultsRequestError("location and latlong are currently supported for weather queries")

    entity = re.fullmatch(r"\s*(?:entity|wikidata)\s*:\s*(.+?)\s*[?.!]*\s*", input_text, re.I)
    if entity:
        query = entity.group(1).strip()
        if not query:
            raise FullResultsRequestError("entity search needs a non-empty query")
        return _knowledge_request(provider="wikidata", operation="search", query=query)

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
) -> dict[str, Any]:
    try:
        knowledge = query_knowledge(request)
    except KnowledgeProviderError as exc:
        raise FullResultsBackendError(
            str(exc), status_code=exc.status_code, error_code="PROVIDER_FAILURE"
        ) from exc

    provider_presentation = {
        "open_meteo": ("Current weather", "Weather", "Weather"),
        "nist_codata": ("Physical constant", "PhysicalConstant", "ScientificConstant"),
        "wikidata": ("Entity matches", "EntitySearch", "Entity"),
    }
    title, pod_id, datatype = provider_presentation[request.provider]
    plaintext = _provider_plaintext(request.provider, knowledge["data"])
    selected_formats = formats or ["plaintext"]
    input_subpod: dict[str, Any] = {"title": ""}
    result_subpod: dict[str, Any] = {"title": "", "data": knowledge["data"]}
    if "plaintext" in selected_formats:
        input_subpod["plaintext"] = input_text
        result_subpod["plaintext"] = plaintext
    if "mathml" in selected_formats:
        input_subpod["mathml"] = _mathml(input_text)
        result_subpod["mathml"] = _mathml(plaintext)
    pods = [
        {
            "title": "Input interpretation",
            "scanner": "Identity",
            "id": "Input",
            "position": 100,
            "error": False,
            "numsubpods": 1,
            "primary": False,
            "subpods": [input_subpod],
        },
        {
            "title": title,
            "scanner": "Data",
            "id": pod_id,
            "position": 200,
            "error": False,
            "numsubpods": 1,
            "primary": True,
            "subpods": [result_subpod],
        },
    ]
    filtered = _select_pods(
        pods, include_pod_ids, exclude_pod_ids, pod_titles, pod_indexes, scanners
    )
    elapsed = perf_counter() - started
    payload = _query_envelope(
        success=True,
        error=False,
        pods=filtered,
        elapsed=elapsed,
        parse_timing=parse_timing,
        datatypes=datatype,
        warnings=_format_warnings(formats),
        assumptions=[],
        interpretation={
            "kind": "typed_data_request",
            "provider": knowledge["provider"],
            "operation": knowledge["operation"],
            "request": request.model_dump(exclude_none=True),
        },
        classification={
            "name": "deterministic_provider_router",
            "method": "explicit query pattern or supported constant alias",
            "ranks_candidates_only": True,
            "selection_authority": False,
        },
        evidence=knowledge["evidence"],
        qualification=knowledge["qualification"],
        sources=knowledge["sources"],
        retrieved_at=knowledge["retrieved_at"],
        data=knowledge["data"],
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
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def _prepare_query(
    input_text: str,
    assumptions: list[str],
    *,
    validate_only: bool,
) -> tuple[dict[str, Any], SymbolicRequest]:
    try:
        operation_request = _parse_operation_call(input_text)
    except ValidationError as exc:
        raise InterpretationError("symbolic operation arguments are invalid") from exc
    if operation_request is None:
        interpreted = query_with_interpretations(
            input_text, assumptions, compute=not validate_only
        )
        expression = str(interpreted["interpretation"]["expression"])
        symbolic_request = SymbolicRequest(
            operation="simplify",
            expression=expression,
            time_limit_seconds=5,
        )
        return interpreted, symbolic_request

    if assumptions:
        raise AssumptionSelectionError("assumption selection is not applicable to this operation")

    parse_started = perf_counter()
    expressions = [
        value for value in (
            operation_request.expression,
            operation_request.lhs,
            operation_request.rhs,
            operation_request.lower,
            operation_request.upper,
        ) if value is not None
    ]
    parse_timing = perf_counter() - parse_started
    if validate_only and expressions:
        combined_expression = " + ".join(f"({value})" for value in expressions)
        parsed = run_symbolic(SymbolicRequest(operation="validate", expression=combined_expression))
        if parsed.get("timed_out"):
            raise InterpretationError("input parsing exceeded its time limit")
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
        },
        "classification": {
            "name": "not_applicable",
            "method": "explicit operation syntax",
            "ranks_candidates_only": True,
        },
    }
    return interpreted, operation_request


def _parse_operation_call(query: str) -> SymbolicRequest | None:
    try:
        node = ast.parse(query, mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None
    operation_name = node.func.id.lower()
    aliases = {
        "simplify": "simplify",
        "differentiate": "differentiate",
        "diff": "differentiate",
        "integrate": "integrate",
        "integral": "integrate",
        "solve": "solve",
        "identity": "identity",
    }
    operation = aliases.get(operation_name)
    if operation is None:
        return None
    if node.keywords:
        raise InterpretationError("symbolic operation calls do not accept keyword arguments")
    args = node.args

    def expression_at(index: int) -> str:
        if index >= len(args):
            raise InterpretationError(f"{operation_name} has too few arguments")
        return ast.unparse(args[index])

    if operation == "simplify":
        if len(args) != 1:
            raise InterpretationError("simplify requires exactly one expression")
        return SymbolicRequest(
            operation="simplify",
            expression=expression_at(0),
            time_limit_seconds=5,
        )
    if operation == "identity":
        if len(args) != 2:
            raise InterpretationError("identity requires exactly two expressions")
        return SymbolicRequest(
            operation="identity",
            lhs=expression_at(0),
            rhs=expression_at(1),
            time_limit_seconds=5,
        )
    if operation in {"differentiate", "solve"}:
        if len(args) != 2 or not isinstance(args[1], ast.Name):
            raise InterpretationError(f"{operation_name} requires an expression and a variable name")
        return SymbolicRequest(
            operation=operation,
            expression=expression_at(0),
            variable=args[1].id,
            time_limit_seconds=5,
        )
    if len(args) not in {2, 4} or not isinstance(args[1], ast.Name):
        raise InterpretationError(
            "integrate requires expression and variable, with optional lower and upper bounds"
        )
    lower = expression_at(2) if len(args) == 4 else None
    upper = expression_at(3) if len(args) == 4 else None
    return SymbolicRequest(
        operation="integrate",
        expression=expression_at(0),
        variable=args[1].id,
        lower=lower,
        upper=upper,
        time_limit_seconds=5,
    )


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


def _unrecognized_query(input_text: str, message: str, elapsed: float) -> dict[str, Any]:
    result = _query_envelope(
        success=False,
        error=False,
        pods=[],
        elapsed=elapsed,
        parse_timing=elapsed,
        datatypes="",
        warnings=[],
        assumptions=[],
        tips=[{"text": message}],
        qualification="The input was not parsed; no result was computed.",
    )
    result["queryresult"]["input"] = input_text
    return result


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
) -> list[dict[str, Any]]:
    selected_formats = formats or ["plaintext"]
    subpod_input: dict[str, Any] = {"title": ""}
    subpod_result: dict[str, Any] = {"title": ""}
    if "plaintext" in selected_formats:
        subpod_input["plaintext"] = expression
        if result_text is not None:
            subpod_result["plaintext"] = result_text
    if "mathml" in selected_formats:
        subpod_input["mathml"] = input_mathml or _mathml(expression)
        if result_text is not None:
            subpod_result["mathml"] = result_mathml or _mathml(result_text)
    return [
        {
            "title": "Input interpretation",
            "scanner": "Identity",
            "id": "Input",
            "position": 100,
            "error": False,
            "numsubpods": 1,
            "primary": False,
            "subpods": [subpod_input],
        },
        {
            "title": "Result",
            "scanner": "Mathematical",
            "id": "Result",
            "position": 200,
            "error": False,
            "numsubpods": 1 if result_text is not None else 0,
            "primary": True,
            "subpods": [subpod_result] if result_text is not None else [],
        },
    ]


def _format_warnings(formats: list[str]) -> list[dict[str, str]]:
    omitted = [name for name in formats if name not in _SUPPORTED_FORMATS]
    if not omitted:
        return []
    return [{
        "text": "This service does not currently render these requested formats: "
        + ", ".join(omitted)
    }]


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


def _filter_response_pods(
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


def _split_values(values: list[str]) -> list[str]:
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


def _seconds(value: float) -> float:
    return round(max(value, 0.0), 6)


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
        if key in special or value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            root.set(key, _xml_value(value))
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
        element.text = str(data["retrieved_at"])
    _append_text_items(root, "warnings", data.get("warnings"))
    _append_text_items(root, "tips", data.get("tips"))
    if data.get("error_detail"):
        error = ET.SubElement(root, "error")
        for key, value in data["error_detail"].items():
            child = ET.SubElement(error, key)
            child.text = str(value)
    for name in ("interpretation", "classification", "evidence"):
        if data.get(name) is not None:
            child = ET.SubElement(root, name)
            for key, value in data[name].items():
                if isinstance(value, (str, int, float, bool)):
                    child.set(key, _xml_value(value))
                else:
                    nested = ET.SubElement(child, key)
                    nested.text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if data.get("qualification"):
        child = ET.SubElement(root, "qualification")
        child.text = str(data["qualification"])
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)


def _append_pod(root: ET.Element, pod: dict[str, Any]) -> None:
    attributes = {
        key: value for key, value in pod.items() if key not in {"subpods", "evidence"}
    }
    element = ET.SubElement(root, "pod", {key: _xml_value(value) for key, value in attributes.items()})
    for subpod in pod.get("subpods", []):
        subpod_element = ET.SubElement(element, "subpod", {"title": str(subpod.get("title", ""))})
        for key, value in subpod.items():
            if key == "title":
                continue
            if key == "mathml":
                try:
                    subpod_element.append(ET.fromstring(str(value)))
                except ET.ParseError:
                    mathml = ET.SubElement(subpod_element, "mathml")
                    mathml.text = str(value)
            elif key == "data":
                child = ET.SubElement(subpod_element, "data", {"encoding": "json"})
                child.text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            else:
                child = ET.SubElement(subpod_element, key)
                child.text = str(value)


def _append_assumptions(root: ET.Element, assumptions: list[dict[str, Any]]) -> None:
    parent = ET.SubElement(root, "assumptions", {"count": str(len(assumptions))})
    for assumption in assumptions:
        attributes = {
            key: _xml_value(value)
            for key, value in assumption.items()
            if key != "values" and isinstance(value, (str, int, float, bool))
        }
        item = ET.SubElement(parent, "assumption", attributes)
        for value in assumption.get("values", []):
            value_attributes = {
                key: _xml_value(content)
                for key, content in value.items()
                if isinstance(content, (str, int, float, bool))
            }
            ET.SubElement(item, "value", value_attributes)


def _append_sources(root: ET.Element, sources: list[dict[str, Any]]) -> None:
    parent = ET.SubElement(root, "sources", {"count": str(len(sources))})
    for source in sources:
        attributes = {
            key: str(value)
            for key, value in source.items()
            if isinstance(value, (str, int, float, bool))
        }
        ET.SubElement(parent, "source", attributes)


def _append_text_items(root: ET.Element, name: str, items: list[dict[str, str]] | None) -> None:
    if not items:
        return
    parent = ET.SubElement(root, name, {"count": str(len(items))})
    for item in items:
        ET.SubElement(parent, name[:-1] if name.endswith("s") else name, {
            key: str(value) for key, value in item.items()
        })


def _xml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
