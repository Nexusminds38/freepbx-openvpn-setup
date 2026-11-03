"""Verification doctrine — the user's standing rules, unchanged since 2026-10-07.

A record VERIFIES only when ALL of these match the booking:
  1. first + last name match
  2. record city == arrest city
  3. record age == booking age, EXACTLY

Unverified records yield NO relatives — never extract contacts off a guess.

Contact rules for verified records:
  - up to 4 relatives; relatives preferred over associates
  - per relative up to 3 phone numbers: Wireless first, unlabeled next,
    Landline only as last resort; keep label + carrier + first-reported date
"""

from __future__ import annotations

from client import PersonRecord, Phone, Relative

MAX_RELATIVES = 4
MAX_PHONES = 3


def _label_rank(phone: Phone) -> int:
    label = (phone.label or "").strip().lower()
    if label == "wireless":
        return 0
    if label == "":
        return 1
    if label == "landline":
        return 2
    return 1


def pick_phones(phones: list[Phone], limit: int = MAX_PHONES) -> list[Phone]:
    return sorted(phones, key=_label_rank)[:limit]


def pick_relatives(relatives: list[Relative], limit: int = MAX_RELATIVES) -> list[Relative]:
    ranked = sorted(relatives, key=lambda r: 0 if (r.relation or "").lower() == "relative" else 1)
    picked = []
    for rel in ranked[:limit]:
        picked.append(Relative(name=rel.name, relation=rel.relation,
                               phones=pick_phones(rel.phones)))
    return picked


def verify(records: list[PersonRecord], *, first: str, last: str,
           city: str, age: int) -> tuple[PersonRecord | None, str]:
    """Return (verified_record_with_picked_contacts, reason)."""
    for rec in records:
        if rec.first.lower() != first.lower() or rec.last.lower() != last.lower():
            continue
        if rec.city.lower() != city.lower():
            continue
        if rec.age is None:
            return None, "age not reported on record — cannot verify"
        if rec.age != age:
            continue
        verified = PersonRecord(
            first=rec.first, last=rec.last, age=rec.age,
            city=rec.city, state=rec.state,
            relatives=pick_relatives(rec.relatives),
        )
        return verified, "name + city + exact age all match"
    return None, "no record matched name + city + exact age"
