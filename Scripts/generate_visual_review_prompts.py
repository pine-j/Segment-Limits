#!/usr/bin/env python3
"""Generate batched visual-review prompts from the manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "_temp" / "visual-review"
DEFAULT_MANIFEST_PATH = DEFAULT_OUTPUT_DIR / "visual-review-manifest.json"
DEFAULT_BATCH_SIZE = 15


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help=f"Manifest JSON path. Default: {DEFAULT_MANIFEST_PATH}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR / "batch-prompts",
        help=f"Prompt output directory. Default: {DEFAULT_OUTPUT_DIR / 'batch-prompts'}",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Endpoints per batch. Default: {DEFAULT_BATCH_SIZE}",
    )
    return parser.parse_args()


def ensure_dirs(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    (output_dir.parent / "batch-results").mkdir(parents=True, exist_ok=True)
    (output_dir.parent / "screenshots").mkdir(parents=True, exist_ok=True)


def chunked(items: list[dict[str, object]], size: int) -> list[list[dict[str, object]]]:
    if size <= 0:
        raise ValueError("batch-size must be greater than zero")
    return [items[index : index + size] for index in range(0, len(items), size)]


def fmt_coord(value: object) -> str:
    if value is None:
        return "null"
    return f"{float(value):.6f}"


def endpoint_note(entry: dict[str, object]) -> str:
    endpoint_hint = str(entry.get("endpoint_hint", "")).strip()
    direction = str(entry.get("direction", "")).strip()
    if entry.get("type") == "Gap":
        return f"GAP segment - {endpoint_hint}; {direction}".strip("; ")
    return f"Continuous segment; {endpoint_hint}; {direction}".strip("; ")


def piece_display(entry: dict[str, object]) -> str:
    if entry.get("type") != "Gap":
        return "-"
    return f"{entry.get('piece')}/{entry.get('piece_count')}"


def render_endpoints_table(batch_entries: list[dict[str, object]]) -> str:
    lines = [
        "| # | Segment | Side | Piece | Navigate to | Notes |",
        "|---|---------|------|-------|-------------|-------|",
    ]
    for endpoint_id, entry in enumerate(batch_entries, start=1):
        navigate_to = f"center: [{fmt_coord(entry.get('lon'))}, {fmt_coord(entry.get('lat'))}]"
        lines.append(
            f"| {endpoint_id} | {entry['segment']} | {entry['side']} | {piece_display(entry)} | "
            f"{navigate_to} | {endpoint_note(entry)} |"
        )
    return "\n".join(lines)


def collect_gap_corridor_instructions(batch_entries: list[dict[str, object]]) -> str:
    """Build selection instructions for GAP segments that need multiple app segments selected."""
    seen_segments: set[str] = set()
    instructions: list[str] = []
    for entry in batch_entries:
        if entry.get("type") != "Gap":
            continue
        segment_name = str(entry["segment"])
        if segment_name in seen_segments:
            continue
        seen_segments.add(segment_name)
        app_names = entry.get("app_segment_names")
        if not app_names or not isinstance(app_names, list):
            continue
        select_calls = "\n".join(
            f'   await window.__selectAndZoomSegment("{name}")'
            for name in app_names
        )
        instructions.append(
            f"- **{segment_name}** (GAP): this segment does not exist as a single "
            f"entry in the app. Select all corridor segments to highlight the full route:\n"
            f"   ```js\n{select_calls}\n   ```"
        )
    if not instructions:
        return ""
    return (
        "\n\n## GAP segment selection\n\n"
        "The following GAP segments span multiple app segments. You must select all "
        "of them so the entire corridor is highlighted in teal. Select them all before "
        "navigating to individual endpoints.\n\n"
        + "\n".join(instructions)
    )


def render_prompt(batch_name: str, batch_entries: list[dict[str, object]]) -> str:
    table = render_endpoints_table(batch_entries)
    gap_instructions = collect_gap_corridor_instructions(batch_entries)
    results_path = f"_temp/visual-review/batch-results/{batch_name}-results.json"
    example_close = f"{batch_name}-ep-01-close.png"
    example_context = f"{batch_name}-ep-01-context.png"
    batch_label = batch_name.replace("-", " ").title()
    return f"""# Visual Review {batch_label}

You are performing INDEPENDENT visual verification of highway segment endpoints.

## CRITICAL: Visual-only assessment

- Do NOT look at any heuristic results files (heuristic-results.csv, etc.)
- Do NOT click roadway lines to open feature popups - popups query the same TxDOT data the heuristic already uses
- Do NOT inspect network requests, API responses, or GeoJSON data
- Do NOT read any CSV, JSON, or data files - only the rendered map
- ONLY use what you can visually read from the basemap: rendered road labels, route shield graphics, county boundary lines, and the segment highlight

Your assessment must come purely from visual map reading, not data queries.

## Web App

- Primary: https://pine-j.github.io/Roadway-Segment-Limits/
- Fallback (local): serve `Web-App/` locally (for example `npx http-server Web-App -p 8080`) and use `http://localhost:8080` if GitHub Pages is down or slow.

Use the primary URL by default. Switch to local only if the page fails to load.

## Browser setup (once per batch)

1. Navigate to the web app URL.
2. Wait for segments to load by running `await window.__waitForSegments()`.
   This returns the number of loaded segments and may take 5-10 seconds on first load.
3. If the app fails to load or `__waitForSegments` hangs for more than 30 seconds, try the local fallback URL. If both fail, report the error and skip this batch.

## Workflow per endpoint

1. Select and highlight the segment (once per segment, covers both From/To):
   `await window.__selectAndZoomSegment("SEGMENT_NAME")`
   If this returns `false`, the segment name was not found. Log it and skip.
2. Zoom to the specific endpoint:
   `await window.__mapView.goTo({{center: [LON, LAT], zoom: 17}})`
3. Wait about 3 seconds for tiles to load.
4. The selected segment is the thick teal line. Unselected segments are thinner maroon lines and should be ignored.
5. Take a CLOSE screenshot (zoom 17+, roughly 200-300m radius, labels readable).
6. **Quality-check the screenshot before moving on.** Inspect it for each
   of the following problems and fix before continuing:

   a. **Blank / blue / grey** — no roads or labels visible, just a solid
      color. The basemap tiles failed to render. Wait 5 seconds and
      retake. If still blank after 2 retries, reload the page
      (`await window.location.reload()`), run
      `await window.__waitForSegments()`, re-select the segment,
      navigate back to the endpoint, and try again.

   b. **No teal highlight visible** — the thick teal segment line is not
      in the screenshot. This means `__selectAndZoomSegment` failed or
      selected the wrong segment. Re-run the select call for this
      segment (check the segment name is exact), then re-navigate and
      retake.

   c. **Teal segment visible but endpoint is off-screen** — the teal
      line is in the frame but its endpoint (where it terminates) is
      not. The screenshot is centered on the wrong location. Re-navigate
      using the coordinates from the endpoint table and retake.

   d. **Road labels not readable** — tiles loaded but text is too small
      to read at the current zoom. Zoom in one level (zoom 18 or 19)
      and retake the close screenshot.

   e. **Feature popup is open** — a popup or info panel is showing
      attribute data from a clicked feature. This is data leakage.
      Close the popup (click elsewhere on the map or press Escape),
      then retake the screenshot.

   Do NOT record results from a screenshot that fails any of these
   checks. Fix the issue and retake first.

7. Zoom out:
   `await window.__mapView.goTo({{center: [LON, LAT], zoom: 15}})`
8. Take a CONTEXT screenshot (zoom 15, surrounding area visible).
   Apply the same quality checks from step 6 (except label readability,
   which is expected to be lower at zoom 15).
9. Record what you see independently.

## What to look for at each endpoint

### A. Read all visible road labels near the endpoint
- Street name labels rendered along road lines (for example `W Vickery Blvd`, `E Euless Blvd`)
- Route shields with numbers (for example `IH 30`, `SH 114`, `US 281`)
- Local or alias names that appear on the map alongside route numbers
- Report BOTH the local name AND the route number if both are visible

### B. Identify which road is actually at the endpoint
- The endpoint is where the teal segment line ends - which road crosses or meets the segment at that exact point?
- A nearby road 200m away is not the limit
- If multiple roads are near the endpoint, identify which one the segment line actually terminates at
- Look carefully at frontage roads versus mainlines

### C. Check for offset situations
- If the endpoint is between intersections, note that explicitly
- Note the compass direction from the nearest identifiable road to the endpoint

### D. Look for county boundaries
- County boundary lines may appear as thin administrative lines on the map
- If the endpoint itself is at a county boundary, set `county_boundary_at_endpoint: true` and use `"[County Name] County Line"` as `limit_identification`
- A county line that is merely nearby should be noted in `reasoning` but not flagged as `county_boundary_at_endpoint`

### E. For GAP segments specifically
- Does the segment line visibly end or restart here?
- Is there a physical discontinuity in the road?
- What roads bound the gap on each side?

## Endpoints

{table}{gap_instructions}

## Screenshots

- Save all screenshots to `_temp/visual-review/screenshots/`
- Naming: `{batch_name}-ep-NN-close.png` and `{batch_name}-ep-NN-context.png`
- Use zero-padded endpoint numbers that match the table row number

## Output format

Write JSON, not markdown, to `{results_path}`.

```json
[
  {{
    "endpoint_id": 1,
    "segment": "SEGMENT_NAME",
    "side": "From",
    "piece": null,
    "close_screenshot": "{example_close}",
    "context_screenshot": "{example_context}",
    "visible_labels": ["Label 1", "Label 2"],
    "visible_shields": ["IH 30"],
    "county_boundary_at_endpoint": false,
    "limit_identification": "Road or County Line",
    "limit_alias": null,
    "is_offset": false,
    "offset_direction": null,
    "offset_from": null,
    "visual_confidence": "high",
    "reasoning": "Why the endpoint appears to end at this limit"
  }},
  {{
    "endpoint_id": 2,
    "segment": "GAP_SEGMENT_NAME",
    "side": "To",
    "piece": 1,
    "close_screenshot": "{batch_name}-ep-02-close.png",
    "context_screenshot": "{batch_name}-ep-02-context.png",
    "visible_labels": ["Route label"],
    "visible_shields": ["Route shield"],
    "county_boundary_at_endpoint": false,
    "limit_identification": "Route or road name",
    "limit_alias": null,
    "is_offset": false,
    "offset_direction": null,
    "offset_from": null,
    "visual_confidence": "high",
    "reasoning": "Why this gap-piece endpoint is bounded by the identified road"
  }}
]
```

Key fields for reconciliation:
- `piece`: null for continuous segments, 1-based integer for gap segments
- `limit_identification`: the primary road or boundary name
- `limit_alias`: local or street name shown alongside the route number, or null if none is visible
- `is_offset`, `offset_direction`, and `offset_from`: structured offset data
- `county_boundary_at_endpoint`: true only if the segment endpoint is the county line itself
- `visible_labels` and `visible_shields`: raw observations for the audit trail
"""


def main() -> None:
    args = parse_args()
    ensure_dirs(args.output_dir)

    manifest_entries = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(manifest_entries, list):
        raise ValueError(f"Manifest must contain a JSON array: {args.manifest}")

    batches = chunked(manifest_entries, args.batch_size)
    for batch_number, batch_entries in enumerate(batches, start=1):
        batch_name = f"batch-{batch_number:02d}"
        prompt_path = args.output_dir / f"{batch_name}.md"
        prompt_path.write_text(render_prompt(batch_name, batch_entries), encoding="utf-8")
        print(f"Wrote file: {prompt_path}")

    print(f"Generated {len(batches)} batch prompt file(s).")


if __name__ == "__main__":
    main()
