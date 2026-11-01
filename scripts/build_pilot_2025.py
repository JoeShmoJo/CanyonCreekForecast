#!/usr/bin/env python3
"""Build a small, time-gated, diagnostic paired dataset. Never train a model.

Publication/feed delays are explicit assumptions, not verified historical receipt times.
Cases are deliberately selected using known events and controls, not sampled for prevalence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import sys
import time
from urllib.parse import quote, unquote, urlsplit

# Initialize the projection library before ecCodes native dependencies.
from probe_basin_qpf import extract_forecast
import numpy as np
import pandas as pd
import requests

from audit_crossings import audit
from test_qpf_archive import get_small, is_wmo_bulletin

JOIN_COLUMNS = ['case_id','decision_time_utc']


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_flow(path):
    f=pd.read_csv(path,keep_default_na=False)
    f['time_utc']=pd.to_datetime(f.time_utc,utc=True,format='ISO8601')
    f['discharge_cfs']=pd.to_numeric(f.discharge_cfs,errors='coerce')
    if f.time_utc.duplicated().any() or f.series_name.nunique()!=1:
        raise ValueError('Need one primary flow series with unique timestamps')
    return f.sort_values('time_utc').set_index('time_utc',drop=False)


def approved_window(flow,start,end):
    index=pd.date_range(start,end,freq='15min')
    window=flow.reindex(index)
    if window.discharge_cfs.isna().any() or not np.isfinite(window.discharge_cfs).all():
        raise ValueError(f'Incomplete quarter-hour flow coverage: {start} to {end}')
    if window.discharge_cfs.lt(0).any():raise ValueError('Negative discharge is not a valid flow feature or label input')
    if not window.qualifiers.eq('Approved').all():
        raise ValueError(f'Non-approved or qualified flow in required window: {start} to {end}')
    return window


def flow_features(flow,decision,delay_minutes=15):
    cutoff=decision-pd.Timedelta(minutes=delay_minutes)
    past=approved_window(flow,cutoff-pd.Timedelta(hours=72),cutoff)
    q=past.discharge_cfs
    if q.iloc[-1]>=450:
        raise ValueError('Last available flow is not below 450 cfs')
    features={'flow_last_cfs':float(q.iloc[-1])}
    for hours in [1,6,24,72]:
        features[f'flow_lag_{hours}h_from_cutoff_cfs']=float(q.loc[cutoff-pd.Timedelta(hours=hours)])
    for hours in [6,24,72]:
        segment=q[q.index>cutoff-pd.Timedelta(hours=hours)]
        features[f'flow_mean_{hours}h_cfs']=float(segment.mean())
    features['flow_max_24h_cfs']=float(q[q.index>cutoff-pd.Timedelta(hours=24)].max())
    features['flow_delta_6h_cfs']=features['flow_last_cfs']-features['flow_lag_6h_from_cutoff_cfs']
    local=decision.tz_convert('America/Los_Angeles')
    year_days=(pd.Timestamp(local.year+1,1,1)-pd.Timestamp(local.year,1,1)).days
    angle=2*math.pi*(local.day_of_year-1)/year_days
    features.update(calendar_doy_sin=math.sin(angle),calendar_doy_cos=math.cos(angle))
    return features,cutoff


def target_for_case(flow,events,decision):
    end=decision+pd.Timedelta(hours=72)
    midnight=decision.tz_convert('America/Los_Angeles').normalize().tz_convert('UTC')
    approved_window(flow,midnight,end)
    current=float(flow.loc[decision,'discharge_cfs'])
    if current>=450:
        raise ValueError('Actual flow at the decision is already at/above 450; not a crossing-from-below case')
    times=[pd.Timestamp(e['time_utc']) for e in events if decision<pd.Timestamp(e['time_utc'])<=end]
    first=min(times) if times else None
    return {'target_crossing_72h':int(first is not None),
            'target_window_end_utc':end.isoformat(),
            'first_crossing_time_utc':None if first is None else first.isoformat(),
            'crossing_lead_hours':None if first is None else (first-decision).total_seconds()/3600,
            'eligibility_flow_at_decision_cfs':current}


def select_issue(objects,decision):
    """First canonical bulletin named within that UTC day's 00:00–01:00 window."""
    day=decision.strftime('%Y%m%d')
    candidates=[]
    for item in objects:
        if not is_wmo_bulletin(item['key']):continue
        stamp=item['key'].rsplit('_',1)[-1]
        if stamp[:8]==day and stamp[8:10]=='00':
            candidates.append(item)
    if not candidates:raise ValueError('No canonical early-UTC bulletin in the predeclared issue window')
    return sorted(candidates,key=lambda item:item['key'])[0]


def issue_clock(intervals,key,decision,delay_minutes=60,max_age_hours=6):
    references={pd.Timestamp(row['reference_time_utc']) for row in intervals}
    if len(references)!=1:raise ValueError('Mixed forecast reference times inside one issue')
    reference=references.pop()
    filename=pd.Timestamp(datetime.strptime(key.rsplit('_',1)[-1],'%Y%m%d%H%M'),tz='UTC')
    issue_day=decision.normalize()
    if not issue_day<=reference<issue_day+pd.Timedelta(hours=1):
        raise ValueError('Actual GRIB reference outside the predeclared early-UTC issue window')
    available=max(reference,filename)+pd.Timedelta(minutes=delay_minutes)
    age=(decision-reference).total_seconds()/3600
    if available>decision or not 0<=age<=max_age_hours:
        raise ValueError('Forecast violates assumed publication delay or maximum age')
    return {'forecast_reference_time_utc':reference.isoformat(),'forecast_filename_time_utc':filename.isoformat(),
            'assumed_forecast_available_utc':available.isoformat(),'forecast_age_hours':age}


def qpf_features(intervals,decision,horizons=(6,12,24,48,60)):
    parsed=[]
    for row in intervals:
        start,end=pd.Timestamp(row['accumulation_start_utc']),pd.Timestamp(row['valid_end_utc'])
        if end-start!=pd.Timedelta(hours=6):raise ValueError('Expected six-hour QPF intervals')
        depth=float(row['basin_mean_mm'])
        if not math.isfinite(depth) or depth<0:raise ValueError('Invalid precipitation depth')
        parsed.append((start,end,depth))
    parsed.sort()
    for a,b in zip(parsed,parsed[1:]):
        if a[1]!=b[0]:raise ValueError('QPF intervals overlap or have a gap')
    lookup={(start,end):depth for start,end,depth in parsed}
    features={}
    for horizon in horizons:
        depths=[]
        for h in range(0,horizon,6):
            segment=(decision+pd.Timedelta(hours=h),decision+pd.Timedelta(hours=h+6))
            if segment not in lookup:raise ValueError(f'Missing complete QPF interval for {horizon}h feature')
            depths.append(lookup[segment])
        features[f'qpf_next_{horizon}h_mm']=sum(depths)
    limit=decision+pd.Timedelta(hours=72)
    coverage=sum(max(0.,(min(end,limit)-max(start,decision)).total_seconds()/3600) for start,end,depth in parsed)
    return features,{'qpf_72h_coverage_hours':coverage,'qpf_72h_complete':math.isclose(coverage,72.)}


def retained_sources():
    sources={}
    reports=[Path('.cache/feasibility')/name/'report.json' for name in
             ['conus','qpf','final-live','water-year-2025/seasonal-qpf']]
    for path in reports:
        if path.exists():
            for item in json.loads(path.read_text()).get('files',[]):
                url=item.get('requested_url','')
                local=Path(item.get('path',''))
                if urlsplit(url).hostname=='noaa-ndfd-pds.s3.amazonaws.com' and local.is_file():
                    sources[unquote(urlsplit(url).path.lstrip('/'))]=local
    return sources


def build(config_path,output,cache,max_new_mib=None,cached_only=False):
    started=time.perf_counter()
    config=json.loads(config_path.read_text())
    if len(config['cases'])>12:raise ValueError('Pilot limited to twelve diagnostic cases')
    if config['flow_lookback_hours']!=72 or config['target_horizon_hours']!=72:
        raise ValueError('This pilot implements a 72h flow lookback and target')
    if config['decision_hour_utc']!=6 or config['flow_delay_minutes']<15 or config['forecast_delay_minutes']<60:
        raise ValueError('Pilot requires 06Z decisions, at least 15 min flow delay and 60 min forecast delay')
    if any(h<=0 or h%6 for h in config['qpf_horizons_hours']):raise ValueError('QPF horizons must be positive multiples of six hours')
    extractor_sha=sha(Path(__file__).with_name('probe_basin_qpf.py'))
    flow_path,boundary=Path(config['flow_csv']),Path(config['boundary'])
    flow=load_flow(flow_path)
    events=audit(flow.reset_index(drop=True))['events']
    inventory={row['date']:row for row in json.loads(Path(config['inventory_json']).read_text())['days']}
    sources=retained_sources()
    cache.mkdir(parents=True,exist_ok=True)
    output.mkdir(parents=True,exist_ok=True)
    manifest={'created_at_utc':datetime.now(timezone.utc).isoformat(),'config':config,
              'config_source':str(config_path),'config_sha256':sha(config_path),
              'pipeline_sha256':sha(Path(__file__)),'extractor_sha256':extractor_sha,
              'target_rules_sha256':sha(Path(__file__).with_name('audit_crossings.py')),
              'package_versions':{name:version(name) for name in ['numpy','pandas','geopandas','shapely','pyproj','eccodes','grib2io']},
              'flow_csv_sha256':sha(flow_path),'boundary_sha256':sha(boundary),
              'availability_status':'Time gating under assumed delays; historic delivery/revision times not independently verified.',
              'selection_bias':'Diagnostic event/control windows selected retrospectively; not representative training or evaluation sampling.',
              'cases':[],'errors':[],'new_download_bytes':0,'new_files':0,'cache_hits':0,
              'model_trained':False,'complete':False}
    budget=int((max_new_mib if max_new_mib is not None else config['new_download_budget_mib'])*2**20)
    features,labels,provenance,tidy=[],[],[],[]
    session=requests.Session()
    for number,case in enumerate(config['cases'],1):
        decision=pd.Timestamp(case['date']+f"T{config['decision_hour_utc']:02}:00:00Z")
        identity={'case_id':f'P{number:03}','decision_time_utc':decision.isoformat()}
        status={**identity,'selection_window':case['window']}
        manifest['cases'].append(status)
        tick=time.perf_counter()
        try:
            past,cutoff=flow_features(flow,decision,config['flow_delay_minutes'])
            target=target_for_case(flow,events,decision)
            day=inventory[case['date']]
            if day.get('error'):raise ValueError('Date inventory is incomplete')
            item=select_issue(day['objects'],decision)
            key=item['key'];url='https://noaa-ndfd-pds.s3.amazonaws.com/'+quote(key,safe='/')
            local=cache/key.rsplit('/',1)[-1]
            if key in sources:local=sources[key]
            if local.exists():
                manifest['cache_hits']+=1
            else:
                if cached_only:raise ValueError('Required GRIB missing; --cached-only prohibits download')
                remaining=budget-manifest['new_download_bytes']
                if item['size']>remaining or manifest['new_files']>=config['max_new_files']:
                    raise ValueError('Pilot download/file budget would be exceeded')
                payload,actual=get_small(session,url,min(remaining,16*2**20))
                manifest['new_download_bytes']+=len(payload);manifest['new_files']+=1
                local.write_bytes(payload)
            if local.stat().st_size!=item['size']:raise ValueError('GRIB size differs from inventory snapshot')
            digest=sha(local)
            extraction_path=cache/(digest+'.extraction.json')
            extraction=json.loads(extraction_path.read_text()) if extraction_path.exists() else None
            if not extraction or extraction['source_sha256']!=digest or extraction['boundary_sha256']!=manifest['boundary_sha256'] or extraction.get('extractor_sha256')!=extractor_sha:
                extraction,_=extract_forecast(local,boundary)
                extraction['extractor_sha256']=extractor_sha
                extraction_path.write_text(json.dumps(extraction,indent=2)+'\n')
            if min(r['valid_area_fraction'] for r in extraction['intervals'])<.99:
                raise ValueError('Insufficient basin coverage')
            clock=issue_clock(extraction['intervals'],key,decision,config['forecast_delay_minutes'],config['max_forecast_age_hours'])
            rain,coverage=qpf_features(extraction['intervals'],decision,config['qpf_horizons_hours'])
            feature={**identity,**past,**rain,'forecast_age_hours':clock['forecast_age_hours']}
            if not all(math.isfinite(v) for k,v in feature.items() if k not in JOIN_COLUMNS):raise ValueError('Nonfinite predictor')
            features.append(feature);labels.append({**identity,**target})
            source={**identity,'selection_window':case['window'],'flow_cutoff_utc':cutoff.isoformat(),
                    **clock,**coverage,'source_url':url,'source_key':key,'source_sha256':digest,
                    'source_local_path':str(local),'min_basin_valid_area_fraction':min(r['valid_area_fraction'] for r in extraction['intervals']),
                    'max_independent_decoder_difference_mm':extraction['max_independent_decoder_difference_mm']}
            provenance.append(source)
            for interval in extraction['intervals']:tidy.append({**identity,'source_sha256':digest,**interval})
            status.update(status='passed',source_key=key,target=target['target_crossing_72h'],
                          extraction_grib2io_version=extraction['grib2io_version'],
                          extraction_g2c_version=extraction['g2c_version'],elapsed_seconds=time.perf_counter()-tick)
            print(f"{identity['case_id']} {case['date']}: paired; target={target['target_crossing_72h']}",flush=True)
        except Exception as exc:
            status.update(status='failed',error=str(exc))
            manifest['errors'].append({**identity,'error':str(exc)})
            print(f"{identity['case_id']} failed: {exc}",file=sys.stderr,flush=True)
            if isinstance(exc,requests.RequestException):break
    manifest['elapsed_seconds']=time.perf_counter()-started
    manifest['rows_paired']=len(features)
    if features:
        manifest['predictor_columns']=[c for c in features[0] if c not in JOIN_COLUMNS]
        manifest['label_columns']=[c for c in labels[0] if c not in JOIN_COLUMNS]
        assert not set(manifest['predictor_columns'])&set(manifest['label_columns'])
    if len(features)==len(config['cases']) and not manifest['errors']:
        for name,rows in [('features',features),('labels',labels),('provenance',provenance),('qpf_intervals',tidy)]:
            destination=output/(name+'.csv');pd.DataFrame(rows).to_csv(destination,index=False)
            manifest.setdefault('output_sha256',{})[destination.name]=sha(destination)
        manifest.update(complete=True,positive_rows=sum(row['target_crossing_72h'] for row in labels))
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(f"Pilot complete={manifest['complete']}; rows={len(features)}; new bytes={manifest['new_download_bytes']}",flush=True)
    return 0 if manifest['complete'] else 1


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=Path('configs/pilot_2025.json'))
    p.add_argument('--output-dir',type=Path,default=Path('data/pilot_2025'))
    p.add_argument('--cache-dir',type=Path,default=Path('.cache/pilot_2025'))
    p.add_argument('--max-new-mib',type=float)
    p.add_argument('--cached-only',action='store_true')
    args=p.parse_args()
    try:
        return build(args.config,args.output_dir,args.cache_dir,args.max_new_mib,args.cached_only)
    except (FileNotFoundError,ValueError,ImportError) as exc:
        print(f'Pilot build failed: {exc}',file=sys.stderr)
        return 1


if __name__=='__main__':
    sys.exit(main())
