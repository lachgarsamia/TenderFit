"""Checks on what the model extracted. Nothing it generated becomes a trusted rule input until it passes here."""
import re
from dataclasses import dataclass

from pydantic import ValidationError

from tenderfit.documents.text import normalise
from tenderfit.rules.engine import Condition, insurance_type_in

ALLOWED_FIELDS = {"lot.quantity", "lot.regions", "lot.contract_end", "notice.deadline"}
KIND_FACTS = {
    "at_least": {"capacity.max_units"},
    "at_most": {"capacity.lead_days"},
    "within_years": {"experience.end"},
    "region_covered": {"region"},
    "has_evidence": {"test_report", "certificate", "service", "product", "registration"},
    "min_amount": {"insurance.amount_gbp"},
    "valid_through": {"certificate", "insurance", "test_report", "registration"},
}
UNITS = {"working days": re.compile(r"\bworking days?\b|\bbusiness days?\b", re.I), "weeks": re.compile(r"\bweeks?\b", re.I),
         "months": re.compile(r"\bmonths?\b", re.I), "days": re.compile(r"\bdays?\b", re.I)}
ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
        "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
SCALES = {"hundred": 100, "thousand": 1_000, "million": 1_000_000}
NUMBER_WORD = "|".join([*ONES, *TENS, *SCALES, "and"])
WORDS = re.compile(rf"\b(?:{NUMBER_WORD})(?:[\s-]+(?:{NUMBER_WORD}))*\b", re.I)
DIGITS = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(k|m|bn|thousand|million|billion)?\b", re.I)
MULTIPLIER = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}


def digit_sequences(text: str) -> set[str]:
    """Every digit run, with thousands separators removed: '5,000,000' and '5000000' are the same sequence."""
    return set(re.findall(r"\d+", re.sub(r"(?<=\d),(?=\d)", "", text)))


def same_digits(quote: str, source: str) -> bool:
    """Fuzzy quote matching forgives one wrong character; a wrong character in a number is a different requirement."""
    return digit_sequences(quote) <= digit_sequences(source)


def _word_value(words: str) -> float | None:
    total, current = 0, 0
    for w in re.split(r"[\s-]+", words.lower()):
        if w == "and":
            continue
        if w in ONES:
            current += ONES[w]
        elif w in TENS:
            current += TENS[w]
        elif w == "hundred":
            current = max(current, 1) * 100
        elif w in SCALES:
            total += max(current, 1) * SCALES[w]
            current = 0
    return float(total + current) or None


def numbers_in(text: str) -> set[float]:
    """Numbers a text states, in digits or words: '£5m', '5,000,000', 'thirty', 'one hundred and fifty'."""
    out: set[float] = set()
    for m in DIGITS.finditer(text):
        try:
            value = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        out.add(value * MULTIPLIER.get((m.group(2) or "").lower(), 1))
    for m in WORDS.finditer(text):
        if (v := _word_value(m.group(0))) is not None:
            out.add(v)
    return out


def unit_in(text: str) -> str | None:
    for unit, pattern in UNITS.items():
        if pattern.search(text):
            return unit
    return None


def normalise_lot(raw: str | None, lots: list[str]) -> tuple[str | None, bool]:
    """Map 'Lot 1', 'lot-1' or ' 1 ' to the notice's lot number. Returns (lot, recognised); null means every lot."""
    if raw is None or not str(raw).strip():
        return None, True
    text = str(raw).strip()
    if text in lots:
        return text, True
    bare = re.sub(r"^\s*lot\b[\s:#-]*", "", text, flags=re.I).strip().lower()
    for lot in lots:
        if lot.lower() == bare:
            return lot, True
    if not lots and bare == "1":
        return "1", True
    return text, False


@dataclass
class Checked:
    condition: dict | None
    note: str | None = None


def _stated(value: float | int, quote: str) -> bool:
    return float(value) in numbers_in(quote)


def check_condition(raw: dict | None, quote: str) -> Checked:
    """Keep a proposed rule condition only if everything in it can be traced to the quote.

    A dropped condition does not lose the requirement: it goes to interpretation, with the reason recorded."""
    if raw is None:
        return Checked(None)
    try:
        c = Condition(**raw)
    except (ValidationError, TypeError) as e:
        return Checked(None, f"invalid condition: {str(e).splitlines()[0][:120]}")
    if c.fact not in KIND_FACTS[c.kind]:
        return Checked(None, f"{c.kind} cannot be checked against {c.fact}")
    for name in ("bound", "min_units"):
        v = getattr(c, name)
        if isinstance(v, str) and v not in ALLOWED_FIELDS:
            return Checked(None, f"{name} '{v[:40]}' is neither a number nor a known notice field")
        if isinstance(v, (int, float)) and not _stated(v, quote):
            return Checked(None, f"{name} {v:g} does not appear in the quote")
    if c.years is not None and not _stated(c.years, quote):
        return Checked(None, f"years {c.years} does not appear in the quote")
    if c.anchor is not None and c.anchor not in ALLOWED_FIELDS:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", c.anchor) or not same_digits(c.anchor, quote):
            return Checked(None, f"date '{c.anchor[:40]}' does not appear in the quote")
    if c.kind == "within_years" and c.years is None:
        return Checked(None, "time window missing")
    if c.kind == "has_evidence":
        if not c.match or not c.match.strip():
            return Checked(None, "the condition does not name the evidence required")
        if normalise(c.match) not in normalise(quote):
            return Checked(None, f"'{c.match[:60]}' does not appear in the quote")
    if c.kind == "valid_through" and (not c.anchor or (c.match and normalise(c.match) not in normalise(quote))):
        return Checked(None, "validity condition has no end date or names evidence not in the quote")
    if c.kind in ("at_least", "at_most", "min_amount") and c.bound is None:
        return Checked(None, "bound missing")
    if c.kind == "at_most":
        stated = unit_in(quote)
        if stated is None:
            return Checked(None, "the quote gives no time unit")
        if c.unit and unit_in(c.unit) != stated:
            return Checked(None, f"unit '{c.unit}' differs from the quote ('{stated}')")
        c.unit = stated
    if c.kind == "min_amount":
        if re.search(r"[€$]|\b(?:eur|euro|usd|dollars?)\b", quote, re.I):
            return Checked(None, "amount is not in pounds")
        in_quote = insurance_type_in(quote)
        named = insurance_type_in(c.qualifier)
        if in_quote and named and in_quote != named:
            return Checked(None, f"insurance type '{c.qualifier}' differs from the quote ('{in_quote}')")
        if c.qualifier and not named and in_quote:
            return Checked(None, f"insurance type '{c.qualifier}' is not recognised")
        c.qualifier = named or in_quote
    return Checked(c.model_dump(exclude_none=False))
