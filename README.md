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

## Run API contract checks

Install the test client dependency and run the integration checks:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

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
- **Wolfram-compatible route:** a supported subset of `WolframLanguageCompute`, including exact arithmetic, Lean-checked additive and doubled/tripled sine/cosine `TrigExpand`, selected calculus (including exact π-bound integrals of rational-frequency sine and cosine with rational `Pi` phase shifts, including ±Pi/4 bounds with Lean-checked `sqrt(2)` results and ±Pi/8 and ±Pi/16 bounds with nested radicals), rational-function limits at rational or bounded real-polynomial points, selected removable singularities with explicit or Lean-verified factorizations of bounded polynomial denominators and conditions for factors that stay nonzero at the point, finite sums/products, real polynomial solving, and bounded linear systems.
- **Natural-language proofs:** a configured model can draft a Lean theorem; Lean checks the theorem, but the service cannot guarantee it captures the user's original wording.
- **Queries and assumptions:** Full Results-style query responses, interpretation candidates, and explicit service-owned assumption IDs.
- **Factual lookups:** configured Wikidata, DBnomics dataset search and series retrieval, NIST CODATA, GeoNames, and Open-Meteo adapters. Retrieved facts include source data; they are not Lean proofs. DBnomics search returns dataset candidates and leaves selection to the caller.

### Assumption overrides

`/v2/query` returns candidate assumptions and applies service defaults unless the caller selects an alternative. A query-wide ID applies to every matching ambiguous call; `@N` selects one-based occurrence `N` for angle, parenthesized-log, or unparenthesized `Function` log calls. For example, this selects degrees for the first trig call while leaving later calls at the default:

```text
GET /v2/query?input=sin%2830%29%2Bcos%2860%29&assumption=angle%3Adegrees%401&output=json
```

Per-occurrence overrides are also available through repeated `assumption` parameters. For one assumption type, do not combine a query-wide choice with per-occurrence choices. A `Function` override changes `log 20` style calls; `LogBase` changes `log(20)` calls.

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
