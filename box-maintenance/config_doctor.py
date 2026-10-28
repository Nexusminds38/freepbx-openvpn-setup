#!/usr/bin/env python3
"""Nexus Config Doctor — one paste, one true board. READ-ONLY.

Kills the "is it broken or is it me" class of evenings: gateway,
brain, catalog ghosts, crew, routing, bootstrap weight, sessions,
screen receiver, webhook, disk/RAM — every probe guarded, failures
printed as lines instead of crashes. Nothing here writes, sets,
restarts, or deletes anything.

Run on the box:  python3 box-maintenance/config_doctor.py
"""
import glob
import json
import subprocess
import urllib.request
from pathlib import Path

HOME = Path.home()
OC = HOME / ".openclaw"


def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return "ERR: %s" % e


def http(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "ERR: %s" % e


def section(title):
    print("\n== %s ==" % title)


def main():
    section("HOST")
    print(sh(["hostname"]).strip())
    print(sh(["nproc"]).strip() + " vCPU")
    print(sh(["free", "-m"]))
    print(sh(["df", "-h", "/"]))

    section("OLLAMA (the brain)")
    dropin = Path("/etc/systemd/system/ollama.service.d/speed-pack.conf")
    print("speed-pack drop-in: %s" % ("PRESENT" if dropin.exists()
                                     else "MISSING"))
    print(sh(["ollama", "list"]))
    print(sh(["ollama", "ps"]))
    status, body = http("http://127.0.0.1:11434/api/ps")
    print("/api/ps: %s %s" % (status, body[:500]))

    section("OPENCLAW GATEWAY")
    print(sh(["openclaw", "gateway", "status"])[:800])

    section("CATALOG vs REALITY (ghost check)")
    models = sh(["openclaw", "models", "list"])
    print(models[:1200])
    ghosts = [ln for ln in models.splitlines()
              if "qwen2.5" in ln.lower()]
    print("qwen2.5 ghost entries: %s" % (ghosts if ghosts else "none"))

    section("CREW + ROUTING")
    print(sh(["openclaw", "agents", "list"])[-1800:])
    for path in ("bindings",
                 "agents.defaults.maxConcurrent",
                 "agents.defaults.subagents.maxConcurrent",
                 "agents.defaults.timeoutSeconds",
                 "agents.defaults.compaction",
                 "agents.defaults.heartbeat"):
        out = sh(["openclaw", "config", "get", path]).strip()
        print("%s = %s" % (path, out[:400]))

    section("OLLAMA PROVIDER AUTH (value never printed)")
    out = sh(["openclaw", "config", "get",
              "models.providers.ollama.apiKey"]).strip()
    print("models.providers.ollama.apiKey: %s"
          % ("SET" if out and "ERR" not in out and "null" not in out
             else "MISSING — dashboard turns will fail auth"))

    section("BOOTSTRAP WEIGHT (prefill tax per fresh turn)")
    total_all = 0
    for ws in sorted(glob.glob(str(OC / "workspaces" / "*")) +
                     [str(OC / "workspace")]):
        parts = []
        total = 0
        for name in ("SOUL.md", "IDENTITY.md", "USER.md",
                     "MEMORY.md", "AGENTS.md"):
            p = Path(ws) / name
            if p.exists():
                n = p.stat().st_size
                total += n
                parts.append("%s %dB" % (name, n))
        if parts:
            total_all += total
            print("%s: %s | TOTAL %dB"
                  % (Path(ws).name, ", ".join(parts), total))
    print("all workspaces bootstrap total: %dB" % total_all)

    section("SESSIONS (history weight)")
    sess = glob.glob(str(OC / "agents" / "*" / "sessions" / "*"))
    size = 0
    for f in sess:
        try:
            size += Path(f).stat().st_size
        except Exception:
            pass
    print("session files: %d, total %d KB" % (len(sess), size // 1024))

    section("NEXUS SCREEN RECEIVER (from the box itself)")
    for url in ("http://100.70.158.8:18789/nexus/screen/health",):
        status, body = http(url)
        print("%s -> %s %s" % (url, status, body[:200]))

    section("FILEOPS WEBHOOK")
    status, body = http("http://127.0.0.1:8741/status")
    print("127.0.0.1:8741/status -> %s %s" % (status, body[:300]))
    print(sh(["systemctl", "is-active", "nexus-webhook.service"]).strip()
          + " / " + sh(["systemctl", "is-enabled",
                        "nexus-webhook.service"]).strip())

    print("\n== DOCTOR DONE — screenshot the whole board ==")


if __name__ == "__main__":
    main()
