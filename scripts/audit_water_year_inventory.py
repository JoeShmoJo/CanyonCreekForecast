#!/usr/bin/env python3
"""Metadata-only inventory of one water year of CONUS NDFD QPF, no GRIB downloads."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import threading

import requests

from test_qpf_archive import list_objects, is_wmo_bulletin


class NetworkCircuitBreaker:
    """Stop scheduling new requests after repeated transport/HTTP failures."""
    def __init__(self, limit=3):
        self.limit = limit
        self.failures = 0
        self.stopped = threading.Event()
        self.lock = threading.Lock()

    def record(self, error):
        if isinstance(error, requests.RequestException):
            with self.lock:
                self.failures += 1
                if self.failures >= self.limit:
                    self.stopped.set()


def filename_diagnostics(rows):
    names, hours, misplaced = {}, {}, []
    for row in rows:
        for item in row.get('objects', []):
            if not is_wmo_bulletin(item['key']):
                continue
            name = item['key'].rsplit('/',1)[-1]
            names.setdefault(name, []).append(item['key'])
            timestamp = name[-12:]
            day = timestamp[:4]+'-'+timestamp[4:6]+'-'+timestamp[6:8]
            hours.setdefault(day,set()).add(timestamp[8:10])
            if day != row['date']:
                misplaced.append({'folder_date':row['date'],'filename_date':day,'key':item['key']})
    return {'unique_bulletin_filenames':len(names),
            'filename_date_folder_mismatches':misplaced,
            'repeated_bulletin_filenames':[{'filename':name,'keys':keys} for name,keys in names.items() if len(keys)>1],
            'missing_hours_grouped_by_filename_date':[
                {'date':row['date'],'hours':[f'{h:02}' for h in range(24) if f'{h:02}' not in hours.get(row['date'],set())]}
                for row in rows if len(hours.get(row['date'],set()))<24]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--water-year", type=int, default=2025)
    p.add_argument("--output-dir", type=Path)
    p.add_argument("--workers", type=int, choices=[1, 2, 3], default=3)
    p.add_argument("--max-network-errors", type=int, choices=range(1,11), default=3)
    p.add_argument('--cached-only',action='store_true')
    args = p.parse_args()
    if not 2021 <= args.water_year <= date.today().year:
        p.error("Choose a completed AWS-era water year")
    start, end = date(args.water_year-1,10,1), date(args.water_year,10,1)
    if end > date.today():
        p.error("Selected water year has not ended")
    args.output_dir = args.output_dir or Path(f".cache/feasibility/water-year-{args.water_year}/inventory")
    days = [start + timedelta(days=i) for i in range((end-start).days)]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    reusable = {}
    # Reuse existing complete date listings before requesting additional metadata.
    for path in [args.output_dir.parent/'seasonal-inventory.json', Path('.cache/feasibility/inventory.json')]:
        if path.exists():
            for row in json.loads(path.read_text()).get('days', []):
                if not row.get('error'):
                    reusable[row['date']] = row
    local = threading.local()
    circuit = NetworkCircuitBreaker(args.max_network_errors)

    def inspect(day):
        key = day.isoformat()
        cache = args.output_dir / (key + '.json')
        cached = json.loads(cache.read_text()) if cache.exists() else None
        row = {'date':key}
        try:
            if cached and not cached.get('error'):
                row = cached
                row['reused_cache'] = True
            elif key in reusable:
                row = dict(reusable[key])
                row['reused_prior_inventory'] = True
            else:
                if args.cached_only:
                    row.update(not_requested=True,error='No cached inventory; network disabled by --cached-only')
                    return row
                if circuit.stopped.is_set():
                    row.update(not_requested=True,error='Network request skipped after repeated network failures')
                    return row
                if not hasattr(local,'session'):
                    local.session = requests.Session()
                prefix = f'wmo/qpf/{day.year:04}/{day.month:02}/{day.day:02}/YIUZ98_KWBN_'
                listing = list_objects(local.session,'https://noaa-ndfd-pds.s3.amazonaws.com/',prefix)
                row['listing_url'] = listing['url']
                if listing['truncated']:
                    raise ValueError('Truncated inventory; cannot count complete daily coverage')
                row['objects'] = listing['objects']
            ignored={x['key']:x for x in row.get('ignored_metadata_objects',[])}
            ignored.update({x['key']:x for x in row['objects'] if not is_wmo_bulletin(x['key'])})
            row['ignored_metadata_objects']=list(ignored.values())
            objects = [x for x in row['objects'] if is_wmo_bulletin(x['key'])]
            row['objects']=objects
            row['conus_bulletin_count'] = len(objects)
            hours = Counter(x['key'][-4:-2] for x in objects)
            row['filename_hour_counts'] = dict(sorted(hours.items()))
            row['hours_without_bulletins'] = [f'{h:02}' for h in range(24) if f'{h:02}' not in hours]
            if not objects:
                raise ValueError('No CONUS bulletin objects on this date')
            cache.write_text(json.dumps(row,indent=2)+'\n')
        except Exception as exc:
            circuit.record(exc)
            row['error'] = str(exc)
        return row

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(inspect, days))
    errors = [r for r in rows if r.get('error')]
    passed = [r for r in rows if not r.get('error')]
    report = {'water_year':args.water_year,'dates_expected':len(days),'dates_verified':len(passed),
              'no_data_or_error_dates':[{'date':r['date'],'error':r['error']} for r in errors],
              'total_bulletins_listed':sum(r['conus_bulletin_count'] for r in passed),
              'days_with_bulletin_count_other_than_48':[
                  {'date':r['date'],'count':r['conus_bulletin_count'],'hours_without_bulletins':r['hours_without_bulletins']}
                  for r in passed if r['conus_bulletin_count']!=48],
              'missing_filename_hour_bins':sum(len(r['hours_without_bulletins']) for r in passed),
              'days':rows,'scope':'Object inventory only; contents, lead intervals, and historic delivery latency not exhaustively verified.',
              'grib_downloads':0,'full_multi_year_history_audited':False}
    report['network_circuit_breaker_open'] = circuit.stopped.is_set()
    report['network_error_count'] = circuit.failures
    report['ignored_metadata_object_count'] = sum(len(r.get('ignored_metadata_objects',[])) for r in passed)
    report['dates_not_requested_after_network_failure'] = sum(bool(r.get('not_requested')) for r in rows)
    report.update(filename_diagnostics(passed))
    (args.output_dir/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(f"Report: {args.output_dir/'report.json'}; dates={len(passed)}/{len(days)}; errors={len(errors)}")
    return int(bool(errors))


if __name__=='__main__':
    sys.exit(main())
