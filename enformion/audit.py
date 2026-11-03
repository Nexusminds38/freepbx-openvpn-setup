"""Audit + records layer for the Enformion research lane (added 2026-10-17
on HBK's order: every research run logged, research + PII data kept track of).

Design (Omerta / Nexus PII doctrine):
- Append-only research log: one JSON line per lookup EVENT, recording when,
  what was queried, which adapter ran it, the verdict, and counts — enough
  to reconstruct who researched whom and what came back, forever auditable.
- PII result records are stored ONLY for VERIFIED lookups (verification
  doctrine: an unverified record yields no contacts — so there is no contact
  PII to keep). Each stored record is hashed (sha256) into the log line so
  later tampering or drift is visible.
- Everything lives locally under the goal's hidden_files area
  (default: ../../hidden_files/enformion-records relative to this file,
  override with $ENFORMION_RECORDS_DIR). Directory mode 700, files 600.
  Never in memory files, never in cloud notes.
- Credential values are never logged, stored, or printed here.
- Retention: kept until HBK orders destruction (shred policy).
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_DIR = os.path.normpath(
    os.path.join(_HERE, "..", "..", "hidden_files", "enformion-records")
)


def records_dir() -> str:
    path = os.environ.get("ENFORMION_RECORDS_DIR", _DEFAULT_DIR)
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    results = os.path.join(path, "results")
    os.makedirs(results, mode=0o700, exist_ok=True)
    try:
        os.chmod(results, 0o700)
    except OSError:
        pass
    return path


def _write_locked(path: str, text: str, append: bool = False) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else 0),
                 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def new_lookup_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> tuple[str, str]:
    utc = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        local = utc.astimezone(ZoneInfo("America/New_York"))
        local_s = local.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        local_s = ""
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ"), local_s


def store_result(lookup_id: str, payload: dict) -> tuple[str, str]:
    """Write the kept research record for one lookup.

    Returns (relative_path, sha256_hex). The payload may contain PII
    (verified contacts) — it is stored locally only, mode 600.
    """
    base = records_dir()
    rel = os.path.join("results", f"{lookup_id}.json")
    text = json.dumps(payload, indent=2, sort_keys=True)
    _write_locked(os.path.join(base, rel), text)
    return rel, hashlib.sha256(text.encode("utf-8")).hexdigest()


def log_event(*, lookup_id: str, adapter: str, query: dict, outcome: str,
              reason: str = "", source_ref: str = "",
              records_returned: int = 0, relatives_returned: int = 0,
              phones_returned: int = 0, result_file: str | None = None,
              result_sha256: str | None = None) -> dict:
    """Append one event line to the research log and return the event."""
    ts_utc, ts_local = _now()
    event = {
        "ts_utc": ts_utc,
        "ts_local": ts_local,
        "lookup_id": lookup_id,
        "adapter": adapter,
        "source_ref": source_ref,
        "query": query,
        "outcome": outcome,
        "reason": reason,
        "records_returned": records_returned,
        "relatives_returned": relatives_returned,
        "phones_returned": phones_returned,
        "result_file": result_file,
        "result_sha256": result_sha256,
    }
    log_path = os.path.join(records_dir(), "research-log.jsonl")
    _write_locked(log_path, json.dumps(event, sort_keys=True) + "\n", append=True)
    return event


def record_to_payload(record) -> dict:
    """Serialize a verified PersonRecord (with picked contacts) for storage."""
    return asdict(record)
