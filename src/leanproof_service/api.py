from __future__ import annotations

import hmac
import hashlib
import json
import os
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from .computation import ComputeRequest, Computation, check_computation, prepare_computation
from .full_results import (
    FullResultsBackendError,
    FullResultsRequestError,
    execute_query,
    normalize_formats,
    normalize_output,
    query_error,
    render_response,
    validate_error,
    validate_query,
)
from .interpretations import (
    AssumptionSelectionError,
    InterpretationError,
    assumption_registry,
    query_with_interpretations,
)
from .knowledge import KnowledgeProviderError, KnowledgeRequest, query_knowledge
from .lean_backend import lake_executable
from .models import ClaimRequest
from .reasoner import analyze
from .symbolic import SymbolicRequest, run_symbolic


app = FastAPI(
    title="Lean Proof Service",
    version="0.1.0",
    description=(
        "Check caller-supplied typed arithmetic claims with Z3 and, when configured, "
        "Lean 4. A solver result is never labeled as a Lean proof. Candidate assumptions "
        "are ranked separately and resolved through a versioned registry."
    ),
)
bearer = HTTPBearer(auto_error=False)


def require_api_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    expected = os.environ.get("API_BEARER_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_BEARER_TOKEN is not configured",
        )
    if not credentials or credentials.scheme.lower() != "bearer" or not hmac.compare_digest(
        credentials.credentials, expected
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict[str, object]:
    import z3
    import sympy

    return {
        "status": "ready",
        "solver": {"name": "z3", "version": z3.get_version_string()},
        "symbolic": {"name": "sympy", "version": sympy.__version__},
        "lean": {"installed": lake_executable() is not None, "project_configured": _lean_available()},
    }


@app.get("/v1/capabilities")
def capabilities() -> dict[str, object]:
    return {
        "formalization": "caller_supplied_typed_specification",
        "sorts": ["Int", "Real"],
        "fragment": "linear arithmetic with equality and ordered comparisons",
        "backends": {
            "sympy": "implemented",
            "z3": "implemented",
            "lean_kernel": "configured" if _lean_available() else "not_configured",
        },
        "symbolic_operations": ["validate", "simplify", "differentiate", "integrate", "solve", "identity"],
        "interpretations": {
            "status": "implemented_initial_slice",
            "path": "/v1/interpret",
            "assumption_registry_version": assumption_registry()["registry_version"],
            "candidate_generator": "deterministic_allowlisted_math_parser",
            "classifier": "deterministic_lexical_baseline; optional Decima ranking via ASSUMPTION_CLASSIFIER=decima",
            "validator": "typed registry ID and explicit-unit compatibility checks",
            "classifier_selects_assumptions": False,
        },
        "factual_sources": {
            "wikidata": {"coverage": "entity search and entity records", "status": "implemented"},
            "dbnomics": {"coverage": "individual time series", "status": "implemented"},
            "nist_codata": {"coverage": "bundled CODATA 2022 constants subset", "status": "implemented"},
            "geonames": {"coverage": "place-name search; requires GEONAMES_USERNAME", "status": "implemented"},
            "open_meteo": {"coverage": "current weather by location or coordinates", "status": "implemented"},
        },
        "compatibility": {
            "wolfram_alpha_full_results": {
                "path": "/v2/query",
                "status": "local_partial_contract",
                "outputs": ["json", "xml"],
                "query_classes": [
                    "safe symbolic expressions",
                    "simplify/differentiate/integrate/solve/identity calls",
                    "weather location queries",
                    "supported CODATA constants",
                    "explicit Wikidata entity searches",
                ],
                "location_parameters": ["location", "latlong"],
                "validation_path": "/v2/validatequery",
            },
            "wolfram_language_compute": {
                "path": "/api/cag/v1/WolframLanguageCompute",
                "status": "allowlisted_subset",
                "expressions": ["Sin[Pi]", "TrigExpand@Sin[2x]", "Integrate[x, {x, a, b}]"]
            }
        },
        "outcomes": [
            "verified",
            "solver_unsat",
            "counterexample_found",
            "inconsistent_assumptions",
            "unknown",
            "timeout",
            "error",
        ],
        "limits": {
            "variables": 64,
            "assumptions": 64,
            "expression_nodes": 4096,
            "wall_seconds_max": 30,
        },
    }


@app.post("/v1/prove", dependencies=[Depends(require_api_token)])
def prove(request: ClaimRequest) -> dict[str, object]:
    return analyze(request)


@app.post("/v1/counterexample", dependencies=[Depends(require_api_token)])
def counterexample(request: ClaimRequest) -> dict[str, object]:
    return analyze(request, attempt_lean=False)


@app.post("/v1/symbolic", dependencies=[Depends(require_api_token)])
def symbolic(request: SymbolicRequest) -> dict[str, object]:
    result = run_symbolic(request)
    if result.get("timed_out"):
        return {
            "outcome": "timeout",
            "operation": request.operation,
            "time_limit_seconds": result.get("limit_seconds"),
            "evidence": {"kind": "symbolic_timeout", "engine": "sympy", "verified": False},
        }
    if result.get("error"):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY if "diagnostics" not in result else status.HTTP_500_INTERNAL_SERVER_ERROR
        raise HTTPException(status_code=code, detail=result)
    return result


@app.get("/v2/assumptions", dependencies=[Depends(require_api_token)])
def list_assumptions() -> dict[str, object]:
    return assumption_registry()


@app.get("/v1/interpret", dependencies=[Depends(require_api_token)])
def interpretation_query(
    input: Annotated[str, Query(min_length=1, max_length=4000)],
    assumption: Annotated[list[str] | None, Query()] = None,
) -> dict[str, object]:
    try:
        return query_with_interpretations(input, assumption or [])
    except InterpretationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@app.get("/v2/query", dependencies=[Depends(require_api_token)])
def full_results_query(
    input: Annotated[str | None, Query()] = None,
    output: Annotated[str, Query()] = "xml",
    format: Annotated[list[str] | None, Query()] = None,
    assumption: Annotated[list[str] | None, Query()] = None,
    includepodid: Annotated[list[str] | None, Query()] = None,
    excludepodid: Annotated[list[str] | None, Query()] = None,
    podtitle: Annotated[list[str] | None, Query()] = None,
    podindex: Annotated[list[str] | None, Query()] = None,
    scanner: Annotated[list[str] | None, Query()] = None,
    location: Annotated[str | None, Query()] = None,
    latlong: Annotated[str | None, Query()] = None,
) -> Response:
    try:
        selected_output = normalize_output(output)
    except FullResultsRequestError as exc:
        response = render_response(query_error(input, str(exc)), "json")
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response

    if input is None or not input.strip() or len(input) > 4000:
        response = render_response(
            query_error(input, "input is required and must contain 1 to 4000 characters"),
            selected_output,
        )
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response

    try:
        formats, _ = normalize_formats(format)
        payload = execute_query(
            input,
            assumption,
            formats,
            location=location,
            latlong=latlong,
            include_pod_ids=includepodid,
            exclude_pod_ids=excludepodid,
            pod_titles=podtitle,
            pod_indexes=podindex,
            scanners=scanner,
        )
    except (AssumptionSelectionError, FullResultsRequestError) as exc:
        response = render_response(query_error(input, str(exc)), selected_output)
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response
    except FullResultsBackendError as exc:
        response = render_response(
            query_error(input, str(exc), code=exc.error_code),
            selected_output,
        )
        response.status_code = exc.status_code
        return response
    return render_response(payload, selected_output)


@app.get("/v2/validatequery", dependencies=[Depends(require_api_token)])
def full_results_validatequery(
    input: Annotated[str | None, Query()] = None,
    output: Annotated[str, Query()] = "xml",
    assumption: Annotated[list[str] | None, Query()] = None,
    location: Annotated[str | None, Query()] = None,
    latlong: Annotated[str | None, Query()] = None,
) -> Response:
    try:
        selected_output = normalize_output(output)
    except FullResultsRequestError as exc:
        response = render_response(validate_error(input, str(exc)), "json")
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response

    if input is None or not input.strip() or len(input) > 4000:
        response = render_response(
            validate_error(input, "input is required and must contain 1 to 4000 characters"),
            selected_output,
        )
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response
    try:
        payload = validate_query(input, assumption, location=location, latlong=latlong)
    except AssumptionSelectionError as exc:
        response = render_response(validate_error(input, str(exc)), selected_output)
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response
    except FullResultsRequestError as exc:
        response = render_response(validate_error(input, str(exc)), selected_output)
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response
    return render_response(payload, selected_output)


@app.post("/v1/knowledge/query", dependencies=[Depends(require_api_token)])
def knowledge_query(request: KnowledgeRequest) -> dict[str, object]:
    try:
        return query_knowledge(request)
    except KnowledgeProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post(
    "/api/cag/v1/WolframLanguageCompute",
    summary="Compute a supported Wolfram Language expression",
    description=(
        "Authorization accepts the configured API key directly or as a bearer token. "
        "Only allowlisted expressions are compiled into generated Lean proofs."
    ),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["code"],
                        "properties": {
                            "code": {"type": "string", "maxLength": 4000},
                            "line": {"type": "integer", "default": 1},
                            "maxChars": {"type": "integer", "default": 10000},
                            "timeConstraint": {"type": "integer", "default": 60},
                        },
                        "additionalProperties": False,
                    }
                }
            },
        }
    },
)
async def wolfram_language_compute(request: Request) -> JSONResponse:
    request_id = str(uuid4())
    expected = os.environ.get("API_BEARER_TOKEN")
    authorization = request.headers.get("authorization", "").strip()
    supplied = authorization[7:].strip() if authorization.lower().startswith("bearer ") else authorization
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        return _compat_response(403, "Missing or invalid API key", request_id)

    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _compat_response(400, "Request body must be valid JSON", request_id)
    try:
        compute_request = ComputeRequest.model_validate(payload)
    except ValidationError as exc:
        errors = "; ".join(error["msg"] for error in exc.errors())
        return _compat_response(400, f"Invalid request: {errors}", request_id)

    computation = prepare_computation(compute_request.code)
    if computation is None:
        return _compat_response(501, "This Wolfram Language expression is not supported", request_id)

    effective_budget = min(compute_request.timeConstraint, 30)
    lean_result = await _run_computation(computation, effective_budget)
    if lean_result.status == "unavailable":
        return _compat_response(503, "Lean proof backend is not ready", request_id)
    if lean_result.status == "timeout":
        return _compat_response(
            200,
            f'Failure["EvaluationTimeExceeded", {effective_budget}]',
            request_id,
            success=True,
        )
    if lean_result.status != "checked":
        return _compat_response(
            500,
            "Lean rejected the generated computation proof",
            request_id,
            evidence={"lean_status": lean_result.status, "diagnostics": lean_result.diagnostics},
        )

    result_text = _format_wolfram_output(compute_request.line, computation.display_result)
    result_text = _limit_output(result_text, compute_request.maxChars)
    body = {
        "result": result_text,
        "code": 200,
        "success": True,
        "uuid": request_id,
        "evidence": {
            "kind": "lean_kernel_checked",
            "lean_status": lean_result.status,
            "axiom_audit": {"status": "passed", "dependencies": list(lean_result.axioms)},
            "lean_source_sha256": hashlib.sha256(lean_result.source.encode("utf-8")).hexdigest(),
            "lean_source": lean_result.source,
            "time_constraint_requested": compute_request.timeConstraint,
            "time_constraint_applied": effective_budget,
        },
    }
    return JSONResponse(status_code=200, content=body)


async def _run_computation(computation: Computation, timeout_seconds: int):
    import asyncio

    return await asyncio.to_thread(check_computation, computation, timeout_seconds)


def _compat_response(
    code: int,
    message: str,
    request_id: str,
    *,
    success: bool = False,
    evidence: dict[str, object] | None = None,
) -> JSONResponse:
    body = {"result": message, "code": code, "success": success, "uuid": request_id}
    if evidence is not None:
        body["evidence"] = evidence
    return JSONResponse(
        status_code=code,
        content=body,
    )


def _format_wolfram_output(line: int, value: str) -> str:
    spacing = "" if all(character in "0123456789/-" for character in value) else " "
    return f"Out[{line}]={spacing}{value}"


def _limit_output(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    omitted = len(value) - max_chars
    for _ in range(4):
        marker = f"<<{omitted}>>"
        keep = max(0, max_chars - len(marker))
        omitted = len(value) - keep
    marker = f"<<{omitted}>>"
    keep = max(0, max_chars - len(marker))
    if keep == 0:
        return marker[:max_chars]
    left = keep // 2
    right = keep - left
    return value[:left] + marker + (value[-right:] if right else "")


def _lean_available() -> bool:
    project_dir = Path(
        os.environ.get("LEAN_PROJECT_DIR", str(Path(__file__).resolve().parents[2] / "lean"))
    )
    return lake_executable() is not None and project_dir.is_dir()
