# Plan 1: Infrastructure — Heuristic Enhancement + Manifest + Prompts

> **Master reference**: [`master-plan.md`](master-plan.md) — read this first for full context on the multi-agent architecture, agent roles, data flow, and design decisions.

## Scope

This plan covers Steps 1–4 and the batch prompt template (Step 3) from the master plan:

| Task | File | Action |
|------|------|--------|
| A | `Scripts/identify_segment_limits.py` | Modify — add coords, confidence, gap data to output |
| B | `Web-App/app.js` | Modify — add 3 programmatic APIs for Playwright |
| C | `Scripts/trusted_review_eval.py` | Modify — include gap segments in evaluation |
| D | `Scripts/generate_visual_review_manifest.py` | Create — heuristic runner + manifest generator |
| E | `Scripts/generate_visual_review_prompts.py` | Create — batch prompt file generator |

## Dependencies

- **Plan dependencies**: None — this is the foundation. Plans 2 and 3 depend on this.
- **Runtime**: Network access to ArcGIS feature services is required. The manifest generator (Task D) imports `identify_segment_limits.py`, which queries live services for segment geometry, county boundaries, and roadway inventory. Local label and roadway caches reduce but do not eliminate network calls.

## Parallel execution

**Wave 1** (3 sub-agents in parallel — no dependencies between them):

### Sub-agent 1: Task A — Modify `Scripts/identify_segment_limits.py`

Add endpoint coordinates and confidence to `RowProcessingResult` (line 308):

```python
@dataclass(frozen=True)
class RowProcessingResult:
    # ... existing fields ...
    from_endpoint_wgs84: tuple[float, float] | None = None  # (lon, lat)
    to_endpoint_wgs84: tuple[float, float] | None = None
    confidence_from: float = 0.0
    confidence_to: float = 0.0
    gap_piece_endpoints: list[dict] | None = None
```

For **continuous segments** (line 3933+): `start_endpoint_wgs84` / `end_endpoint_wgs84` and `from_candidate.confidence` / `to_candidate.confidence` are already computed — thread them into the result.

For **gap segments** (line 3826+): populate `gap_piece_endpoints` with per-piece data:

**Convention: piece indexing is 1-based everywhere.**

```python
gap_piece_endpoints = [
    {
        "piece": 1,
        "from_wgs84": (lon, lat),
        "to_wgs84": (lon, lat),
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

The overall `from_endpoint_wgs84` / `to_endpoint_wgs84` still hold the first piece's From and last piece's To.

Add confidence bucket helper:

```python
def confidence_bucket(score: float) -> str:
    if score >= 0.90: return "high"
    if score >= 0.78: return "medium"
    return "low"
```

**Do NOT change any heuristic logic** — only add data passthrough.

### Sub-agent 2: Task B — Modify `Web-App/app.js`

Add three APIs inside the `require()` closure where `state`, `render`, `syncSelectedGraphics`, `zoomToSegments` are in scope:

```javascript
// After line 121 (view creation)
window.__mapView = view;

// After syncSelectedGraphics and zoomToSegments are defined (~line 560)

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
  const match = state.segments.find(s => s.label === segmentName);
  if (!match) return false;
  state.selectedSegmentIds.clear();
  state.selectedSegmentIds.add(match.objectId);
  render();
  await syncSelectedGraphics();
  await zoomToSegments([match.objectId]);
  return true;
};
```

Key details:
- `state.segments` stores `Readable_SegID` as `label` (app.js line 725)
- `render()` re-renders the sidebar with selection (app.js line 332)
- Selected segments appear as **thick teal** `[11, 107, 122]` line; unselected are thinner **maroon** `[158, 50, 82]`

### Sub-agent 3: Task C — Modify `Scripts/trusted_review_eval.py`

- Remove the gap segment skip at line 83-86
- For gap segments, evaluate only the overall From (first piece's From) and To (last piece's To) against Amy's values — the script already outputs these as `auto_from`/`auto_to`
- Interior piece boundaries are NOT scored against Amy since she only records overall limits

---

**Wave 2** (after Wave 1 — needs Task A's RowProcessingResult changes):

### Sub-agent 4: Task D — Create `Scripts/generate_visual_review_manifest.py`

Import `identify_segment_limits` as module (follow `trusted_review_eval.py` pattern, line 29-36).

**Inputs**: A CSV with a column containing segment names (e.g., Amy's review sheet `FTW-Segments-Limits-Amy.review.csv` with its `Segment` column, or a simple one-column CSV of segment names). The script auto-detects the segment name column. Pass `--all` to run on all segments in the ArcGIS layer instead of a CSV subset.

**Outputs**:

#### a) `_temp/visual-review/heuristic-results.csv`

For continuous segments — one row per side:

| Segment | Direction | Type | Side | Auto-Limit | Heuristic | Confidence | Confidence-Bucket | Lon | Lat |

For gap segments — one row per piece endpoint:

| Segment | Direction | Type | Side | Piece | Auto-Limit | Heuristic | Confidence | Confidence-Bucket | Lon | Lat |

#### b) `_temp/visual-review/visual-review-manifest.json`

Anti-bias firewall — coordinates only, NO heuristic answers:

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
    "endpoint_hint": "start of the teal segment line"
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

Creates the `_temp/visual-review/` directory structure.

---

**Wave 3** (after Wave 2 — needs manifest schema from Task D):

### Sub-agent 5: Task E — Create `Scripts/generate_visual_review_prompts.py`

Reads `visual-review-manifest.json`, generates `_temp/visual-review/batch-prompts/batch-NN.md` files (~15 endpoints per batch).

Each prompt embeds the full batch template from the master plan (Step 3):
- Visual-only assessment rules (no popups, no API, no data files)
- Web app URL (primary + local fallback)
- Browser setup with `__waitForSegments()`
- Per-endpoint workflow with `__selectAndZoomSegment()` and `__mapView.goTo()`
- What to look for (labels, shields, aliases, offsets, county boundaries, gap segments)
- JSON output schema with all fields (`limit_identification`, `limit_alias`, `is_offset`, `county_boundary_at_endpoint`, etc.)

See the master plan Step 3 for the complete template.

---

## Testing after this plan

1. Run `trusted_review_eval.py` — verify nothing broke and gap segments are now scored
2. Run `generate_visual_review_manifest.py` on 5 known segments (including a gap segment) — verify coordinates and confidence data are correct
3. Run `generate_visual_review_prompts.py` — verify batch files are generated with correct coordinates and no leaked heuristic answers
4. Inspect a generated batch prompt — verify it contains the visual-only rules, JSON output schema, and endpoint table

## What Plan 2 needs from this plan

- `heuristic-results.csv` schema (column names + types)
- `batch-NN-results.json` schema (the JSON output format from batch prompts)
- Both are defined in the master plan and implemented here
