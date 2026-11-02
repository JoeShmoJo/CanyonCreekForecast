#!/usr/bin/env python3
"""Build a resumable multi-year dataset pairing GEFSv12 basin QPF with Canyon Creek flow. No model training.

One 12Z decision per Pacific water-year day. Flow features, crossing labels and eligibility rules are
imported unchanged from the validated 2025 pipeline; only the forecast source and decision hour differ.
The 00Z GEFSv12 control run of the decision date supplies disjoint six-hour basin depths for (t, t+72h],
under an assumed (not verified) publication delay. Every completed row is checkpointed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import shutil
import sys
import time

from build_pilot_2025 import JOIN_COLUMNS, sha, load_flow, flow_features, target_for_case, qpf_features
from build_water_year_dataset import atomic_json, scheduled_decisions, exclusion_code
from probe_gefs_reforecast import CycleUnavailable, cycle_time, source_kind, fetch, extract
from audit_crossings import audit
import pandas as pd
import requests

AVERAGE_SOURCE_BYTES = 4 * 2**20  # planning estimate per member-day; the ledger records actual bytes


def combine_flow(pattern, years, destination):
    """Merge annual audit CSVs into one series; overlapping buffer rows must agree exactly."""
    frames = []
    for year in years:
        path = Path(pattern.format(year=year))
        if not path.exists():
            raise FileNotFoundError(f"Missing flow audit for water year {year}: {path}")
        frames.append(pd.read_csv(path, keep_default_na=False, dtype=str))
    merged = pd.concat(frames).drop_duplicates()
    if merged.time_utc.duplicated().any():
        raise ValueError("Annual flow files disagree on overlapping timestamps")
    merged = merged.assign(order=pd.to_datetime(merged.time_utc, utc=True, format="ISO8601")).sort_values("order")
    destination.parent.mkdir(parents=True, exist_ok=True)
    merged.drop(columns="order").to_csv(destination, index=False)
    return destination


def gefs_exclusion_code(error):
    if isinstance(error, CycleUnavailable):
        return "gefs_cycle_unavailable"
    return exclusion_code(error)


def make_plan(config, flow, events):
    hour, delay = config["decision_hour_utc"], pd.Timedelta(hours=config["gefs_assumed_delay_hours"])
    plan = []
    for year in config["water_years"]:
        for decision in scheduled_decisions(year, hour):
            row = {"case_id": "G" + decision.strftime("%Y%m%d"), "decision_time_utc": decision.isoformat(),
                   "water_year": year, "status": "pending", "stage": "antecedent_flow"}
            try:
                past, cutoff = flow_features(flow, decision, config["flow_delay_minutes"])
                row["stage"] = "target_quality"
                target = target_for_case(flow, events, decision)
                row["stage"] = "forecast_cycle"
                reference = decision.normalize() + pd.Timedelta(hours=config["gefs_cycle_hour_utc"])
                cycle = reference.strftime("%Y%m%d%H")
                if source_kind(cycle) is None:
                    raise CycleUnavailable(f"No GEFSv12 cycle {cycle}")
                if reference + delay > decision:
                    raise ValueError("Forecast violates assumed publication delay")
                row.update(stage="planned", past_features=past, target=target, flow_cutoff_utc=cutoff.isoformat(),
                           cycle=cycle, source_kind=source_kind(cycle))
            except ValueError as exc:
                row.update(status="excluded", reason_code=gefs_exclusion_code(exc), reason=str(exc))
            plan.append(row)
    return plan


class GefsStore:
    def __init__(self, cache, boundary, config):
        self.cache, self.boundary, self.config = cache, boundary, config
        self.extractor_sha = sha(Path(__file__).with_name("probe_gefs_reforecast.py"))
        self.boundary_sha = sha(boundary)
        self.ledger_path = cache / "acquisition_ledger.json"
        self.ledger = json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else {"requests": []}
        self.new_bytes, self.new_files, self.cache_hits = 0, 0, 0
        self.session = requests.Session()
        self.lead = config["decision_hour_utc"] - config["gefs_cycle_hour_utc"]

    @property
    def spent(self):
        return sum(x["bytes"] for x in self.ledger["requests"])

    def source_path(self, cycle):
        member = self.config["gefs_member"]
        return self.cache / "gribs" / f"apcp_{cycle}_{member}_f{self.lead:03d}-f{self.lead + 72:03d}.grib2"

    def obtain(self, cycle):
        if self.source_path(cycle).exists():
            self.cache_hits += 1
        elif self.spent + AVERAGE_SOURCE_BYTES > self.config["new_download_budget_gib"] * 2**30:
            raise RuntimeError("Project-wide GEFS download budget would be exceeded")
        local, kind, urls, buckets, downloaded = fetch(cycle, self.config["gefs_member"], self.lead, self.lead + 72,
                                                       self.cache / "gribs", self.session)
        if downloaded:
            self.new_bytes += downloaded
            self.new_files += 1
            self.ledger["requests"].append({"cycle": cycle, "urls": urls, "bytes": downloaded, "sha256": sha(local),
                                            "downloaded_at_utc": datetime.now(timezone.utc).isoformat()})
            atomic_json(self.ledger_path, self.ledger)
        digest = sha(local)
        cached = self.cache / "extractions" / (digest + ".json")
        extraction = json.loads(cached.read_text()) if cached.exists() else None
        if not extraction or extraction.get("extractor_sha256") != self.extractor_sha or extraction.get("boundary_sha256") != self.boundary_sha:
            rows, cells, worst = extract(local, self.boundary, cycle)
            if [(r["start_hour"], r["end_hour"]) for r in rows] != buckets:
                raise ValueError("Decoded accumulation steps differ from the index")
            extraction = {"source_sha256": digest, "boundary_sha256": self.boundary_sha,
                          "extractor_sha256": self.extractor_sha, "cells": cells,
                          "max_independent_decoder_difference_mm": worst, "intervals": rows}
            atomic_json(cached, extraction)
        return extraction, local, digest, kind, urls


def checkpoint_valid(record, fingerprint):
    if record.get("fingerprint") != fingerprint or record.get("status") != "passed":
        return False
    source = record["provenance"]
    path = Path(source["source_local_path"])
    return path.exists() and sha(path) == source["source_sha256"]


def build(config_path, output, cache, plan_only=False):
    started = time.perf_counter()
    config = json.loads(config_path.read_text())
    if config["flow_delay_minutes"] < 15 or config["gefs_assumed_delay_hours"] < 6:
        raise ValueError("Keep at least the 15-minute flow and 6-hour GEFS delays")
    if config["flow_lookback_hours"] != 72 or config["target_horizon_hours"] != 72:
        raise ValueError("Only the validated 72-hour flow/target windows are implemented")
    if config["gefs_member"] != "c00":
        raise ValueError("Only the control member is configured for this build")
    output.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    flow_path = combine_flow(config["flow_csv_pattern"], config["water_years"], cache / "discharge_combined.csv")
    flow = load_flow(flow_path)
    events = audit(flow.reset_index(drop=True))["events"]
    store = GefsStore(cache, Path(config["boundary"]), config)
    inputs = {"config": sha(config_path), "flow": sha(flow_path), "boundary": store.boundary_sha,
              "pipeline": sha(Path(__file__)), "pairing_rules": sha(Path(__file__).with_name("build_pilot_2025.py")),
              "target_rules": sha(Path(__file__).with_name("audit_crossings.py")), "extractor": store.extractor_sha}
    fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    plan = make_plan(config, flow, events)
    atomic_json(cache / "plan.json", {"fingerprint": fingerprint, "config": config, "decisions": plan})
    candidates = [r for r in plan if r["status"] == "pending"]
    needed = sum(AVERAGE_SOURCE_BYTES for r in candidates if not store.source_path(r["cycle"]).exists())
    by_year = Counter(r["water_year"] for r in candidates)
    print(f"Plan: {len(plan)} decisions; {len(candidates)} candidates; "
          f"new source estimate {needed / 2**30:.1f} GiB; per year {dict(sorted(by_year.items()))}", flush=True)
    if needed > config["new_download_budget_gib"] * 2**30 - store.spent:
        raise ValueError("Preflight exceeds remaining project-wide download budget")
    if shutil.disk_usage(cache).free < needed + 2**30:
        raise ValueError("Insufficient free disk for retained grids and checkpoints")
    if plan_only:
        return 0
    manifest = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "config": config,
                "config_source": config_path.as_posix(), "config_sha256": inputs["config"],
                "pipeline_source": f"scripts/{Path(__file__).name}",
                "pipeline_sha256": inputs["pipeline"], "shared_pairing_rules_sha256": inputs["pairing_rules"],
                "target_rules_sha256": inputs["target_rules"], "extractor_sha256": inputs["extractor"],
                "flow_csv_sha256": inputs["flow"], "boundary_sha256": inputs["boundary"], "input_fingerprint": fingerprint,
                "model_trained": False, "complete": False, "scheduled_decisions": len(plan),
                "sampling": "Every scheduled 12Z decision in each configured Pacific water year; no event/control selection.",
                "availability_status": "Assumed 6-hour GEFS publication delay and 15-minute flow delay; historic receipt and revision times not verified.",
                "package_versions": {p: version(p) for p in ["numpy", "pandas", "geopandas", "shapely", "pyproj", "eccodes", "grib2io"]},
                "cases": [], "errors": [], "rows_paired": 0, "resumed_rows": 0}
    results = []

    def save():
        manifest.update(run_new_download_bytes=store.new_bytes, run_new_files=store.new_files,
                        run_source_cache_hits=store.cache_hits, project_successful_download_bytes=store.spent,
                        elapsed_seconds=time.perf_counter() - started)
        atomic_json(output / "manifest.json", manifest)

    statuses = []
    for row in plan:
        status = {**{k: row[k] for k in JOIN_COLUMNS}, "water_year": row["water_year"],
                  "status": row["status"], "stage": row["stage"]}
        if row["status"] == "excluded":
            status.update(reason_code=row["reason_code"], reason=row["reason"])
        statuses.append(status)
    manifest["cases"] = statuses
    save()
    for index, row in enumerate(plan):
        status = statuses[index]
        if row["status"] == "excluded":
            continue
        identity = {k: row[k] for k in JOIN_COLUMNS}
        case_path = cache / "cases" / (row["case_id"] + ".json")
        try:
            previous = json.loads(case_path.read_text()) if case_path.exists() else None
            if previous and checkpoint_valid(previous, fingerprint):
                result = previous
                manifest["resumed_rows"] += 1
            else:
                try:
                    extraction, local, digest, kind, urls = store.obtain(row["cycle"])
                except CycleUnavailable as exc:
                    status.update(status="excluded", stage="forecast_source", reason_code="gefs_cycle_unavailable", reason=str(exc))
                    save()
                    continue
                decision = pd.Timestamp(row["decision_time_utc"])
                try:
                    if min(r["valid_area_fraction"] for r in extraction["intervals"]) < .99:
                        raise ValueError("Valid basin coverage below 99%")
                    rain, coverage = qpf_features(extraction["intervals"], decision, config["qpf_horizons_hours"])
                except ValueError as exc:
                    status.update(status="excluded", stage="forecast_quality", reason_code="forecast_intervals", reason=str(exc))
                    save()
                    continue
                reference = cycle_time(row["cycle"])
                age = (decision - reference).total_seconds() / 3600
                features = {**identity, **row["past_features"], **rain, "forecast_age_hours": age}
                if not all(math.isfinite(v) for k, v in features.items() if k not in JOIN_COLUMNS):
                    raise ValueError("Nonfinite predictor")
                provenance = {**identity, "water_year": row["water_year"], "flow_cutoff_utc": row["flow_cutoff_utc"],
                              "forecast_reference_time_utc": reference.isoformat(),
                              "assumed_forecast_available_utc": (reference + pd.Timedelta(hours=config["gefs_assumed_delay_hours"])).isoformat(),
                              "forecast_age_hours": age, **coverage, "gefs_source": kind, "gefs_member": config["gefs_member"],
                              "source_urls": " ".join(urls), "source_sha256": digest, "source_local_path": local.as_posix(),
                              "min_basin_valid_area_fraction": min(r["valid_area_fraction"] for r in extraction["intervals"]),
                              "max_independent_decoder_difference_mm": extraction["max_independent_decoder_difference_mm"]}
                result = {"status": "passed", "fingerprint": fingerprint, "features": features,
                          "labels": {**identity, **row["target"]}, "provenance": provenance,
                          "intervals": [{**identity, "source_sha256": digest, **r} for r in extraction["intervals"]]}
                atomic_json(case_path, result)
            results.append(result)
            status.update(status="passed", stage="paired", gefs_source=row["source_kind"])
            manifest["rows_paired"] = len(results)
            if len(results) % 25 == 0:
                save()
                print(f"Paired {len(results)}/{len(candidates)} (WY{row['water_year']}); "
                      f"run downloads {store.new_bytes / 2**30:.2f} GiB", flush=True)
        except Exception as exc:
            status.update(status="failed", stage="processing", reason=str(exc))
            manifest["errors"].append({**identity, "error": str(exc), "type": type(exc).__name__})
            save()
            print(f"Stopped at {row['case_id']}: {exc}", file=sys.stderr, flush=True)
            break
    accounted = all(s["status"] in ["passed", "excluded"] for s in statuses)
    if accounted and not manifest["errors"] and results:
        tables = {"features": [r["features"] for r in results], "labels": [r["labels"] for r in results],
                  "provenance": [r["provenance"] for r in results],
                  "qpf_intervals": [x for r in results for x in r["intervals"]], "audit": statuses}
        for name, rows in tables.items():
            destination = output / (name + ".csv")
            pd.DataFrame(rows).to_csv(destination, index=False)
            manifest.setdefault("output_sha256", {})[destination.name] = sha(destination)
        manifest["predictor_columns"] = [c for c in results[0]["features"] if c not in JOIN_COLUMNS]
        manifest["label_columns"] = [c for c in results[0]["labels"] if c not in JOIN_COLUMNS]
        manifest["positive_rows"] = sum(r["labels"]["target_crossing_72h"] for r in results)
        manifest["exclusion_counts"] = dict(Counter(s.get("reason_code") for s in statuses if s["status"] == "excluded"))
        manifest["complete"] = True
    save()
    print(f"Complete={manifest['complete']}; paired={len(results)}; "
          f"excluded={sum(s['status'] == 'excluded' for s in statuses)}; errors={len(manifest['errors'])}", flush=True)
    return 0 if manifest["complete"] else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=Path("configs/dataset_gefs.json"))
    p.add_argument("--output-dir", type=Path, default=Path("data/dataset_gefs"))
    p.add_argument("--cache-dir", type=Path, default=Path(".cache/dataset_gefs"))
    p.add_argument("--plan-only", action="store_true")
    args = p.parse_args()
    try:
        return build(args.config, args.output_dir, args.cache_dir, args.plan_only)
    except Exception as exc:
        print(f"GEFS dataset build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
