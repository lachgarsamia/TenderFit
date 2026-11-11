# Professor demo script (about 10 minutes)

Say first: **"The companies, documents and AI answers here are fictional or scripted. The pipeline around them is real. How accurately a real model reads real tenders has not been measured."**

Setup: see [DEMO.md](DEMO.md#set-up). Run the load the day before; the first load downloads models. Sign in as `brightwell@demo.test`. The password is in `tenderfit/demo/__main__.py`.

| # | Step | Do | Point out | Screenshot |
|---|---|---|---|---|
| 1 | Tender discovery and selection | **Opportunities**, open the Harrowgate tender | Matched to this supplier's products and regions, with a reason | [02](screenshots/02-opportunities.png), [03](screenshots/03-tender.png) |
| 2 | Lot selection | Open **Lot 1**, then the **Lot 2** tab, then back | Each lot is assessed on its own; a tender with many lots stays navigable | [04](screenshots/04-verdict-explanation.png) |
| 3 | Requirement extraction | **Requirements**, then **Documents → Not accepted from extraction** | Each requirement has a source quote. An invented requirement was rejected, and is listed rather than hidden. *Extraction here is scripted* | |
| 4 | Evidence matching | **Hold employers liability insurance**, then **Business profile** | Matched by policy type, not keywords. Declared is not the same as document-backed | [06](screenshots/06-business-profile.png) |
| 5 | Verdict explanation and provenance | **Deliver all desks within 30 days**, then **Read in context** | Required against held, decided by a fixed rule; the clause highlighted on its page | [04](screenshots/04-verdict-explanation.png), [05](screenshots/05-source-provenance.png) |
| 6 | An amendment changes a requirement | Run the command below, then **Changes** | The failure is visible and the earlier result stays. Press **Retry now** | [08](screenshots/08-amendment-failed.png), [09](screenshots/09-amendment-applied.png) |
| 7 | Targeted reassessment | Back to **Lot 1** | Delivery moves from Not met to Needs confirmation. **Public liability also changes though its clause did not**, because the new deadline passes the policy's expiry. Unrelated verdicts are marked unchanged | [10](screenshots/10-after-amendment.png) |
| 8 | Limitations and future research | [LIMITATIONS.md](LIMITATIONS.md), [RESEARCH_ROADMAP.md](RESEARCH_ROADMAP.md) | No real-tender benchmark, no OCR, retrieval benefit unproven. Next: a labelled dataset | |

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml exec api python -m tenderfit.demo amend --fail-first
```

Optional: sign out, sign in as `oakline@demo.test` to show isolation ([11](screenshots/11-isolation-oakline.png)). Oakline does not see Brightwell's private upload, so the Instructions to Tenderers are not read for Oakline.

**Expected question: "How accurate is it?"** Not yet known. The evaluation harness is ready; a labelled dataset is what is missing.
