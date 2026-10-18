# LEAN Engine

LEAN Engine is an HTTP service for symbolic computation and machine-checked mathematics. It routes supported requests to SymPy, Z3, Lean 4 with Mathlib, and optional data providers.

The service is an early, bounded implementation of Wolfram|Alpha-style APIs. It does not run arbitrary Wolfram Language code or provide full Wolfram|Alpha parity.

## Quick start

On Windows, install Python 3.11 or newer and [elan](https://github.com/leanprover/elan) so the repository's Lean toolchain can be installed.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .

# Fetch Mathlib dependencies and its precompiled cache on first setup
Set-Location lean
lake update
lake exe cache get
Set-Location ..

.\scripts\dev.ps1 -Port 8000
```

The API runs at `http://127.0.0.1:8000`. The development script creates a local API token in the git-ignored `.dev-api-token` file. Set `API_BEARER_TOKEN` before starting the server to provide your own token.

Check `GET /healthz` for process health, `GET /readyz` for dependency readiness, and `GET /docs` for interactive API documentation. Health and documentation routes are public; the computation routes require a bearer token.

## Make a request

This PowerShell example asks the Wolfram Language compatibility route to differentiate `Log[x]`:

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

The response includes the computed result and, where available, Lean proof evidence. This derivative is valid under the domain condition `x > 0`. Other supported calculus operations report their own domain conditions.

## API overview

| Route | Purpose |
| --- | --- |
| `POST /api/cag/v1/WolframLanguageCompute` | Evaluate supported Wolfram Language expressions with CAG-style request options. |
| `POST /v1/prove`, `POST /v1/prove-formula` | Check typed claims and bounded arithmetic formulas. |
| `POST /v1/counterexample` | Search supported claims for counterexamples. |
| `POST /v1/symbolic`, `GET /v2/query` | Run symbolic operations or request Full Results-style JSON/XML. |
| `POST /v1/natural-language/prove` | Draft a Lean theorem from a natural-language problem, then compile and audit it. |
| `GET /v1/interpret`, `GET /v2/assumptions` | Inspect interpretations and versioned assumption IDs. |
| `POST /v1/knowledge/query` | Query configured factual data providers. |

`GET /v1/capabilities` describes supported operations, input limits, and configured backends.

## Optional integrations

### Natural-language theorem drafting

The service can call an OpenAI-compatible inference endpoint to draft Lean source. Start a model server separately, then configure:

```powershell
$env:KIMINA_BASE_URL = 'http://127.0.0.1:8081/v1'
$env:KIMINA_MODEL = 'Kimina-Prover-RL-0.6B.i1-Q6_K.gguf'
# Set this only if the endpoint requires authentication
$env:KIMINA_API_KEY = 'your-key'
```

The [Kimina-Prover GGUF model](https://huggingface.co/mradermacher/Kimina-Prover-RL-0.6B-i1-GGUF) is one option for local serving. Lean checks the generated theorem, but the service does not independently verify that the theorem faithfully captures the original natural-language request.

### Factual data providers

Available adapters cover Wikidata, DBnomics, NIST CODATA, GeoNames, and Open-Meteo. GeoNames requires `GEONAMES_USERNAME`. Provider results may include source metadata; retrieval does not make those results Lean-proved facts.

### Assumptions

Ambiguous inputs can have multiple typed interpretations. Inspect candidates with `/v1/interpret`, then pass a service-owned assumption ID to `/v2/query`, for example `assumption=angle:degrees`. The service does not silently change a claim's assumptions just to make it provable.

## Proof and coverage limits

- Each route supports a bounded input grammar. Unsupported expressions are rejected rather than evaluated as arbitrary code.
- A Lean success means Lean checked the formal statement that was submitted. For model-generated statements, it does not prove the translation from natural language was correct.
- Symbolic results and retrieved provider data are not automatically Lean proofs.
- Wolfram|Alpha compatibility is partial. `FreeformPrompt`, unrestricted natural-language math, arbitrary graphics, and general Wolfram Language evaluation are outside the supported subset.

## Project references

- [Feature matrix](features.json)
- [Wolfram API parity audit](WolframAlpha-API-Feature-Parity.md)
- [Wolfram API reference (Markdown)](WolframAlpha-API-Documentation.md) · [HTML](WolframAlpha-API-Documentation.html)
