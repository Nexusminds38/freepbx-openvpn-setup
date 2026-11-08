"""Explicit cleanup for a marked synthetic records store.

Dry-run is the default. Applying a cleanup deletes bytes only when the
caller passes ``apply=True``, the directory is marked synthetic, and an
explicit ``explicit_expires_at`` policy is present. This module does not
choose a retention duration. It does not follow symlinks, write hashes, or
leave an identifying tombstone.

Cleanup never rewrites a file and never creates a temporary copy. A file is
unlinked only when every record in it belongs to an expired episode. A log
or backup that also holds a record that must stay is left untouched, and
that blocks deletion of the expired episode's other files too.

The directory lock is advisory and is taken by this program's own writer
and cleanup. It does not stop a process that writes the store without the
lock. That case is unsupported. A change noticed before unlink cancels the
deletion. A change after that check is still possible for a noncooperating
writer; this code does not claim that gap is closed.

A fixture test of this module is not proof that production stores were deleted.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime

import retention

MARKER_NAME = "synthetic-store.json"
MARKER_SCHEMA = "enformion.synthetic_store.v1"
SYNTHETIC_APPLY_CONFIRMATION = "synthetic-test-store"

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
    """Mark an empty directory as a synthetic test store.

    A directory that already has entries cannot be labeled. That keeps a
    marker file from authorizing cleanup of an existing store.
    """
    absolute = os.path.abspath(directory)
    parent = os.path.dirname(absolute)
    if os.path.islink(directory) or os.path.realpath(parent) != os.path.abspath(parent):
        raise CleanupError("refusing to mark a records path that resolves through a symlink")
    if os.path.lexists(directory):
        if not os.path.isdir(directory) or os.path.islink(directory):
            raise CleanupError("refusing to mark a path that is not a real directory")
        if os.path.realpath(directory) != absolute:
            raise CleanupError("refusing to mark a records path that resolves through a symlink")
        with os.scandir(directory) as entries:
            if any(True for _ in entries):
                raise CleanupError("refusing to mark a directory that already contains entries")
    else:
        os.makedirs(directory, mode=0o700)
    path = os.path.join(directory, MARKER_NAME)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
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
    confirm: str = "",
) -> dict:
    """Apply cleanup only for a marked synthetic test store.

    ``apply`` defaults false. A marker file alone is not enough: ``confirm``
    must be ``synthetic-test-store``. Missing policy, a duration field, a
    symlink, a hardlink, or an unparseable record blocks deletion.
    """
    report = _build_report(root, now=now, policy=policy)
    report["applied"] = False
    if not apply:
        report["reason"] = "dry-run default; no bytes were deleted"
        report["counts"]["files_after"] = report["counts"]["files_before"]
        return _public(report)
    if confirm != SYNTHETIC_APPLY_CONFIRMATION:
        report["reason"] = "synthetic cleanup confirmation is required; no bytes were deleted"
        report["counts"]["files_after"] = report["counts"]["files_before"]
        report["live_linkage"] = "blocked"
        return _public(report)
    if not retention.policy_is_explicit(policy):
        report["reason"] = (
            "explicit expires_at policy is required before cleanup; "
            "no retention duration is configured"
        )
        report["counts"]["files_after"] = report["counts"]["files_before"]
        report["live_linkage"] = "blocked"
        return _public(report)
    if report["containment_blockers"] or report["store_blockers"]:
        report["reason"] = "cleanup blocked; no bytes were deleted"
        report["counts"]["files_after"] = report["counts"]["files_before"]
        report["live_linkage"] = "blocked"
        return _public(report)
    if not _marker_is_synthetic(root):
        report["reason"] = "cleanup apply is limited to a marked synthetic store"
        report["counts"]["files_after"] = report["counts"]["files_before"]
        report["live_linkage"] = "blocked"
        return _public(report)
    return _apply(root, now, report)


def _build_report(root: str, *, now: datetime, policy: object) -> dict:
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise CleanupError("comparison clock must be timezone-aware")
    report = _empty_report(now, policy)
    contained, blockers, seen = _containment(root)
    report["containment_blockers"] = blockers
    if not contained:
        report["counts"]["files_before"] = seen
        report["counts"]["files_after"] = seen
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
            "unknown_files": 0,
            "transaction_artifacts": 0,
            "conflicting_deadline_records": 0,
            "eligible_files": 0,
            "eligible_log_lines": 0,
            "mixed_files": 0,
            "eligible_sidecars": 0,
            "removed_files": 0,
            "removed_log_lines": 0,
            "removed_sidecars": 0,
        },
    }


def _containment(root: str) -> tuple[bool, list[str], int]:
    blockers: list[str] = []
    seen = 0
    if not os.path.isdir(root) or os.path.islink(root):
        return False, ["records path is missing or is a symlink"], 0
    if os.path.realpath(root) != os.path.abspath(root):
        return False, ["records path resolves through a symlink"], 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames):
            path = os.path.join(dirpath, name)
            if os.path.islink(path) or _escapes(root, path):
                blockers.append("symlink or escaping directory; targets are not followed or deleted")
                dirnames.remove(name)
        for name in filenames:
            path = os.path.join(dirpath, name)
            relative = os.path.relpath(path, root)
            seen += 1
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or _escapes(root, path) or _traverses(relative):
                blockers.append("symlink or escaping file; targets are not followed or deleted")
            elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                blockers.append("hardlink; cleanup will not unlink a shared inode")
    return not blockers, blockers, seen


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


def _read_text_nofollow(path: str) -> str:
    """Read a regular file without following a symlink or a shared hardlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("refusing to read a linked file")
        chunks: list[bytes] = []
        while True:
            piece = os.read(descriptor, 65536)
            if not piece:
                break
            chunks.append(piece)
    finally:
        os.close(descriptor)
    return b"".join(chunks).decode("utf-8")


def _marker_is_synthetic(root: str) -> bool:
    path = os.path.join(root, MARKER_NAME)
    if not os.path.isfile(path) or os.path.islink(path):
        return False
    try:
        data = json.loads(_read_text_nofollow(path))
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
    if len(set(parsed)) != 1:
        return "conflict", None
    return "aware", parsed[0]


def _classify_store(root: str, now: datetime, report: dict) -> None:
    counts = report["counts"]
    episodes: dict[str, dict] = {}
    file_units: list[dict] = []
    line_units: list[dict] = []
    sidecars: list[dict] = []
    slug_owner: dict[str, str] = {}
    report["_files"] = {}

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
            if name.endswith(".cleanup-tmp"):
                counts["transaction_artifacts"] += 1
                sibling = path[: -len(".cleanup-tmp")]
                info = os.lstat(path)
                if (
                    stat.S_ISREG(info.st_mode)
                    and info.st_nlink == 1
                    and os.path.isfile(sibling)
                    and not os.path.islink(sibling)
                ):
                    report.setdefault("_temps", []).append(path)
                else:
                    report["store_blockers"].append(
                        "cleanup temp has no canonical file; refusing to guess"
                    )
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
                counts["unknown_files"] += 1
                counts["ambiguous_records"] += 1
                report["store_blockers"].append("unclassified file blocks cleanup")

    expired: set[str] = set()
    for episode_id, row in episodes.items():
        instants = set(row["deadlines"])
        if row["blocked"] or not instants:
            counts["blocked_episodes"] += 1
            continue
        if len(instants) != 1:
            row["blocked"] = True
            counts["blocked_episodes"] += 1
            counts["conflicting_deadline_records"] += 1
            continue
        earliest = next(iter(instants))
        if earliest <= now:
            expired.add(episode_id)
            counts["expired_episodes"] += 1
        else:
            counts["retained_episodes"] += 1

    report["_expired"] = expired
    report["_unlink"] = _exclusive_expired_files(report["_files"], episodes, expired, counts)
    tied_sidecars = []
    for sidecar in sidecars:
        owner = _sidecar_owner(sidecar["name"], slug_owner)
        if owner is None:
            counts["ambiguous_records"] += 1
            report["store_blockers"].append("untied identifying sidecar blocks cleanup")
            continue
        row = episodes.get(owner)
        if row is None or row["blocked"] or row.get("mixed") or owner not in expired:
            continue
        tied_sidecars.append(sidecar)
    report["_sidecars"] = tied_sidecars
    counts["eligible_files"] = len(report["_unlink"])
    counts["eligible_log_lines"] = sum(item["lines"] for item in report["_unlink"])
    counts["eligible_sidecars"] = len(tied_sidecars)
    counts["files_after"] = counts["files_before"]


def _note_file(report: dict, path: str, kind: str, episode_id: str | None, keep: bool) -> dict:
    entry = report["_files"].setdefault(
        path, {"keep": False, "episodes": set(), "lines": 0, "kind": kind}
    )
    if keep:
        entry["keep"] = True
    if episode_id:
        entry["episodes"].add(episode_id)
    return entry


def _exclusive_expired_files(files: dict, episodes: dict, expired: set[str], counts: dict) -> list[dict]:
    """Whole files whose every record belongs to an expired episode.

    A file that also holds a retained, blocked, or ownerless record is not
    rewritten. Expired episodes in that file are marked mixed so their other
    files stay too.
    """
    for entry in files.values():
        if entry["keep"] or not entry["episodes"]:
            for episode_id in entry["episodes"]:
                if episode_id in expired:
                    episodes[episode_id]["mixed"] = True
            if entry["keep"] and entry["episodes"]:
                counts["mixed_files"] += 1
            continue
        if any(episode_id not in expired or episodes[episode_id]["blocked"] for episode_id in entry["episodes"]):
            counts["mixed_files"] += 1
            for episode_id in entry["episodes"]:
                if episode_id in expired:
                    episodes[episode_id]["mixed"] = True
            continue
        entry["exclusive"] = True
    for entry in files.values():
        if entry.get("exclusive") and any(
            episodes[episode_id].get("mixed") or episodes[episode_id]["blocked"]
            for episode_id in entry["episodes"]
        ):
            entry["exclusive"] = False
            for episode_id in entry["episodes"]:
                episodes[episode_id]["mixed"] = True
    return [
        {"path": path, "lines": entry["lines"] if entry["kind"] == "jsonl" else 0}
        for path, entry in files.items()
        if entry.get("exclusive")
    ]


def _take_json(path: str, relative: str, episode, file_units: list, report: dict) -> None:
    counts = report["counts"]
    try:
        text = _read_text_nofollow(path)
        data = json.loads(text)
    except (OSError, json.JSONDecodeError):
        counts["malformed_deadline_records"] += 1
        report["store_blockers"].append("unreadable file blocks cleanup")
        return
    ids = _episode_ids(data)
    state, deadline = _deadline_state(data)
    entry = _note_file(report, path, "json", None, keep=False)
    entry["text"] = text
    if len(ids) != 1 or state != "aware" or deadline is None:
        entry["keep"] = True
        _count_bad(state if len(ids) == 1 else "ambiguous", counts)
        if len(ids) == 1:
            episode(next(iter(ids)))["blocked"] = True
            entry["episodes"].add(next(iter(ids)))
        return
    episode_id = next(iter(ids))
    row = episode(episode_id)
    row["deadlines"].append(deadline)
    entry["episodes"].add(episode_id)
    file_units.append({"path": path, "relative": relative, "episode_id": episode_id})


def _take_jsonl(path: str, relative: str, episode, line_units: list, report: dict) -> None:
    counts = report["counts"]
    try:
        text = _read_text_nofollow(path)
        raw_lines = text.splitlines(keepends=True)
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
    entry = _note_file(report, path, "jsonl", None, keep=False)
    entry["text"] = text
    for index, data in parsed:
        ids = _episode_ids(data)
        state, deadline = _deadline_state(data)
        entry["lines"] += 1
        if len(ids) != 1 or state != "aware" or deadline is None:
            entry["keep"] = True
            _count_bad(state if len(ids) == 1 else "ambiguous", counts)
            if len(ids) == 1:
                episode(next(iter(ids)))["blocked"] = True
                entry["episodes"].add(next(iter(ids)))
            continue
        episode_id = next(iter(ids))
        row = episode(episode_id)
        row["deadlines"].append(deadline)
        entry["episodes"].add(episode_id)
        line_units.append({"path": path, "relative": relative, "line_index": index, "episode_id": episode_id})


def _count_bad(state: str, counts: dict) -> None:
    if state == "missing":
        counts["missing_deadline_records"] += 1
    elif state == "malformed":
        counts["malformed_deadline_records"] += 1
    elif state == "conflict":
        counts["conflicting_deadline_records"] += 1
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
        import audit
        with audit._store_lock(root):
            for item in report.get("_unlink", []):
                current = _read_text_nofollow(item["path"])
                if current != report["_files"][item["path"]].get("text"):
                    raise OSError("file changed before unlink")
            for path in report.get("_temps", []):
                _unlink_contained(root, path)
                removed_files += 1
            for item in report.get("_unlink", []):
                _unlink_contained(root, item["path"])
                removed_files += 1
                removed_lines += item["lines"]
            for sidecar in report.get("_sidecars", []):
                _unlink_contained(root, sidecar["path"])
                removed_sidecars += 1
                removed_files += 1
            _remove_empty_dirs(root)
    except OSError:
        if removed_files == 0 and removed_lines == 0 and removed_sidecars == 0:
            report["partial_failure"] = False
            report["applied"] = False
            report["reason"] = "file changed before unlink; no records were deleted"
            counts["files_after"] = counts["files_before"]
            report["live_linkage"] = "blocked"
            report["retention_compliance_claimed"] = False
            return _public(report)
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


def _require_exclusive_file(root: str, path: str) -> None:
    info = os.lstat(path)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or _escapes(root, path)
    ):
        raise OSError("refusing to rewrite a linked or escaping file")


def _unlink_contained(root: str, path: str) -> None:
    _require_exclusive_file(root, path)
    os.unlink(path)


def _recover_transaction_files(root: str) -> int:
    """Delete leftover cleanup temp files. Do not read their contents."""
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))]
        for name in filenames:
            if not name.endswith(".cleanup-tmp"):
                continue
            path = os.path.join(dirpath, name)
            _unlink_contained(root, path)
            removed += 1
    return removed


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
    for key in ("_expired", "_file_units", "_line_units", "_sidecars", "_files", "_unlink", "_temps"):
        report.pop(key, None)
