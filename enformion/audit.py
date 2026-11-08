"""Local audit writes for an explicitly authorized offline copy.

Writes require an explicit retention policy, a timezone-aware unexpired
``expires_at``, and an explicit records directory. The historical default
under ``hidden_files`` is not opened. Identifying hashes are not stored.
Live linkage stays blocked. Missing policy writes nothing and deletes nothing.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import retention


def new_lookup_id() -> str:
    return uuid.uuid4().hex[:12]


def _write(path: str, text: str, *, append: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, text.encode("utf-8"))
    finally:
        os.close(descriptor)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _ensure_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


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
    slug = "".join(char if char.isalnum() else "-" for char in episode_id)
    parts = [part for part in slug.split("-") if part]
    return ("-".join(parts)[:140] or "episode")


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
    auth = _authorize(policy, expires_at, now)
    if not auth.allowed or auth.expires_at is None:
        return _blocked(auth.reason)
    if not directory:
        return _blocked(
            "records directory is not configured; persistence blocked; live linkage blocked",
            auth.expires_at,
        )
    _ensure_dir(directory)
    event = {
        "episode_id": episode_id,
        "outcome": outcome,
        "reason": reason,
        "adapter": adapter,
        "expires_at": auth.expires_at.isoformat(),
        "lane": "disconnected_mock",
        "live_linkage": "blocked",
    }
    _write(
        os.path.join(directory, "research-log.jsonl"),
        json.dumps(event, sort_keys=True) + "\n",
        append=True,
    )
    return {
        "persisted": True,
        "live_linkage": "blocked",
        "reason": auth.reason,
        "expires_at": auth.expires_at.isoformat(),
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
    auth = _authorize(policy, expires_at, now)
    if not auth.allowed or auth.expires_at is None:
        return _blocked(auth.reason)
    if not directory:
        return _blocked(
            "records directory is not configured; persistence blocked; live linkage blocked",
            auth.expires_at,
        )
    results = os.path.join(directory, "results")
    _ensure_dir(results)
    clean = retention.redact_identifying_contacts(payload)
    clean["expires_at"] = auth.expires_at.isoformat()
    name = _slug(str(clean.get("episode", {}).get("episode_id", "result"))) + ".json"
    path = os.path.join(results, name)
    _write(path, json.dumps(clean, indent=2, sort_keys=True) + "\n")
    return {
        "persisted": True,
        "live_linkage": "blocked",
        "reason": auth.reason,
        "expires_at": auth.expires_at.isoformat(),
        "relative_path": os.path.join("results", name),
    }


def persist_bundle(
    directory: str,
    packet: dict,
    deadline: datetime,
) -> dict:
    """Write the offline packet, result, query log, and research log together.

    Every file receives the same earliest deadline. No identifying hash is
    written. Relative-output files are not created.
    """
    clean = retention.redact_identifying_contacts(packet)
    clean["expires_at"] = deadline.isoformat()
    episode = str(clean.get("episode", {}).get("episode_id", "episode"))
    slug = _slug(episode)
    _ensure_dir(directory)
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
        "expires_at": deadline.isoformat(),
        "adapter": "mock",
        "lane": clean.get("lane"),
        "live_linkage": "blocked",
    }
    _write(
        os.path.join(directory, "research-log.jsonl"),
        json.dumps(log_line, sort_keys=True) + "\n",
        append=True,
    )
    _write(
        os.path.join(directory, "query-log.jsonl"),
        json.dumps(
            {
                "episode_id": episode,
                "expires_at": deadline.isoformat(),
                "adapter": "mock",
                "kind": "query_log",
            },
            sort_keys=True,
        )
        + "\n",
        append=True,
    )
    return {
        "persisted": True,
        "live_linkage": "blocked",
        "expires_at": deadline.isoformat(),
    }
