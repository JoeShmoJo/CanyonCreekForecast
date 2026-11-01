#!/usr/bin/env python3
"""Recompute exported pilot features/labels and check clocks/hashes, without network access."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from build_pilot_2025 import (JOIN_COLUMNS,sha,load_flow,flow_features,target_for_case,
                             qpf_features,issue_clock)
from audit_crossings import audit


def validate(directory):
    manifest=json.loads((directory/'manifest.json').read_text())
    if not manifest['complete'] or manifest['model_trained']:raise ValueError('Need a complete untrained diagnostic pilot')
    config=manifest['config']
    assert sha(Path(manifest['config_source']))==manifest['config_sha256']
    pipeline=Path(manifest.get('pipeline_source',Path(__file__).with_name('build_pilot_2025.py')))
    assert sha(pipeline)==manifest['pipeline_sha256']
    if 'shared_pairing_rules_sha256' in manifest:
        assert sha(Path(__file__).with_name('build_pilot_2025.py'))==manifest['shared_pairing_rules_sha256']
    assert sha(Path(__file__).with_name('probe_basin_qpf.py'))==manifest['extractor_sha256']
    assert sha(Path(__file__).with_name('audit_crossings.py'))==manifest['target_rules_sha256']
    assert sha(Path(config['flow_csv']))==manifest['flow_csv_sha256']
    assert sha(Path(config['boundary']))==manifest['boundary_sha256']
    for name,digest in manifest['output_sha256'].items():assert sha(directory/name)==digest,name
    features=pd.read_csv(directory/'features.csv')
    labels=pd.read_csv(directory/'labels.csv')
    provenance=pd.read_csv(directory/'provenance.csv')
    tidy=pd.read_csv(directory/'qpf_intervals.csv')
    expected={x['case_id'] for x in manifest['cases'] if x['status']=='passed'}
    assert len(features)==len(labels)==len(provenance)==len(expected)
    if 'cases' in config:assert len(features)==len(config['cases'])
    if 'rows_paired' in manifest:assert len(features)==manifest['rows_paired']
    for table in [features,labels,provenance]:
        assert not table.case_id.duplicated().any()
        assert set(table.case_id)==expected
    assert [c for c in features.columns if c not in JOIN_COLUMNS]==manifest['predictor_columns']
    assert not set(manifest['predictor_columns'])&set(manifest['label_columns'])
    assert np.isfinite(features[manifest['predictor_columns']].to_numpy()).all()
    flow=load_flow(Path(config['flow_csv']));events=audit(flow.reset_index(drop=True))['events']
    for case in expected:
        feature=features.set_index('case_id').loc[case]
        target=labels.set_index('case_id').loc[case]
        source=provenance.set_index('case_id').loc[case]
        decision=pd.Timestamp(feature.decision_time_utc)
        assert decision==pd.Timestamp(target.decision_time_utc)==pd.Timestamp(source.decision_time_utc)
        assert sha(Path(source.source_local_path))==source.source_sha256
        past,cutoff=flow_features(flow,decision,config['flow_delay_minutes'])
        assert cutoff==pd.Timestamp(source.flow_cutoff_utc)<decision
        part=tidy[tidy.case_id==case]
        assert len(part)==12 and part.source_sha256.eq(source.source_sha256).all()
        intervals=part.to_dict(orient='records')
        clock=issue_clock(intervals,source.source_key,decision,config['forecast_delay_minutes'],config['max_forecast_age_hours'])
        for name in ['forecast_reference_time_utc','forecast_filename_time_utc','assumed_forecast_available_utc']:
            assert pd.Timestamp(source[name])==pd.Timestamp(clock[name]),(case,name)
        rain,coverage=qpf_features(intervals,decision,config['qpf_horizons_hours'])
        for name,value in {**past,**rain,'forecast_age_hours':clock['forecast_age_hours']}.items():
            assert np.isclose(feature[name],value,rtol=1e-10,atol=1e-8),(case,name)
        assert pd.Timestamp(source.assumed_forecast_available_utc)<=decision
        assert np.isclose(source.qpf_72h_coverage_hours,coverage['qpf_72h_coverage_hours'])
        assert source.min_basin_valid_area_fraction>=.99
        assert source.max_independent_decoder_difference_mm<=1e-4
        truth=target_for_case(flow,events,decision)
        assert target.target_crossing_72h==truth['target_crossing_72h']
        if truth['first_crossing_time_utc'] is not None:
            assert pd.Timestamp(target.first_crossing_time_utc)==pd.Timestamp(truth['first_crossing_time_utc'])
            assert np.isclose(target.crossing_lead_hours,truth['crossing_lead_hours'])
        else:assert pd.isna(target.first_crossing_time_utc)
    print(f'PASS: {len(expected)} paired cases; source/output hashes, recomputed features/labels, and assumed clocks verified.')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',type=Path,default=Path('data/pilot_2025'))
    args=p.parse_args()
    try:validate(args.directory)
    except Exception as exc:
        print(f'Pilot validation failed: {exc}',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    sys.exit(main())
