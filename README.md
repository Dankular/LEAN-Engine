# Lean Proof and Computation Service

A service for LLM clients that separates symbolic computation, constraint solving, formal proof checking, and retrieval from factual data providers. Callers can submit a typed claim or bounded polynomial formula directly, or use `/v1/natural-language/prove` to ask a Lean model for a theorem and proof. Lean checks the generated source; it does not establish that the model preserved the natural-language meaning.

## Current coverage

- Bounded integer and real polynomial arithmetic, including products of variables and nonnegative integer powers.
- Equality, disequality, and ordered comparisons.
- Addition, subtraction, multiplication, and powers up to exponent 16 (maximum polynomial degree 32).
- Assumptions are conjoined. Declared free variables are universally quantified.
- Z3 checks polynomial constraints; counterexamples are replayed with exact Python integer/rational arithmetic.
- `verified` is returned only after Lean compiles the generated theorem and its axiom audit passes.
- Optional natural-language-to-Lean generation uses an OpenAI-compatible Kimina endpoint, applies a restricted source filter, then compiles the theorem with Lean and audits its axioms.
- SymPy 1.14 provides bounded simplify, factor, expand, differentiate, integrate, limit, Taylor series, finite integer sum/product, solve, and identity operations over a safe expression grammar. Mathematical expressions accept native syntax, a bounded presentation-LaTeX subset, or a bounded presentation-MathML subset.
- Factual retrieval adapters cover Wikidata entity search/records, individual DBnomics series, a pinned CODATA 2022 constants subset, GeoNames place search, and current Open-Meteo weather.
- Retrieved facts carry provider and retrieval-time metadata; they are never labeled as formal proofs.
- Lean uses `nlinarith` for nonlinear claims in this bounded polynomial fragment. General nonlinear real arithmetic and unrestricted natural-language formalization remain unimplemented. Async query jobs are an in-process initial slice and are lost when the service restarts.

See [features.json](features.json) for the machine-readable feature status.

## Start the API

Python 3.11 or newer is required. In PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
$env:API_BEARER_TOKEN = "replace-with-a-long-random-token"
uvicorn leanproof_service.api:app --app-dir src --host 127.0.0.1 --port 8000
```

The solver endpoints require `Authorization: Bearer <API_BEARER_TOKEN>`. Health, readiness, capabilities, and OpenAPI discovery are public. The service does not store submitted claims or log request bodies.

To start the API with the local development token setup, run:

```powershell
.\scripts\dev.ps1
```

It serves on `http://127.0.0.1:8000` by default and runs until stopped with Ctrl+C. The script creates a local bearer token in the git-ignored `.dev-api-token` file if `API_BEARER_TOKEN` is not already set. Pass `-Port 8001` to use another port. Lean and Mathlib are checked when a proof request runs.

## Enable Lean checking

Elan and the pinned Lean 4.35.0 compiler are installed on this machine. Prepare the Mathlib project from the repository root:

```powershell
Set-Location lean
lake update
lake exe cache get
Set-Location ..
```

If `lake` is not on `PATH`, set `LAKE_BIN`; to use another prepared Lean project, set `LEAN_PROJECT_DIR`. Until Mathlib is prepared, valid solver conclusions remain `solver_unsat`, never `verified`.

The generated Lean source is returned in `evidence.lean_source` for a solver-unsat claim. Lean checking accepts only the default dependencies `propext`, `Classical.choice`, and `Quot.sound`; unexpected axioms reject the kernel-check result.

## API example

This request says: for every integer `x`, if `x > 5`, then `2*x > 10`.

```http
POST /v1/prove
Authorization: Bearer replace-with-a-long-random-token
Content-Type: application/json
```

```json
{
  "variables": [{"name": "x", "sort": "Int"}],
  "assumptions": [
    {"op": "gt", "left": {"kind": "var", "name": "x"}, "right": {"kind": "number", "value": "5"}}
  ],
  "claim": {
    "op": "gt",
    "left": {
      "kind": "mul",
      "left": {"kind": "number", "value": "2"},
      "right": {"kind": "var", "name": "x"}
    },
    "right": {"kind": "number", "value": "10"}
  },
  "source_text": "For every integer x greater than 5, 2*x is greater than 10",
  "budget": {"wall_seconds": 30}
}
```

Use `/v1/counterexample` to run bounded counterexample search without invoking Lean. Both endpoints return the same evidence vocabulary. `solver_unsat` means Z3 found no counterexample in the encoded fragment; it is not a Lean certificate.

Expressions support bounded integer and real polynomials built from addition, subtraction, multiplication, and nonnegative integer powers. Powers are limited to 16 and total polynomial degree to 32. Nonlinear solving can return `unknown` or `solver_unsat` when Z3 or Lean cannot finish; these outcomes are not promoted to a formal proof. Counterexamples are emitted only when the exact integer or rational witness replays successfully. If Z3 finds only an algebraic irrational real witness, the response is `unknown`.

For example, encode `x² ≥ 0` with a `pow` term:

```json
{
  "variables": [{"name": "x", "sort": "Real"}],
  "claim": {
    "op": "ge",
    "left": {
      "kind": "pow",
      "base": {"kind": "var", "name": "x"},
      "exponent": 2
    },
    "right": {"kind": "number", "value": "0"}
  }
}
```

For a shorter formula-text request, use `POST /v1/prove-formula`. Declare each variable's arithmetic sort explicitly; the service parses a bounded polynomial comparison and returns the same Z3 and Lean evidence as `/v1/prove`.

```json
{
  "formula": "x^2 >= 0",
  "variables": [{"name": "x", "sort": "Real"}],
  "assumptions": []
}
```

It accepts one comparison per formula, exact integer/decimal constants, `+`, `-`, `*`, powers using `^` or `**`, and division by nonzero numeric constants. Each assumption is another comparison, conjoined with the others. Formula text is limited to 4,000 characters, 256 syntax nodes, and 32 levels of nesting. It does not parse natural language, function calls, chained comparisons, or division by a variable. Every variable in the claim or assumptions must be declared; a claim uses one shared `Int` or `Real` sort.

## Natural language to Lean

`POST /v1/natural-language/prove` asks a configured OpenAI-compatible model to produce a complete Lean 4 theorem from a natural-language problem. The service accepts one theorem or lemma, keeps only `Mathlib` imports and simple `open` commands, rejects unsafe or axiom-introducing constructs, then runs Lean with an axiom audit. The caller still sends this service's bearer token.

Configure the model with `KIMINA_BASE_URL` (the API root, such as `http://127.0.0.1:8081/v1`) and `KIMINA_MODEL`. `KIMINA_API_KEY` is optional for local servers and used as a bearer token when set. The request's `budget.wall_seconds` limits generation; `lean_timeout_seconds` separately sets the Lean compile cap (default 120, maximum 180) to allow for cold Mathlib imports. The Q6 quant linked here is stored under the ignored `.models/` directory; start its local server with:

```powershell
llama-server -m ".models\kimina-prover\Kimina-Prover-RL-0.6B.i1-Q6_K.gguf" --host 127.0.0.1 --port 8081 -c 8192 -ngl 99
$env:KIMINA_BASE_URL = "http://127.0.0.1:8081/v1"
$env:KIMINA_MODEL = "Kimina-Prover-RL-0.6B.i1-Q6_K.gguf"
```

```json
{
  "problem": "For every real x, x squared is nonnegative.",
  "budget": {"wall_seconds": 30},
  "lean_timeout_seconds": 120
}
```

The response includes `evidence.lean_source`, `evidence.lean_source_sha256`, the Lean status, and the axiom audit. `outcome == "proof_checked"` means Lean accepted the generated theorem and its axiom audit passed. It does not prove that the theorem preserves the English meaning; `formalization_fidelity.status` remains `unverified_against_natural_language` and the theorem statement must be reviewed. A response with a Lean error or timeout includes the candidate source and diagnostics but is not marked proof-checked. After a Lean rejection, the service may make one repair attempt; it discards the repair if the theorem statement or binders change.

The linked [Kimina-Prover-RL-0.6B model](https://huggingface.co/AI-MO/Kimina-Prover-RL-0.6B) is a Lean 4 proof generator. Its published example supplies both an English problem and a formal statement; the current route also supports an English-only prompt, but semantic alignment still requires review. The GGUF [Q6 quant](https://huggingface.co/mradermacher/Kimina-Prover-RL-0.6B-i1-GGUF) is served locally through llama.cpp.

## Symbolic mathematics

`POST /v1/symbolic` runs SymPy 1.14.0 in a time-limited child process. Supported operations are parse-only `validate`, `simplify`, polynomial `factor`, algebraic `expand`, `differentiate`, `integrate`, two-sided real `limit`, Taylor `series` (order 1–32), finite integer `sum` and `product`, bounded `plot`, `solve` (for an expression equal to zero), and `identity`. A plot uses `plot(expression, variable, lower, upper)`, requires finite real bounds and only the selected variable, and samples 321 points. `/v1/symbolic` accepts `plot_width` (100–1,600; default 720); `/v2/query` uses `plotwidth` (default 200). `/v2/query` defaults to `format=plaintext,image`. Math queries and supported provider queries return inline SVG data URLs in JSON or `<img>` elements in XML for their applicable input and result subpods; numeric math results also include an image for the DecimalApproximation pod. SymPy MathML output uses presentation MathML. Formula images use structural SVG typesetting for a bounded subset and fall back to escaped text for unsupported structures; current-weather and other provider images contain escaped plain text; seven-day forecasts have a daily temperature and precipitation chart; plots contain sampled graph paths. These are local renderings, not Wolfram's typeset images or provider-specific graphics. Plot `moutput` returns the Wolfram Language `Plot[...]` expression used to reproduce the graphic. Image maps, cell, sound, and WAV output remain unavailable. Finite symbolic limit points must be declared `real` in `assumptions`; symbolic sum/product bounds must be declared `integer`. A series request supplies `point` and `order`. Expressions accept native syntax plus a bounded presentation-LaTeX subset (fractions, roots, powers, simple symbol subscripts, common functions, Greek symbols, and degree notation) and a bounded presentation-MathML subset (identifiers, numbers, fractions, powers, roots, simple symbol subscripts, fenced expressions, and common functions). Inputs are limited to 4,000 characters, 256 tokens or MathML nodes, and 32 nesting levels. The parser rejects unsupported commands, MathML elements, declarations, and constructs; it does not evaluate Python code or arbitrary LaTeX/Content MathML.

```json
{
  "operation": "identity",
  "lhs": "sin(x)**2 + cos(x)**2",
  "rhs": "1",
  "assumptions": {"x": ["real"]}
}
```

A limit request supplies its point separately. For a finite symbolic point, declare that point real:

```json
{
  "operation": "limit",
  "expression": "x**2",
  "variable": "x",
  "point": "a",
  "assumptions": {"a": ["real"]}
}
```

Discrete aggregations use inclusive integer bounds. Symbolic bounds need an integer assumption:

```json
{
  "operation": "sum",
  "expression": "k**2",
  "variable": "k",
  "lower": "1",
  "upper": "n",
  "assumptions": {"n": ["integer"]}
}
```

The request can include a `named_units` mapping to bind selected symbols to a curated set of common length, time, mass, volume, angle, SI-derived, and US customary units. The service also accepts explicit quantity literals such as `5 km`, `12 hours`, and `3 kg` in `/v2/query`; the ambiguous `m` abbreviation still returns the service-owned meter, US minim, and minute alternatives. Other one-letter forms retain their symbolic/list interpretations; use a spelled-out unit name or `named_units` to select those units. Expressions that add incompatible dimensions are rejected. The supported canonical unit names and quantity aliases are listed by `/v1/capabilities`. The response reports `evidence.kind = symbolic_result` and `verified = false`. A simplification to zero is useful computational evidence, but only a separately checked Lean theorem is returned as a formal proof.

## Factual knowledge sources

`POST /v1/knowledge/query` dispatches a bounded request to one selected provider and returns its source metadata and retrieval timestamp. Current operations are Wikidata `search`/`entity` and allowlisted `property` lookups (`P1082` population, `P36` capital, `P37` official language, or `P38` currency), one DBnomics `series`, NIST `constant`, GeoNames `search`, and Open-Meteo `weather`. Wikidata property lookups search ranked entity candidates, preserve disambiguation, and return claim rank, qualifiers, and reference counts where available; values remain retrieved data, not Lean proofs. `/v2/query` routes official-language questions and currency questions such as `official languages of Canada`, `currency of France`, and `currency used in France` through the same allowlisted properties. Wikidata API responses use a bounded, process-local cache with a five-minute TTL; cache status and the source-response timestamp are included in evidence. Values remain retrieved source data, not proofs. GeoNames requires `GEONAMES_USERNAME`. NIST is a local subset labeled `CODATA 2022`; it does not silently claim to cover the full constants database. Open-Meteo accepts coordinates or geocodes a location name before retrieving current conditions, with optional `metric` or `nonmetric` units.

```json
{"provider":"nist_codata","operation":"constant","constant":"planck_constant"}
```

These records and live API responses use `evidence.kind = retrieved_source_data`, with `verified = false`. Keep the source and its date with any fact passed into a proof workflow; a source value can be used as an assumption, but Lean only checks the theorem that was actually formalized.

## Wolfram Language Compute compatible route

`POST /api/cag/v1/WolframLanguageCompute` accepts the `code`, `line`, `maxChars`, and `timeConstraint` body fields and returns the familiar `result`, `code`, `success`, and `uuid` fields. It also adds `evidence` with the checked Lean source, source hash, and axiom audit. Send the configured key as `Authorization: <key>`; `Authorization: Bearer <key>` also works.

In addition to `Sin[Pi]`, `TrigExpand@Sin[2x]`, and `Integrate[x, {x, a, b}]` for integer bounds, the endpoint accepts exact rational arithmetic and bounded polynomial inputs, including `Expand[(x + 1)^2]`, `Factor[x^2 - 1]`, and `Simplify[0.1 + 0.2]`. Decimal tokens use exact base-10 rational semantics. The polynomial grammar supports `+`, `-`, `*`, division by nonzero rational constants, and powers from 0 to 16; numeric components are limited to 256 bits, with at most 256 syntax nodes and 32 nesting levels. Each polynomial identity is compiled in Lean over `ℂ`, so the result holds for real and complex values and returned only after the kernel and axiom checks pass. Function calls, arbitrary Wolfram Language code, `FreeformPrompt`, graphics, and remote data lookups remain unsupported and receive HTTP 501. `timeConstraint` defaults to 60 seconds and applies one shared deadline to symbolic evaluation and Lean checking; the applied budget is reported in `evidence`.

`D[polynomial, variable]` also computes derivatives over the same bounded polynomial grammar. SymPy proposes the derivative, and the endpoint returns it only after Lean checks a `HasDerivAt` theorem over `ℝ` and audits its axioms. For example, `D[(x + 1)^2, x]` returns `2*x + 2`. All free variables are treated as real, and polynomial differentiability requires no extra assumptions.

Example:

```bash
curl -X POST http://127.0.0.1:8000/api/cag/v1/WolframLanguageCompute \
  -H "Content-Type: application/json" \
  -H "Authorization: replace-with-a-long-random-token" \
  -d '{"code":"Sin[Pi]"}'
```

The successful result body includes `"result":"Out[1]=0"` and an additive `evidence` object. The response `uuid` is generated locally; it does not identify a remote Wolfram computation.

The Wolfram|Alpha Full Results API reference is saved as [HTML](WolframAlpha-API-Documentation.html) and [Markdown](WolframAlpha-API-Documentation.md), with a [feature parity audit](WolframAlpha-API-Feature-Parity.md). The local `/v2/query` surface follows its query-result/pod shape for the computation classes this service supports. It is not a call to Wolfram|Alpha and does not accept a Wolfram AppID; authenticate with this service's bearer token.

## Full Results-style computation API

`GET /v2/query` returns a `queryresult` envelope in XML by default. Set `output=json` for JSON. The planner accepts native formulas and the bounded presentation-LaTeX or MathML subsets described above, with explicit `simplify(expr)`, `factor(expr)`, `expand(expr)`, `differentiate(expr, variable)`, `integrate(expr, variable[, lower, upper])`, two-sided real `limit(expr, variable, point)`, Taylor `series(expr, variable, point, order)` (order 1–32), `plot(expr, variable, lower, upper)`, finite integer `sum(expr, variable, lower, upper)` and `product(expr, variable, lower, upper)`, `solve(expr, variable)`, and `identity(lhs, rhs)` forms. Sum and product bounds are inclusive integer literals on `/v2/query`; `/v1/symbolic` also accepts symbolic bounds declared integer through `assumptions`. For plots, `plotwidth` controls graph SVG width in the 100–1,600 pixel range (default 200). At finite limit points, left and right results are compared; at infinity the corresponding approach direction is used. A one-sided mismatch is returned as `DoesNotExist` with both sides shown. Finite symbolic limit points must be declared real through `/v1/symbolic`; `/v2/query` currently accepts explicit real points. It returns input interpretation and result pods. Each result includes source/evidence metadata; by default SymPy results are not formally verified, and `verify=true` requests Lean evidence for the supported polynomial subset. `format` defaults to `plaintext,image`; explicit formats such as `plaintext,mathml,minput,moutput,image` request those representations. Mathematical and provider subpods return SVG images where available: formula images structurally render a bounded presentation-MathML subset and fall back to escaped text when unsupported, provider images show escaped plaintext, seven-day weather forecasts include a temperature and precipitation chart, and plot images show sampled graphs. Image maps, cell, sound, and WAV formats are recognized but omitted with a warning; provider-specific charts and maps remain unavailable outside the seven-day forecast.

The `width` parameter (default 500; local range 100–1,600) controls text-oriented SVG output, including mathematical text fallbacks and provider plaintext images. The `maxwidth` parameter (default 500; local range 100–1,600) controls the displayed width of structurally rendered mathematical images. `mag` scales content inside those mathematical images from 0.5 to 2.0 (default 1.0) without changing the SVG dimensions. These controls do not affect plots or provider images; `plotwidth` controls plot and forecast chart widths.

The `ignorecase=true` query parameter case-folds mathematical input before parsing. Provider request patterns are matched without case sensitivity while case-sensitive provider identifiers are preserved. The original input remains in the response, and the result records `ignorecase=true`. Case folding also folds formula variable names, so use it only when case should not distinguish symbols. The same control is available on `/v2/validatequery`. Translation and query reinterpretation are not implemented.

```bash
curl -G http://127.0.0.1:8000/v2/query \
  -H "Authorization: Bearer replace-with-a-long-random-token" \
  --data-urlencode "input=sin(30)" \
  --data-urlencode "output=json"
```

The first pod is the interpreted input and the `Result` pod contains the symbolic result. `includepodid`, `excludepodid`, `podtitle` (with `*` wildcard), `podindex`, and `scanner` can be repeated or comma-separated; positive selectors are combined as alternatives. `includepodid` and `excludepodid` cannot be combined. Recognized mathematical queries produce `Input` and `Result` pods with `Identity` and `Mathematical` scanners.

Finite non-integer numeric results also include a `DecimalApproximation` pod, with a default precision of 50 digits and discoverable `More digits` states. Resubmit the returned state input to `podstate`; repeated values or an `n@` prefix apply multiple precision steps, up to 500 digits. For example, `podstate=2@DecimalApproximation__More digits` requests 200 digits. These states apply to mathematical numeric results, not retrieved data or subpods.

For example, `input=factor(x**2 - 1)` produces `(x - 1)*(x + 1)`, `input=expand((x + 1)**2)` produces `x**2 + 2*x + 1`, `input=limit(sin(x)/x, x, 0)` produces `1`, `input=series(sin(x), x, 0, 6)` produces `x - x**3/6 + x**5/120 + O(x**6)`, `input=sum(k, k, 1, 5)` produces `15`, `input=product(k, k, 1, 5)` produces `120`, `input=differentiate(x**2, x)` produces `2*x`, and `input=integrate(x, x, 0, 1)` produces `1/2`. These are explicit computation forms, not natural-language interpretation.

`/v2/query` also accepts deterministic natural-language templates for evaluate/simplify, factor, expand, differentiate, integrate, solve, limits, Taylor series, finite integer-bounded sums and products, and bounded plots. For example, `what is 2 plus 2?` returns `4`; `differentiate x squared with respect to x` returns `2*x`; `integrate x from 0 to 1` returns `1/2`; and `solve x squared equals 4 for x` returns the roots. Sum and product bounds must be integer literals. `interpretation.natural_language_plan` reports the selected candidate and matching rule. `/v2/validatequery` exposes the proposed plan without computation. If an operation omits its variable, the service infers it only when exactly one free symbol appears; otherwise it returns a clarification tip. Formula fragments pass through the same bounded parser and SymPy validation. Ambiguous function scope requires explicit parentheses, such as `sin(x + 1)` or `sin(x) + 1`. Unsupported free-form language and natural-language data discovery remain unavailable. Evaluation phrases continue to use the existing typed assumption registry when applicable. This route returns SymPy computations unless `verify=true` succeeds; a proof covers the selected parsed expression and does not establish English interpretation fidelity. For natural-language theorem generation, use `POST /v1/natural-language/prove`, which asks the configured model for a Lean theorem and compiles it with Lean; success is returned as `outcome=proof_checked` and does not establish that the theorem preserves the English meaning.

For optional verification, add `verify=true` to `/v2/query`. The response `evidence.formal_verification` includes the status and, when checked, the Lean source, its SHA-256 digest, and an axiom audit. The current verified subset covers exact polynomial evaluation, simplify/factor/expand, polynomial differentiation, polynomial differences, and the definite integral of `x` over integer bounds. Other operations and retrieved provider facts remain unverified. `prooftimeout` controls this separate stage (default 60 seconds; range 1-180 seconds). A timeout, unsupported operation, rejected proof, or result mismatch is reported with `verified=false` while preserving the symbolic result.

For example, `input=\frac{1}{2}`, `input=\sin\left(\frac{\pi}{6}\right)`, and presentation MathML `<math>` expressions are normalized into the same bounded symbolic grammar. `GET /v2/validatequery` accepts these input forms too. The supported subset is listed by `/v1/capabilities`; successful parsing is not a Lean proof.

The same planner routes source-backed data queries to existing providers: `what is the speed of light?` or `constant: planck_constant` uses the bundled NIST CODATA subset; `population of France` searches Wikidata and returns up to three ranked entity candidates with available P1082 population statements; `capital of France` returns ranked candidates with P36 capital relationships; `weather in London` uses current conditions from Open-Meteo, while `weather forecast in London` returns a seven-day forecast and chart; `input=weather&location=London` still requests current conditions; `entity: France` searches Wikidata; `place: Paris` searches GeoNames; and `series: ECB/EXR/A.ARS.EUR.SP00.A` retrieves a DBnomics series; `official languages of Canada` uses P37; `currency of France` and `currency used in France` use P38. These intents preserve ranked entity candidates and claim/reference provenance in the Data pod; retrieved values remain source data and are not Lean proofs. Wikidata API responses use a bounded, process-local cache with a five-minute TTL. GeoNames requires `GEONAMES_USERNAME`. Weather can also use `latlong=51.5072,-0.1276` and `units=metric` or `units=nonmetric`; nonmetric weather uses Fahrenheit, miles per hour, and inches. Forecast charts use daily high and low temperatures plus precipitation totals, and `plotwidth` controls their SVG width. The JSON/XML result contains a Data pod, provider source metadata (including license or attribution fields where provided), retrieval time, and `retrieved_source_data` evidence. Natural-language data interpretation remains a bounded set of explicit routes. See the [Wikibase API documentation](https://www.mediawiki.org/wiki/Wikibase/API) for entity search and retrieval, [DBnomics Web API documentation](https://docs.db.nomics.world/web-api/) for series retrieval and source-specific reuse terms, and the [Open-Meteo API documentation](https://open-meteo.com/en/docs) for weather units and supported variables.

`GET /v2/query` accepts `parsetimeout` (default 5 seconds, range 0.1–30), `podtimeout` (default 4 seconds, range 0.1–30), and `totaltimeout` (default 20 seconds, range 0.1–30). The parse limit applies to isolated SymPy parsing for mathematical inputs; when it expires, the response has `success=false`, `error=false`, and `parsetimedout=true`. The per-pod limit bounds a single symbolic result computation or data-provider retrieval and returns the input pod with the timed-out result identified in `timedoutpods`. This is a local per-result work budget, not a separate formatter-stage timeout. The total limit applies one monotonic deadline to symbolic worker execution and sequential provider requests such as weather geocoding plus forecast retrieval. Scan, formatter-stage, and recalculation controls remain unimplemented. `/v2/validatequery` accepts the same `parsetimeout` control.

The `async` query parameter accepts `false` (default), `true`, or a number from 0 to 30 seconds. `true` allows up to 0.4 seconds for a result before returning a partial response; a number sets that initial wait. If the primary result is still running, its pod has an `async` URL. Fetch that URL with the same bearer token: it returns HTTP 202 with a retry hint while pending, then the completed pod as JSON or XML. Jobs use four workers, allow at most 16 active queries, retain up to 256 jobs, and expire after 30 minutes; under load, older completed jobs can be evicted to make room. They are held in process memory, so a service restart invalidates outstanding URLs. Requests whose pod filters hide the primary result, or that use `podstate`, stay synchronous. This initial slice defers one primary result pod per query; it does not yet format and defer several independent pods as Wolfram does.

`GET /v2/validatequery` parses supported math expressions and bounded symbolic instruction templates, and recognizes data-provider request patterns without running symbolic simplification or retrieving provider data. It returns a `validatequeryresult` envelope and exposes the same typed, service-owned assumption values used by `/v2/query`. Broader factual requests remain available through `/v1/knowledge/query`.

## Candidate meanings and assumptions

For `input=3%3A00`, the API returns `TimeAMOrPM` values `time:pm` and `time:am`, with the active reading listed first. Callers can override it with `assumption=time:am` or `assumption=time:pm`. The default follows the server-local AM/PM, matching Wolfram's documented time-of-day-dependent behavior. Explicit AM/PM and 24-hour clock inputs are interpreted directly and do not produce this assumption.

For `input=log%2020`, the `Function` assumption defaults to the natural logarithm; choose `assumption=function:log-base-10` for base 10. Parenthesized input such as `log(20)` without a whitespace-form log uses the `LogBase` assumption IDs. If a query contains an unparenthesized log, the `Function` choice applies to its log calls in that query.

`GET /v1/interpret?input=sin%2830%29` returns the project-specific interpretation detail response. It parses supported expressions, generates typed assumption alternatives, ranks them, validates the selected registry ID, and routes the selected meaning to SymPy or deterministic date/list/time/base parsers. The Full Results-style `/v2/query` route exposes the same candidates inside its result envelope. A formula can contain both bare numeric trig calls and bare `log(number)` calls; one `AngleUnit` and one `LogBase` choice apply independently to every matching call of that family. The version 2.0.0 registry also handles the documented ambiguous unit abbreviation in `10 m`, numeric date-order ambiguity such as `12/5/1999`, positive mixed-fraction ambiguity such as `3 1/2`, multiplication-versus-list ambiguity such as `3 x`, grouped-number-versus-list ambiguity such as `1,234.5`, and binary-versus-decimal integer ambiguity such as `1011001110`. Lowercase i defaults to i:imaginary-unit and accepts i:variable as an override; uppercase I stays explicit. This choice can coexist with supported angle, logarithm, and list-versus-multiplication choices. Date values use service-owned `date:month-day-year` and `date:day-month-year` IDs, with month/day/year as the service default. A date-order assumption is offered only when both readings form valid Gregorian dates. `GET /v2/assumptions` returns the service-owned IDs; they are not Wolfram's opaque tokens.

For example, `GET /v2/query?input=10%20m` proposes all three service-owned `Unit` interpretations. Resubmit the selected value with `assumption=unit:m-minutes` to interpret it as minutes. Common unambiguous unit literals such as `10 km`, `3 kg`, and `2 hours` are represented as typed quantities and dimension-checked by SymPy. This remains a curated unit set; the service does not infer units across arbitrary formulas or provide a general-purpose unit database.

For an ambiguous numeric date, `GET /v2/query?input=12%2F5%2F1999&output=json` returns both `DateOrder` values and defaults to December 5, 1999. Resubmit with `assumption=date:day-month-year` to get May 12, 1999. This slice supports slash-separated dates with a four-digit year; it does not parse natural-language dates or infer caller locale.

For `input=3%201%2F2`, the service proposes `MixedFraction` interpretations. The default `assumption=mixed-fraction:mix` gives `7/2`; `assumption=mixed-fraction:product` gives `3/2`. This slice accepts positive whole numbers followed by a proper fraction, separated by whitespace.

For `input=3%20x`, the service proposes `ListOrTimes` interpretations. The default `assumption=list-or-times:times` computes `3*x`; `assumption=list-or-times:list` returns the list `{3, x}`. This slice handles a number and one Latin letter separated by whitespace; the `m` abbreviation stays with the existing unit assumption.

For `input=1%2C234.5`, the service proposes `ListOrNumber` interpretations. The default `assumption=list-or-number:number` treats it as `1234.5`; `assumption=list-or-number:list` returns `{1, 234.5}`. This slice accepts one correctly grouped thousands comma and an optional decimal fraction.

For `input=1011001110`, the service proposes `NumberBase` interpretations and defaults to binary, matching the documented example's first-listed value; it returns `718`. Resubmit with `assumption=number-base:decimal` to treat the same digits as decimal `1011001110`. This slice applies to standalone strings of 8–64 binary digits; an explicit `0b` prefix is already unambiguous and does not create an assumption.

```bash
curl -G http://127.0.0.1:8000/v1/interpret \
  -H "Authorization: Bearer replace-with-a-long-random-token" \
  --data-urlencode "input=sin(30)"

curl -G http://127.0.0.1:8000/v1/interpret \
  -H "Authorization: Bearer replace-with-a-long-random-token" \
  --data-urlencode "input=sin(30)" \
  --data-urlencode "assumption=angle:degrees"
```

For the first request, the response lists `angle:radians` and `angle:degrees`, with radians marked as `service_default`. The second request selects degrees with `selection_source: caller_override` and computes `1/2`. An explicit unit in the input is surfaced as `explicit_input`; a conflicting override is rejected. Unknown or inapplicable assumption IDs are rejected. IDs belong to this service and are stored in the versioned `src/leanproof_service/assumption_registry.json`; they are not Wolfram tokens.

For `log(100)`, the service proposes `log:base-e` and `log:base-10`; the default is the natural logarithm. Passing `assumption=log:base-10` selects a common logarithm and computes `2`. The classifier ranks the alternatives but never chooses one based on which result is more useful or easier to prove.

The baseline classifier only ranks generated candidates. By default it uses small deterministic lexical cues and neutral scores for bare `sin(30)`; it cannot select an assumption. `ASSUMPTION_CLASSIFIER=decima` enables the optional [Decima-small decision model](https://huggingface.co/amyrmahdy/decima-small), whose open-option decision format fits candidate ranking. It loads only when enabled and the `decima-ai` package is installed. The returned ranking is not calibrated for this service and never counts as proof. The [Nano-Jev model](https://huggingface.co/sdmlai/nano-jev) targets retrieval relevance, answer sufficiency, and claim groundedness; it is a better fit for triaging retrieved evidence than selecting a math interpretation. Model scores cannot validate facts or establish a theorem.

## Evidence boundary

Lean checks the generated formal proposition under the listed assumptions. It cannot establish that the submitted AST faithfully represents `source_text`, or that an empirical premise matches the world. If source text is supplied, its hash is returned alongside the AST hash so the two artifacts can be reviewed together; the hash does not certify their semantic match. The current natural-language route covers only bounded polynomial arithmetic, not general mathematical autoformalization. This boundary follows a central lesson from [“Navier–Stokes lost in translation”](https://arxiv.org/html/2610.08144v1): kernel acceptance verifies the formal statement presented to Lean, while correspondence to the original mathematical text remains a separate obligation.
