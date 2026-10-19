from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeout
import hmac
import hashlib
import json
import os
from pathlib import Path
from time import monotonic
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from .async_jobs import (
    AsyncJobCapacityError,
    discard_async_pod_job,
    get_async_pod_job,
    submit_async_pod,
)
from .computation import ComputeRequest, Computation, check_computation, prepare_computation
from .full_results import (
    FullResultsBackendError,
    FullResultsRequestError,
    async_pod_descriptor,
    async_query_payload,
    execute_query,
    filter_response_pods,
    normalize_formats,
    normalize_async,
    normalize_output,
    query_error,
    render_pod_response,
    render_response,
    validate_error,
    validate_query,
)
from .formula_input import FormulaInputError, FormulaProofRequest, compile_formula
from .interpretations import (
    AssumptionSelectionError,
    InterpretationError,
    assumption_registry,
    query_with_interpretations,
)
from .knowledge import KnowledgeProviderError, KnowledgeRequest, query_knowledge
from .lean_backend import lake_executable, run_generated_source
from .models import ClaimRequest
from .natural_language import (
    LeanClarificationNeeded,
    LeanGenerationError,
    LeanGeneratorNotConfigured,
    NaturalLanguageLeanRequest,
    generate_lean,
    generator_configuration,
    lean_statement_signature,
)
from .physical_units import SUPPORTED_UNIT_NAMES, UNIT_ALIAS_TO_NAME
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
        "natural_language_lean": generator_configuration(),
    }


@app.get("/v1/capabilities")
def capabilities() -> dict[str, object]:
    registry = assumption_registry()
    return {
        "formalization": "caller_supplied_typed_specification_or_bounded_polynomial_formula",
        "sorts": ["Int", "Real"],
        "fragment": "bounded polynomial arithmetic with equality and ordered comparisons",
        "polynomial_limits": {"maximum_degree": 32, "maximum_power_exponent": 16},
        "formula_input": {
            "path": "/v1/prove-formula",
            "arithmetic": ["+", "-", "*", "division by nonzero numeric constants", "^", "**"],
            "constants": "exact integers, decimals, and rational ratios up to 256 bits",
            "comparisons": ["==", "!=", "<", "<=", ">", ">="],
            "variables": "must be declared and all use one shared Int or Real sort",
            "syntax_limits": {"maximum_characters": 4000, "maximum_nodes": 256, "maximum_depth": 32},
            "natural_language": False,
        },
        "natural_language_to_lean": {
            "path": "/v1/natural-language/prove",
            "status": "configured" if generator_configuration()["configured"] else "requires_kimina_endpoint",
            "protocol": "OpenAI-compatible chat completions",
            "model": generator_configuration()["model"],
            "fragment": "Lean 4 theorem generation from natural-language mathematical problems",
            "formalization_fidelity": "not independently verified against the natural-language input",
            "checked_outcome": "proof_checked; this does not establish natural-language fidelity",
            "proof_verification": "the generated theorem is compiled by Lean and axiom-audited",
            "repair_policy": "one repair attempt follows Lean rejection; the formal theorem signature must remain unchanged",
        },
        "counterexample_models": {
            "witness_replay": "exact integer and rational arithmetic",
            "algebraic_real_witness": "unknown unless an exact rational witness is available",
        },
        "backends": {
            "sympy": "implemented",
            "z3": "implemented",
            "lean_kernel": "configured" if _lean_available() else "not_configured",
        },
        "symbolic_operations": [
            "validate", "simplify", "factor", "expand", "differentiate", "integrate",
            "limit", "series", "sum", "product", "solve", "identity", "plot",
        ],
        "symbolic_plot": {
            "syntax": "plot(expression, variable, lower, upper)",
            "bounds": "explicit finite real values with lower < upper",
            "variables": "one plotted variable; no other free symbols",
            "formats": ["plaintext", "mathml", "minput", "moutput", "image"],
            "image": "inline SVG data URL in the Result pod",
            "plot_width": {"query_parameter": "plotwidth", "default": 200, "range": [100, 1600]},
            "proof_status": "sampled visualization, not a formal proof",
        },
        "symbolic_images": {
            "ordinary_math": "bounded structural presentation-MathML SVG in Input and Result, with escaped-text fallback; DecimalApproximation uses escaped text",
            "plots": "sampled graph SVG with bounded dimensions",
            "provider_data": "escaped provider plaintext as SVG, except seven-day Open-Meteo forecasts which include a chart",
            "text_width": {
                "query_parameter": "width",
                "default": 500,
                "range": [100, 1600],
                "scope": "text-oriented images; plots and graphics use plotwidth",
            },
            "object_max_width": {
                "query_parameter": "maxwidth",
                "default": 500,
                "range": [100, 1600],
                "scope": "structural mathematical images; text fallbacks use width and plots use plotwidth",
            },
            "magnification": {
                "query_parameter": "mag",
                "default": 1.0,
                "range": [0.5, 2.0],
                "scope": "content inside structural mathematical images; provider and plot images are unchanged",
            },
            "typesetting": "bounded structural math rendering with escaped-text fallback for unsupported expressions",
        },
        "symbolic_series": {
            "order_range": [1, 32],
            "expansion_point": "explicit expression",
            "output": "SymPy series with a big-O remainder",
            "wolfram_output": "SeriesData for finite integer-power expansions when representable",
            "proof_status": "symbolic result, not a Lean proof",
        },
        "symbolic_discrete_aggregations": {
            "query_bounds": "inclusive integer literals",
            "maximum_bound_size_bits": 256,
            "symbolic_bounds": "inclusive symbolic bounds must be declared integer in /v1/symbolic assumptions",
            "proof_status": "symbolic result, not a Lean proof",
        },
        "symbolic_limit": {
            "semantics": "two-sided limit over the real line",
            "finite_points": "both one-sided limits are checked; symbolic points must be declared real in /v1/symbolic",
            "infinite_points": "positive and negative infinity use their corresponding approach direction",
            "proof_status": "symbolic result, not a Lean proof",
        },
        "symbolic_named_units": {
            "status": "implemented_initial_slice",
            "unit_names": list(SUPPORTED_UNIT_NAMES),
            "request_field": "named_units",
            "dimension_checks": "incompatible unit dimensions in addition are rejected",
        },
        "symbolic_quantity_literals": {
            "status": "implemented_initial_slice",
            "unit_aliases": "curated common length, time, mass, volume, angle, and derived SI units",
            "aliases": dict(UNIT_ALIAS_TO_NAME),
            "ambiguous_abbreviation": "m returns typed alternatives for meters, US minims, and minutes",
            "single_letter_alias_policy": "other one-letter forms keep existing symbolic/list interpretations; use full unit names or named_units",
            "input_example": "5 km",
            "proof_status": "typed quantity interpretation uses SymPy; it is not a Lean proof",
        },
        "symbolic_input_formats": {
            "native": "bounded safe expression syntax",
            "latex": "bounded presentation-math subset: fractions, roots, powers, common functions, Greek symbols, and degree notation",
            "mathml": "bounded presentation MathML subset: identifiers, numbers, fractions, powers, roots, simple symbol subscripts, fenced expressions, and common functions",
            "limits": {"source_characters": 4000, "tokens_or_nodes": 256, "nesting_depth": 32},
        },
        "symbolic_numeric_approximation": {
            "default_precision_digits": 50,
            "maximum_precision_digits": 500,
            "result_pod": "DecimalApproximation",
        },
        "interpretations": {
            "status": "implemented_initial_slice",
            "path": "/v1/interpret",
            "assumption_registry_version": registry["registry_version"],
            "assumption_types": sorted({item["type"] for item in registry["assumptions"]}),
            "multiple_compatible_selections": True,
            "same_type_selection_scope": "a query-wide selection applies to every matching ambiguous call; use per-occurrence selectors to override individual calls",
            "per_occurrence_selection": "append @N to an assumption ID to select the one-based Nth ambiguous AngleUnit, LogBase, or unparenthesized Function log call; unspecified calls retain the service default",
            "unit_assumption_scope": "single ambiguous m unit in a standalone numeric quantity",
            "candidate_generator": "deterministic_allowlisted_math_parser",
            "classifier": "deterministic_lexical_baseline; optional Decima ranking via ASSUMPTION_CLASSIFIER=decima",
            "validator": "typed registry ID and explicit-unit compatibility checks",
            "classifier_selects_assumptions": False,
        },
        "factual_sources": {
            "wikidata": {"coverage": "entity search and entity records; allowlisted capital (P36), official language (P37), currency (P38), and population (P1082) property retrieval with ranked candidates", "status": "implemented_initial_slice"},
            "dbnomics": {"coverage": "individual time series", "status": "implemented"},
            "nist_codata": {"coverage": "bundled CODATA 2022 constants subset", "status": "implemented"},
            "geonames": {"coverage": "place-name search; requires GEONAMES_USERNAME", "status": "implemented"},
            "open_meteo": {"coverage": "current weather and seven-day forecasts by location or coordinates; forecast chart included", "status": "implemented"},
        },
        "compatibility": {
            "wolfram_alpha_full_results": {
                "path": "/v2/query",
                "status": "local_partial_contract",
                "outputs": ["json", "xml"],
                "input_formats": ["native", "bounded presentation LaTeX", "bounded presentation MathML"],
                "query_classes": [
                    "safe symbolic expressions",
                    "presentation LaTeX and MathML expressions",
                    "simplify/factor/expand/differentiate/integrate/limit/series/sum/product/solve/identity calls",
                    "bounded natural-language symbolic instructions",
                    "weather location queries",
                    "explicit seven-day weather forecast queries",
                    "supported CODATA constants",
                    "explicit Wikidata entity searches",
                    "natural-language population lookups using Wikidata",
                    "natural-language capital relationship lookups using Wikidata",
                    "explicit GeoNames place searches",
                    "explicit DBnomics time-series requests",
                ],
                "natural_language_math": {
                    "status": "bounded_initial_slice",
                    "operations": [
                        "evaluate", "simplify", "factor", "expand", "differentiate",
                        "integrate", "solve", "limit", "series", "sum", "product", "plot",
                    ],
                    "parser": "deterministic allowlisted templates; no model ranking",
                    "symbolic_backend": "SymPy",
                    "sum_product_bounds": "integer literals only",
                    "sum_product_proof_bounds": "Lean proofs support rational-polynomial finite sums/products over inclusive integer ranges of up to 128 terms",
                    "ambiguous_function_scope": "requires explicit parentheses",
                    "proofs": "verify=true attaches Lean-checked evidence for supported polynomial, real-calculus, finite polynomial Sum/Product computations over up to 128 integer terms, and rational-function limits at exact rational or bounded real-polynomial points, including selected removable singularities; denominator conditions are explicit",
                },
                "location_parameters": ["location", "latlong"],
                "supported_weather_parameters": ["location", "latlong", "units"],
                "weather_outputs": ["current conditions", "seven-day forecast", "daily forecast chart"],
                "weather_chart_width": "controlled by plotwidth",
                "text_width": {"query_parameter": "width", "default": 500, "range": [100, 1600]},
                "query_controls": {
                    "ignorecase": True,
                    "reinterpret": False,
                    "translation": False,
                    "verify": "optional Lean check for supported exact polynomial, real-calculus, finite polynomial Sum/Product computations, and rational-function limits at exact rational or bounded real-polynomial points, including selected removable singularities",
                },
                "formal_verification": {
                    "query_parameter": "verify=true",
                    "proof_timeout_parameter": "prooftimeout",
                    "proof_timeout_default_seconds": 60,
                    "proof_timeout_range_seconds": [1, 180],
                    "supported": [
                        "exact polynomial evaluation, simplify, factor, expand, and differentiation",
                        "real differentiation using sin, cos, exp, sinh, and cosh",
                        "indefinite antiderivatives using sin, cos, exp, sinh, and cosh when SymPy returns a supported expression",
                        "polynomial identities expressed as a difference",
                        "indefinite polynomial antiderivatives",
                        "definite integrals of supported expressions with exact rational bounds",
                        "finite sums and products of rational-coefficient polynomials over inclusive integer ranges of up to 128 terms",
                        "finite limits of bounded rational functions at exact rational or bounded real-polynomial points, including selected mixed-denominator removable singularities with explicit conditions",
                    ],
                    "assumption_policy": "proves only the selected interpretation already returned by the query planner; assumptions are preserved in the response",
                    "retrieved_data": "provider values are not Lean-verified",
                },
                "time_controls": {
                    "parsetimeout": {
                        "status": "implemented_initial_slice",
                        "default_seconds": 5,
                        "minimum_seconds": 0.1,
                        "maximum_seconds": 30,
                        "scope": "isolated SymPy parsing for mathematical inputs",
                    },
                    "totaltimeout": {
                        "status": "implemented_initial_slice",
                        "default_seconds": 20,
                        "minimum_seconds": 0.1,
                        "maximum_seconds": 30,
                        "scope": "monotonic deadline for symbolic worker execution and provider request sequences",
                    },
                    "prooftimeout": {
                        "status": "implemented_initial_slice",
                        "default_seconds": 60,
                        "minimum_seconds": 1,
                        "maximum_seconds": 180,
                        "scope": "separate optional Lean compilation and axiom-audit stage after a supported symbolic result",
                    },
                    "podtimeout": {
                        "status": "implemented_initial_slice",
                        "default_seconds": 4,
                        "minimum_seconds": 0.1,
                        "maximum_seconds": 30,
                        "scope": "per-result symbolic execution or provider retrieval budget",
                        "format_stage_equivalent": False,
                    },
                    "unsupported_stage_controls": ["scantimeout", "formattimeout"],
                    "async": {
                        "status": "implemented_initial_slice",
                        "modes": ["false", "true", "numeric seconds"],
                        "true_wait_seconds": 0.4,
                        "numeric_wait_range_seconds": [0, 30],
                        "poll_path": "/v2/asyncPod/{job_id}",
                        "worker_count": 4,
                        "maximum_active_jobs": 16,
                        "maximum_retained_jobs": 256,
                        "result_ttl_seconds": 1800,
                        "storage": "in_process",
                    },
                },
                "pod_states": [
                    "DecimalApproximation__More digits",
                    "DecimalApproximation__Fewer digits",
                ],
                "validation_path": "/v2/validatequery",
                "natural_language_queries": {
                    "status": "bounded_initial_slice",
                    "operations": [
                        "evaluate", "simplify", "factor", "expand", "differentiate",
                        "integrate", "solve", "limit", "series", "sum", "product", "plot",
                    ],
                    "parser": "deterministic phrase templates; no free-form model generation",
                    "validation": "bounded mathematical input parser and SymPy validation",
                    "variable_policy": "infer only one unambiguous free symbol; otherwise return clarification",
                    "assumption_policy": "evaluation phrases use the existing typed assumption registry; operation phrases do not infer assumptions",
                    "result_metadata": "interpretation.natural_language_plan and queryresult.classification",
                    "validatequery_support": True,
                },
            },
            "wolfram_language_compute": {
                "path": "/api/cag/v1/WolframLanguageCompute",
                "status": "allowlisted_subset",
                "operations": [
                    "exact rational arithmetic", "Simplify", "Expand", "Factor",
                    "D for bounded polynomial or supported real-function differentiation",
                    "Integrate for indefinite supported antiderivatives and definite integrals with supported antiderivatives",
                    "Sum and Product for rational-polynomial integrands over at most 128 integer terms",
                    "Limit for bounded rational functions at exact rational or bounded real-polynomial points under explicit denominator conditions",
                    "Solve for univariate degree-at-most-two polynomial equations over Reals with rational roots",
                ],
                "finite_sum_product": {
                    "syntax": (
                        "Sum[expression, {variable, lower, upper}] or "
                        "Product[expression, {variable, lower, upper}]"
                    ),
                    "bounds": "inclusive exact integer bounds; negative and reversed ranges are supported",
                    "proof": "Lean checks the explicit Finset.range computation and audits axioms",
                    "maximum_terms": 128,
                    "expression_domain": "rational-coefficient polynomials with real parameters",
                },
                "limit": {
                    "syntax": "Limit[expression, {variable, point}]",
                    "domain": "bounded rational functions over the reals with real parameters",
                    "point": "exact rational with numerator and denominator up to 256 bits, or a rational-coefficient polynomial in real parameters of total degree at most 64 and coefficient components up to 256 bits",
                    "proof": "Lean proves numeric denominator values nonzero and carries symbolic nonzero values at the point as explicit domain conditions; selected removable singularities are supported for affine vanishing factors and explicit products with affine vanishing factors; polynomial denominator components of degree at most 16 may be factored by SymPy subject to expression and coefficient bounds, but Lean proves the proposed factorization before using it; other product factors need a Lean-checked continuity proof and nonzero-at-point evidence; Lean proves the factors remain nonzero nearby and proves eventual equality on the punctured neighborhood before checking the exact value",
                },
                "rational_function_limits": {
                    "operators": ["+", "-", "*", "/", "^"],
                    "maximum_nodes": 256,
                    "maximum_depth": 32,
                    "maximum_power": 16,
                    "maximum_numeric_component_bits": 256,
                    "variable_domain": "real numbers; symbolic denominator values at the limit point are explicit proof conditions",
                    "symbolic_point_maximum_total_degree": 64,
                    "symbolic_points": "bounded rational-coefficient polynomials in real parameters; free real parameter names are returned in evidence.real_parameters, and nonconstant denominators must be nonzero after substitution except for supported removable singularities",
                    "removable_singularities": "at least one nonconstant denominator must vanish at the point; vanishing factors must be affine rational expressions or positive integer powers of nonzero rational-slope affine factors; explicit products are supported when every vanishing factor meets that rule and each remaining factor has a Lean-checked continuity proof with numeric nonzero-at-point evidence or an explicit condition; polynomial denominator components of degree at most 16 may be factored by SymPy subject to expression and coefficient bounds, and the factorization must pass Lean; simplification must yield a bounded rational function",
                },
                "derivative": {
                    "syntax": "D[expression, variable]",
                    "proof": "Lean HasDerivAt theorem with axiom audit",
                    "functions": [
                        "Sin", "Cos", "Tan", "Sec", "Csc", "Cot", "ArcSin", "ArcCos",
                        "ArcTan", "ArcSinh", "ArcCosh", "Sqrt", "Exp", "Sinh", "Cosh", "Tanh", "Log",
                    ],
                    "assumptions": "all variables are real; differentiation variable is unrestricted",
                    "domain_conditions": "Tan and Sec arguments require nonzero cosine; Csc and Cot arguments require nonzero sine; ArcSin and ArcCos arguments require strict bounds between -1 and 1; variable ArcCosh arguments require values greater than 1; variable Sqrt arguments require positivity; variable denominators require nonzero conditions; real Log arguments require positive conditions; returned in evidence.domain_conditions",
                },
                "indefinite_integral": {
                    "syntax": "Integrate[expression, variable]",
                    "proof": "Lean HasDerivAt theorem with axiom audit",
                    "result_semantics": "one antiderivative representative; additive constant omitted; generated real logarithms use log(abs(argument)) where required",
                    "functions": ["Sin", "Cos", "Tan", "Sec", "Csc", "Cot", "ArcSin", "ArcCos", "ArcTan", "ArcSinh", "ArcCosh", "Sqrt", "Exp", "Sinh", "Cosh", "Tanh", "Log"],
                    "limits": "bounded exact expressions; selected variable-denominator, logarithmic, square-root, reciprocal-trigonometric, tangent, hyperbolic-tangent, inverse-trigonometric, and inverse-hyperbolic antiderivatives are accepted only when Lean checks the result, with required domain conditions returned; examples include Integrate[Sec[x]^2, x], Integrate[Csc[x]^2, x], Integrate[Cot[x]^2, x], Integrate[ArcSin[x], x], Integrate[ArcCos[x], x], Integrate[1/Sqrt[1+x^2], x], Integrate[ArcSinh[x], x], Integrate[1/Sqrt[x^2-1], x], Integrate[ArcCosh[x], x], and Integrate[Tanh[x], x]",
                    "domain_conditions": "submitted Log and variable Sqrt arguments require positivity; inverse-trigonometric results require strict argument bounds between -1 and 1; ArcCosh results require the argument greater than 1; Sec antiderivatives require nonzero cosine and Csc/Cot antiderivatives require nonzero sine; selected inverse-hyperbolic antiderivatives return positivity and denominator conditions when required by their generated expressions; selected denominator poles and generated real logarithm or tangent-pole conditions require nonzero conditions; hyperbolic-tangent integration proves cosh nonzero from cosh positivity and adds no extra condition; remaining conditions are returned in evidence.domain_conditions",
                },
                "solve": {
                    "syntax": "Solve[lhs == rhs, variable, Reals]",
                    "system_syntax": "Solve[{eq1,...,eqm}, {x1,...,xn}, Reals] with 1 <= m <= 4 and 2 <= n <= 4",
                    "domain": "univariate rational-coefficient polynomials of degree at most four with complete rational factorization, plus exact real quadratic-radical roots and quadratics with no real roots, nonzero constant equations, and rational linear systems with 1 to 4 equations and 2 to 4 variables, returning unique, parametric, or empty solution sets",
                    "proof": "Lean proves the returned solution set is equivalent to the submitted equation or system over the reals and audits axioms",
                    "unsupported": [
                        "complex-domain solving",
                        "nonlinear systems or systems with more than four equations or variables",
                        "degree above four",
                        "degree-three or degree-four polynomials without complete rational factorization",
                    ],
                },
                "definite_integral": {
                    "syntax": "Integrate[expression, {variable, lower, upper}]",
                    "proof": "Lean-checked antiderivative plus the interval fundamental theorem of calculus; axiom audit",
                    "bounds": "exact rational numeric expressions",
                    "limits": "bounded real-calculus grammar with exact rational bounds; selected variable-denominator cases are supported and unresolved interval-wide nonzero conditions are returned; other free variables are real parameters",
                },
                "real_calculus_limits": {
                    "operators": ["+", "-", "*", "rational constant denominators; selected variable denominators for indefinite Integrate and D require explicit nonzero conditions", "^ with integer exponent 0–16"],
                    "functions": ["Sin", "Cos", "Tan", "ArcSin", "ArcCos", "ArcTan", "ArcSinh", "ArcCosh", "Sqrt", "Exp", "Sinh", "Cosh", "Tanh", "Log"],
                    "constants": ["Pi", "E", "exact integers, decimals, and rational constants up to 256 bits"],
                    "maximum_nodes": 256,
                    "maximum_depth": 32,
                    "unsupported_examples": ["general variable-denominator integration", "non-elementary antiderivatives", "Sin[x^2] antiderivative"],
                },
                "special_cases": [
                    "Sin[Pi]", "TrigExpand@Sin[2x]"
                ],
                "proof_status": "each returned result is compiled and axiom-audited by Lean",
            },
        },
        "outcomes": [
            "proof_checked",
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


@app.post("/v1/prove-formula", dependencies=[Depends(require_api_token)])
def prove_formula(request: FormulaProofRequest) -> dict[str, object]:
    try:
        typed_claim = compile_formula(request)
    except (FormulaInputError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return analyze(typed_claim)


@app.post("/v1/natural-language/prove", dependencies=[Depends(require_api_token)])
def prove_natural_language(request: NaturalLanguageLeanRequest) -> dict[str, object]:
    generation_started = monotonic()
    try:
        model, lean_source = generate_lean(
            request.problem,
            timeout_seconds=request.budget.wall_seconds / 2,
        )
    except LeanGeneratorNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except LeanClarificationNeeded:
        return {
            "outcome": "needs_clarification",
            "problem": request.problem,
            "formalization_fidelity": {"status": "not_established"},
            "model": {
                "name": generator_configuration()["model"],
                "protocol": "openai_chat_completions",
            },
            "message": "The model did not produce a Lean theorem; provide a more precise mathematical statement.",
        }
    except LeanGenerationError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    lean_started = monotonic()
    lean_result = run_generated_source(
        lean_source,
        timeout_seconds=request.lean_timeout_seconds,
    )
    proof_repair: dict[str, object] = {"attempted": False, "status": "not_needed"}
    generation_attempts = 1
    if lean_result.status == "rejected":
        remaining_generation = request.budget.wall_seconds - (monotonic() - generation_started)
        if remaining_generation > 0.1:
            proof_repair = {"attempted": True, "status": "requested"}
            try:
                _, repaired_source = generate_lean(
                    request.problem,
                    timeout_seconds=min(remaining_generation, request.budget.wall_seconds / 2),
                    previous_source=lean_source,
                    diagnostics=lean_result.diagnostics or "Lean rejected the candidate",
                )
            except LeanGenerationError as exc:
                proof_repair = {
                    "attempted": True,
                    "status": "generation_failed",
                    "reason": str(exc),
                }
            else:
                generation_attempts = 2
                if lean_statement_signature(repaired_source) != lean_statement_signature(lean_source):
                    proof_repair = {
                        "attempted": True,
                        "status": "statement_changed_discarded",
                        "reason": "The repair candidate changed the formal theorem statement.",
                    }
                else:
                    remaining_lean = request.lean_timeout_seconds - (monotonic() - lean_started)
                    if remaining_lean > 0.1:
                        lean_source = repaired_source
                        lean_result = run_generated_source(repaired_source, remaining_lean)
                        proof_repair = {
                            "attempted": True,
                            "status": lean_result.status,
                        }
                    else:
                        proof_repair = {
                            "attempted": True,
                            "status": "timeout_before_recheck",
                            "reason": "The Lean time budget expired before the repaired proof could be checked.",
                        }
    verified = lean_result.status == "checked"
    evidence: dict[str, object] = {
        "kind": "lean_kernel_checked" if verified else "lean_generation_candidate",
        "engine": "Lean 4",
        "lean_status": lean_result.status,
        "verified": verified,
        "generation_attempts": generation_attempts,
        "proof_repair": proof_repair,
        "lean_source_sha256": hashlib.sha256(lean_result.source.encode("utf-8")).hexdigest(),
        "lean_source": lean_result.source,
    }
    if verified:
        evidence["axiom_audit"] = {
            "status": "passed",
            "dependencies": list(lean_result.axioms),
        }
    return {
        "outcome": "proof_checked" if verified else lean_result.status,
        "theorem_status": "checked" if verified else lean_result.status,
        "problem": request.problem,
        "formalization_fidelity": {
            "status": "unverified_against_natural_language",
            "message": (
                "Lean checked the generated theorem statement and proof, but that does not "
                "establish that the formal statement preserves the natural-language meaning."
            ),
        },
        "model": {"name": model, "protocol": "openai_chat_completions"},
        "evidence": evidence,
        **({"diagnostics": lean_result.diagnostics} if lean_result.diagnostics else {}),
    }


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
    request: Request,
    input: Annotated[str | None, Query()] = None,
    output: Annotated[str, Query()] = "xml",
    format: Annotated[list[str] | None, Query()] = None,
    assumption: Annotated[list[str] | None, Query()] = None,
    includepodid: Annotated[list[str] | None, Query()] = None,
    excludepodid: Annotated[list[str] | None, Query()] = None,
    podtitle: Annotated[list[str] | None, Query()] = None,
    podindex: Annotated[list[str] | None, Query()] = None,
    podstate: Annotated[list[str] | None, Query()] = None,
    scanner: Annotated[list[str] | None, Query()] = None,
    location: Annotated[str | None, Query()] = None,
    latlong: Annotated[str | None, Query()] = None,
    units: Annotated[str | None, Query()] = None,
    ignorecase: Annotated[bool, Query()] = False,
    verify: Annotated[
        bool,
        Query(description="Attach a Lean proof when the selected computation is supported"),
    ] = False,
    prooftimeout: Annotated[float, Query(ge=1, le=180)] = 60.0,
    parsetimeout: Annotated[float, Query(ge=0.1, le=30)] = 5.0,
    podtimeout: Annotated[float, Query(ge=0.1, le=30)] = 4.0,
    totaltimeout: Annotated[float, Query(ge=0.1, le=30)] = 20.0,
    width: Annotated[
        int,
        Query(
            ge=100,
            le=1600,
            description="Approximate width for text-oriented images; does not affect plots or graphics",
        ),
    ] = 500,
    maxwidth: Annotated[
        int,
        Query(
            ge=100,
            le=1600,
            description="Approximate maximum width for structural mathematical images",
        ),
    ] = 500,
    mag: Annotated[
        float,
        Query(
            ge=0.5,
            le=2.0,
            description="Magnification of content in structural mathematical images",
        ),
    ] = 1.0,
    plotwidth: Annotated[int, Query(ge=100, le=1600)] = 200,
    async_mode: Annotated[
        str,
        Query(
            alias="async",
            description=(
                "false for synchronous results, true for a 0.4-second initial wait, "
                "or a numeric wait in seconds"
            ),
        ),
    ] = "false",
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
        async_wait_seconds = normalize_async(async_mode)

        def run_query() -> dict[str, object]:
            result = execute_query(
                input,
                assumption,
                formats,
                location=location,
                latlong=latlong,
                units=units,
                pod_states=podstate,
                include_pod_ids=includepodid,
                exclude_pod_ids=excludepodid,
                pod_titles=podtitle,
                pod_indexes=podindex,
                scanners=scanner,
                ignorecase=ignorecase,
                parse_timeout_seconds=parsetimeout,
                pod_timeout_seconds=podtimeout,
                total_timeout_seconds=totaltimeout,
                text_width=width,
                max_image_width=maxwidth,
                magnification=mag,
                plot_width=plotwidth,
                verify=verify,
                proof_timeout_seconds=prooftimeout,
            )
            if ignorecase:
                result["queryresult"]["ignorecase"] = True
            return result

        if async_wait_seconds is not None:
            pod_title, pod_id, pod_scanner = async_pod_descriptor(
                input, location=location, latlong=latlong, units=units
            )
            probe = async_query_payload(
                input,
                pod_id=pod_id,
                pod_title=pod_title,
                scanner=pod_scanner,
                async_url="/v2/asyncPod/pending",
                formats=formats,
                elapsed=0.0,
                text_width=width,
            )
            probe = filter_response_pods(
                probe, includepodid, excludepodid, podtitle, podindex, scanner
            )
            async_visible = any(
                pod.get("id") == pod_id and pod.get("async")
                for pod in probe["queryresult"]["pods"]
            )
            if async_visible and podstate is None:
                job = submit_async_pod(
                    run_query,
                    pod_id=pod_id,
                    pod_title=pod_title,
                    scanner=pod_scanner,
                    output=selected_output,
                )
                async_url = str(request.url_for("full_results_async_pod", job_id=job.job_id))
                pending = async_query_payload(
                    input,
                    pod_id=pod_id,
                    pod_title=pod_title,
                    scanner=pod_scanner,
                    async_url=async_url,
                    formats=formats,
                    text_width=width,
                    elapsed=monotonic() - job.created_at,
                    verify_requested=verify,
                )
                pending = filter_response_pods(
                    pending, includepodid, excludepodid, podtitle, podindex, scanner
                )
                if ignorecase:
                    pending["queryresult"]["ignorecase"] = True
                try:
                    completed = job.future.result(timeout=min(async_wait_seconds, totaltimeout))
                except FutureTimeout:
                    return render_response(pending, selected_output)
                except (AssumptionSelectionError, FullResultsRequestError) as exc:
                    discard_async_pod_job(job.job_id)
                    response = render_response(query_error(input, str(exc)), selected_output)
                    response.status_code = status.HTTP_400_BAD_REQUEST
                    return response
                except FullResultsBackendError as exc:
                    discard_async_pod_job(job.job_id)
                    response = render_response(
                        query_error(input, str(exc), code=exc.error_code), selected_output
                    )
                    response.status_code = exc.status_code
                    return response
                except Exception:
                    discard_async_pod_job(job.job_id)
                    response = render_response(
                        query_error(
                            input,
                            "Asynchronous computation failed",
                            code="COMPUTATION_FAILURE",
                        ),
                        selected_output,
                    )
                    response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
                    return response
                discard_async_pod_job(job.job_id)
                return render_response(completed, selected_output)

        payload = run_query()
    except (AssumptionSelectionError, FullResultsRequestError) as exc:
        response = render_response(query_error(input, str(exc)), selected_output)
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response
    except AsyncJobCapacityError as exc:
        response = render_response(
            query_error(input, str(exc), code="ASYNC_CAPACITY_EXCEEDED"), selected_output
        )
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return response
    except FullResultsBackendError as exc:
        response = render_response(
            query_error(input, str(exc), code=exc.error_code),
            selected_output,
        )
        response.status_code = exc.status_code
        return response
    return render_response(payload, selected_output)


@app.get(
    "/v2/asyncPod/{job_id}",
    dependencies=[Depends(require_api_token)],
    name="full_results_async_pod",
    summary="Fetch an asynchronously computed result pod",
    description=(
        "Use the async URL from /v2/query with the same bearer token. Returns HTTP 202 while "
        "the pod is pending; completed results are retained in process memory for 30 minutes."
    ),
)
def full_results_async_pod(
    job_id: str,
    output: Annotated[str | None, Query()] = None,
) -> Response:
    job = get_async_pod_job(job_id)
    if job is None or job.expires_at <= monotonic():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Async pod result was not found",
        )
    try:
        selected_output = normalize_output(output or job.output)
    except FullResultsRequestError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not job.future.done():
        state = "running" if job.future.running() else "pending"
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            headers={"Retry-After": "1"},
            content={
                "status": state,
                "job_id": job.job_id,
                "pod_id": job.pod_id,
                "retry_after_seconds": 1,
            },
        )

    try:
        result = job.future.result()
    except (AssumptionSelectionError, FullResultsRequestError) as exc:
        pod = _async_error_pod(job.pod_id, job.pod_title, job.scanner, str(exc))
    except FullResultsBackendError as exc:
        pod = _async_error_pod(job.pod_id, job.pod_title, job.scanner, str(exc))
    except Exception:
        pod = _async_error_pod(
            job.pod_id, job.pod_title, job.scanner, "Asynchronous computation failed"
        )
    else:
        queryresult = result.get("queryresult", {})
        pod = next(
            (item for item in queryresult.get("pods", []) if item.get("id") == job.pod_id),
            None,
        )
        if pod is not None and queryresult.get("evidence") is not None:
            pod["evidence"] = queryresult["evidence"]
        if pod is None:
            message = queryresult.get("error_detail", {}).get("msg")
            if not message:
                tips = queryresult.get("tips", [])
                message = tips[0].get("text") if tips else "No result pod was produced"
            pod = _async_error_pod(job.pod_id, job.pod_title, job.scanner, str(message))
    discard_async_pod_job(job.job_id)
    return render_pod_response(pod, selected_output)


def _async_error_pod(pod_id: str, title: str, scanner: str, message: str) -> dict[str, object]:
    return {
        "title": title,
        "scanner": scanner,
        "id": pod_id,
        "position": 200,
        "error": True,
        "error_text": message,
        "numsubpods": 1,
        "primary": True,
        "subpods": [{"title": "", "plaintext": message}],
    }


@app.get("/v2/validatequery", dependencies=[Depends(require_api_token)])
def full_results_validatequery(
    input: Annotated[str | None, Query()] = None,
    output: Annotated[str, Query()] = "xml",
    assumption: Annotated[list[str] | None, Query()] = None,
    location: Annotated[str | None, Query()] = None,
    latlong: Annotated[str | None, Query()] = None,
    units: Annotated[str | None, Query()] = None,
    ignorecase: Annotated[bool, Query()] = False,
    parsetimeout: Annotated[float, Query(ge=0.1, le=30)] = 5.0,
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
        payload = validate_query(
            input, assumption, location=location, latlong=latlong, units=units,
            ignorecase=ignorecase, parse_timeout_seconds=parsetimeout,
        )
    except AssumptionSelectionError as exc:
        response = render_response(validate_error(input, str(exc)), selected_output)
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response
    except FullResultsRequestError as exc:
        response = render_response(validate_error(input, str(exc)), selected_output)
        response.status_code = status.HTTP_400_BAD_REQUEST
        return response
    if ignorecase:
        payload["validatequeryresult"]["ignorecase"] = True
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
        "A bounded arithmetic, real-calculus, finite polynomial Sum/Product, "
        "rational-function limit, and special-case subset is computed and "
        "compiled into Lean proofs."
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

    import asyncio

    effective_budget = compute_request.timeConstraint
    evaluation_started = monotonic()
    computation = await asyncio.to_thread(
        prepare_computation,
        compute_request.code,
        float(effective_budget),
    )
    if computation is None:
        return _compat_response(501, "This Wolfram Language expression is not supported", request_id)
    if computation.backend_error is not None:
        return _compat_response(
            500,
            "The symbolic computation backend failed",
            request_id,
            evidence={"stage": "symbolic", "diagnostic": computation.backend_error},
        )
    if computation.timed_out:
        return _compat_response(
            200,
            _evaluation_time_exceeded(effective_budget),
            request_id,
            success=True,
        )
    remaining_budget = effective_budget - (monotonic() - evaluation_started)
    if remaining_budget <= 0:
        return _compat_response(
            200,
            _evaluation_time_exceeded(effective_budget),
            request_id,
            success=True,
        )
    lean_result = await _run_computation(computation, remaining_budget)
    if lean_result.status == "unavailable":
        return _compat_response(503, "Lean proof backend is not ready", request_id)
    if lean_result.status == "timeout":
        return _compat_response(
            200,
            _evaluation_time_exceeded(effective_budget),
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
            "domain_conditions": list(computation.domain_conditions),
            "real_parameters": list(computation.real_parameters),
            "time_constraint_requested": compute_request.timeConstraint,
            "time_constraint_applied": effective_budget,
        },
    }
    return JSONResponse(status_code=200, content=body)


async def _run_computation(computation: Computation, timeout_seconds: float):
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


def _evaluation_time_exceeded(seconds: int) -> str:
    return (
        'Failure["EvaluationTimeExceeded", '
        '<|"MessageTemplate" -> "Evaluation exceeded the `1` second time limit.", '
        f'"MessageParameters" -> {{{seconds}}}|>]'
    )


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
