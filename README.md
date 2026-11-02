# Canyon Creek Streamflow Forecasting

Investigate predicting whether **Canyon Creek near Amboy, Washington (USGS 14219000)**
will cross from below to above **450 cfs within 72 hours**.

Current phase: **data discovery and feasibility; no predictive model has been trained**.
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

## Before modeling

Review event losses from daily scheduling and observation-quality rules; assess assumed
publication/feed delays and historical flow revisions; decide how to handle the selected
forecasts' 66-hour future coverage; then validate more years. Overlapping rows are not
independent events. Ten years of usable actual forecasts and homogeneous historical
forecast quality have not been established.
