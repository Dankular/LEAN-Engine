# Lean Proof and Computation Service

A service for LLM clients that separates symbolic computation, constraint solving, formal proof checking, and retrieval from factual data providers. It does not translate natural language into a theorem: callers submit a typed claim or a bounded symbolic expression, and each response identifies what kind of evidence it contains.

## Current coverage

- Integer and real linear arithmetic.
- Equality, disequality, and ordered comparisons.
- Addition, subtraction, and multiplication by a constant.
- Assumptions are conjoined. Declared free variables are universally quantified.
- Z3 counterexamples are replayed with exact Python integer/rational arithmetic.
- `verified` is returned only after Lean compiles the generated theorem and its axiom audit passes.
- SymPy 1.14 provides bounded simplify, differentiate, integrate, solve, and identity operations over a safe expression grammar.
- Factual retrieval adapters cover Wikidata entity search/records, individual DBnomics series, a pinned CODATA 2022 constants subset, GeoNames place search, and current Open-Meteo weather.
- Retrieved facts carry provider and retrieval-time metadata; they are never labeled as formal proofs.
- Nonlinear Lean proof generation, arbitrary Lean code, natural-language formalization, persistence, and async jobs are not implemented.

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

For a local development server that automatically restarts when Python files under `src` change, run:

```powershell
.\scripts\dev.ps1
```

It serves on `http://127.0.0.1:8000` by default. The script creates a local bearer token in the git-ignored `.dev-api-token` file if `API_BEARER_TOKEN` is not already set. Pass `-Port 8001` to use another port. Lean and Mathlib are checked when a proof request runs; the watcher reloads the API process when its Python source changes.

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

`POST /v1/symbolic` runs SymPy 1.14.0 in a time-limited child process. Supported operations are parse-only `validate`, `simplify`, `differentiate`, `integrate`, `solve` (for an expression equal to zero), and `identity`. The expression grammar accepts numeric literals, named symbols, `+ - * / **`, and a small allowlist of functions such as `sin`, `cos`, `sqrt`, `log`, and `exp`. It does not evaluate Python code.

```json
{
  "operation": "identity",
  "lhs": "sin(x)**2 + cos(x)**2",
  "rhs": "1",
  "assumptions": {"x": ["real"]}
}
```

The response reports `evidence.kind = symbolic_result` and `verified = false`. A simplification to zero is useful computational evidence, but only a separately checked Lean theorem is returned as a formal proof.

## Factual knowledge sources

`POST /v1/knowledge/query` dispatches a bounded request to one selected provider and returns its source metadata and retrieval timestamp. Current operations are Wikidata `search`/`entity`, one DBnomics `series`, NIST `constant`, GeoNames `search`, and Open-Meteo `weather`. GeoNames requires `GEONAMES_USERNAME`. NIST is a local subset labeled `CODATA 2022`; it does not silently claim to cover the full constants database. Open-Meteo accepts coordinates or geocodes a location name before retrieving current conditions.

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

`GET /v2/query` returns a `queryresult` envelope in XML by default. Set `output=json` for JSON. The planner supports the safe symbolic expression grammar described below, with explicit `simplify(expr)`, `differentiate(expr, variable)`, `integrate(expr, variable[, lower, upper])`, `solve(expr, variable)`, and `identity(lhs, rhs)` forms. It returns input interpretation and result pods. Each result includes source/evidence metadata; SymPy results are explicitly marked as not formally verified. `format=plaintext,mathml` returns available textual and MathML representations. Other documented Wolfram formats are recognized but omitted with a warning until a local renderer exists.

```bash
curl -G http://127.0.0.1:8000/v2/query \
  -H "Authorization: Bearer replace-with-a-long-random-token" \
  --data-urlencode "input=sin(30)" \
  --data-urlencode "output=json"
```

The first pod is the interpreted input and the `Result` pod contains the symbolic result. `includepodid`, `excludepodid`, `podtitle` (with `*` wildcard), `podindex`, and `scanner` can be repeated or comma-separated; positive selectors are combined as alternatives. `includepodid` and `excludepodid` cannot be combined. The service currently produces `Input` and `Result` pods with `Identity` and `Mathematical` scanners.

For example, `input=differentiate(x**2, x)` produces `2*x`, while `input=integrate(x, x, 0, 1)` produces `1/2`. These are explicit computation forms, not natural-language interpretation.

`GET /v2/validatequery` runs the safe parser and candidate-assumption resolver without running the symbolic simplifier. It returns a `validatequeryresult` envelope and exposes the same typed, service-owned assumption values used by `/v2/query`. The current query planner covers mathematical expressions; natural-language knowledge queries remain available through the explicit `/v1/knowledge/query` provider interface.

## Candidate meanings and assumptions

`GET /v1/interpret?input=sin%2830%29` returns the project-specific interpretation detail response. It parses the supported expression, generates typed assumption alternatives, ranks them, validates the selected registry ID, and routes the resolved expression to SymPy. The Full Results-style `/v2/query` route exposes the same candidates inside its result envelope. `GET /v2/assumptions` returns the service-owned registry (currently version 1.1.0), covering angle units for bare numeric `sin`/`cos`/`tan` and base e versus base 10 for a bare `log(number)`.

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
