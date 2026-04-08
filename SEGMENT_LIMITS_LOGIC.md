# Segment Limit Detection Logic

This document explains how [Scripts/identify_segment_limits.py](Scripts/identify_segment_limits.py)
identifies `Limits From` and `Limits To` for FTW segments, what data sources it
uses, how its heuristics choose between candidate endpoints, and how the
heuristic outputs now feed the hybrid visual verification pipeline.

## Purpose

The main script is designed to infer segment limits from ArcGIS and TxDOT data
rather than from manual inspection alone. It can:

- generate auto-detected limits for requested FTW segments
- compare those auto-detected limits against the review CSV
- write review-oriented CSV outputs with heuristic labels, endpoint coordinates,
  confidence scores, and notes
- expose structured gap-segment endpoint detail for downstream visual review

The goal is not just to find a nearby road. The goal is to identify the actual
roadway or county boundary that forms the segment endpoint.

## High-Level Flow

For each FTW segment row:

1. Load the segment geometry from the FTW segmentation layer.
2. Resolve the row to one or more FTW segment features.
3. Orient the segment so it has meaningful `From` and `To` sides.
4. Split true gap geometries into separate pieces when needed.
5. Build endpoint geometry for each side or piece endpoint.
6. Gather candidate limits from:
   - county boundaries
   - FTW route intersections
   - TxDOT road-network labels from the basemap tiles
   - fallback TxDOT roadway label tiles
   - TxDOT Roadway Inventory geometry
7. Run heuristic selection on the gathered candidate set.
8. Emit a `RowProcessingResult` with:
   - overall `auto_from` / `auto_to`
   - `heuristic_from` / `heuristic_to`
   - endpoint coordinates
   - endpoint confidence scores
   - optional structured `gap_piece_endpoints`
9. Write CSV outputs for either:
   - direct heuristic review through `verify_limits()`, or
   - the hybrid visual workflow through
     [Scripts/generate_visual_review_manifest.py](Scripts/generate_visual_review_manifest.py)

The top-level orchestration for heuristic inference happens in
`verify_limits()` inside [Scripts/identify_segment_limits.py](Scripts/identify_segment_limits.py).
Candidate gathering happens in `gather_candidates()`, and the endpoint decision
happens in `select_limit()`.

## Current Heuristic Outputs

When [Scripts/identify_segment_limits.py](Scripts/identify_segment_limits.py) is
run directly, the main review CSV now includes:

- `Auto Limits From`
- `Auto Limits To`
- `Heuristic-From`
- `Heuristic-To`
- `From Endpoint Lon`
- `From Endpoint Lat`
- `To Endpoint Lon`
- `To Endpoint Lat`
- `Confidence-From`
- `Confidence-To`
- `Confidence-Bucket-From`
- `Confidence-Bucket-To`
- `Gap Piece Endpoints`
- `Segment-Direction`
- `Segment-Type`
- `Auto Review Notes`

Those fields are no longer just internal state. They are part of the emitted
output and are used by the visual verification pipeline.

## Hybrid Visual Verification Outputs

The heuristic engine now feeds a larger pipeline:

- [Scripts/generate_visual_review_manifest.py](Scripts/generate_visual_review_manifest.py)
  writes:
  - `_temp/visual-review/heuristic-results.csv`
  - `_temp/visual-review/visual-review-manifest.json`
- [Scripts/generate_visual_review_prompts.py](Scripts/generate_visual_review_prompts.py)
  turns the manifest into batched Visual Review Agent prompts
- Visual Review Agents write `_temp/visual-review/batch-results/batch-NN-results.json`
- [Scripts/reconcile_results.py](Scripts/reconcile_results.py) merges heuristic
  and visual outputs into:
  - `_temp/visual-review/final-segment-limits.csv`
  - `_temp/visual-review/final-segment-limits-collapsed.csv`
- [orchestrator.md](orchestrator.md) describes the full multi-agent workflow

This means the heuristic engine is now both:

- a standalone deterministic endpoint detector
- the first pass in a two-pass heuristic + visual verification system

## Data Sources

The script uses four primary sources.

### 1. FTW Segmentation Master

Source:
- `FTW_Segmentation_Master`

Used for:
- FTW segment geometry
- segment IDs and readable segment names
- route family grouping

Relevant functions:
- `load_segment_features()`
- `resolve_row_features()`

### 2. Texas County Boundaries

Source:
- Texas county boundary ArcGIS feature layer

Used for:
- detecting county-line limits such as `Tarrant County Line`
- detecting near-boundary county offset cases

Relevant functions:
- `load_counties()`
- `infer_county_limit()`

### 3. TxDOT Road-Network Label Tiles

Primary source:
- `TxDOT_Vector_Tile_Basemap`

Fallback source:
- `TxDOT_Roadways_and_Shields_2`

Used for:
- displayed roadway names from the TxDOT map
- exact specific labels such as:
  - `Left Frontage US 81`
  - `Right Frontage US 81`
  - `Morris Dido Newark Rd`
  - `County Road 2745`
- alias labels shown visually alongside route numbers

Relevant functions:
- `fetch_tile_labels()`
- `nearby_labels()`
- `infer_local_label_limit()`
- `find_route_alias_label()`

### 4. TxDOT Roadway Inventory

Source:
- `TxDOT_Roadway_Inventory`

Used for:
- confirming route geometry and route identity
- finding route candidates such as `IH 20`, `US 287`, `BU 81E`
- finding local street geometry when available
- selecting the correct side-specific frontage road when labels are ambiguous

Important limitation:
- the inventory layer is strong for corridor identity, but it can be less
  specific than the TxDOT label layer when the true limit is a frontage road or
  similarly named local variant

Relevant functions:
- `fetch_roadway_inventory_features()`
- `infer_inventory_route_limit()`
- `infer_inventory_local_limit()`

## Segment Orientation

Before identifying `From` and `To`, the script orients each segment line.

This is handled in `orient_feature_sequence()`.

### Current orientation convention

The direction heuristic is:

- mostly horizontal segments: west to east
- mostly vertical segments: north to south
- near-diagonal segments: treated as north to south

That decision is made in `cardinal_start_should_be_reversed()`.

### Why orientation matters

Once the segment is oriented:

- the start endpoint becomes the `From` side
- the end endpoint becomes the `To` side

If the orientation is different, the same physical endpoints may swap between
`From` and `To`.

## Gap Segment Handling

Gap segments are now first-class outputs.

### How gaps are identified

The script splits a geometry into separate pieces when there is a real physical
gap between disconnected parts. Small artifact fragments are filtered out before
piece evaluation.

Relevant helpers:
- `split_gap_pieces()`
- `_merge_connected_parts()`
- `_chain_parts_into_line()`

### How gaps are evaluated

Each piece is oriented independently. For each piece, the script identifies:

- `from_wgs84`
- `to_wgs84`
- `from_limit`
- `to_limit`
- `from_confidence`
- `to_confidence`
- `from_heuristic`
- `to_heuristic`

Those values are stored in the structured `gap_piece_endpoints` list on
`RowProcessingResult`.

### Overall From/To convention

For gap segments:

- overall `auto_from` = first piece's `From`
- overall `auto_to` = last piece's `To`

That same convention is used downstream by:

- [Scripts/generate_visual_review_manifest.py](Scripts/generate_visual_review_manifest.py)
- [Scripts/reconcile_results.py](Scripts/reconcile_results.py)

### Piece indexing

Piece indexing is 1-based everywhere:

- Python structures
- heuristic-results CSVs
- manifest JSON
- visual batch JSON
- final reconciled outputs

## Geometry Built for Each Endpoint

For each side of the segment, the script computes three geometric inputs.

### 1. Endpoint point

This is the actual endpoint coordinate of the segment line.

Used for:
- measuring distance to nearby roads and labels
- deciding what the segment is touching
- writing downstream endpoint coordinates for visual review

### 2. Interior sample point

This is a point a short distance inside the segment from the endpoint.

Used mainly for:
- county-line detection
- distinguishing boundary cases from merely nearby county lines

Computed by:
- `point_along_line()`

### 3. Local angle

This is the local direction of the segment near the endpoint.

Used for:
- comparing the segment direction to nearby route or label geometry
- favoring true crossing roads over parallel roads

Computed by:
- `line_angle_deg()`
- `local_line_angle_for_point()`

## Candidate Types

The script produces `LimitCandidate` objects. Each candidate includes:

- `value`
- `normalized`
- `method`
- `confidence`
- `distance_m`
- `detail`
- `heuristic`

The main methods are:

- `county_boundary`
- `county_boundary_offset`
- `route_intersection`
- `basemap_label`
- `fallback_label`
- `txdot_inventory_route`
- `txdot_inventory_local`

## County Boundary Logic

County lines are checked early because they are high-confidence and easy to
validate geometrically.

Logic in `infer_county_limit()`:

1. Find the county containing the interior sample point.
2. Measure the distance from the endpoint to that county boundary.
3. If the endpoint is close enough to the boundary, create a county-line
   candidate.
4. If the endpoint is not directly on the boundary but is within the configured
   county offset range, create a lower-confidence county offset candidate.

This distinguishes:

- endpoints that truly stop at the county line
- endpoints that are merely near the boundary

## Route Intersection Logic

Route candidates come from nearby FTW route geometries and are evaluated in
`infer_route_limit()`.

### Route-candidate filtering

A nearby FTW route is considered if:

- it is not the current segment
- it is not the same route family as the current segment
- it is within the configured route search radius
- it is either close enough geometrically or confirmed by nearby labels

### Route-candidate scoring

Confidence is influenced by:

- distance
- crossing angle
- label confirmation
- route-system priority

This favors:

- true crossing routes
- strongly confirmed route intersections

### Route aliases

The script can preserve a route alias shown by the basemap.

Example:
- `South Fwy (IH 35W)`

This happens through `find_route_alias_label()` and `format_named_route()`.

Frontage and service-road labels are not allowed to rename a route candidate
without stronger evidence.

## Local and Specific Label Logic

Specific roadway names from the TxDOT road-network label layer are evaluated in
`infer_local_label_limit()`.

### What counts as a local or specific label

Examples:

- `Morris Dido Newark Rd`
- `County Road 2745`
- `Left Frontage US 81`
- `Right Frontage US 81`

### What gets filtered out

The script discards labels that are likely clutter or unusable markers, such as:

- `Supplemental`
- `Main Lane`
- `Auxiliary Lane`
- bare route-number-only labels like `81` or `287`

This is controlled by `should_skip_local_label()`.

### Why specific labels matter

If the actual endpoint connects to a specific named roadway variant, the script
tries to keep that exact name instead of collapsing it to the parent route.

Examples:

- use `Business US 81` instead of `US 81`
- use `Spur 580` instead of `US 81` when that is the actual intersecting route

### How left/right frontage variants are chosen

For side-specific frontage or service-road labels, the script does not rely only
on the nearest vector-tile label fragment.

Instead it:

- reads the `Left` or `Right` hint in the TxDOT label text
- finds nearby inventory features in the same route corridor
- compares the endpoint to the matching inventory roadbed side
- keeps the TxDOT label text, but uses the inventory geometry to choose the
  correct side-specific label

## Inventory Layer Logic

The inventory layer is used as confirmation, not as the only source of truth.

### Route inventory candidates

Handled in `infer_inventory_route_limit()`.

These are useful when:

- the endpoint clearly touches a route corridor
- the label layer is sparse or missing
- a route confirmation is needed

### Local inventory candidates

Handled in `infer_inventory_local_limit()`.

These help when:

- the road-network labels are weak
- the inventory has a useful nearby street geometry and name

## Candidate Selection Logic

The final endpoint decision is made through:

1. `gather_candidates()`
2. `select_limit()`
3. post-formatting for offsets, aliases, and output normalization

### Main decision rules

The chooser compares county, route, local-label, local-inventory, and
interchange candidates together rather than treating any single source as ground
truth.

High-level rules:

- keep county-line candidates when the endpoint is genuinely boundary-anchored
- let explicit route-variant labels win when they clearly describe the crossing
  route
- prefer the mainline crossing route over a same-corridor frontage label when
  the frontage label is only interchange context
- allow a confirmed local road to beat a nearby route when label and inventory
  evidence agree
- convert selected markers into offset phrasing such as `North of SH 183` when
  the endpoint is between intersections
- otherwise use the strongest candidate based on confidence, distance, and
  crossing angle

### Heuristic taxonomy

The script emits explicit heuristic labels so each endpoint can be audited by
decision family. The current taxonomy includes:

- `offset_from_marker`
- `county_boundary`
- `route_intersection`
- `interchange_context`
- `frontage_service_road_variant`
- `local_labeled_road`
- `orientation_direction_effect`
- `route_alias_or_business_label`
- `shared_endpoint_with_adjacent_segment`
- `fallback_or_unclear`

Multiple heuristic families may apply to one endpoint. When that happens, they
are joined deterministically with ` | `.

## Confidence Model

Each `LimitCandidate` carries a numeric `confidence` score from 0.0 to 1.0.
Confidence is per-endpoint, not per-segment. Each `From` and `To` side gets its
own score based on the winning candidate.

The score is built from four signals: source type, distance, crossing angle, and
corroborating evidence.

### Signal 1: Source type

Each candidate method starts with a different base confidence:

| Method | Condition | Base confidence |
|--------|-----------|-----------------|
| `county_boundary` | Endpoint at county line (<= 50m) | 0.99 |
| `county_boundary_offset` | Endpoint near county line (50-100m) | 0.85 |
| `route_intersection` | Strong match (<= 180m) | 0.92 |
| `route_intersection` | Weak match (> 180m, label-confirmed) | 0.82 |
| `basemap_label` / `fallback_label` | Local road name from TxDOT tiles | 0.75-0.94 |
| `txdot_inventory_route` | Roadway Inventory route | 0.78-0.90 |
| `txdot_inventory_local` | Roadway Inventory local street | 0.70-0.88 |

### Signal 2: Distance from endpoint

Closer candidates score higher.

For local labels:

| Effective distance | Confidence |
|-------------------|------------|
| <= 40m | 0.94 |
| <= 90m | 0.92 |
| <= 150m | 0.86 |
| <= 225m | 0.80 |
| > 225m | 0.75 |

For inventory routes:

| Distance | Confidence |
|----------|------------|
| <= 40m | 0.90 |
| <= 90m | 0.86 |
| <= 150m | 0.82 |
| > 150m | 0.78 |

### Signal 3: Crossing angle

The angle between the segment and the candidate road affects confidence.

For route intersections:

| Angle difference | Adjustment |
|-----------------|------------|
| >= 55 deg | +0.08 |
| >= 35 deg | +0.04 |
| <= 12 deg | -0.08 |

For local labels:

| Angle difference | Adjustment |
|-----------------|------------|
| < 20 deg | -0.08 |

An additional -0.08 penalty applies when a route candidate overlaps the current
route family and the crossing angle is <= 15 deg.

### Signal 4: Corroborating evidence

When independent sources agree, confidence increases:

| Evidence | Adjustment |
|----------|------------|
| Nearby TxDOT label text confirms the route | +0.05 |
| High-priority route system at >= 45 deg | +0.04 |
| Route alias visible on basemap | confidence floor raised to 0.93 |
| Inventory confirms same-side geometry | +0.06 |
| Inventory confirms opposite side only | -0.06 |
| Inventory side match present | confidence floor raised to 0.84 |

### Final clamping

All confidence scores are clamped to the range `[0.50, 0.98]`.

### Confidence buckets

For human-readable reporting, scores map to buckets:

| Bucket | Score range | Interpretation |
|--------|-------------|----------------|
| `high` | >= 0.90 | Strong candidate |
| `medium` | 0.78-0.89 | Reasonable candidate |
| `low` | < 0.78 | Weak candidate, likely worth visual review |

### How confidence is used now

Confidence is now used in three places:

1. Candidate selection inside `select_limit()`
2. Direct heuristic CSV outputs from
   [Scripts/identify_segment_limits.py](Scripts/identify_segment_limits.py)
3. Downstream visual reconciliation

Specifically:

- the direct review CSV exposes `Confidence-From`, `Confidence-To`, and bucketed
  versions
- [Scripts/generate_visual_review_manifest.py](Scripts/generate_visual_review_manifest.py)
  exposes per-endpoint confidence and confidence buckets in
  `_temp/visual-review/heuristic-results.csv`
- [Scripts/reconcile_results.py](Scripts/reconcile_results.py) combines
  heuristic confidence with mapped visual confidence buckets when producing final
  outputs

## Normalization and Comparison

The script normalizes names for comparison while keeping displayed output as
specific as possible.

Relevant helpers:

- `canonical()`
- `normalize_limit_key()`
- `route_number_token()`
- `route_number_parts()`
- `route_overlap()`
- `local_limits_equivalent()`
- `limits_equivalent()`

Important note:

- normalization is used for comparison logic
- displayed output should remain as specific as possible
- reconciliation now reuses these same helpers so the visual pass and heuristic
  pass compare roads with the same rules

## Review Output Logic

When a comparison CSV is supplied, the script writes:

- auto limits
- heuristic labels
- endpoint coordinates
- confidence values and buckets
- review statuses
- auto review notes
- structured gap detail

### Review statuses

Per side and per row, the script classifies results as:

- `matched`
- `needs_review`

### Why a row still needs review

A row stays in `needs_review` if:

- no confident candidate is found
- the auto result differs from the existing CSV
- the result is ambiguous

This is intentional. The heuristic script is designed to reduce manual review,
not to blindly overwrite uncertain cases.

## Hybrid Visual Verification Integration

The hybrid workflow adds a second, visual-only pass on top of the deterministic
heuristic engine.

### Phase 1: Heuristic manifest generation

[Scripts/generate_visual_review_manifest.py](Scripts/generate_visual_review_manifest.py)
runs the heuristic engine and emits:

- `heuristic-results.csv` with one row per endpoint
- `visual-review-manifest.json` with navigation-only context and no answers

For gap segments, the manifest expands to 2N endpoint rows, one for each piece
boundary.

### Phase 2: Visual batch prompts

[Scripts/generate_visual_review_prompts.py](Scripts/generate_visual_review_prompts.py)
turns the manifest into batch prompt files for independent Visual Review Agents.

### Phase 3: Visual-only review

Visual Review Agents inspect only the rendered map:

- no heuristic files
- no CSV or JSON inputs beyond the prompt content
- no roadway popups
- no API or GeoJSON inspection

They produce structured batch JSON with:

- `limit_identification`
- `limit_alias`
- `is_offset`
- `county_boundary_at_endpoint`
- `visual_confidence`
- `visible_labels`
- `visible_shields`

### Phase 4: Reconciliation

[Scripts/reconcile_results.py](Scripts/reconcile_results.py) merges heuristic and
visual outputs into:

- endpoint-level final audit rows
- collapsed segment-level final rows

Resolution states are:

- `confirmed`
- `enriched`
- `visual_preferred`
- `conflict`
- `visual_only`

## Common Failure Modes

The main remaining failure modes are:

1. Orientation mismatches
2. Label-placement ambiguity in dense interchanges
3. Parallel corridor complexity
4. Sparse or inconsistent label coverage
5. Inventory generalization
6. True visual-only cases where the rendered map shows information the data
   sources do not expose cleanly

## Design Principles

The heuristic system is built around these principles.

### Principle 1: pick the actual limit, not just a nearby corridor

The endpoint should resolve to the roadway or county boundary that actually forms
the segment end.

### Principle 2: preserve meaningful route variants

If the endpoint is on a business route, spur, loop, bypass, or similarly
distinct route variant, that exact roadway is more correct than the parent
route.

### Principle 3: use the TxDOT label layer as the naming authority when it is
specific

When the map layer gives a specific roadway name, preserve that exact name in
the output unless stronger evidence contradicts it.

### Principle 4: use inventory geometry as confirmation, not blind override

The inventory layer is strong evidence for corridor identity, but it should not
erase a more specific labeled road that is clearly the actual limit.

### Principle 5: be conservative when unsure

If the script cannot confidently justify a correction, it should leave the row
in `needs_review`.

## Short Team Explanation

If you need a short explanation for teammates, use this:

> The heuristic script finds each segment endpoint, gathers county, route,
> label, and inventory candidates, and selects the most defensible endpoint
> anchor using explicit heuristics and confidence scoring. Those heuristic
> results then feed a separate visual-only verification pass, and the two are
> reconciled into final outputs.

## Related Files

- Main heuristic engine:
  [Scripts/identify_segment_limits.py](Scripts/identify_segment_limits.py)
- Manifest generator:
  [Scripts/generate_visual_review_manifest.py](Scripts/generate_visual_review_manifest.py)
- Batch prompt generator:
  [Scripts/generate_visual_review_prompts.py](Scripts/generate_visual_review_prompts.py)
- Reconciler:
  [Scripts/reconcile_results.py](Scripts/reconcile_results.py)
- Orchestrator prompt:
  [orchestrator.md](orchestrator.md)
- Review CSV:
  [FTW-Segments-Limits-Amy.review.csv](FTW-Segments-Limits-Amy.review.csv)
- Web app used for visual QA:
  [Web-App/README.md](Web-App/README.md)
