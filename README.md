# TenderFit

Supplier-side assistant for open UK public tenders (Find a Tender OCDS API): finds opportunities, extracts requirements with source quotes, compares them with an evidence-labelled business profile, and re-assesses only what an amendment affects.

## Run

```
cp .env.example .env
docker compose up -d
```

## Test

```
pip install -e '.[dev]'
pytest
```
