# TenderFit

Supplier-side assistant for open UK public tenders (Find a Tender OCDS API): finds opportunities, extracts requirements with source quotes, compares them with an evidence-labelled business profile, and re-assesses only what an amendment affects.

## Run

```
cp .env.example .env          # add ANTHROPIC_API_KEY for extraction and verdicts
docker compose up -d          # Postgres (pgvector), API on :8000, worker
```

Local development:

```
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
docker compose up -d db
.venv/bin/python -m tenderfit.ingest incremental --backfill-days 14
.venv/bin/python -m tenderfit.profile.seed
.venv/bin/uvicorn tenderfit.api.main:app --port 8000   # docs at /docs
.venv/bin/python -m tenderfit.jobs.worker
```

Web app (screens from the design, Figs. 2 and 9–13):

```
cd web && npm install && npm run dev      # http://localhost:5173, proxies /api to :8000
```

`scripts/demo_seed.py` loads the fictional Northfield running example with scripted model responses, so the
screens can be explored without an API key; those records are labelled fictional in the UI.

Read-only demo page (no server needed): `python scripts/demo_seed.py --reset`, then
`python scripts/snapshot_demo.py` with the API running, then `python scripts/build_demo.py` (writes one
self-contained HTML file to `web/dist-demo/`).

Evaluation: `python -m tenderfit.eval run eval/datasets/<name> --configs B1,B2,B3,B4,B4-full [--live]`
(see `eval/datasets/README.md`).

Scripts: `scripts/gate_r0.sh` (import replay gate), `scripts/replay_check.py`, `scripts/source_study.py fetch|report`.

## Test

```
.venv/bin/pytest
```

Decisions, deviations from the design and open questions are in `NOTES.md`.
