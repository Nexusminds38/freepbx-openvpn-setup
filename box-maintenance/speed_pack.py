#!/usr/bin/env python3
"""Nexus box speed pack (free CPU-turn levers for the Ollama crew brain).

1) Ollama service env via a systemd drop-in (reversible: delete the
   drop-in, daemon-reload, restart ollama):
     OLLAMA_KEEP_ALIVE=24h      model stays loaded - no cold reloads
     OLLAMA_FLASH_ATTENTION=1   faster prompt read, less RAM
     OLLAMA_KV_CACHE_TYPE=q8_0  half-size KV cache on a tight box
     OLLAMA_NUM_PARALLEL=1      one brain, one turn at a time, no thrash
2) OpenClaw config: cap the qwen3:8b catalog contextWindow at 16384
   (backup first, JSON re-validated before it lands) so sessions stop
   budgeting for a 200k workspace the CPU cannot feed.
3) Restart ollama + gateway, warm the model with a 1-token call so
   the first real turn is not the cold one.
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
CFG = HOME / ".openclaw" / "openclaw.json"
DROPIN_DIR = Path("/etc/systemd/system/ollama.service.d")
DROPIN = DROPIN_DIR / "speed-pack.conf"
DROPIN_BODY = """[Service]
Environment="OLLAMA_KEEP_ALIVE=24h"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_NUM_PARALLEL=1"
"""
CTX_CAP = 16384


def run(cmd, **kw):
    print("+ %s" % " ".join(cmd))
    return subprocess.run(cmd, **kw)


def ollama_unit_exists():
    r = subprocess.run(["systemctl", "list-unit-files"],
                       capture_output=True, text=True)
    return "ollama.service" in (r.stdout or "")


def apply_env():
    if not ollama_unit_exists():
        print("!! no ollama.service unit found - env step skipped; "
              "report this line")
        return False
    run(["sudo", "mkdir", "-p", str(DROPIN_DIR)], check=True)
    p = subprocess.run(["sudo", "tee", str(DROPIN)],
                       input=DROPIN_BODY, text=True,
                       capture_output=True)
    if p.returncode != 0:
        print("!! could not write drop-in: %s" % p.stderr.strip())
        return False
    print("drop-in written: %s" % DROPIN)
    run(["sudo", "systemctl", "daemon-reload"], check=True)
    run(["sudo", "systemctl", "restart", "ollama"], check=True)
    return True


def cap_context():
    raw = CFG.read_text()
    data = json.loads(raw)
    hits = []

    def walk(x):
        if isinstance(x, dict):
            if str(x.get("id", "")).endswith("qwen3:8b") \
                    and "contextWindow" in x:
                hits.append(x["contextWindow"])
                x["contextWindow"] = CTX_CAP
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)

    walk(data)
    if not hits:
        print("context cap: no qwen3:8b contextWindow key found - "
              "config untouched")
        return
    bak = CFG.with_suffix(
        ".json.bak-speedpack-" + time.strftime("%Y%m%d%H%M%S"))
    shutil.copy2(CFG, bak)
    out = json.dumps(data, indent=2)
    json.loads(out)
    CFG.write_text(out + "\n")
    print("context cap: contextWindow %s -> %d (backup: %s)"
          % (hits, CTX_CAP, bak))


def wait_ollama(upto=40):
    for _ in range(upto):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:11434/api/tags", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    return False


def warm():
    if not wait_ollama():
        print("!! ollama did not answer on 11434 - warm-up skipped")
        return
    body = json.dumps({
        "model": "qwen3:8b", "prompt": "ok", "stream": False,
        "keep_alive": "24h", "options": {"num_predict": 1},
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            r.read()
        print("warm-up done in %.1fs - model loaded and pinned"
              % (time.time() - t0))
    except Exception as e:
        print("!! warm-up call failed: %s" % e)


def main():
    env_ok = apply_env()
    cap_context()
    r = run(["openclaw", "gateway", "restart"])
    if r.returncode != 0:
        print("!! gateway restart returned %d - run it manually"
              % r.returncode)
    warm()
    run(["ollama", "ps"])
    print("speed pack applied (env drop-in: %s)"
          % ("yes" if env_ok else "SKIPPED - see above"))
    print("undo env: sudo rm %s && sudo systemctl daemon-reload "
          "&& sudo systemctl restart ollama" % DROPIN)


if __name__ == "__main__":
    main()
