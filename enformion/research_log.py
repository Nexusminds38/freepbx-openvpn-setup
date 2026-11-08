"""View an explicitly configured offline research log.

This viewer does not create a records directory and does not open the
historical hidden_files default. Contact fields are not printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show the offline research log.")
    parser.add_argument("--last", type=int, default=20)
    args = parser.parse_args(argv)

    directory = os.environ.get("ENFORMION_RECORDS_DIR")
    if not directory:
        print(
            "Research log unavailable: no records directory is configured. "
            "No default store was opened."
        )
        return 0
    log_path = os.path.join(directory, "research-log.jsonl")
    if not os.path.isfile(log_path):
        print("No lookups logged in the configured directory.")
        return 0

    events = []
    with open(log_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    shown = events[-args.last:]
    print(f"Research log: {len(events)} event(s) in the configured directory.")
    for event in shown:
        episode = event.get("episode_id") or event.get("lookup_id") or ""
        outcome = event.get("outcome", "")
        expires = event.get("expires_at")
        line = f"{episode}  →  {outcome}"
        if expires:
            line += f"  expires {expires}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
