from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
from threading import Lock
from time import monotonic
from typing import Annotated
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import Field, StringConstraints

from .models import Budget, StrictModel


ProblemText = Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class NaturalLanguageLeanRequest(StrictModel):
    problem: ProblemText
    budget: Budget = Field(default_factory=Budget)
    lean_timeout_seconds: int = Field(default=120, ge=1, le=180)
    draft_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")] | None = None
    formalization_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None
    approve_formalization: bool = False


class LeanGenerationError(RuntimeError):
    pass


class LeanGeneratorNotConfigured(LeanGenerationError):
    pass


class LeanClarificationNeeded(LeanGenerationError):
    pass


@dataclass(frozen=True)
class FormalizationDraft:
    problem: str
    model: str
    lean_source: str
    formalization_sha256: str
    expires_at: float


_DRAFT_TTL_SECONDS = 15 * 60
_DRAFT_CAPACITY = 128
_draft_lock = Lock()
_drafts: dict[str, FormalizationDraft] = {}


SYSTEM_PROMPT = """Formalize and prove the user's mathematical problem in Lean 4 using Mathlib.
Return a single complete Lean theorem in one ```lean code block. Include `import Mathlib`, one
theorem declaration, and its proof. Do not add assumptions that are not stated in the problem.
Use a concise theorem name and standard Mathlib tactics. If the problem is ambiguous or cannot be
formalized faithfully, return no theorem and briefly explain what needs clarification. The result
will be compiled and kernel-checked; a successful Lean check does not certify that your formal
statement matches the natural-language problem."""


def generator_configuration() -> dict[str, object]:
    base_url = os.environ.get("KIMINA_BASE_URL", "").strip()
    model = os.environ.get("KIMINA_MODEL", "").strip()
    return {
        "configured": bool(base_url and model),
        "model": model or None,
        "protocol": "OpenAI-compatible chat completions",
    }


def generate_lean(
    problem: str,
    timeout_seconds: float,
    *,
    previous_source: str | None = None,
    diagnostics: str | None = None,
) -> tuple[str, str]:
    base_url = os.environ.get("KIMINA_BASE_URL", "").strip()
    model = os.environ.get("KIMINA_MODEL", "").strip()
    if not base_url or not model:
        raise LeanGeneratorNotConfigured(
            "Set KIMINA_BASE_URL and KIMINA_MODEL to enable natural-language Lean generation"
        )
    endpoint = _chat_completions_url(base_url)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": problem},
    ]
    if previous_source is not None and diagnostics is not None:
        messages.extend((
            {"role": "assistant", "content": f"```lean\n{previous_source}\n```"},
            {
                "role": "user",
                "content": (
                    "Lean rejected that candidate. Correct the proof using the diagnostics "
                    "below. Preserve the theorem statement and all binders exactly; change "
                    "only imports/open commands if needed and the proof. If no faithful proof "
                    "is available, return no theorem.\n\nLean diagnostics:\n"
                    f"{diagnostics[:4000]}"
                ),
            },
        ))
    payload = json.dumps({
        "model": model,
        "temperature": 0.2,
        "max_tokens": 4096,
        "messages": messages,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = os.environ.get("KIMINA_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(endpoint, data=payload, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=max(0.1, min(timeout_seconds, 30.0))) as response:
            body = response.read(128_001)
    except HTTPError as exc:
        raise LeanGenerationError(f"Lean generation model returned HTTP {exc.code}") from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise LeanGenerationError("Lean generation model could not be reached in time") from exc
    if len(body) > 128_000:
        raise LeanGenerationError("Lean generation model response exceeded 128 KB")
    try:
        completion = json.loads(body)
        content = completion["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("message content is not text")
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise LeanGenerationError("Lean generation model returned an invalid chat completion") from exc
    return model, normalize_lean_source(content)


def lean_statement_signature(source: str) -> tuple[tuple[str, ...], str] | None:
    lines = source.splitlines()
    theorem_index = next(
        (index for index, line in enumerate(lines) if re.match(r"^(?:theorem|lemma)\s+", line)),
        None,
    )
    if theorem_index is None:
        return None
    declaration = "\n".join(lines[theorem_index:])
    match = re.match(
        r"^(?:theorem|lemma)\s+[A-Za-z_][A-Za-z0-9_']*(?P<signature>.*?)\s*:=\s*by\b",
        declaration,
        flags=re.DOTALL,
    )
    if match is None:
        return None
    prelude = tuple(
        line.strip()
        for line in lines[:theorem_index]
        if line.strip() and line.strip() != "import Mathlib"
    )
    signature = re.sub(r"\s+", " ", match.group("signature")).strip()
    return prelude, signature


def normalize_lean_source(content: str) -> str:
    candidates = re.findall(
        r"```[^\r\n]*\r?\n(.*?)```",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    rejection: str | None = None
    for candidate in candidates:
        try:
            return _sanitize_candidate(candidate)
        except LeanGenerationError as exc:
            rejection = str(exc)
    if rejection:
        raise LeanGenerationError(f"Model Lean candidate rejected: {rejection}")
    raise LeanClarificationNeeded(
        "The model did not produce a Lean theorem; provide a more precise mathematical statement"
    )


def formalization_digest(problem: str, source: str) -> str:
    signature = lean_statement_signature(source)
    if signature is None:
        raise LeanGenerationError("Generated source does not contain a reviewable theorem statement")
    payload = json.dumps(
        {"problem": problem, "prelude": signature[0], "signature": signature[1]},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def create_formalization_draft(problem: str, model: str, source: str) -> tuple[str, str]:
    digest = formalization_digest(problem, source)
    draft_id = uuid4().hex
    now = monotonic()
    draft = FormalizationDraft(
        problem=problem,
        model=model,
        lean_source=source,
        formalization_sha256=digest,
        expires_at=now + _DRAFT_TTL_SECONDS,
    )
    with _draft_lock:
        expired = [key for key, value in _drafts.items() if value.expires_at <= now]
        for key in expired:
            del _drafts[key]
        while len(_drafts) >= _DRAFT_CAPACITY:
            del _drafts[next(iter(_drafts))]
        _drafts[draft_id] = draft
    return draft_id, digest


def get_formalization_draft(draft_id: str) -> FormalizationDraft | None:
    now = monotonic()
    with _draft_lock:
        draft = _drafts.get(draft_id)
        if draft is None:
            return None
        if draft.expires_at <= now:
            del _drafts[draft_id]
            return None
        return draft


def _sanitize_candidate(candidate: str) -> str:
    source = candidate.replace("\r\n", "\n").strip()
    if not source or len(source) > 20_000:
        raise LeanGenerationError("Generated Lean source is empty or exceeds 20 KB")
    if "--" in source or "/-" in source or "-/" in source:
        raise LeanGenerationError("Generated Lean source cannot contain comments")
    if re.search(
        r"\b(?:sorry|admit|axiom|unsafe|run_tac|run_cmd|macro|elab|initialize|"
        r"builtin_initialize|extern|implemented_by)\b|#(?:eval|run|print|check|reduce|synth)",
        source,
    ):
        raise LeanGenerationError("Generated Lean source contains a disallowed construct")

    lines = [line.rstrip() for line in source.splitlines()]
    theorem_lines = [
        index for index, line in enumerate(lines)
        if re.match(r"^(?:theorem|lemma)\s+", line)
    ]
    if len(theorem_lines) != 1:
        raise LeanGenerationError("Generated source must contain exactly one theorem or lemma")
    theorem_index = theorem_lines[0]
    prelude: list[str] = []
    for line in lines[:theorem_index]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "import Mathlib":
            continue
        if re.fullmatch(r"open(?:\s+[A-Za-z_][A-Za-z0-9_.]*)+", stripped):
            prelude.append(stripped)
            continue
        raise LeanGenerationError("Only Mathlib imports and simple open commands are allowed")

    theorem_source = "\n".join(lines[theorem_index:]).strip()
    if ":=" not in theorem_source or not re.search(r"\bby\b", theorem_source):
        raise LeanGenerationError("Generated theorem must include a proof")
    theorem_name_match = re.match(
        r"^(?:theorem|lemma)\s+"
        r"(?P<name>[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*)\b",
        theorem_source,
    )
    if theorem_name_match is None:
        raise LeanGenerationError("Generated theorem has an invalid name")
    for line in theorem_source.splitlines()[1:]:
        if line and not line[0].isspace():
            raise LeanGenerationError("Generated theorem cannot contain additional top-level commands")

    audited_name = f"llm_natural_language_proof_{uuid4().hex}"
    theorem_source = (
        theorem_source[:theorem_name_match.start("name")]
        + audited_name
        + theorem_source[theorem_name_match.end("name"):]
    )
    source_parts = [
        "import Mathlib",
        *prelude,
        theorem_source,
        f"#print axioms {audited_name}",
    ]
    return "\n\n".join(part for part in source_parts if part)


def _chat_completions_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LeanGeneratorNotConfigured("KIMINA_BASE_URL must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LeanGeneratorNotConfigured("KIMINA_BASE_URL cannot include credentials or query data")
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        return base_url
    return f"{base_url.rstrip('/')}/chat/completions"
