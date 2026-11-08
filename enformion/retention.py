"""Retention and expiry for the offline receiver.

This module does not choose a retention duration. A caller must pass an
explicit policy whose only accepted mode is ``explicit_expires_at`` plus a
timezone-aware ``expires_at`` on the record itself. Missing policy blocks
real persistence and live linkage. It does not delete existing records and
it does not keep new identifying records forever.

Dry-run inventory lists persisted artifact classes and does not delete bytes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

EXPLICIT_POLICY_MODE = "explicit_expires_at"

DURATION_KEYS = (
    "hours",
    "duration",
    "ttl",
    "ttl_hours",
    "retention_hours",
    "seconds",
    "days",
)

# Artifact classes that can hold identifying research data. Inventory always
# reports this catalog, including classes with no files in the scanned root.
PERSISTED_ARTIFACT_CATALOG = (
    {
        "kind": "query_log",
        "where": "query-log.jsonl and other *.query.jsonl files",
    },
    {
        "kind": "audit_log",
        "where": "audit-events.jsonl and audit lines inside research-log.jsonl",
    },
    {
        "kind": "research_log",
        "where": "research-log.jsonl",
    },
    {
        "kind": "saved_result_json",
        "where": "results/*.json saved research records",
    },
    {
        "kind": "relative_output",
        "where": "relative phone or relative-output JSON",
    },
    {
        "kind": "handoff_packet",
        "where": "packets/ and handoff JSON",
    },
    {
        "kind": "backup",
        "where": "backups/ and .bak or .backup copies",
    },
)

_CONTACT_KEYS = {
    "relatives",
    "phones",
    "phone",
    "phoneNumbers",
    "phone_numbers",
    "associates",
    "subject_phones",
    "sha256",
    "result_sha256",
    "identifying_hash",
    "password",
    "ap_password",
    "galaxy-ap-password",
    "galaxy-ap-name",
}

DERIVATIVE_NAMES = ("log", "queue", "output", "packet")


class RetentionRejected(ValueError):
    """A deadline or policy failed closed."""


@dataclass(frozen=True)
class Authorization:
    allowed: bool
    reason: str
    expires_at: datetime | None
    live_linkage: str = "blocked"

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "live_linkage": self.live_linkage,
        }


def _has_duration(policy: dict) -> bool:
    return any(key in policy for key in DURATION_KEYS)


def policy_is_explicit(policy: object) -> bool:
    """True only for an explicit input policy that does not select a duration."""
    return (
        isinstance(policy, dict)
        and policy.get("mode") == EXPLICIT_POLICY_MODE
        and not _has_duration(policy)
    )


def parse_expires_at(value: object, *, now: datetime) -> datetime:
    """Require a timezone-aware deadline still in the future.

    Naive values are rejected. This function does not assume UTC or any
    other zone when the input omits one.
    """
    if value is None or value == "":
        raise RetentionRejected("missing expires_at")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise RetentionRejected("missing expires_at")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
        except ValueError as exc:
            raise RetentionRejected("invalid expires_at") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise RetentionRejected("invalid expires_at")

    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise RetentionRejected("naive expires_at is rejected; timezone was not inferred")
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise RetentionRejected("comparison clock must be timezone-aware")
    if parsed <= now:
        raise RetentionRejected("expired expires_at")
    return parsed


def propose_deadline(current: object, proposed: object, *, now: datetime) -> datetime:
    """Accept a shorter deadline. Reject an extension or a reset that lengthens."""
    current_dt = parse_expires_at(current, now=now)
    proposed_dt = parse_expires_at(proposed, now=now)
    if proposed_dt > current_dt:
        raise RetentionRejected(
            "deadline extension or reset would lengthen retention"
        )
    if proposed_dt < current_dt:
        return proposed_dt
    return current_dt


class DeadlineLedger:
    """Holds the original deadline and the earliest deadline seen so far."""

    def __init__(self, original: object, *, now: datetime) -> None:
        self.original = parse_expires_at(original, now=now)
        self.earliest = self.original

    def propose(self, proposed: object, *, now: datetime) -> datetime:
        candidate = parse_expires_at(proposed, now=now)
        if candidate > self.earliest:
            raise RetentionRejected(
                "deadline extension or reset would lengthen retention"
            )
        if candidate < self.earliest:
            self.earliest = candidate
        return self.earliest

    def stamp_derivatives(self) -> dict:
        return stamp_derivatives(self.earliest)


def bind_derivatives(original: object, derivatives: dict, *, now: datetime) -> dict:
    """Stamp log, queue, output, and packet with the earliest deadline.

    A derivative later than the original deadline is an extension and is
    rejected before any stamp is returned.
    """
    original_dt = parse_expires_at(original, now=now)
    parsed: dict[str, datetime] = {}
    for name, value in derivatives.items():
        deadline = parse_expires_at(value, now=now)
        if deadline > original_dt:
            raise RetentionRejected(
                f"{name} deadline lengthens retention beyond the original expires_at"
            )
        parsed[name] = deadline
    earliest = original_dt
    for deadline in parsed.values():
        if deadline < earliest:
            earliest = deadline
    stamped = stamp_derivatives(earliest)
    for name in parsed:
        if name not in stamped:
            stamped[name] = _stamp(name, earliest)
    return {
        "earliest_expires_at": earliest.isoformat(),
        "derivatives": stamped,
    }


def _stamp(name: str, deadline: datetime) -> dict:
    return {"kind": name, "expires_at": deadline.isoformat()}


def stamp_derivatives(deadline: datetime) -> dict:
    if deadline.tzinfo is None or deadline.tzinfo.utcoffset(deadline) is None:
        raise RetentionRejected("naive expires_at is rejected; timezone was not inferred")
    return {name: _stamp(name, deadline) for name in DERIVATIVE_NAMES}


def authorize(policy: object, expires_at: object, *, now: datetime) -> Authorization:
    """Gate real persistence. Live linkage stays blocked in this scope."""
    if isinstance(policy, dict) and _has_duration(policy):
        return Authorization(
            allowed=False,
            reason=(
                "retention duration must come from an owner policy outside this lane; "
                "refusing to adopt a duration"
            ),
            expires_at=None,
        )
    if not policy_is_explicit(policy):
        return Authorization(
            allowed=False,
            reason="retention policy absent; real persistence and live linkage blocked",
            expires_at=None,
        )
    try:
        deadline = parse_expires_at(expires_at, now=now)
    except RetentionRejected as exc:
        return Authorization(allowed=False, reason=str(exc), expires_at=None)
    return Authorization(
        allowed=True,
        reason="explicit expires_at accepted; live linkage remains blocked",
        expires_at=deadline,
    )


def redact_identifying_contacts(value: object) -> object:
    """Drop contact-discovery fields and identifying hashes from a copy."""
    if isinstance(value, dict):
        return {
            key: redact_identifying_contacts(item)
            for key, item in value.items()
            if key not in _CONTACT_KEYS
        }
    if isinstance(value, list):
        return [redact_identifying_contacts(item) for item in value]
    return value


def classify_artifact(relative_path: str) -> list[str]:
    """Return catalog kinds that apply to one relative path."""
    rel = relative_path.replace("\\", "/")
    base = os.path.basename(rel)
    lower = rel.lower()
    kinds: list[str] = []

    def add(kind: str) -> None:
        if kind not in kinds:
            kinds.append(kind)

    if base == "query-log.jsonl" or base.endswith(".query.jsonl"):
        add("query_log")
    if base == "audit-events.jsonl" or "audit" in base:
        add("audit_log")
    if base == "research-log.jsonl" or base.startswith("research-log"):
        add("research_log")
        add("audit_log")
    parent = os.path.basename(os.path.dirname(rel))
    if parent == "results" and base.endswith(".json") and "relative" not in base:
        add("saved_result_json")
    if "relative" in base and base.endswith(".json"):
        add("relative_output")
    if (
        base.startswith("handoff")
        or "/packets/" in f"/{lower}/"
        or parent == "packets"
    ):
        add("handoff_packet")
    if (
        base.endswith(".bak")
        or base.endswith(".backup")
        or parent in {"backup", "backups"}
        or "/backups/" in f"/{lower}/"
        or "/backup/" in f"/{lower}/"
    ):
        add("backup")
    return kinds


def _expires_raw(path: str) -> str | None:
    try:
        if path.endswith(".json"):
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("expires_at"), str):
                return data["expires_at"]
            return None
        if path.endswith(".jsonl"):
            seen: list[str] = []
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict) and isinstance(data.get("expires_at"), str):
                        seen.append(data["expires_at"])
            return min(seen) if seen else None
    except OSError:
        return None
    return None


def inventory_artifacts(root: str) -> dict:
    """Inventory files under ``root`` without deleting or copying contacts."""
    found: list[dict] = []
    root = os.path.abspath(root)
    if os.path.isdir(root):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                if os.path.islink(path):
                    relative = os.path.relpath(path, root)
                    found.append(
                        {
                            "relative_path": relative,
                            "kinds": classify_artifact(relative) or ["unclassified"],
                            "bytes": None,
                            "expires_at_raw": None,
                            "would_delete": False,
                            "symlink": True,
                        }
                    )
                    continue
                relative = os.path.relpath(path, root)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = None
                found.append(
                    {
                        "relative_path": relative,
                        "kinds": classify_artifact(relative) or ["unclassified"],
                        "bytes": size,
                        "expires_at_raw": _expires_raw(path),
                        "would_delete": False,
                        "symlink": False,
                    }
                )
    kinds_found = sorted({kind for item in found for kind in item["kinds"]})
    catalog_kinds = [item["kind"] for item in PERSISTED_ARTIFACT_CATALOG]
    return {
        "root": root,
        "catalog": [dict(item) for item in PERSISTED_ARTIFACT_CATALOG],
        "artifacts": found,
        "kinds_found": kinds_found,
        "kinds_absent": [kind for kind in catalog_kinds if kind not in kinds_found],
        "real_deletion_performed": False,
    }


def dry_run_deletion(root: str, *, now: datetime, policy: object = None) -> dict:
    """Return an inventory. This function does not delete or modify files."""
    if now.tzinfo is None:
        raise RetentionRejected("comparison clock must be timezone-aware")
    report = inventory_artifacts(root)
    report.update(
        {
            "mode": "inventory_dry_run",
            "deleted": [],
            "would_delete": False,
            "real_deletion_performed": False,
            "policy_present": policy_is_explicit(policy),
            "evaluated_at": now.astimezone(timezone.utc).isoformat(),
            "operational_deletion_proof": {
                "provided_by_this_run": False,
                "summary": (
                    "This dry run records the artifact inventory and leaves every "
                    "file in place. Mock tests do not prove operational deletion."
                ),
                "remaining_requirements": list(OPERATIONAL_DELETION_REQUIREMENTS),
            },
        }
    )
    return report


OPERATIONAL_DELETION_REQUIREMENTS = (
    "An owner-verified retention duration recorded outside this lane. This code does not select one.",
    "A configured records root that is the actual store, including backups and copies outside the repo.",
    "A before-inventory of every artifact class by path and size: query logs, audit logs, research logs, saved result JSON, relative outputs, handoff packets, and backups.",
    "An authorized deletion run that removes only artifacts past their earliest deadline and leaves in-window records untouched.",
    "An after-inventory showing those paths gone, including queues, packet outputs, rotated backups, and any hidden records directory.",
    "A check that no identifying-hash sidecar was written beside the deleted records.",
)
