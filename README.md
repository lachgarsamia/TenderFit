# TenderFit

Decision support for suppliers responding to UK public tenders. TenderFit finds opportunities on Find a Tender, extracts requirements with source quotes, compares them with an evidence-labelled business profile, explains every verdict with traceable evidence, and re-checks only what an amendment affects.

It is a research prototype. It recommends; a person decides. It never prices, commits or submits a bid.

**It combines two kinds of method on purpose.** Numbers, dates, validity and logic are checked by deterministic code. Language models read what code cannot, under checks they cannot override: quotes must exist in the source, numbers must match it, and a rule's answer binds the model's.

## See it working

No API key needed. This is fictional data and scripted model answers, labelled as such in the interface, running through the real pipeline.

```bash
cp .env.example .env
```

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
```

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml exec api python -m tenderfit.demo load
```

Open http://localhost:8080. The full walkthrough is in [docs/DEMO.md](docs/DEMO.md).

## Run against real tenders

Set `POSTGRES_PASSWORD` and `ANTHROPIC_API_KEY` in `.env`, then:

```bash
docker compose up -d --build
```

The worker imports from Find a Tender every 4 hours. Set `IMPORT_ENABLED=false` to stop it. **No screen creates a real supplier profile yet, and there is no login.** See [docs/LIMITATIONS.md](docs/LIMITATIONS.md) before using real supplier data.

## Develop

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

```bash
bash scripts/test.sh
```

```bash
.venv/bin/ruff check tenderfit tests scripts
```

```bash
cd web && npm ci && npm run build
```

`scripts/test.sh` starts a throwaway pgvector container. With `REQUIRE_DB=1`, which it and CI both set, a missing database fails the run rather than skipping tests.

## Evaluate

```bash
python -m tenderfit.eval regression
```

```bash
python -m tenderfit.eval run eval/datasets/<name> --configs B0,B1,B2,B3,B4,B5,B5-full --live
```

The first is a synthetic amendment suite with a scripted model. The second needs a labelled dataset and an API key. No real-world benchmark exists yet; see [docs/RESEARCH_ROADMAP.md](docs/RESEARCH_ROADMAP.md).

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, assessment pipeline, data model, guarantees |
| [docs/DEMO.md](docs/DEMO.md) | Demonstration script with expected results |
| [docs/RESEARCH_ROADMAP.md](docs/RESEARCH_ROADMAP.md) | Research questions and how to test them |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | What it does not do, and known risks |
| [docs/VALIDATION_REPORT.md](docs/VALIDATION_REPORT.md) | What was implemented and tested in this iteration |
| [NOTES.md](NOTES.md) | Decisions and live API findings |
