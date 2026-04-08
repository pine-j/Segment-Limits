# FTW Segment Limits — Master Plan

> **Runtime document**: `orchestrator.md` (project root) is the authoritative
> execution reference. This plan provides architecture context, design
> rationale, and schema definitions. When running the pipeline, follow
> `orchestrator.md`. When planning changes, read this file first.
>
> **Archived plans**: `Project-Plan/archive/` contains the original
> design-phase documents. Use them only for case study updates, not for
> pipeline execution or modification.

## Problem

Identify the `Limits From` and `Limits To` values for each highway segment
in the Fort Worth transportation network. Each endpoint must be described as
a county line, crossing route, frontage road, interchange, named local road,
or offset from a nearby marker.

## Solution: two-pass verification

1. **Heuristic pass** — Python scripts analyze ArcGIS geometry, TxDOT vector
   tile labels, county boundaries, and roadway inventory data to identify
   endpoint limits with confidence scores.
2. **Visual pass** — pre-captured map screenshots are analyzed by AI agents
   independently (without seeing heuristic results) to produce endpoint
   assessments with confidence buckets.
3. **Reconciliation** — merges both passes, categorizes disagreements,
   produces final CSVs, and appends persistent run learnings.

The key design principle: visual agents must NOT see heuristic answers.
This is enforced **architecturally through agent context isolation** — each
agent receives only screenshot file paths and assessment instructions.

## Segment types

The input CSV contains two kinds of entries:

### Individual segments (suffixed): e.g., "IH 20 - B"
Match exactly one ArcGIS feature. If the geometry has pieces separated by
>=200m (after merging connected parts within 50m), it is reported as a Gap
segment with per-piece limits.

### Corridor segments (unsuffixed): e.g., "SH 360"
No exact ArcGIS match. The heuristic script resolves via route family
fallback — gets all sub-segments (A, B, C), chains them, merges contiguous
ones. If all connected: one Continuous segment with extremity limits. If
gaps remain: reported as Gap with per-piece limits.

### Gap detection thresholds

| Constant | Value | Purpose |
|----------|-------|---------|
| `GAP_MERGE_THRESHOLD_M` | 50m | Merge connected parts closer than this |
| `GAP_THRESHOLD_M` | 200m | Real gap — pieces separated by at least this |
| `MIN_PIECE_LENGTH_M` | 600m | Drop artifact fragments shorter than this |
| `MIN_PIECE_RATIO` | 5% | Drop artifacts smaller than 5% of total length |

## Multi-agent architecture

| Agent | Role | Context |
|-------|------|---------|
| **Orchestrator** | Coordinates all phases, reconciles, reports | Everything |
| **Heuristic Agent** | Runs Python scripts | Scripts, ArcGIS data, input CSV |
| **Visual Analysis Agents** (x N) | Analyze pre-captured screenshots | Screenshot files + batch prompt only |

Anti-bias is enforced by architecture: visual agents literally cannot access
`heuristic-results.csv` because it is not in their context.

## Data flow

```
                Orchestrator
                (sees everything)
                     |
              Phase 0: cleanup
                     |
              Phase 1: heuristic
              python Scripts/generate_visual_review_manifest.py
                     |
          +----------+----------+
          |                     |
  heuristic-results.csv   manifest.json
  (PRIVATE)               (SHARED)
                     |
              Phase 2: generate prompts
              python Scripts/generate_visual_review_prompts.py
                     |
              Phase 3a: capture screenshots
              python batch-screenshots.py
                     |
              Phase 3b: dispatch visual analysis agents
              (waves of 3-5, with rescan loop)
                     |
          +----------+----------+
          |          |          |
    batch-01-  batch-02-  batch-NN-
    results    results    results
          |          |          |
          +----------+----------+
                     |
              Phase 3c: spot-check
              (orchestrator reviews disagreements)
                     |
              Phase 4: reconcile
              python Scripts/reconcile_results.py
                     |
          +----------+----------+
          |                     |
  final-segment-        final-segment-
  limits.csv            limits-collapsed.csv
  (endpoint-level)      (per-segment)
                     |
              Phase 5: report + verification log
                     |
              Phases 6-8: optional (dashboard, human review, cleanup)
```

## Pipeline phases

| Phase | What | How | Approx time |
|-------|------|-----|-------------|
| 0 | Pre-flight cleanup | Clear stale `_temp/` dirs | seconds |
| 1 | Heuristic pass | `python Scripts/generate_visual_review_manifest.py` | ~2 min |
| 2 | Generate prompts | `python Scripts/generate_visual_review_prompts.py` | seconds |
| 3a | Capture screenshots | `python batch-screenshots.py --local` | ~5 min |
| 3b | Visual analysis | Sub-agents read screenshots, produce JSON | ~20-30 min |
| 3c | Spot-check | Orchestrator verifies, reviews disagreements | ~10 min |
| 4 | Reconcile | `python Scripts/reconcile_results.py` | seconds |
| 5 | Report | Summary + append to `verification-log.md` | ~2 min |
| 6 | Dashboard (optional) | `python Scripts/generate_review_dashboard.py` | seconds |
| 7 | Human review (optional) | Process reviewer's exported JSON | on request |
| 8 | Cleanup (optional) | Delete screenshots, batch prompts | on request |

## Phase 3 detail: screenshot capture + visual analysis

Phase 3 is the most complex phase. It is split into three sub-phases.

### Phase 3a: Capture screenshots

`batch-screenshots.py` uses Playwright as a Python library (not MCP) with
ArcGIS native `MapView.takeScreenshot()` for GPU-direct PNG capture.

- Opens the web app once, iterates through all endpoints
- For corridors: `__selectCorridorSegments()` highlights all sub-segments
- Two screenshots per endpoint: close (zoom 17) and context (zoom 15)
- Validates screenshots aren't blank (>10KB minimum)
- Supports `--start-batch`, `--end-batch`, `--overwrite`, `--close-zoom`,
  `--context-zoom`

All automation helpers live in `Web-App/app.js` (single source of truth).
The batch script verifies they exist at runtime and fails fast if stale.

### Phase 3b: Visual analysis with rescan loop

Sub-agents receive batch prompt files that reference screenshot file paths.
They read each screenshot pair using the Read tool and assess endpoints.

If a screenshot is unusable (blank, no highlight, unreadable labels), the
agent sets `"needs_rescan": true` on that endpoint.

After each wave, the orchestrator:
1. Collects rescan flags
2. Re-captures flagged endpoints with adjusted zoom
3. Re-dispatches analysis for affected batches
4. Repeats up to 2 times per endpoint

### Phase 3c: Spot-check

The orchestrator validates each batch result:
1. Schema check (all required fields, correct types)
2. Endpoint count matches batch prompt
3. Cross-reference against heuristic (flag disagreements)
4. Reasoning quality (no generic/copy-paste language)
5. Visually verify every disagreement using screenshots
6. Gap/corridor-specific checks
7. Spot-check 10% of agreements

## Output schemas

### heuristic-results.csv

| Column | Type | Description |
|--------|------|-------------|
| Segment | string | Segment name (e.g., "IH 820 - D") |
| Direction | string | "N to S", "S to N", "W to E", "E to W" |
| Type | string | "Continuous" or "Gap" |
| Side | string | "From" or "To" |
| Piece | int or empty | Piece number for Gap segments (1-indexed) |
| Auto-Limit | string | Heuristic-computed limit |
| Heuristic | string | Heuristic classification types (pipe-separated) |
| Confidence | float | 0.0-1.0 |
| Confidence-Bucket | string | "high", "medium", or "low" |
| Lon | float | Longitude |
| Lat | float | Latitude |

### visual-review-manifest.json

```json
[
  {
    "segment": "IH 820 - D",
    "side": "From",
    "type": "Continuous",
    "lon": -97.2155,
    "lat": 32.8330,
    "direction": "N to S",
    "route_family": "IH 820",
    "endpoint_hint": "start of the teal segment line"
  }
]
```

Gap segments add: `"piece"`, `"piece_count"`, `"app_segment_names"`.

### batch-NN-results.json

```json
[
  {
    "endpoint_id": 1,
    "segment": "IH 820 - D",
    "side": "From",
    "piece": null,
    "close_screenshot": "batch-01-ep-01-close.png",
    "context_screenshot": "batch-01-ep-01-context.png",
    "visible_labels": ["NE Loop 820", "SH 183"],
    "visible_shields": ["IH 820", "SH 183"],
    "county_boundary_at_endpoint": false,
    "limit_identification": "SH 183",
    "limit_alias": null,
    "is_offset": false,
    "offset_direction": null,
    "offset_from": null,
    "visual_confidence": "high",
    "reasoning": "The teal segment begins at the IH 820 / SH 183 interchange."
  }
]
```

Optional: `"needs_rescan": true` when screenshot is unusable.

### final-segment-limits.csv

| Column | Description |
|--------|-------------|
| Segment | Segment name |
| Direction | Compass direction |
| Type | Continuous or Gap |
| Side | From or To |
| Piece | Piece number (Gap only) |
| Heuristic-Limit | Heuristic answer |
| Heuristic-Confidence | 0.0-1.0 |
| Visual-Limit | Visual answer |
| Visual-Confidence | high/medium/low |
| Final-Limit | Reconciled answer |
| Final-Confidence | Combined score |
| Resolution | confirmed/enriched/visual_preferred/conflict/visual_only |
| Disagreement-Category | Category of disagreement (if any) |
| Visual-Labels-Seen | Raw label observations |

### final-segment-limits-collapsed.csv

One row per segment. From = first piece's From limit, To = last piece's To.

## Reconciliation logic

| Scenario | Resolution | Final-Limit source |
|----------|-----------|-------------------|
| Both agree | `confirmed` | Heuristic (visual validates) |
| Visual adds alias | `enriched` | Heuristic + visual alias |
| Disagree, visual confidence >= medium | `visual_preferred` | Visual |
| Disagree, both low confidence | `conflict` | Heuristic (flagged for review) |
| Visual only (heuristic missing) | `visual_only` | Visual |

Confidence scoring:
- Confirmed: avg * 1.05 (capped at 0.99)
- Enriched: avg * 1.02 (capped at 0.99)
- Visual preferred: visual confidence
- Conflict: heuristic confidence * 0.6

## Key files

| File | Purpose |
|------|---------|
| `orchestrator.md` | **Runtime execution document** — follow this to run the pipeline |
| `batch-screenshots.py` | Screenshot capture (Playwright + ArcGIS native) |
| `Web-App/app.js` | Web app + automation API (`__selectCorridorSegments`, etc.) |
| `Scripts/identify_segment_limits.py` | Heuristic analysis |
| `Scripts/generate_visual_review_manifest.py` | Manifest + heuristic CSV generation |
| `Scripts/generate_visual_review_prompts.py` | Batch prompt generation |
| `Scripts/reconcile_results.py` | Heuristic + visual reconciliation |
| `Scripts/generate_review_dashboard.py` | Human review dashboard |
| `verification-log.md` | Persistent run history (never deleted) |
| `SEGMENT_LIMITS_CASE_STUDY.md` | Project case study |

## Web app automation API

All helpers live in `Web-App/app.js` (single source of truth).
`batch-screenshots.py` depends on these and verifies they exist at runtime.

| Function | Purpose |
|----------|---------|
| `__waitForSegments()` | Wait for ArcGIS features to load |
| `__selectAndZoomSegment(name)` | Select one segment by exact name |
| `__selectCorridorSegments(name)` | Select all sub-segments in a corridor |
| `__waitForTiles(timeout)` | Wait for `view.updating === false` |
| `__captureView(width, height)` | Native `MapView.takeScreenshot()` |
| `__navigateAndCapture(name, lon, lat, closeZoom, contextZoom)` | Navigate + capture close/context pair |

## Directory structure

```
_temp/visual-review/
  heuristic-results.csv           Phase 1 (private to orchestrator)
  visual-review-manifest.json     Phase 1 (shared with visual agents)
  batch-prompts/                  Phase 2 (batch-01.md through batch-07.md)
  screenshots/                    Phase 3a (batch-NN-ep-MM-{close|context}.png)
  batch-results/                  Phase 3b (batch-NN-results.json)
  final-segment-limits.csv        Phase 4 (authoritative)
  final-segment-limits-collapsed.csv  Phase 4 (authoritative)
  review-dashboard.html           Phase 6 (optional)
```
