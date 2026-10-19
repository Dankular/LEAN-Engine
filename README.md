# LEAN Engine

LEAN Engine is an HTTP API for symbolic computation, selected factual lookups, and machine-checked mathematical proofs. It uses SymPy, Z3, and Lean 4 with Mathlib.

This is an early, bounded implementation. It does not run arbitrary Wolfram Language or match Wolfram|Alpha's full coverage. See the [feature parity audit](WolframAlpha-API-Feature-Parity.md) for the detailed status.

## Run locally

Requirements: Windows, Python 3.11+, and [elan](https://github.com/leanprover/elan).

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .

# First run: download Mathlib and its compiled cache
Set-Location lean
lake update
lake exe cache get
Set-Location ..

.\scripts\dev.ps1
```

The API listens on `http://127.0.0.1:8000`. The dev script creates a local bearer token in `.dev-api-token`. Open [`/docs`](http://127.0.0.1:8000/docs) for the interactive API reference.

## Try a computation

This calls the Wolfram-compatible computation route with a supported differentiation request:

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

Supported results include Lean evidence. For this example, the derivative is checked under the real-domain condition `x > 0`.

## API areas

- **Proofs and counterexamples:** typed claims and bounded arithmetic formulas, checked with Lean or searched with Z3.
- **Symbolic computation:** a bounded expression grammar for algebra, calculus, and solving.
- **Wolfram-compatible route:** a supported subset of `WolframLanguageCompute`, including exact arithmetic, selected calculus, Lean-checked rational-function limits at rational or bounded real-polynomial points, selected removable singularities with explicit or Lean-verified factorizations of bounded polynomial denominators and conditions for factors that stay nonzero at the point, finite sums/products, real polynomial solving, and bounded linear systems.
- **Natural-language proofs:** a configured model can draft a Lean theorem; Lean checks the theorem, but the service cannot guarantee it captures the user's original wording.
- **Queries and assumptions:** Full Results-style query responses, interpretation candidates, and explicit service-owned assumption IDs.
- **Factual lookups:** configured Wikidata, DBnomics, NIST CODATA, GeoNames, and Open-Meteo adapters. Retrieved facts include source data; they are not Lean proofs.

Computation routes require the bearer token. `/healthz`, `/readyz`, and `/docs` are public. GeoNames lookups also require `GEONAMES_USERNAME`.

Natural-language theorem generation requires an OpenAI-compatible model server. The dev script defaults to a local server at port 8081; override these before startup if needed:

```powershell
$env:KIMINA_BASE_URL = 'http://127.0.0.1:8081/v1'
$env:KIMINA_MODEL = 'Kimina-Prover-RL-0.6B.i1-Q6_K.gguf'
# Optional, if the model server requires authentication
$env:KIMINA_API_KEY = 'your-key'
```

## Project documentation

- [Wolfram|Alpha feature parity audit](WolframAlpha-API-Feature-Parity.md)
- [Saved Wolfram API documentation](WolframAlpha-API-Documentation.md) ([HTML](WolframAlpha-API-Documentation.html))
- [Machine-readable feature list](features.json)
