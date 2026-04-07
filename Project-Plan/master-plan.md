# Hybrid Heuristic + Visual Verification Workflow (Master Plan)

> This is the **master reference document**. It contains the complete architecture, data schemas, agent roles, reconciliation logic, dashboard spec, and implementation details.
>
> Execution is split into three sub-plans (all in this folder):
> - [**Plan 1: Infrastructure**](plan-1-infrastructure.md) — Steps 1–4, batch prompt template (Tasks A–E)
> - [**Plan 2: Reconciliation + Orchestrator**](plan-2-reconciliation-orchestrator.md) — Steps 5–6, verification log (Tasks F–G)
> - [**Plan 3: Review Dashboard**](plan-3-review-dashboard.md) — Step 7 (Task H)
>
> Each sub-plan references this master for full context. When passing a sub-plan to an agent, also point it at this file so it can look up any schema, decision table, or design rationale it needs.

## Context

The heuristic model in `identify_segment_limits.py` is approaching 87% accuracy on 150 trained segments. To push toward higher accuracy on **new** segments and build a verification layer, we need a two-pass system:

1. **Heuristic pass** - runs the Python model, outputs limits + confidence for every endpoint
2. **Visual pass** - an independent Visual Review Agent uses Playwright MCP to screenshot each endpoint on the TxDOT map, identifies the limit from visual map reading alone, and reports its own confidence

The key design principle: the Visual Review Agent must NOT see the heuristic answer before making its own assessment. This is enforced **architecturally through agent context isolation** — each agent has its own context and can only see the files explicitly passed to it. This is stronger than instructing a single agent "don't look at this file" — context isolation makes it impossible, not just inadvisable.

**Gap segments** (e.g., FM 2331 - B, FM 1189) are segments whose ArcGIS geometry has physically separated pieces (>= 200m apart). Currently they are classified as `segment_type="Gap"`, processed piece-by-piece, but **skipped in evaluation**. This plan treats gap segments as first-class citizens throughout the pipeline — they get visual verification for every piece endpoint, not just the overall From/To.

---

## Multi-Agent Architecture

This pipeline uses multiple AI agents with **isolated contexts** to ensure independence between the heuristic and visual passes. Each agent has a specific role, sees only what it needs, and uses the appropriate LLM model for its complexity level.

### Agent Roles

| Agent | Role | LLM Model | Context (what it can see) |
|-------|------|-----------|--------------------------|
| **Orchestrator Agent** | Coordinates all phases, dispatches work, runs reconciliation, reports | Heavy model (GPT-5.4) — needs full reasoning | EVERYTHING: heuristic results, visual results, final reconciliation |
| **Heuristic Agent** | Runs Python scripts, manages heuristic pipeline | Lighter model — just script execution and error handling | Python scripts, ArcGIS data, input CSV. Does NOT see visual results. |
| **Visual Review Agent** (x N) | Screenshots endpoints via Playwright MCP, evaluates map visually | Heavy model (GPT-5.4) — needs vision + spatial reasoning | Manifest (coordinates only), Playwright MCP, web app URL. Does NOT see heuristic-results.csv or any data files. |

### Why separate agents (not one agent with instructions)

- **Anti-bias by architecture** — The Visual Review Agents literally cannot access `heuristic-results.csv` because it is not in their context. Context isolation makes bias impossible, not just inadvisable.
- **Right model for each job** — The Heuristic Agent just runs scripts; a lighter, cheaper model works. Visual Review needs heavy vision/reasoning. The Orchestrator needs heavy reasoning for reconciliation.
- **Parallelism** — Multiple Visual Review Agents run simultaneously on different endpoint batches, each with independent context.
- **Autonomous by default** — The Orchestrator runs Phases 1–5 without human confirmation. Phases 6–8 (dashboard, human review, cleanup) are optional and only run if the user wants them.

### Data flow between agents

```
                    Orchestrator Agent
                    (heavy model, sees everything)
                    reads plan, starts pipeline
                           │
                    Phase 0: cleanup stale files
                           │
                           ▼
                    Heuristic Agent
                    (lighter model, runs Python scripts)
                           │
              ┌────────────┼────────────────┐
              ▼            ▼                 │
    heuristic-results.csv  manifest.json     │
    (PRIVATE — only        (SHARED with      │
     Orchestrator sees)     Visual Agents)   │
                           │                 │
              ┌────────────┼───────┐         │
              ▼            ▼       ▼         │
         Visual Agent  Visual Agent  ...     │
         (batch 01)    (batch 02)            │
         (heavy model) (heavy model)         │
              │            │                 │
              ▼            ▼                 │
         batch-01-     batch-02-             │
         results.json  results.json          │
              │            │                 │
              └────────────┼─────────────────┘
                           ▼
                    Orchestrator Agent
                    runs reconcile_results.py
                    with BOTH inputs
                           │
                           ▼
              final-segment-limits.csv (endpoint-level audit — AUTHORITATIVE)
              final-segment-limits-collapsed.csv (per-segment — AUTHORITATIVE)
```

> **Authoritative outputs**: Phase 4's `final-segment-limits.csv` and `final-segment-limits-collapsed.csv` are the pipeline's deliverables. Phases 6–8 (dashboard generation, human review, cleanup) are **optional** quality-check steps. If a human reviewer does provide feedback, Phase 7 can optionally use an LLM to produce adjudicated CSVs incorporating the reviewer's overrides — but the Phase 4 outputs stand on their own.

### Pipeline phases by agent

| Phase | Agent | What it does |
|-------|-------|-------------|
| 0 | Orchestrator | Pre-flight cleanup of stale files |
| 1 | Heuristic Agent | Runs `generate_visual_review_manifest.py` → heuristic-results.csv + manifest.json |
| 2 | Heuristic Agent | Runs `generate_visual_review_prompts.py` → batch prompt files |
| 3 | Visual Review Agents (x N, parallel) | Screenshot + evaluate endpoints, write batch-NN-results.json |
| 4 | Orchestrator | Runs `reconcile_results.py` → final CSVs |
| 5 | Orchestrator | Generates summary report + appends to verification-log.md |
| 6 | Orchestrator | *(Optional)* Runs `generate_review_dashboard.py` → review-dashboard.html |
| 7 | Orchestrator | *(Optional)* Processes human reviewer's exported notes JSON; can generate adjudicated output via LLM |
| 8 | Orchestrator | *(Optional)* Cleanup prompt (screenshots, batch prompts, dashboard) |

### `_temp/visual-review/` directory structure

All intermediate and temporary files live under `_temp/visual-review/`:

```
_temp/visual-review/
├── heuristic-results.csv          # [Phase 1] Heuristic answers + confidence (PRIVATE — Orchestrator only)
├── visual-review-manifest.json    # [Phase 1] Coordinates only, no answers (SHARED with Visual Agents)
├── batch-prompts/                 # [Phase 2] Generated batch prompt files
│   ├── batch-01.md
│   ├── batch-02.md
│   └── ...
├── batch-results/                 # [Phase 3] Visual Review Agent observations (JSON)
│   ├── batch-01-results.json
│   ├── batch-02-results.json
│   └── ...
├── screenshots/                   # [Phase 3] All close + context screenshots
│   ├── batch-01-ep-01-close.png
│   ├── batch-01-ep-01-context.png
│   └── ...
├── final-segment-limits.csv            # [Phase 4] Endpoint-level audit table (FINAL)
├── final-segment-limits-collapsed.csv  # [Phase 4] One row per segment (FINAL)
└── review-dashboard.html              # [Phase 6] Human review dashboard (open in browser)
```

**Lifecycle**:
- `screenshots/` — Delete after human review of the dashboard is complete (Phase 8), or immediately after Phase 5 if dashboard is not requested. Estimated ~120MB (~2 screenshots per endpoint x ~200KB each). The dashboard references screenshots via relative paths, so they must stay until the reviewer is done.
- `batch-prompts/` — Delete after all batches complete. These are generated artifacts, not source files.
- `heuristic-results.csv`, `visual-review-manifest.json`, `batch-results/` — Keep alongside `final-segment-limits.csv` and `final-segment-limits-collapsed.csv` for auditability.

Everything is under `_temp/` so the whole pipeline's intermediate state can be wiped with a single directory delete if needed.

**Cleanup protocol**:

At the **start** of each run, before Phase 1, the orchestrator checks if `_temp/visual-review/` already exists with files from a previous run. If it does:
- **Default (autonomous)**: Log the stale files found, delete `screenshots/`, `batch-prompts/`, and `batch-results/`, then proceed. The heuristic results and manifest will be regenerated.
- **If `--resume` flag is passed**: Skip cleanup and resume from where the last run left off (check for existing `batch-NN-results.json` to determine completed batches).

At the **end** of a successful run (after Phase 5 report), temporary files (screenshots, batch prompts) are kept only if the user requested dashboard generation (Phase 6). Otherwise they can be cleaned up immediately.

This prevents stale files from a crashed/incomplete previous run from accumulating and bloating context in future runs.

---

## Step 1: Enhance `identify_segment_limits.py`

**File**: `Scripts/identify_segment_limits.py`

### 1a. Add endpoint coordinates and confidence to `RowProcessingResult` (line 308):

```python
@dataclass(frozen=True)
class RowProcessingResult:
    # ... existing fields ...
    from_endpoint_wgs84: tuple[float, float] | None = None  # (lon, lat)
    to_endpoint_wgs84: tuple[float, float] | None = None
    confidence_from: float = 0.0
    confidence_to: float = 0.0
    # Gap segment piece-level detail
    gap_piece_endpoints: list[dict] | None = None
```

For **continuous segments** (line 3933+): `start_endpoint_wgs84` / `end_endpoint_wgs84` and `from_candidate.confidence` / `to_candidate.confidence` are already computed — thread them into the result.

For **gap segments** (line 3826+): populate `gap_piece_endpoints` with per-piece data:

**Convention: piece indexing is 1-based everywhere** (Python dataclass, CSV, JSON manifest, batch prompts, results).

```python
gap_piece_endpoints = [
    {
        "piece": 1,            # 1-based, consistent across all outputs
        "from_wgs84": (lon, lat),  # piece_wgs84.coords[0]
        "to_wgs84": (lon, lat),    # piece_wgs84.coords[-1]
        "from_limit": "N SH 171",
        "to_limit": "FM 917",
        "from_confidence": 0.92,
        "to_confidence": 0.85,
        "from_heuristic": "route_intersection",
        "to_heuristic": "local_labeled_road",
    },
    {
        "piece": 2,
        "from_wgs84": (lon, lat),
        "to_wgs84": (lon, lat),
        "from_limit": "FM 917",
        "to_limit": "US 67",
        ...
    }
]
```

The overall `from_endpoint_wgs84` / `to_endpoint_wgs84` still hold the first piece's From and last piece's To (same as `auto_from` / `auto_to`).

### 1b. Confidence bucket helper:

```python
def confidence_bucket(score: float) -> str:
    if score >= 0.90: return "high"
    if score >= 0.78: return "medium"
    return "low"
```

---

## Step 2: Create `generate_visual_review_manifest.py`

**File**: `Scripts/generate_visual_review_manifest.py`

Pattern: Import `identify_segment_limits` as a module (same approach as `trusted_review_eval.py` line 29-36).

**Inputs**: A CSV with a column containing segment names (e.g., Amy's review sheet `FTW-Segments-Limits-Amy.review.csv` with its `Segment` column, or a simple one-column CSV of segment names). The script auto-detects the segment name column. Pass `--all` to run on all segments in the ArcGIS layer instead of a CSV subset.

**Outputs**:

### a) `_temp/visual-review/heuristic-results.csv`

For **continuous segments** — one row per side (From/To):

| Segment | Direction | Type | Side | Auto-Limit | Heuristic | Confidence | Confidence-Bucket | Lon | Lat |
|---------|-----------|------|------|-----------|-----------|------------|-------------------|-----|-----|

For **gap segments** — one row per piece endpoint (piece1-From, piece1-To, piece2-From, ...):

| Segment | Direction | Type | Side | Piece | Auto-Limit | Heuristic | Confidence | Confidence-Bucket | Lon | Lat |
|---------|-----------|------|------|-------|-----------|-----------|------------|-------------------|-----|-----|
| FM 2331 - B | N to S | Gap | From | 1 | N SH 171 | route_intersection | 0.92 | high | -97.31 | 32.45 |
| FM 2331 - B | N to S | Gap | To | 1 | FM 917 | local_labeled_road | 0.85 | medium | -97.30 | 32.38 |
| FM 2331 - B | N to S | Gap | From | 2 | FM 917 | local_labeled_road | 0.86 | medium | -97.29 | 32.32 |
| FM 2331 - B | N to S | Gap | To | 2 | US 67 | route_intersection | 0.94 | high | -97.28 | 32.25 |

This means gap segments produce 2N rows (where N = number of pieces) instead of 2. The intermediate piece endpoints are important — they identify where the gap is and what roads bound each piece.

### b) `_temp/visual-review/visual-review-manifest.json`

Anti-bias firewall — contains only navigation info, NO heuristic answers:

```json
[
  {
    "segment": "FM 730 - A",
    "side": "From",
    "type": "Continuous",
    "lon": -97.5432,
    "lat": 33.2145,
    "direction": "N to S",
    "route_family": "FM 730",
    "endpoint_hint": "start of the teal segment line"  // "end of the teal segment line" for To side
  },
  {
    "segment": "FM 2331 - B",
    "side": "From",
    "type": "Gap",
    "piece": 1,
    "piece_count": 2,
    "lon": -97.31,
    "lat": 32.45,
    "direction": "N to S",
    "route_family": "FM 2331",
    "endpoint_hint": "start of piece 1 (this segment has a physical gap — 2 separate pieces)"
  }
]
```

The manifest tells the Visual Review Agent a segment has gaps and how many pieces, but NOT what the heuristic found at each endpoint.

---

## Step 3: Create `generate_visual_review_prompts.py`

**File**: `Scripts/generate_visual_review_prompts.py`

Reads `visual-review-manifest.json` and generates batch prompt files (~15 endpoints per batch, covering ~8 segments with both From/To).

Each generated prompt follows this structure:

```markdown
# Visual Review Batch NN

You are performing INDEPENDENT visual verification of highway segment endpoints.

## CRITICAL: Visual-only assessment

- Do NOT look at any heuristic results files (heuristic-results.csv, etc.)
- Do NOT click on roadway lines to open feature popups — popups query the 
  same TxDOT data the heuristic already uses, defeating the independence
- Do NOT inspect network requests, API responses, or GeoJSON data
- Do NOT read any CSV, JSON, or data files — only the rendered map
- ONLY use what you can visually read from the basemap: rendered road labels, 
  route shield graphics, county boundary lines, and the segment highlight

Your assessment must come purely from visual map reading, not data queries.

## Web App

- **Primary**: https://pine-j.github.io/Roadway-Segment-Limits/
- **Fallback (local)**: Serve `Web-App/` locally 
  (e.g., `npx http-server Web-App -p 8080`) and use 
  `http://localhost:8080` if GitHub Pages is down or slow.

Use the primary URL by default. Switch to local only if the page fails to load.

## Browser setup (once per batch)

1. Navigate to the web app URL
2. Wait for segments to load by running in the browser console:
   `await window.__waitForSegments()`
   This returns the number of loaded segments. It may take 5-10 seconds on first load.
3. If the app fails to load or `__waitForSegments` hangs for >30 seconds, 
   try the local fallback URL. If both fail, report the error and skip this batch.

## Workflow per endpoint

1. Select and highlight the segment (once per segment, covers both From/To):
   `await window.__selectAndZoomSegment("SEGMENT_NAME")`
   - If this returns `false`, the segment name was not found. Log it and skip.
2. Zoom to the specific endpoint:
   `await window.__mapView.goTo({center: [LON, LAT], zoom: 17})`
3. Wait for tiles to load (~3 seconds)
4. Note which line is the SELECTED segment — it is the **thick teal line** 
   (unselected segments appear as thinner maroon/burgundy lines; ignore those)
5. Take a CLOSE screenshot (zoom 17+, ~200-300m radius, labels readable)
6. Zoom out: `await window.__mapView.goTo({center: [LON, LAT], zoom: 15})`
7. Take a CONTEXT screenshot (zoom 15, shows surrounding area)
8. Record what you see independently

## What to look for at each endpoint

Pay special attention to these common scenarios — they represent the kinds of 
cases where data-driven approaches struggle and visual map reading excels:

### A. Read ALL visible road labels near the endpoint
- Street name labels rendered along road lines (e.g., "W Vickery Blvd", "E Euless Blvd")
- Route shields with numbers (e.g., IH 30, SH 114, US 281)
- Local/alias names that appear on the map alongside route numbers 
  (e.g., "Benbrook Blvd" next to "US 377", "South Fwy" next to "IH 35W",
   "Northwest Pkwy" next to "SH 199")
- Report BOTH the local name AND the route number if both are visible

### B. Identify which road is actually AT the endpoint
- The endpoint is where the teal segment line ends — which road crosses or 
  meets the segment at THAT exact point?
- A nearby road 200m away is NOT the limit — the limit is what's at the endpoint
- If multiple roads are near the endpoint, identify which one the segment 
  line actually terminates at
- Look carefully at frontage roads vs mainlines — "Left Frontage US 81" and 
  "Right Frontage US 81" are different roads on different sides of the highway

### C. Check for offset situations
- If the endpoint is BETWEEN two intersections (not exactly at either one), 
  note this: "endpoint is ~200m south of [Road X]" or "between [Road X] and [Road Y]"
- Note the compass direction from the nearest identifiable road to the endpoint

### D. Look for county boundaries
- County boundary lines may appear as thin administrative lines on the map
- If the endpoint itself is AT a county boundary (the segment line ends at 
  the county line), set `county_boundary_at_endpoint: true` and use 
  "[County Name] County Line" as your `limit_identification`
- A county line that is merely visible nearby but NOT at the endpoint 
  should be noted in `reasoning` but NOT flagged as `county_boundary_at_endpoint`

### E. For GAP segments specifically
- Does the segment line visibly end/restart here?
- Is there a physical discontinuity in the road?
- What roads bound the gap on each side?

## Endpoints

| # | Segment | Side | Piece | Navigate to | Notes |
|---|---------|------|-------|-------------|-------|
| 1 | FM 730 - A | From | — | center: [-97.543, 33.215] | Continuous segment |
| 2 | FM 730 - A | To | — | center: [-97.489, 33.198] | Continuous segment |
| 3 | FM 2331 - B | From | 1/2 | center: [-97.31, 32.45] | GAP segment — piece 1 start |
| 4 | FM 2331 - B | To | 1/2 | center: [-97.30, 32.38] | GAP — piece 1 end (gap starts after) |
| 5 | FM 2331 - B | From | 2/2 | center: [-97.29, 32.32] | GAP — piece 2 start (gap ends before) |
| 6 | FM 2331 - B | To | 2/2 | center: [-97.28, 32.25] | GAP segment — piece 2 end |
...

## Screenshots

Save all screenshots to: `_temp/visual-review/screenshots/`
Naming: `batch-NN-ep-NN-close.png` and `batch-NN-ep-NN-context.png`

## Output format (write to _temp/visual-review/batch-results/batch-NN-results.json)

Write a JSON file (NOT markdown) so reconciliation can parse deterministically.

```json
[
  {
    "endpoint_id": 1,
    "segment": "FM 730 - A",
    "side": "From",
    "piece": null,
    "close_screenshot": "batch-01-ep-01-close.png",
    "context_screenshot": "batch-01-ep-01-context.png",
    "visible_labels": ["W Vickery Blvd", "E Vickery Blvd", "IH 30", "199"],
    "visible_shields": ["IH 30", "SH 199"],
    "county_boundary_at_endpoint": false,
    "limit_identification": "W Vickery Blvd",
    "limit_alias": null,
    "is_offset": false,
    "offset_direction": null,
    "offset_from": null,
    "visual_confidence": "high",
    "reasoning": "Segment line ends exactly at W Vickery Blvd intersection"
  },
  {
    "endpoint_id": 3,
    "segment": "FM 2331 - B",
    "side": "From",
    "piece": 1,
    "close_screenshot": "batch-01-ep-03-close.png",
    "context_screenshot": "batch-01-ep-03-context.png",
    "visible_labels": ["SH 171", "N SH 171"],
    "visible_shields": ["SH 171"],
    "county_boundary_at_endpoint": false,
    "limit_identification": "N SH 171",
    "limit_alias": null,
    "is_offset": false,
    "offset_direction": null,
    "offset_from": null,
    "visual_confidence": "high",
    "reasoning": "Segment piece 1 starts at N SH 171 intersection"
  }
]
```

Key fields for reconciliation:
- `piece`: null for continuous, 1-based integer for gap segments (disambiguates multiple From/To per segment)
- `limit_identification`: the primary road/boundary name (e.g., "IH 35W", "Tarrant County Line")
- `limit_alias`: local/street name visible alongside the route number, or null if none seen (e.g., "South Fwy" when limit_identification is "IH 35W"). This enables the `enriched` reconciliation path deterministically — if `limit_alias` is non-null and the heuristic only has the route number, use `"limit_alias (limit_identification)"` format
- `is_offset` + `offset_direction` + `offset_from`: structured offset data instead of free text
- `county_boundary_at_endpoint`: true only if the segment endpoint IS the county line, not merely nearby
- `visible_labels` / `visible_shields`: raw observations for audit trail
```
(end of batch prompt template)

---

## Known Heuristic Weakness Categories (informs prompt design & reconciliation)

The heuristic script has 31 known mismatches across 296 continuous endpoints. These fall into categories that determine **where visual verification adds value** vs. **where code fixes are needed**:

### Visual verification HIGH value (MCP agent should catch these):

| Category | Count | What happens | What the Visual Review Agent should do |
|----------|-------|--------------|-----------------------------|
| **Different road at endpoint** | 13 | Script picks nearby-but-wrong road (e.g., BU 81D instead of Walnut St) | Read ALL labels near endpoint, identify which road the segment line actually terminates at — not just the nearest route |
| **Alias names not in tile data** | 5 | Script only knows route numbers, but map shows local names (e.g., "South Fwy (IH 35W)", "Northwest Pkwy (SH 199)") | Report BOTH the local alias name AND the route number when both are visible on the map |
| **Offset extra/wrong direction** | 3 | Script adds offset phrasing incorrectly or gets compass direction wrong | Determine if the endpoint is exactly at a road or between roads; if between, give the correct compass direction from the nearest landmark |
| **County line not detected** | 2 | Script misses county boundary, picks a nearby route instead | Check for county boundary lines or county name changes near the endpoint |

### Visual verification MODERATE value (MCP helps confirm, code fix needed too):

| Category | Count | What happens | What the Visual Review Agent should do |
|----------|-------|--------------|-----------------------------|
| **Offset phrasing missing** | 4 | Script picks nearest road without noting the endpoint is offset from it | Note when the endpoint is clearly between intersections, not exactly at one |
| **Offset wording differs** | 2 | Script finds different alias for same interchange (e.g., "Benbrook Pkwy" vs "Benbrook Blvd") | Report the most prominent/visible name at the location |
| **Other (directional prefix, offset missing)** | 2 | Minor wording differences | Report directional prefixes visible on labels (e.g., "N SH 171" vs "SH 171") |

### Reconciliation implications:

When the Visual Review Agent disagrees with the heuristic, the reconciliation script should categorize the disagreement:

- **Visual Agent sees a different road**: likely a "different road at endpoint" case → trust visual (visual_preferred)
- **Visual Agent reports alias + route number, heuristic has only route number**: likely an "alias" case → use visual's richer label
- **Visual Agent says "no offset needed", heuristic added offset**: likely an "offset extra" case → trust visual if confidence >= 0.70, otherwise flag for review
- **Visual Agent reports county boundary, heuristic doesn't**: likely a "county not detected" case → trust visual if confidence >= 0.70, otherwise flag for review
- **Visual Agent and heuristic agree on road but differ on offset phrasing**: lower priority, flag for review

---

## Step 4: Expose programmatic API in Web App

**File**: `Web-App/app.js`

Expose both the map view AND segment selection/highlight so Playwright can drive everything programmatically:

```javascript
// After line 121 (view creation)
window.__mapView = view;

// After syncSelectedGraphics and zoomToSegments are defined (~line 560)

// Readiness helper — waits for both the map view AND the segment list to load.
// state is closure-scoped, so this must be defined inside the require() callback.
window.__waitForSegments = function() {
  return view.when().then(() => {
    return new Promise((resolve) => {
      const check = () => {
        if (state.segments && state.segments.length > 0 && !view.updating) {
          resolve(state.segments.length);
        } else {
          setTimeout(check, 500);
        }
      };
      check();
    });
  });
};

window.__selectAndZoomSegment = async function(segmentName) {
  // state.segments stores Readable_SegID as `label` (app.js line 725)
  const match = state.segments.find(s => s.label === segmentName);
  if (!match) return false;
  state.selectedSegmentIds.clear();
  state.selectedSegmentIds.add(match.objectId);
  render();                        // re-renders sidebar with selection (app.js line 332)
  await syncSelectedGraphics();    // highlights the segment on the map
  await zoomToSegments([match.objectId]);  // zooms to it
  return true;
};
```

This exposes three APIs, all defined inside the `require()` closure where `state` is accessible:
- `window.__waitForSegments()` — returns a Promise that resolves when segments are loaded
- `window.__selectAndZoomSegment(name)` — select, highlight, zoom
- `window.__mapView` — fine-tune camera position

This gives Playwright two APIs:
- `window.__selectAndZoomSegment("FM 730 - A")` — select, highlight, and zoom to a segment (replaces search/checkbox/zoom)
- `window.__mapView.goTo({center: [lon, lat], zoom: 17})` — fine-tune zoom to a specific endpoint after selection

**Per-endpoint workflow becomes**:
1. `browser_evaluate`: `await window.__selectAndZoomSegment("FM 730 - A")` (once per segment)
2. `browser_evaluate`: `await window.__mapView.goTo({center: [lon, lat], zoom: 17})` (per endpoint)
3. Wait for tiles (~3s)
4. `browser_take_screenshot`

This eliminates the search/checkbox/zoom UI interaction entirely. Estimated per-endpoint time: ~20-30s (down from ~90s with UI automation).

---

## Step 5: Create `reconcile_results.py`

**File**: `Scripts/reconcile_results.py`

Reads both `heuristic-results.csv` and all `batch-NN-results.json` files. JSON output from Visual Review Agents is parsed deterministically — no prose parsing needed.

### Visual confidence mapping:

| Bucket | Numeric | Meaning |
|--------|---------|---------|
| `high` | 0.90 | Labels clearly readable, endpoint location unambiguous |
| `medium` | 0.70 | Labels partially readable or multiple plausible roads near endpoint |
| `low` | 0.50 | Labels unreadable, endpoint ambiguous, or low zoom quality |

### Two-phase comparison logic:

| Heuristic | Visual | Resolution | Final |
|-----------|--------|------------|-------|
| Same road (canonical match) | Same road (any conf) | `confirmed` | Use either, boost confidence |
| Route number only | Visual has alias + route (any conf) | `enriched` | Use visual's richer label (e.g., "South Fwy (IH 35W)" instead of "IH 35W") |
| Different road | Visual high/medium conf (>= 0.70) | `visual_preferred` | Use visual answer — likely a "different road at endpoint" case |
| Different road | Visual low conf (< 0.70) | `conflict` | Flag for human review with both answers |
| Has offset | Visual says `is_offset: false` AND conf >= 0.70 | `visual_preferred` | Visual sees endpoint is directly at the road, not offset |
| Has offset | Visual says `is_offset: false` AND conf < 0.70 | `conflict` | Low-confidence screenshot, flag for review |
| No offset | Visual says `is_offset: true` AND conf >= 0.70 | `visual_preferred` | Visual sees endpoint is between intersections |
| No offset | Visual says `is_offset: true` AND conf < 0.70 | `conflict` | Low-confidence screenshot, flag for review |
| Any road | Visual sees `county_boundary_at_endpoint: true` AND conf >= 0.70 | `visual_preferred` | County line not detected by heuristic |
| Any road | Visual sees `county_boundary_at_endpoint: true` AND conf < 0.70 | `conflict` | County boundary claim at low confidence, flag for review |
| Empty | Visual has answer | `visual_only` | Use visual answer |

Road name comparison uses the existing `canonical()` function from `identify_segment_limits.py`. The comparison should also check for alias relationships (e.g., "Benbrook Blvd" ≈ "US 377" are related but not identical).

### Combined confidence:
- `confirmed`: max(heuristic_conf, 0.92)
- `enriched`: max(heuristic_conf, 0.90) — same road, just better labeling
- `visual_preferred`: visual_numeric (0.90, 0.70, or 0.50 per bucket above)
- `conflict`: max(heuristic_conf, visual_numeric) * 0.6 (disagreement lowers certainty)
- `visual_only`: visual_numeric * 0.9

### Output: `_temp/visual-review/final-segment-limits.csv`

| Segment | Type | Side | Piece | Heuristic-Limit | Heuristic-Confidence | Visual-Limit | Visual-Confidence | Final-Limit | Final-Confidence | Resolution | Disagreement-Category | Visual-Labels-Seen |

`Resolution` values: `confirmed`, `enriched`, `visual_preferred`, `conflict`, `visual_only`

`Disagreement-Category` (when Resolution != confirmed): `different_road`, `alias_enrichment`, `offset_extra`, `offset_missing`, `county_not_detected`, `offset_direction`, `other`

For continuous segments, `Piece` is empty. For gap segments, it's `1`, `2`, etc. This lets downstream consumers reconstruct the full gap segment limits: "piece1-From to piece1-To; piece2-From to piece2-To".

Gap segments get the same reconciliation logic per-endpoint as continuous segments — each piece boundary is independently verified.

### Secondary output: `_temp/visual-review/final-segment-limits-collapsed.csv`

One row per segment (matching the format downstream consumers expect):

| Segment | Direction | Type | Final-From | Final-To | From-Confidence | To-Confidence | From-Resolution | To-Resolution |

For gap segments, `Final-From` = first piece's From, `Final-To` = last piece's To (same as the heuristic's convention). Interior piece boundaries are only in the endpoint-level audit table above.

This gives both views: the audit table for debugging, and the collapsed table for delivery.

### Persistent output: `verification-log.md` (NEVER deleted)

This is the **long-lived learning document** that persists across all pipeline runs. Unlike everything in `_temp/`, this file lives at the repo root and is **never cleaned up**. It captures what the hybrid pipeline learns each time it runs, building a history that drives heuristic model improvements over time.

The Orchestrator Agent appends a new section after each run. Structure:

```markdown
<!-- Example output — all counts are derived from the manifest at runtime -->
## Run: 2026-04-07 — 150 segments (FTW-Segments-Limits-Amy.review.csv)

### Summary
- Endpoints evaluated: 304
- Confirmed (heuristic + visual agree): 265 (87.2%)
- Enriched (visual added alias): 8
- Visual preferred (visual overrode heuristic): 19
- Conflict (flagged for human review): 7
- Visual only: 5

### Disagreements where visual overrode heuristic

| Segment | Side | Heuristic said | Visual said | Category | Visual Conf | Heuristic Conf | Visual labels seen |
|---------|------|---------------|-------------|----------|-------------|----------------|-------------------|
| FM 730-A | To | BU 81D | Walnut St | different_road | high | 0.88 | Walnut St, E Walnut St, BU 81D shield |
| SH 199-D | To | IH 30 | W Vickery Blvd | different_road | high | 0.85 | W Vickery Blvd, IH 30, Henderson St |
| FM 2264 | From | Right Frontage US 81 | Left Frontage US 81 | different_road | medium | 0.86 | Left Frontage US 81, Right Frontage US 81 |
...

### Conflicts (unresolved — needs human review)

| Segment | Side | Heuristic said | Visual said | Category | Notes |
|---------|------|---------------|-------------|----------|-------|
...

### Patterns for heuristic improvement

(The Orchestrator analyzes the disagreements and identifies recurring patterns)

- **Pattern: nearby-but-wrong road in dense areas** (7 cases)
  Segments: FM 730-A/To, SH 199-D/To, SH 10/To, ...
  The heuristic picks a nearby route (often a business route or shield number) 
  when the actual endpoint is at a local street. Root cause: route candidates 
  score higher than local labels at distances 40-120m.
  Suggested fix: increase local label weight when distance < 60m and label 
  confidence > 0.90.

- **Pattern: left/right frontage confusion** (2 cases)
  Segments: FM 2264/From, ...
  The heuristic picks the wrong side frontage road.
  Suggested fix: strengthen inventory-side-matching geometry check.

### Traceability
- Input CSV: FTW-Segments-Limits-Amy.review.csv
- Heuristic results: _temp/visual-review/heuristic-results.csv (captured at run time)
- Batch results: _temp/visual-review/batch-results/batch-*.json
- Script version: git commit [hash at run time]
```

**Why this matters**:
- Each run's disagreements are **timestamped and preserved** — you can look back at any point to see what the heuristic was getting wrong on a specific date
- The **patterns section** is the actionable output — it groups individual disagreements into recurring themes that suggest specific code fixes
- The **traceability section** links back to the exact inputs and git version, so you can reproduce any run
- Over multiple runs with different segment sets, patterns accumulate — if "left/right frontage confusion" keeps appearing, it's a priority fix
- The file grows over time but stays manageable — each run adds ~50-100 lines

**Lifecycle**: This file is **never deleted** by the cleanup protocol. It is the institutional memory of the hybrid pipeline.

The `reconcile_results.py` script generates the raw data (disagreement rows, counts). The Orchestrator Agent reads that data and writes the analysis/patterns section using its reasoning capabilities.

---

## Step 6: Create Master Orchestrator Prompt

**File**: `orchestrator.md`

This is the master prompt for the Orchestrator Agent. It reads this, understands the full context, and drives the entire pipeline — dispatching the Heuristic Agent, spawning Visual Review Agents for each batch, running reconciliation, and reporting results. No human confirmation is needed between phases.

```markdown
# FTW Segment Limits — Hybrid Visual Verification Pipeline

## What this is

You are the Orchestrator Agent for a multi-agent pipeline that identifies highway
segment limits (endpoints) in the Fort Worth (FTW) transportation network.

You coordinate three types of agents:

1. **Heuristic Agent** (lighter model) — Runs Python scripts that analyze ArcGIS 
   geometry, TxDOT vector tile labels, county boundaries, and roadway inventory 
   data to deterministically identify endpoints. Produces limits + confidence scores.

2. **Visual Review Agents** (heavy model, x N in parallel) — Use Playwright MCP 
   to take screenshots of each endpoint on the TxDOT basemap web app. They 
   independently identify limits from visual map reading ONLY and report their 
   own confidence. They have NO access to heuristic results.

3. **You (Orchestrator)** — Run reconciliation, compare heuristic vs visual 
   results, categorize disagreements, produce final answers with combined confidence.

## Why two passes

The heuristic script is ~87% accurate but has known blind spots:
- It picks nearby-but-wrong roads (13 known cases)
- It can't read local alias names from tile data (5 cases: "South Fwy" for IH 35W)
- It sometimes adds incorrect offset phrasing (3 cases)
- It misses county boundaries (2 cases)
- It misses offset situations (4 cases)

The basemap VISUALLY shows correct information that the data sources don't expose 
programmatically. Visual Review Agents reading the map catch what the script's data can't.

## CRITICAL: Agent context isolation

Visual Review Agents must NOT have access to heuristic results.
- Pass them ONLY the manifest file (coordinates + navigation context, no answers)
- Do NOT include heuristic-results.csv in their context
- Do NOT mention heuristic findings when dispatching them
This is enforced by architecture (separate agent contexts), not by instruction.

## Your workflow

### Phase 0: Pre-flight cleanup
Check if `_temp/visual-review/` exists with files from a previous run.
If so, log the stale files found and delete `screenshots/`, `batch-prompts/`,
and `batch-results/` before starting fresh. The heuristic results and manifest
will be regenerated. If the user passed `--resume`, skip cleanup and resume
from where the last run left off (check for existing `batch-NN-results.json`
to determine which batches are already complete).

### Phase 1: Dispatch Heuristic Agent
Spawn the Heuristic Agent (lighter model) with instructions to run:
```bash
cd Roadway-Segment-Limits
python Scripts/generate_visual_review_manifest.py --input <segments-csv>
```
It produces:
- `_temp/visual-review/heuristic-results.csv` (answers + confidence — Orchestrator only)
- `_temp/visual-review/visual-review-manifest.json` (coordinates only — for Visual Agents)

### Phase 2: Dispatch Heuristic Agent (cont.)
Same Heuristic Agent runs:
```bash
python Scripts/generate_visual_review_prompts.py
```
This produces: `_temp/visual-review/batch-prompts/batch-01.md` through `batch-NN.md`

### Phase 3: Dispatch Visual Review Agents
For each batch file, spawn a Visual Review Agent (heavy model) with:
- The batch prompt file content (contains coordinates, workflow, JSON schema)
- Playwright MCP access
- NO access to heuristic-results.csv or any other data files

Each Visual Review Agent will:
1. Open the web app (primary or local fallback)
2. Wait for segment list to load
3. For each endpoint: navigate, screenshot, evaluate visually
4. Write structured JSON results to `_temp/visual-review/batch-results/batch-NN-results.json`

Run Visual Review Agents **in parallel** — each has its own independent context.

**Resumability**: Before spawning a Visual Review Agent for a batch, check if 
its results JSON already exists. If so, skip that batch (completed in a previous run).

### Phase 4: Reconcile
```bash
python Scripts/reconcile_results.py
```
This merges heuristic + visual results into:
- `_temp/visual-review/final-segment-limits.csv` (endpoint-level audit)
- `_temp/visual-review/final-segment-limits-collapsed.csv` (one row per segment)

### Phase 5: Report + Append to Verification Log
1. Summarize the results to the user:
   - Total endpoints evaluated
   - How many confirmed (heuristic + visual agree)
   - How many enriched (visual added alias info)
   - How many visual_preferred (visual overrode heuristic)
   - How many conflicts (flagged for human review)

2. **Append a new timestamped section to `verification-log.md`**:
   - Summary counts
   - Full disagreement table (every endpoint where Resolution != confirmed)
   - Conflict table (unresolved cases)
   - Pattern analysis: group disagreements into recurring themes and suggest 
     specific heuristic improvements
   - Traceability: input CSV path, git commit hash, date
   
   This file is NEVER deleted. It persists across all runs and builds the 
   institutional memory of where the heuristic model fails and how to improve it.

### Phase 6: Generate Human Review Dashboard *(Optional — only if user requests review)*
```bash
python Scripts/generate_review_dashboard.py
```
This produces a single self-contained HTML file:
`_temp/visual-review/review-dashboard.html`

The human reviewer opens this file in a browser to review results case by case.
See [Step 7: Create Review Dashboard Generator](#step-7-create-review-dashboard-generator) 
for the full specification.

Tell the user:
> "Review dashboard generated. Open `_temp/visual-review/review-dashboard.html` 
> in your browser. When you're done reviewing, export your notes using the 
> 'Export Notes' button — I can then process them to generate adjudicated output."

### Phase 7: Process Human Review Notes *(Optional — only if the user provides exported notes)*
When the user provides the exported notes JSON from the dashboard:
1. Read the reviewer's notes for each case
2. Cross-reference with the reconciliation results
3. Update `verification-log.md` with the human reviewer's observations
4. Identify cases where the human disagrees with the Orchestrator's decision
5. Suggest specific heuristic improvements based on the human's input
6. **If the user requests adjudicated output**: Apply the reviewer's structured
   overrides (cases marked "Disagree" with `reviewer_corrected_limit`,
   `reviewer_corrected_alias`, and `reviewer_county_boundary_at_endpoint` fields)
   to the Phase 4 CSVs and produce `_temp/visual-review/human-reviewed-segment-limits.csv`
   and a regenerated collapsed CSV. If any "Disagree" case is missing a
   `reviewer_corrected_limit`, flag it and ask the user to fill in the field.
   This step is only performed on explicit user request — the Phase 4 outputs
   remain the default authoritative deliverables.

### Phase 8: Cleanup *(Optional — only if dashboard was generated)*
Delete temporary files that are no longer needed:
- `_temp/visual-review/screenshots/` (~120MB)
- `_temp/visual-review/batch-prompts/`
- `_temp/visual-review/review-dashboard.html`

Keep `heuristic-results.csv`, `batch-results/`, `visual-review-manifest.json`, 
`final-segment-limits.csv`, and `final-segment-limits-collapsed.csv` for auditability.

**Important**: Do NOT delete screenshots until AFTER the human has finished 
reviewing the dashboard — the dashboard references them via relative paths.
If no dashboard was generated, screenshots can be cleaned up after Phase 5.

**NEVER delete `verification-log.md`** — it lives outside `_temp/` 
and persists across all runs. This is the long-term learning document.

## Key files
- `Scripts/identify_segment_limits.py` — heuristic engine
- `Scripts/generate_visual_review_manifest.py` — Phase 1
- `Scripts/generate_visual_review_prompts.py` — Phase 2
- `Scripts/reconcile_results.py` — Phase 4
- `Scripts/generate_review_dashboard.py` — Phase 6
- `verification-log.md` — persistent learning log (NEVER deleted)
- `Web-App/` — map app (hosted at pine-j.github.io/Roadway-Segment-Limits/)
- `SEGMENT_LIMITS_LOGIC.md` — heuristic documentation

## What Visual Review Agents should look for (embedded in batch prompts)
- ALL visible road labels and route shields near the endpoint
- Local alias names alongside route numbers (e.g., "Benbrook Blvd" next to US 377)
- Which road the segment line actually terminates at (not just nearest)
- Left vs Right frontage road distinctions
- Offset situations (endpoint between intersections)
- County boundary lines
- For gap segments: physical discontinuities in the segment line
```

---

## Step 7: Create Review Dashboard Generator

**File**: `Scripts/generate_review_dashboard.py`

Reads `final-segment-limits.csv`, `heuristic-results.csv`, `batch-results/*.json`, and generates a single self-contained `review-dashboard.html` file that the human reviewer opens in a browser.

### What the dashboard shows

**Two view modes**:

1. **Table view** — all endpoints in a sortable/filterable table:
   | Segment | Side | Heuristic | Visual | Resolution | Confidence | Status |
   Filterable by Resolution (confirmed, enriched, visual_preferred, conflict).
   Click any row to switch to case view.

2. **Case view** — one endpoint at a time, with full detail:

```
┌─────────────────────────────────────────────────────────────────────┐
│  ◀ Prev  │  Case 17 of N: FM 730-A / To  │  Next ▶  │ Jump to… │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─── Close Screenshot ───┐  ┌─── Context Screenshot ───┐          │
│  │                        │  │                          │          │
│  │  (batch-01-ep-02-      │  │  (batch-01-ep-02-        │          │
│  │   close.png)           │  │   context.png)           │          │
│  │                        │  │                          │          │
│  └────────────────────────┘  └──────────────────────────┘          │
│                                                                     │
│  ┌─── Heuristic Result ────────────────────────────────────────┐   │
│  │  Limit: BU 81D                                              │   │
│  │  Confidence: 0.88 (medium)                                  │   │
│  │  Heuristic: route_intersection                              │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─── Visual Review Result ────────────────────────────────────┐   │
│  │  Limit: Walnut St                                           │   │
│  │  Alias: null                                                │   │
│  │  Confidence: high (0.90)                                    │   │
│  │  Labels seen: Walnut St, E Walnut St, BU 81D shield         │   │
│  │  Reasoning: Segment line ends at Walnut St intersection     │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─── Orchestrator Decision ───────────────────────────────────┐   │
│  │  Resolution: visual_preferred                               │   │
│  │  Category: different_road                                   │   │
│  │  Final: Walnut St                                           │   │
│  │  Final Confidence: 0.90                                     │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─── Interactive Map ─────────────────────────────────────────┐   │
│  │                                                             │   │
│  │  (iframe: pine-j.github.io/Roadway-Segment-Limits/)                │   │
│  │  Auto-navigated to this endpoint's coordinates              │   │
│  │  Human can zoom in/out, click around                        │   │
│  │                                                             │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─── Reviewer Notes ─────────────────────────────────────────┐   │
│  │                                                             │   │
│  │  [textarea — type your observations here]                   │   │
│  │                                                             │   │
│  │  Notes are auto-saved to browser localStorage               │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  Status: ○ Not reviewed  ● Agree  ○ Disagree  ○ Needs discussion   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Key features

**Navigation**:
- Prev/Next arrows to step through cases
- Jump-to dropdown for any segment
- Filter buttons: Show All / Disagreements Only / Conflicts Only / Unreviewed
- Progress indicator: "Reviewed X of N endpoints" (counts derived from data)

**Interactive map embed**:
- iframe pointing to `https://pine-j.github.io/Roadway-Segment-Limits/`
- When switching cases, the dashboard attempts to call into the iframe:
  ```javascript
  iframe.contentWindow.__selectAndZoomSegment("FM 730-A");
  // then
  iframe.contentWindow.__mapView.goTo({center: [lon, lat], zoom: 17});
  ```
- **Cross-origin limitation**: If the dashboard is opened from `file://` or `localhost`, the browser will block `contentWindow` access to the GitHub Pages iframe. In that case, fall back to displaying "Navigate to: [lon, lat] zoom 17" text with a copy button. To get auto-navigation working, serve both the dashboard and the web app from the same origin (e.g., both from `localhost:8080`).
- Human can freely zoom, pan, and inspect the map while reviewing

**Per-case reviewer notes**:
- Textarea for free-form notes on each endpoint
- Radio buttons for quick status: Not reviewed / Agree / Disagree / Needs discussion
- Auto-saved to `localStorage` on every keystroke — notes survive page refreshes and browser restarts
- **Fully editable at any time**: the reviewer can navigate back to any previously reviewed case, edit notes, change the status, and the updates are saved immediately. Nothing is locked until the reviewer clicks "Export Notes".
- No server needed — everything runs client-side

**Export**:
- "Export Notes" button generates a JSON file with all reviewer notes + statuses:
  ```json
  {
    "export_date": "2026-04-07T14:30:00",
    "reviewer": "manual entry or prompt",
    "cases": [
      {
        "segment": "FM 730-A",
        "side": "To",
        "piece": null,
        "resolution": "visual_preferred",
        "reviewer_status": "agree",
        "reviewer_notes": "Confirmed: Walnut St is clearly visible at endpoint, BU 81D shield is 150m away",
        "reviewer_corrected_limit": null,
        "reviewer_corrected_alias": null,
        "reviewer_county_boundary_at_endpoint": null
      },
      ...
    ]
  }
  ```
- This JSON is what gets passed to the Orchestrator in Phase 7 for processing

**Summary bar** (always visible at top):
- Total cases, reviewed count, agree/disagree/needs-discussion counts
- Filters to jump to specific resolution types

### Technical approach

The dashboard is a **single HTML file** with embedded CSS and JavaScript — no build step, no dependencies, no server. Data is embedded as a `<script>` tag with a JSON payload:

```html
<script>
const REVIEW_DATA = [
  {
    "segment": "FM 730-A",
    "side": "To",
    "piece": null,
    "heuristic_limit": "BU 81D",
    "heuristic_confidence": 0.88,
    "heuristic_label": "route_intersection",
    "visual_limit": "Walnut St",
    "visual_alias": null,
    "visual_confidence": "high",
    "visual_labels_seen": ["Walnut St", "E Walnut St", "BU 81D"],
    "visual_reasoning": "Segment line ends at Walnut St intersection",
    "resolution": "visual_preferred",
    "category": "different_road",
    "final_limit": "Walnut St",
    "final_confidence": 0.90,
    "close_screenshot": "screenshots/batch-01-ep-02-close.png",
    "context_screenshot": "screenshots/batch-01-ep-02-context.png",
    "lon": -97.489,
    "lat": 33.198
  },
  ...
];
</script>
```

Screenshots are referenced via **relative paths** (not base64) — the HTML file lives in `_temp/visual-review/` alongside the `screenshots/` directory. This keeps the HTML file small (~200KB) while supporting 600+ screenshots.

The map iframe uses the hosted web app URL. Cross-origin restrictions **will** block `contentWindow` access when the dashboard is opened from `file://` or a different origin than GitHub Pages. The dashboard must implement a fallback that displays endpoint coordinates with a copy button for manual navigation. To enable auto-navigation, serve both the dashboard and web app from the same origin (e.g., `localhost:8080`).

### Why a single HTML file

- No `npm install`, no build — just open the file for review with manual map navigation (fallback mode)
- For auto-navigation of the embedded map, serve both dashboard and web app from the same origin (e.g., serve the repo root with `python -m http.server 8080` so both `/Web-App/` and `/_temp/visual-review/review-dashboard.html` are on `localhost:8080`)
- Works offline (except the map iframe, which needs network)
- Portable — can be emailed, shared, or archived
- `localStorage` persists notes across sessions without any backend

---

## Key Files to Modify/Create

| Action | File | What |
|--------|------|------|
| Modify | `Scripts/identify_segment_limits.py` | Add coords, confidence, gap_piece_endpoints to RowProcessingResult |
| Modify | `Web-App/app.js` | Add `window.__mapView`, `window.__waitForSegments()`, and `window.__selectAndZoomSegment()` programmatic API |
| Modify | `Scripts/trusted_review_eval.py` | Stop skipping gap segments (line 83-86); evaluate overall From/To (first piece's From, last piece's To) against Amy's From/To |
| Create | `Scripts/generate_visual_review_manifest.py` | Heuristic runner + manifest generator (gap-aware) |
| Create | `Scripts/generate_visual_review_prompts.py` | Batch prompt file generator (gap-aware) |
| Create | `Scripts/reconcile_results.py` | Merge heuristic + visual results (gap-aware) |
| Create | `Scripts/generate_review_dashboard.py` | Generates review-dashboard.html from reconciliation results |
| Create | `orchestrator.md` | Master prompt — hand this to the Orchestrator Agent to drive everything |
| Create | `verification-log.md` | Persistent learning log — Orchestrator appends after each run, NEVER deleted |

---

## Verification Plan

1. **Test heuristic enhancement**: Run `generate_visual_review_manifest.py` on 5 known segments (including FM 2331 - B or FM 1189 as a gap segment), verify coordinates and gap piece data are correct
2. **Test gap evaluation**: Run `trusted_review_eval.py` with gap segments included, verify FM 2331 - B and FM 1189 are now scored. Only the overall From (first piece's From) and To (last piece's To) are compared against Amy's values — interior piece boundaries are NOT scored against Amy since she only records overall limits
3. **Test prompt generation**: Generate a batch of 4-6 endpoints including at least one gap segment, verify the prompt correctly lists piece endpoints and gap context without leaking answers
4. **Test single batch end-to-end**: Run one batch prompt via a Visual Review Agent with Playwright MCP, verify screenshots are taken and observations are structured correctly
5. **Test reconciliation**: Feed the batch results + heuristic results into `reconcile_results.py`, verify the merge logic handles both continuous and gap segment rows
6. **Validate against known segments**: Run the full pipeline on 10-15 of the trained 150 segments where we know the correct answer, compare final results to Amy's review

---

## Scale Estimates

> All counts below are approximate for the current 150-segment dataset and will vary as segments are added/removed. Actual counts are derived from the manifest at runtime.

- ~148 continuous segments x 2 endpoints = ~296 endpoints
- ~2 gap segments x ~4 endpoints each (2 pieces x 2 sides) = ~8 endpoints
- Total: ~304 endpoints (derived from manifest — do not hardcode)
- ~15 endpoints per batch = ~21 batches
- Each batch: ~15-25 min per Visual Review Agent session
- Total: ~5-8 hours of AI time (can run batches in parallel across sessions)
- With `window.__mapView.goTo()`, per-endpoint time drops from ~90s to ~25-30s

## Runtime Prerequisites

- **Network access required**: The heuristic engine (`identify_segment_limits.py`) queries live ArcGIS feature services for segment geometry, county boundaries, and roadway inventory data. The manifest generator (Task D) inherits these dependencies. Local label and roadway caches reduce but do not eliminate network calls. Implementation and testing will fail in network-restricted environments.

---

## Gap Segment Design Notes

**Current behavior** (`identify_segment_limits.py` lines 3826-3931):
- Geometry with pieces separated >= 200m → classified as `Gap`
- Each piece is oriented independently (N-to-S or W-to-E)
- Pieces sorted by cardinal direction
- Each piece gets its own `choose_candidate()` for From and To
- Overall `auto_from` = first piece's From, `auto_to` = last piece's To
- Intermediate piece endpoints recorded in `note` field only

**What this plan changes**:
- Intermediate piece endpoints are now **structured data** (via `gap_piece_endpoints`) instead of buried in a note string
- Each piece endpoint gets its own visual verification screenshot
- The reconciliation handles gap piece rows the same as continuous rows
- `trusted_review_eval.py` stops skipping gap segments — for evaluation, only the overall From (first piece's From) and To (last piece's To) are compared against Amy's values. Interior piece boundaries are visually verified but NOT scored against Amy's data since she only records overall limits

**Visual review benefit for gap segments**: Gap segments are inherently harder — the Visual Review Agent seeing the physical gap on the map can identify whether an intermediate endpoint makes sense (e.g., "the segment line stops at FM 917 and restarts 2km south").

---

## Implementation Instructions

Build this plan using the task graph below. **Use sub-agents for independent tasks to maximize parallelism.**

### Task Dependency Graph

```
    ┌─────────────────┐     ┌──────────────────┐     ┌───────────────────────┐
    │  Task A          │     │  Task B           │     │  Task C               │
    │  Modify           │     │  Modify           │     │  Modify               │
    │  identify_        │     │  app.js            │     │  trusted_review_      │
    │  segment_limits.py│     │  (3 window APIs)  │     │  eval.py              │
    │                   │     │                   │     │  (include gap segs)   │
    └────────┬──────────┘     └───────────────────┘     └───────────────────────┘
             │
             │ depends on A
             ▼
    ┌─────────────────┐
    │  Task D          │
    │  Create           │
    │  generate_visual_ │
    │  review_manifest.py│
    └────────┬──────────┘
             │
             │ depends on D
             ▼
    ┌─────────────────┐
    │  Task E          │
    │  Create           │
    │  generate_visual_ │
    │  review_prompts.py│
    └────────┬──────────┘
             │
             │ depends on D (needs heuristic-results.csv schema)
             ▼
    ┌─────────────────┐
    │  Task F          │
    │  Create           │
    │  reconcile_       │
    │  results.py       │
    └────────┬──────────┘
             │
             │ depends on E + F (needs both schemas finalized)
             ▼
    ┌─────────────────┐
    │  Task G          │
    │  Create           │
    │  orchestrator.md  │
    └─────────────────┘
             │
             │ can be parallel with G (needs F's output schema)
             ▼
    ┌─────────────────┐
    │  Task H          │
    │  Create           │
    │  generate_review_ │
    │  dashboard.py     │
    └─────────────────┘
```

### Parallel execution plan

**Wave 1** (3 sub-agents in parallel — no dependencies between them):
- **Sub-agent 1: Task A** — Modify `Scripts/identify_segment_limits.py`
  - Add `from_endpoint_wgs84`, `to_endpoint_wgs84`, `confidence_from`, `confidence_to`, `gap_piece_endpoints` to `RowProcessingResult` (line 308)
  - Thread existing `start_endpoint_wgs84`/`end_endpoint_wgs84` and `LimitCandidate.confidence` through to the result in `process_request_row()` for both continuous (line 3933+) and gap (line 3826+) paths
  - Add `confidence_bucket()` helper
  - Use 1-based piece indexing
  - **Do NOT change any heuristic logic** — only add data passthrough

- **Sub-agent 2: Task B** — Modify `Web-App/app.js`
  - Add `window.__mapView = view;` after line 121
  - Add `window.__waitForSegments()` — polls `state.segments.length` inside the `require()` closure, returns a Promise
  - Add `window.__selectAndZoomSegment(segmentName)` — finds by `s.label`, clears selection, adds objectId, calls `render()`, `syncSelectedGraphics()`, `zoomToSegments()`
  - All three must be defined inside the `require()` callback where `state`, `render`, `syncSelectedGraphics`, `zoomToSegments` are in scope

- **Sub-agent 3: Task C** — Modify `Scripts/trusted_review_eval.py`
  - Remove the gap segment skip at line 83-86
  - For gap segments, evaluate only the overall From (first piece's From) and To (last piece's To) against Amy's values — the script already outputs these as `auto_from`/`auto_to`

**Wave 2** (after Wave 1 completes — needs Task A's RowProcessingResult changes):
- **Sub-agent 4: Task D** — Create `Scripts/generate_visual_review_manifest.py`
  - Import `identify_segment_limits` as module (follow `trusted_review_eval.py` pattern, line 29-36)
  - Run heuristic pipeline on input CSV
  - Output `_temp/visual-review/heuristic-results.csv` (with Segment, Direction, Type, Side, Piece, Auto-Limit, Heuristic, Confidence, Confidence-Bucket, Lon, Lat)
  - Output `_temp/visual-review/visual-review-manifest.json` (anti-bias: coordinates + context only, NO heuristic answers)
  - Create `_temp/visual-review/` directory structure

**Wave 3** (after Wave 2 — needs manifest schema from Task D):
- **Sub-agent 5: Tasks E + F** — Create both prompt generator and reconciler
  - `Scripts/generate_visual_review_prompts.py` — reads manifest JSON, generates `_temp/visual-review/batch-prompts/batch-NN.md` files (~15 endpoints per batch). Embed the full batch prompt template from this plan (browser setup, visual-only rules, workflow, JSON output schema)
  - `Scripts/reconcile_results.py` — reads `heuristic-results.csv` + `batch-results/batch-NN-results.json` files. Implements the comparison logic table, confidence mapping (high=0.90, medium=0.70, low=0.50), disagreement categorization. Outputs both `final-segment-limits.csv` (endpoint-level) and `final-segment-limits-collapsed.csv` (one row per segment). Uses `canonical()` from `identify_segment_limits.py` for road name comparison.

**Wave 4** (after Wave 3 — needs all schemas finalized, 2 sub-agents in parallel):
- **Sub-agent 6: Task G** — Create `orchestrator.md`
  - The master prompt content is already written in Step 6 of this plan — extract it, add Phase 0 (pre-flight cleanup) through Phase 8 (cleanup), reference the correct file paths

- **Sub-agent 7: Task H** — Create `Scripts/generate_review_dashboard.py`
  - Reads `final-segment-limits.csv`, `heuristic-results.csv`, `batch-results/*.json`
  - Generates a single self-contained `_temp/visual-review/review-dashboard.html`
  - Embeds all review data as a JSON payload in a `<script>` tag
  - References screenshots via relative paths (HTML file is in `_temp/visual-review/`)
  - Includes embedded CSS + JS for: case navigation, table view, map iframe, per-case notes textarea, localStorage persistence, JSON export button
  - See Step 7 for full UI specification

### Important rules for implementation

1. **Read before writing** — read each file you're modifying before making changes
2. **Preserve existing behavior** — Task A must not change any heuristic selection logic, only add fields to the output
3. **Reuse existing code** — `canonical()`, `load_module()`, the `importlib` pattern from `trusted_review_eval.py`
4. **Test after each wave** — run `trusted_review_eval.py` after Wave 1 to verify nothing broke
5. **All paths under `_temp/visual-review/`** — see the directory structure in this plan
6. **1-based piece indexing everywhere** — Python, CSV, JSON, prompts
