# API migration note

For anyone calling the API from `main` at `2b0bce3`. The contract is in [openapi.json](openapi.json); `tests/test_api_contract.py` fails if it falls out of date. It was compared with the OpenAPI document generated from the `2b0bce3` code.

## What every client must change

| Change | Before | Now |
|---|---|---|
| Base path | `/search` | `/v1/search`. Through nginx: `/api/v1/search`. `/health` is unchanged |
| Identity | None | Sign in with `POST /v1/auth/login`, keep the `tf_session` cookie. No session: `401` |
| Supplier | `profile_id` in the query or body | Taken from the session. Choose among your own with `POST /v1/auth/active-profile` |
| Actor | `user` in the body | The signed-in account |
| State-changing requests | Nothing | Header `X-TenderFit: 1`, or `403` |
| Another supplier's resource | Returned | `404`, the same as a resource that does not exist |

```bash
curl -c jar -H 'X-TenderFit: 1' -H 'content-type: application/json' -d @login.json http://localhost:8000/v1/auth/login
```

```bash
curl -b jar -H 'X-TenderFit: 1' -H 'content-type: application/json' -d '{"q":"desks"}' http://localhost:8000/v1/search
```

There is no compatibility layer: old paths return `404`. This is deliberate. Keeping them would keep the unauthenticated access this work removes. No external clients are known, and the frontend in this repository has been moved.

## Endpoint by endpoint

All are **breaking** by the table above. "Same" means no other change to the request. Responses keep their existing fields unless noted.

| Previous | Now | Request change | Response change |
|---|---|---|---|
| `GET /health` | Same | None | None. Not breaking |
| `GET /profiles` | `GET /v1/profiles` | Same | Only your suppliers, not all of them |
| `GET /profile/facts?profile_id` | `GET /v1/profile/facts` | Remove `profile_id` | Same |
| `PUT /profile/facts/{id}` | `PUT /v1/profile/facts/{id}` | `confirmed_at`, `confirm_by` removed (the server sets them); `clear_expiry` added | Adds `jobs`, `detached_from_documents` |
| `POST /profile/evidence` | `POST /v1/profile/evidence` | Remove `profile_id` form field | Same |
| `GET /profiles/{id}/evidence-impact` | `GET /v1/profile/evidence-impact` | Path changed | Same |
| `GET /profiles/{id}/pursued` | `GET /v1/profile/pursued` | Path changed | Same |
| `POST /search` | `POST /v1/search` | Remove `profile_id` | Same |
| `GET /opportunities` | `GET /v1/opportunities` | Same | Same |
| `GET /lots/{id}/assessment?profile_id` | `GET /v1/lots/{id}/assessment` | Remove `profile_id` | Adds processing state, failure, version and condition fields |
| `POST /lots/{id}/assess` | `POST /v1/lots/{id}/assess` | **No body**: `profile_id` and `wait` removed. Always returns queued jobs; poll `GET /v1/jobs/{id}` | `jobs` |
| `GET /lots/{id}/documents` | `GET /v1/lots/{id}/documents` | Same | Adds `needs_ocr`, `page_count`, `ocr_pages`; `status` may be `partial` |
| `POST /lots/{id}/documents` | `POST /v1/lots/{id}/documents` | Remove `user` | Same |
| `GET /lots/{id}/preparation?profile_id` | `GET /v1/lots/{id}/preparation` | Remove `profile_id` | Same |
| `POST /confirmations` | `POST /v1/confirmations` | Remove `user`, `profile_id` | Same |
| `POST /questions/{id}/answer` | `POST /v1/questions/{id}/answer` | Remove `user` | Same |
| `GET /changes?profile_id` | `GET /v1/changes` | Remove `profile_id` | Adds status, attempts, last failure |
| `GET /jobs/{id}` | `GET /v1/jobs/{id}` | Same | `404` if not your supplier's job |
| `GET /assessments/{id}/trace` | `GET /v1/assessments/{id}/trace` | Same | `404` if not your supplier's assessment |

## New

`/v1/auth/config`, `register`, `login`, `logout`, `me`, `active-profile`; `POST /v1/profiles`; `PUT /v1/profile`; `POST /v1/profile/facts`; `POST /v1/profile/facts/{id}/renew`, `/verify`; `POST /v1/changes/{id}/retry`; `GET /v1/import-status`, `/v1/lots/{id}`, `/v1/tenders/{id}`, `/v1/passages/{id}`, `/v1/documents/{id}/file`.

## Versioning

`/v1` is the first version. Add fields freely; a removal or a change of meaning needs `/v2`. There is no support window, because no external clients are known.

## Operator steps on upgrade

1. Back up the database.
2. Start the new code, or run `python -m tenderfit.db migrate`.
3. `python -m tenderfit.auth create-user EMAIL`
4. `python -m tenderfit.auth list-unowned`
5. `python -m tenderfit.auth grant EMAIL PROFILE_ID`
6. Set `SESSION_COOKIE_SECURE=false` only if serving plain http on localhost.
