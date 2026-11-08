"""Synthetic offline handoff contract.

A booking episode is county + source + booking reference. A name is not an
episode key. Source timestamps stay raw, and a missing source timezone is
left missing. Public booking observations keep the classification
``public_observation_not_authorized_referral``. Eligibility is not performed
here. The production destination stays unset.
"""

from __future__ import annotations

from datetime import datetime

from retention import redact_identifying_contacts

SCHEMA_VERSION = "enformion.offline_handoff.v1"
CLASSIFICATION = "public_observation_not_authorized_referral"
LANE = "disconnected_mock"
SYNTHETIC_MARKER = "synthetic_fixture_not_a_person"
EXAMPLE_SOURCE = "https://example.org/fictional-roster"


class _AbsentType:
    def __repr__(self) -> str:
        return "ABSENT"


ABSENT = _AbsentType()


class HandoffError(ValueError):
    """The handoff contract refused the input."""


class SyntheticLiveForbidden(HandoffError):
    """Synthetic input cannot be placed on a live lane."""


def _aware_iso(value: object, field: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise HandoffError(
            f"{field} must be timezone-aware; a missing timezone is not inferred"
        )
    return value.isoformat()


def _component(label: str, value: object) -> str:
    if value is None:
        raise HandoffError(f"episode requires {label}")
    text = " ".join(str(value).strip().casefold().split())
    if not text:
        raise HandoffError(f"episode requires {label}")
    if any(char in text for char in "|\n\r"):
        raise HandoffError(f"{label} contains a reserved separator")
    return text


def episode_id(county: object, source: object, booking_reference: object) -> str:
    """Stable episode key. The person's name is not part of the key."""
    county_text = _component("county", county)
    source_text = _component("source", source)
    booking_text = _component("booking", booking_reference)
    return f"county={county_text}|source={source_text}|booking={booking_text}"


def build_provenance(
    *,
    source: str,
    observed_at: datetime,
    first_seen_at: datetime,
    source_timestamp_raw: str | None = None,
    source_timezone: str | None = None,
) -> dict:
    """Keep raw source time separate from observed and first-seen times.

    ``source_timestamp_raw`` is stored unchanged. This function does not parse
    it and does not copy an offset out of it into ``source_timezone``.
    """
    if source_timezone is None or (isinstance(source_timezone, str) and not source_timezone.strip()):
        zone = None
    elif isinstance(source_timezone, str):
        zone = source_timezone.strip()
    else:
        raise HandoffError("source_timezone must be an explicit string when supplied")
    return {
        "source": source,
        "source_timestamp_raw": source_timestamp_raw,
        "source_timezone": zone,
        "source_timezone_inferred": False,
        "observed_at": _aware_iso(observed_at, "observed_at"),
        "first_seen_at": _aware_iso(first_seen_at, "first_seen_at"),
    }


def build_packet(
    *,
    consistency: dict,
    county: str,
    source: str,
    booking_reference: str,
    observed_at: datetime,
    first_seen_at: datetime,
    supplied_identifiers: dict,
    source_timestamp_raw: str | None = None,
    source_timezone: str | None = None,
    expires_at: object = None,
    referral_authorization: object = ABSENT,
    synthetic: bool = True,
    production_destination: object = None,
) -> dict:
    """Build one disconnected synthetic packet. Live routing is not a mode."""
    if synthetic is not True:
        raise HandoffError(
            "non-synthetic input is refused on the disconnected receiver"
        )
    if production_destination is not None:
        raise HandoffError(
            "production destination cannot be set; the lane is disconnected"
        )
    identifiers = {
        "first": supplied_identifiers.get("first"),
        "last": supplied_identifiers.get("last"),
        "city": supplied_identifiers.get("city"),
        "state": supplied_identifiers.get("state"),
        "age": supplied_identifiers.get("age"),
    }
    packet = {
        "schema_version": SCHEMA_VERSION,
        "synthetic": True,
        "synthetic_marker": SYNTHETIC_MARKER,
        "lane": LANE,
        "live_lane_permitted": False,
        "production_destination": None,
        "classification": CLASSIFICATION,
        "eligibility": "not_performed",
        "episode": {
            "county": county,
            "source": source,
            "booking_reference": booking_reference,
            "episode_id": episode_id(county, source, booking_reference),
        },
        "provenance": build_provenance(
            source=source,
            observed_at=observed_at,
            first_seen_at=first_seen_at,
            source_timestamp_raw=source_timestamp_raw,
            source_timezone=source_timezone,
        ),
        "supplied_identifiers": identifiers,
        "consistency": redact_identifying_contacts(consistency),
    }
    if expires_at is not None:
        if isinstance(expires_at, datetime):
            packet["expires_at"] = _aware_iso(expires_at, "expires_at")
        else:
            packet["expires_at"] = expires_at
    if referral_authorization is not ABSENT:
        packet["referral_authorization"] = referral_authorization
    return packet


def refuse_live_lane(packet: dict) -> None:
    """Reject every attempt to place a packet on a live lane.

    There is no flag that overrides this. Synthetic packets are refused with
    an explicit synthetic-lane error.
    """
    if packet.get("synthetic") is True:
        raise SyntheticLiveForbidden(
            "synthetic input must never enter a live lane"
        )
    raise SyntheticLiveForbidden("live lane is disconnected")
