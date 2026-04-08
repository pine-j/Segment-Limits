#!/usr/bin/env python3
"""Evaluate identify_segment_limits.py against Amy's review data.

Reports two accuracy metrics:
  1. All-sides: every From/To endpoint across all 150 segments (300 sides)
  2. Trusted-only: sides where MCP-Summary == "Correct" and Amy agrees with MCP

Also categorizes mismatches by pattern.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "Scripts" / "identify_segment_limits.py"
DEFAULT_REVIEW_CSV = ROOT / "FTW-Segments-Limits-Amy.review.csv"
DEFAULT_OUTPUT_CSV = ROOT / "_temp" / "trusted-review-eval.csv"


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
    parser.add_argument("--review-csv", type=Path, default=DEFAULT_REVIEW_CSV)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_OUTPUT_CSV)
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args()


def categorize_mismatch(gold: str, pred: str) -> str:
    gold_upper = gold.upper()
    pred_upper = pred.upper()
    dir_pat = r"^(N|S|E|W|NE|NW|SE|SW)\s+"
    g_stripped = re.sub(dir_pat, "", gold_upper)
    p_stripped = re.sub(dir_pat, "", pred_upper)

    if g_stripped == p_stripped and gold_upper != pred_upper:
        return "directional_prefix"
    if " OF " in gold_upper and " OF " not in pred_upper:
        return "offset_missing"
    if " OF " in pred_upper and " OF " not in gold_upper:
        return "offset_extra"
    if " OF " in gold_upper and " OF " in pred_upper:
        return "offset_different"
    if "FRONTAGE" in pred_upper and "FRONTAGE" not in gold_upper:
        return "frontage_instead"
    if "FRONTAGE" in gold_upper and "FRONTAGE" not in pred_upper:
        return "missing_frontage"
    if "COUNTY" in gold_upper or "COUNTY" in pred_upper:
        return "county_wording"
    if "(" in gold and ")" in gold and "(" not in pred:
        return "alias_missing"
    if "(" in pred and ")" in pred and "(" not in gold:
        return "alias_extra"
    return "different_road"


def trusted_side_specs(review_df: pd.DataFrame, module) -> list[dict[str, str]]:
    """All sides are now treated as trusted — MCP review columns have been removed."""
    return all_side_specs(review_df)


def all_side_specs(review_df: pd.DataFrame) -> list[dict[str, str]]:
    sides: list[dict[str, str]] = []
    for row in review_df.to_dict("records"):
        # Skip gap segments — they use piece-by-piece limits, not simple From/To
        amy_from = str(row.get("Limts-From", "")).strip()
        amy_to = str(row.get("Limits-To", "")).strip()
        # Amy records only the overall segment limits, so gap interiors stay unscored.
        if amy_from and amy_from != "nan":
            sides.append(
                {"segment": str(row["Segment"]), "side": "From", "gold": amy_from}
            )
        if amy_to and amy_to != "nan":
            sides.append(
                {"segment": str(row["Segment"]), "side": "To", "gold": amy_to}
            )
    return sides


def score_sides(
    side_specs: list[dict[str, str]],
    predictions: dict[str, dict[str, object]],
    module,
) -> tuple[int, list[dict[str, str]]]:
    correct = 0
    mismatch_rows: list[dict[str, str]] = []
    for spec in side_specs:
        prediction_row = predictions.get(spec["segment"])
        if prediction_row is None:
            mismatch_rows.append(
                {
                    "Segment": spec["segment"],
                    "Side": spec["side"],
                    "Gold": spec["gold"],
                    "Predicted": "",
                    "Heuristic": "",
                    "Category": "no_prediction",
                }
            )
            continue
        predicted = (
            prediction_row["Auto Limits From"]
            if spec["side"] == "From"
            else prediction_row["Auto Limits To"]
        )
        heuristic = (
            prediction_row["Heuristic-From"]
            if spec["side"] == "From"
            else prediction_row["Heuristic-To"]
        )
        predicted = str(predicted).strip() if predicted and str(predicted) != "nan" else ""
        heuristic = str(heuristic).strip() if heuristic and str(heuristic) != "nan" else ""
        matched = module.canonical(predicted) == module.canonical(spec["gold"])
        correct += int(matched)
        if not matched:
            mismatch_rows.append(
                {
                    "Segment": spec["segment"],
                    "Side": spec["side"],
                    "Gold": spec["gold"],
                    "Predicted": predicted,
                    "Heuristic": heuristic,
                    "Category": categorize_mismatch(spec["gold"], predicted),
                }
            )
    return correct, mismatch_rows


def print_category_summary(mismatch_rows: list[dict[str, str]], label: str) -> None:
    categories: dict[str, list[dict[str, str]]] = {}
    for row in mismatch_rows:
        categories.setdefault(row["Category"], []).append(row)

    print(f"\n{label} mismatch categories:")
    for cat in sorted(categories.keys(), key=lambda c: -len(categories[c])):
        items = categories[cat]
        print(f"  {cat}: {len(items)}")
        for item in items:
            print(
                f"    {item['Segment']} / {item['Side']}: "
                f"gold=\"{item['Gold']}\" pred=\"{item['Predicted']}\""
            )


def evaluate(review_csv: Path, output_csv: Path, workers: int) -> None:
    module = load_module()
    review_df = pd.read_csv(review_csv)

    segment_features = module.load_segment_features()
    by_readable, by_family = module.build_lookups(segment_features)
    counties = module.build_county_lookup(module.load_counties())
    roadway_inventory_lookup = module.load_local_roadway_inventory_lookup(
        str(module.DEFAULT_ROADWAY_INVENTORY_PATH.resolve())
    )
    context = module.RowProcessingContext(
        by_readable=by_readable,
        by_family=by_family,
        compare_mode=False,
        counties=counties,
        all_segments=segment_features,
        label_tile_root=str(module.DEFAULT_LABEL_TILE_ROOT.resolve()),
        roadway_inventory_lookup=roadway_inventory_lookup,
    )

    records = review_df.to_dict("records")
    results: list[object | None] = [None] * len(records)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_map = {
            executor.submit(
                module.process_request_row, index, row, context=context
            ): index
            for index, row in enumerate(records)
        }
        for future in as_completed(future_map):
            result = future.result()
            results[result.index] = result

    ordered_results = [result for result in results if result is not None]
    scored_rows: list[dict[str, object]] = []
    for row, result in zip(records, ordered_results, strict=True):
        scored_rows.append(
            {
                "Segment": row["Segment"],
                "Auto Limits From": result.auto_from,
                "Auto Limits To": result.auto_to,
                "Heuristic-From": result.heuristic_from,
                "Heuristic-To": result.heuristic_to,
                "Auto Review Notes": result.note,
            }
        )

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(scored_rows).to_csv(output_csv, index=False)

    predictions = {row["Segment"]: row for row in scored_rows}

    # --- All sides (300) ---
    all_sides = all_side_specs(review_df)
    all_correct, all_mismatches = score_sides(all_sides, predictions, module)
    all_accuracy = (all_correct / len(all_sides) * 100.0) if all_sides else 0.0

    all_mismatch_path = output_csv.with_name(f"{output_csv.stem}.all-mismatches.csv")
    pd.DataFrame(all_mismatches).to_csv(all_mismatch_path, index=False)

    print(f"=== All sides ===")
    print(f"Total sides: {len(all_sides)}")
    print(f"Exact-match accuracy: {all_correct}/{len(all_sides)} ({all_accuracy:.2f}%)")
    print_category_summary(all_mismatches, "All-sides")

    # --- Trusted sides ---
    trusted_sides = trusted_side_specs(review_df, module)
    trusted_correct, trusted_mismatches = score_sides(
        trusted_sides, predictions, module
    )
    trusted_accuracy = (
        (trusted_correct / len(trusted_sides) * 100.0) if trusted_sides else 0.0
    )

    trusted_mismatch_path = output_csv.with_name(
        f"{output_csv.stem}.trusted-mismatches.csv"
    )
    pd.DataFrame(trusted_mismatches).to_csv(trusted_mismatch_path, index=False)

    print(f"\n=== Trusted sides ===")
    print(f"Trusted sides: {len(trusted_sides)}")
    print(
        f"Exact-match accuracy: {trusted_correct}/{len(trusted_sides)} ({trusted_accuracy:.2f}%)"
    )
    print_category_summary(trusted_mismatches, "Trusted-sides")

    print(f"\nPredictions CSV: {output_csv}")
    print(f"All-mismatches CSV: {all_mismatch_path}")
    print(f"Trusted-mismatches CSV: {trusted_mismatch_path}")


def main() -> None:
    args = parse_args()
    evaluate(
        review_csv=args.review_csv.resolve(),
        output_csv=args.output_csv.resolve(),
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
