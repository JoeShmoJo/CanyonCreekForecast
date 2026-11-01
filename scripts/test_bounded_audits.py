"""Regression checks for event semantics and basin extraction safeguards."""
import unittest
from unittest.mock import patch
from contextlib import chdir, redirect_stdout
import io
import json
from pathlib import Path
import tempfile

# See probe_basin_qpf.py: initialize PROJ before the ecCodes native dependencies.
import pyproj
import numpy as np
import pandas as pd

from audit_crossings import audit
from audit_water_year import combine_frames
from test_usgs import api_headers, parse_continuous
from test_qpf_archive import is_wmo_bulletin
import audit_water_year
import audit_water_year_inventory
from audit_water_year_inventory import filename_diagnostics, NetworkCircuitBreaker
import requests
from probe_basin_qpf import weighted_mean


def frame(times, flows):
    return pd.DataFrame({"time_utc": pd.to_datetime(times, utc=True).as_unit("us"),
                         "discharge_cfs": flows, "series_name": "primary", "qualifiers": "Approved"})


class CrossingTests(unittest.TestCase):
    def test_equality_plateau_and_daily_deduplication(self):
        times = pd.date_range("2025-01-01T07:30Z", periods=8, freq="15min")
        r = audit(frame(times, [449, 450, 450, 451, 450, 451, 449, 452]))
        self.assertEqual(r["raw_upcrossings"], 2)
        self.assertEqual(r["deduplicated_crossings"], 1)
        self.assertEqual(r["same_day_repeats_removed"], 1)
        self.assertEqual(r["events"][0]["local_day"], "2025-01-01")

    def test_gap_does_not_create_crossing(self):
        r = audit(frame(["2025-01-02T08:00Z", "2025-01-02T08:30Z"], [449, 451]))
        self.assertEqual(r["raw_upcrossings"], 0)
        self.assertEqual(r["above_threshold_segment_starts_with_unknown_crossing"], 1)

    def test_missing_value_does_not_create_crossing(self):
        times = pd.date_range("2025-01-01T07:30Z", periods=4, freq="15min")
        self.assertEqual(audit(frame(times, [449, np.nan, 451, 452]))["raw_upcrossings"], 0)

    def test_microsecond_datetimes_and_complete_future_windows(self):
        times = pd.date_range("2025-01-01T00:00Z", periods=481, freq="15min")
        flows = np.full(481, 449.0)
        flows[288] = 451
        data = frame(times, flows)
        r = audit(data)
        # The initial partial Pacific calendar day is excluded from decisions.
        self.assertEqual(r["complete_72h_hourly_decisions_below_450"], 41)
        self.assertEqual(r["decisions_with_future_deduplicated_crossing"], 41)
        # Removing one observation invalidates every window spanning it.
        self.assertEqual(audit(data.drop(index=96))["complete_72h_hourly_decisions_below_450"], 17)

    def test_prior_same_day_gap_censors_first_crossing_target(self):
        times = pd.date_range('2025-01-01T00:00Z',periods=481,freq='15min')
        flows = np.full(481,449.)
        flows[112] = 451  # Jan 2 04Z, still Jan 1 in Pacific local time.
        data = frame(times,flows).drop(index=96)  # Earlier gap on that same local day.
        result = audit(data,period_start=pd.Timestamp('2025-01-02T01:00Z'),
                       period_end=pd.Timestamp('2025-01-02T02:00Z'))
        self.assertEqual(result['complete_72h_hourly_decisions_below_450'],0)
        self.assertEqual(result['hourly_decisions_censored_for_incomplete_local_day_history'],1)

    def test_scoped_decisions_use_future_events_beyond_period_end(self):
        times = pd.date_range("2025-01-01T00:00Z", periods=481, freq="15min")
        flows = np.full(481, 449.0)
        flows[288] = 451
        r = audit(frame(times, flows), period_start=pd.Timestamp("2025-01-01T08:00Z"),
                  period_end=pd.Timestamp("2025-01-02T08:00Z"))
        self.assertEqual(r["deduplicated_crossings"], 0)
        self.assertEqual(r["complete_72h_hourly_decisions_below_450"], 24)
        self.assertEqual(r["decisions_with_future_deduplicated_crossing"], 24)

    def test_fall_clock_change_deduplicates_by_local_date(self):
        times = pd.date_range("2025-11-02T06:45Z", periods=17, freq="15min")
        flows = np.full(17, 449.0)
        flows[[3, 10]] = 451
        r = audit(frame(times, flows))
        self.assertEqual(r["raw_upcrossings"], 2)
        self.assertEqual(r["deduplicated_crossings"], 1)
        self.assertEqual(r["events"][0]["local_day"], "2025-11-02")

    def test_month_endpoints_remove_only_identical_duplicates(self):
        times = pd.date_range("2025-01-01T00:00Z", periods=3, freq="15min")
        data = frame(times, [449, 450, 451])
        self.assertEqual(len(combine_frames([data.iloc[:2], data.iloc[1:]])), 3)
        changed = data.iloc[1:].copy()
        changed.loc[1, "discharge_cfs"] = 449
        with self.assertRaises(ValueError):
            combine_frames([data.iloc[:2], changed])


class BasinTests(unittest.TestCase):
    def test_constant_field_invariance(self):
        mean, coverage = weighted_mean(np.array([7., 7.]), np.array([2., 3.]), 5.)
        self.assertEqual(mean, 7)
        self.assertEqual(coverage, 1)

    def test_missing_area_cannot_be_silently_zero(self):
        with self.assertRaises(ValueError):
            weighted_mean(np.array([7., np.nan]), np.array([2., 3.]), 5.)


class InventoryTests(unittest.TestCase):
    def test_metadata_sidecars_are_not_forecast_bulletins(self):
        key='wmo/qpf/2021/02/04/YIUZ98_KWBN_202102041619'
        self.assertTrue(is_wmo_bulletin(key))
        self.assertFalse(is_wmo_bulletin(key+'.aux.xml'))
        result=filename_diagnostics([{'date':'2021-02-04','objects':[{'key':key},{'key':key+'.aux.xml'}]}])
        self.assertEqual(result['unique_bulletin_filenames'],1)
    def test_list_quality_flags_are_preserved(self):
        payload={'features':[{'properties':{'monitoring_location_id':'USGS-14219000','parameter_code':'00060',
            'unit_of_measure':'ft^3/s','value':'400','time':'2023-01-01T00:00:00Z',
            'time_series_id':'primary','approval_status':'Approved','qualifier':['ESTIMATED']}}]}
        result=parse_continuous(payload)
        self.assertEqual(result.qualifiers.iloc[0],'Approved,ESTIMATED')
    def test_optional_usgs_key_is_scoped_to_https_api_host(self):
        with patch.dict('os.environ',{'USGS_API_KEY':'unit-test-value'}):
            self.assertEqual(api_headers('https://api.waterdata.usgs.gov/ogcapi/v1/collections'),{'X-Api-Key':'unit-test-value'})
            self.assertEqual(api_headers('https://waterservices.usgs.gov/nwis/site/'),{})
            self.assertEqual(api_headers('https://noaa-ndfd-pds.s3.amazonaws.com/'),{})
            self.assertEqual(api_headers('http://api.waterdata.usgs.gov/'),{})
    def test_inventory_stops_requesting_after_gateway_failures(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            with patch('sys.argv',['audit','--water-year','2025','--workers','1']), \
                 patch.object(audit_water_year_inventory,'list_objects',side_effect=requests.HTTPError('503 gateway')) as listing, \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(audit_water_year_inventory.main(),1)
            result = json.loads(Path('.cache/feasibility/water-year-2025/inventory/report.json').read_text())
            self.assertEqual(listing.call_count,3)
            self.assertEqual(result['dates_not_requested_after_network_failure'],362)
            self.assertTrue(result['network_circuit_breaker_open'])

    def test_missing_month_endpoint_is_reported_as_a_gap_not_an_abort(self):
        def sample(session,url,params,max_bytes):
            start,end = [pd.Timestamp(x) for x in params['datetime'].split('/')]
            if start.month==10:
                start += pd.Timedelta(minutes=15)
            observations = [{'type':'Feature','properties':{
                'monitoring_location_id':'USGS-14219000','parameter_code':'00060',
                'unit_of_measure':'ft^3/s','value':'400','time':t.isoformat(),
                'time_series_id':'primary','approval_status':'Approved','qualifier':None}}
                for t in [start,end]]
            return json.dumps({'type':'FeatureCollection','features':observations,'links':[]}),url
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            with patch('sys.argv',['audit','--water-year','2025']), \
                 patch.object(audit_water_year,'fetch',side_effect=sample), redirect_stdout(io.StringIO()):
                self.assertEqual(audit_water_year.main(),0)
            result = json.loads(Path('.cache/feasibility/water-year-2025/report.json').read_text())
            self.assertFalse(result['requests'][0]['start_endpoint_present'])
            self.assertTrue(result['gaps'])
            self.assertLess(result['period_observations_returned'],result['period_observations_expected'])

    def test_network_circuit_breaker_stops_after_repeated_transport_failures(self):
        circuit = NetworkCircuitBreaker(3)
        circuit.record(ValueError('No matching product in a valid listing'))
        self.assertFalse(circuit.stopped.is_set())
        circuit.record(requests.ConnectionError('transport failure'))
        circuit.record(requests.HTTPError('503'))
        self.assertFalse(circuit.stopped.is_set())
        circuit.record(requests.ConnectionError('transport failure'))
        self.assertTrue(circuit.stopped.is_set())
        self.assertEqual(circuit.failures,3)
    def test_repeated_bulletin_in_later_folder_is_not_a_new_forecast(self):
        name = 'YIUZ98_KWBN_202502042116'
        rows = [{'date':day,'objects':[{'key':f'wmo/qpf/{day.replace("-","/")}/{name}'}]}
                for day in ['2025-02-04','2025-02-06']]
        result = filename_diagnostics(rows)
        self.assertEqual(result['unique_bulletin_filenames'],1)
        self.assertEqual(len(result['repeated_bulletin_filenames']),1)
        self.assertEqual(len(result['filename_date_folder_mismatches']),1)


if __name__ == "__main__":
    unittest.main()
