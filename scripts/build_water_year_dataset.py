#!/usr/bin/env python3
"""Build a resumable, fixed-schedule paired water year. No model training.

Uses the pilot's timing/quality contract. Successful source payloads are capped by a
project-wide ledger; each completed row is checkpointed before the next download.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime,timezone
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import shutil
import sys
import time
from urllib.parse import quote

from build_pilot_2025 import (JOIN_COLUMNS,sha,load_flow,flow_features,target_for_case,
                             select_issue,issue_clock,qpf_features,retained_sources)
from probe_basin_qpf import extract_forecast
from audit_crossings import audit
from test_qpf_archive import list_objects,is_wmo_bulletin,get_small
import numpy as np
import pandas as pd
import requests


def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2)+'\n')
    temporary.replace(path)


def scheduled_decisions(year,hour=6):
    start=pd.Timestamp(year-1,10,1).tz_localize('America/Los_Angeles').tz_convert('UTC')
    end=pd.Timestamp(year,10,1).tz_localize('America/Los_Angeles').tz_convert('UTC')
    first=start.normalize()+pd.Timedelta(hours=hour)
    if first<start:first+=pd.Timedelta(days=1)
    return pd.date_range(first,end,freq='24h',inclusive='left')


def exclusion_code(error):
    text=str(error)
    for prefix,code in [('Incomplete quarter-hour','incomplete_observations'),
                        ('Non-approved or qualified','qualified_observations'),
                        ('Negative discharge','invalid_observations'),
                        ('Last available flow','last_flow_not_below_450'),
                        ('Actual flow at','decision_flow_not_below_450'),
                        ('No canonical early','no_early_issue')]:
        if text.startswith(prefix):return code
    raise error


def extra_inventory(day,year,cache,cached_only):
    # The final 06Z decision lies on October 1 UTC but September 30 local time.
    choices=[Path(f'.cache/feasibility/water-year-{y}/inventory/{day}.json') for y in [year,year+1]]
    choices.append(cache/'inventories'/(day+'.json'))
    for path in choices:
        if path.exists():
            row=json.loads(path.read_text())
            if not row.get('error'):return row
    if cached_only:raise ValueError(f'Inventory not cached for {day}')
    d=pd.Timestamp(day)
    listing=list_objects(requests.Session(),'https://noaa-ndfd-pds.s3.amazonaws.com/',
                         f'wmo/qpf/{d:%Y/%m/%d}/YIUZ98_KWBN_')
    if listing['truncated']:raise ValueError('Truncated extra-day inventory')
    row={'date':day,'objects':[o for o in listing['objects'] if is_wmo_bulletin(o['key'])],
         'listing_url':listing['url']}
    atomic_json(choices[-1],row)
    return row


def make_plan(config,flow,events,cache,cached_only):
    inventory={r['date']:r for r in json.loads(Path(config['inventory_json']).read_text())['days']}
    plan=[]
    for decision in scheduled_decisions(config['water_year'],config['decision_hour_utc']):
        row={'case_id':'D'+decision.strftime('%Y%m%d'),'decision_time_utc':decision.isoformat(),
             'status':'pending','stage':'antecedent_flow'}
        try:
            past,cutoff=flow_features(flow,decision,config['flow_delay_minutes'])
            row['stage']='target_quality'
            target=target_for_case(flow,events,decision)
            row['stage']='forecast_inventory'
            day=decision.strftime('%Y-%m-%d')
            if day not in inventory:inventory[day]=extra_inventory(day,config['water_year'],cache,cached_only)
            source=inventory[day]
            if source.get('error'):raise RuntimeError(f'Unverified inventory for {day}')
            item=select_issue(source['objects'],decision)
            row.update(stage='planned',past_features=past,target=target,flow_cutoff_utc=cutoff.isoformat(),item=item)
        except ValueError as exc:
            row.update(status='excluded',reason_code=exclusion_code(exc),reason=str(exc))
        plan.append(row)
    return plan


def available_sources(cache):
    sources=retained_sources()
    path=Path('data/pilot_2025/provenance.csv')
    if path.exists():
        for row in pd.read_csv(path).itertuples():
            local=Path(row.source_local_path)
            if local.exists():sources[row.source_key]=local
    return sources


class ForecastStore:
    def __init__(self,cache,boundary,config,cached_only):
        self.cache,self.boundary,self.config,self.cached_only=cache,boundary,config,cached_only
        self.sources=available_sources(cache)
        self.extractor_sha=sha(Path(__file__).with_name('probe_basin_qpf.py'))
        self.boundary_sha=sha(boundary)
        self.ledger_path=cache/'acquisition_ledger.json'
        self.ledger=json.loads(self.ledger_path.read_text()) if self.ledger_path.exists() else {'requests':[]}
        self.new_bytes,self.new_files,self.cache_hits=0,0,0
        self.session=requests.Session()

    @property
    def spent(self):return sum(x['bytes'] for x in self.ledger['requests'])

    def source_path(self,item):
        local=self.cache/'gribs'/item['key'].rsplit('/',1)[-1]
        if local.exists():return local
        return self.sources.get(item['key'],local)

    def obtain(self,item):
        local=self.source_path(item)
        url='https://noaa-ndfd-pds.s3.amazonaws.com/'+quote(item['key'],safe='/')
        if local.exists():self.cache_hits+=1
        else:
            if self.cached_only:raise RuntimeError('Required source is not cached; network prohibited')
            remaining=int(self.config['new_download_budget_gib']*2**30)-self.spent
            if item['size']>remaining or len(self.ledger['requests'])>=self.config['max_new_files']:
                raise RuntimeError('Project-wide source-download budget would be exceeded')
            payload,actual=get_small(self.session,url,min(remaining,16*2**20))
            local.parent.mkdir(parents=True,exist_ok=True)
            temporary=local.with_suffix('.download')
            temporary.write_bytes(payload);temporary.replace(local)
            self.new_bytes+=len(payload);self.new_files+=1
            self.ledger['requests'].append({'key':item['key'],'url':actual,'bytes':len(payload),
                'sha256':sha(local),'downloaded_at_utc':datetime.now(timezone.utc).isoformat()})
            atomic_json(self.ledger_path,self.ledger)
        if local.stat().st_size!=item['size']:raise RuntimeError('Source size differs from inventory snapshot')
        digest=sha(local)
        candidates=[self.cache/'extractions'/(digest+'.json'),Path('.cache/pilot_2025')/(digest+'.extraction.json')]
        extraction=None
        for path in candidates:
            if path.exists():
                value=json.loads(path.read_text())
                if value.get('source_sha256')==digest and value.get('boundary_sha256')==self.boundary_sha and value.get('extractor_sha256')==self.extractor_sha:
                    extraction=value;break
        if extraction is None:
            extraction,_=extract_forecast(local,self.boundary)
            extraction['extractor_sha256']=self.extractor_sha
            atomic_json(candidates[0],extraction)
        return extraction,local,digest,url


def checkpoint_valid(record,fingerprint,source_key):
    if record.get('fingerprint')!=fingerprint or record.get('status')!='passed':return False
    source=record['provenance']
    path=Path(source['source_local_path'])
    return source['source_key']==source_key and path.exists() and sha(path)==source['source_sha256']


def build(config_path,output,cache,cached_only=False,plan_only=False):
    started=time.perf_counter();config=json.loads(config_path.read_text())
    if config['decision_hour_utc']!=6 or config['flow_delay_minutes']<15 or config['forecast_delay_minutes']<60:
        raise ValueError('Preserve the validated 06Z timing contract')
    if config['flow_lookback_hours']!=72 or config['target_horizon_hours']!=72:
        raise ValueError('Only the validated 72-hour flow/target windows are implemented')
    flow=load_flow(Path(config['flow_csv']));events=audit(flow.reset_index(drop=True))['events']
    output.mkdir(parents=True,exist_ok=True);cache.mkdir(parents=True,exist_ok=True)
    store=ForecastStore(cache,Path(config['boundary']),config,cached_only)
    inputs={'config':sha(config_path),'flow':sha(Path(config['flow_csv'])),'boundary':store.boundary_sha,
            'pipeline':sha(Path(__file__)),'pairing_rules':sha(Path(__file__).with_name('build_pilot_2025.py')),
            'target_rules':sha(Path(__file__).with_name('audit_crossings.py')),'extractor':store.extractor_sha}
    fingerprint=hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest()
    plan=make_plan(config,flow,events,cache,cached_only)
    atomic_json(cache/'plan.json',{'fingerprint':fingerprint,'config':config,'decisions':plan})
    candidates=[r for r in plan if r['status']=='pending']
    needed=sum(r['item']['size'] for r in candidates if not store.source_path(r['item']).exists())
    print(f"Plan: {len(plan)} decisions; {len(candidates)} candidates; new source estimate {needed/2**30:.3f} GiB",flush=True)
    if needed>int(config['new_download_budget_gib']*2**30)-store.spent:
        raise ValueError('Preflight exceeds remaining project-wide payload budget')
    if shutil.disk_usage(cache).free<needed+256*2**20:raise ValueError('Insufficient free disk for retained grids and checkpoints')
    if plan_only:return 0
    manifest={'created_at_utc':datetime.now(timezone.utc).isoformat(),'config':config,'config_source':str(config_path),
        'config_sha256':inputs['config'],'pipeline_source':str(Path(__file__)),'pipeline_sha256':inputs['pipeline'],
        'shared_pairing_rules_sha256':inputs['pairing_rules'],'target_rules_sha256':inputs['target_rules'],
        'extractor_sha256':inputs['extractor'],'flow_csv_sha256':inputs['flow'],'boundary_sha256':inputs['boundary'],
        'input_fingerprint':fingerprint,'model_trained':False,'complete':False,'scheduled_decisions':len(plan),
        'sampling':'Every scheduled 06Z decision in the Pacific water year; no event/control selection.',
        'availability_status':'Assumed feed/publication delays, not verified historic receipt or revision times.',
        'package_versions':{p:version(p) for p in ['numpy','pandas','geopandas','shapely','pyproj','eccodes','grib2io']},
        'cases':[],'errors':[],'rows_paired':0,'resumed_rows':0}
    results=[]

    def save():
        manifest['run_new_download_bytes']=store.new_bytes
        manifest['run_new_files']=store.new_files
        manifest['run_source_cache_hits']=store.cache_hits
        manifest['project_successful_download_bytes']=store.spent
        manifest['elapsed_seconds']=time.perf_counter()-started
        atomic_json(output/'manifest.json',manifest)

    statuses=[]
    for row in plan:
        identity={k:row[k] for k in JOIN_COLUMNS}
        status={**identity,'status':row['status'],'stage':row['stage']}
        if row['status']=='excluded':status.update(reason_code=row['reason_code'],reason=row['reason'])
        statuses.append(status)
    manifest['cases']=statuses;save()
    for index,row in enumerate(plan):
        status=statuses[index]
        if row['status']=='excluded':continue
        identity={k:row[k] for k in JOIN_COLUMNS}
        case_path=cache/'cases'/(row['case_id']+'.json')
        try:
            previous=json.loads(case_path.read_text()) if case_path.exists() else None
            if previous and checkpoint_valid(previous,fingerprint,row['item']['key']):
                result=previous;manifest['resumed_rows']+=1
            else:
                extraction,local,digest,url=store.obtain(row['item'])
                if min(r['valid_area_fraction'] for r in extraction['intervals'])<.99:
                    status.update(status='excluded',stage='forecast_quality',reason_code='basin_coverage',reason='Valid basin coverage below 99%')
                    save();continue
                decision=pd.Timestamp(row['decision_time_utc'])
                try:
                    clock=issue_clock(extraction['intervals'],row['item']['key'],decision,config['forecast_delay_minutes'],config['max_forecast_age_hours'])
                    rain,coverage=qpf_features(extraction['intervals'],decision,config['qpf_horizons_hours'])
                except ValueError as exc:
                    status.update(status='excluded',stage='forecast_quality',reason_code='forecast_timing_or_intervals',reason=str(exc))
                    save();continue
                identity={k:row[k] for k in JOIN_COLUMNS}
                features={**identity,**row['past_features'],**rain,'forecast_age_hours':clock['forecast_age_hours']}
                if not all(math.isfinite(v) for k,v in features.items() if k not in JOIN_COLUMNS):raise ValueError('Nonfinite predictor')
                provenance={**identity,'selection_window':'fixed_full_year_schedule','flow_cutoff_utc':row['flow_cutoff_utc'],
                    **clock,**coverage,'source_key':row['item']['key'],'source_url':url,'source_sha256':digest,
                    'source_local_path':str(local),'min_basin_valid_area_fraction':min(r['valid_area_fraction'] for r in extraction['intervals']),
                    'max_independent_decoder_difference_mm':extraction['max_independent_decoder_difference_mm']}
                result={'status':'passed','fingerprint':fingerprint,'features':features,'labels':{**identity,**row['target']},
                    'provenance':provenance,'intervals':[{**identity,'source_sha256':digest,**r} for r in extraction['intervals']]}
                atomic_json(case_path,result)
            results.append(result);status.update(status='passed',stage='paired',source_key=row['item']['key'])
            manifest['rows_paired']=len(results);save()
            if len(results)%25==0:print(f"Paired {len(results)}/{len(candidates)}; run downloads {store.new_bytes/2**30:.3f} GiB",flush=True)
        except Exception as exc:
            status.update(status='failed',stage='processing',reason=str(exc))
            manifest['errors'].append({**identity,'error':str(exc),'type':type(exc).__name__})
            save();print(f"Stopped at {row['case_id']}: {exc}",file=sys.stderr,flush=True);break
    accounted=all(s['status'] in ['passed','excluded'] for s in statuses)
    if accounted and not manifest['errors'] and results:
        tables={'features':[r['features'] for r in results],'labels':[r['labels'] for r in results],
                'provenance':[r['provenance'] for r in results],'qpf_intervals':[x for r in results for x in r['intervals']],
                'audit':statuses}
        for name,rows in tables.items():
            destination=output/(name+'.csv');pd.DataFrame(rows).to_csv(destination,index=False)
            manifest.setdefault('output_sha256',{})[destination.name]=sha(destination)
        manifest['predictor_columns']=[c for c in results[0]['features'] if c not in JOIN_COLUMNS]
        manifest['label_columns']=[c for c in results[0]['labels'] if c not in JOIN_COLUMNS]
        manifest['positive_rows']=sum(r['labels']['target_crossing_72h'] for r in results)
        manifest['exclusion_counts']=dict(Counter(s.get('reason_code') for s in statuses if s['status']=='excluded'))
        manifest['complete']=True
    save()
    print(f"Complete={manifest['complete']}; paired={len(results)}; excluded={sum(s['status']=='excluded' for s in statuses)}; errors={len(manifest['errors'])}",flush=True)
    return 0 if manifest['complete'] else 1


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=Path('configs/dataset_2025.json'))
    p.add_argument('--output-dir',type=Path,default=Path('data/dataset_2025'))
    p.add_argument('--cache-dir',type=Path,default=Path('.cache/dataset_2025'))
    p.add_argument('--cached-only',action='store_true')
    p.add_argument('--plan-only',action='store_true')
    args=p.parse_args()
    try:return build(args.config,args.output_dir,args.cache_dir,args.cached_only,args.plan_only)
    except Exception as exc:
        print(f'Dataset build failed: {exc}',file=sys.stderr);return 1


if __name__=='__main__':sys.exit(main())
