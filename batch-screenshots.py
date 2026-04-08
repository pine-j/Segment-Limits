"""
Batch screenshot capture for the FTW Segment Explorer visual review pipeline.

Uses Playwright as a Python library (NOT MCP) to open the web app once,
then iterates through every endpoint in the manifest and captures close/context
screenshots using ArcGIS MapView.takeScreenshot() — the GPU-native capture path.

Usage:
    python batch-screenshots.py [options]

Options:
    --url URL           Web app URL (default: GitHub Pages)
    --local             Use local server at http://localhost:8080
    --manifest PATH     Path to visual-review-manifest.json
    --outdir PATH       Screenshot output directory
    --batch-size N      Endpoints per batch (default: 15)
    --start-batch N     First batch to process (1-indexed, default: 1)
    --end-batch N       Last batch to process (inclusive, default: all)
    --overwrite         Re-capture existing screenshots
    --headless          Run browser headless (default: headed for tile rendering)
    --close-zoom N      Zoom level for close screenshots (default: 17)
    --context-zoom N    Zoom level for context screenshots (default: 15)
"""

import argparse
import base64
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


DEFAULT_URL = "https://pine-j.github.io/Roadway-Segment-Limits/"
LOCAL_URL = "http://localhost:8080"
DEFAULT_MANIFEST = Path("_temp/visual-review/visual-review-manifest.json")
DEFAULT_OUTDIR = Path("_temp/visual-review/screenshots")
BATCH_SIZE = 15


def parse_args():
    parser = argparse.ArgumentParser(description="Batch screenshot capture for visual review")
    parser.add_argument("--url", default=DEFAULT_URL, help="Web app URL")
    parser.add_argument("--local", action="store_true", help="Use local server")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--start-batch", type=int, default=1, help="First batch (1-indexed)")
    parser.add_argument("--end-batch", type=int, default=0, help="Last batch (0 = all)")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--close-zoom", type=int, default=17)
    parser.add_argument("--context-zoom", type=int, default=15)
    return parser.parse_args()


def save_data_url(data_url: str, path: Path):
    """Decode a data:image/png;base64,... URL and write the PNG file."""
    header, encoded = data_url.split(",", 1)
    path.write_bytes(base64.b64decode(encoded))


INJECT_HELPERS = """
window.__waitForTiles = function (timeout) {
    timeout = timeout || 15000;
    var view = window.__mapView;
    return new Promise(function (resolve) {
        if (!view.updating) { resolve(true); return; }
        var settled = false;
        var handle = view.watch("updating", function (updating) {
            if (!updating && !settled) {
                settled = true;
                handle.remove();
                resolve(true);
            }
        });
        setTimeout(function () {
            if (!settled) {
                settled = true;
                handle.remove();
                resolve(false);
            }
        }, timeout);
    });
};

window.__captureView = async function (width, height) {
    var view = window.__mapView;
    await window.__waitForTiles(15000);
    var screenshot = await view.takeScreenshot({
        width: width || 1920,
        height: height || 1080,
        format: "png"
    });
    return screenshot.dataUrl;
};

window.__selectCorridorSegments = async function (segmentName) {
    // Access state/render/sync — prefer exposed refs, fall back to __selectAndZoomSegment
    var st = window.__segments_state;
    var renderFn = window.__render;
    var syncFn = window.__syncSelectedGraphics;

    if (!st || !renderFn || !syncFn) {
        // Fallback: the old app.js without exposed internals — just use exact match
        return await window.__selectAndZoomSegment(segmentName);
    }

    // First try exact match
    var exact = st.segments.find(function(s) { return s.label === segmentName; });
    if (exact) {
        return await window.__selectAndZoomSegment(segmentName);
    }

    // No exact match — select all sub-segments that start with this name
    var prefix = segmentName + " - ";
    var matches = st.segments.filter(function(s) {
        return s.label.indexOf(prefix) === 0;
    });
    if (matches.length === 0) return false;

    st.selectedSegmentIds.clear();
    matches.forEach(function(m) { st.selectedSegmentIds.add(m.objectId); });
    renderFn();
    await syncFn();
    return matches.length;
};

window.__navigateAndCapture = async function (segmentName, lon, lat, closeZoom, contextZoom) {
    var view = window.__mapView;
    closeZoom = closeZoom || 17;
    contextZoom = contextZoom || 15;

    await window.__selectCorridorSegments(segmentName);

    await view.goTo({ center: [lon, lat], zoom: closeZoom }, { animate: false });
    await window.__waitForTiles(15000);
    var closeImg = await view.takeScreenshot({ width: 1920, height: 1080, format: "png" });

    await view.goTo({ center: [lon, lat], zoom: contextZoom }, { animate: false });
    await window.__waitForTiles(15000);
    var contextImg = await view.takeScreenshot({ width: 1920, height: 1080, format: "png" });

    return { close: closeImg.dataUrl, context: contextImg.dataUrl };
};
"""


def main():
    args = parse_args()
    url = LOCAL_URL if args.local else args.url

    # Load manifest
    if not args.manifest.exists():
        print(f"ERROR: Manifest not found at {args.manifest}")
        sys.exit(1)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    total_endpoints = len(manifest)
    print(f"Loaded manifest: {total_endpoints} endpoints")

    # Split into batches
    batches = []
    for i in range(0, total_endpoints, args.batch_size):
        batches.append(manifest[i : i + args.batch_size])

    total_batches = len(batches)
    start = args.start_batch
    end = args.end_batch if args.end_batch > 0 else total_batches

    print(f"Total batches: {total_batches} (processing {start}–{end})")

    # Ensure output directory
    args.outdir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})

        # Navigate and wait for app to load
        print(f"Loading {url} ...")
        page.goto(url, wait_until="networkidle", timeout=60000)

        seg_count = page.evaluate("window.__waitForSegments()")
        print(f"App ready — {seg_count} segments loaded")

        # Inject helper functions into the page
        page.evaluate(INJECT_HELPERS)
        print("Injected screenshot helpers")

        captured = 0
        skipped = 0
        errors = 0
        t_start = time.time()

        for batch_idx in range(start - 1, end):
            batch_num = batch_idx + 1
            batch = batches[batch_idx]
            print(f"\n{'='*60}")
            print(f"BATCH {batch_num:02d} — {len(batch)} endpoints")
            print(f"{'='*60}")

            current_segment = None

            for ep_idx, ep in enumerate(batch):
                ep_num = ep_idx + 1
                close_path = args.outdir / f"batch-{batch_num:02d}-ep-{ep_num:02d}-close.png"
                context_path = args.outdir / f"batch-{batch_num:02d}-ep-{ep_num:02d}-context.png"

                # Skip if both files exist and not overwriting
                if not args.overwrite and close_path.exists() and context_path.exists():
                    skipped += 1
                    print(f"  [{ep_num:02d}] {ep['segment']} ({ep['side']}) — SKIP (exists)")
                    continue

                seg_name = ep["segment"]
                lon = ep["lon"]
                lat = ep["lat"]

                print(f"  [{ep_num:02d}] {seg_name} ({ep['side']}) @ ({lon:.4f}, {lat:.4f})")

                try:
                    # Select segment (or corridor) if changed
                    if seg_name != current_segment:
                        result = page.evaluate(
                            f"window.__selectCorridorSegments('{seg_name}')"
                        )
                        if not result:
                            print(f"       WARNING: Segment '{seg_name}' not found — capturing without highlight")
                        elif isinstance(result, int) and result > 1:
                            print(f"       Corridor: selected {result} sub-segments for '{seg_name}'")
                        current_segment = seg_name

                    # Use the combined navigate-and-capture for efficiency
                    data = page.evaluate(
                        f"window.__navigateAndCapture('{seg_name}', {lon}, {lat}, {args.close_zoom}, {args.context_zoom})"
                    )

                    save_data_url(data["close"], close_path)
                    save_data_url(data["context"], context_path)

                    close_kb = close_path.stat().st_size / 1024
                    context_kb = context_path.stat().st_size / 1024
                    print(f"       close: {close_kb:.0f}KB  context: {context_kb:.0f}KB")
                    captured += 1

                except Exception as exc:
                    print(f"       ERROR: {exc}")
                    errors += 1

        elapsed = time.time() - t_start
        browser.close()

    print(f"\n{'='*60}")
    print(f"DONE in {elapsed:.1f}s")
    print(f"  Captured: {captured}")
    print(f"  Skipped:  {skipped}")
    print(f"  Errors:   {errors}")
    print(f"  Output:   {args.outdir.resolve()}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
