"""View the Enformion research log — the kept track of every lookup.

Usage:
  python3 research_log.py [--last N] [--subject "First Last"] [--verified]

Metadata only: timestamps, subject queried, verdict, counts, and the kept
record's path. Contact PII is never printed here — it lives in the stored
result files (see audit.py) and comes out through run_lookup.py / packets.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import audit


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=20)
    ap.add_argument("--subject", default="")
    ap.add_argument("--verified", action="store_true")
    args = ap.parse_args()

    log_path = os.path.join(audit.records_dir(), "research-log.jsonl")
    if not os.path.exists(log_path):
        print("No lookups logged yet.")
        return 0

    events = []
    with open(log_path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    total = len(events)
    counts: dict[str, int] = {}
    for ev in events:
        counts[ev["outcome"]] = counts.get(ev["outcome"], 0) + 1

    if args.subject:
        first, _, last = args.subject.partition(" ")
        events = [e for e in events
                  if e["query"]["first"].lower() == first.lower()
                  and e["query"]["last"].lower() == last.lower()]
    if args.verified:
        events = [e for e in events if e["outcome"] == "VERIFIED"]
    events = events[-args.last:]

    summary = " · ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    print(f"Research log: {total} lookup(s) total — {summary}")
    for ev in events:
        q = ev["query"]
        line = (f"{ev['ts_local'] or ev['ts_utc']}  [{ev['lookup_id']}]  "
                f"{q['first']} {q['last']} — {q['city']}, {q['state']}, "
                f"age {q['age']}  → {ev['outcome']}")
        if ev["outcome"] == "VERIFIED":
            line += (f"  ({ev['relatives_returned']} relatives, "
                     f"{ev['phones_returned']} numbers; kept: {ev['result_file']})")
        elif ev.get("reason"):
            line += f"  ({ev['reason']})"
        if ev.get("source_ref"):
            line += f"  [source: {ev['source_ref']}]"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
