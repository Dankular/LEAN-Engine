# Wolfram|Alpha Full Results API: feature parity audit

> Source: [Wolfram|Alpha Full Results API documentation](https://products.wolframalpha.com/api/documentation)  
> Documentation revision read: API 2.0, Revision A  
> Audited: 2026-10-18  
> Local copies: [downloaded HTML](WolframAlpha-API-Documentation.html) · [Markdown conversion](WolframAlpha-API-Documentation.md)

## Executive summary

The project implements a narrow computational API and selected factual providers, plus an initial local Full Results-style contract. This is protocol groundwork, not full Wolfram|Alpha compatibility: query coverage, result formats, data breadth, and execution controls remain limited.

There are two distinct parity goals:

1. **Protocol parity:** paths, parameters, result envelopes, pod selection, assumptions, formats, errors, and time controls. This is a tractable engineering roadmap.
2. **Answer and coverage parity:** interpreting arbitrary natural-language queries and producing the same broad set of results as Wolfram|Alpha. The public API reference does not provide the underlying scanners or knowledgebase. Our independently sourced providers can grow coverage, but cannot claim identical results or completeness.

Wolfram documents the Full Results API as a GET service with `appid` and `input`, returning XML or JSON around pod results. It also notes that some subjects can be restricted. See the [API introduction and request example](https://products.wolframalpha.com/api/documentation#sample-query).

## Current project surface

| Project endpoint | Current scope | Parity implication |
|---|---|---|
| `GET /v2/query` | Local `queryresult` JSON/XML envelope, Input, Result, DecimalApproximation, selected Data pods, symbolic expression results, AngleUnit/LogBase assumptions (including both in a compound formula), Function alternatives for unparenthesized forms such as `log 20`, Unit assumptions for `10 m`, DateOrder assumptions for valid ambiguous numeric dates, MixedFraction alternatives for forms such as `3 1/2`, ListOrTimes alternatives for forms such as `3 x`, ListOrNumber alternatives for forms such as `1,234.5`, NumberBase alternatives for binary-looking integer strings, I alternatives for lowercase `i`, TimeAMOrPM alternatives for ambiguous clock values, decimal pod states, pod filters, plaintext/MathML, and explicit routes to five factual providers | Partial protocol and query coverage; `/v1/interpret` retains the project-specific interpretation response |
| `GET /v2/validatequery` | Safe parsing and candidate assumption discovery without the symbolic simplification step | Initial validatequery behavior; not a natural-language scanner |
| `GET /v2/assumptions` | Service-owned, versioned angle-unit, logarithm-base, `Function`, ambiguous `m` unit, numeric `DateOrder`, `MixedFraction`, `ListOrTimes`, `ListOrNumber`, `NumberBase`, `I`, and `TimeAMOrPM` IDs | Initial slices only; other WA assumption families and general unit parsing remain |
| `POST /v1/prove`, `/v1/prove-formula`, and `/v1/counterexample` | Typed or bounded formula-text integer/real polynomial claims, Z3 counterexamples, and Lean kernel checks | Bounded arithmetic fragment; variables need explicit sorts; nonlinear polynomials use Z3 and Lean `nlinarith`, subject to the configured degree and time limits; formula text is not natural-language formalization |
| `POST /v1/symbolic` | Bounded SymPy grammar and operations | Computation building block, not a general free-form query interpreter |
| `POST /v1/knowledge/query` | Explicit Wikidata, DBnomics, CODATA subset, GeoNames, and Open-Meteo operations | Provider adapters; `/v2/query` wraps explicit weather, CODATA, Wikidata entity, GeoNames place, and DBnomics series requests as source-backed pods |
| `POST /api/cag/v1/WolframLanguageCompute` | Small allowlisted expressions checked by Lean | Separate Wolfram Language Compute-shaped endpoint; not Full Results API compatibility |

The current interpretation endpoint, accepted symbolic input formats, and assumption slice are described in [README.md](README.md#symbolic-mathematics) and [README.md](README.md#candidate-meanings-and-assumptions), while supported operations and factual providers are listed in [features.json](features.json).

## Full Results API feature inventory

The documented parameter groups are:

| Group | Parameters or behavior | Current project status | Priority |
|---|---|---|---|
| Request and identity | `input`, `appid`, URL encoding; GET `/v2/query` | `input` and URL encoding; service bearer auth is used instead of an `appid` query credential; no Wolfram credential is accepted | P0 |
| Input syntax | Free-form text; math input may use presentation LaTeX or MathML | Safe native expressions plus bounded presentation-LaTeX and presentation-MathML subsets, explicit SymPy operations, and a bounded polynomial comparison grammar on `/v1/prove-formula`; natural-language scanning and general TeX/MathML remain unsupported | P0/P3 |
| Result envelope | `output=xml/json`; query-level status and metadata such as `success`, `error`, `numpods`, `datatypes`, timing, timeouts, version, and recalculation information | XML and JSON `queryresult` envelopes with core metadata, timing, and stable query IDs; stage-specific timing and recalculation are not implemented | P0 |
| Pod model | Ordered pods with `id`, `title`, `scanner`, `position`, `primary`, per-pod error, subpods, and supplemental elements | Input, Result, and selected Data pods use the documented core fields; source records and retrieved data are exposed as additive elements; broad supplemental families remain absent | P0 |
| Pod selection | `includepodid`, `excludepodid`, `podtitle` (including `*`), `podindex`, `scanner`; repeated values supported | Implemented over locally generated math and provider pods; repeated and comma-separated values accepted | P1 |
| Output representation | Per-pod `format`: `image`, `imagemap`, `plaintext`, `minput`, `moutput`, `cell`, `mathml`, `sound`, `wav`; multiple format values | Mathematical pods render plaintext, MathML, Wolfram Language input (`minput`), and Wolfram Language output (`moutput`); other known formats produce a warning and are omitted; data pods omit inapplicable Wolfram expressions | P2 |
| Assumptions | Discover alternatives in result data and apply one or more exact values on a subsequent query | Versioned service IDs, ranked alternatives, and validated overrides cover angle units, bare-log bases, the `m` abbreviation in a standalone quantity, valid ambiguous numeric date order, positive mixed-fraction versus product readings, numeric symbol multiplication versus a two-item list, a comma-grouped number versus two-item list, binary-versus-decimal integer readings, lowercase `i` as imaginary unit or variable, and AM/PM for ambiguous clock times. Angle and log choices can coexist in compound formulas. The time default follows server-local meridiem. General unit parsing and other assumption families remain limited | P0/P1 |
| Location and units | `ip`, `latlong`, `location`; location can affect results; `units=metric/nonmetric` | `location`, `latlong`, and `units=metric/nonmetric` route explicit weather queries to Open-Meteo; units are not shared across other domains, and IP-derived location is not implemented | P2 |
| Pod states | `podstate` changes a specific pod/subpod using state input returned by the result; repeated changes and `n@` shorthand are supported | Initial `DecimalApproximation` pod slice: returned state inputs support repeated and `n@` More/Fewer digits changes, recomputed from the base query; other pod families and subpod states are not implemented | P2 |
| Presentation sizing | `width`, `maxwidth`, `plotwidth`, `mag` | Not implemented | P2 |
| Time controls | `scantimeout`, `podtimeout`, `formattimeout`, `parsetimeout`, `totaltimeout`; `async` partial results and recalculate URL | `parsetimeout` has a 5-second default and 0.1–30 second range for isolated SymPy parsing; it is available on `/v2/query` and `/v2/validatequery`. Local `podtimeout` has a 4-second default and 0.1–30 second range for one symbolic result computation or provider retrieval; it is not a separate formatter-stage limit. `totaltimeout` has a 20-second default, 0.1–30 second range, and one monotonic deadline across symbolic worker execution or sequential provider requests. Initial `async` support defers one primary result pod behind a poll URL; jobs are in-process, capped at 16 active, and expire after 30 minutes. Scan and formatter-stage timeouts, independent multi-pod formatting, and recalculation remain absent | P2 |
| Query interpretation controls | `reinterpret`, `translation`, `ignorecase`; `validatequery` parses without full result computation | `ignorecase=true` case-folds mathematical parsing, while provider patterns are matched case-insensitively and preserve case-sensitive identifiers; original input is retained. `/v2/validatequery` uses the same control. Translation and reinterpretation remain unsupported | P1 |
| Classification | Clash assumptions and `datatypes`; pod `scanner` helps identify result subject | Candidate ranking covers angle units and logarithm bases; ambiguous `m`, lowercase `i`, and AM/PM clock candidates use neutral ties; a deterministic router handles selected weather, constant, entity, place, and series patterns; no general query classifier | P1/P3 |
| Informational elements | `assumptions`, `sources`, `warnings`, `infos`, `generalization`, `didyoumeans`, `tips`, `spellcheck`, `translation`, `reinterpret`, `examplepage`, `futuretopic`, `languagemsg`, and errors | Query results include assumptions, provider sources, warnings, tips, and errors; other informational element families remain absent | P1 |
| Error semantics | Distinguishes invalid requests/internal errors (`error=true`) from unrecognized queries (`success=false`); pod errors can be local | `/v2/query` returns queryresult-compatible invalid-request/backend error details with `error=true`, and unrecognized queries with `success=false`, `error=false`, and tips; provider/backend failures use HTTP 5xx | P0 |
| Forward compatibility | Clients should tolerate new result elements and attributes | No generic pod schema/parser yet | P1 |

### Assumption coverage

The reference groups assumptions into categorical (`Clash`, `MultiClash`, `SubCategory`, `Attribute`), mathematical (`Unit`, `AngleUnit`, `Function`, `ListOrTimes`, `ListOrNumber`, `CoordinateSystem`, `I`, `NumberBase`, `MixedFraction`), date/time (`TimeAMOrPM`, `DateOrder`, `MortalityYearDOB`), scientific (`TideStation`), and formula-input families (`FormulaSelect`, `FormulaSolve`, `FormulaVariable`, `FormulaVariableOption`, `FormulaVariableInclude`). The current registry implements `AngleUnit` for bare numeric trig inputs, `LogBase` for parenthesized `log(number)` calls, and `Function` for unparenthesized numeric log syntax such as `log 20`; `Unit` for the standalone ambiguous quantity `number m`; `DateOrder` for slash-separated numeric dates when both month/day and day/month readings are valid Gregorian dates; `MixedFraction` for positive whole numbers followed by a proper fraction; `ListOrTimes` for a numeric literal and one spaced Latin letter; `ListOrNumber` for one thousands-grouped numeric comma; `NumberBase` for standalone binary-looking integer strings; `I` for lowercase `i` as the imaginary unit or a variable; and `TimeAMOrPM` for ambiguous `H:MM` clock input. This service follows the server-local AM/PM for its default choice; Wolfram documents a default that varies with the query time. These `I` readings can coexist with supported angle, logarithm, and list-versus-multiplication choices. Compound formulas can combine angle choices with log choices: parenthesized-only log inputs expose `LogBase`, while an unparenthesized log input exposes `Function` and applies its selected meaning to the query's log calls. Unit alternatives currently apply only to the standalone quantity form. Other WA assumption families, general unit parsing, and per-occurrence overrides remain unimplemented.

Keep our IDs stable and service-owned. Store a registry version and typed meaning, expose the active choice and all alternatives, and validate overrides before routing. The classifier must never change a claim's scope or choose an assumption based on which one makes a proof succeed. See [Wolfram's assumption structure and application rules](https://products.wolframalpha.com/api/documentation?scrollTo=using-assumptions) for the wire behavior we are modeling; the project's IDs must remain distinct from Wolfram's opaque `input` tokens.

## Recommended implementation order

### P0 — establish a compatible request and response contract

1. **Done:** move interpretation details to `/v1/interpret` and reserve `/v2/query` for the Full Results-style envelope.
2. **Done for local access:** retain this service's bearer credential; do not accept or require a Wolfram AppID or send user queries to Wolfram.
3. **Initial slice:** validate `input`, `output`, and supported `format` names with stable query-level errors; stage-specific timeout controls remain.
4. **Initial slice:** version the query envelope and pod/subpod model; preserve typed assumptions and symbolic evidence as additive fields.
5. **Initial slice:** XML and JSON are rendered from the same response model. Unknown future result elements are not yet consumed from external providers because no WA provider is integrated.

### P1 — query control and inspectable interpretation

1. **Done for current pods:** implement the pod filters over Input and Result.
2. **Done for symbolic expressions:** `/v2/validatequery` discovers parse validity and candidate interpretations without simplification.
3. **Done for supported math input:** add `ignorecase=true` to `/v2/query` and `/v2/validatequery`; translation and reinterpretation still need suitable local language and scanner models.
4. Extend the typed assumption registry beyond the current unit, numeric date, mixed-fraction, `ListOrTimes`, `ListOrNumber`, `NumberBase`, `I`, `TimeAMOrPM`, and `Function` slices to other explicit, supportable unit inputs and coordinate systems. Add data/entity and formula assumptions only when a deterministic validator and a source-backed meaning are available.
5. **Initial slice:** return `datatypes`, `scanner`, assumptions, source citations, warnings, unsupported-query tips, and query-level errors from the same result planner; localized pod errors and more info element families remain.
6. **Initial slice:** explicit selection works for `AngleUnit` and either parenthesized `LogBase` or unparenthesized `Function` log syntax in one formula; a `Function` choice applies to all log calls in that query. `Unit` selection covers standalone `number m`; `DateOrder` selection covers valid ambiguous slash-separated numeric dates; `MixedFraction` selection covers a positive whole number followed by a proper fraction; `ListOrTimes` selection covers a numeric literal and spaced single-letter symbol; `ListOrNumber` selection covers a single thousands-grouped numeric comma; `NumberBase` selection covers standalone binary-looking integer strings; `I` selection distinguishes lowercase `i` as the imaginary unit or a variable; `TimeAMOrPM` selects AM or PM for ambiguous clock inputs. Unknown, repeated, conflicting, or non-applicable selections are rejected. Other assumption families and per-occurrence choices remain.

### P2 — presentation, locality, and execution control

1. Extend the bounded presentation-LaTeX and MathML parsers against Wolfram's accepted mathematical inputs, then improve plaintext and MathML rendering; add image/plot URLs, image maps, Wolfram-input/output-like text, cell-like render payloads, and audio only where a local backend can produce them safely.
2. Add location and preferred units as request context. Make IP-derived location opt-in and avoid retaining the caller's IP.
3. **Initial slice:** return and accept `DecimalApproximation__More digits` / `Fewer digits` state inputs, including repeated values and `n@` shorthand. Extend states to other mathematical and data pods, subpod states, and stateful paging.
4. **Done (initial slice):** add `parsetimeout`, a local per-result `podtimeout` work budget, `totaltimeout`, and asynchronous delivery for one primary result pod with bounded in-process jobs and polling. Add distinct scan and formatting stages, independent per-pod formatting and delivery, and a recalculation/resume contract; the local pod budget does not yet match Wolfram's formatter-stage control.
5. Add width and display scaling only for result types that support renderable output.

### P3 — broaden independently sourced coverage

Build coverage around licensed and attributable providers, measuring query-class recall and source freshness. The initial weather, CODATA, Wikidata entity, GeoNames place, and DBnomics series routes now appear as Full Results-style pods; next broaden entity relationships, economic series discovery, geography, units, and weather controls. Do not describe provider coverage as full Wolfram|Alpha parity; preserve provider, license/attribution, retrieval time, and whether the value is computed, retrieved, solver-derived, or kernel-checked.

## Acceptance checklist for a parity release

- The `/v2/query` route has a stable, documented request contract and does not confuse our credentials with Wolfram credentials.
- `GET /v2/validatequery` can report parse/interpretation candidates independently of result computation.
- JSON and XML encode the same query result and preserve unknown additive fields safely.
- Pod IDs are stable within a query contract; filtering supports repeatable include/exclude and wildcard title options.
- Assumption proposals list typed, versioned service IDs; caller overrides are validated before backend routing and never chosen from proof outcomes.
- Decimal approximation state inputs are returned with the pod, can be resubmitted through `podstate`, and produce the requested precision within the documented cap.
- Every pod reports its source and evidence kind; warnings, errors, and timeouts are distinguishable.
- Timeouts, async jobs, and pod states have documented resource limits and deterministic follow-up behavior.
- Compatibility tests cover request validation, all supported query parameters, response schemas, assumption round-trips, and error cases.
- The product documentation distinguishes protocol compatibility from data/answer coverage.

## Source notes

The audit is based on the downloaded API 2.0 Revision A page, including its parameter reference, result element catalog, pod formatting, location, assumption, timeout/async, error, and `validatequery` sections. The page states that queries are GET requests, supports XML/JSON result envelopes, and cautions clients to tolerate future elements and attributes. See the [official API reference](https://products.wolframalpha.com/api/documentation) and its [assumption section](https://products.wolframalpha.com/api/documentation?scrollTo=using-assumptions).
