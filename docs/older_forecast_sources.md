# Extending the record before April 2020

Verified November 1, 2026 on a local WSL Ubuntu workstation. No model has been trained.
The target is unchanged: from a decision with observed flow strictly below 450 cfs, predict an
upward crossing to strictly above 450 cfs within 72 hours (one crossing per Pacific day).

## Summary

| Source | Period | Verified | Use |
| --- | --- | --- | --- |
| USGS 14219000 instantaneous flow | WY2008–2018 | 11 annual audits, all observations approved | **272 crossing days**, about double the 2021–2026 count |
| GEFSv12 reforecast (AWS) | 2000–2019, daily 00Z, 5 members | Complete cycle inventory; actual files fetched, decoded twice and basin-averaged | Main candidate for pre-2020 forecast input |
| NDFD old archive (NCEI THREDDS) | June 2018 – May 2020 | Weekly catalog sample (122 days) | Irregular issue times; usable only with a later decision time |

The 2025 dataset was also rebuilt from fresh downloads on this machine. Labels and audit rows were
identical; forecast-derived values differed by at most 3.9e-16 relative (floating-point summation order);
all 258 source hashes matched.

## Streamflow, water years 2008–2018

`scripts/audit_water_year.py` supports water years from 2008. Its existing rules were applied
unchanged. Reports are in `feasibility_evidence/water_year_<year>_flow.json`.

| Water year | Crossing days | Raw upcrossings | Peak cfs |
| --- | ---: | ---: | ---: |
| 2008 | 26 | 34 | 5,620 |
| 2009 | 11 | 17 | 7,570 |
| 2010 | 20 | 25 | 3,070 |
| 2011 | 32 | 93 | 6,390 |
| 2012 | 20 | 48 | 5,740 |
| 2013 | 36 | 87 | 3,910 |
| 2014 | 28 | 53 | 4,370 |
| 2015 | 19 | 38 | 4,080 |
| 2016 | 16 | 45 | 9,390 |
| 2017 | 39 | 97 | 4,670 |
| 2018 | 25 | 54 | 8,350 |
| **Total** | **272** | | |

Water years 2019 and 2020 were interrupted by USGS rate limiting (HTTP 429, no API key) and are
recorded separately once retried. Many crossings are recorded at 452 cfs, just above the threshold, so
marginal events are common.

## GEFSv12 reforecast

Bucket `noaa-gefs-retrospective`, prefix `GEFSv12/reforecast/<year>/<YYYYMMDD00>/<member>/Days:1-10/`.
Each cycle has members `c00` and `p01`–`p04`. The full cycle listing is in `feasibility_evidence/gefs/reforecast_cycles.txt`:
every day from 2000-01-01 to 2019-12-31 is present. The apparent gaps reported for 2002/2006 are
next-year cycles misfiled under the earlier year's prefix, and the 2020 prefix holds one misfiled 2010 cycle;
no reforecast exists after 2019.

`scripts/probe_gefs_reforecast.py`:

1. Reads the `.idx` sidecar of `apcp_sfc_<cycle>_<member>.grib2` and selects only disjoint six-hour
   buckets covering the requested window (default f006–f078, i.e. 72 hours from 06Z). A missing bucket fails.
2. Fetches those twelve messages by HTTP byte range (about 3.6 MB instead of 25 MB per member-day).
3. Decodes with grib2io and independently with ecCodes; values and missing masks must agree within 1e-4 mm.
4. Weights regular 0.25° cells by exact intersection area with the repository basin polygon in EPSG:5070,
   using the declared 6,371,229 m sphere. Four cells cover the basin (fractions 0.655, 0.189, 0.103, 0.052).

| Cycle (00Z) | Context | c00 basin total, f006–f078 |
| --- | --- | ---: |
| 2015-10-30 | Crossing 19:45 UTC that day; atmospheric river | 187.9 mm |
| 2016-02-25 | Marginal crossing (452 cfs) | 14.6 mm |
| 2016-04-24 | Marginal crossing (469 cfs) | 14.2 mm |
| 2016-07-15 | Dry-season control | 3.8 mm |

All five members for 2015-10-30 gave 161.8–205.7 mm. Maximum decoder difference was 1.2e-5 mm.
Outputs are in `feasibility_evidence/gefs/`. These are plausibility checks, not skill estimates.

Caveats:

- **Resolution:** 4 coarse cells versus 46 NDFD cells. Orographic enhancement over the basin is smoothed.
- **Different system:** GEFS is a raw ensemble model; NDFD is forecaster-edited. Their biases differ, so a
  model trained on GEFS cannot be assumed to transfer to NDFD inputs without calibration over a common period.
- **Timing:** Reforecasts exist only for 00Z. Operational GEFS output is published several hours after
  cycle time (historical delivery times not verified here), so pairing a 00Z run with a 06Z decision is
  optimistic. A 12Z decision is more defensible.
- **Volume:** one member for WY2008–2019 is about 4,400 cycles × 3.6 MB ≈ 16 GB; five members ≈ 80 GB.

## NDFD at NCEI, June 2018 – May 2020

The old NCEI THREDDS catalog (`model-ndfd-file_kwbn-old/<YYYYMM>/<YYYYMMDD>/catalog.xml`) was sampled every
seventh day from 2018-06-05 to 2020-09-29 (122 days; `feasibility_evidence/ncei_weekly_sample.json`).
Each daily catalog is about 3.4 MB, so the sample was kept weekly.

- Same-day CONUS QPF (`YIUZ98_KWBN`) issue counts were typically 30–48, but as low as 15 in summer 2018.
- An issue in the 00:00–01:00 UTC hour, required by the current 2025 selection rule, existed on only about
  half of sampled days and on almost none in winter (0/5 January 2019, 0/5 December 2019, 0/4 February 2020).
- The first issue was often 03:00–05:00 UTC and occasionally 05:46, 06:15, 08:17 or 13:48 UTC.
- Before the catalog ended, two sampled days had no issues: 2019-06-25 and 2019-11-19. The catalog ends in
  May 2020; June–September 2020 dates returned no catalog.

NCEI cannot reproduce the 2025 selection rule. With a rule such as "latest issue at least one hour before
the decision, no older than N hours" and a 12Z decision, most sampled days would have a usable forecast.
Its main value is the overlap with GEFS (June 2018 – December 2019) for comparing the two systems.

## Proposed next decisions

1. **Decision time:** move to 12Z (04:00/05:00 Pacific) for all sources, so 00Z GEFS and irregular NCEI
   issues are realistically available. This changes the 2025 dataset definition and requires a rebuild.
2. **Forecast source by period:** GEFS c00 for WY2008–2019; NDFD (AWS) for WY2021–2026; WY2020 either GEFS
   through December 2019 then NDFD, or excluded.
3. **Bias bridge:** acquire operational GEFSv12 for the NDFD years as well, so the same model input exists
   in every year and NDFD is an additional feature rather than a replacement. Bucket `noaa-gefs-pds` has
   daily folders from 2017-01-01; the GEFSv12 layout (`gefs.<date>/<cycle>/atmos/pgrb2sp25/`, 0.25°) is
   present from 2020-09-23. Earlier 2017–2020 folders are the previous GEFS version. A consistent GEFSv12
   series therefore covers 2000-01-01 to 2019-12-31 and 2020-09-23 onward, leaving January–September 2020.
   The operational precipitation files and their indexes have not yet been fetched or decoded.
