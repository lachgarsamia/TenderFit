import re
from dataclasses import dataclass

CUE = r"(?:see|refer to|referred to in|as set out in|set out in|in accordance with|as described in|detailed in|specified in|defined in|using|per)"
SECTION = re.compile(r"\b(?:section|clause|paragraph|para\.?)\s+(\d{1,2}(?:\.\d{1,2}){0,3})\b", re.I)
PART = re.compile(r"\b(appendix|annex|annexe|schedule|attachment|lot)\s+([A-Z]{1,2}|\d{1,2})\b", re.I)
NAMED = re.compile(
    r"\b(?:the\s+)?(instructions? to (?:tenderers|bidders)|invitation to tender|specification|"
    r"pricing (?:schedule|document)|price schedule|form of tender|selection questionnaire|"
    r"terms and conditions|conditions of contract|clarification log|evaluation criteria)\b",
    re.I,
)
CUED = re.compile(CUE + r"\s+", re.I)


@dataclass(frozen=True)
class Reference:
    kind: str  # section | part | named
    label: str
    raw: str


def find(text: str) -> list[Reference]:
    refs: dict[tuple, Reference] = {}
    for m in SECTION.finditer(text):
        refs[("section", m.group(1))] = Reference("section", f"Section {m.group(1)}", m.group(0))
    for m in PART.finditer(text):
        if m.group(1).lower() == "lot":
            continue
        label = f"{m.group(1).title()} {m.group(2).upper()}"
        refs[("part", label.lower())] = Reference("part", label, m.group(0))
    for m in NAMED.finditer(text):
        label = re.sub(r"\s+", " ", m.group(1)).strip().title().replace(" To ", " to ").replace(" Of ", " of ").replace(" And ", " and ")
        refs[("named", label.lower())] = Reference("named", label, m.group(0))
    return list(refs.values())


def has_unresolved_cue(text: str) -> bool:
    return bool(CUED.search(text) and (SECTION.search(text) or PART.search(text) or NAMED.search(text)))
