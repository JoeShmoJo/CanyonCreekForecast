"""Offline tests for GEFSv12 reforecast index selection and lat/lon basin weights."""
import unittest

import geopandas as gpd
import numpy as np
from shapely.geometry import box

from probe_gefs_reforecast import six_hour_ranges, latlon_weights


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

    def test_weights_cover_basin_spanning_four_cells(self):
        lats, lons = np.meshgrid(np.arange(46.5, 45.24, -.25), np.arange(237.0, 238.26, .25), indexing="ij")
        boundary = gpd.GeoDataFrame(geometry=[box(-122.35, 45.8, -122.05, 46.05)], crs="EPSG:4326")
        indices, areas, basin_area = latlon_weights(lats, lons, boundary)
        self.assertEqual(len(indices), 4)
        self.assertAlmostEqual(areas.sum() / basin_area, 1, places=4)


if __name__ == "__main__":
    unittest.main()
