# Canyon Creek Streamflow Forecasting

Investigate predicting whether **Canyon Creek near Amboy, Washington (USGS 14219000)**
will cross from below to above **450 cfs within 72 hours**.

Current phase: **baseline modeling**; first cross-validated baselines are trained, nothing is deployed.
This project was separated from [WatershedForecast](https://github.com/JoeShmoJo/WatershedForecast).
See [origin and migration notes](ORIGIN.md).

## Progress

- Verified retrieval of actual archived operational NOAA/NWS precipitation forecasts.
- Audited six water years of forecast listings and USGS observations, identifying 138
  crossing days with at most one crossing per Pacific calendar day.
- Built and validated a full 2025 paired dataset: **258 daily rows, 19 positive targets
  representing seven of twelve observed crossing days**. Every scheduled decision is audited.
- Implemented fractional-area basin precipitation extraction, explicit timing assumptions,
  resumable acquisition, source hashes and offline regression checks.
- Audited water years 2008–2020 (**320 more crossing days**, 458 in total) and verified GEFSv12 reforecasts
  (2000–2019) as a pre-2020 forecast source; NCEI's older NDFD archive is irregular. See
  [extending the record](docs/older_forecast_sources.md).
- Built a **WY2008–2026 GEFSv12 dataset** ([`data/dataset_gefs/`](data/dataset_gefs/)): one 12Z
  decision per day from the 00Z control run, with full 72-hour forecast coverage. **4,146 passed rows,
  531 positive**, from the reforecast (2008–2019) and the operational archive (Sep 2020 onward). No
  GEFSv12 forecast exists for Jan–Sep 2020 (178 decisions). Built with
  `scripts/build_gefs_dataset.py` from ~15 GB of retained raw GRIB.
- Traced all 458 crossing days to dataset rows (`scripts/audit_gefs_event_losses.py`,
  [`data/dataset_gefs_event_losses.csv`](data/dataset_gefs_event_losses.csv)): **249 captured**,
  156 outside the target (flow already ≥450 cfs at every covering 12Z decision), 53 lost to flow
  gaps or the 2020 forecast gap.
- Extracted hourly observed basin precipitation for 2007–2025 from NOAA AORC v1.1 (1 km, 343 basin
  cells, 335 MiB of Zarr chunks; `scripts/build_aorc_basin_precip.py`,
  [`data/observed_precip_aorc/`](data/observed_precip_aorc/)) and compared it with the dataset QPF
  (`scripts/compare_gefs_aorc.py`). GEFS under-forecasts basin rain: 72-hour totals are **0.79× observed
  for the reforecast and 0.73× for the operational archive** (correlation 0.89 and 0.91). Annual ratios
  range 0.66–1.07 within the reforecast years, so the source difference is smaller than year-to-year
  spread but consistent in sign. A ±3-hour shift test peaks at zero, confirming AORC's hour-ending times.
- Trained first baselines (`scripts/train_baseline.py`, [`data/baseline/`](data/baseline/metrics.json)),
  validated leave-one-water-year-out (base rate 12.8%). Flow alone: average precision 0.47. **Flow + GEFS
  QPF (logistic regression): average precision 0.81, ROC AUC 0.96, 71% precision at 80% recall**, equally
  good on reforecast and operational years. Observed rain in place of the forecast lifts average precision
  only to 0.83–0.85, so forecast error is not the main limit. A hand rule — alert when 72 h QPF ≥ ~25 mm and
  flow ≥ ~54 cfs — gets 64% precision at 75% recall.

Read the [feasibility findings](docs/data_feasibility.md), [pilot report](docs/pilot_2025.md),
and [full-year results](docs/dataset_2025.md). The small processed datasets are in
[`data/`](data/dataset_2025/README.md); the Canyon Creek boundary is in
[`Basins/Canyon_Creek_WA/`](Basins/Canyon_Creek_WA/basin.geojson).

## Setup

Use Python 3.11 or newer. From this repository root:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -B -m unittest discover -s scripts -p 'test_*.py' -v
```

Full basin extraction additionally requires Git, a C compiler and the independent decoder:

```bash
bash scripts/setup_grib2io.sh
```

That helper builds a pinned NOAA g2c source and installs `requirements-basin.txt`.
Raw downloads, native builds and checkpoints live under ignored `.cache/`; they are
**not distributed by a Git clone**. Never commit API keys or credential files.

### Existing cloud workspace

The migration copied the existing raw caches into this folder and retained the working
shared Python environment. These checks have been run from the new repository:

```bash
source /workspace/venvs/watershed/bin/activate
python -B -m unittest discover -s scripts -p 'test_*.py' -v
python -B scripts/validate_pilot_2025.py
python -B scripts/validate_water_year_dataset.py
```

For a cached replay without altering the retained historical dataset:

```bash
python -B scripts/build_water_year_dataset.py --cached-only --output-dir .cache/migration-replay
python -B scripts/validate_water_year_dataset.py --directory .cache/migration-replay
```

### Fresh acquisition

The offline regression suite requires no source downloads. To recreate the inputs and
full-year dataset on another machine after installing the optional decoder:

```bash
python -B scripts/audit_water_year.py --water-year 2025
python -B scripts/audit_water_year_inventory.py --water-year 2025
python -B scripts/build_water_year_dataset.py --plan-only
python -B scripts/build_water_year_dataset.py
python -B scripts/validate_water_year_dataset.py
python -B scripts/summarize_water_year_dataset.py
```

The current full-year configuration limits new successful forecast payloads to 2 GiB
cumulatively across restarts. Public forecast S3 requests need no key. USGS access may
require `USGS_API_KEY`, provided securely through the environment. Upstream revisions
can change fresh input hashes; the committed reports describe the retained tested sources.

## Open questions

The GEFS dataset resolves forecast length (full 72 hours) and record length (18 usable water
years). Reforecast and operational QPF are close but not identical (operational ~7% drier relative to
AORC), so record `gefs_source` and check held-out errors by source; any model trained on observed rain
must account for GEFS's 20–30% low bias. Still open: the assumed 6-hour GEFS publication delay and historical flow revisions;
and the 156 crossings excluded because flow was already above 450 cfs at 12Z. Overlapping rows are
not independent events, so validate by held-out water year.
