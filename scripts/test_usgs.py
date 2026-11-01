#!/usr/bin/env python3
"""Verify a small USGS 14219000 instantaneous-discharge sample, not a model."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
from urllib.parse import urljoin, urlsplit

import numpy as np
import pandas as pd
import requests

SITE = "14219000"


def api_headers(endpoint):
    """Optional key stays in an HTTPS header, never URLs, reports, or other hosts."""
    key = os.environ.get('USGS_API_KEY')
    if key and urlsplit(endpoint).scheme == 'https' and urlsplit(endpoint).hostname == 'api.waterdata.usgs.gov':
        return {'X-Api-Key': key}
    return {}


def fetch(session, endpoint, params, max_bytes=8_000_000, _redirects=3):
    headers = api_headers(endpoint)
    with session.get(endpoint, params=params, headers=headers, allow_redirects=not bool(headers),
                     timeout=(10, 30), stream=True) as response:
        if headers and response.is_redirect:
            target = urljoin(response.url, response.headers['Location'])
            if _redirects <= 0 or urlsplit(target).scheme != 'https' or urlsplit(target).hostname != 'api.waterdata.usgs.gov':
                raise ValueError('Refusing to forward USGS API key through an untrusted or excessive redirect')
            return fetch(session,target,None,max_bytes,_redirects-1)
        if response.status_code == 429:
            hint = 'API key supplied' if headers else 'no USGS_API_KEY supplied'
            raise requests.HTTPError(f"USGS rate limit (429; {hint}); Retry-After={response.headers.get('Retry-After','not provided')}",response=response)
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("Response exceeds 8 MB sample limit")
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8"), response.url


def parse_rdb(text):
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    if len(lines) < 2:
        raise ValueError("Missing RDB header/type rows")
    return pd.read_csv(io.StringIO("\n".join([lines[0]] + lines[2:])), sep="\t", dtype=str).fillna("")


def parse_discharge(payload):
    rows = []
    for ts in payload["value"]["timeSeries"]:
        codes = {str(x["value"]) for x in ts["variable"]["variableCode"]}
        sites = {str(x["value"]) for x in ts["sourceInfo"]["siteCode"]}
        if "00060" not in codes or SITE not in sites:
            continue
        units = ts["variable"]["unit"]["unitCode"]
        if units not in ("ft3/s", "ft^3/s"):
            raise ValueError(f"Unexpected discharge units: {units}; threshold requires cfs")
        nodata = float(ts["variable"].get("noDataValue", -999999))
        for block in ts["values"]:
            for value in block["value"]:
                try:
                    discharge = float(value["value"])
                except (ValueError, TypeError):
                    discharge = np.nan
                if discharge == nodata or discharge < 0:
                    discharge = np.nan
                rows.append({"time_utc": value["dateTime"],
                             "discharge_cfs": discharge,
                             "qualifiers": ",".join(value.get("qualifiers", [])),
                             "series_name": ts.get("name", "")})
    if not rows:
        raise ValueError("No instantaneous discharge records returned for station 14219000")
    frame = pd.DataFrame(rows)
    frame['time_utc'] = pd.to_datetime(frame.time_utc,utc=True,format='ISO8601')
    return frame.sort_values("time_utc")


def parse_continuous(payload):
    if any(link.get("rel") == "next" for link in payload.get("links", [])):
        raise ValueError("USGS response has another page; shorten sample rather than treating partial data as complete")
    rows = []
    for feature in payload.get("features", []):
        p = feature["properties"]
        if p["monitoring_location_id"] != f"USGS-{SITE}" or p["parameter_code"] != "00060":
            raise ValueError("Unexpected station or parameter in response")
        if p["unit_of_measure"] not in ("ft3/s", "ft^3/s"):
            raise ValueError("Unexpected discharge units")
        value = pd.to_numeric(p.get("value"), errors="coerce")
        qualifier = p.get('qualifier')
        if qualifier is None:
            qualifier = []
        elif isinstance(qualifier,str):
            qualifier = [qualifier]
        elif not isinstance(qualifier,list) or not all(isinstance(x,str) for x in qualifier):
            raise ValueError('Unexpected USGS qualifier schema; preserve and inspect the response')
        rows.append({"time_utc": p["time"],
                     "discharge_cfs": value if pd.notna(value) and value >= 0 else np.nan,
                     "qualifiers": ",".join(filter(None, [p.get("approval_status"), *qualifier])),
                     "series_name": p["time_series_id"]})
    if not rows:
        raise ValueError("No continuous discharge observations returned")
    frame = pd.DataFrame(rows)
    frame['time_utc'] = pd.to_datetime(frame.time_utc,utc=True,format='ISO8601')
    return frame.sort_values("time_utc")


def summarize(frame, threshold=450.0):
    # Do not silently combine distinct sensor/statistic series.
    if frame["series_name"].nunique() > 1:
        raise ValueError("Multiple discharge series returned; inspect/select before computing statistics")
    duplicates = int(frame["time_utc"].duplicated().sum())
    if duplicates:
        raise ValueError(f"{duplicates} duplicate timestamps; resolve before computing statistics")
    valid = frame.dropna(subset=["discharge_cfs"]).set_index("time_utc")
    if len(valid) < 2:
        raise ValueError("Fewer than two valid discharge observations")
    diffs = valid.index.to_series().diff().dropna().dt.total_seconds()
    nominal = float(diffs.mode().iloc[0])
    exceed = valid["discharge_cfs"] > threshold
    # Observed runs are separated by gaps, and are not necessarily independent floods.
    gaps = valid.index.to_series().diff().dt.total_seconds() > 1.5 * nominal
    runs = exceed & (~exceed.shift(1, fill_value=False) | gaps)
    hours = 72 * 3600
    steps = int(round(hours / nominal))
    # Count complete future windows in the *sample*, without interpolating missing values.
    end = np.arange(len(valid)) + steps
    eligible = end < len(valid)
    starts = np.flatnonzero(eligible)
    complete = 0
    for i in starts:
        j = i + steps
        if abs((valid.index[j] - valid.index[i]).total_seconds() - hours) < 1:
            interval = valid.index[i:j + 1].to_series().diff().dropna().dt.total_seconds()
            complete += int(bool((np.abs(interval - nominal) < 1).all()))
    return {
        "first_observation_utc": valid.index[0].isoformat(),
        "last_observation_utc": valid.index[-1].isoformat(),
        "returned_records": len(frame), "valid_records": len(valid),
        "missing_or_invalid_values": int(frame["discharge_cfs"].isna().sum()),
        "min_discharge_cfs": float(valid["discharge_cfs"].min()),
        "max_discharge_cfs": float(valid["discharge_cfs"].max()),
        "nominal_interval_seconds_in_sample": nominal,
        "interval_counts_seconds": {str(k): int(v) for k, v in diffs.value_counts().head(10).items()},
        "gaps_over_1_5_nominal": int((diffs > 1.5 * nominal).sum()),
        "maximum_observed_gap_seconds": float(diffs.max()),
        "strict_threshold_cfs": threshold, "observations_above_threshold": int(exceed.sum()),
        "fraction_of_observed_values_above_threshold": float(exceed.mean()),
        "observed_exceedance_runs_including_left_censored": int(runs.sum()),
        "sample_begins_above_threshold": bool(exceed.iloc[0]),
        "complete_72h_future_windows_in_sample": complete,
        "qualifier_counts": frame["qualifiers"].value_counts().to_dict(),
        "warning": "Sample statistics only, not long-term flood frequency. Gaps outside returned span are not measured. Provisional/estimated qualifiers retained.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2021-11-11")
    parser.add_argument("--end", default="2021-11-18")
    parser.add_argument("--output-dir", type=Path, default=Path(".cache/feasibility/usgs"))
    parser.add_argument("--iv-url", default="https://waterservices.usgs.gov/nwis/iv/")
    parser.add_argument("--site-url", default="https://waterservices.usgs.gov/nwis/site/")
    parser.add_argument("--legacy", action="store_true", help="Use legacy IV JSON instead of the modern continuous API")
    args = parser.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if not 0 < (end - start).days <= 31:
        parser.error("Sample must span 1–31 days; no bulk retrieval in this test")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {"tested_at_utc": datetime.now(timezone.utc).isoformat(), "site": SITE,
              "usgs_api_key_present": bool(os.environ.get('USGS_API_KEY')),
              "requests": [], "errors": [], "requested_start": args.start, "requested_end": args.end}
    session = requests.Session()
    operations = [
        ("site_catalog", args.site_url, {"format": "rdb", "sites": SITE,
                                        "seriesCatalogOutput": "true", "siteStatus": "all"}),
        ("instantaneous_discharge", args.iv_url if args.legacy else
         "https://api.waterdata.usgs.gov/ogcapi/v0/collections/continuous/items",
         {"format": "json", "sites": SITE, "parameterCd": "00060", "startDT": args.start,
          "endDT": args.end, "siteStatus": "all"} if args.legacy else
         {"f": "json", "monitoring_location_id": f"USGS-{SITE}", "parameter_code": "00060",
          "datetime": f"{args.start}T00:00:00Z/{args.end}T00:00:00Z", "limit": 10000}),
        ("time_series_metadata", "https://api.waterdata.usgs.gov/ogcapi/v0/collections/time-series-metadata/items",
         {"f": "json", "monitoring_location_id": f"USGS-{SITE}", "parameter_code": "00060", "limit": 100}),
        ("annual_peaks", "https://api.waterdata.usgs.gov/ogcapi/v0/collections/peaks/items",
         {"f": "json", "monitoring_location_id": f"USGS-{SITE}", "limit": 100}),
        # New USGS API discovery only: do not assume legacy JSON schema or query names.
        ("modern_api_collections", "https://api.waterdata.usgs.gov/ogcapi/v0/collections", {"f": "json"}),
    ]
    for name, endpoint, params in operations:
        record = {"operation": name, "endpoint": endpoint, "params": params}
        report["requests"].append(record)
        try:
            body, url = fetch(session, endpoint, params)
            record["url"] = url
            suffix = ".rdb" if name == "site_catalog" else ".json"
            (args.output_dir / (name + suffix)).write_text(body)
            if name == "site_catalog":
                catalog = parse_rdb(body)
                report["site_catalog_rows"] = catalog.to_dict(orient="records")
                report["record_note"] = "Inspect 00060 + uv rows for instantaneous record; dv is daily, not interchangeable."
            elif name == "instantaneous_discharge":
                frame = parse_discharge(json.loads(body)) if args.legacy else parse_continuous(json.loads(body))
                frame.to_csv(args.output_dir / "discharge.csv", index=False)
                report["sample_summary"] = summarize(frame)
            elif name == "time_series_metadata":
                report["time_series_metadata"] = [x["properties"] for x in json.loads(body).get("features", [])]
            elif name == "annual_peaks":
                payload = json.loads(body)
                if any(x.get("rel") == "next" for x in payload.get("links", [])):
                    raise ValueError("Annual peaks truncated; do not compute frequency")
                peaks = [x["properties"] for x in payload.get("features", [])
                         if x["properties"]["parameter_code"] == "00060"]
                if not peaks or any(x["unit_of_measure"] not in ("ft3/s", "ft^3/s") for x in peaks):
                    raise ValueError("No usable cfs annual peaks")
                report["annual_peak_summary"] = {
                    "returned_discharge_peaks": len(peaks),
                    "water_years": sorted({x["water_year"] for x in peaks}),
                    "water_years_with_peak_above_450_cfs": sorted({x["water_year"] for x in peaks if float(x["value"]) > 450}),
                    "min_annual_peak_cfs": min(float(x["value"]) for x in peaks),
                    "max_annual_peak_cfs": max(float(x["value"]) for x in peaks),
                    "warning": "Years with recorded annual peaks only; cannot estimate independent events/year or 72h prevalence."}
            else:
                report["modern_collections"] = [x["id"] for x in json.loads(body).get("collections", [])]
        except Exception as exc:
            record["error"] = str(exc)
            report["errors"].append({"operation": name, "error": str(exc)})
    output = args.output_dir / "report.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Report: {output}; discharge_sample={'sample_summary' in report}; errors={len(report['errors'])}")
    # Discovery failure alone doesn't invalidate a retrieved legacy discharge sample.
    return 0 if "sample_summary" in report and "site_catalog_rows" in report else 1


if __name__ == "__main__":
    sys.exit(main())
