"""Local audit writes for an explicitly authorized offline copy.

Writes require an explicit retention policy, a timezone-aware unexpired
``expires_at``, and an explicit records directory. The historical default
under ``hidden_files`` is not opened. Identifying hashes are not stored.
Live linkage stays blocked. Missing policy writes nothing and deletes nothing.
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import retention


@contextmanager
def _store_lock(directory: str):
    """Exclusive lock on the directory itself. No lock file is left behind."""
    if os.path.islink(directory) or os.path.realpath(directory) != os.path.abspath(directory):
        raise retention.RetentionRejected("records directory is a symlink; refusing to follow it")
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def new_lookup_id() -> str:
    return uuid.uuid4().hex[:12]


def _write(path: str, text: str, *, append: bool = False) -> None:
    if os.path.islink(path):
        raise retention.RetentionRejected(
            "refusing to write through a symlink in the records directory"
        )
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_APPEND if append else os.O_TRUNC
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise retention.RetentionRejected(
            "refusing to write through a records path that cannot be opened without following a symlink"
        ) from exc
    try:
        os.write(descriptor, text.encode("utf-8"))
    finally:
        os.close(descriptor)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _ensure_dir(path: str) -> None:
    if os.path.islink(path):
        raise retention.RetentionRejected(
            "records directory is a symlink; refusing to follow it"
        )
    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.path.islink(path) or not os.path.isdir(path):
        raise retention.RetentionRejected(
            "records directory is a symlink; refusing to follow it"
        )
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _guard_directory(directory: str) -> None:
    """Refuse symlink roots, symlink children, and hash sidecars before any write."""
    if os.path.islink(directory):
        raise retention.RetentionRejected(
            "records directory is a symlink; refusing to follow it"
        )
    absolute = os.path.abspath(directory)
    if os.path.realpath(directory) != absolute:
        raise retention.RetentionRejected(
            "records directory path resolves through a symlink; refusing to follow it"
        )
    if not os.path.isdir(directory):
        return
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        for name in list(dirnames):
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                raise retention.RetentionRejected(
                    "symlink inside the records directory blocks persistence"
                )
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                raise retention.RetentionRejected(
                    "symlink inside the records directory blocks persistence"
                )
            if "identifying_hash_sidecar" in retention.classify_artifact(name):
                raise retention.RetentionRejected(
                    "identifying hash sidecar blocks persistence; this lane does not write or delete hashes"
                )


def _record_kind(path: str) -> str:
    folded = path.casefold()
    if folded.endswith(".jsonl") or folded.endswith(".jsonl.bak") or folded.endswith(".jsonl.backup"):
        return "jsonl"
    if folded.endswith(".json") or folded.endswith(".json.bak") or folded.endswith(".json.backup"):
        return "json"
    return ""


def _read_deadlines(path: str, episode_id: str) -> list[str]:
    if _record_kind(path) == "jsonl":
        found: list[str] = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise retention.RetentionRejected(
                        "unreadable records file blocks persistence; refusing to ignore its deadline"
                    ) from exc
                if retention.mentions_episode(data, episode_id):
                    found.extend(retention.iter_expires_at(data))
        return found
    if _record_kind(path) == "json":
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise retention.RetentionRejected(
                "unreadable records file blocks persistence; refusing to ignore its deadline"
            ) from exc
        if retention.mentions_episode(data, episode_id):
            return retention.iter_expires_at(data)
    return []


def _stored_deadlines(directory: str, episode_id: str) -> list[str]:
    if not episode_id or not os.path.isdir(directory) or os.path.islink(directory):
        return []
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames[:] = [
            name for name in dirnames
            if not os.path.islink(os.path.join(dirpath, name))
        ]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            found.extend(_read_deadlines(path, episode_id))
    return found


def _rewrite_tree(directory: str, episode_id: str, deadline: str) -> None:
    if not episode_id or not os.path.isdir(directory) or os.path.islink(directory):
        return
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames[:] = [
            name for name in dirnames
            if not os.path.islink(os.path.join(dirpath, name))
        ]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                files.append(path)
    for path in files:
        if _record_kind(path) == "jsonl":
            _rewrite_jsonl(path, episode_id, deadline)
        elif _record_kind(path) == "json":
            _rewrite_json(path, episode_id, deadline)


def _rewrite_jsonl(path: str, episode_id: str, deadline: str) -> None:
    with open(path, encoding="utf-8") as handle:
        raw_lines = handle.readlines()
    changed = False
    output: list[str] = []
    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            output.append(raw if raw.endswith("\n") else raw + "\n")
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise retention.RetentionRejected(
                "unreadable records file blocks persistence; refusing to ignore its deadline"
            ) from exc
        if isinstance(data, dict) and retention.mentions_episode(data, episode_id):
            updated = retention.rewrite_expires_at(data, deadline)
            if updated != data:
                changed = True
            output.append(json.dumps(updated, sort_keys=True) + "\n")
        else:
            output.append(raw if raw.endswith("\n") else raw + "\n")
    if changed:
        _write(path, "".join(output))


def _rewrite_json(path: str, episode_id: str, deadline: str) -> None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise retention.RetentionRejected(
            "unreadable records file blocks persistence; refusing to ignore its deadline"
        ) from exc
    if not retention.mentions_episode(data, episode_id):
        return
    updated = retention.rewrite_expires_at(data, deadline)
    if updated != data:
        _write(path, json.dumps(updated, indent=2, sort_keys=True) + "\n")


def _values_for_episode(data: object, episode_id: str, key: str) -> list[str]:
    if not isinstance(data, dict) or not retention.mentions_episode(data, episode_id):
        return []
    found: list[str] = []
    if isinstance(data.get(key), str):
        found.append(data[key])
    provenance = data.get("provenance")
    if isinstance(provenance, dict) and isinstance(provenance.get(key), str):
        found.append(provenance[key])
    return found


def _stored_origins(directory: str, episode_id: str) -> list[str]:
    return _stored_strings(directory, episode_id, "first_collected_at")


def _stored_strings(directory: str, episode_id: str, key: str) -> list[str]:
    if not episode_id or not os.path.isdir(directory) or os.path.islink(directory):
        return []
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames[:] = [
            name for name in dirnames
            if not os.path.islink(os.path.join(dirpath, name))
        ]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path) or name.endswith(".cleanup-tmp"):
                continue
            found.extend(_strings_in_file(path, episode_id, key))
    return found


def _strings_in_file(path: str, episode_id: str, key: str) -> list[str]:
    if _record_kind(path) == "jsonl":
        found: list[str] = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise retention.RetentionRejected(
                        "unreadable records file blocks persistence; refusing to ignore its deadline"
                    ) from exc
                found.extend(_values_for_episode(data, episode_id, key))
        return found
    if _record_kind(path) == "json":
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise retention.RetentionRejected(
                "unreadable records file blocks persistence; refusing to ignore its deadline"
            ) from exc
        return _values_for_episode(data, episode_id, key)
    return []


def _deadline_for_write(
    directory: str,
    episode_id: str,
    proposed: object,
    now: datetime,
    packet: dict | None = None,
    policy: object = None,
    first_collected_at: object = None,
) -> str:
    _guard_directory(directory)
    stored = _stored_deadlines(directory, episode_id)
    if retention.policy_is_owner_23h(policy):
        earliest = retention.bound_owner_deadline(
            first_collected_at,
            proposed,
            stored,
            _stored_origins(directory, episode_id),
            now=now,
        )
        return earliest.isoformat()
    packet_deadlines = retention.iter_expires_at(packet) if packet is not None else []
    earliest = retention.resolve_episode_deadline(
        proposed,
        stored,
        now=now,
        packet_deadlines=packet_deadlines,
    )
    return earliest.isoformat()


def _blocked(reason: str, expires_at: datetime | None = None) -> dict:
    return {
        "persisted": False,
        "live_linkage": "blocked",
        "reason": reason,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


def _authorize(policy: object, expires_at: object, now: datetime | None) -> retention.Authorization:
    clock = now or datetime.now(timezone.utc)
    return retention.authorize(policy, expires_at, now=clock)


def _slug(episode_id: str) -> str:
    return retention.episode_slug(episode_id)


def log_event(
    *,
    policy: object = None,
    expires_at: object = None,
    directory: str | None = None,
    now: datetime | None = None,
    episode_id: str = "",
    outcome: str = "",
    reason: str = "",
    adapter: str = "mock",
) -> dict:
    """Append one research-log line when retention authorization allows it."""
    clock = now or datetime.now(timezone.utc)
    auth = _authorize(policy, expires_at, clock)
    if not auth.allowed or auth.expires_at is None:
        return _blocked(auth.reason)
    if not directory:
        return _blocked(
            "records directory is not configured; persistence blocked; live linkage blocked",
            auth.expires_at,
        )
    try:
        deadline = _deadline_for_write(directory, episode_id, auth.expires_at, clock)
        _ensure_dir(directory)
        _rewrite_tree(directory, episode_id, deadline)
        event = {
            "episode_id": episode_id,
            "outcome": outcome,
            "reason": reason,
            "adapter": adapter,
            "expires_at": deadline,
            "lane": "disconnected_mock",
            "live_linkage": "blocked",
        }
        _write(
            os.path.join(directory, "research-log.jsonl"),
            json.dumps(event, sort_keys=True) + "\n",
            append=True,
        )
    except retention.RetentionRejected as exc:
        return _blocked(str(exc))
    return {
        "persisted": True,
        "live_linkage": "blocked",
        "reason": auth.reason,
        "expires_at": deadline,
        "event": event,
    }


def store_result(
    *,
    directory: str | None,
    payload: dict,
    policy: object = None,
    expires_at: object = None,
    now: datetime | None = None,
) -> dict:
    """Store one redacted result JSON when retention authorization allows it."""
    clock = now or datetime.now(timezone.utc)
    auth = _authorize(policy, expires_at, clock)
    if not auth.allowed or auth.expires_at is None:
        return _blocked(auth.reason)
    if not directory:
        return _blocked(
            "records directory is not configured; persistence blocked; live linkage blocked",
            auth.expires_at,
        )
    episode_id = str(payload.get("episode", {}).get("episode_id", ""))
    try:
        deadline = _deadline_for_write(directory, episode_id, auth.expires_at, clock, packet=payload)
        results = os.path.join(directory, "results")
        _ensure_dir(results)
        _rewrite_tree(directory, episode_id, deadline)
        clean = retention.rewrite_expires_at(retention.redact_identifying_contacts(payload), deadline)
        clean["expires_at"] = deadline
        name = _slug(episode_id or "result") + ".json"
        path = os.path.join(results, name)
        _write(path, json.dumps(clean, indent=2, sort_keys=True) + "\n")
    except retention.RetentionRejected as exc:
        return _blocked(str(exc))
    return {
        "persisted": True,
        "live_linkage": "blocked",
        "reason": auth.reason,
        "expires_at": deadline,
        "relative_path": os.path.join("results", name),
    }


def persist_bundle(
    directory: str,
    packet: dict,
    deadline: datetime,
    *,
    now: datetime,
    policy: object = None,
) -> dict:
    """Write the offline packet, result, query log, and research log together.

    Re-ingestion cannot move an episode's deadline later. A shorter deadline
    is written onto the new files and onto every earlier derivative for that
    episode. Symlinks are not followed. No identifying hash is written.
    """
    try:
        if not isinstance(deadline, datetime) or deadline.tzinfo is None or deadline.tzinfo.utcoffset(deadline) is None:
            raise retention.RetentionRejected(
                "naive expires_at is rejected; timezone was not inferred"
            )
        episode = str(packet.get("episode", {}).get("episode_id", ""))
        origin = None
        provenance = packet.get("provenance") if isinstance(packet.get("provenance"), dict) else {}
        origin = provenance.get("first_collected_at")
        if os.path.lexists(directory) and (
            os.path.islink(directory) or os.path.realpath(directory) != os.path.abspath(directory)
        ):
            raise retention.RetentionRejected("records directory is a symlink; refusing to follow it")
        if not os.path.isdir(directory):
            os.makedirs(directory, mode=0o700)
        _guard_directory(directory)
        with _store_lock(directory):
            iso = _deadline_for_write(
                directory, episode, deadline, now, packet=packet, policy=policy,
                first_collected_at=origin,
            )
            _ensure_dir(directory)
            _rewrite_tree(directory, episode, iso)
            clean = retention.rewrite_expires_at(
                retention.redact_identifying_contacts(packet),
                iso,
            )
            clean["expires_at"] = iso
            consistency = clean.get("consistency")
            if isinstance(consistency, dict):
                consistency["identity_proof"] = False
                consistency["verified_export"] = False
            slug = _slug(episode or "episode")
            packets = os.path.join(directory, "packets")
            results = os.path.join(directory, "results")
            _ensure_dir(packets)
            _ensure_dir(results)
            _write(
                os.path.join(packets, f"handoff-{slug}.json"),
                json.dumps(clean, indent=2, sort_keys=True) + "\n",
            )
            _write(
                os.path.join(results, f"{slug}.json"),
                json.dumps(clean, indent=2, sort_keys=True) + "\n",
            )
            log_line = {
                "episode_id": episode,
                "outcome": clean.get("consistency", {}).get("status"),
                "expires_at": iso,
                "adapter": "mock",
                "lane": clean.get("lane"),
                "live_linkage": "blocked",
            }
            if isinstance(origin, str):
                log_line["first_collected_at"] = origin
            _write(
                os.path.join(directory, "research-log.jsonl"),
                json.dumps(log_line, sort_keys=True) + "\n",
                append=True,
            )
            query_line = {
                "episode_id": episode,
                "expires_at": iso,
                "adapter": "mock",
                "kind": "query_log",
            }
            if isinstance(origin, str):
                query_line["first_collected_at"] = origin
            _write(
                os.path.join(directory, "query-log.jsonl"),
                json.dumps(query_line, sort_keys=True) + "\n",
                append=True,
            )
    except retention.RetentionRejected as exc:
        return {
            "persisted": False,
            "live_linkage": "blocked",
            "reason": str(exc),
            "expires_at": None,
        }
    return {
        "persisted": True,
        "live_linkage": "blocked",
        "expires_at": iso,
    }
