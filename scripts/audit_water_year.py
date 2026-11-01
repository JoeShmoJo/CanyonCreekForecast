#!/usr/bin/env python3
"""Audit one complete Pacific water year; bounded flow transfer, no QPF download/model."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

import pandas as pd
import requests

from audit_crossings import audit
from test_usgs import fetch, parse_continuous


def combine_frames(frames):
    combined = pd.concat(frames, ignore_index=True)
    # Adjacent inclusive API windows share their endpoint. Remove only identical rows.
    combined = combined.drop_duplicates().sort_values("time_utc").reset_index(drop=True)
    if combined.time_utc.duplicated().any():
        raise ValueError("Conflicting observations at a shared boundary; inspect before combining")
    return combined


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--water-year", type=int, default=2025)
    p.add_argument("--output-dir", type=Path)
    p.add_argument("--reuse-dir", type=Path, default=Path(".cache/feasibility/crossings"))
    p.add_argument("--max-new-mib", type=float, default=40, help="Additional network-transfer cap; cached reads do not consume it")
    args = p.parse_args()
    if args.max_new_mib < 0:
        p.error("New-transfer budget cannot be negative")
    if not 2008 <= args.water_year <= pd.Timestamp.now(tz="America/Los_Angeles").year:
        p.error("Select a completed modern water year (2008 through the current calendar year)")
    year = args.water_year
    args.output_dir = args.output_dir or Path(f".cache/feasibility/water-year-{year}")
    period_start = pd.Timestamp(year - 1, 10, 1).tz_localize("America/Los_Angeles").tz_convert("UTC")
    period_end = pd.Timestamp(year, 10, 1).tz_localize("America/Los_Angeles").tz_convert("UTC")
    if period_end > pd.Timestamp.now(tz="UTC"):
        p.error("Selected water year has not ended")
    boundaries = pd.date_range(f"{year-1}-10-01", f"{year}-10-01", freq="MS", tz="UTC")
    windows = list(zip(boundaries[:-1], boundaries[1:]))
    # Retain 72h future observations for decisions at the end of the water year.
    windows.append((boundaries[-1], boundaries[-1] + pd.Timedelta(days=5)))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {"water_year": year, "requests": [], "errors": [], "full_multi_year_history_audited": False,
              "usgs_api_key_present": bool(os.environ.get('USGS_API_KEY')),
              "transfer_budget_mib": 40, "decision_interval": "hourly, flow strictly below 450 cfs"}
    frames, budget, new_bytes = [], 0, 0
    try:
        for start, end in windows:
            tag = start.strftime("%Y-%m-%d") + "_" + end.strftime("%Y-%m-%d")
            reuse = args.reuse_dir / (tag + ".csv")
            cache = args.output_dir / (tag + ".json")
            params = {"f": "json", "monitoring_location_id": "USGS-14219000", "parameter_code": "00060",
                      "statistic_id": "00011", "datetime": start.isoformat() + "/" + end.isoformat(), "limit": 10000}
            url = "https://api.waterdata.usgs.gov/ogcapi/v0/collections/continuous/items"
            record = {"requested_url": requests.Request("GET", url, params=params).prepare().url}
            report["requests"].append(record)
            if reuse.exists():
                frame = pd.read_csv(reuse, keep_default_na=False)
                frame["time_utc"] = pd.to_datetime(frame.time_utc, utc=True)
                frame["discharge_cfs"] = pd.to_numeric(frame.discharge_cfs, errors="coerce")
                record.update(reused_csv=str(reuse), new_download_bytes=0)
            else:
                if cache.exists():
                    body = cache.read_text()
                    record["reused_cache"] = True
                else:
                    remaining = int(args.max_new_mib*1024**2)-new_bytes
                    if remaining <= 0:
                        raise ValueError("New-flow transfer budget exhausted; cached files retained")
                    body, actual = fetch(requests.Session(), url, params, max_bytes=min(8_000_000, 40*1024**2-budget, remaining))
                    cache.write_text(body)
                    record.update(final_url=actual, new_download_bytes=len(body.encode()))
                    new_bytes += len(body.encode())
                budget += len(body.encode())
                if budget > 40 * 1024**2:
                    raise ValueError("40 MiB per-year response budget exceeded")
                record["response_sha256"] = hashlib.sha256(body.encode()).hexdigest()
                record["response_bytes"] = len(body.encode())
                payload = json.loads(body)
                if payload.get('type') != 'FeatureCollection' or not isinstance(payload.get('features'),list):
                    raise ValueError('Unexpected USGS response schema; cannot interpret as an empty interval')
                if not payload.get('features') and not any(x.get('rel')=='next' for x in payload.get('links',[])):
                    record.update(observations=0, empty_interval=True)
                    continue
                frame = parse_continuous(payload)
            if frame.time_utc.min() < start or frame.time_utc.max() > end:
                raise ValueError(f"Sample {tag} contains observations outside the requested interval")
            # Missing endpoints can be genuine gaps; preserve them rather than aborting the year.
            record['start_endpoint_present'] = bool(frame.time_utc.min() == start)
            record['end_endpoint_present'] = bool(frame.time_utc.max() == end)
            record["observations"] = len(frame)
            frames.append(frame)
        combined = combine_frames(frames)
        combined.to_csv(args.output_dir / "discharge.csv", index=False)
        report['merged_flow_csv_sha256'] = hashlib.sha256((args.output_dir/'discharge.csv').read_bytes()).hexdigest()
        report["audit"] = audit(combined, period_start=period_start, period_end=period_end)
        exact = combined[(combined.time_utc >= period_start) & (combined.time_utc < period_end)]
        expected = int((period_end-period_start).total_seconds()/900)
        report["period_observations_expected"] = expected
        report["period_observations_returned"] = len(exact)
        report["period_missing_or_invalid_values"] = int(exact.discharge_cfs.isna().sum())
        report['period_qualifier_counts'] = exact.qualifiers.fillna('').value_counts().to_dict()
        report["observed_hourly_decisions_below_450"] = int(((exact.time_utc.dt.minute == 0) & (exact.discharge_cfs < 450)).sum())
        intervals = combined.time_utc.diff().dt.total_seconds()
        report["gaps"] = [{"before_utc": combined.time_utc.iloc[i-1].isoformat(),
                           "after_utc": combined.time_utc.iloc[i].isoformat(),
                           "interval_seconds": float(intervals.iloc[i]),
                           "missing_15min_slots": int(intervals.iloc[i] / 900 - 1),
                           "before_cfs": float(combined.discharge_cfs.iloc[i-1]),
                           "after_cfs": float(combined.discharge_cfs.iloc[i])}
                          for i in combined.index[(intervals > 900).fillna(False)]]
        report["monthly_crossing_counts_local"] = pd.Series([x["local_day"][:7] for x in report["audit"]["events"]], dtype=str).value_counts().sort_index().to_dict()
        report["new_download_bytes"] = sum(x.get("new_download_bytes",0) for x in report["requests"])
        report["retained_response_bytes_excluding_reused_csv"] = sum(x.get("response_bytes",0) for x in report["requests"])
        report["conservative_note"] = "One-year feasibility audit, not a multi-year event frequency estimate. Overlapping decision windows are correlated."
    except Exception as exc:
        report["errors"].append(str(exc))
    output = args.output_dir / "report.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Report: {output}; errors={len(report['errors'])}")
    return int(bool(report["errors"]))


if __name__ == "__main__":
    sys.exit(main())
