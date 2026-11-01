#!/usr/bin/env python3
"""Offline regression checks. Synthetic inputs do not validate any archive."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import eccodes as ec
import numpy as np

from test_qpf_archive import decode_grib, get_small, list_objects
from test_usgs import parse_continuous, parse_discharge, summarize


class FeasibilityTests(unittest.TestCase):
    def test_accumulation_times_and_domain(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "synthetic.grib2"
            handle = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
            try:
                for key, value in {"Ni": 3, "Nj": 3, "latitudeOfFirstGridPointInDegrees": 46.1,
                                   "latitudeOfLastGridPointInDegrees": 45.7,
                                   "longitudeOfFirstGridPointInDegrees": 237.4,
                                   "longitudeOfLastGridPointInDegrees": 237.8,
                                   "iDirectionIncrementInDegrees": .2,
                                   "jDirectionIncrementInDegrees": .2,
                                   "dataDate": 20211114, "dataTime": 0,
                                   "stepType": "accum", "startStep": 0, "endStep": 6,
                                   "discipline": 0, "parameterCategory": 1,
                                   "parameterNumber": 8}.items():
                    ec.codes_set(handle, key, value)
                ec.codes_set_values(handle, np.ones(9))
                with path.open("wb") as stream:
                    ec.codes_write(handle, stream)
            finally:
                ec.codes_release(handle)
            messages = decode_grib(path)
            self.assertEqual(len(messages), 1)
            row = messages[0]
            self.assertEqual((row["dataDate"], row["dataTime"]), (20211114, 0))
            self.assertEqual((row["startStep"], row["endStep"], row["validityTime"]), (0, 6, 600))
            self.assertTrue(row["precipitation_candidate"])
            self.assertTrue(row["amboy_point_check"]["inside_coordinate_envelope"])

    def test_error_page_is_not_grib(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "error.html"
            path.write_text("<html>not a forecast</html>")
            with self.assertRaises(ValueError):
                decode_grib(path)

    def test_transfer_budget_without_content_length(self):
        response = Mock(headers={}, url="https://example.org/data")
        response.iter_content.return_value = iter([b"123", b"456"])
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        session = Mock()
        session.get.return_value = response
        with self.assertRaises(ValueError):
            get_small(session, response.url, 5)

    def test_s3_truncation_is_preserved(self):
        body = b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>true</IsTruncated><Contents><Key>wmo/qpf/a</Key><Size>10</Size></Contents></ListBucketResult>'
        response = Mock(headers={}, url="https://example.org/")
        response.iter_content.return_value = iter([body])
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        session = Mock()
        session.get.return_value = response
        result = list_objects(session, response.url, "wmo/qpf/")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["objects"][0]["size"], 10)

    def test_usgs_missing_values_threshold_and_gap(self):
        values = [{"dateTime": f"2021-11-14T{h:02}:00:00Z", "value": str(v), "qualifiers": ["A"]}
                  for h, v in [(0, 450), (1, 451), (2, -999999), (4, 500)]]
        payload = {"value": {"timeSeries": [{"name": "test", "sourceInfo": {"siteCode": [{"value": "14219000"}]},
                    "variable": {"variableCode": [{"value": "00060"}], "unit": {"unitCode": "ft3/s"}, "noDataValue": -999999},
                    "values": [{"value": values}]}]}}
        summary = summarize(parse_discharge(payload))
        self.assertEqual(summary["observations_above_threshold"], 2)
        self.assertEqual(summary["missing_or_invalid_values"], 1)
        self.assertEqual(summary["gaps_over_1_5_nominal"], 1)
        self.assertEqual(summary["complete_72h_future_windows_in_sample"], 0)

    def test_modern_api_pagination_is_not_complete_data(self):
        with self.assertRaises(ValueError):
            parse_continuous({"features": [], "links": [{"rel": "next", "href": "https://example.org/page2"}]})

    def test_modern_api_units_checked(self):
        with self.assertRaises(ValueError):
            parse_continuous({"features": [{"properties": {
                "monitoring_location_id": "USGS-14219000", "parameter_code": "00060",
                "unit_of_measure": "m3/s"}}]})


if __name__ == "__main__":
    unittest.main()
