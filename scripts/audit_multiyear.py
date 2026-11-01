#!/usr/bin/env python3
"""Bounded AWS-era feasibility audit: station flow plus S3 metadata, never forecast grids."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from test_qpf_archive import is_wmo_bulletin


def read(path):
    return json.loads(path.read_text()) if path.exists() else None


def summarize(years, output):
    result = {'years':[], 'errors':[], 'scope':'One station, six complete AWS-era water years maximum. No model or bulk GRIB download.',
              'created_utc':datetime.now(timezone.utc).isoformat(), 'multi_year_contents_exhaustively_validated':False}
    all_names=set()
    for year in years:
        base = Path(f'.cache/feasibility/water-year-{year}')
        flow,inv = read(base/'report.json'),read(base/'inventory/report.json')
        row = {'water_year':year}
        result['years'].append(row)
        if not flow or flow.get('errors') or not inv or inv.get('no_data_or_error_dates'):
            row.update(status='incomplete_or_blocked',flow_response_files_cached=len(list(base.glob('????-??-??_????-??-??.json'))),
                       inventory_dates_verified=0 if not inv else inv.get('dates_verified',0),
                       inventory_date_pages_cached=len(list((base/'inventory').glob('????-??-??.json'))))
            row['error'] = {'flow':None if not flow else flow.get('errors'), 'inventory':None if not inv else inv.get('no_data_or_error_dates')}
            result['errors'].append(row)
            continue
        a = flow['audit']
        all_names.update(o['key'].rsplit('/',1)[-1] for day in inv['days'] for o in day['objects'] if is_wmo_bulletin(o['key']))
        row.update(expected_observations=flow['period_observations_expected'], returned_observations=flow['period_observations_returned'],
                   invalid_values=flow['period_missing_or_invalid_values'], raw_crossings=a['raw_upcrossings'],
                   daily_crossings=a['deduplicated_crossings'],eligible_hourly_decisions=a['complete_72h_hourly_decisions_below_450'],
                   positive_hourly_decisions=a['decisions_with_future_deduplicated_crossing'],
                   inventory_dates_expected=inv['dates_expected'],inventory_dates_verified=inv['dates_verified'],
                   unique_bulletin_filenames=inv['unique_bulletin_filenames'],
                   missing_filename_hour_bins=sum(len(x['hours']) for x in inv['missing_hours_grouped_by_filename_date']),
                   repeated_bulletin_names=len(inv['repeated_bulletin_filenames']),
                   ignored_metadata_objects=inv.get('ignored_metadata_object_count',0),
                   total_bulletin_objects=inv['total_bulletins_listed'],
                   gaps=flow['gaps'], events=a['events'],monthly_crossings=flow['monthly_crossing_counts_local'])
    good = [x for x in result['years'] if 'error' not in x]
    fields = ['expected_observations','returned_observations','invalid_values','raw_crossings','daily_crossings',
              'eligible_hourly_decisions','positive_hourly_decisions','inventory_dates_expected','inventory_dates_verified',
              'unique_bulletin_filenames','missing_filename_hour_bins','repeated_bulletin_names']
    fields += ['ignored_metadata_objects','total_bulletin_objects']
    result['totals'] = {name:sum(row[name] for row in good) for name in fields}
    result['complete_years'] = len(good)
    result['global_unique_bulletin_filenames'] = len(all_names)
    if len(good)==len(years) and years==list(range(min(years),max(years)+1)):
        times=sorted(datetime.strptime(name[-12:],'%Y%m%d%H%M').replace(tzinfo=timezone.utc) for name in all_names)
        if len(times)>1:
            before,after=max(zip(times,times[1:]),key=lambda pair:pair[1]-pair[0])
            result['longest_bulletin_filename_timestamp_interval']={
                'before_utc':before.isoformat(),'after_utc':after.isoformat(),
                'hours':(after-before).total_seconds()/3600,
                'note':'Filename timestamp interval, not measured operational delivery latency.'}
    result['warning'] = 'Crossing counts can be censored by gaps; daily deduplication does not imply independent storms. Hourly windows overlap.'
    output.mkdir(parents=True,exist_ok=True)
    (output/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    print(f"Summary: {output/'summary.json'}; complete audit reports={len(good)}/{len(years)}")
    return int(bool(result['errors']))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--years',nargs='+',type=int,default=list(range(2021,2027)))
    p.add_argument('--mode',choices=['all','flow','inventory','summarize'],default='all')
    p.add_argument('--max-new-flow-mib',type=float,default=160)
    p.add_argument('--cached-only',action='store_true',help='Use no network; require cached responses/inventories')
    p.add_argument('--output-dir',type=Path,default=Path('.cache/feasibility/multiyear'))
    args = p.parse_args()
    years = sorted(set(args.years))
    if len(years)>6 or min(years)<2021 or max(years)>datetime.now(timezone.utc).year:
        p.error('Select at most six completed AWS-era water years, starting in 2021')
    args.output_dir.mkdir(parents=True,exist_ok=True)
    errors = 0
    new_bytes = 0
    runs = []
    for mode in (['flow','inventory'] if args.mode=='all' else [args.mode]):
        if mode=='summarize':
            continue
        for year in years:
            command = [sys.executable,'-B','scripts/audit_water_year.py' if mode=='flow' else 'scripts/audit_water_year_inventory.py','--water-year',str(year)]
            if mode=='flow':
                remaining = 0 if args.cached_only else args.max_new_flow_mib-new_bytes/1024**2
                command += ['--max-new-mib',str(max(0,remaining))]
            elif args.cached_only:
                command += ['--cached-only']
            outcome = subprocess.run(command,check=False)
            errors += int(outcome.returncode!=0)
            runs.append({'mode':mode,'water_year':year,'exit_code':outcome.returncode})
            if mode=='flow':
                report = read(Path(f'.cache/feasibility/water-year-{year}/report.json'))
                new_bytes += sum(x.get('new_download_bytes',0) for x in (report or {}).get('requests',[]))
            else:
                report = read(Path(f'.cache/feasibility/water-year-{year}/inventory/report.json'))
            (args.output_dir/(mode+'_run.json')).write_text(json.dumps({'runs':runs,'new_flow_bytes':new_bytes},indent=2)+'\n')
            messages = json.dumps((report or {}).get('errors',[]))
            if (report or {}).get('network_circuit_breaker_open') or (outcome.returncode and any(x in messages for x in ['429','503','ProxyError','connection','Connection'])):
                print('Stopped multi-year expansion after a network failure; successful cache retained.',file=sys.stderr)
                if args.mode=='all':
                    summarize(years,args.output_dir)
                return 1
        (args.output_dir/(mode+'_run.json')).write_text(json.dumps({'runs':runs,'new_flow_bytes':new_bytes},indent=2)+'\n')
    if args.mode in ['all','summarize']:
        errors += summarize(years,args.output_dir)
    return int(bool(errors))


if __name__=='__main__':
    sys.exit(main())
