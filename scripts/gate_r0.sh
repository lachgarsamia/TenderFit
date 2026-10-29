#!/usr/bin/env bash
# R0 gate: two-week live import, identical re-import adds nothing, incremental run, replay reproduces state.
set -euo pipefail
PY=${PY:-python}
$PY -m tenderfit.ingest incremental --backfill-days 14
read -r FROM TO < <($PY -c "
from tenderfit.db import connect
r = connect().execute('SELECT window_from, window_to FROM import_run ORDER BY id LIMIT 1').fetchone()
print(*(r[k].strftime('%Y-%m-%dT%H:%M:%S') for k in ('window_from', 'window_to')))")
$PY -m tenderfit.ingest window --from "$FROM" --to "$TO"
$PY -m tenderfit.ingest incremental
$PY scripts/replay_check.py
