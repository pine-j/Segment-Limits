# FTW Segment Limits - Hybrid Visual Verification Pipeline

## Your task

Read this file, then execute Phases 0 through 5 below. Do not wait for
confirmation between phases — run them sequentially and report results at the
end. Do not run Phases 6–8 unless the user explicitly asks.

For full architecture, schemas, and design rationale, refer to
`Project-Plan/master-plan.md`.

**Do NOT read files in `Project-Plan/archive/`** — those are outdated
design-phase documents that describe a different architecture (e.g.,
Playwright MCP for screenshots). They exist only for case study reference.
This file and `Project-Plan/master-plan.md` are the current sources of truth.

## Input

**Default**: `FTW-Segments-Limits-Amy.review.csv` (all segments)

To run a subset, create a one-column CSV with a `Segment` header listing only
the segments you want, and pass it to Phase 1 instead. Example:

```csv
Segment
FM 730 - A
SH 199 - D
FM 2331 - B
```

Save it as (for example) `_temp/visual-review-test-segments.csv` and the
Phase 1 command becomes:

```bash
python Scripts/generate_visual_review_manifest.py --input _temp/visual-review-test-segments.csv
```

If no input override is provided, the default CSV is used.

## What this is

You are the Orchestrator for a pipeline that identifies highway segment limits
(endpoints) in the Fort Worth transportation network. The pipeline has two
independent passes:

1. **Heuristic pass** — Python scripts analyze ArcGIS geometry, TxDOT vector
   tile labels, county boundaries, and roadway inventory data to identify
   endpoint limits with confidence scores.
2. **Visual pass** — `batch-screenshots.py` captures map screenshots, then
   sub-agents analyze the screenshots independently and produce endpoint
   assessments with confidence buckets.
3. **Reconciliation** — you merge both passes, categorize disagreements, produce
   final CSVs, and append persistent run learnings.

## Why two passes

The heuristic script is strong but not complete. Known failure modes include:

- Nearby-but-wrong roads in dense interchanges
- Missing alias or local names shown visually on the map
- Incorrect offset phrasing
- Missed county boundaries
- Missed offset situations

The visual pass reads the rendered map, which exposes clues the data-driven
heuristic cannot reliably infer.

## Segment types and gap handling

The input CSV contains two kinds of segment entries. Every script in the
pipeline must handle both.

### 1. Individual segments (suffixed): e.g., "IH 20 - B"

These have a letter suffix and match exactly one feature in the ArcGIS layer.
The heuristic script queries the feature geometry directly.

- **No gap in geometry**: reported as `Continuous` with From/To limits at the
  extremities
- **Gap in geometry** (pieces separated by ≥200m after merging connected parts
  within 50m): reported as `Gap` with per-piece limits. Each piece gets its
  own From/To. Small artifact fragments (<600m and <5% of total length) are
  dropped.

### 2. Corridor segments (unsuffixed): e.g., "SH 360"

These have no letter suffix. They represent the entire corridor, which may
contain multiple sub-segments in the ArcGIS layer (e.g., "SH 360 - A",
"SH 360 - B", "SH 360 - C").

The heuristic script resolves these via route family fallback
(`resolve_row_features()` in `identify_segment_limits.py`):

1. No exact match for "SH 360" in ArcGIS → looks up all features in the
   "SH 360" family → gets A, B, C sub-segments
2. Chains them into a single oriented geometry
   (`orient_feature_sequence()`)
3. Merges contiguous sub-segments (gap <50m between endpoints)
4. **If all sub-segments are contiguous** → dissolves into one mega-segment →
   reported as `Continuous` with limits at the corridor extremities
5. **If physical gaps remain (≥200m)** → reported as `Gap` with per-piece
   limits for each contiguous leg. Limits are identified independently at
   each piece's From and To endpoints.

**Screenshot handling**: `batch-screenshots.py` uses
`__selectCorridorSegments()` which tries exact match first, then selects all
sub-segments in the family so the full corridor is highlighted in teal.

**Reconciliation**: corridor entries keep the corridor name (e.g., "SH 360")
throughout — matching happens on `(segment_name, side, piece)`, not on
individual sub-segment names.

### Gap detection thresholds (identify_segment_limits.py)

| Constant | Value | Purpose |
|----------|-------|---------|
| `GAP_MERGE_THRESHOLD_M` | 50m | Merge connected parts closer than this |
| `GAP_THRESHOLD_M` | 200m | Real gap — pieces separated by at least this |
| `MIN_PIECE_LENGTH_M` | 600m | Drop artifact fragments shorter than this |
| `MIN_PIECE_RATIO` | 5% | Drop artifacts smaller than 5% of total length |

## HARD REQUIREMENTS — violations invalidate the entire run

### 1. Screenshots must be real captures from the web app

Phase 3a uses `batch-screenshots.py` (Playwright library + ArcGIS native
`MapView.takeScreenshot()`) to capture all endpoint screenshots. If the script
fails or produces blank/missing screenshots:

- **STOP the pipeline immediately.**
- Report the exact error to the user.
- Do NOT generate visual review results without real screenshots on disk.
  Results produced without actual map screenshots are fabricated data and will
  corrupt the pipeline.
- Fix the issue and re-run the script before proceeding to Phase 3b.

### 2. Every visual result must have real screenshots

Each endpoint in a batch result JSON must have corresponding screenshot files
on disk (`_temp/visual-review/screenshots/batch-NN-ep-MM-close.png` and
`-context.png`). If a sub-agent produces a results JSON but the screenshot
files do not exist, that batch is invalid — delete the results and re-run it.

### 3. Visual review independence

Visual Review sub-agents must never see heuristic answers.

- Pass each Visual Review sub-agent only its batch prompt content (from
  `_temp/visual-review/batch-prompts/batch-NN.md`) and Playwright MCP access
- Do NOT include `heuristic-results.csv` or any heuristic output in their context
- Do NOT mention heuristic findings when dispatching them
- Do NOT read heuristic files during Phase 3, even as the orchestrator

The batch prompts contain only coordinates and navigation instructions — no
heuristic answers. This ensures the visual pass is an independent check, not a
confirmation of what the heuristic already said. Context isolation makes bias
impossible, not just inadvisable.

### 4. No fabrication

If any phase fails for any reason, stop and report. Do not generate synthetic
or placeholder data to keep the pipeline moving. Every data file in this
pipeline must come from either a Python script execution or real map
screenshots captured by `batch-screenshots.py` — never from the LLM inventing
plausible values.

## Workflow

### Phase 0: Pre-flight cleanup

Check whether `_temp/visual-review/` contains stale files from a previous run.

- Default behavior: log what exists, then delete `screenshots/`,
  `batch-prompts/`, `batch-results/`, `contact-sheets/`, and
  `automation/` (if present from a prior Codex run)
- Keep nothing from those transient directories unless the user explicitly asked
  to resume
- **If deletion fails** (sandbox restrictions, permission errors, locked
  files): **STOP immediately**. Report exactly which directories could not
  be cleaned and ask the user to delete them manually. Do NOT proceed to
  Phase 1 with stale files — the pipeline will produce unreliable results.
  Resume only after the user confirms cleanup is done.
- If the user passed `--resume`, skip cleanup and resume from existing files
- On resume, check which `batch-NN-results.json` files already exist and do not
  respawn completed visual batches

### Phase 1: Dispatch Heuristic Agent

Run:

```bash
python Scripts/generate_visual_review_manifest.py --input <input-csv>
```

Replace `<input-csv>` with the path from the Input section above (default:
`FTW-Segments-Limits-Amy.review.csv`).

Outputs:

- `_temp/visual-review/heuristic-results.csv`
- `_temp/visual-review/visual-review-manifest.json`

The first file is private to the Orchestrator. The manifest is the anti-bias
firewall shared with Visual Review Agents.

### Phase 2: Dispatch Heuristic Agent (prompt generation)

Run:

```bash
python Scripts/generate_visual_review_prompts.py
```

This creates `_temp/visual-review/batch-prompts/batch-01.md` through
`batch-NN.md`.

### Phase 3a: Capture screenshots

Run `batch-screenshots.py` to capture all endpoint screenshots at once. This
uses Playwright as a Python library (not MCP) and ArcGIS native
`MapView.takeScreenshot()` for fast, reliable capture.

```bash
# Option A: Use GitHub Pages (deployed app)
python batch-screenshots.py

# Option B: Use local server (if app.js changes haven't been deployed yet)
cd Web-App && python -m http.server 8080 &
cd .. && python batch-screenshots.py --local
```

Key flags:
- `--overwrite` — re-capture all screenshots (default: skip existing)
- `--start-batch N` / `--end-batch N` — capture a specific batch range
- `--headless` — run without visible browser (default: headed for tile rendering)

**Verify after capture:**
- Check the script output for errors (segment not found, etc.)
- Confirm screenshot count matches endpoint count (104 endpoints = 208 PNGs)
- Spot-check a few screenshots to verify tiles loaded and segments are
  highlighted

If any screenshots are missing or blank, re-run with `--overwrite` for the
affected batch range.

### Phase 3b: Dispatch Visual Analysis sub-agents

Phase 3b is iterative: dispatch agents, collect results, recapture any bad
screenshots, re-analyze — repeat until every endpoint has a clean result.

#### Initial dispatch

For each batch prompt file in `_temp/visual-review/batch-prompts/`:

1. **Check resumability**: if `_temp/visual-review/batch-results/batch-NN-results.json`
   already exists, skip that batch
2. **Verify screenshot prerequisites**: confirm that all screenshot files
   referenced in the batch prompt exist on disk and are non-empty. If any are
   missing, re-run `batch-screenshots.py` for that batch range before
   dispatching the sub-agent.
3. **Spawn a sub-agent** with:
   - The batch prompt file content as its task (contains endpoint table with
     screenshot file paths, assessment criteria, and JSON output schema)
   - Read tool access (to view the screenshot image files)
   - **Nothing else** — no heuristic files, no CSVs, no data files, no
     Playwright MCP
4. Each sub-agent will:
   - For each endpoint: read the close and context screenshot files using the
     Read tool (which displays images visually)
   - Assess each endpoint based on visible road labels, route shields, county
     boundaries, and segment highlight position
   - If a screenshot is unusable (blank, no segment highlight, labels
     unreadable), the agent must still write a result entry but set
     `"needs_rescan": true` and `visual_confidence: "low"` with reasoning
     explaining the specific issue (e.g., "close screenshot is blank — tiles
     failed to render", "labels too small to read at zoom 17")
   - Write structured JSON to
     `_temp/visual-review/batch-results/batch-NN-results.json`

**Run sub-agents in waves of 3–5 batches at a time**, not all at once. Each
sub-agent reads 15 endpoint pairs of screenshots (30 images) and produces
detailed assessments — this uses significant context. Complete one wave, run
the recapture loop, then start the next.

Each sub-agent has its own isolated context and cannot see the other batches'
results or any heuristic data.

#### Recapture loop (after each wave)

After each wave of sub-agents completes:

1. **Validate results**: check that each batch JSON exists, has the correct
   endpoint count, and all referenced screenshots are on disk.
2. **Collect rescan requests**: scan each batch result for entries with
   `"needs_rescan": true`. Group them by batch number.
3. **If no rescans needed**: proceed to the next wave (or Phase 3c if all
   waves are done).
4. **If rescans are needed**: for each flagged endpoint, determine what would
   help based on the agent's reasoning:
   - "blank screenshot" / "tiles failed" → recapture at same zoom
   - "labels too small" → recapture with `--close-zoom 19`
   - "no segment highlight" → recapture with `--local` (corridor support)
   - "county boundary just outside frame" → recapture with
     `--context-zoom 13`

   Run `batch-screenshots.py` with the appropriate flags for the affected
   batch range:
   ```bash
   python batch-screenshots.py --start-batch N --end-batch N --overwrite \
     --close-zoom 19 --context-zoom 14
   ```

5. **Re-analyze flagged endpoints**: delete the affected batch results JSON
   and re-dispatch the sub-agent for that batch. The agent will now see the
   fresh screenshots.
6. **Repeat** up to 2 times per endpoint. If an endpoint still has
   `needs_rescan: true` after 2 recaptures, accept the low-confidence result
   and let it flow to reconciliation as a `conflict` for human review.

This loop ensures that only clean, readable screenshots produce visual
results. Bad screenshots are fixed at the source, not patched downstream.

### Phase 3c: Spot-check visual results

After each wave of visual analysis sub-agents completes, spot-check the new
batch result files before starting the next wave. Do NOT skip this step.

**Known failure modes to watch for** (discovered in earlier runs):

- **Blank/blue screenshots**: basemap tiles sometimes fail to render during
  capture. The batch prompt instructs analysis agents to flag these as
  low-confidence. If multiple endpoints in a batch have blank screenshots,
  re-run `batch-screenshots.py --overwrite --start-batch N --end-batch N`
  to recapture, then re-run the analysis.
- **Missing teal highlight**: corridor-level segments (e.g., `SH 360`,
  `FM 1189`) require selecting all sub-segments. The `batch-screenshots.py`
  script handles this via `__selectCorridorSegments`. If a screenshot lacks
  the teal highlight, re-run the capture with `--local` flag (uses updated
  app.js with corridor support).
- **Fabricated observations**: the analysis agent may describe labels or
  shields that aren't actually visible in the screenshot. Cross-reference
  `visible_labels` and `visible_shields` against what you can see in the
  screenshots during disagreement review.

For each new `batch-NN-results.json`:

1. **Schema check** — verify every entry has all required fields
   (`endpoint_id`, `segment`, `side`, `piece`, `close_screenshot`,
   `context_screenshot`, `visible_labels`, `visible_shields`,
   `county_boundary_at_endpoint`, `limit_identification`, `limit_alias`,
   `is_offset`, `offset_direction`, `offset_from`, `visual_confidence`,
   `reasoning`) and that types are correct (arrays for labels/shields, boolean
   for county boundary and is_offset, string for confidence bucket).

2. **Endpoint count** — confirm the number of entries matches the number of
   rows in the corresponding `batch-NN.md` prompt table. A mismatch means the
   sub-agent skipped or duplicated endpoints.

3. **Cross-reference against heuristic** — read `heuristic-results.csv` and
   compare each visual `limit_identification` to the heuristic `Auto-Limit`
   for the same segment/side/piece. For every disagreement, flag it for
   visual verification in step 5.

4. **Reasoning quality** — skim the `reasoning` text for generic or
   copy-paste language. Each entry should reference specific visual
   observations (label text, shield numbers, boundary lines). Flag entries
   where the reasoning is vague or could apply to any endpoint.

5. **Verify every disagreement visually** — for each endpoint where the
   visual and heuristic limits differ, the Orchestrator must open the
   close and context screenshots (`_temp/visual-review/screenshots/
   batch-NN-ep-MM-close.png` and `-context.png`) and independently
   determine what road or boundary is actually at the endpoint. Check:

   - Does the `limit_identification` match what is visually readable in
     the screenshot? Look for the road label or route shield the visual
     agent cited.
   - Does the heuristic limit appear in the screenshot instead? The
     heuristic may have been right all along.
   - Is the visual agent's confidence justified? Downgrade or reject if:
     - The identified limit does not appear in `visible_labels` or
       `visible_shields` (the agent inferred rather than read it)
     - The screenshot is too zoomed out, blurry, or tiles failed to load
       (unreliable observation)
     - The reasoning is vague and doesn't cite specific labels
   - For county boundary disagreements: is a boundary line actually
     visible in the screenshot?
   - For offset disagreements: does the endpoint visually sit between
     intersections, or is it clearly at a crossing?

   After reviewing, take one of these actions on the batch result entry:

   - **Confirm visual** — the screenshot supports the visual agent's
     answer. No change needed.
   - **Correct to heuristic** — the screenshot actually shows the
     heuristic limit is right. Update `limit_identification` and
     `visual_confidence` to `"low"` in the batch result JSON, and note
     `"orchestrator_corrected": true` in the entry.
   - **Correct to a third answer** — neither was right, the screenshot
     shows something else. Update `limit_identification`, set
     `visual_confidence` to the Orchestrator's assessed confidence, and
     note `"orchestrator_corrected": true`.
   - **Inconclusive — request rescan** — if the existing screenshots
     are not clear enough to make a determination (labels too small,
     tiles didn't load, wrong zoom level, endpoint at edge of frame),
     re-capture at a different zoom before giving up:

     ```bash
     # Example: re-capture batch 02 endpoint 10 at zoom 19 for close
     python batch-screenshots.py --start-batch 2 --end-batch 2 \
       --close-zoom 19 --context-zoom 14 --overwrite
     ```

     Or for a single targeted rescan, use a one-off Playwright call:
     ```python
     from playwright.sync_api import sync_playwright
     # Navigate, select segment, goTo at desired zoom, takeScreenshot
     # Save as batch-NN-ep-MM-rescan.png
     ```

     Common situations that call for a rescan:
     - Labels too small at zoom 17 — try zoom 19
     - County boundary line just outside frame — try zoom 14
     - Dense interchange with overlapping labels — zoom 19 + panned
     - Tiles failed to render — retry at same zoom

     After recapturing, re-read the new screenshot and make a
     determination. If still inconclusive, set `visual_confidence` to
     `"low"` so reconciliation routes it to `conflict` for human review.

   This step is what makes the pipeline reliable. The visual sub-agents
   operate without heuristic context, so they can miss things the
   heuristic caught. The Orchestrator has the full picture and the
   screenshots — use both.

6. **Gap and corridor-specific checks** — for Gap segments:
   - Verify that each piece's From and To limits are different roads
     (a piece shouldn't start and end at the same intersection)
   - Check that the gap between pieces is visible in the screenshots
     (the teal line should visibly stop and restart)
   - For corridor segments (unsuffixed like "SH 360"): verify that all
     sub-segments are highlighted in the screenshots (the full corridor
     should be teal, not just one sub-segment)
   - If a corridor screenshot only shows a single sub-segment highlighted,
     the capture used the GitHub Pages version without corridor support.
     Re-capture with `--local` flag.

7. **Also spot-check a sample of agreements** — for ~10% of endpoints
   where visual and heuristic agree, open the screenshots and confirm the
   identified limit is actually visible. This catches cases where both
   passes are wrong for the same reason.

If a batch fails checks 1, 2, or 4 badly (schema errors, missing
endpoints, all-generic reasoning):

- Delete the bad `batch-NN-results.json`
- Re-run that batch in the next wave
- If the same batch fails twice, log it and move on — it will surface as a
  missing-visual-result error in Phase 4

Report a spot-check summary after each wave before proceeding, including
how many disagreements were verified and any corrections made.

### Phase 4: Reconcile

Run:

```bash
python Scripts/reconcile_results.py
```

Outputs:

- `_temp/visual-review/final-segment-limits.csv`
- `_temp/visual-review/final-segment-limits-collapsed.csv`

The endpoint-level CSV is the audit table. The collapsed CSV is the delivery
view with one row per segment.

### Phase 5: Report and append to verification log

Summarize for the user:

- Total endpoints evaluated
- Confirmed
- Enriched
- Visual preferred
- Conflict
- Visual only

Then append a new timestamped `## Run:` section to `verification-log.md` with:

- Summary counts
- Disagreements where visual overrode heuristic
- Conflict table for unresolved human-review cases
- Pattern analysis that groups recurring disagreement themes and suggests
  concrete heuristic improvements
- Traceability: input CSV, batch results path, and current git commit hash

This file is never deleted.

### Phase 6: Generate review dashboard (optional)

Only if the user requests human review, run:

```bash
python Scripts/generate_review_dashboard.py
```

Output:

- `_temp/visual-review/review-dashboard.html`

Tell the user to open the dashboard in a browser, review cases, and export the
notes JSON when finished.

### Phase 7: Process human review notes (optional, only on explicit request)

When the user provides the exported reviewer JSON:

1. Read `final-segment-limits.csv`
2. Read the exported reviewer JSON
3. For each case marked `Disagree`, use the structured override fields:
   `reviewer_corrected_limit`, `reviewer_corrected_alias`,
   `reviewer_county_boundary_at_endpoint`
4. If any disagree case is missing `reviewer_corrected_limit`, stop and ask the
   user to fill it in before generating adjudicated output
5. Produce `_temp/visual-review/human-reviewed-segment-limits.csv`
6. Produce `_temp/visual-review/human-reviewed-segment-limits-collapsed.csv`
7. Append a Human Review section to `verification-log.md` summarizing the
   overrides and rationale

This phase runs only when the user explicitly asks for adjudicated output.
Phase 4 outputs remain the default authoritative deliverables.

### Phase 8: Cleanup (optional)

After reporting, clean up only when appropriate.

- If no dashboard was requested, screenshots can be deleted after Phase 5
- If a dashboard was generated, keep screenshots until the reviewer is done,
  because the dashboard references them by relative path
- Optional cleanup targets:
  - `_temp/visual-review/screenshots/`
  - `_temp/visual-review/batch-prompts/`
  - `_temp/visual-review/review-dashboard.html`
- Keep:
  - `_temp/visual-review/heuristic-results.csv`
  - `_temp/visual-review/visual-review-manifest.json`
  - `_temp/visual-review/batch-results/`
  - `_temp/visual-review/final-segment-limits.csv`
  - `_temp/visual-review/final-segment-limits-collapsed.csv`

Never delete `verification-log.md`.

## Visual Review focus points

Visual Review Agents should explicitly inspect:

- All visible road labels and route shields near the endpoint
- Local alias names shown alongside route numbers
- Which road the teal segment line actually terminates at
- Left vs right frontage distinctions
- Offset situations where the endpoint is between intersections
- County boundary lines
- Physical discontinuities for gap segments

## Key files

- `Scripts/identify_segment_limits.py`
- `Scripts/generate_visual_review_manifest.py`
- `Scripts/generate_visual_review_prompts.py`
- `Scripts/reconcile_results.py`
- `Scripts/generate_review_dashboard.py`
- `batch-screenshots.py` — Playwright library + ArcGIS native screenshot capture
- `verification-log.md`
- `Web-App/`
- `Project-Plan/master-plan.md`
