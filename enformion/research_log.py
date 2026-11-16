"""View an explicitly configured offline research log.

This viewer does not create a records directory and does not open the
historical hidden_files default. Contact fields are not printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import retention


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
    events = []
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))]
        for name in sorted(filenames):
            folded = name.casefold()
            if folded.endswith(".replacing"):
                continue
            if folded not in {"research.jsonl", "research-log.jsonl"} and not retention._numbered_episode_log(folded, "research"):
                continue
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
    if not events:
        print("No lookups logged in the configured directory.")
        return 0
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
