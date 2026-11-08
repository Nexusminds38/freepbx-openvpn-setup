"""Offline synthetic consistency check.

The default workflow is disconnected and mock-only. It does not call the
network, does not read credentials, and does not expand relatives. A unique
consistent candidate is candidate consistency, not a verified identity and
not an authorized referral.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

import client
import handoff
import receiver
import retention
import verify


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise handoff.HandoffError(f"{field} is not a timestamp") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise handoff.HandoffError(
            f"{field} must be timezone-aware; a missing timezone is not inferred"
        )
    return parsed


def prepare_offline(
    *,
    first: str,
    last: str,
    city: str,
    state: str,
    age: int | None,
    county: str,
    source: str,
    booking_reference: str,
    observed_at: datetime,
    first_seen_at: datetime,
    source_timestamp_raw: str | None = None,
    source_timezone: str | None = None,
    candidates: list | None = None,
    policy: dict | None = None,
    expires_at: object = None,
    now: datetime | None = None,
    records_directory: str | None = None,
    referral_authorization: object = handoff.ABSENT,
    phone: str | None = None,
    supplied_states: tuple | list | None = None,
    supplied_ages: tuple | list | None = None,
) -> dict:
    """Run the disconnected workflow and return the handoff report."""
    if candidates is None:
        candidates = client.get_adapter(mock=True).search_person(first, last, city, state)
    decision = verify.check_consistency(
        candidates,
        first=first,
        last=last,
        city=city,
        state=state,
        age=age,
        phone=phone,
        supplied_states=supplied_states,
        supplied_ages=supplied_ages,
    )
    packet = handoff.build_packet(
        consistency=decision.as_dict(),
        county=county,
        source=source,
        booking_reference=booking_reference,
        observed_at=observed_at,
        first_seen_at=first_seen_at,
        source_timestamp_raw=source_timestamp_raw,
        source_timezone=source_timezone,
        supplied_identifiers={
            "first": first,
            "last": last,
            "city": city,
            "state": state,
            "age": age,
        },
        expires_at=expires_at,
        referral_authorization=referral_authorization,
    )
    receipt = receiver.receive(
        packet,
        policy=policy,
        now=now or datetime.now(observed_at.tzinfo),
        records_directory=records_directory,
    )
    if receipt.get("persisted") and receipt.get("expires_at"):
        packet = retention.rewrite_expires_at(packet, receipt["expires_at"])
    elif not receipt.get("persisted") and "lengthen" in str(receipt.get("reason") or ""):
        packet = retention.without_expires_at(packet)
    return {
        "workflow": "disconnected_mock",
        "consistency": decision.as_dict(),
        "handoff": packet,
        "receipt": receipt,
    }


def _policy_from_arg(value: str) -> dict | None:
    if not value:
        return None
    if value == "explicit_expires_at":
        return {"mode": "explicit_expires_at"}
    raise handoff.HandoffError(
        "retention policy must be explicit_expires_at; this lane does not accept a duration"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline synthetic consistency check. No live lookup."
    )
    parser.add_argument("--first", required=True)
    parser.add_argument("--last", required=True)
    parser.add_argument("--city", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--age", required=True, type=int)
    parser.add_argument("--county", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--booking-ref", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--first-seen-at", required=True)
    parser.add_argument("--source-timestamp-raw", default="")
    parser.add_argument("--source-timezone", default="")
    parser.add_argument("--expires-at", default="")
    parser.add_argument(
        "--retention-policy",
        default="",
        help="Set to explicit_expires_at to allow an offline copy. Durations are rejected.",
    )
    args = parser.parse_args(argv)

    try:
        policy = _policy_from_arg(args.retention_policy)
        directory = os.environ.get("ENFORMION_RECORDS_DIR") if policy else None
        report = prepare_offline(
            first=args.first,
            last=args.last,
            city=args.city,
            state=args.state,
            age=args.age,
            county=args.county,
            source=args.source,
            booking_reference=args.booking_ref,
            observed_at=_parse_time(args.observed_at, "observed-at"),
            first_seen_at=_parse_time(args.first_seen_at, "first-seen-at"),
            source_timestamp_raw=args.source_timestamp_raw or None,
            source_timezone=args.source_timezone or None,
            policy=policy,
            expires_at=args.expires_at or None,
            records_directory=directory,
        )
    except (handoff.HandoffError, receiver.ReceiverError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3

    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    if policy and not report["receipt"]["persisted"]:
        return 3
    status = report["consistency"]["status"]
    if status == verify.STATUS_CANDIDATE_CONSISTENT:
        return 0
    if status == verify.STATUS_NEEDS_HUMAN_REVIEW:
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
