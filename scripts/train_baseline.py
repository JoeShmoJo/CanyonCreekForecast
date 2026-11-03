#!/usr/bin/env python3
"""First baseline models for the 450 cfs / 72 h crossing target, validated by leave-one-water-year-out.

Every score is out-of-fold: each water year is predicted by models fit on all other years, because
overlapping daily rows within a year are not independent. Feature sets separate what flow alone gives,
what GEFS QPF adds, and what perfect rain knowledge (AORC observed) would add, the last on rows with
complete observations only. A two-threshold rule (72 h QPF and current flow) fitted per fold is the
simple benchmark a paddler could apply by hand.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, precision_recall_curve, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler

FLOW = ["flow_last_cfs", "flow_lag_1h_from_cutoff_cfs", "flow_lag_6h_from_cutoff_cfs", "flow_lag_24h_from_cutoff_cfs",
        "flow_lag_72h_from_cutoff_cfs", "flow_mean_6h_cfs", "flow_mean_24h_cfs", "flow_mean_72h_cfs",
        "flow_max_24h_cfs", "flow_delta_6h_cfs", "calendar_doy_sin", "calendar_doy_cos"]
QPF = [f"qpf_next_{h}h_mm" for h in [6, 12, 24, 48, 72]]
OBS = [f"obs_next_{h}h_mm" for h in [6, 12, 24, 48, 72]]
FEATURE_SETS = {"flow_only": FLOW, "flow_gefs": FLOW + QPF + ["is_operational"], "flow_observed_rain": FLOW + OBS}
SIGNED = {"flow_delta_6h_cfs", "calendar_doy_sin", "calendar_doy_cos", "is_operational"}


def models(columns):
    positive = [c not in SIGNED for c in columns]
    def logify(x):
        x = np.array(x, dtype=float)
        x[:, positive] = np.log1p(np.clip(x[:, positive], 0, None))
        return x
    return {"logistic": make_pipeline(FunctionTransformer(logify), StandardScaler(), LogisticRegression(C=1.0, max_iter=2000)),
            "boosted_trees": HistGradientBoostingClassifier(max_depth=3, learning_rate=.05, max_iter=250,
                                                            l2_regularization=1.0, min_samples_leaf=30, random_state=0)}


def fit_rule(qpf, flow, y):
    """Best-F1 pair of thresholds: alert when 72 h QPF >= a and current flow >= b."""
    best = (-1, 0, 0)
    for a in np.quantile(qpf, np.linspace(.5, .99, 40)):
        for b in np.quantile(flow, np.linspace(0, .95, 40)):
            alert = (qpf >= a) & (flow >= b)
            tp = (alert & y).sum()
            f1 = 2 * tp / max(alert.sum() + y.sum(), 1)
            if f1 > best[0]:
                best = (f1, a, b)
    return best[1], best[2]


def precision_at_recall(y, p, recall):
    precision, recalls, _ = precision_recall_curve(y, p)
    return float(precision[recalls >= recall].max())


def scores(y, p):
    base = brier_score_loss(y, np.full(len(y), y.mean()))
    return {"rows": int(len(y)), "positives": int(y.sum()), "roc_auc": round(roc_auc_score(y, p), 3),
            "average_precision": round(average_precision_score(y, p), 3),
            "brier_skill_vs_base_rate": round(1 - brier_score_loss(y, p) / base, 3),
            "precision_at_recall_50": round(precision_at_recall(y, p, .5), 3),
            "precision_at_recall_80": round(precision_at_recall(y, p, .8), 3)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, default=Path("data/dataset_gefs"))
    p.add_argument("--observed", type=Path, default=Path("data/observed_precip_aorc/gefs_rows_observed.csv"))
    p.add_argument("--output-dir", type=Path, default=Path("data/baseline"))
    args = p.parse_args()
    rows = (pd.read_csv(args.dataset / "features.csv")
            .merge(pd.read_csv(args.dataset / "labels.csv")[["case_id", "target_crossing_72h"]], on="case_id", validate="one_to_one")
            .merge(pd.read_csv(args.dataset / "provenance.csv", usecols=["case_id", "water_year", "gefs_source"]), on="case_id")
            .merge(pd.read_csv(args.observed)[["case_id"] + OBS], on="case_id", how="left"))
    rows["is_operational"] = (rows.gefs_source == "operational").astype(float)
    y, years = rows.target_crossing_72h.to_numpy().astype(bool), rows.water_year.to_numpy()
    oof = pd.DataFrame({"case_id": rows.case_id, "water_year": years, "gefs_source": rows.gefs_source, "target": y.astype(int)})
    observed_rows = rows[OBS].notna().all(axis=1).to_numpy()
    for name, columns in FEATURE_SETS.items():
        usable = observed_rows if name == "flow_observed_rain" else np.ones(len(rows), bool)
        for model_name in models(columns):
            prediction = np.full(len(rows), np.nan)
            for year in np.unique(years):
                train, test = usable & (years != year), usable & (years == year)
                if not test.any():
                    continue
                model = models(columns)[model_name].fit(rows.loc[train, columns].to_numpy(), y[train])
                prediction[test] = model.predict_proba(rows.loc[test, columns].to_numpy())[:, 1]
            oof[f"{name}__{model_name}"] = prediction
    rule = np.zeros(len(rows), bool)
    thresholds = {}
    for year in np.unique(years):
        train, test = years != year, years == year
        a, b = fit_rule(rows.qpf_next_72h_mm.to_numpy()[train], rows.flow_last_cfs.to_numpy()[train], y[train])
        rule[test] = (rows.qpf_next_72h_mm.to_numpy()[test] >= a) & (rows.flow_last_cfs.to_numpy()[test] >= b)
        thresholds[int(year)] = {"qpf_72h_mm": round(float(a), 1), "flow_cfs": round(float(b), 1)}
    oof["rule_alert"] = rule.astype(int)
    tp = (rule & y).sum()
    report = {"validation": "leave-one-water-year-out; all scores pooled out-of-fold",
              "base_rate": round(float(y.mean()), 3),
              "rule_qpf72_and_flow": {"precision": round(tp / max(rule.sum(), 1), 3), "recall": round(tp / y.sum(), 3),
                                      "alerts": int(rule.sum()), "median_thresholds": {k: float(np.median([t[k] for t in thresholds.values()]))
                                                                                      for k in ["qpf_72h_mm", "flow_cfs"]}},
              "all_rows": {}, "rows_with_observed_rain": {}, "flow_gefs_by_source": {}}
    for column in [c for c in oof if "__" in c]:
        if not column.startswith("flow_observed_rain"):
            report["all_rows"][column] = scores(y, oof[column].to_numpy())
        report["rows_with_observed_rain"][column] = scores(y[observed_rows], oof[column].to_numpy()[observed_rows])
    for source in ["reforecast", "operational"]:
        mask = (rows.gefs_source == source).to_numpy()
        report["flow_gefs_by_source"][source] = {m: scores(y[mask], oof[f"flow_gefs__{m}"].to_numpy()[mask])
                                                 for m in ["logistic", "boosted_trees"]}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    oof.to_csv(args.output_dir / "oof_predictions.csv", index=False, float_format="%.5f")
    (args.output_dir / "metrics.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
