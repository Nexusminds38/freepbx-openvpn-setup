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
    {
        "kind": "queue",
        "where": "queue.jsonl and files under queue/",
    },
    {
        "kind": "identifying_hash_sidecar",
        "where": "sha256 or .hash sidecars beside research records",
    },
)

_CONTACT_KEYS = {
    "relatives",
    "relative",
    "relativesSummary",
    "relatives_summary",
    "phones",
    "phone",
    "phoneNumber",
    "phoneNumbers",
    "phone_numbers",
    "numbers",
    "contacts",
    "emergency_contacts",
    "associates",
    "associatesSummary",
    "associates_summary",
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


def try_aware_deadline(value: object) -> datetime | None:
    """Parse a deadline for comparison. Naive and invalid values return None.

    This does not treat a missing timezone as UTC.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed


def iter_expires_at(node: object) -> list[str]:
    """Collect raw ``expires_at`` strings from a JSON-like tree."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "expires_at" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(iter_expires_at(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(iter_expires_at(item))
    return found


def without_expires_at(node: object) -> object:
    """Drop every deadline field so a rejected proposal cannot be re-stored."""
    if isinstance(node, dict):
        return {
            key: without_expires_at(value)
            for key, value in node.items()
            if key != "expires_at"
        }
    if isinstance(node, list):
        return [without_expires_at(item) for item in node]
    return node


def rewrite_expires_at(node: object, deadline: str) -> object:
    """Return a copy whose every ``expires_at`` string is ``deadline``."""
    if isinstance(node, dict):
        rewritten = {}
        for key, value in node.items():
            if key == "expires_at":
                rewritten[key] = deadline
            else:
                rewritten[key] = rewrite_expires_at(value, deadline)
        return rewritten
    if isinstance(node, list):
        return [rewrite_expires_at(item, deadline) for item in node]
    return node


def mentions_episode(node: object, episode_id: str) -> bool:
    if not episode_id or not isinstance(node, dict):
        return False
    if node.get("episode_id") == episode_id:
        return True
    episode = node.get("episode")
    return isinstance(episode, dict) and episode.get("episode_id") == episode_id


def resolve_episode_deadline(
    proposed: object,
    stored: list[object],
    *,
    now: datetime,
    packet_deadlines: list[object] | None = None,
) -> datetime:
    """Return the earliest deadline, or reject a lengthening re-ingest.

    A proposal later than a deadline already stored for the episode is an
    extension and raises. A shorter proposal becomes the earliest. Naive or
    invalid stored values raise so a timezone is not invented and the bytes
    are left untouched. This function does not choose a retention duration.
    """
    proposed_dt = parse_expires_at(proposed, now=now)
    stored_dts: list[datetime] = []
    for value in stored:
        parsed = try_aware_deadline(value)
        if parsed is None:
            raise RetentionRejected(
                "stored deadline is missing a timezone or is invalid; "
                "refusing to infer one, extend retention, or rewrite the record"
            )
        if parsed <= now:
            raise RetentionRejected(
                "stored deadline is expired; refusing to extend, reset, or rewrite it"
            )
        stored_dts.append(parsed)
    packet_dts: list[datetime] = []
    for value in packet_deadlines or []:
        parsed = parse_expires_at(value, now=now)
        packet_dts.append(parsed)
    if stored_dts:
        stored_earliest = min(stored_dts)
        if proposed_dt > stored_earliest:
            raise RetentionRejected(
                "deadline extension or reset would lengthen retention "
                "beyond the stored earliest deadline"
            )
        for item in packet_dts:
            if item > stored_earliest:
                raise RetentionRejected(
                    "packet deadline would lengthen retention beyond the stored earliest deadline"
                )
        earliest = stored_earliest
        if proposed_dt < earliest:
            earliest = proposed_dt
        for item in packet_dts:
            if item < earliest:
                earliest = item
        return earliest
    earliest = proposed_dt
    for item in packet_dts:
        if item < earliest:
            earliest = item
    return earliest


def _is_hash_sidecar(base: str) -> bool:
    folded = base.casefold()
    return (
        folded.endswith(".sha256")
        or folded.endswith(".hash")
        or "sha256" in folded
        or "identifying_hash" in folded
        or "identifying-hash" in folded
    )


def classify_artifact(relative_path: str) -> list[str]:
    """Return catalog kinds that apply to one relative path."""
    rel = relative_path.replace("\\", "/")
    base = os.path.basename(rel)
    folded = base.casefold()
    lower = rel.casefold()
    parent = os.path.basename(os.path.dirname(rel)).casefold()
    kinds: list[str] = []

    def add(kind: str) -> None:
        if kind not in kinds:
            kinds.append(kind)

    if folded in {"query-log.jsonl", "query_log.jsonl"} or folded.endswith(".query.jsonl"):
        add("query_log")
    if folded in {"audit-events.jsonl", "audit_events.jsonl"} or "audit" in folded:
        add("audit_log")
    if folded.startswith("research-log") or folded.startswith("research_log"):
        add("research_log")
        add("audit_log")
    if parent == "results" and folded.endswith(".json") and "relative" not in folded:
        add("saved_result_json")
    if "relative" in folded and folded.endswith(".json"):
        add("relative_output")
    if (
        folded.startswith("handoff")
        or "/packets/" in f"/{lower}/"
        or parent == "packets"
    ):
        add("handoff_packet")
    if (
        folded.endswith(".bak")
        or folded.endswith(".backup")
        or parent in {"backup", "backups"}
        or "/backups/" in f"/{lower}/"
        or "/backup/" in f"/{lower}/"
    ):
        add("backup")
    if parent == "queue" or folded in {"queue.jsonl", "queue.json"} or "/queue/" in f"/{lower}/":
        add("queue")
    if _is_hash_sidecar(base):
        add("identifying_hash_sidecar")
    return kinds


def _deadline_observation(values: list[str]) -> dict:
    """Pick the earliest timezone-aware instant. Do not sort the raw strings.

    Naive and invalid values stay in ``expires_at_unparsed`` and are not
    treated as UTC.
    """
    winner_raw = None
    winner_dt = None
    unparsed: list[str] = []
    for value in values:
        parsed = try_aware_deadline(value)
        if parsed is None:
            unparsed.append(value)
            continue
        if winner_dt is None or parsed < winner_dt:
            winner_dt = parsed
            winner_raw = value
    return {
        "expires_at_raw": winner_raw,
        "expires_at_unparsed": unparsed,
    }


def _expires_in_file(path: str) -> dict:
    values: list[str] = []
    try:
        if path.endswith(".json"):
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            values = iter_expires_at(data)
        elif path.endswith(".jsonl"):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        values.append(line)
                        continue
                    values.extend(iter_expires_at(data))
    except (OSError, json.JSONDecodeError):
        return {"expires_at_raw": None, "expires_at_unparsed": []}
    return _deadline_observation(values)


def _escapes_root(root: str, path: str) -> bool:
    root_real = os.path.realpath(root)
    try:
        target = os.path.realpath(path)
    except OSError:
        return True
    try:
        common = os.path.commonpath([root_real, target])
    except ValueError:
        return True
    return common != root_real


def _artifact_record(root: str, path: str, *, symlink: bool, directory: bool = False) -> dict:
    relative = os.path.relpath(path, root)
    record = {
        "relative_path": relative,
        "kinds": classify_artifact(relative) or ["unclassified"],
        "bytes": None,
        "expires_at_raw": None,
        "expires_at_unparsed": [],
        "would_delete": False,
        "symlink": symlink,
        "symlink_directory": directory and symlink,
        "followed": False,
        "escapes_root": _escapes_root(root, path) if symlink else False,
    }
    if symlink:
        return record
    try:
        record["bytes"] = os.path.getsize(path)
    except OSError:
        record["bytes"] = None
    observed = _expires_in_file(path)
    record["expires_at_raw"] = observed["expires_at_raw"]
    record["expires_at_unparsed"] = observed["expires_at_unparsed"]
    return record


def inventory_artifacts(root: str) -> dict:
    """Inventory files under ``root`` without deleting or copying contacts.

    Symlinks are listed and not followed, so a directory symlink cannot hide
    a target tree inside a claim that the scan was complete.
    """
    found: list[dict] = []
    root = os.path.abspath(root)
    if os.path.islink(root):
        found.append(_artifact_record(os.path.dirname(root) or "/", root, symlink=True, directory=True))
    elif os.path.isdir(root):
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            real_dirs = []
            for name in sorted(dirnames):
                path = os.path.join(dirpath, name)
                if os.path.islink(path):
                    found.append(_artifact_record(root, path, symlink=True, directory=True))
                    continue
                real_dirs.append(name)
            dirnames[:] = real_dirs
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                found.append(_artifact_record(root, path, symlink=os.path.islink(path)))
    kinds_found = sorted({kind for item in found for kind in item["kinds"]})
    catalog_kinds = [item["kind"] for item in PERSISTED_ARTIFACT_CATALOG]
    return {
        "root": root,
        "catalog": [dict(item) for item in PERSISTED_ARTIFACT_CATALOG],
        "artifacts": found,
        "kinds_found": kinds_found,
        "kinds_absent": [kind for kind in catalog_kinds if kind not in kinds_found],
        "symlinks_followed": False,
        "real_deletion_performed": False,
        "inventory_note": (
            "Symlink files and symlink directories are listed and not opened. "
            "Their targets are outside this inventory."
        ),
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
