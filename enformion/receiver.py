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


def _expires_values(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "expires_at" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_expires_values(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_expires_values(item))
    return found


def _reconcile_deadlines(packet: dict, *, now: datetime) -> tuple[datetime, dict]:
    """Keep the earliest deadline and reject any deadline that lengthens it."""
    original = retention.parse_expires_at(packet.get("expires_at"), now=now)
    parsed = [retention.parse_expires_at(value, now=now) for value in _expires_values(packet)]
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
    stored = audit.persist_bundle(records_directory, packet, earliest)
    receipt["persisted"] = stored["persisted"]
    receipt["live_linkage"] = "blocked"
    receipt["reason"] = (
        "offline copy stored under the earliest deadline; live linkage remains blocked"
    )
    return receipt
