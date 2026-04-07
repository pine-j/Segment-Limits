# Plan 3: Human Review Dashboard

> **Master reference**: [`master-plan.md`](master-plan.md)
> **Depends on**: Plan 2 (Reconciliation) — needs `final-segment-limits.csv`, `heuristic-results.csv`, and `batch-results/*.json` schemas

## Scope

| Task | File | Action |
|------|------|--------|
| H | `Scripts/generate_review_dashboard.py` | Create — generates `review-dashboard.html` from reconciliation results |

## Task H: Create `Scripts/generate_review_dashboard.py`

Reads `final-segment-limits.csv`, `heuristic-results.csv`, `batch-results/*.json`, and generates a single self-contained `_temp/visual-review/review-dashboard.html`.

### Two view modes

**1. Table view** — all endpoints in a sortable/filterable table:

| Segment | Side | Heuristic | Visual | Resolution | Confidence | Status |

Filterable by Resolution (confirmed, enriched, visual_preferred, conflict).
Click any row to switch to case view.

**2. Case view** — one endpoint at a time with full detail:

```
┌─────────────────────────────────────────────────────────────────────┐
│  ◀ Prev  │  Case 17 of N: FM 730-A / To  │  Next ▶  │ Jump to… │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─── Close Screenshot ───┐  ┌─── Context Screenshot ───┐          │
│  │  (batch-01-ep-02-      │  │  (batch-01-ep-02-        │          │
│  │   close.png)           │  │   context.png)           │          │
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
│  │  (iframe: pine-j.github.io/Roadway-Segment-Limits/)                │   │
│  │  Auto-navigated to this endpoint's coordinates              │   │
│  │  Human can zoom in/out, click around                        │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─── Reviewer Notes ─────────────────────────────────────────┐   │
│  │  [textarea — type your observations here]                   │   │
│  │  Notes are auto-saved to browser localStorage               │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  Status: ○ Not reviewed  ● Agree  ○ Disagree  ○ Needs discussion   │
└─────────────────────────────────────────────────────────────────────┘
```

### Features

**Navigation**:
- Prev/Next arrows to step through cases
- Jump-to dropdown for any segment
- Filter buttons: Show All / Disagreements Only / Conflicts Only / Unreviewed
- Progress indicator: "Reviewed X of N endpoints" (counts derived from data)

**Interactive map embed**:
- iframe pointing to `https://pine-j.github.io/Roadway-Segment-Limits/`
- When switching cases, attempt to call into the iframe:
  ```javascript
  iframe.contentWindow.__selectAndZoomSegment("FM 730-A");
  iframe.contentWindow.__mapView.goTo({center: [lon, lat], zoom: 17});
  ```
- **Cross-origin limitation**: When the dashboard is opened from `file://` or `localhost`, the browser will block `contentWindow` access to the GitHub Pages iframe. The dashboard **must** implement a fallback that displays "Navigate to: [lon, lat] zoom 17" with a copy button. To get auto-navigation, serve both dashboard and web app from the same origin (e.g., `localhost:8080`).
- Human can freely zoom, pan, inspect

**Per-case reviewer notes**:
- Textarea for free-form notes on each endpoint
- Radio buttons: Not reviewed / Agree / Disagree / Needs discussion
- **Structured override fields** (shown only when status is "Disagree"):
  - `Corrected limit` — text input for the reviewer's corrected road/boundary name
  - `Corrected alias` — optional text input for a local alias name
  - `County boundary at endpoint` — checkbox (true if the endpoint IS a county line)
  These structured fields feed directly into adjudicated CSV generation (Phase 7), avoiding free-text inference by the LLM.
- Auto-saved to `localStorage` on every keystroke
- **Fully editable at any time** — reviewer can go back to any case, edit notes, change status. Nothing is locked until export.
- Survives page refreshes and browser restarts

**Export**:
- "Export Notes" button generates a JSON file:
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
        "reviewer_notes": "Confirmed: Walnut St clearly visible",
        "reviewer_corrected_limit": null,
        "reviewer_corrected_alias": null,
        "reviewer_county_boundary_at_endpoint": null
      }
    ]
  }
  ```
- This JSON gets passed to the Orchestrator Agent in Phase 7 (optional)
- If the user requests adjudicated output, the Orchestrator uses an LLM to apply reviewer overrides and produce `human-reviewed-segment-limits.csv`

**Summary bar** (always visible at top):
- Total cases, reviewed count, agree/disagree/needs-discussion counts
- Filter shortcuts

### Technical approach

Single HTML file with embedded CSS and JavaScript. Data embedded as inline JSON:

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

- Screenshots via **relative paths** — HTML lives in `_temp/visual-review/` alongside `screenshots/`
- Keeps HTML small (~200KB) while supporting 600+ screenshots
- No npm, no build — just open the file for review with manual map navigation (fallback mode)
- For auto-navigation of the embedded map, serve both dashboard and web app from the same origin (e.g., serve the repo root with `python -m http.server 8080` so both `/Web-App/` and `/_temp/visual-review/review-dashboard.html` are on `localhost:8080`)
- Works offline (except map iframe)
- Portable — can be emailed, shared, archived

### Implementation notes

1. The Python script reads the CSVs and JSONs, assembles the `REVIEW_DATA` array, and injects it into an HTML template string
2. CSS should be clean and functional — focus on readability, not aesthetics
3. The map iframe **will** face cross-origin restrictions for `__selectAndZoomSegment()` when opened from `file://` or a different origin — implement a try/catch fallback that shows "Navigate to: [-97.489, 33.198] zoom 17" text with a copy button
4. `localStorage` key should include a run identifier (e.g., date) so notes from different runs don't collide

---

## Testing

1. Create mock reconciliation data (5-10 endpoints with mixed resolutions)
2. Run `generate_review_dashboard.py` — verify HTML is generated
3. Open the HTML in a browser — verify:
   - Table view loads with all cases
   - Click navigates to case view
   - Screenshots display (use placeholder images for testing)
   - Notes textarea saves to localStorage
   - Navigation (prev/next/jump) works
   - Export button generates valid JSON
   - Editing a previous case's notes works after navigating away and back
4. Test with a gap segment case — verify `piece` column shows correctly
