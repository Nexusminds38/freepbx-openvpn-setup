"""Supplied-identifier consistency for the offline research lane.

A record used to verify when the first name, city, and exact age matched.
That treated one hit as sufficient, ignored state, and copied relatives onto
the result. This module fails closed instead.

Outcomes:
- zero plausible matches, a missing necessary identifier, or a conflicting
  supplied identifier (including age and state when they are supplied) are
  not verified
- duplicate equal candidates and any other multiple plausible matches require
  human review and are not exported as verified
- one consistent candidate is candidate consistency only. It is not identity
  proof and it carries no relative or private-contact expansion
"""

from __future__ import annotations

from dataclasses import dataclass

STATUS_CANDIDATE_CONSISTENT = "candidate_consistent"
STATUS_NEEDS_HUMAN_REVIEW = "needs_human_review"
STATUS_NO_CANDIDATE = "no_candidate"
STATUS_MISSING_IDENTIFIER = "missing_identifier"
STATUS_CONFLICTING_IDENTIFIER = "conflicting_identifier"

_IDENTITY_FIELDS = ("first", "last", "city", "state", "age")


@dataclass(frozen=True)
class ConsistencyDecision:
    status: str
    reason: str
    review_required: bool
    plausible_count: int
    disposition: str
    candidate: dict | None = None
    verified_export: bool = False
    identity_proof: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "verified_export", False)
        object.__setattr__(self, "identity_proof", False)

    def as_dict(self) -> dict:
        body = {
            "status": self.status,
            "reason": self.reason,
            "review_required": self.review_required,
            "verified_export": False,
            "identity_proof": False,
            "consistency_meaning": (
                "candidate_consistency_not_identity_proof"
                if self.status == STATUS_CANDIDATE_CONSISTENT
                else "not_verified"
            ),
            "plausible_count": self.plausible_count,
            "disposition": self.disposition,
            "relative_search": "disabled",
            "relative_output_present": False,
        }
        if self.status == STATUS_CANDIDATE_CONSISTENT and self.candidate is not None:
            body["candidate"] = {
                field: self.candidate[field] for field in _IDENTITY_FIELDS
            }
        return body


def _norm(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _digits(value: object) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _identity(record: object) -> dict:
    return {
        "first": getattr(record, "first", None),
        "last": getattr(record, "last", None),
        "city": getattr(record, "city", None),
        "state": getattr(record, "state", None),
        "age": getattr(record, "age", None),
    }


def _decision(
    status: str,
    reason: str,
    plausible_count: int,
    candidate: dict | None = None,
) -> ConsistencyDecision:
    if status == STATUS_NEEDS_HUMAN_REVIEW:
        disposition = "needs_human_review"
        review_required = True
    elif status == STATUS_CANDIDATE_CONSISTENT:
        disposition = "candidate_consistent"
        review_required = False
    else:
        disposition = "not_verified"
        review_required = False
    return ConsistencyDecision(
        status=status,
        reason=reason,
        review_required=review_required,
        plausible_count=plausible_count,
        disposition=disposition,
        candidate=candidate,
    )


def _missing_supplied(first: object, last: object, city: object, state: object, age: object) -> list[str]:
    missing: list[str] = []
    if not _norm(first):
        missing.append("first")
    if not _norm(last):
        missing.append("last")
    if not _norm(city):
        missing.append("city")
    if state is None or not _norm(state):
        missing.append("state")
    if isinstance(age, bool) or not isinstance(age, int):
        missing.append("age")
    return missing


def check_consistency(
    records: list,
    *,
    first: object,
    last: object,
    city: object,
    state: object,
    age: object,
    phone: str | None = None,
    supplied_states: tuple[object, ...] | list[object] | None = None,
    supplied_ages: tuple[object, ...] | list[object] | None = None,
) -> ConsistencyDecision:
    """Compare supplied identifiers with candidate rows. Never selects contacts.

    ``phone`` is a supplied contact to check. Subject numbers already on the
    candidate may confirm or contradict it. Relative numbers are not read,
    and no number is added to the decision.
    """
    if supplied_states:
        states = {_norm(state)}
        states.update(_norm(item) for item in supplied_states)
        states.discard("")
        if len(states) > 1:
            return _decision(
                STATUS_CONFLICTING_IDENTIFIER,
                "conflicting supplied identifier: state",
                0,
            )
    if supplied_ages:
        distinct = {age, *supplied_ages}
        if len(distinct) > 1:
            return _decision(
                STATUS_CONFLICTING_IDENTIFIER,
                "conflicting supplied identifier: age",
                0,
            )

    missing = _missing_supplied(first, last, city, state, age)
    if missing:
        return _decision(
            STATUS_MISSING_IDENTIFIER,
            "missing necessary identifier: " + ", ".join(missing),
            0,
        )

    plausible: list[dict] = []
    missing_fields: list[str] = []
    conflict_fields: list[str] = []
    wanted_phone = _digits(phone) if phone else ""

    for record in records or []:
        if _norm(getattr(record, "first", "")) != _norm(first):
            continue
        if _norm(getattr(record, "last", "")) != _norm(last):
            continue
        if _norm(getattr(record, "city", "")) != _norm(city):
            conflict_fields.append("city")
            continue
        record_age = getattr(record, "age", None)
        record_state = getattr(record, "state", None)
        if isinstance(record_age, bool) or not isinstance(record_age, int):
            missing_fields.append("age")
            continue
        if record_state is None or not _norm(record_state):
            missing_fields.append("state")
            continue
        if record_age != age:
            conflict_fields.append("age")
            continue
        if _norm(record_state) != _norm(state):
            conflict_fields.append("state")
            continue
        if wanted_phone:
            numbers = [
                _digits(item)
                for item in (getattr(record, "subject_phones", None) or [])
                if _digits(item)
            ]
            if not numbers:
                missing_fields.append("contact")
                continue
            if wanted_phone not in numbers:
                conflict_fields.append("contact")
                continue
        plausible.append(_identity(record))

    if len(plausible) > 1:
        keys = {
            (
                _norm(item["first"]),
                _norm(item["last"]),
                _norm(item["city"]),
                _norm(item["state"]),
                item["age"],
            )
            for item in plausible
        }
        if len(keys) == 1:
            reason = (
                "duplicate equal candidates require human review; "
                "not exported as verified"
            )
        else:
            reason = (
                "multiple plausible matches require human review; "
                "not exported as verified"
            )
        return _decision(STATUS_NEEDS_HUMAN_REVIEW, reason, len(plausible))

    if len(plausible) == 1:
        if missing_fields:
            return _decision(
                STATUS_MISSING_IDENTIFIER,
                "missing necessary identifier: " + ", ".join(sorted(set(missing_fields))),
                0,
            )
        return _decision(
            STATUS_CANDIDATE_CONSISTENT,
            (
                "unique candidate is consistent with supplied identifiers; "
                "this is candidate consistency, not identity proof"
            ),
            1,
            candidate=plausible[0],
        )

    if missing_fields:
        return _decision(
            STATUS_MISSING_IDENTIFIER,
            "missing necessary identifier: " + ", ".join(sorted(set(missing_fields))),
            0,
        )
    if conflict_fields:
        field = "state" if "state" in conflict_fields else conflict_fields[0]
        return _decision(
            STATUS_CONFLICTING_IDENTIFIER,
            f"conflicting supplied identifier: {field}",
            0,
        )
    return _decision(STATUS_NO_CANDIDATE, "zero plausible matches", 0)
