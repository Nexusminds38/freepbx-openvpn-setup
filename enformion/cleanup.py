"""Explicit cleanup for a marked synthetic records store.

Dry-run is the default. Applying a cleanup deletes bytes only when the
caller passes ``apply=True``, the directory is marked synthetic, and an
explicit ``explicit_expires_at`` policy is present. This module does not
choose a retention duration. It does not follow symlinks, write hashes, or
leave an identifying tombstone.

A fixture test of this module is not proof that production stores were deleted.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import retention

MARKER_NAME = "synthetic-store.json"
MARKER_SCHEMA = "enformion.synthetic_store.v1"

OUT_OF_SCOPE_STORES = (
    "Any records directory that is not explicitly marked synthetic.",
    "The historical hidden_files/enformion-records path.",
    "Home-directory credential files and any /home/box client.",
    "Bookings databases, deployed collectors, and the 5-county monitor.",
    "The separate Roster-watch repository.",
    "Provider-side retention, chat transcripts, platform backups, and disk snapshots.",
)


class CleanupError(RuntimeError):
    """Cleanup could not safely continue."""


def marker_document() -> dict:
    """Non-identifying marker. It carries no episode, name, or deadline."""
    return {
        "schema": MARKER_SCHEMA,
        "synthetic_store": True,
        "production_destination": None,
        "live_lane_permitted": False,
        "identifying_records": False,
    }


def mark_synthetic_store(directory: str) -> str:
    """Write the synthetic marker. Refuses symlink paths."""
    if os.path.islink(directory) or os.path.realpath(directory) != os.path.abspath(directory):
        raise CleanupError("refusing to mark a records path that resolves through a symlink")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, MARKER_NAME)
    if os.path.islink(path):
        raise CleanupError("refusing to write the synthetic marker through a symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, (json.dumps(marker_document(), indent=2, sort_keys=True) + "\n").encode())
    finally:
        os.close(descriptor)
    return path


def plan_cleanup(root: str, *, now: datetime, policy: object = None) -> dict:
    """Count what an explicit cleanup would remove. This does not delete."""
    return _public(_build_report(root, now=now, policy=policy))


def apply_synthetic_cleanup(
    root: str,
    *,
    now: datetime,
    policy: object = None,
    apply: bool = False,
) -> dict:
    """Apply cleanup only for a marked synthetic store.

    ``apply`` defaults false. Missing policy, a duration field, a missing
    marker, a symlink, or an unparseable record blocks deletion.
    """
    report = _build_report(root, now=now, policy=policy)
    report["applied"] = False
    if not apply:
        report["reason"] = "dry-run default; no bytes were deleted"
        return _public(report)
    if not retention.policy_is_explicit(policy):
        report["reason"] = (
            "explicit expires_at policy is required before cleanup; "
            "no retention duration is configured"
        )
        report["live_linkage"] = "blocked"
        return _public(report)
    if report["containment_blockers"] or report["store_blockers"]:
        report["reason"] = "cleanup blocked; no bytes were deleted"
        report["live_linkage"] = "blocked"
        return _public(report)
    if not _marker_is_synthetic(root):
        report["reason"] = "cleanup apply is limited to a marked synthetic store"
        report["live_linkage"] = "blocked"
        return _public(report)
    return _apply(root, now, report)


def _build_report(root: str, *, now: datetime, policy: object) -> dict:
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise CleanupError("comparison clock must be timezone-aware")
    report = _empty_report(now, policy)
    contained, blockers = _containment(root)
    report["containment_blockers"] = blockers
    if not contained:
        report["live_linkage"] = "blocked"
        return report
    _classify_store(root, now, report)
    report["live_linkage"] = "blocked"
    return report


def _public(report: dict) -> dict:
    cleaned = dict(report)
    _strip_private(cleaned)
    return cleaned


def _empty_report(now: datetime, policy: object) -> dict:
    return {
        "applied": False,
        "partial_failure": False,
        "live_linkage": "blocked",
        "production_destination": None,
        "retention_compliance_claimed": False,
        "production_deletion_proof": False,
        "policy_present": retention.policy_is_explicit(policy),
        "evaluated_at": now.isoformat(),
        "reason": "",
        "containment_blockers": [],
        "store_blockers": [],
        "out_of_scope_stores": list(OUT_OF_SCOPE_STORES),
        "counts": {
            "files_before": 0,
            "files_after": 0,
            "expired_episodes": 0,
            "retained_episodes": 0,
            "blocked_episodes": 0,
            "missing_deadline_records": 0,
            "malformed_deadline_records": 0,
            "ambiguous_records": 0,
            "eligible_files": 0,
            "eligible_log_lines": 0,
            "eligible_sidecars": 0,
            "removed_files": 0,
            "removed_log_lines": 0,
            "removed_sidecars": 0,
        },
    }


def _containment(root: str) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    if not os.path.isdir(root) or os.path.islink(root):
        return False, ["records path is missing or is a symlink"]
    if os.path.realpath(root) != os.path.abspath(root):
        return False, ["records path resolves through a symlink"]
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames):
            path = os.path.join(dirpath, name)
            if os.path.islink(path) or _escapes(root, path):
                blockers.append("symlink or escaping directory; targets are not followed or deleted")
                dirnames.remove(name)
        for name in filenames:
            path = os.path.join(dirpath, name)
            relative = os.path.relpath(path, root)
            if os.path.islink(path) or _escapes(root, path) or _traverses(relative):
                blockers.append("symlink or escaping file; targets are not followed or deleted")
    return not blockers, blockers


def _escapes(root: str, path: str) -> bool:
    if os.path.islink(path):
        return True
    root_real = os.path.realpath(root)
    try:
        target = os.path.realpath(path)
        common = os.path.commonpath([root_real, target])
    except (OSError, ValueError):
        return True
    return common != root_real


def _traverses(relative: str) -> bool:
    parts = relative.replace("\\", "/").split("/")
    return any(part in {"", ".", ".."} for part in parts)


def _marker_is_synthetic(root: str) -> bool:
    path = os.path.join(root, MARKER_NAME)
    if not os.path.isfile(path) or os.path.islink(path):
        return False
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(data, dict)
        and data.get("schema") == MARKER_SCHEMA
        and data.get("synthetic_store") is True
        and data.get("production_destination") is None
        and data.get("live_lane_permitted") is False
        and data.get("identifying_records") is False
    )


def _episode_ids(node: object) -> set[str]:
    if not isinstance(node, dict):
        return set()
    found: set[str] = set()
    episode_id = node.get("episode_id")
    if isinstance(episode_id, str) and episode_id.strip():
        found.add(episode_id)
    episode = node.get("episode")
    if isinstance(episode, dict):
        nested = episode.get("episode_id")
        if isinstance(nested, str) and nested.strip():
            found.add(nested)
    return found


def _deadline_state(node: object) -> tuple[str, datetime | None]:
    values = retention.iter_expires_at(node)
    if not values:
        return "missing", None
    parsed: list[datetime] = []
    for value in values:
        deadline = retention.try_aware_deadline(value)
        if deadline is None:
            return "malformed", None
        parsed.append(deadline)
    return "aware", min(parsed)


def _classify_store(root: str, now: datetime, report: dict) -> None:
    counts = report["counts"]
    episodes: dict[str, dict] = {}
    file_units: list[dict] = []
    line_units: list[dict] = []
    sidecars: list[dict] = []
    slug_owner: dict[str, str] = {}

    def episode(episode_id: str) -> dict:
        row = episodes.get(episode_id)
        if row is None:
            row = {"deadlines": [], "blocked": False}
            episodes[episode_id] = row
            slug = retention.episode_slug(episode_id)
            previous = slug_owner.get(slug)
            if previous is not None and previous != episode_id:
                row["blocked"] = True
                episodes[previous]["blocked"] = True
            else:
                slug_owner[slug] = episode_id
        return row

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))]
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            relative = os.path.relpath(path, root)
            if os.path.islink(path):
                continue
            counts["files_before"] += 1
            if name == MARKER_NAME and dirpath == root:
                continue
            if retention.is_hash_sidecar(path):
                sidecars.append({"path": path, "relative": relative, "name": name})
                continue
            kind = _record_kind(path)
            if kind == "json":
                _take_json(path, relative, episode, file_units, report)
            elif kind == "jsonl":
                _take_jsonl(path, relative, episode, line_units, report)
            else:
                counts["ambiguous_records"] += 1
                report["store_blockers"].append("unclassified file blocks cleanup")

    expired: set[str] = set()
    for episode_id, row in episodes.items():
        if row["blocked"] or not row["deadlines"]:
            counts["blocked_episodes"] += 1
            continue
        earliest = min(row["deadlines"])
        if earliest <= now:
            expired.add(episode_id)
            counts["expired_episodes"] += 1
        else:
            counts["retained_episodes"] += 1

    report["_expired"] = expired
    report["_file_units"] = [
        unit for unit in file_units if unit["episode_id"] in expired and not episodes[unit["episode_id"]]["blocked"]
    ]
    report["_line_units"] = {}
    for unit in line_units:
        if unit["episode_id"] in expired and not episodes[unit["episode_id"]]["blocked"]:
            report["_line_units"].setdefault(unit["path"], set()).add(unit["line_index"])
    tied_sidecars = []
    for sidecar in sidecars:
        owner = _sidecar_owner(sidecar["name"], slug_owner)
        if owner is None:
            counts["ambiguous_records"] += 1
            continue
        row = episodes.get(owner)
        if row is None or row["blocked"] or owner not in expired:
            continue
        tied_sidecars.append(sidecar)
    report["_sidecars"] = tied_sidecars
    counts["eligible_files"] = len(report["_file_units"])
    counts["eligible_log_lines"] = sum(len(lines) for lines in report["_line_units"].values())
    counts["eligible_sidecars"] = len(tied_sidecars)
    counts["files_after"] = counts["files_before"]


def _take_json(path: str, relative: str, episode, file_units: list, report: dict) -> None:
    counts = report["counts"]
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        counts["malformed_deadline_records"] += 1
        report["store_blockers"].append("unreadable file blocks cleanup")
        return
    ids = _episode_ids(data)
    state, deadline = _deadline_state(data)
    if len(ids) != 1 or state != "aware" or deadline is None:
        _count_bad(state if len(ids) == 1 else "ambiguous", counts)
        if len(ids) == 1:
            episode(next(iter(ids)))["blocked"] = True
        return
    episode_id = next(iter(ids))
    row = episode(episode_id)
    row["deadlines"].append(deadline)
    file_units.append({"path": path, "relative": relative, "episode_id": episode_id})


def _take_jsonl(path: str, relative: str, episode, line_units: list, report: dict) -> None:
    counts = report["counts"]
    try:
        with open(path, encoding="utf-8") as handle:
            raw_lines = handle.readlines()
    except OSError:
        counts["malformed_deadline_records"] += 1
        report["store_blockers"].append("unreadable file blocks cleanup")
        return
    parsed: list[tuple[int, object]] = []
    for index, raw in enumerate(raw_lines):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            parsed.append((index, json.loads(stripped)))
        except json.JSONDecodeError:
            counts["malformed_deadline_records"] += 1
            report["store_blockers"].append("unreadable log line blocks cleanup")
            return
    for index, data in parsed:
        ids = _episode_ids(data)
        state, deadline = _deadline_state(data)
        if len(ids) != 1 or state != "aware" or deadline is None:
            _count_bad(state if len(ids) == 1 else "ambiguous", counts)
            if len(ids) == 1:
                episode(next(iter(ids)))["blocked"] = True
            continue
        episode_id = next(iter(ids))
        row = episode(episode_id)
        row["deadlines"].append(deadline)
        line_units.append({"path": path, "relative": relative, "line_index": index, "episode_id": episode_id})


def _count_bad(state: str, counts: dict) -> None:
    if state == "missing":
        counts["missing_deadline_records"] += 1
    elif state == "malformed":
        counts["malformed_deadline_records"] += 1
    else:
        counts["ambiguous_records"] += 1


def _record_kind(path: str) -> str:
    folded = path.casefold()
    if folded.endswith(".jsonl") or folded.endswith(".jsonl.bak") or folded.endswith(".jsonl.backup"):
        return "jsonl"
    if folded.endswith(".json") or folded.endswith(".json.bak") or folded.endswith(".json.backup"):
        return "json"
    return ""


def _sidecar_owner(name: str, slug_owner: dict[str, str]) -> str | None:
    stem = name
    folded = name.casefold()
    for suffix in (".sha256", ".hash"):
        if folded.endswith(suffix):
            stem = name[: -len(suffix)]
            break
    owner = slug_owner.get(stem)
    if owner:
        return owner
    if stem.startswith("handoff-"):
        return slug_owner.get(stem[len("handoff-"):])
    return None


def _apply(root: str, now: datetime, report: dict) -> dict:
    counts = report["counts"]
    removed_files = 0
    removed_lines = 0
    removed_sidecars = 0
    try:
        for path, indexes in sorted(report["_line_units"].items()):
            removed_lines += _drop_lines(root, path, indexes, now)
        for unit in report["_file_units"]:
            _unlink_contained(root, unit["path"])
            removed_files += 1
        for sidecar in report["_sidecars"]:
            _unlink_contained(root, sidecar["path"])
            removed_sidecars += 1
            removed_files += 1
        _remove_empty_dirs(root)
    except OSError:
        report["partial_failure"] = True
        report["applied"] = True
        report["reason"] = "cleanup stopped after a partial failure; no tombstone was written"
        counts["removed_files"] = removed_files
        counts["removed_log_lines"] = removed_lines
        counts["removed_sidecars"] = removed_sidecars
        counts["files_after"] = _count_files(root)
        report["live_linkage"] = "blocked"
        report["retention_compliance_claimed"] = False
        return _public(report)
    counts["removed_files"] = removed_files
    counts["removed_log_lines"] = removed_lines
    counts["removed_sidecars"] = removed_sidecars
    counts["files_after"] = _count_files(root)
    report["applied"] = True
    report["reason"] = "synthetic cleanup applied; production deletion is not claimed"
    report["live_linkage"] = "blocked"
    report["retention_compliance_claimed"] = False
    return _public(report)


def _drop_lines(root: str, path: str, indexes: set[int], now: datetime) -> int:
    if os.path.islink(path) or _escapes(root, path):
        raise OSError("refusing to rewrite outside the synthetic store")
    with open(path, encoding="utf-8") as handle:
        raw_lines = handle.readlines()
    kept: list[str] = []
    removed = 0
    for index, raw in enumerate(raw_lines):
        if index not in indexes:
            kept.append(raw if raw.endswith("\n") or raw == "" else raw + "\n")
            continue
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError as exc:
            raise OSError("log line changed before cleanup") from exc
        state, deadline = _deadline_state(data)
        ids = _episode_ids(data)
        if state != "aware" or deadline is None or deadline > now or len(ids) != 1:
            raise OSError("log line is no longer an expired episode")
        removed += 1
    temporary = path + ".cleanup-tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.write(descriptor, "".join(kept).encode())
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return removed


def _unlink_contained(root: str, path: str) -> None:
    if os.path.islink(path) or not os.path.isfile(path) or _escapes(root, path):
        raise OSError("refusing to delete outside the synthetic store")
    os.unlink(path)


def _remove_empty_dirs(root: str) -> None:
    for name in ("results", "packets", "backups", "queue"):
        path = os.path.join(root, name)
        if os.path.isdir(path) and not os.path.islink(path) and not os.listdir(path):
            os.rmdir(path)


def _count_files(root: str) -> int:
    total = 0
    for _dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(_dirpath, name))]
        total += sum(1 for name in filenames if not os.path.islink(os.path.join(_dirpath, name)))
    return total


def _strip_private(report: dict) -> None:
    for key in ("_expired", "_file_units", "_line_units", "_sidecars"):
        report.pop(key, None)
