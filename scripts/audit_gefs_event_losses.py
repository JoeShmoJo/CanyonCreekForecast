#!/usr/bin/env python3
"""Trace every observed 450 cfs crossing day to the GEFS dataset rows that could label it. No model training.

A crossing at time e is reachable from the 12Z decisions d with d < e <= d + 72h. It is captured when at
least one of those decisions passed (and so carries a positive label). Otherwise it is lost, attributed to
the exclusion codes of its covering decisions: "structural" when every covering decision was already at or
above 450 (outside the target definition), "data" when any was excluded for flow quality or forecast gaps.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

import pandas as pd

from audit_crossings import audit
from build_pilot_2025 import load_flow

STRUCTURAL = {"last_flow_not_below_450", "decision_flow_not_below_450"}


def trace(events, statuses, horizon_hours=72):
    statuses = statuses.assign(decision=pd.to_datetime(statuses.decision_time_utc, utc=True, format="ISO8601"))
    first, last = statuses.decision.min(), statuses.decision.max() + pd.Timedelta(hours=horizon_hours)
    horizon = pd.Timedelta(hours=horizon_hours)
    rows = []
    for event in events:
        time = pd.Timestamp(event["time_utc"])
        if not first < time <= last:
            continue
        cover = statuses[(statuses.decision < time) & (time <= statuses.decision + horizon)]
        codes = sorted(set(cover.reason_code.dropna()))
        if (cover.status == "passed").any():
            outcome = "captured"
        elif cover.empty:
            outcome = "uncovered"
        elif set(codes) <= STRUCTURAL:
            outcome = "structural"
        else:
            outcome = "data"
        rows.append({"crossing_time_utc": time.isoformat(), "local_day": event["local_day"],
                     "water_year": int(cover.water_year.max()) if len(cover) else None,
                     "crossing_cfs": event["discharge_cfs"], "covering_decisions": len(cover),
                     "passed_decisions": int((cover.status == "passed").sum()),
                     "outcome": outcome, "exclusion_codes": " ".join(codes)})
    return pd.DataFrame(rows)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--flow", type=Path, default=Path(".cache/dataset_gefs/discharge_combined.csv"))
    p.add_argument("--audit", type=Path, default=Path("data/dataset_gefs/audit.csv"))
    p.add_argument("--output", type=Path, default=Path("data/dataset_gefs_event_losses.csv"))
    args = p.parse_args()
    flow = load_flow(args.flow)
    events = audit(flow.reset_index(drop=True))["events"]
    statuses = pd.read_csv(args.audit, keep_default_na=False, dtype=str).replace("", None)
    table = trace(events, statuses)
    table.to_csv(args.output, index=False)
    summary = {"crossing_days": len(table), "outcomes": dict(Counter(table.outcome)),
               "data_loss_codes": dict(Counter(c for codes in table[table.outcome == "data"].exclusion_codes
                                               for c in codes.split())),
               "data_losses_by_water_year": {int(k): int(v) for k, v in
                                             table[table.outcome == "data"].water_year.value_counts().sort_index().items()}}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
