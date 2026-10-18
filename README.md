# LEAN Engine

An API for symbolic computation, constraint solving, Lean proof checking, and source-backed facts. It gives LLM clients a way to request computations and inspect the formal evidence returned by the service.

## What it does

- **SymPy** proposes results for a bounded mathematical grammar.
- **Z3** searches supported polynomial claims for counterexamples.
- **Lean 4 + Mathlib** checks generated proof terms and reports the axiom audit.
- **Wikidata, DBnomics, NIST CODATA, GeoNames, and Open-Meteo** provide attributed factual data.
- **Kimina-compatible models** can generate Lean theorems from natural-language problems.

Lean certifies the formal statement it receives. It does not by itself certify that a model translated the caller’s English correctly or that a retrieved fact is true.

The service implements bounded slices of these tools. See the [feature matrix](features.json) and [Wolfram API parity audit](WolframAlpha-API-Feature-Parity.md) for the detailed coverage and gaps.

## Run locally

Requirements: Python 3.11 or newer, Lean 4, and the repository’s Mathlib project.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
.\scripts\dev.ps1 -Port 8000
```

The development script creates a local bearer token in the ignored `.dev-api-token` file. It prints no token. The API listens on `http://127.0.0.1:8000` until stopped with Ctrl+C. Set `API_BEARER_TOKEN` yourself to use a chosen token.

If Mathlib has not been prepared yet, run this from the repository root:

```powershell
Set-Location lean
lake update
lake exe cache get
Set-Location ..
```

Set `LAKE_BIN` if `lake` is not on `PATH`, or `LEAN_PROJECT_DIR` to select a prepared Lean project. The `/readyz` endpoint reports whether the Lean toolchain and project are available.

## API map

Protected routes use `Authorization: Bearer <token>`. The Compute-compatible route also accepts the raw configured token. Health, readiness, capabilities, and OpenAPI discovery are public.

| Route | Purpose |
| --- | --- |
| `POST /v1/prove` | Prove a typed claim using the configured solver and Lean checker. |
| `POST /v1/prove-formula` | Submit a bounded polynomial comparison as formula text. |
| `POST /v1/counterexample` | Search for and exactly replay a supported counterexample. |
| `POST /v1/natural-language/prove` | Ask a configured model to generate a Lean theorem, then compile and audit it. |
| `POST /v1/symbolic` | Run a supported SymPy operation. |
| `GET /v1/interpret` | Inspect candidate meanings and typed assumption IDs. |
| `GET /v2/query` | Return a Wolfram Full Results-style JSON or XML response. |
| `GET /v2/validatequery` | Parse a supported query and expose its proposed interpretation. |
| `POST /v1/knowledge/query` | Query one of the supported factual data providers. |
| `POST /api/cag/v1/WolframLanguageCompute` | Compute a bounded Wolfram Language-compatible expression and return Lean evidence. |
| `GET /healthz`, `GET /readyz`, `GET /v1/capabilities` | Inspect service health and supported operations. |

## Prove a formula

`/v1/prove-formula` accepts bounded polynomial comparisons. Variables must have declared `Int` or `Real` sorts.

```bash
curl -X POST http://127.0.0.1:8000/v1/prove-formula \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"formula":"x^2 >= 0","variables":[{"name":"x","sort":"Real"}]}'
```

The response distinguishes a Z3 result from a Lean-checked proof. `verified` is returned only when Lean compiles the theorem and the axiom audit passes. Counterexamples are returned only when the service can replay them with exact integer or rational arithmetic.

## Natural language to Lean

Set `KIMINA_BASE_URL` to an OpenAI-compatible chat-completions API and `KIMINA_MODEL` to the model name. `KIMINA_API_KEY` is optional for local servers.

For the linked Kimina GGUF model, start a local llama.cpp server and configure the API process:

```powershell
llama-server -m ".models\kimina-prover\Kimina-Prover-RL-0.6B.i1-Q6_K.gguf" --host 127.0.0.1 --port 8081 -c 8192 -ngl 99
$env:KIMINA_BASE_URL = "http://127.0.0.1:8081/v1"
$env:KIMINA_MODEL = "Kimina-Prover-RL-0.6B.i1-Q6_K.gguf"
```

The service filters the generated source, compiles it with Lean, and audits its axioms. A `proof_checked` result certifies the generated theorem only; review the theorem statement against the original problem. The model route does not currently establish natural-language fidelity.

## Symbolic and Full Results queries

`/v1/symbolic` supports bounded simplify, factor, expand, differentiate, integrate, limits, Taylor series, finite sums and products, solve, identity checks, and plots. Inputs use a safe expression grammar, with limited LaTeX and presentation MathML support.

`/v2/query` accepts explicit forms such as:

```text
differentiate(x**2, x)
integrate(x**2, x, 0, 1)
limit(sin(x)/x, x, 0)
```

Use `output=json` or `output=xml`. Add `verify=true` to request Lean evidence for the supported proof subset. The proof covers the selected formula and computed result; it does not prove that a natural-language query was interpreted correctly. `format`, query controls, timeouts, image behavior, assumption families, and known compatibility gaps are described in the [parity audit](WolframAlpha-API-Feature-Parity.md).

## Assumptions and factual data

Interpretation choices have service-owned, versioned IDs. For example, `sin(30)` can be interpreted in radians or degrees; callers can select `assumption=angle:degrees`. A classifier may rank candidates, but it does not silently choose an assumption because that choice makes a claim easier to prove.

The knowledge API supports a bounded set of Wikidata properties, DBnomics series, NIST CODATA constants, GeoNames searches, and Open-Meteo weather. Responses include source metadata where available. Retrieved facts are source data, not Lean proofs. GeoNames requires `GEONAMES_USERNAME`.

## Wolfram Language Compute-compatible route

The route accepts `code`, `line`, `maxChars`, and `timeConstraint`. A successful response includes the familiar `result`, `code`, `success`, and `uuid` fields, plus `evidence` containing the Lean source, its hash, and an axiom audit.

Supported examples include exact rational arithmetic, bounded polynomial `Simplify`/`Expand`/`Factor`, selected real derivatives, and Lean-checked indefinite or definite integrals:

```json
{"code":"D[Sin[x^2], x]"}
```

Variable-denominator derivatives carry explicit nonzero conditions in `evidence.domain_conditions`. For example, `D[1/x, x]` returns `-1/x^2` with the condition `x ≠ 0`. Free variables are treated as real in the calculus route. Indefinite and definite integration still require the supported constant-denominator grammar and a Lean-representable antiderivative.

This route does not evaluate arbitrary Wolfram Language code, `FreeformPrompt`, graphics, or remote Wolfram data requests. Unsupported inputs return HTTP 501. Decimal literals use exact base-10 rational semantics.

## Evidence and limitations

- The symbolic backend proposes a result; Lean must check a supported proof before the Compute endpoint returns it.
- The default axiom audit allows `propext`, `Classical.choice`, and `Quot.sound`.
- The natural-language theorem route checks formal syntax and proof validity, not translation fidelity.
- Provider results remain retrieved data, with provenance and retrieval time where available.
- Broad nonlinear reasoning, unrestricted natural-language formalization, arbitrary Wolfram Language evaluation, and full Wolfram API parity are outside the current supported slice.

The local Wolfram API reference is available as [HTML](WolframAlpha-API-Documentation.html) and [Markdown](WolframAlpha-API-Documentation.md). The project’s tracked capabilities are in [features.json](features.json).
