#!/usr/bin/env python3
"""Bounded NCEI catalog/NCSS service discovery; success is not subset-data validation."""
import argparse
from datetime import date
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import requests

from test_qpf_archive import get_small


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--date',default='2025-03-15')
    p.add_argument('--output-dir',type=Path,default=Path('.cache/feasibility/ncei-subset'))
    args = p.parse_args()
    d = date.fromisoformat(args.date)
    url = f'https://www.ncei.noaa.gov/thredds/catalog/model-ndfd-file/access/{d:%Y%m}/{d:%Y%m%d}/catalog.xml'
    args.output_dir.mkdir(parents=True,exist_ok=True)
    report = {'date':args.date,'catalog_url':url,'errors':[],'basin_subset_data_verified':False}
    session = requests.Session()
    try:
        payload,actual = get_small(session,url,3_000_000)
        tree = ET.fromstring(payload)
        ns = {'t':'http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0'}
        paths = [x.attrib['urlPath'] for x in tree.findall('.//t:dataset',ns)
                 if 'YIUZ98_KWBN_' in x.attrib.get('urlPath','')]
        report['catalog_qpf_paths'] = paths
        if not paths:
            raise ValueError('No CONUS QPF paths found in catalog')
        ncss = 'https://www.ncei.noaa.gov/thredds/ncss/grid/'+paths[0]+'/dataset.xml'
        report['ncss_metadata_url'] = ncss
        # Preserve a bounded service-error excerpt, rather than reducing it to a generic HTTP failure.
        with session.get(ncss,timeout=(10,30),stream=True) as response:
            report['ncss_status'] = response.status_code
            body = response.raw.read(200_001,decode_content=True)
            if len(body)>200_000:
                raise ValueError('NCSS metadata exceeds bounded probe size')
            report['ncss_response_excerpt'] = body.decode(errors='replace')[:2000]
            response.raise_for_status()
            (args.output_dir/'dataset.xml').write_bytes(body)
            report['note'] = 'NCSS metadata accessible; actual subset download and decoder comparison still required.'
    except Exception as exc:
        report['errors'].append(str(exc))
    out = args.output_dir/'report.json'
    out.write_text(json.dumps(report,indent=2)+'\n')
    print(f"Report: {out}; service_errors={len(report['errors'])}; subset_data_verified=False")
    return 1 if report['errors'] else 2


if __name__=='__main__':
    sys.exit(main())
