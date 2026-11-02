#!/usr/bin/env python3
"""Probe GEFSv12 reforecast precipitation (2000-2019) over the Canyon Creek basin.

Downloads only the six-hour APCP messages needed for a 72-hour window by HTTP byte range,
decodes them with grib2io, cross-checks every value against ecCodes, and reports
fractional-area basin means. Requires the optional grib2io setup (scripts/setup_grib2io.sh).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

# Initialize pyproj before loading the ecCodes wheel's native dependencies.
from pyproj import CRS, Transformer

import geopandas as gpd
import numpy as np
import eccodes as ec
import requests
from shapely.geometry import box
from shapely.ops import transform

from probe_basin_qpf import weighted_mean

BASE = "https://noaa-gefs-retrospective.s3.amazonaws.com/GEFSv12/reforecast"
IDX_LINE = re.compile(r"^(\d+):(\d+):d=(\d{10}):APCP:surface:(\d+)-(\d+) hour acc fcst:")
# GEFSv12 GRIB2 files declare a spherical earth of radius 6,371,229 m.
GEFS_EARTH = CRS.from_proj4("+proj=longlat +R=6371229 +no_defs")


def object_url(cycle, member):
    return f"{BASE}/{cycle[:4]}/{cycle}/{member}/Days:1-10/apcp_sfc_{cycle}_{member}.grib2"


def six_hour_ranges(idx_text, first_hour, last_hour):
    """Byte ranges of disjoint six-hour buckets covering (first_hour, last_hour]."""
    entries = []
    for line in idx_text.strip().splitlines():
        match = IDX_LINE.match(line)
        if not match:
            raise ValueError(f"Unexpected index line: {line}")
        entries.append((int(match[2]), int(match[4]), int(match[5])))
    selected = []
    for k, (offset, start, end) in enumerate(entries):
        if end - start == 6 and start % 6 == 0 and first_hour <= start and end <= last_hour:
            stop = entries[k + 1][0] - 1 if k + 1 < len(entries) else None
            selected.append((start, end, offset, stop))
    expected = list(range(first_hour, last_hour, 6))
    if [s[0] for s in selected] != expected:
        raise ValueError(f"Missing six-hour buckets: have {[s[0] for s in selected]}, need {expected}")
    return selected


def fetch(cycle, member, first_hour, last_hour, cache_dir):
    url = object_url(cycle, member)
    target = cache_dir / f"apcp_{cycle}_{member}_f{first_hour:03d}-f{last_hour:03d}.grib2"
    idx = requests.get(url + ".idx", timeout=60)
    idx.raise_for_status()
    ranges = six_hour_ranges(idx.text, first_hour, last_hour)
    if not target.exists():
        parts = []
        for start, end, offset, stop in ranges:
            header = {"Range": f"bytes={offset}-{'' if stop is None else stop}"}
            response = requests.get(url, headers=header, timeout=120)
            if response.status_code != 206 or not response.content.startswith(b"GRIB"):
                raise ValueError(f"Bad range response for {start}-{end} h: {response.status_code}")
            parts.append(response.content)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".part")
        tmp.write_bytes(b"".join(parts))
        tmp.replace(target)
    return target, url, [(s, e) for s, e, _, _ in ranges]


def latlon_weights(lats, lons, boundary):
    """Fractional basin area of each regular lat/lon cell, measured in EPSG:5070."""
    to_area = Transformer.from_crs(GEFS_EARTH, "EPSG:5070", always_xy=True)
    basin = boundary.to_crs("EPSG:5070").geometry.union_all()
    west, south, east, north = boundary.to_crs("EPSG:4326").total_bounds
    lon360 = np.mod(lons, 360)
    dlat = abs(float(lats[1, 0] - lats[0, 0]))
    dlon = abs(float(lon360[0, 1] - lon360[0, 0]))
    rows = np.where((lats[:, 0] >= south - dlat) & (lats[:, 0] <= north + dlat))[0]
    cols = np.where((lon360[0] >= np.mod(west, 360) - dlon) & (lon360[0] <= np.mod(east, 360) + dlon))[0]
    indices, areas = [], []
    for j in rows:
        for i in cols:
            lat, lon = float(lats[j, i]), float(lon360[j, i]) - 360
            cell = box(lon - dlon / 2, lat - dlat / 2, lon + dlon / 2, lat + dlat / 2).segmentize(.005)
            overlap = transform(to_area.transform, cell).intersection(basin).area
            if overlap > .01:
                indices.append((j, i))
                areas.append(overlap)
    areas = np.asarray(areas)
    if not np.isclose(areas.sum(), basin.area, rtol=1e-4):
        raise ValueError("Grid-cell intersections do not cover the full basin")
    return np.asarray(indices), areas, float(basin.area)


def extract(path, boundary_path):
    import grib2io
    boundary = gpd.read_file(boundary_path)
    rows, worst = [], 0.0
    with grib2io.open(path) as archive, path.open("rb") as independent:
        first = archive[0]
        if first.gridDefinitionTemplateNumber != 0:
            raise ValueError("Expected a regular latitude/longitude grid")
        lats, lons = first.grid()
        indices, areas, basin_area = latlon_weights(lats, lons, boundary)
        signature = np.asarray(first.gridDefinitionSection).tobytes()
        for message in archive:
            if message.shortName != "APCP" or message.units != "kg m-2":
                raise ValueError("Unexpected precipitation parameter or units")
            if np.asarray(message.gridDefinitionSection).tobytes() != signature:
                raise ValueError("Grid changed inside file")
            values = np.asarray(message.data, dtype=float)
            handle = ec.codes_grib_new_from_file(independent)
            try:
                other = np.asarray(ec.codes_get_values(handle)).reshape(values.shape)
                other[other == ec.codes_get(handle, "missingValue")] = np.nan
                start = ec.codes_get(handle, "startStep")
                end = ec.codes_get(handle, "endStep")
            finally:
                ec.codes_release(handle)
            valid = np.isfinite(values)
            if not np.array_equal(valid, np.isfinite(other)):
                raise ValueError("Decoders disagree about missing cells")
            difference = float(np.max(np.abs(values[valid] - other[valid])))
            if difference > 1e-4:
                raise ValueError(f"Independent decoders disagree by {difference} mm")
            worst = max(worst, difference)
            mean, fraction = weighted_mean(values[indices[:, 0], indices[:, 1]], areas, basin_area)
            rows.append({"start_hour": int(start), "end_hour": int(end), "basin_mean_mm": mean,
                         "valid_area_fraction": fraction, "max_decoder_difference_mm": difference})
    cells = [{"lat": float(lats[j, i]), "lon": float(np.mod(lons[j, i], 360) - 360),
              "basin_area_fraction": float(a / basin_area)} for (j, i), a in zip(indices, areas)]
    return rows, cells, worst


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cycles", nargs="+", required=True, help="YYYYMMDD00 reforecast cycles")
    p.add_argument("--members", nargs="+", default=["c00"])
    p.add_argument("--first-hour", type=int, default=6, help="Decision lead, e.g. 6 for 06Z from a 00Z run")
    p.add_argument("--last-hour", type=int, default=78)
    p.add_argument("--boundary", type=Path, default=Path("Basins/Canyon_Creek_WA/basin.geojson"))
    p.add_argument("--cache-dir", type=Path, default=Path(".cache/gefs_reforecast"))
    p.add_argument("--output", type=Path, default=Path(".cache/gefs_reforecast/probe_report.json"))
    args = p.parse_args()
    report = []
    for cycle in args.cycles:
        for member in args.members:
            path, url, buckets = fetch(cycle, member, args.first_hour, args.last_hour, args.cache_dir)
            rows, cells, worst = extract(path, args.boundary)
            if [(r["start_hour"], r["end_hour"]) for r in rows] != buckets:
                raise ValueError("Decoded accumulation steps differ from the index")
            total = sum(r["basin_mean_mm"] for r in rows)
            report.append({"cycle": cycle, "member": member, "source_url": url, "bytes": path.stat().st_size,
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "cells": cells,
                           "max_decoder_difference_mm": worst, "total_mm": total, "intervals": rows})
            print(f"{cycle} {member}: {len(rows)} six-hour buckets f{args.first_hour:03d}-f{args.last_hour:03d}, "
                  f"{path.stat().st_size / 1e6:.1f} MB, {len(cells)} cells, basin total {total:.1f} mm, "
                  f"decoder max diff {worst:.2e} mm")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=1))
    print(f"Report: {args.output}")


if __name__ == "__main__":
    main()
