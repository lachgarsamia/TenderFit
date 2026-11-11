# TenderFit

Decision support for suppliers responding to UK public tenders. TenderFit finds opportunities on Find a Tender, extracts requirements with source quotes, compares them with an evidence-labelled business profile, explains every verdict with traceable evidence, and re-checks only what an amendment affects.

It is a research prototype. It recommends; a person decides. It never prices, commits or submits a bid.

**It combines two kinds of method on purpose.** Numbers, dates, validity and logic are checked by deterministic code. Language models read what code cannot, under checks they cannot override: quotes must exist in the source, numbers must match it, and a rule's answer binds the model's.

## Architecture

Python 3.12 with FastAPI and raw SQL on Postgres 16 with pgvector. A worker processes a job queue held in Postgres. The web app is React and TypeScript, served by nginx, which proxies `/api/`. Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Demo and real use are different

| | Demo mode | Real use |
|---|---|---|
| Tenders | One fictional tender | Imported from Find a Tender |
| Model answers | **Scripted** | Claude, needs `ANTHROPIC_API_KEY` |
| Accounts | Two fictional accounts with a published password | Created by you |
| What it shows | The system behaves correctly around the model | **Accuracy on real tenders has not been measured** |

## Set up

```bash
cp .env.example .env
```

Set `POSTGRES_PASSWORD` in `.env`. Everything else in [.env.example](.env.example) is commented. Migrations run when the API starts, or with `python -m tenderfit.db migrate`.

## Demo mode

No API key needed.

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
```

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml exec api python -m tenderfit.demo load
```

Open http://localhost:8080. Sign-in details and the walkthrough are in [docs/DEMO.md](docs/DEMO.md); a 10-minute version is in [docs/PROFESSOR_DEMO.md](docs/PROFESSOR_DEMO.md).

## Real use

Set `ANTHROPIC_API_KEY`, then:

```bash
docker compose up -d --build
```

The worker imports from Find a Tender every 4 hours. Set `IMPORT_ENABLED=false` to stop it.

### Authentication

Nobody can sign in until you create an account. Open sign-up is off unless `REGISTRATION_ENABLED=true`.

```bash
docker compose exec api python -m tenderfit.auth create-user you@example.com
```

Then sign in and create your supplier. On a database from before accounts existed, existing suppliers belong to nobody until you grant them:

```bash
docker compose exec api python -m tenderfit.auth list-unowned
```

```bash
docker compose exec api python -m tenderfit.auth grant you@example.com PROFILE_ID
```

Session cookies are `Secure` by default, and Compose does not provide TLS. Put HTTPS in front of it, or set `SESSION_COOKIE_SECURE=false` for plain http on localhost only. **Read [docs/LIMITATIONS.md](docs/LIMITATIONS.md) before using real supplier data.**

## Test

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
.venv/bin/mypy
```

```bash
cd web && npm ci && npm run typecheck && npm test && npm run build
```

```bash
bash scripts/secret_scan.sh
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
| [docs/PROFESSOR_DEMO.md](docs/PROFESSOR_DEMO.md) | Short demonstration script |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | What it does not do, and known risks |
| [docs/VALIDATION_REPORT.md](docs/VALIDATION_REPORT.md) | What was tested, and what was not |
| [docs/API_MIGRATION.md](docs/API_MIGRATION.md) | API changes for existing clients |
| [docs/MIGRATION_VALIDATION.md](docs/MIGRATION_VALIDATION.md) | Database migration results |
| [CHANGELOG.md](CHANGELOG.md) | Changes |
| [NOTES.md](NOTES.md) | Decisions and live API findings |
