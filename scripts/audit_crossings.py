#!/usr/bin/env python3
"""Bounded instantaneous-flow crossing audit; no predictive model or bulk history."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import requests

from test_usgs import fetch, parse_continuous

DEFAULT_WINDOWS = ["2019-01-01/2019-02-01", "2021-02-01/2021-03-01",
                   "2023-01-01/2023-02-01", "2025-03-01/2025-04-01"]


def audit(frame, threshold=450.0, cadence_seconds=900, period_start=None, period_end=None):
    """Equality preserves the last strictly-below/above state; gaps reset it."""
    f = frame.copy().sort_values("time_utc").reset_index(drop=True)
    if f.time_utc.duplicated().any() or f.series_name.nunique() != 1:
        raise ValueError("Need one series with unique timestamps")
    raw, unknown_starts, previous_time, state = [], 0, None, None
    for row in f.itertuples():
        contiguous = previous_time is not None and (row.time_utc - previous_time).total_seconds() == cadence_seconds
        if not contiguous:
            state = None
        previous_time = row.time_utc
        if not np.isfinite(row.discharge_cfs):
            state = None
            continue
        if row.discharge_cfs < threshold:
            state = "below"
        elif row.discharge_cfs > threshold:
            if state == "below":
                raw.append({"time_utc": row.time_utc.isoformat(),
                            "local_day": str(row.time_utc.tz_convert("America/Los_Angeles").date()),
                            "discharge_cfs": float(row.discharge_cfs), "qualifiers": row.qualifiers})
            elif state is None:
                unknown_starts += 1
            state = "above"
    retained, seen = [], set()
    first_local = f.time_utc.iloc[0].tz_convert("America/Los_Angeles")
    first_local_day = str(first_local.date())
    partial_first_day = first_local != first_local.normalize()
    first_full_day = (first_local.normalize() + pd.DateOffset(days=int(partial_first_day))).tz_convert("UTC")
    excluded_partial_day = 0
    for event in raw:
        if partial_first_day and event["local_day"] == first_local_day:
            # Cannot establish that this was the first crossing on a partial first day.
            excluded_partial_day += 1
            continue
        if event["local_day"] not in seen:
            retained.append(event)
            seen.add(event["local_day"])
    # Count below-threshold hourly decision times with completely observed +72h.
    # A gap or missing value anywhere in the window makes it ineligible here.
    # pandas can store datetimes at microsecond resolution; normalize explicitly.
    times = f.time_utc.array.as_unit("ns").asi8 // 1_000_000_000
    finite = np.isfinite(f.discharge_cfs.to_numpy())
    broken = np.r_[False, np.diff(times) != cadence_seconds]
    local_dates = f.time_utc.dt.tz_convert('America/Los_Angeles').dt.date.to_numpy()
    history_bad_days = set(local_dates[~finite])
    for i in np.flatnonzero(broken):
        left = f.time_utc.iloc[i-1] + pd.Timedelta(seconds=cadence_seconds)
        right = f.time_utc.iloc[i] - pd.Timedelta(seconds=cadence_seconds)
        if left > right:
            history_bad_days.add(local_dates[i])
        else:
            days = pd.date_range(left.tz_convert('America/Los_Angeles').normalize(),
                                 right.tz_convert('America/Los_Angeles').normalize(),freq='D')
            history_bad_days.update(x.date() for x in days)
    bad_prefix = np.r_[0, np.cumsum(~finite)]
    gap_prefix = np.r_[0, np.cumsum(broken)]
    steps = 72 * 3600 // cadence_seconds
    hourly = (f.time_utc.dt.minute.eq(0) & f.time_utc.dt.second.eq(0)
              & f.time_utc.ge(first_full_day))
    if period_start is not None:
        hourly &= f.time_utc.ge(period_start)
    if period_end is not None:
        hourly &= f.time_utc.lt(period_end)
    eligible, positives, history_censored = 0, 0, 0
    events = np.array([pd.Timestamp(x["time_utc"]).timestamp() for x in retained])
    for i in np.flatnonzero((hourly & f.discharge_cfs.lt(threshold)).to_numpy()):
        j = i + steps
        if j >= len(f) or times[j] - times[i] != 72 * 3600:
            continue
        if bad_prefix[j + 1] != bad_prefix[i] or gap_prefix[j + 1] != gap_prefix[i + 1]:
            continue
        # A pre-decision gap on this local day could hide an earlier first crossing.
        # Complete future data alone cannot resolve that ambiguity.
        if local_dates[i] in history_bad_days:
            history_censored += 1
            continue
        eligible += 1
        positives += int(bool(((events > times[i]) & (events <= times[j])).any()))
    diffs = f.time_utc.diff().dt.total_seconds().dropna()
    def within_period(event):
        t = pd.Timestamp(event["time_utc"])
        return ((period_start is None or t >= period_start)
                and (period_end is None or t < period_end))
    scoped_raw = [event for event in raw if within_period(event)]
    scoped_retained = [event for event in retained if within_period(event)]
    scoped_partial = sum(partial_first_day and event["local_day"] == first_local_day for event in scoped_raw)
    return {"first_utc": f.time_utc.iloc[0].isoformat(), "last_utc": f.time_utc.iloc[-1].isoformat(),
            "counting_period_start_utc": None if period_start is None else period_start.isoformat(),
            "counting_period_end_exclusive_utc": None if period_end is None else period_end.isoformat(),
            "observations": len(f), "missing_values": int((~finite).sum()),
            "non_15min_intervals": int((diffs != cadence_seconds).sum()),
            "min_cfs": float(f.discharge_cfs.min()), "max_cfs": float(f.discharge_cfs.max()),
            "raw_upcrossings": len(scoped_raw), "crossings_on_partial_first_local_day_excluded": scoped_partial,
            "deduplicated_crossings": len(scoped_retained),
            "same_day_repeats_removed": len(scoped_raw) - scoped_partial - len(scoped_retained),
            "above_threshold_segment_starts_with_unknown_crossing": unknown_starts,
            "complete_72h_hourly_decisions_below_450": eligible,
            "hourly_decisions_censored_for_incomplete_local_day_history": history_censored,
            "decisions_with_future_deduplicated_crossing": positives,
            "qualifier_counts": f.qualifiers.fillna("").value_counts().to_dict(),
            "events": scoped_retained}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--windows", nargs="+", default=DEFAULT_WINDOWS)
    p.add_argument("--input", type=Path, help="Audit a retained discharge CSV without requests")
    p.add_argument("--output-dir", type=Path, default=Path(".cache/feasibility/crossings"))
    args = p.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {"tested_at_utc": datetime.now(timezone.utc).isoformat(),
              "rule": "Strictly below to strictly above 450; equality retains state; no crossing across gaps; first crossing per America/Los_Angeles calendar day.",
              "samples": [], "errors": [], "full_history_audited": False}
    inputs = [str(args.input)] if args.input else args.windows
    if len(inputs) > 6:
        p.error("At most six sample windows per invocation")
    for item in inputs:
        record = {"window_or_input": item}
        report["samples"].append(record)
        try:
            if args.input:
                f = pd.read_csv(args.input, keep_default_na=False)
                f["time_utc"] = pd.to_datetime(f.time_utc, utc=True)
                f["discharge_cfs"] = pd.to_numeric(f.discharge_cfs, errors="coerce")
            else:
                start, end = item.split("/")
                if not 0 < (date.fromisoformat(end) - date.fromisoformat(start)).days <= 31:
                    raise ValueError("Each sample must span 1–31 days")
                cache = args.output_dir / (start + "_" + end + ".json")
                params = {"f": "json", "monitoring_location_id": "USGS-14219000",
                          "parameter_code": "00060", "statistic_id": "00011", "limit": 10000,
                          "datetime": f"{start}T00:00:00Z/{end}T00:00:00Z"}
                url = "https://api.waterdata.usgs.gov/ogcapi/v0/collections/continuous/items"
                record["requested_url"] = requests.Request("GET", url, params=params).prepare().url
                if cache.exists():
                    body = cache.read_text()
                    record["used_cached_response"] = True
                else:
                    body, actual_url = fetch(requests.Session(), url, params)
                    cache.write_text(body)
                    record["final_url"] = actual_url
                f = parse_continuous(json.loads(body))
                f.to_csv(cache.with_suffix(".csv"), index=False)
            record.update(audit(f))
        except Exception as exc:
            record["error"] = str(exc)
            report["errors"].append({"sample": item, "error": str(exc)})
    out = args.output_dir / "report.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Report: {out}; errors={len(report['errors'])}")
    return int(bool(report["errors"]))


if __name__ == "__main__":
    sys.exit(main())
