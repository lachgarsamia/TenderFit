# Migration validation report

**All work used throwaway databases in a local `pgvector/pgvector:pg16` container. No user database was touched.**

| Check | Method | Result |
|---|---|---|
| Ordering | File names 001–018 | Contiguous, sorted by name, 18 files |
| Fresh install | `python -m tenderfit.db migrate` on an empty database | All 18 applied |
| Repeat | Second `migrate` | `applied: nothing` |
| Same schema either way | One at a time vs `migrate`, `pg_dump -s` compared | Identical (2,020 lines) |
| Failure | A broken migration (`test_a_failing_migration_leaves_the_database_unchanged`) | Whole run rolled back |
| Upgrade from 010 | Tests, including repairing rows a failed run left behind | Pass |
| Upgrade from 016 | Test: documents stay readable | Pass |
| **Real upgrade** | Database built and seeded by the code at `2b0bce3` using its own seed script, then upgraded | See below |
| Backup and restore | `pg_dump -Fc`, `pg_restore` of the upgraded copy | Row counts and checksum match |
| Constraints | `pg_constraint` on the result | 0 unvalidated, all 43 tables have primary keys |

## The real upgrade

Migrations 011–018 applied to 10 already-applied migrations with data present.

- **Counts identical before and after** for profiles (3), facts (31), opportunities (2), requirements (16), assessments (6), and verdicts, citations, documents, passages, dependencies, change events and impacts, confirmations and evidence.
- **Assessment id and recommendation checksum identical.**
- 0 `question` rows with no supplier after the backfill.
- 0 memberships: **nobody gains access by upgrading.** After `create-user` and `grant`: sign-in, search, assessment, facts and changes all read correctly.

This used seeded fictional data, not a real deployment, because none exists.

## What each data-changing migration does

| Migration | Effect | Risk |
|---|---|---|
| 010 | Backfills one document column | Low |
| 011 | **Marks assessments left unfinished as `failed`**, and supersedes all but the latest per lot and supplier | Intended; a run in progress during the upgrade is marked failed and can be re-run. Stop the worker first |
| 012 | Existing change events default to `applied` | Low |
| 014 | Fills `question.profile_id` from its assessment; replaces a unique constraint with a per-supplier one | **Uploads made before 014 stay public**, as their owner is unknown |
| 018 | Replaces two status check constraints to allow `partial` | Low. Documents already read are not re-judged, so an old partly scanned PDF stays `read` until re-processed |

Other migrations only add tables, columns, indexes or constraints. Added `NOT NULL` columns all have constant or `now()` defaults, so they need no table rewrite on Postgres 16.

## Findings

- **No downgrade scripts: migrations are forward-only.** Rollback means restoring a backup. Tested above.
- **All pending migrations run in one transaction,** so failure is safe but the locks are held for the whole run. Indexes are not built `CONCURRENTLY`. Plan a short maintenance window on large data. Lock durations were not measured.
- **The old code still runs on the upgraded schema** (profiles, facts and search returned 200 with the `2b0bce3` code). Rolling back the code without the database works for those routes but **restores unauthenticated access**. Only those three were tried.
- **Upgraded assessments have no `verdict_condition` rows** and null new verdict fields until re-assessed. They display, without the new detail.
- **40 foreign keys have no index starting on their column** (for example `requirement.lot_id`, `verdict.requirement_id`). Harmless at this size; it will slow joins and parent deletes at scale. Pre-existing for most. Not changed here, and not measured.
- 73 foreign keys use `NO ACTION`, so deleting a parent fails rather than cascading. 2 cascade, from `app_user`.

## Commands

```bash
bash scripts/test.sh tests/test_migrations.py
```

The real upgrade was run by hand. To repeat it: `git archive 2b0bce3` into a temp folder, run its `python -m tenderfit.db migrate` and `scripts/demo_seed.py` against an empty database, then run this branch's migrate against the same database and compare counts.
