# Canyon Creek diagnostic pilot

Twelve deliberately selected cases verify the paired-data pipeline. The six positive rows
come from two observed crossings; this is not representative training or evaluation sampling.
No model was trained.

Use `manifest.json` for column roles and provenance. Only `predictor_columns` belong in a
future model's inputs. `labels.csv` contains future outcomes; `provenance.csv` includes
retrospective case selection and timing audits.

See [the pilot report](../../docs/pilot_2025.md) for timing assumptions, the incomplete 72-hour
precipitation horizon, measured costs, validation and reproduction commands. Raw grids remain
in ignored cache directories.
