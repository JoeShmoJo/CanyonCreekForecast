#!/usr/bin/env python3
"""Plot retained one-year flow events and forecast inventory; downloads nothing."""
import argparse
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import ScalarFormatter
import pandas as pd


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',type=Path,default=Path('.cache/feasibility/water-year-2025'))
    p.add_argument('--output',type=Path,default=Path('docs/feasibility_evidence/water_year_2025.png'))
    args = p.parse_args()
    report = json.loads((args.directory/'report.json').read_text())
    inventory = json.loads((args.directory/'inventory/report.json').read_text())
    a = report['audit']
    start,end = pd.Timestamp(a['counting_period_start_utc']),pd.Timestamp(a['counting_period_end_exclusive_utc'])
    f = pd.read_csv(args.directory/'discharge.csv')
    f.time_utc = pd.to_datetime(f.time_utc,utc=True)
    flow = f.set_index('time_utc').discharge_cfs.reindex(pd.date_range(start,end,freq='15min',inclusive='left'))
    zone = ZoneInfo('America/Los_Angeles')
    events = a['events']
    event_times = pd.to_datetime([x['time_utc'] for x in events],utc=True)
    dates = pd.to_datetime([x['date'] for x in inventory['days']]).tz_localize(zone)
    counts = [x['conus_bulletin_count'] for x in inventory['days']]
    months = pd.date_range(start.tz_convert(zone),end.tz_convert(zone),freq='MS',inclusive='left')
    monthly = [report['monthly_crossing_counts_local'].get(x.strftime('%Y-%m'),0) for x in months]
    fig,axes = plt.subplots(3,1,figsize=(12,9),layout='constrained',gridspec_kw={'height_ratios':[2,1,1]})
    axes[0].plot(flow.index,flow.values,lw=.7,color='#23699a',label='15-minute discharge; gaps left blank')
    axes[0].axhline(450,ls='--',color='gray',lw=1,label='450 cfs')
    axes[0].scatter(event_times,[x['discharge_cfs'] for x in events],s=26,color='#c44830',zorder=4,label='First upward crossing per local day')
    axes[0].set_yscale('log')
    axes[0].set_yticks([20,50,100,450,1000,5000])
    axes[0].yaxis.set_major_formatter(ScalarFormatter())
    axes[0].set_ylabel('Discharge (cfs; log scale)')
    axes[0].legend(fontsize=8,loc='upper right')
    axes[0].set_title(f"{a['deduplicated_crossings']} retained crossings; {report['period_observations_returned']:,}/{report['period_observations_expected']:,} expected observations")
    axes[1].bar(months,monthly,width=20,color='#c44830')
    axes[1].set_ylabel('Retained crossings')
    axes[1].set_yticks(range(max(monthly)+1))
    axes[2].plot(dates,counts,lw=.8,color='#23699a')
    axes[2].axhline(48,color='gray',ls='--',lw=.8)
    anomalies = [(d,c) for d,c in zip(dates,counts) if c!=48]
    axes[2].scatter([d for d,c in anomalies],[c for d,c in anomalies],s=12,color='#c44830')
    axes[2].set_ylim(35,56)
    axes[2].set_ylabel('QPF objects / date folder')
    repeated = len(inventory.get('repeated_bulletin_filenames',[]))
    axes[2].set_title(f"{inventory['dates_verified']}/{inventory['dates_expected']} dates verified; {repeated} bulletin names repeat across folders",fontsize=10)
    for ax in axes:
        ax.set_xlim(start,end)
        ax.xaxis.set_major_locator(mdates.MonthLocator(tz=zone))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b',tz=zone))
        ax.grid(axis='y',alpha=.2)
    axes[-1].set_xlabel('Local calendar month (America/Los_Angeles)')
    fig.suptitle(f"Canyon Creek near Amboy — water year {report['water_year']} data feasibility",fontsize=15)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    fig.savefig(args.output,dpi=160)
    plt.close(fig)
    print(args.output)


if __name__=='__main__':
    main()
