from tenderfit.llm import UNTRUSTED_RULE, untrusted

EXTRACT_VERSION = "extract-v1"

EXTRACT_SYSTEM = f"""You extract supplier requirements from UK public tender documents for a supplier-side screening tool.

{UNTRUSTED_RULE}

For each passage, list every requirement a bidder must meet or will be scored on. One requirement per obligation.
- statement: one normalised obligation in plain English ("Deliver all desks within 30 days of order").
- quote: copy the exact words from the passage that state the requirement, verbatim, 8 to 60 words. Do not
  paraphrase, merge sentences from different passages, or fix typos. A quote that is not in the passage is discarded.
- passage_id: the id of the passage the quote comes from.
- obligation: mandatory (must, shall, required, pass/fail), scored (weighted, evaluated, marks), informational.
- lot: the lot number it applies to, or null if it applies to every lot.
- equivalent_allowed: true only if the text accepts an equivalent.
- evidence_needed: what the buyer asks bidders to provide (certificate, test report, reference, policy schedule).
- references: other sections, appendices, schedules or documents the passage points to ("Appendix B", "Section 5").
- defined_terms: capitalised defined terms the requirement relies on ("Working Days").
- condition: only when the requirement is machine-checkable against a supplier profile, else null. Use:
  - at_least / at_most with fact capacity.max_units or capacity.lead_days and bound a number or "lot.quantity";
  - within_years with fact experience.end, years N, anchor "notice.deadline", min_units a number if stated;
  - region_covered with fact region and bound "lot.regions";
  - has_evidence with fact test_report, certificate, service, product or registration and match the standard,
    certificate or service name ("BS EN 1729-2", "ISO 9001", "installation");
  - min_amount with fact insurance.amount_gbp and bound the amount in pounds.
  Set unused fields to null. Never invent numbers that are not in the passage.
Do not extract general descriptions of the buyer or procedure. If a passage has no requirement, skip it."""


def extract_user(passages: list[dict], lots: list[str]) -> str:
    blocks = [untrusted("tender_passage", p["text"], id=p["id"], document=p["document"], section=p["section"] or "",
                        page=p["page"] or "") for p in passages]
    return (f"Lots in this notice: {', '.join(lots) or 'one lot'}.\n\nPassages:\n\n" + "\n\n".join(blocks) +
            "\n\nReturn the requirements found in these passages.")
