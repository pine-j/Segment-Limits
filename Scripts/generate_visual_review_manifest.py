#!/usr/bin/env python3
"""Generate heuristic endpoint data and a visual-review manifest."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "Scripts" / "identify_segment_limits.py"
DEFAULT_INPUT_CSV = ROOT / "FTW-Segments-Limits-Amy.review.csv"
DEFAULT_OUTPUT_DIR = ROOT / "_temp" / "visual-review"


def load_module():
    spec = importlib.util.spec_from_file_location("identify_segment_limits", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_parser(module) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        "--input-csv",
        dest="input_csv",
        type=Path,
        default=DEFAULT_INPUT_CSV,
        help="CSV containing a segment-name column. Ignored when --all is used.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run against every segment in the ArcGIS layer instead of a CSV subset.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional row limit after segment selection.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=module.DEFAULT_MAX_WORKERS,
        help=f"Worker threads for heuristic processing. Default: {module.DEFAULT_MAX_WORKERS}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output root. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--label-tile-root",
        type=Path,
        default=module.DEFAULT_LABEL_TILE_ROOT,
        help=f"Local label tile cache. Default: {module.DEFAULT_LABEL_TILE_ROOT}",
    )
    parser.add_argument(
        "--roadway-inventory-path",
        type=Path,
        default=module.DEFAULT_ROADWAY_INVENTORY_PATH,
        help=f"Local roadway inventory subset. Default: {module.DEFAULT_ROADWAY_INVENTORY_PATH}",
    )
    parser.add_argument(
        "--download-label-tiles-first",
        action="store_true",
        help="Populate the local label tile cache before processing.",
    )
    parser.add_argument(
        "--download-roadway-inventory-subset-first",
        action="store_true",
        help="Populate the local roadway inventory subset before processing.",
    )
    parser.add_argument(
        "--use-live-label-tiles",
        action="store_true",
        help="Bypass the local label tile cache and query live tiles only.",
    )
    parser.add_argument(
        "--use-live-roadway-inventory",
        action="store_true",
        help="Bypass the local roadway inventory subset and query live services only.",
    )
    return parser


def normalize_header(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def detect_segment_column(dataframe: pd.DataFrame) -> str:
    normalized = {column: normalize_header(column) for column in dataframe.columns}
    preferred = [
        "segment",
        "segmentname",
        "segmentid",
        "readablesegid",
        "readablesegmentid",
    ]
    for target in preferred:
        for column, normalized_name in normalized.items():
            if normalized_name == target:
                return column
    for column, normalized_name in normalized.items():
        if "segment" in normalized_name:
            return column
    if len(dataframe.columns) == 1:
        return str(dataframe.columns[0])
    raise ValueError(
        "Could not detect a segment-name column. Add a column like 'Segment' or pass a one-column CSV."
    )


def load_segment_names(input_csv: Path, module) -> list[str]:
    dataframe = pd.read_csv(input_csv)
    segment_column = detect_segment_column(dataframe)
    names: list[str] = []
    seen: set[str] = set()
    for value in dataframe[segment_column].tolist():
        if value is None or pd.isna(value):
            continue
        name = module.normalize_spacing(str(value))
        if not name or name.lower() == "nan" or name in seen:
            continue
        names.append(name)
        seen.add(name)
    if not names:
        raise ValueError(f"No segment names were found in column '{segment_column}' of {input_csv}")
    print(f"Using segment column '{segment_column}' from {input_csv}")
    return names


def ensure_output_dirs(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "batch-prompts").mkdir(parents=True, exist_ok=True)
    (output_dir / "batch-results").mkdir(parents=True, exist_ok=True)
    (output_dir / "screenshots").mkdir(parents=True, exist_ok=True)


def build_context(module, args: argparse.Namespace):
    print("Fetching segment geometries...")
    segment_features = module.load_segment_features()
    by_readable, by_family = module.build_lookups(segment_features)

    label_tile_root_str: str | None = None
    if args.download_label_tiles_first:
        print(f"Downloading label tiles to {args.label_tile_root}...")
        label_counts = module.download_label_tiles(
            output_root=args.label_tile_root,
            segment_features=segment_features,
        )
        print(
            "Label tile cache ready: "
            f"{label_counts['total']} jobs, "
            f"{label_counts['downloaded']} downloaded, "
            f"{label_counts['cached']} already cached, "
            f"{label_counts['missing']} missing"
        )
    if not args.use_live_label_tiles:
        label_tile_root_str = str(args.label_tile_root.resolve())
        if args.label_tile_root.exists():
            print(f"Using local label tile cache at {args.label_tile_root}...")

    roadway_inventory_lookup = None
    if args.download_roadway_inventory_subset_first:
        print(f"Downloading roadway inventory subset to {args.roadway_inventory_path}...")
        subset_count = module.download_roadway_inventory_subset(
            output_path=args.roadway_inventory_path,
            segment_features=segment_features,
        )
        print(
            f"Wrote roadway inventory subset: {args.roadway_inventory_path} ({subset_count} features)"
        )
    if not args.use_live_roadway_inventory and args.roadway_inventory_path.exists():
        print(f"Loading roadway inventory subset from {args.roadway_inventory_path}...")
        roadway_inventory_lookup = module.load_local_roadway_inventory_lookup(
            str(args.roadway_inventory_path.resolve())
        )
        print(f"Loaded {len(roadway_inventory_lookup.features)} roadway inventory features.")

    print("Fetching county boundaries...")
    counties = module.build_county_lookup(module.load_counties())

    context = module.RowProcessingContext(
        by_readable=by_readable,
        by_family=by_family,
        compare_mode=False,
        counties=counties,
        all_segments=segment_features,
        label_tile_root=label_tile_root_str,
        roadway_inventory_lookup=roadway_inventory_lookup,
    )
    return segment_features, context


def build_request_dataframe(module, args: argparse.Namespace, segment_features: list[object]) -> pd.DataFrame:
    if args.all:
        print("Using all ArcGIS segments.")
        return module.build_request_dataframe(
            compare_csv_path=None,
            segment_names=[],
            segment_features=segment_features,
            limit=args.limit,
        )

    segment_names = load_segment_names(args.input_csv, module)
    return module.build_request_dataframe(
        compare_csv_path=None,
        segment_names=segment_names,
        segment_features=segment_features,
        limit=args.limit,
    )


def process_records(module, records: list[dict[str, object]], context, workers: int):
    results: list[object | None] = [None] * len(records)
    workers = max(1, workers)
    total_rows = len(records)
    if workers > 1 and total_rows > 1:
        print(f"Processing {total_rows} rows with {workers} worker threads...")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(module.process_request_row, index, row, context=context): index
                for index, row in enumerate(records)
            }
            for future in as_completed(future_map):
                result = future.result()
                results[result.index] = result
                print(f"[{result.index + 1}/{total_rows}] {result.segment_name}")
    else:
        for index, row in enumerate(records):
            result = module.process_request_row(index, row, context=context)
            results[index] = result
            print(f"[{index + 1}/{total_rows}] {result.segment_name}")
    return [result for result in results if result is not None]


def route_family_for_segment(module, segment_name: str) -> str:
    return module.readable_to_route_family(
        module.ROUTE_FAMILY_OVERRIDES.get(segment_name, segment_name)
    )


def piece_endpoint_hint(piece: int, piece_count: int, side: str) -> str:
    side_label = "start" if side == "From" else "end"
    return (
        f"{side_label} of piece {piece} "
        f"(this segment has a physical gap - {piece_count} separate pieces)"
    )


def _build_family_to_suffixed_lookup(
    results: list[object],
    module,
) -> dict[str, list[str]]:
    """Map route_family to sorted list of suffixed segment names in the app."""
    family_segments: dict[str, set[str]] = {}
    for result in results:
        if result.segment_type == "Gap":
            continue
        family = route_family_for_segment(module, result.segment_name)
        family_segments.setdefault(family, set()).add(result.segment_name)
    return {family: sorted(names) for family, names in family_segments.items()}


def build_outputs(results: list[object], module) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    heuristic_rows: list[dict[str, object]] = []
    manifest_rows: list[dict[str, object]] = []
    family_lookup = _build_family_to_suffixed_lookup(results, module)

    for result in results:
        route_family = route_family_for_segment(module, result.segment_name)
        if result.segment_type == "Gap" and result.gap_piece_endpoints:
            piece_count = len(result.gap_piece_endpoints)
            app_segment_names = family_lookup.get(route_family, [])
            for piece_info in result.gap_piece_endpoints:
                piece_number = int(piece_info["piece"])
                for side in ("From", "To"):
                    is_from = side == "From"
                    coords = piece_info["from_wgs84"] if is_from else piece_info["to_wgs84"]
                    heuristic_rows.append(
                        {
                            "Segment": result.segment_name,
                            "Direction": result.segment_direction,
                            "Type": "Gap",
                            "Side": side,
                            "Piece": piece_number,
                            "Auto-Limit": piece_info["from_limit"] if is_from else piece_info["to_limit"],
                            "Heuristic": piece_info["from_heuristic"] if is_from else piece_info["to_heuristic"],
                            "Confidence": piece_info["from_confidence"] if is_from else piece_info["to_confidence"],
                            "Confidence-Bucket": module.confidence_bucket(
                                piece_info["from_confidence"] if is_from else piece_info["to_confidence"]
                            ),
                            "Lon": coords[0],
                            "Lat": coords[1],
                        }
                    )
                    manifest_entry: dict[str, object] = {
                        "segment": result.segment_name,
                        "side": side,
                        "type": "Gap",
                        "piece": piece_number,
                        "piece_count": piece_count,
                        "lon": coords[0],
                        "lat": coords[1],
                        "direction": result.segment_direction,
                        "route_family": route_family,
                        "endpoint_hint": piece_endpoint_hint(piece_number, piece_count, side),
                    }
                    if app_segment_names:
                        manifest_entry["app_segment_names"] = app_segment_names
                    manifest_rows.append(manifest_entry)
            continue

        continuous_points = [
            (
                "From",
                result.auto_from,
                result.heuristic_from,
                result.confidence_from,
                result.from_endpoint_wgs84,
                "start of the teal segment line",
            ),
            (
                "To",
                result.auto_to,
                result.heuristic_to,
                result.confidence_to,
                result.to_endpoint_wgs84,
                "end of the teal segment line",
            ),
        ]
        for side, auto_limit, heuristic, confidence, coords, endpoint_hint in continuous_points:
            lon = coords[0] if coords is not None else None
            lat = coords[1] if coords is not None else None
            heuristic_rows.append(
                {
                    "Segment": result.segment_name,
                    "Direction": result.segment_direction,
                    "Type": result.segment_type or "Continuous",
                    "Side": side,
                    "Piece": None,
                    "Auto-Limit": auto_limit,
                    "Heuristic": heuristic,
                    "Confidence": confidence,
                    "Confidence-Bucket": module.confidence_bucket(confidence),
                    "Lon": lon,
                    "Lat": lat,
                }
            )
            manifest_rows.append(
                {
                    "segment": result.segment_name,
                    "side": side,
                    "type": result.segment_type or "Continuous",
                    "lon": lon,
                    "lat": lat,
                    "direction": result.segment_direction,
                    "route_family": route_family,
                    "endpoint_hint": endpoint_hint,
                }
            )

    return heuristic_rows, manifest_rows


def write_outputs(
    output_dir: Path,
    heuristic_rows: list[dict[str, object]],
    manifest_rows: list[dict[str, object]],
) -> tuple[Path, Path]:
    heuristic_path = output_dir / "heuristic-results.csv"
    manifest_path = output_dir / "visual-review-manifest.json"

    heuristic_columns = [
        "Segment",
        "Direction",
        "Type",
        "Side",
        "Piece",
        "Auto-Limit",
        "Heuristic",
        "Confidence",
        "Confidence-Bucket",
        "Lon",
        "Lat",
    ]
    pd.DataFrame(heuristic_rows, columns=heuristic_columns).to_csv(heuristic_path, index=False)
    manifest_path.write_text(json.dumps(manifest_rows, indent=2), encoding="utf-8")
    return heuristic_path, manifest_path


def main() -> None:
    module = load_module()
    parser = build_parser(module)
    args = parser.parse_args()

    ensure_output_dirs(args.output_dir)
    segment_features, context = build_context(module, args)
    request_df = build_request_dataframe(module, args, segment_features)
    records = request_df.to_dict("records")
    if not records:
        raise RuntimeError("No segments were selected for manifest generation.")

    ordered_results = process_records(module, records, context, args.workers)
    heuristic_rows, manifest_rows = build_outputs(ordered_results, module)
    heuristic_path, manifest_path = write_outputs(args.output_dir, heuristic_rows, manifest_rows)

    print(f"\nWrote file: {heuristic_path}")
    print(f"Wrote file: {manifest_path}")
    print(f"Heuristic endpoint rows: {len(heuristic_rows)}")
    print(f"Manifest endpoints: {len(manifest_rows)}")


if __name__ == "__main__":
    main()
