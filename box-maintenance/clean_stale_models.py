#!/usr/bin/env python3
"""Remove stale qwen2.5 model entries from ~/.openclaw/openclaw.json.

Only catalog entries go: dict keys naming qwen2.5, list items that are
qwen2.5 strings, and model dicts whose id/name/model is qwen2.5.
Agent model assignments are untouched (the crew runs qwen3:8b).
A timestamped backup of openclaw.json is written first, and the
cleaned JSON is re-parsed before it lands, so a bad edit cannot
replace the live config.
"""
import json
import shutil
import sys
import time
from pathlib import Path

CFG = Path.home() / ".openclaw" / "openclaw.json"
STALE = "qwen2.5"


def scrub(x):
    if isinstance(x, dict):
        return {k: scrub(v) for k, v in x.items() if STALE not in k}
    if isinstance(x, list):
        out = []
        for i in x:
            if isinstance(i, str) and STALE in i:
                continue
            if isinstance(i, dict) and any(
                STALE in str(i.get(f, "")) for f in ("id", "name", "model")
            ):
                continue
            out.append(scrub(i))
        return out
    return x


def main():
    raw = CFG.read_text()
    before = raw.count(STALE)
    if before == 0:
        print("no qwen2.5 references found - nothing to do")
        return
    data = json.loads(raw)
    bak = CFG.with_suffix(
        ".json.bak-stale-cleanup-" + time.strftime("%Y%m%d%H%M%S"))
    shutil.copy2(CFG, bak)
    cleaned = scrub(data)
    out = json.dumps(cleaned, indent=2)
    json.loads(out)  # prove valid before it lands
    CFG.write_text(out + "\n")
    after = CFG.read_text().count(STALE)
    print("backup: %s" % bak)
    print("qwen2.5 references: %d -> %d" % (before, after))
    if after != 0:
        print("WARNING: references remain - inspect manually",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
