# Case Study: Automating Fort Worth Highway Segment Limits

This project is a good example of the kind of work I want to keep doing: taking a messy real-world mapping problem, improving the observability of the problem first, and then building a human-in-the-loop automation workflow that gets more reliable over time instead of becoming more brittle.

## The problem I was solving

I was working with a layer of Fort Worth highway segments, and the task was to identify the `Limits From` and `Limits To` values for each segment. In plain terms, I needed to answer a deceptively simple question:

"What exactly defines the start and end of this highway segment?"

At first glance, this sounds like a manual GIS task. A person can zoom into a segment endpoint, look at the nearby roads, and decide whether that endpoint is best described by:

- a county line
- a crossing route
- a frontage road
- an interchange
- a named local road
- an offset from a nearby marker, such as "North of US 67"

The manual approach works for a while, but it does not scale well, and it is also inconsistent if the map does not expose the right road-network information.

## Why the first dashboard was not enough

Before building the automation, I had already developed an ArcGIS dashboard for reviewing the segments:

`https://jacobs.maps.arcgis.com/apps/dashboards/34e1981ba2124c6a918e9810598efe1c`

That dashboard was useful for general review, but it had a major limitation:
Amy could not inspect the road network at a segment endpoint in a way that let
her confidently determine the exact roadway name or highway designation that
should anchor the limit.

We could see route shields and other visual cues, but that was not enough. In many of the hard cases, the real question was not just "what road is near this endpoint?" The real question was "what is the exact designation of that road?" A shield alone can still leave ambiguity:

- is this `SH` or a business route?
- is this the parent highway or the frontage road?
- is this the mainline designation or a concurrent route label?

Without being able to click the road network itself, the reviewer had to infer too much from symbols and map context. That made the review process slower and also made it easier to confuse route family, alias, and actual endpoint anchor.

That mattered because these segment limits were not always clean, literal intersections. Many of the difficult cases involved:

- frontage roads versus the parent highway
- business routes versus mainline routes
- concurrent routes such as `US 81/287`
- county-line endpoints
- interchange-context endpoints
- cases where the best description was offset phrasing rather than a direct road name

So the first blocker was not the heuristics. The first blocker was observability. If the reviewer cannot see the right network context, then the automation effort starts on weak ground.

## The first real step: build a better inspection surface

To fix that, I built a custom web map in `Web-App/`.

I designed it to combine:

- the `FTW_Segmentation_Master` layer
- TxDOT's statewide planning basemap
- a TxDOT roadway layer that could be queried manually by clicking near a
  segment endpoint

That web app ended up being important in two separate ways.

First, it gave Amy a much better manual verification tool. She could search for
a segment, zoom directly to it, inspect both endpoints, and manually click
nearby roads to see what TxDOT considered the roadway name or route designation.

Second, it gave me a stable interface for verification through Playwright MCP.
In this workflow, I used Playwright MCP as the browser automation and inspection
layer, which let me open the map, zoom to endpoints, inspect labels, and capture
evidence in the same app the human reviewer was using. Later, that verification
workflow became intentionally visual-only: the automated pass reads rendered
basemap labels and route shields, but does not click roadway popups or inspect
service data. That let me build a workflow where manual review and automated
verification were looking at the same evidence surface instead of two
disconnected systems.

This was a major shift in the project. I was no longer trying to automate a poorly observable problem. I had created a map experience that exposed the problem clearly enough to support both people and automation.

## The automation philosophy I used

From the beginning, I did not want the browser to become the primary engine. I wanted the main system to stay deterministic and data-driven.

So I built `Scripts/identify_segment_limits.py` as a data-first script. The script pulls from multiple sources:

- segment geometry from `FTW_Segmentation_Master`
- county boundaries from the Texas county layer
- roadway labels from TxDOT vector tiles
- roadway geometry and route identity from the TxDOT roadway inventory

The script's job is to infer segment limits from those sources without needing the browser for every case.

The philosophy was:

- use structured GIS and roadway data as the primary engine
- use the map for verification and arbitration
- avoid treating visual inspection as the default solution
- make every improvement generalizable rather than segment-specific

That distinction mattered. I was not trying to create a browser bot that "looks at the map and guesses." I was trying to build a system that reasons from data, then uses the map only when human-style verification is actually needed.

## How I used Amy's manual input

Amy's role was critical, but I did not use her input as a source of one-off corrections. I used it as structured problem discovery.

She started manually checking segment endpoints in the custom web app and recording what she believed the limits should be. That manual work revealed the real classes of failure in the first automation pass.

Her input showed me that the problem was not just "pick the nearest road." The real logic had to distinguish between different endpoint situations:

- The endpoint is truly at a county line.
- The endpoint is at a crossing route.
- The endpoint touches a frontage road, but the better limit is the mainline route or interchange.
- The endpoint is best described by a named local road rather than the route number alone.
- The endpoint is not exactly at an intersection and is better described as "north of", "south of", "east of", or "west of" a defensible marker.
- The same corridor can legitimately appear under multiple display names or route families.

In other words, Amy's manual review gave me the scenario inventory that the heuristics needed to cover.

I stored that manual work in a CSV and then expanded it into a review sheet that contained:

- Amy's original values
- Playwright MCP verification results
- per-side verdicts
- reviewer notes about why a limit was correct or incorrect

That review sheet became the bridge between manual expertise and automation.

## How I used Playwright MCP to verify instead of blindly trusting either side

One of the most important choices I made was not to treat Amy's first-pass manual review as unquestioned ground truth, and not to treat the script as ground truth either.

Instead, I used Playwright MCP as an adjudication layer.

I used Playwright MCP to inspect the custom web app and verify disputed endpoints. The goal was not to replace the script with browser reasoning. The Playwright MCP workflow was:

- open the segment in the purpose-built web app
- zoom to the endpoint in question
- inspect the nearby TxDOT labels and roadway-click results
- determine whether Amy's wording, the script's wording, or neither one was the better answer
- capture evidence, including screenshots, for difficult cases

This let me separate three kinds of situations:

- cases where Amy was clearly correct
- cases where the script was clearly correct
- cases where both sides needed further interpretation

The most important downstream decision was how I defined a trusted training and evaluation set.

I only treated an endpoint side as trusted when:

- the Playwright MCP review marked that side as correct
- and Amy's value matched the Playwright MCP result

If Amy and Playwright MCP disagreed, or if Playwright MCP marked the side wrong, I did not use that side as a trusted label for heuristic improvement.

This made the process much more conservative, but also much more defensible. I was only learning from reviewed agreement, not from uncertain corrections.

## The different iterations I went through

### Iteration 1: make the problem inspectable

The first meaningful iteration was not in the Python script at all. It was the custom web app.

I had to solve the visibility problem before I could solve the automation problem. The app made endpoint review concrete by allowing segment search, endpoint inspection, zoom-to-selection, and clickable roadway-name queries against TxDOT data.

Without this step, Amy's review would have remained slower and less reliable, and the Playwright MCP verification workflow would have had no stable target.

### Iteration 2: build the first data-driven endpoint detector

Once I had a better way to inspect endpoints, I built the first version of `Scripts/identify_segment_limits.py`.

That version already had the core idea of the system:

- load segment geometry
- orient each segment so `From` and `To` are meaningful
- build endpoint context for both sides
- gather possible candidates from multiple data sources
- choose the best candidate for each endpoint

The important thing here is that I was already thinking in terms of candidate generation and candidate selection, not a single hardcoded rule.

### Iteration 3: compare automation against human review

After the first script existed, the next step was not to keep tweaking blindly. The next step was to compare the script's outputs against Amy's reviewed values at scale.

That comparison made the mismatch patterns visible. I could now see that the failures were not random. They clustered into recurring classes such as:

- county boundary interpretation
- frontage-road versus mainline-route interpretation
- interchange-context naming
- local-road versus route-number naming
- directional wording
- alias or business-route wording
- offset phrasing for mid-segment endpoints

At this stage, the project stopped being "fix a few wrong outputs" and became "identify repeatable mismatch families and build heuristics around them."

### Iteration 4: use Playwright MCP to separate trusted labels from disputed labels

This was the point where the workflow became much more rigorous.

Instead of treating every reviewed row as equal, I used Playwright MCP to distinguish:

- trusted reviewed sides
- disputed sides
- sides that still needed more manual work

That decision mattered because it protected the script from overfitting to incorrect or uncertain labels. It also made it possible to measure progress on a trusted subset rather than on a noisy full sheet.

### Iteration 5: refactor the script so heuristics could evolve cleanly

Once I understood the mismatch families, I refactored the script so the logic was easier to improve systematically.

I separated the workflow into two conceptual stages:

- gather candidates from every source
- select the best candidate using heuristics

That separation was important because it let me ask two different questions:

- "Did I fail because I never found the right candidate?"
- "Or did I find it, but choose the wrong one?"

That is a much better debugging model than having all logic collapsed into one function.

I also added explicit heuristic labels to the output so each endpoint could say not just what the answer was, but why the script believed it. That gave the workflow traceability and made spreadsheet review much easier.

### Iteration 6: improve by heuristic family, not by segment

At this stage, I stopped thinking about "problem rows" and started thinking about "problem patterns."

The major heuristic families I worked through included:

- county-boundary logic
- route-intersection logic
- frontage and service-road disambiguation
- interchange-context logic
- local labeled road versus stronger network anchor
- offset-from-marker formatting
- orientation-based phrasing
- route alias and business-label handling

For each family, I asked:

- Is the right answer already in the candidate pool?
- If so, what general rule should choose it?
- If not, what source is missing or underused?

This let me keep the improvements general. I was not adding rows like "if segment equals X, output Y." I was trying to describe geometry and network behavior in a reusable way.

### Iteration 7: keep the deterministic engine primary, use Playwright MCP for unresolved ambiguity

By the later iterations, the workflow had settled into a clear pattern:

- the script makes a deterministic first pass
- Amy's manual review and Playwright MCP verification identify the hard cases
- I group the remaining mismatches by pattern
- I improve the heuristics where a general rule exists
- I leave truly ambiguous or display-only issues for further verification instead of forcing brittle logic

This last part is important. In several remaining cases, the script was already finding the correct corridor, but it did not have enough source text to emit Amy's exact display wording. In those situations, I did not want to fake confidence with string hacks. That is where Playwright MCP remained useful.

## How I decided what to fix in code

I followed a simple rule: if a mismatch could be improved by a general heuristic, I fixed the heuristic. If it required a case-specific exception or a display string that the candidate pool did not actually contain, I deferred it for verification or richer data.

That meant the code changes were driven by repeatable patterns like:

- the endpoint is near a frontage road, but the crossing route is the real anchor
- the endpoint is mid-segment and should be rendered as an offset from a marker
- the local-road label and roadway-inventory geometry agree, so that local road is more trustworthy
- the endpoint is at a county boundary, and the county line is stronger than a nearby street label

This is the part of the work I care about most. I like building systems where the logic becomes more explainable with each iteration, not less.

## How I measured progress

I did not just inspect a few sample rows and decide the script "looked better." I built an evaluation loop around the trusted subset.

The evaluator measured side-level exact matches on trusted reviewed endpoints. That let me answer:

- how many endpoint sides were trusted
- how many exact matches the script currently achieved
- which sides were still mismatched
- which heuristic families were responsible for those mismatches

I also tracked improvements iteratively rather than narratively. In the committed heuristic lessons, I improved trusted-side exact-match accuracy from `123/268` to `139/268`, and then to `140/268` through additional offset handling. Later local work in my current workspace has pushed that a bit further.

Just as important, I tracked whether a proposed heuristic fixed more cases than it regressed. That is a much healthier improvement loop than simply chasing a few satisfying wins.

## How the script knows its confidence level

One of the design decisions I made early was to give each endpoint candidate a numeric confidence score (0.0–1.0) rather than just picking the nearest road. This was important because "nearest" is not always "correct" — a county line 45 meters away is almost certainly the right answer, while a road label 200 meters away in a dense interchange area might not be.

The confidence score is built from four independent signals:

### 1. Source type

Different data sources have different inherent reliability. A county boundary line is precise geometry — if the endpoint is within 50 meters of one, that's a 0.99 confidence. A road label from the TxDOT vector tile basemap is less precise because label placement depends on rendering, so it starts lower (0.75–0.94 depending on distance).

### 2. Distance from endpoint

Closer candidates score higher. For local road labels, a label within 40 meters gets 0.94 confidence, while one 225 meters away gets 0.80. This is a simple but effective heuristic — it reflects the physical reality that the endpoint is more likely to be defined by a road that's right there than by one that's far away.

### 3. Crossing angle

A road that crosses the segment perpendicularly (+0.08 bonus for angles >= 55°) is a much stronger limit signal than one running nearly parallel (-0.08 penalty for angles <= 12°). This is the signal that helps the script distinguish between a true crossing intersection and a frontage road or service road running alongside the highway.

### 4. Corroborating evidence

When multiple independent sources agree on the same answer, confidence goes up. If TxDOT label text confirms a route intersection (+0.05), or if the roadway inventory geometry confirms which side of the highway the endpoint is on (+0.06 for same-side match), that convergence is meaningful.

All scores are clamped to [0.50, 0.98] — the script never claims absolute certainty and never completely rejects a candidate.

### Why this matters for the hybrid workflow

The confidence score is what makes the hybrid visual verification workflow
possible. When the script reports a high-confidence endpoint (>= 0.90), visual
verification is likely to confirm it. When confidence is low (< 0.78), that's a
signal that visual verification is most likely to add value - those are the
cases where the data sources disagree, the crossing angle is ambiguous, or the
nearest road label is far from the endpoint.

The full confidence scoring details are documented in
[`SEGMENT_LIMITS_LOGIC.md`](SEGMENT_LIMITS_LOGIC.md#confidence-model).

## What this project taught me

This project reinforced a few principles that I want to keep applying in future work.

### 1. Fix observability before fixing automation

The custom web app was not a side tool. It was part of the solution. Once the problem became easy to inspect, both manual review and automation improved.

### 2. Human review is most valuable when it exposes patterns

Amy's manual input mattered because it revealed recurring endpoint scenarios. I used that review to understand the problem space, not just to patch outputs.

### 3. Verification should be conservative

I did not want to "train on disagreements." I only promoted reviewed agreement into the trusted set. That protected the project from false confidence.

### 4. Playwright MCP works best as a verifier and arbitrator

Using Playwright MCP through the custom web app was powerful precisely because I did not ask it to do everything. I used it where visual context mattered and where deterministic logic alone was not enough.

### 5. Good heuristics are explicit and auditable

I wanted each endpoint result to be explainable in terms of heuristic families. That made the system easier to review, improve, and trust.

### 6. General rules are better than clever exceptions

I deliberately avoided hardcoding segment-specific answers from the review sheet. That kept the script aligned with the actual problem instead of turning it into a lookup table disguised as logic.

## Why this is the kind of work I want to do more of

This project sits at the intersection of several things I enjoy:

- spatial reasoning
- messy real-world data
- human-in-the-loop systems
- product thinking about tooling and observability
- heuristic design
- iterative evaluation
- automation that stays grounded in evidence

I like problems where the answer is not just "write a script" or "label data manually." I like building the whole workflow around the problem:

- create the right inspection tool
- structure the human review
- add targeted automation
- use verification carefully
- turn repeated judgment into explicit logic
- keep the system measurable as it improves

That is what this project became for me: not just a script that guesses segment limits, but a full problem-solving pipeline for turning ambiguous map interpretation into a repeatable, auditable automation workflow.

## Iteration 8: MCP-driven ground truth correction and output standardization

This was the most impactful phase of the project. Instead of only improving the script's heuristics, I used Playwright MCP to systematically verify Amy's manual inputs and correct them where the map evidence disagreed.

### The approach

I wrote five targeted MCP verification prompts, each covering a specific mismatch category, and ran them in parallel through Codex with Playwright:

1. **Verify named aliases** (55 cases) — checking whether alias names like "Denton Hwy (US 377)" were actually visible on the basemap
2. **Verify directional prefixes** (19 cases) — checking whether labels like "W Highway 67" appeared on the map
3. **Verify offset phrasing** (17 cases) — checking whether endpoints were at intersections or between them
4. **Verify different roads** (18 cases) — checking which road was actually at disputed endpoints
5. **Verify minor wording** (11 cases) — checking directional suffixes, concurrent routes, and alias inclusion

Each prompt instructed the agent to visit the web app, zoom to the endpoint, read the basemap labels, and report a verdict with evidence. The results were appended directly to the prompt files.

### Key findings from MCP verification

**Most of Amy's aliases were invisible on the basemap.** In 54 out of 55 alias cases, the basemap only showed route shields and numbers — not the named-road alias Amy had included. Amy was drawing from local knowledge or road signs not rendered on the TxDOT basemap. The correct standardized value was the bare route name.

**Directional prefixes were real but inconsistent.** The TxDOT basemap does show labels like "W Highway 67" and "E State Highway 6", but only for US, SH, and FM routes, and not at every location even within those systems. Since the label tile data only contains bare numbers ("67", "199"), the directional text cannot be extracted programmatically. I decided to standardize on bare route names.

**Several route system corrections were needed.** Amy wrote "SH 180" in multiple places where the map actually shows "US 180". She also wrote "US 287" where the endpoint was specifically at "BU 287P" (the business route designation).

**The offset phrasing was almost always correct.** MCP confirmed that all 17 offset cases were better described by Amy's phrasing ("North of US 67") than by the script's nearby-road pick ("Left Frontage IH 35"). These represent endpoints between intersections on freeway corridors.

### Ground truth corrections applied

From the MCP results, I applied corrections to `FTW-Segments-Limits-Amy.review.csv`:

- **38 alias removals** — stripped named aliases not visible on basemap (e.g., "Denton Hwy (US 377)" → "US 377")
- **18 directional prefix removals** — standardized to bare route names (e.g., "W US 67" → "US 67")
- **7 route system corrections** — SH 180 → US 180, US 287 → BU 287P
- **9 minor wording fixes** — FM 730 N → FM 730, US 380 W → US 380, etc.
- **3 county line corrections** — endpoints that were not at county lines
- **3 different-road corrections** — Amy picked wrong road at endpoint

### Output standardization in the script

Alongside the CSV corrections, I added output formatting rules to `abbreviate_output_value()`:

- **County Road → CR** — abbreviate at the output stage so internal matching still works
- **Direction abbreviation** — North → N, South → S, East → E, West → W, Southwest → SW, etc.
- **Protected road names** — "South Fwy", "Northwest Pkwy" kept intact (direction is part of the name)
- **County line direction stripping** — bare "X County Line" with no directional prefix
- **County line offset format** — endpoints 50-100m from boundary rendered as "85m N of Tarrant County Line" with distance and direction, competing against road candidates

### Other script improvements in this phase

- **Tightened alias label distance** from 450m to 150m — prevented distant alias labels (e.g., "South Fwy" 200m away) from being incorrectly attached to endpoints
- **County boundary offset detection** — new 50-100m range with lower confidence (0.85) so offset county candidates compete against route/local candidates rather than automatically winning

### Accuracy progression

| Milestone | All-sides (300) | Trusted |
|-----------|----------------|---------|
| Codex heuristic baseline | — | 140/268 (52.24%) |
| After MCP alias + wording fixes | — | 151/267 (56.55%) |
| After all MCP corrections + output standardization | 217/300 (72.33%) | 216/267 (80.90%) |
| After county offset detection | 221/300 (73.67%) | — |
| After dropping directional prefixes from Amy | 239/300 (79.67%) | — |
| After frontage heuristic (Codex) | 247/300 (82.33%) | 239/267 (89.51%) |
| After visual-only re-verification (37 cases) | 254/300 (84.67%) | 236/261 (90.42%) |

The jump from 52% to 80% came primarily from correcting Amy's ground truth to match what the basemap actually shows, not from heuristic improvements. This reinforced a key lesson: ground truth quality matters more than model sophistication.

### Iteration 9: Visual-only re-verification (2026-04-06)

A critical lesson emerged when reviewing the first round of Codex MCP verification results. Codex had been clicking on TxDOT roadway lines to read popup data — the same service data the script already uses. For FM 1189, the TxDOT API returned `FM0004-KG` at 0.0m, but the basemap clearly shows "US Highway 281" at that location. Codex reported the script-favorable answer because it was reading the script's own data source rather than the visual map.

I rewrote the verification prompt with a **visual-only constraint**: no clicking roadway popups, no reading CSV/JSON data, no using API responses. Only visible basemap labels and route shields count as evidence. I also included three values per case (Original Amy, Current Amy, Script) instead of two, so the agent could recommend reverting changes if warranted.

Results from the re-verification of 37 disputed cases:
- **8 cases**: Amy corrected to match script (SH 254, FM 1189, FM 51, SH 199, SH 254, Tyra Ln, TL 360, FM 51)
- **3 cases**: Previous corrections reverted to original Amy (IH 35W not "N South Fwy", Boyd Rd not Speer St)
- **4 cases**: Disputed endpoints resolved with new values (Northwest Pkwy, SH 114, Grove St, Tarrant County Line)
- **1 case**: Offset description corrected (E of Holiday Hills Dr)
- **19 cases**: Current Amy confirmed correct, no change needed

### Remaining mismatches (46 all-sides, 25 trusted as of 2026-04-06)

| Category | All-sides | Trusted | Fixable? |
|----------|-----------|---------|----------|
| offset_missing | 22 | 17 | Needs new heuristic for mid-segment endpoints on freeway corridors |
| different_road | 13 | 3 | 3 trusted are real script limitations (frontage side, combined streets, interchange) |
| alias_missing | 7 | 4 | Alias names not in TxDOT tile data — needs different data source |
| alias_extra | 2 | 1 | Script adds alias Amy doesn't use — minor formatting |
| offset_different | 2 | 0 | — |

The remaining offset_missing cases (mostly IH 35W corridor) require a new heuristic for freeway mid-segment endpoints where the gold road is not in the candidate pool at the endpoint location.

## Current pipeline state (2026-04)

The project is no longer just a script plus ad hoc verification. It now has a
formal two-pass pipeline:

1. The heuristic pass runs
   [`Scripts/identify_segment_limits.py`](Scripts/identify_segment_limits.py)
   through
   [`Scripts/generate_visual_review_manifest.py`](Scripts/generate_visual_review_manifest.py)
   and emits endpoint-level heuristic rows with coordinates, heuristic labels,
   confidence scores, and gap-piece detail.
2. The manifest is turned into batch prompts by
   [`Scripts/generate_visual_review_prompts.py`](Scripts/generate_visual_review_prompts.py).
3. Independent Visual Review Agents inspect only the rendered map and write
   structured JSON results for each endpoint.
4. [`Scripts/reconcile_results.py`](Scripts/reconcile_results.py) merges the
   heuristic and visual passes into:
   - `_temp/visual-review/final-segment-limits.csv`
   - `_temp/visual-review/final-segment-limits-collapsed.csv`
5. [orchestrator.md](orchestrator.md) defines the end-to-end workflow, including
   resumability, verification logging, optional dashboard generation, and
   optional human-reviewed adjudication.

That matters because the project has shifted from "heuristic script with manual
spot checks" to "deterministic first pass plus independent visual arbitration."
The anti-bias constraint is architectural now: Visual Review Agents do not see
the heuristic answers before producing their own result.

## What this phase taught me

### 1. Ground truth correction is higher-leverage than heuristic tuning

The single biggest accuracy gain came from fixing Amy's inputs, not from improving the script. When I corrected aliases, route systems, and wording to match what the basemap actually shows, accuracy jumped from 52% to 80%. The script was already finding the right roads — Amy's notation was just inconsistent with the data sources.

### 2. Parallel MCP verification scales well

Running five Codex agents in parallel with targeted prompts let me verify 120 endpoints in one batch. Each prompt was scoped to a specific mismatch pattern, which made the verification results actionable and the agent's task well-defined.

### 3. Standardization decisions compound

Small decisions like "always use bare route names" and "abbreviate directions" eliminated entire mismatch categories. The 18 directional prefix cases disappeared instantly when I decided the data sources could not reliably support them.

### 4. The rendered map is not the same as the data

The directional prefix investigation confirmed that what a human sees on the basemap ("W Highway 67") is not what the vector tile data contains ("67"). This is a fundamental constraint that should be verified early before designing heuristics around visual rendering.

### 5. MCP works best as a batch verifier, not an oracle

The most effective use of Playwright MCP was not "go look at the map and tell me the answer." It was "here are 55 specific cases where Amy and the script disagree — check each one and tell me who is right." That structured approach produced clear, actionable corrections.

### 6. Verify against the rendered map, not the service data

When using MCP agents to verify map endpoints, the agent must be constrained to read **visual basemap labels only** — not click roadway popups or read API data. Roadway popups return TxDOT inventory data, which is the same source the script already uses. An agent that clicks popups to verify the script is just rubber-stamping the script's own data source. The FM 1189 case proved this: the TxDOT API said FM 4 at 0.0m, but the basemap clearly shows US Highway 281. Visual-only verification caught errors that service-data verification missed.

### 7. Three-value comparison reduces verification bias

The re-verification prompt showed three values per case (Original Amy, Current Amy, Script) instead of two. This let the agent recommend reverting previous corrections when warranted — which happened in 3 cases where the first MCP run had incorrectly changed Amy's values. Showing only two options (Amy vs Script) creates a false binary that biases toward one side.
