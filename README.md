# LEAN Engine

LEAN Engine is a local HTTP service for symbolic computation, factual lookups, and machine-checked proofs. It combines SymPy, Z3, and Lean 4 with Mathlib behind a small API.

This is an early, bounded implementation. It does not execute arbitrary Wolfram Language or provide full Wolfram|Alpha coverage. See the [feature parity audit](WolframAlpha-API-Feature-Parity.md) for the detailed status of each API feature.

## Quick start

Prerequisites: Windows, Python 3.11 or newer, and [elan](https://github.com/leanprover/elan) for installing and managing Lean.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .

# First-time setup: fetch Mathlib and its compiled cache
Set-Location lean
lake update
lake exe cache get
Set-Location ..

.\scripts\dev.ps1
```

The API starts at `http://127.0.0.1:8000`. The script creates a local token in `.dev-api-token`; provide your own by setting `API_BEARER_TOKEN` before startup. Open `http://127.0.0.1:8000/docs` for interactive API documentation.

## Make a request

This example asks the Wolfram-compatible route to differentiate `Log[x]`:

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

The response includes the derivative and Lean proof evidence when the request is within the supported subset. For this example, the proof states the domain condition `x > 0`.

## API map

| Route | Use |
| --- | --- |
| `POST /v1/prove`, `/v1/prove-formula` | Check supported typed claims and arithmetic formulas. |
| `POST /v1/counterexample` | Search supported claims for a counterexample. |
| `POST /v1/symbolic` | Run bounded symbolic math operations. |
| `POST /api/cag/v1/WolframLanguageCompute` | Evaluate the supported Wolfram Language subset. |
| `POST /v1/natural-language/prove` | Draft a Lean theorem from a natural-language request, then compile and audit it. |
| `GET /v2/query`, `/v2/validatequery` | Request Full Results-style output or validate an input and inspect interpretations. |
| `GET /v1/interpret`, `/v2/assumptions` | Inspect candidate meanings and service-owned assumption IDs. |
| `POST /v1/knowledge/query` | Query configured factual data providers. |
| `GET /v1/capabilities` | List supported operations and configured backends. |

`GET /healthz` reports process health and `GET /readyz` reports dependency readiness. These routes and `/docs` are public; computation routes require the API bearer token.

## How results are checked

- SymPy computes supported symbolic results. Where the route provides proof evidence, Lean checks the corresponding formal statement.
- Z3 searches supported constraint problems and can produce counterexamples.
- The natural-language route asks a configured model to propose Lean source. Lean checks that source, but does not establish that the generated theorem faithfully represents the original request.
- Factual providers return sourced data. Retrieved data is not automatically a Lean-proved fact.
- Ambiguous inputs can expose typed interpretations and versioned assumptions. Callers can select a supported assumption explicitly; the service does not narrow a claim just to make it provable.

## Optional integrations

The natural-language route can use an OpenAI-compatible model server. Set its endpoint and model before starting the API; the development script uses these defaults:

```powershell
$env:KIMINA_BASE_URL = 'http://127.0.0.1:8081/v1'
$env:KIMINA_MODEL = 'Kimina-Prover-RL-0.6B.i1-Q6_K.gguf'
# Set this if the model endpoint requires authentication
$env:KIMINA_API_KEY = 'your-key'
```

The [Kimina-Prover GGUF model](https://huggingface.co/mradermacher/Kimina-Prover-RL-0.6B-i1-GGUF) is one option for local serving. Factual data adapters include Wikidata, DBnomics, NIST CODATA, GeoNames, and Open-Meteo; GeoNames requires `GEONAMES_USERNAME`.

## Project references

- [Detailed API feature parity audit](WolframAlpha-API-Feature-Parity.md)
- [Saved Wolfram API documentation (Markdown)](WolframAlpha-API-Documentation.md) · [HTML](WolframAlpha-API-Documentation.html)
- [Machine-readable feature list](features.json)
