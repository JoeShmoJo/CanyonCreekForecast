# Canyon Creek water-year 2025 paired data

See [the dataset report](../../docs/dataset_2025.md) for definitions and reproduction.

- `features.csv`: predictors with neutral IDs and decision times.
- `labels.csv`: observed 72-hour crossing outcomes and eligibility audit fields.
- `provenance.csv`: exact original forecast URLs/keys/hashes and assumed availability clocks.
- `qpf_intervals.csv`: basin-average six-hour forecast accumulations, including elapsed bins for audit.
- `audit.csv`: every scheduled decision and its inclusion/exclusion reason.
- `manifest.json`: completion status, source/code/config/output hashes and processing versions.
- `coverage_report.json`: monthly and event representation, and measured data volume.
- `cost_checks.json`: measured initial acquisition and byte-identical zero-download replay.

No predictive model is included. Overlapping targets are correlated; positive rows are
not independent crossing events. Raw grids and source observations remain in ignored caches.
