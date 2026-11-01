"""Pilot timing, label-boundary and leakage regression tests; no network/model."""
import unittest

import numpy as np
import pandas as pd

from build_pilot_2025 import flow_features, target_for_case, qpf_features, issue_clock, select_issue
from audit_crossings import audit


def flow():
    times=pd.date_range('2024-12-28T00:00Z',periods=10*96+1,freq='15min')
    frame=pd.DataFrame({'time_utc':times,'discharge_cfs':100.,'series_name':'primary','qualifiers':'Approved'})
    return frame.set_index('time_utc',drop=False)


def intervals(decision):
    return [{'reference_time_utc':(decision-pd.Timedelta(hours=5.5)).isoformat(),
             'accumulation_start_utc':(decision+pd.Timedelta(hours=6*i)).isoformat(),
             'valid_end_utc':(decision+pd.Timedelta(hours=6*(i+1))).isoformat(),
             'basin_mean_mm':100. if i==-1 else 1.} for i in range(-1,11)]


class PilotTests(unittest.TestCase):
    def test_future_flow_changes_do_not_change_predictors(self):
        t=pd.Timestamp('2025-01-02T06:00Z');before=flow()
        features,cutoff=flow_features(before,t)
        after=before.copy();after.loc[after.index>cutoff,'discharge_cfs']=9000.
        self.assertEqual(flow_features(after,t)[0],features)
        self.assertEqual(cutoff,t-pd.Timedelta(minutes=15))

    def test_qpf_excludes_elapsed_bin_and_does_not_invent_72h(self):
        t=pd.Timestamp('2025-01-02T06:00Z')
        features,coverage=qpf_features(intervals(t),t)
        self.assertEqual(features['qpf_next_6h_mm'],1)
        self.assertEqual(features['qpf_next_24h_mm'],4)
        self.assertEqual(features['qpf_next_60h_mm'],10)
        self.assertEqual(coverage['qpf_72h_coverage_hours'],66)
        self.assertFalse(coverage['qpf_72h_complete'])

    def test_overlapping_accumulations_fail(self):
        t=pd.Timestamp('2025-01-02T06:00Z');rows=intervals(t)
        rows.append(rows[3].copy())
        with self.assertRaises(ValueError):qpf_features(rows,t)

    def test_late_issue_rejected_while_future_valid_times_are_allowed(self):
        t=pd.Timestamp('2025-01-02T06:00Z');rows=intervals(t)
        self.assertEqual(issue_clock(rows,'wmo/qpf/2025/01/02/YIUZ98_KWBN_202501020016',t)['forecast_age_hours'],5.5)
        with self.assertRaises(ValueError):
            issue_clock(rows,'wmo/qpf/2025/01/02/YIUZ98_KWBN_202501020616',t)

    def test_target_excludes_decision_time_includes_72h_endpoint(self):
        t=pd.Timestamp('2025-01-02T06:00Z');data=flow()
        self.assertEqual(target_for_case(data,[{'time_utc':t.isoformat()}],t)['target_crossing_72h'],0)
        event={'time_utc':(t+pd.Timedelta(hours=72)).isoformat()}
        self.assertEqual(target_for_case(data,[event],t)['target_crossing_72h'],1)
        event={'time_utc':(t+pd.Timedelta(hours=72,minutes=15)).isoformat()}
        self.assertEqual(target_for_case(data,[event],t)['target_crossing_72h'],0)

    def test_prior_same_day_crossing_suppresses_later_oscillation(self):
        t=pd.Timestamp('2025-01-02T06:00Z');data=flow()
        data.loc[t-pd.Timedelta(hours=3),'discharge_cfs']=451.
        data.loc[t+pd.Timedelta(hours=1),'discharge_cfs']=451.
        events=audit(data.reset_index(drop=True))['events']
        self.assertEqual(target_for_case(data,events,t)['target_crossing_72h'],0)

    def test_missing_or_unapproved_history_is_not_imputed(self):
        t=pd.Timestamp('2025-01-02T06:00Z');data=flow()
        data=data.drop(t-pd.Timedelta(hours=1))
        with self.assertRaises(ValueError):flow_features(data,t)
        data=flow();data.loc[t-pd.Timedelta(hours=1),'qualifiers']='Provisional'
        with self.assertRaises(ValueError):flow_features(data,t)

    def test_issue_selection_is_fixed_and_ignores_sidecars(self):
        t=pd.Timestamp('2025-01-02T06:00Z')
        rows=[{'key':'wmo/qpf/2025/01/02/YIUZ98_KWBN_202501020016.aux.xml'},
              {'key':'wmo/qpf/2025/01/02/YIUZ98_KWBN_202501020046'},
              {'key':'wmo/qpf/2025/01/02/YIUZ98_KWBN_202501020016'}]
        self.assertTrue(select_issue(rows,t)['key'].endswith('0016'))


if __name__=='__main__':
    unittest.main()
