# Baseline before this iteration

Recorded on 2026-11-03 at commit `2b0bce3`, branch `main`, clean tree, 10 migrations, against PostgreSQL 16.15 with pgvector in a throwaway container.

| Check | Result |
|---|---|
| `pytest` (104 tests) | **103 passed, 1 failed, 0 skipped**, 5m22s |
| The failure | `test_assessment_endpoint_and_confirmations`: the fixture's fixed deadline (2026-10-30) had passed, so status read `closed`. A date bug in the test, not the product |
| `ruff check` | 122 findings (no project configuration) |
| `mypy` | 333 errors (not configured) |
| Frontend `tsc -b` and `npm run build` | Both passed |
| `docker compose config` | Passed once `.env` existed; the published database port 5432 clashes with a local Postgres on this machine |
| CI on GitHub | None |
| Skips | `conftest.py` turned a missing database into a skip, so most important tests could vanish silently |
| External needs | Docker or Postgres 13+ with pgvector; `ANTHROPIC_API_KEY` for real extraction and verdicts; model downloads from Hugging Face |
