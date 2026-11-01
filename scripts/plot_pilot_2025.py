#!/usr/bin/env python3
"""Plot the diagnostic pilot's inputs and known outcomes; not model performance."""
import argparse
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import numpy as np
import pandas as pd


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory',type=Path,default=Path('data/pilot_2025'))
    p.add_argument('--output',type=Path,default=Path('docs/pilot_2025.png'))
    args=p.parse_args()
    f=pd.read_csv(args.directory/'features.csv')
    labels=pd.read_csv(args.directory/'labels.csv')
    data=f.merge(labels[['case_id','target_crossing_72h']],on='case_id',validate='one_to_one')
    x=np.arange(len(data))
    colors=np.where(data.target_crossing_72h.eq(1),'#bf5b30','#347da3')
    fig,axes=plt.subplots(2,1,figsize=(11,7),sharex=True,layout='constrained')
    axes[0].scatter(x,data.flow_last_cfs,c=colors,s=55)
    axes[0].axhline(450,color='gray',ls='--',lw=1)
    axes[0].set_ylim(0,490)
    axes[0].set_ylabel('Last available flow (cfs)')
    axes[0].set_title('Flow feature cutoff is 15 minutes before each 06:00 UTC decision',fontsize=10)
    axes[1].bar(x,data.qpf_next_24h_mm,color=colors,width=.7)
    axes[1].set_ylabel('Forecast basin precipitation\nnext 24 hours (mm)')
    axes[1].set_xticks(x,pd.to_datetime(data.decision_time_utc,utc=True).dt.strftime('%Y-%m-%d'),rotation=40,ha='right')
    axes[1].set_xlabel('Selected diagnostic cases (categorical spacing, not continuous time)')
    axes[0].legend(handles=[Patch(color='#bf5b30',label='Observed retained crossing within 72h'),
                            Patch(color='#347da3',label='No retained crossing within 72h')],fontsize=9,loc='upper left')
    for ax in axes:ax.grid(axis='y',alpha=.2)
    fig.suptitle('Canyon Creek paired-data pilot — selected cases, no model trained',fontsize=14)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    fig.savefig(args.output,dpi=160)
    plt.close(fig)
    print(args.output)


if __name__=='__main__':
    main()
