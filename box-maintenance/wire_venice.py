#!/usr/bin/env python3
"""Wire Venice AI as the Nexus crew's API brain in ~/.openclaw/openclaw.json.

- Prompts for the Venice API key with hidden input. The key is written
  only into the config file (chmod 600 after) - never printed, never
  passed on a command line, never logged.
- Adds models.providers.venice (OpenAI-compatible chat completions at
  https://api.venice.ai/api/v1) with qwen3-235b-a22b-instruct-2507.
- Heals the local lane too: sets the ollama provider's dummy apiKey
  ("ollama") so the local fallback stops failing missing-provider-auth.
- Points agents.defaults.model and every agent entry whose model
  references ollama/ at venice/qwen3-235b-a22b-instruct-2507, printing
  each switch. The local qwen3:8b catalog entry stays - fallback is a
  config change away.
- Timestamped backup first; JSON re-validated before it lands;
  gateway restarted at the end.
"""
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CFG = Path.home() / ".openclaw" / "openclaw.json"
MODEL_ID = "qwen3-235b-a22b-instruct-2507"
REF = "venice/" + MODEL_ID
BASE_URL = "https://api.venice.ai/api/v1"


def set_model_field(holder, where, changes):
    """holder is a dict that may carry a 'model' field (str or dict)."""
    if not isinstance(holder, dict) or "model" not in holder:
        return
    cur = holder["model"]
    if isinstance(cur, str):
        if cur != REF:
            changes.append("%s: %s -> %s" % (where, cur, REF))
            holder["model"] = REF
    elif isinstance(cur, dict):
        old = cur.get("primary")
        if old != REF:
            changes.append("%s.primary: %s -> %s" % (where, old, REF))
            cur["primary"] = REF


def main():
    key = getpass.getpass("Venice API key (hidden, stored only in "
                          "openclaw.json): ").strip()
    if not key:
        print("no key entered - aborting, config untouched")
        return 1

    data = json.loads(CFG.read_text())
    bak = CFG.with_suffix(
        ".json.bak-venice-" + time.strftime("%Y%m%d%H%M%S"))
    shutil.copy2(CFG, bak)
    print("backup: %s" % bak)

    models = data.setdefault("models", {})
    providers = models.setdefault("providers", {})

    venice = providers.get("venice") or {}
    venice.update({
        "baseUrl": BASE_URL,
        "apiKey": key,
        "api": "openai-completions",
        "models": [{
            "id": MODEL_ID,
            "name": "Venice Qwen3 235B Instruct",
            "contextWindow": 131072,
        }],
    })
    providers["venice"] = venice
    print("provider added: venice (%s, model %s)" % (BASE_URL, MODEL_ID))

    ollama = providers.get("ollama")
    if isinstance(ollama, dict):
        if ollama.get("apiKey") != "ollama":
            ollama["apiKey"] = "ollama"
            print("local lane healed: ollama provider dummy apiKey set")
    else:
        print("note: no ollama provider block found - local heal skipped")

    changes = []
    agents = data.get("agents", {})
    set_model_field(agents.get("defaults", {}), "agents.defaults", changes)
    for i, entry in enumerate(agents.get("list", []) or []):
        if not isinstance(entry, dict):
            continue
        cur = entry.get("model")
        cur_s = cur if isinstance(cur, str) else (
            cur.get("primary") if isinstance(cur, dict) else None)
        if cur_s and str(cur_s).startswith("ollama/"):
            set_model_field(
                entry, "agents.list[%s]" % entry.get("id", i), changes)
    for c in changes:
        print("switched", c)
    if not changes:
        print("note: no agent model fields needed switching "
              "(check agents list output)")

    out = json.dumps(data, indent=2)
    json.loads(out)
    CFG.write_text(out + "\n")
    os.chmod(CFG, 0o600)
    print("config written (mode 600 - it now holds a real key)")

    r = subprocess.run(["openclaw", "gateway", "restart"])
    if r.returncode != 0:
        print("!! gateway restart returned %d - run it manually"
              % r.returncode)
        return 1
    print("done - verify with: openclaw models list")
    return 0


if __name__ == "__main__":
    sys.exit(main())
