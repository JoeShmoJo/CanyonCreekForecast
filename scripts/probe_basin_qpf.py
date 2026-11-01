#!/usr/bin/env python3
"""Extract one retained NDFD forecast with fractional basin-cell area weights.

Requires optional grib2io native setup: bash scripts/setup_grib2io.sh.
Downloads no data. Cross-checks values/scan order against ecCodes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

# Initialize pyproj before loading the ecCodes wheel's native dependencies.
# The opposite order caused a PROJ symbol/database conflict in this environment.
from pyproj import CRS, Transformer

import geopandas as gpd
import numpy as np
import eccodes as ec
from shapely.geometry import box
from shapely.ops import transform

from test_qpf_archive import decode_grib


def weighted_mean(values, areas, basin_area, min_coverage=.99):
    finite = np.isfinite(values)
    covered = float(areas[finite].sum())
    fraction = covered / basin_area
    if fraction < min_coverage:
        raise ValueError(f"Only {fraction:.3%} valid basin coverage; require {min_coverage:.1%}")
    return float(np.dot(values[finite], areas[finite]) / covered), fraction


def build_weights(message, boundary):
    if list(message.scanModeFlags[:4]) != [0, 1, 0, 1]:
        raise ValueError("This bounded probe is validated only for the sampled NDFD scan convention")
    source = CRS.from_dict(message.projParameters)
    to_native = Transformer.from_crs("EPSG:4326", source, always_xy=True)
    to_area = Transformer.from_crs(source, "EPSG:5070", always_xy=True)
    basin_area_geom = boundary.to_crs("EPSG:5070").geometry.union_all()
    basin = boundary.to_crs(source).geometry.union_all()
    x0, y0 = to_native.transform(float(message.longitudeFirstGridpoint), float(message.latitudeFirstGridpoint))
    dx, dy = float(message.gridlengthXDirection), float(message.gridlengthYDirection)
    xmin, ymin, xmax, ymax = basin.bounds
    imin = max(0, int(np.floor((xmin - x0) / dx - .5)))
    imax = min(message.nx - 1, int(np.ceil((xmax - x0) / dx + .5)))
    jmin = max(0, int(np.floor((ymin - y0) / dy - .5)))
    jmax = min(message.ny - 1, int(np.ceil((ymax - y0) / dy + .5)))
    indices, areas, polygons = [], [], []
    for j in range(jmin, jmax + 1):
        for i in range(imin, imax + 1):
            x, y = x0 + i * dx, y0 + j * dy
            # Densify edges before changing projection; area is never measured in degrees.
            cell = box(x - dx / 2, y - dy / 2, x + dx / 2, y + dy / 2).segmentize(250)
            projected = transform(to_area.transform, cell)
            intersection = projected.intersection(basin_area_geom)
            if intersection.area > .01:
                indices.append((j, i))
                areas.append(intersection.area)
                polygons.append(projected)
    area = float(basin_area_geom.area)
    areas = np.asarray(areas)
    if not np.isclose(areas.sum(), area, rtol=1e-4):
        raise ValueError("Grid-cell intersections do not cover the full basin")
    return np.asarray(indices), areas, area, polygons, basin_area_geom


def align_eccodes(handle, shape):
    values = np.asarray(ec.codes_get_values(handle)).reshape(shape)
    values[values == ec.codes_get(handle, "missingValue")] = np.nan
    # Independent grib2io comparison verified that ecCodes leaves odd rows reversed.
    if ec.codes_get(handle, "alternativeRowScanning"):
        values[1::2] = values[1::2, ::-1]
    return values


def extract_forecast(path, boundary_path):
    try:
        import grib2io
    except ImportError as exc:
        raise RuntimeError("Install optional decoder with bash scripts/setup_grib2io.sh") from exc
    boundary = gpd.read_file(boundary_path)
    if boundary.crs is None or boundary.empty or not boundary.is_valid.all():
        raise ValueError("Need nonempty valid boundary with a declared CRS")
    metadata = decode_grib(path)
    rows = []
    with grib2io.open(path) as archive, path.open("rb") as independent:
        first = archive[0]
        indices, areas, basin_area, polygons, basin = build_weights(first, boundary)
        signature = np.asarray(first.gridDefinitionSection).tobytes()
        values_for_plot = []
        worst_difference = 0.0
        for index, meta in enumerate(metadata):
            if not meta['precipitation_candidate'] or meta['typeOfProcessedData'] != 'fc' or meta['stepType'] != 'accum':
                raise ValueError('Expected an operational accumulated precipitation forecast')
            message = archive[index]
            if message.shortName != "APCP" or message.units != "kg m-2":
                raise ValueError("Unexpected precipitation parameter or units")
            if np.asarray(message.gridDefinitionSection).tobytes() != signature:
                raise ValueError("Grid changed inside file; need separate weights")
            values = np.asarray(message.data)
            handle = ec.codes_grib_new_from_file(independent)
            try:
                other = align_eccodes(handle, values.shape)
            finally:
                ec.codes_release(handle)
            if not np.array_equal(np.isfinite(values), np.isfinite(other)):
                raise ValueError("Decoders disagree about missing cells")
            valid = np.isfinite(values)
            difference = float(np.max(np.abs(values[valid] - other[valid])))
            if difference > 1e-4:
                raise ValueError(f"Independent decoders disagree by {difference} mm")
            worst_difference = max(worst_difference, difference)
            selected = values[indices[:, 0], indices[:, 1]].astype(float)
            mean, fraction = weighted_mean(selected, areas, basin_area)
            rows.append({"reference_time_utc": meta["reference_time_utc"],
                         "accumulation_start_utc": meta["accumulation_start_utc"],
                         "valid_end_utc": meta["valid_end_utc"],
                         "basin_mean_mm": mean, "valid_area_fraction": fraction,
                         "max_decoder_difference_mm": difference})
            values_for_plot.append(selected)
        constant, _ = weighted_mean(np.ones(len(areas)) * 7, areas, basin_area)
        assert abs(constant - 7) < 1e-10
    report = {"source_file": str(path), "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "boundary": str(boundary_path), "boundary_sha256": hashlib.sha256(boundary_path.read_bytes()).hexdigest(),
              "grib2io_version": grib2io.__version__, "g2c_version": "2.3.0",
              "area_crs": "EPSG:5070", "basin_area_km2": basin_area / 1e6,
              "intersecting_grid_cells": len(areas), "weights_sum_relative_to_basin": float(areas.sum() / basin_area),
              "constant_field_test_passed": True, "units": "mm water equivalent",
              "scan_order": "grib2io canonical rows; ecCodes odd rows reversed before comparison",
              "max_independent_decoder_difference_mm": worst_difference,
              "intervals": rows, "note": "One forecast extraction; not a complete historical dataset or a model."}
    return report, {'values_for_plot':values_for_plot,'polygons':polygons,'basin':basin}


def run(path, boundary_path, output):
    report,geometry = extract_forecast(path,boundary_path)
    values_for_plot,polygons,basin = (geometry[k] for k in ['values_for_plot','polygons','basin'])
    rows = report['intervals']
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    # Small standalone diagnostic figure for geographic and interval review.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PatchCollection
    from matplotlib.patches import Polygon
    from matplotlib.colors import Normalize
    import pandas as pd
    fig, axes = plt.subplots(1, 2, figsize=(11, 5), layout="constrained")
    totals = np.sum(np.array(values_for_plot[:4]), axis=0)
    patches = [Polygon(np.asarray(poly.exterior.coords)) for poly in polygons]
    collection = PatchCollection(patches, cmap="Blues", norm=Normalize(0, max(float(np.nanmax(totals)), 1)), edgecolor="gray", linewidth=.4)
    collection.set_array(totals)
    axes[0].add_collection(collection)
    gpd.GeoSeries([basin], crs="EPSG:5070").boundary.plot(ax=axes[0], color="black", linewidth=1.5)
    axes[0].autoscale_view()
    axes[0].set_aspect("equal")
    axes[0].set_title("Canyon Creek boundary and intersecting QPF cells")
    axes[0].set_axis_off()
    fig.colorbar(collection, ax=axes[0], label="First four 6-hour forecast depths summed (mm)", shrink=.8)
    times = pd.to_datetime([r["valid_end_utc"] for r in rows], utc=True)
    axes[1].bar(times, [r["basin_mean_mm"] for r in rows], width=.2)
    axes[1].set_ylabel("Basin-average interval precipitation (mm)")
    axes[1].set_xlabel("Accumulation end (UTC)")
    axes[1].set_title("Six-hour forecast accumulations")
    axes[1].tick_params(axis="x", rotation=40)
    fig.suptitle("Archived NDFD forecast reference: " + rows[0]["reference_time_utc"])
    fig.savefig(output / "basin_qpf.png", dpi=160)
    plt.close(fig)
    print(f"Report: {output / 'report.json'}; cells={report['intersecting_grid_cells']}; max decoder difference={report['max_independent_decoder_difference_mm']:.3g} mm")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file", type=Path, required=True)
    p.add_argument("--boundary", type=Path, default=Path("Basins/Canyon_Creek_WA/basin.geojson"))
    p.add_argument("--output-dir", type=Path, default=Path(".cache/feasibility/basin"))
    args = p.parse_args()
    try:
        run(args.file, args.boundary, args.output_dir)
    except Exception as exc:
        print(f"Basin extraction failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
