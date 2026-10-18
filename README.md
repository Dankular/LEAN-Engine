# LEAN Engine

An HTTP API for symbolic computation and machine-checked mathematics. SymPy produces
symbolic candidates, Z3 searches supported constraints, and Lean 4 with Mathlib checks
supported proofs.

This project implements a bounded subset of Wolfram|Alpha-style computation. Current
coverage and known gaps are summarized below.

## Quick start

Requirements: Python 3.11+, Lean 4, and the repository's Mathlib project.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .

# Prepare Mathlib on first run
Set-Location lean
lake update
lake exe cache get
Set-Location ..

.\scripts\dev.ps1 -Port 8000
```

The API listens at `http://127.0.0.1:8000`. The development script creates a local
token in the ignored `.dev-api-token` file. Set `API_BEARER_TOKEN` before startup to
use your own token.

`/healthz`, `/readyz`, `/v1/capabilities`, and `/docs` are public. `/readyz` reports
whether Lean and Mathlib are available.

## Make a computation request

Protected routes use `Authorization: Bearer <token>`. The CAG compatibility route also
accepts the configured token directly in `Authorization`. For example, in PowerShell:

```powershell
$token = (Get-Content .dev-api-token -Raw).Trim()
$body = @{ code = 'D[Log[x], x]' } | ConvertTo-Json

Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8000/api/cag/v1/WolframLanguageCompute' `
  -Method Post `
  -Headers @{ Authorization = "Bearer $token" } `
  -ContentType 'application/json' `
  -Body $body
```

The response includes the result and Lean evidence, including generated source and an
axiom audit. Calculus results carry explicit domain conditions. For example,
`D[Log[x], x]` requires `x > 0`, and `Integrate[1/x, x]` returns `log(Abs(x))` under
`x ≠ 0`. The derivative and antiderivative of `Tan[x]` require `Cos[x] ≠ 0`. The route
requires `-1 < argument < 1` for derivatives of `ArcSin[argument]` and
`ArcCos[argument]`. It accepts the `line`, `maxChars`, and `timeConstraint` options.
When the time limit is reached, it returns a message-bearing
`Failure["EvaluationTimeExceeded", ...]` result with `success: true`.

## API routes

| Route | Purpose |
| --- | --- |
| `POST /api/cag/v1/WolframLanguageCompute` | Compute supported Wolfram Language expressions and return Lean evidence. |
| `POST /v1/prove`, `POST /v1/prove-formula` | Prove typed claims or bounded polynomial comparisons. |
| `POST /v1/counterexample` | Search for supported counterexamples and replay them exactly. |
| `POST /v1/symbolic`, `GET /v2/query` | Run symbolic operations or request Full Results-style JSON/XML. |
| `POST /v1/natural-language/prove` | Draft a Lean theorem from natural language, then compile and audit it. |
| `GET /v1/interpret`, `GET /v2/assumptions` | Inspect candidate interpretations and versioned assumption IDs. |
| `POST /v1/knowledge/query` | Query configured knowledge providers. |

Open `http://127.0.0.1:8000/docs` for request schemas and interactive API documentation.
Supported operations are listed by `GET /v1/capabilities`.

## Optional integrations

Natural-language theorem drafting requires an OpenAI-compatible inference server. The
model proposes Lean source; the service compiles it and audits the proof. Configure
`KIMINA_BASE_URL` and `KIMINA_MODEL`, and set `KIMINA_API_KEY` if the server requires
authentication. The [Kimina-Prover GGUF model](https://huggingface.co/mradermacher/Kimina-Prover-RL-0.6B-i1-GGUF)
can be served locally with `llama.cpp`.

Knowledge adapters cover Wikidata, DBnomics, NIST constants, GeoNames, and Open-Meteo.
GeoNames requires `GEONAMES_USERNAME`. Retrieved provider data may include source
metadata, but retrieval is not a Lean proof.

Interpretations use service-owned, versioned assumption IDs. For an ambiguous input such
as `sin(30)`, callers can inspect or override the angle-unit assumption, for example
with `assumption=angle:degrees`.

## Limits and references

The symbolic and Wolfram-compatible routes accept bounded grammars. They do not evaluate
arbitrary Wolfram Language code, `FreeformPrompt`, graphics, or unrestricted
natural-language mathematics. Unsupported inputs return an error instead of a guessed
result.

Lean checks the formal statement supplied to it. A successful natural-language proof
does not establish that the model translated the original text correctly, and a
retrieved factual answer is not formally verified.

- [Feature matrix](features.json)
- [Wolfram API parity audit](WolframAlpha-API-Feature-Parity.md)
- [Wolfram API reference (HTML)](WolframAlpha-API-Documentation.html) · [Markdown](WolframAlpha-API-Documentation.md)
