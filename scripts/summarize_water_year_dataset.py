#!/usr/bin/env python3
"""Describe dataset coverage and crossing representation; no predictive model."""
import argparse
from collections import Counter
import json
from pathlib import Path

from build_water_year_dataset import load_flow, audit
import pandas as pd


def summarize(directory):
    manifest=json.loads((directory/'manifest.json').read_text())
    if not manifest['complete']:raise ValueError('Dataset is incomplete')
    config=manifest['config'];year=config['water_year']
    flow=load_flow(Path(config['flow_csv']))
    start=pd.Timestamp(year-1,10,1).tz_localize('America/Los_Angeles').tz_convert('UTC')
    end=pd.Timestamp(year,10,1).tz_localize('America/Los_Angeles').tz_convert('UTC')
    events=audit(flow.reset_index(drop=True),period_start=start,period_end=end)['events']
    statuses=manifest['cases']
    labels=pd.read_csv(directory/'labels.csv')
    decisions=pd.to_datetime(labels.decision_time_utc,utc=True)
    all_decisions=pd.to_datetime([r['decision_time_utc'] for r in statuses],utc=True)
    event_rows=[]
    for event in events:
        when=pd.Timestamp(event['time_utc'])
        before=(all_decisions<when)&(when<=all_decisions+pd.Timedelta(hours=72))
        paired=(decisions<when)&(when<=decisions+pd.Timedelta(hours=72))
        excluded=Counter(r.get('reason_code','passed') for r,inside in zip(statuses,before) if inside)
        event_rows.append({'crossing_time_utc':when.isoformat(),'local_day':event['local_day'],
            'paired_pre_event_decisions':int(paired.sum()),'pre_event_schedule_status_counts':dict(excluded)})
    months={}
    for row in statuses:
        month=pd.Timestamp(row['decision_time_utc']).tz_convert('America/Los_Angeles').strftime('%Y-%m')
        counts=months.setdefault(month,Counter())
        counts['scheduled']+=1;counts[row['status']]+=1
        if row['status']=='excluded':counts[row['reason_code']]+=1
    provenance=pd.read_csv(directory/'provenance.csv')
    sources=provenance.drop_duplicates('source_sha256')
    raw_bytes=sum(Path(p).stat().st_size for p in sources.source_local_path)
    report={'scheduled_decisions':len(statuses),'paired_rows':len(labels),
        'positive_rows':int(labels.target_crossing_72h.sum()),
        'observed_crossing_days':len(events),
        'crossing_days_with_any_paired_pre_event_decision':sum(r['paired_pre_event_decisions']>0 for r in event_rows),
        'unique_first_crossings_in_positive_labels':int(labels.first_crossing_time_utc.nunique()),
        'crossings':event_rows,'monthly_coverage':{k:dict(v) for k,v in months.items()},
        'exclusion_counts':manifest['exclusion_counts'],
        'unique_source_files':len(sources),'selected_raw_bytes':raw_bytes,
        'annual_new_download_bytes':manifest['project_successful_download_bytes'],
        'small_export_bytes':sum(p.stat().st_size for p in directory.glob('*.csv')),
        'max_decoder_difference_mm':float(provenance.max_independent_decoder_difference_mm.max()),
        'minimum_valid_basin_fraction':float(provenance.min_basin_valid_area_fraction.min()),
        'forecast_reference_hours_utc':sorted(pd.to_datetime(provenance.forecast_reference_time_utc,utc=True).dt.strftime('%H:%M').unique().tolist()),
        'forecast_ages_hours':sorted(provenance.forecast_age_hours.unique().tolist()),
        'qpf_72h_coverage_hours':sorted(provenance.qpf_72h_coverage_hours.unique().tolist()),
        'note':'Rows share overlapping targets. Event counts are observed crossings, not independent storms. No model trained.'}
    destination=directory/'coverage_report.json'
    destination.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ['crossings','monthly_coverage']},indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=Path('data/dataset_2025'))
    summarize(parser.parse_args().directory)
