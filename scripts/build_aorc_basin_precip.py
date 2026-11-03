#!/usr/bin/env python3
"""Hourly observed basin-mean precipitation from NOAA AORC v1.1 (1 km) on AWS. No model training.

Only the Zarr chunks that intersect the basin are downloaded and kept (raw, compressed) under the cache.
AORC timestamps come from wgrib2 verftime(), i.e. the END of each one-hour accumulation, so a value at
time T is the depth over (T - 1h, T]. Cells are weighted by their fractional basin area in EPSG:5070.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import Transformer
import requests
from shapely.geometry import box
from shapely.ops import transform
import zstandard

BUCKET = "https://noaa-nws-aorc-v1-1-1km.s3.amazonaws.com"
VARIABLE = "APCP_surface"
FILL = -32767


class Store:
    """Zarr v2 reader for one public bucket; every fetched object is cached and logged by size and hash."""

    def __init__(self, cache):
        self.cache, self.session = cache, requests.Session()
        self.new_bytes, self.new_files, self.hits = 0, 0, 0

    def raw(self, key):
        path = self.cache / key
        if path.exists():
            self.hits += 1
            return path.read_bytes()
        for attempt in range(4):
            try:
                response = self.session.get(f"{BUCKET}/{key}", timeout=60)
                if response.status_code == 200:
                    break
            except requests.RequestException:
                if attempt == 3:
                    raise
        else:
            raise ValueError(f"AORC object unavailable ({response.status_code}): {key}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(response.content)
        tmp.replace(path)
        self.new_bytes += len(response.content)
        self.new_files += 1
        return response.content

    def meta(self, key):
        return json.loads(self.raw(key))

    def chunk(self, array, meta, index):
        shape = [meta["chunks"][i] for i in range(len(index))]
        data = zstandard.ZstdDecompressor().decompress(self.raw(f"{array}/{'.'.join(map(str, index))}"),
                                                       max_output_size=int(np.prod(shape)) * 8)
        return np.frombuffer(data, dtype=meta["dtype"]).reshape(shape)

    def vector(self, array):
        meta = self.meta(f"{array}/.zarray")
        if meta["compressor"]["id"] != "zstd" or meta["filters"]:
            raise ValueError(f"Unexpected Zarr encoding for {array}")
        n, size = meta["shape"][0], meta["chunks"][0]
        parts = [self.chunk(array, meta, (i,)) for i in range(-(-n // size))]
        return np.concatenate(parts)[:n]


def cell_weights(lats, lons, boundary):
    """Fractional basin area of each regular WGS84 lat/lon cell centred on (lats, lons), in EPSG:5070."""
    to_area = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
    basin = boundary.to_crs("EPSG:5070").geometry.union_all()
    dlat, dlon = abs(float(lats[1] - lats[0])), abs(float(lons[1] - lons[0]))
    cells = []
    for j, lat in enumerate(lats):
        for i, lon in enumerate(lons):
            cell = box(lon - dlon / 2, lat - dlat / 2, lon + dlon / 2, lat + dlat / 2).segmentize(dlon / 4)
            overlap = transform(to_area.transform, cell).intersection(basin).area
            if overlap > .01:
                cells.append((j, i, overlap))
    areas = np.array([a for *_, a in cells])
    if not np.isclose(areas.sum(), basin.area, rtol=1e-4):
        raise ValueError("Grid-cell intersections do not cover the full basin")
    return np.array([(j, i) for j, i, _ in cells]), areas


def year_series(store, year, boundary, reference_grid):
    root = f"{year}.zarr"
    meta = store.meta(f"{root}/{VARIABLE}/.zarray")
    attrs = store.meta(f"{root}/{VARIABLE}/.zattrs")
    if (meta["dtype"], meta["compressor"]["id"], meta["filters"], meta.get("dimension_separator", ".")) != ("<i2", "zstd", None, "."):
        raise ValueError(f"Unexpected {VARIABLE} encoding in {year}")
    if attrs["units"] != "kg/m^2" or attrs["_ARRAY_DIMENSIONS"] != ["time", "latitude", "longitude"]:
        raise ValueError(f"Unexpected {VARIABLE} units or dimensions in {year}")
    scale = float(attrs["scale_factor"])
    lats, lons = store.vector(f"{root}/latitude"), store.vector(f"{root}/longitude")
    times = pd.to_datetime(store.vector(f"{root}/time"), unit="s", utc=True)
    if reference_grid is not None and not (np.array_equal(lats, reference_grid[0]) and np.array_equal(lons, reference_grid[1])):
        raise ValueError(f"AORC grid changed in {year}")
    if len(times) != meta["shape"][0] or (times.year != year).any() or (times[1:] - times[:-1] != pd.Timedelta(hours=1)).any():
        raise ValueError(f"AORC {year} times are not one contiguous hourly calendar year")
    west, south, east, north = boundary.to_crs("EPSG:4326").total_bounds
    dlat, dlon = abs(lats[1] - lats[0]), abs(lons[1] - lons[0])
    rows = np.flatnonzero((lats >= south - dlat) & (lats <= north + dlat))
    cols = np.flatnonzero((lons >= west - dlon) & (lons <= east + dlon))
    indices, areas = cell_weights(lats[rows], lons[cols], boundary)
    cells = np.column_stack([rows[indices[:, 0]], cols[indices[:, 1]]])
    tc, yc, xc = meta["chunks"]
    blocks = sorted({(j // yc, i // xc) for j, i in cells})
    keys = [(t, by, bx) for t in range(-(-len(times) // tc)) for by, bx in blocks]
    with ThreadPoolExecutor(8) as pool:
        decoded = dict(zip(keys, pool.map(lambda k: store.chunk(f"{root}/{VARIABLE}", meta, k), keys)))
    values = np.empty((len(times), len(cells)))
    for t in range(-(-len(times) // tc)):
        span = slice(t * tc, min((t + 1) * tc, len(times)))
        for n, (j, i) in enumerate(cells):
            values[span, n] = decoded[(t, j // yc, i // xc)][:span.stop - span.start, j % yc, i % xc]
    valid = values != FILL
    depth = np.where(valid, values * scale, 0.0)
    if (depth < 0).any():
        raise ValueError(f"Negative AORC precipitation in {year}")
    fraction = (valid * areas).sum(axis=1) / areas.sum()
    mean = np.where(fraction > 0, (depth * areas).sum(axis=1) / np.maximum((valid * areas).sum(axis=1), 1e-12), np.nan)
    frame = pd.DataFrame({"hour_end_utc": times.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                          "basin_mean_mm": mean, "valid_area_fraction": fraction})
    return frame, (lats, lons), len(cells)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--first-year", type=int, default=2007)
    p.add_argument("--last-year", type=int, default=2025)
    p.add_argument("--boundary", type=Path, default=Path("Basins/Canyon_Creek_WA/basin.geojson"))
    p.add_argument("--output-dir", type=Path, default=Path("data/observed_precip_aorc"))
    p.add_argument("--cache-dir", type=Path, default=Path(".cache/aorc"))
    args = p.parse_args()
    boundary = gpd.read_file(args.boundary)
    store = Store(args.cache_dir)
    frames, grid, summary = [], None, {}
    try:
        for year in range(args.first_year, args.last_year + 1):
            frame, grid, n_cells = year_series(store, year, boundary, grid)
            frames.append(frame)
            summary[year] = {"hours": len(frame), "incomplete_hours": int((frame.valid_area_fraction < .99).sum()),
                             "basin_cells": n_cells, "total_mm": round(float(frame.basin_mean_mm.sum()), 1)}
            print(f"AORC {year}: {summary[year]}; downloaded so far {store.new_bytes / 2**20:.0f} MiB", flush=True)
    except Exception as exc:
        print(f"AORC extraction failed: {exc}", file=sys.stderr)
        return 1
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / "basin_hourly.csv"
    pd.concat(frames).to_csv(output, index=False, float_format="%.4f")
    manifest = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "source": BUCKET, "variable": VARIABLE,
                "time_convention": "hour_end_utc closes a one-hour accumulation (wgrib2 verftime)",
                "boundary": args.boundary.as_posix(), "boundary_sha256": sha(args.boundary),
                "pipeline_source": f"scripts/{Path(__file__).name}", "pipeline_sha256": sha(Path(__file__)),
                "years": summary, "run_new_download_bytes": store.new_bytes, "run_new_objects": store.new_files,
                "cache_hits": store.hits, "output_sha256": {output.name: sha(output)}}
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote {output}; new download {store.new_bytes / 2**20:.0f} MiB in {store.new_files} objects")
    return 0


if __name__ == "__main__":
    sys.exit(main())
