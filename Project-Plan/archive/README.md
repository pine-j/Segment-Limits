# Archived Plan Documents

These files are the **original design-phase plans** that guided initial
implementation. They are preserved for historical reference and for updating
the case study (`SEGMENT_LIMITS_CASE_STUDY.md`).

## When to read these files

- Updating or writing the case study
- Understanding original design decisions and trade-offs
- Reviewing how the architecture evolved

## When NOT to read these files

- **Running the orchestration pipeline** — use `orchestrator.md` at the
  project root. It is the authoritative runtime document.
- **Modifying scripts or workflow** — use `orchestrator.md` and the current
  `Project-Plan/master-plan.md` (parent directory).
- **Debugging pipeline issues** — these archived plans describe an older
  architecture (e.g., Playwright MCP for screenshots) that no longer applies.

## What changed since these plans were written

- Phase 3 was a single Playwright MCP step; it is now three sub-phases
  (3a: batch capture, 3b: visual analysis with rescan loop, 3c: spot-check)
- Screenshots are captured by `batch-screenshots.py` using native ArcGIS
  `MapView.takeScreenshot()`, not Playwright MCP
- Visual analysis agents read pre-captured screenshots from disk instead of
  driving a browser
- Corridor segment handling (`__selectCorridorSegments`) was added
- `needs_rescan` flag and recapture loop were added
- Segment type taxonomy (individual vs corridor vs gap) was documented
