#!/usr/bin/env python3
"""
Identify segment limits from the ArcGIS dashboard's backing services.

This is intentionally data-driven instead of browser-driven:
  - segment geometry comes from FTW_Segmentation_Master
  - county boundaries come from the Texas county layer
  - local road labels come first from the TxDOT statewide planning basemap tiles
  - the older TxDOT roadway vector tiles remain as a fallback label source
  - a second-source roadway lookup comes from TxDOT's Roadway Inventory layer

Its primary purpose is to identify segment limits from ArcGIS and TxDOT data.
It can optionally compare those identified limits against
Segment-Limits/limts-FTW-Segments.csv as a secondary review step.

When a comparison CSV is supplied, it writes a review CSV with:
  - Segment-Direction
  - Auto Limits From
  - Auto Limits To
  - Auto Review Status From
  - Auto Review Status To
  - Auto Review Status
  - Auto Review Notes

It also writes a separate CSV containing only rows whose
`Auto Review Status` is `needs_review`.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Iterable

import mercantile
import pandas as pd
import requests
from mapbox_vector_tile import decode as decode_vector_tile
from pyproj import Transformer
from shapely.geometry import LineString, MultiLineString, Point, box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points, transform
from shapely.strtree import STRtree


ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_COMPARE_CSV = ROOT / "FTW-Segments-Limits-Amy.review.csv"
DEFAULT_OUTPUT = ROOT / "segment-limits.auto.csv"
DEFAULT_LABEL_TILE_ROOT = ROOT / "FTW-TxDOT-Labels"
DEFAULT_ROADWAY_INVENTORY_PATH = (
    ROOT / "FTW-Roadway-Inventory" / "roadway-inventory.ftw.geojson"
)

SEGMENT_QUERY_URL = (
    "https://services9.arcgis.com/eNX73FDxjlKFtCtH/arcgis/rest/services/"
    "FTW_Segmentation_Master/FeatureServer/0/query"
)
COUNTY_QUERY_URL = (
    "https://services.arcgis.com/KTcxiTD9dsQw4r7Z/arcgis/rest/services/"
    "Texas_County_Boundaries/FeatureServer/0/query"
)
ROADWAY_INVENTORY_QUERY_URL = (
    "https://services.arcgis.com/KTcxiTD9dsQw4r7Z/arcgis/rest/services/"
    "TxDOT_Roadway_Inventory/FeatureServer/0/query"
)
ROADWAY_INVENTORY_OUT_FIELDS = "RIA_RTE_ID,HWY,HSYS,HNUM,HSUF,STE_NAM,RDBD_ID,REC,CO"
PRIMARY_VECTOR_TILE_TEMPLATE = (
    "https://tiles.arcgis.com/tiles/KTcxiTD9dsQw4r7Z/arcgis/rest/services/"
    "TxDOT_Vector_Tile_Basemap/VectorTileServer/tile/{z}/{y}/{x}.pbf"
)
FALLBACK_VECTOR_TILE_TEMPLATE = (
    "https://vectortileservices9.arcgis.com/eNX73FDxjlKFtCtH/arcgis/rest/services/"
    "TxDOT_Roadways_and_Shields_2/VectorTileServer/tile/{z}/{y}/{x}.pbf"
)
VECTOR_TILE_SOURCES = {
    "basemap": {
        "template": PRIMARY_VECTOR_TILE_TEMPLATE,
        "layers": ("TxDOT Roadways/label", "Concurrencies/label"),
    },
    "fallback": {
        "template": FALLBACK_VECTOR_TILE_TEMPLATE,
        "layers": ("TxDOT_Roadways/label", "TxDOT Roadways/label"),
    },
}
LABEL_TILE_ZOOMS = (19, 18, 17, 16)
MID_CORRIDOR_LABEL_TILE_ZOOMS = (18, 17, 16)

COUNTY_BOUNDARY_TOLERANCE_M = 50.0
COUNTY_BOUNDARY_OFFSET_TOLERANCE_M = 100.0
INTERIOR_SAMPLE_M = 120.0
ROUTE_SEARCH_RADIUS_M = 800.0
ROUTE_STRONG_DISTANCE_M = 180.0
LABEL_SEARCH_RADIUS_M = 325.0
LABEL_ROUTE_CONFIRM_RADIUS_M = 200.0
ROADWAY_INVENTORY_SEARCH_RADIUS_M = 325.0
HTTP_RETRY_COUNT = 3
HTTP_RETRY_BACKOFF_S = 1.0
HTTP_POOL_SIZE = 32
DEFAULT_TILE_DOWNLOAD_WORKERS = 16
SLOW_SEGMENT_THRESHOLD_S = 8.0
CLOSE_ROUTE_CANDIDATE_M = 80.0
VISUAL_LOCAL_CLUE_MAX_DISTANCE_M = 40.0
VISUAL_LOCAL_CLUE_MIN_CONFIDENCE = 0.88
CONTINUATION_ROUTE_MAX_ANGLE_DEG = 55.0
FALSE_LIMIT_CONTINUATION_MAX_ANGLE_DEG = 30.0
INTERCHANGE_LABEL_MAX_DISTANCE_M = 120.0
INTERCHANGE_ROUTE_MAX_ANGLE_DEG = 35.0
FRONTAGE_OVERRIDE_LABEL_MAX_DISTANCE_M = 40.0
FRONTAGE_DIRECT_OVERRIDE_LABEL_MAX_DISTANCE_M = 25.0
FRONTAGE_OVERRIDE_MAINLINE_MIN_DISTANCE_M = 25.0
FRONTAGE_ROUTE_INTERSECTION_OVERRIDE_MIN_DISTANCE_M = 120.0
MID_CORRIDOR_SEARCH_DISTANCE_M = 1000.0
MID_CORRIDOR_LABEL_SEARCH_DISTANCE_M = 800.0
MID_CORRIDOR_INVENTORY_SEARCH_DISTANCE_M = 1000.0
MID_CORRIDOR_MIN_CROSSING_ANGLE_DEG = 40.0
MID_CORRIDOR_STRONG_CROSSING_ANGLE_DEG = 75.0
MID_CORRIDOR_ROUTE_CONTINUATION_MAX_ANGLE_DEG = 20.0
MID_CORRIDOR_LOCAL_OFFSET_MIN_M = 80.0
MID_CORRIDOR_ROUTE_SEARCH_TRIGGER_MIN_M = 30.0

COMBINED_SEGMENT_OVERRIDES = {
    "FM 1187 - A/B": ["FM 1187 - A", "FM 1187 - B"],
}
ROUTE_FAMILY_OVERRIDES = {
    "US 81": "US 81/287",
}
TITLE_TOKEN_OVERRIDES = {
    "BLVD": "Blvd",
    "CT": "Ct",
    "CR": "CR",
    "DR": "Dr",
    "E": "E",
    "FM": "FM",
    "FWY": "Fwy",
    "HWY": "Hwy",
    "IH": "IH",
    "LN": "Ln",
    "N": "N",
    "NE": "NE",
    "NW": "NW",
    "PKWY": "Pkwy",
    "PL": "Pl",
    "RD": "Rd",
    "S": "S",
    "SE": "SE",
    "SH": "SH",
    "ST": "St",
    "SW": "SW",
    "TL": "TL",
    "TRL": "Trl",
    "US": "US",
    "W": "W",
}

SUPPORTED_ROUTE_PREFIXES = {
    "IH",
    "US",
    "SH",
    "FM",
    "RM",
    "SS",
    "SL",
    "BS",
    "BU",
    "BI",
    "CR",
    "FS",
    "PA",
    "PR",
    "TL",
}

WGS84_TO_3081 = Transformer.from_crs(4326, 3081, always_xy=True)
PROJ_3081_TO_WGS84 = Transformer.from_crs(3081, 4326, always_xy=True)
WGS84_TO_3857 = Transformer.from_crs(4326, 3857, always_xy=True)
WEBM_TO_WGS84 = Transformer.from_crs(3857, 4326, always_xy=True)
DEFAULT_MAX_WORKERS = min(8, max(1, os.cpu_count() or 1))

_THREAD_LOCAL = threading.local()


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "FTW-Segment-Limits-Verification/1.0"})
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=HTTP_POOL_SIZE,
        pool_maxsize=HTTP_POOL_SIZE,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = build_session()
        _THREAD_LOCAL.session = session
    return session


def session_get(
    url: str,
    *,
    timeout: float,
    params: dict[str, str] | None = None,
    suppress_errors: bool = False,
) -> requests.Response | None:
    last_error: Exception | None = None
    for attempt in range(HTTP_RETRY_COUNT):
        try:
            response = get_session().get(url, params=params, timeout=timeout)
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < HTTP_RETRY_COUNT:
                time.sleep(HTTP_RETRY_BACKOFF_S * (attempt + 1))

    if suppress_errors:
        return None
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Failed GET request for {url}")


@dataclass(frozen=True)
class SegmentFeature:
    segment_id: str
    readable_segid: str
    route_family: str
    county_names: tuple[str, ...]
    geometry_wgs84: BaseGeometry
    geometry_proj: BaseGeometry
    line_wgs84: LineString
    line_proj: LineString


@dataclass(frozen=True)
class CountyFeature:
    name: str
    geometry_proj: BaseGeometry
    boundary_proj: BaseGeometry
    area: float


@dataclass(frozen=True)
class CountyLookup:
    counties: tuple[CountyFeature, ...]
    tree: STRtree


@dataclass(frozen=True)
class LabelFeature:
    name: str
    source: str
    geometry_proj: BaseGeometry


@dataclass(frozen=True)
class RoadwayInventoryFeature:
    value: str
    normalized: str
    kind: str
    roadbed_id: str
    geometry_proj: BaseGeometry
    detail: str


@dataclass(frozen=True)
class RoadwayInventoryLookup:
    features: tuple[RoadwayInventoryFeature, ...]
    tree: STRtree


@dataclass(frozen=True)
class LimitCandidate:
    value: str
    normalized: str
    method: str
    confidence: float
    distance_m: float
    detail: str
    angle_diff: float | None = None
    heuristic: str = ""
    anchor_geometry_proj: BaseGeometry | None = None


@dataclass(frozen=True)
class RowProcessingContext:
    by_readable: dict[str, SegmentFeature]
    by_family: dict[str, list[SegmentFeature]]
    compare_mode: bool
    counties: CountyLookup
    all_segments: list[SegmentFeature]
    label_tile_root: str | None
    roadway_inventory_lookup: RoadwayInventoryLookup | None


@dataclass(frozen=True)
class RowProcessingResult:
    index: int
    segment_name: str
    auto_from: str
    auto_to: str
    heuristic_from: str
    heuristic_to: str
    segment_direction: str
    segment_type: str
    side_status_from: str
    side_status_to: str
    status: str
    note: str
    processing_time_s: float
    from_endpoint_wgs84: tuple[float, float] | None = None
    to_endpoint_wgs84: tuple[float, float] | None = None
    confidence_from: float = 0.0
    confidence_to: float = 0.0
    gap_piece_endpoints: list[dict[str, object]] | None = None


def safe_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value)


def point_to_lon_lat(point: Point) -> tuple[float, float]:
    return (float(point.x), float(point.y))


def confidence_bucket(score: float) -> str:
    if score >= 0.90:
        return "high"
    if score >= 0.78:
        return "medium"
    return "low"


def query_arcgis_geojson(
    url: str,
    *,
    where: str = "1=1",
    out_fields: str = "*",
    page_size: int = 1000,
    extra_params: dict[str, str] | None = None,
) -> list[dict]:
    features: list[dict] = []
    offset = 0
    while True:
        params = {
            "where": where,
            "outFields": out_fields,
            "returnGeometry": "true",
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": page_size,
        }
        if extra_params:
            params.update(extra_params)

        response = session_get(url, params=params, timeout=120)
        assert response is not None
        response.raise_for_status()
        data = response.json()
        batch = data.get("features", [])
        if not batch:
            break
        features.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return features


def project_geometry(geometry: BaseGeometry) -> BaseGeometry:
    return transform(WGS84_TO_3081.transform, geometry)


GAP_MERGE_THRESHOLD_M = 50.0   # Merge connected parts within this distance
GAP_THRESHOLD_M = 200.0         # Real gap segments have pieces separated by at least this
MIN_PIECE_LENGTH_M = 600.0
MIN_PIECE_RATIO = 0.05  # 5% of total segment length


def representative_line(geometry: BaseGeometry) -> LineString:
    """Return the longest single LineString part from the geometry.
    This preserves the original endpoint behavior for SegmentFeature.line_proj."""
    if isinstance(geometry, LineString):
        return geometry
    if isinstance(geometry, MultiLineString):
        return max(geometry.geoms, key=lambda line: line.length)
    raise TypeError(f"Unsupported geometry type: {geometry.geom_type}")


def _endpoint_gap(line_a: LineString, line_b: LineString) -> float:
    """Minimum distance between any pair of endpoints of two lines."""
    pts_a = [Point(line_a.coords[0]), Point(line_a.coords[-1])]
    pts_b = [Point(line_b.coords[0]), Point(line_b.coords[-1])]
    return min(pa.distance(pb) for pa in pts_a for pb in pts_b)


def _merge_connected_parts(parts: list[LineString]) -> list[LineString]:
    """Merge LineString parts that are within GAP_THRESHOLD_M of each other
    into continuous lines. Returns a list of merged LineStrings — one per
    disconnected group."""
    if not parts:
        return []
    if len(parts) == 1:
        return list(parts)

    # Sort parts by their centroid x then y for stable ordering
    remaining = sorted(parts, key=lambda p: (p.centroid.x, p.centroid.y))
    groups: list[list[LineString]] = [[remaining.pop(0)]]

    while remaining:
        merged_any = False
        for part in list(remaining):
            for group in groups:
                # Check if this part connects to any line in the group
                # via endpoint-to-endpoint distance (not min geometry distance)
                for existing in group:
                    gap = _endpoint_gap(existing, part)
                    if gap < GAP_MERGE_THRESHOLD_M:
                        group.append(part)
                        remaining.remove(part)
                        merged_any = True
                        break
                if part not in remaining:
                    break
        if not merged_any:
            # Start a new group with the next remaining part
            groups.append([remaining.pop(0)])

    # Merge each group into a single LineString by chaining coordinates
    result: list[LineString] = []
    for group in groups:
        if len(group) == 1:
            result.append(group[0])
        else:
            result.append(_chain_parts_into_line(group))
    return result


def _chain_parts_into_line(parts: list[LineString]) -> LineString:
    """Chain multiple LineString parts into a single LineString, ordering
    them to minimize gaps between consecutive parts."""
    if len(parts) == 1:
        return parts[0]

    # Greedy nearest-neighbor chaining
    remaining = list(parts)
    chain = [remaining.pop(0)]

    while remaining:
        last_end = Point(chain[-1].coords[-1])
        best_idx = 0
        best_dist = float("inf")
        best_reverse = False
        for i, part in enumerate(remaining):
            d_fwd = last_end.distance(Point(part.coords[0]))
            d_rev = last_end.distance(Point(part.coords[-1]))
            if d_fwd < best_dist:
                best_dist = d_fwd
                best_idx = i
                best_reverse = False
            if d_rev < best_dist:
                best_dist = d_rev
                best_idx = i
                best_reverse = True
        part = remaining.pop(best_idx)
        if best_reverse:
            part = LineString(list(part.coords)[::-1])
        chain.append(part)

    # Concatenate all coordinates
    all_coords: list[tuple[float, ...]] = []
    for part in chain:
        coords = list(part.coords)
        if all_coords:
            # Skip first coord if it's very close to previous end (avoid dups)
            if Point(coords[0]).distance(Point(all_coords[-1])) < 1.0:
                coords = coords[1:]
        all_coords.extend(coords)
    return LineString(all_coords)


def split_gap_pieces(geometry_proj: BaseGeometry) -> list[LineString]:
    """Split a geometry into separate LineStrings where real physical gaps
    (>= GAP_THRESHOLD_M) exist. Connected/overlapping parts are merged first,
    then small detached artifact fragments are dropped.
    Returns a list of 1+ LineStrings."""
    if isinstance(geometry_proj, LineString):
        return [geometry_proj]
    if not isinstance(geometry_proj, MultiLineString):
        return [representative_line(geometry_proj)]
    parts = list(geometry_proj.geoms)
    if len(parts) <= 1:
        return parts if parts else []
    # First merge connected parts (gap < GAP_MERGE_THRESHOLD_M)
    merged = _merge_connected_parts(parts)
    if len(merged) <= 1:
        return merged
    # Drop detached artifact fragments that are too small
    total_length = sum(p.length for p in merged)
    significant = [
        p for p in merged
        if p.length >= MIN_PIECE_LENGTH_M
        or p.length >= total_length * MIN_PIECE_RATIO
    ]
    if not significant:
        significant = [max(merged, key=lambda p: p.length)]
    return significant


def reverse_line(line: LineString) -> LineString:
    return LineString(list(line.coords)[::-1])


def reverse_oriented_sequence(
    oriented: list[tuple[SegmentFeature, LineString, LineString]],
) -> list[tuple[SegmentFeature, LineString, LineString]]:
    reversed_sequence: list[tuple[SegmentFeature, LineString, LineString]] = []
    for feature, line_wgs84, line_proj in reversed(oriented):
        reversed_sequence.append(
            (feature, reverse_line(line_wgs84), reverse_line(line_proj))
        )
    return reversed_sequence


def cardinal_start_should_be_reversed(
    start_point: Point,
    end_point: Point,
) -> bool:
    dx = end_point.x - start_point.x
    dy = end_point.y - start_point.y

    # Convention: N to S (From=north, To=south) by default.
    # W to E (From=west, To=east) only when clearly horizontal
    # (dx > dy * 1.25). Diagonal routes default to N to S.
    if abs(dx) > abs(dy) * 1.25:
        # W to E: start should be the western point
        return start_point.x > end_point.x
    # N to S: start should be the northern point
    return start_point.y < end_point.y


def segment_direction_label(
    start_point: Point,
    end_point: Point,
) -> str:
    dx = end_point.x - start_point.x
    dy = end_point.y - start_point.y
    if abs(dx) > abs(dy) * 1.25:
        return "W to E"
    return "N to S"


def readable_to_route_family(readable_segid: str) -> str:
    match = re.match(r"^(.*?)(?:\s+-\s+[A-Z](?:/[A-Z])?)?$", readable_segid.strip())
    if not match:
        return readable_segid.strip()
    return match.group(1).strip()


def part_sort_key(readable_segid: str) -> tuple[int, str]:
    match = re.search(r"\s+-\s+([A-Z])$", readable_segid.strip())
    if not match:
        return (999, readable_segid)
    return (ord(match.group(1)) - ord("A"), readable_segid)


def segment_name_sort_key(segment_name: str) -> tuple[str, tuple[int, str]]:
    normalized = normalize_spacing(segment_name)
    return (readable_to_route_family(normalized), part_sort_key(normalized))


def load_segment_features() -> list[SegmentFeature]:
    features = query_arcgis_geojson(
        SEGMENT_QUERY_URL,
        out_fields="Segment_ID,Readable_SegID,HSYS,County",
    )
    segment_features: list[SegmentFeature] = []
    for feature in features:
        props = feature["properties"]
        geometry_wgs84 = shape(feature["geometry"])
        line_wgs84 = representative_line(geometry_wgs84)
        line_proj = transform(WGS84_TO_3081.transform, line_wgs84)
        segment_features.append(
            SegmentFeature(
                segment_id=props["Segment_ID"],
                readable_segid=props["Readable_SegID"],
                route_family=readable_to_route_family(props["Readable_SegID"]),
                county_names=tuple(
                    sorted(
                        {
                            county_name
                            for county_name in (
                                normalize_spacing(part).title()
                                for part in safe_text(props.get("County", "")).split(",")
                            )
                            if county_name
                        }
                    )
                ),
                geometry_wgs84=geometry_wgs84,
                geometry_proj=project_geometry(geometry_wgs84),
                line_wgs84=line_wgs84,
                line_proj=line_proj,
            )
        )
    return segment_features


def load_counties() -> list[CountyFeature]:
    features = query_arcgis_geojson(
        COUNTY_QUERY_URL,
        out_fields="CNTY_NM",
        page_size=500,
    )
    counties: list[CountyFeature] = []
    for feature in features:
        props = feature["properties"]
        geometry_proj = project_geometry(shape(feature["geometry"]))
        if not geometry_proj.is_valid:
            geometry_proj = geometry_proj.buffer(0)
        counties.append(
            CountyFeature(
                name=props["CNTY_NM"].strip().title(),
                geometry_proj=geometry_proj,
                boundary_proj=geometry_proj.boundary,
                area=geometry_proj.area,
            )
        )
    return counties


def build_county_lookup(counties: Iterable[CountyFeature]) -> CountyLookup:
    county_items = tuple(counties)
    return CountyLookup(
        counties=county_items,
        tree=STRtree([county.geometry_proj for county in county_items]),
    )


def build_lookups(
    segment_features: Iterable[SegmentFeature],
) -> tuple[dict[str, SegmentFeature], dict[str, list[SegmentFeature]]]:
    by_readable = {feature.readable_segid: feature for feature in segment_features}
    by_family: dict[str, list[SegmentFeature]] = {}
    for feature in segment_features:
        by_family.setdefault(feature.route_family, []).append(feature)
    for family in by_family:
        by_family[family] = sorted(by_family[family], key=lambda f: part_sort_key(f.readable_segid))
    return by_readable, by_family


def resolve_row_features(
    segment_name: str,
    by_readable: dict[str, SegmentFeature],
    by_family: dict[str, list[SegmentFeature]],
) -> list[SegmentFeature]:
    if segment_name in COMBINED_SEGMENT_OVERRIDES:
        return [by_readable[name] for name in COMBINED_SEGMENT_OVERRIDES[segment_name]]

    if segment_name in by_readable:
        return [by_readable[segment_name]]

    family = ROUTE_FAMILY_OVERRIDES.get(segment_name, segment_name)
    return by_family.get(family, [])


def orient_feature_sequence(features: list[SegmentFeature]) -> list[tuple[SegmentFeature, LineString, LineString]]:
    if not features:
        return []
    if len(features) == 1:
        feature = features[0]
        oriented = [(feature, feature.line_wgs84, feature.line_proj)]
        start_point = Point(oriented[0][2].coords[0])
        end_point = Point(oriented[-1][2].coords[-1])
        if cardinal_start_should_be_reversed(start_point, end_point):
            return reverse_oriented_sequence(oriented)
        return oriented

    options: list[tuple[float, list[tuple[SegmentFeature, LineString, LineString]]]] = []
    for reverse_first in (False, True):
        total_gap = 0.0
        oriented: list[tuple[SegmentFeature, LineString, LineString]] = []
        prev_end: Point | None = None
        for index, feature in enumerate(features):
            line_wgs84 = feature.line_wgs84
            line_proj = feature.line_proj
            if index == 0 and reverse_first:
                line_wgs84 = reverse_line(line_wgs84)
                line_proj = reverse_line(line_proj)
            elif index > 0 and prev_end is not None:
                forward_gap = prev_end.distance(Point(line_proj.coords[0]))
                reverse_gap = prev_end.distance(Point(line_proj.coords[-1]))
                if reverse_gap < forward_gap:
                    line_wgs84 = reverse_line(line_wgs84)
                    line_proj = reverse_line(line_proj)
                    total_gap += reverse_gap
                else:
                    total_gap += forward_gap
            oriented.append((feature, line_wgs84, line_proj))
            prev_end = Point(line_proj.coords[-1])
        options.append((total_gap, oriented))
    oriented = min(options, key=lambda item: item[0])[1]
    start_point = Point(oriented[0][2].coords[0])
    end_point = Point(oriented[-1][2].coords[-1])
    if cardinal_start_should_be_reversed(start_point, end_point):
        return reverse_oriented_sequence(oriented)
    return oriented


def point_along_line(line: LineString, *, at_start: bool, distance_m: float) -> Point:
    if line.length == 0:
        return Point(line.coords[0])
    offset = min(distance_m, max(line.length * 0.2, 1.0))
    measure = offset if at_start else max(line.length - offset, 0.0)
    return line.interpolate(measure)


def line_angle_deg(line: LineString, *, at_start: bool) -> float:
    point_a = point_along_line(line, at_start=at_start, distance_m=5.0)
    point_b = point_along_line(line, at_start=at_start, distance_m=INTERIOR_SAMPLE_M)
    if point_a.equals(point_b):
        point_a = Point(line.coords[0] if at_start else line.coords[-1])
        point_b = Point(line.coords[1] if at_start else line.coords[-2])
    dx = point_b.x - point_a.x
    dy = point_b.y - point_a.y
    angle = math.degrees(math.atan2(dy, dx)) % 180.0
    return angle


def angle_difference_deg(angle_a: float, angle_b: float) -> float:
    diff = abs(angle_a - angle_b) % 180.0
    return min(diff, 180.0 - diff)


def local_line_angle_for_point(line: LineString, point: Point) -> float:
    if line.length == 0:
        return 0.0

    measure = line.project(point)
    start_measure = max(0.0, measure - 40.0)
    end_measure = min(line.length, measure + 40.0)
    point_a = line.interpolate(start_measure)
    point_b = line.interpolate(end_measure)

    if point_a.equals(point_b):
        coords = list(line.coords)
        if len(coords) >= 2:
            point_a = Point(coords[0])
            point_b = Point(coords[-1])

    dx = point_b.x - point_a.x
    dy = point_b.y - point_a.y
    return math.degrees(math.atan2(dy, dx)) % 180.0


def find_county_for_point(point_proj: Point, counties: CountyLookup) -> CountyFeature | None:
    containing = [
        counties.counties[int(index)]
        for index in counties.tree.query(point_proj, predicate="intersects")
    ]
    if containing:
        return min(containing, key=lambda county: county.area)

    nearest_index = counties.tree.nearest(point_proj)
    if nearest_index is None:
        return None
    return counties.counties[int(nearest_index)]


def _county_offset_direction(endpoint_proj: Point, boundary_proj: BaseGeometry) -> str:
    """Compute cardinal direction from the nearest boundary point to the endpoint."""
    from shapely.ops import nearest_points

    nearest_on_boundary, _ = nearest_points(boundary_proj, endpoint_proj)
    dx = endpoint_proj.x - nearest_on_boundary.x
    dy = endpoint_proj.y - nearest_on_boundary.y
    if abs(dx) < 1.0 and abs(dy) < 1.0:
        return ""
    if abs(dx) >= abs(dy):
        return "E" if dx > 0 else "W"
    return "N" if dy > 0 else "S"


def infer_county_limit(
    endpoint_proj: Point,
    interior_proj: Point,
    counties: CountyLookup,
) -> LimitCandidate | None:
    county = find_county_for_point(interior_proj, counties)
    if county is None:
        return None
    distance_to_boundary = endpoint_proj.distance(county.boundary_proj)
    if distance_to_boundary > COUNTY_BOUNDARY_OFFSET_TOLERANCE_M:
        return None

    county_line_name = f"{county.name} County Line"

    if distance_to_boundary <= COUNTY_BOUNDARY_TOLERANCE_M:
        # At the boundary — bare county line
        return LimitCandidate(
            value=county_line_name,
            normalized=normalize_limit_key(county_line_name),
            method="county_boundary",
            confidence=0.99,
            distance_m=distance_to_boundary,
            detail=f"{county.name} county boundary ({distance_to_boundary:.1f}m)",
            angle_diff=None,
            anchor_geometry_proj=county.boundary_proj,
        )

    # Offset from county line — return as lower-confidence candidate
    # so select_limit() can compare it against route/local candidates
    direction = _county_offset_direction(endpoint_proj, county.boundary_proj)
    if direction:
        rounded_dist = round(distance_to_boundary / 5.0) * 5  # round to nearest 5m
        value = f"{int(rounded_dist)}m {direction} of {county_line_name}"
    else:
        value = county_line_name
    return LimitCandidate(
        value=value,
        normalized=normalize_limit_key(county_line_name),
        method="county_boundary_offset",
        confidence=0.85,
        distance_m=distance_to_boundary,
        detail=f"{county.name} county boundary offset ({distance_to_boundary:.1f}m {direction})",
        angle_diff=None,
        anchor_geometry_proj=county.boundary_proj,
    )


def route_tokens(route_family: str) -> set[str]:
    tokens = {normalize_limit_key(route_family)}
    for raw in re.findall(r"\d+[A-Z]?", route_family.upper()):
        tokens.add(raw)
        tokens.add(re.sub(r"[A-Z]+$", "", raw))
    return {token for token in tokens if token}


def route_system(route_family: str) -> str:
    match = re.match(r"^(IH|US|SH|FM|RM|SS|SL|BS|BU|BI|CR|FS|PA|PR|TL)\b", normalize_limit_key(route_family))
    return match.group(1) if match else ""


def route_system_priority(route_family: str) -> int:
    priorities = {
        "IH": 6,
        "US": 5,
        "SH": 4,
        "TL": 4,
        "BU": 3,
        "BS": 3,
        "BI": 3,
        "FM": 2,
        "RM": 2,
        "CR": 2,
        "PA": 2,
        "PR": 2,
        "FS": 2,
        "SL": 1,
        "SS": 1,
    }
    return priorities.get(route_system(route_family), 0)


def compact_route_number(raw_value: object) -> str:
    value = normalize_spacing(safe_text(raw_value)).upper()
    if not value:
        return ""

    parts: list[str] = []
    for token in value.split("/"):
        match = re.fullmatch(r"0*([0-9]+)([A-Z]?)", token)
        if not match:
            return ""
        number = match.group(1).lstrip("0") or "0"
        parts.append(f"{number}{match.group(2)}")
    return "/".join(parts)


def parse_compact_route_name(raw_value: object) -> str:
    value = re.sub(r"[\s-]+", "", normalize_spacing(safe_text(raw_value)).upper())
    match = re.fullmatch(
        r"(IH|US|SH|FM|RM|SS|SL|BS|BU|BI|CR|FS|PA|PR|TL)0*([0-9]+)([A-Z]?)",
        value,
    )
    if not match:
        return ""
    number = match.group(2).lstrip("0") or "0"
    return f"{match.group(1)} {number}{match.group(3)}".strip()


def inventory_route_name(properties: dict[str, object]) -> str:
    prefix = normalize_spacing(properties.get("HSYS", "")).upper()
    number = compact_route_number(properties.get("HNUM"))
    suffix = normalize_spacing(properties.get("HSUF", "")).upper()

    if prefix in SUPPORTED_ROUTE_PREFIXES and number:
        return f"{prefix} {number}{suffix}".strip()

    return parse_compact_route_name(properties.get("HWY"))


def roadway_inventory_value(properties: dict[str, object]) -> tuple[str, str]:
    route_name = inventory_route_name(properties)
    if route_name:
        return ("route", route_name)

    street_name = smart_title(properties.get("STE_NAM", ""))
    if street_name:
        return ("local", street_name)

    return ("", "")


def build_inventory_detail(properties: dict[str, object]) -> str:
    parts: list[str] = []
    route_id = normalize_spacing(properties.get("RIA_RTE_ID", ""))
    if route_id:
        parts.append(route_id)
    roadbed_id = normalize_spacing(properties.get("RDBD_ID", "")).upper()
    if roadbed_id:
        parts.append(f"roadbed={roadbed_id}")
    record_type = normalize_spacing(properties.get("REC", ""))
    if record_type:
        parts.append(f"rec={record_type}")
    return ", ".join(parts)


def roadway_inventory_feature_from_geojson(
    feature: dict[str, object],
) -> RoadwayInventoryFeature | None:
    properties = feature.get("properties", {})
    if not isinstance(properties, dict):
        return None

    kind, value = roadway_inventory_value(properties)
    if not value:
        return None

    geometry_data = feature.get("geometry")
    if geometry_data is None:
        return None

    return RoadwayInventoryFeature(
        value=value,
        normalized=normalize_limit_key(value),
        kind=kind,
        roadbed_id=normalize_spacing(properties.get("RDBD_ID", "")).upper(),
        geometry_proj=project_geometry(shape(geometry_data)),
        detail=build_inventory_detail(properties),
    )


def build_roadway_inventory_lookup(
    inventory_features: Iterable[RoadwayInventoryFeature],
) -> RoadwayInventoryLookup:
    inventory_items = tuple(inventory_features)
    return RoadwayInventoryLookup(
        features=inventory_items,
        tree=STRtree([feature.geometry_proj for feature in inventory_items]),
    )


def project_county_names(segment_features: Iterable[SegmentFeature]) -> list[str]:
    return sorted({county_name for feature in segment_features for county_name in feature.county_names})


def roadway_inventory_subset_where_clause(segment_features: Iterable[SegmentFeature]) -> str:
    county_names = project_county_names(segment_features)
    if not county_names:
        raise ValueError("No county names were found in the FTW segmentation data.")

    quoted_names = ", ".join("'" + name.replace("'", "''") + "'" for name in county_names)
    county_features = query_arcgis_geojson(
        COUNTY_QUERY_URL,
        where=f"CNTY_NM IN ({quoted_names})",
        out_fields="CNTY_NM,CNTY_NBR",
        page_size=50,
    )

    county_numbers_by_name = {
        normalize_spacing(feature["properties"].get("CNTY_NM", "")).title():
        int(feature["properties"]["CNTY_NBR"])
        for feature in county_features
        if feature.get("properties", {}).get("CNTY_NBR") is not None
    }
    missing = [name for name in county_names if name not in county_numbers_by_name]
    if missing:
        missing_text = ", ".join(sorted(missing))
        raise ValueError(f"Could not resolve county numbers for: {missing_text}")

    county_numbers = sorted({county_numbers_by_name[name] for name in county_names})
    return f"CO IN ({', '.join(str(number) for number in county_numbers)})"


def download_roadway_inventory_subset(
    *,
    output_path: pathlib.Path,
    segment_features: Iterable[SegmentFeature],
) -> int:
    where_clause = roadway_inventory_subset_where_clause(segment_features)
    subset_features = query_arcgis_geojson(
        ROADWAY_INVENTORY_QUERY_URL,
        where=where_clause,
        out_fields=ROADWAY_INVENTORY_OUT_FIELDS,
        page_size=2000,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "name": "ftw_roadway_inventory_subset",
                "features": subset_features,
            }
        ),
        encoding="utf-8",
    )
    return len(subset_features)


def same_route_limit(value_a: str, value_b: str) -> bool:
    return (
        is_route_limit(value_a)
        and is_route_limit(value_b)
        and route_system_matches(value_a, value_b)
        and route_overlap(value_a, value_b)
    )


def is_specific_route_variant_name(value: str) -> bool:
    text = normalize_spacing(value).upper()
    if not text:
        return False
    return any(
        token in text
        for token in (
            "FRONTAGE",
            "SERVICE ROAD",
            "SERVICE RD",
            "BUSINESS",
            "BUS ",
            "SPUR",
            "LOOP",
            "BYPASS",
            "ALT ",
            "ALTERNATE",
        )
    )


def is_frontage_or_service_route_variant_name(value: str) -> bool:
    text = normalize_spacing(value).upper()
    if not is_specific_route_variant_name(text):
        return False
    return any(token in text for token in ("FRONTAGE", "SERVICE RD", "SERVICE ROAD"))


def label_side_hint(value: str) -> str:
    text = normalize_spacing(value).upper()
    if re.search(r"\bLEFT\b", text):
        return "LEFT"
    if re.search(r"\bRIGHT\b", text):
        return "RIGHT"
    return ""


def roadbed_side_hint(roadbed_id: str) -> str:
    text = normalize_spacing(roadbed_id).upper()
    if text.startswith("L"):
        return "LEFT"
    if text.startswith("R"):
        return "RIGHT"
    return ""


def is_side_specific_route_variant_name(value: str) -> bool:
    text = normalize_spacing(value).upper()
    if not is_frontage_or_service_route_variant_name(text):
        return False
    if not label_side_hint(text):
        return False
    return True


def same_route_corridor(value_a: str, value_b: str) -> bool:
    parts_a = route_number_parts(value_a)
    parts_b = route_number_parts(value_b)
    if not parts_a or not parts_b:
        return False
    return route_system_matches(value_a, value_b) and bool(parts_a & parts_b)


def route_number_base_overlap(value_a: str, value_b: str) -> bool:
    parts_a = {re.sub(r"[A-Z]+$", "", part) for part in route_number_parts(value_a)}
    parts_b = {re.sub(r"[A-Z]+$", "", part) for part in route_number_parts(value_b)}
    parts_a.discard("")
    parts_b.discard("")
    return bool(parts_a and parts_b and parts_a & parts_b)


def variant_limit_normalized(value: str) -> str:
    text = normalize_spacing(smart_title(value))
    if not text:
        return ""
    if is_specific_route_variant_name(text):
        return text
    return normalize_limit_key(text)


def should_skip_local_label(name: str) -> bool:
    text = normalize_spacing(name).upper()
    if not text:
        return True
    if is_route_limit(text) and not is_specific_route_variant_name(text):
        return True
    if re.fullmatch(r"\d+[A-Z]?(?:/\d+[A-Z]?)*", text):
        return True
    return any(
        token in text
        for token in (
            "SUPPLEMENTAL",
            "MAIN LANE",
            "AUXILIARY LANE",
        )
    )


def labels_match_route(labels: Iterable[LabelFeature], route_family: str) -> bool:
    tokens = route_tokens(route_family)
    for label in labels:
        name = normalize_limit_key(label.name)
        if name in tokens:
            return True
    return False


def infer_route_limit(
    endpoint_proj: Point,
    current_angle: float,
    current_feature_ids: set[str],
    current_route_family: str,
    all_segments: list[SegmentFeature],
    nearby_labels: list[LabelFeature],
) -> LimitCandidate | None:
    nearby_route_labels = [
        label
        for label in nearby_labels
        if endpoint_proj.distance(label.geometry_proj) <= LABEL_ROUTE_CONFIRM_RADIUS_M
    ]
    candidates: list[tuple[tuple[float, int, float, float], LimitCandidate]] = []
    for feature in all_segments:
        if feature.segment_id in current_feature_ids:
            continue
        if feature.route_family == current_route_family:
            continue
        distance = endpoint_proj.distance(feature.line_proj)
        if distance <= ROUTE_SEARCH_RADIUS_M:
            strong_match = distance <= ROUTE_STRONG_DISTANCE_M
            label_confirmed = labels_match_route(nearby_route_labels, feature.route_family)
            if not strong_match and not label_confirmed:
                continue

            route_angle = local_line_angle_for_point(feature.line_proj, endpoint_proj)
            angle_diff = angle_difference_deg(current_angle, route_angle)
            confidence = 0.92 if strong_match else 0.82
            if label_confirmed:
                confidence += 0.05
            if angle_diff >= 55.0:
                confidence += 0.08
            elif angle_diff >= 35.0:
                confidence += 0.04
            elif angle_diff <= 12.0:
                confidence -= 0.08

            if route_overlap(current_route_family, feature.route_family) and angle_diff <= 15.0:
                confidence -= 0.08
            if route_system_priority(feature.route_family) >= 5 and angle_diff >= 45.0:
                confidence += 0.04

            value = feature.route_family
            route_alias = find_route_alias_label(endpoint_proj, feature, nearby_labels)
            if route_alias:
                value = format_named_route(route_alias, feature.route_family)
                confidence = max(confidence, 0.93)

            confidence = min(max(confidence, 0.5), 0.98)
            candidate = LimitCandidate(
                value=value,
                normalized=normalize_limit_key(value),
                method="route_intersection",
                confidence=confidence,
                distance_m=distance,
                detail=f"{feature.route_family} ({distance:.1f}m, angle={angle_diff:.1f})",
                angle_diff=angle_diff,
                anchor_geometry_proj=feature.line_proj,
            )
            score = (
                confidence,
                route_system_priority(feature.route_family),
                angle_diff,
                -distance,
            )
            candidates.append((score, candidate))

    if not candidates:
        return None

    return max(candidates, key=lambda item: item[0])[1]


def smart_title(label: str) -> str:
    label = safe_text(label)
    words = []
    for token in re.split(r"(\W+)", label.strip()):
        upper = token.upper()
        if not token or re.fullmatch(r"\W+", token):
            words.append(token)
            continue
        if re.fullmatch(r"\d+(ST|ND|RD|TH)", upper):
            words.append(upper[:-2] + upper[-2:].lower())
            continue
        if upper in TITLE_TOKEN_OVERRIDES:
            words.append(TITLE_TOKEN_OVERRIDES[upper])
            continue
        words.append(token.capitalize())
    return "".join(words).strip()


def looks_like_route_alias(name: str) -> bool:
    upper = normalize_spacing(name).upper()
    return any(
        token in upper
        for token in (
            " HWY", " HIGHWAY", " FWY", " FREEWAY",
            " EXPY", " EXPRESSWAY",
        )
    )


def format_named_route(road_name: str, route_family: str) -> str:
    road_name = normalize_spacing(road_name)
    route_family = normalize_spacing(route_family)
    if not road_name:
        return route_family
    if is_specific_route_variant_name(road_name):
        return road_name
    if normalize_limit_key(road_name) == normalize_limit_key(route_family):
        return route_family
    return f"{road_name} ({route_family})"


def find_route_alias_label(
    endpoint_proj: Point,
    route_feature: SegmentFeature,
    nearby_labels: list[LabelFeature],
) -> str:
    choices: list[tuple[float, float, str]] = []
    route_angle = local_line_angle_for_point(route_feature.line_proj, endpoint_proj)
    route_token_set = route_tokens(route_feature.route_family)

    for label in nearby_labels:
        if label.geometry_proj.geom_type == "Point":
            continue

        label_name = smart_title(label.name)
        if not looks_like_route_alias(label_name):
            continue
        label_key = normalize_limit_key(label_name)
        if not label_key:
            continue
        if label_key in route_token_set and not is_specific_route_variant_name(label_name):
            continue
        if is_frontage_or_service_route_variant_name(label_name):
            continue

        endpoint_distance = endpoint_proj.distance(label.geometry_proj)
        if endpoint_distance > 150.0:
            continue

        route_distance = route_feature.line_proj.distance(label.geometry_proj)
        if route_distance > 70.0:
            continue

        label_angle = local_line_angle_for_point(
            representative_line(label.geometry_proj),
            endpoint_proj,
        )
        angle_diff = angle_difference_deg(route_angle, label_angle)
        if angle_diff > 25.0:
            continue

        choices.append((route_distance, endpoint_distance, label_name))

    if not choices:
        return ""

    _, _, best_label = min(choices, key=lambda item: (item[0], item[1], item[2]))
    return best_label


def matching_inventory_side_distances(
    label_name: str,
    endpoint_proj: Point,
    inventory_features: list[RoadwayInventoryFeature],
) -> tuple[float | None, float | None]:
    if not is_side_specific_route_variant_name(label_name):
        return (None, None)

    label_side = label_side_hint(label_name)
    if not label_side:
        return (None, None)

    same_side_distances: list[float] = []
    other_side_distances: list[float] = []
    for feature in inventory_features:
        if feature.kind != "route":
            continue
        if not same_route_corridor(label_name, feature.value):
            continue
        roadbed_side = roadbed_side_hint(feature.roadbed_id)
        if not roadbed_side:
            continue
        distance = endpoint_proj.distance(feature.geometry_proj)
        if distance > ROADWAY_INVENTORY_SEARCH_RADIUS_M:
            continue
        if roadbed_side == label_side:
            same_side_distances.append(distance)
        else:
            other_side_distances.append(distance)

    return (
        min(same_side_distances) if same_side_distances else None,
        min(other_side_distances) if other_side_distances else None,
    )


def infer_local_label_limit(
    endpoint_proj: Point,
    current_route_family: str,
    current_angle: float,
    nearby_labels: list[LabelFeature],
    inventory_features: list[RoadwayInventoryFeature],
) -> LimitCandidate | None:
    current_tokens = route_tokens(current_route_family)
    choices: list[tuple[float, float, float, str, LabelFeature, float | None, float | None]] = []
    relaxed: list[tuple[float, float, float, str, LabelFeature, float | None, float | None]] = []
    for label in nearby_labels:
        if label.geometry_proj.geom_type == "Point":
            continue
        if should_skip_local_label(label.name):
            continue
        name_key = normalize_limit_key(label.name)
        if name_key in current_tokens:
            continue
        distance = endpoint_proj.distance(label.geometry_proj)
        if distance > LABEL_SEARCH_RADIUS_M:
            continue
        line = representative_line(label.geometry_proj)
        label_angle = line_angle_deg(line, at_start=True)
        angle_diff = angle_difference_deg(current_angle, label_angle)
        same_side_distance, other_side_distance = matching_inventory_side_distances(
            label.name,
            endpoint_proj,
            inventory_features,
        )
        effective_distance = distance
        if same_side_distance is not None:
            effective_distance = min(distance, same_side_distance)
        item = (
            effective_distance,
            distance,
            -angle_diff,
            smart_title(label.name),
            label,
            same_side_distance,
            other_side_distance,
        )
        if angle_diff >= 25.0:
            choices.append(item)
        relaxed.append(item)

    if choices:
        effective_distance, distance, angle_score, _, label, same_side_distance, other_side_distance = min(
            choices,
            key=lambda item: (item[0], item[1], item[2], item[3]),
        )
        angle_diff = -angle_score
    elif relaxed:
        effective_distance, distance, angle_score, _, label, same_side_distance, other_side_distance = min(
            relaxed,
            key=lambda item: (item[0], item[1], item[2], item[3]),
        )
        angle_diff = -angle_score
    else:
        return None

    confidence = 0.75
    if effective_distance <= 40:
        confidence = 0.94
    elif effective_distance <= 90:
        confidence = 0.92
    elif effective_distance <= 150:
        confidence = 0.86
    elif effective_distance <= 225:
        confidence = 0.8
    if angle_diff < 20:
        confidence -= 0.08
    if same_side_distance is not None:
        confidence = max(confidence, 0.84)
        if other_side_distance is None or same_side_distance + 8.0 < other_side_distance:
            confidence += 0.06
        elif other_side_distance + 8.0 < same_side_distance:
            confidence -= 0.06

    value = smart_title(label.name)
    detail_parts = [
        f"{label.source}:{label.name}",
        f"effective={effective_distance:.1f}m",
        f"label={distance:.1f}m",
        f"angle={angle_diff:.1f}",
    ]
    if same_side_distance is not None:
        detail_parts.append(f"inventory_side={same_side_distance:.1f}m")
    if other_side_distance is not None:
        detail_parts.append(f"opposite_side={other_side_distance:.1f}m")
    return LimitCandidate(
        value=value,
        normalized=variant_limit_normalized(value),
        method=f"{label.source}_label",
        confidence=max(0.5, confidence),
        distance_m=effective_distance,
        detail=", ".join(detail_parts),
        angle_diff=angle_diff,
        anchor_geometry_proj=label.geometry_proj,
    )


def candidate_preference_key(candidate: LimitCandidate) -> tuple[float, float, str]:
    return (candidate.confidence, -candidate.distance_m, candidate.value)


def is_txdot_label_method(method: str) -> bool:
    return method.endswith("_label")


def candidate_matches_route_family(value: str, route_family: str) -> bool:
    if same_route_corridor(value, route_family):
        return True
    return has_named_road_with_route(value) and route_overlap(value, route_family)


def is_visual_local_clue(candidate: LimitCandidate | None) -> bool:
    return bool(
        candidate is not None
        and candidate.distance_m <= VISUAL_LOCAL_CLUE_MAX_DISTANCE_M
        and candidate.confidence >= VISUAL_LOCAL_CLUE_MIN_CONFIDENCE
        and (candidate.angle_diff is None or candidate.angle_diff >= 30.0)
    )


def is_continuation_like_route(candidate: LimitCandidate | None) -> bool:
    return bool(
        candidate is not None
        and candidate.angle_diff is not None
        and candidate.distance_m <= 15.0
        and candidate.angle_diff <= CONTINUATION_ROUTE_MAX_ANGLE_DEG
    )


def is_interchange_style_route(candidate: LimitCandidate | None) -> bool:
    if candidate is None or not is_route_limit(candidate.value):
        return False
    if "rec=0" in candidate.detail:
        return True
    return bool(
        candidate.angle_diff is not None
        and candidate.distance_m > 15.0
        and candidate.angle_diff <= INTERCHANGE_ROUTE_MAX_ANGLE_DEG
    )


def label_supports_route(label_name: str, route_family: str) -> bool:
    return normalize_limit_key(label_name) in route_tokens(route_family)


def maybe_interchange_candidate(
    *,
    endpoint_proj: Point,
    current_route_family: str,
    route_candidate: LimitCandidate | None,
    nearby_labels: Iterable[LabelFeature],
) -> LimitCandidate | None:
    if route_candidate is None:
        return None
    if not is_route_limit(route_candidate.value):
        return None
    if not is_interchange_style_route(route_candidate):
        return None
    if candidate_matches_route_family(route_candidate.value, current_route_family):
        return None
    if route_candidate.distance_m > CLOSE_ROUTE_CANDIDATE_M:
        return None

    interchange_labels = []
    for label in nearby_labels:
        if endpoint_proj.distance(label.geometry_proj) > INTERCHANGE_LABEL_MAX_DISTANCE_M:
            continue
        if not label_supports_route(label.name, route_candidate.value):
            continue
        upper = normalize_spacing(label.name).upper()
        if upper in route_tokens(route_candidate.value) or "SUPPLEMENTAL" in upper or "MAIN LANE" in upper:
            interchange_labels.append(label.name)

    if not interchange_labels:
        return None

    return LimitCandidate(
        value=f"{route_candidate.value} Interchange",
        normalized=normalize_limit_key(route_candidate.value),
        method="interchange_context",
        confidence=max(route_candidate.confidence, 0.94),
        distance_m=route_candidate.distance_m,
        detail=f"{route_candidate.detail}; interchange_labels={', '.join(sorted(set(interchange_labels))[:4])}",
        angle_diff=route_candidate.angle_diff,
        anchor_geometry_proj=route_candidate.anchor_geometry_proj,
    )


def select_preferred_candidate(
    primary: LimitCandidate | None,
    secondary: LimitCandidate | None,
) -> LimitCandidate | None:
    if primary is None:
        return secondary
    if secondary is None:
        return primary

    primary_route_like = is_route_limit(primary.value) or has_named_road_with_route(primary.value)
    secondary_route_like = is_route_limit(secondary.value) or has_named_road_with_route(secondary.value)
    if (
        primary_route_like
        and secondary_route_like
        and primary.method != secondary.method
        and route_system_matches(primary.value, secondary.value)
        and route_overlap(primary.value, secondary.value)
    ):
        if (
            primary.method == "route_intersection"
            and route_number_part_count(primary.value) > route_number_part_count(secondary.value)
            and primary.distance_m <= max(secondary.distance_m * 1.25, 30.0)
            and primary.confidence >= secondary.confidence - 0.05
        ):
            return primary
        if (
            secondary.method == "route_intersection"
            and route_number_part_count(secondary.value) > route_number_part_count(primary.value)
            and secondary.distance_m <= max(primary.distance_m * 1.25, 30.0)
            and secondary.confidence >= primary.confidence - 0.05
        ):
            return secondary

    if primary.normalized == secondary.normalized:
        if (
            primary.distance_m <= CLOSE_ROUTE_CANDIDATE_M
            and primary.distance_m < secondary.distance_m * 0.35
            and primary.confidence >= 0.7
        ):
            return primary
        if (
            secondary.distance_m <= CLOSE_ROUTE_CANDIDATE_M
            and secondary.distance_m < primary.distance_m * 0.35
            and secondary.confidence >= 0.7
        ):
            return secondary
        if (
            secondary.distance_m < primary.distance_m * 0.35
            and secondary.confidence >= primary.confidence - 0.03
        ):
            return secondary
        if (
            primary.distance_m < secondary.distance_m * 0.35
            and primary.confidence >= secondary.confidence - 0.03
        ):
            return primary
        return max((primary, secondary), key=candidate_preference_key)

    if secondary.confidence > primary.confidence + 0.04:
        return secondary
    if primary.confidence > secondary.confidence + 0.04:
        return primary
    if secondary.distance_m < primary.distance_m * 0.8:
        return secondary
    return primary


def infer_inventory_route_limit(
    endpoint_proj: Point,
    current_route_family: str,
    current_angle: float,
    inventory_features: list[RoadwayInventoryFeature],
) -> LimitCandidate | None:
    best_by_route: dict[str, tuple[tuple[float, int, float, float], LimitCandidate]] = {}
    for feature in inventory_features:
        if feature.kind != "route":
            continue
        if same_route_limit(current_route_family, feature.value):
            continue

        distance = endpoint_proj.distance(feature.geometry_proj)
        if distance > ROADWAY_INVENTORY_SEARCH_RADIUS_M:
            continue

        line = representative_line(feature.geometry_proj)
        feature_angle = local_line_angle_for_point(line, endpoint_proj)
        angle_diff = angle_difference_deg(current_angle, feature_angle)
        if angle_diff < 20.0 and distance > 8.0:
            continue

        confidence = 0.78
        if distance <= 10.0:
            confidence = 0.9
        elif distance <= 30.0:
            confidence = 0.86
        elif distance <= 80.0:
            confidence = 0.82

        if angle_diff >= 75.0:
            confidence += 0.08
        elif angle_diff >= 45.0:
            confidence += 0.05
        elif angle_diff >= 30.0:
            confidence += 0.02
        else:
            confidence -= 0.08

        if feature.roadbed_id in {"KG", "MG"}:
            confidence += 0.02
        if feature.roadbed_id in {"XG", "GS"} and angle_diff >= 45.0:
            confidence += 0.02

        confidence = min(max(confidence, 0.5), 0.98)
        candidate = LimitCandidate(
            value=feature.value,
            normalized=feature.normalized,
            method="txdot_inventory_route",
            confidence=confidence,
            distance_m=distance,
            detail=f"{feature.detail} ({distance:.1f}m, angle={angle_diff:.1f})",
            angle_diff=angle_diff,
            anchor_geometry_proj=feature.geometry_proj,
        )
        score = (
            confidence,
            route_system_priority(feature.value),
            angle_diff,
            -distance,
        )
        existing = best_by_route.get(candidate.normalized)
        if existing is None:
            best_by_route[candidate.normalized] = (score, candidate)
            continue

        _, existing_candidate = existing
        if (
            candidate.distance_m <= CLOSE_ROUTE_CANDIDATE_M
            and candidate.distance_m < existing_candidate.distance_m * 0.35
            and candidate.confidence >= existing_candidate.confidence - 0.2
        ):
            best_by_route[candidate.normalized] = (score, candidate)
            continue
        if (
            existing_candidate.distance_m <= CLOSE_ROUTE_CANDIDATE_M
            and existing_candidate.distance_m < candidate.distance_m * 0.35
            and existing_candidate.confidence >= candidate.confidence - 0.2
        ):
            continue
        if score > existing[0]:
            best_by_route[candidate.normalized] = (score, candidate)

    if not best_by_route:
        return None

    return max(best_by_route.values(), key=lambda item: item[0])[1]


def infer_inventory_local_limit(
    endpoint_proj: Point,
    current_route_family: str,
    current_angle: float,
    inventory_features: list[RoadwayInventoryFeature],
) -> LimitCandidate | None:
    current_tokens = route_tokens(current_route_family)
    best_by_name: dict[str, LimitCandidate] = {}

    for feature in inventory_features:
        if feature.kind != "local":
            continue
        if feature.normalized in current_tokens:
            continue

        distance = endpoint_proj.distance(feature.geometry_proj)
        if distance > ROADWAY_INVENTORY_SEARCH_RADIUS_M:
            continue

        line = representative_line(feature.geometry_proj)
        feature_angle = local_line_angle_for_point(line, endpoint_proj)
        angle_diff = angle_difference_deg(current_angle, feature_angle)
        if angle_diff < 20.0 and distance > 20.0:
            continue

        confidence = 0.7
        if distance <= 10.0:
            confidence = 0.88
        elif distance <= 40.0:
            confidence = 0.84
        elif distance <= 100.0:
            confidence = 0.78
        elif distance <= 180.0:
            confidence = 0.72

        if angle_diff >= 70.0:
            confidence += 0.04
        elif angle_diff >= 45.0:
            confidence += 0.02
        elif angle_diff < 20.0:
            confidence -= 0.08

        confidence = min(max(confidence, 0.5), 0.94)
        candidate = LimitCandidate(
            value=feature.value,
            normalized=feature.normalized,
            method="txdot_inventory_local",
            confidence=confidence,
            distance_m=distance,
            detail=f"{feature.detail} ({distance:.1f}m, angle={angle_diff:.1f})",
            angle_diff=angle_diff,
            anchor_geometry_proj=feature.geometry_proj,
        )
        existing = best_by_name.get(candidate.normalized)
        if existing is None or candidate_preference_key(candidate) > candidate_preference_key(existing):
            best_by_name[candidate.normalized] = candidate

    if not best_by_name:
        return None

    return max(best_by_name.values(), key=candidate_preference_key)


def tile_geometry_to_wgs84(geometry: BaseGeometry, tile: mercantile.Tile) -> BaseGeometry:
    west, south, east, north = mercantile.bounds(tile)
    min_x, min_y = WGS84_TO_3857.transform(west, south)
    max_x, max_y = WGS84_TO_3857.transform(east, north)
    extent = 65536.0
    scale_x = (max_x - min_x) / extent
    scale_y = (max_y - min_y) / extent

    def convert(x: float, y: float, z: float | None = None) -> tuple[float, float]:
        web_x = min_x + x * scale_x
        web_y = max_y - y * scale_y
        return WEBM_TO_WGS84.transform(web_x, web_y)

    return transform(convert, geometry)


def label_tile_cache_path(
    label_tile_root: pathlib.Path,
    source_name: str,
    z: int,
    x: int,
    y: int,
) -> pathlib.Path:
    return label_tile_root / source_name / str(z) / str(x) / f"{y}.pbf"


def label_tile_missing_marker_path(cache_path: pathlib.Path) -> pathlib.Path:
    return cache_path.with_suffix(".missing")


def endpoint_neighbor_tiles(endpoint_wgs84: Point) -> set[tuple[int, int, int]]:
    tiles: set[tuple[int, int, int]] = set()
    for zoom in LABEL_TILE_ZOOMS:
        tile = mercantile.tile(endpoint_wgs84.x, endpoint_wgs84.y, zoom)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                tiles.add((tile.z, tile.x + dx, tile.y + dy))
    return tiles


def collect_label_tile_jobs(
    segment_features: Iterable[SegmentFeature],
    *,
    source_names: Iterable[str] | None = None,
) -> list[tuple[str, int, int, int]]:
    if source_names is None:
        source_names = VECTOR_TILE_SOURCES.keys()

    endpoint_tiles: set[tuple[int, int, int]] = set()
    for feature in segment_features:
        endpoint_tiles.update(endpoint_neighbor_tiles(Point(feature.line_wgs84.coords[0])))
        endpoint_tiles.update(endpoint_neighbor_tiles(Point(feature.line_wgs84.coords[-1])))

    jobs = [
        (source_name, z, x, y)
        for source_name in source_names
        for z, x, y in endpoint_tiles
    ]
    return sorted(jobs, key=lambda item: (item[0], item[1], item[2], item[3]))


def fetch_label_tile_bytes(
    source_name: str,
    z: int,
    x: int,
    y: int,
    *,
    label_tile_root: pathlib.Path | None = None,
    save_to_cache: bool = True,
) -> bytes | None:
    cache_path: pathlib.Path | None = None
    missing_marker_path: pathlib.Path | None = None
    if label_tile_root is not None:
        cache_path = label_tile_cache_path(label_tile_root, source_name, z, x, y)
        missing_marker_path = label_tile_missing_marker_path(cache_path)
        if cache_path.exists():
            return cache_path.read_bytes()
        if missing_marker_path.exists():
            return None

    source = VECTOR_TILE_SOURCES[source_name]
    url = source["template"].format(z=z, y=y, x=x)
    response = session_get(url, timeout=60, suppress_errors=True)
    if response is None:
        return None
    if response.status_code == 404:
        if missing_marker_path is not None and save_to_cache:
            missing_marker_path.parent.mkdir(parents=True, exist_ok=True)
            missing_marker_path.touch()
        return None

    response.raise_for_status()
    content = response.content
    if cache_path is not None and save_to_cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(content)
    return content


def cache_label_tile(
    label_tile_root: pathlib.Path,
    source_name: str,
    z: int,
    x: int,
    y: int,
) -> str:
    cache_path = label_tile_cache_path(label_tile_root, source_name, z, x, y)
    missing_marker_path = label_tile_missing_marker_path(cache_path)
    if cache_path.exists():
        return "cached"
    if missing_marker_path.exists():
        return "missing"

    content = fetch_label_tile_bytes(
        source_name,
        z,
        x,
        y,
        label_tile_root=label_tile_root,
        save_to_cache=True,
    )
    if content is None:
        return "missing"
    return "downloaded"


def download_label_tiles(
    *,
    output_root: pathlib.Path,
    segment_features: Iterable[SegmentFeature],
    workers: int = DEFAULT_TILE_DOWNLOAD_WORKERS,
) -> dict[str, int]:
    jobs = collect_label_tile_jobs(segment_features)
    counts = {
        "total": len(jobs),
        "downloaded": 0,
        "cached": 0,
        "missing": 0,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    if not jobs:
        return counts

    worker_count = max(1, workers)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(cache_label_tile, output_root, source_name, z, x, y)
            for source_name, z, x, y in jobs
        ]
        for future in as_completed(futures):
            counts[future.result()] += 1
    return counts


@lru_cache(maxsize=4096)
def fetch_tile_labels(
    source_name: str,
    z: int,
    x: int,
    y: int,
    label_tile_root: str | None = None,
) -> tuple[LabelFeature, ...]:
    source = VECTOR_TILE_SOURCES[source_name]
    tile_bytes = fetch_label_tile_bytes(
        source_name,
        z,
        x,
        y,
        label_tile_root=pathlib.Path(label_tile_root) if label_tile_root else None,
        save_to_cache=True,
    )
    if tile_bytes is None:
        return ()
    data = decode_vector_tile(tile_bytes)
    labels: list[LabelFeature] = []
    for layer_name in source["layers"]:
        for feature in data.get(layer_name, {}).get("features", []):
            name = str(feature.get("properties", {}).get("_name", "")).strip()
            if not name:
                continue
            geom = tile_geometry_to_wgs84(shape(feature["geometry"]), mercantile.Tile(x=x, y=y, z=z))
            labels.append(
                LabelFeature(
                    name=name,
                    source=source_name,
                    geometry_proj=project_geometry(geom),
                )
            )
    return tuple(labels)


def nearby_labels(
    endpoint_wgs84: Point,
    endpoint_proj: Point,
    *,
    source_names: tuple[str, ...] = ("basemap", "fallback"),
    label_tile_root: str | None = None,
) -> list[LabelFeature]:
    labels: list[LabelFeature] = []
    seen: set[tuple[str, str]] = set()
    for source_name in source_names:
        for zoom in LABEL_TILE_ZOOMS:
            tile = mercantile.tile(endpoint_wgs84.x, endpoint_wgs84.y, zoom)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbor = mercantile.Tile(x=tile.x + dx, y=tile.y + dy, z=tile.z)
                    for label in fetch_tile_labels(
                        source_name,
                        neighbor.z,
                        neighbor.x,
                        neighbor.y,
                        label_tile_root,
                    ):
                        if endpoint_proj.distance(label.geometry_proj) > LABEL_SEARCH_RADIUS_M * 1.5:
                            continue
                        key = (label.name, label.geometry_proj.wkt)
                        if key in seen:
                            continue
                        seen.add(key)
                        labels.append(label)
    return labels


def tile_radius_for_search_distance(
    endpoint_wgs84: Point,
    zoom: int,
    search_distance_m: float,
) -> int:
    tile = mercantile.tile(endpoint_wgs84.x, endpoint_wgs84.y, zoom)
    west, south, east, north = mercantile.bounds(tile)
    tile_proj = project_geometry(box(west, south, east, north))
    min_x, min_y, max_x, max_y = tile_proj.bounds
    tile_span_m = max(max_x - min_x, max_y - min_y, 1.0)
    return max(1, int(math.ceil(search_distance_m / tile_span_m)) + 1)


def search_labels_within_distance(
    endpoint_wgs84: Point,
    endpoint_proj: Point,
    *,
    max_search_distance_m: float,
    source_names: tuple[str, ...] = ("basemap", "fallback"),
    label_tile_root: str | None = None,
) -> list[LabelFeature]:
    labels: list[LabelFeature] = []
    seen: set[tuple[str, str]] = set()
    for source_name in source_names:
        for zoom in MID_CORRIDOR_LABEL_TILE_ZOOMS:
            tile = mercantile.tile(endpoint_wgs84.x, endpoint_wgs84.y, zoom)
            tile_radius = tile_radius_for_search_distance(
                endpoint_wgs84,
                zoom,
                max_search_distance_m,
            )
            for dx in range(-tile_radius, tile_radius + 1):
                for dy in range(-tile_radius, tile_radius + 1):
                    neighbor = mercantile.Tile(x=tile.x + dx, y=tile.y + dy, z=tile.z)
                    for label in fetch_tile_labels(
                        source_name,
                        neighbor.z,
                        neighbor.x,
                        neighbor.y,
                        label_tile_root,
                    ):
                        if endpoint_proj.distance(label.geometry_proj) > max_search_distance_m:
                            continue
                        key = (label.name, label.geometry_proj.wkt)
                        if key in seen:
                            continue
                        seen.add(key)
                        labels.append(label)
    return labels


def fetch_roadway_inventory_features_within_distance(
    lon: float,
    lat: float,
    search_distance_m: float,
) -> tuple[RoadwayInventoryFeature, ...]:
    features = query_arcgis_geojson(
        ROADWAY_INVENTORY_QUERY_URL,
        out_fields=ROADWAY_INVENTORY_OUT_FIELDS,
        page_size=1000,
        extra_params={
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326",
            "distance": str(search_distance_m),
            "units": "esriSRUnit_Meter",
        },
    )

    inventory_features: list[RoadwayInventoryFeature] = []
    for feature in features:
        inventory_feature = roadway_inventory_feature_from_geojson(feature)
        if inventory_feature is not None:
            inventory_features.append(inventory_feature)
    return tuple(inventory_features)


@lru_cache(maxsize=4096)
def fetch_roadway_inventory_features(lon: float, lat: float) -> tuple[RoadwayInventoryFeature, ...]:
    features = query_arcgis_geojson(
        ROADWAY_INVENTORY_QUERY_URL,
        out_fields=ROADWAY_INVENTORY_OUT_FIELDS,
        page_size=200,
        extra_params={
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326",
            "distance": str(ROADWAY_INVENTORY_SEARCH_RADIUS_M),
            "units": "esriSRUnit_Meter",
        },
    )

    inventory_features: list[RoadwayInventoryFeature] = []
    for feature in features:
        inventory_feature = roadway_inventory_feature_from_geojson(feature)
        if inventory_feature is not None:
            inventory_features.append(inventory_feature)
    return tuple(inventory_features)


@lru_cache(maxsize=4)
def load_local_roadway_inventory_lookup(path_str: str) -> RoadwayInventoryLookup:
    path = pathlib.Path(path_str)
    data = json.loads(path.read_text(encoding="utf-8"))
    inventory_features: list[RoadwayInventoryFeature] = []
    for feature in data.get("features", []):
        inventory_feature = roadway_inventory_feature_from_geojson(feature)
        if inventory_feature is not None:
            inventory_features.append(inventory_feature)
    return build_roadway_inventory_lookup(inventory_features)


def nearby_roadway_inventory(
    endpoint_wgs84: Point,
    endpoint_proj: Point,
    roadway_inventory_lookup: RoadwayInventoryLookup | None = None,
) -> list[RoadwayInventoryFeature]:
    if roadway_inventory_lookup is not None:
        search_area = endpoint_proj.buffer(ROADWAY_INVENTORY_SEARCH_RADIUS_M)
        nearby_features: list[RoadwayInventoryFeature] = []
        for index in roadway_inventory_lookup.tree.query(search_area):
            feature = roadway_inventory_lookup.features[int(index)]
            if endpoint_proj.distance(feature.geometry_proj) <= ROADWAY_INVENTORY_SEARCH_RADIUS_M:
                nearby_features.append(feature)
        return nearby_features

    return list(
        fetch_roadway_inventory_features(
            round(endpoint_wgs84.x, 6),
            round(endpoint_wgs84.y, 6),
        )
    )


def search_roadway_inventory_within_distance(
    endpoint_wgs84: Point,
    endpoint_proj: Point,
    *,
    roadway_inventory_lookup: RoadwayInventoryLookup | None = None,
    max_search_distance_m: float,
) -> list[RoadwayInventoryFeature]:
    if roadway_inventory_lookup is not None:
        search_area = endpoint_proj.buffer(max_search_distance_m)
        nearby_features: list[RoadwayInventoryFeature] = []
        for index in roadway_inventory_lookup.tree.query(search_area):
            feature = roadway_inventory_lookup.features[int(index)]
            if endpoint_proj.distance(feature.geometry_proj) <= max_search_distance_m:
                nearby_features.append(feature)
        return nearby_features

    return list(
        fetch_roadway_inventory_features_within_distance(
            round(endpoint_wgs84.x, 6),
            round(endpoint_wgs84.y, 6),
            max_search_distance_m,
        )
    )


def normalize_spacing(value: str) -> str:
    return re.sub(r"\s+", " ", safe_text(value)).strip()


def csv_segment_family(segment_name: str) -> str:
    return readable_to_route_family(normalize_spacing(safe_text(segment_name)))


def canonical(value: str) -> str:
    """Minimal normalization for exact-match comparison: trim, collapse whitespace, lowercase."""
    return normalize_spacing(safe_text(value)).lower()


def normalize_limit_key(value: str) -> str:
    text = normalize_spacing(safe_text(value))
    if not text:
        return ""
    upper = text.upper()

    route_match = re.search(r"\((IH|US|SH|FM|RM|SS|SL|BS|BU|BI|CR|FS|PA|PR|TL)\s+([0-9]+[A-Z]?(?:/[0-9]+[A-Z]?)?)\)", upper)
    if route_match:
        return f"{route_match.group(1)} {route_match.group(2)}".strip()

    route_match = re.search(r"\b(IH|US|SH|FM|RM|SS|SL|BS|BU|BI|CR|FS|PA|PR|TL)\s+([0-9]+[A-Z]?(?:/[0-9]+[A-Z]?)?)\b", upper)
    if route_match:
        return f"{route_match.group(1)} {route_match.group(2)}".strip()

    county_match = re.search(r"\b([A-Z]+(?:\s+[A-Z]+)*)\s+COUNTY(?:\s+LINE)?\b", upper)
    if county_match:
        return smart_title(county_match.group(1)) + " County"

    cleaned = re.sub(r"^(NORTH|SOUTH|EAST|WEST|NE|NW|SE|SW)\s+OF\s+", "", upper)
    cleaned = cleaned.replace(" COUNTY LINE", "")
    cleaned = cleaned.replace(" INTERCHANGE", "")
    return normalize_spacing(smart_title(cleaned))


def is_route_limit(value: str) -> bool:
    normalized = normalize_limit_key(value)
    return bool(
        re.match(r"^(IH|US|SH|FM|RM|SS|SL|BS|BU|BI|CR|FS|PA|PR|TL)\s+\d", normalized)
    )


def is_county_limit(value: str) -> bool:
    return "COUNTY" in normalize_spacing(safe_text(value)).upper()


def has_directional_description(value: str) -> bool:
    text = normalize_spacing(safe_text(value)).upper()
    return bool(
        re.search(
            r"\b(NORTH|SOUTH|EAST|WEST|NE|NW|SE|SW)\s+OF\b|\bBEFORE\b|\bAFTER\b",
            text,
        )
    )


def has_service_or_frontage_reference(value: str) -> bool:
    text = normalize_spacing(safe_text(value)).upper()
    return any(token in text for token in ("FRONTAGE", "SERVICE RD", "SERVICE ROAD", "INTERCHANGE"))


def normalize_local_name_key(value: str) -> str:
    text = normalize_spacing(safe_text(value)).upper()
    if not text:
        return ""

    text = re.sub(r"\([^)]*\)", "", text)
    replacements = {
        "COUNTY ROAD": "CR",
        "FRONTAGE ROAD": "FRONTAGE RD",
        "SERVICE ROAD": "SERVICE RD",
        "STREET": "ST",
        "AVENUE": "AVE",
        "BOULEVARD": "BLVD",
        "PARKWAY": "PKWY",
        "HIGHWAY": "HWY",
        "TRAIL": "TRL",
        "ROAD": "RD",
        "DRIVE": "DR",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)

    text = re.sub(r"^(NORTH|SOUTH|EAST|WEST|NE|NW|SE|SW)\s+OF\s+", "", text)
    text = re.sub(r"\b(N|S|E|W|NE|NW|SE|SW)\b", "", text)
    text = re.sub(r"[^A-Z0-9]", "", text)
    return text


def local_limits_equivalent(value_a: str, value_b: str) -> bool:
    key_a = normalize_local_name_key(value_a)
    key_b = normalize_local_name_key(value_b)
    return bool(key_a and key_b and key_a == key_b)


def route_number_token(value: str) -> str:
    text = normalize_spacing(safe_text(value)).upper()
    if not text:
        return ""
    match = re.search(
        r"(?:\(|\b)(IH|US|SH|FM|RM|SS|SL|BS|BU|BI|CR|FS|PA|PR|TL)\s+([0-9]+[A-Z]?(?:/[0-9]+[A-Z]?)?)",
        text,
    )
    if not match:
        return ""
    return match.group(2)


def route_number_parts(value: str) -> set[str]:
    token = route_number_token(value)
    if not token:
        return set()
    return {part for part in token.split("/") if part}


def route_number_part_count(value: str) -> int:
    return len(route_number_parts(value))


def named_route_matches_business_variant(existing_value: str, candidate_value: str) -> bool:
    if not has_named_road_with_route(existing_value):
        return False

    candidate_system = route_system(candidate_value)
    if candidate_system not in {"BU", "BS", "BI"}:
        return False

    existing_parts = route_number_parts(existing_value)
    candidate_parts = route_number_parts(candidate_value)
    if not existing_parts or not candidate_parts:
        return False

    for candidate_part in candidate_parts:
        match = re.fullmatch(r"(\d+)([A-Z])", candidate_part)
        if not match:
            continue
        suffix = match.group(2)
        if suffix in {"N", "S", "E", "W"}:
            continue
        if match.group(1) in existing_parts:
            return True
    return False


def has_named_road_with_route(value: str) -> bool:
    text = safe_text(value)
    return "(" in text and ")" in text and bool(route_number_token(text))


def route_system_matches(value_a: str, value_b: str) -> bool:
    return normalize_limit_key(value_a).split(" ", 1)[0:1] == normalize_limit_key(value_b).split(" ", 1)[0:1]


def route_overlap(existing_value: str, candidate_value: str) -> bool:
    existing_parts = route_number_parts(existing_value)
    candidate_parts = route_number_parts(candidate_value)
    if not existing_parts or not candidate_parts:
        return False
    return bool(existing_parts & candidate_parts)


def limits_equivalent(existing_value: str, candidate: LimitCandidate | None) -> bool:
    if candidate is None:
        return False

    existing_norm = normalize_limit_key(existing_value)
    if existing_norm == candidate.normalized or normalize_spacing(existing_value) == candidate.normalized:
        return True

    candidate_is_route_like = is_route_limit(candidate.value) or has_named_road_with_route(candidate.value)
    if (
        candidate_is_route_like
        and has_named_road_with_route(existing_value)
        and route_overlap(existing_value, candidate.value)
    ):
        return True

    if (
        candidate_is_route_like
        and has_named_road_with_route(existing_value)
        and named_route_matches_business_variant(existing_value, candidate.value)
    ):
        return True

    if (
        candidate_is_route_like
        and is_route_limit(existing_value)
        and route_system_matches(existing_value, candidate.value)
        and route_overlap(existing_value, candidate.value)
    ):
        return True

    return False


@dataclass(frozen=True)
class GatheredCandidates:
    """All candidates collected for a single endpoint before heuristic selection."""
    endpoint_wgs84: Point
    endpoint_proj: Point
    current_angle: float
    current_feature_ids: frozenset[str]
    county: LimitCandidate | None
    route: LimitCandidate | None
    ftw_route: LimitCandidate | None
    local: LimitCandidate | None
    local_inventory: LimitCandidate | None
    interchange: LimitCandidate | None
    current_route_family: str
    primary_labels: tuple[LabelFeature, ...]
    fallback_labels: tuple[LabelFeature, ...]
    inventory_features: tuple[RoadwayInventoryFeature, ...]
    all_segments: tuple[SegmentFeature, ...]
    label_tile_root: str | None
    roadway_inventory_lookup: RoadwayInventoryLookup | None


def gather_candidates(
    *,
    endpoint_wgs84: Point,
    endpoint_proj: Point,
    interior_proj: Point,
    current_angle: float,
    current_route_family: str,
    current_feature_ids: set[str],
    counties: CountyLookup,
    all_segments: list[SegmentFeature],
    label_tile_root: str | None = None,
    roadway_inventory_lookup: RoadwayInventoryLookup | None = None,
) -> GatheredCandidates:
    """Stage 1: Collect all candidate limits from every data source."""
    county_candidate = infer_county_limit(endpoint_proj, interior_proj, counties)

    # Preferred model:
    #   1. use the roadway inventory as the primary geometry matcher
    #   2. use the TxDOT label layers as the naming authority when they provide
    #      a more specific roadway name for that matched geometry
    #   3. keep FTW route intersections and fallback labels as supporting evidence
    inventory_features = nearby_roadway_inventory(
        endpoint_wgs84,
        endpoint_proj,
        roadway_inventory_lookup,
    )
    primary_labels = nearby_labels(
        endpoint_wgs84,
        endpoint_proj,
        source_names=("basemap",),
        label_tile_root=label_tile_root,
    )
    fallback_labels = nearby_labels(
        endpoint_wgs84,
        endpoint_proj,
        source_names=("fallback",),
        label_tile_root=label_tile_root,
    )

    ftw_route_candidate = infer_route_limit(
        endpoint_proj,
        current_angle,
        current_feature_ids,
        current_route_family,
        all_segments,
        primary_labels,
    )
    ftw_route_candidate = select_preferred_candidate(
        ftw_route_candidate,
        infer_route_limit(
            endpoint_proj,
            current_angle,
            current_feature_ids,
            current_route_family,
            all_segments,
            fallback_labels,
        ),
    )

    route_candidate = infer_inventory_route_limit(
        endpoint_proj,
        current_route_family,
        current_angle,
        inventory_features,
    )
    route_candidate = select_preferred_candidate(
        route_candidate,
        ftw_route_candidate,
    )

    local_inventory_candidate = infer_inventory_local_limit(
        endpoint_proj,
        current_route_family,
        current_angle,
        inventory_features,
    )
    local_candidate = local_inventory_candidate
    local_candidate = select_preferred_candidate(
        local_candidate,
        infer_local_label_limit(
            endpoint_proj,
            current_route_family,
            current_angle,
            primary_labels,
            inventory_features,
        ),
    )
    local_candidate = select_preferred_candidate(
        local_candidate,
        infer_local_label_limit(
            endpoint_proj,
            current_route_family,
            current_angle,
            fallback_labels,
            inventory_features,
        ),
    )

    interchange_candidate = maybe_interchange_candidate(
        endpoint_proj=endpoint_proj,
        current_route_family=current_route_family,
        route_candidate=route_candidate,
        nearby_labels=[*primary_labels, *fallback_labels],
    )

    return GatheredCandidates(
        endpoint_wgs84=endpoint_wgs84,
        endpoint_proj=endpoint_proj,
        current_angle=current_angle,
        current_feature_ids=frozenset(current_feature_ids),
        county=county_candidate,
        route=route_candidate,
        ftw_route=ftw_route_candidate,
        local=local_candidate,
        local_inventory=local_inventory_candidate,
        interchange=interchange_candidate,
        current_route_family=current_route_family,
        primary_labels=tuple(primary_labels),
        fallback_labels=tuple(fallback_labels),
        inventory_features=tuple(inventory_features),
        all_segments=tuple(all_segments),
        label_tile_root=label_tile_root,
        roadway_inventory_lookup=roadway_inventory_lookup,
    )


HEURISTIC_LABEL_ORDER = (
    "offset_from_marker",
    "county_boundary",
    "route_intersection",
    "interchange_context",
    "frontage_service_road_variant",
    "local_labeled_road",
    "orientation_direction_effect",
    "route_alias_or_business_label",
    "shared_endpoint_with_adjacent_segment",
    "fallback_or_unclear",
)

COUNTY_OFFSET_FORMAT_MIN_M = 20.0
COUNTY_OFFSET_FORMAT_MAX_M = 50.0
LOCAL_OFFSET_FORMAT_MIN_M = 30.0
LOCAL_OFFSET_FORMAT_MAX_M = 60.0
MAJOR_ROUTE_PREFIXES = {"IH", "US", "SH", "TL", "SL", "SS", "BU", "BS", "BI"}


def combine_heuristic_labels(*labels: str) -> str:
    seen: set[str] = set()
    ordered: list[str] = []
    label_order = {label: index for index, label in enumerate(HEURISTIC_LABEL_ORDER)}
    for item in labels:
        for part in item.split("|"):
            label = normalize_spacing(part)
            if not label or label in seen:
                continue
            seen.add(label)
            ordered.append(label)
    ordered.sort(key=lambda label: (label_order.get(label, len(HEURISTIC_LABEL_ORDER)), label))
    return " | ".join(ordered)


def base_heuristic_labels(candidate: LimitCandidate) -> list[str]:
    if candidate.method == "county_boundary":
        labels = ["county_boundary"]
    elif candidate.method == "interchange_context":
        labels = ["interchange_context"]
    elif candidate.method in {"route_intersection", "txdot_inventory_route"}:
        labels = ["route_intersection"]
    elif candidate.method.endswith("_label") or candidate.method == "txdot_inventory_local":
        labels = ["local_labeled_road"]
    else:
        labels = ["fallback_or_unclear"]

    if is_frontage_or_service_route_variant_name(candidate.value):
        labels.append("frontage_service_road_variant")
    elif is_specific_route_variant_name(candidate.value) or has_named_road_with_route(candidate.value):
        labels.append("route_alias_or_business_label")
    return labels


def with_heuristics(candidate: LimitCandidate, *labels: str) -> LimitCandidate:
    return replace(
        candidate,
        heuristic=combine_heuristic_labels(candidate.heuristic, *base_heuristic_labels(candidate), *labels),
    )


def more_descriptive_route_candidate(
    route_candidate: LimitCandidate,
    ftw_route_candidate: LimitCandidate | None,
) -> LimitCandidate:
    if ftw_route_candidate is None:
        return route_candidate
    if not route_overlap(route_candidate.value, ftw_route_candidate.value):
        return route_candidate
    if route_number_part_count(ftw_route_candidate.value) <= route_number_part_count(route_candidate.value):
        return route_candidate
    if ftw_route_candidate.distance_m > 60.0:
        return route_candidate
    if ftw_route_candidate.confidence < route_candidate.confidence - 0.05:
        return route_candidate
    return ftw_route_candidate


def frontage_variant_matches_route(
    local_candidate: LimitCandidate | None,
    route_candidate: LimitCandidate | None,
) -> bool:
    return bool(
        local_candidate is not None
        and route_candidate is not None
        and is_frontage_or_service_route_variant_name(local_candidate.value)
        and candidate_matches_route_family(local_candidate.value, route_candidate.value)
    )


def should_prefer_mainline_route_over_frontage(candidates: GatheredCandidates) -> bool:
    route_candidate = candidates.route
    local_candidate = candidates.local
    if not frontage_variant_matches_route(local_candidate, route_candidate):
        return False
    assert route_candidate is not None
    assert local_candidate is not None

    current_prefix = route_system(candidates.current_route_family)
    if current_prefix in MAJOR_ROUTE_PREFIXES and route_candidate.distance_m <= 10.0:
        return True
    if route_candidate.distance_m <= 2.0 and local_candidate.distance_m >= 14.0:
        return True
    return bool(
        route_candidate.angle_diff is not None
        and route_candidate.angle_diff >= 75.0
        and local_candidate.distance_m >= 20.0
    )


def route_supports_frontage_override(route_value: str) -> bool:
    return route_system(route_value) not in {"IH", "TL"}


def nearest_route_roadbed_distance(
    route_value: str,
    endpoint_proj: Point,
    inventory_features: tuple[RoadwayInventoryFeature, ...],
    roadbed_ids: set[str],
) -> float | None:
    distances = [
        endpoint_proj.distance(feature.geometry_proj)
        for feature in inventory_features
        if feature.kind == "route"
        and feature.roadbed_id in roadbed_ids
        and same_route_corridor(route_value, feature.value)
    ]
    return min(distances) if distances else None


def frontage_label_side_rank(
    same_side_distance: float | None,
    other_side_distance: float | None,
) -> int:
    if same_side_distance is not None and (
        other_side_distance is None or same_side_distance + 8.0 < other_side_distance
    ):
        return 0
    if other_side_distance is not None and (
        same_side_distance is None or other_side_distance + 8.0 < same_side_distance
    ):
        return 2
    return 1


def matching_frontage_label_candidate(
    candidates: GatheredCandidates,
) -> LimitCandidate | None:
    route_candidate = candidates.route
    if route_candidate is None:
        return None

    best_candidate: LimitCandidate | None = None
    best_key: tuple[object, ...] | None = None
    for label in (*candidates.primary_labels, *candidates.fallback_labels):
        value = smart_title(label.name)
        if not is_frontage_or_service_route_variant_name(value):
            continue
        if not candidate_matches_route_family(value, route_candidate.value):
            continue
        if candidate_matches_route_family(value, candidates.current_route_family):
            continue

        distance = candidates.endpoint_proj.distance(label.geometry_proj)
        if distance > FRONTAGE_OVERRIDE_LABEL_MAX_DISTANCE_M:
            continue

        same_side_distance, other_side_distance = matching_inventory_side_distances(
            value,
            candidates.endpoint_proj,
            list(candidates.inventory_features),
        )
        side_rank = frontage_label_side_rank(same_side_distance, other_side_distance)

        confidence = 0.88
        if distance <= 5.0:
            confidence = 0.98
        elif distance <= 15.0:
            confidence = 0.96
        elif distance <= 30.0:
            confidence = 0.92
        if side_rank == 0:
            confidence += 0.02
        elif side_rank == 2:
            confidence -= 0.08
        confidence = min(max(confidence, 0.5), 1.0)

        detail_parts = [
            f"{label.source}:{label.name}",
            f"label={distance:.1f}m",
        ]
        if same_side_distance is not None:
            detail_parts.append(f"inventory_side={same_side_distance:.1f}m")
        if other_side_distance is not None:
            detail_parts.append(f"opposite_side={other_side_distance:.1f}m")

        candidate = LimitCandidate(
            value=value,
            normalized=variant_limit_normalized(value),
            method=f"{label.source}_label",
            confidence=confidence,
            distance_m=distance,
            detail=", ".join(detail_parts),
            anchor_geometry_proj=label.geometry_proj,
        )
        key = (
            side_rank,
            0 if label.source == "basemap" else 1,
            distance,
            same_side_distance if same_side_distance is not None else float("inf"),
            candidate.value,
        )
        if best_key is None or key < best_key:
            best_candidate = candidate
            best_key = key

    return best_candidate


def frontage_override_has_route_evidence(
    candidates: GatheredCandidates,
    frontage_candidate: LimitCandidate,
) -> bool:
    route_candidate = candidates.route
    if route_candidate is None:
        return False

    mainline_distance = nearest_route_roadbed_distance(
        route_candidate.value,
        candidates.endpoint_proj,
        candidates.inventory_features,
        {"KG", "MG"},
    )
    same_side_distance, _ = matching_inventory_side_distances(
        frontage_candidate.value,
        candidates.endpoint_proj,
        list(candidates.inventory_features),
    )

    if (
        mainline_distance is not None
        and mainline_distance < FRONTAGE_OVERRIDE_MAINLINE_MIN_DISTANCE_M
    ):
        return False
    if (
        same_side_distance is not None
        and mainline_distance is not None
        and same_side_distance >= mainline_distance
    ):
        return False
    return True


def should_prefer_frontage_over_mainline(
    candidates: GatheredCandidates,
) -> LimitCandidate | None:
    route_candidate = candidates.route
    if route_candidate is None:
        return None
    if not is_route_limit(route_candidate.value):
        return None
    if is_frontage_or_service_route_variant_name(route_candidate.value):
        return None
    if candidate_matches_route_family(route_candidate.value, candidates.current_route_family):
        return None
    if not route_supports_frontage_override(route_candidate.value):
        return None

    frontage_candidate = matching_frontage_label_candidate(candidates)
    if frontage_candidate is None:
        return None
    if not frontage_override_has_route_evidence(candidates, frontage_candidate):
        return None

    local_candidate = candidates.local
    if frontage_variant_matches_route(local_candidate, route_candidate):
        if (
            local_candidate.distance_m <= FRONTAGE_OVERRIDE_LABEL_MAX_DISTANCE_M
            and frontage_candidate.distance_m <= FRONTAGE_DIRECT_OVERRIDE_LABEL_MAX_DISTANCE_M
            and should_prefer_mainline_route_over_frontage(candidates)
        ):
            return frontage_candidate
        return None

    if (
        route_candidate.method == "txdot_inventory_route"
        and route_candidate.distance_m <= 2.0
        and frontage_candidate.distance_m <= FRONTAGE_DIRECT_OVERRIDE_LABEL_MAX_DISTANCE_M
    ):
        if (
            local_candidate is not None
            and local_candidate.distance_m <= 3.0
            and local_candidate.angle_diff is not None
            and local_candidate.angle_diff < 25.0
        ):
            return None
        return frontage_candidate

    if (
        route_candidate.method == "route_intersection"
        and route_candidate.distance_m >= FRONTAGE_ROUTE_INTERSECTION_OVERRIDE_MIN_DISTANCE_M
        and local_candidate is not None
        and not candidate_matches_route_family(local_candidate.value, route_candidate.value)
    ):
        return frontage_candidate

    return None


def should_prefer_confirmed_local_anchor(candidates: GatheredCandidates) -> bool:
    route_candidate = candidates.route
    local_candidate = candidates.local
    local_inventory_candidate = candidates.local_inventory
    if route_candidate is None or local_candidate is None or local_inventory_candidate is None:
        return False
    if canonical(local_candidate.value) != canonical(local_inventory_candidate.value):
        return False
    if local_inventory_candidate.distance_m > 10.0 or local_candidate.distance_m > 25.0:
        return False
    if local_inventory_candidate.angle_diff is None or local_inventory_candidate.angle_diff < 70.0:
        return False
    if route_candidate.angle_diff is None or route_candidate.angle_diff > CONTINUATION_ROUTE_MAX_ANGLE_DEG:
        return False
    return True


def should_prefer_inventory_cross_street(candidates: GatheredCandidates) -> bool:
    local_candidate = candidates.local
    local_inventory_candidate = candidates.local_inventory
    if candidates.route is not None or local_candidate is None or local_inventory_candidate is None:
        return False
    if canonical(local_candidate.value) == canonical(local_inventory_candidate.value):
        return False
    if local_inventory_candidate.distance_m > 60.0 or local_inventory_candidate.confidence < 0.8:
        return False
    if local_candidate.distance_m > 15.0:
        return False
    if local_inventory_candidate.angle_diff is None or local_candidate.angle_diff is None:
        return False
    return (
        local_inventory_candidate.angle_diff >= 80.0
        and local_candidate.angle_diff <= 70.0
    )


def local_reference_priority(value: str) -> int:
    upper = normalize_spacing(value).upper()
    if re.search(r"\b(HIGHWAY|HWY|FREEWAY|FWY|EXPWY|EXPY)\b", upper):
        return 5
    if re.search(r"\b(COUNTY ROAD|CR|PARKWAY|PKWY|BOULEVARD|BLVD|ROAD|RD|STREET|ST|AVENUE|AVE)\b", upper):
        return 4
    if re.search(r"\b(DRIVE|DR|TRAIL|TRL|LANE|LN)\b", upper):
        return 3
    if re.search(r"\b(TERRACE|TER|COURT|CT|WAY|PLACE|PL)\b", upper):
        return 2
    return 3


def references_current_corridor(
    value: str,
    current_route_family: str,
    angle_diff: float | None,
) -> bool:
    if candidate_matches_route_family(value, current_route_family):
        return True
    if is_frontage_or_service_route_variant_name(value) and route_number_base_overlap(value, current_route_family):
        return True
    return bool(
        is_route_limit(value)
        and route_number_base_overlap(value, current_route_family)
        and (
            route_system(value) in {"BU", "BS", "BI", "TL", "SL", "SS"}
            or (
                route_system(value) == route_system(current_route_family)
                and
                angle_diff is not None
                and angle_diff <= MID_CORRIDOR_ROUTE_CONTINUATION_MAX_ANGLE_DEG
            )
        )
    )


def candidate_reference_geometry_distance(
    candidate: LimitCandidate,
    endpoint_proj: Point,
    inventory_features: Iterable[RoadwayInventoryFeature],
) -> float:
    distances: list[float] = []
    if candidate.anchor_geometry_proj is not None:
        distances.append(endpoint_proj.distance(candidate.anchor_geometry_proj))

    if is_route_limit(candidate.value) or has_named_road_with_route(candidate.value):
        for feature in inventory_features:
            if feature.kind != "route":
                continue
            if not route_system_matches(feature.value, candidate.value):
                continue
            if not route_overlap(feature.value, candidate.value):
                continue
            distances.append(endpoint_proj.distance(feature.geometry_proj))
    else:
        for feature in inventory_features:
            if feature.kind != "local":
                continue
            if not local_limits_equivalent(feature.value, candidate.value):
                continue
            distances.append(endpoint_proj.distance(feature.geometry_proj))

    return min(distances) if distances else candidate.distance_m


def add_mid_corridor_candidate(
    best_by_key: dict[str, LimitCandidate],
    candidate: LimitCandidate,
) -> None:
    key = candidate.normalized or variant_limit_normalized(candidate.value)
    if not key:
        key = normalize_limit_key(candidate.value)
    existing = best_by_key.get(key)
    best_by_key[key] = select_preferred_candidate(existing, candidate) if existing is not None else candidate


def mid_corridor_reference_sort_key(candidate: LimitCandidate) -> tuple[float, float, int, int, float, str]:
    route_like = is_route_limit(candidate.value) or has_named_road_with_route(candidate.value)
    class_priority = (
        10 + route_system_priority(candidate.value)
        if route_like
        else local_reference_priority(candidate.value)
    )
    angle_diff = candidate.angle_diff or 0.0
    angle_bucket = 2 if angle_diff >= MID_CORRIDOR_STRONG_CROSSING_ANGLE_DEG else (1 if angle_diff >= 70.0 else 0)
    method_priority = {
        "route_intersection": 0,
        "txdot_inventory_route": 1,
        "txdot_inventory_local": 2,
        "basemap_label": 3,
        "fallback_label": 4,
    }.get(candidate.method, 5)
    return (
        float(class_priority),
        float(angle_bucket),
        -candidate.distance_m,
        int(angle_diff * 10),
        -method_priority,
        int(candidate.confidence * 100),
        candidate.value,
    )


def find_nearest_crossing_on_corridor(
    candidates: GatheredCandidates,
    *,
    max_search_distance_m: float = MID_CORRIDOR_SEARCH_DISTANCE_M,
) -> LimitCandidate | None:
    def add_label_candidates(label_features: Iterable[LabelFeature]) -> None:
        for label in label_features:
            if label.geometry_proj.geom_type == "Point":
                continue
            if should_skip_local_label(label.name):
                continue

            value = smart_title(label.name)
            if normalize_limit_key(value) in current_tokens:
                continue

            distance = candidates.endpoint_proj.distance(label.geometry_proj)
            if distance > max_search_distance_m:
                continue
            angle_diff = angle_difference_deg(
                candidates.current_angle,
                local_line_angle_for_point(representative_line(label.geometry_proj), candidates.endpoint_proj),
            )
            if angle_diff < MID_CORRIDOR_MIN_CROSSING_ANGLE_DEG:
                continue
            if references_current_corridor(value, candidates.current_route_family, angle_diff):
                continue

            anchor_geometry = label.geometry_proj
            matching_inventory = [
                feature
                for feature in wider_inventory
                if feature.kind == "local"
                and local_limits_equivalent(feature.value, value)
            ]
            if matching_inventory:
                anchor_geometry = min(
                    matching_inventory,
                    key=lambda feature: candidates.endpoint_proj.distance(feature.geometry_proj),
                ).geometry_proj

            confidence = min(0.94, 0.7 + local_reference_priority(value) * 0.04)
            candidate = LimitCandidate(
                value=value,
                normalized=variant_limit_normalized(value),
                method=f"{label.source}_label",
                confidence=confidence,
                distance_m=distance,
                detail=f"{label.source}:{label.name}, label={distance:.1f}m, angle={angle_diff:.1f}",
                angle_diff=angle_diff,
                anchor_geometry_proj=anchor_geometry,
            )
            add_mid_corridor_candidate(best_by_key, candidate)

    wider_inventory = search_roadway_inventory_within_distance(
        candidates.endpoint_wgs84,
        candidates.endpoint_proj,
        roadway_inventory_lookup=candidates.roadway_inventory_lookup,
        max_search_distance_m=min(max_search_distance_m, MID_CORRIDOR_INVENTORY_SEARCH_DISTANCE_M),
    )

    best_by_key: dict[str, LimitCandidate] = {}
    combined_labels = (*candidates.primary_labels, *candidates.fallback_labels)
    current_tokens = route_tokens(candidates.current_route_family)

    for feature in candidates.all_segments:
        if feature.segment_id in candidates.current_feature_ids:
            continue
        distance = candidates.endpoint_proj.distance(feature.line_proj)
        if distance > max_search_distance_m:
            continue
        angle_diff = angle_difference_deg(
            candidates.current_angle,
            local_line_angle_for_point(feature.line_proj, candidates.endpoint_proj),
        )
        if angle_diff < MID_CORRIDOR_MIN_CROSSING_ANGLE_DEG:
            continue
        if references_current_corridor(feature.route_family, candidates.current_route_family, angle_diff):
            continue

        value = feature.route_family
        route_alias = find_route_alias_label(candidates.endpoint_proj, feature, list(combined_labels))
        if route_alias:
            value = format_named_route(route_alias, feature.route_family)
        candidate = LimitCandidate(
            value=value,
            normalized=normalize_limit_key(value),
            method="route_intersection",
            confidence=min(0.98, 0.82 + route_system_priority(feature.route_family) * 0.02),
            distance_m=distance,
            detail=f"{feature.route_family} ({distance:.1f}m, angle={angle_diff:.1f})",
            angle_diff=angle_diff,
            anchor_geometry_proj=feature.line_proj,
        )
        add_mid_corridor_candidate(best_by_key, candidate)

    for feature in wider_inventory:
        distance = candidates.endpoint_proj.distance(feature.geometry_proj)
        if distance > max_search_distance_m:
            continue

        line = representative_line(feature.geometry_proj)
        angle_diff = angle_difference_deg(
            candidates.current_angle,
            local_line_angle_for_point(line, candidates.endpoint_proj),
        )
        if angle_diff < MID_CORRIDOR_MIN_CROSSING_ANGLE_DEG:
            continue
        if references_current_corridor(feature.value, candidates.current_route_family, angle_diff):
            continue
        if feature.kind == "route":
            confidence = min(0.98, 0.8 + route_system_priority(feature.value) * 0.02)
            candidate = LimitCandidate(
                value=feature.value,
                normalized=feature.normalized,
                method="txdot_inventory_route",
                confidence=confidence,
                distance_m=distance,
                detail=f"{feature.detail} ({distance:.1f}m, angle={angle_diff:.1f})",
                angle_diff=angle_diff,
                anchor_geometry_proj=feature.geometry_proj,
            )
            add_mid_corridor_candidate(best_by_key, candidate)
            continue
        if feature.kind != "local":
            continue
        if feature.normalized in current_tokens:
            continue
        candidate = LimitCandidate(
            value=feature.value,
            normalized=feature.normalized,
            method="txdot_inventory_local",
            confidence=min(0.94, 0.72 + local_reference_priority(feature.value) * 0.03),
            distance_m=distance,
            detail=f"{feature.detail} ({distance:.1f}m, angle={angle_diff:.1f})",
            angle_diff=angle_diff,
            anchor_geometry_proj=feature.geometry_proj,
        )
        add_mid_corridor_candidate(best_by_key, candidate)
    add_label_candidates(combined_labels)

    best_candidate = max(best_by_key.values(), key=mid_corridor_reference_sort_key) if best_by_key else None
    should_widen_labels = best_candidate is None or (
        not (is_route_limit(best_candidate.value) or has_named_road_with_route(best_candidate.value))
        and local_reference_priority(best_candidate.value) < 4
    )
    if should_widen_labels:
        wider_labels = search_labels_within_distance(
            candidates.endpoint_wgs84,
            candidates.endpoint_proj,
            max_search_distance_m=min(max_search_distance_m, MID_CORRIDOR_LABEL_SEARCH_DISTANCE_M),
            source_names=("basemap", "fallback"),
            label_tile_root=candidates.label_tile_root,
        )
        add_label_candidates(wider_labels)

    if not best_by_key:
        return None
    return max(best_by_key.values(), key=mid_corridor_reference_sort_key)


def should_use_offset_phrasing(
    selected: LimitCandidate,
    candidates: GatheredCandidates,
) -> bool:
    # Never offset a strong crossing route that isn't the corridor's own route
    if (
        is_route_limit(selected.value)
        and not references_current_corridor(selected.value, candidates.current_route_family, selected.angle_diff)
        and not is_frontage_or_service_route_variant_name(selected.value)
        and selected.confidence >= 0.85
    ):
        return False
    # Never offset when the route candidate is a strong crossing (even if a
    # different candidate was selected — the route is the real anchor)
    route_c = candidates.route
    if (
        route_c is not None
        and not references_current_corridor(route_c.value, candidates.current_route_family, route_c.angle_diff)
        and not is_frontage_or_service_route_variant_name(route_c.value)
        and route_c.confidence >= 0.85
        and route_c.distance_m <= 15.0
        and route_c.angle_diff is not None
        and route_c.angle_diff >= 40.0
    ):
        return False
    return should_search_for_mid_corridor_reference(selected, candidates) or should_use_selected_mid_corridor_reference(
        selected,
        candidates,
    )


def should_search_for_mid_corridor_reference(
    selected: LimitCandidate,
    candidates: GatheredCandidates,
) -> bool:
    if is_county_limit(selected.value):
        return False

    if is_frontage_or_service_route_variant_name(selected.value) and route_number_base_overlap(selected.value, candidates.current_route_family):
        return bool(
            selected.distance_m >= 20.0
            or (
                selected.angle_diff is not None
                and selected.angle_diff < 30.0
            )
        )

    if references_current_corridor(selected.value, candidates.current_route_family, selected.angle_diff):
        selected_system = route_system(selected.value)
        if selected_system in {"BU", "BS", "BI"}:
            return False
        if selected_system in {"TL", "SL", "SS"}:
            return len(candidates.current_feature_ids) > 1
        return True

    geometry_distance = candidate_reference_geometry_distance(
        selected,
        candidates.endpoint_proj,
        candidates.inventory_features,
    )
    current_system = route_system(candidates.current_route_family)
    if current_system not in MAJOR_ROUTE_PREFIXES:
        return False

    if (
        not is_route_limit(selected.value)
        and selected.angle_diff is not None
        and selected.angle_diff < MID_CORRIDOR_MIN_CROSSING_ANGLE_DEG
    ):
        # Don't search if there's a strong non-corridor route candidate nearby
        # (e.g., IH 20 at an IH 820 endpoint) — the route is the real answer
        route_c = candidates.route
        if (
            route_c is not None
            and not references_current_corridor(route_c.value, candidates.current_route_family, route_c.angle_diff)
            and route_c.confidence >= 0.80
            and route_c.distance_m <= 30.0
        ):
            return False
        return True

    return bool(
        not is_route_limit(selected.value)
        and geometry_distance > 20.0
        and selected.distance_m >= MID_CORRIDOR_ROUTE_SEARCH_TRIGGER_MIN_M
        and (
            (
                current_system in {"IH", "TL"}
                and local_reference_priority(selected.value) <= 2
            )
            or (
                local_reference_priority(selected.value) >= 4
                and
                selected.angle_diff is not None
                and selected.angle_diff < 60.0
            )
        )
    )


def should_use_selected_mid_corridor_reference(
    selected: LimitCandidate,
    candidates: GatheredCandidates,
) -> bool:
    if is_county_limit(selected.value):
        return False

    if (
        is_route_limit(selected.value)
        and route_number_base_overlap(selected.value, candidates.current_route_family)
        and route_system(selected.value) in {"BU", "BS", "BI"}
        and not is_frontage_or_service_route_variant_name(selected.value)
        and selected.angle_diff is not None
        and selected.angle_diff <= MID_CORRIDOR_ROUTE_CONTINUATION_MAX_ANGLE_DEG
        and len(candidates.current_feature_ids) == 1
    ):
        return True

    geometry_distance = candidate_reference_geometry_distance(
        selected,
        candidates.endpoint_proj,
        candidates.inventory_features,
    )
    return bool(
        not should_search_for_mid_corridor_reference(selected, candidates)
        and not is_frontage_or_service_route_variant_name(selected.value)
        and
        geometry_distance > 20.0
        and selected.distance_m >= MID_CORRIDOR_LOCAL_OFFSET_MIN_M
        and selected.distance_m <= 200.0
        and local_reference_priority(selected.value) >= 3
        and selected.angle_diff is not None
        and selected.angle_diff >= MID_CORRIDOR_MIN_CROSSING_ANGLE_DEG
    )


def offset_direction(
    candidate: LimitCandidate,
    endpoint_proj: Point,
    *,
    allow_diagonal: bool = False,
    current_angle: float | None = None,
) -> str:
    if candidate.anchor_geometry_proj is None:
        return ""
    continuation_route = bool(
        is_route_limit(candidate.value)
        and candidate.angle_diff is not None
        and candidate.angle_diff <= MID_CORRIDOR_ROUTE_CONTINUATION_MAX_ANGLE_DEG
    )
    if continuation_route:
        line = representative_line(candidate.anchor_geometry_proj)
        endpoints = [Point(line.coords[0]), Point(line.coords[-1])]
        anchor_point = min(endpoints, key=endpoint_proj.distance)
        endpoint_point = endpoint_proj
    else:
        anchor_point, endpoint_point = nearest_points(candidate.anchor_geometry_proj, endpoint_proj)
    dx = endpoint_point.x - anchor_point.x
    dy = endpoint_point.y - anchor_point.y
    if abs(dx) < 1.0 and abs(dy) < 1.0:
        return ""
    if allow_diagonal:
        abs_dx = abs(dx)
        abs_dy = abs(dy)
        diagonal_threshold = 0.4 if continuation_route else 0.7
        if min(abs_dx, abs_dy) >= diagonal_threshold * max(abs_dx, abs_dy):
            ns = "north" if dy > 0 else "south"
            ew = "east" if dx > 0 else "west"
            return f"{ns}{ew}"
    if current_angle is not None and not continuation_route:
        if 45.0 <= current_angle <= 135.0:
            return "north" if dy > 0 else "south"
        return "east" if dx > 0 else "west"
    if abs(dx) >= abs(dy):
        return "east" if dx > 0 else "west"
    return "north" if dy > 0 else "south"


def cleaned_offset_marker(marker: str, direction: str) -> str:
    marker = normalize_spacing(marker)
    match = re.match(r"^(N|S|E|W)\s+(.+)$", marker)
    if not match:
        return marker
    leading = match.group(1).upper()
    if direction in {"east", "west"} and leading in {"E", "W"}:
        return match.group(2)
    if direction in {"north", "south"} and leading in {"N", "S"}:
        return match.group(2)
    return marker


def format_offset_candidate(
    candidate: LimitCandidate,
    endpoint_proj: Point,
) -> LimitCandidate:
    direction = offset_direction(candidate, endpoint_proj)
    if not direction:
        return candidate
    marker = cleaned_offset_marker(candidate.value, direction)
    if is_county_limit(marker):
        value = f"{direction.title()} of the {marker}"
    else:
        value = f"{direction.title()} of {marker}"
    return replace(
        candidate,
        value=value,
        normalized=normalize_limit_key(value),
    )


def format_mid_corridor_offset(
    reference_road: LimitCandidate,
    endpoint_proj: Point,
    *,
    current_angle: float,
) -> LimitCandidate:
    direction = offset_direction(
        reference_road,
        endpoint_proj,
        allow_diagonal=True,
        current_angle=current_angle,
    )
    if not direction:
        return reference_road
    marker = cleaned_offset_marker(reference_road.value, direction)
    value = f"{direction.title()} of {marker}"
    return replace(
        reference_road,
        value=value,
        normalized=normalize_limit_key(value),
    )


def should_format_county_offset(candidate: LimitCandidate, candidates: GatheredCandidates) -> bool:
    return bool(
        candidate is candidates.county
        and COUNTY_OFFSET_FORMAT_MIN_M <= candidate.distance_m <= COUNTY_OFFSET_FORMAT_MAX_M
    )


def should_format_local_offset(candidate: LimitCandidate, candidates: GatheredCandidates) -> bool:
    return bool(
        candidate is candidates.local
        and candidates.route is None
        and not is_specific_route_variant_name(candidate.value)
        and LOCAL_OFFSET_FORMAT_MIN_M <= candidate.distance_m <= LOCAL_OFFSET_FORMAT_MAX_M
        and candidate.angle_diff is not None
        and 25.0 <= candidate.angle_diff <= 70.0
    )


def should_format_route_offset(candidate: LimitCandidate, candidates: GatheredCandidates) -> bool:
    return bool(
        candidate is candidates.route
        and candidates.local is not None
        and frontage_variant_matches_route(candidates.local, candidate)
        and candidates.local.distance_m > 50.0
        and candidate.distance_m <= 10.0
        and candidate.angle_diff is not None
        and candidate.angle_diff >= 70.0
    )


def finalize_candidate(
    candidate: LimitCandidate | None,
    candidates: GatheredCandidates,
    *labels: str,
) -> LimitCandidate | None:
    if candidate is None:
        return None

    finalized = candidate
    extra_labels = list(labels)
    if should_format_county_offset(finalized, candidates):
        finalized = format_offset_candidate(finalized, candidates.endpoint_proj)
        extra_labels.append("offset_from_marker")
    elif should_use_offset_phrasing(finalized, candidates):
        mid_corridor_reference: LimitCandidate | None = None
        if should_search_for_mid_corridor_reference(finalized, candidates):
            mid_corridor_reference = find_nearest_crossing_on_corridor(candidates)
        if mid_corridor_reference is not None:
            finalized = format_mid_corridor_offset(
                mid_corridor_reference,
                candidates.endpoint_proj,
                current_angle=candidates.current_angle,
            )
            extra_labels.extend(("offset_from_marker", "orientation_direction_effect"))
        elif should_use_selected_mid_corridor_reference(finalized, candidates):
            finalized = format_mid_corridor_offset(
                finalized,
                candidates.endpoint_proj,
                current_angle=candidates.current_angle,
            )
            extra_labels.extend(("offset_from_marker", "orientation_direction_effect"))
    elif should_format_local_offset(finalized, candidates):
        finalized = format_offset_candidate(finalized, candidates.endpoint_proj)
        extra_labels.extend(("offset_from_marker", "orientation_direction_effect"))
    elif should_format_route_offset(finalized, candidates):
        finalized = format_offset_candidate(finalized, candidates.endpoint_proj)
        extra_labels.extend(("offset_from_marker", "orientation_direction_effect"))
    finalized = maybe_combine_local_and_route(finalized, candidates)
    finalized = abbreviate_output_value(finalized)
    return with_heuristics(finalized, *extra_labels)


def maybe_combine_local_and_route(
    selected: LimitCandidate,
    candidates: GatheredCandidates,
) -> LimitCandidate:
    """When the selected candidate is a bare route and a close local candidate
    names the same physical road with a street name, combine as 'Name (Route)'.
    Also handles the reverse: local selected with a close matching route.

    Only combines when there is strong evidence the local name and route refer
    to the same crossing road — confirmed by inventory agreement + crossing angle,
    or by the local name being a known route alias."""
    route_candidate = candidates.route
    local_candidate = candidates.local
    local_inventory = candidates.local_inventory

    if route_candidate is None or local_candidate is None:
        return selected

    # Don't combine if the local candidate IS a route or frontage variant
    if is_route_limit(local_candidate.value) or is_frontage_or_service_route_variant_name(local_candidate.value):
        return selected

    # Don't combine if the local name is the same as the route (no added info)
    if normalize_limit_key(local_candidate.value) == normalize_limit_key(route_candidate.value):
        return selected

    # Only combine when the local candidate is a confirmed cross-street:
    # - very close to endpoint (within 5m)
    # - confirmed by inventory (local_inventory agrees on the name)
    # - crosses the route (high angle difference)
    local_confirmed = (
        local_candidate.distance_m <= 2.0
        and route_candidate.distance_m <= 2.0
        and local_inventory is not None
        and canonical(local_candidate.value) == canonical(local_inventory.value)
        and local_candidate.angle_diff is not None
        and local_candidate.angle_diff >= 50.0
    )

    # OR: the local candidate is a named-road alias for the route (same corridor,
    # like "Chisholm Trail Parkway (Toll)" for TL 38)
    local_is_route_alias = (
        local_candidate.distance_m <= 15.0
        and (
            looks_like_route_alias(local_candidate.value)
            or "(TOLL)" in local_candidate.value.upper()
        )
    )

    if not local_confirmed and not local_is_route_alias:
        return selected

    # Selected is a bare route -> combine with local name
    if selected is route_candidate or (
        is_route_limit(selected.value)
        and normalize_limit_key(selected.value) == normalize_limit_key(route_candidate.value)
    ):
        local_name = smart_title(local_candidate.value)
        # Strip "(Toll)" or similar parenthetical from alias names
        local_name = re.sub(r"\s*\(Toll\)\s*$", "", local_name, flags=re.IGNORECASE)
        combined = format_named_route(local_name, selected.value)
        if combined != selected.value:
            return replace(selected, value=combined)

    # Selected is a local name -> append route if available
    if selected is local_candidate or (
        not is_route_limit(selected.value)
        and canonical(selected.value) == canonical(local_candidate.value)
    ):
        combined = format_named_route(selected.value, route_candidate.value)
        if combined != selected.value:
            return replace(selected, value=combined)

    return selected


_DIRECTION_ABBREVIATIONS = [
    (r"\bNortheast\b", "NE"),
    (r"\bNorthwest\b", "NW"),
    (r"\bSoutheast\b", "SE"),
    (r"\bSouthwest\b", "SW"),
    (r"\bNorth\b", "N"),
    (r"\bSouth\b", "S"),
    (r"\bEast\b", "E"),
    (r"\bWest\b", "W"),
]

# Road names where "North/South/East/West" is part of the proper name, not a direction
_DIRECTION_IN_NAME = {"South Fwy", "North Fwy", "East Fwy", "West Fwy", "Northwest Pkwy"}


def abbreviate_output_value(candidate: LimitCandidate) -> LimitCandidate:
    """Apply display abbreviations to the final output value."""
    value = candidate.value

    # County Road → CR
    if re.search(r"\bCounty Road\b", value, re.IGNORECASE):
        value = re.sub(r"\bCounty Road\b", "CR", value, flags=re.IGNORECASE)

    # Parkway → Pkwy (but not "Northwest Pkwy" which is already abbreviated)
    if re.search(r"\bParkway\b", value):
        value = re.sub(r"\bParkway\b", "Pkwy", value)

    # Strip directions from bare county line values (but not offset format like "85m W of ...")
    if is_county_limit(value) and not re.match(r"^\d+m\s+", value):
        value = re.sub(
            r"^(?:N|S|E|W|NE|NW|SE|SW|North|South|East|West)\s+(?:of\s+(?:the\s+)?)?",
            "",
            value,
            flags=re.IGNORECASE,
        )
        value = re.sub(r"\s+(?:N|S|E|W|north|south|east|west)$", "", value, flags=re.IGNORECASE)

    # Abbreviate directions (North→N, etc.) but protect road names
    protected_spans: list[tuple[int, int]] = []
    for name in _DIRECTION_IN_NAME:
        for m in re.finditer(re.escape(name), value, re.IGNORECASE):
            protected_spans.append((m.start(), m.end()))

    for pattern, abbrev in _DIRECTION_ABBREVIATIONS:
        for m in reversed(list(re.finditer(pattern, value, re.IGNORECASE))):
            if any(start <= m.start() < end for start, end in protected_spans):
                continue
            value = value[: m.start()] + abbrev + value[m.end() :]

    value = normalize_spacing(value)
    if value == candidate.value:
        return candidate
    return replace(candidate, value=value)


def select_limit(candidates: GatheredCandidates) -> LimitCandidate | None:
    """Stage 2: Apply heuristic selection rules to choose the best candidate."""
    if candidates.county is not None:
        if candidates.county.method == "county_boundary":
            # At the boundary — always use county line
            return finalize_candidate(candidates.county, candidates)
        # Offset county — only use if no strong route/local candidate exists
        route_candidate = candidates.route
        local_candidate = candidates.local
        has_strong_route = route_candidate is not None and route_candidate.confidence >= 0.85 and route_candidate.distance_m <= 40.0
        has_strong_local = local_candidate is not None and local_candidate.confidence >= 0.85 and local_candidate.distance_m <= 30.0
        if not has_strong_route and not has_strong_local:
            return finalize_candidate(candidates.county, candidates)
        # Fall through to normal selection — county offset loses to strong road candidates

    route_candidate = candidates.route
    local_candidate = candidates.local
    local_inventory_candidate = candidates.local_inventory
    ftw_route_candidate = candidates.ftw_route
    interchange_candidate = candidates.interchange
    current_route_family = candidates.current_route_family

    frontage_override = should_prefer_frontage_over_mainline(candidates)
    if frontage_override is not None:
        return finalize_candidate(frontage_override, candidates, "frontage_service_road_variant")

    if route_candidate is not None and local_candidate is not None:
        if should_prefer_confirmed_local_anchor(candidates):
            return finalize_candidate(
                local_inventory_candidate,
                candidates,
                "orientation_direction_effect",
            )
        if (
            is_txdot_label_method(local_candidate.method)
            and is_specific_route_variant_name(local_candidate.value)
            and not is_frontage_or_service_route_variant_name(local_candidate.value)
            and local_candidate.confidence >= 0.8
            and local_candidate.distance_m <= 140.0
        ):
            return finalize_candidate(local_candidate, candidates, "route_alias_or_business_label")
        if (
            is_specific_route_variant_name(local_candidate.value)
            and not is_frontage_or_service_route_variant_name(local_candidate.value)
            and candidate_matches_route_family(local_candidate.value, route_candidate.value)
            and local_candidate.confidence >= 0.8
            and local_candidate.distance_m <= 140.0
        ):
            return finalize_candidate(local_candidate, candidates, "route_alias_or_business_label")
        if should_prefer_mainline_route_over_frontage(candidates):
            preferred_route = more_descriptive_route_candidate(route_candidate, ftw_route_candidate)
            extra_labels = ["frontage_service_road_variant"]
            if preferred_route is ftw_route_candidate:
                extra_labels.append("shared_endpoint_with_adjacent_segment")
            return finalize_candidate(preferred_route, candidates, *extra_labels)
        if (
            local_inventory_candidate is not None
            and is_frontage_or_service_route_variant_name(local_candidate.value)
            and not has_named_road_with_route(local_inventory_candidate.value)
            and not (ftw_route_candidate is not None and ftw_route_candidate.distance_m <= 5.0)
            and route_candidate.angle_diff is not None
            and route_candidate.angle_diff <= CONTINUATION_ROUTE_MAX_ANGLE_DEG
            and (
                route_candidate.angle_diff <= FALSE_LIMIT_CONTINUATION_MAX_ANGLE_DEG
                or candidate_matches_route_family(route_candidate.value, current_route_family)
                or candidate_matches_route_family(local_candidate.value, current_route_family)
            )
            and local_inventory_candidate.angle_diff is not None
            and local_inventory_candidate.angle_diff >= 45.0
            and local_inventory_candidate.distance_m <= 120.0
        ):
            return finalize_candidate(
                local_inventory_candidate,
                candidates,
                "frontage_service_road_variant",
                "orientation_direction_effect",
            )
        if (
            is_specific_route_variant_name(local_candidate.value)
            and candidate_matches_route_family(local_candidate.value, route_candidate.value)
            and not (ftw_route_candidate is not None and ftw_route_candidate.distance_m <= 5.0)
            and local_candidate.confidence >= 0.84
            and local_candidate.distance_m <= VISUAL_LOCAL_CLUE_MAX_DISTANCE_M
        ):
            return finalize_candidate(local_candidate, candidates, "route_alias_or_business_label")
        if (
            is_visual_local_clue(local_candidate)
            and is_continuation_like_route(route_candidate)
            and not (ftw_route_candidate is not None and ftw_route_candidate.distance_m <= 5.0)
            and not candidate_matches_route_family(local_candidate.value, current_route_family)
            and local_candidate.distance_m <= max(20.0, route_candidate.distance_m * 2.0)
        ):
            return finalize_candidate(local_candidate, candidates, "orientation_direction_effect")
        if (
            interchange_candidate is not None
            and (
                local_candidate.distance_m > INTERCHANGE_LABEL_MAX_DISTANCE_M
                or candidate_matches_route_family(local_candidate.value, current_route_family)
            )
        ):
            return finalize_candidate(interchange_candidate, candidates)
        if route_candidate.distance_m <= 15.0:
            return finalize_candidate(route_candidate, candidates)
        if route_candidate.distance_m <= 40.0 and local_candidate.distance_m >= route_candidate.distance_m * 3.0:
            return finalize_candidate(route_candidate, candidates)
        if (
            route_candidate.confidence >= local_candidate.confidence
            and route_candidate.distance_m <= local_candidate.distance_m * 0.5
        ):
            return finalize_candidate(route_candidate, candidates)
        if (
            route_candidate.confidence >= 0.94
            and route_candidate.distance_m <= max(80.0, local_candidate.distance_m * 0.5)
        ):
            return finalize_candidate(route_candidate, candidates)
        return finalize_candidate(local_candidate, candidates)

    if should_prefer_inventory_cross_street(candidates):
        return finalize_candidate(
            local_inventory_candidate,
            candidates,
            "orientation_direction_effect",
        )

    if interchange_candidate is not None:
        return finalize_candidate(interchange_candidate, candidates)

    return finalize_candidate(route_candidate or local_candidate, candidates)


def choose_candidate(
    *,
    endpoint_wgs84: Point,
    endpoint_proj: Point,
    interior_proj: Point,
    current_angle: float,
    current_route_family: str,
    current_feature_ids: set[str],
    counties: CountyLookup,
    all_segments: list[SegmentFeature],
    label_tile_root: str | None = None,
    roadway_inventory_lookup: RoadwayInventoryLookup | None = None,
) -> LimitCandidate | None:
    candidates = gather_candidates(
        endpoint_wgs84=endpoint_wgs84,
        endpoint_proj=endpoint_proj,
        interior_proj=interior_proj,
        current_angle=current_angle,
        current_route_family=current_route_family,
        current_feature_ids=current_feature_ids,
        counties=counties,
        all_segments=all_segments,
        label_tile_root=label_tile_root,
        roadway_inventory_lookup=roadway_inventory_lookup,
    )
    return select_limit(candidates)


def evaluate_side(existing_value: str, candidate: LimitCandidate | None) -> str:
    if candidate is None:
        return "needs_review"
    if limits_equivalent(existing_value, candidate):
        return "matched"
    return "needs_review"


def classify_review_side(
    *,
    existing_value: str,
    auto_value: str,
    side_status: str,
    repeated_limit_pair: bool,
) -> str:
    if side_status != "needs_review":
        return ""

    if repeated_limit_pair and (is_route_limit(existing_value) or is_county_limit(existing_value) or has_named_road_with_route(existing_value)):
        return "Human"

    if has_directional_description(existing_value):
        return "Human"

    if local_limits_equivalent(existing_value, auto_value):
        return "Script"

    if is_county_limit(existing_value) and is_county_limit(auto_value):
        return "Script"

    existing_route_like = is_route_limit(existing_value) or has_named_road_with_route(existing_value)
    auto_route_like = is_route_limit(auto_value) or has_named_road_with_route(auto_value)
    auto_county_like = is_county_limit(auto_value)
    existing_local = bool(normalize_spacing(existing_value)) and not is_county_limit(existing_value) and not existing_route_like
    auto_local = bool(normalize_spacing(auto_value)) and not auto_county_like and not auto_route_like

    if existing_route_like and auto_local:
        return "Script"

    if existing_local and (auto_route_like or auto_county_like):
        if has_service_or_frontage_reference(existing_value):
            return "Human"
        return "Ambiguity"

    if existing_route_like and auto_route_like:
        if route_overlap(existing_value, auto_value):
            return "Script"
        return "Ambiguity"

    if existing_local and auto_local:
        return "Ambiguity"

    return "Ambiguity"


def classify_review_row(side_labels: list[str]) -> str:
    labels = [label for label in side_labels if label]
    if not labels:
        return ""
    unique = set(labels)
    if len(unique) == 1:
        return labels[0]
    return "Ambiguity"


def describe_candidate(side: str, candidate: LimitCandidate | None) -> str:
    if candidate is None:
        return f"{side}=unresolved"
    return (
        f"{side}={candidate.value} "
        f"[{candidate.method}, {candidate.confidence:.2f}, heuristic={candidate.heuristic or 'fallback_or_unclear'}, {candidate.detail}]"
    )


def get_existing_from_value(row: pd.Series | dict[str, object]) -> str:
    return safe_text(
        row.get(
            "Limts-From",
            row.get(
                "Limts From",
                row.get("Limits From", ""),
            ),
        )
    )


def get_existing_to_value(row: pd.Series | dict[str, object]) -> str:
    return safe_text(
        row.get(
            "Limits-To",
            row.get(
                "Limits To",
                row.get("Limts To", ""),
            ),
        )
    )


def process_request_row(
    index: int,
    row: dict[str, object],
    *,
    context: RowProcessingContext,
) -> RowProcessingResult:
    row_started = time.perf_counter()
    segment_name = safe_text(row.get("Segment", "")).strip()

    features = resolve_row_features(segment_name, context.by_readable, context.by_family)
    if not features:
        elapsed_s = time.perf_counter() - row_started
        return RowProcessingResult(
            index=index,
            segment_name=segment_name,
            auto_from="",
            auto_to="",
            heuristic_from="",
            heuristic_to="",
            segment_direction=safe_text(row.get("Segment-Direction", "")),
            segment_type="",
            side_status_from="needs_review",
            side_status_to="needs_review",
            status="needs_review" if context.compare_mode else "",
            note="No ArcGIS features resolved for this requested segment name.",
            processing_time_s=round(elapsed_s, 3),
        )

    oriented = orient_feature_sequence(features)
    feature_ids = {feature.segment_id for feature, _, _ in oriented}
    current_route_family = readable_to_route_family(
        ROUTE_FAMILY_OVERRIDES.get(segment_name, segment_name)
    )

    # Detect gap pieces — merge connected parts, split on real gaps
    all_geometry_proj = features[0].geometry_proj if len(features) == 1 else oriented[0][0].geometry_proj
    if len(features) > 1:
        # Multiple ArcGIS features — use the oriented sequence directly
        gap_pieces_proj: list[LineString] = []
        for _, _, lp in oriented:
            gap_pieces_proj.append(lp)
        # Merge connected pieces
        gap_pieces_proj = _merge_connected_parts(gap_pieces_proj)
    else:
        gap_pieces_proj = split_gap_pieces(all_geometry_proj)

    is_gap_segment = len(gap_pieces_proj) > 1

    if is_gap_segment:
        # Orient each piece: N-to-S or W-to-E
        oriented_pieces: list[LineString] = []
        for piece in gap_pieces_proj:
            start_pt = Point(piece.coords[0])
            end_pt = Point(piece.coords[-1])
            if cardinal_start_should_be_reversed(start_pt, end_pt):
                piece = reverse_line(piece)
            oriented_pieces.append(piece)
        # Sort pieces by cardinal direction (north-to-south or west-to-east)
        # Use the overall direction to decide sort axis
        overall_start = Point(oriented_pieces[0].coords[0])
        overall_end = Point(oriented_pieces[-1].coords[-1])
        dx = overall_end.x - overall_start.x
        dy = overall_end.y - overall_start.y
        if abs(dx) > abs(dy) * 1.25:
            # W-to-E: sort by x (west first)
            oriented_pieces.sort(key=lambda p: p.centroid.x)
        else:
            # N-to-S: sort by y (north first = highest y)
            oriented_pieces.sort(key=lambda p: -p.centroid.y)

        # Process each piece independently
        piece_froms: list[str] = []
        piece_tos: list[str] = []
        piece_heuristic_froms: list[str] = []
        piece_heuristic_tos: list[str] = []
        piece_confidence_froms: list[float] = []
        piece_confidence_tos: list[float] = []
        piece_notes: list[str] = []
        gap_piece_endpoints: list[dict[str, object]] = []
        for piece_idx, piece_proj in enumerate(oriented_pieces):
            piece_wgs84 = transform(PROJ_3081_TO_WGS84.transform, piece_proj)

            p_start_wgs84 = Point(piece_wgs84.coords[0])
            p_start_proj = Point(piece_proj.coords[0])
            p_start_interior = point_along_line(piece_proj, at_start=True, distance_m=INTERIOR_SAMPLE_M)
            p_start_angle = line_angle_deg(piece_proj, at_start=True)

            p_end_wgs84 = Point(piece_wgs84.coords[-1])
            p_end_proj = Point(piece_proj.coords[-1])
            p_end_interior = point_along_line(piece_proj, at_start=False, distance_m=INTERIOR_SAMPLE_M)
            p_end_angle = line_angle_deg(piece_proj, at_start=False)

            p_from = choose_candidate(
                endpoint_wgs84=p_start_wgs84,
                endpoint_proj=p_start_proj,
                interior_proj=p_start_interior,
                current_angle=p_start_angle,
                current_route_family=current_route_family,
                current_feature_ids=feature_ids,
                counties=context.counties,
                all_segments=context.all_segments,
                label_tile_root=context.label_tile_root,
                roadway_inventory_lookup=context.roadway_inventory_lookup,
            )
            p_to = choose_candidate(
                endpoint_wgs84=p_end_wgs84,
                endpoint_proj=p_end_proj,
                interior_proj=p_end_interior,
                current_angle=p_end_angle,
                current_route_family=current_route_family,
                current_feature_ids=feature_ids,
                counties=context.counties,
                all_segments=context.all_segments,
                label_tile_root=context.label_tile_root,
                roadway_inventory_lookup=context.roadway_inventory_lookup,
            )
            piece_froms.append(p_from.value if p_from else "")
            piece_tos.append(p_to.value if p_to else "")
            piece_heuristic_froms.append(p_from.heuristic if p_from else "")
            piece_heuristic_tos.append(p_to.heuristic if p_to else "")
            piece_confidence_froms.append(p_from.confidence if p_from else 0.0)
            piece_confidence_tos.append(p_to.confidence if p_to else 0.0)
            gap_piece_endpoints.append(
                {
                    "piece": piece_idx + 1,
                    "from_wgs84": point_to_lon_lat(p_start_wgs84),
                    "to_wgs84": point_to_lon_lat(p_end_wgs84),
                    "from_limit": p_from.value if p_from else "",
                    "to_limit": p_to.value if p_to else "",
                    "from_confidence": p_from.confidence if p_from else 0.0,
                    "to_confidence": p_to.confidence if p_to else 0.0,
                    "from_heuristic": p_from.heuristic if p_from else "",
                    "to_heuristic": p_to.heuristic if p_to else "",
                }
            )
            piece_notes.append(
                f"piece{piece_idx + 1}: {describe_candidate('from', p_from)}; {describe_candidate('to', p_to)}"
            )

        # Output: From = first piece's From, To = last piece's To
        # Plus semicolon-joined piece detail in Limits-Amy column format
        auto_from = piece_froms[0] if piece_froms else ""
        auto_to = piece_tos[-1] if piece_tos else ""
        heuristic_from = piece_heuristic_froms[0] if piece_heuristic_froms else ""
        heuristic_to = piece_heuristic_tos[-1] if piece_heuristic_tos else ""
        segment_direction = segment_direction_label(
            Point(oriented_pieces[0].coords[0]),
            Point(oriented_pieces[-1].coords[-1]),
        )

        # Build piece-by-piece limits detail
        piece_limits = [
            f"{f} to {t}" for f, t in zip(piece_froms, piece_tos)
        ]
        gap_detail = f"gap_pieces={'; '.join(piece_limits)}"

        elapsed_s = time.perf_counter() - row_started
        return RowProcessingResult(
            index=index,
            segment_name=segment_name,
            auto_from=auto_from,
            auto_to=auto_to,
            heuristic_from=heuristic_from,
            heuristic_to=heuristic_to,
            segment_direction=segment_direction,
            segment_type="Gap",
            side_status_from="",
            side_status_to="",
            status="",
            note="; ".join(piece_notes + [gap_detail, f"resolved_features={', '.join(feature.segment_id for feature in features)}"]),
            processing_time_s=round(elapsed_s, 3),
            from_endpoint_wgs84=(
                gap_piece_endpoints[0]["from_wgs84"] if gap_piece_endpoints else None
            ),
            to_endpoint_wgs84=(
                gap_piece_endpoints[-1]["to_wgs84"] if gap_piece_endpoints else None
            ),
            confidence_from=piece_confidence_froms[0] if piece_confidence_froms else 0.0,
            confidence_to=piece_confidence_tos[-1] if piece_confidence_tos else 0.0,
            gap_piece_endpoints=gap_piece_endpoints,
        )

    # --- Continuous segment (no gap) ---
    _, first_line_wgs84, first_line_proj = oriented[0]
    _, last_line_wgs84, last_line_proj = oriented[-1]

    start_endpoint_wgs84 = Point(first_line_wgs84.coords[0])
    start_endpoint_proj = Point(first_line_proj.coords[0])
    start_interior_proj = point_along_line(first_line_proj, at_start=True, distance_m=INTERIOR_SAMPLE_M)
    start_angle = line_angle_deg(first_line_proj, at_start=True)

    end_endpoint_wgs84 = Point(last_line_wgs84.coords[-1])
    end_endpoint_proj = Point(last_line_proj.coords[-1])
    end_interior_proj = point_along_line(last_line_proj, at_start=False, distance_m=INTERIOR_SAMPLE_M)
    end_angle = line_angle_deg(last_line_proj, at_start=False)
    segment_direction = segment_direction_label(start_endpoint_proj, end_endpoint_proj)

    existing_from = get_existing_from_value(row) if context.compare_mode else ""
    existing_to = get_existing_to_value(row) if context.compare_mode else ""

    from_candidate = choose_candidate(
        endpoint_wgs84=start_endpoint_wgs84,
        endpoint_proj=start_endpoint_proj,
        interior_proj=start_interior_proj,
        current_angle=start_angle,
        current_route_family=current_route_family,
        current_feature_ids=feature_ids,
        counties=context.counties,
        all_segments=context.all_segments,
        label_tile_root=context.label_tile_root,
        roadway_inventory_lookup=context.roadway_inventory_lookup,
    )
    to_candidate = choose_candidate(
        endpoint_wgs84=end_endpoint_wgs84,
        endpoint_proj=end_endpoint_proj,
        interior_proj=end_interior_proj,
        current_angle=end_angle,
        current_route_family=current_route_family,
        current_feature_ids=feature_ids,
        counties=context.counties,
        all_segments=context.all_segments,
        label_tile_root=context.label_tile_root,
        roadway_inventory_lookup=context.roadway_inventory_lookup,
    )

    auto_from = from_candidate.value if from_candidate is not None else ""
    auto_to = to_candidate.value if to_candidate is not None else ""
    heuristic_from = from_candidate.heuristic if from_candidate is not None else ""
    heuristic_to = to_candidate.heuristic if to_candidate is not None else ""

    if context.compare_mode:
        side_status_from = evaluate_side(existing_from, from_candidate)
        side_status_to = evaluate_side(existing_to, to_candidate)
        status = "needs_review" if "needs_review" in (side_status_from, side_status_to) else "matched"
    else:
        side_status_from = ""
        side_status_to = ""
        status = ""

    elapsed_s = time.perf_counter() - row_started
    return RowProcessingResult(
        index=index,
        segment_name=segment_name,
        auto_from=auto_from,
        auto_to=auto_to,
        heuristic_from=heuristic_from,
        heuristic_to=heuristic_to,
        segment_direction=segment_direction,
        segment_type="Continuous",
        side_status_from=side_status_from,
        side_status_to=side_status_to,
        status=status,
        note="; ".join(
            [
                describe_candidate("from", from_candidate),
                describe_candidate("to", to_candidate),
                f"resolved_features={', '.join(feature.segment_id for feature in features)}",
            ]
        ),
        processing_time_s=round(elapsed_s, 3),
        from_endpoint_wgs84=point_to_lon_lat(start_endpoint_wgs84),
        to_endpoint_wgs84=point_to_lon_lat(end_endpoint_wgs84),
        confidence_from=from_candidate.confidence if from_candidate is not None else 0.0,
        confidence_to=to_candidate.confidence if to_candidate is not None else 0.0,
    )


def build_request_dataframe(
    *,
    compare_csv_path: pathlib.Path | None,
    segment_names: list[str],
    segment_features: list[SegmentFeature],
    limit: int | None,
) -> pd.DataFrame:
    if compare_csv_path is not None:
        dataframe = pd.read_csv(compare_csv_path).copy()
        if segment_names:
            requested = {normalize_spacing(name) for name in segment_names}
            dataframe = dataframe[
                dataframe["Segment"].map(lambda value: normalize_spacing(value) in requested)
            ].copy()
    elif segment_names:
        dataframe = pd.DataFrame({"Segment": segment_names})
    else:
        readable_segments = sorted(
            {feature.readable_segid for feature in segment_features},
            key=segment_name_sort_key,
        )
        dataframe = pd.DataFrame({"Segment": readable_segments})

    if limit is not None:
        return dataframe.head(limit).copy()
    return dataframe.copy()


def needs_review_output_path(output_path: pathlib.Path) -> pathlib.Path:
    return output_path.with_name(f"{output_path.stem}.needs-review.csv")


def verify_limits(
    *,
    output_path: pathlib.Path,
    compare_csv_path: pathlib.Path | None = None,
    segment_names: list[str] | None = None,
    limit: int | None = None,
    workers: int = DEFAULT_MAX_WORKERS,
    label_tile_root: pathlib.Path = DEFAULT_LABEL_TILE_ROOT,
    download_label_tiles_first: bool = False,
    use_live_label_tiles: bool = False,
    roadway_inventory_path: pathlib.Path = DEFAULT_ROADWAY_INVENTORY_PATH,
    download_roadway_inventory_subset_first: bool = False,
    use_live_roadway_inventory: bool = False,
) -> None:
    segment_names = [normalize_spacing(name) for name in (segment_names or []) if normalize_spacing(name)]
    workers = max(1, workers)

    print("Fetching segment geometries...")
    segment_features = load_segment_features()
    by_readable, by_family = build_lookups(segment_features)

    dataframe = build_request_dataframe(
        compare_csv_path=compare_csv_path,
        segment_names=segment_names,
        segment_features=segment_features,
        limit=limit,
    )
    compare_mode = compare_csv_path is not None

    label_tile_root_str: str | None = None
    if download_label_tiles_first:
        print(f"Downloading label tiles to {label_tile_root}...")
        label_counts = download_label_tiles(
            output_root=label_tile_root,
            segment_features=segment_features,
        )
        print(
            "Label tile cache ready: "
            f"{label_counts['total']} jobs, "
            f"{label_counts['downloaded']} downloaded, "
            f"{label_counts['cached']} already cached, "
            f"{label_counts['missing']} missing"
        )
    if not use_live_label_tiles:
        label_tile_root_str = str(label_tile_root.resolve())
        if label_tile_root.exists():
            print(f"Using local label tile cache at {label_tile_root}...")

    roadway_inventory_lookup: RoadwayInventoryLookup | None = None
    if download_roadway_inventory_subset_first:
        print(f"Downloading roadway inventory subset to {roadway_inventory_path}...")
        subset_count = download_roadway_inventory_subset(
            output_path=roadway_inventory_path,
            segment_features=segment_features,
        )
        print(f"Wrote roadway inventory subset: {roadway_inventory_path} ({subset_count} features)")
    if not use_live_roadway_inventory and roadway_inventory_path.exists():
        print(f"Loading roadway inventory subset from {roadway_inventory_path}...")
        roadway_inventory_lookup = load_local_roadway_inventory_lookup(str(roadway_inventory_path.resolve()))
        print(f"Loaded {len(roadway_inventory_lookup.features)} roadway inventory features.")

    print("Fetching county boundaries...")
    counties = build_county_lookup(load_counties())

    context = RowProcessingContext(
        by_readable=by_readable,
        by_family=by_family,
        compare_mode=compare_mode,
        counties=counties,
        all_segments=segment_features,
        label_tile_root=label_tile_root_str,
        roadway_inventory_lookup=roadway_inventory_lookup,
    )
    records = dataframe.to_dict("records")
    results: list[RowProcessingResult | None] = [None] * len(records)
    slow_segments: list[tuple[str, float]] = []

    total_rows = len(records)
    if workers > 1 and total_rows > 1:
        print(f"Processing {total_rows} rows with {workers} worker threads...")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_index = {
                executor.submit(process_request_row, index, row, context=context): index
                for index, row in enumerate(records)
            }
            for future in as_completed(future_to_index):
                result = future.result()
                results[result.index] = result
                print(f"[{result.index + 1}/{total_rows}] {result.segment_name}")
                if result.processing_time_s >= SLOW_SEGMENT_THRESHOLD_S:
                    slow_segments.append((result.segment_name, result.processing_time_s))
                    print(
                        f"  slow segment: {result.segment_name} took "
                        f"{result.processing_time_s:.1f}s"
                    )
    else:
        for index, row in enumerate(records):
            result = process_request_row(index, row, context=context)
            results[index] = result
            print(f"[{index + 1}/{total_rows}] {result.segment_name}")
            if result.processing_time_s >= SLOW_SEGMENT_THRESHOLD_S:
                slow_segments.append((result.segment_name, result.processing_time_s))
                print(
                    f"  slow segment: {result.segment_name} took "
                    f"{result.processing_time_s:.1f}s"
                )

    ordered_results = [result for result in results if result is not None]

    dataframe["Auto Limits From"] = [result.auto_from for result in ordered_results]
    dataframe["Auto Limits To"] = [result.auto_to for result in ordered_results]
    dataframe["Heuristic-From"] = [result.heuristic_from for result in ordered_results]
    dataframe["Heuristic-To"] = [result.heuristic_to for result in ordered_results]
    dataframe["From Endpoint Lon"] = [
        result.from_endpoint_wgs84[0] if result.from_endpoint_wgs84 is not None else None
        for result in ordered_results
    ]
    dataframe["From Endpoint Lat"] = [
        result.from_endpoint_wgs84[1] if result.from_endpoint_wgs84 is not None else None
        for result in ordered_results
    ]
    dataframe["To Endpoint Lon"] = [
        result.to_endpoint_wgs84[0] if result.to_endpoint_wgs84 is not None else None
        for result in ordered_results
    ]
    dataframe["To Endpoint Lat"] = [
        result.to_endpoint_wgs84[1] if result.to_endpoint_wgs84 is not None else None
        for result in ordered_results
    ]
    dataframe["Confidence-From"] = [result.confidence_from for result in ordered_results]
    dataframe["Confidence-To"] = [result.confidence_to for result in ordered_results]
    dataframe["Confidence-Bucket-From"] = [
        confidence_bucket(result.confidence_from) for result in ordered_results
    ]
    dataframe["Confidence-Bucket-To"] = [
        confidence_bucket(result.confidence_to) for result in ordered_results
    ]
    dataframe["Gap Piece Endpoints"] = [
        json.dumps(result.gap_piece_endpoints, ensure_ascii=True)
        if result.gap_piece_endpoints
        else ""
        for result in ordered_results
    ]
    dataframe["Segment-Direction"] = [result.segment_direction for result in ordered_results]
    dataframe["Segment-Type"] = [result.segment_type for result in ordered_results]
    if compare_mode:
        dataframe["Auto Review Status From"] = [result.side_status_from for result in ordered_results]
        dataframe["Auto Review Status To"] = [result.side_status_to for result in ordered_results]
        dataframe["Auto Review Status"] = [result.status for result in ordered_results]
    dataframe["Auto Review Notes"] = [result.note for result in ordered_results]
    dataframe["Processing Time Seconds"] = [result.processing_time_s for result in ordered_results]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    dataframe.to_csv(output_path, index=False)
    print(f"\nWrote file: {output_path}")
    if compare_mode:
        review_path = needs_review_output_path(output_path)
        dataframe[dataframe["Auto Review Status"] == "needs_review"].to_csv(review_path, index=False)
        print(f"Wrote file: {review_path}")
        print(dataframe["Auto Review Status"].value_counts(dropna=False).to_string())
    if slow_segments:
        print("\nSlow segments:")
        for segment_name, elapsed_s in sorted(slow_segments, key=lambda item: item[1], reverse=True):
            print(f"  {segment_name}: {elapsed_s:.1f}s")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Identify FTW segment limits for new or unreviewed segments from ArcGIS data."
    )
    parser.add_argument(
        "--compare-csv",
        dest="compare_csv_path",
        type=pathlib.Path,
        default=None,
        help=(
            "Optional CSV of manual limits to compare against. "
            f"Its values are not used to infer the auto limits. Example: {DEFAULT_COMPARE_CSV}"
        ),
    )
    parser.add_argument(
        "--csv-path",
        dest="compare_csv_path",
        type=pathlib.Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--output-path",
        type=pathlib.Path,
        default=DEFAULT_OUTPUT,
        help=f"Output CSV path. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--segment-name",
        action="append",
        default=[],
        help=(
            "Optional segment name to inspect. Repeat this flag for multiple rows. "
            "If omitted, the script uses the comparison CSV rows when provided, "
            "otherwise all ArcGIS readable segments."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional row limit for smoke testing.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help=(
            "Worker thread count for row processing. "
            f"Default: {DEFAULT_MAX_WORKERS}. Use 1 to keep serial behavior."
        ),
    )
    parser.add_argument(
        "--label-tile-root",
        type=pathlib.Path,
        default=DEFAULT_LABEL_TILE_ROOT,
        help=(
            "Local folder for TxDOT label tile cache files. "
            f"Default: {DEFAULT_LABEL_TILE_ROOT}"
        ),
    )
    parser.add_argument(
        "--download-label-tiles",
        action="store_true",
        help=(
            "Download the TxDOT label tiles needed around all FTW segment endpoints "
            "before processing."
        ),
    )
    parser.add_argument(
        "--live-label-tiles",
        action="store_true",
        help="Ignore any local label tile cache and fetch labels live.",
    )
    parser.add_argument(
        "--roadway-inventory-path",
        type=pathlib.Path,
        default=DEFAULT_ROADWAY_INVENTORY_PATH,
        help=(
            "Optional local roadway inventory GeoJSON subset. "
            f"Default: {DEFAULT_ROADWAY_INVENTORY_PATH}"
        ),
    )
    parser.add_argument(
        "--download-roadway-inventory-subset",
        action="store_true",
        help=(
            "Download or refresh the project roadway inventory subset before processing. "
            "The subset is limited to counties touched by the FTW segmentation data."
        ),
    )
    parser.add_argument(
        "--live-roadway-inventory",
        action="store_true",
        help="Ignore any local roadway inventory subset and query the statewide service live.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    verify_limits(
        output_path=args.output_path,
        compare_csv_path=args.compare_csv_path,
        segment_names=args.segment_name,
        limit=args.limit,
        workers=args.workers,
        label_tile_root=args.label_tile_root,
        download_label_tiles_first=args.download_label_tiles,
        use_live_label_tiles=args.live_label_tiles,
        roadway_inventory_path=args.roadway_inventory_path,
        download_roadway_inventory_subset_first=args.download_roadway_inventory_subset,
        use_live_roadway_inventory=args.live_roadway_inventory,
    )


if __name__ == "__main__":
    main()
