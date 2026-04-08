# Segment Limits — Playbook

> This document defines **how to identify segment endpoint limits**. It is
> read by both AI agents and human reviewers. If you disagree with how a
> scenario is handled, edit this file — the pipeline will adopt your changes
> on the next run.
>
> Each scenario includes: what to look for, how to determine the limit,
> what to report, and known heuristic failure modes. Add new scenarios or
> update existing ones as they come up.
>
> See also: [Segment-Limits-Heuristics-Logic.md](Segment-Limits-Heuristics-Logic.md)
> for the heuristic engine internals and confidence model.

## What is a "limit"?

A limit is the geographic reference that defines where a highway segment
starts (From) or ends (To). Limits describe the endpoint in terms that a
transportation engineer would recognize:

- **A crossing route**: "SH 183" — a highway that crosses or meets the
  segment at its endpoint
- **A county boundary**: "Johnson County Line" — an administrative boundary
- **An offset**: "N of SH 183" — when the endpoint is between intersections

---

## Priority rules

When multiple features are at an endpoint, use this priority order:

1. **County boundary** — if the basemap color changes at the endpoint
2. **Interstate highway (IH)** — highest-tier route
3. **US highway** — second tier
4. **State highway (SH)** — third tier
5. **Farm-to-market road (FM/RM)** — fourth tier
6. **Business route (BU), State Spur (SS), State Loop (SL)** — fifth tier
7. **Named local road** — only if no highway-level route exists nearby

The `limit_identification` field should use the **route designation** (e.g.,
"IH 30"), not the local street name. Local names go in `limit_alias`.

---

## Scenarios

### Scenario 1: Highway interchange

**What you see**: circular or curved ramp roads, multiple highway shields,
large road intersection with grade separation.

**How to determine**: the limit is the **crossing highway** at the
interchange, not the ramps or frontage roads. Use the road query data to
find the highest-priority route at the endpoint.

**Report**:
- `limit_identification`: the crossing route (e.g., "IH 30")
- `limit_alias`: local name if visible (e.g., "W Lancaster Ave")
- `is_offset`: false
- `visual_confidence`: "high"

**Example**: segment SH 199 - D ends at the IH 30 interchange. Even though
"W Vickery Blvd" is the nearest readable label, the limit is "IH 30".

**Known heuristic weakness**: in dense interchanges the heuristic may pick a
spatially close but wrong road (e.g., a ramp road or a parallel frontage
road instead of the main crossing highway). The crossing angle heuristic
helps but is not always sufficient when multiple routes converge.

---

### Scenario 2: Simple road crossing

**What you see**: two roads cross at grade. No interchange ramps. One or
more route shields visible.

**How to determine**: the limit is the road that **crosses** the segment
(not the segment itself). Use the road query to confirm the route name.

**Report**:
- `limit_identification`: the crossing route (e.g., "FM 731")
- `limit_alias`: local name if visible
- `is_offset`: false
- `visual_confidence`: "high"

**Known heuristic weakness**: generally reliable for this scenario. Can fail
when the crossing road has multiple names (e.g., FM 731 / Burleson Blvd)
and the heuristic picks the local name instead of the route designation.

---

### Scenario 3: County boundary

**What you see**: an amber county boundary line crosses the endpoint area,
with a county name label (e.g., "Johnson County") displayed alongside it.
The road query may show roads from two different counties.

**How to determine**: check the `county` field in the road query results.
If roads on one side are "Tarrant" and the other side are "Johnson", the
endpoint is at the Tarrant / Johnson County Line.

**Report**:
- `limit_identification`: use the county name that the segment is **entering**
  (the far side of the boundary from the segment's perspective). E.g., if
  the segment runs from Tarrant County into Johnson County, the limit is
  "Johnson County Line". If the direction is ambiguous, use both names:
  "Tarrant / Johnson County Line".
- `county_boundary_at_endpoint`: true
- `is_offset`: false
- `visual_confidence`: "high" if the amber boundary line is visible; "medium" if
  only road query county fields differ and no boundary line is visible

**Example**: US 287 - A ends where the amber county boundary line crosses
the road. The label reads "Johnson County". The limit is "Johnson County
Line".

**Known heuristic weakness**: the heuristic detects county boundaries
geometrically (distance from endpoint to county polygon boundary). It can
miss boundaries when the endpoint is slightly offset from the exact
boundary line, or when the county polygon data has alignment errors. The
amber county boundary line rendered on the map is a more reliable visual
indicator than the geometric distance calculation.

---

### Scenario 4: Offset — endpoint between intersections

**What you see**: the thick segment line ends in the middle of a road
stretch, not at any visible crossing or interchange. No route crosses at
the exact endpoint.

**How to determine**: the road query at 50m returns empty or only the
segment's own route. Check `roads_within_200m` for the nearest crossing
route. Use the screenshots to determine the compass direction from that
route to the endpoint.

**Report**:
- `limit_identification`: "N of SH 183" (direction + nearest route)
- `is_offset`: true
- `offset_direction`: "N", "S", "E", "W", "NE", etc.
- `offset_from`: "SH 183"
- `visual_confidence`: "medium"

**Example**: IH 35W - D starts 300m north of SH 183. The road query at
50m shows only IH 35W (the segment itself). The 200m query shows SH 183.
The limit is "N of SH 183".

**Known heuristic weakness**: offset detection and phrasing is one of the
heuristic's weakest areas. Common failures include:
- Picking a road that's nearby but in the wrong direction
- Using a local street name for the offset reference instead of the nearest
  highway (e.g., "N of Dewey St" instead of "N of SH 183")
- Failing to detect that the endpoint is offset when it's very close to
  (but not at) an intersection

---

### Scenario 5: Frontage road vs mainline

**What you see**: the segment ends where it meets a highway, but the road
query shows both "Main Lane" and "Frontage" entries for the same route.

**How to determine**: check `roadbed_type` in the road query:
- If the segment connects to the frontage road (not the main lanes), report
  "Left Frontage IH 20" or "Right Frontage IH 20" depending on the side.
- If the segment connects to the main lanes, report just "IH 20".
- Use the screenshots to see whether the segment line reaches the main
  highway or stops at the frontage road.

**Report**:
- `limit_identification`: "Left Frontage IH 20" or "IH 20"
- `visual_confidence`: "medium" (frontage/mainline distinction can be subtle)

**Known heuristic weakness**: frontage road side selection (left vs right)
is a common source of error. The heuristic uses TxDOT label text and
inventory geometry to determine the side, but when labels are sparse or
the inventory geometry is generalized, it may pick the wrong side or
collapse to the mainline designation.

---

### Scenario 6: Business route or spur

**What you see**: a route shield with "BU" (business) or "SS" (state spur)
prefix. These are lower-tier routes but still valid highway designations.

**How to determine**: use the road query's `route_prefix` field. Business
routes (BU) and state spurs (SS) are legitimate limits.

**Report**:
- `limit_identification`: "BU 287P" or "SS 280"
- `visual_confidence`: "high"

**Known heuristic weakness**: generally reliable. Can sometimes miss the
business route designation and instead report the parent route (e.g.,
"US 287" instead of "BU 287P") when the business route is short or the
label tile coverage is sparse.

---

### Scenario 7: Local road only — no highway nearby

**What you see**: the endpoint is in a residential or rural area. No route
shields visible. Road query shows only local streets within 200m.

**How to determine**: this is rare. Use the highest-profile road in the
road query results (check if any have a route_prefix, even at 200m). If
truly no highway exists, use the most prominent local road name.

**Report**:
- `limit_identification`: the local road name
- `limit_alias`: null
- `visual_confidence`: "low"
- `reasoning`: explain why no highway-level route was found

**Known heuristic weakness**: these endpoints typically have low heuristic
confidence. The heuristic falls back to the nearest labeled road, which may
not be the most meaningful reference point.

---

### Scenario 8: Two highways cross at the endpoint

**What you see**: the endpoint is at an interchange where two or more
highways meet. The road query returns multiple routes.

**How to determine**: the limit is the **crossing** route, not the route
the segment runs along. If the segment is IH 820 and the endpoint is at
the SH 183 interchange, the limit is "SH 183" (not "IH 820", because
that's the segment itself).

Filter out the segment's own route family from the road query results.
The remaining highest-priority route is the limit.

**Report**:
- `limit_identification`: the crossing route
- `visual_confidence`: "high"

**Known heuristic weakness**: the heuristic uses `same_route_corridor()`
to filter the segment's own route family, but this can fail for complex
corridors where the segment's route appears under multiple names. It may
also pick the lower-priority of two crossing routes at a three-way
interchange.

---

### Scenario 9: Gap segment piece boundary

**What you see**: the thick segment line stops and resumes after a visible
gap. This is a GAP segment piece endpoint.

**How to determine**: treat each piece endpoint independently using
Scenarios 1-8 above. The gap itself is not the limit — the road or
boundary at each piece's endpoint is the limit.

**Report**: same as whichever scenario applies at that piece endpoint.

**Known heuristic weakness**: gap piece endpoints are processed
independently, which is correct. However, piece orientation can occasionally
be wrong (pieces sorted by cardinal direction, not geometry order), which
would swap a piece's From and To limits.

---

### Scenario 10: Endpoint needs further investigation

**What you see**: the initial data (screenshots + road query at 50m/200m/500m)
is insufficient. The endpoint is ambiguous — maybe between two possible
roads, or in a complex interchange where it's unclear which road is the
limit.

**How the agent should handle this**:

1. Check all three query radii (50m, 200m, 500m) in the roads.json — the
   wider radii may reveal a highway that was outside the initial search
2. Use the context screenshot (zoom 15) for clues about the broader road
   network — interchanges, amber county boundary lines, highway shields
3. Cross-reference what you see in the screenshots with the road query data
4. Make the best determination possible and document the uncertainty

If after using all available data (three query radii + two screenshots +
playbook scenarios) you still cannot determine a confident limit:

- Set `visual_confidence: "low"`
- Set `"needs_investigation": true`
- In `reasoning`, describe specifically what is ambiguous and what additional
  data would help (e.g., "Need closer screenshot at zoom 19 to read labels
  at interchange", "Road visible in screenshot but not in any query radius —
  may be outside 500m")

The orchestrator will review all `needs_investigation` endpoints and can:
- Re-capture screenshots at different zoom levels
- Run targeted road queries at specific coordinates
- Make the final determination using the full pipeline context

**The goal is zero unresolved endpoints.** The agent should resolve as many
as possible; the orchestrator handles the rest.

---

### Scenario 11: Dead-end / terminus

**What you see**: the highway simply ends. There is no crossing road, no
county boundary — the road terminates.

**How to determine**: the road query will show only the segment's own route.
The screenshots show the road ending (no continuation). This is different
from an offset (Scenario 4) where the highway continues but the segment
stops.

**Report**:
- `limit_identification`: "End of [route]" (e.g., "End of FM 1189")
- `is_offset`: false
- `visual_confidence`: "medium"

**Known heuristic weakness**: the heuristic does not have a dedicated
terminus detection mode. It will fall back to the nearest road, which may
be far away and irrelevant.

---

### Additional limit types (for reference)

These features may appear near endpoints. They are listed here so reviewers
know how to handle them if encountered:

- **Toll roads (TL)**: rank between FM/RM and named local roads in priority
- **County roads (CR)**: rank between FM/RM and named local roads
- **Park roads (PA/PR), forest service roads (FS)**: same as county roads
- **City limit boundaries**: NOT used as segment limits in TxDOT convention.
  If a segment happens to end at a city limit, look for the actual road or
  county boundary instead.
- **Railroad grade crossings**: NOT used as segment limits. Report the
  nearest road crossing or offset instead.

---

## How to add a new scenario

If you encounter a situation not covered above:

1. Describe what you see (visual + road query data)
2. Explain how to determine the correct limit
3. Show the expected output fields
4. Document any known heuristic weaknesses for this scenario
5. Add it as a new numbered scenario in this file

The next pipeline run will automatically use the updated logic.

## How to tune existing scenarios

To change how a scenario is handled:
- Edit the "How to determine" section
- Update the "Report" fields
- Update "Known heuristic weakness" if you've observed new failure patterns
- Add examples if helpful

All agents read this file before assessing endpoints. Changes take effect
immediately on the next run.

## How to give feedback

If you are reviewing the pipeline output and disagree with how a limit was
determined:

1. Note the segment name, side, and what you think the correct limit is
2. Identify which scenario above applies (or propose a new one)
3. Explain what the agent should have done differently
4. Edit this file or provide feedback to the pipeline maintainer

Your feedback directly improves both the AI agent behavior (through prompt
updates) and the heuristic model (through code changes to
`identify_segment_limits.py`).
