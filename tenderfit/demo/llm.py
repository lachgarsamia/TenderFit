"""Scripted model responses for the fictional tender, so the demo needs no API key and costs nothing.

These are not model output. The interface labels every record of this notice as demo data with scripted AI answers. All the
deterministic stages still run for real on these responses: quote checks, condition validation, rules, guards, dependencies."""
import json
import re

from tenderfit.llm import JsonResult, LLMError, ToolTurn, Usage


def c(kind: str, fact: str, **kw) -> dict:
    return {"kind": kind, "fact": fact, "bound": None, "years": None, "anchor": None, "min_units": None, "match": None,
            "qualifier": None, "unit": None, **kw}


def r(pid: int, lot: str | None, statement: str, quote: str, category: str = "other", obligation: str = "mandatory", condition: dict | None = None,
      **kw) -> dict:
    return {"passage_id": pid, "lot": lot, "statement": statement, "category": category, "obligation": obligation, "condition": condition,
            "equivalent_allowed": False, "evidence_needed": [], "quote": quote, "section": None, "page": None, "references": [],
            "defined_terms": [], "scope": None, "conditions": [], **kw}


def _passages(user: str) -> list[tuple[int, str, str]]:
    return [(int(m.group(1)), re.sub(r"\s+", " ", m.group(3)), m.group(2))
            for m in re.finditer(r'<tender_passage id="(\d+)"[^>]*?section="([^"]*)"[^>]*>\n(.*?)\n</tender_passage>', user, re.S)]


def _extract(user: str) -> dict:
    out: list[dict] = []
    for pid, text, section in _passages(user):
        days = re.search(r"deliver all desks within (\d+) days of order", text)
        if days:
            d = days.group(1)
            out.append(r(pid, "1", f"Deliver all desks within {d} days of order", f"The supplier shall deliver all desks within {d} days of order.",
                         "delivery", condition=c("at_most", "capacity.lead_days", bound=int(d), qualifier="desks")))
        if "shall supply 200 student desks" in text:
            out.append(r(pid, "1", "Supply 200 student desks", "The supplier shall supply 200 student desks for Lot 1.", "quantity",
                         condition=c("at_least", "capacity.max_units", bound="lot.quantity", qualifier="desks")))
        if "shall supply 400 chairs" in text:
            out.append(r(pid, "2", "Supply 400 chairs", "The supplier shall supply 400 chairs for Lot 2.", "quantity",
                         condition=c("at_least", "capacity.max_units", bound="lot.quantity", qualifier="chairs")))
        if "Spare parts shall" in text:
            # Scripted to propose a wrong number on purpose: validation must refuse to turn it into a rule.
            out.append(r(pid, None, "Spare parts available for dispatch within 10 days", "Spare parts shall be available for dispatch within 10 days of a request",
                         "delivery", condition=c("at_most", "capacity.lead_days", bound=14)))
            # And an invented requirement whose number is not in the document: rejected, and listed for the supplier.
            out.append(r(pid, None, "Spare parts available for 25 years", "Spare parts shall be available for 25 years after delivery of the desks",
                         "other"))
        if "Installation of all desks" in text:
            out.append(r(pid, "1", "Install all desks on Working Days", "Installation of all desks shall take place on Working Days as defined in Appendix B.",
                         references=["Appendix B"], defined_terms=["Working Days"], scope="installation"))
        w = re.search(r"warranty of (\d+) years on all desks", text)
        if w and section.startswith("4.1"):  # the ITT states it differently; that is what the contradiction check must find
            out.append(r(pid, "1", f"Provide a warranty of {w.group(1)} years on all desks", f"The supplier shall provide a warranty of {w.group(1)} years on all desks."))
        for kind in ("public", "employers"):
            m = re.search(rf"{kind} liability insurance of at least £([\d,]+)", text)
            if m:
                out.append(r(pid, None, f"Hold {kind} liability insurance of at least £{m.group(1)}", f"Bidders must hold {kind} liability insurance of at least £{m.group(1)}.",
                             "insurance", condition=c("min_amount", "insurance.amount_gbp", bound=int(m.group(1).replace(",", "")), qualifier=f"{kind} liability")))
        if "ISO 14001 certification" in text:
            out.append(r(pid, None, "Hold ISO 14001, valid for the contract, from an accredited body",
                         "Suppliers must hold ISO 14001 certification, valid throughout the contract period, issued by an accredited certification body.",
                         "certificate", evidence_needed=["certificate"], conditions=[
                             {"label": "Certified to ISO 14001", "mandatory": True, "quote": "Suppliers must hold ISO 14001 certification",
                              "condition": c("has_evidence", "certificate", match="ISO 14001"), "evidence_needed": ["certificate"]},
                             {"label": "Valid throughout the contract period", "mandatory": True,
                              "quote": "ISO 14001 certification, valid throughout the contract period",
                              "condition": c("valid_through", "certificate", match="ISO 14001", anchor="lot.contract_end"), "evidence_needed": []},
                             {"label": "Issued by an accredited body", "mandatory": True, "quote": "issued by an accredited certification body",
                              "condition": None, "evidence_needed": ["accreditation mark"]}]))
        if "BS EN 1729-2" in text:
            out.append(r(pid, "1", "Desks comply with BS EN 1729-2 or an accepted equivalent",
                         "Desks shall comply with BS EN 1729-2 or an equivalent standard accepted by the Trust.", "standard",
                         equivalent_allowed=True, evidence_needed=["test report"], condition=c("has_evidence", "test_report", match="BS EN 1729-2")))
        if "comparable contract" in text:
            out.append(r(pid, None, "A comparable contract of 150+ items in the last 3 years, with a reference",
                         "at least one comparable contract for 150 or more items delivered in the last three years, with a customer reference.",
                         "experience", evidence_needed=["customer reference"],
                         condition=c("within_years", "experience.end", years=3, anchor="notice.deadline", min_units=150)))
        if "recycled material" in text:
            out.append(r(pid, "1", "Share of recycled material in the desks (scored)", "The share of recycled material in the desks will be scored",
                         obligation="scored"))
    return {"requirements": out}


def _facts(user: str) -> list[dict]:
    return json.loads(re.split(r"\n\n(?:Passages:|User answers|Judge only)", user.split("validity):\n", 1)[1], maxsplit=1)[0])


def _verdict(user: str) -> dict:
    facts = _facts(user)
    pid = int(re.search(r"\(passage (\d+)\)", user).group(1))
    ids = lambda attr: [f["id"] for f in facts if f["attribute"] == attr]  # noqa: E731

    def out(status, reason, support=None, quote=None, question=None, conflict=False):
        return {"status": status, "reason": reason, "supporting_fact_ids": support or [], "contrary_fact_ids": [],
                "citations": [{"passage_id": pid, "quote": quote}] if quote else [], "question": question, "conflict": conflict}
    flat = re.sub(r"\s+", " ", user)
    if "Install all desks" in user:
        hours = "Working Days means Monday to Friday" in flat
        if not ids("service"):
            return out("unknown", "No installation service in the profile.", None, None, "Do you install furniture?")
        if not hours:
            return out("needs_confirmation", "Working Days are not defined as Monday to Friday here, so your usual hours may not fit.", ids("service"),
                       "Installation of all desks shall take place on Working Days as defined in Appendix B.", "Can you install on the days this tender defines?")
        return out("met", "Your declared installation service fits: Appendix B defines Working Days as Monday to Friday.", ids("service"),
                   "Installation of all desks shall take place on Working Days as defined in Appendix B.")
    if "accredited" in user and "Hold ISO 14001" in user:
        certs = [f for f in facts if "14001" in str(f["value"])]
        return out("needs_confirmation", "The certificate on file does not state the accreditation of the issuing body.", [f["id"] for f in certs],
                   None, "Is the issuing body accredited by UKAS or an equivalent national body?")
    if "Spare parts" in user:
        return out("unknown", "Nothing in the profile says how quickly spare parts can be dispatched.", None,
                   "Spare parts shall be available for dispatch within 10 days of a request", "How quickly can you dispatch spare parts?")
    if "warranty" in user.lower():
        q = re.search(r"The supplier shall provide a warranty of \d+ years on all desks\.", flat)
        return out("unknown", "The profile says nothing about the warranty you offer.", None, q.group(0) if q else None,
                   "What warranty do you offer on desks?")
    return out("unknown", "No fact in the profile addresses this.", None, None, "What share of recycled material do your desks contain?")


class DemoLLM:
    """Implements the LLM protocol with scripted answers for the fictional notice. Any other notice gets no requirements."""
    failing = 0

    def __init__(self) -> None:
        self.calls = 0

    def json(self, role: str, system: str, user: str, schema: dict) -> JsonResult:
        self.calls += 1
        if DemoLLM.failing > 0 and role == "extract":
            DemoLLM.failing -= 1
            raise LLMError("provider error 529: overloaded (simulated for the demonstration)")
        return JsonResult(_extract(user) if role == "extract" else _verdict(user), Usage("scripted-demo", 0, 0))

    def tool_turn(self, role: str, system: str, messages: list[dict], tools: list[dict]) -> ToolTurn:
        raise LLMError("the scripted demonstration does not run the agent")
