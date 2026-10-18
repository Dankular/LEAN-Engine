# Lean Proof and Computation Service

A service for LLM clients that separates symbolic computation, constraint solving, formal proof checking, and retrieval from factual data providers. It does not translate natural language into a theorem: callers submit a typed claim or a bounded symbolic expression, and each response identifies what kind of evidence it contains.

## Current coverage

- Integer and real linear arithmetic.
- Equality, disequality, and ordered comparisons.
- Addition, subtraction, and multiplication by a constant.
- Assumptions are conjoined. Declared free variables are universally quantified.
- Z3 counterexamples are replayed with exact Python integer/rational arithmetic.
- `verified` is returned only after Lean compiles the generated theorem and its axiom audit passes.
- SymPy 1.14 provides bounded simplify, differentiate, integrate, solve, and identity operations over a safe expression grammar. Mathematical expressions accept native syntax, a bounded presentation-LaTeX subset, or a bounded presentation-MathML subset.
- Factual retrieval adapters cover Wikidata entity search/records, individual DBnomics series, a pinned CODATA 2022 constants subset, GeoNames place search, and current Open-Meteo weather.
- Retrieved facts carry provider and retrieval-time metadata; they are never labeled as formal proofs.
- Nonlinear Lean proof generation, arbitrary Lean code, and natural-language formalization are not implemented. Async query jobs are an in-process initial slice and are lost when the service restarts.

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

## Symbolic mathematics

`POST /v1/symbolic` runs SymPy 1.14.0 in a time-limited child process. Supported operations are parse-only `validate`, `simplify`, `differentiate`, `integrate`, `solve` (for an expression equal to zero), and `identity`. Expressions accept native syntax plus a bounded presentation-LaTeX subset (fractions, roots, powers, simple symbol subscripts, common functions, Greek symbols, and degree notation) and a bounded presentation-MathML subset (identifiers, numbers, fractions, powers, roots, simple symbol subscripts, fenced expressions, and common functions). Inputs are limited to 4,000 characters, 256 tokens or MathML nodes, and 32 nesting levels. The parser rejects unsupported commands, MathML elements, declarations, and constructs; it does not evaluate Python code or arbitrary LaTeX/Content MathML.

```json
{
  "operation": "identity",
  "lhs": "sin(x)**2 + cos(x)**2",
  "rhs": "1",
  "assumptions": {"x": ["real"]}
}
```

The request can include a `named_units` mapping to bind selected symbols to the initial `meter`, `minute`, or `us_minim` unit set. Expressions that add incompatible dimensions are rejected. The response reports `evidence.kind = symbolic_result` and `verified = false`. A simplification to zero is useful computational evidence, but only a separately checked Lean theorem is returned as a formal proof.

## Factual knowledge sources

`POST /v1/knowledge/query` dispatches a bounded request to one selected provider and returns its source metadata and retrieval timestamp. Current operations are Wikidata `search`/`entity`, one DBnomics `series`, NIST `constant`, GeoNames `search`, and Open-Meteo `weather`. GeoNames requires `GEONAMES_USERNAME`. NIST is a local subset labeled `CODATA 2022`; it does not silently claim to cover the full constants database. Open-Meteo accepts coordinates or geocodes a location name before retrieving current conditions, with optional `metric` or `nonmetric` units.

```json
{"provider":"nist_codata","operation":"constant","constant":"planck_constant"}
```

These records and live API responses use `evidence.kind = retrieved_source_data`, with `verified = false`. Keep the source and its date with any fact passed into a proof workflow; a source value can be used as an assumption, but Lean only checks the theorem that was actually formalized.

## Wolfram Language Compute compatible route

`POST /api/cag/v1/WolframLanguageCompute` accepts the `code`, `line`, `maxChars`, and `timeConstraint` body fields and returns the familiar `result`, `code`, `success`, and `uuid` fields. It also adds `evidence` with the checked Lean source, source hash, and axiom audit. Send the configured key as `Authorization: <key>`; `Authorization: Bearer <key>` also works.

The initial allowlisted expressions are `Sin[Pi]`, `TrigExpand@Sin[2x]`, and `Integrate[x, {x, a, b}]` for integer bounds. Each result is returned only after its generated Lean theorem passes the kernel and axiom checks. Arbitrary Wolfram Language code, `FreeformPrompt`, graphics, and remote data lookups are unsupported and receive HTTP 501. The request accepts `timeConstraint` up to 60 seconds, while the service applies its current 30-second maximum and reports the applied limit in `evidence`.

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

`GET /v2/query` returns a `queryresult` envelope in XML by default. Set `output=json` for JSON. The planner accepts native formulas and the bounded presentation-LaTeX or MathML subsets described above, with explicit `simplify(expr)`, `differentiate(expr, variable)`, `integrate(expr, variable[, lower, upper])`, `solve(expr, variable)`, and `identity(lhs, rhs)` forms. It returns input interpretation and result pods. Each result includes source/evidence metadata; SymPy results are explicitly marked as not formally verified. `format=plaintext,mathml,minput,moutput` requests textual, MathML, Wolfram Language input, and Wolfram Language output forms for mathematical pods. Image, imagemap, cell, sound, and wav formats are recognized but omitted with a warning; data pods can omit MInput/MOutput when no meaningful Wolfram expression exists.

The `ignorecase=true` query parameter case-folds mathematical input before parsing. Provider request patterns are matched without case sensitivity while case-sensitive provider identifiers are preserved. The original input remains in the response, and the result records `ignorecase=true`. Case folding also folds formula variable names, so use it only when case should not distinguish symbols. The same control is available on `/v2/validatequery`. Translation and query reinterpretation are not implemented.

```bash
curl -G http://127.0.0.1:8000/v2/query \
  -H "Authorization: Bearer replace-with-a-long-random-token" \
  --data-urlencode "input=sin(30)" \
  --data-urlencode "output=json"
```

The first pod is the interpreted input and the `Result` pod contains the symbolic result. `includepodid`, `excludepodid`, `podtitle` (with `*` wildcard), `podindex`, and `scanner` can be repeated or comma-separated; positive selectors are combined as alternatives. `includepodid` and `excludepodid` cannot be combined. Recognized mathematical queries produce `Input` and `Result` pods with `Identity` and `Mathematical` scanners.

Finite non-integer numeric results also include a `DecimalApproximation` pod, with a default precision of 50 digits and discoverable `More digits` states. Resubmit the returned state input to `podstate`; repeated values or an `n@` prefix apply multiple precision steps, up to 500 digits. For example, `podstate=2@DecimalApproximation__More digits` requests 200 digits. These states apply to mathematical numeric results, not retrieved data or subpods.

For example, `input=differentiate(x**2, x)` produces `2*x`, while `input=integrate(x, x, 0, 1)` produces `1/2`. These are explicit computation forms, not natural-language interpretation.

For example, `input=\frac{1}{2}`, `input=\sin\left(\frac{\pi}{6}\right)`, and presentation MathML `<math>` expressions are normalized into the same bounded symbolic grammar. `GET /v2/validatequery` accepts these input forms too. The supported subset is listed by `/v1/capabilities`; successful parsing is not a Lean proof.

The same planner routes explicit source-backed data queries to the existing providers: `what is the speed of light?` or `constant: planck_constant` uses the bundled NIST CODATA subset; `weather in London` or `input=weather&location=London` uses Open-Meteo; `entity: France` searches Wikidata; `place: Paris` searches GeoNames; and `series: ECB/EXR/A.ARS.EUR.SP00.A` retrieves a DBnomics series. GeoNames requires `GEONAMES_USERNAME`. Weather can also use `latlong=51.5072,-0.1276` and `units=metric` or `units=nonmetric`; nonmetric weather uses Fahrenheit, miles per hour, and inches. The JSON/XML result contains a Data pod, provider source metadata (including license or attribution fields where provided), retrieval time, and `retrieved_source_data` evidence. These explicit patterns are an initial routing slice; they do not imply general natural-language coverage. See the [DBnomics Web API documentation](https://docs.db.nomics.world/web-api/) for its series endpoint and source-specific reuse terms, and the [Open-Meteo API documentation](https://open-meteo.com/en/docs) for weather units and supported variables.

`GET /v2/query` accepts `parsetimeout` (default 5 seconds, range 0.1–30), `podtimeout` (default 4 seconds, range 0.1–30), and `totaltimeout` (default 20 seconds, range 0.1–30). The parse limit applies to isolated SymPy parsing for mathematical inputs; when it expires, the response has `success=false`, `error=false`, and `parsetimedout=true`. The per-pod limit bounds a single symbolic result computation or data-provider retrieval and returns the input pod with the timed-out result identified in `timedoutpods`. This is a local per-result work budget, not a separate formatter-stage timeout. The total limit applies one monotonic deadline to symbolic worker execution and sequential provider requests such as weather geocoding plus forecast retrieval. Scan, formatter-stage, and recalculation controls remain unimplemented. `/v2/validatequery` accepts the same `parsetimeout` control.

The `async` query parameter accepts `false` (default), `true`, or a number from 0 to 30 seconds. `true` allows up to 0.4 seconds for a result before returning a partial response; a number sets that initial wait. If the primary result is still running, its pod has an `async` URL. Fetch that URL with the same bearer token: it returns HTTP 202 with a retry hint while pending, then the completed pod as JSON or XML. Jobs use four workers, allow at most 16 active queries, retain up to 256 jobs, and expire after 30 minutes; under load, older completed jobs can be evicted to make room. They are held in process memory, so a service restart invalidates outstanding URLs. Requests whose pod filters hide the primary result, or that use `podstate`, stay synchronous. This initial slice defers one primary result pod per query; it does not yet format and defer several independent pods as Wolfram does.

`GET /v2/validatequery` parses supported math expressions and recognizes data-provider request patterns without running symbolic simplification or retrieving provider data. It returns a `validatequeryresult` envelope and exposes the same typed, service-owned assumption values used by `/v2/query`. Broader factual requests remain available through `/v1/knowledge/query`.

## Candidate meanings and assumptions

`GET /v1/interpret?input=sin%2830%29` returns the project-specific interpretation detail response. It parses supported expressions, generates typed assumption alternatives, ranks them, validates the selected registry ID, and routes the selected meaning to SymPy or the deterministic Gregorian calendar parser. The Full Results-style `/v2/query` route exposes the same candidates inside its result envelope. A formula can contain both bare numeric trig calls and bare `log(number)` calls; one `AngleUnit` and one `LogBase` choice apply independently to every matching call of that family. The version 1.3.0 registry also handles the documented ambiguous unit abbreviation in `10 m` and numeric date-order ambiguity such as `12/5/1999`. Date values use service-owned `date:month-day-year` and `date:day-month-year` IDs, with month/day/year as the service default. A date-order assumption is offered only when both readings form valid Gregorian dates. `GET /v2/assumptions` returns the service-owned IDs; they are not Wolfram's opaque tokens.

For example, `GET /v2/query?input=10%20m` proposes all three service-owned `Unit` interpretations. Resubmit the selected value with `assumption=unit:m-minutes` to interpret it as minutes. This initial unit assumption covers standalone numeric quantities using the abbreviation `m`; it does not infer units across arbitrary formulas.

For an ambiguous numeric date, `GET /v2/query?input=12%2F5%2F1999&output=json` returns both `DateOrder` values and defaults to December 5, 1999. Resubmit with `assumption=date:day-month-year` to get May 12, 1999. This slice supports slash-separated dates with a four-digit year; it does not parse natural-language dates or infer caller locale.

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

Lean checks the generated formal proposition under the listed assumptions. It cannot establish that the submitted AST faithfully represents `source_text`, or that an empirical premise matches the world. If source text is supplied, its hash is returned alongside the AST hash so the two artifacts can be reviewed together; the hash does not certify their semantic match. The current interpretation route is a narrow initial slice, not natural-language formalization. This boundary follows a central lesson from [“Navier–Stokes lost in translation”](https://arxiv.org/html/2610.08144v1): kernel acceptance verifies the formal statement presented to Lean, while correspondence to the original mathematical text remains a separate obligation.
