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
- **Wolfram-compatible route:** a supported subset of `WolframLanguageCompute`, including exact arithmetic, Lean-checked additive and integer-multiple sine/cosine `TrigExpand` through 12x, selected calculus (including exact π-bound integrals of rational-frequency sine and cosine with rational `Pi` phase shifts, including ±Pi/4 bounds with Lean-checked `sqrt(2)` results and ±Pi/8, ±Pi/16, and ±Pi/32 bounds with nested radicals), rational-function limits at rational or bounded real-polynomial points, selected removable singularities with explicit or Lean-verified factorizations of bounded polynomial denominators and conditions for factors that stay nonzero at the point, finite sums/products, real polynomial solving through degree eight for polynomials that factor completely over the rationals, and bounded linear systems.
- **Exact trigonometric constants:** the CAG endpoint returns Lean-checked Sin and Cos values for selected rational Pi multiples, including fifths and tenths of Pi, powers of two through Pi/4096, and up to eight repeated half-angle reductions from Pi/6 and Pi/10, with supported half-pi complements. Tan is derived as an exact Sin/Cos quotient wherever both values are supported and the cosine is nonzero; the result is returned in quotient form. Angles outside the supported reductions and tangent poles remain unsupported.
- **Natural-language proofs:** the first request returns a Lean theorem draft for review and does not compile it. A second request must explicitly approve the same draft ID and formal-statement digest before Lean checks it. This makes the formalization visible and reviewable; neither caller approval nor Lean establishes that the theorem preserves the original English meaning.
- **Queries and assumptions:** Full Results-style query responses, interpretation candidates, and explicit service-owned assumption IDs. The `cell` format returns a text-backed Wolfram `Cell[...]` wrapper for mathematical and provider subpods; rich Wolfram notebook typesetting is not reproduced. `verify=true` adds Lean evidence for supported computations, including bounded sums, products, and rational scaling of exact sine, cosine, and tangent values at supported rational multiples of pi; the Result pod uses the exact value Lean checked. `async=true` returns both a primary-pod polling URL and a query-level URL for retrieving the complete result envelope from the same computation. Scan timeouts can return a bounded in-process recalculation URL; continuation reruns the single pending result with a larger budget. Per-pod `podtimeout` and cumulative `formattimeout` omit bounded render outputs that exceed their budgets; they do not interrupt a render already in progress. Final XML/JSON serialization occurs after those pod-rendering budgets.
- **Factual lookups:** configured Wikidata entity, country, continent, capital, official-language, currency, population, coordinates, elevation, area, and GDP lookups; DBnomics dataset search, dataset-scoped series discovery, and series retrieval; NIST CODATA; GeoNames; and Open-Meteo adapters. Retrieved facts include source data; they are not Lean proofs. DBnomics dataset and series searches return candidates and leave selection to the caller.

`/v2/query` also accepts Wolfram-style `N[expression]` and `N[expression, precision]` calls for finite numeric expressions in the bounded parser. `N[expression]` defaults to 16 digits; explicit precision is limited to 1–500 digits. These are SymPy numeric approximations, not Lean proofs. Decimal input literals use the service's exact base-10 semantics.

The CAG endpoint supports `N[expression]` and `N[expression, precision]` for exact rationals, square roots of nonnegative rationals, and exact `Sin`/`Cos`/`Tan` results that reduce to rational linear combinations or positive-denominator quotients of supported nested square roots, up to 500 significant digits. It also certifies bounded expressions involving `Pi`, including square roots, sign-definite products, integer powers up to 16 when the base interval has a proven sign, and quotients whose denominator interval is strictly positive or negative, using rational intervals from Mathlib. This includes wholly negative bases and denominators; products whose factor intervals cross zero, nonzero powers whose base intervals cross zero, and quotients whose denominator intervals cross zero are declined. Affine expressions in `Pi` with rational coefficients are supported up to 20 digits; other `Pi` expressions are accepted only when the available interval proves the requested error bound. High precision near cancellation or with large coefficients may be rejected. Lean checks that `evidence.numeric_approximation.value` differs from the exact value by no more than `evidence.numeric_approximation.absolute_error_bound`; the default is 16 digits. Other algebraic or transcendental inputs are not yet supported by this certified route.

After choosing a DBnomics dataset, discover matching series with input such as `series search: GDP within OECD/DP_LIVE`. `/v2/query` returns five candidates per page; use `seriesoffset` to page through results. `/v1/knowledge/query` accepts the same provider, dataset, and phrase with an `offset` field.

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

The natural-language proof route uses a two-request review flow. First, request a draft:

```powershell
$draftBody = @{ problem = 'For every real x, x + 0 = x.' } | ConvertTo-Json
$draft = Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8000/v1/natural-language/prove' `
  -Method Post `
  -Headers @{ Authorization = "Bearer $token" } `
  -ContentType 'application/json' `
  -Body $draftBody

# Inspect $draft.formalization.lean_source before approving it.
```

After reviewing the theorem statement, approve that exact draft:

```powershell
$approvalBody = @{
  problem = $draft.problem
  draft_id = $draft.formalization.draft_id
  formalization_sha256 = $draft.formalization.sha256
  approve_formalization = $true
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8000/v1/natural-language/prove' `
  -Method Post `
  -Headers @{ Authorization = "Bearer $token" } `
  -ContentType 'application/json' `
  -Body $approvalBody
```

Drafts expire after 15 minutes and are held in process memory, so both requests must reach the same running API process.

## Project documentation

- [Wolfram|Alpha feature parity audit](WolframAlpha-API-Feature-Parity.md)
- [Saved Wolfram API documentation](WolframAlpha-API-Documentation.md) ([HTML](WolframAlpha-API-Documentation.html))
- [Machine-readable feature list](features.json)
