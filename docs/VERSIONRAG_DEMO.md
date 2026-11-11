# Reproduce the amendment

## Automated demonstration

With the development environment from the project README installed, run the following from the repository root. The test runner uses a disposable pgvector database. Each arm gets a fresh supplier profile and tender.

```sh
bash scripts/test.sh -s tests/test_versionrag.py
```

The quick run uses deterministic embeddings and reranking. To use the configured MiniLM and cross-encoder, prefix the command with `VERSIONRAG_ENCODERS=production`. The first production run requires local model files or permission to download them. Extraction and verdict generation use the same scripted model in both modes.

Follow the installation requirement. Its source identifies Appendix B, which names Schedule C. Version 1 permits weekdays. Version 2 replaces the timetable with weekends. The supplier's usual installation service now needs confirmation. The test prints retrieved sections, passage paths, changes, the per-lot comparison with a fresh full assessment and the extra requirements reopened.

## Show the two versions in the application

Use a fresh demo database. In the project's local .env set POSTGRES_PASSWORD and VERSIONRAG_ENABLED=true. Start the demo Compose stack described in the README. For a separate project volume, use the same `-p tenderfit-versionrag` flag on both startup and subsequent commands. The published default ports must be available.

```sh
docker compose -p tenderfit-versionrag -f docker-compose.yml -f docker-compose.demo.yml up -d --build
docker compose -p tenderfit-versionrag -f docker-compose.yml -f docker-compose.demo.yml exec api python -m tenderfit.demo load --linked-timetable --fast
```

Sign in as Brightwell using the fictional account in [the demo guide](DEMO.md). Open the Harrowgate tender, Lot 1, then **Install all desks on Working Days**. Expand **Documents consulted and how** and open the timetable passage. The trace identifies its release.

```sh
docker compose -p tenderfit-versionrag -f docker-compose.yml -f docker-compose.demo.yml exec api python -m tenderfit.demo amend --fast --audit
```

Open **Changes**, then reopen the installation verdict. Read the weekend timetable and select **Compare earlier passage** for the former wording. The ordinary application demo also changes lead time and deadline. The automated comparison holds lead time constant to isolate the timetable and deadline changes. The interface exposes recorded evidence paths. A person still decides whether the supplier can satisfy the revised dates.
