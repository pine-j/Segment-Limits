# FTW Segment Limits - Hybrid Visual Verification Pipeline

## Your task

Read this file, then execute Phases 0 through 5 below. Do not wait for
confirmation between phases — run them sequentially and report results at the
end. Do not run Phases 6–8 unless the user explicitly asks.

For full architecture, schemas, and design rationale, refer to
`Project-Plan/master-plan.md`.

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
2. **Visual pass** — Playwright MCP inspects the rendered map visually, takes
   screenshots, and produces independent endpoint assessments with confidence
   buckets.
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

## CRITICAL: Visual review independence

Visual Review sub-agents must never see heuristic answers.

- Pass each Visual Review sub-agent only its batch prompt content (from
  `_temp/visual-review/batch-prompts/batch-NN.md`) and Playwright MCP access
- Do NOT include `heuristic-results.csv` or any heuristic output in their context
- Do NOT mention heuristic findings when dispatching them

The batch prompts contain only coordinates and navigation instructions — no
heuristic answers. This ensures the visual pass is an independent check, not a
confirmation of what the heuristic already said. Context isolation makes bias
impossible, not just inadvisable.

## Workflow

### Phase 0: Pre-flight cleanup

Check whether `_temp/visual-review/` contains stale files from a previous run.

- Default behavior: log what exists, then delete `screenshots/`,
  `batch-prompts/`, and `batch-results/`
- Keep nothing from those transient directories unless the user explicitly asked
  to resume
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

### Phase 3: Dispatch Visual Review sub-agents

For each batch prompt file in `_temp/visual-review/batch-prompts/`:

1. **Check resumability**: if `_temp/visual-review/batch-results/batch-NN-results.json`
   already exists, skip that batch
2. **Spawn a sub-agent** with:
   - The batch prompt file content as its task (contains endpoint table,
     workflow, and JSON output schema)
   - Playwright MCP access
   - **Nothing else** — no heuristic files, no CSVs, no data files
3. Each sub-agent will:
   - Open `https://pine-j.github.io/Roadway-Segment-Limits/`
   - Run `await window.__waitForSegments()` to confirm the app is loaded
   - For each endpoint: select segment, navigate to coordinates, take close
     and context screenshots, record visual assessment
   - Write structured JSON to
     `_temp/visual-review/batch-results/batch-NN-results.json`

**Run sub-agents in waves of 3–5 batches at a time**, not all at once. Each
sub-agent needs its own Playwright browser session, navigates to multiple
endpoints, takes screenshots, and records assessments — that is a lot of
context and runtime per batch. Spawning every remaining batch in parallel will
likely hit resource limits (browser sessions, memory, rate limits). Complete
one wave, verify the outputs, then start the next.

Each sub-agent has its own isolated context and cannot see the other batches'
results or any heuristic data.

### Phase 3b: Spot-check visual results

After each wave of visual review sub-agents completes, spot-check the new
batch result files before starting the next wave. Do NOT skip this step.

**Known failure modes to watch for** (discovered in earlier runs):

- **Blank/blue screenshots**: basemap tiles sometimes fail to render. The
  batch prompt now instructs sub-agents to detect and retry blank
  screenshots before recording results. If a blank screenshot still slips
  through (visible in the contact sheet or during step 5 review), the
  batch must be re-run for affected endpoints.
- **GAP segment selection**: GAP segments use unsuffixed names (e.g.,
  `SH 121`) but the web app only has suffixed variants (`SH 121 - A`,
  `SH 121 - B`, `SH 121 - C`). The batch prompt now includes a
  "GAP segment selection" section telling the agent to select all corridor
  segments. If you see a GAP endpoint screenshot without a teal highlight,
  the agent ignored this instruction — re-run the batch.
- **Data leakage via popups**: if a screenshot shows a feature popup open
  (showing TxDOT attributes), the visual independence is compromised. The
  batch prompt explicitly forbids clicking roadway lines. Flag these
  endpoints and re-run them.

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
   - **Inconclusive — request new screenshot** — if the existing
     screenshots are not clear enough to make a determination (labels
     too small, tiles didn't load, wrong zoom level, endpoint is at the
     edge of frame), the Orchestrator should request a fresh screenshot
     before giving up. To do this:

     1. Decide what would help: zoom in closer (zoom 18–19) to read
        small labels, zoom out further (zoom 13–14) to see surrounding
        context and county lines, or pan to re-center the endpoint.
     2. Spawn a single Playwright sub-agent with these instructions:
        - Navigate to the web app and run `__waitForSegments()`
        - Select the segment: `__selectAndZoomSegment("SEGMENT_NAME")`
        - Go to the specific coordinates at the requested zoom level
        - Take a screenshot and save it as
          `_temp/visual-review/screenshots/batch-NN-ep-MM-rescan.png`
        - The sub-agent needs only coordinates and zoom — do NOT pass
          heuristic or visual results to it
     3. Review the new screenshot and make a determination. If still
        inconclusive after the rescan, set `visual_confidence` to
        `"low"` so reconciliation routes it to `conflict` for human
        review.

     Common situations that call for a rescan:
     - Labels are rendered too small at zoom 17 — try zoom 19
     - County boundary line might be just outside the frame — try
       zoom 14 to see the wider area
     - Dense interchange with overlapping labels — try zoom 19 and
       also a panned view slightly off-center
     - Tiles failed to render (blank or grey areas) — retry at the
       same zoom level

   This step is what makes the pipeline reliable. The visual sub-agents
   operate without heuristic context, so they can miss things the
   heuristic caught. The Orchestrator has the full picture and the
   screenshots — use both.

6. **Also spot-check a sample of agreements** — for ~10% of endpoints
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
- `verification-log.md`
- `Web-App/`
- `Project-Plan/master-plan.md`
