"""Offline tests for GEFSv12 source selection, index parsing, basin weights and flow merging."""
from pathlib import Path
import tempfile
import unittest

import geopandas as gpd
import numpy as np
from shapely.geometry import box

from build_gefs_dataset import combine_flow
from probe_gefs_reforecast import six_hour_ranges, latlon_weights, source_kind, operational_url, parse_index


def index_text(drop=None):
    lines, offset = [], 0
    for n, end in enumerate(range(3, 91, 3), start=1):
        start = end - 3 if end % 6 else end - 6
        if end != drop:
            lines.append(f"{n}:{offset}:d=2015111700:APCP:surface:{start}-{end} hour acc fcst:ENS=low-res ctl")
        offset += 1000 + n
    return "\n".join(lines) + "\n"


class GefsReforecastTests(unittest.TestCase):
    def test_selects_disjoint_six_hour_buckets_with_byte_ends(self):
        ranges = six_hour_ranges(index_text(), 6, 78)
        self.assertEqual([(s, e) for s, e, _, _ in ranges], [(h, h + 6) for h in range(6, 78, 6)])
        for _, _, offset, stop in ranges:
            self.assertGreater(stop, offset)

    def test_missing_bucket_fails_instead_of_shortening_window(self):
        with self.assertRaisesRegex(ValueError, "Missing six-hour buckets"):
            six_hour_ranges(index_text(drop=36), 6, 78)

    def test_rejects_non_precipitation_index(self):
        with self.assertRaisesRegex(ValueError, "Unexpected index line"):
            six_hour_ranges("1:0:d=2015111700:TMP:2 m above ground:0-3 hour:ENS=low-res ctl\n", 6, 78)

    def test_source_by_cycle_date(self):
        self.assertEqual(source_kind("2007100100"), "reforecast")
        self.assertEqual(source_kind("2019123100"), "reforecast")
        self.assertIsNone(source_kind("2020010100"))
        self.assertIsNone(source_kind("2020092200"))
        self.assertEqual(source_kind("2020092300"), "operational")
        self.assertIsNone(source_kind("2021111412"))

    def test_operational_lead_object_and_bucket(self):
        url = operational_url("2021111400", "c00", 84)
        self.assertTrue(url.endswith("gefs.20211114/00/atmos/pgrb2sp25/gec00.t00z.pgrb2s.0p25.f084"))
        self.assertIn("gep03.t00z", operational_url("2021111400", "p03", 12))
        text = ("15:100:d=2021111400:PRATE:surface:6-12 hour ave fcst:ENS=low-res ctl\n"
                "16:200:d=2021111400:APCP:surface:6-12 hour acc fcst:ENS=low-res ctl\n"
                "17:350:d=2021111400:TMP:2 m above ground:12 hour fcst:ENS=low-res ctl\n")
        self.assertEqual(parse_index(text, "2021111400"), {(6, 12): (200, 349)})
        with self.assertRaisesRegex(ValueError, "differs from cycle"):
            parse_index(text, "2021111500")

    def test_flow_merge_rejects_conflicting_overlap(self):
        header = "time_utc,discharge_cfs,qualifiers,series_name\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "2020.csv").write_text(header + "2020-10-01 00:00:00+00:00,10.0,Approved,s\n"
                                           "2020-10-01 00:15:00+00:00,11.0,Approved,s\n")
            (root / "2021.csv").write_text(header + "2020-10-01 00:15:00+00:00,11.0,Approved,s\n"
                                           "2020-10-01 00:30:00+00:00,12.0,Approved,s\n")
            merged = combine_flow(str(root / "{year}.csv"), [2020, 2021], root / "out.csv")
            self.assertEqual(len(merged.read_text().splitlines()), 4)
            (root / "2021.csv").write_text(header + "2020-10-01 00:15:00+00:00,99.0,Approved,s\n")
            with self.assertRaisesRegex(ValueError, "disagree"):
                combine_flow(str(root / "{year}.csv"), [2020, 2021], root / "out.csv")

    def test_weights_cover_basin_spanning_four_cells(self):
        lats, lons = np.meshgrid(np.arange(46.5, 45.24, -.25), np.arange(237.0, 238.26, .25), indexing="ij")
        boundary = gpd.GeoDataFrame(geometry=[box(-122.35, 45.8, -122.05, 46.05)], crs="EPSG:4326")
        indices, areas, basin_area = latlon_weights(lats, lons, boundary)
        self.assertEqual(len(indices), 4)
        self.assertAlmostEqual(areas.sum() / basin_area, 1, places=4)


if __name__ == "__main__":
    unittest.main()
