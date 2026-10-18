# Wolfram|Alpha Full Results API: feature parity audit

> Source: [Wolfram|Alpha Full Results API documentation](https://products.wolframalpha.com/api/documentation)  
> Documentation revision read: API 2.0, Revision A  
> Audited: 2026-10-18  
> Local copies: [downloaded HTML](WolframAlpha-API-Documentation.html) · [Markdown conversion](WolframAlpha-API-Documentation.md)

## Executive summary

The project currently implements a narrow computational API and selected factual providers. It does **not** yet implement the Wolfram|Alpha Full Results API contract. The most important design issue is that our experimental `GET /v2/query` already occupies the same path but accepts a different request/response contract. Reserve that path for the compatibility surface before expanding it.

There are two distinct parity goals:

1. **Protocol parity:** paths, parameters, result envelopes, pod selection, assumptions, formats, errors, and time controls. This is a tractable engineering roadmap.
2. **Answer and coverage parity:** interpreting arbitrary natural-language queries and producing the same broad set of results as Wolfram|Alpha. The public API reference does not provide the underlying scanners or knowledgebase. Our independently sourced providers can grow coverage, but cannot claim identical results or completeness.

Wolfram documents the Full Results API as a GET service with `appid` and `input`, returning XML or JSON around pod results. It also notes that some subjects can be restricted. See the [API introduction and request example](https://products.wolframalpha.com/api/documentation#sample-query).

## Current project surface

| Project endpoint | Current scope | Parity implication |
|---|---|---|
| `GET /v2/query` | Safe symbolic query slice; currently recognizes bare numeric trig ambiguity and returns a project-specific JSON response | Path collision: this must move or become the Full Results-compatible route |
| `GET /v2/assumptions` | Service-owned, versioned angle-unit IDs | Good registry foundation; only `AngleUnit` is implemented |
| `POST /v1/symbolic` | Bounded SymPy grammar and operations | Computation building block, not a general free-form query interpreter |
| `POST /v1/knowledge/query` | Explicit Wikidata, DBnomics, CODATA subset, GeoNames, and Open-Meteo operations | Provider adapters, not a multi-pod natural-language answer planner |
| `POST /api/cag/v1/WolframLanguageCompute` | Small allowlisted expressions checked by Lean | Separate Wolfram Language Compute-shaped endpoint; not Full Results API compatibility |

The current interpretation endpoint and assumption slice are described in [README.md](README.md#candidate-meanings-and-assumptions), while supported operations and factual providers are listed in [features.json](features.json).

## Full Results API feature inventory

The documented parameter groups are:

| Group | Parameters or behavior | Current project status | Priority |
|---|---|---|---|
| Request and identity | `input`, `appid`, URL encoding; GET `/v2/query` | `input` exists on a narrower route; service uses bearer auth; no service AppID contract | P0 |
| Input syntax | Free-form text; math input may use presentation LaTeX or MathML | Narrow safe Python-like expression grammar; no general natural-language, LaTeX, or MathML parser | P0/P3 |
| Result envelope | `output=xml/json`; query-level status and metadata such as `success`, `error`, `numpods`, `datatypes`, timing, timeouts, version, and recalculation information | Custom JSON response; no Full Results envelope or XML serializer | P0 |
| Pod model | Ordered pods with `id`, `title`, `scanner`, `position`, `primary`, per-pod error, subpods, and supplemental elements | No common pod/subpod result model | P0 |
| Pod selection | `includepodid`, `excludepodid`, `podtitle` (including `*`), `podindex`, `scanner`; repeated values supported | Not implemented | P1 |
| Output representation | Per-pod `format`: `image`, `imagemap`, `plaintext`, `minput`, `moutput`, `cell`, `mathml`, `sound`, `wav`; multiple format values | Text/JSON only in the query slice; graphics are unsupported by the WLC-shaped route | P2 |
| Assumptions | Discover alternatives in result data and apply one or more exact values on a subsequent query | Versioned service IDs and explicit overrides exist for angle units only; no dynamic candidate coverage or multiple assumption types | P0/P1 |
| Location and units | `ip`, `latlong`, `location`; location can affect results; `units=metric/nonmetric` | Some providers accept location/coordinates internally; no common request context, IP-derived location, or preferred-units option | P2 |
| Pod states | `podstate` changes a specific pod/subpod using state input returned by the result | Not implemented | P2 |
| Presentation sizing | `width`, `maxwidth`, `plotwidth`, `mag` | Not implemented | P2 |
| Time controls | `scantimeout`, `podtimeout`, `formattimeout`, `parsetimeout`, `totaltimeout`; `async` partial results and recalculate URL | Local wall-clock caps exist; no scan/format stages, async pod delivery, or recalculate operation | P2 |
| Query interpretation controls | `reinterpret`, `translation`, `ignorecase`; `validatequery` parses without full result computation | No query validation endpoint, translation, spelling repair, or controlled reinterpretation | P1 |
| Classification | Clash assumptions and `datatypes`; pod `scanner` helps identify result subject | Candidate ranking is limited to angle interpretation; no general query datatype classifier | P1/P3 |
| Informational elements | `assumptions`, `sources`, `warnings`, `infos`, `generalization`, `didyoumeans`, `tips`, `spellcheck`, `translation`, `reinterpret`, `examplepage`, `futuretopic`, `languagemsg`, and errors | Provider provenance exists on knowledge responses; no compatible unified informational-element model | P1 |
| Error semantics | Distinguishes invalid requests/internal errors (`error=true`) from unrecognized queries (`success=false`); pod errors can be local | FastAPI errors and WLC compatibility errors are separate shapes; no queryresult-compatible semantics | P0 |
| Forward compatibility | Clients should tolerate new result elements and attributes | No generic pod schema/parser yet | P1 |

### Assumption coverage

The reference groups assumptions into categorical (`Clash`, `MultiClash`, `SubCategory`, `Attribute`), mathematical (`Unit`, `AngleUnit`, `Function`, `ListOrTimes`, `ListOrNumber`, `CoordinateSystem`, `I`, `NumberBase`, `MixedFraction`), date/time (`TimeAMOrPM`, `DateOrder`, `MortalityYearDOB`), scientific (`TideStation`), and formula-input families (`FormulaSelect`, `FormulaSolve`, `FormulaVariable`, `FormulaVariableOption`, `FormulaVariableInclude`). The current registry implements only `AngleUnit` for a restricted trig input form.

Keep our IDs stable and service-owned. Store a registry version and typed meaning, expose the active choice and all alternatives, and validate overrides before routing. The classifier must never change a claim's scope or choose an assumption based on which one makes a proof succeed. See [Wolfram's assumption structure and application rules](https://products.wolframalpha.com/api/documentation?scrollTo=using-assumptions) for the wire behavior we are modeling; the project's IDs must remain distinct from Wolfram's opaque `input` tokens.

## Recommended implementation order

### P0 — establish a compatible request and response contract

1. Move the current experimental `GET /v2/query` behavior to a clearly project-specific path such as `/v1/interpret`; reserve `GET /v2/query` for Full Results-compatible requests.
2. Define our own service credential/AppID model. Do not accept or require a real Wolfram AppID, and do not send user queries to Wolfram unless a separately authorized provider integration is added.
3. Parse and validate `input`, required credentials, `output`, and `format`; return stable query-level error fields and HTTP status behavior.
4. Add a versioned result envelope and pod/subpod model. Preserve source attribution and evidence kinds as additive fields.
5. Implement XML and JSON encoders from the same internal result representation. Ignore unrecognized additive fields when consuming provider data.

### P1 — query control and inspectable interpretation

1. Implement the pod filters (`includepodid`, `excludepodid`, `podtitle`, `podindex`, `scanner`) over our own generated pods.
2. Add `/v2/validatequery` for syntax and candidate-interpretation discovery without solver execution.
3. Extend the typed assumption registry across explicit, supportable ambiguities: angle units, function base (`log`), units, number/list parsing, coordinate systems, and date order. Add data/entity and formula assumptions only when a deterministic validator and a source-backed meaning are available.
4. Return `datatypes`, `scanner`, assumptions, source citations, warnings, unsupported-topic tips, and localized pod errors from the same result planner.
5. Support explicit selection and multiple compatible assumption IDs; reject unknown, stale, conflicting, or non-applicable IDs.

### P2 — presentation, locality, and execution control

1. Add plaintext and MathML first; then image/plot URLs, image maps, Wolfram-input/output-like text, cell-like render payloads, and audio only where a local backend can produce them safely.
2. Add location and preferred units as request context. Make IP-derived location opt-in and avoid retaining the caller's IP.
3. Add pod states with stable state IDs and explicit follow-up requests.
4. Add per-operation time budgets, partial asynchronous jobs, polling, and a recalculation/resume contract. A single request wall timeout is not equivalent to WA's scan/format stage controls.
5. Add width and display scaling only for result types that support renderable output.

### P3 — broaden independently sourced coverage

Build coverage around licensed and attributable providers, measuring query-class recall and source freshness. Prioritize mathematical operations, units, entities, economics, physical constants, geography, weather, and other agreed domains. Do not describe provider coverage as full Wolfram|Alpha parity; preserve provider, license/attribution, retrieval time, and whether the value is computed, retrieved, solver-derived, or kernel-checked.

## Acceptance checklist for a parity release

- The `/v2/query` route has a stable, documented request contract and does not confuse our credentials with Wolfram credentials.
- `GET /v2/validatequery` can report parse/interpretation candidates independently of result computation.
- JSON and XML encode the same query result and preserve unknown additive fields safely.
- Pod IDs are stable within a query contract; filtering supports repeatable include/exclude and wildcard title options.
- Assumption proposals list typed, versioned service IDs; caller overrides are validated before backend routing and never chosen from proof outcomes.
- Every pod reports its source and evidence kind; warnings, errors, and timeouts are distinguishable.
- Timeouts, async jobs, and pod states have documented resource limits and deterministic follow-up behavior.
- Compatibility tests cover request validation, all supported query parameters, response schemas, assumption round-trips, and error cases.
- The product documentation distinguishes protocol compatibility from data/answer coverage.

## Source notes

The audit is based on the downloaded API 2.0 Revision A page, including its parameter reference, result element catalog, pod formatting, location, assumption, timeout/async, error, and `validatequery` sections. The page states that queries are GET requests, supports XML/JSON result envelopes, and cautions clients to tolerate future elements and attributes. See the [official API reference](https://products.wolframalpha.com/api/documentation) and its [assumption section](https://products.wolframalpha.com/api/documentation?scrollTo=using-assumptions).
