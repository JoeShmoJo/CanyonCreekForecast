# Project origin and migration

This project originated in `JoeShmoJo/WatershedForecast`, which remains a separate project.
The Canyon Creek feasibility scripts, reports, small derived datasets and basin boundary
were copied into this repository on November 1, 2026. The original checkout was preserved.

The source checkout was at commit `7dcffd0f` (the full identifier is recorded in
[`docs/migration.json`](docs/migration.json)). The Canyon Creek research files were
uncommitted working files at migration, so they do not belong to that historical commit.
This repository's initial commit preserves them and records hashes of the copied files.

The basin polygon and basin illustration come from `Basins/Canyon_Creek_WA/` in the source
repository. No additional code license has been invented or applied during migration;
no license file was present in the source checkout. Upstream dependencies retain their licenses.
Source observations and forecasts come from the public USGS and NOAA/NWS services identified
in the evidence reports; generated datasets retain source keys, URLs and checksums.

Copied scripts/configurations and all processed CSVs were preserved byte-for-byte. The annual
dataset manifest's `pipeline_source` was changed from the original absolute workspace path
to a repository-relative path. Its code and output hashes were preserved. Dependencies were
narrowed to those used by this project; the independent decoder remains optional for offline tests.

Raw downloads and checkpoints were copied into this repository's ignored `.cache/` for local
continuation without new downloads. They remain outside Git; a fresh clone must recreate
them using the documented retrieval commands. No predictive model or original WatershedForecast
application, generated website, unrelated basin or application automation was copied.
