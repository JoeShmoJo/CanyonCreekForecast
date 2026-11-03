#!/usr/bin/env python3
"""Compare GEFS basin QPF in the dataset rows with AORC observed basin precipitation. No model training.

For each passed row and horizon H, the observed depth is the sum of AORC hours ending in (t, t+H]; rows
whose window has any hour below 99% valid basin area, or that extend past the AORC record, are dropped.
Reports bias and skill by forecast source (reforecast vs operational) and water year, plus a timing check
that shifts the observed series by whole hours against the six-hour GEFS intervals.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

HORIZONS = [6, 12, 24, 48, 72]
WET_72H_MM = 50.0


def observed_sums(hourly, decisions, horizons=HORIZONS):
    """Observed depth over (t, t+H] for each decision t; NaN where any required hour is missing or incomplete."""
    series = hourly.set_index("hour_end")
    good = series.valid_area_fraction.ge(.99)
    depth = series.basin_mean_mm.where(good)
    full = pd.date_range(series.index.min(), series.index.max(), freq="h")
    depth = depth.reindex(full)
    cum = np.r_[0.0, np.nancumsum(depth.to_numpy())]
    bad = np.r_[0, np.cumsum(depth.isna().to_numpy())]
    out = {}
    for horizon in horizons:
        start = full.get_indexer(decisions + pd.Timedelta(hours=1))
        stop = full.get_indexer(decisions + pd.Timedelta(hours=horizon))
        ok = (start >= 0) & (stop >= 0)
        s, e = np.where(ok, start, 0), np.where(ok, stop, 0) + 1
        values = np.where(ok & (bad[e] == bad[s]), cum[e] - cum[s], np.nan)
        out[f"obs_next_{horizon}h_mm"] = values
    return pd.DataFrame(out, index=decisions)


def skill(forecast, observed):
    f, o = np.asarray(forecast), np.asarray(observed)
    wet_o, wet_f = o >= WET_72H_MM, f >= WET_72H_MM
    return {"rows": int(len(o)), "mean_forecast_mm": round(float(f.mean()), 2), "mean_observed_mm": round(float(o.mean()), 2),
            "ratio_forecast_to_observed": round(float(f.sum() / o.sum()), 3),
            "correlation": round(float(np.corrcoef(f, o)[0, 1]), 3),
            "rmse_mm": round(float(np.sqrt(((f - o) ** 2).mean())), 2),
            f"observed_wet_rows_ge_{WET_72H_MM:g}mm": int(wet_o.sum()),
            "wet_hit_rate": round(float((wet_f & wet_o).sum() / max(wet_o.sum(), 1)), 3),
            "wet_false_alarm_ratio": round(float((wet_f & ~wet_o).sum() / max(wet_f.sum(), 1)), 3)}


def timing_check(hourly, intervals, shifts=range(-3, 4)):
    """Correlation of first-day six-hour GEFS depths with observed depths shifted by whole hours."""
    first = intervals[intervals.end_hour - 12 <= 12]
    ends = pd.to_datetime(first.valid_end_utc, utc=True, format="ISO8601")
    series = hourly.set_index("hour_end").basin_mean_mm
    roll = series.rolling(6).sum()
    result = {}
    for shift in shifts:
        obs = roll.reindex(ends + pd.Timedelta(hours=shift)).to_numpy()
        ok = np.isfinite(obs)
        result[shift] = round(float(np.corrcoef(first.basin_mean_mm.to_numpy()[ok], obs[ok])[0, 1]), 4)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, default=Path("data/dataset_gefs"))
    p.add_argument("--observed", type=Path, default=Path("data/observed_precip_aorc/basin_hourly.csv"))
    p.add_argument("--output-dir", type=Path, default=Path("data/observed_precip_aorc"))
    args = p.parse_args()
    hourly = pd.read_csv(args.observed)
    hourly["hour_end"] = pd.to_datetime(hourly.hour_end_utc, utc=True, format="ISO8601")
    features = pd.read_csv(args.dataset / "features.csv")
    provenance = pd.read_csv(args.dataset / "provenance.csv", usecols=["case_id", "water_year", "gefs_source"])
    rows = features.merge(provenance, on="case_id", validate="one_to_one")
    decisions = pd.DatetimeIndex(pd.to_datetime(rows.decision_time_utc, utc=True, format="ISO8601"))
    observed = observed_sums(hourly, decisions).reset_index(drop=True)
    paired = pd.concat([rows[["case_id", "decision_time_utc", "water_year", "gefs_source"]
                             + [f"qpf_next_{h}h_mm" for h in HORIZONS]], observed], axis=1)
    paired.to_csv(args.output_dir / "gefs_rows_observed.csv", index=False, float_format="%.4f")
    usable = paired.dropna(subset=[f"obs_next_{h}h_mm" for h in HORIZONS])
    report = {"rows": len(paired), "rows_with_complete_observations": len(usable),
              "by_source": {}, "by_source_72h_wet_season_oct_mar": {}, "by_water_year_72h": {},
              "timing_correlation_by_observed_shift_hours": timing_check(hourly, pd.read_csv(args.dataset / "qpf_intervals.csv"))}
    for source, group in usable.groupby("gefs_source"):
        report["by_source"][source] = {f"{h}h": skill(group[f"qpf_next_{h}h_mm"], group[f"obs_next_{h}h_mm"]) for h in HORIZONS}
        month = pd.to_datetime(group.decision_time_utc, utc=True, format="ISO8601").dt.month
        wet = group[month.isin([10, 11, 12, 1, 2, 3])]
        report["by_source_72h_wet_season_oct_mar"][source] = skill(wet.qpf_next_72h_mm, wet.obs_next_72h_mm)
    for year, group in usable.groupby("water_year"):
        report["by_water_year_72h"][int(year)] = {"source": "/".join(sorted(group.gefs_source.unique())),
                                                  **skill(group.qpf_next_72h_mm, group.obs_next_72h_mm)}
    (args.output_dir / "gefs_comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
