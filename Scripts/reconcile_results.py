#!/usr/bin/env python3
"""Reconcile heuristic endpoint results with visual-review batch JSON files."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "Scripts" / "identify_segment_limits.py"
DEFAULT_VISUAL_REVIEW_DIR = ROOT / "_temp" / "visual-review"
DEFAULT_HEURISTIC_RESULTS = DEFAULT_VISUAL_REVIEW_DIR / "heuristic-results.csv"
DEFAULT_BATCH_RESULTS_DIR = DEFAULT_VISUAL_REVIEW_DIR / "batch-results"
FINAL_RESULTS_NAME = "final-segment-limits.csv"
COLLAPSED_RESULTS_NAME = "final-segment-limits-collapsed.csv"
VISUAL_CONFIDENCE_SCORES = {"high": 0.90, "medium": 0.70, "low": 0.50}


@dataclass(frozen=True)
class EndpointKey:
    segment: str
    side: str
    piece: int | None


def load_module():
    spec = importlib.util.spec_from_file_location("identify_segment_limits", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--heuristic-results",
        type=Path,
        default=DEFAULT_HEURISTIC_RESULTS,
        help=f"Heuristic endpoint CSV. Default: {DEFAULT_HEURISTIC_RESULTS}",
    )
    parser.add_argument(
        "--batch-results-dir",
        type=Path,
        default=DEFAULT_BATCH_RESULTS_DIR,
        help=f"Directory containing batch-NN-results.json files. Default: {DEFAULT_BATCH_RESULTS_DIR}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_VISUAL_REVIEW_DIR,
        help=f"Output directory for final CSVs. Default: {DEFAULT_VISUAL_REVIEW_DIR}",
    )
    return parser.parse_args()


def safe_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def parse_float(value: object) -> float:
    text = safe_text(value)
    if not text:
        return 0.0
    return float(text)


def parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = safe_text(value).lower()
    return text in {"1", "true", "yes", "y"}


def parse_piece(value: object) -> int | None:
    text = safe_text(value)
    if not text:
        return None
    return int(float(text))


def normalize_side(value: object) -> str:
    text = safe_text(value).lower()
    if text == "from":
        return "From"
    if text == "to":
        return "To"
    raise ValueError(f"Unsupported side value: {value!r}")


def endpoint_key(module, segment: object, side: object, piece: object) -> EndpointKey:
    return EndpointKey(
        segment=module.normalize_spacing(safe_text(segment)),
        side=normalize_side(side),
        piece=parse_piece(piece),
    )


def build_fake_candidate(module, value: str):
    return module.LimitCandidate(
        value=value,
        normalized=module.normalize_limit_key(value),
        method="visual",
        confidence=0.0,
        distance_m=0.0,
        detail="visual reconciliation",
    )


def limits_match(module, value_a: str, value_b: str) -> bool:
    if not value_a or not value_b:
        return False
    if module.canonical(value_a) == module.canonical(value_b):
        return True
    if module.normalize_limit_key(value_a) == module.normalize_limit_key(value_b):
        return True
    if module.local_limits_equivalent(value_a, value_b):
        return True
    return bool(
        module.limits_equivalent(value_a, build_fake_candidate(module, value_b))
        or module.limits_equivalent(value_b, build_fake_candidate(module, value_a))
    )


def normalize_offset_direction(value: object) -> str:
    text = safe_text(value).lower()
    mapping = {
        "n": "North",
        "north": "North",
        "s": "South",
        "south": "South",
        "e": "East",
        "east": "East",
        "w": "West",
        "west": "West",
        "ne": "NE",
        "nw": "NW",
        "se": "SE",
        "sw": "SW",
    }
    if text in mapping:
        return mapping[text]
    return text.title()


def render_visual_base_limit(module, visual_row: dict[str, object]) -> str:
    limit_identification = safe_text(visual_row.get("limit_identification"))
    limit_alias = safe_text(visual_row.get("limit_alias"))
    if not limit_alias:
        return limit_identification
    if not limit_identification:
        return limit_alias
    if module.is_route_limit(limit_identification) or module.has_named_road_with_route(limit_identification):
        return module.format_named_route(limit_alias, limit_identification)
    return limit_identification


def render_visual_limit(module, visual_row: dict[str, object]) -> str:
    base_limit = render_visual_base_limit(module, visual_row)
    if not parse_bool(visual_row.get("is_offset")):
        return base_limit

    offset_from = safe_text(visual_row.get("offset_from"))
    anchor = offset_from or base_limit
    if not anchor:
        return base_limit

    direction = normalize_offset_direction(visual_row.get("offset_direction"))
    if direction:
        return f"{direction} of {anchor}"
    return f"Offset from {anchor}"


def visual_labels_seen_text(visual_row: dict[str, object]) -> str:
    values: list[str] = []
    for label in visual_row.get("visible_labels", []) or []:
        text = safe_text(label)
        if text:
            values.append(text)
    for shield in visual_row.get("visible_shields", []) or []:
        text = safe_text(shield)
        if text:
            values.append(f"{text} shield")

    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return ", ".join(deduped)


def load_heuristic_rows(module, heuristic_results: Path):
    dataframe = pd.read_csv(heuristic_results)
    required_columns = {
        "Segment",
        "Direction",
        "Type",
        "Side",
        "Piece",
        "Auto-Limit",
        "Heuristic",
        "Confidence",
        "Confidence-Bucket",
    }
    missing = sorted(required_columns - set(dataframe.columns))
    if missing:
        raise ValueError(
            f"Heuristic results file is missing required columns: {', '.join(missing)}"
        )

    rows_by_key: dict[EndpointKey, dict[str, object]] = {}
    ordered_rows: list[dict[str, object]] = []
    for row in dataframe.to_dict("records"):
        key = endpoint_key(module, row.get("Segment"), row.get("Side"), row.get("Piece"))
        if key in rows_by_key:
            raise ValueError(f"Duplicate heuristic row for {key}")
        rows_by_key[key] = row
        ordered_rows.append(row)
    return ordered_rows, rows_by_key


def load_visual_rows(module, batch_results_dir: Path) -> dict[EndpointKey, dict[str, object]]:
    batch_paths = sorted(batch_results_dir.glob("*.json"))
    if not batch_paths:
        raise FileNotFoundError(f"No batch results were found in {batch_results_dir}")

    visual_rows: dict[EndpointKey, dict[str, object]] = {}
    for path in batch_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"Expected a JSON array in {path}")
        for entry in payload:
            if not isinstance(entry, dict):
                raise ValueError(f"Each visual-review entry must be an object in {path}")
            key = endpoint_key(module, entry.get("segment"), entry.get("side"), entry.get("piece"))
            if key in visual_rows:
                raise ValueError(f"Duplicate visual-review row for {key}")

            visual_confidence = safe_text(entry.get("visual_confidence")).lower()
            if visual_confidence not in VISUAL_CONFIDENCE_SCORES:
                raise ValueError(
                    f"Unsupported visual_confidence {visual_confidence!r} in {path}"
                )

            entry = dict(entry)
            entry["_source_file"] = path.name
            entry["_visual_confidence_numeric"] = VISUAL_CONFIDENCE_SCORES[visual_confidence]
            entry["_visual_base_limit"] = render_visual_base_limit(module, entry)
            entry["_visual_display_limit"] = render_visual_limit(module, entry)
            entry["_visual_labels_seen"] = visual_labels_seen_text(entry)

            if not safe_text(entry["_visual_display_limit"]):
                raise ValueError(
                    f"Visual-review entry has no usable limit for {key} in {path}"
                )

            visual_rows[key] = entry
    return visual_rows


def round_confidence(value: float) -> float:
    return round(value, 3)


def resolve_endpoint(
    module,
    heuristic_row: dict[str, object],
    visual_row: dict[str, object],
) -> dict[str, object]:
    heuristic_limit = safe_text(heuristic_row.get("Auto-Limit"))
    heuristic_confidence = parse_float(heuristic_row.get("Confidence"))
    visual_bucket = safe_text(visual_row.get("visual_confidence")).lower()
    visual_numeric = float(visual_row["_visual_confidence_numeric"])
    visual_base_limit = safe_text(visual_row["_visual_base_limit"])
    visual_display_limit = safe_text(visual_row["_visual_display_limit"])
    visual_has_alias = bool(safe_text(visual_row.get("limit_alias")))

    heuristic_has_offset = module.has_directional_description(heuristic_limit)
    visual_is_offset = parse_bool(visual_row.get("is_offset"))
    heuristic_is_county = module.is_county_limit(heuristic_limit)
    visual_is_county = parse_bool(visual_row.get("county_boundary_at_endpoint"))
    same_road = limits_match(module, heuristic_limit, visual_base_limit)
    heuristic_is_route_only = bool(heuristic_limit) and module.is_route_limit(
        heuristic_limit
    ) and not module.has_named_road_with_route(heuristic_limit)

    resolution = ""
    category = ""
    final_limit = heuristic_limit

    if not heuristic_limit:
        resolution = "visual_only"
        category = "other"
        final_limit = visual_display_limit
    elif visual_is_county and not heuristic_is_county:
        resolution = "visual_preferred" if visual_numeric >= 0.70 else "conflict"
        category = "county_not_detected"
        final_limit = visual_display_limit if resolution == "visual_preferred" else heuristic_limit
    elif heuristic_has_offset != visual_is_offset:
        resolution = "visual_preferred" if visual_numeric >= 0.70 else "conflict"
        category = "offset_extra" if heuristic_has_offset else "offset_missing"
        final_limit = visual_display_limit if resolution == "visual_preferred" else heuristic_limit
    elif (
        heuristic_has_offset
        and visual_is_offset
        and same_road
        and module.canonical(heuristic_limit) != module.canonical(visual_display_limit)
    ):
        resolution = "visual_preferred" if visual_numeric >= 0.70 else "conflict"
        category = "offset_direction"
        final_limit = visual_display_limit if resolution == "visual_preferred" else heuristic_limit
    elif same_road:
        if heuristic_is_route_only and visual_has_alias:
            resolution = "enriched"
            category = "alias_enrichment"
            final_limit = visual_base_limit
        else:
            resolution = "confirmed"
            final_limit = visual_display_limit if visual_is_offset else heuristic_limit
    else:
        resolution = "visual_preferred" if visual_numeric >= 0.70 else "conflict"
        category = "different_road"
        final_limit = visual_display_limit if resolution == "visual_preferred" else heuristic_limit

    if resolution == "confirmed":
        final_confidence = max(heuristic_confidence, 0.92)
    elif resolution == "enriched":
        final_confidence = max(heuristic_confidence, 0.90)
    elif resolution == "visual_preferred":
        final_confidence = visual_numeric
    elif resolution == "conflict":
        final_confidence = max(heuristic_confidence, visual_numeric) * 0.6
    elif resolution == "visual_only":
        final_confidence = visual_numeric * 0.9
    else:
        raise RuntimeError(f"Unhandled resolution: {resolution}")

    return {
        "Segment": safe_text(heuristic_row.get("Segment")),
        "Direction": safe_text(heuristic_row.get("Direction")),
        "Type": safe_text(heuristic_row.get("Type")) or "Continuous",
        "Side": normalize_side(heuristic_row.get("Side")),
        "Piece": parse_piece(heuristic_row.get("Piece")),
        "Heuristic-Limit": heuristic_limit,
        "Heuristic-Confidence": heuristic_confidence,
        "Visual-Limit": visual_display_limit,
        "Visual-Confidence": visual_bucket,
        "Final-Limit": final_limit,
        "Final-Confidence": round_confidence(final_confidence),
        "Resolution": resolution,
        "Disagreement-Category": category,
        "Visual-Labels-Seen": safe_text(visual_row.get("_visual_labels_seen")),
    }


def collapse_segment_rows(final_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    by_segment: dict[str, list[dict[str, object]]] = {}
    for row in final_rows:
        by_segment.setdefault(row["Segment"], []).append(row)

    collapsed_rows: list[dict[str, object]] = []
    for segment_rows in by_segment.values():
        from_rows = [row for row in segment_rows if row["Side"] == "From"]
        to_rows = [row for row in segment_rows if row["Side"] == "To"]
        if not from_rows or not to_rows:
            raise ValueError(f"Segment is missing From or To rows: {segment_rows[0]['Segment']}")

        from_row = min(from_rows, key=lambda row: row["Piece"] or 0)
        to_row = max(to_rows, key=lambda row: row["Piece"] or 0)
        collapsed_rows.append(
            {
                "Segment": from_row["Segment"],
                "Direction": from_row["Direction"],
                "Type": from_row["Type"],
                "Final-From": from_row["Final-Limit"],
                "Final-To": to_row["Final-Limit"],
                "From-Confidence": from_row["Final-Confidence"],
                "To-Confidence": to_row["Final-Confidence"],
                "From-Resolution": from_row["Resolution"],
                "To-Resolution": to_row["Resolution"],
            }
        )
    return collapsed_rows


def write_outputs(
    output_dir: Path,
    final_rows: list[dict[str, object]],
    collapsed_rows: list[dict[str, object]],
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    final_path = output_dir / FINAL_RESULTS_NAME
    collapsed_path = output_dir / COLLAPSED_RESULTS_NAME

    final_columns = [
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
    ]
    final_dataframe = pd.DataFrame(final_rows)
    final_dataframe["Piece"] = final_dataframe["Piece"].map(
        lambda value: "" if pd.isna(value) else int(value)
    )
    final_dataframe.to_csv(final_path, index=False, columns=final_columns)

    collapsed_columns = [
        "Segment",
        "Direction",
        "Type",
        "Final-From",
        "Final-To",
        "From-Confidence",
        "To-Confidence",
        "From-Resolution",
        "To-Resolution",
    ]
    pd.DataFrame(collapsed_rows).to_csv(
        collapsed_path,
        index=False,
        columns=collapsed_columns,
    )
    return final_path, collapsed_path


def reconcile(
    *,
    heuristic_results: Path,
    batch_results_dir: Path,
    output_dir: Path,
) -> tuple[Path, Path]:
    module = load_module()
    heuristic_rows, heuristic_by_key = load_heuristic_rows(module, heuristic_results)
    visual_by_key = load_visual_rows(module, batch_results_dir)

    extra_visual_keys = sorted(set(visual_by_key) - set(heuristic_by_key), key=lambda key: (key.segment, key.piece or 0, key.side))
    if extra_visual_keys:
        extra_text = ", ".join(
            f"{key.segment} / {key.side} / piece {key.piece or '-'}" for key in extra_visual_keys
        )
        raise ValueError(f"Visual results contain endpoints not present in heuristic-results.csv: {extra_text}")

    final_rows: list[dict[str, object]] = []
    missing_visual_keys: list[EndpointKey] = []
    for heuristic_row in heuristic_rows:
        key = endpoint_key(module, heuristic_row.get("Segment"), heuristic_row.get("Side"), heuristic_row.get("Piece"))
        visual_row = visual_by_key.get(key)
        if visual_row is None:
            missing_visual_keys.append(key)
            continue
        final_rows.append(resolve_endpoint(module, heuristic_row, visual_row))

    if missing_visual_keys:
        missing_text = ", ".join(
            f"{key.segment} / {key.side} / piece {key.piece or '-'}" for key in missing_visual_keys
        )
        raise ValueError(f"Missing visual results for heuristic endpoints: {missing_text}")

    collapsed_rows = collapse_segment_rows(final_rows)
    return write_outputs(output_dir, final_rows, collapsed_rows)


def main() -> None:
    args = parse_args()
    final_path, collapsed_path = reconcile(
        heuristic_results=args.heuristic_results.resolve(),
        batch_results_dir=args.batch_results_dir.resolve(),
        output_dir=args.output_dir.resolve(),
    )

    final_df = pd.read_csv(final_path)
    counts = final_df["Resolution"].value_counts(dropna=False).to_dict()

    print(f"Wrote file: {final_path}")
    print(f"Wrote file: {collapsed_path}")
    print(f"Endpoint rows reconciled: {len(final_df)}")
    for resolution in ("confirmed", "enriched", "visual_preferred", "conflict", "visual_only"):
        print(f"  {resolution}: {counts.get(resolution, 0)}")


if __name__ == "__main__":
    main()
