# Demonstration guide

Everything is fictional: Harrowgate Education Trust, Brightwell Furniture, Oakline Seating, and every document and certificate. Model answers are **scripted**, and the interface says so. Everything else is the real system: parsing, quote checks, validation, rules, retrieval, dependencies, worker and API. Dates are relative to the day you load it. Say this at the start; it is the honest framing, and it lets you demonstrate without an API key.

**This shows that the machinery works. It does not show how well a real model reads real tenders.** That has not been measured.

## Set up

```bash
cp .env.example .env
```

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
```

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml exec api python -m tenderfit.demo load
```

The first load downloads the embedding and reranking models, so run it before the meeting. Open http://localhost:8080 and sign in as `brightwell@demo.test` or `oakline@demo.test`. The shared password is the `DEMO_PASSWORD` constant in `tenderfit/demo/__main__.py`. Those accounts exist only because of `demo load`, which refuses to run outside demo mode. Set `DEMO_PASSWORD` to choose your own.

To start again, remove the tender and load it:

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml exec api python -m tenderfit.demo reset
```

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml exec api python -m tenderfit.demo load
```

A short version of this script is in [PROFESSOR_DEMO.md](PROFESSOR_DEMO.md).

## Script (about 15 minutes)

### 1. The problem (1 min)
A supplier reads a long pack across several documents to answer one question: can we bid, and what stands in the way? Keyword matching finds tenders; it cannot say whether "£5m employers' liability" is met by a £10m public liability policy.

### 2. Opportunities (1 min)
Sign in as Brightwell and open **Opportunities**. The list is matched to the supplier's own products and regions, with a reason per lot. Open the tender.

*Point out:* the tender page lists both lots, the documents, and which were readable.

### 3. A blocker that can be checked (2 min)
Open **Lot 1**. Recommendation: *Blocker found*. Five dimensions, no single score.

Select **Deliver all desks within 30 days**: Not met · Fixed rule · Contradicted. Required: at most 30 days. You have: 45 days.

*Talking point:* no AI judgement decided this. Press **Read in context** to see the clause highlighted, then open the PDF at the page.

### 4. Neuro-symbolic matching (2 min)
Select **Hold employers liability insurance**. Brightwell holds a £10m public liability policy *and* a £10m employers' policy, declared only. The rule matched the right policy by type, and asks for proof.

Switch to **Lot 2 · Seating** and **Supply 400 chairs**: Unknown. Brightwell has capacity for 300 *desks*, and that does not count for chairs.

*Talking point:* a previous version of the rule engine said MET for both kinds of case. There is a regression table of these, and property tests that listing order never changes a verdict.

### 5. Decomposition (2 min)
Back in Lot 1, select **Hold ISO 14001**. Three conditions, judged separately: the standard (rule, met), validity through the contract (rule, needs confirmation: the certificate ends first), the accredited body (read by the model).

*Talking point:* the model only judged the third, and cannot lift a condition a rule did not accept.

### 6. Graph-enhanced retrieval (1 min)
Select **Install all desks on Working Days**, open **Documents consulted and how**: Appendix B was retrieved *because this requirement refers to Appendix B*. It is one typed hop in Postgres, not a graph database.

*Be careful:* this is a working mechanism. Whether it improves answers over similarity search alone is an open experiment.

### 7. Refusing to decide (2 min)
- **Provide a warranty of 5 years:** the Specification says 5 years, the ITT says 3. Needs confirmation; both are cited; the next step is to ask the buyer. This check is deterministic.
- **Spare parts:** the scripted extractor proposed a threshold of 14 days for a clause that says 10. Validation refused to make it a rule, and says so.
- Under **Documents → Not accepted from extraction**: an invented "25 years" requirement, rejected because the quote is not in the source, and listed rather than hidden.

### 8. Evidence is not proof (1 min)
**Business profile:** expand a *Checked* line. It shows what the file's text shows: supplier name, policy type, amount, expiry. "Confirmed by a person" is separate. Say plainly: these checks read text; they cannot establish that a document is genuine.

### 9. Amendment, failure and recovery (3 min)
The buyer changes delivery to 45 days and moves the deadline two weeks.

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml exec api python -m tenderfit.demo amend --fail-first
```

Open **Changes**: *Could not finish*. Open Lot 1: the earlier result is still there, labelled as for the earlier version.

*Talking point:* previously a failure here lost the amendment for good.

Press **Retry now** on Changes, and wait a few seconds. Then show:
- **Deliver all desks:** Not met → Needs confirmation, because the clause changed.
- **Public liability:** Met → Needs confirmation. **Nothing in that clause changed.** The new deadline falls after the policy ends, and the verdict depends on the deadline through the fact's date. This is the implicit dependency.
- 6 verdicts were kept, and both lots are reported.

Lot 1's recommendation moves from *Blocker found* to *Relevant: needs confirmation*.

### 10. Supplier isolation (1 min)
Sign out, and sign in as **Oakline**. The ITT is *not read* for Oakline, because Brightwell's copy is private to Brightwell, and Oakline has no requirements from it.

### 11. Evidence behind the claim (1 min)
```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml exec api python -m tenderfit.eval regression
```
This needs a database admin login that can create a database, and takes under a minute. Or show `docs/VALIDATION_REPORT.md`. Eight controlled amendments compared with full reassessment as the reference: dependencies known only from extraction missed the deadline case; recorded dependencies did not, and cost more reassessment. Scripted model, synthetic edits: **evidence about the bookkeeping, not a benchmark**.

### 12. What remains open (1 min)
See [RESEARCH_ROADMAP.md](RESEARCH_ROADMAP.md). The honest state: mechanisms are built and tested; no accuracy has been measured on real tenders with a real model.

## Likely questions

**Is this GraphRAG?** A small, typed version: one hop along links the requirement itself names. It deliberately has no community summaries or graph database. Its value is still to be measured.

**How accurate is it?** Not yet known. The harness is ready; a labelled dataset is the missing piece.

**What stops the model making things up?** Quotes must be in the source and their digits must match exactly. Rule inputs must trace to the quote. MET and NOT MET need current facts. The model only ever sees text as untrusted data. It can still misread text in ways these checks do not catch, which is why a person decides.

**Why not use the model for everything?** Dates and numbers should not depend on one. It also makes verdicts reproducible and cheap.
