# Plan 2: Reconciliation + Orchestrator + Verification Log

> **Master reference**: [`master-plan.md`](master-plan.md)
> **Depends on**: Plan 1 (Infrastructure) — needs `heuristic-results.csv` schema and `batch-NN-results.json` schema

## Scope

| Task | File | Action |
|------|------|--------|
| F | `Scripts/reconcile_results.py` | Create — merges heuristic + visual results |
| G | `orchestrator.md` | Create — master prompt for the Orchestrator Agent |
| — | `verification-log.md` | Create — persistent learning document (Orchestrator appends to it) |

## Task F: Create `Scripts/reconcile_results.py`

Reads `heuristic-results.csv` + all `batch-results/batch-NN-results.json` files. JSON output from Visual Review Agents is parsed deterministically.

### Visual confidence mapping

| Bucket | Numeric | Meaning |
|--------|---------|---------|
| `high` | 0.90 | Labels clearly readable, endpoint location unambiguous |
| `medium` | 0.70 | Labels partially readable or multiple plausible roads near endpoint |
| `low` | 0.50 | Labels unreadable, endpoint ambiguous, or low zoom quality |

### Two-phase comparison logic

| Heuristic | Visual | Resolution | Final |
|-----------|--------|------------|-------|
| Same road (canonical match) | Same road (any conf) | `confirmed` | Use either, boost confidence |
| Route number only | Visual has alias + route (any conf) | `enriched` | Use visual's richer label (e.g., "South Fwy (IH 35W)") |
| Different road | Visual high/medium conf (>= 0.70) | `visual_preferred` | Use visual answer |
| Different road | Visual low conf (< 0.70) | `conflict` | Flag for human review with both answers |
| Has offset | Visual says `is_offset: false` AND conf >= 0.70 | `visual_preferred` | Visual sees endpoint directly at road |
| Has offset | Visual says `is_offset: false` AND conf < 0.70 | `conflict` | Low-confidence, flag for review |
| No offset | Visual says `is_offset: true` AND conf >= 0.70 | `visual_preferred` | Visual sees endpoint between intersections |
| No offset | Visual says `is_offset: true` AND conf < 0.70 | `conflict` | Low-confidence, flag for review |
| Any road | Visual `county_boundary_at_endpoint: true` AND conf >= 0.70 | `visual_preferred` | County line not detected by heuristic |
| Any road | Visual `county_boundary_at_endpoint: true` AND conf < 0.70 | `conflict` | County claim at low confidence, flag for review |
| Empty | Visual has answer | `visual_only` | Use visual answer |

Road name comparison uses `canonical()` from `identify_segment_limits.py`. Also check for alias relationships (e.g., "Benbrook Blvd" ≈ "US 377").

For the `enriched` path: if `limit_alias` is non-null and the heuristic only has the route number, format as `"limit_alias (limit_identification)"`.

### Combined confidence

- `confirmed`: max(heuristic_conf, 0.92)
- `enriched`: max(heuristic_conf, 0.90)
- `visual_preferred`: visual_numeric (0.90, 0.70, or 0.50)
- `conflict`: max(heuristic_conf, visual_numeric) * 0.6
- `visual_only`: visual_numeric * 0.9

### Output 1: `_temp/visual-review/final-segment-limits.csv`

| Segment | Type | Side | Piece | Heuristic-Limit | Heuristic-Confidence | Visual-Limit | Visual-Confidence | Final-Limit | Final-Confidence | Resolution | Disagreement-Category | Visual-Labels-Seen |

`Resolution` values: `confirmed`, `enriched`, `visual_preferred`, `conflict`, `visual_only`

`Disagreement-Category` (when Resolution != confirmed): `different_road`, `alias_enrichment`, `offset_extra`, `offset_missing`, `county_not_detected`, `offset_direction`, `other`

### Output 2: `_temp/visual-review/final-segment-limits-collapsed.csv`

One row per segment:

| Segment | Direction | Type | Final-From | Final-To | From-Confidence | To-Confidence | From-Resolution | To-Resolution |

For gap segments: `Final-From` = first piece's From, `Final-To` = last piece's To.

### Gap handling

Gap segments get the same reconciliation logic per-endpoint. `Piece` column distinguishes entries. Continuous segments have `Piece` empty.

### Implementation notes

- Import `identify_segment_limits` as module to reuse `canonical()` (follow `trusted_review_eval.py` pattern)
- 1-based piece indexing everywhere
- All output paths under `_temp/visual-review/`

---

## Task G: Create `orchestrator.md`

The master prompt for the Orchestrator Agent. Content is already written in the master plan Step 6 — extract and finalize it.

Key sections the orchestrator prompt must include:

1. **What this is** — multi-agent pipeline with 3 agent types
2. **Agent context isolation** — Visual Review Agents get NO heuristic data
3. **Phase 0**: Pre-flight cleanup (check for stale `_temp/visual-review/` files)
4. **Phase 1**: Dispatch Heuristic Agent → `generate_visual_review_manifest.py`
5. **Phase 2**: Dispatch Heuristic Agent → `generate_visual_review_prompts.py`
6. **Phase 3**: Dispatch Visual Review Agents (parallel, one per batch)
7. **Phase 4**: Run `reconcile_results.py`
8. **Phase 5**: Report + append to `verification-log.md`
9. **Phase 6**: Run `generate_review_dashboard.py` (from Plan 3)
10. **Phase 7** *(Optional)*: Process human reviewer's exported notes JSON; optionally generate adjudicated CSVs via LLM
11. **Phase 8** *(Optional)*: Cleanup (screenshots, batch prompts, dashboard)

Resumability: check for existing `batch-NN-results.json` before re-spawning.

Screenshot lifecycle: do NOT delete until after human review (Phase 8), or immediately after Phase 5 if no dashboard was requested.

### Phase 7 adjudicated output (optional, on user request)

When the user provides the exported reviewer notes JSON and requests adjudicated output, the Orchestrator uses the LLM to:

1. Read `final-segment-limits.csv` and the reviewer's exported JSON
2. For each endpoint where the reviewer marked "Disagree", read the structured override fields (`reviewer_corrected_limit`, `reviewer_corrected_alias`, `reviewer_county_boundary_at_endpoint`) — these provide the reviewer's intended correction without free-text inference
3. If any "Disagree" case is missing a `reviewer_corrected_limit`, flag it and ask the user to fill in the structured field before generating output
4. Produce `_temp/visual-review/human-reviewed-segment-limits.csv` — same schema as `final-segment-limits.csv` but with overridden rows updated (Resolution set to `human_override`, Final-Limit set to `reviewer_corrected_limit`)
5. Regenerate a collapsed CSV: `_temp/visual-review/human-reviewed-segment-limits-collapsed.csv`
6. Append a "Human Review" section to `verification-log.md` summarizing which endpoints were overridden and why

This step is only performed on explicit user request. The Phase 4 outputs (`final-segment-limits.csv` and `final-segment-limits-collapsed.csv`) remain the default authoritative deliverables.

---

## Verification Log: `verification-log.md`

Create this file at the repo root. It is **NEVER deleted**.

The Orchestrator appends a timestamped section after each run:

```markdown
## Run: 2026-04-07 — 150 segments (FTW-Segments-Limits-Amy.review.csv)

### Summary
- Endpoints evaluated: {N} (derived from manifest)
- Confirmed (heuristic + visual agree): 265 (87.2%)
- Enriched (visual added alias): 8
- Visual preferred (visual overrode heuristic): 19
- Conflict (flagged for human review): 7
- Visual only: 5

### Disagreements where visual overrode heuristic

| Segment | Side | Heuristic said | Visual said | Category | Visual Conf | Heuristic Conf | Visual labels seen |
|---------|------|---------------|-------------|----------|-------------|----------------|-------------------|
| FM 730-A | To | BU 81D | Walnut St | different_road | high | 0.88 | Walnut St, E Walnut St, BU 81D shield |
...

### Conflicts (unresolved — needs human review)

| Segment | Side | Heuristic said | Visual said | Category | Notes |
...

### Patterns for heuristic improvement

- **Pattern: nearby-but-wrong road in dense areas** (7 cases)
  Segments: FM 730-A/To, SH 199-D/To, SH 10/To, ...
  Root cause: route candidates score higher than local labels at 40-120m.
  Suggested fix: increase local label weight when distance < 60m.

### Traceability
- Input CSV: FTW-Segments-Limits-Amy.review.csv
- Script version: git commit [hash]
```

---

## Testing after this plan

1. Create mock `heuristic-results.csv` and `batch-01-results.json` files with known values
2. Run `reconcile_results.py` — verify it produces both CSVs with correct resolutions
3. Test confirmed, enriched, visual_preferred, and conflict cases
4. Verify gap segment rows are handled correctly (Piece column)
5. Verify `verification-log.md` is created with the correct structure

## What Plan 3 needs from this plan

- `final-segment-limits.csv` schema (all columns)
- `heuristic-results.csv` schema (from Plan 1)
- `batch-results/batch-NN-results.json` schema (from Plan 1)
- All three are needed by the dashboard generator
