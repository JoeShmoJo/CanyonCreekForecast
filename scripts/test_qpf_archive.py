#!/usr/bin/env python3
"""Bounded public NDFD discovery/download and GRIB inspection; never a model.

No credentials needed. A failed request is recorded and exits nonzero.
S3 listing dates are NOT accepted as proof of forecast issuance dates.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote
import xml.etree.ElementTree as ET

import numpy as np
import requests

NS = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
DEFAULT_DATES = ["2021-11-14", "2022-12-26", "2023-12-04", "2024-01-27", "2025-03-15"]


def is_wmo_bulletin(key):
    """Canonical archive bulletin names have no extension; .aux.xml files are metadata."""
    name = key.rsplit('/',1)[-1]
    if not re.fullmatch(r'[A-Z0-9]{6}_[A-Z0-9]{4}_\d{12}',name):
        return False
    try:
        datetime.strptime(name[-12:],'%Y%m%d%H%M')
        return True
    except ValueError:
        return False


def get_small(session, url, limit, *, params=None):
    """Bound transfer size even if Content-Length is absent or incorrect."""
    with session.get(url, params=params, timeout=(10, 30), stream=True) as response:
        response.raise_for_status()
        if int(response.headers.get("Content-Length", 0)) > limit:
            raise ValueError(f"Response exceeds {limit} bytes: {response.url}")
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > limit:
                raise ValueError(f"Transfer exceeded {limit} bytes: {response.url}")
            chunks.append(chunk)
        return b"".join(chunks), response.url


def list_objects(session, bucket_url, prefix, delimiter=None):
    params = {"list-type": "2", "prefix": prefix, "max-keys": 200}
    if delimiter:
        params["delimiter"] = delimiter
    payload, url = get_small(session, bucket_url, 2_000_000, params=params)
    tree = ET.fromstring(payload)
    return {
        "url": url,
        "truncated": tree.findtext("s:IsTruncated", namespaces=NS) == "true",
        "prefixes": [x.text for x in tree.findall("s:CommonPrefixes/s:Prefix", NS)],
        "objects": [{"key": x.findtext("s:Key", namespaces=NS),
                     "size": int(x.findtext("s:Size", namespaces=NS)),
                     "last_modified": x.findtext("s:LastModified", namespaces=NS)}
                    for x in tree.findall("s:Contents", NS)],
    }


def decode_grib(path, max_messages=500):
    import eccodes as ec

    def key(handle, name):
        try:
            return ec.codes_get(handle, name)
        except ec.CodesInternalError:
            return None

    names = ["edition", "centre", "subCentre", "shortName", "name", "units",
             "discipline", "parameterCategory", "parameterNumber", "typeOfLevel",
             "dataDate", "dataTime", "significanceOfReferenceTime", "stepType",
             "forecastTime", "indicatorOfUnitOfTimeRange",
             "tablesVersion", "localTablesVersion", "typeOfGeneratingProcess",
             "startStep", "endStep", "stepUnits", "stepRange", "validityDate",
             "validityTime", "lengthOfTimeRange", "indicatorOfUnitForTimeRange",
             "productDefinitionTemplateNumber", "typeOfProcessedData",
             "gridType", "Nx", "Ny", "DxInMetres", "DyInMetres", "numberOfPoints",
             "iScansNegatively", "jScansPositively", "jPointsAreConsecutive",
             "alternativeRowScanning", "bitmapPresent", "numberOfMissing"]
    messages = []
    domain_checks = {}
    with path.open("rb") as stream:
        while True:
            handle = ec.codes_grib_new_from_file(stream)
            if handle is None:
                break
            try:
                if len(messages) >= max_messages:
                    raise ValueError("GRIB message limit reached; inspection incomplete")
                row = {name: key(handle, name) for name in names}
                row["message"] = len(messages) + 1
                reference = datetime.strptime(f"{row['dataDate']:08}{row['dataTime']:04}", "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
                valid = datetime.strptime(f"{row['validityDate']:08}{row['validityTime']:04}", "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
                row["reference_time_utc"] = reference.isoformat()
                row["valid_end_utc"] = valid.isoformat()
                # Prefer explicit interval end + duration: NDFD step accessors may
                # round minute-valued leads or mix units as ecCodes keys are read.
                seconds = {0: 60, 1: 3600, 2: 86400, 10: 10800, 11: 21600, 12: 43200, 13: 1}
                unit = row["indicatorOfUnitForTimeRange"]
                if row["stepType"] == "accum" and unit in seconds and row["lengthOfTimeRange"] is not None:
                    duration = row["lengthOfTimeRange"] * seconds[unit]
                    row["accumulation_start_utc"] = (valid - timedelta(seconds=duration)).isoformat()
                    row["accumulation_hours"] = duration / 3600
                # Paired coordinates retain decoder scanning order. Never reshape naively.
                signature = key(handle, "md5Section3") or f"message-{len(messages)}"
                if signature not in domain_checks:
                    lat = np.asarray(ec.codes_get_array(handle, "latitudes"))
                    lon = (np.asarray(ec.codes_get_array(handle, "longitudes")) + 180) % 360 - 180
                    # This is a point-domain check, NOT basin-average extraction.
                    distance = np.hypot((lat - 45.9404) * 111.2,
                                        (lon + 122.3226) * 111.2 * np.cos(np.deg2rad(45.9404)))
                    nearest = int(np.nanargmin(distance))
                    domain_checks[signature] = {
                        "latitude": 45.9404, "longitude": -122.3226,
                        "nearest_grid_distance_km": float(distance[nearest]),
                        "inside_coordinate_envelope": bool(lat.min() <= 45.9404 <= lat.max()
                                                           and lon.min() <= -122.3226 <= lon.max()),
                        "note": "Check full supplied watershed intersection separately; not a basin mean.",
                    }
                    del lat, lon, distance
                row["amboy_point_check"] = domain_checks[signature]
                row["precipitation_candidate"] = bool(
                    row["discipline"] == 0 and row["parameterCategory"] == 1
                    and row["parameterNumber"] == 8)
                messages.append(row)
            finally:
                ec.codes_release(handle)
    if not messages:
        raise ValueError("No GRIB messages decoded (HTML/error pages are not data)")
    return messages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", nargs="+", default=DEFAULT_DATES)
    parser.add_argument("--bucket-url", default="https://noaa-ndfd-pds.s3.amazonaws.com/")
    parser.add_argument("--parameter", help="Exact WMO parameter prefix discovered from bucket listing")
    parser.add_argument("--key-contains", default="YIUZ98_KWBN", help="CONUS QPF bulletin selector; override for other products")
    parser.add_argument("--prefix-template", default="wmo/{parameter}/{year}/{month}/{day}/")
    parser.add_argument("--url", action="append", default=[], help="Exact verified GRIB URL; repeatable")
    parser.add_argument("--file", type=Path, help="Inspect a local GRIB without network access")
    parser.add_argument("--output-dir", type=Path, default=Path(".cache/feasibility/qpf"))
    parser.add_argument("--max-mib", type=int, default=32, help="Per object; total also capped")
    parser.add_argument("--total-mib", type=int, default=64)
    parser.add_argument("--max-files", type=int, default=5)
    args = parser.parse_args()
    if min(args.max_mib, args.total_mib, args.max_files) <= 0:
        parser.error("Size/file limits must be positive")
    days = [date.fromisoformat(x) for x in args.dates]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {"tested_at_utc": datetime.now(timezone.utc).isoformat(),
              "listings": [], "files": [], "errors": [], "ten_year_training_dataset_validated": False}
    session = requests.Session()
    total = 0

    def retrieve(url, expected_size=None):
        nonlocal total
        if len(report["files"]) >= args.max_files:
            raise ValueError("Maximum file count reached")
        limit = min(args.max_mib * 1024**2, args.total_mib * 1024**2 - total)
        if limit <= 0 or (expected_size is not None and expected_size > limit):
            raise ValueError("Object exceeds remaining download budget")
        payload, final_url = get_small(session, url, limit)
        total += len(payload)
        digest = hashlib.sha256(payload).hexdigest()
        path = args.output_dir / (digest + ".grib2")
        if payload.startswith(b"\x1f\x8b"):
            # Bounded decompression protects against unexpectedly large containers.
            import io
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as zipped:
                decoded = zipped.read(args.max_mib * 1024**2 + 1)
            if len(decoded) > args.max_mib * 1024**2:
                raise ValueError("Decompressed file exceeds limit")
        else:
            decoded = payload
        path.write_bytes(decoded)
        record = {"requested_url": url, "final_url": final_url, "download_bytes": len(payload),
                  "download_sha256": digest, "path": str(path)}
        report["files"].append(record)
        record["messages"] = decode_grib(path)
        if not any(x["precipitation_candidate"] for x in record["messages"]):
            raise ValueError("Downloaded file contains no recognized precipitation messages")
        if any(x["stepType"] != "accum" or x["typeOfProcessedData"] != "fc"
               for x in record["messages"] if x["precipitation_candidate"]):
            raise ValueError("Precipitation is not identified as an accumulated forecast")
        if not all(x["amboy_point_check"]["inside_coordinate_envelope"]
                   and x["amboy_point_check"]["nearest_grid_distance_km"] < 30
                   for x in record["messages"] if x["precipitation_candidate"]):
            raise ValueError("Precipitation grid fails Amboy point-domain check; select a CONUS product")

    if args.file:
        try:
            report["files"].append({"path": str(args.file), "messages": decode_grib(args.file),
                                    "local_only": True})
        except Exception as exc:
            report["errors"].append({"operation": "local decode", "error": str(exc)})
    elif args.url:
        for url in args.url[:args.max_files]:
            try:
                retrieve(url)
            except Exception as exc:
                report["errors"].append({"url": url, "error": str(exc)})
        if len(args.url) > args.max_files:
            report["errors"].append({"error": "Some requested URLs omitted by max-files"})
    else:
        parameter = args.parameter
        if not parameter:
            try:
                listing = list_objects(session, args.bucket_url, "wmo/", "/")
                report["listings"].append(listing)
                matches = [p.split("/")[1] for p in listing["prefixes"]
                           if re.search(r"qpf|precip|apcp", p, re.I)]
                if len(matches) != 1:
                    raise ValueError("Cannot identify unique QPF prefix; inspect listing and supply --parameter")
                parameter = matches[0]
            except Exception as exc:
                report["errors"].append({"operation": "parameter discovery", "error": str(exc)})
        # Even if root discovery fails, attempt all requested date listings explicitly.
        for day in days:
            prefix = args.prefix_template.format(parameter=parameter or "qpf", year=f"{day.year:04}",
                                                  month=f"{day.month:02}", day=f"{day.day:02}",
                                                  date=day.isoformat(), ymd=day.strftime("%Y%m%d"))
            try:
                listing = list_objects(session, args.bucket_url, prefix)
                listing["requested_date"] = day.isoformat()
                listing["prefix_inferred"] = parameter is None
                report["listings"].append(listing)
                if listing["truncated"]:
                    report["errors"].append({"prefix": prefix, "error": "Listing truncated at 200; not a completeness audit"})
                objects = [o for o in listing["objects"] if o["size"] > 0
                           and args.key_contains in o["key"] and is_wmo_bulletin(o["key"])]
                if not objects:
                    raise ValueError("No objects in tested prefix; does not prove archive lacks that date")
                # Download at most one object per day; contents must verify the parameter.
                item = next((o for o in objects if o["size"] <= args.max_mib * 1024**2), None)
                if item is None:
                    raise ValueError("No listed object fits per-file budget")
                retrieve(args.bucket_url.rstrip("/") + "/" + quote(item["key"], safe="/"), item["size"])
            except Exception as exc:
                report["errors"].append({"date": day.isoformat(), "prefix": prefix, "error": str(exc)})
    report["total_download_bytes"] = total
    report["note"] = "Decode success does not establish issuance semantics, ten-year completeness or full watershed coverage."
    output = args.output_dir / "report.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Report: {output}; files={len(report['files'])}; errors={len(report['errors'])}")
    return 1 if report["errors"] or not report["files"] else 0


if __name__ == "__main__":
    sys.exit(main())
