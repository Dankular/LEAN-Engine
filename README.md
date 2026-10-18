# LEAN Engine

An API for bounded symbolic computation and machine-checked mathematics. SymPy and Z3 generate or search for results; Lean 4 with Mathlib checks supported proofs. The service also includes natural-language theorem drafting, typed assumptions, and factual-data adapters.

This is a scoped implementation, not full Wolfram|Alpha parity. See the [feature matrix](features.json) and [parity audit](WolframAlpha-API-Feature-Parity.md) for supported operations and known gaps.

## Run locally

Requirements: Python 3.11+, Lean 4, and the repository's Mathlib project.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .

# Prepare Mathlib if needed
Set-Location lean
lake update
lake exe cache get
Set-Location ..

.\scripts\dev.ps1 -Port 8000
```

The server listens on `http://127.0.0.1:8000`. The development script creates a local API token in the ignored `.dev-api-token` file. Set `API_BEARER_TOKEN` before starting the script to supply your own. `/healthz`, `/readyz`, `/v1/capabilities`, and `/docs` are public; `/readyz` reports whether Lean and Mathlib are available.

## Make a computation request

The Wolfram-compatible route accepts a bounded subset of Wolfram Language syntax and returns Lean evidence for supported computations:

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

The response includes the result and an `evidence` object with the Lean source and axiom audit. Domain requirements are explicit: `D[Log[x], x]` yields `1/x` under `x > 0`, and `D[Tan[x], x]` requires `Cos[x] ≠ 0`; tangent poles are rejected. The route also accepts `line`, `maxChars`, and `timeConstraint`.

## API routes

Protected routes use `Authorization: Bearer <token>`.

| Route | Use |
| --- | --- |
| `POST /api/cag/v1/WolframLanguageCompute` | Compute supported Wolfram Language expressions and return Lean evidence. |
| `POST /v1/prove`, `POST /v1/prove-formula` | Prove typed claims or bounded polynomial comparisons. |
| `POST /v1/counterexample` | Search for supported counterexamples and replay them exactly. |
| `POST /v1/symbolic`, `GET /v2/query` | Run supported symbolic operations or request Full Results-style JSON/XML. |
| `POST /v1/natural-language/prove` | Generate a Lean theorem with a configured model, then compile and audit it. |
| `GET /v1/interpret`, `GET /v2/assumptions` | Inspect candidate interpretations and versioned assumption IDs. |
| `POST /v1/knowledge/query` | Query the configured Wikidata, DBnomics, NIST, GeoNames, or Open-Meteo adapters. |

For request schemas and interactive API documentation, open `http://127.0.0.1:8000/docs` while the service is running.

## Natural-language theorem drafting

The model proposes Lean source; the service filters it, compiles it, and audits the resulting proof. A successful proof certifies the generated theorem, not that the model translated the original English correctly. Configure `KIMINA_BASE_URL`, `KIMINA_MODEL`, and optionally `KIMINA_API_KEY` for an OpenAI-compatible inference server. The linked GGUF model can be served locally with `llama.cpp`.

## Assumptions and data

Interpretations have service-owned, versioned IDs. For example, `sin(30)` can use radians or degrees; callers can choose `assumption=angle:degrees`. The service exposes proposed assumptions so callers can inspect or override them. Retrieved provider data includes source metadata where available; retrieval is not a Lean proof. GeoNames queries require `GEONAMES_USERNAME`.

## Scope and references

The symbolic and Wolfram-compatible routes support bounded grammars. They do not evaluate arbitrary Wolfram Language code, `FreeformPrompt`, graphics, or unrestricted natural-language mathematics. Unsupported inputs return an error instead of a guessed result. Lean checks only the formal statement supplied to it; source facts and translation fidelity require separate review.

- [Wolfram API reference (HTML)](WolframAlpha-API-Documentation.html)
- [Wolfram API reference (Markdown)](WolframAlpha-API-Documentation.md)
- [Feature matrix](features.json)
- [Wolfram API parity audit](WolframAlpha-API-Feature-Parity.md)
