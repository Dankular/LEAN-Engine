# Wolfram|Alpha Full Results API: feature parity audit

> Source: [Wolfram|Alpha Full Results API documentation](https://products.wolframalpha.com/api/documentation)  
> Documentation revision read: API 2.0, Revision A  
> Audited: 2026-10-19
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
| `GET /v2/query` | Local `queryresult` JSON/XML envelope, Input, Result, DecimalApproximation, selected Data pods, symbolic expression results and bounded `N[expr]` / `N[expr,n]` numeric evaluation, AngleUnit/LogBase assumptions (including both in a compound formula), Function alternatives for unparenthesized forms such as `log 20`, typed quantity literals and compound expressions for curated physical-unit aliases, Unit assumptions for numeric `m` tokens in standalone and compound formulas, DateOrder assumptions for valid ambiguous numeric dates, MixedFraction alternatives for forms such as `3 1/2`, ListOrTimes alternatives for forms such as `3 x`, ListOrNumber alternatives for forms such as `1,234.5`, NumberBase alternatives for binary-looking integer strings, I alternatives for lowercase `i`, TimeAMOrPM alternatives for ambiguous clock values, decimal pod states, pod filters, bounded natural-language math instructions, plaintext/MathML, and explicit routes to five factual providers | Partial protocol and query coverage; `/v1/interpret` retains the project-specific interpretation response |
| `GET /v2/validatequery` | Safe parsing and candidate assumption discovery without the symbolic simplification step | Shares the bounded symbolic operation templates; broad natural-language scanning remains unsupported |
| `GET /v2/assumptions` | Service-owned, versioned angle-unit, logarithm-base, `Function`, ambiguous `m` unit, numeric `DateOrder`, `MixedFraction`, `ListOrTimes`, `ListOrNumber`, `NumberBase`, `I`, `TimeAMOrPM`, `GDPType`, and `GDPPerCapitaType` IDs | Initial slices only; other WA assumption families and general unit parsing remain |
| `POST /v1/prove`, `/v1/prove-formula`, and `/v1/counterexample` | Typed or bounded formula-text integer/real polynomial claims, Z3 counterexamples, and Lean kernel checks | Bounded arithmetic fragment; variables need explicit sorts; nonlinear polynomials use Z3 and Lean `nlinarith`, subject to the configured degree and time limits; formula text is not natural-language formalization |
| `POST /v1/natural-language/prove` | Kimina-compatible model proposes one restricted Lean theorem; caller reviews and approves the draft before Lean compiles it and audits axioms | The first response returns the full Lean source, a draft ID, and a digest over the exact problem and theorem statement. Lean is not run until the caller submits that ID and digest with `approve_formalization=true`. Drafts are process-local, capped at 128, and expire after 15 minutes. Success is labeled `proof_checked`, while correspondence to the English request remains unverified even after caller approval |
| `POST /v1/symbolic` | Bounded SymPy grammar with simplify, factor, expand, calculus, real limits and Taylor series, finite sums/products, solve, and identity operations | Computation building block, not a general free-form query interpreter |
| `POST /v1/knowledge/query` | Explicit Wikidata search/entity and allowlisted property queries (population P1082, capital P36, official language P37, currency P38, coordinate location P625, elevation P2044, area P2046, nominal GDP P2131, GDP at purchasing power parity P4010, nominal GDP per capita P2132, PPP GDP per capita P2299), DBnomics dataset search and series retrieval, CODATA subset, GeoNames, and Open-Meteo operations | Current provider adapters; `/v2/query` wraps population, capital, official-language, currency, coordinates, elevation, area, GDP, and GDP per capita questions through Wikidata with up to three ranked entity candidates and property statements, plus explicit weather, CODATA, Wikidata entity, GeoNames place, DBnomics dataset-search, and series requests as source-backed pods. DBnomics search returns ranked dataset candidates and requires caller selection before an exact series request. Generic GDP and GDP per capita queries require their typed measure choices; quantity properties preserve units, rank, reference counts, bounds, and bounded qualifier details including point-in-time values. Retrieved facts remain source data, not Lean proofs |
| `POST /api/cag/v1/WolframLanguageCompute` | Exact rational arithmetic; bounded polynomial `Simplify`/`Expand`/`Factor`; Lean-checked additive and integer-multiple `TrigExpand` through 12× for `Sin` and `Cos`; `D[expression, variable]`, indefinite `Integrate[expression, variable]`, and definite `Integrate[expression, {variable, lower, upper}]` for supported expressions; real polynomial `Solve` for supported equations | Decimal tokens use exact base-10 rational semantics. Calculus inputs are bounded to 256 nodes, 32 levels, 256-bit numeric components, powers 0–16, and `Sin`, `Cos`, `Exp`, `Sinh`, `Cosh`, `Log` differentiation under positive-argument conditions, `ArcTan`, `ArcSinh`, `ArcCosh`, `ArcTanh`, and `Tanh` differentiation in the bounded real grammar, `Tan` differentiation under explicit `Cos[argument] ≠ 0` conditions, and reciprocal trig derivatives `Sec`, `Csc`, and `Cot` under explicit nonzero cosine or sine conditions. `D` supports variable denominators when Lean can prove the derivative under explicit nonzero domain conditions; all required domain conditions are returned as `evidence.domain_conditions`. Tangent inputs at poles are rejected. TrigExpand accepts bounded sine and cosine expressions with sums/differences of variables and integer multiples 2 through 12 of one variable; SymPy proposes the expansion, and Lean checks the identity before returning it. Indefinite `Integrate` returns one antiderivative representative with its additive constant omitted, only when Lean checks its derivative against the submitted integrand. Selected variable-denominator forms such as `1/x`, `1/(x+1)`, and `1/(x+1)^2` return explicit nonzero conditions; logarithmic antiderivatives such as `Integrate[1/x, x]` and `Integrate[Log[x], x]` are supported with real domain conditions. Selected tangent antiderivatives such as `Integrate[Tan[x], x]` return `-log(Abs(cos(x)))` under `Cos[x] ≠ 0`; other forms remain conditional on the generated result fitting the Lean grammar. Definite integrals accept exact rational bounds. Rational-frequency `Sin[a*x+b*Pi]` and `Cos[a*x+b*Pi]` also accept exact `Pi` bounds from `{-Pi, -Pi/2, -Pi/4, -Pi/8, -Pi/16, -Pi/32, 0, Pi/32, Pi/16, Pi/8, Pi/4, Pi/2, Pi}` when both scaled and shifted endpoint angles reduce to one of `0`, `±Pi/32`, `±Pi/16`, `±Pi/8`, `±Pi/4`, `±Pi/2`, `±Pi`, or `±2Pi`. SymPy proposes exact values in the Q-span of 1, sqrt(2), sqrt(2+sqrt(2)), sqrt(2-sqrt(2)), sqrt(2+sqrt(2+sqrt(2))), sqrt(2-sqrt(2+sqrt(2))), sqrt(2+sqrt(2+sqrt(2+sqrt(2)))), and sqrt(2-sqrt(2+sqrt(2+sqrt(2)))); Lean checks the affine antiderivative and exact endpoint identities. Other real-radian offsets are unsupported. Lean checks values through the interval fundamental theorem of calculus and audits axioms. Selected variable-denominator cases are supported. Lean automatically proves affine nonzero conditions implied by exact interval bounds; unresolved conditions are returned in evidence.domain_conditions. Verified examples include 1/x and 1/(x+1) on [1, 2] with no returned conditions, plus 1/(x-a) on [0, 1] with an explicit interval-wide condition, `Integrate[1/Sqrt[1-x^2], {x, 0, 1/2}]` returning `pi/6` with a checked endpoint identity and the square-root nonzero condition still explicit, `Integrate[Sin[x], {x, 0, Pi}]` returning 2, `Integrate[Sin[2x], {x, 0, Pi}]` returning 0, and `Integrate[Cos[x/2], {x, 0, Pi}]` returning 2, `Integrate[Sin[x+Pi/2], {x, 0, Pi/2}]` returning 1, and `Integrate[Cos[x+Pi/2], {x, 0, Pi/2}]` returning -1, `Integrate[Cos[x], {x, 0, Pi/4}]` returning `sqrt(2)/2`, and `Integrate[Sin[x], {x, 0, Pi/4}]` returning `1 - sqrt(2)/2`, `Integrate[Cos[x], {x, 0, Pi/8}]` returning `sqrt(1/2 - sqrt(2)/4)`, and `Integrate[Sin[x], {x, 0, Pi/8}]` returning `1 - sqrt(2 + sqrt(2))/2`, `Integrate[Cos[x], {x, 0, Pi/16}]` returning `sqrt(2 - sqrt(2 + sqrt(2)))/2`, and `Integrate[Sin[x], {x, 0, Pi/16}]` returning `1 - sqrt(2 + sqrt(2 + sqrt(2)))/2`, `Integrate[Cos[x], {x, 0, Pi/32}]` returning `sqrt(2 - sqrt(2 + sqrt(2 + sqrt(2))))/2`, and `Integrate[Sin[x], {x, 0, Pi/32}]` returning `1 - sqrt(2 + sqrt(2 + sqrt(2 + sqrt(2))))/2`; free variables are real parameters. Rational-function `Limit` supports exact rational points and bounded rational-coefficient polynomial points in real parameters; free real parameters are listed in `evidence.real_parameters`, denominator conditions are returned explicitly, and selected removable singularities support explicit products and SymPy-proposed factors of polynomial denominator components of degree at most 16, with Lean-checked factorizations and explicit conditions for other factors. The route accepts `line`, `maxChars`, and `timeConstraint`; evaluation timeouts return the documented message-bearing `EvaluationTimeExceeded` Failure result with `success=true`. Non-elementary antiderivatives, arbitrary Wolfram Language code, graphics outside the bounded 2D primitive renderer, unsupported or ambiguous free-form prompts, and arbitrary remote data lookup receive HTTP 501. The initial FreeformPrompt and leading-equals slice routes supported provider-backed data intents through the local query parser, preserves source provenance, and does not present retrieved facts as Lean proofs; supported symbolic intents request Lean verification. This is not Full Results API compatibility |

The [README](README.md) covers local setup and the main API areas. Supported operations and factual providers are listed in [features.json](features.json).

### CAG graphics slice

The CAG route renders bounded `Graphics` expressions containing circles and ellipses, disks, rectangles, lines, arrows, polygons, and points. `Arrow[{{x1,y1},...,{xn,yn}}]` draws a linear path with a default endpoint head; custom `Arrowheads`, curved paths, and 3D graphics remain unsupported. It supports a curated set of color directives (`Red`, `Blue`, `Green`, `Black`, `White`, `Gray`, `Orange`, `Purple`, `Cyan`, `Magenta`, `Yellow`, `Pink`, `Brown`, `RGBColor`, and `GrayLevel`), plus `Opacity`, line thickness, point size, `PlotRange`, `ImageSize`, and `Background`. Results are Markdown links to service-hosted SVGs. The capability URL is retained in a bounded process-local store for 15 minutes. Expressions and options outside this grammar remain unsupported; graphics are renderings, not Lean proofs.

### CAG API request contract

`POST /api/cag/v1/WolframLanguageCompute` requires a JSON `code` string and accepts
`line` (default `1`), `maxChars` (default `10000`), and `timeConstraint` (default `60`)
within the service's configured bounds. The response carries `result`, `code`, `success`,
and `uuid`; successful proofs also include the service's Lean evidence. A timeout is an
HTTP 200 response with `success=true` and a Wolfram `Failure` expression containing the
documented `MessageTemplate` and `MessageParameters` fields. Authentication accepts the
configured key directly or as a bearer token.

The CAG route also evaluates exact Sin, Cos, and selected Tan values for bounded
rational multiples of Pi. It reduces the coefficient by integer periods and reflection,
then accepts a base angle of zero or Pi/d. Sin and Cos also accept half-pi complements
Pi/2 - Pi/d for d = 5, 8, 10, 16, 32, powers of two through 4096, and up to eight
successive halvings from Pi/6 or Pi/10. Sin and Cos support d = 2, 3, 4, 5, 6, 8, 10,
16, 32, powers of two from 64 through 4096, and denominators 6*2^k or 10*2^k for
1 <= k <= 8; Tan supports d = 3, 4, and 6, with poles rejected. The rational numerator
and denominator are limited to 256 bits. Lean checks period, reflection, and cofunction
identities plus the Mathlib base values; fifth and tenth angle values derive from
Mathlib's cosine-at-Pi/5 theorem, while the dyadic and iterated half-angle values use
Mathlib's half-angle theorems. For example, Sin[2 Pi/3] reduces to Sin[Pi - Pi/3],
Sin[3 Pi/8] reduces to Cos[Pi/8], Sin[2 Pi/5] reduces to Cos[Pi/10], Sin[Pi/12] is
derived from Pi/6, and Sin[31 Pi/64] reduces to Cos[Pi/64]. Other reduced angles and
arbitrary numeric angles remain unsupported.

The CAG endpoint and `/v2/query?verify=true` also support finite `Sum` and `Product`
over inclusive integer bounds when the integrand and result fit the rational-polynomial
grammar. Ranges are limited to 128 terms; negative bounds and reversed bounds are handled,
with reversed ranges using the empty-sum value `0` and empty-product value `1`. Lean checks
the finite `Finset.range` computation, and the query verifier attaches evidence only when
that exact result matches the SymPy result.

`Limit[expression, {variable, point}]` also receives Lean evidence for bounded rational
functions at exact rational points within the 256-bit numeric limit, and at bounded
rational-coefficient polynomial points in real parameters (total degree at most 64,
coefficient components up to 256 bits). For a symbolic point, its symbols are treated as
real. The proof evidence lists all free real parameters in `evidence.real_parameters`.
For an ordinary continuity proof, every nonconstant denominator must be nonzero after
substitution; required conditions are returned in `evidence.domain_conditions`. Free real
parameters are supported in numerators and symbolic denominators. A removable-singularity
proof is attempted when at least one denominator vanishes at the point. A vanishing
denominator may be an affine rational expression, a positive integer power of one, or an
explicit product of factors containing affine rational factors and other factors that
remain nonzero at the point. Polynomial components of degree at most 16 in the limit
variable can also be factored by SymPy, subject to the existing expression, coefficient,
and total-degree bounds; Lean proves the resulting factorization before it is used. At
symbolic points, the vanishing affine factors must have a nonzero rational slope. Lean
proves the factorization and nonzero behavior on the
punctured neighborhood; any unresolved nonzero-at-point condition for the other factors
is returned in `evidence.domain_conditions`. The full expression must simplify to a
bounded rational function, and Lean proves the original formula equals it on the punctured
neighborhood before checking the exact limit value. For example,
`Limit[((x-a)/(x-a))/(x+1), {x,a}]` is checked as `1/(a+1)` with the condition
`x + 1 ≠ 0 at x = a`, while `Limit[(x-a)/(x-a), {x,a}]` is checked as `1` and reports
`a` in `evidence.real_parameters`. `Limit[(x-1)/(x^2-1), {x,1}]` is checked as `1/2`
with no unresolved conditions. For a symbolic point,
`Limit[((x-a)*(x+1))/((x-a)*(x+1)), {x,a}]` is checked as `1` with the explicit condition
`x + 1 ≠ 0 at x = a`. True poles such as `Limit[1/(x-a), {x,a}]` and removable forms
outside the supported factor grammar remain symbolic results without Lean evidence on
`/v2/query`; the CAG route returns its documented unsupported-expression status for
unsupported forms.

The verified calculus subset also supports `D[ArcTan[2x], x]` and the
`Integrate[1/(1+x^2), x]` and `Integrate[ArcTan[x], x]` cases. Results are returned only
after Lean checks the derivative; generated conditions such as `x^2 + 1 ≠ 0` are
included in the evidence when the proof currently needs them.

`D[Sec[x], x]`, `D[Csc[x], x]`, and `D[Cot[x], x]` are also Lean-checked. They return
the required nonzero conditions `Cos[x] ≠ 0` or `Sin[x] ≠ 0` in
`evidence.domain_conditions`.

Explicit-base `Log[base, argument]` differentiation and selected indefinite
integrals accept a statically rational base greater than zero and different from
one. Selected definite integrals also work when the argument is affine in the
integration variable and remains positive over the exact rational bounds. The
service proves the logarithm of the base is nonzero and Lean checks the derivative
and interval-integral result. Variable bases and nonlinear definite arguments
remain outside this slice.

`Solve[lhs == rhs, variable, Reals]` supports univariate rational-coefficient
polynomials through degree eight when they factor completely over the rationals.
For degrees five through eight, each coefficient numerator and denominator is limited
to 128 bits; degrees at most four retain the 256-bit coefficient bound. Exact real
quadratic-radical roots and quadratics with no real roots are also supported. Nonzero
constant equations are also supported. `Solve[{eq1, ..., eqm}, {x1, ..., xn}, Reals]`
supports rational linear systems with one to four equations and two to four variables.
It returns unique solutions, parametric rules for consistent underdetermined systems,
or an empty set when the system is inconsistent. Nonlinear systems or systems with
more than four equations or variables remain unsupported, as do complex-domain solving,
degree greater than eight, degree-three through degree-eight polynomials without a
complete rational factorization, and degree-five through degree-eight inputs that exceed
the coefficient bound. Lean checks each returned solution set against the submitted
equation or system.

`D[ArcSin[argument], x]` and `D[ArcCos[argument], x]` now require the explicit real
domain conditions `-1 < argument` and `argument < 1`. Constant arguments outside
`[-1, 1]` are rejected by this real-calculus route.

`D[Sqrt[x], x]` is checked under `x > 0`; selected square-root antiderivatives are also
checked, including `Integrate[1/Sqrt[x], x] = 2*sqrt(x)`. The request evidence reports
the positivity condition and any separate denominator condition used by the proof.

The indefinite integral path also supports `Integrate[1/Sqrt[1-x^2], x]`, returning
`asin(x)` after Lean checks its derivative. The response includes the strict inverse-sine
argument bounds and the radicand and denominator conditions used by the proof.
It also supports `Integrate[ArcSin[x], x] = x*asin(x) + sqrt(1 - x^2)` and
`Integrate[ArcCos[x], x] = x*acos(x) - sqrt(1 - x^2)`; scaled arguments such as
`Integrate[ArcSin[2x], x]` retain the corresponding strict argument bounds in evidence.

Lean also checks the reciprocal-trigonometric square integrals
`Integrate[Sec[x]^2, x]`, `Integrate[Csc[x]^2, x]`, and
`Integrate[Cot[x]^2, x]`. The service returns explicit `Cos[x] != 0` for the
secant case and `Sin[x] != 0` for the cosecant and cotangent cases. These proofs use the
sine-cosine identity after differentiating the generated antiderivative.

The real-calculus grammar now includes `ArcSinh`. Lean checks `D[ArcSinh[x], x]` as
`1/sqrt(1 + x^2)` and checks `Integrate[1/Sqrt[1+x^2], x] = asinh(x)` and
`Integrate[ArcSinh[x], x] = x*asinh(x) - sqrt(1 + x^2)`. The integral evidence reports
the positivity and denominator conditions generated for the square-root expressions.

`ArcCosh` differentiation requires `argument > 1`; `D[ArcCosh[x], x]` returns
`1/sqrt(x^2 - 1)` with that condition. The engine also checks
`Integrate[1/Sqrt[x^2-1], x] = log(Abs(x + sqrt(x^2 - 1)))` and
`Integrate[ArcCosh[x], x] = x*acosh(x) - sqrt(x^2 - 1)`, reporting the generated
real-domain and denominator conditions.

`Tanh` differentiation is Lean-checked for bounded real compositions using the
hyperbolic quotient identity, and `Integrate[Tanh[x], x]` is accepted when the
symbolic backend proposes `log(cosh(x))`. Lean proves `cosh` is positive in this
proof, so the result has no added domain condition.
Definite `Integrate[Tanh[a*x+b], {x,l,u}]` supports rational nonzero slopes
and exact rational bounds; Lean checks the log-cosh endpoint difference through
the interval fundamental theorem, with no extra domain conditions.

`Coth` is represented as `Real.cosh argument / Real.sinh argument` in generated
Lean code. `D[Coth[f(x)], x]` is accepted when Lean checks the derivative and the
service returns the explicit pole condition `Sinh[f(x)] ≠ 0`. Indefinite
integrals of `Coth[a*x+b]` are supported for rational nonzero `a`; the candidate
`log(Abs(Sinh[a*x+b]))/a` is returned only after Lean checks its derivative,
with the same nonzero-sinh condition in the evidence.
Definite `Integrate[Coth[a*x+b], {x,l,u}]` also supports rational nonzero slopes
and exact rational bounds when the affine argument has the same nonzero sign at
both endpoints. Lean checks the exact endpoint log-sinh difference through the
interval fundamental theorem and returns an interval-wide `Sinh[argument] != 0`
condition. Bounds containing or crossing a Coth pole are rejected.

`Sech` and `Csch` derivatives are also checked for bounded real compositions.
`Sech` is represented as `1/Cosh[argument]`; Lean derives the required
nonzero-cosh fact from positivity, so no extra condition is exposed. `Csch` is
represented as `1/Sinh[argument]` and returns `Sinh[argument] ≠ 0`. Their
antiderivatives are handled separately: `Integrate[Sech[a*x+b], x]` supports
rational nonzero slopes `a` and returns `ArcTan[Sinh[a*x+b]]/a` after Lean checks
the derivative using `cosh² - sinh² = 1`. It needs no additional domain
condition. Definite `Integrate[Sech[a*x+b], {x,l,u}]` also supports rational
nonzero slopes and exact rational bounds; Lean checks the arctan-sinh endpoint
difference, with no extra domain conditions. `Integrate[Csch[a*x+b], x]` supports
rational nonzero slopes `a` and
returns `Log[Abs[Tanh[(a*x+b)/2]]]/a` after Lean checks the derivative using the
hyperbolic double-angle identity. It returns nonzero conditions for both
`Tanh[(a*x+b)/2]` and `Sinh[a*x+b]`; nonlinear Csch arguments remain unsupported.
For definite integrals, exact rational bounds are accepted when the affine
argument has the same nonzero sign at both endpoints. Lean checks the endpoint
log-tanh difference through the interval fundamental theorem and returns
interval-wide nonzero conditions for the sinh denominator and tanh logarithm.
Bounds that contain or cross a Csch pole are rejected.

`ArcTanh` differentiation is checked under the explicit strict conditions
`-1 < argument` and `argument < 1`. For `Integrate[1/(1-x^2), x]`, the service
proposes `atanh(x)` and returns those bounds alongside any denominator condition.

## Full Results API feature inventory

The documented parameter groups are:

| Group | Parameters or behavior | Current project status | Priority |
|---|---|---|---|
| Request and identity | `input`, `appid`, URL encoding; GET `/v2/query` | `input` and URL encoding; service bearer auth is used instead of an `appid` query credential; no Wolfram credential is accepted | P0 |
| Input syntax | Free-form text; math input may use presentation LaTeX or MathML | Safe native expressions plus bounded presentation-LaTeX and presentation-MathML subsets, explicit SymPy operations including `N[expr]` and `N[expr,n]` for finite numeric inputs, and a bounded polynomial comparison grammar on `/v1/prove-formula`; bounded natural-language templates cover common supported math operations and explicit identity proof requests such as “prove that x + 0 equals x”; `verify=true` checks supported polynomial and exact rational-Pi trigonometric differences with Lean, and reports proved identities or refutations from a nonzero constant difference, validated rational counterexample, or Lean-certified interval that excludes zero. Z3 counterexamples are independently replayed with exact rational arithmetic. English-to-formula semantic fidelity remains unverified. Sums/products require integer-literal bounds and ambiguous function scope requires explicit parentheses; broad language scanning and general TeX/MathML remain unsupported | P0/P3 |
| Result envelope | `output=xml/json`; query-level status and metadata such as `success`, `error`, `numpods`, `datatypes`, timing, timeouts, version, and recalculation information | XML and JSON `queryresult` envelopes include documented aggregate `timing`, `parsetiming`, and `parsetimedout` fields with stable query IDs. The reference does not define separate scan- or format-duration fields. Scan-timeout results can include the bounded, process-local recalculation extension described below | P0 |
| Pod model | Ordered pods with `id`, `title`, `scanner`, `position`, `primary`, per-pod error, subpods, and supplemental elements | Input, Result, and selected Data pods use the documented core fields. Each pod now also carries a service-owned `source`, `evidence_kind`, and `verified` field: provider pods identify their source records, computation pods identify the engine and Lean proof checker when used, and Input pods are marked as unverified interpretations. Broad supplemental families remain absent | P0 |
| Pod selection | `includepodid`, `excludepodid`, `podtitle` (including `*`), `podindex`, `scanner`; repeated values supported | Implemented over locally generated math and provider pods; repeated and comma-separated values accepted | P1 |
| Output representation | Per-pod `format`: `image`, `imagemap`, `plaintext`, `minput`, `moutput`, `cell`, `mathml`, `sound`, `wav`; multiple format values; default `plaintext,image` | The default is aligned. Mathematical subpods return inline SVG images for Input, Result, and DecimalApproximation in JSON or `<img>` elements in XML. `cell` returns a Wolfram `Cell[BoxData[FormBox[RowBox[…]]]]` wrapper around the visible text for mathematical and provider subpods; this is text-backed and does not reproduce Wolfram's rich typesetting. Seven-day Open-Meteo forecasts include a daily temperature and precipitation chart; other provider data subpods receive text SVGs. Formula images structurally render a bounded presentation-MathML subset and fall back to escaped text for unsupported structures; plots show sampled graph paths and honor `plotwidth` (default 200; local range 100–1,600). Plot `moutput` returns its reproducing `Plot[...]` expression. Provider-specific charts/maps, image maps, sound, and WAV remain unavailable | P2 |
| Assumptions | Discover alternatives in result data and apply one or more exact values on a subsequent query | Versioned service IDs, ranked alternatives, and validated overrides cover angle units, bare-log bases, the `m` abbreviation in a standalone quantity, valid ambiguous numeric date order, positive mixed-fraction versus product readings, numeric symbol multiplication versus a two-item list, a comma-grouped number versus two-item list, binary-versus-decimal integer readings, lowercase `i` as imaginary unit or variable, and AM/PM for ambiguous clock times. Angle and log choices can coexist in compound formulas. The time default follows server-local meridiem. Direct quantities now support a curated set of common physical units, while the wider unit database and other assumption families remain limited | P0/P1 |
| Location and units | `ip`, `latlong`, `location`; location can affect results; `units=metric/nonmetric` | `location`, `latlong`, and `units=metric/nonmetric` route current weather and seven-day forecast queries to Open-Meteo; forecast charts use `plotwidth`; units are not shared across other domains, and IP-derived location is not implemented | P2 |
| Pod states | `podstate` changes a specific pod/subpod using state input returned by the result; repeated changes and `n@` shorthand are supported | Initial `DecimalApproximation` pod slice: returned state inputs support repeated and `n@` More/Fewer digits changes, recomputed from the base query; other pod families and subpod states are not implemented | P2 |
| Presentation sizing | `width`, `maxwidth`, `plotwidth`, `mag` | `width` controls text-oriented SVGs from 100 to 1,600 pixels, default 500; `maxwidth` controls structural mathematical images over the same range, default 500; `plotwidth` controls plots and forecast graphics from 100 to 1,600 pixels, default 200. `mag` scales content inside structural mathematical SVGs from 0.5 to 2.0, default 1.0, without changing the pod dimensions; provider and plot images do not apply it. `maxwidth` does not affect text fallbacks or plots | P2 |
| Time controls | `scantimeout`, `podtimeout`, `formattimeout`, `parsetimeout`, `totaltimeout`; `async` partial results and recalculate URL | `parsetimeout` has a 5-second default and 0.1–30 second range for isolated SymPy parsing. `scantimeout` has a 3-second default and 0.1–30 second range for symbolic computation or provider retrieval. `podtimeout` has a 4-second default and 0.1–30 second range per output pod; `formattimeout` has an 8-second default and the same range for cumulative output formatting. These controls check each bounded synchronous render after it completes and omit over-budget pods; an active render is not interrupted. `totaltimeout` has a 20-second default, 0.1–30 second range, and one monotonic deadline across symbolic execution, provider retrieval, and output pod rendering; final XML/JSON serialization occurs afterward. Project extension `prooftimeout` defaults to 60 seconds (range 1–180) and is one monotonic budget shared across result proof checking, identity counterexample search, and any secondary Lean-certified nonzero-interval check; it is separate from the query scan/format budget. Initial `async` support provides a primary-pod poll URL and a query-level `/v2/asyncQuery/{job_id}` URL that returns the complete query envelope from the same computation. Jobs are in-process, capped at 16 active, and expire after 30 minutes. Pods are returned together after computation; progressive per-pod delivery and partial scanner results are not implemented. A scan timeout can return a single-use `/v2/recalculate/{job_id}` URL; its in-process token expires after 30 minutes, and continuation reruns the one result request with increased budgets without reusing solver state. Hard interruption of active formatting remains absent | P2 |
| Query interpretation controls | `reinterpret`, `translation`, `ignorecase`; `validatequery` parses without full result computation | `ignorecase=true` case-folds mathematical parsing, while provider patterns are matched case-insensitively and preserve case-sensitive identifiers; original input is retained. Project extension `verify=true` requests Lean evidence for supported polynomial evaluation, real polynomial solving, real-calculus computations, and certified numeric approximations in the rational, radical, exact-trigonometric, and Pi interval fragments. Solve proofs attach only when the SymPy result matches the exact real solution set from the Lean candidate; proofs cover the selected formula, not natural-language fidelity. `/v2/validatequery` uses the same case control. Translation and reinterpretation remain unsupported | P1 |
| Classification | Clash assumptions and `datatypes`; pod `scanner` helps identify result subject | Candidate ranking covers angle units and logarithm bases; ambiguous `m`, lowercase `i`, and AM/PM clock candidates use neutral ties; deterministic templates route supported symbolic instructions, while a deterministic router handles selected weather, constant, Wikidata population/capital, entity, place, and series patterns; no general query classifier | P1/P3 |
| Informational elements | `assumptions`, `sources`, `warnings`, `infos`, `generalization`, `didyoumeans`, `tips`, `spellcheck`, `translation`, `reinterpret`, `examplepage`, `futuretopic`, `languagemsg`, and errors | Query results include assumptions, provider sources, warnings, tips, and errors; other informational element families remain absent | P1 |
| Error semantics | Distinguishes invalid requests/internal errors (`error=true`) from unrecognized queries (`success=false`); pod errors can be local | `/v2/query` returns queryresult-compatible invalid-request/backend error details with `error=true`, and unrecognized queries with `success=false`, `error=false`, and tips; provider/backend failures use HTTP 5xx | P0 |
| Forward compatibility | Clients should tolerate new result elements and attributes | JSON preserves additive fields; XML keeps additive scalar query/pod fields as attributes and serializes complex fields at the query, pod, subpod, assumption, source, warning, state, and evidence levels as JSON-encoded extension elements. Invalid XML field names are retained through a `<field name="…" encoding="json">` element. The mapping is covered by focused serializer contract tests; exhaustive parameter and result-family fixtures remain open | P1 |

`verify=true` also covers bounded sums, products, and rational scaling over supported `sin`, `cos`, and `tan` calls at rational multiples of pi. The verifier composes the CAG trigonometric theorems in Lean and displays the exact expression proved in the Result pod. Division is limited to nonzero rational denominators and powers to nonnegative integer exponents through 16. This remains an explicit formula slice; general trigonometric identities and natural-language fidelity are not implied.

The CAG `Tan` route now derives additional supported rational-pi values from the corresponding Lean-checked `Sin` and `Cos` values. It returns their exact quotient, applies the existing period/reflection and half-pi-complement identities, and rejects poles. Radical quotient simplification remains outside this slice.

### Assumption coverage

The reference groups assumptions into categorical (`Clash`, `MultiClash`, `SubCategory`, `Attribute`), mathematical (`Unit`, `AngleUnit`, `Function`, `ListOrTimes`, `ListOrNumber`, `CoordinateSystem`, `I`, `NumberBase`, `MixedFraction`), date/time (`TimeAMOrPM`, `DateOrder`, `MortalityYearDOB`), scientific (`TideStation`), and formula-input families (`FormulaSelect`, `FormulaSolve`, `FormulaVariable`, `FormulaVariableOption`, `FormulaVariableInclude`). The current registry implements `AngleUnit` for bare numeric trig inputs, `LogBase` for parenthesized `log(number)` calls, and `Function` for unparenthesized numeric log syntax such as `log 20`; `Unit` for ambiguous numeric `m` tokens in standalone and compound formulas, including per-occurrence overrides; `DateOrder` for slash-separated numeric dates when both month/day and day/month readings are valid Gregorian dates; `MixedFraction` for positive whole numbers followed by a proper fraction; `ListOrTimes` for a numeric literal and one spaced Latin letter; `ListOrNumber` for one thousands-grouped numeric comma; `NumberBase` for standalone binary-looking integer strings; `I` for lowercase `i` as the imaginary unit or a variable; and `TimeAMOrPM` for ambiguous `H:MM` clock input. This service follows the server-local AM/PM for its default choice; Wolfram documents a default that varies with the query time. These `I` readings can coexist with supported angle, logarithm, and list-versus-multiplication choices. Compound formulas can combine angle choices with log choices: parenthesized `log(number)` inputs expose `LogBase`, while unparenthesized `log 20` inputs expose `Function`. Append `@N` to a service assumption ID to override the one-based Nth occurrence for angle, parenthesized-log, unparenthesized function-log, or numeric `m` unit interpretations (for example, `angle:degrees@1`, `log:base-10@1`, `function:log-base-10@1`, or `unit:m-minutes@2`); unselected occurrences keep their service defaults. `Function` and `LogBase` remain independent when both syntaxes occur in one query. A query-wide choice and per-occurrence choices cannot be mixed for the same assumption type. Direct and compound quantities use the curated physical-unit vocabulary and reject incompatible dimensions; the ambiguous `m` alternatives remain separate service-owned assumptions. Other WA assumption families and unit aliases outside the curated set remain unimplemented.

The current CoordinateSystem slice supports the documented `div(x rho,y z,z x)` example as a three-component polynomial divergence. `coordinate:cartesian-3d` is the service default; `coordinate:cylindrical-3d` is an explicit alternative. Both use neutral tied candidate scores, and callers can use `@1` for the single divergence occurrence. Cylindrical results include `rho > 0`. The expression is calculated with SymPy; `verify=true` reports unsupported because the coordinate-calculus transformation itself does not yet have a Lean certificate. Other coordinate-system values and vector-calculus operators remain unsupported.

Keep our IDs stable and service-owned. Store a registry version and typed meaning, expose the active choice and all alternatives, and validate overrides before routing. The classifier must never change a claim's scope or choose an assumption based on which one makes a proof succeed. See [Wolfram's assumption structure and application rules](https://products.wolframalpha.com/api/documentation?scrollTo=using-assumptions) for the wire behavior we are modeling; the project's IDs must remain distinct from Wolfram's opaque `input` tokens.

## Recommended implementation order

### P0 — establish a compatible request and response contract

1. **Done:** move interpretation details to `/v1/interpret` and reserve `/v2/query` for the Full Results-style envelope.
2. **Done for local access:** retain this service's bearer credential; do not accept or require a Wolfram AppID or send user queries to Wolfram.
3. **Initial slice:** validate `input`, `output`, and supported `format` names with stable query-level errors. Parse, scan, cumulative format, per-pod format, and total time controls are accepted; documented `timing`, `parsetiming`, and `parsetimedout` fields are returned. Active rendering is not preempted.
4. **Initial slice:** version the query envelope and pod/subpod model; preserve typed assumptions and symbolic evidence as additive fields.
5. **Initial slice:** XML and JSON are rendered from the same response model. Unknown future result elements are not yet consumed from external providers because no WA provider is integrated.

### P1 — query control and inspectable interpretation

1. **Done for current pods:** implement the pod filters over Input and Result.
2. **Done for symbolic expressions:** `/v2/validatequery` discovers parse validity and candidate interpretations without simplification.
3. **Done for supported math input:** add `ignorecase=true` to `/v2/query` and `/v2/validatequery`. **Done (initial slice):** bounded deterministic natural-language templates route evaluate, simplify, factor, expand, differentiate, integrate, solve, limit, series, integer-bounded finite sum/product, and plot instructions; `/v2/validatequery` exposes the same proposed plan. **Current data intent slice:** `population of <entity>` and `capital of <entity>` search Wikidata and return up to three ranked entity candidates with P1082 or P36 statements; they do not claim English-language fidelity or choose among ambiguous entities. These are retrieved facts, not Lean proofs. P37 official-language and P38 currency intents are implemented in the current slice; they preserve ranked candidates, claim rank, qualifiers, and references where available. Keep the same source-data-not-proof boundary. Broaden afterward to other data intents, compositional questions, and ambiguity-aware candidate ranking; translation and reinterpretation still need suitable local language and scanner models.
4. **Initial slice:** `CoordinateSystem` alternatives select Cartesian3D or Cylindrical3D for bounded polynomial `div(f1,f2,f3)` inputs. The divergence result is still unverified by Lean. Extend to other coordinate systems and vector-calculus operators only with checked chart formulas; add other data/entity and formula assumptions only when a deterministic validator and source-backed meaning are available.
5. **Initial slice:** return `datatypes`, `scanner`, assumptions, source citations, warnings, unsupported-query tips, and query-level errors from the same result planner; localized pod errors and more info element families remain.
6. **Initial slice:** explicit selection works for `AngleUnit`, parenthesized `LogBase`, and unparenthesized `Function` log syntax in one formula; query-wide choices apply to every matching call, while `AngleUnit`, `LogBase`, `Function`, and `Unit` accept one-based per-occurrence IDs such as `angle:degrees@1`, `log:base-10@1`, `function:log-base-10@1`, and `unit:m-minutes@2`. `Function` and `LogBase` remain independent in mixed-syntax formulas. Unselected occurrences retain their service defaults, and query-wide/per-occurrence choices for the same type cannot be mixed. `Unit` selection covers standalone and compound numeric `m` tokens with query-wide or per-occurrence choices; explicit aliases from the curated physical-unit vocabulary also work in compound formulas and are dimension-checked; `DateOrder` selection covers valid ambiguous slash-separated numeric dates; `MixedFraction` selection covers a positive whole number followed by a proper fraction; `ListOrTimes` selection covers a numeric literal and spaced single-letter symbol; `ListOrNumber` selection covers a single thousands-grouped numeric comma; `NumberBase` selection covers standalone binary-looking integer strings; `I` selection distinguishes lowercase `i` as the imaginary unit or a variable; `TimeAMOrPM` selects AM or PM for ambiguous clock inputs. Unknown, repeated, conflicting, or non-applicable selections are rejected. Validator rejections preserve the selected assumption records and typed interpretation. Other assumption families remain.
7. Broaden natural-language parsing beyond the deterministic symbolic templates; generate candidate meanings for data and mixed-domain questions, then rank only validator-approved candidates and return clarification when meaning remains ambiguous.

### P2 — presentation, locality, and execution control

1. Extend the bounded presentation-LaTeX and MathML parsers and broaden structural formula rendering. The Full Results `cell` format has an initial text-backed Wolfram Cell wrapper; rich cell rendering remains future work. Open-Meteo seven-day forecasts now have a local chart; other provider charts/maps, image maps, and audio remain future work where a local backend can produce them safely.
2. Add location and preferred units as request context. Make IP-derived location opt-in and avoid retaining the caller's IP.
3. **Initial slice:** return and accept `DecimalApproximation__More digits` / `Fewer digits` state inputs, including repeated values and `n@` shorthand. Extend states to other mathematical and data pods, subpod states, and stateful paging.
4. **Initial slice:** `parsetimeout`, `scantimeout`, per-pod `podtimeout`, cumulative `formattimeout`, `totaltimeout`, asynchronous primary-pod and complete-query polling, and a bounded recalculation URL are implemented. Both async URLs share one computation; the query URL returns all final pods together. Formatting budgets omit pods whose bounded render operation completes over budget; they do not interrupt a render in progress. Recalculation reruns the single result request with a larger budget and does not preserve partial solver state. Still needed: interruptible formatting, progressive per-pod delivery, and partial scanner results. Internal per-stage instrumentation may be useful operationally, but separate scan/format durations are not defined by the referenced response contract.
5. **Done (initial slice):** add `width` for text-oriented SVGs and `maxwidth` for structural mathematical SVGs, each with a bounded 100–1,600 pixel range and a 500 pixel default; `mag` scales structural math content from 0.5 to 2.0 with a 1.0 default. Plots and graphics remain controlled by `plotwidth`; sizing controls for additional renderer families remain.

### P3 — broaden independently sourced coverage

Build coverage around licensed and attributable providers, measuring query-class recall and source freshness. The initial weather, CODATA, Wikidata entity, country (P17), continent (P30), population, capital, official-language and currency relationships, coordinate location, elevation and area measurements, nominal and PPP GDP, GDP per capita, GeoNames place, and DBnomics routes now appear as Full Results-style pods. The P17/P30 routes resolve up to three ranked Wikidata entity candidates and preserve non-deprecated relationship statements, references, ranks, and time qualifiers; continent responses may contain multiple continent statements, and P17 is not used for people. DBnomics dataset search uses `series search: <phrase>` or `dbnomics search: <phrase>` and returns candidate datasets. After selecting a dataset, `series search: <phrase> within <provider>/<dataset>` returns five series candidates per page; `seriesoffset` selects a page up to offset 10,000. `/v1/knowledge/query` exposes the same pagination as `offset`. Callers select a series code before requesting observations. Allowlisted P17/P30/P37/P38 queries retain entity ranking and claim references; P625 coordinates retain validated latitude/longitude, globe, precision, rank, references, and bounded qualifiers; P2044/P2046 quantity results preserve units, bounds, rank, reference counts, and bounded qualifier details including point-in-time values. Broaden other entity properties, series-level filtering by dimensions, geography, units, and weather controls. Do not describe provider coverage as full Wolfram|Alpha parity; preserve provider, license/attribution, retrieval time, and whether the value is computed, retrieved, solver-derived, or kernel-checked.

### Current GDP query slice

`/v1/knowledge/query` accepts Wikidata quantity properties [P2131](https://www.wikidata.org/wiki/Property:P2131)
(nominal GDP), [P4010](https://www.wikidata.org/wiki/Property:P4010) (GDP at purchasing
power parity), [P2132](https://www.wikidata.org/wiki/Property:P2132) (nominal GDP per capita),
and [P2299](https://www.wikidata.org/wiki/Property:P2299) (PPP GDP per capita).
`/v2/query` recognizes explicit phrases such as `nominal GDP of France`, `GDP (PPP) of
France`, and `nominal GDP per capita of France`. A generic query such as `GDP of France`
returns versioned `GDPType` alternatives; `GDP per capita of France` returns separate
`GDPPerCapitaType` alternatives. Callers select with `assumption=gdp:type:nominal`,
`assumption=gdp:type:ppp`, `assumption=gdp-per-capita:type:nominal`, or
`assumption=gdp-per-capita:type:ppp`. The service does not select a measure from proof
success. Values keep their currency/unit, reported bounds, rank, references, and point-in-time
data; they remain retrieved facts rather than Lean proofs. Broader economic indicator
discovery is not included in this slice.

### Numeric evaluation slice

`/v2/query` accepts `N[expression]` and `N[expression, precision]` for finite numeric
expressions without free symbols in the bounded parser. The default is 16 decimal digits;
explicit integer precision is limited to 1–500. Wolfram documents `N[expr]` as machine
precision and `N[expr,n]` as an attempt at n-digit precision ([official `N` reference](https://reference.wolfram.com/language/ref/N.html)). The service uses SymPy and returns the approximation as the primary Result by default. With `verify=true`, the supported certified numeric fragment returns a Lean-proved absolute-error bound and displays that certified value; unsupported functions remain unverified. Decimal literals keep the service's exact base-10 interpretation, so approximate-input precision propagation is not reproduced.

## Certified numeric evaluation

The CAG endpoint accepts Wolfram-style `N[expression]` and `N[expression, precision]` for exact rational expressions, square roots of nonnegative rationals, exact `Sin`/`Cos`/`Tan` results that reduce to rational linear combinations or positive-denominator quotients of supported nested square roots, and bounded expressions involving `Pi`, including square roots, sign-definite interval products, integer powers up to 16 with sign-definite bases, and quotients with denominator intervals strictly above or below zero. Product factors with intervals crossing zero and nonzero powers of bases whose intervals cross zero are rejected; so are quotients whose denominator intervals cross zero. The intervals may include wholly negative values and are accepted when Mathlib's bounds prove the requested error. Rational and supported radical results allow up to 500 significant digits. Affine `Pi` results support up to 100 significant digits; other interval-evaluated `Pi` expressions are capped at 100 and accepted when their generated rational interval proves the requested error. Some high-precision requests near zero or with large coefficients are rejected when the interval is too wide. The default is 16 digits. The result includes `evidence.numeric_approximation.value`, the requested significant digits, and a rational absolute-error bound. For `Pi` expressions, Lean uses built-in Mathlib bounds through 20 digits and generated rational bounds proved with `pi_lower_bound` and `pi_upper_bound` above 20 digits; for rational expressions it proves the bound directly with `norm_num`; for square-root expressions it proves rational lower and upper bounds recursively around `Real.sqrt`. The trig numeric path composes the exact Lean identity with those interval proofs. Other algebraic and transcendental inputs remain unsupported by this certified CAG path. `/v2/query` numeric evaluation continues to cover bounded finite numeric expressions through SymPy and does not claim a Lean proof.

## Acceptance checklist for a parity release

- The `/v2/query` route has a stable, documented request contract and does not confuse our credentials with Wolfram credentials.
- `GET /v2/validatequery` can report parse/interpretation candidates independently of result computation.
- JSON and XML preserve additive query fields; XML uses JSON-encoded extension elements when an unknown field has no documented element mapping. Focused tests cover query, pod, subpod, and assumption extensions; result-family and parameter coverage remains to be expanded.
- Pod IDs are stable within a query contract; filtering supports repeatable include/exclude and wildcard title options.
- Assumption proposals list typed, versioned service IDs; query-wide and per-occurrence caller overrides are validated before backend routing and never chosen from proof outcomes.
- Decimal approximation state inputs are returned with the pod, can be resubmitted through `podstate`, and produce the requested precision within the documented cap.
- Every pod reports its source and evidence kind; warnings, errors, and timeouts are distinguishable.
- Timeouts, async jobs, and pod states have documented resource limits and deterministic follow-up behavior.
- Compatibility tests cover request validation, all supported query parameters, response schemas, assumption round-trips, and error cases.
- The product documentation distinguishes protocol compatibility from data/answer coverage.

## Source notes

The audit is based on the downloaded API 2.0 Revision A page, including its parameter reference, result element catalog, pod formatting, location, assumption, timeout/async, error, and `validatequery` sections. The page states that queries are GET requests, supports XML/JSON result envelopes, and cautions clients to tolerate future elements and attributes. See the [official API reference](https://products.wolframalpha.com/api/documentation) and its [assumption section](https://products.wolframalpha.com/api/documentation?scrollTo=using-assumptions). The DBnomics adapter follows its [Web API documentation](https://docs.db.nomics.world/web-api/) and dataset page documentation for [full-text series search within a dataset](https://docs.db.nomics.world/website/).
