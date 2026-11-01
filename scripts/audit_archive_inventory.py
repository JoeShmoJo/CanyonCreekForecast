#!/usr/bin/env python3
"""Small date-inventory audit around sampled crossings; downloads no GRIB files."""
import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import sys

import requests

from test_qpf_archive import list_objects, is_wmo_bulletin


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dates", nargs="+", default=["2021-02-01", "2021-02-02", "2021-02-03",
        "2023-01-03", "2023-01-04", "2023-01-05", "2025-03-14", "2025-03-15", "2025-03-16"])
    p.add_argument("--output", type=Path, default=Path(".cache/feasibility/inventory.json"))
    args = p.parse_args()
    if len(args.dates) > 12:
        p.error("At most twelve dates per audit; not a full-history crawler")
    report = {"days": [], "errors": [], "full_history_completeness_validated": False,
              "note": "Object counts are not promises of production schedule or content/lead completeness."}
    session = requests.Session()
    for value in args.dates:
        row = {"date": value}
        report["days"].append(row)
        try:
            d = date.fromisoformat(value)
            prefix = f"wmo/qpf/{d.year:04}/{d.month:02}/{d.day:02}/"
            listing = list_objects(session, "https://noaa-ndfd-pds.s3.amazonaws.com/", prefix)
            row["listing_url"] = listing["url"]
            if listing["truncated"]:
                raise ValueError("Truncated listing; cannot report complete daily count")
            objects = [x for x in listing["objects"] if "YIUZ98_KWBN_" in x["key"] and is_wmo_bulletin(x['key'])]
            row["objects"] = objects
            row["conus_bulletin_count"] = len(objects)
            hours = Counter(x["key"][-4:-2] for x in objects)
            row["filename_hour_counts"] = dict(sorted(hours.items()))
            row["hours_without_bulletins"] = [f"{h:02}" for h in range(24) if f"{h:02}" not in hours]
            if not objects:
                raise ValueError("No CONUS objects in tested prefix")
        except Exception as exc:
            row["error"] = str(exc)
            report["errors"].append({"date": value, "error": str(exc)})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Report: {args.output}; errors={len(report['errors'])}")
    return int(bool(report["errors"]))


if __name__ == "__main__":
    sys.exit(main())
