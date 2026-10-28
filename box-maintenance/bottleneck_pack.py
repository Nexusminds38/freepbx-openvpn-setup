#!/usr/bin/env python3
"""Nexus bottleneck pack — OpenClaw-side pile-up killers for a one-brain box.

Why (measured, 2026-10-27): the speed pack fixed the Ollama layer
(model resident 24h, flash attention, q8 KV, NUM_PARALLEL=1, warm-up
16s, first answer sub-minute). The layer ABOVE it was never inspected:
OpenClaw's own concurrency / timeout / heartbeat / routing defaults
decide how many turns hit that single CPU brain at once and what
happens when a slow turn looks "stuck". Defaults tuned for a cloud
API (fast, parallel) are wrong for one local 8B on CPU:

  1. maxConcurrent 4 (default class) -> up to 4 runs pile onto a
     NUM_PARALLEL=1 brain; every run looks hung, users retry, load
     doubles. THE loop. Fix: serialize at the gateway too.
  2. Short run timeouts -> a slow-but-working CPU turn gets killed
     and retried from scratch, paying the prefill twice. Fix: give a
     turn 15 minutes before anything calls it dead.
  3. Routing rules 0 -> every channel falls through to implicit
     default routing. Fix (v1): one explicit binding, webchat -> main,
     so the route HBK actually uses is deterministic and counted.
     Crew agents stay one dashboard pick away; per-agent channel
     bindings land when a second channel (Signal) is real.
  4. Compaction: if the config exposes it, keep it in safeguard mode
     so long sessions prune instead of re-feeding a 4k window.

Every step prints BEFORE -> AFTER with the command's own output, and
skips loudly when the config shape is not what the step expects —
nothing is forced. Heartbeat is INSPECTED and printed, never changed
here: it changes agent behavior, and that call is HBK's.

Undo: every value below is one `openclaw config set` away; the pack
prints the full before-state first so it can be restored by hand.
"""
import json
import subprocess
import sys
import time
import urllib.request

MODEL = "qwen3:8b"


def run(cmd, **kw):
    print("+ %s" % " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def oc_get(path):
    r = run(["openclaw", "config", "get", path])
    if r.returncode != 0:
        print("  (get %s failed: %s)" % (path, (r.stderr or "").strip()[:200]))
        return None
    out = (r.stdout or "").strip()
    print("  %s = %s" % (path, out[:600]))
    try:
        return json.loads(out)
    except Exception:
        return out


def oc_set(path, value):
    r = run(["openclaw", "config", "set", path, value])
    if r.returncode != 0:
        print("  !! set %s failed: %s" % (path, (r.stderr or "").strip()[:300]))
        return False
    print("  set %s -> %s" % (path, value))
    return True


def inspect():
    print("== INSPECT (before) ==")
    run(["nproc"])
    print(run(["free", "-m"]).stdout)
    oc_get("agents.defaults")
    oc_get("bindings")
    oc_get("channels")


def apply_concurrency():
    print("== CONCURRENCY: one brain, one turn at a time ==")
    oc_set("agents.defaults.maxConcurrent", "1")
    oc_get("agents.defaults.maxConcurrent")
    oc_set("agents.defaults.subagents.maxConcurrent", "1")
    oc_get("agents.defaults.subagents.maxConcurrent")


def apply_timeout():
    print("== TIMEOUT: slow is not stuck — no retry loops ==")
    oc_set("agents.defaults.timeoutSeconds", "900")
    oc_get("agents.defaults.timeoutSeconds")


def apply_compaction():
    print("== COMPACTION: prune history, protect the 4k window ==")
    defaults = oc_get("agents.defaults.compaction")
    if isinstance(defaults, dict):
        if defaults.get("mode") != "safeguard":
            oc_set("agents.defaults.compaction.mode", "safeguard")
            oc_get("agents.defaults.compaction")
        else:
            print("  already in safeguard mode — untouched")
    else:
        print("  compaction shape not exposed here — skipped loudly, "
              "config untouched")


def apply_bindings():
    print("== BINDINGS v1: webchat -> main, explicit and counted ==")
    current = oc_get("bindings")
    if isinstance(current, list) and len(current) > 0:
        print("  bindings already present (%d) — untouched; report "
              "this state" % len(current))
        return
    binding = [{"agentId": "main", "match": {"channel": "webchat"}}]
    if oc_set("bindings", json.dumps(binding)):
        oc_get("bindings")


def inspect_heartbeat():
    print("== HEARTBEAT (inspect only — HBK's call, not this pack's) ==")
    oc_get("agents.defaults.heartbeat")


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
        print("!! ollama did not answer on 11434 — warm-up skipped")
        return
    body = json.dumps({
        "model": MODEL, "prompt": "ok", "stream": False,
        "keep_alive": "24h", "options": {"num_predict": 1},
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            r.read()
        print("warm-up done in %.1fs — model loaded and pinned"
              % (time.time() - t0))
    except Exception as e:
        print("!! warm-up call failed: %s" % e)


def main():
    inspect()
    apply_concurrency()
    apply_timeout()
    apply_compaction()
    apply_bindings()
    inspect_heartbeat()
    print("== RESTART + WARM ==")
    r = run(["openclaw", "gateway", "restart"])
    if r.returncode != 0:
        print("!! gateway restart returned %d — run it manually"
              % r.returncode)
    warm()
    print(run(["ollama", "ps"]).stdout)
    r = run(["openclaw", "agents", "list"])
    print(r.stdout[-1500:] if r.stdout else "(agents list unavailable)")
    print("== RECEIPTS TO REPORT BACK ==")
    print("  1. agents.defaults.maxConcurrent reads 1")
    print("  2. agents.defaults.timeoutSeconds reads 900")
    print("  3. bindings shows the webchat -> main rule (agents list "
          "routing rules no longer 0 for main)")
    print("  4. the heartbeat block printed above (no change made)")
    print("  5. warm-up seconds + ollama ps UNTIL '24 hours from now'")


if __name__ == "__main__":
    sys.exit(main())
