#!/usr/bin/env python3
"""Probe GEFSv12 precipitation over the Canyon Creek basin.

Reforecast cycles (2000-2019) come from noaa-gefs-retrospective, where one object holds every lead;
operational GEFSv12 cycles (from 2020-09-23) come from noaa-gefs-pds, one object per lead. Either way
only the six-hour APCP messages for the requested window are fetched by HTTP byte range, decoded with
grib2io, cross-checked value-by-value against ecCodes, and reduced to fractional-area basin means.
Requires the optional grib2io setup (scripts/setup_grib2io.sh).
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import re
import time
from pathlib import Path

# Initialize pyproj before loading the ecCodes wheel's native dependencies.
from pyproj import CRS, Transformer

import geopandas as gpd
import numpy as np
import eccodes as ec
import pandas as pd
import requests
from shapely.geometry import box
from shapely.ops import transform

from probe_basin_qpf import weighted_mean

REFORECAST = "https://noaa-gefs-retrospective.s3.amazonaws.com/GEFSv12/reforecast"
OPERATIONAL = "https://noaa-gefs-pds.s3.amazonaws.com"
REFORECAST_LAST = pd.Timestamp("2019-12-31T00:00Z")
OPERATIONAL_FIRST = pd.Timestamp("2020-09-23T00:00Z")
IDX_LINE = re.compile(r"^(\d+):(\d+):d=(\d{10}):([^:]+):([^:]+):([^:]*):")
APCP_RANGE = re.compile(r"^(\d+)-(\d+) hour acc fcst$")
# GEFSv12 GRIB2 files declare a spherical earth of radius 6,371,229 m.
GEFS_EARTH = CRS.from_proj4("+proj=longlat +R=6371229 +no_defs")


class CycleUnavailable(ValueError):
    """The requested GEFSv12 cycle/lead does not exist in either archive."""


def cycle_time(cycle):
    return pd.Timestamp(f"{cycle[:4]}-{cycle[4:6]}-{cycle[6:8]}T{cycle[8:10]}:00Z")


def source_kind(cycle):
    """'reforecast', 'operational', or None for a YYYYMMDDHH cycle string."""
    reference = cycle_time(cycle)
    if reference.hour != 0:
        return None
    if reference <= REFORECAST_LAST:
        return "reforecast"
    if reference >= OPERATIONAL_FIRST:
        return "operational"
    return None


def reforecast_url(cycle, member):
    return f"{REFORECAST}/{cycle[:4]}/{cycle}/{member}/Days:1-10/apcp_sfc_{cycle}_{member}.grib2"


def operational_url(cycle, member, lead):
    prefix = "gec" if member == "c00" else "gep"
    return (f"{OPERATIONAL}/gefs.{cycle[:8]}/{cycle[8:10]}/atmos/pgrb2sp25/"
            f"{prefix}{member[1:]}.t{cycle[8:10]}z.pgrb2s.0p25.f{lead:03d}")


def parse_index(idx_text, cycle):
    """(offset, next_offset_or_None, (start, end)) for each six-hour APCP entry."""
    entries = []
    for line in idx_text.strip().splitlines():
        match = IDX_LINE.match(line)
        if not match:
            raise ValueError(f"Unexpected index line: {line}")
        entries.append((int(match[2]), match[3], match[4], match[5], match[6]))
    found = {}
    for k, (offset, date, variable, level, step) in enumerate(entries):
        span = APCP_RANGE.match(step)
        if variable != "APCP" or level != "surface" or not span:
            continue
        if date != cycle:
            raise ValueError(f"Index reference {date} differs from cycle {cycle}")
        start, end = int(span[1]), int(span[2])
        if end - start == 6 and start % 6 == 0:
            stop = entries[k + 1][0] - 1 if k + 1 < len(entries) else None
            found[(start, end)] = (offset, stop)
    return found


def six_hour_ranges(idx_text, first_hour, last_hour, cycle=None):
    """Byte ranges of disjoint six-hour buckets covering (first_hour, last_hour] in one object."""
    if cycle is None:
        match = IDX_LINE.match(idx_text.strip().splitlines()[0])
        if not match:
            raise ValueError(f"Unexpected index line: {idx_text.strip().splitlines()[0]}")
        cycle = match[3]
    found = parse_index(idx_text, cycle)
    if any(not IDX_LINE.match(line) or IDX_LINE.match(line)[4] != "APCP" for line in idx_text.strip().splitlines()):
        raise ValueError("Unexpected index line: reforecast APCP objects contain only APCP")
    expected = [(h, h + 6) for h in range(first_hour, last_hour, 6)]
    missing = [span for span in expected if span not in found]
    if missing:
        raise ValueError(f"Missing six-hour buckets: have {sorted(found)}, need {expected}")
    return [(start, end, *found[(start, end)]) for start, end in expected]


def get(session, url, headers=None, attempts=4):
    for attempt in range(attempts):
        try:
            response = session.get(url, headers=headers, timeout=120)
        except requests.RequestException:
            if attempt == attempts - 1:
                raise
        else:
            if response.status_code in (403, 404):
                raise CycleUnavailable(f"{response.status_code} for {url}")
            if response.status_code < 500 and response.status_code != 429:
                response.raise_for_status()
                return response
            if attempt == attempts - 1:
                response.raise_for_status()
        time.sleep(2 ** attempt * 3)


def plan_requests(session, cycle, member, first_hour, last_hour):
    """List of (url, start, end, first_byte, last_byte_or_None) for every needed bucket."""
    kind = source_kind(cycle)
    if kind == "reforecast":
        url = reforecast_url(cycle, member)
        ranges = six_hour_ranges(get(session, url + ".idx").text, first_hour, last_hour, cycle)
        return kind, [(url, start, end, offset, stop) for start, end, offset, stop in ranges]
    if kind == "operational":
        planned = []
        for start in range(first_hour, last_hour, 6):
            url = operational_url(cycle, member, start + 6)
            found = parse_index(get(session, url + ".idx").text, cycle)
            if (start, start + 6) not in found:
                raise CycleUnavailable(f"No {start}-{start + 6} h APCP in {url}")
            planned.append((url, start, start + 6, *found[(start, start + 6)]))
        return kind, planned
    raise CycleUnavailable(f"No GEFSv12 cycle {cycle}: reforecast ends 2019-12-31, operational begins 2020-09-23")


def fetch(cycle, member, first_hour, last_hour, cache_dir, session=None, workers=6):
    session = session or requests.Session()
    target = cache_dir / f"apcp_{cycle}_{member}_f{first_hour:03d}-f{last_hour:03d}.grib2"
    kind, planned = plan_requests(session, cycle, member, first_hour, last_hour)
    downloaded = 0
    if not target.exists():
        def one(request):
            url, start, end, offset, stop = request
            response = get(session, url, {"Range": f"bytes={offset}-{'' if stop is None else stop}"})
            if response.status_code != 206 or not response.content.startswith(b"GRIB") or not response.content.endswith(b"7777"):
                raise ValueError(f"Bad range response for {start}-{end} h from {url}: {response.status_code}")
            return response.content
        with ThreadPoolExecutor(workers) as pool:
            parts = list(pool.map(one, planned))
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".part")
        tmp.write_bytes(b"".join(parts))
        tmp.replace(target)
        downloaded = sum(len(p) for p in parts)
    return target, kind, sorted({u for u, *_ in planned}), [(s, e) for _, s, e, _, _ in planned], downloaded


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


def extract(path, boundary_path, cycle=None):
    """Basin-mean six-hour depths; rows carry absolute UTC bounds when the cycle is given."""
    import grib2io
    boundary = gpd.read_file(boundary_path)
    reference = None if cycle is None else cycle_time(cycle)
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
                stamp = f"{ec.codes_get(handle, 'dataDate'):08d}{ec.codes_get(handle, 'dataTime'):04d}"
            finally:
                ec.codes_release(handle)
            if reference is not None and stamp != reference.strftime("%Y%m%d%H%M"):
                raise ValueError(f"Message reference {stamp} differs from cycle {cycle}")
            valid = np.isfinite(values)
            if not np.array_equal(valid, np.isfinite(other)):
                raise ValueError("Decoders disagree about missing cells")
            difference = float(np.max(np.abs(values[valid] - other[valid])))
            if difference > 1e-4:
                raise ValueError(f"Independent decoders disagree by {difference} mm")
            worst = max(worst, difference)
            mean, fraction = weighted_mean(values[indices[:, 0], indices[:, 1]], areas, basin_area)
            row = {"start_hour": int(start), "end_hour": int(end), "basin_mean_mm": mean,
                   "valid_area_fraction": fraction, "max_decoder_difference_mm": difference}
            if reference is not None:
                row.update(reference_time_utc=reference.isoformat(),
                           accumulation_start_utc=(reference + pd.Timedelta(hours=int(start))).isoformat(),
                           valid_end_utc=(reference + pd.Timedelta(hours=int(end))).isoformat())
            rows.append(row)
    cells = [{"lat": float(lats[j, i]), "lon": float(np.mod(lons[j, i], 360) - 360),
              "basin_area_fraction": float(a / basin_area)} for (j, i), a in zip(indices, areas)]
    return rows, cells, worst


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cycles", nargs="+", required=True, help="YYYYMMDD00 cycles (reforecast or operational)")
    p.add_argument("--members", nargs="+", default=["c00"])
    p.add_argument("--first-hour", type=int, default=6, help="Decision lead, e.g. 6 for 06Z from a 00Z run")
    p.add_argument("--last-hour", type=int, default=78)
    p.add_argument("--boundary", type=Path, default=Path("Basins/Canyon_Creek_WA/basin.geojson"))
    p.add_argument("--cache-dir", type=Path, default=Path(".cache/gefs_reforecast"))
    p.add_argument("--output", type=Path, default=Path(".cache/gefs_reforecast/probe_report.json"))
    args = p.parse_args()
    report, session = [], requests.Session()
    for cycle in args.cycles:
        for member in args.members:
            path, kind, urls, buckets, _ = fetch(cycle, member, args.first_hour, args.last_hour, args.cache_dir, session)
            rows, cells, worst = extract(path, args.boundary, cycle)
            if [(r["start_hour"], r["end_hour"]) for r in rows] != buckets:
                raise ValueError("Decoded accumulation steps differ from the index")
            total = sum(r["basin_mean_mm"] for r in rows)
            report.append({"cycle": cycle, "member": member, "source_kind": kind, "source_urls": urls,
                           "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "cells": cells, "max_decoder_difference_mm": worst, "total_mm": total, "intervals": rows})
            print(f"{cycle} {member} ({kind}): {len(rows)} six-hour buckets f{args.first_hour:03d}-f{args.last_hour:03d}, "
                  f"{path.stat().st_size / 1e6:.1f} MB, {len(cells)} cells, basin total {total:.1f} mm, "
                  f"decoder max diff {worst:.2e} mm")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=1))
    print(f"Report: {args.output}")


if __name__ == "__main__":
    main()
