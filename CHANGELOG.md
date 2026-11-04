# Changelog

Changes on `release-hardening` compared with `main` at `2b0bce3`. Nothing here has been released.

## Breaking

- **Every route except `/health` moved under `/v1` and needs a signed-in session.** See [docs/API_MIGRATION.md](docs/API_MIGRATION.md).
- **The supplier comes from the session.** `profile_id` and `user` are gone from every request.
- **Upgraded databases have no accounts.** Nobody can read an existing profile until an operator runs `python -m tenderfit.auth grant`.
- **`replay` no longer truncates anything.** The old behaviour needs `--destructive-rebuild --confirm-database NAME`.
- **Document status gains `partial`.** A scanned PDF with a text cover page used to count as read.

## Added

- Accounts: Argon2id passwords, hashed session tokens in an HttpOnly cookie, per-supplier membership, a sign-in lockout, a required `X-TenderFit` header on state-changing requests, and optional sign-up (off by default).
- Operator commands: `python -m tenderfit.auth create-user | grant | list-unowned`.
- Supplier onboarding: create and edit a supplier, add and correct facts, set expiry dates, with sign-in screens in the web app.
- Page-level PDF status (`text`, `needs_ocr`, `blank`). No OCR is performed.
- Frontend tests (Vitest, Testing Library), `docs/openapi.json` with a test that keeps it current, `scripts/secret_scan.sh`, `scripts/export_openapi.py`.
- Assessment work from the previous iteration, also not on `main`: crash-safe lifecycle, retryable amendments, condition decomposition, reference-following retrieval, contradiction detection, evidence checks, per-verdict dependencies, demo, redesigned screens. See `docs/ARCHITECTURE.md`.

## Changed

- CI now runs mypy, a real migration with an idempotence check, fails on any skipped test, runs frontend type checks and tests, and scans the whole Git history. It has **not yet run on GitHub**.
- `mypy` reports 0 errors (333 on `main` with the same settings). `mypy` is now in the dev dependencies.
- `react-router-dom` 6.26.2 to 6.30.6 and `vite` 5.4.10 to 5.4.21, for security advisories. Some advisories remain; see `docs/LIMITATIONS.md`.
- The read-only demo snapshot now signs in as the fictional accounts.

## Fixed

- A caller could read or change any supplier by passing its `profile_id`.
- Assessment jobs could be read by any caller.
- A corrupt evidence upload surfaced as a server error rather than a clear refusal.
- Changing a fact's value kept the old documents counting as proof. They are now detached, and the screen says so.
