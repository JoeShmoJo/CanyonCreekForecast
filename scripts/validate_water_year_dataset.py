#!/usr/bin/env python3
"""Validate all scheduled decisions and recompute exported pairs, without network."""
import argparse
import json
from pathlib import Path

from build_water_year_dataset import make_plan, load_flow, audit
from validate_pilot_2025 import validate
import pandas as pd


def validate_year(directory,cache):
    validate(directory)
    manifest=json.loads((directory/'manifest.json').read_text())
    config=manifest['config']
    flow=load_flow(Path(config['flow_csv']))
    plan=make_plan(config,flow,audit(flow.reset_index(drop=True))['events'],cache,True)
    statuses={r['case_id']:r for r in manifest['cases']}
    exported=pd.read_csv(directory/'audit.csv').set_index('case_id')
    assert len(plan)==len(statuses)==len(exported)==manifest['scheduled_decisions']
    assert set(exported.index)==set(statuses)
    for row in plan:
        result=statuses[row['case_id']]
        assert result['decision_time_utc']==row['decision_time_utc']
        assert exported.loc[row['case_id'],'status']==result['status']
        if row['status']=='excluded':
            assert result['status']=='excluded' and result['reason_code']==row['reason_code']
        else:
            assert result['status'] in ['passed','excluded']
            if result['status']=='excluded':assert result['stage']=='forecast_quality'
            else:assert result['source_key']==row['item']['key']
    assert not manifest['errors']
    print(f"PASS: all {len(plan)} scheduled decisions accounted for; eligibility and fixed issue selection recomputed.")


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=Path('data/dataset_2025'))
    parser.add_argument('--cache-dir',type=Path,default=Path('.cache/dataset_2025'))
    args=parser.parse_args()
    validate_year(args.directory,args.cache_dir)
