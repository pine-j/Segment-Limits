#!/usr/bin/env python3
"""Generate a self-contained human-review dashboard from visual-review outputs."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VISUAL_REVIEW_DIR = ROOT / "_temp" / "visual-review"
HOSTED_MAP_URL = "https://pine-j.github.io/Roadway-Segment-Limits/"


@dataclass(frozen=True)
class EndpointKey:
    segment: str
    side: str
    piece: int | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--visual-review-dir",
        type=Path,
        default=DEFAULT_VISUAL_REVIEW_DIR,
        help=f"Root directory containing visual-review artifacts. Default: {DEFAULT_VISUAL_REVIEW_DIR}",
    )
    parser.add_argument("--final-results", type=Path, default=None)
    parser.add_argument("--heuristic-results", type=Path, default=None)
    parser.add_argument("--batch-results-dir", type=Path, default=None)
    parser.add_argument("--output-path", type=Path, default=None)
    return parser.parse_args()


def resolve_path(path: Path | None, fallback: Path) -> Path:
    return (path or fallback).resolve()


def require_exists(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} was not found: {path}")


def safe_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def maybe_text(value: object) -> str | None:
    text = safe_text(value)
    return text or None


def parse_float(value: object, default: float | None = 0.0) -> float | None:
    text = safe_text(value)
    if not text:
        return default
    return float(text)


def parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return safe_text(value).lower() in {"1", "true", "yes", "y"}


def parse_piece(value: object) -> int | None:
    text = safe_text(value)
    if not text:
        return None
    return int(float(text))


def normalize_segment(value: object) -> str:
    return " ".join(safe_text(value).split())


def normalize_side(value: object) -> str:
    text = safe_text(value).lower()
    if text == "from":
        return "From"
    if text == "to":
        return "To"
    raise ValueError(f"Unsupported side value: {value!r}")


def endpoint_key(segment: object, side: object, piece: object) -> EndpointKey:
    return EndpointKey(
        segment=normalize_segment(segment),
        side=normalize_side(side),
        piece=parse_piece(piece),
    )


def validate_columns(dataframe: pd.DataFrame, required: set[str], label: str) -> None:
    missing = sorted(required - set(dataframe.columns))
    if missing:
        raise ValueError(f"{label} is missing required columns: {', '.join(missing)}")


def load_csv_records(path: Path, *, required: set[str], label: str) -> list[dict[str, object]]:
    require_exists(path, label)
    dataframe = pd.read_csv(path)
    validate_columns(dataframe, required, label)
    return dataframe.to_dict("records")


def load_rows_by_key(
    records: list[dict[str, object]],
    *,
    label: str,
) -> dict[EndpointKey, dict[str, object]]:
    rows_by_key: dict[EndpointKey, dict[str, object]] = {}
    for row in records:
        key = endpoint_key(
            row.get("Segment") or row.get("segment"),
            row.get("Side") or row.get("side"),
            row.get("Piece") or row.get("piece"),
        )
        if key in rows_by_key:
            raise ValueError(f"Duplicate {label} row for {key}")
        rows_by_key[key] = row
    return rows_by_key


def load_visual_rows(batch_results_dir: Path) -> dict[EndpointKey, dict[str, object]]:
    require_exists(batch_results_dir, "Batch-results directory")
    batch_paths = sorted(batch_results_dir.glob("*.json"))
    if not batch_paths:
        raise FileNotFoundError(f"No batch result JSON files were found in {batch_results_dir}")

    visual_rows: dict[EndpointKey, dict[str, object]] = {}
    for path in batch_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"Expected a JSON array in {path}")
        for entry in payload:
            if not isinstance(entry, dict):
                raise ValueError(f"Each visual-review entry must be an object in {path}")
            key = endpoint_key(entry.get("segment"), entry.get("side"), entry.get("piece"))
            if key in visual_rows:
                raise ValueError(f"Duplicate visual-review row for {key}")
            entry_copy = dict(entry)
            entry_copy["_source_file"] = path.name
            visual_rows[key] = entry_copy
    return visual_rows


def dedupe_text(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = safe_text(value)
        if not text:
            continue
        marker = text.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(text)
    return deduped


def build_visual_labels(entry: dict[str, object]) -> tuple[list[str], list[str], str]:
    visible_labels = dedupe_text(list(entry.get("visible_labels", []) or []))
    visible_shields = dedupe_text(list(entry.get("visible_shields", []) or []))
    rendered = visible_labels + [f"{shield} shield" for shield in visible_shields]
    return visible_labels, visible_shields, ", ".join(dedupe_text(rendered))


def relative_path(from_dir: Path, to_path: Path) -> str:
    return os.path.relpath(to_path, start=from_dir).replace("\\", "/")


def resolve_screenshot_path(
    filename: object,
    *,
    output_dir: Path,
    screenshots_dir: Path,
) -> str | None:
    text = safe_text(filename)
    if not text:
        return None
    path = Path(text)
    if path.is_absolute():
        target = path
    elif path.parts and path.parts[0] == "screenshots":
        target = screenshots_dir.parent / path
    else:
        target = screenshots_dir / path.name
    return relative_path(output_dir, target)


def build_case_label(segment: str, side: str, piece: int | None) -> str:
    if piece is None:
        return f"{segment} / {side}"
    return f"{segment} / {side} / piece {piece}"


def required_value(row: dict[str, object] | None, key: EndpointKey, label: str) -> dict[str, object]:
    if row is None:
        raise ValueError(
            f"Missing {label} row for endpoint: {key.segment} / {key.side} / piece {key.piece or '-'}"
        )
    return row


def build_review_data(
    *,
    final_rows: list[dict[str, object]],
    heuristic_by_key: dict[EndpointKey, dict[str, object]],
    visual_by_key: dict[EndpointKey, dict[str, object]],
    output_path: Path,
) -> list[dict[str, object]]:
    screenshots_dir = output_path.parent / "screenshots"
    review_data: list[dict[str, object]] = []
    for index, final_row in enumerate(final_rows, start=1):
        key = endpoint_key(final_row.get("Segment"), final_row.get("Side"), final_row.get("Piece"))
        heuristic_row = required_value(heuristic_by_key.get(key), key, "heuristic")
        visual_row = required_value(visual_by_key.get(key), key, "visual-review")
        visible_labels, visible_shields, labels_seen_text = build_visual_labels(visual_row)
        segment = normalize_segment(final_row.get("Segment"))
        side = normalize_side(final_row.get("Side"))
        piece = parse_piece(final_row.get("Piece"))
        review_data.append(
            {
                "order": index,
                "case_key": f"{segment}::{side}::{piece if piece is not None else ''}",
                "label": build_case_label(segment, side, piece),
                "segment": segment,
                "direction": safe_text(heuristic_row.get("Direction")),
                "type": safe_text(final_row.get("Type") or heuristic_row.get("Type") or "Continuous"),
                "side": side,
                "piece": piece,
                "heuristic_limit": safe_text(final_row.get("Heuristic-Limit") or heuristic_row.get("Auto-Limit")),
                "heuristic_confidence": parse_float(final_row.get("Heuristic-Confidence"), 0.0),
                "heuristic_label": safe_text(heuristic_row.get("Heuristic")),
                "heuristic_confidence_bucket": safe_text(heuristic_row.get("Confidence-Bucket")),
                "visual_limit": safe_text(final_row.get("Visual-Limit")),
                "visual_alias": maybe_text(visual_row.get("limit_alias")),
                "visual_confidence": safe_text(final_row.get("Visual-Confidence") or visual_row.get("visual_confidence")),
                "visual_labels_seen": visible_labels,
                "visual_shields_seen": visible_shields,
                "visual_labels_seen_text": labels_seen_text or safe_text(final_row.get("Visual-Labels-Seen")),
                "visual_reasoning": safe_text(visual_row.get("reasoning")),
                "resolution": safe_text(final_row.get("Resolution")),
                "category": safe_text(final_row.get("Disagreement-Category")),
                "final_limit": safe_text(final_row.get("Final-Limit")),
                "final_confidence": parse_float(final_row.get("Final-Confidence"), 0.0),
                "close_screenshot": resolve_screenshot_path(
                    visual_row.get("close_screenshot"),
                    output_dir=output_path.parent,
                    screenshots_dir=screenshots_dir,
                ),
                "context_screenshot": resolve_screenshot_path(
                    visual_row.get("context_screenshot"),
                    output_dir=output_path.parent,
                    screenshots_dir=screenshots_dir,
                ),
                "lon": parse_float(heuristic_row.get("Lon"), None),
                "lat": parse_float(heuristic_row.get("Lat"), None),
                "county_boundary_at_endpoint": parse_bool(visual_row.get("county_boundary_at_endpoint")),
                "is_offset": parse_bool(visual_row.get("is_offset")),
                "offset_direction": maybe_text(visual_row.get("offset_direction")),
                "offset_from": maybe_text(visual_row.get("offset_from")),
                "visual_review_file": safe_text(visual_row.get("_source_file")),
            }
        )
    return review_data


def compute_run_id(review_data: list[dict[str, object]]) -> str:
    payload = json.dumps(review_data, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:12]


def json_for_script(value: object) -> str:
    return json.dumps(value, ensure_ascii=True).replace("</", "<\\/")


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Roadway Segment Review Dashboard</title>
  <style>
__PAGE_STYLE__
  </style>
</head>
<body>
  <div class="summary-bar">
    <div class="summary-header">
      <div>
        <div class="summary-title">Roadway Segment Review Dashboard</div>
        <div class="summary-meta" id="summaryMeta"></div>
      </div>
      <div class="view-toggle">
        <button type="button" id="tableViewButton">Table View</button>
        <button type="button" id="caseViewButton">Case View</button>
      </div>
    </div>
    <div class="summary-metrics" id="summaryMetrics"></div>
    <div class="summary-controls">
      <div class="controls-cluster">
        <div class="filter-group">
          <button type="button" class="filter-pill" data-scope-filter="all" id="scopeAllButton"></button>
          <button type="button" class="filter-pill" data-scope-filter="disagreements" id="scopeDisagreementsButton"></button>
          <button type="button" class="filter-pill" data-scope-filter="conflicts" id="scopeConflictsButton"></button>
          <button type="button" class="filter-pill" data-scope-filter="unreviewed" id="scopeUnreviewedButton"></button>
        </div>
        <label class="control-label">
          Resolution
          <select id="resolutionFilter">
            <option value="all">All resolutions</option>
            <option value="confirmed">confirmed</option>
            <option value="enriched">enriched</option>
            <option value="visual_preferred">visual_preferred</option>
            <option value="conflict">conflict</option>
            <option value="visual_only">visual_only</option>
          </select>
        </label>
        <label class="control-label">
          Reviewer
          <input type="text" id="reviewerName" placeholder="Optional reviewer name">
        </label>
      </div>
      <div class="controls-cluster">
        <button type="button" class="mini-button" id="exportButton">Export Notes</button>
      </div>
    </div>
  </div>
  <main>
    <section id="tableView" class="view panel">
      <div class="table-toolbar">
        <div class="muted" id="tableSummary"></div>
      </div>
      <div class="table-wrapper" id="tableWrapper"></div>
    </section>
    <section id="caseView" class="view hidden case-shell">
      <div class="panel case-toolbar">
        <div>
          <div class="case-title" id="caseTitle"></div>
          <div class="case-subtitle" id="caseSubtitle"></div>
        </div>
        <div class="case-controls">
          <button type="button" class="nav-button" id="prevCaseButton">Prev</button>
          <button type="button" class="nav-button" id="nextCaseButton">Next</button>
          <label class="control-label jump-control">
            Jump to
            <select id="caseJumpSelect"></select>
          </label>
        </div>
      </div>
      <div class="case-grid">
        <div class="panel image-card" id="closeImageCard"></div>
        <div class="panel image-card" id="contextImageCard"></div>
      </div>
      <div class="detail-grid">
        <div class="card" id="heuristicCard"></div>
        <div class="card" id="visualCard"></div>
        <div class="card" id="decisionCard"></div>
      </div>
      <div class="panel map-card">
        <div class="map-toolbar">
          <div>
            <h3>Interactive Map</h3>
            <div class="map-note" id="mapStatusText"></div>
          </div>
          <div class="controls-cluster">
            <button type="button" class="mini-button" data-map-source="same-origin" id="mapSameOriginButton">Same-Origin Map</button>
            <button type="button" class="mini-button" data-map-source="hosted" id="mapHostedButton">Hosted Map</button>
          </div>
        </div>
        <iframe class="map-frame" id="mapFrame" title="Roadway segment map"></iframe>
        <div class="map-fallback" id="mapFallback"></div>
      </div>
      <div class="panel card notes-card">
        <h3>Reviewer Notes</h3>
        <div class="notes-help">Changes are auto-saved in localStorage for this run. You can revisit any case and edit it at any time before export.</div>
        <div class="status-options" id="statusOptions"></div>
        <textarea id="reviewerNotes" placeholder="Type observations, rationale, or follow-up questions here."></textarea>
        <div class="override-grid" id="overrideGrid"></div>
        <div class="notes-help">`Corrected limit` is required later if you want adjudicated CSV output for a disagree case.</div>
      </div>
    </section>
  </main>
  <script>
__PAGE_SCRIPT__
  </script>
</body>
</html>
"""

PAGE_STYLE = """
    :root {
      color-scheme: light;
      --bg: #f4f6f8;
      --panel: #ffffff;
      --panel-alt: #eef2f5;
      --border: #c7d0d8;
      --border-strong: #8fa1b2;
      --text: #1d2935;
      --muted: #5d6b78;
      --accent: #005f8f;
      --success: #216e39;
      --warn: #8a5600;
      --danger: #a02323;
      --shadow: 0 8px 22px rgba(20, 35, 52, 0.08);
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); line-height: 1.45; }
    button, input, select, textarea { font: inherit; }
    .summary-bar { position: sticky; top: 0; z-index: 10; background: rgba(244,246,248,.96); backdrop-filter: blur(8px); border-bottom: 1px solid var(--border); padding: 16px 20px 14px; }
    .summary-header, .summary-metrics, .summary-controls, .controls-cluster, .view-toggle, .filter-group, .case-controls, .jump-control, .status-options, .map-toolbar { display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: center; }
    .summary-header { justify-content: space-between; margin-bottom: 10px; }
    .summary-title { font-size: 20px; font-weight: 700; }
    .summary-meta { color: var(--muted); font-size: 13px; }
    .summary-controls { margin-top: 10px; justify-content: space-between; }
    .control-label { display: inline-flex; gap: 8px; align-items: center; color: var(--muted); font-size: 14px; }
    .control-label input, .control-label select, .override-field input { min-height: 38px; border: 1px solid var(--border); border-radius: 10px; background: var(--panel); padding: 8px 10px; color: var(--text); }
    .metric-pill, .filter-pill, .view-toggle button, .table-sort, .mini-button, .nav-button { border: 1px solid var(--border); background: var(--panel); color: var(--text); border-radius: 999px; padding: 8px 12px; cursor: pointer; transition: border-color .15s ease, background .15s ease, color .15s ease; }
    .metric-pill { cursor: default; box-shadow: var(--shadow); }
    .filter-pill.active, .view-toggle button.active, .table-sort.active, .mini-button.active { background: var(--accent); border-color: var(--accent); color: #fff; }
    main { padding: 18px 20px 32px; }
    .view.hidden { display: none; }
    .panel, .card { background: var(--panel); border: 1px solid var(--border); border-radius: 18px; box-shadow: var(--shadow); }
    .panel { overflow: hidden; }
    .table-toolbar { display: flex; justify-content: space-between; gap: 10px; padding: 16px 18px 0; }
    .table-wrapper { overflow: auto; padding: 12px 18px 18px; }
    table { width: 100%; border-collapse: collapse; min-width: 860px; }
    thead th { position: sticky; top: 0; background: var(--panel); border-bottom: 1px solid var(--border); padding: 12px 10px; text-align: left; white-space: nowrap; z-index: 1; }
    tbody td { border-bottom: 1px solid #e6ebf0; padding: 12px 10px; vertical-align: top; }
    tbody tr { cursor: pointer; }
    tbody tr:hover { background: #f8fbfd; }
    tbody tr.selected { background: #edf6fb; }
    .resolution-badge, .status-badge, .category-badge { display: inline-flex; align-items: center; gap: 6px; border-radius: 999px; padding: 4px 10px; font-size: 12px; font-weight: 600; white-space: nowrap; }
    .resolution-confirmed, .status-agree { background: #e6f4ea; color: var(--success); }
    .resolution-enriched { background: #e5f1f8; color: var(--accent); }
    .resolution-visual_preferred { background: #fff2df; color: var(--warn); }
    .resolution-conflict, .status-disagree { background: #fde8e8; color: var(--danger); }
    .resolution-visual_only { background: #f2ebfb; color: #6c3fb4; }
    .status-not_reviewed { background: #eef2f5; color: var(--muted); }
    .status-needs_discussion { background: #fff5d9; color: #845900; }
    .category-badge { background: var(--panel-alt); color: var(--muted); margin-top: 8px; }
    .empty-state { padding: 28px 20px; color: var(--muted); }
    .case-shell { display: grid; gap: 16px; }
    .case-toolbar { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px; padding: 16px 18px; }
    .case-title { font-size: 20px; font-weight: 700; }
    .case-subtitle { color: var(--muted); font-size: 14px; margin-top: 4px; }
    .case-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
    .detail-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
    .image-card, .card, .map-card { padding: 16px 18px; }
    .image-card h3, .card h3, .map-card h3 { margin: 0 0 12px; font-size: 16px; }
    .image-card img { width: 100%; min-height: 220px; max-height: 440px; object-fit: contain; border-radius: 12px; border: 1px solid var(--border); background: var(--panel-alt); }
    .image-meta, .card p, .map-note, .notes-help { margin: 8px 0 0; color: var(--muted); font-size: 14px; }
    .card dl { display: grid; grid-template-columns: minmax(0, 130px) minmax(0, 1fr); gap: 8px 10px; margin: 0; }
    .card dt { font-weight: 600; color: var(--muted); }
    .card dd { margin: 0; overflow-wrap: anywhere; }
    .map-toolbar { justify-content: space-between; margin-bottom: 10px; }
    .map-frame { width: 100%; height: 430px; border: 1px solid var(--border); border-radius: 14px; background: #dfe7ee; }
    .map-fallback { margin-top: 12px; padding: 12px 14px; border-radius: 12px; border: 1px dashed var(--border-strong); background: var(--panel-alt); display: grid; gap: 8px; }
    .map-fallback code { display: inline-block; padding: 6px 8px; border-radius: 8px; background: #fff; border: 1px solid var(--border); overflow-wrap: anywhere; }
    .notes-card textarea { width: 100%; min-height: 140px; resize: vertical; border: 1px solid var(--border); border-radius: 12px; padding: 12px; background: #fff; color: var(--text); }
    .status-options { margin: 14px 0; }
    .status-option { display: inline-flex; gap: 8px; align-items: center; padding: 8px 10px; border: 1px solid var(--border); border-radius: 999px; background: #fff; }
    .override-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }
    .override-field { display: grid; gap: 6px; color: var(--muted); font-size: 14px; }
    .checkbox-field { display: flex; align-items: center; gap: 10px; padding: 0 4px; color: var(--text); }
    .muted { color: var(--muted); }
    .mono { font-family: "Cascadia Mono", Consolas, "Courier New", monospace; }
    .image-placeholder { min-height: 220px; display: grid; place-items: center; border: 1px dashed var(--border-strong); border-radius: 12px; background: var(--panel-alt); color: var(--muted); text-align: center; padding: 18px; }
    @media (max-width: 1100px) { .detail-grid, .override-grid { grid-template-columns: 1fr; } }
    @media (max-width: 900px) {
      .case-grid { grid-template-columns: 1fr; }
      .summary-controls, .map-toolbar, .case-toolbar { flex-direction: column; align-items: stretch; }
      .case-controls, .jump-control, .view-toggle, .controls-cluster { width: 100%; }
    }
"""

PAGE_SCRIPT = """
    const REVIEW_DATA = __REVIEW_DATA__;
    const STORAGE_KEY = __STORAGE_KEY__;
    const GENERATED_AT = __GENERATED_AT__;
    const RUN_ID = __RUN_ID__;
    const SAME_ORIGIN_MAP_URL = __SAME_ORIGIN_MAP_URL__;
    const HOSTED_MAP_URL = __HOSTED_MAP_URL__;
    const STATUS_LABELS = {
      not_reviewed: "Not reviewed",
      agree: "Agree",
      disagree: "Disagree",
      needs_discussion: "Needs discussion",
    };

    const state = {
      view: "table",
      scopeFilter: "all",
      resolutionFilter: "all",
      selectedCaseKey: REVIEW_DATA.length ? REVIEW_DATA[0].case_key : null,
      tableSort: { key: "order", direction: "asc" },
      mapSource: window.location.protocol === "file:" ? "hosted" : "same-origin",
      reviewState: loadReviewState(),
    };

    const elements = {
      tableView: document.getElementById("tableView"),
      caseView: document.getElementById("caseView"),
      tableWrapper: document.getElementById("tableWrapper"),
      tableSummary: document.getElementById("tableSummary"),
      summaryMeta: document.getElementById("summaryMeta"),
      summaryMetrics: document.getElementById("summaryMetrics"),
      resolutionFilter: document.getElementById("resolutionFilter"),
      reviewerName: document.getElementById("reviewerName"),
      tableViewButton: document.getElementById("tableViewButton"),
      caseViewButton: document.getElementById("caseViewButton"),
      exportButton: document.getElementById("exportButton"),
      scopeButtons: Array.from(document.querySelectorAll("[data-scope-filter]")),
      caseTitle: document.getElementById("caseTitle"),
      caseSubtitle: document.getElementById("caseSubtitle"),
      prevCaseButton: document.getElementById("prevCaseButton"),
      nextCaseButton: document.getElementById("nextCaseButton"),
      caseJumpSelect: document.getElementById("caseJumpSelect"),
      closeImageCard: document.getElementById("closeImageCard"),
      contextImageCard: document.getElementById("contextImageCard"),
      heuristicCard: document.getElementById("heuristicCard"),
      visualCard: document.getElementById("visualCard"),
      decisionCard: document.getElementById("decisionCard"),
      mapFrame: document.getElementById("mapFrame"),
      mapStatusText: document.getElementById("mapStatusText"),
      mapFallback: document.getElementById("mapFallback"),
      mapSameOriginButton: document.getElementById("mapSameOriginButton"),
      mapHostedButton: document.getElementById("mapHostedButton"),
      statusOptions: document.getElementById("statusOptions"),
      reviewerNotes: document.getElementById("reviewerNotes"),
      overrideGrid: document.getElementById("overrideGrid"),
    };

    let currentMapSrc = "";
    let mapSyncToken = 0;

    function loadReviewState() {
      try {
        const raw = window.localStorage.getItem(STORAGE_KEY);
        if (!raw) return { reviewer: "", cases: {} };
        const parsed = JSON.parse(raw);
        return {
          reviewer: typeof parsed.reviewer === "string" ? parsed.reviewer : "",
          cases: parsed.cases && typeof parsed.cases === "object" ? parsed.cases : {},
        };
      } catch (error) {
        console.warn("Unable to load review state.", error);
        return { reviewer: "", cases: {} };
      }
    }

    function saveReviewState() {
      try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state.reviewState)); }
      catch (error) { console.warn("Unable to save review state.", error); }
    }

    function defaultCaseState() {
      return {
        reviewer_status: "not_reviewed",
        reviewer_notes: "",
        reviewer_corrected_limit: "",
        reviewer_corrected_alias: "",
        reviewer_county_boundary_at_endpoint: false,
      };
    }

    function getCaseState(caseKey) {
      if (!state.reviewState.cases[caseKey]) state.reviewState.cases[caseKey] = defaultCaseState();
      return state.reviewState.cases[caseKey];
    }

    function getFilteredCases() {
      return REVIEW_DATA.filter((item) => {
        if (state.resolutionFilter !== "all" && item.resolution !== state.resolutionFilter) return false;
        const caseState = getCaseState(item.case_key);
        if (state.scopeFilter === "disagreements") return item.resolution !== "confirmed";
        if (state.scopeFilter === "conflicts") return item.resolution === "conflict";
        if (state.scopeFilter === "unreviewed") return caseState.reviewer_status === "not_reviewed";
        return true;
      });
    }

    function getCurrentCase() {
      const filtered = getFilteredCases();
      if (!filtered.length) return null;
      let selected = filtered.find((item) => item.case_key === state.selectedCaseKey);
      if (!selected) {
        selected = filtered[0];
        state.selectedCaseKey = selected.case_key;
      }
      return selected;
    }

    function compareValues(left, right) {
      if (typeof left === "number" && typeof right === "number") return left - right;
      return String(left).localeCompare(String(right), undefined, { sensitivity: "base", numeric: true });
    }

    function tableSortValue(item, key) {
      const caseState = getCaseState(item.case_key);
      if (key === "segment") return item.segment;
      if (key === "side") return item.side + String(item.piece || "");
      if (key === "heuristic") return item.heuristic_limit;
      if (key === "visual") return item.visual_limit;
      if (key === "resolution") return item.resolution;
      if (key === "confidence") return Number(item.final_confidence || 0);
      if (key === "status") return caseState.reviewer_status;
      return Number(item.order || 0);
    }

    function getSortedTableCases() {
      const filtered = [...getFilteredCases()];
      const { key, direction } = state.tableSort;
      filtered.sort((left, right) => {
        const comparison = compareValues(tableSortValue(left, key), tableSortValue(right, key));
        return direction === "asc" ? comparison : -comparison;
      });
      return filtered;
    }

    function formatLabel(value) {
      if (!value) return "None";
      return String(value).replaceAll("_", " ").replace(/\\b\\w/g, (match) => match.toUpperCase());
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function formatConfidence(value, bucket) {
      if (value == null || Number.isNaN(Number(value))) return bucket ? escapeHtml(bucket) : "None";
      const rendered = Number(value).toFixed(3).replace(/0+$/, "").replace(/\\.$/, "");
      return bucket ? `${rendered} (${escapeHtml(bucket)})` : rendered;
    }

    function resolutionClass(value) { return `resolution-${String(value || "").replaceAll(" ", "_")}`; }
    function statusClass(value) { return `status-${String(value || "").replaceAll(" ", "_")}`; }

    function renderSummary() {
      const total = REVIEW_DATA.length;
      const states = REVIEW_DATA.map((item) => getCaseState(item.case_key));
      const reviewed = states.filter((item) => item.reviewer_status !== "not_reviewed").length;
      const agree = states.filter((item) => item.reviewer_status === "agree").length;
      const disagree = states.filter((item) => item.reviewer_status === "disagree").length;
      const needsDiscussion = states.filter((item) => item.reviewer_status === "needs_discussion").length;
      const unreviewed = total - reviewed;
      elements.summaryMeta.textContent = `Run ${RUN_ID} | Generated ${GENERATED_AT}`;
      elements.summaryMetrics.innerHTML = `
        <span class="metric-pill">Total cases: ${total}</span>
        <span class="metric-pill">Reviewed: ${reviewed} of ${total}</span>
        <span class="metric-pill">Agree: ${agree}</span>
        <span class="metric-pill">Disagree: ${disagree}</span>
        <span class="metric-pill">Needs discussion: ${needsDiscussion}</span>
      `;
      document.getElementById("scopeAllButton").textContent = `Show All (${total})`;
      document.getElementById("scopeDisagreementsButton").textContent = `Disagreements Only (${REVIEW_DATA.filter((item) => item.resolution !== "confirmed").length})`;
      document.getElementById("scopeConflictsButton").textContent = `Conflicts Only (${REVIEW_DATA.filter((item) => item.resolution === "conflict").length})`;
      document.getElementById("scopeUnreviewedButton").textContent = `Unreviewed (${unreviewed})`;
      elements.scopeButtons.forEach((button) => button.classList.toggle("active", button.dataset.scopeFilter === state.scopeFilter));
      elements.tableViewButton.classList.toggle("active", state.view === "table");
      elements.caseViewButton.classList.toggle("active", state.view === "case");
      elements.mapSameOriginButton.classList.toggle("active", state.mapSource === "same-origin");
      elements.mapHostedButton.classList.toggle("active", state.mapSource === "hosted");
      elements.resolutionFilter.value = state.resolutionFilter;
      elements.reviewerName.value = state.reviewState.reviewer || "";
    }

    function renderTableView() {
      const rows = getSortedTableCases();
      elements.tableSummary.textContent = rows.length
        ? `${rows.length} case(s) match the current filters. Click a row to open the case view.`
        : "No cases match the current filters.";
      if (!rows.length) {
        elements.tableWrapper.innerHTML = `<div class="empty-state">No rows matched the current filter combination.</div>`;
        return;
      }
      const headers = [
        { key: "segment", label: "Segment" },
        { key: "side", label: "Side" },
        { key: "heuristic", label: "Heuristic" },
        { key: "visual", label: "Visual" },
        { key: "resolution", label: "Resolution" },
        { key: "confidence", label: "Confidence" },
        { key: "status", label: "Status" },
      ];
      const thead = headers.map((header) => {
        const active = state.tableSort.key === header.key;
        const arrow = active ? (state.tableSort.direction === "asc" ? " ^" : " v") : "";
        return `<th><button type="button" class="table-sort${active ? " active" : ""}" data-sort-key="${header.key}">${escapeHtml(header.label + arrow)}</button></th>`;
      }).join("");
      const tbody = rows.map((item) => {
        const caseState = getCaseState(item.case_key);
        const selected = item.case_key === state.selectedCaseKey ? "selected" : "";
        return `
          <tr class="${selected}" data-case-key="${escapeHtml(item.case_key)}">
            <td>${escapeHtml(item.segment)}${item.piece ? `<div class="muted">piece ${item.piece}</div>` : ""}</td>
            <td>${escapeHtml(item.side)}</td>
            <td>${escapeHtml(item.heuristic_limit || "None")}</td>
            <td>${escapeHtml(item.visual_limit || "None")}</td>
            <td><span class="resolution-badge ${resolutionClass(item.resolution)}">${escapeHtml(formatLabel(item.resolution))}</span></td>
            <td>${formatConfidence(item.final_confidence)}</td>
            <td><span class="status-badge ${statusClass(caseState.reviewer_status)}">${escapeHtml(STATUS_LABELS[caseState.reviewer_status])}</span></td>
          </tr>`;
      }).join("");
      elements.tableWrapper.innerHTML = `<table><thead><tr>${thead}</tr></thead><tbody>${tbody}</tbody></table>`;
      Array.from(elements.tableWrapper.querySelectorAll("[data-sort-key]")).forEach((button) => {
        button.addEventListener("click", () => {
          const key = button.dataset.sortKey;
          state.tableSort = state.tableSort.key === key
            ? { key, direction: state.tableSort.direction === "asc" ? "desc" : "asc" }
            : { key, direction: "asc" };
          render();
        });
      });
      Array.from(elements.tableWrapper.querySelectorAll("tbody tr")).forEach((row) => {
        row.addEventListener("click", () => {
          state.selectedCaseKey = row.dataset.caseKey;
          state.view = "case";
          render();
        });
      });
    }

    function renderImageCard(container, title, path, subtitle) {
      const pathHtml = path ? `<div class="image-meta mono">${escapeHtml(path)}</div>` : "";
      if (!path) {
        container.innerHTML = `<h3>${escapeHtml(title)}</h3><div class="image-placeholder">No screenshot path was provided.</div>${subtitle ? `<div class="image-meta">${escapeHtml(subtitle)}</div>` : ""}`;
        return;
      }
      container.innerHTML = `<h3>${escapeHtml(title)}</h3><img src="${escapeHtml(path)}" alt="${escapeHtml(title)}">${pathHtml}${subtitle ? `<div class="image-meta">${escapeHtml(subtitle)}</div>` : ""}`;
      const image = container.querySelector("img");
      image.addEventListener("error", () => {
        image.replaceWith(Object.assign(document.createElement("div"), { className: "image-placeholder", textContent: `Image not found: ${path}` }));
      }, { once: true });
    }

    function definitionRow(label, value) {
      const rendered = value == null || value === "" ? "None" : escapeHtml(value);
      return `<dt>${escapeHtml(label)}</dt><dd>${rendered}</dd>`;
    }

    function updateCaseNavigation(filtered, currentCase) {
      const currentIndex = filtered.findIndex((item) => item.case_key === currentCase.case_key);
      const reviewedCount = REVIEW_DATA.filter((item) => getCaseState(item.case_key).reviewer_status !== "not_reviewed").length;
      elements.caseTitle.textContent = `Case ${currentIndex + 1} of ${filtered.length}: ${currentCase.label}`;
      elements.caseSubtitle.textContent = `${currentCase.type} | ${currentCase.direction || "Direction unavailable"} | Reviewed ${reviewedCount} of ${REVIEW_DATA.length}`;
      elements.prevCaseButton.disabled = currentIndex <= 0;
      elements.nextCaseButton.disabled = currentIndex >= filtered.length - 1;
      elements.caseJumpSelect.innerHTML = filtered.map((item) => `<option value="${escapeHtml(item.case_key)}"${item.case_key === currentCase.case_key ? " selected" : ""}>${escapeHtml(item.label)}</option>`).join("");
    }

    function renderCaseDetails(currentCase) {
      renderImageCard(elements.closeImageCard, "Close Screenshot", currentCase.close_screenshot, currentCase.visual_review_file ? `Batch: ${currentCase.visual_review_file}` : "");
      renderImageCard(elements.contextImageCard, "Context Screenshot", currentCase.context_screenshot, "");
      elements.heuristicCard.innerHTML = `
        <h3>Heuristic Result</h3>
        <dl>
          ${definitionRow("Limit", currentCase.heuristic_limit)}
          ${definitionRow("Confidence", formatConfidence(currentCase.heuristic_confidence, currentCase.heuristic_confidence_bucket))}
          ${definitionRow("Heuristic", currentCase.heuristic_label)}
          ${definitionRow("Coordinates", currentCase.lon != null && currentCase.lat != null ? `${currentCase.lon}, ${currentCase.lat}` : null)}
        </dl>`;
      elements.visualCard.innerHTML = `
        <h3>Visual Review Result</h3>
        <dl>
          ${definitionRow("Limit", currentCase.visual_limit)}
          ${definitionRow("Alias", currentCase.visual_alias)}
          ${definitionRow("Confidence", currentCase.visual_confidence)}
          ${definitionRow("Labels seen", currentCase.visual_labels_seen_text)}
          ${definitionRow("Reasoning", currentCase.visual_reasoning)}
        </dl>`;
      const category = currentCase.category ? `<div class="category-badge">${escapeHtml(formatLabel(currentCase.category))}</div>` : "";
      elements.decisionCard.innerHTML = `
        <h3>Orchestrator Decision</h3>
        <dl>
          ${definitionRow("Resolution", formatLabel(currentCase.resolution))}
          ${definitionRow("Final", currentCase.final_limit)}
          ${definitionRow("Final confidence", formatConfidence(currentCase.final_confidence))}
          ${definitionRow("County boundary", currentCase.county_boundary_at_endpoint ? "Yes" : "No")}
        </dl>${category}`;
    }

    function renderStatusOptions(caseState) {
      elements.statusOptions.innerHTML = Object.entries(STATUS_LABELS).map(([value, label]) => `
        <label class="status-option">
          <input type="radio" name="reviewerStatus" value="${escapeHtml(value)}"${caseState.reviewer_status === value ? " checked" : ""}>
          <span>${escapeHtml(label)}</span>
        </label>`).join("");
      Array.from(elements.statusOptions.querySelectorAll('input[name="reviewerStatus"]')).forEach((input) => {
        input.addEventListener("change", () => {
          const current = getCurrentCase();
          if (!current) return;
          getCaseState(current.case_key).reviewer_status = input.value;
          saveReviewState();
          render();
        });
      });
    }

    function renderOverrideFields(caseState) {
      if (caseState.reviewer_status !== "disagree") {
        elements.overrideGrid.innerHTML = "";
        return;
      }
      elements.overrideGrid.innerHTML = `
        <label class="override-field">
          Corrected limit
          <input type="text" id="overrideLimit" placeholder="Required for disagree cases" value="${escapeHtml(caseState.reviewer_corrected_limit)}">
        </label>
        <label class="override-field">
          Corrected alias
          <input type="text" id="overrideAlias" placeholder="Optional local alias" value="${escapeHtml(caseState.reviewer_corrected_alias)}">
        </label>
        <label class="override-field checkbox-field">
          <input type="checkbox" id="overrideCountyBoundary"${caseState.reviewer_county_boundary_at_endpoint ? " checked" : ""}>
          <span>County boundary at endpoint</span>
        </label>`;
      const current = getCurrentCase();
      if (!current) return;
      document.getElementById("overrideLimit").addEventListener("input", (event) => {
        getCaseState(current.case_key).reviewer_corrected_limit = event.target.value;
        saveReviewState();
      });
      document.getElementById("overrideAlias").addEventListener("input", (event) => {
        getCaseState(current.case_key).reviewer_corrected_alias = event.target.value;
        saveReviewState();
      });
      document.getElementById("overrideCountyBoundary").addEventListener("change", (event) => {
        getCaseState(current.case_key).reviewer_county_boundary_at_endpoint = Boolean(event.target.checked);
        saveReviewState();
      });
    }

    function setMapStatus(message) { elements.mapStatusText.textContent = message; }
    function mapSourceUrl() { return state.mapSource === "same-origin" ? SAME_ORIGIN_MAP_URL : HOSTED_MAP_URL; }
    function coordinateInstruction(caseData) {
      if (caseData.lon == null || caseData.lat == null) return "Coordinates unavailable for this endpoint.";
      return `center: [${caseData.lon}, ${caseData.lat}], zoom: 17`;
    }

    function updateMapFallback(caseData, detail) {
      const coordinateText = coordinateInstruction(caseData);
      const detailText = detail ? `<div class="muted">${escapeHtml(detail)}</div>` : "";
      elements.mapFallback.innerHTML = `
        <div>Navigate to:</div>
        <code id="coordinateCode">${escapeHtml(coordinateText)}</code>
        <div class="controls-cluster">
          <button type="button" class="mini-button" id="copyCoordinatesButton">Copy Coordinates</button>
        </div>
        <div class="muted">Automatic navigation requires the dashboard and web app to share an origin. Serving the repo root locally and using the same-origin map button enables that path.</div>
        ${detailText}`;
      const copyButton = document.getElementById("copyCoordinatesButton");
      if (copyButton) {
        copyButton.addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(coordinateText);
            copyButton.textContent = "Copied";
            window.setTimeout(() => { copyButton.textContent = "Copy Coordinates"; }, 1200);
          } catch (error) {
            window.prompt("Copy coordinates:", coordinateText);
          }
        });
      }
    }

    function ensureMapFrameSource() {
      const nextSrc = mapSourceUrl();
      if (currentMapSrc === nextSrc) return true;
      currentMapSrc = nextSrc;
      elements.mapFrame.src = nextSrc;
      setMapStatus(`Loading ${state.mapSource === "same-origin" ? "same-origin" : "hosted"} map...`);
      return false;
    }

    function sleep(ms) { return new Promise((resolve) => window.setTimeout(resolve, ms)); }

    async function waitForMapApi(frameWindow) {
      for (let attempt = 0; attempt < 20; attempt += 1) {
        if (
          typeof frameWindow.__waitForSegments === "function" &&
          typeof frameWindow.__selectAndZoomSegment === "function" &&
          frameWindow.__mapView &&
          typeof frameWindow.__mapView.goTo === "function"
        ) return;
        await sleep(500);
      }
      throw new Error("Programmatic map API is unavailable from this origin.");
    }

    async function syncMapToCurrentCase() {
      const currentCase = getCurrentCase();
      if (!currentCase) return;
      updateMapFallback(currentCase, "");
      if (!ensureMapFrameSource()) return;
      const token = ++mapSyncToken;
      try {
        const frameWindow = elements.mapFrame.contentWindow;
        if (!frameWindow) throw new Error("Map iframe is not ready yet.");
        await waitForMapApi(frameWindow);
        if (token !== mapSyncToken) return;
        await frameWindow.__waitForSegments();
        if (token !== mapSyncToken) return;
        const selected = await frameWindow.__selectAndZoomSegment(currentCase.segment);
        if (selected === false) throw new Error(`Segment not found in map: ${currentCase.segment}`);
        if (currentCase.lon != null && currentCase.lat != null) {
          await Promise.resolve(frameWindow.__mapView.goTo({ center: [currentCase.lon, currentCase.lat], zoom: 17 }));
        }
        setMapStatus(`Map synchronized to ${currentCase.label}.`);
      } catch (error) {
        const message = error && error.message ? error.message : String(error);
        setMapStatus("Automatic map navigation is unavailable here.");
        updateMapFallback(currentCase, message);
      }
    }

    function renderCaseView() {
      const filtered = getFilteredCases();
      if (!filtered.length) {
        elements.caseTitle.textContent = "No matching cases";
        elements.caseSubtitle.textContent = "Adjust the filters to review a case.";
        elements.closeImageCard.innerHTML = `<div class="empty-state">No case is available under the current filters.</div>`;
        elements.contextImageCard.innerHTML = "";
        elements.heuristicCard.innerHTML = "";
        elements.visualCard.innerHTML = "";
        elements.decisionCard.innerHTML = "";
        elements.statusOptions.innerHTML = "";
        elements.overrideGrid.innerHTML = "";
        elements.reviewerNotes.value = "";
        elements.mapFallback.innerHTML = `<div class="muted">No endpoint coordinates are available under the current filters.</div>`;
        setMapStatus("Map synchronization is paused until a case is visible.");
        return;
      }
      const currentCase = getCurrentCase();
      const caseState = getCaseState(currentCase.case_key);
      updateCaseNavigation(filtered, currentCase);
      renderCaseDetails(currentCase);
      renderStatusOptions(caseState);
      elements.reviewerNotes.value = caseState.reviewer_notes;
      renderOverrideFields(caseState);
      syncMapToCurrentCase();
    }

    function buildExportPayload() {
      return {
        export_date: new Date().toISOString(),
        run_id: RUN_ID,
        reviewer: (state.reviewState.reviewer || "").trim(),
        cases: REVIEW_DATA.map((item) => {
          const caseState = getCaseState(item.case_key);
          return {
            segment: item.segment,
            side: item.side,
            piece: item.piece,
            resolution: item.resolution,
            reviewer_status: caseState.reviewer_status,
            reviewer_notes: caseState.reviewer_notes.trim(),
            reviewer_corrected_limit: caseState.reviewer_status === "disagree" && caseState.reviewer_corrected_limit.trim() ? caseState.reviewer_corrected_limit.trim() : null,
            reviewer_corrected_alias: caseState.reviewer_status === "disagree" && caseState.reviewer_corrected_alias.trim() ? caseState.reviewer_corrected_alias.trim() : null,
            reviewer_county_boundary_at_endpoint: caseState.reviewer_status === "disagree" ? Boolean(caseState.reviewer_county_boundary_at_endpoint) : null,
          };
        }),
      };
    }

    function downloadExport() {
      const payload = buildExportPayload();
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const link = document.createElement("a");
      const stamp = new Date().toISOString().slice(0, 19).replaceAll(":", "").replace("T", "-");
      link.href = URL.createObjectURL(blob);
      link.download = `review-notes-${RUN_ID}-${stamp}.json`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(link.href);
    }

    function render() {
      renderSummary();
      elements.tableView.classList.toggle("hidden", state.view !== "table");
      elements.caseView.classList.toggle("hidden", state.view !== "case");
      renderTableView();
      renderCaseView();
    }

    elements.tableViewButton.addEventListener("click", () => { state.view = "table"; render(); });
    elements.caseViewButton.addEventListener("click", () => { state.view = "case"; render(); });
    elements.scopeButtons.forEach((button) => button.addEventListener("click", () => { state.scopeFilter = button.dataset.scopeFilter; render(); }));
    elements.resolutionFilter.addEventListener("change", (event) => { state.resolutionFilter = event.target.value; render(); });
    elements.reviewerName.addEventListener("input", (event) => { state.reviewState.reviewer = event.target.value; saveReviewState(); renderSummary(); });
    elements.exportButton.addEventListener("click", () => { downloadExport(); });
    elements.prevCaseButton.addEventListener("click", () => {
      const filtered = getFilteredCases();
      const current = getCurrentCase();
      if (!current) return;
      const index = filtered.findIndex((item) => item.case_key === current.case_key);
      if (index > 0) { state.selectedCaseKey = filtered[index - 1].case_key; render(); }
    });
    elements.nextCaseButton.addEventListener("click", () => {
      const filtered = getFilteredCases();
      const current = getCurrentCase();
      if (!current) return;
      const index = filtered.findIndex((item) => item.case_key === current.case_key);
      if (index >= 0 && index < filtered.length - 1) { state.selectedCaseKey = filtered[index + 1].case_key; render(); }
    });
    elements.caseJumpSelect.addEventListener("change", (event) => { state.selectedCaseKey = event.target.value; render(); });
    elements.reviewerNotes.addEventListener("input", (event) => {
      const current = getCurrentCase();
      if (!current) return;
      getCaseState(current.case_key).reviewer_notes = event.target.value;
      saveReviewState();
      renderTableView();
      renderSummary();
    });
    elements.mapSameOriginButton.addEventListener("click", () => { state.mapSource = "same-origin"; currentMapSrc = ""; renderSummary(); syncMapToCurrentCase(); });
    elements.mapHostedButton.addEventListener("click", () => { state.mapSource = "hosted"; currentMapSrc = ""; renderSummary(); syncMapToCurrentCase(); });
    elements.mapFrame.addEventListener("load", () => { syncMapToCurrentCase(); });
    render();
"""


def render_html(
    *,
    review_data: list[dict[str, object]],
    storage_key: str,
    run_id: str,
    output_path: Path,
    generated_at: str,
) -> str:
    same_origin_map_url = relative_path(output_path.parent, ROOT / "Web-App") + "/"
    html = HTML_TEMPLATE
    html = html.replace("__PAGE_STYLE__", PAGE_STYLE)
    html = html.replace("__PAGE_SCRIPT__", PAGE_SCRIPT)
    html = html.replace("__REVIEW_DATA__", json_for_script(review_data))
    html = html.replace("__STORAGE_KEY__", json_for_script(storage_key))
    html = html.replace("__RUN_ID__", json_for_script(run_id))
    html = html.replace("__GENERATED_AT__", json_for_script(generated_at))
    html = html.replace("__SAME_ORIGIN_MAP_URL__", json_for_script(same_origin_map_url))
    html = html.replace("__HOSTED_MAP_URL__", json_for_script(HOSTED_MAP_URL))
    return html


def generate_dashboard(
    *,
    final_results: Path,
    heuristic_results: Path,
    batch_results_dir: Path,
    output_path: Path,
) -> Path:
    final_rows = load_csv_records(
        final_results,
        required={
            "Segment",
            "Type",
            "Side",
            "Piece",
            "Heuristic-Limit",
            "Heuristic-Confidence",
            "Visual-Limit",
            "Visual-Confidence",
            "Final-Limit",
            "Final-Confidence",
            "Resolution",
            "Disagreement-Category",
            "Visual-Labels-Seen",
        },
        label="Final results CSV",
    )
    heuristic_rows = load_csv_records(
        heuristic_results,
        required={
            "Segment",
            "Direction",
            "Type",
            "Side",
            "Piece",
            "Auto-Limit",
            "Heuristic",
            "Confidence",
            "Confidence-Bucket",
        },
        label="Heuristic results CSV",
    )
    heuristic_by_key = load_rows_by_key(heuristic_rows, label="heuristic")
    visual_by_key = load_visual_rows(batch_results_dir)
    review_data = build_review_data(
        final_rows=final_rows,
        heuristic_by_key=heuristic_by_key,
        visual_by_key=visual_by_key,
        output_path=output_path,
    )
    run_id = compute_run_id(review_data)
    storage_key = f"review-dashboard:{run_id}"
    generated_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_html(
            review_data=review_data,
            storage_key=storage_key,
            run_id=run_id,
            output_path=output_path,
            generated_at=generated_at,
        ),
        encoding="utf-8",
    )
    return output_path


def main() -> None:
    args = parse_args()
    visual_review_dir = args.visual_review_dir.resolve()
    final_results = resolve_path(args.final_results, visual_review_dir / "final-segment-limits.csv")
    heuristic_results = resolve_path(args.heuristic_results, visual_review_dir / "heuristic-results.csv")
    batch_results_dir = resolve_path(args.batch_results_dir, visual_review_dir / "batch-results")
    output_path = resolve_path(args.output_path, visual_review_dir / "review-dashboard.html")

    dashboard_path = generate_dashboard(
        final_results=final_results,
        heuristic_results=heuristic_results,
        batch_results_dir=batch_results_dir,
        output_path=output_path,
    )
    print(f"Wrote file: {dashboard_path}")


if __name__ == "__main__":
    main()
