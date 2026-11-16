"""The closed vocabulary of hypothetical changes. A simulation can express only what these types allow.

Each change is validated twice: here for its shape (types, finite numbers, allowed values, lengths), and in
`scenario.apply_changes` against the snapshot it is applied to (does the fact or condition exist, does the edit make sense
for its kind). An LLM may later propose changes, but only as these objects, and through the same two checks."""
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, TypeAdapter, ValidationError, model_validator

EVIDENCE_STATUSES = ("document_on_file", "declared", "missing", "unknown", "needs_update")  # profile_fact CHECK constraint
NOTICE_FIELDS = ("notice.deadline", "lot.quantity")

# A non-negative finite number that is not a boolean: the same values rules.engine._number accepts.
Amount = Annotated[StrictInt | StrictFloat, Field(ge=0, allow_inf_nan=False)]
Id = Annotated[int, Field(strict=True, gt=0)]
Text = Annotated[str, Field(min_length=1, max_length=4000)]


class SimulationError(ValueError):
    """A simulation request that cannot be carried out, with a message a person can act on."""


class SimulationNotFound(SimulationError):
    """Unknown, or another supplier's: the caller is not told which."""


class SimulationConflict(SimulationError):
    """The request is valid, but not in the simulation's (or its baseline's) current state: it has already run, it is
    discarded, or the assessment cannot be a baseline. The API answers 409, where other SimulationErrors are 422."""


class _Change(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SetConditionBound(_Change):
    """A numeric threshold of one condition, such as an insurance minimum or a maximum lead time."""
    kind: Literal["set_condition_bound"] = "set_condition_bound"
    requirement_id: Id
    condition: Annotated[int, Field(strict=True, ge=0)]  # requirement_condition.ordinal
    bound: Amount


class SetConditionMatch(_Change):
    """The certificate, standard or document a condition asks for."""
    kind: Literal["set_condition_match"] = "set_condition_match"
    requirement_id: Id
    condition: Annotated[int, Field(strict=True, ge=0)]
    match: Annotated[str, Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def _not_blank(self):
        if not self.match.strip():
            raise ValueError("match must name the evidence required")
        return self


class SetFactValidity(_Change):
    """When a fact stops being valid; None means it has no expiry date on record."""
    kind: Literal["set_fact_validity"] = "set_fact_validity"
    fact_id: int
    valid_until: date | None


class SetFactEvidence(_Change):
    kind: Literal["set_fact_evidence"] = "set_fact_evidence"
    fact_id: int
    evidence_status: Literal["document_on_file", "declared", "missing", "unknown", "needs_update"]


class RemoveFact(_Change):
    kind: Literal["remove_fact"] = "remove_fact"
    fact_id: int


class SetFactValue(_Change):
    """One field of a fact's value, re-checked with the same rules as the live profile (profile.service.clean_fact)."""
    kind: Literal["set_fact_value"] = "set_fact_value"
    fact_id: int
    key: Annotated[str, Field(min_length=1, max_length=60)]
    value: StrictInt | StrictFloat | bool | Annotated[str, Field(max_length=500)] | list[Annotated[str, Field(max_length=200)]]


class AddFact(_Change):
    """A fact the supplier does not hold today. It gets a negative id, so it can never be mistaken for a real one."""
    kind: Literal["add_fact"] = "add_fact"
    attribute: Annotated[str, Field(min_length=1, max_length=40)]
    label: Annotated[str, Field(min_length=1, max_length=200)]
    value: dict
    evidence_status: Literal["document_on_file", "declared", "missing", "unknown"] = "declared"
    valid_until: date | None = None


class SetNoticeField(_Change):
    kind: Literal["set_notice_field"] = "set_notice_field"
    field: Literal["notice.deadline", "lot.quantity"]
    value: date | Amount

    @model_validator(mode="after")
    def _type_matches_field(self):
        if self.field == "notice.deadline" and not isinstance(self.value, date):
            raise ValueError("notice.deadline takes a date")
        if self.field == "lot.quantity" and isinstance(self.value, date):
            raise ValueError("lot.quantity takes a number")
        return self


class SetRequirementText(_Change):
    """New wording. Rules cannot read wording, so evaluating this needs the model; the simulation says so rather than guess."""
    kind: Literal["set_requirement_text"] = "set_requirement_text"
    requirement_id: Id
    statement: Text | None = None
    quote: Text | None = None

    @model_validator(mode="after")
    def _something_changes(self):
        if self.statement is None and self.quote is None:
            raise ValueError("give a new statement, a new quote, or both")
        return self


HypotheticalChange = Annotated[
    SetConditionBound | SetConditionMatch | SetFactValidity | SetFactEvidence | RemoveFact | SetFactValue | AddFact
    | SetNoticeField | SetRequirementText,
    Field(discriminator="kind"),
]
_ADAPTER: TypeAdapter[HypotheticalChange] = TypeAdapter(HypotheticalChange)
KINDS = tuple(t.model_fields["kind"].default for t in (SetConditionBound, SetConditionMatch, SetFactValidity, SetFactEvidence,
                                                       RemoveFact, SetFactValue, AddFact, SetNoticeField, SetRequirementText))


def parse_change(data: object) -> HypotheticalChange:
    """A change from untrusted input (an API body, a stored payload, later an agent's proposal), or SimulationError."""
    if isinstance(data, _Change):
        return data  # type: ignore[return-value]
    try:
        return _ADAPTER.validate_python(data)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'change'}: {err['msg']}" for err in e.errors()[:5])
        raise SimulationError(f"invalid change: {problems}") from None


def dump_change(change: HypotheticalChange) -> dict:
    """The stored form: JSON-safe, and parse_change(dump_change(c)) == c."""
    return change.model_dump(mode="json")
