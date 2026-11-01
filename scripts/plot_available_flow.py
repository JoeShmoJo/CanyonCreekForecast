#!/usr/bin/env python3
"""Plot cached crossing counts, with partial years explicitly distinguished."""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,default=Path('.cache/feasibility/multiyear/available_flow.json'))
    p.add_argument('--output',type=Path,default=Path('docs/feasibility_evidence/available_crossings.png'))
    args=p.parse_args()
    rows=json.loads(args.input.read_text())['years']
    good=[r for r in rows if not r.get('error')]
    partial=any(r['calendar_months_cached']<12 for r in good)
    fig,ax=plt.subplots(figsize=(9,5),layout='constrained')
    for i,row in enumerate(good):
        complete=row['calendar_months_cached']==12
        count=row['audit']['deduplicated_crossings']
        ax.bar(i,count,color='#286b92' if complete else '#cc8030',hatch=None if complete else '//',width=.65)
        ax.text(i,count+.4,str(count),ha='center',fontweight='bold')
    ax.set_xticks(range(len(good)),[f"{r['water_year']}\n{r['calendar_months_cached']}/12 months" for r in good])
    ax.set_ylabel('Observed crossing days under the selected daily rule')
    ax.set_xlabel('Water year and available monthly intervals')
    subtitle='Partial-year counts are not annual totals' if partial else 'First upward crossing per Pacific calendar day; threshold 450 cfs'
    ax.set_title('Canyon Creek: observed crossing days\n'+subtitle)
    if partial:
        ax.legend(handles=[Patch(color='#286b92',label='All 12 monthly responses cached'),
                           Patch(facecolor='#cc8030',hatch='//',label='Partial year')],loc='upper right')
    ax.set_ylim(0,max(r['audit']['deduplicated_crossings'] for r in good)+7)
    ax.grid(axis='y',alpha=.2)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    fig.savefig(args.output,dpi=160)
    plt.close(fig)
    print(args.output)


if __name__=='__main__':
    main()
