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

Scripts: `scripts/gate_r0.sh` (import replay gate), `scripts/replay_check.py`, `scripts/source_study.py fetch|report`.

## Test

```
.venv/bin/pytest
```

Decisions, deviations from the design and open questions are in `NOTES.md`.
