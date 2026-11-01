# Canyon Creek water-year 2025 paired dataset

This expands the [twelve-row pilot](pilot_2025.md) to a fixed daily schedule, without
training a model. The decision time is 06:00 UTC every day inside the **Pacific** water
year. Consequently the UTC dates run from October 2, 2024 through October 1, 2025.
Every scheduled decision is retained in the audit, including exclusions.

## Verified results

The full build completed successfully. All 258 selected operational NDFD bulletins were
retrieved or reused from verified local sources, decoded, averaged over the basin, and paired.
No model was trained.

| Measure | Result |
| --- | ---: |
| Scheduled daily decisions | 365 |
| Paired rows / predictor columns | 258 / 18 |
| Positive rows | 19 (7.36% of eligible rows) |
| Observed crossing days in this water year | 12 |
| Crossing days represented by positive rows | 7 |
| Excluded: last available flow at least 450 cfs | 83 |
| Excluded: incomplete observation windows | 23 |
| Excluded: no required early forecast issue | 1 |
| Additional forecast-content exclusions / processing errors | 0 / 0 |

The 19 positive rows represent seven crossing days, not 19 independent events. These
are data-coverage counts, not a model's event recall. The five omitted crossing days
have these pre-event daily eligibility results (dates are Pacific):

| Crossing day | Why no paired pre-event decision |
| --- | --- |
| 2024-12-16 | All three decisions have incomplete required observation windows |
| 2025-01-09 | One incomplete window; last available flow already above threshold for the other two |
| 2025-01-31 | All three decisions have incomplete required observation windows |
| 2025-04-04 | Last available flow already above threshold at all three daily decisions |
| 2025-04-13 | Last available flow already above threshold at all three daily decisions |

The represented dates are November 1/12, December 7, February 17, March 15/20 and April 7.
Eligibility losses concentrate in the wet season: only six of December's 31 daily decisions
remain, versus all 31 in May and July. The source archive itself did not cause these flow
exclusions. Full per-month and per-event counts are in `coverage_report.json`.

All selected GRIB references are 00:30 UTC, giving a 5.5-hour age at the decision. All
have 66 hours of future precipitation coverage, approximately 100% valid basin area,
and a largest independent-decoder difference of **0.00001160 mm** across the annual build.

Validation recomputed all exported flow features, forecast interval sums and crossing labels,
checked source/output hashes and assumed clocks, and accounted for all 365 scheduled decisions.
**35 offline regression tests passed.** A separate cached replay reproduced all five CSVs
byte-for-byte, resumed all 258 rows, and downloaded zero bytes.

Measured acquisition: **246 new files / 1,304,328,505 bytes (1.215 GiB)**, with twelve
previously cached files reused. All 258 raw sources total **1.280 GiB**. The initial run took
**730 seconds** including retrieval and extraction; warm replay took **1.88 seconds** including
Python startup. The five CSV exports total about **925 KiB**. `cost_checks.json` records these
measurements. This distinguishes raw download/storage cost from the small basin-level dataset.

## Data contract

The [configuration](../configs/dataset_2025.json) fixes the pilot's timing rules:

- Use the earliest canonical CONUS QPF bulletin named between 00:00 and 01:00 UTC
  on the decision date. Validate its GRIB reference time and accumulation intervals;
  require assumed publication at least one hour after the later filename/reference time
  and an issue age no greater than six hours.
- Use approved quarter-hour discharge only, ending fifteen minutes before the decision.
  Require complete antecedent 72-hour coverage. Last available flow must be below 450 cfs.
- Require complete approved observations from current Pacific midnight through +72h
  to label the first crossing that day. Actual flow at the decision must also be below
  450 as a retrospective eligibility check, exported with labels rather than predictors.
- Label an observed transition strictly below to strictly above 450 in `(t,t+72h]`;
  equality retains the preceding state, gaps break continuity, and only the first crossing
  per Pacific calendar day counts.
- Compute fractional-area basin averages using the repository's Canyon Creek polygon,
  intersecting 46 forecast cells. Require at least 99% valid basin area and independent
  decoder agreement. Accumulate only bins entirely within the requested future window.

Predictors contain antecedent discharge, seasonal sine/cosine, forecast age, and exact
future 6/12/24/48/60-hour precipitation totals. Selected forecasts ordinarily cover only
66 hours beyond 06Z, so there is **no fabricated 72-hour precipitation total**.

The daily schedule is fixed independently of the outcome. Eligibility depends on both
antecedent and future observation quality, so exclusions can still change event representation.
These are historical final discharge values: publication delays and revisions have not been
independently reconstructed. The assumed timing policy needs operational validation before
claiming a prospective hindcast or evaluating a deployable model.

## Files and reproducibility

[`data/dataset_2025/`](../data/dataset_2025/) contains predictors, separate labels, source
provenance, six-hour basin precipitation intervals, a 365-decision audit and a manifest.
`coverage_report.json` reports crossing representation and monthly coverage. Raw grids,
extraction caches, the acquisition ledger and per-row checkpoints stay in ignored `.cache/`.

From the repository root in this cloud environment:

```bash
source /workspace/venvs/watershed/bin/activate
python -B scripts/build_water_year_dataset.py --plan-only --cached-only
python -B scripts/build_water_year_dataset.py
python -B scripts/validate_water_year_dataset.py
python -B scripts/summarize_water_year_dataset.py
python -B -m unittest discover -s scripts -p 'test_*.py' -v
```

The build stops on the first unexpected processing/network error, writes an incomplete
manifest, and returns nonzero. Run the same command to resume. Completed rows are reused
only when their input/code/config fingerprint and raw-source hash match. No authentication
is needed for the public S3 requests. New successful source payloads are capped cumulatively
at 2 GiB, rather than resetting the download allowance on each restart.

For a zero-network replay and validation in a separate output directory:

```bash
python -B scripts/build_water_year_dataset.py --cached-only --output-dir .cache/dataset_2025/replay
python -B scripts/validate_water_year_dataset.py --directory .cache/dataset_2025/replay
```

The original and replay manifests' `output_sha256` mappings must match for all five CSVs.

On a fresh machine, install `requirements.txt`, build the optional decoder using
`bash scripts/setup_grib2io.sh`, and recreate the flow/inventory prerequisites:

```bash
python -B scripts/audit_water_year.py --water-year 2025
python -B scripts/audit_water_year_inventory.py --water-year 2025
python -B scripts/build_water_year_dataset.py
```

The last decision requires the October 1, 2025 UTC inventory; the builder uses an existing
cached adjacent-year listing when available, or retrieves that one extra day. The USGS
audit honors `USGS_API_KEY` if provided. API availability, upstream revisions and access
limits may affect a fresh download; retained response/source hashes identify the tested inputs.

## Interpretation and next gate

One year is a pipeline test, not enough data to establish forecast skill. Daily rows overlap
in their 72-hour labels, and distinct crossing days can belong to the same storm. A future
evaluation should group storm episodes, split chronologically and purge overlapping windows.
The 2025 results also cannot establish that older archive contents and forecast skill are
homogeneous; additional years need the same GRIB/time/coverage checks.

Keep the current conservative observation policy explicit rather than filling missing flow
or substituting realized rain. Before expanding acquisition, review how much event coverage
the strict quality windows and one-decision-per-day schedule remove. Additional within-day
decisions may recover brief below-threshold opportunities but produce more correlated rows;
they do not create additional independent storms.

Delivery-latency assumptions, final-versus-contemporaneous flow revisions, acceptance or
remediation of the 66-hour QPF coverage, and adequate multi-year event counts need review
before model development.
Public S3 access is demonstrated by actual file retrieval; a ten-year usable actual-forecast
dataset remains unverified. See the broader [data feasibility findings](data_feasibility.md).
