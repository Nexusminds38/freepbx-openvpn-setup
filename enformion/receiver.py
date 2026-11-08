"""Offline receiver for synthetic handoff packets.

The receiver accepts an in-memory synthetic packet on the disconnected lane.
Real persistence requires an explicit retention policy, a valid original
deadline, and an explicit directory. Live linkage stays blocked either way.
A missing policy does not delete anything.
"""

from __future__ import annotations

from datetime import datetime

import audit
import handoff
import retention


class ReceiverError(ValueError):
    """The offline receiver refused a packet."""


def _require_offline(packet: dict) -> None:
    if packet.get("schema_version") != handoff.SCHEMA_VERSION:
        raise ReceiverError("unexpected handoff schema_version")
    if packet.get("synthetic") is not True:
        raise ReceiverError("synthetic marker is required; non-synthetic input is refused")
    if packet.get("synthetic_marker") != handoff.SYNTHETIC_MARKER:
        raise ReceiverError("synthetic marker is required")
    if packet.get("lane") != handoff.LANE or packet.get("live_lane_permitted") is not False:
        raise handoff.SyntheticLiveForbidden(
            "synthetic input must never enter a live lane"
        )
    if packet.get("production_destination") is not None:
        raise ReceiverError("production destination must stay unset")
    if packet.get("classification") != handoff.CLASSIFICATION:
        raise ReceiverError("classification must stay public_observation_not_authorized_referral")
    if packet.get("eligibility") != "not_performed":
        raise ReceiverError("eligibility remains not_performed")
    episode = packet.get("episode") or {}
    if not episode.get("episode_id"):
        raise ReceiverError("episode id is required")


def _certainty_claim(packet: dict) -> str | None:
    consistency = packet.get("consistency")
    if not isinstance(consistency, dict):
        return None
    if consistency.get("identity_proof") not in (False, None):
        return "packet claims identity proof; refusing to store it as certainty"
    if consistency.get("verified_export") not in (False, None):
        return "packet claims a verified export; refusing to store it"
    status = str(consistency.get("status", "")).replace(" ", "").casefold()
    if status in {"verified", "identity_proven", "identityproof"}:
        return "packet status claims identity certainty; refusing to store it"
    return None


def _reconcile_deadlines(packet: dict, *, now: datetime) -> tuple[datetime, dict]:
    """Keep the earliest deadline and reject any deadline that lengthens it."""
    original = retention.parse_expires_at(packet.get("expires_at"), now=now)
    parsed = [retention.parse_expires_at(value, now=now) for value in retention.iter_expires_at(packet)]
    for deadline in parsed:
        if deadline > original:
            raise retention.RetentionRejected(
                "conflicting deadline would lengthen retention beyond the original expires_at"
            )
    earliest = original
    for deadline in parsed:
        if deadline < earliest:
            earliest = deadline
    return earliest, retention.stamp_derivatives(earliest)


def receive(
    packet: dict,
    *,
    policy: object = None,
    now: datetime,
    records_directory: str | None = None,
) -> dict:
    """Accept one offline packet. Persistence and live linkage follow policy."""
    _require_offline(packet)
    claim = _certainty_claim(packet)
    if claim:
        return {
            "accepted_offline": False,
            "persisted": False,
            "live_linkage": "blocked",
            "production_destination": None,
            "reason": claim,
            "expires_at": None,
            "synthetic": True,
        }
    auth = retention.authorize(policy, packet.get("expires_at"), now=now)
    receipt = {
        "accepted_offline": True,
        "persisted": False,
        "live_linkage": "blocked",
        "production_destination": None,
        "reason": auth.reason,
        "expires_at": auth.expires_at.isoformat() if auth.expires_at else None,
        "synthetic": True,
    }
    if not auth.allowed or auth.expires_at is None:
        return receipt
    try:
        earliest, stamped = _reconcile_deadlines(packet, now=now)
    except retention.RetentionRejected as exc:
        receipt["accepted_offline"] = False
        receipt["expires_at"] = None
        receipt["reason"] = str(exc)
        return receipt
    receipt["expires_at"] = earliest.isoformat()
    receipt["derivatives"] = stamped
    if not records_directory:
        receipt["reason"] = (
            "records directory is not configured; persistence blocked; live linkage blocked"
        )
        return receipt
    stored = audit.persist_bundle(records_directory, packet, earliest, now=now)
    if not stored.get("persisted"):
        receipt["accepted_offline"] = False
        receipt["persisted"] = False
        receipt["expires_at"] = None
        receipt.pop("derivatives", None)
        receipt["reason"] = stored.get("reason")
        return receipt
    stored_deadline = retention.parse_expires_at(stored.get("expires_at"), now=now)
    receipt["persisted"] = True
    receipt["live_linkage"] = "blocked"
    receipt["expires_at"] = stored_deadline.isoformat()
    receipt["derivatives"] = retention.stamp_derivatives(stored_deadline)
    receipt["reason"] = (
        "offline copy stored under the earliest deadline; live linkage remains blocked"
    )
    return receipt
