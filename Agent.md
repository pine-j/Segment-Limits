# Project: Roadway Segment Limits

## Core workflow

- Use [Scripts/identify_segment_limits.py](Scripts/identify_segment_limits.py)
  for deterministic segment-endpoint inference. It is data-driven and combines
  FTW segmentation geometry, county boundaries, TxDOT vector tile labels, and
  TxDOT Roadway Inventory geometry.
- Treat
  [FTW-Segments-Limits-Amy.review.csv](FTW-Segments-Limits-Amy.review.csv) as
  the working review sheet for From/To values plus segment metadata.
- Use [Scripts/trusted_review_eval.py](Scripts/trusted_review_eval.py) to score
  current heuristics against Amy's review sheet.
- Prefer the local caches in `FTW-TxDOT-Labels/` and
  `FTW-Roadway-Inventory/`. Only use `--download-label-tiles`,
  `--download-roadway-inventory-subset`, `--live-label-tiles`, or
  `--live-roadway-inventory` when refreshing caches or explicitly validating
  against live services.
- For targeted reruns, use `--segment-name`, `--limit`, and `--workers`.

## Hybrid visual verification pipeline

- The current pipeline is documented in:
  - [Project-Plan/master-plan.md](Project-Plan/master-plan.md)
  - [Project-Plan/plan-1-infrastructure.md](Project-Plan/plan-1-infrastructure.md)
  - [Project-Plan/plan-2-reconciliation-orchestrator.md](Project-Plan/plan-2-reconciliation-orchestrator.md)
  - [Project-Plan/plan-3-review-dashboard.md](Project-Plan/plan-3-review-dashboard.md)
- The current orchestrator prompt lives in [orchestrator.md](orchestrator.md).
- The pipeline stages are:
  1. Generate heuristic endpoint results and anti-bias manifest with
     [Scripts/generate_visual_review_manifest.py](Scripts/generate_visual_review_manifest.py)
  2. Generate visual-review batch prompts with
     [Scripts/generate_visual_review_prompts.py](Scripts/generate_visual_review_prompts.py)
  3. Run Visual Review Agents in parallel against the rendered map
  4. Merge heuristic and visual outputs with
     [Scripts/reconcile_results.py](Scripts/reconcile_results.py)
  5. Append run learnings to [verification-log.md](verification-log.md)
  6. Optionally generate the review dashboard from Plan 3

## Agent roles and boundaries

- Heuristic work is deterministic and script-driven. Use the Python scripts and
  existing data sources first.
- Visual Review Agents must be independent of heuristic answers.
- Visual Review Agents get only manifest-derived batch prompt content and the
  web app, not `heuristic-results.csv`.
- Visual Review Agents are visual-only:
  - do not read heuristic CSV or JSON files
  - do not inspect API responses, network traffic, or GeoJSON
  - do not click roadway popups to read the same TxDOT service data the
    heuristic already uses
  - use only rendered basemap labels, route shields, county lines, and the teal
    selected segment line
- Manual human review in the web app can still use clicks and broader inspection
  when needed. The restriction above is specifically for the independent visual
  agent pass.

## Visual-review artifacts

- All visual-review intermediates live under `_temp/visual-review/`.
- Key files:
  - `_temp/visual-review/heuristic-results.csv`
  - `_temp/visual-review/visual-review-manifest.json`
  - `_temp/visual-review/batch-prompts/`
  - `_temp/visual-review/batch-results/`
  - `_temp/visual-review/screenshots/`
  - `_temp/visual-review/final-segment-limits.csv`
  - `_temp/visual-review/final-segment-limits-collapsed.csv`
- `verification-log.md` is persistent and must never be deleted.
- Screenshots must be kept until human review is finished if a dashboard is in
  use, because the dashboard references them by relative path.

## Gap segments

- Gap segments are first-class citizens in the pipeline.
- Piece indexing is 1-based everywhere.
- The heuristic engine emits structured `gap_piece_endpoints`.
- Manifest, prompt, visual JSON, and reconciled outputs all preserve piece-level
  endpoint rows.
- Collapsed final outputs still use first piece `From` and last piece `To` as
  the segment-level limits.

## Web app usage

- Use `Web-App/` and the hosted app at
  `https://pine-j.github.io/Roadway-Segment-Limits/` for manual inspection and
  visual-review execution.
- When running locally, serve the repo root with
  `python -m http.server 8080` and open `/Web-App/`.
- The programmatic browser hooks for visual review are:
  - `window.__waitForSegments()`
  - `window.__selectAndZoomSegment(segmentName)`
  - `window.__mapView.goTo(...)`

## Documentation

- [SEGMENT_LIMITS_LOGIC.md](SEGMENT_LIMITS_LOGIC.md) is the authoritative
  explanation of the current heuristic engine, confidence model, gap handling,
  and hybrid pipeline integration.
- [SEGMENT_LIMITS_CASE_STUDY.md](SEGMENT_LIMITS_CASE_STUDY.md) contains the
  project narrative and historical rationale.
- When changing orchestration or pipeline behavior, keep
  [orchestrator.md](orchestrator.md) and the relevant plan files in sync.

## Cleanup

- Put temporary artifacts under `_temp/`.
- Do not treat `FTW-TxDOT-Labels/` or `FTW-Roadway-Inventory/` as disposable
  temp data.
- Delete ad hoc scratch artifacts before finishing unless the user explicitly
  asks to keep them.
- For the visual-review pipeline, follow the staged cleanup rules in
  [orchestrator.md](orchestrator.md) instead of deleting everything
  immediately.

## Project structure

- [Scripts/identify_segment_limits.py](Scripts/identify_segment_limits.py) -
  heuristic engine
- [Scripts/trusted_review_eval.py](Scripts/trusted_review_eval.py) -
  evaluation harness
- [Scripts/generate_visual_review_manifest.py](Scripts/generate_visual_review_manifest.py) -
  heuristic results + anti-bias manifest generator
- [Scripts/generate_visual_review_prompts.py](Scripts/generate_visual_review_prompts.py) -
  visual batch prompt generator
- [Scripts/reconcile_results.py](Scripts/reconcile_results.py) -
  heuristic/visual reconciliation
- [orchestrator.md](orchestrator.md) - orchestrator agent prompt
- [verification-log.md](verification-log.md) - persistent run log
- [SEGMENT_LIMITS_LOGIC.md](SEGMENT_LIMITS_LOGIC.md) - technical logic doc
- [SEGMENT_LIMITS_CASE_STUDY.md](SEGMENT_LIMITS_CASE_STUDY.md) - case study
- [Project-Plan/](Project-Plan/) - master plan plus sub-plans
- [Web-App/](Web-App/) - inspection and review surface
- [_temp/](_temp/) - scratch and pipeline artifacts

## Tech stack

- Python 3.13
- GIS / spatial analysis: geopandas, shapely, requests, pandas, pyproj,
  mercantile, mapbox-vector-tile
- ArcGIS REST services and vector tiles
