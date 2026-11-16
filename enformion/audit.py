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

import publish
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


def _publish_or_replace(directory: str, name: str, text: str) -> None:
    """Write one episode file. A new name appears only when the bytes are complete."""
    data = text.encode("utf-8")
    path = os.path.join(directory, name)
    if not os.path.lexists(path):
        publish.create_complete_file(directory, name, data)
        return
    publish.replace_exclusive_file(directory, name, data, publish.read_exclusive(path))


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
            if name.endswith(".replacing") or name.endswith(".cleanup-tmp"):
                continue
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                files.append(path)
    for path in files:
        if _record_kind(path) == "jsonl":
            _rewrite_jsonl(path, episode_id, deadline)
        elif _record_kind(path) == "json":
            _rewrite_json(path, episode_id, deadline)


def _json_episode_ids(node: object) -> set[str]:
    found: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            episode_id = value.get("episode_id")
            if isinstance(episode_id, str) and episode_id.strip():
                found.add(episode_id)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return found


def _line_episode_ids(data: object) -> set[str]:
    found: set[str] = set()
    if not isinstance(data, dict):
        return found
    episode_id = data.get("episode_id")
    if isinstance(episode_id, str) and episode_id.strip():
        found.add(episode_id)
    nested = data.get("episode")
    if isinstance(nested, dict):
        inner = nested.get("episode_id")
        if isinstance(inner, str) and inner.strip():
            found.add(inner)
    return found


def _rewrite_jsonl(path: str, episode_id: str, deadline: str) -> None:
    try:
        original = publish.read_exclusive(path)
    except (OSError, publish.PublishError) as exc:
        raise retention.RetentionRejected(
            "refusing to rewrite a linked or unreadable log"
        ) from exc
    raw_lines = original.decode("utf-8").splitlines(keepends=True)
    changed = False
    output: list[str] = []
    owners: set[str] = set()
    ownerless = False
    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            output.append(raw if raw.endswith("\n") else raw + "\n")
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise retention.RetentionRejected(
                "unreadable records file blocks persistence; refusing to rewrite a shared log"
            ) from exc
        ids = _line_episode_ids(data)
        if len(ids) != 1:
            ownerless = True
            output.append(raw if raw.endswith("\n") else raw + "\n")
            continue
        owners.add(next(iter(ids)))
        if next(iter(ids)) == episode_id:
            updated = retention.rewrite_expires_at(data, deadline)
            if updated != data:
                changed = True
            output.append(json.dumps(updated, sort_keys=True) + "\n")
        else:
            output.append(raw if raw.endswith("\n") else raw + "\n")
    if episode_id not in owners:
        return
    if ownerless or owners != {episode_id}:
        raise retention.RetentionRejected(
            "refusing to rewrite a log shared with another episode or an ownerless line"
        )
    if changed:
        publish.replace_exclusive_file(
            os.path.dirname(path), os.path.basename(path), "".join(output).encode(), original,
        )


def _rewrite_json(path: str, episode_id: str, deadline: str) -> None:
    try:
        original = publish.read_exclusive(path)
        data = json.loads(original.decode("utf-8"))
    except (OSError, json.JSONDecodeError, publish.PublishError) as exc:
        raise retention.RetentionRejected(
            "unreadable records file blocks persistence; refusing to ignore its deadline"
        ) from exc
    ids = _json_episode_ids(data)
    if episode_id not in ids:
        return
    if ids != {episode_id}:
        raise retention.RetentionRejected(
            "refusing to rewrite a JSON file shared by more than one episode"
        )
    updated = retention.rewrite_expires_at(data, deadline)
    if updated != data:
        publish.replace_exclusive_file(
            os.path.dirname(path),
            os.path.basename(path),
            (json.dumps(updated, indent=2, sort_keys=True) + "\n").encode(),
            original,
        )


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
        if not episode_id.strip():
            raise retention.RetentionRejected(
                "episode id is required; refusing a shared research log"
            )
        _guard_directory(directory)
        with _store_lock(directory):
            publish.recover_staged_replacements(directory)
            deadline = _deadline_for_write(directory, episode_id, auth.expires_at, clock)
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
            episode_dir = os.path.join(directory, "episodes", _slug(episode_id))
            _ensure_dir(episode_dir)
            _publish_or_replace(
                episode_dir, "research.jsonl", json.dumps(event, sort_keys=True) + "\n",
            )
    except (retention.RetentionRejected, publish.PublishError) as exc:
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
        if not episode_id.strip():
            raise retention.RetentionRejected(
                "episode id is required; refusing a shared result file"
            )
        _guard_directory(directory)
        with _store_lock(directory):
            publish.recover_staged_replacements(directory)
            deadline = _deadline_for_write(
                directory, episode_id, auth.expires_at, clock, packet=payload,
            )
            _rewrite_tree(directory, episode_id, deadline)
            clean = retention.rewrite_expires_at(retention.redact_identifying_contacts(payload), deadline)
            clean["expires_at"] = deadline
            results = os.path.join(directory, "results")
            _ensure_dir(results)
            name = _slug(episode_id) + ".json"
            _publish_or_replace(results, name, json.dumps(clean, indent=2, sort_keys=True) + "\n")
    except (retention.RetentionRejected, publish.PublishError) as exc:
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
    is written onto that episode's own files. A shared log is not rewritten.
    Symlinks are not followed. No identifying hash is written.
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
            publish.recover_staged_replacements(directory)
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
            episode_dir = os.path.join(directory, "episodes", slug)
            _ensure_dir(packets)
            _ensure_dir(results)
            _ensure_dir(episode_dir)
            _publish_or_replace(packets, f"handoff-{slug}.json", json.dumps(clean, indent=2, sort_keys=True) + "\n")
            _publish_or_replace(results, f"{slug}.json", json.dumps(clean, indent=2, sort_keys=True) + "\n")
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
            query_line = {
                "episode_id": episode,
                "expires_at": iso,
                "adapter": "mock",
                "kind": "query_log",
            }
            if isinstance(origin, str):
                query_line["first_collected_at"] = origin
            _publish_or_replace(episode_dir, "research.jsonl", json.dumps(log_line, sort_keys=True) + "\n")
            _publish_or_replace(episode_dir, "query.jsonl", json.dumps(query_line, sort_keys=True) + "\n")
    except (retention.RetentionRejected, publish.PublishError) as exc:
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
