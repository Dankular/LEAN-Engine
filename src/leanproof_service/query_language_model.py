"""Optional model-backed proposal for unsupported natural-language math requests."""

from __future__ import annotations

import json
import os
import re
from typing import Annotated, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pydantic import Field, StringConstraints, ValidationError, model_validator

from .models import StrictModel
from .natural_language_query import (
    NaturalLanguageClarification,
    NaturalLanguageOperation,
)


QueryOperation = Literal[
    "evaluate",
    "simplify",
    "factor",
    "expand",
    "differentiate",
    "integrate",
    "solve",
    "limit",
    "series",
    "sum",
    "product",
    "identity",
    "plot",
]
_MathIntent = re.compile(
    r"\b(?:differentiat\w*|derivative|derive|integrat\w*|antiderivative|solve|root|equation|"
    r"simplif\w*|factor\w*|expand\w*|limit|series|summation|\bsum\b|product|"
    r"identity|prove|proof|verify|plot|graph|polynomial|formula|expression)\b",
    re.IGNORECASE,
)


class QueryInterpretationResponse(StrictModel):
    status: Literal["candidate", "clarification"]
    operation: QueryOperation | None = None
    expression: Annotated[str, StringConstraints(min_length=1, max_length=4000)] | None = None
    rhs: Annotated[str, StringConstraints(min_length=1, max_length=4000)] | None = None
    variable: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
    ] | None = None
    lower: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None
    upper: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None
    point: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None
    order: int | None = Field(default=None, ge=1, le=32)
    question: Annotated[str, StringConstraints(min_length=1, max_length=1000)] | None = None

    @model_validator(mode="after")
    def validate_response_shape(self) -> QueryInterpretationResponse:
        candidate_fields = (
            self.operation,
            self.expression,
            self.rhs,
            self.variable,
            self.lower,
            self.upper,
            self.point,
            self.order,
        )
        if self.status == "candidate":
            if (
                self.operation is None
                or self.expression is None
                or self.question is not None
            ):
                raise ValueError("candidate responses require operation and expression only")
            if self.operation == "identity" and self.rhs is None:
                raise ValueError("identity candidates require rhs")
            if self.operation != "identity" and self.rhs is not None:
                raise ValueError("rhs is only valid for an identity candidate")
            if self.operation in {
                "differentiate",
                "integrate",
                "solve",
                "limit",
                "series",
                "sum",
                "product",
                "plot",
            } and self.variable is None:
                raise ValueError(f"{self.operation} candidates require variable")
            if self.operation not in {
                "differentiate",
                "integrate",
                "solve",
                "limit",
                "series",
                "sum",
                "product",
                "plot",
            } and self.variable is not None:
                raise ValueError(f"{self.operation} candidates do not accept variable")
            if self.operation in {"sum", "product", "plot"} and (
                self.lower is None or self.upper is None
            ):
                raise ValueError(f"{self.operation} candidates require lower and upper bounds")
            if self.operation == "limit" and self.point is None:
                raise ValueError("limit candidates require point")
            if self.operation == "series" and (self.point is None or self.order is None):
                raise ValueError("series candidates require point and order")
            if self.operation != "series" and self.order is not None:
                raise ValueError("order is only valid for a series candidate")
            if self.operation not in {"limit", "series"} and self.point is not None:
                raise ValueError(f"{self.operation} candidates do not accept point")
            if (self.lower is None) != (self.upper is None):
                raise ValueError("lower and upper must be supplied together")
            if self.operation not in {"integrate", "sum", "product", "plot"} and (
                self.lower is not None or self.upper is not None
            ):
                raise ValueError(f"{self.operation} candidates do not accept bounds")
        elif self.question is None or any(value is not None for value in candidate_fields):
            raise ValueError("clarification responses require question only")
        return self


def query_interpreter_configuration() -> dict[str, object]:
    base_url = os.environ.get("QUERY_INTERPRETER_BASE_URL", "").strip()
    model = os.environ.get("QUERY_INTERPRETER_MODEL", "").strip()
    return {
        "configured": bool(base_url and model),
        "model": model or None,
        "protocol": "OpenAI-compatible chat completions",
        "enabled_for_math_intents_only": True,
    }


def generate_query_candidate(
    query: str,
    timeout_seconds: float,
) -> NaturalLanguageOperation | None:
    """Ask a configured model for one typed proposal; callers still validate it."""
    if not _MathIntent.search(query):
        return None
    config = query_interpreter_configuration()
    if not config["configured"]:
        return None
    base_url = os.environ["QUERY_INTERPRETER_BASE_URL"].strip()
    model = os.environ["QUERY_INTERPRETER_MODEL"].strip()
    endpoint = _chat_completions_url(base_url)
    payload = json.dumps({
        "model": model,
        "temperature": 0,
        "max_tokens": 1200,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ],
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = os.environ.get("QUERY_INTERPRETER_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(endpoint, data=payload, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=max(0.1, min(timeout_seconds, 30.0))) as response:
            body = response.read(32_769)
    except HTTPError as exc:
        raise NaturalLanguageClarification(
            f"The configured query model returned HTTP {exc.code}; try a supported explicit query."
        ) from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise NaturalLanguageClarification(
            "The configured query model could not be reached in time; try a supported explicit query."
        ) from exc
    if len(body) > 32_768:
        raise NaturalLanguageClarification(
            "The configured query model returned an oversized response; try a supported explicit query."
        )
    try:
        completion = json.loads(body)
        content = completion["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("message content is not text")
        parsed = QueryInterpretationResponse.model_validate(_parse_json_content(content))
    except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
        raise NaturalLanguageClarification(
            "The configured query model did not return a valid typed interpretation; "
            "try an explicit supported query."
        ) from exc

    if parsed.status == "clarification":
        assert parsed.question is not None
        raise NaturalLanguageClarification(parsed.question)
    assert parsed.operation is not None and parsed.expression is not None
    return NaturalLanguageOperation(
        operation=parsed.operation,
        expression=parsed.expression,
        rhs=parsed.rhs,
        variable=parsed.variable,
        variable_source="model_candidate" if parsed.variable is not None else None,
        lower=parsed.lower,
        upper=parsed.upper,
        point=parsed.point,
        order=parsed.order,
        rule_id="model_query_candidate",
        candidate_generator="language_model",
        model=model,
    )


def _parse_json_content(content: str) -> object:
    value = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, re.IGNORECASE | re.DOTALL)
    if fenced:
        value = fenced.group(1)
    return json.loads(value)


def _chat_completions_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise NaturalLanguageClarification(
            "QUERY_INTERPRETER_BASE_URL must be an HTTP(S) OpenAI-compatible endpoint."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise NaturalLanguageClarification(
            "QUERY_INTERPRETER_BASE_URL cannot include credentials or query data."
        )
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        return base_url
    return f"{base_url.rstrip('/')}/chat/completions"


_SYSTEM_PROMPT = """Convert one natural-language math request into one candidate for a bounded symbolic API.
Return exactly one JSON object and no surrounding prose. Use this schema:
{"status":"candidate","operation":"differentiate","expression":"x^2","variable":"x"}
or {"status":"clarification","question":"Which variable should I use?"}.

Allowed operations: evaluate, simplify, factor, expand, differentiate, integrate, solve, limit,
series, sum, product, identity, plot. Candidate fields are operation, expression, rhs (identity only),
variable, lower, upper, point, and order. Include only fields required for the selected operation.
For solve, return the equation as its left-hand side minus right-hand side in expression, so the
solver finds roots of expression = 0. For identity, put the left-hand side in expression and the
right-hand side in rhs. Use exact mathematical expressions in ordinary parser syntax, with ^ for
powers. Never return Lean code, proofs, assumptions, coordinate systems, units, data lookups, or
arbitrary code. Do not narrow
quantifiers or add unstated domain restrictions. If the request is ambiguous, unsupported, or needs
an assumption, return clarification rather than guessing. The API will validate the proposal and
report that English-to-formula fidelity is not proved."""
