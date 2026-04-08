#!/usr/bin/env python3
"""Generate batched visual-review prompts from the manifest.

Produces batch prompt files that instruct sub-agents to analyze
pre-captured screenshots and road query data from batch-screenshots.py.
"""

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


def render_endpoints_table(batch_name: str, batch_entries: list[dict[str, object]]) -> str:
    lines = [
        "| # | Segment | Side | Piece | Coordinates | Close screenshot | Context screenshot | Road data | Notes |",
        "|---|---------|------|-------|-------------|-----------------|-------------------|-----------|-------|",
    ]
    for endpoint_id, entry in enumerate(batch_entries, start=1):
        coords = f"({fmt_coord(entry.get('lon'))}, {fmt_coord(entry.get('lat'))})"
        ss = f"_temp/visual-review/screenshots/{batch_name}-ep-{endpoint_id:02d}"
        close_file = f"`{ss}-close.png`"
        context_file = f"`{ss}-context.png`"
        roads_file = f"`{ss}-roads.json`"
        lines.append(
            f"| {endpoint_id} | {entry['segment']} | {entry['side']} | {piece_display(entry)} | "
            f"{coords} | {close_file} | {context_file} | {roads_file} | {endpoint_note(entry)} |"
        )
    return "\n".join(lines)


def render_prompt(batch_name: str, batch_entries: list[dict[str, object]]) -> str:
    table = render_endpoints_table(batch_name, batch_entries)
    results_path = f"_temp/visual-review/batch-results/{batch_name}-results.json"
    example_close = f"{batch_name}-ep-01-close.png"
    example_context = f"{batch_name}-ep-01-context.png"
    batch_label = batch_name.replace("-", " ").title()
    return f"""# Visual Review {batch_label} — Screenshot Analysis

You are performing INDEPENDENT visual verification of highway segment endpoints
by analyzing pre-captured map screenshots.

## CRITICAL: Visual-only assessment

- Do NOT look at any heuristic results files (heuristic-results.csv, etc.)
- Do NOT read any CSV or data files in `_temp/` other than the screenshot image
  files and road query JSON files listed in the endpoint table below
- ONLY use what you can visually read from the map screenshots: rendered road
  labels, route shield graphics, county boundary lines, and the segment highlight
- Do NOT fabricate observations — if a label is unreadable, say so

Your assessment must come purely from visual map reading and road query data.

## Required reading

Before processing any endpoints, read `SEGMENT_LIMITS_PLAYBOOK.md` in the
project root. It defines the scenarios, priority rules, and decision logic
for determining limits. Follow it exactly.

## How screenshots were captured

Each endpoint has two pre-captured screenshots taken from the FTW Segment
Explorer web app (https://pine-j.github.io/Roadway-Segment-Limits/):

- **Close** (zoom 17, ~200-300m radius): high detail, road labels and route
  shields should be readable
- **Context** (zoom 15, wider area): shows surrounding roads, interchanges,
  and county boundaries for spatial context

The selected segment is the **thick maroon/teal line**. Unselected segments
are thinner and should be ignored. The endpoint is where the thick segment
line terminates.

**Segment types visible in screenshots:**
- **Individual segments** (suffixed, e.g., "IH 20 - B"): one segment is
  highlighted. Its endpoints are where the thick line starts/ends.
- **Corridor segments** (unsuffixed, e.g., "SH 360"): the entire corridor is
  highlighted — all sub-segments (A, B, C, etc.) show as one continuous thick
  line. The endpoint is where the overall corridor line terminates.
- **GAP segments**: the thick line has one or more visible breaks. Each
  contiguous stretch is a "piece" with its own From/To endpoints. The Notes
  column in the endpoint table indicates which piece and the total count.

## Workflow per endpoint

For each row in the endpoint table:

1. **Read the CLOSE screenshot** using the Read tool with the file path from
   the table. This is an image file — the Read tool will display it visually.
2. **Read the CONTEXT screenshot** the same way.
3. **Quality-check both screenshots** before recording results. If any of
   these problems exist, you MUST flag the endpoint for recapture by setting
   `"needs_rescan": true` in the JSON output for that endpoint:

   - **Blank / grey / blue screenshot** — tiles failed to render, no roads
     visible. Set `needs_rescan: true`, reasoning: describe which screenshot
     is blank (close, context, or both).
   - **No thick segment line visible** — the segment was not selected or
     highlighted during capture. Set `needs_rescan: true`, reasoning:
     "no segment highlight visible in screenshots".
   - **Road labels unreadable** — tiles loaded but text is too small to read
     at the captured zoom level. Try to use the context view first. If
     neither view has readable labels near the endpoint, set
     `needs_rescan: true`, reasoning: "labels too small to read at current
     zoom — need closer view".
   - **Endpoint off-screen** — the thick segment line is visible but its
     termination point is not in frame. Set `needs_rescan: true`, reasoning:
     "endpoint not visible in frame".

   For flagged endpoints: still fill in all other fields with your best guess
   (or null/empty where you truly cannot determine), set
   `visual_confidence: "low"`, and include `"needs_rescan": true`. The
   orchestrator will recapture and re-run analysis for these endpoints.

   If the screenshots are usable (even if not perfect), do NOT set
   `needs_rescan`. Only flag truly unusable screenshots.
4. **Assess the endpoint** based on what you see (details below).
5. **Record your assessment** in the JSON output.

## What to look for at each endpoint

### Understanding what a "limit" is

A segment limit is the **major highway, route, or county boundary** that
defines where the segment starts or ends. Limits are expressed as:

- **Route designations**: IH 30, US 287, SH 183, FM 731, BU 287P, SS 280
- **County boundary lines**: Johnson County Line, Tarrant / Wise County Line
- **Offsets from a route**: N of SH 183, S of E Altamesa Blvd

A limit is almost NEVER a local residential street name. Local streets go in
`limit_alias`, not `limit_identification`.

**Priority order for `limit_identification`:**
1. County boundary line (if an amber boundary line is visible at the endpoint)
2. Interstate highway (IH) crossing or interchange
3. US highway crossing
4. State highway (SH) crossing
5. Farm-to-market road (FM/RM) crossing
6. Business route (BU), State Spur (SS), or State Loop (SL)
7. Named local road ONLY if no highway-level route exists near the endpoint

### Data available per endpoint

Each endpoint has three sources of information:

1. **Close screenshot** (zoom 17): spatial layout, some labels visible
2. **Context screenshot** (zoom 15): wider area, interchanges, county
   boundaries visible
3. **Road query data** (`roads.json` file): TxDOT attribute data for all
   roads within 50m, 200m, and 500m of the endpoint — this is the same data
   a human sees when they click a road in the web app. Each road entry has:
   - `route_name`: official TxDOT route name (e.g., "IH 0030-KG")
   - `route_prefix`: highway system (IH, US, SH, FM, etc.)
   - `route_number`: route number
   - `map_label`: display label (e.g., "IH 30", "W Vickery Blvd")
   - `roadbed_type`: "Main Lane", "Frontage", etc.
   - `county`: county name

### Assessment workflow

For each endpoint, work through this process:

**Step 1 — Read the road query data** (`roads.json`). This tells you what
TxDOT says is at the endpoint. Look at the `roads_within_50m` list first:
- If it contains a highway route (IH/US/SH/FM prefix), that is likely the
  limit. Note the `map_label` and `roadbed_type`. Use `map_label` (not
  `route_name`) for `limit_identification` — `map_label` uses the clean
  display format (e.g., "IH 30") while `route_name` uses TxDOT internal
  format (e.g., "IH 0030-KG").
- If `roads_within_50m` is empty or contains only local roads, check
  `roads_within_200m` for the nearest highway — this is an offset situation.
- If `roads_within_200m` is also empty, check `roads_within_500m` — for
  rural endpoints the nearest highway may be farther away.

**Step 2 — Read the screenshots** to verify spatially:
- Does the thick segment line actually terminate at the road identified in
  Step 1?
- Is there an interchange visible (circular ramps)? The major route at the
  interchange is the limit.
- Is there an amber county boundary line at the endpoint? County boundaries
  are rendered as distinct amber lines with county name labels — this takes
  priority over other limits.
- Does the segment end between intersections? That's an offset.

**Step 3 — Determine the limit** by combining both sources:
- **Road query shows a highway at 50m + screenshots confirm crossing** →
  use the highway designation as `limit_identification`
- **Road query shows a highway at 50m + screenshots show the endpoint is
  NOT at that highway** → the endpoint may be offset. Check if the segment
  terminates before reaching the highway. Set `is_offset: true`.
- **Road query at 50m is empty, 200m shows a highway** → offset situation.
  The limit is "N/S/E/W of [highway]". Use the screenshots to determine
  the compass direction.
- **Amber county boundary line visible in screenshots** → use "[County]
  County Line" as the limit. The boundary is rendered as a distinct amber
  line with a county name label. The road query may show roads from both
  counties (check the `county` field to confirm the boundary).
- **Multiple highways at the endpoint** → use the screenshots to determine
  which one the segment line actually meets. The road query tells you the
  names; the screenshots tell you which road is at the endpoint.

**Step 4 — If still uncertain, flag for investigation.** If Steps 1-3 do
not produce a confident answer, the orchestrator will investigate further
with wider road queries and additional screenshots at different zoom levels.
Set `visual_confidence: "low"` and describe what is ambiguous in `reasoning`.

### Additional considerations

- Many highways have BOTH a route number and a local name (e.g., IH 35W is
  also "South Fwy"). Report the **route designation** as
  `limit_identification` and the **local name** as `limit_alias`.
- Look carefully at frontage roads versus mainlines. The road query's
  `roadbed_type` field distinguishes "Main Lane" from "Frontage".
- For GAP segments: each piece's From and To limits are identified
  independently. The thick line has visible breaks — each contiguous stretch
  is a separate piece.

## Endpoints

{table}

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
    "visual_confidence": "low",
    "needs_rescan": true,
    "reasoning": "Close screenshot is blank - tiles failed to render. Cannot identify endpoint."
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
- `needs_rescan`: set to `true` ONLY if the screenshot is unusable (blank,
  no highlight, unreadable labels, endpoint off-screen). Omit this field
  entirely when the screenshot is usable.
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
