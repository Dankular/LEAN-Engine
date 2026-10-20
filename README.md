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

The Wolfram-compatible CAG route also accepts `FreeformPrompt["France population"]` and the leading `=` prompt form for supported provider-backed natural-language lookups and bounded natural-language math requests. Retrieved facts retain provider provenance and are not Lean proofs; supported math requests can return Lean verification evidence. Unsupported or ambiguous prompts return suggestions.

The CAG route renders bounded 2D `Graphics` expressions containing circles, disks, rectangles, lines, arrows, polygons, and points as service-hosted SVG image URLs. Arrow paths receive a default endpoint head. It supports a curated color/style set and plot-range sizing. It does not execute arbitrary Wolfram graphics code, and graphics are not Lean proofs.

An optional OpenAI-compatible query model can propose typed symbolic operations when a math request does not match the deterministic templates. Configure it before starting the service:

```powershell
$env:QUERY_INTERPRETER_BASE_URL = 'http://127.0.0.1:8082/v1'
$env:QUERY_INTERPRETER_MODEL = 'your-instruct-model'
# Optional, if the model server requires authentication
$env:QUERY_INTERPRETER_API_KEY = 'your-key'
```

The model can propose only an allowlisted operation and formula fields. The bounded parser and symbolic engine validate them before execution, and `verify=true` can ask Lean to check the resulting formula. The response identifies model-generated candidates and states that English-to-formula fidelity is unverified. The model cannot choose assumption IDs or submit arbitrary code. With these settings absent, the deterministic planner remains in use.

- **Proofs and counterexamples:** typed claims and bounded arithmetic formulas, checked with Lean or searched with Z3.
- **Symbolic computation:** a bounded expression grammar for algebra, calculus, and solving.
- **Wolfram-compatible route:** a supported subset of `WolframLanguageCompute`, including exact arithmetic, Lean-checked additive and integer-multiple sine/cosine `TrigExpand` through 12x, selected calculus (including exact π-bound integrals of rational-frequency sine and cosine with rational `Pi` phase shifts, including ±Pi/4 bounds with Lean-checked `sqrt(2)` results and ±Pi/8, ±Pi/16, and ±Pi/32 bounds with nested radicals), rational-function limits at rational or bounded real-polynomial points, selected removable singularities with explicit or Lean-verified factorizations of bounded polynomial denominators and conditions for factors that stay nonzero at the point, finite sums/products, real polynomial solving through degree eight for polynomials that factor completely over the rationals, and bounded linear systems.
- **Exact trigonometric constants:** the CAG endpoint returns Lean-checked Sin and Cos values for selected rational Pi multiples, including fifths and tenths of Pi, powers of two through Pi/4096, and up to eight repeated half-angle reductions from Pi/6 and Pi/10, with supported half-pi complements. Tan is derived as an exact Sin/Cos quotient wherever both values are supported and the cosine is nonzero; the result is returned in quotient form. Angles outside the supported reductions and tangent poles remain unsupported.
- **Natural-language proofs:** the first `/v1/natural-language/prove` request returns a Lean theorem draft for review and does not compile it. A second request must explicitly approve the same draft ID and formal-statement digest before Lean checks it. `/v2/query` also recognizes bounded explicit identity forms such as `Prove that x plus zero equals x` and `Prove that sin(pi/6) equals 1/2`; add `verify=true` for a Lean check. `formal_verification.claim_status` distinguishes proved identities from refutations using a nonzero constant difference, exact rational counterexample, or Lean-certified interval that excludes zero. Other identities remain `not_established`. These deterministic templates do not establish that the formalization preserves the original English meaning.
- **Natural-language coordinate calculus:** `/v2/query`, `/v2/validatequery`, and CAG `FreeformPrompt` accept explicit requests such as `Find the gradient of x^2 + y^2`, `Find the divergence of (x, y, z)`, `Find the curl of (0, 0, x*y)`, and `Find the Laplacian of x^2 + y^2 + z^2`. Scalar Laplacians and vector divergence, gradient, and curl are supported in Cartesian, cylindrical, and spherical coordinates. Add a chart phrase to select it; otherwise the registered default and assumption alternatives apply. `verify=true` checks the selected formula in Lean, including second derivatives for the Laplacian. It does not prove that the English request was formalized as intended.
- **Queries and assumptions:** Full Results-style query responses, interpretation candidates, and explicit service-owned assumption IDs. The `cell` format returns a text-backed Wolfram `Cell[...]` wrapper for mathematical and provider subpods; rich Wolfram notebook typesetting is not reproduced. `verify=true` adds Lean evidence for supported computations, including bounded sums, products, rational scaling of exact sine, cosine, and tangent values at supported rational multiples of pi, and certified numeric approximations for supported rational, radical, exact-trigonometric, and Pi interval expressions; numeric Result pods display the value Lean checked. `async=true` returns both a primary-pod polling URL and a query-level URL for retrieving the complete result envelope from the same computation. Scan timeouts can return a bounded in-process recalculation URL; continuation reruns the single pending result with a larger budget. Per-pod `podtimeout` and cumulative `formattimeout` omit bounded render outputs that exceed their budgets; they do not interrupt a render already in progress. Final XML/JSON serialization occurs after those pod-rendering budgets.

`/v2/query` exposes a `CoordinateSystem` assumption for bounded `div(f1,f2,f3)`, `grad(f)`, `curl(f1,f2,f3)`, and `laplacian(f)` inputs. They support Cartesian3D, Cylindrical3D, and Spherical3D interpretations. Each operator accepts rational-coefficient polynomials through total degree 16 and bounded real expressions using exact constants, arithmetic, and `sin`/`cos` calls (up to 256 nodes, depth 32, integer powers 0 through 16, and no variable denominators in the input field). Curl components use the orthonormal coordinate basis. Spherical coordinates use `r` for radius, `theta` for polar angle, and `phi` for azimuth; cylindrical coordinates use `rho` for radius, `theta` for azimuth, and `z` for the axis. Select an alternate chart with `assumption=coordinate:cylindrical-3d` or `assumption=coordinate:spherical-3d`. Cylindrical results report `rho > 0`; spherical results report `r > 0` and `sin(theta) != 0`. With `verify=true`, Lean checks the component or second derivatives and selected chart formula and audits theorem axioms. The proof covers the selected formula and chart conditions, not the English interpretation.
- **Factual lookups:** configured Wikidata entity, country, continent, capital, official-language, currency, population, coordinates, elevation, area, and GDP lookups; DBnomics dataset search, dataset-scoped series discovery, and series retrieval; NIST CODATA; GeoNames; and Open-Meteo adapters. Retrieved facts include source data; they are not Lean proofs. DBnomics dataset and series searches return candidates and leave selection to the caller.

`/v2/query` also accepts Wolfram-style `N[expression]` and `N[expression, precision]` calls for finite numeric expressions in the bounded parser. `N[expression]` defaults to 16 digits; explicit precision is limited to 1–500 digits. These use SymPy by default without Lean proofs. With `verify=true`, supported certified numeric expressions include a Lean-proved absolute-error bound and the Result pod displays the certified decimal. Decimal input literals use the service's exact base-10 semantics.

The CAG endpoint supports `N[expression]` and `N[expression, precision]` for exact rationals, square roots of nonnegative rationals, and exact `Sin`/`Cos`/`Tan` results that reduce to rational linear combinations or positive-denominator quotients of supported nested square roots, up to 500 significant digits. It also certifies bounded expressions involving `Pi`, including square roots, sign-definite products, integer powers up to 16 when the base interval has a proven sign, and quotients whose denominator interval is strictly positive or negative, using rational intervals from Mathlib. This includes wholly negative bases and denominators; products whose factor intervals cross zero, nonzero powers whose base intervals cross zero, and quotients whose denominator intervals cross zero are declined. Affine expressions in `Pi` with rational coefficients support up to 100 significant digits. Other interval-evaluated `Pi` expressions can also reach 100 digits when generated rational bounds prove the requested error; at higher precision or near cancellation, the service may decline the request. Mathlib's built-in 20-digit bounds are used through 20 digits; for higher precision, candidate rational intervals are generated and their bounds are proved with Mathlib's `pi_lower_bound` and `pi_upper_bound` tactics. Lean checks that `evidence.numeric_approximation.value` differs from the exact value by no more than `evidence.numeric_approximation.absolute_error_bound`; the default is 16 digits. Other algebraic or transcendental inputs are not yet supported by this certified route.

After choosing a DBnomics dataset, discover matching series with input such as `series search: GDP within OECD/DP_LIVE`. `/v2/query` returns five candidates per page; use `seriesoffset` to page through results. `/v1/knowledge/query` accepts the same provider, dataset, and phrase with an `offset` field.

### Assumption overrides

`/v2/query` returns candidate assumptions and applies service defaults unless the caller selects an alternative. A query-wide ID applies to every matching ambiguous call; `@N` selects one-based occurrence `N` for angle, log, function, or ambiguous `m` unit interpretations. For example, this selects degrees for the first trig call while leaving later calls at the default:

```text
GET /v2/query?input=sin%2830%29%2Bcos%2860%29&assumption=angle%3Adegrees%401&output=json
```

Per-occurrence overrides are also available through repeated `assumption` parameters. For one assumption type, do not combine a query-wide choice with per-occurrence choices. A `Function` override changes `log 20` style calls; `LogBase` changes `log(20)` calls.

Numeric `m` tokens can also be disambiguated inside formulas. This selects minutes for the second occurrence in `10 m * 5 m`:

```text
GET /v2/query?input=10%20m%2A5%20m&assumption=unit%3Am-minutes%402&output=json
```

The service checks unit dimensions after applying the choices; incompatible units in an addition are rejected.

Wikidata property lookups expose dynamic `WikidataEntity` assumptions. The response lists the current ranked candidates as stable `entity:wikidata:Q…` IDs. Select one with a repeated `assumption` parameter, for example:

```text
GET /v2/query?input=population%20of%20Springfield&assumption=entity%3Awikidata%3AQ90&output=json
```

The selected QID must be in the current candidate set. Without a selection, the response keeps all matches and leaves `WikidataEntity.selected` unset. `/v2/validatequery` checks the selector format and defers candidate membership validation until query execution. The dynamic assumption scheme and entry version are available from `/v2/assumptions`.
When a selected interpretation fails that check, `/v2/query` and `/v2/validatequery` retain the selected assumption IDs and typed interpretation with the rejection reason.

The curated physical-unit vocabulary also works in compound expressions when spelled explicitly, for example `10 m + 5 cm` or `3 kg + 500 g`. These units are dimension-checked before computation; incompatible dimensions are rejected.

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
