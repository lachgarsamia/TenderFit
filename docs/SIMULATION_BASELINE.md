# Stage 2 baseline: checks before any simulation work

Branch `feature/isolated-simulations` at `6669600` (= `origin/main`), with no application code changed. Run on 2026-11-15
on macOS (Darwin 24.5), Python 3.12.7 (`.venv`), Node 24.2.0 / npm 11.8.0, Docker 27.4.0.

Every row below was actually executed and its result observed. The commands are the ones documented in `README.md` and used in
`.github/workflows/ci.yml`.

| Check | Command | Result |
|---|---|---|
| Lint | `.venv/bin/ruff check tenderfit tests scripts` | **pass**: "All checks passed!" |
| Types | `.venv/bin/mypy` | **pass**: "no issues found in 67 source files" |
| Backend tests | `bash scripts/test.sh` (pgvector/pgvector:pg16 on 127.0.0.1:55432, `REQUIRE_DB=1`) | **pass**: 361 passed, 0 failed, 0 errors, **0 skipped**, 138 s |
| Migrations | empty DB, then `python -m tenderfit.db migrate` twice | **pass**: run 1 applied 001–018, run 2 "applied: nothing" |
| Web typecheck | `npm run typecheck` (`tsc -b`) | **pass**: exit 0 |
| Web tests | `npm test` (vitest) | **pass**: 14/14, in 1 file |
| Web build | `npm run build` | **pass**: exit 0, 47 modules, JS 249 kB (78 kB gzip) |
| Secret scan | `bash scripts/secret_scan.sh` (gitleaks v8.18.4) | **pass**: history 0 findings; tree 20 raw, 0 outside dependencies/caches |
| Compose config | `docker compose … config -q` (CI job) | **not run**: it needs `.env` replaced by `.env.example`, and a real `.env` exists locally that I didn't touch |

There are no pre-existing failures.

## Tests that guard the areas the simulation work touches

| Module | Tests | What it protects |
|---|---|---|
| `test_rules`, `test_rules_regression` | 15 + 30 | the deterministic rule engine (`rules/engine.py`) |
| `test_pipeline` | 3 | `assess_lot` / `decide` end to end |
| `test_soundness` | 26 | guard and verdict soundness invariants |
| `test_assessment_lifecycle` | 13 | running/done/failed, the previous assessment surviving failures |
| `test_isolation` | 25 | supplier-to-supplier isolation; the `world` fixture to reuse |
| `test_changes_impact`, `test_changes_apply`, `test_amendment_retry` | 4 + 4 + 10 | dependency planning and amendment processing |
| `test_versionrag`, `test_versionrag_eval` | 4 + 8 | version-pinned retrieval and the shadow comparison |
| `test_agent` | 4 | the agent loop |
| `test_migrations` | 7 | the schema migration runner |

Any refactor of `pipeline.decide()` (Stage 5) must keep all of these green, unchanged.

## Environment notes

- The backend tests need Docker. The local Homebrew Postgres on 5432 has no `tenderfit` role and wasn't used or changed.
- Starting Docker Desktop also restarted an existing compose stack, `tenderfit-release-*` (db on 5433, api 8000, worker,
  web 8080), because its containers use `restart: unless-stopped`. This work didn't touch it.
- `scripts/test.sh` leaves the `tenderfit-testdb` container running for reuse. The migration check left a
  `tenderfit_ci_migrate` database inside that throwaway container.
- Warnings only: React Router v7 future-flag notices in the web tests; a SWIG `DeprecationWarning` at interpreter exit.
