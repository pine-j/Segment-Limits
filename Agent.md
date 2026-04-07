# Project: Roadway Segment Limits

## Segment-Limits Workflow

- Use `Scripts/identify_segment_limits.py` for deterministic segment-endpoint inference. It is data-driven and combines FTW segmentation geometry, county boundaries, TxDOT vector tile labels, and TxDOT Roadway Inventory geometry.
- Treat `FTW-Segments-Limits-Amy.review.csv` as the current working review sheet. It carries Amy's `Limts-From` / `Limits-To` values plus segment metadata.
- Use `Scripts/trusted_review_eval.py` to score the current heuristics against Amy's review sheet.
- Prefer the local caches in `FTW-TxDOT-Labels/` and `FTW-Roadway-Inventory/`. Only use `--download-label-tiles`, `--download-roadway-inventory-subset`, `--live-label-tiles`, or `--live-roadway-inventory` when refreshing or explicitly validating against live services.
- For targeted reruns, use `--segment-name`, `--limit`, and `--workers`. The script's built-in `--compare-csv` default points at `FTW-Segments-Limits-Amy.review.csv`.
- Use `Web-App/` and the hosted app at `https://pine-j.github.io/Roadway-Segment-Limits/` for manual inspection, roadway clicks, and Playwright-assisted map review. Serve the local app from the repo root with `python -m http.server 8080` and open `/Web-App/` when local inspection is needed.
- Treat the web app and Playwright review as the primary evidence source during manual adjudication; use `Scripts/identify_segment_limits.py` and `SEGMENT_LIMITS_LOGIC.md` as the current heuristic framework, not as the final source of truth.
- `SEGMENT_LIMITS_LOGIC.md` is the authoritative explanation of the current endpoint heuristics. Consult it before changing orientation logic, route-vs-local selection, frontage-road handling, or comparison rules.
- `SEGMENT_LIMITS_CASE_STUDY.md` contains project background, workflow evolution, and rationale.

## Hybrid Visual Verification Pipeline

- The master plan and sub-plans are in `Project-Plan/`:
  - `Project-Plan/master-plan.md` - Full architecture, data schemas, agent roles, reconciliation logic, dashboard spec
  - `Project-Plan/plan-1-infrastructure.md` - Heuristic enhancement + manifest + prompt generation (Tasks A-E)
  - `Project-Plan/plan-2-reconciliation-orchestrator.md` - Reconciliation + orchestrator + verification log (Tasks F-G)
  - `Project-Plan/plan-3-review-dashboard.md` - Human review dashboard (Task H)
- `verification-log.md` (once created) is the persistent learning document - NEVER delete it.

## Temporary Artifact Cleanup

- Put temporary artifacts under `_temp/` rather than in project output folders.
- Do not treat cached working data such as `FTW-TxDOT-Labels/` or `FTW-Roadway-Inventory/` as disposable temp artifacts.
- Once a test, QA pass, or debugging session is complete, delete the temporary artifacts before wrapping up the task.
- Do not leave behind ad hoc files such as `*.check.csv`, scratch Python scripts, or one-off comparison exports unless the user explicitly asks to keep them.

## Project Structure

- `Scripts/identify_segment_limits.py` - ArcGIS/TxDOT-driven segment limit inference script and heuristic engine
- `Scripts/trusted_review_eval.py` - Accuracy/evaluation harness for comparing current heuristics against Amy's review sheet
- `SEGMENT_LIMITS_LOGIC.md` - Detailed explanation of the current segment endpoint heuristics, candidate selection rules, and confidence model
- `SEGMENT_LIMITS_CASE_STUDY.md` - Narrative background, workflow evolution, and rationale
- `FTW-Segments-Limits-Amy.review.csv` - Current working review sheet with Amy's From/To values and segment metadata
- `FTW-TxDOT-Labels/` - Local TxDOT label vector tile cache plus `.missing` marker files
- `FTW-Roadway-Inventory/` - Local TxDOT roadway inventory subset used by the limit script
- `Web-App/` - Static ArcGIS explorer for segment inspection and roadway-name click queries
- `Project-Plan/` - Hybrid visual verification pipeline plans (master + 3 sub-plans)
- `_temp/` - Conventional scratch location for evaluator outputs and visual-review artifacts; create as needed

## Tech Stack

- Python 3.13
- GIS / spatial analysis: geopandas, shapely, requests, pandas, pyproj, mercantile, mapbox-vector-tile (ArcGIS REST API + vector tiles)
