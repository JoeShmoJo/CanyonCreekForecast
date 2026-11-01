#!/usr/bin/env python3
"""Inspect available cached flow only, distinguishing partial coverage from annual audits."""
import argparse
import json
from pathlib import Path
import sys

import pandas as pd

from audit_crossings import audit
from audit_water_year import combine_frames
from test_usgs import parse_continuous


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--years',nargs='+',type=int,default=list(range(2021,2027)))
    p.add_argument('--output',type=Path,default=Path('.cache/feasibility/multiyear/available_flow.json'))
    args=p.parse_args()
    report={'network_requests':0,'years':[],'errors':[],
            'scope':'Counts from available cached periods; not full-year event rates or independent-storm counts.'}
    for year in args.years:
        row={'water_year':year,'cached_months':[],'missing_months':[]}
        report['years'].append(row)
        try:
            bounds=pd.date_range(f'{year-1}-10-01',f'{year}-10-01',freq='MS',tz='UTC')
            windows=list(zip(bounds[:-1],bounds[1:]))+[(bounds[-1],bounds[-1]+pd.Timedelta(days=5))]
            frames=[]
            for start,end in windows:
                tag=start.strftime('%Y-%m-%d')+'_'+end.strftime('%Y-%m-%d')
                source=Path(f'.cache/feasibility/water-year-{year}')/(tag+'.json')
                reuse=Path('.cache/feasibility/crossings')/(tag+'.csv')
                if source.exists():
                    payload=json.loads(source.read_text())
                    if not payload.get('features'):
                        continue
                    f=parse_continuous(payload)
                elif reuse.exists():
                    f=pd.read_csv(reuse,keep_default_na=False)
                    f['time_utc']=pd.to_datetime(f.time_utc,utc=True)
                    f['discharge_cfs']=pd.to_numeric(f.discharge_cfs,errors='coerce')
                else:
                    if start<bounds[-1]:row['missing_months'].append(start.strftime('%Y-%m'))
                    continue
                frames.append(f)
                if start<bounds[-1]:row['cached_months'].append(start.strftime('%Y-%m'))
            f=combine_frames(frames)
            start=pd.Timestamp(year-1,10,1).tz_localize('America/Los_Angeles').tz_convert('UTC')
            end=pd.Timestamp(year,10,1).tz_localize('America/Los_Angeles').tz_convert('UTC')
            result=audit(f,period_start=start,period_end=end)
            row.update(audit=result,calendar_months_cached=len(row['cached_months']),
                       full_year_monthly_responses_cached=len(row['cached_months'])==12)
            selected=f[(f.time_utc>=start)&(f.time_utc<end)]
            estimated=f.qualifiers.str.contains('estimated',case=False,regex=False)
            row['estimated_values_in_counting_period']=int((selected.qualifiers.str.contains('estimated',case=False,regex=False)).sum())
            row['counting_period_observations_available']=len(selected)
            if estimated.any():
                sensitivity=f.copy()
                sensitivity.loc[estimated,'discharge_cfs']=float('nan')
                without=audit(sensitivity,period_start=start,period_end=end)
                row['crossing_count_if_estimated_values_censored']=without['deduplicated_crossings']
        except Exception as exc:
            row['error']=str(exc)
            report['errors'].append({'water_year':year,'error':str(exc)})
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(f"Cached-flow summary: {args.output}; errors={len(report['errors'])}; network_requests=0")
    return int(bool(report['errors']))


if __name__=='__main__':
    sys.exit(main())
